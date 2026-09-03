# Known limits and design tradeoffs

Findings from an adversarial cross-model review (GPT-5.6, x-high effort,
2026-07-11) that are deliberate tradeoffs or blocked on upstream, documented
here so users can judge them for their own threat model.

## Claude usage binding is trust-on-first-use

The Anthropic usage endpoint identifies its organization in a response
header, but a login's *default* org (from `claude auth status`) can
legitimately differ from its *usage* org (multi-org accounts). headroom
therefore pins the usage-org fingerprint per slot on the first successful
read and holds the slot if it ever changes. The first read itself is
unpinned — if an attacker controls your config home *before* first use, TOFU
cannot detect it (they could also just take the credentials). Run
`headroom collect` once right after connecting to close the window.

## Anthropic throttles the usage feed per account, with up to 1h Retry-After

`/api/oauth/usage` (the same read the Claude Code CLI makes for its own
usage display) answers `429 rate_limit_error` per *token*, not per IP: on
2026-09-01 one Max slot was throttled with `Retry-After: 571` while four
sibling slots answered 200 in the same second. The throttle seems to be an
hourly quota on that account: a slot that also runs live Claude Code sessions
(which poll the same endpoint) trips it far sooner than headroom's 5-minute
collect alone. headroom therefore holds only the throttled slot until its
Retry-After (ledger key `accounts.<provider>:<name>` in
`state/provider-backoff.json`) and keeps reading the others. A held slot shows
**Rate-limited** on the dashboard and is not routable until the window ends.

## Codex reads need a Codex CLI with the app-server

Codex usage is read live from `codex app-server`
(`account/rateLimits/read` + `account/read`), which requires a reasonably
recent Codex CLI. On an older Codex without the app-server, headroom falls
back to a best-effort read of the CLI's on-disk `rate_limits` session
telemetry — which is only current while you're actively using that account
and is held by the router (shown Idle/Waiting on the dashboard) until a fresh
reading appears. Set `HEADROOM_CODEX_ROUTING=0` to force Codex dashboard-only.

## A project's own CLI settings can override the selected provider

headroom scrubs provider-override environment variables before launching a
CLI, but Claude Code and Codex also read their OWN config after startup — a
project `.claude/settings.json` with an `env` block or `apiKeyHelper`, or a
Codex `config.toml` custom provider, is applied by the CLI itself and can send
your session to a different provider/account than the slot headroom selected.
headroom can't override that from outside. If you use alternate-provider
settings (Bedrock/Vertex/custom gateways), headroom's account routing does not
apply to those sessions — use headroom only with direct OAuth/subscription
logins.

## The Codex fallback path (only when the app-server is unavailable)

The primary Codex read is the live app-server call above. If that fails (an
older Codex CLI), headroom falls back to the CLI's on-disk `rate_limits`
session telemetry, which is best-effort:

- an account you're actively using shows **Live**;
- a quiet account shows **Idle — last seen Nh ago** (held by the router);
- an account that has never run Codex shows **Waiting — run Codex once**;
- a rate-limited account shows **Limited — resets …**.

