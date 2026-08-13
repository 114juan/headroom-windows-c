"""Build and serve the themed usage dashboard.

`build` renders ``dashboard/template.html`` with the user's settings injected
into one JSON block and writes it next to the public snapshot, so the whole
dashboard is two static files: ``index.html`` + ``usage.json``. Host them
anywhere — or don't: `serve` runs a tiny local server whose ``/usage.json``
transparently re-collects when the snapshot is stale, so the page is always
current with zero cron setup.
"""
import http.server
import ipaddress
import json
import os
import shutil
import sys
import threading
import time
import urllib.parse
import webbrowser

from . import collect as collector
from . import history, paths, registry

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
    with open(sample, encoding="utf-8") as handle:
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
    build(demo_config, out_dir, demo=True)
    with open(os.path.join(out_dir, "usage.json"), "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    return out_dir


def build(config=None, out_dir=None, snapshot_file=None, demo=False):
    config = registry.load() if config is None else config
    settings = registry.dashboard_settings(config)
    out_dir = paths.public_dir() if out_dir is None else out_dir
    os.makedirs(out_dir, exist_ok=True)
    with open(TEMPLATE, encoding="utf-8") as handle:
        html = handle.read()
    injected = {
        "theme": settings["theme"],
        "title": settings["title"],
        "redact": bool(settings.get("redact_emails", True)),
        "demo": bool(demo),
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
    with open(index, "w", encoding="utf-8") as handle:
        handle.write(html)
    target = os.path.join(out_dir, "usage.json")
    if snapshot_file and os.path.exists(snapshot_file) \
            and os.path.realpath(snapshot_file) != os.path.realpath(target):
        shutil.copy2(snapshot_file, target)
    print(f"dashboard built: {index}")
    return index


class QuietServer(http.server.ThreadingHTTPServer):
    daemon_threads = True  # don't let a lingering connection block Ctrl-C

    def handle_error(self, request, client_address):
        # a browser closing/reloading the tab mid-response aborts the socket
        # (WinError 10053 / ECONNRESET); that's routine, not a server fault
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


_collect_lock = threading.Lock()


class Handler(http.server.SimpleHTTPRequestHandler):
    demo = False

    def __init__(self, *args, directory=None, **kwargs):
        super().__init__(*args, directory=directory, **kwargs)

    def log_message(self, format, *args):  # noqa: A002 — stdlib signature
        pass

    def _host_ok(self):
        # reject anything but a loopback Host, so a remote page can't reach the
        # server via DNS-rebinding and read the usage feed cross-origin.
        raw = (self.headers.get("Host") or "").strip()
        if not raw:
            return False
        if raw.startswith("["):            # [::1]:port
            host = raw[1:].split("]")[0]
        elif raw.count(":") == 1:          # host:port (IPv4 or name)
            host = raw.split(":")[0]
        else:                              # bare name or bracketless IPv6
            host = raw
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def do_GET(self):
        if not self._host_ok():
            body = b"forbidden: non-loopback Host"
            self.send_response(403)
            self.send_header("content-type", "text/plain")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        route = self.path.split("?")[0]
        if route == "/history.json":
            self._serve_history()
            return
        # in demo mode the sample usage.json is a static file in the served dir;
        # never try to re-collect (there are no real accounts)
        if route == "/usage.json" and not self.demo:
            snapshot = paths.load_json(paths.public_snapshot_path())
            generated = (snapshot or {}).get("generated", 0)
            if not snapshot or time.time() - generated > SERVE_MAX_AGE:
                # one collect at a time: concurrent stale requests wait here,
                # then re-check and reuse the snapshot the winner just wrote
                with _collect_lock:
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

    def _send_body(self, status, content_type, body):
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_history(self):
        """Stats tab feed: window percentages only, never emails or tokens."""
        try:
            if not history.enabled():
                self._send_body(
                    503, "application/json",
                    b'{"error":"history_disabled"}')
                return
            query = urllib.parse.parse_qs(
                urllib.parse.urlsplit(self.path).query)
            try:
                days = int((query.get("days") or [7])[0])
            except (TypeError, ValueError):
                days = 7
            days = min(history.retention_days(), max(1, days))
            if self.demo:
                snapshot = paths.load_json(
                    os.path.join(self.directory, "usage.json"))
                live_ids = {history.slot_id(account)
                            for account in (snapshot or {}).get("accounts", [])}
                live_ids.discard(None)
                rows = history.demo_rows(snapshot, days) \
                    if isinstance(snapshot, dict) else []
            else:
                config = registry.load()
                live_ids = {history.slot_id(account)
                            for account in registry.accounts(config)}
                live_ids.discard(None)
                rows = history.load_series(days, live_ids)
            if not rows:
                self._send_body(
                    503, "application/json",
                    b'{"error":"no history yet"}')
                return
            value = history.response(
                days, live_ids, rows=rows, generated=int(time.time()))
            body = json.dumps(value, allow_nan=False,
                              separators=(",", ":")).encode("utf-8")
        except Exception:
            self._send_body(
                503, "application/json",
                b'{"error":"invalid history"}')
            return
        self._send_body(200, "application/json", body)

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _account_param(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = 0
        if length > 4096:
            return None
        raw_body = self.rfile.read(length) if length > 0 else b""
        if raw_body:
            try:
                payload = json.loads(raw_body.decode("utf-8"))
                name = payload.get("account") if isinstance(payload, dict) else None
                if name:
                    return name
            except (ValueError, UnicodeDecodeError):
                pass
        from urllib.parse import parse_qs, urlparse
        params = parse_qs(urlparse(self.path).query)
        return params.get("account", [None])[0]

    def do_POST(self):
        if not self._host_ok():
            body = b"forbidden: non-loopback Host"
            self.send_response(403)
            self.send_header("content-type", "text/plain")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        clean_path = self.path.split("?")[0]
        if clean_path in ("/api/clear-token", "/api/logout", "/api/remove"):
            status, payload = apply_mutation(
                clean_path, self._account_param(), demo=self.demo)
            self._json(status, payload)
            return

        body = b"not found"
        self.send_response(404)
        self.send_header("content-type", "text/plain")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)



def apply_mutation(path, account_name, demo=False):
    """Logout or remove a slot. Used by the local dashboard POSTs.
    Returns (http_status, json_payload)."""
    if demo:
        return 400, {"ok": False, "error": "disabled in demo mode"}
    if not account_name:
        return 400, {"ok": False, "error": "missing account parameter"}
    from . import connect
    try:
        config = registry.load()
    except registry.RegistryError as error:
        return 400, {"ok": False, "error": str(error)}
    if path == "/api/remove":
        ok, msg = connect.remove_account(config, account_name)
    elif path in ("/api/clear-token", "/api/logout"):
        ok, msg = connect.clear_token(config, account_name)
    else:
        return 404, {"ok": False, "error": "not found"}
    if not ok:
        return 400, {"ok": False, "error": msg}
    collector.run_collect(quiet=True)
    snapshot = paths.load_json(paths.public_snapshot_path())
    return 200, {"ok": True, "message": msg, "snapshot": snapshot}


def serve(open_browser=True, port=None, demo=False):
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
        server = QuietServer(("127.0.0.1", port), handler)
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
    finally:
        server.server_close()
