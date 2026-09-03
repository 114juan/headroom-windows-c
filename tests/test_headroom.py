"""headroom test suite — stdlib unittest only, no pytest, no network.

Run:  python3 -m unittest discover -s tests   (from the repo root)

Covers the load-bearing safety logic: config validation, the fail-closed
router (`block_reason`), redaction, and the public-snapshot projection.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from headroom import collect, registry, route  # noqa: E402


def _claude_row(name="a", used5h=10.0, used7d=20.0, ok=True, **over):
    now = int(time.time())
    row = {
        "name": name, "provider": "claude", "plan": "Max 20x", "ok": ok,
        "stale": False, "routable": ok, "identity_verified": True,
        "identity": {"account_fingerprint": "AAAA", "credential_digest": "BBBB"},
        "trust_state": "verified" if ok else "held", "captured_at": now - 10,
        "source": "anthropic_usage_api",
        "windows": {
            "5h": {"used_percent": used5h, "resets_at": now + 3600,
                   "window_minutes": 300},
            "7d": {"used_percent": used7d, "resets_at": now + 8 * 86400,
                   "window_minutes": 10080},
        },
    }
    row.update(over)
    return row


def _account(name="a", provider="claude"):
    return {"name": name, "provider": provider, "home": "/tmp/hr-t/" + name}


class RegistryValidation(unittest.TestCase):
    def test_rejects_bad_schema(self):
        with self.assertRaises(registry.RegistryError):
            registry.validate({"accounts": []})

    def test_rejects_bad_name(self):
        cfg = {"schema_version": 1, "accounts": [
            {"name": "Bad Name!", "provider": "claude", "home": "/tmp/x"}]}
        with self.assertRaises(registry.RegistryError):
            registry.validate(cfg)

    def test_rejects_duplicate_home(self):
        cfg = {"schema_version": 1, "accounts": [
            {"name": "a", "provider": "claude", "home": "/tmp/x"},
            {"name": "b", "provider": "claude", "home": "/tmp/x"}]}
        with self.assertRaises(registry.RegistryError):
            registry.validate(cfg)

    def test_accepts_valid(self):
        cfg = {"schema_version": 1, "accounts": [
            {"name": "personal", "provider": "claude", "home": "~/.claude"}]}
        self.assertEqual(registry.validate(cfg), cfg)

    def test_unknown_model_family_raises(self):
        with self.assertRaises(registry.RegistryError):
            registry.family("banana-model-xyz")

    def test_known_families(self):
        self.assertEqual(registry.family("claude-opus-4"), "opus")
        self.assertEqual(registry.family("gpt-5.6-codex"), "codex")
        self.assertEqual(registry.family("grok-4.6"), "grok")
        self.assertEqual(registry.family("grok-build"), "grok")
        self.assertEqual(registry.family(""), "claude")


class BlockReasonFailClosed(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        # the router re-derives the slot's live identity+credential; in tests
        # there are no real homes, so return the fixture's bound values
        self._orig_binding = collect.local_binding
        collect.local_binding = lambda provider, home: ("AAAA", "BBBB")

    def tearDown(self):
        collect.local_binding = self._orig_binding

    _UNSET = object()

    def reason(self, row, fam="sonnet", cool=_UNSET):
        cool = {} if cool is self._UNSET else cool
        return route.block_reason(_account(), fam, row, cool, self.now)

    def test_healthy_routes(self):
        self.assertIsNone(self.reason(_claude_row(used5h=10)))

    def test_100pct_holds(self):
        self.assertIsNotNone(self.reason(_claude_row(used5h=100)))

    def test_missing_row_holds(self):
        self.assertIsNotNone(self.reason(None))

    def test_not_ok_holds(self):
        self.assertIsNotNone(self.reason(_claude_row(ok=False)))

    def test_string_percent_holds(self):
        row = _claude_row()
        row["windows"]["5h"]["used_percent"] = "10"
        self.assertIsNotNone(self.reason(row))

    def test_future_capture_holds(self):
        row = _claude_row()
        row["captured_at"] = self.now + 10_000
        self.assertIsNotNone(self.reason(row))

    def test_stale_holds(self):
        self.assertIsNotNone(self.reason(_claude_row(stale=True)))

    def test_corrupt_cooldown_value_holds(self):
        r = self.reason(_claude_row(), cool={"a:sonnet": "not-a-number"})
        self.assertIsNotNone(r)

    def test_none_ledger_holds(self):
        self.assertIsNotNone(self.reason(_claude_row(), cool=None))

    def test_trust_routable_mismatch_holds(self):
        row = _claude_row()
        row["trust_state"] = "held"  # but routable stayed True
        self.assertIsNotNone(self.reason(row))

    def test_expired_observation_holds(self):
        row = _claude_row()
        row["windows"]["5h"] = {"used_percent": None,
                                "freshness": "expired_observation",
                                "resets_at": 1, "window_minutes": 300}
        self.assertIsNotNone(self.reason(row))

    def test_identity_mismatch_holds(self):
        collect.local_binding = lambda provider, home: ("XXXX", "BBBB")
        self.assertIsNotNone(self.reason(_claude_row()))

    def test_credential_changed_holds(self):
        collect.local_binding = lambda provider, home: ("AAAA", "WRONG")
        self.assertIsNotNone(self.reason(_claude_row()))

    def test_identity_match_routes(self):
        # setUp already patches local_binding to the matching values
        self.assertIsNone(self.reason(_claude_row()))

    def test_no_snapshot_identity_holds(self):
        row = _claude_row()
        row.pop("identity")
        self.assertIsNotNone(self.reason(row))

    def test_no_credential_digest_holds(self):
        row = _claude_row()
        row["identity"] = {"account_fingerprint": "AAAA"}  # no credential_digest
        self.assertIsNotNone(self.reason(row))

    def test_non_dict_windows_holds(self):
        row = _claude_row()
        row["windows"] = ["not", "a", "dict"]
        self.assertIsNotNone(self.reason(row))

    def test_generic_claude_not_blocked_by_opus_cap(self):
        row = _claude_row()
        row["windows"]["scoped:Opus"] = {"used_percent": 100.0,
                                         "resets_at": self.now + 8 * 86400,
                                         "window_minutes": 10080}
        # generic claude route must NOT be held by an Opus-only cap
        self.assertIsNone(self.reason(row, fam="claude"))
        # but the opus family IS held
        self.assertIsNotNone(self.reason(row, fam="opus"))


class Redaction(unittest.TestCase):
    def test_redacts_email(self):
        self.assertEqual(collect.redact_email("paul@x.com"), "p***@x.com")

    def test_non_email_fully_masked(self):
        self.assertEqual(collect.redact_email("not-an-email"), "***")

    def test_none_passthrough(self):
        self.assertIsNone(collect.redact_email(None))

    def test_fingerprint_rejects_falsy(self):
        with self.assertRaises(collect.IdentityBindingError):
            collect.fingerprint(None)


class PublicSnapshot(unittest.TestCase):
    def test_error_never_leaks_to_public_note(self):
        snap = {"schema_version": 1, "run_id": "t", "generated": 1,
                "generated_iso": "x", "integrity_warnings": [],
                "accounts": [{
                    "name": "a", "provider": "claude", "ok": False,
                    "error": "FileNotFoundError: /home/secret/.creds",
                    "note": "FileNotFoundError: /home/secret/.creds"}]}
        pub = collect.public_snapshot(snap, redact_emails=True)
        note = pub["accounts"][0].get("note", "")
        self.assertNotIn("secret", note)
        self.assertNotIn("error", pub["accounts"][0])

    def test_redacts_emails_when_asked(self):
        snap = {"schema_version": 1, "run_id": "t", "generated": 1,
                "generated_iso": "x", "integrity_warnings": [],
                "accounts": [{"name": "a", "provider": "claude",
                              "email": "paul@x.com", "ok": True}]}
        pub = collect.public_snapshot(snap, redact_emails=True)
        self.assertEqual(pub["accounts"][0]["email"], "p***@x.com")

    def test_public_fields_whitelist_unchanged(self):
        self.assertEqual(collect.PUBLIC_FIELDS, {
            "name", "email", "provider", "plan", "ok", "note", "error_code",
            "retry_at", "captured_at", "source", "stale", "windows",
            "identity_verified", "identity_method", "trust_state", "routable",
            "subscription",
        })

    def test_binding_note_includes_connect_command(self):
        note = collect.binding_note(
            "claude_local_binding_missing",
            {"name": "mykwaadriana-fresh",
             "expected_email": "adriana.piracoca@mykywa.com"})
        self.assertIn("headroom connect mykwaadriana-fresh", note)
        self.assertIn("adriana.piracoca@mykywa.com", note)

    def test_binding_note_unexpected_email(self):
        note = collect.binding_note(
            "slot_bound_to_unexpected_email",
            {"name": "work", "expected_email": "a@x.com"},
            {"email": "b@x.com"})
        self.assertIn("b@x.com", note)
        self.assertIn("a@x.com", note)
        self.assertIn("headroom remove work", note)

    def test_binding_note_refresh_expired(self):
        note = collect.binding_note(
            "claude_refresh_expired",
            {"name": "juanquijano",
             "expected_email": "jquijanobustos@gmail.com"})
        self.assertIn("refresh token expired", note)
        self.assertIn("headroom connect juanquijano", note)
        self.assertIn("jquijanobustos@gmail.com", note)


class CodexWindowMapping(unittest.TestCase):
    """The app-server reports windows by real duration and omits any that is
    not a current constraint, so 5h/7d must be bucketed by windowDurationMins,
    never by primary/secondary position."""

    def test_standard_primary_secondary(self):
        rl = {"primary": {"usedPercent": 12, "windowDurationMins": 300},
              "secondary": {"usedPercent": 88, "windowDurationMins": 10080}}
        w = collect.codex_windows(rl, now=1000)
        self.assertEqual(w["5h"]["used_percent"], 12.0)
        self.assertEqual(w["7d"]["used_percent"], 88.0)

    def test_weekly_in_primary_slot_with_null_secondary(self):
        # freshly reset 5h omitted; weekly lands in the primary slot
        rl = {"primary": {"usedPercent": 16, "windowDurationMins": 10080},
              "secondary": None}
        w = collect.codex_windows(rl, now=1000)
        self.assertEqual(w["7d"]["used_percent"], 16.0)
        self.assertEqual(w["5h"]["used_percent"], 0.0)  # absent -> available
        self.assertEqual(w["5h"]["window_minutes"], 300)

    def test_only_5h_present(self):
        rl = {"primary": {"usedPercent": 40, "windowDurationMins": 300}}
        w = collect.codex_windows(rl, now=1000)
        self.assertEqual(w["5h"]["used_percent"], 40.0)
        self.assertEqual(w["7d"]["used_percent"], 0.0)

    def test_empty_payload_defaults_available(self):
        w = collect.codex_windows({}, now=1000)
        self.assertEqual(w["5h"]["used_percent"], 0.0)
        self.assertEqual(w["7d"]["used_percent"], 0.0)


class NetworkAuditorTests(unittest.TestCase):
    def test_codex_identity_closes_http_error(self):
        import io
        import urllib.error
        import base64
        import json
        from unittest.mock import patch

        class TrackedHTTPError(urllib.error.HTTPError):
            def __init__(self):
                super().__init__("http://test", 401, "Unauthorized", {}, io.BytesIO(b""))
                self.close_called = False
            def close(self):
                self.close_called = True
                super().close()

        err = TrackedHTTPError()
        def mock_opener(req, timeout):
            raise err

        payload = {
            "exp": 1900000000,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "a1",
                "chatgpt_plan_type": "pro"
            },
            "email": "test@test.com"
        }
        payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        id_token = f"header.{payload_b64}.signature"

        with patch("headroom.paths.load_json") as mock_load:
            mock_load.return_value = {
                "tokens": {
                    "id_token": id_token,
                    "access_token": "token"
                }
            }
            res = collect.codex_identity("/dummy/home", opener=mock_opener)
            self.assertFalse(res["verified"])
            self.assertTrue(err.close_called)

    def test_claude_limits_closes_http_error(self):
        import io
        import urllib.error
        from unittest.mock import patch

        class TrackedHTTPError(urllib.error.HTTPError):
            def __init__(self, code):
                super().__init__("http://test", code, "Error", {}, io.BytesIO(b""))
                self.close_called = False
            def close(self):
                self.close_called = True
                super().close()

        err = TrackedHTTPError(429)
        def mock_opener(req, timeout):
            raise err

        with patch("headroom.paths.load_json") as mock_load:
            mock_load.return_value = {
                "claudeAiOauth": {
                    "accessToken": "token",
                    "expiresAt": 1900000000000
                }
            }
            with self.assertRaises(collect.ProviderThrottleError):
                collect.claude_limits("/dummy/home", "expected_fp", opener=mock_opener)
            self.assertTrue(err.close_called)

    def test_claude_limits_closes_retry_http_error(self):
        import io
        import urllib.error
        from unittest.mock import patch

        class TrackedHTTPError(urllib.error.HTTPError):
            def __init__(self, code):
                super().__init__("http://test", code, "Error", {}, io.BytesIO(b""))
                self.close_called = False
            def close(self):
                self.close_called = True
                super().close()

        err1 = TrackedHTTPError(401)
        err2 = TrackedHTTPError(429)

        calls = 0
        def mock_opener(req, timeout):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise err1
            else:
                raise err2

        with patch("headroom.paths.load_json") as mock_load, \
             patch("headroom.collect.refresh_claude_token", return_value=True):
            mock_load.return_value = {
                "claudeAiOauth": {
                    "accessToken": "token",
                    "expiresAt": 1900000000000
                }
            }
            with self.assertRaises(collect.ProviderThrottleError):
                collect.claude_limits("/dummy/home", "expected_fp", opener=mock_opener)
            self.assertTrue(err1.close_called)
            self.assertTrue(err2.close_called)

    def test_claude_limits_malformed_json(self):
        from unittest.mock import patch, MagicMock

        response = MagicMock()
        response.headers = {"anthropic-organization-id": "org1"}
        response.read.return_value = b"invalid json{"
        # Make the response mock act as context manager
        response.__enter__.return_value = response

        def mock_opener(req, timeout):
            return response

        with patch("headroom.paths.load_json") as mock_load:
            mock_load.return_value = {
                "claudeAiOauth": {
                    "accessToken": "token",
                    "expiresAt": 1900000000000
                }
            }
            with self.assertRaises(ValueError) as ctx:
                collect.claude_limits("/dummy/home", None, opener=mock_opener)
            self.assertIn("malformed usage response payload", str(ctx.exception))

    def test_codex_app_server_encoding_and_close(self):
        from unittest.mock import patch, MagicMock
        import io

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = io.StringIO('{"jsonrpc": "2.0", "id": 1, "result": {}}\n'
                                       '{"jsonrpc": "2.0", "id": 2, "result": {"rateLimits": {}}}\n'
                                       '{"jsonrpc": "2.0", "id": 3, "result": {"account": {"email": "test@test.com"}}}\n')

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen, \
             patch("headroom.collect.codex_bin", return_value="codex"):
            res = collect.codex_app_server_read("/dummy/home", timeout=2)
            self.assertEqual(res["account"]["email"], "test@test.com")

            mock_popen.assert_called_once()
            kwargs = mock_popen.call_args[1]
            self.assertEqual(kwargs.get("encoding"), "utf-8")
            self.assertEqual(kwargs.get("errors"), "replace")

            mock_proc.stdin.close.assert_called_once()

    def test_codex_app_server_malformed_response(self):
        from unittest.mock import patch, MagicMock
        import io

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        # "result" is a string instead of a dict
        mock_proc.stdout = io.StringIO('{"jsonrpc": "2.0", "id": 1, "result": {}}\n'
                                       '{"jsonrpc": "2.0", "id": 2, "result": "malformed_string"}\n'
                                       '{"jsonrpc": "2.0", "id": 3, "result": {"account": "another_malformed"}}\n')

        with patch("subprocess.Popen", return_value=mock_proc), \
             patch("headroom.collect.codex_bin", return_value="codex"):
            res = collect.codex_app_server_read("/dummy/home", timeout=2)
            self.assertEqual(res["account"], {})
            self.assertEqual(res["rate_limits"], {})


class FileOpsAndStateIntegrityTests(unittest.TestCase):
    def test_utf8_load_save(self):
        import tempfile
        from headroom import paths
        with tempfile.TemporaryDirectory() as tmp:
            test_file = os.path.join(tmp, "test_utf8.json")
            data = {"message": "héllo, ユーザー!"}
            paths.write_json_atomic(test_file, data)
            loaded = paths.load_json(test_file)
            self.assertEqual(loaded, data)

    def test_corrupt_recovery(self):
        import tempfile
        from headroom import paths
        with tempfile.TemporaryDirectory() as tmp:
            test_file = os.path.join(tmp, "corrupt.json")
            with open(test_file, "w", encoding="utf-8") as f:
                f.write("{invalid: json}")
            loaded = paths.load_json(test_file)
            self.assertIsNone(loaded)

    def test_nonexistent_returns_none(self):
        from headroom import paths
        loaded = paths.load_json("does_not_exist_xyz.json")
        self.assertIsNone(loaded)

    def test_prepare_subprocess(self):
        from headroom import paths
        from unittest.mock import patch

        # Non-Windows test
        with patch("sys.platform", "linux"):
            cmd = ["claude", "auth", "login"]
            res_cmd, use_shell = paths.prepare_subprocess(cmd)
            self.assertEqual(res_cmd, cmd)
            self.assertFalse(use_shell)

        # Windows tests
        with patch("sys.platform", "win32"):
            # ps1 script
            with patch("shutil.which", return_value="C:\\path\\claude.ps1"):
                res_cmd, use_shell = paths.prepare_subprocess(["claude", "auth"])
                self.assertEqual(res_cmd, ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "C:\\path\\claude.ps1", "auth"])
                self.assertFalse(use_shell)

            # cmd script
            with patch("shutil.which", return_value="C:\\path with spaces\\claude.cmd"):
                res_cmd, use_shell = paths.prepare_subprocess(["claude", "auth", "login"])
                self.assertEqual(res_cmd, 'cmd.exe /s /c ""C:\\path with spaces\\claude.cmd" auth login"')
                self.assertFalse(use_shell)

            # exe or standard command
            with patch("shutil.which", return_value="C:\\path\\claude.exe"):
                res_cmd, use_shell = paths.prepare_subprocess(["claude", "auth"])
                self.assertEqual(res_cmd, ["C:\\path\\claude.exe", "auth"])
                self.assertFalse(use_shell)



class LockAndConcurrencyTests(unittest.TestCase):
    def test_flock_lock_and_unlock(self):
        import tempfile
        from headroom import fcntl_compat as fcntl

        with tempfile.TemporaryDirectory() as tmp:
            lock_path = os.path.join(tmp, "test.lock")
            with open(lock_path, "w") as f1:
                # Lock should succeed
                fcntl.flock(f1, fcntl.LOCK_EX)

                # Try locking from a different file descriptor, should raise BlockingIOError
                with open(lock_path, "w") as f2:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(f2, fcntl.LOCK_EX | fcntl.LOCK_NB)

                # Unlock should succeed
                fcntl.flock(f1, fcntl.LOCK_UN)

                # Unlocking again should not raise any error (silent no-op compat)
                fcntl.flock(f1, fcntl.LOCK_UN)

    def test_replace_atomic_retries_on_sharing_violation(self):
        import tempfile
        from headroom import paths
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src.json")
            dst = os.path.join(tmp, "dst.json")
            with open(src, "w") as f:
                f.write("source")
            with open(dst, "w") as f:
                f.write("dest")

            # Mock os.replace to raise PermissionError once, then succeed
            call_count = 0
            original_replace = os.replace

            def mock_replace(s, d):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    # Under Windows, PermissionError inherits from OSError and matches WinError 5
                    e = PermissionError("[WinError 5] Access is denied")
                    e.winerror = 5
                    raise e
                original_replace(s, d)

            with patch("os.replace", side_effect=mock_replace):
                paths.replace_atomic(src, dst)

            self.assertEqual(call_count, 2)
            with open(dst, "r") as f:
                self.assertEqual(f.read(), "source")


class ConversationSync(unittest.TestCase):
    """sync_project must carry the current project's transcripts to the
    successor home on rotation — copy-if-newer, junction-aware, fail-soft."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="hr-sync-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)

    def _home(self, name, files=None):
        from headroom import history_sync
        home = os.path.join(self.tmp, name)
        slug = history_sync.project_slug(self.tmp)
        project = os.path.join(home, "projects", slug)
        os.makedirs(project, exist_ok=True)
        for fname, content in (files or {}).items():
            with open(os.path.join(project, fname), "w", encoding="utf-8") as f:
                f.write(content)
        return home, project

    def test_slug_matches_claude_code_format(self):
        from headroom import history_sync
        slug = history_sync.project_slug(r"C:\Users\USER\.headroom") \
            if os.name == "nt" else history_sync.project_slug("/home/u/.headroom")
        self.assertNotIn(os.sep, slug)
        self.assertRegex(slug, r"^[A-Za-z0-9-]+$")

    def test_copies_transcripts_to_successor(self):
        from headroom import history_sync
        src_home, _ = self._home("a", {"s1.jsonl": "hello"})
        dst_home, dst_proj = self._home("b")
        copied = history_sync.sync_project(src_home, dst_home, cwd=self.tmp)
        self.assertEqual(copied, 1)
        with open(os.path.join(dst_proj, "s1.jsonl"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "hello")

    def test_never_clobbers_newer_destination(self):
        from headroom import history_sync
        src_home, src_proj = self._home("a", {"s1.jsonl": "old"})
        dst_home, dst_proj = self._home("b", {"s1.jsonl": "newer"})
        past = time.time() - 3600
        os.utime(os.path.join(src_proj, "s1.jsonl"), (past, past))
        copied = history_sync.sync_project(src_home, dst_home, cwd=self.tmp)
        self.assertEqual(copied, 0)
        with open(os.path.join(dst_proj, "s1.jsonl"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "newer")

    def test_same_physical_dir_reports_shared(self):
        from headroom import history_sync
        src_home, _ = self._home("a", {"s1.jsonl": "x"})
        result = history_sync.sync_project(src_home, src_home, cwd=self.tmp)
        self.assertEqual(result, "shared")

    def test_no_transcripts_returns_none(self):
        from headroom import history_sync
        src_home = os.path.join(self.tmp, "empty-src")
        dst_home, _ = self._home("b")
        self.assertIsNone(
            history_sync.sync_project(src_home, dst_home, cwd=self.tmp))


class DashboardServer(unittest.TestCase):
    """QuietServer must swallow client-side disconnects (browser reloads abort
    the socket mid-write — WinError 10053 / ECONNRESET) but still report real
    faults through the stdlib traceback path."""

    def _server(self):
        from headroom import dashboard
        server = dashboard.QuietServer.__new__(dashboard.QuietServer)
        return server

    def _handle_error_with(self, exc):
        import io
        from unittest.mock import patch
        server = self._server()
        stderr = io.StringIO()
        try:
            raise exc
        except Exception:
            with patch("sys.stderr", stderr):
                server.handle_error(None, ("127.0.0.1", 1234))
        return stderr.getvalue()

    def test_connection_abort_is_silenced(self):
        out = self._handle_error_with(ConnectionAbortedError(10053, "aborted"))
        self.assertEqual(out, "")

    def test_connection_reset_is_silenced(self):
        out = self._handle_error_with(ConnectionResetError(10054, "reset"))
        self.assertEqual(out, "")

    def test_real_errors_still_reported(self):
        out = self._handle_error_with(ValueError("boom"))
        self.assertIn("ValueError", out)


class IsolatedHeadroom(unittest.TestCase):
    """Every test gets its own HEADROOM_DIR so mutate() cannot touch the
    operator's real ~/.headroom/config.json."""

    def setUp(self):
        import json
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        self._old_dir = os.environ.get("HEADROOM_DIR")
        os.environ["HEADROOM_DIR"] = self.tmpdir
        self.home1 = os.path.join(self.tmpdir, "homes", "acct1")
        self.home2 = os.path.join(self.tmpdir, "homes", "acct2")
        os.makedirs(self.home1)
        os.makedirs(self.home2)
        with open(os.path.join(self.home1, ".credentials.json"), "w",
                  encoding="utf-8") as handle:
            handle.write('{"claudeAiOauth": {"accessToken": "secret"}}')
        with open(os.path.join(self.home1, ".claude.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"oauthAccount": {"emailAddress": "test@test.com"}},
                      handle)
        self.config = self._write_config([
            {"name": "acct1", "provider": "claude", "home": self.home1,
             "expected_email": "test@test.com",
             "pinned_usage_org": "abc123"},
            {"name": "acct2", "provider": "claude", "home": self.home2,
             "expected_email": "other@test.com"},
        ])

    def tearDown(self):
        import shutil
        if self._old_dir is None:
            os.environ.pop("HEADROOM_DIR", None)
        else:
            os.environ["HEADROOM_DIR"] = self._old_dir
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_config(self, accounts):
        from headroom import registry
        config = {"schema_version": 1, "accounts": accounts}
        registry.save(config)
        return config


class AccountLifecycle(IsolatedHeadroom):
    def test_clear_token_removes_credentials_keeps_email(self):
        import json
        from headroom import connect, registry
        ok, msg = connect.clear_token(self.config, "acct1")
        self.assertTrue(ok)
        self.assertFalse(os.path.exists(
            os.path.join(self.home1, ".credentials.json")))
        with open(os.path.join(self.home1, ".claude.json"),
                  encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertNotIn("oauthAccount", data)
        saved = registry.load()
        slot = next(a for a in saved["accounts"] if a["name"] == "acct1")
        self.assertEqual(slot["expected_email"], "test@test.com")
        self.assertNotIn("pinned_usage_org", slot)

    def test_clear_token_unknown_account_fails(self):
        from headroom import connect
        ok, msg = connect.clear_token(self.config, "unknown")
        self.assertFalse(ok)
        self.assertIn("no account found", msg)

    def test_add_account_existing_unpins_and_updates_email(self):
        from headroom import connect, registry
        connect.add_account(self.config, "acct1", "claude", self.home1,
                            "new@test.com")
        saved = registry.load()
        slot = next(a for a in saved["accounts"] if a["name"] == "acct1")
        self.assertEqual(slot["expected_email"], "new@test.com")
        self.assertNotIn("pinned_usage_org", slot)

    def test_cmd_connect_existing_name_relgins(self):
        from unittest.mock import patch
        from headroom import connect
        with patch.object(connect, "connect_fresh",
                          return_value={"name": "acct1"}) as mocked:
            code = connect.cmd_connect(["acct1"])
        self.assertEqual(code, 0)
        mocked.assert_called_once()
        self.assertEqual(mocked.call_args[0][1], "acct1")

    def test_remove_account_drops_slot_keeps_home(self):
        from headroom import connect, registry
        ok, msg = connect.remove_account(self.config, "acct2")
        self.assertTrue(ok)
        saved = registry.load()
        self.assertEqual([a["name"] for a in saved["accounts"]], ["acct1"])
        self.assertTrue(os.path.isdir(self.home2))
        self.assertIn("home left in place", msg)

    def test_remove_last_account_refused(self):
        from headroom import connect, registry
        connect.remove_account(self.config, "acct2")
        ok, msg = connect.remove_account(registry.load(), "acct1")
        self.assertFalse(ok)
        self.assertIn("last account", msg)
        self.assertEqual(len(registry.load()["accounts"]), 1)

    def test_apply_mutation_disabled_in_demo(self):
        from headroom import dashboard
        status, payload = dashboard.apply_mutation(
            "/api/clear-token", "acct1", demo=True)
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])
        self.assertIn("demo", payload["error"])
        status, payload = dashboard.apply_mutation(
            "/api/remove", "acct1", demo=True)
        self.assertEqual(status, 400)

    def test_apply_mutation_missing_account(self):
        from headroom import dashboard
        status, payload = dashboard.apply_mutation("/api/remove", None)
        self.assertEqual(status, 400)
        self.assertIn("missing account", payload["error"])

    def test_doctor_lines_held_slot(self):
        from headroom import __main__ as main
        snapshot = {"accounts": [{
            "name": "acct1", "ok": False,
            "error_code": "claude_local_binding_missing",
        }]}
        lines = main._doctor_account_lines(
            [{"name": "acct1", "provider": "claude", "home": self.home1}],
            snapshot)
        joined = "\n".join(lines)
        self.assertIn("HELD", joined)
        self.assertIn("headroom connect acct1", joined)

    def test_doctor_lines_refresh_expired(self):
        from headroom import __main__ as main
        snapshot = {"accounts": [{
            "name": "acct1", "ok": False,
            "error_code": "claude_refresh_expired",
        }]}
        lines = main._doctor_account_lines(
            [{"name": "acct1", "provider": "claude", "home": self.home1}],
            snapshot)
        joined = "\n".join(lines)
        self.assertIn("claude_refresh_expired", joined)
        self.assertIn("headroom connect acct1", joined)


class _ScriptedOpener:
    """urlopen stand-in: each call pops (status, payload)."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, request, timeout):
        import io
        import json
        import urllib.error
        url = getattr(request, "full_url", None) or request.get_full_url()
        self.calls.append(url)
        if not self.script:
            raise AssertionError("unexpected request to %s" % url)
        status, payload = self.script.pop(0)
        if status >= 400:
            body = b'{"error":"invalid_grant"}' if payload is None \
                else json.dumps(payload).encode()
            raise urllib.error.HTTPError(
                url, status, "error", {}, io.BytesIO(body))

        class _Resp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *args):
                return False

            def read(self_inner):
                return json.dumps(payload).encode()

        return _Resp()


class ClaudeTokenRefresh(unittest.TestCase):
    """HTTP OAuth refresh — never the CLI, never inference."""

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        self.home = os.path.join(self.tmpdir, "slot")
        os.makedirs(self.home)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_oauth(self, **over):
        from headroom import paths
        oauth = {
            "accessToken": "old-access",
            "refreshToken": "old-refresh",
            "expiresAt": int(time.time() * 1000) + 8 * 3600 * 1000,
            "subscriptionType": "max",
            "rateLimitTier": "default_claude_max_5x",
            "scopes": ["user:inference", "user:profile"],
        }
        oauth.update(over)
        paths.write_json_atomic(
            os.path.join(self.home, ".credentials.json"),
            {"claudeAiOauth": oauth}, mode=0o600)
        return oauth

    def _read_oauth(self):
        from headroom import paths
        return (paths.load_json(os.path.join(self.home, ".credentials.json"))
                or {}).get("claudeAiOauth") or {}

    def test_refresh_needed_access_near_expiry(self):
        now = 1_700_000_000
        oauth = {"expiresAt": (now + 60) * 1000}
        self.assertTrue(collect.refresh_needed(oauth, now=now))

    def test_refresh_needed_refresh_near_expiry(self):
        now = 1_700_000_000
        oauth = {
            "expiresAt": (now + 8 * 3600) * 1000,
            "refreshTokenExpiresAt": (now + 600) * 1000,
        }
        self.assertTrue(collect.refresh_needed(oauth, now=now))

    def test_refresh_needed_does_not_loop_when_refresh_dies_first(self):
        # refresh expires in 3h, access in 8h — do NOT refresh every collect
        now = 1_700_000_000
        oauth = {
            "expiresAt": (now + 8 * 3600) * 1000,
            "refreshTokenExpiresAt": (now + 3 * 3600) * 1000,
        }
        self.assertFalse(collect.refresh_needed(oauth, now=now))

    def test_refresh_needed_refresh_already_past(self):
        now = 1_700_000_000
        oauth = {
            "expiresAt": (now + 4 * 3600) * 1000,
            "refreshTokenExpiresAt": (now - 60) * 1000,
        }
        self.assertTrue(collect.refresh_needed(oauth, now=now))

    def test_refresh_200_rewrites_tokens_keeps_subscription(self):
        from unittest.mock import patch
        self._write_oauth()
        opener = _ScriptedOpener([(200, {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
            "refresh_expires_in": 7200,
            "scope": "user:inference user:profile",
        })])
        with patch("subprocess.run") as runner:
            self.assertTrue(collect.refresh_claude_token(
                self.home, opener=opener, now=time.time()))
            runner.assert_not_called()
        oauth = self._read_oauth()
        self.assertEqual(oauth["accessToken"], "new-access")
        self.assertEqual(oauth["refreshToken"], "new-refresh")
        self.assertEqual(oauth["subscriptionType"], "max")
        self.assertEqual(oauth["rateLimitTier"], "default_claude_max_5x")
        self.assertAlmostEqual(
            oauth["expiresAt"] / 1000, time.time() + 3600, delta=5)
        self.assertAlmostEqual(
            oauth["refreshTokenExpiresAt"] / 1000, time.time() + 7200, delta=5)
        self.assertTrue(opener.calls[0].startswith(
            "https://platform.claude.com/"))

    def test_primary_404_falls_back_to_legacy(self):
        self._write_oauth()
        opener = _ScriptedOpener([
            (404, None),
            (200, {"access_token": "legacy-access", "expires_in": 1800}),
        ])
        self.assertTrue(collect.refresh_claude_token(
            self.home, opener=opener))
        self.assertEqual(self._read_oauth()["accessToken"], "legacy-access")
        self.assertEqual(len(opener.calls), 2)
        self.assertIn("console.anthropic.com", opener.calls[1])

    def test_invalid_grant_raises_expired_no_cli(self):
        from unittest.mock import patch
        self._write_oauth()
        opener = _ScriptedOpener([(400, {"error": "invalid_grant"})])
        with patch("subprocess.run") as runner:
            with self.assertRaises(collect.IdentityBindingError) as ctx:
                collect.refresh_claude_token(self.home, opener=opener)
            self.assertEqual(ctx.exception.code, "claude_refresh_expired")
            runner.assert_not_called()

    def test_stale_refresh_timestamp_still_posts(self):
        # local expiry stamp is in the past — still try the server
        self._write_oauth(refreshTokenExpiresAt=int(time.time() * 1000) - 60_000)
        opener = _ScriptedOpener([(200, {
            "access_token": "still-good",
            "expires_in": 3600,
        })])
        self.assertTrue(collect.refresh_claude_token(self.home, opener=opener))
        self.assertEqual(self._read_oauth()["accessToken"], "still-good")
        self.assertNotIn("refreshTokenExpiresAt", self._read_oauth())

    def test_missing_refresh_token_raises(self):
        self._write_oauth(refreshToken=None)
        opener = _ScriptedOpener([])
        with self.assertRaises(collect.IdentityBindingError) as ctx:
            collect.refresh_claude_token(self.home, opener=opener)
        self.assertEqual(ctx.exception.code, "claude_refresh_expired")
        self.assertEqual(opener.calls, [])

    def test_rotated_refresh_without_expiry_gets_default_ttl(self):
        self._write_oauth()
        opener = _ScriptedOpener([(200, {
            "access_token": "a2",
            "refresh_token": "r2",
            "expires_in": 3600,
        })])
        collect.refresh_claude_token(self.home, opener=opener)
        oauth = self._read_oauth()
        self.assertAlmostEqual(
            oauth["refreshTokenExpiresAt"] / 1000,
            time.time() + collect.DEFAULT_REFRESH_TTL, delta=5)

    def test_identity_uses_local_metadata_skips_cli(self):
        import json
        from unittest.mock import patch
        with open(os.path.join(self.home, ".claude.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"oauthAccount": {
                "emailAddress": "a@x.com",
                "organizationUuid": "org-1",
            }}, handle)
        with open(os.path.join(self.home, ".credentials.json"), "w",
                  encoding="utf-8") as handle:
            handle.write('{"claudeAiOauth": {"accessToken": "x"}}')
        with patch("subprocess.run") as runner:
            identity = collect.claude_identity(self.home)
        self.assertEqual(identity["email"], "a@x.com")
        self.assertEqual(identity["method"], "claude_local_metadata")
        runner.assert_not_called()

    def test_identity_missing_creds_skips_cli(self):
        from unittest.mock import patch
        with patch("subprocess.run") as runner:
            with self.assertRaises(collect.IdentityBindingError) as ctx:
                collect.claude_identity(self.home)
        self.assertEqual(ctx.exception.code, "claude_local_binding_missing")
        runner.assert_not_called()


class GrokProvider(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        self.home = os.path.join(self.tmpdir, "slot")
        os.makedirs(self.home)
        self.now = time.time()
        self._orig_binding = collect.local_binding
        collect.local_binding = lambda provider, home: ("AAAA", "BBBB")

    def tearDown(self):
        import shutil
        collect.local_binding = self._orig_binding
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_auth(self, **over):
        from headroom import paths
        entry = {
            "auth_mode": "oidc",
            "key": "access-token",
            "refresh_token": "refresh-token",
            "expires_at": "2099-01-01T00:00:00Z",
            "email": "grok@example.test",
            "user_id": "user-1",
            "team_id": "team-1",
            "principal_type": "User",
            "oidc_client_id": "client-1",
            "oidc_issuer": "https://auth.x.ai",
        }
        entry.update(over)
        paths.write_json_atomic(
            os.path.join(self.home, "auth.json"),
            {"https://auth.x.ai::client-1": entry}, mode=0o600)
        return entry

    def test_select_auth_prefers_oidc(self):
        from headroom import grok as grok_provider
        root = {
            "https://accounts.x.ai/sign-in": {"key": "legacy", "email": "a@x.com"},
            "https://auth.x.ai::abc": {"key": "oidc", "email": "b@x.com"},
        }
        scope, entry = grok_provider.select_auth_entry(root)
        self.assertTrue(scope.startswith("https://auth.x.ai::"))
        self.assertEqual(entry["key"], "oidc")

    def test_parse_credits_percent_and_period(self):
        from headroom import grok as grok_provider
        parsed = grok_provider.parse_credits({
            "config": {
                "creditUsagePercent": 41.2,
                "currentPeriod": {"end": "2026-08-28T00:00:00Z"},
                "onDemandCap": {"val": 1000},
                "onDemandUsed": {"val": 250},
                "subscriptionTier": "supergrok",
            }
        })
        self.assertAlmostEqual(parsed["used_percent"], 41.2)
        self.assertEqual(parsed["resets_at"], "2026-08-28T00:00:00Z")
        self.assertAlmostEqual(parsed["extra_percent"], 25.0)

    def test_parse_credits_zero_when_period_but_no_percent(self):
        from headroom import grok as grok_provider
        parsed = grok_provider.parse_credits({
            "config": {"billingPeriodEnd": "2026-08-28T00:00:00Z"}
        })
        self.assertEqual(parsed["used_percent"], 0.0)

    def test_identity_from_auth_file(self):
        self._write_auth()
        identity = collect.grok_identity(self.home)
        self.assertEqual(identity["email"], "grok@example.test")
        self.assertEqual(identity["method"], "grok_local_auth")
        self.assertTrue(identity["account_fingerprint"])
        self.assertTrue(identity["credential_digest"])

    def test_limits_maps_weekly_pool(self):
        self._write_auth()
        opener = _ScriptedOpener([
            (200, {"config": {
                "creditUsagePercent": 12.0,
                "currentPeriod": {"end": "2026-08-28T00:00:00Z"},
            }}),
            (200, {"subscription_tier_display": "SuperGrok Heavy"}),
        ])
        identity, plan, windows = collect.grok_limits(
            self.home, opener=opener, now=self.now)
        self.assertEqual(plan, "SuperGrok Heavy")
        self.assertEqual(windows["7d"]["used_percent"], 12.0)
        self.assertEqual(windows["5h"]["freshness"], "not_applicable")
        self.assertTrue(identity["verified"])
        collect.validate_required_windows(windows, "grok")

    def test_team_403_holds(self):
        self._write_auth(principal_type="Team")
        opener = _ScriptedOpener([(403, {"error": "team"})])
        with self.assertRaises(collect.IdentityBindingError) as ctx:
            collect.grok_limits(self.home, opener=opener, now=self.now)
        self.assertEqual(ctx.exception.code, "grok_team_usage_unsupported")

    def test_refresh_rewrites_access_token(self):
        self._write_auth(expires_at="2000-01-01T00:00:00Z")
        opener = _ScriptedOpener([
            (200, {"access_token": "new-access", "expires_in": 3600,
                   "refresh_token": "new-refresh"}),
        ])
        self.assertTrue(collect.refresh_grok_token(
            self.home, opener=opener, now=self.now))
        from headroom import paths
        auth = paths.load_json(os.path.join(self.home, "auth.json"))
        entry = next(iter(auth.values()))
        self.assertEqual(entry["key"], "new-access")
        self.assertEqual(entry["refresh_token"], "new-refresh")

    def test_router_uses_weekly_window_only(self):
        account = {"name": "g", "provider": "grok", "home": self.home}
        row = {
            "name": "g", "ok": True, "routable": True,
            "trust_state": "verified", "stale": False,
            "captured_at": self.now,
            "identity": {"account_fingerprint": "AAAA",
                         "credential_digest": "BBBB"},
            "windows": {
                "5h": {"used_percent": None, "freshness": "not_applicable"},
                "7d": {"used_percent": 40.0},
            },
        }
        self.assertIsNone(route.block_reason(account, "grok", row, {}, self.now))
        row["windows"]["7d"]["used_percent"] = 100.0
        self.assertIn("7d", route.block_reason(account, "grok", row, {}, self.now))

    def test_env_and_registry(self):
        self.assertEqual(route.env_key({"provider": "grok"}), "GROK_HOME")
        cfg = {"schema_version": 1, "accounts": [
            {"name": "g1", "provider": "grok", "home": self.home}]}
        self.assertEqual(registry.validate(cfg), cfg)
        self.assertEqual(registry.family_provider("grok"), "grok")


class AgyProvider(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        self.home = os.path.join(self.tmpdir, "slot")
        os.makedirs(self.home)
        self.now = time.time()
        self._orig_binding = collect.local_binding
        collect.local_binding = lambda provider, home: ("AAAA", "BBBB")

    def tearDown(self):
        import shutil
        collect.local_binding = self._orig_binding
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @staticmethod
    def _id_token(email="agy@example.test", sub="google-sub-1"):
        import base64
        import json as _json
        payload = base64.urlsafe_b64encode(
            _json.dumps({"email": email, "sub": sub}).encode()).decode().rstrip("=")
        return "header." + payload + ".signature"

    def _write_creds(self, relative="oauth_creds.json", **over):
        from headroom import paths
        creds = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "id_token": self._id_token(),
            "token_type": "Bearer",
            "client_id": "client-1.apps.googleusercontent.com",
            "client_secret": "secret-1",
            "expiry_date": int((self.now + 3600) * 1000),
        }
        creds.update(over)
        path = os.path.join(self.home, *relative.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        paths.write_json_atomic(path, creds, mode=0o600)
        return path, creds

    @staticmethod
    def _quota(*buckets):
        return {"groups": [{"displayName": "Gemini", "buckets": list(buckets)}]}

    # ------------------------------------------------------------- parsing

    def test_parse_window_minutes_shapes(self):
        from headroom import agy as agy_provider
        cases = {
            "18000s": 300.0,          # protobuf Duration
            "PT5H": 300.0,            # ISO-8601
            "5h0m0s": 300.0,          # Go
            "5 hours": 300.0,         # prose
            "per day": 1440.0,
            "weekly": 10080.0,
            "P7D": 10080.0,
            "30m": 30.0,
        }
        for text, minutes in cases.items():
            self.assertAlmostEqual(
                agy_provider.parse_window_minutes(text), minutes,
                msg="window %r" % text)
        for junk in (None, "", "  ", "whenever", {}, True):
            self.assertIsNone(agy_provider.parse_window_minutes(junk),
                              msg="window %r" % (junk,))

    def test_bucket_percent_needs_a_fraction(self):
        from headroom import agy as agy_provider
        self.assertEqual(
            agy_provider.bucket_used_percent({"remainingFraction": 0.25}), 75.0)
        self.assertEqual(
            agy_provider.bucket_used_percent({"remainingFraction": 0}), 100.0)
        # an amount has no denominator, so it can never become a percentage
        self.assertIsNone(
            agy_provider.bucket_used_percent({"remainingAmount": 42}))
        self.assertIsNone(agy_provider.bucket_used_percent({}))
        self.assertIsNone(
            agy_provider.bucket_used_percent({"remainingFraction": "0.5"}))

    def test_windows_map_shortest_to_session_and_rest_to_scoped(self):
        from headroom import agy as agy_provider
        windows = agy_provider.windows_from_quota(self._quota(
            {"bucketId": "pro", "displayName": "Gemini 3 Pro", "window": "PT5H",
             "remainingFraction": 0.4, "resetTime": "2026-09-02T18:00:00Z"},
            {"bucketId": "flash", "displayName": "Flash", "window": "P1D",
             "remainingFraction": 0.9},
            {"bucketId": "off", "displayName": "Disabled", "window": "PT5H",
             "remainingFraction": 0.1, "disabled": True},
        ))
        self.assertEqual(windows["5h"]["used_percent"], 60.0)
        self.assertEqual(windows["5h"]["window_minutes"], 300)
        self.assertEqual(windows["5h"]["resets_at"], "2026-09-02T18:00:00Z")
        self.assertEqual(windows["7d"]["freshness"], "not_applicable")
        self.assertEqual(windows["scoped:Gemini Flash"]["used_percent"], 10.0)
        self.assertNotIn("scoped:Gemini Disabled", windows)

    def test_windows_pick_longest_weekly(self):
        from headroom import agy as agy_provider
        windows = agy_provider.windows_from_quota(self._quota(
            {"displayName": "Weekly", "window": "P7D", "remainingFraction": 0.5},
            {"displayName": "Session", "window": "PT5H", "remainingFraction": 0.8},
        ))
        self.assertEqual(windows["7d"]["used_percent"], 50.0)
        self.assertEqual(windows["5h"]["used_percent"], 20.0)
        collect.validate_required_windows(windows, "agy")

    def test_windows_none_when_nothing_usable(self):
        from headroom import agy as agy_provider
        self.assertIsNone(agy_provider.windows_from_quota({}))
        self.assertIsNone(agy_provider.windows_from_quota(self._quota(
            {"displayName": "Amounts only", "window": "PT5H",
             "remainingAmount": 7})))

    def test_plan_label_prefers_server_name(self):
        from headroom import agy as agy_provider
        self.assertEqual(
            agy_provider.plan_label({"currentTier": {"id": "standard-tier",
                                                     "name": "Antigravity Ultra"}}),
            "Antigravity Ultra")
        self.assertEqual(
            agy_provider.plan_label({"currentTier": {"id": "free-tier"}}),
            "Antigravity Free")
        self.assertIsNone(agy_provider.plan_label({}))

    # ------------------------------------------------------------ identity

    def test_credentials_found_in_nested_home(self):
        self._write_creds(relative=".gemini/antigravity-cli/oauth_creds.json")
        path, creds = collect.agy_credential_file(self.home)
        self.assertTrue(path.endswith("oauth_creds.json"))
        self.assertEqual(creds["access_token"], "access-token")

    def test_missing_credentials_hold_the_slot(self):
        with self.assertRaises(collect.IdentityBindingError) as ctx:
            collect.agy_credential_file(self.home)
        self.assertEqual(ctx.exception.code, "agy_auth_missing")

    def test_partial_credential_file_is_not_a_login(self):
        from headroom import paths
        paths.write_json_atomic(
            os.path.join(self.home, "oauth_creds.json"),
            {"refresh_token": "only-refresh"}, mode=0o600)
        with self.assertRaises(collect.IdentityBindingError) as ctx:
            collect.agy_credential_file(self.home)
        self.assertEqual(ctx.exception.code, "agy_auth_missing")

    def test_identity_from_id_token(self):
        self._write_creds()
        identity = collect.agy_local_identity(self.home)
        self.assertEqual(identity["email"], "agy@example.test")
        self.assertEqual(identity["method"], "agy_local_token")
        self.assertTrue(identity["account_fingerprint"])
        self.assertTrue(identity["credential_digest"])

    # -------------------------------------------------------------- limits

    def test_limits_reads_quota_and_plan(self):
        self._write_creds()
        opener = _ScriptedOpener([
            (200, {"currentTier": {"id": "standard-tier", "name": "Antigravity Pro"},
                   "cloudaicompanionProject": "proj-1"}),
            (200, self._quota(
                {"displayName": "Gemini 3 Pro", "window": "PT5H",
                 "remainingFraction": 0.75,
                 "resetTime": "2026-09-02T18:00:00Z"})),
        ])
        identity, plan, windows = collect.agy_limits(
            self.home, now=self.now, opener=opener)
        self.assertEqual(plan, "Antigravity Pro")
        self.assertEqual(windows["5h"]["used_percent"], 25.0)
        self.assertEqual(windows["7d"]["freshness"], "not_applicable")
        self.assertTrue(identity["verified"])
        self.assertEqual(identity["method"], "agy_code_assist_api")
        collect.validate_required_windows(windows, "agy")
        self.assertIn("retrieveUserQuotaSummary", opener.calls[1])

    def test_limits_hold_on_wrong_email(self):
        self._write_creds()
        opener = _ScriptedOpener([
            (200, {"currentTier": {"id": "free-tier"}}),
            (200, self._quota({"displayName": "Pro", "window": "PT5H",
                               "remainingFraction": 0.5})),
        ])
        with self.assertRaises(collect.IdentityBindingError) as ctx:
            collect.agy_limits(self.home, "someone.else@example.test",
                               opener=opener, now=self.now)
        self.assertEqual(ctx.exception.code, "slot_bound_to_unexpected_email")

    def test_limits_429_throttles_only_this_account(self):
        self._write_creds()
        opener = _ScriptedOpener([
            (200, {"currentTier": {"id": "free-tier"}}),
            (429, {"error": "RESOURCE_EXHAUSTED"}),
        ])
        with self.assertRaises(collect.ProviderThrottleError) as ctx:
            collect.agy_limits(self.home, opener=opener, now=self.now)
        self.assertEqual(ctx.exception.scope, "account")
        self.assertTrue(ctx.exception.provider_response)

    def test_limits_hold_when_quota_feed_is_down(self):
        self._write_creds()
        opener = _ScriptedOpener([
            (200, {"currentTier": {"id": "free-tier"}}),
            (500, {"error": "boom"}),
        ])
        with self.assertRaises(collect.IdentityBindingError) as ctx:
            collect.agy_limits(self.home, opener=opener, now=self.now)
        self.assertEqual(ctx.exception.code, "agy_quota_unavailable")

    def test_refresh_rewrites_token_in_place(self):
        path, _ = self._write_creds(expiry_date=int((self.now - 60) * 1000))
        opener = _ScriptedOpener([
            (200, {"access_token": "new-access", "expires_in": 3600,
                   "refresh_token": "new-refresh"}),
        ])
        self.assertTrue(collect.refresh_agy_token(
            self.home, opener=opener, now=self.now))
        from headroom import paths
        creds = paths.load_json(path)
        self.assertEqual(creds["access_token"], "new-access")
        self.assertEqual(creds["refresh_token"], "new-refresh")
        # google-auth-library keeps this field in milliseconds
        self.assertAlmostEqual(creds["expiry_date"] / 1000.0,
                               self.now + 3600, delta=2)

    def test_refresh_without_a_client_is_terminal(self):
        self._write_creds(expiry_date=int((self.now - 60) * 1000),
                          client_id=None, client_secret=None)
        with self.assertRaises(collect.IdentityBindingError) as ctx:
            collect.refresh_agy_token(self.home, opener=_ScriptedOpener([]),
                                      now=self.now)
        self.assertEqual(ctx.exception.code, "agy_refresh_expired")

    # ------------------------------------------------------- wiring/policy

    def test_validation_needs_at_least_one_real_window(self):
        from headroom import agy as agy_provider
        windows = {"5h": agy_provider.na_window(300),
                   "7d": agy_provider.na_window(10080)}
        with self.assertRaises(ValueError):
            collect.validate_required_windows(windows, "agy")

    def test_registry_wiring(self):
        self.assertEqual(registry.family("gemini-3-pro"), "agy")
        self.assertEqual(registry.family("antigravity"), "agy")
        self.assertEqual(registry.family_provider("agy"), "agy")
        self.assertEqual(registry.required_windows("agy"), ("5h",))
        self.assertEqual(registry.required_windows("grok"), ("7d",))
        self.assertEqual(registry.required_windows("claude"), ("5h", "7d"))
        self.assertEqual(registry.primary_window("agy"), "5h")
        self.assertEqual(registry.primary_window("grok"), "7d")
        cfg = {"schema_version": 1, "accounts": [
            {"name": "a1", "provider": "agy", "home": self.home}]}
        self.assertEqual(registry.validate(cfg), cfg)

    def test_router_tracks_but_does_not_route(self):
        account = {"name": "a1", "provider": "agy", "home": self.home}
        row = {
            "name": "a1", "ok": True, "routable": True,
            "trust_state": "verified", "stale": False,
            "captured_at": self.now,
            "identity": {"account_fingerprint": "AAAA",
                         "credential_digest": "BBBB"},
            "windows": {
                "5h": {"used_percent": 20.0},
                "7d": {"used_percent": None, "freshness": "not_applicable"},
            },
        }
        self.assertIn("tracked",
                      route.block_reason(account, "agy", row, {}, self.now))
        orig = route.AGY_ROUTING_ENABLED
        route.AGY_ROUTING_ENABLED = True
        try:
            self.assertIsNone(
                route.block_reason(account, "agy", row, {}, self.now))
            row["windows"]["5h"]["used_percent"] = 100.0
            self.assertIn("5h", route.block_reason(
                account, "agy", row, {}, self.now))
        finally:
            route.AGY_ROUTING_ENABLED = orig

    def test_collect_reports_the_account(self):
        self._write_creds()
        calls = []

        def fake_limits(home, expected=None, now=None):
            calls.append(home)
            return ({"verified": True, "email": "agy@example.test",
                     "account_fingerprint": "FP", "method": "agy_code_assist_api",
                     "credential_digest": "DG"},
                    "Antigravity Pro",
                    {"5h": {"used_percent": 30.0, "resets_at": None,
                            "window_minutes": 300, "freshness": "fresh"},
                     "7d": {"used_percent": None, "resets_at": None,
                            "window_minutes": 10080,
                            "freshness": "not_applicable"}})

        orig = collect.agy_limits
        collect.agy_limits = fake_limits
        try:
            snapshot = collect.collect([{"name": "a1", "provider": "agy",
                                         "home": self.home}])
        finally:
            collect.agy_limits = orig
        row = snapshot["accounts"][0]
        self.assertTrue(row["ok"], row.get("note") or row.get("error"))
        self.assertEqual(row["plan"], "Antigravity Pro")
        self.assertEqual(row["source"], "google_code_assist_api")
        self.assertEqual(row["trust_state"], "verified")
        self.assertEqual(calls, [self.home])

    def test_auth_override_vars_cover_google(self):
        for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "CLOUD_CODE_URL"):
            self.assertIn(var, collect.AUTH_OVERRIDE_VARS)
        env = collect.scrubbed_env({"GEMINI_API_KEY": "x", "PATH": "/bin"})
        self.assertNotIn("GEMINI_API_KEY", env)
        self.assertEqual(env["PATH"], "/bin")

    def test_expiry_epoch_parses_iso_string(self):
        from headroom import agy as agy_provider
        creds = {"expiry": "2026-09-03T17:27:33Z"}
        self.assertEqual(agy_provider.expiry_epoch(creds), 1788456453)
        nested = {"token": {"expiry": "2026-09-03T17:27:33Z"}}
        self.assertEqual(agy_provider.expiry_epoch(nested), 1788456453)

    def test_limits_normalizes_iso_resets_at(self):
        self._write_creds()
        opener = _ScriptedOpener([
            (200, {"currentTier": {"id": "free-tier"}}),
            (200, self._quota(
                {"displayName": "Session", "window": "PT5H",
                 "remainingFraction": 0.8,
                 "resetTime": "2026-09-03T17:27:33Z"})),
        ])
        identity, plan, windows = collect.agy_limits(
            self.home, now=self.now, opener=opener)
        self.assertIsInstance(windows["5h"]["resets_at"], int)
        self.assertEqual(windows["5h"]["resets_at"], 1788456453)

    def test_slot_identity_userinfo_fallback(self):
        from unittest import mock
        from headroom import connect
        self._write_creds(id_token=None)
        with mock.patch("headroom.collect._agy_get",
                        return_value=(200, {"email": "keyring-user@example.test", "sub": "sub-123"})):
            identity = connect.slot_identity("agy", self.home)
        self.assertIsNotNone(identity)
        self.assertEqual(identity["email"], "keyring-user@example.test")
        self.assertEqual(identity["method"], "agy_userinfo")


if __name__ == "__main__":
    unittest.main()



