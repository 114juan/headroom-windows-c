"""Build and serve the themed usage dashboard.

`build` renders ``dashboard/template.html`` with the user's settings injected
into one JSON block and writes it next to the public snapshot, so the whole
dashboard is two static files: ``index.html`` + ``usage.json``. Host them
anywhere — or don't: `serve` runs a tiny local server whose ``/usage.json``
transparently re-collects when the snapshot is stale, so the page is always
current with zero cron setup.
"""
import http.server
import json
import os
import shutil
import sys
import threading
import time
import webbrowser

from . import collect as collector
from . import paths, registry

TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "dashboard", "template.html")
SERVE_MAX_AGE = int(os.environ.get("HEADROOM_SERVE_MAX_AGE", "300"))


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_demo(out_dir=None):
    """Render the dashboard from the bundled sample data — no accounts, no
    config, no network. Lets anyone preview it in seconds before connecting."""
    import time
    sample = os.path.join(_repo_root(), "examples", "usage.sample.json")
    with open(sample) as handle:
        data = json.load(handle)
    now = int(time.time())
    data["generated"] = now - 30
    resets = {"5h": now + 2 * 3600 + 11 * 60, "7d": now + 3 * 86400}
    for account in data.get("accounts", []):
        account["captured_at"] = now - 30
        for key, window in (account.get("windows") or {}).items():
            window["resets_at"] = resets["5h"] if key == "5h" else resets["7d"]
            if "observed_at" in window:
                window["observed_at"] = now - 30
        sub = account.get("subscription")
        if sub and sub.get("status") == "active_through":
            sub["active_until"] = now + 21 * 86400
            sub["checked_at"] = now - 3600
    out_dir = out_dir or os.path.join(paths.base_dir(), "demo")
    os.makedirs(out_dir, exist_ok=True)
    demo_config = {"schema_version": 1,
                   "dashboard": {"theme": "midnight", "title": "headroom (demo)"},
                   "accounts": [{"name": a["name"], "provider": a["provider"],
                                 "home": "/tmp/demo/" + a["name"]}
                                for a in data["accounts"]]}
    build(demo_config, out_dir)
    with open(os.path.join(out_dir, "usage.json"), "w") as handle:
        json.dump(data, handle)
    return out_dir


def build(config=None, out_dir=None, snapshot_file=None):
    config = registry.load() if config is None else config
    settings = registry.dashboard_settings(config)
    out_dir = paths.public_dir() if out_dir is None else out_dir
    os.makedirs(out_dir, exist_ok=True)
    with open(TEMPLATE) as handle:
        html = handle.read()
    injected = {
        "theme": settings["theme"],
        "title": settings["title"],
        "accounts": [{"name": account["name"], "provider": account["provider"]}
                     for account in registry.accounts(config)],
    }
    # script-safe serialization: <, >, & escaped so a hostile title/name can
    # never terminate the <script> element (stored XSS via config)
    payload = (json.dumps(injected, indent=None)
               .replace("<", "\\u003c").replace(">", "\\u003e")
               .replace("&", "\\u0026"))
    html = html.replace("/*__HEADROOM_CONFIG__*/ null", payload)
    index = os.path.join(out_dir, "index.html")
    with open(index, "w") as handle:
        handle.write(html)
    target = os.path.join(out_dir, "usage.json")
    if snapshot_file and os.path.exists(snapshot_file) \
            and os.path.realpath(snapshot_file) != os.path.realpath(target):
        shutil.copy2(snapshot_file, target)
    print(f"dashboard built: {index}")
    return index


class Handler(http.server.SimpleHTTPRequestHandler):
    demo = False

    def __init__(self, *args, directory=None, **kwargs):
        super().__init__(*args, directory=directory, **kwargs)

    def log_message(self, format, *args):  # noqa: A002 — stdlib signature
        pass

    def _host_ok(self):
        # reject anything but a loopback Host, so a remote page can't reach the
        # server via DNS-rebinding and read the usage feed cross-origin.
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]")
        return host in ("127.0.0.1", "localhost", "::1", "")

    def do_GET(self):
        if not self._host_ok():
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"forbidden: non-loopback Host")
            return
        # in demo mode the sample usage.json is a static file in the served dir;
        # never try to re-collect (there are no real accounts)
        if self.path.split("?")[0] == "/usage.json" and not self.demo:
            snapshot = paths.load_json(paths.public_snapshot_path())
            generated = (snapshot or {}).get("generated", 0)
            if not snapshot or time.time() - generated > SERVE_MAX_AGE:
                try:
                    collector.run_collect(quiet=True)
                    snapshot = paths.load_json(paths.public_snapshot_path())
                except Exception:  # noqa: BLE001 — serve the last good snapshot
                    pass
            if not snapshot:
                self.send_response(503)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "no usage snapshot yet"}')
                return
            body = json.dumps(snapshot).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("cache-control", "no-store")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


def serve(open_browser=False, port=None, demo=False):
    if demo:
        out_dir = build_demo()
        port = port or 8377
    else:
        config = registry.load()
        settings = registry.dashboard_settings(config)
        port = settings["port"] if port is None else port
        out_dir = paths.public_dir()
        build(config, out_dir)
    handler_cls = type("HeadroomHandler", (Handler,), {"demo": demo})
    handler = lambda *args, **kwargs: handler_cls(*args, directory=out_dir, **kwargs)  # noqa: E731
    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError as error:
        print(f"headroom: cannot bind port {port} ({error}). "
              f"Is `headroom serve` already running? Try --port <N>.",
              file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{port}/"
    print(f"headroom dashboard: {url}  (Ctrl-C to stop)")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        return 0
