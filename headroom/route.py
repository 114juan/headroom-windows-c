"""Model-aware routing with fail-closed cooldowns.

`pick` answers one question: which connected account has PROVEN headroom for
this model family right now? "Proven" means a fresh, identity-bound usage
reading — never a guess. An account is skipped when its reading is missing,
stale, out of range, at 100%, or inside a cooldown from a previous limit-hit.

`run` executes a command on the chosen account and watches its output for
limit errors; on a hit it cools that account down until the relevant window
resets and retries the next candidate.
"""
import datetime
import math
import os
import re
import subprocess
import sys
import time

from . import collect as collector
from . import paths, registry

SNAPSHOT_MAX_AGE = int(os.environ.get("HEADROOM_SNAPSHOT_MAX_AGE", "900"))
CLOCK_SKEW = int(os.environ.get("HEADROOM_CLOCK_SKEW", "300"))

LIMIT_RE = re.compile(
    r"(hit your (?:session|weekly|usage|5[- ]?hour|five[- ]?hour)[^.\n]*limit"
    r"|usage limit reached|rate_limit_error|\brate limit\b"
    r"|429 Too Many|status 429|overloaded_error)", re.I)
WEEKLY_RE = re.compile(r"week", re.I)


def _number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def tfmt(epoch):
    try:
        return datetime.datetime.fromtimestamp(epoch).strftime("%a %H:%M")
    except (OSError, OverflowError, ValueError):
        return str(epoch)


def cooldowns():
    value = paths.load_json(paths.cooldowns_path())
    return value if isinstance(value, dict) else {}


def save_cooldowns(value):
    paths.write_json_atomic(paths.cooldowns_path(), value)


def ensure_fresh_snapshot(max_age=None):
    """Return a fresh private snapshot, collecting inline when stale/absent."""
    max_age = SNAPSHOT_MAX_AGE if max_age is None else max_age
    snapshot = paths.load_json(paths.private_snapshot_path())
    now = time.time()
    generated = (snapshot or {}).get("generated")
    if snapshot is None or not _number(generated) or now - generated > max_age \
            or generated > now + CLOCK_SKEW:
        snapshot = collector.run_collect(quiet=True)
    return snapshot


def _snapshot_accounts(snapshot):
    return {row["name"]: row for row in (snapshot or {}).get("accounts") or []
            if isinstance(row, dict) and row.get("name")}


def scoped_window_for(fam, windows):
    for key, window in (windows or {}).items():
        if key.startswith("scoped:") and fam in key.lower():
            return window
    return None


def block_reason(account, fam, snapshot_row, cool, now):
    """None when the account has proven headroom; otherwise why not."""
    if snapshot_row is None:
        return "no usage reading yet"
    if snapshot_row.get("ok") is not True:
        return "held: " + str(snapshot_row.get("error_code")
                              or snapshot_row.get("note") or "not ok")
    if snapshot_row.get("routable") is not True:
        return "trust unverified: " + str(snapshot_row.get("trust_state"))
    if snapshot_row.get("stale"):
        return "reading stale"
    captured_at = snapshot_row.get("captured_at")
    if not _number(captured_at) or captured_at > now + CLOCK_SKEW:
        return "reading clock invalid"
    windows = snapshot_row.get("windows") or {}
    for key in ("5h", "7d"):
        window = windows.get(key)
        if not isinstance(window, dict):
            return f"{key} window missing"
        percent = window.get("used_percent")
        if window.get("freshness") == "expired_observation":
            continue
        if not _number(percent) or not 0 <= percent <= 100:
            return f"{key} reading invalid"
        if percent >= 100:
            return f"{key} at 100%"
        if window.get("severity") == "critical" and window.get("is_active"):
            return f"{key} critical"
    scoped = scoped_window_for(fam, windows)
    if scoped is not None and _number(scoped.get("used_percent")) \
            and scoped["used_percent"] >= 100:
        return f"{fam} weekly cap at 100%"
    cooldown = cool.get(f"{account['name']}:{fam}")
    if _number(cooldown) and now < cooldown:
        return f"cooldown until {tfmt(cooldown)}"
    return None


def candidates(fam, snapshot=None):
    """[(account, reason-or-None), ...] in preference order."""
    snapshot = ensure_fresh_snapshot() if snapshot is None else snapshot
    rows = _snapshot_accounts(snapshot)
    cool = cooldowns()
    now = time.time()
    result = []
    for account in registry.ordered_for(fam):
        reason = block_reason(account, fam, rows.get(account["name"]), cool, now)
        result.append((account, reason))
    return result


def pick(fam):
    for account, reason in candidates(fam):
        if reason is None:
            return account
    return None


def env_key(account):
    return "CLAUDE_CONFIG_DIR" if account["provider"] == "claude" else "CODEX_HOME"


