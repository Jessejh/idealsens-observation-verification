"""GUI tests: widget wiring, the settings panel, and the threading model.

Skipped where Tkinter or a display is unavailable — the pipeline it drives is
covered by test_pipeline.py regardless. Run them with:

    xvfb-run -a python -m unittest tests.test_gui
"""

from __future__ import annotations

import gc
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def _probe_tk() -> tuple[bool, str]:
    """Can we open a window here?

    The probe interpreter is destroyed and dropped inside this function. A
    lingering reference to a dead Tk gets finalised on whichever thread happens
    to trigger the collection, and Tcl aborts the process when that is not the
    thread that created it — which is how a GUI test module takes the rest of
    the suite down with it.
    """
    try:
        import tkinter
        probe = tkinter.Tk()
        probe.destroy()
        del probe
        return True, ""
    except Exception as exc:  # no Tkinter, or no display
        return False, str(exc)


TK_AVAILABLE, TK_REASON = _probe_tk()

# Tk keeps process-global state and aborts with "Tcl_AsyncDelete: async handler
# deleted by the wrong thread" if any of it is finalised on a worker thread. The
# other test modules run HTTP servers and pipeline workers, so these tests get a
# process to themselves. run_tests.py does that for you; so does
#     CURBTOOL_GUI_TESTS=1 python -m unittest tests.test_gui
if not os.environ.get("CURBTOOL_GUI_TESTS"):
    TK_AVAILABLE, TK_REASON = False, (
        "GUI tests need their own process — run run_tests.py, or set "
        "CURBTOOL_GUI_TESTS=1")
if TK_AVAILABLE:
    import tkinter as tk
else:  # keep the module importable so the skips can report why
    tk = None  # type: ignore[assignment]

_ROOT = None


def _shared_root():
    """One hidden Tk interpreter, reused by every test in this module."""
    global _ROOT
    if _ROOT is None:
        _ROOT = tk.Tk()
        _ROOT.withdraw()
    return _ROOT

from tests.gopro_fixture import drive_plan, patch_read_payloads, telemetry_payloads, write_clip

UTC = timezone.utc
BASE = datetime(2024, 6, 1, 8, 0, 0, tzinfo=UTC)


def tearDownModule():
    """Destroy the shared interpreter while its variables are still collectable.

    Everything Tk must be gone before a later test module starts threads: a
    stray Tk object finalised on a worker thread aborts the whole process with
    Tcl_AsyncDelete.
    """
    global _ROOT
    gc.collect()
    if _ROOT is not None:
        try:
            _ROOT.destroy()
        except Exception:
            pass
        _ROOT = None
    gc.collect()


