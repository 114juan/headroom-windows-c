"""Read every account's usage windows WITHOUT consuming an inference window.

Claude: the same OAuth usage endpoint the Claude Code UI uses
(``/api/oauth/usage``), authenticated with the account's existing login token.
The response is bound to the account by comparing the organization id the
provider returns against the identity bound inside that slot's config home —
a clobbered or swapped login can never report another account's headroom.

Codex: read live from the Codex app-server (``codex app-server`` ->
``account/rateLimits/read`` + ``account/read``), identity-bound to each slot's
CODEX_HOME. Falls back to on-disk ``rate_limits`` session telemetry only when
the app-server is unavailable (older Codex CLI). No inference tokens spent.

Grok: SuperGrok weekly pool from the Grok CLI-proxy billing feed
(``/v1/billing?format=credits``), authenticated with the slot's
``$GROK_HOME/auth.json`` token. Identity is the OIDC user/team id bound in
that file. No inference tokens spent.

Antigravity (AGY): the Google account's Code Assist quota summary
(``cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary``) plus
``:loadCodeAssist`` for the plan, authenticated with the file-backed OAuth
token inside that slot's home. Identity is the ``id_token`` subject (or the
Google userinfo endpoint when the login stored no id_token). No inference
tokens spent.

Fail-closed rules:
  * an account with unverifiable identity or an out-of-range reading is HELD
    (ok=false) rather than guessed at;
  * a 429 from the Claude usage endpoint binds to THAT slot's token only
    (verified live: sibling tokens answer 200 in the same second), so just
    that slot is held until its Retry-After and a per-account backoff ledger
    keeps later runs from re-hitting it; a Grok 429 still sets a
    provider-wide backoff;
  * snapshots are written atomically, and a sanitized public projection is
    derived for the dashboard (optionally with emails redacted).
"""
import base64
import email.utils
from . import fcntl_compat as fcntl
import glob
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

from . import agy as agy_provider
from . import grok as grok_provider
from . import paths, registry

IDENTITY_TIMEOUT = int(os.environ.get("HEADROOM_IDENTITY_TIMEOUT", "15"))
CODEX_STALE_AFTER = int(os.environ.get("HEADROOM_CODEX_STALE_AFTER", "1800"))
SCHEMA_VERSION = 1

# Claude Code's public PKCE client. Used only to refresh an already-granted
# token; it cannot mint a new login.
CLAUDE_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_TOKEN_URLS = (
    "https://platform.claude.com/v1/oauth/token",
    "https://console.anthropic.com/v1/oauth/token",
)
ACCESS_REFRESH_SKEW = 300  # refresh when access dies in < 5 min
# Refresh tokens can die *before* the access token (seen ~3h vs ~8h).
# Collect cadence is 5–15 min, so this window must be wider than that or
# we miss the last chance to rotate. After a successful refresh we write a
# new refreshTokenExpiresAt so this does not fire every collect.
REFRESH_PROACTIVE_SKEW = 3600
DEFAULT_ACCESS_TTL = 28800
DEFAULT_REFRESH_TTL = 10800  # 3h guess when the server rotates but omits expiry
CLAUDE_UA = "claude-cli/2.1.257 (external, cli)"
# Same endpoint + beta flag the Claude Code CLI's own ``fetchUtilization``
# hits (GET /api/oauth/usage). Verified against claude-cli 2.1.257.
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

# Operator should re-login these slots (`headroom connect <name>`).
RECONNECT_CODES = frozenset({
    "claude_local_binding_missing",
    "claude_credentials_missing",
    "claude_refresh_expired",
    "slot_bound_to_unexpected_email",
    "codex_auth_missing",
    "codex_identity_email_missing",
    "codex_reauth_required",
    "grok_auth_missing",
    "grok_identity_email_missing",
    "grok_refresh_expired",
    "grok_team_usage_unsupported",
    "agy_auth_missing",
    "agy_identity_email_missing",
    "agy_refresh_expired",
})

PUBLIC_FIELDS = {
    "name", "email", "provider", "plan", "ok", "note", "error_code", "retry_at",
    "captured_at", "source", "stale", "windows", "identity_verified",
    "identity_method", "trust_state", "routable", "subscription",
}


# Which usage feed a provider throttle belongs to, for the backoff ledger.
THROTTLE_SOURCE = {
    "claude": "anthropic_usage_api",
    "codex": "openai_app_server",
    "grok": "grok_billing_api",
    "agy": "google_code_assist_api",
}


class IdentityBindingError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class ProviderThrottleError(RuntimeError):
    """``scope`` is "account" when the throttle binds to one slot's token
    (Claude's ``/api/oauth/usage`` 429) or "provider" when every slot of
    that provider must wait (Grok billing feed)."""

    def __init__(self, retry_at, provider_response=False, scope="provider"):
        self.retry_at = int(retry_at)
        self.provider_response = provider_response
        self.scope = scope
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
    if not value:  # never mint a valid-looking fingerprint from a missing id
        raise IdentityBindingError("identity_id_missing")
    return hashlib.sha256(str(value).encode()).hexdigest()[:16]


# Auth-override variables that would silently redirect a provider CLI or API
# call to a different account/provider than the slot we selected (see
# anthropics/claude-code#16238). Scrubbed from every subprocess/env we build.
# Covers direct keys/tokens, alternate-provider selectors (Bedrock/Vertex),
# their credentials and base URLs, and Codex's API-key / agent-identity paths.
AUTH_OVERRIDE_VARS = (
    # Anthropic direct
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    # Claude Code alternate providers — these reroute Claude off the OAuth slot
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
    "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_VERTEX_BASE_URL",
    "AWS_PROFILE", "AWS_BEARER_TOKEN_BEDROCK", "AWS_REGION",
    "CLOUD_ML_REGION", "ANTHROPIC_VERTEX_PROJECT_ID", "GOOGLE_APPLICATION_CREDENTIALS",
    # OpenAI / Codex
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY", "CODEX_AGENT_IDENTITY",
    # Grok / xAI — an API key in the parent env must not override a slot's
    # SuperGrok OAuth session.
    "XAI_API_KEY", "GROK_OAUTH_TOKEN",
    # Google / Antigravity — an API key or a redirected Code Assist backend
    # must not override the slot's Antigravity OAuth session.
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_CLOUD_PROJECT", "GEMINI_BASE_URL", "CLOUD_CODE_URL",
    "AGY_ADC_AUTH",
)


