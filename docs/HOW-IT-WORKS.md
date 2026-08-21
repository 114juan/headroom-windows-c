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
              │  usage.json  │      │ rotate +     │    CODEX_HOME / GROK_HOME
              │  (5 themes)  │      │ cooldowns    │
              └──────────────┘      └──────────────┘
```

## The identity model

Every account is a *slot*: a name, a provider, and an isolated CLI config
home. The provider's own login flow binds an identity (email + org/account
id) *into* that home. headroom then:

1. reads the bound identity back from local slot metadata first
   (`.claude.json` / the Codex id token). The provider CLI
   (`claude auth status`) is a last resort and is never spawned when the
   slot has no credentials. Codex can re-verify live via userinfo.
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

## Claude token refresh

Access tokens die in about an hour (older slots closer to 8h). headroom
refreshes them with a read-only POST to Anthropic's OAuth token endpoint
(`platform.claude.com/v1/oauth/token`, with the legacy console URL as a
404/405 fallback). It never runs `claude --print` and never spends
inference quota.

Refresh happens when the access token is within 5 minutes of expiry, when
the refresh token itself is within an hour of dying (it can expire *before*
access), or when the usage API returns 401. A dead or rejected refresh
token holds the slot with `claude_refresh_expired` and tells you to run
`headroom connect <name>`.

## The windows

| window | provider | meaning |
|---|---|---|
| `5h` | Claude, Codex | rolling session window (Grok has none) |
| `7d` | all | weekly all-models / SuperGrok weekly pool |
| `scoped:<Model>` | Claude | weekly cap for a specific model tier (e.g. Opus) |
| `scoped:Extra` | Grok | Extra Usage Credits, when an on-demand cap is present |

The dashboard and router treat *remaining* capacity (100 − used) as the
primary number. The router additionally honours provider `severity` flags and
holds anything at 100%.

## Cooldowns

A limit-hit writes `"<account>:<scope>": <reset-epoch>` into
`state/cooldowns.json`. `<scope>` is `*` for a session or weekly-all limit
(account-wide — every model family on that account is held) and a specific
model family only for a genuine model-scoped cap. The reset epoch comes from
the provider's own `resets_at` when known, else a conservative future floor
(≥15 min for a session hit, ≥6 h for a weekly hit). Cooldowns expire on their
own; `headroom clear <account>` (or `<account>:<scope>`) removes them early,
and `headroom clear` with no argument resets all.

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
`HEADROOM_SNAPSHOT_MAX_AGE`, `HEADROOM_OBSERVATION_MAX_AGE`,
`HEADROOM_CLOCK_SKEW`, `HEADROOM_CODEX_STALE_AFTER`,
`HEADROOM_IDENTITY_TIMEOUT`, `HEADROOM_SERVE_MAX_AGE`, `HEADROOM_BIN_DIR`.
