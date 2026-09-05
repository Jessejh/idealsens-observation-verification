"""Tests for the pre-flight check.

Its whole job is to be trusted before an afternoon is committed, so the cases
that matter are the ones where it could mislead: advising a campaign-wide clock
shift to accommodate one stray tag, or reporting READY when observations are
missing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from curbtool import gpmf
from curbtool.config import Settings
from curbtool.observations import Observation, suggest_clock_offset
from curbtool.verify import CampaignCheck, FileCheck, check_campaign
from tests.gopro_fixture import drive_plan, patch_read_payloads, telemetry_payloads

UTC = timezone.utc
BASE = datetime(2024, 6, 1, 8, 0, 0, tzinfo=UTC)


def observation(external_id: str, at_s: float) -> Observation:
    return Observation(external_id=external_id, utc=BASE + timedelta(seconds=at_s),
                       lat=None, lon=None, category="curb")


class TestClockOffsetSuggestion(unittest.TestCase):
    """A clock offset is systematic. The suggestion must reflect that."""

    def setUp(self):
        self.start = BASE
        self.end = BASE + timedelta(seconds=60)

    def test_picks_the_shift_that_rescues_the_most_observations(self):
        # Four tags three hours late, one stray tag nine hours late. The right
        # answer is -3 h, which fixes four — not -9 h, which fixes one.
        rows = [observation(f"o{i}", 0) for i in range(4)]
        for row in rows:
            row.utc += timedelta(hours=3)
        stray = observation("stray", 0)
        stray.utc += timedelta(hours=9)

        result = suggest_clock_offset(rows + [stray], self.start, self.end)
        self.assertIsNotNone(result)
        seconds, count = result
        self.assertEqual(seconds, -3 * 3600)
        self.assertEqual(count, 4)

    def test_reports_none_when_no_whole_hour_helps(self):
        far = observation("a", 0)
        far.utc += timedelta(days=30)
        self.assertIsNone(suggest_clock_offset([far], self.start, self.end))

    def test_reports_none_for_no_observations(self):
        self.assertIsNone(suggest_clock_offset([], self.start, self.end))


class TestCampaignCheck(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(campaign="test")
        self.payloads = telemetry_payloads(drive_plan(BASE, [(6.0, 14.0), (20.0, 30.0)], 40.0))

    def check(self, observations, settings=None):
        with patch_read_payloads(self.payloads):
            return check_campaign([Path(__file__)], observations, settings or self.settings)

    def test_counts_matched_and_snapped_without_decoding_anything(self):
        result = self.check([observation("a", 9.0), observation("b", 25.0)])
        self.assertEqual(result.csv_rows, 2)
        self.assertEqual(result.matched_count, 2)
        self.assertEqual(result.total_snapped, 2)
        self.assertTrue(result.ready)

    def test_a_tag_while_moving_counts_as_matched_but_not_snapped(self):
        result = self.check([observation("moving", 17.0)])
        self.assertEqual(result.matched_count, 1)
        self.assertEqual(result.total_snapped, 0)

    def test_unmatched_observations_are_listed_and_block_ready(self):
        result = self.check([observation("in", 9.0), observation("out", 5000.0)])
        self.assertEqual(len(result.unmatched), 1)
        self.assertEqual(result.unmatched[0].external_id, "out")
        self.assertFalse(result.ready)
        self.assertIn("out", result.render())

    def test_one_stray_tag_does_not_trigger_clock_offset_advice(self):
        # Four of five match. That is a stray tag, not a broken clock, and
        # advising a shift would break the four that work.
        stray = observation("stray", 0)
        stray.utc += timedelta(hours=4)
        result = self.check([observation("a", 9.0), observation("b", 25.0),
                             observation("c", 10.0), observation("d", 26.0), stray])
        self.assertEqual(len(result.unmatched), 1)
        self.assertIsNone(result.clock_offset_hint)
        self.assertNotIn("clock-offset", result.render())

    def test_a_whole_campaign_in_local_time_does_trigger_it(self):
        rows = [observation("a", 9.0), observation("b", 25.0), observation("c", 10.0)]
        for row in rows:
            row.utc += timedelta(hours=3)
        result = self.check(rows)

        self.assertEqual(result.matched_count, 0)
        self.assertEqual(result.clock_offset_hint, -3 * 3600)
        self.assertEqual(result.clock_offset_rescues, 3)
        self.assertIn("--clock-offset -10800", result.render())
        self.assertFalse(result.ready)

    def test_applying_the_suggested_offset_makes_it_ready(self):
        rows = [observation("a", 9.0), observation("b", 25.0)]
        for row in rows:
            row.utc += timedelta(hours=3)
        first = self.check(rows)
        self.assertFalse(first.ready)

        fixed = self.check(rows, self.settings.merged(
            clock_offset_s=first.clock_offset_hint))
        self.assertTrue(fixed.ready)
        self.assertEqual(fixed.matched_count, 2)

    def test_an_unreadable_file_is_reported_and_blocks_ready(self):
        def explode(_path):
            raise gpmf.GpmfError("no GPS fixes at quality >= 2")

        original = gpmf.read_payloads
        gpmf.read_payloads = explode
        try:
            result = check_campaign([Path(__file__)], [observation("a", 9.0)], self.settings)
        finally:
            gpmf.read_payloads = original

        self.assertFalse(result.files[0].ok)
        self.assertFalse(result.ready)
        self.assertIn("UNREADABLE", result.render())

    def test_the_render_names_the_window_stops_and_snap_ratio(self):
        text = self.check([observation("a", 9.0), observation("b", 17.0)]).render()
        self.assertIn("08:00:00-08:00:39", text)
        self.assertIn("50%", text)          # one of two landed during a stop

    def test_a_low_snap_ratio_warns_without_blocking(self):
        # Everything matched, so the run is ready — but half the tags landed
        # while moving, which means stop detection wants tuning.
        result = self.check([observation("a", 9.0), observation("b", 17.0)])
        self.assertTrue(result.ready)
        self.assertIn("--stop-speed", result.render())

    def test_a_healthy_snap_ratio_stays_quiet(self):
        result = self.check([observation("a", 9.0), observation("b", 25.0)])
        self.assertNotIn("--stop-speed", result.render())

    def test_ready_says_so_plainly(self):
        self.assertIn("READY: a full ingest should account for every observation",
                      self.check([observation("a", 9.0)]).render())


if __name__ == "__main__":
    unittest.main()