def scrubbed_env(base=None):
    env = dict(os.environ if base is None else base)
    for var in AUTH_OVERRIDE_VARS:
        env.pop(var, None)
    return env


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Authenticated requests never follow redirects — a redirect would
    forward the bearer token to whatever origin the response names."""

    def redirect_request(self, *args, **kwargs):
        return None


_no_redirect_opener = urllib.request.build_opener(_NoRedirect)


def open_authenticated(request, timeout):
    return _no_redirect_opener.open(request, timeout=timeout)


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
        "account_fingerprint": fingerprint(f"{org}:{email_address}"),
        "method": "claude_local_metadata",
        "plan_type": None,
    }


def credential_digest(provider, home):
    """A digest of the ACTUAL token the provider CLI will use — the Claude
    `.credentials.json` accessToken or the Codex `auth.json` access_token.
    Binding to this (not just the identity metadata) closes the split-token
    TOCTOU: swapping only the credential file changes this digest even if the
    identity metadata still names the old account."""
    try:
        if provider == "claude":
            oauth = (paths.load_json(os.path.join(home, ".credentials.json"))
                     or {}).get("claudeAiOauth") or {}
            token = oauth.get("accessToken")
        elif provider == "grok":
            _scope, entry = grok_provider.select_auth_entry(
                paths.load_json(os.path.join(home, "auth.json")) or {})
            token = (entry or {}).get("key")
        elif provider == "agy":
            _path, creds = agy_credential_file(home)
            token = agy_provider.access_token(creds)
        else:
            token = ((paths.load_json(os.path.join(home, "auth.json")) or {})
                     .get("tokens") or {}).get("access_token")
        return hashlib.sha256(token.encode()).hexdigest()[:16] if token else None
    except (OSError, ValueError, AttributeError):
        return None


def local_binding(provider, home):
    """(identity_fingerprint, credential_digest) currently bound in the slot,
    from local files only (no network). The router compares BOTH against the
    snapshot to detect a home re-logged into a different account/token."""
    try:
        if provider == "claude":
            fp = claude_local_identity(home)["account_fingerprint"]
        elif provider == "grok":
            fp = grok_identity(home)["account_fingerprint"]
        elif provider == "agy":
            fp = agy_local_identity(home)["account_fingerprint"]
        else:
            auth = paths.load_json(os.path.join(home, "auth.json")) or {}
            claims = decode_jwt_payload((auth.get("tokens") or {}).get("id_token"))
            provider_claims = claims.get("https://api.openai.com/auth") or {}
            fp = fingerprint(provider_claims.get("chatgpt_account_id")
                             or claims.get("sub"))
    except (IdentityBindingError, ValueError, KeyError, OSError):
        fp = None
    return fp, credential_digest(provider, home)


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
    """Local metadata first. The CLI is a last resort and is never spawned
    when the slot has no credentials (a dead home used to cost 15s)."""
    try:
        return claude_local_identity(home)
    except IdentityBindingError:
        pass
    creds = os.path.join(home, ".credentials.json")
    if not os.path.isfile(creds):
        raise IdentityBindingError("claude_local_binding_missing")
    binary = claude_bin()
    if binary:
        env = scrubbed_env()
        env["CLAUDE_CONFIG_DIR"] = home
        try:
            cmd_args, use_shell = paths.prepare_subprocess(
                [binary, "auth", "status", "--json"])
            process = runner(
                cmd_args, env=env,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=IDENTITY_TIMEOUT, shell=use_shell
            )
            if process.returncode == 0:
                status = json.loads(process.stdout)
                if status.get("loggedIn"):
                    return {
                        "verified": True,
                        "email": status.get("email"),
                        "account_fingerprint": fingerprint(
                            f"{status.get('orgId')}:{status.get('email')}"),
                        "method": "claude_auth_status",
                        "plan_type": status.get("subscriptionType"),
                    }
        except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
            pass
    return claude_local_identity(home)


def codex_bin():
    return shutil.which("codex")


def codex_app_server_read(home, timeout=None):
    """Live Codex read via the codex app-server (`codex app-server`, JSON-RPC
    over stdio): real-time rate limits AND the network-verified logged-in
    account, both bound to this slot's CODEX_HOME. This replaces stale
    session-log scraping — Codex usage becomes as live as Claude's.

    Returns {"account": {...email, planType...}, "rate_limits": {...}} or
    raises IdentityBindingError."""
    import threading
    timeout = int(os.environ.get("HEADROOM_CODEX_APPSERVER_TIMEOUT", "25")) \
        if timeout is None else timeout
    binary = codex_bin()
    if not binary:
        raise IdentityBindingError("codex_cli_missing")
    env = scrubbed_env()
    env["CODEX_HOME"] = home
    try:
        cmd_args, use_shell = paths.prepare_subprocess([binary, "app-server"])
        proc = subprocess.Popen(
            cmd_args, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            encoding="utf-8", errors="replace",
            env=env, bufsize=1, shell=use_shell)
    except OSError as error:
        raise IdentityBindingError("codex_app_server_spawn_failed") from error
    stdin, stdout = proc.stdin, proc.stdout
    if stdin is None or stdout is None:
        raise IdentityBindingError("codex_app_server_spawn_failed")
    responses = {}

    def reader():
        try:
            for line in stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    continue
                if isinstance(message, dict) and "id" in message:
                    responses[message["id"]] = message
        except Exception:
            pass

    threading.Thread(target=reader, daemon=True).start()

    def send(obj):
        stdin.write(json.dumps(obj) + "\n")
        stdin.flush()

    deadline = time.time() + timeout
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "headroom", "version": "0.1"}}})
        while 1 not in responses and time.time() < deadline:
            time.sleep(0.05)
        send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        send({"jsonrpc": "2.0", "id": 2,
              "method": "account/rateLimits/read", "params": {}})
        send({"jsonrpc": "2.0", "id": 3, "method": "account/read", "params": {}})
        while (2 not in responses or 3 not in responses) \
                and time.time() < deadline:
            time.sleep(0.05)
    except (OSError, ValueError):
        raise IdentityBindingError("codex_app_server_io_failed")
    finally:
        try:
            stdin.close()
        except OSError:
            pass
        try:
            stdout.close()
        except OSError:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except (subprocess.SubprocessError, OSError):
            try:
                proc.kill()
            except OSError:
                pass
    if 2 not in responses or 3 not in responses:
        raise IdentityBindingError("codex_app_server_no_response")
    for request_id in (2, 3):
        if not isinstance(responses[request_id], dict) or responses[request_id].get("error"):
            raise IdentityBindingError("codex_app_server_error")
    account_res = responses[3].get("result")
    account = account_res.get("account") if isinstance(account_res, dict) else None
    if not isinstance(account, dict):
        account = {}
    result = responses[2].get("result")
    if not isinstance(result, dict):
        result = {}
    # Prefer the canonical per-limit bucket; fall back to the backward-compatible
    # single-bucket view. Both carry primary/secondary RateLimitWindow objects.
    by_id = result.get("rateLimitsByLimitId") or {}
    rate_limits = by_id.get("codex") or result.get("rateLimits") or {}
    return {"account": account, "rate_limits": rate_limits}


def codex_window(window, now):
    """Map an app-server RateLimitWindow to a headroom usage window (live)."""
    if not isinstance(window, dict):
        return None
    used = window.get("usedPercent")
    if not isinstance(used, (int, float)) or isinstance(used, bool) \
            or not 0 <= used <= 100:
        return None
    return {
        "used_percent": float(used),
        "resets_at": iso_ep(window.get("resetsAt")),
        "window_minutes": window.get("windowDurationMins"),
        "observed_at": now,
        "freshness": "fresh",
    }


# The app-server reports each rate-limit window by its actual duration and OMITS
# any window that is not currently a constraint: a freshly reset 5-hour window at
# ~0% comes back as a null secondary, and the "primary" slot can then hold the
# weekly window instead. So we must NOT assume primary==5h / secondary==7d.
CODEX_STANDARD_WINDOWS = {300: "5h", 10080: "7d"}


def codex_windows(rate_limits, now):
    """Build headroom's 5h/7d windows from an app-server rate-limits payload,
    robust to the server reordering or omitting windows.

    Windows are bucketed by their real ``windowDurationMins`` rather than their
    primary/secondary position. A standard window the server left out is treated
    as fully available (0% used): a binding window is always reported, so an
    absent one means that limit is not currently a constraint."""
    if not isinstance(rate_limits, dict):
        rate_limits = {}
    buckets = {}
    for slot in ("primary", "secondary"):
        mapped = codex_window(rate_limits.get(slot), now)
        if mapped is None:
            continue
        key = CODEX_STANDARD_WINDOWS.get(mapped.get("window_minutes"))
        if key and key not in buckets:
            buckets[key] = mapped

    def available(minutes):
        return {"used_percent": 0.0, "resets_at": None,
                "window_minutes": minutes, "observed_at": now,
                "freshness": "fresh"}
    return {
        "5h": buckets.get("5h") or available(300),
        "7d": buckets.get("7d") or available(10080),
    }


def codex_live(home, expected_email=None, now=None):
    """Full live Codex read: network-verified identity + real-time windows.
    account_fingerprint/credential come from the local id token (stable);
    email/plan/usage come live from the app-server."""
    now = int(time.time()) if now is None else now
    auth = paths.load_json(os.path.join(home, "auth.json"))
    if not auth:
        raise IdentityBindingError("codex_auth_missing")
    claims = decode_jwt_payload((auth.get("tokens") or {}).get("id_token"))
    provider_claims = claims.get("https://api.openai.com/auth") or {}
    account_id = provider_claims.get("chatgpt_account_id") or claims.get("sub")
    read = codex_app_server_read(home)
    account = read["account"]
    email = account.get("email") or claims.get("email")
    if not email:
        raise IdentityBindingError("codex_identity_email_missing")
    if expected_email and email.lower() != expected_email.lower():
        raise IdentityBindingError("slot_bound_to_unexpected_email")
    plan_type = account.get("planType") or provider_claims.get("chatgpt_plan_type")
    rate_limits = read["rate_limits"]
    identity = {
        "verified": True,
        "email": email,
        "account_fingerprint": fingerprint(account_id),
        "method": "codex_app_server",
        "plan_type": plan_type,
        "credential_digest": credential_digest("codex", home),
        "subscription": codex_subscription(provider_claims),
    }
    windows = codex_windows(rate_limits, now)
    return identity, plan_type, windows


def codex_identity(home, opener=open_authenticated):
    auth = paths.load_json(os.path.join(home, "auth.json"))
    if not auth:
        raise IdentityBindingError("codex_auth_missing")
    tokens = auth.get("tokens") or {}
    claims = decode_jwt_payload(tokens.get("id_token"))
    # An expired id_token still names the right identity (Codex refreshes
    # access tokens separately) — it lowers trust to local-only rather than
    # holding the slot, and the userinfo call below can re-verify live.
    expires = claims.get("exp")
    token_stale = isinstance(expires, (int, float)) \
        and expires < time.time() - 300
    provider_claims = claims.get("https://api.openai.com/auth") or {}
    record = {
        "verified": False,
        "email": claims.get("email"),
        "account_fingerprint": fingerprint(
            provider_claims.get("chatgpt_account_id") or claims.get("sub")
        ),
        "method": "openai_local_id_token_expired" if token_stale
                  else "openai_local_id_token",
        "plan_type": provider_claims.get("chatgpt_plan_type"),
        "subscription": codex_subscription(provider_claims),
    }
    try:
        request = urllib.request.Request(
            "https://auth.openai.com/oauth/userinfo",
            headers={"authorization": "Bearer " + tokens["access_token"]},
        )
        with opener(request, timeout=IDENTITY_TIMEOUT) as response:
            try:
                userinfo = json.load(response)
            except (json.JSONDecodeError, ValueError) as json_error:
                raise ValueError("malformed userinfo payload") from json_error
        if isinstance(userinfo, dict) and userinfo.get("sub") == claims.get("sub"):
            record["verified"] = True
            record["email"] = userinfo.get("email") or record["email"]
            record["method"] = "openai_userinfo"
    except urllib.error.HTTPError as error:
        error.close()
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


def grok_auth_record(home):
    """Load the preferred SuperGrok/legacy entry from ``home/auth.json``."""
    auth = paths.load_json(os.path.join(home, "auth.json"))
    scope, entry = grok_provider.select_auth_entry(auth or {})
    if entry is None:
        raise IdentityBindingError("grok_auth_missing")
    return scope, entry, auth


def grok_identity(home):
    """Identity bound in the slot from local Grok auth.json only (no network)."""
    _scope, entry, _auth = grok_auth_record(home)
    email = entry.get("email")
    if not isinstance(email, str) or not email:
        raise IdentityBindingError("grok_identity_email_missing")
    ident = grok_provider.fingerprint_id(entry)
    if not ident:
        raise IdentityBindingError("identity_id_missing")
    return {
        "verified": False,
        "email": email,
        "account_fingerprint": fingerprint(ident),
        "method": "grok_local_auth",
        "plan_type": grok_provider.plan_label(None, None, entry.get("auth_mode")),
        "credential_digest": credential_digest("grok", home),
        "principal_type": entry.get("principal_type"),
    }


def _grok_headers(token):
    return {
        "authorization": "Bearer " + token,
        "x-xai-token-auth": grok_provider.AUTH_HEADER,
        "accept": "application/json",
        "user-agent": "headroom",
    }


def grok_token_near_expiry(entry, now=None):
    now = time.time() if now is None else now
    expires = iso_ep(entry.get("expires_at"))
    if expires is None:
        return False
    return expires < now + ACCESS_REFRESH_SKEW


def refresh_grok_token(home, opener=None, now=None):
    """Refresh the SuperGrok OIDC access token. Never spawns ``grok``.

    Returns True when auth.json was rewritten. Raises
    IdentityBindingError('grok_refresh_expired') when the refresh token is
    missing or the server rejects it. Returns False on a transient failure
    so a still-valid access token can be used.
    """
    scope, entry, auth = grok_auth_record(home)
    refresh = entry.get("refresh_token")
    client_id = entry.get("oidc_client_id")
    if not isinstance(refresh, str) or not refresh:
        raise IdentityBindingError("grok_refresh_expired")
    if not isinstance(client_id, str) or not client_id:
        raise IdentityBindingError("grok_refresh_expired")
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": client_id,
    }).encode("utf-8")
    opener = open_authenticated if opener is None else opener
    request = urllib.request.Request(
        grok_provider.TOKEN_URL, data=body, method="POST",
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "user-agent": "headroom",
            "accept": "application/json",
        },
    )
    try:
        response = opener(request, timeout=15)
    except urllib.error.HTTPError as error:
        with error:
            if error.code in (400, 401):
                raise IdentityBindingError("grok_refresh_expired") from error
            return False
    except (OSError, urllib.error.URLError):
        return False
    with response:
        try:
            data = json.load(response)
        except (json.JSONDecodeError, ValueError, TypeError):
            return False
    if not isinstance(data, dict) or not data.get("access_token"):
        return False
    now = time.time() if now is None else now
    rewritten = dict(entry)
    rewritten["key"] = data["access_token"]
    if data.get("refresh_token"):
        rewritten["refresh_token"] = data["refresh_token"]
    rewritten["expires_at"] = grok_provider.expires_at_iso(
        now, data.get("expires_in"))
    auth = dict(auth)
    auth[scope] = rewritten
    try:
        paths.write_json_atomic(os.path.join(home, "auth.json"), auth, mode=0o600)
    except OSError as error:
        raise IdentityBindingError("grok_refresh_expired") from error
    return True


def _grok_json_get(url, token, opener, timeout):
    request = urllib.request.Request(url, headers=_grok_headers(token))
    try:
        response = opener(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        with error:
            body = b""
            try:
                body = error.read()[:400]
            except OSError:
                pass
            return error.code, body, None
    except (OSError, urllib.error.URLError):
        return None, b"", None
    with response:
        try:
            payload = json.load(response)
        except (json.JSONDecodeError, ValueError, TypeError):
            return getattr(response, "status", 200), b"", None
        return getattr(response, "status", 200), b"", payload


def grok_limits(home, expected_email=None, opener=None, now=None):
    """Live SuperGrok weekly pool + optional Extra credits. No inference."""
    now = int(time.time() if now is None else now)
    opener = open_authenticated if opener is None else opener
    scope, entry, _auth = grok_auth_record(home)
    if grok_token_near_expiry(entry, now=now):
        if refresh_grok_token(home, opener=opener, now=now):
            scope, entry, _auth = grok_auth_record(home)
    token = entry.get("key")
    if not isinstance(token, str) or not token:
        raise IdentityBindingError("grok_auth_missing")
    status, _body, payload = _grok_json_get(
        grok_provider.BILLING_URL, token, opener, 15)
    if status == 401:
        if refresh_grok_token(home, opener=opener, now=now):
            scope, entry, _auth = grok_auth_record(home)
            token = entry.get("key")
            status, _body, payload = _grok_json_get(
                grok_provider.BILLING_URL, token, opener, 15)
        else:
            raise IdentityBindingError("grok_refresh_expired")
    if status == 429:
        raise ProviderThrottleError(now + 300, provider_response=True)
    if status in (403, 404) and grok_provider.principal_is_team(entry):
        raise IdentityBindingError("grok_team_usage_unsupported")
    if status != 200 or payload is None:
        if grok_provider.principal_is_team(entry):
            raise IdentityBindingError("grok_team_usage_unsupported")
        raise IdentityBindingError("grok_billing_unavailable")
    credits = grok_provider.parse_credits(payload)
    if credits is None:
        raise ValueError("malformed grok billing payload")
    settings = None
    _st, _sb, settings_payload = _grok_json_get(
        grok_provider.SETTINGS_URL, token, opener, 3)
    if _st == 200 and isinstance(settings_payload, dict):
        settings = settings_payload
    identity = grok_identity(home)
    if expected_email and identity["email"].lower() != expected_email.lower():
        raise IdentityBindingError("slot_bound_to_unexpected_email")
    identity["verified"] = True
    identity["method"] = "grok_billing_api"
    identity["credential_digest"] = credential_digest("grok", home)
    identity["plan_type"] = grok_provider.plan_label(
        settings, credits.get("subscription_tier"), entry.get("auth_mode"))
    windows = grok_provider.windows_from_credits(
        credits, iso_ep(credits.get("resets_at")))
    return identity, identity["plan_type"], windows



# --------------------------------------------------------------- antigravity

def agy_credential_file(home):
    """(path, credentials) for the Antigravity login in a slot.

    ``agy`` prefers the OS keyring and only writes a token file where no
    keyring is available. headroom checks file-backed tokens first, falling
    back to the Windows Credential Manager when adopting the desktop user home
    (see docs/KNOWN-LIMITS.md).
    """
    for relative in agy_provider.CREDENTIAL_FILES:
        path = os.path.join(home, *relative.split("/"))
        creds = agy_provider.select_credentials(paths.load_json(path))
        if creds is not None:
            return path, creds
    if os.name == "nt":
        user_home = os.path.expanduser("~")
        is_user_home = (
            os.path.normcase(os.path.abspath(home)) ==
            os.path.normcase(os.path.abspath(user_home))
        )
        if is_user_home:
            keyring_creds = agy_provider.read_windows_keyring()
            if keyring_creds is not None and agy_provider.access_token(keyring_creds):
                cache_path = os.path.join(home, ".gemini", "oauth_creds.json")
                if os.path.exists(cache_path):
                    existing = paths.load_json(cache_path) or {}
                    if existing.get("email"):
                        keyring_creds["email"] = existing["email"]
                try:
                    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                    paths.write_json_atomic(cache_path, keyring_creds, mode=0o600)
                    return cache_path, keyring_creds
                except Exception:
                    return "keyring:gemini:antigravity", keyring_creds
    raise IdentityBindingError("agy_auth_missing")


def agy_local_identity(home):
    """Identity bound in the slot from the local token only (no network)."""
    _path, creds = agy_credential_file(home)
    claims = {}
    raw = agy_provider.id_token(creds)
    if raw:
        try:
            claims = decode_jwt_payload(raw)
        except ValueError:
            claims = {}
    email_address = claims.get("email") or creds.get("email")
    if not isinstance(email_address, str) or not email_address:
        raise IdentityBindingError("agy_identity_email_missing")
    return {
        "verified": False,
        "email": email_address,
        "account_fingerprint": fingerprint(claims.get("sub") or email_address),
        "method": "agy_local_token",
        "plan_type": None,
        "credential_digest": credential_digest("agy", home),
    }


def _agy_headers(token):
    return {
        "authorization": "Bearer " + token,
        "accept": "application/json",
        "user-agent": "Antigravity",
    }


def _agy_post(url, token, body, opener, timeout):
    """(status, payload). A status of None means the request never landed."""
    opener = open_authenticated if opener is None else opener
    headers = _agy_headers(token)
    headers["content-type"] = "application/json"
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers=headers)
    try:
        response = opener(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        with error:
            return error.code, None
    except (OSError, urllib.error.URLError):
        return None, None
    with response:
        try:
            return getattr(response, "status", 200), json.load(response)
        except (json.JSONDecodeError, ValueError, TypeError):
            return getattr(response, "status", 200), None


def _agy_get(url, token, opener, timeout):
    opener = open_authenticated if opener is None else opener
    request = urllib.request.Request(url, headers=_agy_headers(token))
    try:
        response = opener(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        with error:
            return error.code, None
    except (OSError, urllib.error.URLError):
        return None, None
    with response:
        try:
            return getattr(response, "status", 200), json.load(response)
        except (json.JSONDecodeError, ValueError, TypeError):
            return getattr(response, "status", 200), None


def agy_token_near_expiry(creds, now=None):
    now = time.time() if now is None else now
    expires = agy_provider.expiry_epoch(creds)
    if expires is None:
        return False
    return expires < now + ACCESS_REFRESH_SKEW


def refresh_agy_token(home, opener=None, now=None):
    """Refresh the slot Google access token in place. Never spawns ``agy``.

    Returns True when the credential file was rewritten. Raises
    IdentityBindingError('agy_refresh_expired') when there is nothing left to
    refresh with or the server rejects the grant; returns False on a transient
    failure so a still-valid access token can be used.
    """
    path, creds = agy_credential_file(home)
    refresh = agy_provider.refresh_token(creds)
    client_id, client_secret = agy_provider.oauth_client(creds)
    if not refresh or not client_id:
        # no client recorded beside the token: only ``agy`` can renew it
        raise IdentityBindingError("agy_refresh_expired")
    form = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": client_id,
    }
    if client_secret:
        form["client_secret"] = client_secret
    opener = open_authenticated if opener is None else opener
    request = urllib.request.Request(
        agy_provider.TOKEN_URL,
        data=urllib.parse.urlencode(form).encode("utf-8"), method="POST",
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "user-agent": "headroom",
            "accept": "application/json",
        },
    )
    try:
        response = opener(request, timeout=15)
    except urllib.error.HTTPError as error:
        with error:
            if error.code in (400, 401):
                raise IdentityBindingError("agy_refresh_expired") from error
            return False
    except (OSError, urllib.error.URLError):
        return False
    with response:
        try:
            data = json.load(response)
        except (json.JSONDecodeError, ValueError, TypeError):
            return False
    if not isinstance(data, dict) or not data.get("access_token"):
        return False
    now = time.time() if now is None else now
    rewritten = dict(creds)
    rewritten["access_token"] = data["access_token"]
    if data.get("refresh_token"):
        rewritten["refresh_token"] = data["refresh_token"]
    if data.get("id_token"):
        rewritten["id_token"] = data["id_token"]
    ttl = data.get("expires_in")
    if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or ttl <= 0:
        ttl = 3600
    # google-auth-library writes this field in MILLISECONDS; keep the file in
    # the exact shape ``agy`` expects to read back
    rewritten["expiry_date"] = int((now + int(ttl)) * 1000)
    try:
        paths.write_json_atomic(path, rewritten, mode=0o600)
    except OSError as error:
        raise IdentityBindingError("agy_refresh_expired") from error
    return True


def agy_read(token, opener):
    """(load_status, load_payload, quota_status, quota_payload).

    ``loadCodeAssist`` names the plan and the Cloud AI Companion project the
    quota is billed against; the project is then passed to the quota summary
    so an enterprise slot reads its own pool and not a personal one.
    """
    load_status, load_payload = _agy_post(
        agy_provider.LOAD_CODE_ASSIST_URL, token,
        {"metadata": {"ideType": "IDE_UNSPECIFIED",
                      "platform": "PLATFORM_UNSPECIFIED",
                      "pluginType": "GEMINI"}}, opener, 15)
    body = {}
    if load_status == 200 and isinstance(load_payload, dict):
        project = agy_provider.companion_project(load_payload)
        if project:
            body["project"] = project
    else:
        load_payload = None
    quota_status, quota_payload = _agy_post(
        agy_provider.QUOTA_SUMMARY_URL, token, body, opener, 15)
    return load_status, load_payload, quota_status, quota_payload


def agy_limits(home, expected_email=None, opener=None, now=None):
    """Live Antigravity quota summary + plan. No inference spent."""
    now = int(time.time() if now is None else now)
    opener = open_authenticated if opener is None else opener
    _path, creds = agy_credential_file(home)
    if agy_token_near_expiry(creds, now=now):
        if refresh_agy_token(home, opener=opener, now=now):
            _path, creds = agy_credential_file(home)
    token = agy_provider.access_token(creds)
    if not token:
        raise IdentityBindingError("agy_auth_missing")
    load_status, load_payload, status, payload = agy_read(token, opener)
    if 401 in (status, load_status):
        if refresh_agy_token(home, opener=opener, now=now):
            _path, creds = agy_credential_file(home)
            token = agy_provider.access_token(creds)
            load_status, load_payload, status, payload = agy_read(token, opener)
        else:
            raise IdentityBindingError("agy_refresh_expired")
    if status == 429:
        # Code Assist throttles per user, so only this slot has to wait
        raise ProviderThrottleError(now + 300, provider_response=True,
                                    scope="account")
    if status in (401, 403):
        raise IdentityBindingError("agy_quota_forbidden")
    if status != 200 or payload is None:
        raise IdentityBindingError("agy_quota_unavailable")
    windows = agy_provider.windows_from_quota(payload)
    if windows is None:
        raise ValueError("malformed antigravity quota payload")
    for w in windows.values():
        if isinstance(w, dict) and "resets_at" in w and w["resets_at"] is not None:
            w["resets_at"] = iso_ep(w["resets_at"])
    try:
        identity = agy_local_identity(home)
    except IdentityBindingError as error:
        if error.code != "agy_identity_email_missing":
            raise
        # the login stored no id_token: ask Google who this token belongs to
        info_status, info = _agy_get(
            agy_provider.USERINFO_URL, token, opener, 10)
        if info_status != 200 or not isinstance(info, dict) \
                or not info.get("email"):
            raise
        identity = {
            "verified": True,
            "email": info["email"],
            "account_fingerprint": fingerprint(info.get("sub") or info["email"]),
            "method": "agy_userinfo",
            "plan_type": None,
            "credential_digest": credential_digest("agy", home),
        }
        if isinstance(creds, dict) and not creds.get("email") and _path and not _path.startswith("keyring:"):
            try:
                creds["email"] = info["email"]
                paths.write_json_atomic(_path, creds, mode=0o600)
            except OSError:
                pass
    if expected_email and identity["email"].lower() != expected_email.lower():
        raise IdentityBindingError("slot_bound_to_unexpected_email")
    identity["verified"] = True
    identity["method"] = "agy_code_assist_api"
    identity["credential_digest"] = credential_digest("agy", home)
    identity["plan_type"] = agy_provider.plan_label(load_payload) or "Antigravity"
    return identity, identity["plan_type"], windows

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


def _epoch_ms(value):
    """Normalize expiresAt-like fields to epoch milliseconds."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if value < 1e12:  # stored in seconds
        return int(value * 1000)
    return int(value)


