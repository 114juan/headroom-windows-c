"""Filesystem layout and atomic JSON I/O.

Everything headroom owns lives under one directory (default ``~/.headroom``,
override with ``HEADROOM_DIR``):

    config.json          account registry + dashboard preferences
    homes/<name>/        isolated CLI config home per connected account
    state/               snapshots, cooldowns, backoff ledgers (private)
    state/public/        the sanitized snapshot + dashboard build
"""
import json
import os
import tempfile


def base_dir():
    raw = os.environ.get("HEADROOM_DIR") or "~/.headroom"
    expanded = os.path.expanduser(raw)
    # A relative HEADROOM_DIR would resolve against the current directory, so
    # state/credentials would scatter per-cwd and the cooldown belt would be
    # silently forgotten from a new directory. Refuse it rather than normalize.
    if not os.path.isabs(expanded):
        raise ValueError(
            f"HEADROOM_DIR must be an absolute path (got {raw!r})")
    return os.path.abspath(expanded)


def ensure_private(directory):
    os.makedirs(directory, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def config_path():
    return os.path.join(base_dir(), "config.json")


def homes_dir():
    return os.path.join(base_dir(), "homes")


def state_dir():
    return os.path.join(base_dir(), "state")


def public_dir():
    return os.path.join(state_dir(), "public")


def private_snapshot_path():
    return os.path.join(state_dir(), "usage-private.json")


def public_snapshot_path():
    return os.path.join(public_dir(), "usage.json")


def cooldowns_path():
    return os.path.join(state_dir(), "cooldowns.json")


def backoff_path():
    return os.path.join(state_dir(), "provider-backoff.json")


def collect_lock_path():
    return os.path.join(state_dir(), "collect.lock")


def load_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def replace_atomic(src, dst):
    """Robust os.replace with retries on Windows for sharing/access violations."""
    import sys
    if sys.platform != "win32":
        os.replace(src, dst)
        return

    import errno
    import time
    max_retries = 10
    backoff = 0.001
    for i in range(max_retries):
        try:
            os.replace(src, dst)
            return
        except OSError as e:
            # WinError 5: Access denied, WinError 32: Sharing violation
            if getattr(e, "winerror", None) in (5, 32) or e.errno in (errno.EACCES, errno.EEXIST):
                if i == max_retries - 1:
                    raise
                time.sleep(backoff)
                backoff *= 2
            else:
                raise


def write_json_atomic(path, value, mode=0o600):
    """Write JSON so readers never observe a partial file."""
    ensure_private(base_dir())
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".headroom-", suffix=".json.tmp", dir=directory
    )
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        replace_atomic(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def safe_subprocess_args(cmd_list):
    """Return a tuple of (command, shell) safe for execution on the host OS."""
    import sys
    import subprocess
    import shutil
    
    if sys.platform != "win32":
        return cmd_list, False
        
    if not cmd_list:
        return cmd_list, False
        
    exe = shutil.which(cmd_list[0]) or cmd_list[0]
    exe_lower = exe.lower()
    
    if exe_lower.endswith((".cmd", ".bat")):
        if '"' in exe or any('"' in arg for arg in cmd_list[1:]):
            raise ValueError("Paths and arguments cannot contain double quotes.")
        cmd_line = subprocess.list2cmdline(cmd_list)
        return f'cmd.exe /s /c "{cmd_line}"', False
    elif exe_lower.endswith(".ps1"):
        new_cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", exe] + cmd_list[1:]
        return new_cmd, False
        
    return cmd_list, False


def prepare_subprocess(command):
    """
    Given a command list, e.g. ["claude", "auth", "login"], resolves the executable on Windows,
    and returns a tuple (new_command, shell_flag) suitable for subprocess calls.
    Avoids cmd.exe quote-stripping bugs and correctly launches .ps1 files on Windows.
    """
    import sys
    import shutil
    import subprocess
    
    if sys.platform != "win32" or not command:
        return command, False
        
    exe = shutil.which(command[0]) or command[0]
    exe_lower = exe.lower()
    
    if exe_lower.endswith(".ps1"):
        new_cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", exe] + command[1:]
        return new_cmd, False
    elif exe_lower.endswith((".cmd", ".bat")):
        # Use cmd.exe /s /c and wrap with extra quotes to prevent quote-stripping
        cmdline = subprocess.list2cmdline([exe] + command[1:])
        return f'cmd.exe /s /c "{cmdline}"', False
    else:
        # For standard executables or unresolved commands
        return [exe] + command[1:], False