Upstream gaps that make the fallback best-effort: session logs don't reliably
identity-stamp which user a `rate_limits` event belongs to (openai/codex#16323)
and some versions emit `rate_limits: null` (openai/codex#14880). The live
app-server read has none of these problems — it returns identity-bound,
real-time data — so keeping your Codex CLI current is the way to get
first-class Codex tracking.

## `verified_local` identities are routable

When the network or provider CLI is unavailable, identity falls back to
local credential metadata and is labeled `verified_local` (visible in the
snapshot and on the dashboard). This keeps offline/air-gapped setups usable.
If you want provider-verified-only routing, treat `verified_local` as held —
open an issue if you want this as a config flag.

## Refresh tokens can die before the access token

Some Claude slots store a `refreshTokenExpiresAt` earlier than `expiresAt`
(hours, not days). headroom now refreshes over HTTP when either timestamp
is near, but if both have already lapsed the slot is held with
`claude_refresh_expired` — only `headroom connect <name>` can mint a new
login. Leaving the dashboard/collector off overnight can miss the window.

## Grok billing is the CLI-proxy feed, not a public usage API

Grok SuperGrok remaining allowance is read from
`https://cli-chat-proxy.grok.com/v1/billing?format=credits` with the local
`auth.json` bearer — the same path the Grok CLI uses for `/usage`. xAI does
not publish this as a third-party API. If the feed changes shape or auth,
headroom holds the slot (`grok_billing_unavailable`) instead of guessing.
Team principals have no supported usage surface yet and are held with
`grok_team_usage_unsupported`. API keys (`XAI_API_KEY`) are pay-per-token
rate limits, not the SuperGrok weekly pool, and are not treated as
subscription headroom.

Grok has no 5-hour session window. Routing and rotation use the weekly
pool only. Isolated slots are `GROK_HOME` directories; `grok login` writes
`auth.json` there.

## Antigravity is tracked, not rotated (its login is machine-wide)

Google Antigravity spend is read from the Cloud Code companion backend that
the Antigravity IDE and the `agy` CLI already use:

```
POST https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary
POST https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist
```

Google does not publish these as a third-party API. If the response changes
shape, headroom holds the slot (`agy_quota_unavailable`) instead of guessing,
and a bucket that carries a `remainingAmount` without a denominator is
dropped rather than turned into an invented percentage.

**Two limits worth knowing before you set an AGY slot up.**

**1. `agy` stores its token in the OS keyring, not in a file.** headroom prefers
file-backed tokens for isolated slots (<home>/oauth_creds.json, etc.).
On Windows, when adopting the default user home (`~`), headroom reads the
credentials directly from the Windows Credential Manager (`gemini:antigravity`)
and caches them to `~/.gemini/oauth_creds.json`. For isolated slot homes, headroom
looks in, most specific first:

```
<home>/oauth_creds.json
<home>/.gemini/oauth_creds.json
<home>/.gemini/antigravity-cli/oauth_creds.json
<home>/.gemini/antigravity/oauth_creds.json
```

The file is the standard google-auth-library shape (`access_token`,
`refresh_token`, `id_token`, `expiry_date` in **milliseconds**). headroom
refreshes it only with the `client_id`/`client_secret` the login itself wrote
next to the token — it embeds no Google OAuth client of its own, so it can
extend a grant you already made but never mint a new one. Without those
fields an expired token is terminal (`agy_refresh_expired`) and only `agy`
can renew it.

**2. There is no isolated AGY login.** `agy` has no headless login subcommand
and signs in through that same machine-wide keyring, so
`headroom connect <name> --provider agy` refuses the fresh-login path and
points you at adoption instead:

```
headroom connect antigravity --provider agy --adopt ~
```

Because a launch cannot be handed a different Google account than the one
already signed in, rotating AGY would be a lie — so the router holds every
AGY slot with *"Antigravity is tracked, not routed"*. The reading is still
collected, charted, alerted on and kept in history. If you do run per-slot,
file-backed AGY logins, set `HEADROOM_AGY_ROUTING=1` to route them like any
other provider.

**Windows.** Antigravity meters rolling per-model buckets and publishes no
fleet-wide weekly pool, so `7d` is `not_applicable` and `5h` carries the
shortest bucket — with its real length in `window_minutes`, since Google names
the window per bucket. Every other bucket is published as `scoped:<Model>`.
Code Assist throttles per user, so a 429 holds only that slot
(`usage_source_rate_limited`), never the whole fleet.

## File-based credentials required (macOS Keychain caveat)

headroom reads usage tokens from files (`.credentials.json`, `auth.json`).
Two cases where that isn't where the token lives:

- **macOS default Claude login.** Recent Claude Code on macOS can store its
  token in the system Keychain, so the default `~/.claude` has no readable
  `.credentials.json`. headroom will detect the identity but hold the account
  with a clear message. **Fix:** connect a *fresh* isolated login instead of
  adopting the default — `headroom connect work-fresh` runs `claude auth login`
  inside its own `CLAUDE_CONFIG_DIR`, which writes file-based credentials that
  headroom can read. (Linux/Windows default logins are already file-based.)
- **Codex `cli_auth_credentials_store = "keyring"`** and other non-file stores
  are likewise invisible; such slots show as not logged in.

## Scoped model caps aren't enforced on the generic `claude` route

`headroom claude` routes on the account-wide 5h/7d windows — it can't know
which model the Claude CLI will actually use, so it does NOT hold an account
just because one model's weekly cap (e.g. Opus) is exhausted (that would
wrongly block Sonnet/Haiku work on the same account). To gate on a specific
model's cap, name it: `headroom claude --model opus` holds when the Opus
weekly cap is full.

## `headroom run` retries are for idempotent commands

Rotation replays the whole command on the next account when a run *fails*
with a provider-limit error on stderr. If your command has side effects
before the limit hits, those side effects happen once per attempt. Use
`headroom claude`/`env`/`pick` for non-idempotent work.

## The local dashboard is plain HTTP on 127.0.0.1

`headroom serve` binds loopback only AND validates the `Host` header — a
non-loopback Host is rejected with 403, so a remote page can't reach it via
DNS-rebinding. What it does NOT have is authentication: any process on the
same machine using a normal loopback Host can read the served feed (the
sanitized public snapshot — emails redacted by default). For anything shared
or multi-user, put the static build behind your own web server and auth.
