"""Tests for reading the tagging app's export.

Shaped after a real export that carries the same instant three ways — local
time, UTC and epoch milliseconds — alongside a session id shared by every row
in a session. Both are traps: pick the local column and every frame is hours
out; treat the session id as the observation's identity and hundreds of
observations collapse onto one database row.
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

from curbtool.observations import (ObservationError, load_observations,
                                   load_phone_track, parse_timestamp,
                                   score_time_column)
from curbtool.pipeline import observation_id_for, session_id_for

UTC = timezone.utc
BASE = datetime(2026, 8, 26, 8, 47, 53, tzinfo=UTC)
EEST = timedelta(hours=3)


class ExportTestCase(unittest.TestCase):
    """Builds an export with the same column shape as the real one."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rows: int = 20, sessions: int = 2, columns=None,
              name="export.csv") -> Path:
        columns = columns or ["observation_type", "label", "time_local_eest",
                              "time_utc", "ts_utc_ms", "lat", "lon", "accuracy_m",
                              "session_id", "device_id"]
        path = self.tmp / name
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            for i in range(rows):
                utc = BASE + timedelta(seconds=i * 30)
                values = {
                    "observation_type": "poor_surface" if i % 2 else "difficult_curb",
                    "label": "Poor surface material" if i % 2 else "Difficult curb",
                    "time_local_eest": (utc + EEST).strftime("%Y-%m-%d %H:%M:%S"),
                    "time_utc": utc.isoformat().replace("+00:00", "Z"),
                    "ts_utc_ms": str(int(utc.timestamp() * 1000)),
                    "lat": f"{58.3781 + i * 1e-4:.7f}",
                    "lon": f"{24.5019 + i * 1e-4:.7f}",
                    "accuracy_m": "3.3",
                    "session_id": f"session-{i % sessions}",
                    "device_id": "device_e7atfmfs64d",
                    "observation_id": f"obs-{i:04d}",
                }
                writer.writerow([values[c] for c in columns])
        return path


class TestIdentity(ExportTestCase):
    def test_a_shared_session_id_is_never_used_as_an_observation_id(self):
        # The real export has no per-observation id, only a session id shared
        # by every row in that session. Using it would silently merge them.
        observations = load_observations(self.write(rows=20, sessions=2))
        self.assertEqual(len(observations), 20)
        self.assertEqual(len({o.external_id for o in observations}), 20)
        self.assertIsNone(observations.columns["id"])

    def test_every_observation_gets_its_own_database_row(self):
        observations = load_observations(self.write(rows=20, sessions=2))
        session = session_id_for("parnu-2026", "GX010042.MP4", 1234)
        ids = {observation_id_for(session, o.external_id) for o in observations}
        self.assertEqual(len(ids), 20, "derived ids must not collide")

    def test_a_device_id_is_not_mistaken_for_an_identifier_either(self):
        observations = load_observations(
            self.write(columns=["time_utc", "lat", "lon", "device_id"]))
        self.assertIsNone(observations.columns["id"])

    def test_a_real_id_column_is_used(self):
        path = self.write(columns=["observation_id", "time_utc", "lat", "lon"])
        observations = load_observations(path)
        self.assertEqual(observations.columns["id"], "observation_id")

    def test_a_duplicated_id_column_falls_back_to_row_numbers(self):
        path = self.tmp / "dupes.csv"
        with path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["id", "time_utc"])
            for i in range(6):
                stamp = BASE + timedelta(seconds=i)
                writer.writerow(["same-for-everything",
                                 stamp.isoformat().replace("+00:00", "Z")])

        observations = load_observations(path)
        self.assertEqual(len({o.external_id for o in observations}), 6)
        self.assertTrue(any("not unique" in w for w in observations.warnings))


