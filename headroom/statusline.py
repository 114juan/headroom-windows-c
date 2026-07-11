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
    current_home = os.path.realpath(
        os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude")))
    current = None
    try:
        for account in registry.accounts():
            if os.path.realpath(account["home"]) == current_home:
                current = account
                break
    except registry.RegistryError:
        pass
    parts = []
    if current and current["name"] in rows:
        row = rows[current["name"]]
        windows = row.get("windows") or {}
        parts.append(f"{current['name']}")
        parts.append(window_text(windows, "5h", "5h"))
        parts.append(window_text(windows, "7d", "7d"))
        used = (windows.get("5h") or {}).get("used_percent")
        if used is not None and used >= 75:
            from . import route
            candidate = next(
                (account for account, reason in route.candidates(
                    "claude", snapshot)
                 if reason is None and account["name"] != current["name"]),
                None)
            if candidate:
                parts.append(f"{DIM}next: {candidate['name']}{RESET}")
    else:
        ok_rows = [row for row in rows.values()
                   if row.get("ok") and row.get("provider") == "claude"]
        if ok_rows:
            best = min(ok_rows, key=lambda row: (
                (row.get("windows", {}).get("5h") or {}).get("used_percent")
                or 100))
            windows = best.get("windows") or {}
            parts.append(f"{DIM}best:{RESET} {best['name']}")
            parts.append(window_text(windows, "5h", "5h"))
            parts.append(window_text(windows, "7d", "7d"))
        else:
            parts.append(f"{DIM}headroom: all accounts held{RESET}")
    print(" · ".join(parts))
    return 0
