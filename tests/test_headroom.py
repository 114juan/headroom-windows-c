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


if __name__ == "__main__":
    unittest.main()

