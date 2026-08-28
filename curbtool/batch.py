"""Running a list of files, shared by the CLI and the GUI.

Kept out of both so that "one failure must not abort the batch" is implemented
once. A file that fails is marked failed and the run carries on; only a cancel
stops everything.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .config import Settings, SupabaseConfig
from .observations import (Observation, ObservationError, PhoneFix,
                           load_observations, load_phone_track)
from .pipeline import (BatchSummary, Cancelled, IngestJob, IngestResult, PipelineError,
                       Progress, ingest_file)
from .supabase_io import SupabaseClient, SupabaseError

VIDEO_SUFFIXES = (".mp4", ".MP4", ".mov", ".MOV")


def find_videos(target: str | Path) -> list[Path]:
    """Videos in a folder, or the single file given. .LRV companions excluded."""
    target = Path(target)
    if target.is_file():
        return [target]
    if not target.is_dir():
        return []
    videos = [p for p in sorted(target.iterdir())
              if p.is_file() and p.suffix.lower() == ".mp4"]
    return videos


def load_inputs(settings: Settings) -> tuple[list[Observation], list[PhoneFix]]:
    """Read the campaign CSVs once for the whole batch."""
    observations: list[Observation] = []
    if settings.observations_csv:
        observations = load_observations(settings.observations_csv)
    phone_fixes: list[PhoneFix] = []
    if settings.gnss_csv:
        phone_fixes = load_phone_track(settings.gnss_csv)
    return observations, phone_fixes


def make_client(supabase: SupabaseConfig, settings: Settings) -> SupabaseClient | None:
    """A client, or None when uploads are off or credentials are missing."""
    if not settings.upload:
        return None
    if not supabase.configured:
        raise SupabaseError(
            "uploads are enabled but Supabase is not configured. Set SUPABASE_URL and "
            "SUPABASE_SERVICE_KEY in .env, or turn uploads off.")
    return SupabaseClient(supabase.url, supabase.service_key,
                          state_dir=Path(settings.work_dir) / "uploads")


def run_batch(videos: Sequence[Path], settings: Settings, supabase: SupabaseConfig,
              force: bool = False, reuse_media: bool = False,
              on_progress: Callable[[Progress], None] = lambda p: None,
              on_file_done: Callable[[IngestResult], None] = lambda r: None,
              on_log: Callable[[str], None] = lambda m: None,
              should_cancel: Callable[[], bool] = lambda: False) -> BatchSummary:
    """Process every file, surviving individual failures.

    Files are processed one at a time. Encoding is CPU-bound and holds the GIL
    inside PyAV, so parallelism here would buy little and cost a lot.
    """
    observations, phone_fixes = load_inputs(settings)
    client = make_client(supabase, settings)

    summary = BatchSummary(campaign=settings.campaign, csv_rows=len(observations))
    on_log(f"{len(videos)} file(s), {len(observations)} observation(s) in the CSV"
           + (f", {len(phone_fixes)} phone fixes" if phone_fixes else ""))

    for index, video in enumerate(videos, start=1):
        if should_cancel():
            on_log("cancelled — stopping before the next file")
            break

        on_log(f"[{index}/{len(videos)}] {video.name}")
        job = IngestJob(
            video=video,
            settings=settings,
            observations=observations,
            phone_fixes=phone_fixes,
            client=client,
            frame_bucket=supabase.frame_bucket,
            proxy_bucket=supabase.proxy_bucket,
            force=force,
            reuse_media=reuse_media,
        )
        try:
            result = ingest_file(job, on_progress=on_progress, should_cancel=should_cancel)
        except Cancelled as exc:
            on_log(f"    cancelled: {exc}")
            summary.add(IngestResult(file=video.name, session_id="", status="cancelled",
                                     error=str(exc)))
            on_file_done(summary.results[-1])
            break
        except (PipelineError, SupabaseError, ObservationError, OSError) as exc:
            # One bad file must not take the other sixteen with it.
            on_log(f"    FAILED: {exc}")
            result = IngestResult(file=video.name, session_id="", status="failed",
                                  error=str(exc))

        summary.add(result)
        on_file_done(result)
        if result.status == "skipped":
            on_log("    already ingested — skipped")
        elif result.status == "done":
            on_log(f"    {result.matched} observations, {result.frames} frames, "
                   f"{result.elapsed_s:.0f}s")
            if result.hint:
                on_log(f"    hint: {result.hint}")

    return summary