@unittest.skipUnless(TK_AVAILABLE, f"Tkinter unavailable: {TK_REASON}")
class GuiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.footage = Path(tempfile.mkdtemp())
        write_clip(cls.footage / "GX010042.MP4", 20.0)
        write_clip(cls.footage / "GX010043.MP4", 20.0)
        cls.payloads = telemetry_payloads(drive_plan(BASE, [(6.0, 14.0)], 20.0))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.footage, ignore_errors=True)

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        # Redirect ~/.curbtool.json so tests never touch the real settings.
        import curbtool.config as config
        self._saved_config_path = config.CONFIG_PATH
        config.CONFIG_PATH = self.home / ".curbtool.json"

        from curbtool.config import Settings
        Settings(campaign="test-campaign", work_dir=str(self.home / "work"),
                 upload=False, max_frames=2, frame_width=320,
                 proxy_height=180, proxy_bitrate_kbps=300).save(config.CONFIG_PATH)

        from curbtool import gui as gui_module
        self.gui_module = gui_module
        # One Tcl interpreter for the whole run, a fresh window per test:
        # creating and tearing down interpreters instead trips Tcl_AsyncDelete
        # when Tk variables outlive the interpreter that owns them.
        self.root = tk.Toplevel(_shared_root())
        self.root.withdraw()
        self.app = gui_module.CurbToolGUI(self.root)
        self.app.settings = Settings.load(config.CONFIG_PATH)
        self.app._load_settings_into_widgets()
        self.root.update()

    def tearDown(self):
        self.app.shutdown()
        if self.app.worker is not None:
            self.app.worker.join(timeout=60)
        # Drop every reference to a tk.Variable while its interpreter is still
        # running: collected afterwards, their __del__ calls into a dead Tk.
        self.app.vars.clear()
        self.app.status_var = None
        self.app.entries.clear()
        self.app = None
        gc.collect()
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        self.root = None
        gc.collect()
        import curbtool.config as config
        config.CONFIG_PATH = self._saved_config_path
        shutil.rmtree(self.home, ignore_errors=True)

    def pump(self, seconds: float = 0.4) -> None:  # noqa: D401
        """Run the Tk event loop for a while, as a real session would."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.02)

    def wait_for(self, predicate, timeout: float = 180.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.root.update()
            if predicate():
                return True
            time.sleep(0.05)
        return False


class TestFileList(GuiTestCase):
    def test_adding_a_folder_lists_its_videos(self):
        self.app.add_paths([str(self.footage)])
        self.root.update()
        self.assertEqual(len(self.app.entries), 2)
        self.assertEqual(len(self.app.tree.get_children()), 2)

    def test_the_same_file_is_not_added_twice(self):
        self.app.add_paths([str(self.footage)])
        self.app.add_paths([str(self.footage)])
        self.root.update()
        self.assertEqual(len(self.app.entries), 2)

    def test_length_and_lrv_are_filled_in_by_the_background_probe(self):
        self.app.add_paths([str(self.footage)])
        self.assertTrue(self.wait_for(
            lambda: all(e.duration_s is not None for e in self.app.entries), timeout=30))
        entry = self.app.entries[0]
        self.assertEqual(entry.duration_text, "0:20")
        self.assertFalse(entry.lrv)
        self.assertIn("0:20", self.app.tree.item(str(entry.path), "values"))

    def test_lrv_companion_is_detected(self):
        shutil.copy(self.footage / "GX010042.MP4", self.footage / "GX010042.LRV")
        try:
            self.app.add_paths([str(self.footage / "GX010042.MP4")])
            self.assertTrue(self.wait_for(
                lambda: self.app.entries and self.app.entries[0].duration_s is not None,
                timeout=30))
            self.assertTrue(self.app.entries[0].lrv)
        finally:
            (self.footage / "GX010042.LRV").unlink(missing_ok=True)

    def test_an_lrv_is_not_listed_as_a_file_to_process(self):
        shutil.copy(self.footage / "GX010042.MP4", self.footage / "GX010042.LRV")
        try:
            self.app.add_paths([str(self.footage)])
            self.root.update()
            self.assertEqual(len(self.app.entries), 2)
        finally:
            (self.footage / "GX010042.LRV").unlink(missing_ok=True)


class TestSettingsPanel(GuiTestCase):
    def test_saved_settings_populate_the_panel(self):
        self.assertEqual(self.app.vars["campaign"].get(), "test-campaign")
        self.assertFalse(self.app.vars["upload"].get())

    def test_edits_are_read_back_with_the_right_types(self):
        self.app.vars["campaign"].set("helsinki-2024")
        self.app.vars["clock_offset_s"].set("-3600")
        self.app.vars["max_frames"].set("7")
        self.app.vars["upload"].set(True)
        settings = self.app._settings_from_widgets()
        self.assertEqual(settings.campaign, "helsinki-2024")
        self.assertEqual(settings.clock_offset_s, -3600.0)
        self.assertEqual(settings.max_frames, 7)
        self.assertIs(settings.upload, True)

    def test_a_decimal_comma_is_accepted(self):
        self.app.vars["stop_speed_mps"].set("0,9")
        self.assertAlmostEqual(self.app._settings_from_widgets().stop_speed_mps, 0.9)

    def test_a_typo_keeps_the_previous_value(self):
        before = self.app.settings.stop_speed_mps
        self.app.vars["stop_speed_mps"].set("fast")
        self.assertEqual(self.app._settings_from_widgets().stop_speed_mps, before)

    def test_settings_survive_a_restart(self):
        import curbtool.config as config
        self.app.vars["campaign"].set("persisted-campaign")
        self.app._settings_from_widgets().save(config.CONFIG_PATH)

        from curbtool.config import Settings
        self.assertEqual(Settings.load(config.CONFIG_PATH).campaign, "persisted-campaign")

    def test_starting_without_a_campaign_is_refused(self):
        shown = []
        self.gui_module.messagebox.showerror = lambda *a, **k: shown.append(a)
        self.app.add_paths([str(self.footage)])
        self.app.vars["campaign"].set("")
        self.app.start()
        self.assertTrue(shown, "an empty campaign must be refused, not silently used")
        self.assertIsNone(self.app.worker)


class TestRunning(GuiTestCase):
    def setUp(self):
        super().setUp()
        self.gui_module.messagebox.showwarning = lambda *a, **k: None
        self.gui_module.messagebox.showinfo = lambda *a, **k: None
        self.app.add_paths([str(self.footage)])
        self.root.update()

    def test_a_batch_runs_to_completion_and_re_enables_the_controls(self):
        with patch_read_payloads(self.payloads):
            self.app.start()
            finished = self.wait_for(lambda: self.app.worker is None)
        self.assertTrue(finished, "the batch did not finish in time")

        self.assertEqual([e.status for e in self.app.entries], ["done", "done"])
        self.assertEqual(str(self.app.start_button["state"]), "normal")
        self.assertEqual(str(self.app.cancel_button["state"]), "disabled")
        self.assertEqual(self.app.status_var.get(), "idle")

    def test_progress_reaches_every_file_and_the_rows_update(self):
        with patch_read_payloads(self.payloads):
            self.app.start()
            self.wait_for(lambda: self.app.worker is None)
        for entry in self.app.entries:
            self.assertEqual(entry.fraction, 1.0)
            self.assertIn("done", self.app.tree.item(str(entry.path), "values"))

    def test_cancel_stops_the_batch_before_the_second_file(self):
        with patch_read_payloads(self.payloads):
            self.app.start()
            # Let the first file get going, then cancel as the operator would.
            self.wait_for(lambda: any(e.status == "running" for e in self.app.entries),
                          timeout=60)
            self.app.cancel()
            finished = self.wait_for(lambda: self.app.worker is None, timeout=120)

        self.assertTrue(finished, "cancel must actually stop the worker")
        self.assertTrue(any(e.status in ("cancelled", "queued") for e in self.app.entries),
                        "at least one file should not have completed")
        self.assertEqual(str(self.app.start_button["state"]), "normal")

    def test_one_failing_file_does_not_abort_the_batch(self):
        broken = self.footage / "BROKEN.MP4"
        broken.write_bytes(b"not a video at all")
        try:
            self.app.entries.clear()
            self.app.add_paths([str(broken), str(self.footage / "GX010042.MP4")])
            self.root.update()
            with patch_read_payloads(self.payloads):
                self.app.start()
                self.wait_for(lambda: self.app.worker is None)

            statuses = {e.path.name: e.status for e in self.app.entries}
            self.assertEqual(statuses["BROKEN.MP4"], "failed")
            self.assertEqual(statuses["GX010042.MP4"], "done")
            self.assertIn("FAILED", self.app.log_text.get("1.0", "end"))
        finally:
            broken.unlink(missing_ok=True)

    def test_the_summary_is_logged_and_written(self):
        with patch_read_payloads(self.payloads):
            self.app.start()
            self.wait_for(lambda: self.app.worker is None)
        log = self.app.log_text.get("1.0", "end")
        self.assertIn("frames extracted", log)
        summary = Path(self.app.settings.work_dir) / "test-campaign" / "summary.json"
        self.assertTrue(summary.exists(), "the batch summary should be saved")


@unittest.skipUnless(TK_AVAILABLE, f"Tkinter unavailable: {TK_REASON}")
class TestStageWeighting(unittest.TestCase):
    def test_per_file_progress_never_steps_backwards(self):
        from curbtool.gui import _overall_fraction
        from curbtool.pipeline import STAGES, Progress

        values = [_overall_fraction(Progress("f", stage, step, 10))
                  for stage in STAGES for step in range(11)]
        self.assertEqual(values, sorted(values))
        self.assertEqual(values[0], 0.0)
        self.assertEqual(values[-1], 1.0)


if __name__ == "__main__":
    unittest.main()
