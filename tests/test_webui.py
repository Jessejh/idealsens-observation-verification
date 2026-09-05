"""Tests for the local web UI: its HTTP surface, its guards, and its error codes.

The guards matter more than they look. This server runs local work and holds a
key that bypasses every database rule, so "only this machine, only this token"
has to actually hold.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from curbtool import errors
from curbtool.config import Settings
from curbtool.webui import AppState, Server
from tests.gopro_fixture import drive_plan, patch_read_payloads, telemetry_payloads, write_clip

UTC = timezone.utc
BASE = datetime(2024, 6, 1, 8, 0, 0, tzinfo=UTC)


class WebTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.footage = Path(tempfile.mkdtemp())
        write_clip(cls.footage / "GX010042.MP4", 20.0)
        cls.payloads = telemetry_payloads(drive_plan(BASE, [(6.0, 14.0)], 20.0))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.footage, ignore_errors=True)

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        import curbtool.config as config
        self._saved = config.CONFIG_PATH
        config.CONFIG_PATH = self.home / ".curbtool.json"
        Settings(campaign="test-campaign", work_dir=str(self.home / "work"),
                 upload=False, max_frames=2, frame_width=320,
                 proxy_source="none").save(config.CONFIG_PATH)

        self.state = AppState()
        self.state.settings = Settings.load(config.CONFIG_PATH)
        self.server = Server(self.state, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.state.cancel_event.set()
        if self.state.worker is not None:
            self.state.worker.join(timeout=60)
        self.server.shutdown()
        self.thread.join(timeout=5)
        import curbtool.config as config
        config.CONFIG_PATH = self._saved
        shutil.rmtree(self.home, ignore_errors=True)

    # -- helpers --------------------------------------------------------

    def request(self, path, body=None, token=None, host=None):
        url = f"http://127.0.0.1:{self.server.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
        req.add_header("X-Curbtool-Token",
                       self.server.token if token is None else token)
        req.add_header("Content-Type", "application/json")
        if host:
            req.add_header("Host", host)
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return res.status, json.loads(res.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def write_csv(self, offset_hours=0.0, name="tags.csv"):
        import csv as csv_module
        path = self.home / name
        with path.open("w", newline="") as handle:
            writer = csv_module.writer(handle)
            writer.writerow(["id", "timestamp", "category"])
            for obs_id, at in (("obs-1", 9.0), ("obs-2", 11.0)):
                stamp = BASE + timedelta(seconds=at, hours=offset_hours)
                writer.writerow([obs_id, stamp.isoformat().replace("+00:00", "Z"), "curb"])
        return path

    def wait_idle(self, timeout=180):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.state.busy():
                return True
            time.sleep(0.1)
        return False


class TestAccessControl(WebTestCase):
    def test_a_request_without_the_token_is_refused(self):
        status, _ = self.request("/api/state", token="")
        self.assertEqual(status, 403)

    def test_a_wrong_token_is_refused(self):
        status, _ = self.request("/api/state", token="not-the-token")
        self.assertEqual(status, 403)

    def test_a_non_loopback_host_header_is_refused(self):
        # Blocks DNS rebinding: a hostile page resolving its own name to
        # 127.0.0.1 would otherwise reach this server from the browser.
        status, _ = self.request("/api/state", host="curbtool.example.com")
        self.assertEqual(status, 403)

    def test_the_correct_token_is_accepted(self):
        status, payload = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertIn("settings", payload)

    def test_the_service_key_is_never_sent_to_the_page(self):
        self.state.supabase.url = "https://example.supabase.co"
        self.state.supabase.service_key = "super-secret-service-role-key"
        _, payload = self.request("/api/state")
        self.assertNotIn("super-secret-service-role-key", json.dumps(payload))
        self.assertTrue(payload["supabase"]["configured"])

    def test_the_token_also_works_as_a_query_parameter(self):
        # The page itself is opened by URL, so the first request has no header.
        url = f"http://127.0.0.1:{self.server.port}/?t={self.server.token}"
        with urllib.request.urlopen(url, timeout=30) as res:
            self.assertEqual(res.status, 200)
            self.assertIn(b"curbtool", res.read())


class TestSettings(WebTestCase):
    def test_settings_round_trip_with_the_right_types(self):
        status, payload = self.request("/api/settings", {
            "campaign": "helsinki-2024", "clock_offset_s": "-3600",
            "max_frames": "7", "upload": True})
        self.assertEqual(status, 200)
        self.assertEqual(payload["settings"]["campaign"], "helsinki-2024")
        self.assertEqual(payload["settings"]["clock_offset_s"], -3600.0)
        self.assertEqual(payload["settings"]["max_frames"], 7)
        self.assertIs(payload["settings"]["upload"], True)

    def test_a_decimal_comma_is_accepted(self):
        _, payload = self.request("/api/settings", {"stop_speed_mps": "0,9"})
        self.assertAlmostEqual(payload["settings"]["stop_speed_mps"], 0.9)

    def test_a_typo_keeps_the_previous_value(self):
        before = self.state.settings.stop_speed_mps
        _, payload = self.request("/api/settings", {"stop_speed_mps": "fast"})
        self.assertEqual(payload["settings"]["stop_speed_mps"], before)

    def test_unknown_keys_are_ignored(self):
        status, payload = self.request("/api/settings", {"evil": "x", "campaign": "ok"})
        self.assertEqual(status, 200)
        self.assertNotIn("evil", payload["settings"])

    def test_settings_are_persisted(self):
        self.request("/api/settings", {"campaign": "persisted"})
        import curbtool.config as config
        self.assertEqual(Settings.load(config.CONFIG_PATH).campaign, "persisted")


class TestFiles(WebTestCase):
    def test_adding_a_folder_lists_its_chapters(self):
        status, payload = self.request("/api/files/add", {"paths": [str(self.footage)]})
        self.assertEqual(status, 200)
        self.assertEqual(payload["added"], 1)
        self.assertEqual(payload["files"][0]["name"], "GX010042.MP4")

    def test_the_same_file_is_not_added_twice(self):
        self.request("/api/files/add", {"paths": [str(self.footage)]})
        _, payload = self.request("/api/files/add", {"paths": [str(self.footage)]})
        self.assertEqual(payload["added"], 0)
        self.assertEqual(len(payload["files"]), 1)

    def test_a_folder_with_no_footage_reports_a_code(self):
        empty = self.home / "empty"
        empty.mkdir()
        self.request("/api/files/add", {"paths": [str(empty)]})
        _, state = self.request("/api/state")
        self.assertEqual([p["code"] for p in state["problems"]], ["E003"])

    def test_browse_lists_folders_and_the_right_kind_of_file(self):
        self.write_csv()
        _, videos = self.request(f"/api/browse?path={self.footage}&kind=video")
        self.assertEqual([f["name"] for f in videos["files"]], ["GX010042.MP4"])
        _, csvs = self.request(f"/api/browse?path={self.home}&kind=csv")
        self.assertIn("tags.csv", [f["name"] for f in csvs["files"]])
        _, dirs = self.request(f"/api/browse?path={self.home}&kind=dir")
        self.assertEqual(dirs["files"], [], "a folder picker should list no files")

    def test_browsing_a_missing_folder_reports_it_without_crashing(self):
        status, payload = self.request("/api/browse?path=/no/such/folder&kind=video")
        self.assertEqual(status, 200)
        self.assertIn("error", payload)


class TestCheckAndIngest(WebTestCase):
    def add_footage(self):
        self.request("/api/files/add", {"paths": [str(self.footage)]})

    def test_check_reports_matches_and_writes_nothing(self):
        self.add_footage()
        self.request("/api/settings", {"observations_csv": str(self.write_csv())})
        with patch_read_payloads(self.payloads):
            self.request("/api/check", {})
            self.assertTrue(self.wait_idle())

        _, state = self.request("/api/state")
        self.assertEqual(state["check"]["matched"], 2)
        self.assertTrue(state["check"]["ready"])
        self.assertFalse(Path(self.state.settings.work_dir).exists(),
                         "check must not create the work folder")

    def test_a_campaign_in_local_time_raises_the_clock_offset_code(self):
        self.add_footage()
        self.request("/api/settings",
                     {"observations_csv": str(self.write_csv(offset_hours=3))})
        with patch_read_payloads(self.payloads):
            self.request("/api/check", {})
            self.assertTrue(self.wait_idle())

        _, state = self.request("/api/state")
        codes = [p["code"] for p in state["problems"]]
        self.assertIn("E110", codes)
        problem = next(p for p in state["problems"] if p["code"] == "E110")
        self.assertIn("-10800", problem["detail"])
        self.assertTrue(problem["fix"], "every code must carry a next action")

    def test_check_without_a_csv_reports_a_code_rather_than_crashing(self):
        self.add_footage()
        self.request("/api/settings", {"observations_csv": ""})
        self.request("/api/check", {})
        self.assertTrue(self.wait_idle(timeout=30))
        _, state = self.request("/api/state")
        self.assertIn("E201", [p["code"] for p in state["problems"]])

    def test_an_unreadable_csv_reports_a_code(self):
        self.add_footage()
        bad = self.home / "bad.csv"
        bad.write_text("just some words\nwith no columns\n")
        self.request("/api/settings", {"observations_csv": str(bad)})
        self.request("/api/check", {})
        self.assertTrue(self.wait_idle(timeout=30))
        _, state = self.request("/api/state")
        codes = [p["code"] for p in state["problems"]]
        self.assertTrue(any(c.startswith("E2") for c in codes), codes)

    def test_ingest_runs_and_marks_the_file_done(self):
        self.add_footage()
        self.request("/api/settings", {"observations_csv": str(self.write_csv())})
        with patch_read_payloads(self.payloads):
            self.request("/api/ingest", {})
            self.assertTrue(self.wait_idle())

        _, state = self.request("/api/state")
        self.assertEqual(state["files"][0]["status"], "done")
        self.assertEqual(state["summary"]["matched"], 2)
        self.assertGreater(state["summary"]["frames"], 0)

    def test_ingest_without_a_campaign_reports_a_code(self):
        self.add_footage()
        self.request("/api/settings",
                     {"campaign": "", "observations_csv": str(self.write_csv())})
        self.request("/api/ingest", {})
        self.assertTrue(self.wait_idle(timeout=30))
        _, state = self.request("/api/state")
        self.assertIn("E001", [p["code"] for p in state["problems"]])

    def test_a_corrupt_file_is_coded_and_does_not_stop_the_batch(self):
        broken = self.footage / "BROKEN.MP4"
        broken.write_bytes(b"not a video")
        try:
            self.request("/api/files/add", {"paths": [str(broken)]})
            self.add_footage()
            self.request("/api/settings", {"observations_csv": str(self.write_csv())})
            with patch_read_payloads(self.payloads):
                self.request("/api/ingest", {})
                self.assertTrue(self.wait_idle())

            _, state = self.request("/api/state")
            statuses = {f["name"]: f["status"] for f in state["files"]}
            self.assertEqual(statuses["BROKEN.MP4"], "failed")
            self.assertEqual(statuses["GX010042.MP4"], "done")
            self.assertTrue([p for p in state["problems"] if p["where"] == "BROKEN.MP4"])
        finally:
            broken.unlink(missing_ok=True)

    def test_a_second_run_while_busy_is_refused(self):
        self.add_footage()
        self.request("/api/settings", {"observations_csv": str(self.write_csv())})
        with patch_read_payloads(self.payloads):
            self.request("/api/ingest", {})
            status, payload = self.request("/api/ingest", {})
            self.assertEqual(status, 409)
            self.assertEqual(payload["error"], "busy")
            self.wait_idle()

    def test_cancel_stops_the_run(self):
        self.add_footage()
        self.request("/api/settings", {"observations_csv": str(self.write_csv())})
        with patch_read_payloads(self.payloads):
            self.request("/api/ingest", {})
            time.sleep(0.4)
            status, payload = self.request("/api/cancel", {})
            self.assertEqual(status, 200)
            self.assertTrue(payload["cancelling"])
            self.assertTrue(self.wait_idle(timeout=120))

    def test_events_stream_incrementally(self):
        self.add_footage()
        _, first = self.request("/api/state?since=0")
        self.assertTrue(first["events"])
        _, second = self.request(f"/api/state?since={first['cursor']}")
        self.assertEqual(second["events"], [], "already-seen events must not repeat")


class TestErrorCodes(unittest.TestCase):
    def test_every_code_carries_a_meaning_and_a_fix(self):
        for code in errors.CODES.values():
            self.assertTrue(code.title, code.code)
            self.assertTrue(code.meaning, code.code)
            self.assertTrue(code.fix, f"{code.code} must say what to do next")

    def test_codes_are_unique_and_well_formed(self):
        for key, code in errors.CODES.items():
            self.assertEqual(key, code.code)
            self.assertRegex(code.code, r"^E\d{3}$")

    def test_unknown_failures_fall_back_to_the_catch_all(self):
        described = errors.describe(ValueError("something odd"))
        self.assertEqual(described["code"], "E900")
        self.assertIn("something odd", described["detail"])

    def test_the_raw_error_is_always_preserved(self):
        described = errors.describe(RuntimeError("the exact text"))
        self.assertIn("RuntimeError", described["detail"])
        self.assertIn("the exact text", described["detail"])


if __name__ == "__main__":
    unittest.main()
