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

## Codex is tracked, not routed (in this release)

Because Codex usage is log-derived and can't be cryptographically bound to the
login (see below), headroom does NOT make capacity-based routing/rotation
decisions for Codex accounts in this release — it would risk routing on
another account's stale numbers. Codex accounts are fully tracked on the
dashboard (best-effort), and `headroom codex` still launches on your first
configured Codex account; but `headroom pick/rotate/run` for Codex is held
with a clear message. Claude — which has a live, identity-bound usage API — is
the fully-routed provider. (Set `HEADROOM_CODEX_ROUTING=1` to opt in anyway.)
Codex routing returns once the app-server live read lands.

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

## Codex tracking is best-effort (log-derived), not real-time

Codex has no live usage API that headroom can call today, so Codex usage is
read from the CLI's own `rate_limits` session telemetry on disk. Consequences,
all surfaced honestly on the dashboard rather than hidden:

- an account you're actively using shows **Live**;
- an account that's been quiet shows **Idle — last seen Nh ago** with its last
  known reading (never promoted to "live", never counted as verified headroom
  by the router);
- an account that has never run Codex shows **Waiting — run Codex once to
  start tracking**;
- a genuinely rate-limited account shows **Limited — resets …**.

This is why Claude is the real-time first-class provider and Codex is labelled
best-effort. The durable fix is the Codex **app-server** (`account/rateLimits`)
live read; it initializes cleanly and is on the roadmap, gated on the method
being stable across Codex releases. Additional upstream gaps: session logs
don't reliably identity-stamp which user a `rate_limits` event belongs to
(openai/codex#16323) and some versions emit `rate_limits: null`
(openai/codex#14880). headroom binds telemetry to the slot's directory and
validates the event shape. If you recycle a Codex home between accounts,
delete `sessions/` when switching.

## `verified_local` identities are routable

When the network or provider CLI is unavailable, identity falls back to
local credential metadata and is labeled `verified_local` (visible in the
snapshot and on the dashboard). This keeps offline/air-gapped setups usable.
If you want provider-verified-only routing, treat `verified_local` as held —
open an issue if you want this as a config flag.

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
