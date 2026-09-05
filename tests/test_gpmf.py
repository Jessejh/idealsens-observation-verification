"""Unit tests for the GPMF parser, time mapping and stop detection."""

from __future__ import annotations

import math
import os
import struct
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from curbtool import gpmf
from tests import klvfixtures as fx

UTC = timezone.utc


class TestKlvLayer(unittest.TestCase):
    def test_reads_key_type_size_and_repeat(self):
        raw = fx.klv("GPSF", "L", 4, 1, struct.pack(">I", 3))
        item, = list(gpmf.iter_klv(raw))
        self.assertEqual(item.key, "GPSF")
        self.assertEqual(item.type, "L")
        self.assertEqual(item.struct_size, 4)
        self.assertEqual(item.repeat, 1)
        self.assertEqual(gpmf.klv_values(item), [(3,)])

    def test_skips_four_byte_alignment_padding(self):
        # A 2-byte payload is padded with 2 bytes; the next record must still parse.
        raw = fx.klv("GPSP", "S", 2, 1, struct.pack(">H", 180)) + fx.gpsf(3)
        keys = [i.key for i in gpmf.iter_klv(raw)]
        self.assertEqual(keys, ["GPSP", "GPSF"])

    def test_nested_containers_expose_their_children(self):
        raw = fx.nested("DEVC", fx.text("DVNM", "HERO5 Black"))
        outer, = list(gpmf.iter_klv(raw))
        self.assertTrue(outer.is_nested)
        inner, = list(gpmf.iter_klv(outer.payload))
        self.assertEqual(gpmf.klv_values(inner), ["HERO5 Black"])

    def test_truncated_tail_stops_cleanly_instead_of_raising(self):
        # A card pulled mid-write leaves a half-written final payload. The rest
        # of the file is still worth having.
        raw = fx.gpsf(3) + fx.klv("GPS5", "l", 20, 4, b"\x00" * 8)
        keys = [i.key for i in gpmf.iter_klv(raw)]
        self.assertEqual(keys, ["GPSF"])

    def test_repeated_scalars_decode_as_one_row_each(self):
        # SCAL for GPS5 is written as five int32 structs, not one struct of five.
        item, = list(gpmf.iter_klv(fx.scal(10000000, 10000000, 1000, 1000, 1000)))
        self.assertEqual(item.struct_size, 4)
        self.assertEqual(item.repeat, 5)
        self.assertEqual(gpmf.klv_values(item),
                         [(10000000,), (10000000,), (1000,), (1000,), (1000,)])

    def test_multi_value_structs_decode_as_rows(self):
        # GPS5 packs five int32 into one 20-byte struct per sample.
        item, = list(gpmf.iter_klv(fx.gps5([(60.1701, 24.9412, 15.0, 4.2, 4.3)])))
        self.assertEqual(item.struct_size, 20)
        self.assertEqual(gpmf.klv_values(item), [(601701000, 249412000, 15000, 4200, 4300)])


class TestGpsuStamps(unittest.TestCase):
    def test_parses_fractional_seconds_as_utc(self):
        item, = list(gpmf.iter_klv(fx.gpsu("170417105755.123")))
        stamp, = gpmf.klv_values(item)
        self.assertEqual(stamp, datetime(2017, 4, 17, 10, 57, 55, 123000, tzinfo=UTC))

    def test_malformed_stamp_yields_none_rather_than_raising(self):
        item, = list(gpmf.iter_klv(fx.gpsu("99999999999999")))
        self.assertIsNone(gpmf.klv_values(item)[0])


class TestGps5(unittest.TestCase):
    def setUp(self):
        self.rows = [
            (60.170100, 24.941200, 15.0, 0.10, 0.12),
            (60.170110, 24.941220, 15.1, 4.20, 4.30),
            (60.170120, 24.941240, 15.2, 4.10, 4.20),
        ]
        self.payload = fx.gps5_payload(self.rows, stamp="170417105755.000")

    def test_applies_scal_divisors(self):
        samples = gpmf.parse_payload(self.payload, offset_s=10.0, duration_s=1.0)
        self.assertEqual(len(samples), 3)
        self.assertAlmostEqual(samples[0].lat, 60.170100, places=6)
        self.assertAlmostEqual(samples[0].lon, 24.941200, places=6)
        self.assertAlmostEqual(samples[0].alt_m, 15.0, places=3)
        self.assertAlmostEqual(samples[1].speed_2d, 4.20, places=3)

    def test_spreads_samples_across_the_payload_duration(self):
        samples = gpmf.parse_payload(self.payload, offset_s=10.0, duration_s=1.5)
        self.assertAlmostEqual(samples[0].offset_s, 10.0, places=6)
        self.assertAlmostEqual(samples[1].offset_s, 10.5, places=6)
        self.assertAlmostEqual(samples[2].offset_s, 11.0, places=6)

    def test_payload_gpsu_advances_across_samples(self):
        samples = gpmf.parse_payload(self.payload, offset_s=0.0, duration_s=3.0)
        self.assertEqual(samples[0].utc, datetime(2017, 4, 17, 10, 57, 55, tzinfo=UTC))
        self.assertEqual(samples[2].utc, datetime(2017, 4, 17, 10, 57, 57, tzinfo=UTC))

    def test_payload_gpsf_and_gpsp_apply_to_every_sample(self):
        samples = gpmf.parse_payload(self.payload, offset_s=0.0, duration_s=1.0)
        self.assertTrue(all(s.fix == 3 for s in samples))
        self.assertTrue(all(abs(s.dop - 1.8) < 1e-6 for s in samples))

    def test_drops_samples_below_the_fix_threshold(self):
        # A cold-started camera reports fix 0 and timestamps from its RTC, which
        # can be minutes off. Those must not reach the matcher.
        cold = fx.gps5_payload(self.rows, fix=0)
        self.assertEqual(gpmf.parse_payload(cold, 0.0, 1.0), [])
        self.assertEqual(len(gpmf.parse_payload(cold, 0.0, 1.0, min_fix=0)), 3)

    def test_drops_null_island_positions(self):
        rows = [(0.0, 0.0, 0.0, 0.0, 0.0)] + self.rows
        samples = gpmf.parse_payload(fx.gps5_payload(rows), 0.0, 1.0)
        self.assertEqual(len(samples), 3)


