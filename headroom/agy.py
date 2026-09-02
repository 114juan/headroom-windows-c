"""Google Antigravity (AGY) credential + Code Assist quota parsing.

Antigravity — the Google agentic IDE and its ``agy`` CLI — bills a Google
account against the Cloud Code companion backend, the same
``cloudcode-pa.googleapis.com`` surface Gemini Code Assist uses. Remaining
allowance comes from::

    POST https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary

whose response (``google.internal.cloud.code.v1internal``) is::

    RetrieveUserQuotaSummaryResponse {
      repeated QuotaSummaryBucket buckets;   // deprecated, flat form
      repeated QuotaSummaryGroup  groups;
      string description;
    }
    QuotaSummaryGroup  { display_name; description; repeated QuotaSummaryBucket buckets }
    QuotaSummaryBucket { bucket_id; display_name; description; window;
                         oneof remaining { remaining_fraction; remaining_amount }
                         disabled; reset_time }

The plan comes from ``v1internal:loadCodeAssist`` (``currentTier``).

Parsing lives here so collect can stay fail-closed: a bucket we cannot turn
into a real percentage is dropped, never guessed at.
"""
import math
import re

CODE_ASSIST_BASE = "https://cloudcode-pa.googleapis.com"
QUOTA_SUMMARY_URL = CODE_ASSIST_BASE + "/v1internal:retrieveUserQuotaSummary"
LOAD_CODE_ASSIST_URL = CODE_ASSIST_BASE + "/v1internal:loadCodeAssist"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
TOKEN_URL = "https://oauth2.googleapis.com/token"

# Where an Antigravity / Code Assist login can leave a file-backed token inside
# an isolated slot home, most specific first. ``agy`` prefers the OS keyring
# and only falls back to a file when no keyring is available, so a slot with
# none of these is HELD rather than guessed at (see docs/KNOWN-LIMITS.md).
CREDENTIAL_FILES = (
    "oauth_creds.json",
    ".gemini/oauth_creds.json",
    ".gemini/antigravity-cli/oauth_creds.json",
    ".gemini/antigravity/oauth_creds.json",
)

# loadCodeAssist tier ids -> the name we show when the server sends no display
# name of its own.
TIER_NAMES = {
    "free-tier": "Antigravity Free",
    "legacy-tier": "Antigravity (legacy)",
    "standard-tier": "Antigravity Pro",
    "enterprise-tier": "Antigravity Enterprise",
}

# A bucket at or under this length is the rolling session window headroom
# reports as "5h"; at or over WEEKLY_MIN it is the "7d" pool. Antigravity's
# own windows are 5-hourly, but the server names the length, so the mapping
# is by duration and the true length is kept in ``window_minutes``.
SESSION_MAX_MINUTES = 24 * 60
WEEKLY_MIN_MINUTES = 5 * 24 * 60

_ISO_DURATION = re.compile(
    r"P(?:(?P<days>\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(?P<hours>\d+(?:\.\d+)?)H)?"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?", re.IGNORECASE)
_GO_DURATION = re.compile(
    r"(?:(?P<hours>\d+(?:\.\d+)?)h)?"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)m)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)s)?")
_HUMAN_DURATION = re.compile(
    r"(?:(?P<count>\d+(?:\.\d+)?)\s*[-\s]?\s*)?"
    r"(?P<unit>minute|min|hour|hr|day|week)s?\b", re.IGNORECASE)
_UNIT_MINUTES = {
    "minute": 1.0, "min": 1.0,
    "hour": 60.0, "hr": 60.0,
    "day": 1440.0, "week": 10080.0,
}


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def _positive(minutes):
    return minutes if minutes and minutes > 0 else None


