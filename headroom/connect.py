"""Connect accounts smoothly — and never let a login clobber another slot.

Two paths:

* ``adopt``   — register a login that already exists on this machine
                (your current ``~/.claude`` or ``~/.codex``). Zero friction:
                headroom just reads it, it never moves or copies credentials.
* ``fresh``   — create an isolated config home under ``~/.headroom/homes/``
                and run the provider's own interactive login inside it
                (``claude auth login`` / ``codex login``).

Every fresh login is verified afterwards: if it bound the slot to an identity
that is already connected on another slot, the credentials are rolled back and
the connect is refused — duplicate logins silently eating each other's
headroom is the classic multi-account failure mode.
"""
import os
import shutil
import subprocess
import sys
import time

from . import collect as collector
from . import paths, registry

CREDENTIAL_FILES = {
    "claude": [".credentials.json", ".claude.json"],
    "codex": ["auth.json"],
}
DEFAULT_HOMES = {"claude": "~/.claude", "codex": "~/.codex"}


def provider_binary(provider):
    return shutil.which("claude" if provider == "claude" else "codex")


def login_argv(provider, binary):
    return [binary, "auth", "login"] if provider == "claude" else [binary, "login"]


def slot_identity(provider, home):
    """Best-effort identity read for a slot; None when nothing is bound."""
    try:
        if provider == "claude":
            identity = collector.claude_identity(home)
        else:
            identity = collector.codex_identity(home)
        return identity
    except Exception:  # noqa: BLE001 — absence of identity is a valid answer
        return None


def detect_existing():
    """Discover logins already on this machine, for the wizard/adopt flow."""
    found = []
    for provider, default in DEFAULT_HOMES.items():
        home = os.path.expanduser(
            os.environ.get(
                "CLAUDE_CONFIG_DIR" if provider == "claude" else "CODEX_HOME",
                default,
            )
        )
        if not os.path.isdir(home):
            continue
        identity = slot_identity(provider, home)
        if identity and identity.get("email"):
            found.append({"provider": provider, "home": home,
                          "email": identity["email"],
                          "fingerprint": identity.get("account_fingerprint")})
    return found


def backup_credentials(home, provider):
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    directory = os.path.join(home, ".headroom-login-backups", stamp)
    saved = []
    for filename in CREDENTIAL_FILES[provider]:
        source = os.path.join(home, filename)
        if os.path.exists(source):
            os.makedirs(directory, exist_ok=True)
            shutil.copy2(source, os.path.join(directory, filename))
            saved.append(filename)
    return directory if saved else None, saved


def restore_credentials(home, provider, backup_dir, saved):
    for filename in CREDENTIAL_FILES[provider]:
        target = os.path.join(home, filename)
        if filename in saved:
            shutil.copy2(os.path.join(backup_dir, filename), target)
        elif os.path.exists(target):
            os.remove(target)


def existing_fingerprints(config, provider):
    result = {}
    for account in registry.accounts(config):
        if account["provider"] != provider:
            continue
        identity = slot_identity(provider, account["home"])
        if identity and identity.get("account_fingerprint"):
            result[identity["account_fingerprint"]] = account["name"]
    return result


def add_account(config, name, provider, home, expected_email=None):
    entry = {"name": name, "provider": provider, "home": home}
    if expected_email:
        entry["expected_email"] = expected_email
    config.setdefault("accounts", []).append(entry)
    registry.save(config)
    return entry