class TestGps9(unittest.TestCase):
    def test_reads_per_sample_time_dop_and_fix_via_type(self):
        # 2024-06-01T08:30:15Z = 8918 days after 2000-01-01, 30615 s into the day.
        rows = [
            (60.1701, 24.9412, 15.0, 0.1, 0.12, 8918, 30615.0, 1.4, 3),
            (60.1702, 24.9413, 15.1, 0.2, 0.22, 8918, 30616.0, 1.4, 3),
        ]
        samples = gpmf.parse_payload(fx.gps9_payload(rows), offset_s=5.0, duration_s=2.0)
        self.assertEqual(len(samples), 2)
        self.assertEqual(samples[0].utc, datetime(2024, 6, 1, 8, 30, 15, tzinfo=UTC))
        self.assertEqual(samples[1].utc, datetime(2024, 6, 1, 8, 30, 16, tzinfo=UTC))
        self.assertAlmostEqual(samples[0].lat, 60.1701, places=6)
        self.assertAlmostEqual(samples[0].dop, 1.4, places=3)
        self.assertEqual(samples[0].fix, 3)

    def test_drops_low_fix_samples_individually(self):
        rows = [
            (60.1701, 24.9412, 15.0, 0.1, 0.12, 8918, 30615.0, 9.9, 0),
            (60.1702, 24.9413, 15.1, 0.2, 0.22, 8918, 30616.0, 1.4, 3),
        ]
        samples = gpmf.parse_payload(fx.gps9_payload(rows), 0.0, 2.0)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].fix, 3)


def synthetic_track():
    """A drive with three stops, at 1 Hz.

    Roll for 20 s, stand still for 12 s, roll 20 s, stand still for 4 s, roll
    20 s, stand still 15 s, roll 10 s. Positions advance only while moving.
    """
    plan = [(20, 5.0), (12, 0.0), (20, 5.0), (4, 0.0), (20, 5.0), (15, 0.0), (10, 5.0)]
    samples = []
    t = 0.0
    lat, lon = 60.1700, 24.9400
    base = datetime(2024, 6, 1, 8, 0, 0, tzinfo=UTC)
    for count, speed in plan:
        for _ in range(count):
            # A metre per second of jitter while parked is normal GPS noise.
            jitter = 0.4 if speed == 0.0 and len(samples) % 5 == 0 else 0.0
            samples.append(gpmf.GpsSample(
                offset_s=t, utc=base + timedelta(seconds=t), lat=lat, lon=lon,
                alt_m=15.0, speed_2d=speed + jitter, speed_3d=speed + jitter,
                fix=3, dop=1.5,
            ))
            lat += speed * 9e-6
            t += 1.0
    return samples