def refresh_needed(oauth, now=None):
    """True when the access token is near expiry, OR the refresh token is
    near expiry / already past (it can die before access; rotate while we
    still can). Does NOT fire merely because refreshTokenExpiresAt is
    earlier than expiresAt — that used to refresh on every collect."""
    now = time.time() if now is None else now
    now_ms = int(now * 1000)
    expires = _epoch_ms(oauth.get("expiresAt"))
    refresh_exp = _epoch_ms(oauth.get("refreshTokenExpiresAt"))
    if expires is None or expires < now_ms + ACCESS_REFRESH_SKEW * 1000:
        return True
    if refresh_exp is not None \
            and refresh_exp < now_ms + REFRESH_PROACTIVE_SKEW * 1000:
        return True
    return False


def _refresh_expires_ms(data, now_ms):
    """Best-effort refresh-token expiry from a token-endpoint payload."""
    for key in ("refresh_expires_in", "refresh_token_expires_in"):
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) \
                and value > 0:
            return now_ms + int(value) * 1000
    for key in ("refresh_expires_at", "refresh_token_expires_at"):
        parsed = _epoch_ms(data.get(key))
        if parsed is not None:
            return parsed
    return None


def refresh_claude_token(home, opener=None, token_urls=None, now=None):
    """Refresh Claude OAuth via the token endpoint. Never spawns the CLI
    and never spends inference tokens.

    Returns True when credentials were rewritten. Raises
    IdentityBindingError('claude_refresh_expired') when the refresh token
    is missing or the server rejects it (invalid_grant / 401). Returns
    False on a transient network/5xx so the caller can keep a still-valid
    access token."""
    creds_path = os.path.join(home, ".credentials.json")
    credentials = paths.load_json(creds_path) or {}
    oauth = credentials.get("claudeAiOauth") or {}
    refresh = oauth.get("refreshToken")
    if not refresh:
        raise IdentityBindingError("claude_refresh_expired")
    # Do not refuse the POST just because refreshTokenExpiresAt is in the
    # past — that timestamp is often pessimistic or unit-ambiguous. Let the
    # server accept or return invalid_grant.

    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": CLAUDE_OAUTH_CLIENT_ID,
    }
    scopes = oauth.get("scopes") or []
    if isinstance(scopes, list) and scopes:
        payload["scope"] = " ".join(str(scope) for scope in scopes)
    body = json.dumps(payload).encode("utf-8")
    opener = open_authenticated if opener is None else opener
    urls = CLAUDE_TOKEN_URLS if token_urls is None else tuple(token_urls)
    data = None
    for index, url in enumerate(urls):
        request = urllib.request.Request(
            url, data=body, method="POST",
            headers={
                "content-type": "application/json",
                "user-agent": CLAUDE_UA,
            },
        )
        try:
            response = opener(request, timeout=15)
        except urllib.error.HTTPError as error:
            with error:
                moved = error.code in (404, 405) and index + 1 < len(urls)
                if moved:
                    continue
                if error.code in (400, 401):
                    raise IdentityBindingError("claude_refresh_expired") from error
                return False
        except (OSError, urllib.error.URLError):
            if index + 1 < len(urls):
                continue
            return False
        with response:
            try:
                data = json.load(response)
            except (json.JSONDecodeError, ValueError, TypeError):
                return False
        break
    if not isinstance(data, dict) or not data.get("access_token"):
        return False

    # Re-read so a concurrent Claude Code write is not clobbered more than
    # we have to; then overwrite only the token fields.
    credentials = paths.load_json(creds_path) or credentials
    oauth = dict(credentials.get("claudeAiOauth") or {})
    now_ms = int((time.time() if now is None else now) * 1000)
    oauth["accessToken"] = data["access_token"]
    if data.get("refresh_token"):
        oauth["refreshToken"] = data["refresh_token"]
    expires_in = data.get("expires_in")
    if not isinstance(expires_in, (int, float)) or isinstance(expires_in, bool) \
            or expires_in <= 0:
        expires_in = DEFAULT_ACCESS_TTL
    oauth["expiresAt"] = now_ms + int(expires_in) * 1000
    refresh_exp = _refresh_expires_ms(data, now_ms)
    if refresh_exp is not None:
        oauth["refreshTokenExpiresAt"] = refresh_exp
    elif data.get("refresh_token"):
        # Rotated, but the server omitted expiry. Pin a conservative TTL so
        # refresh_needed does not immediately fire again, and so we still
        # proactively rotate before a typical short-lived refresh dies.
        oauth["refreshTokenExpiresAt"] = now_ms + DEFAULT_REFRESH_TTL * 1000
    elif _epoch_ms(oauth.get("refreshTokenExpiresAt")) is not None \
            and _epoch_ms(oauth.get("refreshTokenExpiresAt")) <= now_ms:
        # Same refresh token, stale expiry stamp — drop it so we do not
        # refresh-loop every collect.
        oauth.pop("refreshTokenExpiresAt", None)
    if data.get("scope"):
        oauth["scopes"] = str(data["scope"]).split()
    credentials["claudeAiOauth"] = oauth
    try:
        paths.write_json_atomic(creds_path, credentials, mode=0o600)
    except OSError as error:
        # Server already rotated the refresh token; the file must not keep
        # the old one or the next collect cannot recover.
        raise IdentityBindingError("claude_refresh_expired") from error
    return True


