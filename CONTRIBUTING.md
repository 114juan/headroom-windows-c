# Contributing

Thanks for looking at headroom! It's intentionally small, dependency-free, and
stdlib-only — please keep it that way.

## Ground rules

- **No runtime dependencies.** Python 3.9+ standard library only. If you reach
  for a package, there's almost always a stdlib way.
- **Fail closed.** Anything touching routing or identity must default to
  HOLDING when state is missing, stale, corrupt, or unverifiable. When in
  doubt, don't route. New routing/identity logic needs a test proving the
  unhappy path holds.
- **Never widen the public feed.** `collect.public_snapshot()` has an explicit
  field whitelist. Don't add raw provider strings, paths, or identity material
  to it.
- **Match the house style.** Terse, commented only where a constraint isn't
  obvious from the code.

## Running the tests

```bash
python3 -m unittest discover -s tests
```

No pytest, no fixtures framework — plain `unittest`, no network.

## Handy while developing

```bash
headroom serve --demo     # dashboard on bundled sample data, no accounts
headroom doctor           # what headroom sees on this machine
python3 -m py_compile headroom/*.py
```

## Scope

headroom tracks and routes accounts you already hold. Features that create
accounts, share credentials, or work around provider limits are out of scope.