def connect_fresh(config, name, provider, quiet=False):
    """Isolated home + interactive provider login + verify + rollback."""
    binary = provider_binary(provider)
    if not binary:
        print(f"cannot find the `{'claude' if provider == 'claude' else 'codex'}` "
              f"CLI on PATH — install it first", file=sys.stderr)
        return None
    home = os.path.join(paths.homes_dir(), name)
    os.makedirs(home, exist_ok=True)
    backup_dir, saved = backup_credentials(home, provider)
    duplicates = existing_fingerprints(config, provider)

    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR" if provider == "claude" else "CODEX_HOME"] = home
    if not quiet:
        print(f"\nStarting the {provider} login for slot '{name}'.")
        print("Complete the browser flow with the account you want on THIS slot.\n")
    code = subprocess.run(login_argv(provider, binary), env=env).returncode
    if code != 0:
        if backup_dir:
            restore_credentials(home, provider, backup_dir, saved)
        print(f"login exited {code}; slot unchanged", file=sys.stderr)
        return None

    identity = slot_identity(provider, home)
    if not identity or not identity.get("email"):
        if backup_dir:
            restore_credentials(home, provider, backup_dir, saved)
        print("login completed but no identity could be read; rolled back",
              file=sys.stderr)
        return None
    fingerprint = identity.get("account_fingerprint")
    if fingerprint in duplicates:
        if backup_dir:
            restore_credentials(home, provider, backup_dir, saved)
        else:
            for filename in CREDENTIAL_FILES[provider]:
                target = os.path.join(home, filename)
                if os.path.exists(target):
                    os.remove(target)
        print(f"REFUSED: that login ({identity['email']}) is already connected "
              f"as slot '{duplicates[fingerprint]}'. Slot rolled back.\n"
              f"Log in with a different account, or use the existing slot.",
              file=sys.stderr)
        return None

    entry = add_account(config, name, provider, home, identity["email"])
    if not quiet:
        print(f"connected: {name} -> {identity['email']} ({provider})")
    return entry


def connect_adopt(config, name, provider, home, quiet=False):
    home = os.path.expanduser(home)
    identity = slot_identity(provider, home)
    if not identity or not identity.get("email"):
        print(f"no {provider} login found in {home}", file=sys.stderr)
        return None
    duplicates = existing_fingerprints(config, provider)
    fingerprint = identity.get("account_fingerprint")
    if fingerprint in duplicates:
        print(f"that login ({identity['email']}) is already connected as slot "
              f"'{duplicates[fingerprint]}'", file=sys.stderr)
        return None
    entry = add_account(config, name, provider, home, identity["email"])
    if not quiet:
        print(f"connected: {name} -> {identity['email']} ({provider}, adopted {home})")
    return entry


def cmd_connect(args):
    """CLI: `headroom connect [name] [--provider claude|codex] [--adopt PATH]`."""
    try:
        config = registry.load()
    except registry.RegistryError:
        config = {"schema_version": 1,
                  "dashboard": dict(registry.DEFAULT_DASHBOARD),
                  "accounts": []}
    name = None
    provider = None
    adopt_path = None
    rest = list(args)
    while rest:
        arg = rest.pop(0)
        if arg == "--provider" and rest:
            provider = rest.pop(0)
        elif arg == "--adopt" and rest:
            adopt_path = rest.pop(0)
        elif not arg.startswith("-") and name is None:
            name = arg
    if provider not in registry.PROVIDERS:
        provider = prompt_choice("Which provider is this account for?",
                                 ["claude", "codex"])
    if name is None:
        taken = {account["name"] for account in config.get("accounts", [])}
        default = next(
            candidate for candidate in
            [f"{provider}-{index}" for index in range(1, 100)]
            if candidate not in taken)
        name = input(f"Slot name for this account [{default}]: ").strip() or default
    if any(account.get("name") == name for account in config.get("accounts", [])):
        print(f"slot '{name}' already exists", file=sys.stderr)
        return 1
    entry = (connect_adopt(config, name, provider, adopt_path)
             if adopt_path else connect_fresh(config, name, provider))
    return 0 if entry else 1


def prompt_choice(question, options, default_index=0):
    print(question)
    for index, option in enumerate(options, 1):
        marker = " (default)" if index - 1 == default_index else ""
        print(f"  {index}. {option}{marker}")
    while True:
        raw = input("> ").strip()
        if not raw:
            return options[default_index]
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        if raw in options:
            return raw
        print("pick a number from the list")