def _usage_request(access_token):
    return urllib.request.Request(
        CLAUDE_USAGE_URL,
        headers={
            "authorization": "Bearer " + access_token,
            "anthropic-beta": "oauth-2025-04-20",
            "anthropic-version": "2023-06-01",
            "user-agent": CLAUDE_UA,
        },
    )


def _usage_throttle(error):
    # Anthropic throttles /api/oauth/usage per token: a 429 here means THIS
    # slot waits out Retry-After while its siblings keep reading.
    return ProviderThrottleError(
        retry_after_epoch(error.headers), provider_response=True, scope="account")


def claude_limits(home, expected_fingerprint, opener=open_authenticated):
    credentials = paths.load_json(os.path.join(home, ".credentials.json")) or {}
    oauth = credentials.get("claudeAiOauth") or {}
    if refresh_needed(oauth):
        if refresh_claude_token(home, opener=opener):
            credentials = paths.load_json(os.path.join(home, ".credentials.json")) or {}
            oauth = credentials.get("claudeAiOauth") or {}

    if not oauth.get("accessToken"):
        raise IdentityBindingError("claude_credentials_missing")
    try:
        response = opener(_usage_request(oauth["accessToken"]), timeout=30)
    except urllib.error.HTTPError as error:
        with error:
            if error.code == 429:
                raise _usage_throttle(error) from error
            if error.code != 401 or not refresh_claude_token(home, opener=opener):
                raise
        # 401 -> refreshed: retry once with the rotated token
        credentials = paths.load_json(os.path.join(home, ".credentials.json")) or {}
        oauth = credentials.get("claudeAiOauth") or {}
        if not oauth.get("accessToken"):
            raise IdentityBindingError("claude_credentials_missing")
        try:
            response = opener(_usage_request(oauth["accessToken"]), timeout=30)
        except urllib.error.HTTPError as retry_error:
            with retry_error:
                if retry_error.code == 429:
                    raise _usage_throttle(retry_error) from retry_error
                raise
    with response:
        response_org = response.headers.get("anthropic-organization-id")
        response_fingerprint = fingerprint(response_org) if response_org else None
        # The usage org can legitimately differ from the login's default org
        # (multi-org accounts), so binding is trust-on-first-use per slot:
        # the caller pins this fingerprint and holds the slot if it CHANGES.
        # Once pinned, a response with NO org header can't be verified against
        # the pin, so it must hold rather than silently accept.
        # require the org header on EVERY response (including the first, before
        # any pin) — without it the usage can't be bound to the login at all
        if not response_fingerprint:
            raise IdentityBindingError("claude_usage_org_unverifiable")
        if (expected_fingerprint
                and response_fingerprint != expected_fingerprint):
            raise IdentityBindingError("claude_usage_org_changed")
        try:
            data = json.load(response)
        except (json.JSONDecodeError, ValueError) as json_error:
            raise ValueError("malformed usage response payload") from json_error
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
    for path in files[:15]:
        file_mtime = int(os.path.getmtime(path))
        try:
            with open(path, "rb") as raw:
                # bound the scan: only the tail of each session log
                raw.seek(max(0, os.fstat(raw.fileno()).st_size - 512 * 1024))
                tail = raw.read().decode("utf-8", errors="ignore")
            for line_number, line in enumerate(tail.splitlines()):
                if '"rate_limits"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                limits = _find_rate_limits(event)
                if not limits or not isinstance(limits.get("primary"), dict) \
                        and not isinstance(limits.get("secondary"), dict):
                    continue
                event_ts = iso_ep(event.get("timestamp"))
                # the event's OWN timestamp attests when the provider observed
                # the limit; file mtime only locates the log. Without a real
                # timestamp we can order candidates but must not call it fresh.
                captured_at = event_ts if event_ts is not None else file_mtime
                if captured_at > now + 300:
                    captured_at = file_mtime
                order = (captured_at, file_mtime, path, line_number)
                if newest is None or order > newest[0]:
                    newest = (order, captured_at, limits, event_ts is not None)
        except OSError:
            continue
    if newest is None:
        return {"note": "no rate_limits event in recent Codex sessions"}
    _, captured_at, limits, has_timestamp = newest
    stale = (not has_timestamp) or (now - captured_at) > CODEX_STALE_AFTER

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

