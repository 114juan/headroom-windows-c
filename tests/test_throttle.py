"""A Claude usage-endpoint 429 binds to ONE slot's token, never the fleet.

Observed live (2026-09-01): one Max slot answered 429 ``rate_limit_error``
with ``Retry-After: 571`` while four sibling slots answered 200 in the same
second. The old provider-wide backoff turned that single throttle into an
hour-long outage for every Claude account.
"""
import io
import os
import sys
import time
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from headroom import collect  # noqa: E402


def _account(name):
    return {"name": name, "provider": "claude", "home": "/tmp/hr-t/" + name}


def _identity(home):
    name = os.path.basename(home)
    return {"verified": False, "email": name + "@x.test",
            "account_fingerprint": "fp-" + name,
            "method": "claude_local_metadata", "plan_type": None}


def _reading():
    now = int(time.time())
    return {
        "captured_at": now, "source": "anthropic_usage_api",
        "source_identity_fingerprint": "org-x", "stale": False,
        "windows": {
            "5h": {"used_percent": 5.0, "resets_at": now + 600,
                   "window_minutes": 300},
            "7d": {"used_percent": 9.0, "resets_at": now + 86400,
                   "window_minutes": 10080},
        },
    }


def _creds():
    return {"claudeAiOauth": {"accessToken": "token", "expiresAt": 1900000000000}}


class UsageEndpointThrottle(unittest.TestCase):
    def test_429_is_account_scoped_and_honours_retry_after(self):
        err = urllib.error.HTTPError(
            "https://api.anthropic.com/api/oauth/usage", 429, "Too Many Requests",
            {"Retry-After": "571"}, io.BytesIO(b'{"error":{"type":"rate_limit_error"}}'))

        def opener(request, timeout):
            raise err

        before = int(time.time())
        with patch("headroom.paths.load_json", return_value=_creds()):
            with self.assertRaises(collect.ProviderThrottleError) as caught:
                collect.claude_limits("/dummy/home", "fp", opener=opener)
        self.assertEqual(caught.exception.scope, "account")
        self.assertTrue(caught.exception.provider_response)
        self.assertGreaterEqual(caught.exception.retry_at, before + 571)

    def test_usage_request_matches_claude_code(self):
        seen = {}

        def opener(request, timeout):
            seen["url"] = request.full_url
            seen["beta"] = request.get_header("Anthropic-beta")
            seen["ua"] = request.get_header("User-agent")
            raise urllib.error.HTTPError(request.full_url, 500, "boom", {}, io.BytesIO(b""))

        with patch("headroom.paths.load_json", return_value=_creds()):
            with self.assertRaises(urllib.error.HTTPError):
                collect.claude_limits("/dummy/home", "fp", opener=opener)
        self.assertEqual(seen["url"], "https://api.anthropic.com/api/oauth/usage")
        self.assertEqual(seen["beta"], "oauth-2025-04-20")
        self.assertTrue(seen["ua"].startswith("claude-cli/"))


