# How headroom works

```
                       ┌─────────────────────────────┐
                       │  ~/.headroom/config.json     │  what you EXPECT
                       │  accounts: name→provider→home│  (never trusted blindly)
                       └──────────────┬──────────────┘
                                      │
      every 10 min / on demand        ▼
  ┌────────────┐   OAuth usage API  ┌────────────────┐
  │  Claude     │◄──────────────────│                │
  │  provider   │  (read-only, the  │   collect      │──► state/usage-private.json
  └────────────┘   app's own call)  │  identity-bound│        (0600, full detail)
  ┌────────────┐   session logs     │   fail-closed  │──► state/public/usage.json
  │  Codex CLI  │◄──────────────────│                │        (sanitized, dashboard)
  │  telemetry  │   (on disk, free) └────────────────┘
  └────────────┘                            │
                                            ▼
              ┌──────────────┐      ┌──────────────┐
              │  dashboard   │      │    route     │
              │ index.html + │      │ pick/run/    │──► CLAUDE_CONFIG_DIR /
              │  usage.json  │      │ rotate +     │    CODEX_HOME env for
              │  (5 themes)  │      │ cooldowns    │    the chosen account
              └──────────────┘      └──────────────┘
```

## The identity model

Every account is a *slot*: a name, a provider, and an isolated CLI config
home. The provider's own login flow binds an identity (email + org/account
id) *into* that home. headroom then:

1. reads the bound identity back (via `claude auth status` / the OpenAI
   userinfo endpoint, falling back to local metadata when offline),
2. fingerprints the provider account id (SHA-256, truncated — the raw id
   never leaves the private snapshot),
3. verifies at usage-read time that the usage response belongs to the same
   fingerprint (Claude returns the organization id in a response header).

Consequences:

- a login that got clobbered (you logged into the wrong account in the wrong
  terminal) is detected and HELD, not silently mixed in;
- two slots that turn out to be the same login are both held with a
  `duplicate_identity` warning — otherwise the router would "rotate" onto
  the same exhausted quota;
- `expected_email` in config (set automatically by `headroom connect`) pins a
  slot to a specific identity permanently.

## The windows

| window | provider | meaning |
|---|---|---|
| `5h` | both | rolling session window |
| `7d` | both | weekly all-models window |
| `scoped:<Model>` | Claude | weekly cap for a specific model tier (e.g. Opus) |

The dashboard and router treat *remaining* capacity (100 − used) as the
primary number. The router additionally honours provider `severity` flags and
holds anything at 100%.

## Cooldowns

A limit-hit writes `"<account>:<family>": <reset-epoch>` into
`state/cooldowns.json`. The reset epoch comes from the provider's own
`resets_at` when known, else a conservative default (5h for session, 7d for
weekly). Cooldowns expire on their own; `headroom clear` removes them early.

## Staleness

Routing decisions require a snapshot younger than `HEADROOM_SNAPSHOT_MAX_AGE`
(default 900s). Older snapshots trigger an inline re-collect. If collection
fails, the router *does not* fall back to the stale data — no account gets
picked on unproven capacity.

## Files

| path | perms | contents |
|---|---|---|
| `~/.headroom/config.json` | 0600 | slots + dashboard preferences |
| `~/.headroom/homes/<name>/` | provider-managed | isolated CLI credentials |
| `~/.headroom/state/usage-private.json` | 0600 | full snapshot incl. identity fingerprints |
| `~/.headroom/state/public/usage.json` | 0644 | sanitized dashboard feed |
| `~/.headroom/state/cooldowns.json` | 0600 | active cooldowns |
| `~/.headroom/state/provider-backoff.json` | 0600 | usage-endpoint 429 backoff |

## Environment overrides

Everything is overridable for testing or custom layouts: `HEADROOM_DIR`,
`HEADROOM_SNAPSHOT_MAX_AGE`, `HEADROOM_CODEX_STALE_AFTER`,
`HEADROOM_IDENTITY_TIMEOUT`, `HEADROOM_SERVE_MAX_AGE`, `HEADROOM_BIN_DIR`.
