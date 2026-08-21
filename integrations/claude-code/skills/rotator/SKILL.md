---
name: rotator
description: "Rotate to the next connected account with proven headroom when a usage limit hits, carrying the current conversation over so nothing is lost. Triggers: /rotator, /rotate, rotate the account, hit my session limit, hit the weekly limit, out of usage, out of tokens, switch account, no headroom left, which account has capacity. Uses the headroom CLI to pick the next eligible login, cool the exhausted one down until its window resets, and sync this project's conversation to the new account."
---

# rotator

Rotate BETWEEN connected logins for the same provider when one hits a usage
limit. Powered by the `headroom` CLI (https://github.com/domanski-ai/headroom).

## When to fire

- The user types `/rotator` or `/rotate` (optionally with a model, default `claude`; use `grok` or `codex` when that is the limited session).
- A command or session fails with a session/weekly/usage-limit or 429 error.
- The user asks which account has capacity, or says they are out of usage/tokens.

## What to do

1. Run `headroom rotate <model>` (default model family: `claude`).
   It cools the current account down until its window resets, picks the next
   login with PROVEN headroom, and — key part — carries this project's
   conversation over to the new account's home (instant when share-history
   junctions are set up; copy-if-newer otherwise).
2. Relay the rotation result to the user:
   `rotated <old> -> <new> (<family>); <old> cools until <reset>`,
   plus the `conversation:` line if headroom printed one.
3. Tell the user how to continue THIS conversation on the new account —
   the running process keeps its old login, so they should exit and run ONE of:
   - `headroom claude -c` — launch on the best account, continue this
     project's latest conversation exactly where it left off; or
   - `headroom rotate claude --launch` — rotate and relaunch in one step
     (only from a plain terminal, not from inside a session).
   No manual context summary is needed when the conversation was carried over.
4. Only if headroom printed a `CONTEXT SUMMARY FROM PREVIOUS SESSION` block
   (meaning there was no transcript to sync), use that summary to help the
   user resume their tasks in the new session.
5. If it exits 2, every account is limited. Report the earliest reset time it
   printed — never silently fail, never downgrade the model.
6. For a status view first, run `headroom status <model>` and show the table.

## Guardrails

- Never edit `~/.headroom/config.json` by hand from this skill; account
  changes go through `headroom connect` / `headroom setup`.
- Cooldowns are fail-closed: if headroom says nobody has proven capacity,
  believe it. Do not retry the limited account "just in case".
- The new account takes effect for NEW processes that inherit
  `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, or `GROK_HOME`. The current interactive session keeps
  its own login — but the conversation follows: exiting and running
  `headroom claude -c` resumes it on the new account with full history.
- Recommend `headroom share-history` once if the `conversation:` line says
  files were copied rather than `shared history` — junctions make future
  rotations instant and bidirectional.
