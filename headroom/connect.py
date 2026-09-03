"""Connect accounts smoothly — and never let a login clobber another slot.

Two paths:

* ``adopt``   — register a login that already exists on this machine
                (your current ``~/.claude``, ``~/.codex``, ``~/.grok``, or
                the home holding Antigravity's ``.gemini``).
                Zero friction: headroom just reads it, it never moves or
                copies credentials.
* ``fresh``   — create an isolated config home under ``~/.headroom/homes/``
                and run the provider's own interactive login inside it
                (``claude auth login`` / ``codex login`` / ``grok login``).

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

from . import agy as agy_provider
from . import collect as collector
from . import paths, registry

CREDENTIAL_FILES = {
    "claude": [".credentials.json", ".claude.json"],
    "codex": ["auth.json"],
    "grok": ["auth.json"],
    # Antigravity writes wherever the login found a home; back up every place
    # collect is willing to read one from.
    "agy": [path.replace("/", os.sep) for path in agy_provider.CREDENTIAL_FILES],
}
DEFAULT_HOMES = dict(registry.DEFAULT_HOMES)


def provider_binary(provider):
    binary = {"claude": "claude", "codex": "codex", "grok": "grok",
              "agy": "agy"}.get(provider)
    return shutil.which(binary) if binary else None


def login_argv(provider, binary):
    return [binary, "auth", "login"] if provider == "claude" else [binary, "login"]


def slot_identity(provider, home):
    """Best-effort identity read for a slot; None when nothing is bound."""
    try:
        if provider == "claude":
            identity = collector.claude_identity(home)
        elif provider == "grok":
            identity = collector.grok_identity(home)
        elif provider == "agy":
            try:
                identity = collector.agy_local_identity(home)
            except collector.IdentityBindingError as error:
                if error.code != "agy_identity_email_missing":
                    raise
                _path, creds = collector.agy_credential_file(home)
                token = agy_provider.access_token(creds)
                if not token:
                    raise
                info_status, info = collector._agy_get(
                    agy_provider.USERINFO_URL, token, None, 10)
                email = (info or {}).get("email")
                if not email or not isinstance(email, str):
                    raise
                identity = {
                    "verified": False,
                    "email": email,
                    "account_fingerprint": collector.fingerprint(info.get("sub") or email),
                    "method": "agy_userinfo",
                    "credential_digest": collector.credential_digest("agy", home),
                }
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
            os.environ.get(registry.HOME_ENV[provider], default)
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
            os.makedirs(directory, mode=0o700, exist_ok=True)
            os.chmod(os.path.dirname(directory), 0o700)
            target = os.path.join(directory, filename)
            # a provider credential can sit in a subdirectory of the home
            os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
            shutil.copy2(source, target)
            saved.append(filename)
    return directory if saved else None, saved


def discard_backup(backup_dir):
    if backup_dir:
        shutil.rmtree(backup_dir, ignore_errors=True)


def restore_credentials(home, provider, backup_dir, saved):
    for filename in CREDENTIAL_FILES[provider]:
        target = os.path.join(home, filename)
        if filename in saved:
            os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
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
    # always store an absolute, canonical home — a relative path would resolve
    # against whatever directory a later command runs from
    entry = {"name": name, "provider": provider, "home": registry.expand(home)}
    if expected_email:
        entry["expected_email"] = expected_email

    previous_email = None
    for acct in config.get("accounts", []):
        if acct.get("name") == name:
            previous_email = acct.get("expected_email")
            break

    def _upsert(cfg):
        for acct in cfg.get("accounts", []):
            if acct.get("name") == name:
                # re-login: drop the old usage-org pin so the next collect
                # re-binds to whatever org this login actually reports
                acct.update(entry)
                acct.pop("pinned_usage_org", None)
                return
        cfg.setdefault("accounts", []).append(dict(entry))

    try:
        # locked reload-append against the latest on-disk config, so a
        # concurrent collector pin-write or connect can't drop this account
        registry.mutate(_upsert)
    except registry.RegistryError:
        # config doesn't exist yet (wizard building a fresh one) — create it
        config.setdefault("accounts", []).append(entry)
        registry.save(config)
        return entry
    # reflect into the caller's in-memory config too (the wizard keeps using it)
    for acct in config.get("accounts", []):
        if acct.get("name") == name:
            acct.update(entry)
            acct.pop("pinned_usage_org", None)
            break
    else:
        config.setdefault("accounts", []).append(entry)
    if (previous_email and expected_email
            and previous_email.lower() != expected_email.lower()):
        print(f"warning: slot '{name}' expected {previous_email}, "
              f"now bound to {expected_email}", file=sys.stderr)
    return entry


def connect_fresh(config, name, provider, quiet=False):
    """Isolated home + interactive provider login + verify + rollback."""
    if provider == "agy":
        # ``agy`` has no headless login subcommand and stores its token in the
        # machine-wide OS keyring, so an isolated home cannot hold a second
        # Antigravity account. Adopt the login that is already signed in.
        print("Antigravity has no isolated login: `agy` signs in through the "
              "OS keyring, one account per desktop user.\n"
              "Sign in with the Antigravity IDE or `agy`, then adopt it:\n"
              f"  headroom connect {name} --provider agy --adopt ~\n"
              "headroom reads a file-backed token "
              "(<home>/oauth_creds.json or <home>/.gemini/oauth_creds.json); "
              "see docs/KNOWN-LIMITS.md.", file=sys.stderr)
        return None
    binary = provider_binary(provider)
    if not binary:
        print(f"cannot find the `{provider}` CLI on PATH — install it first",
              file=sys.stderr)
        return None
    if not registry.NAME_RE.fullmatch(name):
        print(f"slot name {name!r} invalid: lowercase letters, digits, - and _ "
              f"only (max 32 chars)", file=sys.stderr)
        return None
    home = os.path.join(paths.homes_dir(), name)
    if os.path.normcase(os.path.realpath(home)) != os.path.normcase(os.path.realpath(
            os.path.join(paths.homes_dir(), os.path.basename(name)))):
        print("slot name resolves outside the homes directory; refused",
              file=sys.stderr)
        return None
    os.makedirs(home, mode=0o700, exist_ok=True)
    existing = next((a for a in config.get("accounts", [])
                     if a.get("name") == name), None)
    if existing and not quiet:
        expected = existing.get("expected_email") or "unknown"
        print(f"re-login slot '{name}' (expected {expected})")
    backup_dir, saved = backup_credentials(home, provider)
    duplicates = existing_fingerprints(config, provider)

    def rollback():
        if backup_dir:
            restore_credentials(home, provider, backup_dir, saved)
        else:
            for filename in CREDENTIAL_FILES[provider]:
                target = os.path.join(home, filename)
                if os.path.exists(target):
                    os.remove(target)

    env = collector.scrubbed_env()
    env[registry.HOME_ENV[provider]] = home
    if not quiet:
        print(f"\nStarting the {provider} login for slot '{name}'.")
        print("Complete the browser flow with the account you want on THIS slot.\n")
    completed = False
    try:
        cmd_args, use_shell = paths.prepare_subprocess(login_argv(provider, binary))
        code = subprocess.run(cmd_args, env=env, shell=use_shell).returncode
        if code != 0:
            print(f"login exited {code}; slot unchanged", file=sys.stderr)
            return None
        identity = slot_identity(provider, home)
        if not identity or not identity.get("email"):
            print("login completed but no identity could be read; rolled back",
                  file=sys.stderr)
            return None
        fingerprint = identity.get("account_fingerprint")
        if fingerprint in duplicates and duplicates[fingerprint] != name:
            print(f"REFUSED: that login ({identity['email']}) is already "
                  f"connected as slot '{duplicates[fingerprint]}'. Slot rolled "
                  f"back.\nLog in with a different account, or use the "
                  f"existing slot.", file=sys.stderr)
            return None
        entry = add_account(config, name, provider, home, identity["email"])
        completed = True
        if not quiet:
            print(f"connected: {name} -> {identity['email']} ({provider})")
        return entry
    finally:
        if not completed:
            rollback()
            # tidy the slot dir we created if the connect was refused and it's
            # now empty (credentials were rolled back)
            try:
                if os.path.isdir(home) and not os.listdir(home):
                    os.rmdir(home)
            except OSError:
                pass
        discard_backup(backup_dir)


def connect_adopt(config, name, provider, home, quiet=False):
    home = os.path.expanduser(home)
    identity = slot_identity(provider, home)
    if not identity or not identity.get("email"):
        print(f"no {provider} login found in {home}", file=sys.stderr)
        return None
    duplicates = existing_fingerprints(config, provider)
    fingerprint = identity.get("account_fingerprint")
    if fingerprint in duplicates and duplicates[fingerprint] != name:
        print(f"that login ({identity['email']}) is already connected as slot "
              f"'{duplicates[fingerprint]}'", file=sys.stderr)
        return None
    entry = add_account(config, name, provider, home, identity["email"])
    if not quiet:
        print(f"connected: {name} -> {identity['email']} ({provider}, adopted {home})")
    return entry


def cmd_connect(args):
    """CLI: `headroom connect [name] [--provider claude|codex|grok|agy] [--adopt PATH]`."""
    try:
        config = registry.load()
    except registry.RegistryError:
        if os.path.exists(paths.config_path()):
            # a corrupt existing config must be repaired, never silently
            # replaced with an empty one that then overwrites every slot
            print(f"headroom: {paths.config_path()} exists but is unreadable; "
                  f"fix or delete it before connecting", file=sys.stderr)
            return 1
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

    existing_acct = next((a for a in config.get("accounts", []) if a.get("name") == name), None)
    if existing_acct:
        if adopt_path:
            print(f"slot '{name}' already exists", file=sys.stderr)
            return 1
        provider = existing_acct.get("provider", provider or "claude")
    else:
        if provider not in registry.PROVIDERS:
            provider = prompt_choice("Which provider is this account for?",
                                     list(registry.PROVIDERS))
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


def find_account(config, name_or_email):
    """Match a slot by name or expected_email (case-insensitive)."""
    name_clean = str(name_or_email or "").strip().lower()
    if not name_clean:
        return None
    for account in registry.accounts(config):
        if account.get("name", "").lower() == name_clean \
                or account.get("expected_email", "").lower() == name_clean:
            return account
    return None


def remove_account(config, name_or_email):
    """Drop a slot from the registry. The home directory is left in place
    (share-history junctions may point at shared_claude_state)."""
    account = find_account(config, name_or_email)
    if not account:
        return False, f"no account found matching {name_or_email!r}"
    name = account["name"]

    def _drop(cfg):
        before = cfg.get("accounts", [])
        remaining = [entry for entry in before if entry.get("name") != name]
        if len(remaining) == len(before):
            raise registry.RegistryError(f"no account named {name!r}")
        if not remaining:
            raise registry.RegistryError(
                "refusing to remove the last account "
                "(run `headroom setup` to start over)")
        cfg["accounts"] = remaining

    try:
        registry.mutate(_drop)
    except registry.RegistryError as error:
        return False, str(error)
    config["accounts"] = [entry for entry in config.get("accounts", [])
                          if entry.get("name") != name]
    try:
        from . import history as usage_history
        usage_history.remove_account(usage_history.slot_id(account),
                                     legacy_name=name)
    except Exception:
        pass
    email_str = f" ({account['expected_email']})" if account.get("expected_email") else ""
    return True, f"removed slot '{name}'{email_str} (home left in place)"


def clear_token(config, name_or_email):
    """Delete file-based tokens and credentials for a given account slot or email."""
    account = find_account(config, name_or_email)
    if not account:
        return False, f"no account found matching {name_or_email!r}"

    home = account["home"]
    removed = []

    failed = []
    if os.path.isdir(home):
        target_files = [".credentials.json", "auth.json", "mcp-needs-auth-cache.json"]
        for fname in target_files:
            fpath = os.path.join(home, fname)
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                    removed.append(fname)
                except OSError:
                    failed.append(fname)

        import glob
        for tmp_file in glob.glob(os.path.join(home, ".claude.json.tmp.*")):
            try:
                os.remove(tmp_file)
                removed.append(os.path.basename(tmp_file))
            except OSError:
                failed.append(os.path.basename(tmp_file))

        claude_json_path = os.path.join(home, ".claude.json")
        if os.path.exists(claude_json_path):
            data = paths.load_json(claude_json_path)
            if isinstance(data, dict) and "oauthAccount" in data:
                try:
                    data.pop("oauthAccount", None)
                    paths.write_json_atomic(claude_json_path, data)
                    removed.append(".claude.json:oauthAccount")
                except OSError:
                    failed.append(".claude.json")

    def _unpin(cfg):
        for acct in cfg.get("accounts", []):
            if acct.get("name") == account["name"]:
                acct.pop("pinned_usage_org", None)

    try:
        registry.mutate(_unpin)
    except registry.RegistryError:
        pass

    email_str = f" ({account['expected_email']})" if account.get("expected_email") else ""
    if failed:
        return False, (
            f"could not delete {', '.join(failed)} for slot "
            f"'{account['name']}'{email_str}")
    return True, f"cleared token for slot '{account['name']}'{email_str}"