def validate_required_windows(windows, provider=None):
    """Every standard window a provider does publish must be a real reading.

    ``registry.OPTIONAL_WINDOWS`` names the ones a provider genuinely has not
    got (Grok has no session window, Antigravity no weekly pool). A provider
    with an optional window must still land at least one usable reading, so a
    slot that reported nothing at all is held, never treated as wide open.
    """
    optional = set(registry.OPTIONAL_WINDOWS.get(provider, ()))
    usable = 0
    for key in ("5h", "7d"):
        window = windows.get(key)
        if not isinstance(window, dict):
            raise ValueError(f"missing required {key} usage window")
        if window.get("freshness") == "not_applicable":
            if key in optional:
                continue
            raise ValueError(f"missing required {key} usage window")
        if window.get("used_percent") is None \
                and window.get("freshness") != "expired_observation":
            raise ValueError(f"missing required {key} usage window")
        if window.get("freshness") == "expired_observation":
            continue
        percent = window["used_percent"]
        if not isinstance(percent, (int, float)) or not 0 <= percent <= 100:
            raise ValueError(f"invalid {key} usage percentage")
        usable += 1
    if optional and not usable:
        raise ValueError("no usable usage window")


def empty_backoff():
    return {"schema_version": 1, "providers": {}, "accounts": {}}


