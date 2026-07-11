"""Account registry: load, validate, and query config.json.

The registry is intentionally boring. Each account is a named *slot* bound to
one provider and one isolated CLI config home. Identity (email, plan) is
*discovered* from the provider at collect time, never trusted from config —
config only records what the operator expects, so a clobbered login can be
detected.

Config shape (schema_version 1)::

    {
      "schema_version": 1,
      "dashboard": {"theme": "midnight", "title": "AI Fleet",
                     "redact_emails": false, "port": 8377},
      "accounts": [
        {"name": "personal", "provider": "claude",
         "home": "~/.claude",  # or ~/.headroom/homes/personal
         "expected_email": "me@example.com"}   # optional but recommended
      ]
    }
"""
import os

from . import paths

PROVIDERS = ("claude", "codex")
DEFAULT_DASHBOARD = {
    "theme": "midnight",
    "title": "AI Fleet",
    "redact_emails": False,
    "port": 8377,
}

# Model-family -> provider. `pick`/`run` accept any model string; family()
# reduces it to one of these.
FAMILY_PROVIDER = {
    "opus": "claude",
    "sonnet": "claude",
    "haiku": "claude",
    "fable": "claude",
    "claude": "claude",
    "codex": "codex",
    "gpt": "codex",
}


class RegistryError(ValueError):
    pass


def family(model):
    model = (model or "").lower()
    for name in ("fable", "opus", "sonnet", "haiku", "codex", "gpt"):
        if name in model:
            return "codex" if name == "gpt" else name
    if "claude" in model:
        return "claude"
    return "claude"


def family_provider(fam):
    return FAMILY_PROVIDER.get(fam, "claude")


def expand(path):
    return os.path.abspath(os.path.expanduser(path))


def validate(config):
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise RegistryError("config.json missing or wrong schema_version (expected 1)")
    accounts = config.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise RegistryError("config.json has no accounts; run `headroom setup`")
    names, homes = set(), set()
    for account in accounts:
        if not isinstance(account, dict):
            raise RegistryError("account entries must be objects")
        name = account.get("name")
        provider = account.get("provider")
        home = account.get("home")
        if not isinstance(name, str) or not name or name in names:
            raise RegistryError(f"account name missing/duplicate: {name!r}")
        if provider not in PROVIDERS:
            raise RegistryError(f"account {name}: provider must be one of {PROVIDERS}")
        if not isinstance(home, str) or not home:
            raise RegistryError(f"account {name}: home missing")
        resolved = expand(home)
        if resolved in homes:
            raise RegistryError(f"account {name}: home {resolved} already used by another account")
        names.add(name)
        homes.add(resolved)
    return config


def load():
    config = paths.load_json(paths.config_path())
    if config is None:
        raise RegistryError(
            f"no config at {paths.config_path()}; run `headroom setup` first"
        )
    return validate(config)


def accounts(config=None):
    config = load() if config is None else config
    result = []
    for account in config["accounts"]:
        row = dict(account)
        row["home"] = expand(row["home"])
        result.append(row)
    return result


def dashboard_settings(config=None):
    config = load() if config is None else config
    settings = dict(DEFAULT_DASHBOARD)
    settings.update(config.get("dashboard") or {})
    return settings


def ordered_for(fam, config=None):
    """Accounts eligible for a model family, in registry (preference) order."""
    provider = family_provider(fam)
    return [account for account in accounts(config) if account["provider"] == provider]


def save(config):
    validate(config)
    paths.write_json_atomic(paths.config_path(), config, mode=0o600)