def parse_window_minutes(value):
    """Length of a quota bucket's window, in minutes, or None.

    The server sends ``window`` as a free string, so accept every shape one
    could reasonably arrive in: protobuf Duration (``"18000s"``), ISO-8601
    (``"PT5H"``), Go (``"5h0m0s"``) and prose (``"5 hours"``, ``"per day"``,
    ``"weekly"``). An unrecognised string is None, never a guess.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = _finite(value)
        return _positive(seconds / 60.0) if seconds is not None else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?s", text):          # protobuf Duration
        return _positive(float(text[:-1]) / 60.0)
    if re.fullmatch(r"\d+(?:\.\d+)?", text):           # bare seconds
        return _positive(float(text) / 60.0)
    match = _ISO_DURATION.fullmatch(text)
    if match and any(match.groupdict().values()):
        return _positive(float(match.group("days") or 0) * 1440.0
                         + float(match.group("hours") or 0) * 60.0
                         + float(match.group("minutes") or 0)
                         + float(match.group("seconds") or 0) / 60.0)
    match = _GO_DURATION.fullmatch(text)
    if match and any(match.groupdict().values()):
        return _positive(float(match.group("hours") or 0) * 60.0
                         + float(match.group("minutes") or 0)
                         + float(match.group("seconds") or 0) / 60.0)
    lowered = text.lower()
    for word, minutes in (("hourly", 60.0), ("daily", 1440.0),
                          ("weekly", 10080.0), ("monthly", 43200.0)):
        if word in lowered:
            return minutes
    match = _HUMAN_DURATION.search(text)
    if match:
        count = float(match.group("count") or 1)
        return _positive(count * _UNIT_MINUTES[match.group("unit").lower()])
    return None


def bucket_used_percent(bucket):
    """0-100 used, or None when the bucket carries no usable reading.

    ``remaining_fraction`` and ``remaining_amount`` share a proto ``oneof``,
    so protobuf-JSON emits the chosen one even when it is 0 — an absent
    fraction means the server sent an *amount* (no denominator, so no
    percentage) or nothing at all. Either way: unusable, not "empty".
    """
    if not isinstance(bucket, dict):
        return None
    fraction = _finite(bucket.get("remainingFraction"))
    if fraction is None:
        fraction = _finite(bucket.get("remaining_fraction"))
    if fraction is None:
        return None
    fraction = min(1.0, max(0.0, fraction))
    return round((1.0 - fraction) * 100.0, 1)


def bucket_reset(bucket):
    if not isinstance(bucket, dict):
        return None
    reset = bucket.get("resetTime") or bucket.get("reset_time")
    return reset if isinstance(reset, str) and reset.strip() else None


def bucket_label(bucket):
    for key in ("displayName", "display_name", "bucketId", "bucket_id"):
        value = (bucket or {}).get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def iter_buckets(payload):
    """Every bucket in the response, flat, in ``groups`` then legacy order.

    Group display names prefix their buckets so two models' identically named
    buckets stay distinguishable as scoped windows.
    """
    if not isinstance(payload, dict):
        return []
    found = []
    for group in payload.get("groups") or []:
        if not isinstance(group, dict):
            continue
        prefix = group.get("displayName") or group.get("display_name")
        prefix = prefix.strip() if isinstance(prefix, str) else None
        for bucket in group.get("buckets") or []:
            if isinstance(bucket, dict):
                found.append((prefix, bucket))
    for bucket in payload.get("buckets") or []:
        if isinstance(bucket, dict):
            found.append((None, bucket))
    return found


def _scoped_name(prefix, bucket, index):
    label = bucket_label(bucket)
    if prefix and label and prefix.lower() != label.lower():
        label = prefix + " " + label
    label = label or prefix or ("bucket%d" % index)
    # scoped keys are "scoped:<name>"; a colon would break that split
    return label.replace(":", "-").strip()


def _window(used_percent, resets_at, minutes):
    return {
        "used_percent": round(float(used_percent), 1),
        "resets_at": resets_at,
        "window_minutes": int(round(minutes)) if minutes else None,
        "freshness": "fresh",
    }


def na_window(minutes):
    """Keep the slot shape when Antigravity publishes no such window."""
    return {
        "used_percent": None,
        "resets_at": None,
        "window_minutes": minutes,
        "freshness": "not_applicable",
    }


def windows_from_quota(payload):
    """``{"5h": ..., "7d": ..., "scoped:<name>": ...}`` from a quota summary.

    The shortest usable bucket of at most a day becomes the session window and
    the longest of at least five days the weekly pool — both keep their real
    length in ``window_minutes``. Everything else (and every extra bucket in
    the session band) is published as a scoped window so nothing is lost.
    Returns None when the payload yields no usable bucket at all.
    """
    session = None
    weekly = None
    leftovers = []
    for index, (prefix, bucket) in enumerate(iter_buckets(payload)):
        if bucket.get("disabled") is True:
            continue
        used = bucket_used_percent(bucket)
        if used is None:
            continue
        minutes = parse_window_minutes(
            bucket.get("window", bucket.get("window_minutes")))
        entry = (minutes, used, bucket_reset(bucket),
                 _scoped_name(prefix, bucket, index))
        if minutes is not None and minutes <= SESSION_MAX_MINUTES:
            if session is None or minutes < session[0]:
                if session is not None:
                    leftovers.append(session)
                session = entry
            else:
                leftovers.append(entry)
        elif minutes is not None and minutes >= WEEKLY_MIN_MINUTES:
            if weekly is None or minutes > weekly[0]:
                if weekly is not None:
                    leftovers.append(weekly)
                weekly = entry
            else:
                leftovers.append(entry)
        else:
            leftovers.append(entry)
    if session is None and weekly is None and not leftovers:
        return None
    windows = {
        "5h": _window(session[1], session[2], session[0]) if session
        else na_window(300),
        "7d": _window(weekly[1], weekly[2], weekly[0]) if weekly
        else na_window(10080),
    }
    for minutes, used, resets_at, name in leftovers:
        windows["scoped:" + name] = _window(used, resets_at, minutes)
    return windows


def plan_label(load_response):
    """Human plan name from ``loadCodeAssist``. The server's own name wins."""
    tier = (load_response or {}).get("currentTier")
    if not isinstance(tier, dict):
        return None
    name = tier.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    tier_id = tier.get("id")
    if not isinstance(tier_id, str) or not tier_id.strip():
        return None
    return TIER_NAMES.get(tier_id.strip().lower(), tier_id.strip())