def mark(name, fam, epoch=None):
    cool = cooldowns()
    cool[f"{name}:{fam}"] = epoch if epoch is not None else time.time() + 5 * 3600
    save_cooldowns(cool)
    return cool[f"{name}:{fam}"]


def clear(key=None):
    if key is None:
        save_cooldowns({})
    else:
        cool = cooldowns()
        cool.pop(key, None)
        save_cooldowns(cool)


def window_reset(snapshot, name, window_key):
    row = _snapshot_accounts(snapshot).get(name) or {}
    return ((row.get("windows") or {}).get(window_key) or {}).get("resets_at")


def cmd_status(fam):
    snapshot = ensure_fresh_snapshot()
    rows = _snapshot_accounts(snapshot)
    print(f"model family: {fam}")
    chosen = None
    for account, reason in candidates(fam, snapshot):
        windows = (rows.get(account["name"]) or {}).get("windows") or {}
        head = "5h=%s 7d=%s" % (
            collector.display_percent(windows.get("5h")),
            collector.display_percent(windows.get("7d")))
        scoped = scoped_window_for(fam, windows)
        if scoped is not None:
            head += " %s=%s" % (fam, collector.display_percent(scoped))
        marker = "AVAIL" if reason is None else "skip "
        note = "" if reason is None else f"({reason})"
        print(f"  {marker}  {account['name']:<18} {head:<28} {note}")
        if reason is None and chosen is None:
            chosen = account["name"]
    print(f"-> chosen: {chosen or 'NONE — no account has proven headroom'}")
    return 0 if chosen else 2


def cmd_run(fam, command):
    snapshot = ensure_fresh_snapshot()
    for account, reason in candidates(fam, snapshot):
        if reason:
            print(f"[headroom] skip {account['name']}: {reason}", file=sys.stderr)
            continue
        environment = os.environ.copy()
        environment[env_key(account)] = account["home"]
        print(f"[headroom] running on {account['name']}", file=sys.stderr)
        process = subprocess.run(command, env=environment,
                                 capture_output=True, text=True)
        output = (process.stdout or "") + "\n" + (process.stderr or "")
        if LIMIT_RE.search(output):
            window_key = "7d" if WEEKLY_RE.search(output) else "5h"
            reset = window_reset(snapshot, account["name"], window_key) \
                or time.time() + (7 * 86400 if window_key == "7d" else 5 * 3600)
            mark(account["name"], fam, reset)
            print(f"[headroom] {account['name']} hit its {window_key} limit -> "
                  f"cooled until {tfmt(reset)}; rotating", file=sys.stderr)
            continue
        sys.stdout.write(process.stdout or "")
        sys.stderr.write(process.stderr or "")
        print(f"[headroom] completed on {account['name']} "
              f"(exit {process.returncode})", file=sys.stderr)
        return process.returncode
    print(f"[headroom] NO account for '{fam}' has proven headroom",
          file=sys.stderr)
    return 2


def cmd_exec(fam, command):
    """Interactive launch: pick once, exec with the account's env, no capture."""
    account = pick(fam)
    if account is None:
        print(f"[headroom] no account for '{fam}' has proven headroom; "
              f"try `headroom status {fam}`", file=sys.stderr)
        return 2
    os.environ[env_key(account)] = account["home"]
    print(f"[headroom] {fam} -> {account['name']} ({account['home']})",
          file=sys.stderr)
    try:
        os.execvp(command[0], command)
    except OSError as error:
        print(f"[headroom] cannot exec {command[0]}: {error}", file=sys.stderr)
        return 127


def cmd_rotate(fam):
    """Manual rotation: cool the current best down, report the next one."""
    snapshot = ensure_fresh_snapshot()
    ranked = candidates(fam, snapshot)
    current = next((a for a, r in ranked if r is None), None)
    if current is None:
        print(f"every account for '{fam}' is already limited or held")
        earliest = None
        for account, _ in ranked:
            reset = window_reset(snapshot, account["name"], "5h")
            if _number(reset) and (earliest is None or reset < earliest):
                earliest = reset
        if earliest:
            print(f"earliest 5h reset: {tfmt(earliest)}")
        return 2
    reset = window_reset(snapshot, current["name"], "5h") \
        or time.time() + 5 * 3600
    mark(current["name"], fam, reset)
    successor = pick(fam)
    if successor is None:
        print(f"rotated {current['name']} out (cools until {tfmt(reset)}) — "
              f"but no other account has headroom for '{fam}'")
        return 2
    print(f"rotated {current['name']} -> {successor['name']} ({fam}); "
          f"{current['name']} cools until {tfmt(reset)}")
    print(f"export {env_key(successor)}=\"{successor['home']}\"")
    return 0