class TestTimeColumnChoice(ExportTestCase):
    def test_utc_is_chosen_over_local_wall_clock(self):
        observations = load_observations(self.write())
        self.assertEqual(observations.columns["time"], "time_utc")
        self.assertEqual(observations[0].utc, BASE)

    def test_the_local_column_is_ranked_last(self):
        observations = load_observations(self.write())
        names = [name for name, _, _ in observations.time_candidates]
        self.assertEqual(names[0], "time_utc")
        self.assertEqual(names[-1], "time_local_eest")

    def test_an_epoch_column_alone_is_used_happily(self):
        observations = load_observations(
            self.write(columns=["ts_utc_ms", "lat", "lon"]))
        self.assertEqual(observations.columns["time"], "ts_utc_ms")
        self.assertEqual(observations[0].utc, BASE)

    def test_a_local_only_export_is_read_but_flagged(self):
        observations = load_observations(
            self.write(columns=["time_local_eest", "lat", "lon"]))
        self.assertEqual(observations.columns["time"], "time_local_eest")
        self.assertTrue(any("local" in w for w in observations.warnings),
                        "reading local time as UTC must not pass silently")

    def test_an_explicit_column_wins(self):
        observations = load_observations(self.write(), time_column="time_local_eest")
        self.assertEqual(observations.columns["time"], "time_local_eest")
        self.assertEqual(observations[0].utc, BASE + EEST)

    def test_naming_a_missing_column_fails_loudly(self):
        with self.assertRaises(ObservationError):
            load_observations(self.write(), time_column="nope")

    def test_scoring_prefers_a_zoned_utc_column(self):
        utc_score, _ = score_time_column("time_utc", ["2026-08-26T08:47:53Z"])
        local_score, _ = score_time_column("time_local_eest", ["2026-08-26 11:47:53"])
        epoch_score, _ = score_time_column("ts_utc_ms", ["1787734073659"])
        self.assertGreater(utc_score, epoch_score)
        self.assertGreater(epoch_score, local_score)


class TestTimezone(ExportTestCase):
    def test_a_named_zone_converts_naive_timestamps(self):
        path = self.write(columns=["time_local_eest", "lat", "lon"])
        observations = load_observations(path, timezone_name="Europe/Tallinn")
        # 11:47:53 in Tallinn during August is 08:47:53 UTC.
        self.assertEqual(observations[0].utc, BASE)

    def test_without_a_zone_a_naive_timestamp_is_read_as_utc(self):
        path = self.write(columns=["time_local_eest", "lat", "lon"])
        observations = load_observations(path)
        self.assertEqual(observations[0].utc, BASE + EEST)

    def test_summer_and_winter_are_handled_differently(self):
        # A fixed offset cannot do this; a named zone can.
        summer = parse_timestamp("2026-08-26 12:00:00", _zone("Europe/Tallinn"))
        winter = parse_timestamp("2026-12-26 12:00:00", _zone("Europe/Tallinn"))
        self.assertEqual(summer.hour, 9)     # EEST, UTC+3
        self.assertEqual(winter.hour, 10)    # EET, UTC+2

    def test_a_zone_never_overrides_a_stamp_that_states_its_own(self):
        path = self.write()
        observations = load_observations(path, timezone_name="America/New_York")
        self.assertEqual(observations[0].utc, BASE)

    def test_an_unknown_zone_warns_rather_than_failing(self):
        path = self.write(columns=["time_local_eest", "lat", "lon"])
        observations = load_observations(path, timezone_name="Mars/Olympus_Mons")
        self.assertTrue(any("could not be loaded" in w for w in observations.warnings))
        self.assertEqual(len(observations), 20)


class TestOtherColumns(ExportTestCase):
    def test_the_machine_key_becomes_the_category_and_the_label_the_note(self):
        observations = load_observations(self.write())
        self.assertEqual(observations.columns["category"], "observation_type")
        self.assertEqual(observations.columns["note"], "label")
        self.assertEqual(observations[0].category, "difficult_curb")
        self.assertEqual(observations[0].note, "Difficult curb")

    def test_one_column_is_never_used_as_both_category_and_note(self):
        observations = load_observations(self.write(columns=["label", "time_utc"]))
        self.assertEqual(observations.columns["category"], "label")
        self.assertIsNone(observations.columns["note"])

    def test_coordinates_are_read(self):
        observations = load_observations(self.write())
        self.assertAlmostEqual(observations[0].lat, 58.3781, places=4)
        self.assertAlmostEqual(observations[0].lon, 24.5019, places=4)

    def test_the_same_export_also_serves_as_a_phone_track(self):
        # It carries a timestamp, coordinates and an accuracy per row, which is
        # exactly what the phone GNSS loader wants.
        fixes = load_phone_track(self.write())
        self.assertEqual(len(fixes), 20)
        self.assertEqual(fixes[0].utc, BASE)
        self.assertAlmostEqual(fixes[0].accuracy_m, 3.3, places=2)


def _zone(name):
    from zoneinfo import ZoneInfo
    return ZoneInfo(name)


if __name__ == "__main__":
    unittest.main()
