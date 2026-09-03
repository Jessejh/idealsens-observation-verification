"""Tests for settings, and for finding the campaign data shipped with the tool.

The point of the discovery is that a fresh unpack goes straight to Check with
nothing typed. The point of the tests is that it never overrides something the
operator deliberately cleared.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from curbtool import config
from curbtool.config import Settings, bundled_defaults, describe_inputs


class DataDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.config_path = self.tmp / ".curbtool.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_csv(self, name="tags.csv") -> Path:
        path = self.data / name
        path.write_text("time_utc,lat,lon\n2026-08-26T08:47:53Z,58.37,24.50\n")
        return path

    def write_manifest(self, **fields) -> Path:
        path = self.data / "campaign.json"
        path.write_text(json.dumps(fields))
        return path

    def load_with_data(self, config_path=None) -> Settings:
        """Settings.load() as if this temp folder were the shipped data/."""
        original = config.DATA_DIR
        config.DATA_DIR = self.data
        try:
            return Settings.load(config_path or self.config_path)
        finally:
            config.DATA_DIR = original


class TestDiscovery(DataDirTestCase):
    def test_a_manifest_names_the_files(self):
        self.write_csv("parnu.csv")
        self.write_manifest(campaign="parnu-2026", observations="parnu.csv",
                            gnss="parnu.csv")
        found = bundled_defaults(self.data)
        self.assertEqual(found["campaign"], "parnu-2026")
        self.assertEqual(Path(found["observations_csv"]).name, "parnu.csv")
        self.assertEqual(Path(found["gnss_csv"]).name, "parnu.csv")

    def test_a_lone_csv_needs_no_manifest(self):
        self.write_csv("helsinki-2027.csv")
        found = bundled_defaults(self.data)
        self.assertEqual(Path(found["observations_csv"]).name, "helsinki-2027.csv")
        self.assertEqual(found["campaign"], "helsinki-2027")
        self.assertEqual(found["gnss_csv"], found["observations_csv"],
                         "the export doubles as a phone track")

    def test_two_csvs_without_a_manifest_are_ambiguous(self):
        self.write_csv("one.csv")
        self.write_csv("two.csv")
        # Guessing here would be worse than asking.
        self.assertEqual(bundled_defaults(self.data), {})

    def test_a_manifest_disambiguates_two_csvs(self):
        self.write_csv("one.csv")
        self.write_csv("two.csv")
        self.write_manifest(campaign="c", observations="two.csv")
        found = bundled_defaults(self.data)
        self.assertEqual(Path(found["observations_csv"]).name, "two.csv")

    def test_a_manifest_naming_a_missing_file_resolves_nothing(self):
        self.write_manifest(campaign="c", observations="not-here.csv")
        found = bundled_defaults(self.data)
        self.assertNotIn("observations_csv", found)
        self.assertEqual(found.get("campaign"), "c")

    def test_a_broken_manifest_falls_back_to_the_lone_csv(self):
        self.write_csv("only.csv")
        (self.data / "campaign.json").write_text("{ this is not json")
        self.assertEqual(Path(bundled_defaults(self.data)["observations_csv"]).name,
                         "only.csv")

    def test_an_empty_data_folder_finds_nothing(self):
        self.assertEqual(bundled_defaults(self.data), {})

    def test_a_missing_data_folder_finds_nothing(self):
        self.assertEqual(bundled_defaults(self.tmp / "gone"), {})

    def test_the_readme_is_not_mistaken_for_data(self):
        (self.data / "README.md").write_text("# data")
        self.write_csv("only.csv")
        self.assertEqual(Path(bundled_defaults(self.data)["observations_csv"]).name,
                         "only.csv")


class TestLoadAppliesThem(DataDirTestCase):
    def test_a_fresh_install_picks_up_the_shipped_campaign(self):
        self.write_csv("parnu.csv")
        self.write_manifest(campaign="parnu-2026", observations="parnu.csv",
                            gnss="parnu.csv")
        settings = self.load_with_data()
        self.assertEqual(settings.campaign, "parnu-2026")
        self.assertEqual(Path(settings.observations_csv).name, "parnu.csv")

    def test_a_cleared_field_stays_cleared(self):
        # Clearing a field means "none", not "go and find one for me". The web
        # and desktop UIs both rely on this to report E201.
        self.write_csv("parnu.csv")
        Settings(campaign="mine", observations_csv="").save(self.config_path)
        settings = self.load_with_data()
        self.assertEqual(settings.observations_csv, "")
        self.assertEqual(settings.campaign, "mine")

    def test_a_saved_value_beats_the_bundled_one(self):
        self.write_csv("parnu.csv")
        Settings(campaign="mine", observations_csv="/elsewhere/mine.csv").save(
            self.config_path)
        self.assertEqual(self.load_with_data().observations_csv, "/elsewhere/mine.csv")

    def test_a_key_the_saved_file_never_mentions_is_filled(self):
        # A settings file written by an older build, before data/ existed.
        self.write_csv("parnu.csv")
        self.config_path.write_text(json.dumps({"campaign": "older-build"}))
        settings = self.load_with_data()
        self.assertEqual(settings.campaign, "older-build")
        self.assertEqual(Path(settings.observations_csv).name, "parnu.csv")

    def test_an_unreadable_settings_file_still_gets_the_bundle(self):
        self.write_csv("parnu.csv")
        self.config_path.write_text("{ not json at all")
        self.assertEqual(Path(self.load_with_data().observations_csv).name, "parnu.csv")

    def test_the_dataclass_itself_touches_no_files(self):
        # Discovery belongs in load(), not in a dataclass default.
        self.assertEqual(Settings().observations_csv, "")
        self.assertEqual(Settings().campaign, "")


class TestDefaults(unittest.TestCase):
    def test_uploads_are_off_until_supabase_is_set_up(self):
        # .env does not ship, so an upload on a fresh unpack could only fail.
        self.assertFalse(Settings().upload)

    def test_video_is_off(self):
        self.assertEqual(Settings().proxy_source, "none")

    def test_frames_are_full_hd(self):
        # 1280 gave 720p stills, which reviewers found too coarse to grade a
        # cracked kerb from.
        self.assertEqual(Settings().frame_width, 1920)

    def test_a_folder_of_frames_is_still_the_default(self):
        # The single image is the deliberate choice, not the fallback: nine
        # frames around the stop is what rescues a tag whose timing is off.
        self.assertFalse(Settings().single_frame)

    def test_describe_says_when_only_one_image_is_being_cut(self):
        self.assertIn("(single)", Settings(single_frame=True).describe())
        self.assertNotIn("(single)", Settings().describe())

    def test_the_real_bundle_resolves_the_shipped_campaign(self):
        # Guards the thing the operator actually hit: unpack, press Check.
        found = bundled_defaults()
        self.assertIn("observations_csv", found)
        self.assertTrue(Path(found["observations_csv"]).is_file())
        self.assertEqual(found["campaign"], "parnu-2026")


class TestDescribeInputs(DataDirTestCase):
    def test_it_names_the_file_and_counts_the_rows(self):
        csv = self.write_csv("parnu.csv")
        text = describe_inputs(Settings(observations_csv=str(csv)))
        self.assertIn("parnu.csv", text)
        self.assertIn("1 rows", text)

    def test_it_says_when_nothing_is_set(self):
        self.assertIn("no observation CSV", describe_inputs(Settings()))

    def test_it_says_when_the_path_is_wrong(self):
        text = describe_inputs(Settings(observations_csv="/no/such.csv"))
        self.assertIn("not found", text)


if __name__ == "__main__":
    unittest.main()
