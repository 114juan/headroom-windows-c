"""Read every account's usage windows WITHOUT consuming an inference window.

Claude: the same OAuth usage endpoint the Claude Code UI uses
(``/api/oauth/usage``), authenticated with the account's existing login token.
The response is bound to the account by comparing the organization id the
provider returns against the identity bound inside that slot's config home —
a clobbered or swapped login can never report another account's headroom.

Codex: the Codex CLI writes ``rate_limits`` telemetry into its session logs on
every turn. We read the newest event from disk. No network, no tokens spent.

Fail-closed rules:
  * an account with unverifiable identity or an out-of-range reading is HELD
    (ok=false) rather than guessed at;
  * a 429 from the usage endpoint sets a provider-wide backoff ledger honoured
    by later runs;
  * snapshots are written atomically, and a sanitized public projection is
    derived for the dashboard (optionally with emails redacted).
"""
import base64
import email.utils
import fcntl
import glob
import hashlib
import json
import math
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

from . import paths, registry

IDENTITY_TIMEOUT = int(os.environ.get("HEADROOM_IDENTITY_TIMEOUT", "15"))
CODEX_STALE_AFTER = int(os.environ.get("HEADROOM_CODEX_STALE_AFTER", "1800"))
SCHEMA_VERSION = 1

PUBLIC_FIELDS = {
    "name", "email", "provider", "plan", "ok", "note", "error_code", "retry_at",
    "captured_at", "source", "stale", "windows", "identity_verified",
    "identity_method", "trust_state", "routable", "subscription",
}


class IdentityBindingError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class ProviderThrottleError(RuntimeError):
    def __init__(self, retry_at, provider_response=False):
        self.retry_at = int(retry_at)
        self.provider_response = provider_response
        super().__init__("usage_source_rate_limited")


