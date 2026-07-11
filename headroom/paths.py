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
    # normalize once: a relative HEADROOM_DIR must never scatter credentials
    # and state into whatever directory the command happens to run from
    return os.path.abspath(os.path.expanduser(raw))


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
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def write_json_atomic(path, value, mode=0o600):
    """Write JSON so readers never observe a partial file."""
    ensure_private(base_dir())
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".headroom-", suffix=".json.tmp", dir=directory
    )
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(value, handle, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
