"""headroom — usage tracking, live dashboard, and account rotation
for Claude Code and Codex subscriptions.

usage:
  headroom setup                    first-run wizard (accounts + dashboard style)
  headroom connect [name] [--provider claude|codex] [--adopt PATH]
                                    add an account (fresh login or adopt existing)
  headroom collect                  read usage for every account (no tokens spent)
  headroom status [model]           who has headroom right now (default: claude)
  headroom pick <model>             print the best account name (exit 2 if none)
  headroom env <model>              print the export line for the best account
  headroom claude [args...]         launch Claude Code on the best account
  headroom codex [args...]          launch Codex on the best account
  headroom run <model> -- <cmd...>  headless run with auto-rotation on limit-hit
  headroom rotate [model]           cool the current account down, pick the next
  headroom mark <name> <model> [epoch]   manual cooldown
  headroom clear [name:family]      clear cooldown(s)
  headroom dashboard                (re)build the static dashboard
  headroom serve [--open] [--port N]     local live dashboard
  headroom statusline               Claude Code status line output
  headroom accounts                 list connected accounts
"""
import sys

from . import __version__, registry


def main(argv=None):
    try:
        return _dispatch(sys.argv[1:] if argv is None else argv)
    except registry.RegistryError as error:
        print(f"headroom: {error}", file=sys.stderr)
        return 1
    except ValueError as error:
        # e.g. a relative HEADROOM_DIR — a config problem, not a crash
        print(f"headroom: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print()
        return 130
    except EOFError:
        print("\nheadroom: this command needs an interactive terminal "
              "(no input available on stdin).", file=sys.stderr)
        return 1


def _dispatch(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    command, args = argv[0], argv[1:]

    if command in ("-V", "--version", "version"):
        print(f"headroom {__version__}")
        return 0
    if command == "setup":
        from . import wizard
        return wizard.run_setup()
    if command == "connect":
        from . import connect
        return connect.cmd_connect(args)
    if command == "collect":
        from . import collect
        collect.run_collect()
        return 0
    if command == "status":
        from . import route
        return route.cmd_status(registry.family(args[0] if args else "claude"))
    if command == "pick":
        from . import route
        account = route.pick(registry.family(args[0] if args else "claude"))
        print(account["name"] if account else "")
        return 0 if account else 2
    if command == "env":
        import shlex

        from . import route
        account = route.pick(registry.family(args[0] if args else "claude"))
        if not account:
            print("# no account with proven headroom", file=sys.stderr)
            return 2
        print(f"export {route.env_key(account)}={shlex.quote(account['home'])}"
              f"  # account={account['name']}")
        return 0
    if command in ("claude", "codex"):
        from . import route
        fam = "claude" if command == "claude" else "codex"
        # honour an explicit model flag so scoped weekly caps (e.g. Opus)
        # still gate the routing decision
        if "--model" in args and args.index("--model") + 1 < len(args):
            fam = registry.family(args[args.index("--model") + 1])
        return route.cmd_exec(fam, [command] + args)
    if command == "run":
        from . import route
        if not args or "--" not in args or args.index("--") == len(args) - 1:
            print("usage: headroom run <model> -- <command...>", file=sys.stderr)
            return 2
        separator = args.index("--")
        return route.cmd_run(registry.family(args[0]), args[separator + 1:])
    if command == "rotate":
        from . import route
        return route.cmd_rotate(registry.family(args[0] if args else "claude"))
    if command == "mark":
        import time

        from . import route
        if len(args) < 2:
            print("usage: headroom mark <name> <model> [epoch-unix-timestamp]",
                  file=sys.stderr)
            return 2
        known = {account["name"] for account in registry.accounts()}
        if args[0] not in known:
            print(f"headroom: no connected account named {args[0]!r} "
                  f"(have: {', '.join(sorted(known)) or 'none'})", file=sys.stderr)
            return 2
        if len(args) > 2:
            try:
                epoch = float(args[2])
            except ValueError:
                print("usage: headroom mark <name> <model> "
                      "[epoch-unix-timestamp]", file=sys.stderr)
                return 2
        else:
            epoch = time.time() + 5 * 3600
        epoch = route.mark(args[0], registry.family(args[1]), epoch)
        print(f"cooled {args[0]}:{registry.family(args[1])} "
              f"until {route.tfmt(epoch)}")
        return 0
    if command == "clear":
        from . import route
        route.clear(args[0] if args else None)
        print("cleared " + (args[0] if args else "all cooldowns"))
        return 0
    if command == "dashboard":
        from . import dashboard, paths
        dashboard.build(snapshot_file=paths.public_snapshot_path())
        return 0
    if command == "serve":
        from . import dashboard
        port = None
        if "--port" in args:
            try:
                port = int(args[args.index("--port") + 1])
                if not 1 <= port <= 65535:
                    raise ValueError
            except (IndexError, ValueError):
                print("usage: headroom serve [--open] [--port 1-65535]",
                      file=sys.stderr)
                return 2
        return dashboard.serve(open_browser="--open" in args, port=port) or 0
    if command == "statusline":
        from . import statusline
        return statusline.main()
    if command == "accounts":
        try:
            for account in registry.accounts():
                print(f"  {account['name']:<16} {account['provider']:<7} "
                      f"{account.get('expected_email', '')}  {account['home']}")
            return 0
        except registry.RegistryError as error:
            print(str(error), file=sys.stderr)
            return 1
    print(f"unknown command: {command}\n", file=sys.stderr)
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