def _retry_at_after(entry, now):
    retry_at = entry.get("retry_at", 0) if isinstance(entry, dict) else 0
    if not isinstance(retry_at, (int, float)) or isinstance(retry_at, bool) \
            or not math.isfinite(retry_at):
        return 0
    return int(retry_at) if retry_at > now else 0


def active_backoff(document, provider, now):
    if not isinstance(document, dict):
        return 0
    return _retry_at_after((document.get("providers") or {}).get(provider), now)


def backoff_account_key(provider, name):
    return f"{provider}:{name}"


def active_account_backoff(document, provider, name, now):
    if not isinstance(document, dict):
        return 0
    return _retry_at_after(
        (document.get("accounts") or {}).get(backoff_account_key(provider, name)),
        now)


def prune_backoff(document, snapshot, now):
    """Drop ledger entries that no longer bind: expired windows, slots that
    read fine this run, slots gone from the registry, and the legacy
    provider-wide Claude entry (Claude throttles are per account now)."""
    providers = document.setdefault("providers", {})
    providers.pop("anthropic_usage_api", None)
    accounts = document.setdefault("accounts", {})
    live = {backoff_account_key(row.get("provider"), row.get("name")): row
            for row in (snapshot.get("accounts") or [])}
    for key in list(accounts):
        row = live.get(key)
        if row is None or row.get("ok") or not _retry_at_after(accounts[key], now):
            accounts.pop(key, None)
    return document


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
    grok_backoff_until = active_backoff(backoff, "grok_billing_api", now)
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
                identity["credential_digest"] = credential_digest(
                    "claude", account["home"])
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
                held_until = active_account_backoff(
                    backoff, "claude", account["name"], now)
                if held_until > now:
                    # this slot's token is still inside its Retry-After;
                    # re-hitting it would only extend the throttle
                    raise ProviderThrottleError(held_until, scope="account")
                result.update(claude_limits(account["home"],
                                            account.get("pinned_usage_org")))
                # Recalculate digest in case token was refreshed during claude_limits
                identity["credential_digest"] = credential_digest(
                    "claude", account["home"])
                if not account.get("pinned_usage_org") \
                        and result.get("source_identity_fingerprint"):
                    # trust-on-first-use: remember which org this slot's
                    # usage feed belongs to; a later change means the login
                    # underneath was swapped and the slot must be held
                    result["pin_usage_org"] = result["source_identity_fingerprint"]
                validate_required_windows(result["windows"], "claude")
                result["ok"] = True
            elif account["provider"] == "grok":
                expected = account.get("expected_email")
                if grok_backoff_until > now:
                    identity = grok_identity(account["home"])
                    result["identity"] = identity
                    result["identity_verified"] = identity["verified"]
                    result["identity_method"] = identity["method"]
                    result["email"] = identity["email"]
                    result["plan"] = identity.get("plan_type") or "Grok"
                    if expected and identity["email"].lower() != expected.lower():
                        raise IdentityBindingError("slot_bound_to_unexpected_email")
                    raise ProviderThrottleError(grok_backoff_until)
                identity, plan_type, windows = grok_limits(
                    account["home"], expected, now=now)
                result["identity"] = identity
                result["identity_verified"] = identity["verified"]
                result["identity_method"] = identity["method"]
                result["email"] = identity["email"]
                result["plan"] = plan_type or "Grok"
                result["subscription"] = {"status": "unknown",
                                          "source": "provider_not_exposed"}
                result["source"] = "grok_billing_api"
                result["stale"] = False
                result["captured_at"] = now
                result["windows"] = windows
                validate_required_windows(result["windows"], "grok")
                result["ok"] = True
            elif account["provider"] == "agy":
                expected = account.get("expected_email")
                held_until = active_account_backoff(
                    backoff, "agy", account["name"], now)
                if held_until > now:
                    identity = agy_local_identity(account["home"])
                    result["identity"] = identity
                    result["identity_verified"] = identity["verified"]
                    result["identity_method"] = identity["method"]
                    result["email"] = identity["email"]
                    result["plan"] = identity.get("plan_type") or "Antigravity"
                    if expected and identity["email"].lower() != expected.lower():
                        raise IdentityBindingError(
                            "slot_bound_to_unexpected_email")
                    raise ProviderThrottleError(held_until, scope="account")
                identity, plan_type, windows = agy_limits(
                    account["home"], expected, now=now)
                result["identity"] = identity
                result["identity_verified"] = identity["verified"]
                result["identity_method"] = identity["method"]
                result["email"] = identity["email"]
                result["plan"] = plan_type or "Antigravity"
                result["subscription"] = {"status": "unknown",
                                          "source": "provider_not_exposed"}
                result["source"] = "google_code_assist_api"
                result["stale"] = False
                result["captured_at"] = now
                result["windows"] = windows
                validate_required_windows(result["windows"], "agy")
                result["ok"] = True
            elif account["provider"] == "codex":
                expected = account.get("expected_email")
                try:
                    # PRIMARY: live, identity-bound read via the codex app-server
                    identity, plan_type, windows = codex_live(
                        account["home"], expected, now)
                    result["identity"] = identity
                    result["identity_verified"] = True
                    result["identity_method"] = identity["method"]
                    result["email"] = identity["email"]
                    result["subscription"] = identity.get("subscription")
                    result["source"] = "codex_app_server"
                    result["stale"] = False
                    result["captured_at"] = now
                    result["windows"] = windows
                    result["plan"] = {
                        "pro": "ChatGPT Pro", "plus": "ChatGPT Plus",
                        "prolite": "ChatGPT Pro Lite", "free": "Free",
                    }.get(str(plan_type or ""), plan_type or "Unknown")
                    validate_required_windows(result["windows"], "codex")
                    result["ok"] = True
                except IdentityBindingError as app_error:
                    # FALLBACK for older Codex without the app-server: best-effort
                    # session-log read (dashboard-only, may be stale/idle)
                    if not str(app_error.code).startswith("codex_app_server"):
                        raise
                    identity = codex_identity(account["home"])
                    identity["credential_digest"] = credential_digest(
                        "codex", account["home"])
                    result["identity"] = identity
                    result["identity_verified"] = identity["verified"]
                    result["identity_method"] = identity["method"]
                    result["email"] = identity["email"]
                    result["subscription"] = identity.get("subscription")
                    if expected and identity["email"].lower() != expected.lower():
                        raise IdentityBindingError("slot_bound_to_unexpected_email")
                    telemetry = codex_limits(account["home"], now=now)
                    plan_type = str(telemetry.pop("plan_type", None)
                                    or identity.get("plan_type") or "")
                    result["plan"] = {
                        "pro": "ChatGPT Pro", "plus": "ChatGPT Plus",
                        "prolite": "ChatGPT Pro Lite", "free": "Free",
                    }.get(plan_type, plan_type or "Unknown")
                    result.update(telemetry)
                    if "windows" in result:
                        validate_required_windows(result["windows"], "codex")
                        result["ok"] = True
                    else:
                        result["ok"] = False
            else:
                raise IdentityBindingError("unknown_provider")
        except ProviderThrottleError as error:
            source = THROTTLE_SOURCE.get(account["provider"],
                                          "anthropic_usage_api")
            if error.scope == "account":
                # one slot's token is throttled; every sibling keeps reading
                if error.provider_response and persist_backoff is not None:
                    persist_backoff(
                        error.retry_at, source=source,
                        account=backoff_account_key(account["provider"],
                                                    account["name"]))
                result["note"] = ("usage source rate-limited this account's "
                                  "token; held until its retry window "
                                  "(other accounts unaffected)")
            else:
                if source == "grok_billing_api":
                    grok_backoff_until = max(grok_backoff_until, error.retry_at)
                if error.provider_response and persist_backoff is not None:
                    try:
                        persist_backoff(error.retry_at, source=source)
                    except TypeError:
                        persist_backoff(error.retry_at)
                result["note"] = ("usage source temporarily rate-limited; "
                                  "account held until provider retry window")
            result["ok"] = False
            result["error_code"] = "usage_source_rate_limited"
            result["retry_at"] = error.retry_at
        except IdentityBindingError as error:
            result["ok"] = False
            result["error_code"] = error.code
            # held slots still show who they *should* be, so the dashboard
            # and doctor can name the reconnect target
            if not result.get("email") and account.get("expected_email"):
                result["email"] = account["expected_email"]
            result["note"] = binding_note(error.code, account, result)
        except Exception as error:  # noqa: BLE001 — every account must report
            result["ok"] = False
            # `error` is PRIVATE-only (may contain local paths / usernames).
            # `note` is published, so it must stay generic.
            result["error"] = type(error).__name__ + ": " + str(error)[:120]
            result["note"] = "collector error; see private snapshot for detail"
        snapshot["accounts"].append(result)
    snapshot["integrity_warnings"] = apply_integrity(snapshot["accounts"])
    completed = int(time.time())
    snapshot["generated"] = completed
    snapshot["generated_iso"] = datetime.fromtimestamp(
        completed, timezone.utc
    ).isoformat().replace("+00:00", "Z")
    return snapshot


