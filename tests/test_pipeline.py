"""Pipeline tests: matching, idempotency, cancellation and the batch summary.

The end-to-end cases run real decoding and real transcoding against a
generated clip, and real HTTP against the fake Supabase. Only the GPMF track
is substituted, because PyAV cannot mux a timed-metadata track — the telemetry
itself is genuine KLV parsed by the real parser.
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

from curbtool import gpmf, pipeline
from curbtool.config import Settings
from curbtool.observations import Observation, PhoneFix, load_observations, load_phone_track
from curbtool.pipeline import (BatchSummary, Cancelled, IngestJob, IngestResult,
                               ingest_file, match_observations, session_id_for)
from curbtool.supabase_io import SupabaseClient
from tests.fakesupabase import FakeSupabase
from tests.gopro_fixture import drive_plan, patch_read_payloads, telemetry_payloads, write_clip

UTC = timezone.utc
BASE = datetime(2024, 6, 1, 8, 0, 0, tzinfo=UTC)
STOPS = [(6.0, 14.0), (20.0, 30.0)]
CLIP_SECONDS = 40.0


def make_telemetry():
    rows = drive_plan(BASE, STOPS, CLIP_SECONDS)
    payloads = telemetry_payloads(rows)
    return payloads, gpmf.parse_payloads(payloads)


def observation(external_id: str, at_s: float, category: str = "curb") -> Observation:
    return Observation(external_id=external_id, utc=BASE + timedelta(seconds=at_s),
                       lat=None, lon=None, category=category, note="")


class TestMatching(unittest.TestCase):
    def setUp(self):
        _, self.samples = make_telemetry()
        self.stops = gpmf.detect_stops(self.samples, speed_threshold=0.7, min_duration_s=3.0)
        self.settings = Settings(campaign="test")
        self.session = session_id_for("test", "GX010042.MP4", 1234)

    def match(self, observations, **kwargs):
        settings = self.settings.merged(**kwargs) if kwargs else self.settings
        return match_observations(observations, self.samples, self.stops,
                                  self.session, settings)

    def test_a_tag_during_a_stop_snaps_to_that_stop(self):
        matches, out = self.match([observation("a", 9.0)])
        self.assertEqual(out, 0)
        self.assertEqual(len(matches), 1)
        self.assertTrue(matches[0].snapped)
        self.assertEqual(matches[0].stop.index, 0)
        # The window is the whole stationary period, not a fixed span.
        self.assertAlmostEqual(matches[0].window_start_s, 6.0, places=1)
        self.assertAlmostEqual(matches[0].window_end_s, 13.0, places=1)

    def test_a_tag_while_moving_falls_back_to_a_fixed_window(self):
        matches, _ = self.match([observation("a", 17.0)])
        self.assertFalse(matches[0].snapped)
        self.assertAlmostEqual(matches[0].window_mid_s, 17.0, places=1)
        self.assertAlmostEqual(matches[0].window_end_s - matches[0].window_start_s,
                               2 * self.settings.fallback_window_s, places=1)

    def test_a_tag_outside_the_file_window_is_counted_out_of_range(self):
        # Each chapter gets the whole campaign CSV and keeps only its own rows.
        matches, out = self.match([observation("a", 9.0), observation("b", 5000.0)])
        self.assertEqual(len(matches), 1)
        self.assertEqual(out, 1)

    def test_clock_offset_shifts_the_whole_campaign(self):
        # A tag stamped an hour early matches once the offset corrects it.
        early = Observation(external_id="a", utc=BASE + timedelta(seconds=9) - timedelta(hours=1),
                            lat=None, lon=None)
        matches, out = self.match([early])
        self.assertEqual((len(matches), out), (0, 1))

        matches, out = self.match([early], clock_offset_s=3600.0)
        self.assertEqual(len(matches), 1)
        self.assertTrue(matches[0].snapped)

    def test_matches_are_returned_in_timeline_order(self):
        matches, _ = self.match([observation("late", 25.0), observation("early", 9.0)])
        self.assertEqual([m.observation.external_id for m in matches], ["early", "late"])

    def test_observation_ids_are_stable_across_runs(self):
        first, _ = self.match([observation("a", 9.0)])
        second, _ = self.match([observation("a", 9.0)])
        self.assertEqual(first[0].observation_id, second[0].observation_id)

    def test_gopro_position_is_taken_from_the_middle_of_the_stop(self):
        matches, _ = self.match([observation("a", 9.0)])
        self.assertIsNotNone(matches[0].gopro_lat)
        expected = gpmf.offset_to_latlon(self.samples, matches[0].window_mid_s)
        self.assertAlmostEqual(matches[0].gopro_lat, expected[0], places=6)


class TestPhonePosition(unittest.TestCase):
    def setUp(self):
        _, self.samples = make_telemetry()
        self.stops = gpmf.detect_stops(self.samples, speed_threshold=0.7, min_duration_s=3.0)
        self.settings = Settings(campaign="test")
        self.session = session_id_for("test", "GX010042.MP4", 1234)

    def phone_fixes(self, lat: float, lon: float) -> list[PhoneFix]:
        return [PhoneFix(utc=BASE + timedelta(seconds=t), lat=lat, lon=lon, accuracy_m=3.0)
                for t in range(6, 15)]

    def test_phone_position_wins_where_a_log_exists(self):
        fixes = self.phone_fixes(60.17050, 24.94050)
        matches, _ = match_observations([observation("a", 9.0)], self.samples, self.stops,
                                        self.session, self.settings, fixes)
        lat, lon, source = matches[0].position
        self.assertEqual(source, "phone")
        self.assertAlmostEqual(lat, 60.17050, places=6)
        self.assertGreater(matches[0].phone_fix_count, 1, "fixes should be averaged")

    def test_disagreement_between_receivers_is_recorded(self):
        # Put the phone about 30 m from where the camera thinks it is.
        matches, _ = match_observations([observation("a", 9.0)], self.samples, self.stops,
                                        self.session, self.settings,
                                        self.phone_fixes(60.17055, 24.94000))
        gap = matches[0].gps_disagreement_m
        self.assertIsNotNone(gap)
        self.assertGreater(gap, 5.0)
        self.assertLess(gap, 200.0)

    def test_gopro_position_is_used_when_there_is_no_phone_log(self):
        matches, _ = match_observations([observation("a", 9.0)], self.samples, self.stops,
                                        self.session, self.settings, [])
        self.assertEqual(matches[0].position[2], "gopro")
        self.assertIsNone(matches[0].gps_disagreement_m)


class EndToEndTestCase(unittest.TestCase):
    """Real decode, real transcode, real HTTP — only the GPMF track is injected."""

    @classmethod
    def setUpClass(cls):
        cls.clip_dir = Path(tempfile.mkdtemp())
        cls.clip = write_clip(cls.clip_dir / "GX010042.MP4", CLIP_SECONDS)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.clip_dir, ignore_errors=True)

    def setUp(self):
        self.server = FakeSupabase().start()
        self.tmp = Path(tempfile.mkdtemp())
        self.client = SupabaseClient(self.server.url, "service-key",
                                     state_dir=self.tmp / "uploads",
                                     max_attempts=2, backoff_base=0.01)
        self.payloads, _ = make_telemetry()
        self.settings = Settings(
            campaign="helsinki-2024",
            work_dir=str(self.tmp / "work"),
            max_frames=3,
            frame_width=640,
            proxy_height=240,
            proxy_bitrate_kbps=400,
        )

    def tearDown(self):
        self.server.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def job(self, observations=None, **kwargs) -> IngestJob:
        return IngestJob(
            video=self.clip,
            settings=self.settings,
            observations=observations if observations is not None
            else [observation("obs-1", 9.0), observation("obs-2", 25.0)],
            client=self.client,
            **kwargs,
        )

    def run_ingest(self, job=None, **kwargs):
        events = []
        with patch_read_payloads(self.payloads):
            result = ingest_file(job or self.job(), on_progress=events.append, **kwargs)
        return result, events


class TestEndToEnd(EndToEndTestCase):
    def test_processes_a_file_into_rows_media_and_a_proxy(self):
        result, events = self.run_ingest()

        self.assertEqual(result.status, "done")
        self.assertEqual(result.matched, 2)
        self.assertEqual(result.snapped, 2)
        self.assertEqual(result.stops, 2)
        self.assertEqual(result.frames, 6)          # 2 observations x 3 frames
        self.assertGreater(result.proxy_bytes, 0)
        self.assertTrue(result.uploaded)

        self.assertEqual(len(self.server.tables["observations"]), 2)
        self.assertEqual(len(self.server.tables["frames"]), 6)
        self.assertEqual(len(self.server.tables["sessions"]), 1)
        self.assertGreater(len(self.server.tables["track_points"]), 10)

        frames = [k for k in self.server.objects if k.startswith("frames/")]
        proxies = [k for k in self.server.objects if k.startswith("proxies/")]
        self.assertEqual(len(frames), 6)
        self.assertEqual(len(proxies), 1)

    def test_session_is_marked_complete_only_at_the_end(self):
        self.run_ingest()
        session = self.server.tables["sessions"][0]
        self.assertEqual(session["ingest_status"], "complete")
        self.assertIsNotNone(session["ingested_at"])
        self.assertEqual(session["observation_count"], 2)
        self.assertEqual(session["frame_count"], 6)

    def test_frame_delta_is_zero_at_the_middle_of_the_stop(self):
        self.run_ingest()
        rows = sorted(self.server.tables["frames"], key=lambda r: (r["observation_id"], r["seq"]))
        deltas = [r["delta_s"] for r in rows[:3]]
        self.assertEqual(len(deltas), 3)
        # Three frames across the window: one before the middle, one at it, one after.
        self.assertAlmostEqual(deltas[1], 0.0, delta=0.6)
        self.assertLess(deltas[0], deltas[1])
        self.assertLess(deltas[1], deltas[2])

    def test_every_stage_reports_progress(self):
        _, events = self.run_ingest()
        stages = {event.stage for event in events}
        self.assertEqual(stages, set(pipeline.STAGES))
        # The two slow stages report real per-item movement, not a spinner.
        frame_events = [e for e in events if e.stage == "frames"]
        proxy_events = [e for e in events if e.stage == "proxy"]
        self.assertGreater(len({e.current for e in frame_events}), 2)
        self.assertGreater(len({e.current for e in proxy_events}), 5)

    def test_rerunning_a_completed_file_is_a_no_op(self):
        self.run_ingest()
        before = len(self.server.requests)

        result, _ = self.run_ingest()

        self.assertEqual(result.status, "skipped")
        self.assertEqual(len(self.server.tables["observations"]), 2, "must not duplicate")
        self.assertLess(len(self.server.requests) - before, 10,
                        "a skip should cost one query, not a re-upload")

    def test_a_different_file_is_not_skipped_by_the_resume_check(self):
        # The resume check must look up this file's own session, not any
        # completed session: two chapters differ only by filename.
        self.run_ingest()
        other = self.clip_dir / "GX010043.MP4"
        shutil.copy(self.clip, other)
        try:
            result, _ = self.run_ingest(job=self.job())
            self.assertEqual(result.status, "skipped")

            job = IngestJob(video=other, settings=self.settings,
                            observations=[observation("obs-1", 9.0)], client=self.client)
            second, _ = self.run_ingest(job=job)
            self.assertEqual(second.status, "done")
            self.assertNotEqual(second.session_id, result.session_id)
            self.assertEqual(len(self.server.tables["sessions"]), 2)
        finally:
            other.unlink(missing_ok=True)

    def test_force_reruns_and_still_does_not_duplicate(self):
        self.run_ingest()
        first_ids = {r["id"] for r in self.server.tables["observations"]}

        result, _ = self.run_ingest(job=self.job(force=True))

        self.assertEqual(result.status, "done")
        self.assertEqual(len(self.server.tables["observations"]), 2)
        self.assertEqual({r["id"] for r in self.server.tables["observations"]}, first_ids,
                         "derived IDs must survive a re-ingest so reviews stay attached")

    def test_cancelling_stops_and_leaves_the_session_incomplete(self):
        events = []

        def cancel():
            return len([e for e in events if e.stage == "frames"]) > 1

        with patch_read_payloads(self.payloads):
            with self.assertRaises(Cancelled):
                ingest_file(self.job(), on_progress=events.append, should_cancel=cancel)

        sessions = self.server.tables.get("sessions", [])
        self.assertTrue(all(s["ingest_status"] != "complete" for s in sessions),
                        "a cancelled run must not leave a session looking finished")

    def test_upload_disabled_leaves_media_in_the_work_folder(self):
        self.settings = self.settings.merged(upload=False)
        result, _ = self.run_ingest()

        self.assertEqual(result.status, "done")
        self.assertFalse(result.uploaded)
        self.assertEqual(self.server.objects, {})
        self.assertEqual(self.server.tables, {})
        work = Path(self.settings.work_dir) / "helsinki-2024" / "GX010042"
        self.assertTrue((work / "GX010042_proxy.mp4").exists())
        self.assertEqual(len(list((work / "frames").rglob("*.jpg"))), 6)

    def test_a_file_with_no_observations_still_builds_a_proxy_and_track(self):
        result, _ = self.run_ingest(job=self.job(observations=[]))
        self.assertEqual(result.status, "done")
        self.assertEqual(result.matched, 0)
        self.assertEqual(result.frames, 0)
        self.assertGreater(result.proxy_bytes, 0)
        self.assertGreater(len(self.server.tables["track_points"]), 10)

    def test_suggests_a_clock_offset_when_nothing_matches(self):
        # Tags exported in local time (UTC+3) instead of UTC.
        shifted = [Observation(external_id="a", utc=BASE + timedelta(seconds=9, hours=3),
                               lat=None, lon=None)]
        result, _ = self.run_ingest(job=self.job(observations=shifted))
        self.assertEqual(result.matched, 0)
        self.assertIn("clock-offset", result.hint)
        self.assertIn("-3", result.hint)

    def test_a_second_run_resumes_the_proxy_upload_rather_than_restarting(self):
        self.run_ingest()
        uploads_before = len(self.server.uploads)
        self.run_ingest(job=self.job(force=True))
        # The object is already there at the same size, so nothing is re-sent.
        self.assertEqual(len(self.server.uploads), uploads_before)


class TestBatchSummary(unittest.TestCase):
    def summary(self) -> BatchSummary:
        s = BatchSummary(campaign="helsinki-2024", csv_rows=340)
        s.add(IngestResult(file="GX010042.MP4", session_id="a", status="done",
                           matched=200, snapped=180, frames=600, proxy_bytes=400_000_000))
        s.add(IngestResult(file="GX010043.MP4", session_id="b", status="done",
                           matched=112, snapped=100, frames=330, proxy_bytes=380_000_000))
        return s

    def test_reports_total_matched_against_the_csv(self):
        text = self.summary().render()
        self.assertIn("312 matched of 340 rows", text)

    def test_shouts_about_observations_that_matched_nothing(self):
        # The number nobody notices unless the tool says it out loud.
        text = self.summary().render()
        self.assertIn("28 observation(s) matched no file", text)

    def test_stays_quiet_when_everything_matched(self):
        s = BatchSummary(csv_rows=10)
        s.add(IngestResult(file="a.MP4", session_id="a", status="done", matched=10))
        self.assertNotIn("matched no file", s.render())

    def test_flags_more_matches_than_rows(self):
        s = BatchSummary(csv_rows=10)
        s.add(IngestResult(file="a.MP4", session_id="a", status="done", matched=8))
        s.add(IngestResult(file="b.MP4", session_id="b", status="done", matched=6))
        self.assertIn("overlapping chapters", s.render())

    def test_counts_statuses_and_shows_per_file_snap_ratio(self):
        s = self.summary()
        s.add(IngestResult(file="GX010044.MP4", session_id="c", status="failed",
                           error="no GPS fixes"))
        text = s.render()
        self.assertIn("90%", text)          # 180/200
        self.assertIn("1 failed", text)
        self.assertIn("2 done", text)
        self.assertIn("no GPS fixes", text)

    def test_saves_a_machine_readable_copy(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            path = self.summary().save(tmp / "summary.json")
            import json
            data = json.loads(path.read_text())
            self.assertEqual(data["total_matched"], 312)
            self.assertEqual(data["csv_rows"], 340)
            self.assertEqual(data["unmatched"], 28)
            self.assertEqual(len(data["files"]), 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
