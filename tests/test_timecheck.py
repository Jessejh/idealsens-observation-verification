"""Tests for the timezone audit.

The two failures it exists to catch: an export whose local-time column gets
read as UTC, and a CSV paired with the wrong folder of footage. Both leave
everything else in the tool looking like it worked.
"""

from __future__ import annotations

import csv
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from curbtool import gpmf
from curbtool.timecheck import audit, audit_csv, audit_video
from tests.gopro_fixture import drive_plan, telemetry_payloads, write_clip

UTC = timezone.utc
START = datetime(2026, 8, 26, 9, 0, 0, tzinfo=UTC)
EEST = timedelta(hours=3)
PARNU = (58.3781, 24.5020)


class TimecheckTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        # A chapter whose container carries the camera's local clock, as GoPro
        # writes it, while its telemetry carries satellite UTC.
        cls.clip = write_clip(
            cls.tmp / "GX010042.MP4", 2.0, width=320, height=180,
            creation_time=(START + EEST).strftime("%Y-%m-%dT%H:%M:%S.000000Z"))
        cls.payloads = telemetry_payloads(
            drive_plan(START, [(60.0, 200.0)], 1800.0, origin=PARNU))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self._original = gpmf.read_payloads
        gpmf.read_payloads = lambda p: list(self.payloads)
        self.work = Path(tempfile.mkdtemp())

    def tearDown(self):
        gpmf.read_payloads = self._original
        shutil.rmtree(self.work, ignore_errors=True)

    def write_export(self, count=60, spacing_s=25, origin=PARNU,
                     name="export.csv") -> Path:
        path = self.work / name
        with path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["observation_type", "time_local_eest", "time_utc",
                             "ts_utc_ms", "lat", "lon"])
            for i in range(count):
                utc = START + timedelta(seconds=60 + i * spacing_s)
                writer.writerow([
                    "poor_surface",
                    (utc + EEST).strftime("%Y-%m-%d %H:%M:%S"),
                    utc.isoformat().replace("+00:00", "Z"),
                    int(utc.timestamp() * 1000),
                    f"{origin[0] + i * 1e-4:.6f}", f"{origin[1] + i * 1e-4:.6f}"])
        return path


class TestCsvAudit(TimecheckTestCase):
    def test_it_proves_the_offset_from_the_file_itself(self):
        result = audit_csv(self.write_export())
        pairs = {(a, b): round(hours, 2) for a, b, hours in result.offsets}
        self.assertEqual(pairs[("time_utc", "time_local_eest")], -3.0)
        self.assertAlmostEqual(pairs[("time_utc", "ts_utc_ms")], 0.0, places=2)

    def test_it_chooses_the_utc_column(self):
        result = audit_csv(self.write_export())
        self.assertEqual(result.chosen, "time_utc")
        self.assertEqual(result.columns[0].name, "time_utc")
        self.assertEqual(result.columns[-1].name, "time_local_eest")

    def test_every_time_column_gets_its_own_window(self):
        result = audit_csv(self.write_export())
        utc = next(c for c in result.columns if c.name == "time_utc")
        local = next(c for c in result.columns if c.name == "time_local_eest")
        self.assertEqual((local.first - utc.first).total_seconds(), 3 * 3600)


class TestVideoAudit(TimecheckTestCase):
    def test_telemetry_utc_is_read_from_the_gps_track(self):
        result = audit_video(self.clip)
        self.assertTrue(result.ok)
        self.assertEqual(result.gpmf_first, START)
        self.assertIn(result.kind, ("GPS5", "GPS9"))

    def test_the_camera_timezone_is_exposed_from_the_container_stamp(self):
        # GoPro writes local time into creation_time with a "Z" that is a lie.
        # Reporting the gap is how the operator sees which is which.
        result = audit_video(self.clip)
        self.assertAlmostEqual(result.camera_offset_h, 3.0, places=2)

    def test_an_unreadable_chapter_is_reported_not_raised(self):
        broken = self.work / "BROKEN.MP4"
        broken.write_bytes(b"not a video")
        gpmf.read_payloads = self._original
        result = audit_video(broken)
        self.assertFalse(result.ok)
        self.assertTrue(result.error)


class TestVerdict(TimecheckTestCase):
    def test_matching_clocks_pass(self):
        result = audit([self.clip], self.write_export())
        self.assertTrue(result.ok)
        self.assertGreater(result.inside(0.0), 0)
        self.assertEqual(result.best_shift[0], 0.0)
        self.assertIn("clocks", result.render())

    def test_local_time_read_as_utc_fails_and_names_a_shift(self):
        result = audit([self.clip], self.write_export(),
                       time_column="time_local_eest")
        self.assertFalse(result.ok)
        self.assertNotEqual(result.best_shift[0], 0.0)
        self.assertTrue(result.shift_is_clear)
        self.assertIn("--clock-offset", result.render())

    def test_naming_the_zone_repairs_a_local_only_reading(self):
        result = audit([self.clip], self.write_export(),
                       time_column="time_local_eest",
                       timezone_name="Europe/Tallinn")
        self.assertTrue(result.ok, "a named zone should make the clocks agree")

    def test_footage_from_another_town_is_caught(self):
        # Tartu, ~130 km from Pärnu: the wrong CSV for this folder.
        result = audit([self.clip], self.write_export(origin=(58.3776, 26.7290)))
        self.assertIsNotNone(result.separation_m)
        self.assertGreater(result.separation_m, 5000)
        self.assertFalse(result.ok)
        self.assertIn("km", result.render())

    def test_the_same_town_passes_the_place_check(self):
        result = audit([self.clip], self.write_export())
        self.assertLess(result.separation_m, 5000)
        self.assertIn("Same campaign", result.render())

    def test_a_thin_slice_of_footage_refuses_to_name_a_shift(self):
        # Two observations cannot separate a three-hour error from an eight-hour
        # one. Saying a number there would be a confident wrong answer.
        result = audit([self.clip], self.write_export(count=2, spacing_s=30),
                       time_column="time_local_eest")
        self.assertFalse(result.shift_is_clear)
        self.assertIn("too small a difference", result.render())

    def test_it_says_so_when_there_is_nothing_to_compare(self):
        result = audit([], self.write_export())
        self.assertFalse(result.ok)
        self.assertIn("Not enough to compare", result.render())


if __name__ == "__main__":
    unittest.main()