def binding_note(code, account, result=None):
    """Operator-facing reason + next command for a held identity binding."""
    result = result or {}
    name = account.get("name") or "account"
    expected = account.get("expected_email")
    expected_bit = f" (expected {expected})" if expected else ""
    reconnect = f"run `headroom connect {name}`"
    if code == "claude_local_binding_missing":
        return f"no login in slot '{name}'{expected_bit} — {reconnect}"
    if code == "claude_credentials_missing":
        return ("Claude login found but its token isn't file-based "
                f"(macOS Keychain?). Isolated re-login: `{reconnect}`")
    if code == "claude_refresh_expired":
        return f"refresh token expired for slot '{name}'{expected_bit} — {reconnect}"
    if code == "grok_auth_missing":
        return f"no Grok login in slot '{name}'{expected_bit} — {reconnect}"
    if code == "grok_identity_email_missing":
        return (f"Grok login in slot '{name}' has no email "
                f"— {reconnect}")
    if code == "grok_refresh_expired":
        return f"Grok refresh token expired for slot '{name}'{expected_bit} — {reconnect}"
    if code == "grok_team_usage_unsupported":
        return (f"Grok team principal in slot '{name}' has no supported usage "
                f"feed yet — identity held")
    if code == "grok_billing_unavailable":
        return (f"Grok billing feed unreachable for slot '{name}' "
                f"— retry `headroom collect` or {reconnect}")
    if code == "agy_auth_missing":
        return (f"no file-backed Antigravity login in slot '{name}'"
                f"{expected_bit} — {reconnect} (agy keeps its token in the OS "
                f"keyring unless the slot home holds oauth_creds.json)")
    if code == "agy_identity_email_missing":
        return (f"Antigravity token in slot '{name}' names no account "
                f"— {reconnect}")
    if code == "agy_refresh_expired":
        return (f"Antigravity token expired for slot '{name}'{expected_bit} "
                f"— {reconnect}")
    if code == "agy_quota_forbidden":
        return (f"Antigravity quota is not readable for slot '{name}' "
                f"(token lacks the Code Assist scope) — {reconnect}")
    if code == "agy_quota_unavailable":
        return (f"Antigravity quota feed unreachable for slot '{name}' "
                f"— retry `headroom collect` or {reconnect}")
    if code == "slot_bound_to_unexpected_email":
        got = ((result.get("identity") or {}).get("email")
               or result.get("email") or "unknown")
        expect = expected or "a pinned email"
        return (f"logged in as {got}; slot '{name}' expects {expect} "
                f"— {reconnect} or `headroom remove {name}`")
    return (f"identity could not be bound to slot '{name}' ({code}); "
            f"account held — {reconnect}")


