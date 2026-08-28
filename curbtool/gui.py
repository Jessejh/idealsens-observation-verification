"""Tkinter batch GUI.

Tkinter specifically, not PySide or Electron: it is in the standard library,
so there is no installer and no executable to run. The operator's machine has
AppLocker group policy restrictions that already forced this pipeline off
ffmpeg binaries and onto PyAV, and the same constraint applies to anything
added here.

Threading model, which is the part that goes wrong if it is improvised:

* Work runs in one ``threading.Thread``. One file at a time — encoding is
  CPU-bound and holds the GIL inside PyAV, so parallelism would buy little and
  cost a lot.
* The worker never touches a widget. It puts messages on a ``queue.Queue``.
* The main thread drains that queue from ``root.after(100, ...)`` and does all
  the drawing.
* Cancel sets a ``threading.Event`` that the pipeline actually checks between
  items, rather than a flag nobody reads.
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .batch import find_videos
from .config import Settings, SupabaseConfig
from .media import MediaError, find_lrv, probe
from .observations import ObservationError
from .pipeline import (STAGES, BatchSummary, Cancelled, IngestJob, IngestResult,
                       Progress, ingest_file)
from .supabase_io import SupabaseError

POLL_MS = 100

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_CANCELLED = "cancelled"

ROW_COLOURS = {
    STATUS_DONE: "#1b5e20",
    STATUS_FAILED: "#b71c1c",
    STATUS_SKIPPED: "#616161",
    STATUS_RUNNING: "#0d47a1",
    STATUS_CANCELLED: "#e65100",
}


@dataclass
class FileEntry:
    """One row in the file list."""

    path: Path
    duration_s: float | None = None
    lrv: bool = False
    status: str = STATUS_QUEUED
    fraction: float = 0.0
    detail: str = ""

    @property
    def duration_text(self) -> str:
        if self.duration_s is None:
            return "…"
        minutes, seconds = divmod(int(self.duration_s), 60)
        return f"{minutes}:{seconds:02d}"

    @property
    def progress_text(self) -> str:
        if self.status in (STATUS_QUEUED, STATUS_SKIPPED):
            return ""
        filled = int(round(10 * self.fraction))
        return f"{'█' * filled}{'░' * (10 - filled)} {100 * self.fraction:3.0f}%"


class CurbToolGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("curbtool — ingest")
        self.root.geometry("1080x760")
        self.root.minsize(900, 620)

        self.settings = Settings.load()
        self.supabase = SupabaseConfig.from_env()
        self.entries: list[FileEntry] = []
        self.messages: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.vars: dict[str, tk.Variable] = {}

        self._build()
        self._load_settings_into_widgets()
        if self.settings.last_folder and Path(self.settings.last_folder).is_dir():
            self.add_paths([self.settings.last_folder], announce=False)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._poll_id: str | None = self.root.after(POLL_MS, self._drain)
        self.log(self.supabase.describe())

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=8)
        outer.pack(fill="both", expand=True)

        panes = ttk.PanedWindow(outer, orient="horizontal")
        panes.pack(fill="both", expand=True)

        left = ttk.Frame(panes)
        right = ttk.Frame(panes)
        panes.add(left, weight=3)
        panes.add(right, weight=2)

        self._build_file_list(left)
        self._build_settings(right)
        self._build_controls(outer)
        self._build_log(outer)

    def _build_file_list(self, parent: ttk.Frame) -> None:
        header = ttk.Frame(parent)
        header.pack(fill="x", pady=(0, 4))
        ttk.Label(header, text="Files", font=("", 10, "bold")).pack(side="left")
        ttk.Button(header, text="Add files…", command=self.choose_files).pack(side="right")
        ttk.Button(header, text="Add folder…", command=self.choose_folder).pack(side="right", padx=4)
        ttk.Button(header, text="Remove", command=self.remove_selected).pack(side="right")

        columns = ("file", "duration", "lrv", "status", "progress")
        self.tree = ttk.Treeview(parent, columns=columns, show="headings", height=14)
        for column, heading, width, anchor in (
            ("file", "File", 190, "w"),
            ("duration", "Length", 60, "e"),
            ("lrv", "LRV", 50, "center"),
            ("status", "Status", 80, "w"),
            ("progress", "Progress", 150, "w"),
        ):
            self.tree.heading(column, text=heading)
            self.tree.column(column, width=width, anchor=anchor, stretch=(column == "file"))
        for status, colour in ROW_COLOURS.items():
            self.tree.tag_configure(status, foreground=colour)

        scroll = ttk.Scrollbar(parent, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self._enable_drop(self.tree)

    def _build_settings(self, parent: ttk.Frame) -> None:
        canvas = ttk.Frame(parent)
        canvas.pack(fill="both", expand=True, padx=(8, 0))
        ttk.Label(canvas, text="Settings", font=("", 10, "bold")).pack(anchor="w", pady=(0, 4))

        box = ttk.Frame(canvas)
        box.pack(fill="both", expand=True)
        box.columnconfigure(1, weight=1)
        row = 0

        row = self._add_path_row(box, row, "observations_csv", "Observation CSV",
                                 "The campaign-wide export from the tagging app.")
        row = self._add_path_row(box, row, "gnss_csv", "Phone GNSS CSV (optional)",
                                 "Phone fixes, averaged across each stop for position.")
        row = self._add_entry_row(box, row, "campaign", "Campaign",
                                  "Part of the derived session ID — changing it re-ingests "
                                  "everything as new sessions.")
        row = self._add_entry_row(box, row, "clock_offset_s", "Clock offset (s)",
                                  "Added to every observation timestamp. Verify on one file "
                                  "before batching.")

        ttk.Separator(box, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="ew", pady=8)
        row += 1

        row = self._add_entry_row(box, row, "stop_speed_mps", "Stop speed (m/s)",
                                  "At or below this the scooter counts as stopped.")
        row = self._add_entry_row(box, row, "stop_min_duration_s", "Min stop (s)",
                                  "Shortest stationary period that counts as a stop.")
        row = self._add_entry_row(box, row, "frame_width", "Frame width (px)")
        row = self._add_entry_row(box, row, "max_frames", "Max frames per observation")
        row = self._add_entry_row(box, row, "proxy_height", "Proxy height (px)")
        row = self._add_entry_row(box, row, "proxy_bitrate_kbps", "Proxy bitrate (kbit/s)")

        self.vars["proxy_source"] = tk.StringVar()
        ttk.Label(box, text="Proxy from").grid(row=row, column=0, sticky="w", pady=2)
        ttk.Combobox(box, textvariable=self.vars["proxy_source"], state="readonly",
                     values=("hd", "lrv", "auto"), width=8).grid(
            row=row, column=1, sticky="w", pady=2)
        row += 1

        row = self._add_path_row(box, row, "work_dir", "Work folder", is_dir=True)

        self.vars["upload"] = tk.BooleanVar()
        ttk.Checkbutton(box, text="Upload to Supabase", variable=self.vars["upload"]).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(8, 2))
        row += 1

        self.vars["force"] = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="Re-process files already marked complete",
                        variable=self.vars["force"]).grid(
            row=row, column=0, columnspan=3, sticky="w", pady=2)
        row += 1

        ttk.Label(box, text="Settings are saved to ~/.curbtool.json when a run starts.",
                  foreground="#666", wraplength=320).grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def _add_entry_row(self, parent, row: int, key: str, label: str, hint: str = "") -> int:
        self.vars[key] = tk.StringVar()
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        entry = ttk.Entry(parent, textvariable=self.vars[key], width=16)
        entry.grid(row=row, column=1, sticky="w", pady=2)
        if hint:
            self._tooltip(entry, hint)
        return row + 1

    def _add_path_row(self, parent, row: int, key: str, label: str, hint: str = "",
                      is_dir: bool = False) -> int:
        self.vars[key] = tk.StringVar()
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        entry = ttk.Entry(parent, textvariable=self.vars[key])
        entry.grid(row=row, column=1, sticky="ew", pady=2)
        ttk.Button(parent, text="…", width=3,
                   command=lambda: self._pick_path(key, is_dir)).grid(
            row=row, column=2, sticky="w", padx=(4, 0))
        if hint:
            self._tooltip(entry, hint)
        return row + 1

    def _build_controls(self, parent: ttk.Frame) -> None:
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(8, 4))

        self.start_button = ttk.Button(bar, text="Start", command=self.start)
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(bar, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=4)
        ttk.Button(bar, text="Open work folder", command=self.open_work_folder).pack(side="left")

        self.status_var = tk.StringVar(value="idle")
        ttk.Label(bar, textvariable=self.status_var).pack(side="right")

        self.progress = ttk.Progressbar(parent, mode="determinate", maximum=1000)
        self.progress.pack(fill="x", pady=(0, 4))

    def _build_log(self, parent: ttk.Frame) -> None:
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=False)
        ttk.Label(frame, text="Log", font=("", 10, "bold")).pack(anchor="w")

        self.log_text = tk.Text(frame, height=11, wrap="none", state="disabled",
                                font=("TkFixedFont", 9))
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _tooltip(self, widget: tk.Widget, text: str) -> None:
        """A plain hover tip — no dependencies, and the hints are worth having."""
        tip: dict[str, tk.Toplevel | None] = {"window": None}

        def show(_event=None):
            if tip["window"] is not None:
                return
            x = widget.winfo_rootx()
            y = widget.winfo_rooty() + widget.winfo_height() + 2
            window = tk.Toplevel(widget)
            window.wm_overrideredirect(True)
            window.wm_geometry(f"+{x}+{y}")
            tk.Label(window, text=text, background="#ffffe0", relief="solid",
                     borderwidth=1, wraplength=280, justify="left",
                     padx=6, pady=3).pack()
            tip["window"] = window

        def hide(_event=None):
            if tip["window"] is not None:
                tip["window"].destroy()
                tip["window"] = None

        widget.bind("<Enter>", show)
        widget.bind("<Leave>", hide)

    def _enable_drop(self, widget: tk.Widget) -> None:
        """Enable folder drag-and-drop where tkdnd is present.

        tkdnd is not in the standard library and cannot be assumed on a machine
        with software restrictions, so this is a bonus rather than the way in:
        Add files… and Add folder… always work.
        """
        try:
            widget.drop_target_register("DND_Files")  # type: ignore[attr-defined]
            widget.dnd_bind(  # type: ignore[attr-defined]
                "<<Drop>>",
                lambda event: self.add_paths(self.root.tk.splitlist(event.data)))
        except (AttributeError, tk.TclError):
            pass

    # ------------------------------------------------------------------
    # Settings binding
    # ------------------------------------------------------------------

    def _load_settings_into_widgets(self) -> None:
        for key, var in self.vars.items():
            if key == "force":
                continue
            value = getattr(self.settings, key, None)
            if isinstance(var, tk.BooleanVar):
                var.set(bool(value))
            else:
                var.set("" if value is None else str(value))

    def _settings_from_widgets(self) -> Settings:
        """Read the panel back into a Settings, keeping the old value on a typo."""
        current = self.settings
        values: dict[str, Any] = {}
        for key, var in self.vars.items():
            if key == "force":
                continue
            existing = getattr(current, key, None)
            raw = var.get()
            if isinstance(existing, bool):
                values[key] = bool(raw)
            elif isinstance(existing, int):
                values[key] = _as_int(raw, existing)
            elif isinstance(existing, float):
                values[key] = _as_float(raw, existing)
            else:
                values[key] = str(raw).strip()
        return current.merged(**values)

    def _pick_path(self, key: str, is_dir: bool) -> None:
        current = self.vars[key].get()
        initial = str(Path(current).parent) if current else ""
        if is_dir:
            chosen = filedialog.askdirectory(title="Choose a folder", initialdir=initial or None)
        else:
            chosen = filedialog.askopenfilename(
                title="Choose a CSV", initialdir=initial or None,
                filetypes=[("CSV", "*.csv"), ("All files", "*.*")])
        if chosen:
            self.vars[key].set(chosen)

    # ------------------------------------------------------------------
    # File list
    # ------------------------------------------------------------------

    def choose_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Choose GoPro files",
            initialdir=self.settings.last_folder or None,
            filetypes=[("GoPro video", "*.MP4 *.mp4"), ("All files", "*.*")])
        if paths:
            self.add_paths(paths)

    def choose_folder(self) -> None:
        folder = filedialog.askdirectory(
            title="Choose a folder of GoPro files",
            initialdir=self.settings.last_folder or None)
        if folder:
            self.add_paths([folder])

    def add_paths(self, paths, announce: bool = True) -> None:
        existing = {entry.path for entry in self.entries}
        added: list[FileEntry] = []
        for raw in paths:
            for video in find_videos(raw):
                if video in existing:
                    continue
                entry = FileEntry(path=video)
                self.entries.append(entry)
                existing.add(video)
                added.append(entry)
            folder = Path(raw)
            self.settings = self.settings.merged(
                last_folder=str(folder if folder.is_dir() else folder.parent))

        self._refresh_rows()
        if added:
            if announce:
                self.log(f"added {len(added)} file(s)")
            # Probing touches every file; keep it off the UI thread.
            threading.Thread(target=self._probe_files, args=(added,), daemon=True).start()

    def _probe_files(self, entries: list[FileEntry]) -> None:
        for entry in entries:
            # Reading a length is a convenience. Whatever goes wrong — an
            # unreadable file, a missing codec — it must not kill the thread
            # and leave every row stuck showing no length at all.
            try:
                duration = probe(entry.path).duration_s
            except Exception:
                duration = 0.0
            try:
                has_lrv = find_lrv(entry.path) is not None
            except OSError:
                has_lrv = False
            self.messages.put(("probed", (entry.path, duration, has_lrv)))

    def remove_selected(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        selected = {self.tree.item(item, "values")[0] for item in self.tree.selection()}
        self.entries = [e for e in self.entries if e.path.name not in selected]
        self._refresh_rows()

    def _refresh_rows(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for entry in self.entries:
            self.tree.insert("", "end", iid=str(entry.path), tags=(entry.status,), values=(
                entry.path.name,
                entry.duration_text,
                "yes" if entry.lrv else "—",
                entry.status,
                entry.progress_text,
            ))

    def _update_row(self, entry: FileEntry) -> None:
        iid = str(entry.path)
        if not self.tree.exists(iid):
            return
        self.tree.item(iid, tags=(entry.status,), values=(
            entry.path.name,
            entry.duration_text,
            "yes" if entry.lrv else "—",
            entry.status,
            entry.progress_text,
        ))

    def _entry_for(self, filename: str) -> FileEntry | None:
        return next((e for e in self.entries if e.path.name == filename), None)

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        if not self.entries:
            messagebox.showinfo("Nothing to do", "Add some GoPro files first.")
            return

        settings = self._settings_from_widgets()
        if not settings.campaign:
            messagebox.showerror(
                "Campaign required",
                "Set a campaign name. It is part of the derived session ID, so "
                "changing it later re-ingests everything as new sessions.")
            return
        if settings.upload and not self.supabase.configured:
            messagebox.showerror(
                "Supabase not configured",
                "Set SUPABASE_URL and SUPABASE_SERVICE_KEY in .env, or turn off "
                "\"Upload to Supabase\".")
            return

        self.settings = settings
        try:
            self.settings.save()
        except OSError as exc:
            self.log(f"could not save settings: {exc}")

        for entry in self.entries:
            entry.status = STATUS_QUEUED
            entry.fraction = 0.0
        self._refresh_rows()

        self.cancel_event.clear()
        self.start_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.status_var.set("running")

        force = bool(self.vars["force"].get())
        self.worker = threading.Thread(target=self._run_batch, args=(settings, force),
                                       daemon=True)
        self.worker.start()

    def cancel(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            self.cancel_event.set()
            self.cancel_button.configure(state="disabled")
            self.status_var.set("cancelling after the current step…")
            self.messages.put(("log", "cancel requested"))

    # -- worker thread; nothing below here may touch a widget ------------

    def _run_batch(self, settings: Settings, force: bool) -> None:
        from .batch import load_inputs, make_client

        put = self.messages.put
        summary = BatchSummary(campaign=settings.campaign)
        try:
            observations, phone_fixes = load_inputs(settings)
            client = make_client(self.supabase, settings)
            summary.csv_rows = len(observations)
            put(("log", f"{len(self.entries)} file(s), {len(observations)} observation(s)"
                        + (f", {len(phone_fixes)} phone fixes" if phone_fixes else "")))
        except (ObservationError, SupabaseError, OSError) as exc:
            put(("log", f"cannot start: {exc}"))
            put(("done", summary))
            return

        for index, entry in enumerate(list(self.entries), start=1):
            if self.cancel_event.is_set():
                put(("log", "cancelled — stopping before the next file"))
                break

            put(("status", (entry.path.name, STATUS_RUNNING, 0.0)))
            put(("log", f"[{index}/{len(self.entries)}] {entry.path.name}"))
            job = IngestJob(
                video=entry.path, settings=settings,
                observations=observations, phone_fixes=phone_fixes, client=client,
                frame_bucket=self.supabase.frame_bucket,
                proxy_bucket=self.supabase.proxy_bucket,
                force=force,
            )
            try:
                result = ingest_file(job, on_progress=lambda p: put(("progress", p)),
                                     should_cancel=self.cancel_event.is_set)
            except Cancelled as exc:
                put(("status", (entry.path.name, STATUS_CANCELLED, 0.0)))
                put(("log", f"    cancelled: {exc}"))
                summary.add(IngestResult(file=entry.path.name, session_id="",
                                         status="cancelled", error=str(exc)))
                break
            except Exception as exc:  # one bad file must not take the batch down
                put(("status", (entry.path.name, STATUS_FAILED, 0.0)))
                put(("log", f"    FAILED: {exc}"))
                put(("log", "    " + traceback.format_exc().strip().splitlines()[-1]))
                summary.add(IngestResult(file=entry.path.name, session_id="",
                                         status="failed", error=str(exc)))
                continue

            summary.add(result)
            put(("status", (entry.path.name, result.status, 1.0)))
            if result.status == "skipped":
                put(("log", "    already ingested — skipped"))
            else:
                put(("log", f"    {result.matched} observations, {result.frames} frames, "
                            f"{result.elapsed_s:.0f}s"))
            if result.hint:
                put(("log", f"    hint: {result.hint}"))

        put(("done", summary))

    # ------------------------------------------------------------------
    # Main thread: drain the queue and draw
    # ------------------------------------------------------------------

    def _drain(self) -> None:
        if not self._window_alive():
            # The window has gone. Stop rescheduling rather than filling the
            # console with errors from callbacks against dead widgets.
            self._poll_id = None
            return
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "progress":
                    self._on_progress(payload)
                elif kind == "status":
                    name, status, fraction = payload
                    entry = self._entry_for(name)
                    if entry is not None:
                        entry.status = status
                        entry.fraction = fraction
                        self._update_row(entry)
                elif kind == "probed":
                    path, duration, has_lrv = payload
                    entry = next((e for e in self.entries if e.path == path), None)
                    if entry is not None:
                        entry.duration_s = duration
                        entry.lrv = has_lrv
                        self._update_row(entry)
                elif kind == "done":
                    self._on_batch_done(payload)
        except queue.Empty:
            pass
        except tk.TclError:
            self._poll_id = None
            return
        self._poll_id = self.root.after(POLL_MS, self._drain)

    def _window_alive(self) -> bool:
        try:
            return bool(self.root.winfo_exists())
        except tk.TclError:
            return False

    def shutdown(self) -> None:
        """Stop the poll loop and let the worker finish. Safe to call twice."""
        self.cancel_event.set()
        if self._poll_id is not None:
            try:
                self.root.after_cancel(self._poll_id)
            except tk.TclError:
                pass
            self._poll_id = None

    def _on_progress(self, progress: Progress) -> None:
        entry = self._entry_for(progress.file)
        if entry is not None:
            entry.fraction = _overall_fraction(progress)
            entry.status = STATUS_RUNNING
            self._update_row(entry)
        self.progress["value"] = 1000 * progress.fraction
        self.status_var.set(f"{progress.file} — {progress.stage}: {progress.message}")

    def _on_batch_done(self, summary: BatchSummary) -> None:
        self.worker = None
        self.start_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self.progress["value"] = 0
        self.status_var.set("idle")

        if not summary.results:
            return
        self.log("")
        for line in summary.render().splitlines():
            self.log(line)
        try:
            path = summary.save(
                Path(self.settings.work_dir) / (self.settings.campaign or "campaign")
                / "summary.json")
            self.log(f"summary written to {path}")
        except OSError as exc:
            self.log(f"could not write summary: {exc}")

        missing = summary.csv_rows - summary.total_matched
        if missing > 0:
            messagebox.showwarning(
                "Observations unaccounted for",
                f"{summary.total_matched} of {summary.csv_rows} observations in the CSV "
                f"matched a video.\n\n{missing} matched nothing. They fell into a chapter "
                "gap or a GPS dropout, or the clock offset is wrong.")

    # ------------------------------------------------------------------

    def log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{stamp}  {message}\n" if message else "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def open_work_folder(self) -> None:
        folder = Path(self._settings_from_widgets().work_dir).resolve()
        folder.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform.startswith("win"):
                import os
                os.startfile(folder)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except OSError as exc:
            messagebox.showinfo("Work folder", f"{folder}\n\n(could not open it: {exc})")

    def on_close(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            if not messagebox.askokcancel(
                    "Quit", "A run is still going. Cancel it and quit?"):
                return
            self.cancel_event.set()
        try:
            self._settings_from_widgets().save()
        except OSError:
            pass
        self.shutdown()
        self.root.destroy()


# --------------------------------------------------------------------------

def _overall_fraction(progress: Progress) -> float:
    """Weight the stages so the per-file bar advances roughly with real time.

    Frames and the proxy transcode dominate; the rest is close to instant.
    """
    weights = {"track": 0.03, "stops": 0.02, "match": 0.02,
               "frames": 0.30, "proxy": 0.45, "upload": 0.15, "rows": 0.03}
    if progress.stage not in weights:
        return progress.fraction
    # STAGES is the pipeline's own running order — never a second copy of it.
    before = sum(weights[s] for s in STAGES[:STAGES.index(progress.stage)])
    # Rounded so accumulated float noise cannot make the bar step backwards.
    return round(min(1.0, before + weights[progress.stage] * progress.fraction), 6)


def _as_int(raw: Any, fallback: int) -> int:
    try:
        return int(float(str(raw).replace(",", ".")))
    except (TypeError, ValueError):
        return fallback


def _as_float(raw: Any, fallback: float) -> float:
    try:
        return float(str(raw).replace(",", "."))
    except (TypeError, ValueError):
        return fallback


def main() -> int:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"cannot open a window: {exc}", file=sys.stderr)
        return 1
    CurbToolGUI(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
