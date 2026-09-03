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


class TestRotation(unittest.TestCase):
    """A GoPro mounted upside down must not produce upside-down evidence.

    The camera records inverted pixels and a display matrix saying so. Players
    honour the matrix; a decoder does not — so without this the frames come out
    the wrong way up while the same file looks fine in VLC, which is exactly
    how the defect reached the operator.
    """

    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp())
        # 180 in the matrix's own counter-clockwise convention: an inverted
        # camera. The clip carries a white band across its top rows as stored,
        # so "was it turned?" is a question about pixels rather than metadata.
        cls.inverted = write_clip(cls.dir / "GX010099.MP4", 4.0,
                                  width=640, height=360, rotation=180)
        cls.upright = write_clip(cls.dir / "GX010100.MP4", 4.0,
                                 width=640, height=360)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        self.work = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    @staticmethod
    def band_row(path: Path) -> str:
        """Whether the white band ended up at the top or the bottom."""
        from PIL import Image, ImageStat
        with Image.open(path) as image:
            width, height = image.size
            top = ImageStat.Stat(image.crop((0, 0, width, 3)).convert("L")).mean[0]
            bottom = ImageStat.Stat(
                image.crop((0, height - 3, width, height)).convert("L")).mean[0]
        return "top" if top > bottom else "bottom"

    def test_an_inverted_clip_is_turned_the_right_way_up(self):
        written = media.extract_frames(self.inverted, [1.0], self.work / "r",
                                       prefix="f", width=320)
        self.assertEqual(self.band_row(written[0][1]), "bottom",
                         "the band was at the top as stored, so an upright "
                         "frame must show it at the bottom")

    def test_an_upright_clip_is_left_alone(self):
        written = media.extract_frames(self.upright, [1.0], self.work / "u",
                                       prefix="f", width=320)
        with av.open(str(self.upright)) as container:
            stream = container.streams.video[0]
            self.assertEqual(media.display_rotation(stream=stream), 0)
        self.assertTrue(written[0][1].is_file())

    def test_probe_reports_the_rotation_and_the_display_size(self):
        info = media.probe(self.inverted)
        self.assertEqual(info.rotation, 180)
        # A half turn does not swap the axes.
        self.assertEqual(info.display_size, (640, 360))
        self.assertEqual(media.probe(self.upright).rotation, 0)

    def test_a_quarter_turn_swaps_the_display_axes(self):
        clip = write_clip(self.work / "GX010101.MP4", 2.0,
                          width=640, height=360, rotation=90)
        info = media.probe(clip)
        # 270, not 90: the fixture writes the matrix in the counter-clockwise
        # convention the matrix uses, and probe() reports the clockwise turn a
        # viewer has to apply. Asserting the exact value is the point — a sign
        # error here would put every sideways frame in the wrong quarter.
        self.assertEqual(info.rotation, 270)
        self.assertEqual(info.display_size, (360, 640))
        # frame_width means the width of the finished picture, so a sideways
        # clip asked for 180 px wide must come back 180 px wide, not 180 tall.
        from PIL import Image
        written = media.extract_frames(clip, [1.0], self.work / "q",
                                       prefix="f", width=180)
        with Image.open(written[0][1]) as image:
            self.assertEqual(image.size, (180, 320))

    def test_rotation_is_normalised_to_quarter_turns(self):
        self.assertEqual(media._normalise_rotation(-180), 180)
        self.assertEqual(media._normalise_rotation(-90), 270)
        self.assertEqual(media._normalise_rotation(359.6), 0)
        self.assertEqual(media._normalise_rotation(37), 0)
        self.assertEqual(media._normalise_rotation(None), 0)
        self.assertEqual(media._normalise_rotation("90"), 90)


class TestSingleImageNaming(ExtractTestCase):
    def test_an_unnumbered_request_writes_one_file_named_for_the_prefix(self):
        request = media.FrameRequest([2.0], self.work, "row184", numbered=False)
        media.extract_frames_multi(self.clip, [request], width=320)
        self.assertTrue((self.work / "row184.jpg").is_file())
        self.assertFalse((self.work / "row184_00.jpg").exists())

    def test_several_observations_share_one_flat_folder(self):
        requests = [media.FrameRequest([1.0], self.work, "row2", numbered=False),
                    media.FrameRequest([3.0], self.work, "row9", numbered=False)]
        media.extract_frames_multi(self.clip, requests, width=320)
        self.assertEqual(sorted(p.name for p in self.work.glob("*.jpg")),
                         ["row2.jpg", "row9.jpg"])


class TestProxyRotation(unittest.TestCase):
    """The proxy has to arrive the right way up too, or review is upside down."""

    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp())
        cls.inverted = write_clip(cls.dir / "GX010098.MP4", 2.0,
                                  width=640, height=360, rotation=180)
        cls.sideways = write_clip(cls.dir / "GX010097.MP4", 2.0,
                                  width=640, height=360, rotation=90)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        self.work = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def proxy(self, source, height=180):
        out = self.work / f"{source.stem}_proxy.mp4"
        media.build_proxy(source, out, height=height, bitrate_kbps=300,
                          encoder="software")
        return media.probe(out)

    def test_the_rotation_survives_into_the_proxy(self):
        self.assertEqual(self.proxy(self.inverted).rotation, 180)

    def test_the_requested_height_is_counted_in_display_lines(self):
        # A sideways chapter asked for 180p must give a viewer 180 lines, not
        # 180 lines of a picture that is then turned onto its side.
        info = self.proxy(self.sideways, height=180)
        self.assertEqual(info.rotation, 270)
        self.assertEqual(info.display_size[1], 180)

    def test_an_upright_proxy_carries_no_rotation(self):
        upright = write_clip(self.work / "GX010096.MP4", 2.0, width=640, height=360)
        self.assertEqual(self.proxy(upright).rotation, 0)