def redact_email(address):
    if not address:
        return address
    if "@" not in address:
        return "***"  # redaction must never pass an unrecognized value through
    local, _, domain = address.partition("@")
    return (local[0] if local else "") + "***@" + domain


def public_snapshot(snapshot, redact_emails=False):
    accounts = []
    for account in snapshot["accounts"]:
        public = {k: v for k, v in account.items() if k in PUBLIC_FIELDS}
        if account.get("error"):
            # never publish raw exception text, whatever `note` already holds
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

        def persist(retry_at, source="anthropic_usage_api", account=None):
            entry = {
                "retry_at": int(retry_at),
                "observed_at": min(int(time.time()), int(retry_at) - 1),
            }
            if account:
                backoff.setdefault("accounts", {})[account] = entry
            else:
                backoff.setdefault("providers", {})[source] = entry
            paths.write_json_atomic(paths.backoff_path(), backoff)

        snapshot = collect(registry.accounts(config), backoff, persist)
        pins = {a["name"]: a.pop("pin_usage_org")
                for a in snapshot["accounts"] if a.get("pin_usage_org")}
        # merge pins under the config lock against the LATEST config, so a
        # concurrent `connect` account-add is never overwritten by our stale copy
        registry.apply_pins(pins)
        limited = {a.get("provider")
                   for a in snapshot["accounts"]
                   if a.get("error_code") == "usage_source_rate_limited"}
        providers = backoff.setdefault("providers", {})
        if any(a.get("provider") == "grok" and a.get("ok")
               for a in snapshot["accounts"]) and "grok" not in limited:
            providers.pop("grok_billing_api", None)
        prune_backoff(backoff, snapshot, int(time.time()))
        paths.write_json_atomic(paths.backoff_path(), backoff)
        paths.write_json_atomic(paths.private_snapshot_path(), snapshot)
        # reload settings fresh (not the config loaded at collect start) so a
        # redaction change made mid-collect governs the published projection,
        # and default to redacted if unset
        settings = registry.dashboard_settings()
        paths.write_json_atomic(
            paths.public_snapshot_path(),
            public_snapshot(snapshot, settings.get("redact_emails", True)),
            mode=0o644,
        )
        try:
            from . import history as usage_history
            if usage_history.enabled():
                live = {usage_history.slot_id(account)
                        for account in snapshot.get("accounts") or []}
                live.discard(None)
                usage_history.append_snapshot(snapshot, live_ids=live)
        except Exception as error:  # noqa: BLE001 — history must never block collect
            if not quiet:
                print("WARNING history not recorded:", error)
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
            # only the windows this provider actually publishes (Grok has no
            # session window, Antigravity no weekly pool)
            gauges = " ".join(
                "%s=%-5s" % (key, display_percent(windows.get(key)))
                for key in registry.required_windows(account.get("provider")))
            print("%-16s %-14s %s %s%s" % (
                account["name"], account.get("plan", ""), gauges,
                scoped, " STALE" if account.get("stale") else ""))
        else:
            print("%-16s HELD: %s" % (
                account["name"],
                account.get("note") or account.get("error") or "unknown"))
    for warning in snapshot.get("integrity_warnings", []):
        print("WARNING", warning)
