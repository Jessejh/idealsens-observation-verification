#!/usr/bin/env python3
"""Run the whole test suite.

The GUI tests get their own process. Tk keeps process-global state and aborts
with "Tcl_AsyncDelete: async handler deleted by the wrong thread" if any of it
is finalised on a worker thread — and the other modules run HTTP servers and
pipeline workers. Isolating them is cheaper than the alternatives and does not
weaken either half.

    python run_tests.py            # everything
    python run_tests.py --no-gui   # skip the GUI process
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest


def run_core() -> bool:
    loader = unittest.TestLoader()
    suite = loader.discover("tests", top_level_dir=".")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return result.wasSuccessful()


def run_gui() -> bool | None:
    """Returns True/False, or None if Tkinter or a display is unavailable."""
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("\nGUI tests skipped: Tkinter is not installed "
              "(on Debian/Ubuntu: apt install python3-tk).")
        return None

    env = {**os.environ, "CURBTOOL_GUI_TESTS": "1"}
    print("\n" + "=" * 70)
    print("GUI tests (separate process)")
    print("=" * 70)
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "tests.test_gui", "-v"], env=env)
    if completed.returncode != 0:
        print("\nIf this failed with a display error, there is no window system here. "
              "On a headless machine: xvfb-run -a python run_tests.py")
        return False
    return True


def main() -> int:
    core_ok = run_core()
    gui_ok = None if "--no-gui" in sys.argv else run_gui()

    print("\n" + "=" * 70)
    print(f"core: {'PASS' if core_ok else 'FAIL'}")
    print(f"gui : {'PASS' if gui_ok else ('SKIPPED' if gui_ok is None else 'FAIL')}")
    print("=" * 70)
    return 0 if core_ok and gui_ok is not False else 1


if __name__ == "__main__":
    sys.exit(main())
