"""Claude Code status line: your live headroom at the bottom of every session.

Claude Code pipes a JSON payload on stdin (model, workspace, etc.) and renders
whatever this prints. We show the account the CURRENT session is running on
(matched via CLAUDE_CONFIG_DIR), its 5h/7d headroom color-coded, and — when
the current account is running low — who the rotator would pick next.

Wire it up in ~/.claude/settings.json:

    {"statusLine": {"type": "command", "command": "headroom statusline"}}
"""
import json
import os
import sys

from . import paths, registry

GREEN, YELLOW, ORANGE, RED, DIM, RESET = (
    "\x1b[32m", "\x1b[33m", "\x1b[38;5;208m", "\x1b[31m", "\x1b[2m", "\x1b[0m")


def color(used):
    if used is None:
        return DIM
    if used < 50:
        return GREEN
    if used < 75:
        return YELLOW
    if used < 90:
        return ORANGE
    return RED


def window_text(windows, key, label):
    window = (windows or {}).get(key) or {}
    used = window.get("used_percent")
    if used is None:
        return f"{DIM}{label} ?{RESET}"
    return f"{color(used)}{label} {round(used)}%{RESET}"


def main():
    try:
        json.load(sys.stdin)  # payload available if ever needed; presence only
    except (ValueError, OSError):
        pass
    snapshot = paths.load_json(paths.private_snapshot_path())
    if not snapshot:
        print(f"{DIM}headroom: no snapshot yet (run `headroom collect`){RESET}")
        return 0
    rows = {row["name"]: row for row in snapshot.get("accounts", [])
            if isinstance(row, dict) and row.get("name")}
    current = None
    try:
        for account in registry.accounts():
            env_name = registry.HOME_ENV.get(account["provider"])
            hinted = os.environ.get(env_name) if env_name else None
            home = os.path.realpath(account["home"])
            if hinted and os.path.normcase(os.path.realpath(
                    os.path.expanduser(hinted))) == os.path.normcase(home):
                current = account
                break
            if env_name == "CLAUDE_CONFIG_DIR" and not hinted:
                default = os.path.realpath(os.path.expanduser("~/.claude"))
                if os.path.normcase(home) == os.path.normcase(default):
                    current = account
                    break
    except registry.RegistryError:
        pass
    if current is None:
        try:
            current_home = os.path.realpath(
                os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude")))
            for account in registry.accounts():
                if os.path.normcase(os.path.realpath(account["home"])) == os.path.normcase(current_home):
                    current = account
                    break
        except registry.RegistryError:
            pass
    parts = []
    if current and current["name"] in rows:
        row = rows[current["name"]]
        windows = row.get("windows") or {}
        parts.append(f"{current['name']}")
        if current.get("provider") != "grok":
            parts.append(window_text(windows, "5h", "5h"))
        parts.append(window_text(windows, "7d", "7d"))
        probe = windows.get("7d") if current.get("provider") == "grok" \
            else windows.get("5h")
        used = (probe or {}).get("used_percent")
        if used is not None and used >= 75:
            from . import route
            fam = "grok" if current.get("provider") == "grok" else (
                "codex" if current.get("provider") == "codex" else "claude")
            candidate = next(
                (account for account, reason in route.candidates(
                    fam, snapshot)
                 if reason is None and account["name"] != current["name"]),
                None)
            if candidate:
                parts.append(f"{DIM}next: {candidate['name']}{RESET}")
    else:
        ok_rows = [row for row in rows.values()
                   if row.get("ok") and row.get("routable")
                   and not row.get("stale") and row.get("provider") == "claude"]
        if ok_rows:
            def used_5h(row):
                value = (row.get("windows", {}).get("5h") or {}).get("used_percent")
                return value if isinstance(value, (int, float)) else 101
            best = min(ok_rows, key=used_5h)
            windows = best.get("windows") or {}
            parts.append(f"{DIM}best:{RESET} {best['name']}")
            parts.append(window_text(windows, "5h", "5h"))
            parts.append(window_text(windows, "7d", "7d"))
        else:
            parts.append(f"{DIM}headroom: all accounts held{RESET}")
    print(" · ".join(parts))
    return 0
