"""Tests for frame extraction — the single-pass batch decoder.

The one change in this project that could alter *output* rather than just
speed, so the central test is equality against the previous one-call-per-
observation behaviour rather than a fresh set of expectations.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import av

from curbtool import media
from tests.gopro_fixture import write_clip

CLIP_SECONDS = 12.0


class ExtractTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp())
        cls.clip = write_clip(cls.dir / "GX010042.MP4", CLIP_SECONDS,
                              width=640, height=360)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        self.work = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def requests(self, windows, interval=0.5, cap=9):
        plans = [media.frame_times(a, b, interval, cap) for a, b in windows]
        return plans, [media.FrameRequest(t, self.work / f"new{i}", "f")
                       for i, t in enumerate(plans)]

    @staticmethod
    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()


class TestEqualsThePreviousBehaviour(ExtractTestCase):
    """The batch pass must pick exactly the frames the old one picked."""

    WINDOWS = [(1.0, 3.0),      # plain
               (2.5, 4.5),      # overlaps the previous window
               (4.4, 5.0),      # adjacent, short
               (9.0, 11.5)]     # far away — the only one worth a seek

    def run_both(self, windows=None, **kwargs):
        windows = windows or self.WINDOWS
        plans, requests = self.requests(windows, **kwargs)
        old = [media.extract_frames(self.clip, t, self.work / f"old{i}",
                                    prefix="f", width=320)
               for i, t in enumerate(plans)]
        new = media.extract_frames_multi(self.clip, requests, width=320)
        return old, new

    def test_the_same_offsets_come_back(self):
        old, new = self.run_both()
        self.assertEqual([[round(o, 6) for o, _ in group] for group in old],
                         [[round(o, 6) for o, _ in group] for group in new])

    def test_the_same_filenames_come_back(self):
        old, new = self.run_both()
        self.assertEqual([[p.name for _, p in group] for group in old],
                         [[p.name for _, p in group] for group in new])

    def test_the_jpegs_are_byte_identical(self):
        old, new = self.run_both()
        for group_old, group_new in zip(old, new):
            for (_, a), (_, b) in zip(group_old, group_new):
                self.assertEqual(self.digest(a), self.digest(b), a.name)

    def test_widely_separated_windows_still_agree(self):
        # Forces a seek between every observation.
        old, new = self.run_both(windows=[(0.5, 1.0), (5.0, 5.5), (10.0, 10.5)])
        self.assertEqual([[round(o, 6) for o, _ in g] for g in old],
                         [[round(o, 6) for o, _ in g] for g in new])

    def test_a_long_sparse_window_still_agrees(self):
        # Nine frames spread over ten seconds: the case the old code decoded
        # end to end and the new one segments.
        old, new = self.run_both(windows=[(1.0, 11.0)], interval=1.0, cap=9)
        self.assertEqual([round(o, 6) for o, _ in old[0]],
                         [round(o, 6) for o, _ in new[0]])
        self.assertEqual(len(new[0]), 9)


class TestOnePassPerChapter(ExtractTestCase):
    def count_opens(self, fn):
        opens = []
        real = av.open

        def counting(*args, **kwargs):
            opens.append(args[0] if args else kwargs.get("file"))
            return real(*args, **kwargs)

        av.open = counting
        try:
            fn()
        finally:
            av.open = real
        return len(opens)

    def test_the_file_is_opened_once_for_the_whole_chapter(self):
        _, requests = self.requests([(1.0, 3.0), (5.0, 6.0), (9.0, 10.0)])
        opens = self.count_opens(
            lambda: media.extract_frames_multi(self.clip, requests, width=320))
        self.assertEqual(opens, 1)

    def test_the_old_path_opened_it_once_per_observation(self):
        # Guards the claim this change is based on.
        plans, _ = self.requests([(1.0, 3.0), (5.0, 6.0), (9.0, 10.0)])
        opens = self.count_opens(lambda: [
            media.extract_frames(self.clip, t, self.work / f"o{i}", prefix="f",
                                 width=320)
            for i, t in enumerate(plans)])
        self.assertEqual(opens, 3)


class TestSegmentPlanning(unittest.TestCase):
    def plan(self, windows, gap=media.SEEK_GAP_S):
        requests = [media.FrameRequest(t, Path(f"/tmp/x{i}"), "f")
                    for i, t in enumerate(windows)]
        return media._plan_segments(requests, gap)

    def test_overlapping_windows_merge_into_one_segment(self):
        segments = self.plan([[1.0, 1.5, 2.0], [1.8, 2.3, 2.8]])
        self.assertEqual(len(segments), 1)
        self.assertEqual(len(segments[0]), 6)

    def test_a_far_window_starts_a_new_segment(self):
        segments = self.plan([[1.0, 1.5], [30.0, 30.5]])
        self.assertEqual(len(segments), 2)

    def test_a_sparse_window_is_split_so_it_is_not_decoded_end_to_end(self):
        # Nine targets 7.5 s apart: nine segments, not one 60 s decode.
        segments = self.plan([[i * 7.5 for i in range(9)]])
        self.assertEqual(len(segments), 9)

    def test_targets_are_globally_time_ordered(self):
        segments = self.plan([[10.0], [1.0], [5.0]])
        times = [item[0] for segment in segments for item in segment]
        self.assertEqual(times, sorted(times))

    def test_negative_targets_are_dropped(self):
        segments = self.plan([[-2.0, 1.0]])
        self.assertEqual([item[0] for s in segments for item in s], [1.0])

    def test_no_targets_means_no_segments(self):
        self.assertEqual(self.plan([[]]), [])


class TestCancellation(ExtractTestCase):
    def test_the_batch_pass_raises_rather_than_returning_early(self):
        # A cancel during the last observation used to return quietly, so the
        # run only stopped later at the proxy stage.
        _, requests = self.requests([(1.0, 3.0), (5.0, 7.0), (9.0, 11.0)])
        seen = {"n": 0}

        def cancel():
            seen["n"] += 1
            return seen["n"] > 40

        with self.assertRaises(media.Cancelled):
            media.extract_frames_multi(self.clip, requests, width=320,
                                       should_cancel=cancel)

    def test_the_single_observation_wrapper_still_returns_what_it_managed(self):
        targets = media.frame_times(1.0, 5.0, 0.5, 9)
        seen = {"n": 0}

        def cancel():
            seen["n"] += 1
            return seen["n"] > 25

        written = media.extract_frames(self.clip, targets, self.work / "one",
                                       prefix="f", width=320, should_cancel=cancel)
        self.assertLess(len(written), len(targets))


class TestScaling(ExtractTestCase):
    def test_frames_are_downscaled_to_the_requested_width(self):
        from PIL import Image
        written = media.extract_frames(self.clip, [2.0], self.work / "s",
                                       prefix="f", width=320)
        with Image.open(written[0][1]) as image:
            self.assertEqual(image.size, (320, 180))
            self.assertEqual(image.mode, "RGB")

    def test_a_narrower_source_is_never_upscaled(self):
        # The default 1280 would otherwise blow up a 640-wide .LRV.
        from PIL import Image
        written = media.extract_frames(self.clip, [2.0], self.work / "u",
                                       prefix="f", width=1280)
        with Image.open(written[0][1]) as image:
            self.assertEqual(image.size, (640, 360))

    def test_progressive_is_off_by_default_and_optimize_is_on(self):
        plain = media.extract_frames(self.clip, [2.0], self.work / "a",
                                     prefix="f", width=320)
        unoptimised = media.extract_frames(self.clip, [2.0], self.work / "b",
                                           prefix="f", width=320, optimize=False)
        self.assertLess(plain[0][1].stat().st_size, unoptimised[0][1].stat().st_size,
                        "optimize=True should still be shrinking the file")


class TestErrors(ExtractTestCase):
    def test_a_corrupt_file_reports_frame_extraction_so_it_codes_as_E302(self):
        from curbtool import errors
        broken = self.work / "BROKEN.MP4"
        broken.write_bytes(b"not a video at all")
        _, requests = self.requests([(1.0, 2.0)])
        with self.assertRaises(media.MediaError) as caught:
            media.extract_frames_multi(broken, requests)
        self.assertIn("frame extraction", str(caught.exception))
        self.assertEqual(errors.classify(caught.exception).code, "E302")

    def test_no_targets_returns_empty_lists_without_opening_anything(self):
        requests = [media.FrameRequest([], self.work / "empty", "f")]
        self.assertEqual(media.extract_frames_multi(self.clip, requests), [[]])


if __name__ == "__main__":
    unittest.main()
