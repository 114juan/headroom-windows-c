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
import threading
import time
import webbrowser

from . import collect as collector
from . import paths, registry

TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "dashboard", "template.html")
SERVE_MAX_AGE = int(os.environ.get("HEADROOM_SERVE_MAX_AGE", "300"))


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
    html = html.replace("/*__HEADROOM_CONFIG__*/ null",
                        json.dumps(injected, indent=None))
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
    def __init__(self, *args, directory=None, **kwargs):
        super().__init__(*args, directory=directory, **kwargs)

    def log_message(self, format, *args):  # noqa: A002 — stdlib signature
        pass

    def do_GET(self):
        if self.path.split("?")[0] == "/usage.json":
            snapshot = paths.load_json(paths.public_snapshot_path())
            generated = (snapshot or {}).get("generated", 0)
            if not snapshot or time.time() - generated > SERVE_MAX_AGE:
                try:
                    collector.run_collect(quiet=True)
                    snapshot = paths.load_json(paths.public_snapshot_path())
                except Exception:  # noqa: BLE001 — serve the last good snapshot
                    pass
            body = json.dumps(snapshot or {}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("cache-control", "no-store")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


def serve(open_browser=False, port=None):
    config = registry.load()
    settings = registry.dashboard_settings(config)
    port = settings["port"] if port is None else port
    out_dir = paths.public_dir()
    build(config, out_dir)
    handler = lambda *args, **kwargs: Handler(*args, directory=out_dir, **kwargs)  # noqa: E731
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"headroom dashboard: {url}  (Ctrl-C to stop)")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        return 0