def iso_ep(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except (TypeError, ValueError):
        return None


def fingerprint(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:16]


def retry_after_epoch(headers, now=None):
    now = int(time.time()) if now is None else int(now)
    raw = (headers.get("retry-after") or headers.get("Retry-After")) if headers else None
    if raw:
        try:
            return now + max(1, int(float(raw)))
        except (TypeError, ValueError, OverflowError):
            try:
                parsed = email.utils.parsedate_to_datetime(raw)
                return max(now + 1, int(parsed.timestamp()))
            except (TypeError, ValueError, OverflowError):
                pass
    return now + 300


# ---------------------------------------------------------------- identity

def decode_jwt_payload(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * ((4 - len(payload) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("invalid local identity token") from error


def claude_local_identity(home):
    """Identity bound inside the slot from local metadata only (no network)."""
    metadata = paths.load_json(os.path.join(home, ".claude.json")) or {}
    oauth = metadata.get("oauthAccount") or {}
    email_address = oauth.get("emailAddress")
    org = oauth.get("organizationUuid")
    if not email_address or not org:
        raise IdentityBindingError("claude_local_binding_missing")
    return {
        "verified": False,
        "email": email_address,
        "account_fingerprint": fingerprint(org),
        "method": "claude_local_metadata",
        "plan_type": None,
    }


def claude_plan(home):
    credentials = paths.load_json(os.path.join(home, ".credentials.json")) or {}
    oauth = credentials.get("claudeAiOauth") or {}
    tier = str(oauth.get("rateLimitTier") or "").lower()
    if "max_20x" in tier:
        return "Max 20x"
    if "max_5x" in tier:
        return "Max 5x"
    subscription = str(oauth.get("subscriptionType") or "").lower()
    return {"max": "Max", "pro": "Pro", "free": "Free"}.get(subscription)


def claude_bin():
    return shutil.which("claude")


def claude_identity(home, runner=subprocess.run):
    """Provider-verified identity via `claude auth status`; local fallback."""
    binary = claude_bin()
    if binary:
        env = os.environ.copy()
        env["CLAUDE_CONFIG_DIR"] = home
        try:
            process = runner(
                [binary, "auth", "status", "--json"], env=env,
                capture_output=True, text=True, timeout=IDENTITY_TIMEOUT,
            )
            if process.returncode == 0:
                status = json.loads(process.stdout)
                if status.get("loggedIn"):
                    return {
                        "verified": True,
                        "email": status.get("email"),
                        "account_fingerprint": fingerprint(status.get("orgId")),
                        "method": "claude_auth_status",
                        "plan_type": status.get("subscriptionType"),
                    }
        except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
            pass
    return claude_local_identity(home)


def codex_identity(home, opener=urllib.request.urlopen):
    auth = paths.load_json(os.path.join(home, "auth.json"))
    if not auth:
        raise IdentityBindingError("codex_auth_missing")
    tokens = auth.get("tokens") or {}
    claims = decode_jwt_payload(tokens.get("id_token"))
    provider_claims = claims.get("https://api.openai.com/auth") or {}
    record = {
        "verified": False,
        "email": claims.get("email"),
        "account_fingerprint": fingerprint(
            provider_claims.get("chatgpt_account_id") or claims.get("sub")
        ),
        "method": "openai_local_id_token",
        "plan_type": provider_claims.get("chatgpt_plan_type"),
        "subscription": codex_subscription(provider_claims),
    }
    try:
        request = urllib.request.Request(
            "https://auth.openai.com/oauth/userinfo",
            headers={"authorization": "Bearer " + tokens["access_token"]},
        )
        with opener(request, timeout=IDENTITY_TIMEOUT) as response:
            userinfo = json.load(response)
        if userinfo.get("sub") == claims.get("sub"):
            record["verified"] = True
            record["email"] = userinfo.get("email") or record["email"]
            record["method"] = "openai_userinfo"
    except (OSError, KeyError, ValueError, urllib.error.URLError):
        pass  # identity stays local-only; usage still reported, trust reduced
    if not record["email"]:
        raise IdentityBindingError("codex_identity_email_missing")
    return record


def codex_subscription(provider_claims, now=None):
    now = int(time.time()) if now is None else int(now)
    active_until = iso_ep(provider_claims.get("chatgpt_subscription_active_until"))
    checked_at = iso_ep(provider_claims.get("chatgpt_subscription_last_checked"))
    if (active_until is None or checked_at is None or checked_at > now + 300
            or active_until <= checked_at):
        return {"status": "unknown", "source": "provider_not_exposed"}
    return {
        "status": "active_through",
        "active_until": active_until,
        "checked_at": checked_at,
        "source": "openai_id_token_claim",
    }


# ------------------------------------------------------------------ limits

def limit_entry(limit, minutes):
    percent = limit.get("percent")
    if percent is not None:
        percent = float(percent)
        if not 0 <= percent <= 100:
            raise ValueError(f"usage percentage out of range: {percent}")
    return {
        "used_percent": None if percent is None else round(percent, 1),
        "resets_at": iso_ep(limit.get("resets_at")),
        "severity": limit.get("severity"),
        "is_active": limit.get("is_active"),
        "window_minutes": minutes,
    }


def claude_limits(home, expected_fingerprint, opener=urllib.request.urlopen):
    credentials = paths.load_json(os.path.join(home, ".credentials.json")) or {}
    oauth = credentials.get("claudeAiOauth") or {}
    if not oauth.get("accessToken"):
        raise IdentityBindingError("claude_credentials_missing")
    request = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            "authorization": "Bearer " + oauth["accessToken"],
            "anthropic-beta": "oauth-2025-04-20",
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        response = opener(request, timeout=30)
    except urllib.error.HTTPError as error:
        if error.code == 429:
            raise ProviderThrottleError(
                retry_after_epoch(error.headers), provider_response=True
            ) from error
        raise
    with response:
        response_org = response.headers.get("anthropic-organization-id")
        response_fingerprint = fingerprint(response_org) if response_org else None
        # The usage org can legitimately differ from the login's default org
        # (multi-org accounts), so binding is trust-on-first-use per slot:
        # the caller pins this fingerprint and holds the slot if it CHANGES.
        if (expected_fingerprint and response_fingerprint
                and response_fingerprint != expected_fingerprint):
            raise IdentityBindingError("claude_usage_org_changed")
        data = json.load(response)
    session = weekly = None
    scoped = {}
    for limit in data.get("limits") or []:
        kind = limit.get("kind")
        if kind == "session":
            session = limit_entry(limit, 300)
        elif kind == "weekly_all":
            weekly = limit_entry(limit, 10080)
        elif kind == "weekly_scoped":
            name = (((limit.get("scope") or {}).get("model") or {})
                    .get("display_name")) or "Scoped"
            scoped[name] = limit_entry(limit, 10080)
    if session is None and isinstance(data.get("five_hour"), dict) \
            and data["five_hour"].get("utilization") is not None:
        session = {"used_percent": round(float(data["five_hour"]["utilization"]), 1),
                   "resets_at": iso_ep(data["five_hour"].get("resets_at")),
                   "window_minutes": 300}
    if weekly is None and isinstance(data.get("seven_day"), dict) \
            and data["seven_day"].get("utilization") is not None:
        weekly = {"used_percent": round(float(data["seven_day"]["utilization"]), 1),
                  "resets_at": iso_ep(data["seven_day"].get("resets_at")),
                  "window_minutes": 10080}
    windows = {"5h": session, "7d": weekly}
    for name, window in scoped.items():
        windows["scoped:" + name] = window
    return {
        "captured_at": int(time.time()),
        "source": "anthropic_usage_api",
        "source_identity_fingerprint": response_fingerprint,
        "stale": False,
        "windows": windows,
    }


def _find_rate_limits(value):
    if isinstance(value, dict):
        limits = value.get("rate_limits")
        if isinstance(limits, dict):
            return limits
        for child in value.values():
            found = _find_rate_limits(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_rate_limits(child)
            if found:
                return found
    return None


def codex_limits(home, now=None):
    now = time.time() if now is None else now
    files = glob.glob(os.path.join(home, "sessions", "2*", "*", "*", "*.jsonl"))
    if not files:
        return {"note": "no Codex telemetry yet — run one Codex turn on this account"}
    files.sort(key=os.path.getmtime, reverse=True)
    newest = None
    for path in files[:40]:
        file_mtime = int(os.path.getmtime(path))
        try:
            with open(path, errors="ignore") as handle:
                for line_number, line in enumerate(handle):
                    if '"rate_limits"' not in line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    limits = _find_rate_limits(event)
                    if not limits:
                        continue
                    captured_at = iso_ep(event.get("timestamp")) or file_mtime
                    if captured_at > now + 300:
                        captured_at = file_mtime
                    order = (captured_at, file_mtime, path, line_number)
                    if newest is None or order > newest[0]:
                        newest = (order, captured_at, limits)
        except OSError:
            continue
    if newest is None:
        return {"note": "no rate_limits event in recent Codex sessions"}
    _, captured_at, limits = newest
    stale = (now - captured_at) > CODEX_STALE_AFTER

    def window(key):
        value = limits.get(key) or {}
        used = value.get("used_percent")
        if used is not None:
            used = float(used)
            if not 0 <= used <= 100:
                raise ValueError(f"Codex {key} percentage out of range: {used}")
        reset = iso_ep(value.get("resets_at"))
        result = {
            "used_percent": used,
            "window_minutes": value.get("window_minutes"),
            "resets_at": reset,
            "observed_at": captured_at,
        }
        if stale and reset is not None and reset <= now:
            result["last_observed_used_percent"] = used
            result["used_percent"] = None
            result["freshness"] = "expired_observation"
        else:
            result["freshness"] = "stale_observation" if stale else "fresh"
        return result

    return {
        "captured_at": captured_at,
        "source": "codex_session_telemetry",
        "stale": stale,
        "windows": {"5h": window("primary"), "7d": window("secondary")},
        "plan_type": limits.get("plan_type"),
    }


# ---------------------------------------------------------------- snapshot

def validate_required_windows(windows):
    for key in ("5h", "7d"):
        window = windows.get(key)
        if not isinstance(window, dict):
            raise ValueError(f"missing required {key} usage window")
        if window.get("used_percent") is None \
                and window.get("freshness") != "expired_observation":
            raise ValueError(f"missing required {key} usage window")
        if window.get("freshness") == "expired_observation":
            continue
        percent = window["used_percent"]
        if not isinstance(percent, (int, float)) or not 0 <= percent <= 100:
            raise ValueError(f"invalid {key} usage percentage")


def empty_backoff():
    return {"schema_version": 1, "providers": {}}


def active_backoff(document, provider, now):
    if not isinstance(document, dict):
        return 0
    entry = (document.get("providers") or {}).get(provider) or {}
    retry_at = entry.get("retry_at", 0)
    if not isinstance(retry_at, (int, float)) or isinstance(retry_at, bool) \
            or not math.isfinite(retry_at):
        return 0
    return int(retry_at) if retry_at > now else 0


def apply_integrity(accounts):
    """Trust states + duplicate-identity detection across the fleet."""
    fingerprints = {}
    warnings = []
    for result in accounts:
        identity = result.get("identity") or {}
        if not result.get("ok"):
            result["trust_state"] = "held"
        elif result.get("stale"):
            result["trust_state"] = "stale_observation"
        elif identity.get("verified"):
            result["trust_state"] = "verified"
        else:
            result["trust_state"] = "verified_local"
        result["routable"] = result["trust_state"] in ("verified", "verified_local")

        key = (result.get("provider"), identity.get("account_fingerprint"))
        if key[1]:
            if key in fingerprints:
                other = fingerprints[key]
                for account in (other, result):
                    account["trust_state"] = "duplicate_identity"
                    account["routable"] = False
                warnings.append(
                    f"duplicate {key[0]} identity: {other['name']} and "
                    f"{result['name']} are the same login; routing held"
                )
            else:
                fingerprints[key] = result
    return warnings


def collect(accounts, backoff=None, persist_backoff=None):
    now = int(time.time())
    backoff = empty_backoff() if backoff is None else backoff
    claude_backoff_until = active_backoff(backoff, "anthropic_usage_api", now)
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "run_id": uuid.uuid4().hex,
        "run_started": now,
        "generated": None,
        "generated_iso": None,
        "accounts": [],
    }
    for account in accounts:
        result = {"name": account["name"], "provider": account["provider"]}
        try:
            if account["provider"] == "claude":
                identity = claude_identity(account["home"])
                result["identity"] = identity
                result["identity_verified"] = identity["verified"]
                result["identity_method"] = identity["method"]
                result["email"] = identity["email"]
                result["plan"] = claude_plan(account["home"]) or "Unknown"
                result["subscription"] = {"status": "unknown",
                                          "source": "provider_not_exposed"}
                expected = account.get("expected_email")
                if expected and identity["email"] \
                        and identity["email"].lower() != expected.lower():
                    raise IdentityBindingError("slot_bound_to_unexpected_email")
                if claude_backoff_until > now:
                    raise ProviderThrottleError(claude_backoff_until)
                result.update(claude_limits(account["home"],
                                            account.get("pinned_usage_org")))
                if not account.get("pinned_usage_org") \
                        and result.get("source_identity_fingerprint"):
                    # trust-on-first-use: remember which org this slot's
                    # usage feed belongs to; a later change means the login
                    # underneath was swapped and the slot must be held
                    result["pin_usage_org"] = result["source_identity_fingerprint"]
                validate_required_windows(result["windows"])
                result["ok"] = True
            else:
                identity = codex_identity(account["home"])
                result["identity"] = identity
                result["identity_verified"] = identity["verified"]
                result["identity_method"] = identity["method"]
                result["email"] = identity["email"]
                result["subscription"] = identity.get("subscription")
                expected = account.get("expected_email")
                if expected and identity["email"].lower() != expected.lower():
                    raise IdentityBindingError("slot_bound_to_unexpected_email")
                telemetry = codex_limits(account["home"], now=now)
                plan_type = str(telemetry.pop("plan_type", None)
                                or identity.get("plan_type") or "")
                result["plan"] = {"pro": "ChatGPT Pro", "plus": "ChatGPT Plus",
                                  "prolite": "ChatGPT Pro Lite",
                                  "free": "Free"}.get(plan_type,
                                                      plan_type or "Unknown")
                result.update(telemetry)
                if "windows" in result:
                    validate_required_windows(result["windows"])
                    result["ok"] = True
                else:
                    result["ok"] = False
        except ProviderThrottleError as error:
            claude_backoff_until = max(claude_backoff_until, error.retry_at)
            if error.provider_response and persist_backoff is not None:
                persist_backoff(claude_backoff_until)
            result["ok"] = False
            result["error_code"] = "usage_source_rate_limited"
            result["retry_at"] = error.retry_at
            result["note"] = ("usage source temporarily rate-limited; "
                              "account held until provider retry window")
        except IdentityBindingError as error:
            result["ok"] = False
            result["error_code"] = error.code
            result["note"] = ("identity could not be bound to this slot; "
                              "account held — run `headroom connect` to re-login")
        except Exception as error:  # noqa: BLE001 — every account must report
            result["ok"] = False
            result["error"] = type(error).__name__ + ": " + str(error)[:120]
            result["note"] = result["error"]
        snapshot["accounts"].append(result)
    snapshot["integrity_warnings"] = apply_integrity(snapshot["accounts"])
    completed = int(time.time())
    snapshot["generated"] = completed
    snapshot["generated_iso"] = datetime.fromtimestamp(
        completed, timezone.utc
    ).isoformat().replace("+00:00", "Z")
    return snapshot


def redact_email(address):
    if not address or "@" not in address:
        return address
    local, _, domain = address.partition("@")
    return (local[0] if local else "") + "***@" + domain


def public_snapshot(snapshot, redact_emails=False):
    accounts = []
    for account in snapshot["accounts"]:
        public = {k: v for k, v in account.items() if k in PUBLIC_FIELDS}
        if account.get("error") and not public.get("note"):
            public["note"] = "collector error; see private snapshot"
        if redact_emails:
            public["email"] = redact_email(public.get("email"))
        accounts.append(public)
    return {
        "schema_version": snapshot["schema_version"],
        "run_id": snapshot["run_id"],
        "generated": snapshot["generated"],
        "generated_iso": snapshot["generated_iso"],
        "integrity_warnings": snapshot.get("integrity_warnings", []),
        "accounts": accounts,
    }


def run_collect(quiet=False):
    """Full collect run: lock, read, write both snapshots. Returns snapshot."""
    config = registry.load()
    lock_path = paths.collect_lock_path()
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if not quiet:
                print("collector already running; skipped")
            return paths.load_json(paths.private_snapshot_path())
        backoff = paths.load_json(paths.backoff_path()) or empty_backoff()

        def persist(retry_at):
            backoff.setdefault("providers", {})["anthropic_usage_api"] = {
                "retry_at": int(retry_at),
                "observed_at": min(int(time.time()), int(retry_at) - 1),
            }
            paths.write_json_atomic(paths.backoff_path(), backoff)

        snapshot = collect(registry.accounts(config), backoff, persist)
        pins = {a["name"]: a.pop("pin_usage_org")
                for a in snapshot["accounts"] if a.get("pin_usage_org")}
        if pins:
            for entry in config["accounts"]:
                if entry["name"] in pins and not entry.get("pinned_usage_org"):
                    entry["pinned_usage_org"] = pins[entry["name"]]
            registry.save(config)
        if any(a.get("provider") == "claude" and a.get("ok")
               for a in snapshot["accounts"]) \
                and not any(a.get("error_code") == "usage_source_rate_limited"
                            for a in snapshot["accounts"]):
            (backoff.get("providers") or {}).pop("anthropic_usage_api", None)
            paths.write_json_atomic(paths.backoff_path(), backoff)
        paths.write_json_atomic(paths.private_snapshot_path(), snapshot)
        settings = registry.dashboard_settings(config)
        paths.write_json_atomic(
            paths.public_snapshot_path(),
            public_snapshot(snapshot, settings.get("redact_emails", False)),
            mode=0o644,
        )
        if not quiet:
            print_snapshot(snapshot)
        return snapshot


def display_percent(window):
    if not window or window.get("used_percent") is None:
        return "-"
    return "%d%%" % round(window["used_percent"])


def print_snapshot(snapshot):
    for account in snapshot["accounts"]:
        windows = account.get("windows") or {}
        scoped = " ".join(
            "%s=%s" % (key.split(":", 1)[1], display_percent(windows[key]))
            for key in windows if key.startswith("scoped:")
        )
        if account.get("ok"):
            print("%-16s %-14s 5h=%-5s 7d=%-5s %s%s" % (
                account["name"], account.get("plan", ""),
                display_percent(windows.get("5h")),
                display_percent(windows.get("7d")),
                scoped, " STALE" if account.get("stale") else ""))
        else:
            print("%-16s HELD: %s" % (
                account["name"],
                account.get("note") or account.get("error") or "unknown"))
    for warning in snapshot.get("integrity_warnings", []):
        print("WARNING", warning)
