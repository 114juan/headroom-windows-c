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

    def test_analytics_tracks_burn_peak_hour_and_resets(self):
        identity = slot_id("alpha")
        # Hours 10, 11, 14, 15 in the mocked local clock.
        stamps = [NOW - 5 * 3600, NOW - 4 * 3600, NOW - 3600, NOW]

        def fake_localtime(ts):
            hour = {stamps[0]: 10, stamps[1]: 11, stamps[2]: 14,
                    stamps[3]: 15}[int(ts)]
            return time.struct_time((2026, 1, 1, hour, 0, 0, 3, 1, -1))

        rows = []
        used = [10.0, 12.0, 40.0, 18.0]  # +2, +28, reset 40→18
        for ts, value in zip(stamps, used):
            rows.append({
                "ts": ts,
                "accounts": [{
                    "id": identity, "name": "alpha", "provider": "claude",
                    "plan": "Max", "ok": True, "stale": False,
                    "windows": {
                        "5h": {"used_percent": value, "resets_at": ts + 3600},
                        "7d": {"used_percent": value + 5,
                               "resets_at": ts + 86400},
                    },
                }],
            })
        with mock.patch.object(history.time, "localtime",
                               side_effect=fake_localtime), \
                mock.patch.object(history.time, "time", return_value=NOW):
            payload = history.response(1, {identity}, rows=rows, generated=NOW)
        session = payload["summary"][0]["windows"]["5h"]
        self.assertEqual(session["spent_in_range"], 30.0)
        self.assertEqual(session["reset_count"], 1)
        self.assertEqual(session["peak_hour"], 14)
        self.assertGreater(session["burn_per_hour"], 0)
        self.assertEqual(payload["analytics"]["windows"]["5h"]["peak_hour"], 14)
        self.assertEqual(payload["analytics"]["windows"]["5h"]["spent_in_range"],
                         30.0)
        encoded = json.dumps(payload, allow_nan=False)
        self.assertIn("burn_per_hour", encoded)
        self.assertNotIn("NaN", encoded)

    def test_hours_to_empty_uses_recent_pace(self):
        identity = slot_id("alpha")
        rows = []
        for offset, used in ((7200, 20.0), (3600, 40.0), (0, 60.0)):
            ts = NOW - offset
            rows.append({
                "ts": ts,
                "accounts": [{
                    "id": identity, "name": "alpha", "provider": "claude",
                    "plan": "Max", "ok": True, "stale": False,
                    "windows": {
                        "5h": {"used_percent": used, "resets_at": ts + 3600},
                    },
                }],
            })
        with mock.patch.object(history.time, "time", return_value=NOW):
            payload = history.response(1, {identity}, rows=rows, generated=NOW)
        session = payload["summary"][0]["windows"]["5h"]
        # 40 points in 2 hours → 20%/h, 40% left → 2 hours to empty.
        self.assertAlmostEqual(session["recent_burn_per_hour"], 20.0)
        self.assertAlmostEqual(session["hours_to_empty"], 2.0)
        fleet = payload["analytics"]["windows"]["5h"]
        self.assertEqual(fleet["soonest_empty"]["name"], "alpha")
        self.assertEqual(fleet["fastest"]["name"], "alpha")
        self.assertEqual(fleet["draining_accounts"], 1)

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

    def test_agy_scoped_windows_and_iso_resets_in_history(self):
        scoped_key = "scoped:Claude and GPT models Weekly Limit Remaining"
        agy_snapshot = {
            "schema_version": 1,
            "generated": NOW,
            "accounts": [{
                "id": slot_id("antigravity"),
                "name": "antigravity",
                "email": "owner@example.test",
                "provider": "agy",
                "plan": "Antigravity",
                "ok": True,
                "stale": False,
                "windows": {
                    "5h": {"used_percent": 15.0, "resets_at": "2026-09-03T17:27:33Z"},
                    "7d": {"used_percent": 25.0, "resets_at": "2026-09-06T16:44:09Z"},
                    scoped_key: {"used_percent": 5.0, "resets_at": "2026-09-10T13:50:23Z"},
                },
            }],
        }
        self.assertTrue(history.append_snapshot(agy_snapshot, now=NOW))
        with mock.patch.object(history.time, "time", return_value=NOW):
            payload = history.response(1, live_ids("antigravity"), generated=NOW)
        acct = payload["summary"][0]
        self.assertEqual(acct["provider"], "agy")
        self.assertIn("5h", acct["windows"])
        self.assertIn("7d", acct["windows"])
        self.assertIn(scoped_key, acct["windows"])
        self.assertEqual(acct["windows"][scoped_key]["current"], 5.0)
        self.assertIn(scoped_key, payload["analytics"]["windows"])


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
        self.assertIn("analytics", payload)
        self.assertTrue(payload["analytics"]["windows"])
        names = {item["name"] for item in payload["series"]}
        self.assertIn("personal", names)
        for account in payload["series"]:
            self.assertNotIn("email", account)
            self.assertNotIn("@", account["name"])
        with open(os.path.join(out, "index.html"), encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn('id="boot"', html)
        self.assertIn('id="hour-chart"', html)
        self.assertIn('id="spend-kpis"', html)
        self.assertIn('id="usage-pulse"', html)
        self.assertNotIn("/*__HEADROOM_CONFIG__*/ null", html)
