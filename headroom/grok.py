"""Grok Build / SuperGrok credential + weekly-usage parsing.

The Grok CLI stores OIDC tokens in ``$GROK_HOME/auth.json`` (default
``~/.grok/auth.json``) and reads remaining SuperGrok allowance from the same
CLI-proxy billing feed it uses for ``/usage``. Parsing lives here so collect
can stay fail-closed: a missing or unreadable payload never becomes a guessed
percentage.
"""
import math
from datetime import datetime, timezone


OIDC_SCOPE_PREFIX = "https://auth.x.ai::"
LEGACY_SESSION_SCOPE = "https://accounts.x.ai/sign-in"
BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
SETTINGS_URL = "https://cli-chat-proxy.grok.com/v1/settings"
TOKEN_URL = "https://auth.x.ai/oauth2/token"
AUTH_HEADER = "xai-grok-cli"


def select_auth_entry(root):
    """Pick the SuperGrok OIDC record, else a legacy session record.

    ``auth.json`` is a map keyed by OIDC scope URL. Empty/partial OIDC
    entries must not shadow a healthy legacy token.
    """
    if not isinstance(root, dict):
        return None, None
    oidc = None
    legacy = None
    for scope, entry in root.items():
        if not isinstance(scope, str) or not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if not isinstance(key, str) or not key:
            continue
        if scope.startswith(OIDC_SCOPE_PREFIX):
            oidc = (scope, entry)
        elif scope == LEGACY_SESSION_SCOPE or "/sign-in" in scope:
            legacy = (scope, entry)
    return oidc or legacy or (None, None)


def money_val(value):
    """CLI-proxy amounts are ``{"val": <number>}``; also accept a bare number."""
    if isinstance(value, dict):
        value = value.get("val")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def _finite_percent(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return min(100.0, max(0.0, float(value)))


def parse_credits(payload):
    """Return ``{used_percent, resets_at, extra_percent, subscription_tier}``.

    ``used_percent`` is the SuperGrok weekly pool. ``extra_percent`` is Extra
    Usage Credits when an on-demand cap is present. ``None`` means unparseable.
    """
    if not isinstance(payload, dict):
        return None
    config = payload.get("config")
    if not isinstance(config, dict):
        config = payload
    used = _finite_percent(config.get("creditUsagePercent"))
    if used is None:
        cap = money_val(config.get("onDemandCap"))
        spent = money_val(config.get("onDemandUsed"))
        if cap is not None and cap > 0 and spent is not None:
            used = _finite_percent(spent / cap * 100)
    period = config.get("currentPeriod")
    period_end = period.get("end") if isinstance(period, dict) else None
    reset_raw = period_end or config.get("billingPeriodEnd") \
        or payload.get("billingPeriodEnd")
    extra = None
    extra_cap = money_val(config.get("onDemandCap"))
    extra_spent = money_val(config.get("onDemandUsed"))
    if extra_cap is not None and extra_cap > 0 and extra_spent is not None:
        extra = _finite_percent(extra_spent / extra_cap * 100)
    tier = config.get("subscriptionTier") or payload.get("subscriptionTier")
    tier = tier if isinstance(tier, str) and tier.strip() else None
    if used is None:
        # A current period with no percent is zero usage, not unknown.
        if period_end or config.get("billingPeriodEnd"):
            used = 0.0
        else:
            return None
    return {
        "used_percent": used,
        "resets_at": reset_raw,
        "extra_percent": extra,
        "subscription_tier": tier,
    }


def plan_label(settings, billing_tier=None, auth_mode=None):
    """Human plan name. Settings ``subscription_tier_display`` wins."""
    if isinstance(settings, dict):
        display = settings.get("subscription_tier_display")
        if isinstance(display, str) and display.strip():
            return display.strip()
    raw = billing_tier if isinstance(billing_tier, str) else ""
    compact = raw.strip().lower().replace(" ", "_").replace("-", "_")
    named = {
        "supergrok_heavy": "SuperGrok Heavy",
        "super_grok_heavy": "SuperGrok Heavy",
        "heavy": "SuperGrok Heavy",
        "supergrok": "SuperGrok",
        "super_grok": "SuperGrok",
        "premium_plus": "X Premium+",
        "premiumplus": "X Premium+",
    }.get(compact)
    if named:
        return named
    if raw.strip():
        return raw.strip()
    if str(auth_mode or "").lower() == "oidc":
        return "SuperGrok"
    return "Grok"


def na_session_window():
    """Grok has no 5-hour session window; keep the slot shape, mark N/A."""
    return {
        "used_percent": None,
        "resets_at": None,
        "window_minutes": 300,
        "freshness": "not_applicable",
    }


def weekly_window(used_percent, resets_at):
    return {
        "used_percent": round(float(used_percent), 1),
        "resets_at": resets_at,
        "window_minutes": 10080,
        "freshness": "fresh",
    }


def extra_window(used_percent, resets_at):
    return {
        "used_percent": round(float(used_percent), 1),
        "resets_at": resets_at,
        "window_minutes": 10080,
        "freshness": "fresh",
    }


def windows_from_credits(credits, resets_at):
    windows = {
        "5h": na_session_window(),
        "7d": weekly_window(credits["used_percent"], resets_at),
    }
    extra = credits.get("extra_percent")
    if extra is not None:
        windows["scoped:Extra"] = extra_window(extra, resets_at)
    return windows


def principal_is_team(entry):
    value = str((entry or {}).get("principal_type") or "").strip().lower()
    return value == "team"


def fingerprint_id(entry):
    """Stable quota owner: user id for personal, team id for team principals."""
    if not isinstance(entry, dict):
        return None
    if principal_is_team(entry):
        ident = entry.get("team_id") or entry.get("user_id") \
            or entry.get("principal_id")
    else:
        ident = entry.get("user_id") or entry.get("principal_id")
    if not isinstance(ident, str) or not ident:
        return None
    return ident


def expires_at_iso(now, expires_in):
    if not isinstance(expires_in, (int, float)) or isinstance(expires_in, bool) \
            or expires_in <= 0:
        expires_in = 3600
    stamp = datetime.fromtimestamp(now + int(expires_in), timezone.utc)
    return stamp.isoformat().replace("+00:00", "Z")