def companion_project(load_response):
    """The Cloud AI Companion project the quota is billed against, if any."""
    for key in ("cloudaicompanionProject", "cloudaicompanion_project"):
        value = (load_response or {}).get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    tier = (load_response or {}).get("currentTier")
    if isinstance(tier, dict):
        value = tier.get("userDefinedCloudaicompanionProject")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def select_credentials(root):
    """Validate a google-auth-library credentials file.

    Returns the record when it carries a usable ``access_token``, else None —
    a partially written file must never look like a healthy login.
    """
    if not isinstance(root, dict):
        return None
    if not access_token(root):
        return None
    return root


def access_token(creds):
    value = (creds or {}).get("access_token") or (creds or {}).get("accessToken")
    return value if isinstance(value, str) and value else None


def refresh_token(creds):
    value = (creds or {}).get("refresh_token") or (creds or {}).get("refreshToken")
    return value if isinstance(value, str) and value else None


def id_token(creds):
    value = (creds or {}).get("id_token") or (creds or {}).get("idToken")
    return value if isinstance(value, str) and value else None


def expiry_epoch(creds):
    """Access-token expiry as epoch seconds, or None.

    google-auth-library writes ``expiry_date`` in **milliseconds**; other
    writers use ``expiry``/``expires_at`` in seconds.
    """
    if not isinstance(creds, dict):
        return None
    millis = _finite(creds.get("expiry_date"))
    if millis is not None and millis > 0:
        return int(millis / 1000)
    for key in ("expires_at", "expiry"):
        seconds = _finite(creds.get(key))
        if seconds is not None and seconds > 0:
            return int(seconds)
    return None


def oauth_client(creds):
    """``(client_id, client_secret)`` recorded next to the token, or (None, None).

    Antigravity's own OAuth client is deliberately not embedded here: headroom
    refreshes only with the client the login itself wrote, so it can never
    mint a new grant, only extend one the operator already made.
    """
    if not isinstance(creds, dict):
        return None, None
    client_id = creds.get("client_id") or creds.get("clientId")
    secret = creds.get("client_secret") or creds.get("clientSecret")
    return (client_id if isinstance(client_id, str) and client_id else None,
            secret if isinstance(secret, str) and secret else None)