class PerAccountBackoff(unittest.TestCase):
    def _patches(self, limits):
        return (
            patch.object(collect, "claude_identity", side_effect=_identity),
            patch.object(collect, "credential_digest", return_value="dig"),
            patch.object(collect, "claude_plan", return_value="Pro"),
            patch.object(collect, "claude_limits", side_effect=limits),
        )

    def _run(self, accounts, backoff, persist, limits):
        p1, p2, p3, p4 = self._patches(limits)
        with p1, p2, p3, p4:
            return collect.collect(accounts, backoff, persist)

    def test_one_throttled_slot_does_not_hold_siblings(self):
        now = int(time.time())
        persisted = []

        def limits(home, pinned, opener=None):
            if home.endswith("/a"):
                raise collect.ProviderThrottleError(
                    now + 600, provider_response=True, scope="account")
            return _reading()

        def persist(retry_at, source="anthropic_usage_api", account=None):
            persisted.append((retry_at, source, account))

        backoff = collect.empty_backoff()
        snap = self._run([_account("a"), _account("b"), _account("c")],
                         backoff, persist, limits)
        rows = {r["name"]: r for r in snap["accounts"]}
        self.assertFalse(rows["a"]["ok"])
        self.assertEqual(rows["a"]["error_code"], "usage_source_rate_limited")
        self.assertEqual(rows["a"]["retry_at"], now + 600)
        self.assertFalse(rows["a"]["routable"])
        self.assertTrue(rows["b"]["ok"])
        self.assertTrue(rows["c"]["ok"])
        self.assertTrue(rows["b"]["routable"])
        self.assertEqual(persisted, [(now + 600, "anthropic_usage_api", "claude:a")])
        self.assertNotIn("anthropic_usage_api", backoff["providers"])

    def test_held_slot_is_not_re_hit_before_its_retry_at(self):
        now = int(time.time())
        calls = []

        def limits(home, pinned, opener=None):
            calls.append(os.path.basename(home))
            return _reading()

        backoff = collect.empty_backoff()
        backoff["accounts"]["claude:a"] = {"retry_at": now + 600,
                                           "observed_at": now - 60}
        snap = self._run([_account("a"), _account("b")], backoff,
                         lambda *a, **k: None, limits)
        rows = {r["name"]: r for r in snap["accounts"]}
        self.assertEqual(calls, ["b"])
        self.assertEqual(rows["a"]["error_code"], "usage_source_rate_limited")
        self.assertEqual(rows["a"]["retry_at"], now + 600)
        self.assertTrue(rows["b"]["ok"])

    def test_expired_account_backoff_reads_again(self):
        now = int(time.time())
        calls = []

        def limits(home, pinned, opener=None):
            calls.append(os.path.basename(home))
            return _reading()

        backoff = collect.empty_backoff()
        backoff["accounts"]["claude:a"] = {"retry_at": now - 1,
                                           "observed_at": now - 3601}
        snap = self._run([_account("a")], backoff, lambda *a, **k: None, limits)
        self.assertEqual(calls, ["a"])
        self.assertTrue(snap["accounts"][0]["ok"])

    def test_legacy_provider_wide_claude_backoff_is_ignored(self):
        # the old ledger key froze every slot for an hour after ONE token's 429
        now = int(time.time())
        backoff = collect.empty_backoff()
        backoff["providers"]["anthropic_usage_api"] = {"retry_at": now + 3000,
                                                       "observed_at": now - 600}
        snap = self._run([_account("b")], backoff, lambda *a, **k: None,
                         lambda home, pinned, opener=None: _reading())
        self.assertTrue(snap["accounts"][0]["ok"])

    def test_prune_backoff_drops_expired_recovered_gone_and_legacy(self):
        now = int(time.time())
        doc = {
            "schema_version": 1,
            "providers": {"anthropic_usage_api": {"retry_at": now + 900},
                          "grok_billing_api": {"retry_at": now + 900}},
            "accounts": {"claude:a": {"retry_at": now + 900},
                         "claude:b": {"retry_at": now - 5},
                         "claude:c": {"retry_at": now + 900},
                         "claude:gone": {"retry_at": now + 900},
                         "claude:junk": "not-a-dict"},
        }
        snap = {"accounts": [
            {"provider": "claude", "name": "a", "ok": False,
             "error_code": "usage_source_rate_limited"},
            {"provider": "claude", "name": "b", "ok": False,
             "error_code": "usage_source_rate_limited"},
            {"provider": "claude", "name": "c", "ok": True},
            {"provider": "claude", "name": "junk", "ok": False},
        ]}
        collect.prune_backoff(doc, snap, now)
        self.assertEqual(sorted(doc["accounts"]), ["claude:a"])
        self.assertNotIn("anthropic_usage_api", doc["providers"])
        self.assertIn("grok_billing_api", doc["providers"])


if __name__ == "__main__":
    unittest.main()
