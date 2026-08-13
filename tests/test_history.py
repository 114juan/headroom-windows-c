"""Rolling percentage-history persistence and dashboard Stats feed."""
import hashlib
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from headroom import history, paths


NOW = 2_000_000_000


def slot_id(name):
    return hashlib.sha256(name.encode()).hexdigest()[:12]


def live_ids(*names):
    return {slot_id(name) for name in names}


def snapshot(used=42.0, name="alpha", email="owner@example.test",
             account_id=None):
    return {
        "schema_version": 1,
        "generated": NOW,
        "accounts": [{
            "id": account_id or slot_id(name),
            "name": name,
            "email": email,
            "provider": "claude",
            "plan": "Max",
            "ok": True,
            "stale": False,
            "identity": {"account_id": "secret"},
            "windows": {
                "5h": {"used_percent": used, "resets_at": NOW + 3600,
                       "email": "window@example.test"},
                "7d": {"used_percent": used + 5,
                       "resets_at": NOW + 86400},
            },
        }],
    }


class HistoryPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {
            "HEADROOM_DIR": self.temp.name,
            "HEADROOM_HISTORY": "1",
            "HEADROOM_HISTORY_MIN_INTERVAL": "60",
            "HEADROOM_HISTORY_RETENTION_DAYS": "30",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_append_throttles_within_interval(self):
        self.assertTrue(history.append_snapshot(snapshot(), now=NOW))
        self.assertFalse(history.append_snapshot(snapshot(50), now=NOW + 59))
        self.assertTrue(history.append_snapshot(snapshot(51), now=NOW + 60))
        with open(paths.history_path(), encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle]
        self.assertEqual([value["ts"] for value in rows], [NOW, NOW + 60])

    def test_projection_strips_email_and_identity(self):
        projected = history.project_snapshot(snapshot(), ts=NOW)
        account = projected["accounts"][0]
        self.assertNotIn("email", account)
        self.assertNotIn("identity", account)
        self.assertEqual(account["windows"]["5h"]["used_percent"], 42.0)
        self.assertNotIn("email", account["windows"]["5h"])

    def test_synthesized_id_when_registry_has_none(self):
        raw = snapshot()
        raw["accounts"][0].pop("id")
        projected = history.project_snapshot(raw, ts=NOW)
        self.assertTrue(history.ID_RE.fullmatch(projected["accounts"][0]["id"]))
        self.assertEqual(projected["accounts"][0]["id"],
                         history.slot_id(raw["accounts"][0]))

    def test_malformed_lines_are_ignored_and_loaded_rows_are_sanitized(self):
        paths.ensure_private(paths.history_dir())
        valid = history.project_snapshot(snapshot(30), ts=NOW - 10)
        valid["accounts"][0]["email"] = "injected@example.test"
        with open(paths.history_path(), "w", encoding="utf-8") as handle:
            handle.write("not-json\n")
            handle.write(json.dumps({"ts": "bad", "accounts": []}) + "\n")
            handle.write(json.dumps(valid) + "\n")
        with mock.patch.object(history.time, "time", return_value=NOW):
            loaded = history.load_series(1, live_ids("alpha"))
        self.assertEqual(len(loaded), 1)
        self.assertNotIn("email", loaded[0]["accounts"][0])

    def test_kill_switch_returns_before_filesystem_access(self):
        with mock.patch.dict(os.environ, {"HEADROOM_HISTORY": "0"}), \
                mock.patch.object(history, "_oldest_row") as oldest:
            self.assertFalse(history.append_snapshot(snapshot(), now=NOW))
        oldest.assert_not_called()
        self.assertFalse(os.path.exists(paths.history_dir()))

    def test_summarize_and_leaderboard_use_used_percent(self):
        history.append_snapshot(snapshot(20), now=NOW - 120)
        history.append_snapshot(snapshot(80), now=NOW)
        with mock.patch.object(history.time, "time", return_value=NOW):
            payload = history.response(1, live_ids("alpha"), generated=NOW)
        weekly = payload["summary"][0]["windows"]["7d"]
        self.assertEqual(weekly["current"], 85.0)
        self.assertEqual(weekly["peak"]["value"], 85.0)
        self.assertEqual(payload["leaderboard"][0]["name"], "alpha")
        self.assertEqual(payload["leaderboard"][0]["rank"], 1)
        self.assertTrue(payload["series"][0]["windows"]["5h"])

    def test_demo_rows_are_percentage_only(self):
        rows = history.demo_rows(snapshot(40, name="personal"), days=7, now=NOW)
        self.assertGreater(len(rows), 10)
        last = rows[-1]["accounts"][0]
        self.assertNotIn("email", last)
        self.assertLessEqual(last["windows"]["5h"]["used_percent"], 100)
        self.assertGreaterEqual(last["windows"]["5h"]["used_percent"], 0)
        self.assertAlmostEqual(last["windows"]["5h"]["used_percent"], 40.0)

    def test_remove_account_drops_rows(self):
        history.append_snapshot(snapshot(10, name="a"), now=NOW - 120)
        history.append_snapshot(snapshot(20, name="b"), now=NOW)
        self.assertTrue(history.remove_account(slot_id("a"), "a"))
        with mock.patch.object(history.time, "time", return_value=NOW):
            left = history.load_series(1, live_ids("a", "b"))
        names = {account["name"] for row in left for account in row["accounts"]}
        self.assertEqual(names, {"b"})


class DemoHistoryFeed(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HEADROOM_DIR": self.temp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_demo_build_serves_history_payload(self):
        from headroom import dashboard
        out = dashboard.build_demo(os.path.join(self.temp.name, "demo"))
        sample = os.path.join(out, "usage.json")
        with open(sample, encoding="utf-8") as handle:
            snap = json.load(handle)
        live = {history.slot_id(account) for account in snap["accounts"]}
        live.discard(None)
        rows = history.demo_rows(snap, 7)
        payload = history.response(7, live, rows=rows, generated=int(time.time()))
        self.assertEqual(payload["schema_version"], 1)
        self.assertTrue(payload["series"])
        self.assertTrue(payload["summary"])
        names = {item["name"] for item in payload["series"]}
        self.assertIn("personal", names)
        for account in payload["series"]:
            self.assertNotIn("email", account)
            self.assertNotIn("@", account["name"])
