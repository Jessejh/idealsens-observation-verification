"""Tests for the Supabase client, in particular resumable proxy uploads."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from curbtool.supabase_io import Cancelled, SupabaseClient, SupabaseError, _tus_metadata
from tests.fakesupabase import FakeSupabase

CHUNK = 64 * 1024  # small chunks keep the tests quick


class SupabaseTestCase(unittest.TestCase):
    def setUp(self):
        self.server = FakeSupabase().start()
        self.tmp = Path(tempfile.mkdtemp())
        self.client = SupabaseClient(self.server.url, "service-key-not-a-real-one",
                                     state_dir=self.tmp / "uploads",
                                     max_attempts=2, backoff_base=0.01)

    def tearDown(self):
        self.server.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_file(self, name: str, size: int) -> Path:
        path = self.tmp / name
        path.write_bytes(bytes(range(256)) * (size // 256) + b"\x00" * (size % 256))
        return path


class TestTusMetadata(unittest.TestCase):
    def test_values_are_base64_encoded_pairs(self):
        encoded = _tus_metadata({"bucketName": "proxies", "objectName": "a/b.mp4"})
        self.assertEqual(encoded, "bucketName cHJveGllcw==,objectName YS9iLm1wNA==")


class TestResumableUpload(SupabaseTestCase):
    def test_uploads_a_file_in_chunks(self):
        source = self.make_file("proxy.mp4", CHUNK * 3 + 100)
        seen = []
        result = self.client.upload_resumable(
            "proxies", "campaign/proxy.mp4", source, chunk_size=CHUNK,
            on_progress=lambda sent, total: seen.append(sent))

        self.assertEqual(result.size_bytes, source.stat().st_size)
        self.assertEqual(self.server.objects["proxies/campaign/proxy.mp4"],
                         source.read_bytes())
        # Progress is reported per chunk, not once at the end.
        self.assertGreaterEqual(len(seen), 4)
        self.assertEqual(seen[-1], source.stat().st_size)
        self.assertEqual(seen, sorted(seen))

    def test_object_metadata_names_the_bucket_and_path(self):
        source = self.make_file("proxy.mp4", CHUNK)
        self.client.upload_resumable("proxies", "c/GX010042.mp4", source, chunk_size=CHUNK)
        upload, = self.server.uploads.values()
        self.assertEqual(upload["meta"]["bucketName"], "proxies")
        self.assertEqual(upload["meta"]["objectName"], "c/GX010042.mp4")
        self.assertEqual(upload["meta"]["contentType"], "video/mp4")

    def test_resumes_from_the_server_offset_after_a_dropped_connection(self):
        source = self.make_file("proxy.mp4", CHUNK * 4)
        self.server.fail_patch_after = CHUNK * 2  # drop during the third chunk

        result = self.client.upload_resumable(
            "proxies", "campaign/proxy.mp4", source, chunk_size=CHUNK)

        # The whole file still lands, and it was not restarted from zero.
        self.assertEqual(self.server.objects["proxies/campaign/proxy.mp4"],
                         source.read_bytes())
        created = [r for r in self.server.requests
                   if r == ("POST", "/storage/v1/upload/resumable")]
        self.assertEqual(len(created), 1, "a dropped chunk must not restart the upload")

    def test_resumes_across_a_restart_of_the_tool(self):
        source = self.make_file("proxy.mp4", CHUNK * 4)

        # First run: cancel after two chunks, as if the operator hit Cancel.
        sent = []
        with self.assertRaises(Cancelled):
            self.client.upload_resumable(
                "proxies", "campaign/proxy.mp4", source, chunk_size=CHUNK,
                on_progress=lambda s, t: sent.append(s),
                should_cancel=lambda: len(sent) > 2)
        partial = len(self.server.uploads["u1"]["buffer"])
        self.assertGreater(partial, 0)
        self.assertNotIn("proxies/campaign/proxy.mp4", self.server.objects)

        # Second run with a brand new client, as after closing and reopening.
        fresh = SupabaseClient(self.server.url, "service-key-not-a-real-one",
                               state_dir=self.tmp / "uploads",
                               max_attempts=2, backoff_base=0.01)
        resumed = []
        result = fresh.upload_resumable(
            "proxies", "campaign/proxy.mp4", source, chunk_size=CHUNK,
            on_progress=lambda s, t: resumed.append(s))

        self.assertTrue(result.resumed)
        self.assertEqual(resumed[0], partial, "resume must start where the server left off")
        self.assertEqual(self.server.objects["proxies/campaign/proxy.mp4"],
                         source.read_bytes())
        self.assertEqual(len(self.server.uploads), 1, "resume must reuse the upload URL")

    def test_skips_an_object_that_is_already_the_right_size(self):
        source = self.make_file("proxy.mp4", CHUNK)
        self.server.objects["proxies/campaign/proxy.mp4"] = source.read_bytes()

        result = self.client.upload_resumable(
            "proxies", "campaign/proxy.mp4", source, chunk_size=CHUNK)

        self.assertTrue(result.skipped)
        self.assertEqual(len(self.server.uploads), 0, "nothing should have been re-sent")

    def test_state_file_is_cleaned_up_after_success(self):
        source = self.make_file("proxy.mp4", CHUNK * 2)
        self.client.upload_resumable("proxies", "c/p.mp4", source, chunk_size=CHUNK)
        self.assertEqual(list((self.tmp / "uploads").glob("*.json")), [])

    def test_missing_file_is_reported_clearly(self):
        with self.assertRaises(SupabaseError):
            self.client.upload_resumable("proxies", "c/p.mp4", self.tmp / "nope.mp4")


class TestFrameUpload(SupabaseTestCase):
    def test_small_objects_go_up_on_the_standard_endpoint(self):
        frame = self.make_file("frame.jpg", 4096)
        result = self.client.upload("frames", "session/obs/00.jpg", frame)
        self.assertEqual(result.size_bytes, 4096)
        self.assertEqual(self.server.objects["frames/session/obs/00.jpg"], frame.read_bytes())
        self.assertEqual(len(self.server.uploads), 0, "frames must not use TUS")

    def test_paths_with_spaces_are_escaped(self):
        frame = self.make_file("frame.jpg", 16)
        self.client.upload("frames", "a b/c d.jpg", frame)
        self.assertIn("frames/a b/c d.jpg", self.server.objects)

    def test_public_url_points_at_the_public_object_route(self):
        url = self.client.public_url("frames", "session/obs/00.jpg")
        self.assertEqual(url, f"{self.server.url}/storage/v1/object/public/frames/session/obs/00.jpg")


class TestRows(SupabaseTestCase):
    def test_upsert_merges_duplicates_rather_than_inserting_twice(self):
        row = {"id": "abc", "campaign": "helsinki"}
        self.client.upsert("sessions", [row])
        self.client.upsert("sessions", [{"id": "abc", "campaign": "helsinki-2024"}])
        self.assertEqual(len(self.server.tables["sessions"]), 1)
        self.assertEqual(self.server.tables["sessions"][0]["campaign"], "helsinki-2024")

    def test_upsert_of_nothing_makes_no_request(self):
        before = len(self.server.requests)
        self.assertEqual(self.client.upsert("sessions", []), [])
        self.assertEqual(len(self.server.requests), before)

    def test_upsert_batches_large_row_sets(self):
        rows = [{"id": f"row{i}"} for i in range(1200)]
        self.client.upsert("frames", rows, chunk_size=500)
        posts = [r for r in self.server.requests if r == ("POST", "/rest/v1/frames")]
        self.assertEqual(len(posts), 3)
        self.assertEqual(len(self.server.tables["frames"]), 1200)

    def test_count_reads_the_content_range_header(self):
        self.client.upsert("observations", [{"id": f"o{i}"} for i in range(7)])
        self.assertEqual(self.client.count("observations"), 7)

    def test_delete_without_a_filter_is_refused(self):
        # A DELETE with no filter against PostgREST empties the table.
        with self.assertRaises(SupabaseError):
            self.client.delete("observations", {})

    def test_ensure_bucket_creates_only_when_missing(self):
        self.client.ensure_bucket("frames")
        self.assertIn("frames", self.server.buckets)
        creates = [r for r in self.server.requests if r == ("POST", "/storage/v1/bucket")]
        self.client.ensure_bucket("frames")
        creates_after = [r for r in self.server.requests if r == ("POST", "/storage/v1/bucket")]
        self.assertEqual(len(creates), len(creates_after))


class TestClientConstruction(unittest.TestCase):
    def test_refuses_to_start_without_credentials(self):
        with self.assertRaises(SupabaseError):
            SupabaseClient("", "")
        with self.assertRaises(SupabaseError):
            SupabaseClient("https://x.supabase.co", "")


if __name__ == "__main__":
    unittest.main()