class TestStopDetection(unittest.TestCase):
    def setUp(self):
        self.samples = synthetic_track()
        self.stops = gpmf.detect_stops(self.samples)

    def test_finds_the_stops_that_meet_the_minimum_duration(self):
        # The 4 s stop is below the 3 s floor but above it too; all three qualify.
        self.assertEqual(len(self.stops), 3)

    def test_short_stops_can_be_excluded_by_raising_the_floor(self):
        stops = gpmf.detect_stops(self.samples, min_duration_s=6.0)
        self.assertEqual(len(stops), 2)
        self.assertTrue(all(s.duration_s >= 6.0 for s in stops))

    def test_stop_bounds_match_the_stationary_period(self):
        first = self.stops[0]
        self.assertAlmostEqual(first.start_s, 20.0, places=3)
        self.assertAlmostEqual(first.end_s, 31.0, places=3)
        self.assertAlmostEqual(first.mid_s, 25.5, places=3)

    def test_gps_jitter_at_standstill_does_not_split_a_stop(self):
        # The 15 s stop contains samples that jitter above the threshold.
        long_stop = max(self.stops, key=lambda s: s.duration_s)
        self.assertGreater(long_stop.duration_s, 12.0)

    def test_stop_position_averages_the_stationary_fixes(self):
        first = self.stops[0]
        self.assertAlmostEqual(first.lat, 60.1700 + 20 * 5.0 * 9e-6, places=5)

    def test_stop_for_offset_finds_the_containing_stop(self):
        found = gpmf.stop_for_offset(self.stops, 25.0)
        self.assertIsNotNone(found)
        self.assertEqual(found.index, 0)

    def test_stop_for_offset_tolerates_a_tag_just_after_moving_off(self):
        # The operator often taps the phone as the scooter is rolling away.
        self.assertIsNotNone(gpmf.stop_for_offset(self.stops, 32.5, tolerance_s=2.0))
        self.assertIsNone(gpmf.stop_for_offset(self.stops, 45.0, tolerance_s=2.0))

    def test_stop_for_offset_returns_none_while_moving(self):
        self.assertIsNone(gpmf.stop_for_offset(self.stops, 10.0))


class TestTimeMapping(unittest.TestCase):
    def setUp(self):
        self.samples = synthetic_track()
        self.base = self.samples[0].utc

    def test_maps_utc_onto_the_video_timeline(self):
        offset = gpmf.utc_to_offset(self.samples, self.base + timedelta(seconds=25))
        self.assertAlmostEqual(offset, 25.0, places=3)

    def test_interpolates_between_fixes(self):
        offset = gpmf.utc_to_offset(self.samples, self.base + timedelta(seconds=25.4))
        self.assertAlmostEqual(offset, 25.4, places=3)

    def test_returns_none_outside_the_file_window(self):
        self.assertIsNone(gpmf.utc_to_offset(self.samples, self.base - timedelta(seconds=30)))
        self.assertIsNone(gpmf.utc_to_offset(self.samples, self.base + timedelta(hours=1)))

    def test_bridges_a_gps_dropout_with_the_linear_fit(self):
        # Lose 30 s of fixes in the middle. Position is unknown there, but time
        # is not: both clocks are the camera's and advance together.
        gapped = [s for s in self.samples if not (40.0 <= s.offset_s < 70.0)]
        offset = gpmf.utc_to_offset(gapped, self.base + timedelta(seconds=55))
        self.assertIsNotNone(offset)
        self.assertAlmostEqual(offset, 55.0, places=1)

    def test_offset_to_latlon_interpolates_position(self):
        pos = gpmf.offset_to_latlon(self.samples, 10.5)
        self.assertIsNotNone(pos)
        lat, lon = pos
        self.assertAlmostEqual(lat, (self.samples[10].lat + self.samples[11].lat) / 2, places=7)

    def test_offset_to_latlon_returns_none_past_the_end(self):
        self.assertIsNone(gpmf.offset_to_latlon(self.samples, 10_000.0))

    def test_mean_position_averages_a_window(self):
        result = gpmf.mean_position(self.samples, 20.0, 31.0)
        self.assertIsNotNone(result)
        lat, lon, count = result
        self.assertEqual(count, 12)


class TestHaversine(unittest.TestCase):
    def test_known_distance(self):
        # One degree of latitude is a bit over 111 km.
        d = gpmf.haversine_m(60.0, 24.0, 61.0, 24.0)
        self.assertAlmostEqual(d, 111195.0, delta=50.0)

    def test_zero_for_identical_points(self):
        self.assertEqual(gpmf.haversine_m(60.17, 24.94, 60.17, 24.94), 0.0)

    def test_small_east_west_offset(self):
        # At 60 degrees north a degree of longitude is about half its equatorial width.
        d = gpmf.haversine_m(60.0, 24.0, 60.0, 24.001)
        self.assertAlmostEqual(d, 55.6, delta=1.0)


class TestPayloadSequence(unittest.TestCase):
    def test_parse_payloads_orders_and_offsets_samples(self):
        rows = [(60.1701, 24.9412, 15.0, 1.0, 1.1)]
        payloads = [
            (2.0, 1.0, fx.gps5_payload(rows, stamp="240601080002.000")),
            (0.0, 1.0, fx.gps5_payload(rows, stamp="240601080000.000")),
            (1.0, 1.0, fx.gps5_payload(rows, stamp="240601080001.000")),
        ]
        samples = gpmf.parse_payloads(payloads)
        self.assertEqual([s.offset_s for s in samples], [0.0, 1.0, 2.0])
        self.assertEqual(samples[0].utc, datetime(2024, 6, 1, 8, 0, 0, tzinfo=UTC))
        self.assertEqual(samples[2].utc, datetime(2024, 6, 1, 8, 0, 2, tzinfo=UTC))


if __name__ == "__main__":
    unittest.main()
