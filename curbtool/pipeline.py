"""The ingest pipeline: one video file in, rows and media in Supabase out.

Extracted from the original monolithic CLI so the command line and the GUI
drive exactly the same code. Everything long-running reports through
*on_progress* and checks *should_cancel* between items — a stage that can take
twenty minutes must never look like a hang, and Cancel must actually stop
rather than set a flag nobody reads.

Session and observation IDs are derived, not generated. Re-running a file
therefore updates the rows it wrote last time instead of duplicating them, and
reviews stay attached to their observations across a re-ingest. That is what
makes a partial batch safe to retry.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import gpmf, media
from .config import Settings
from .observations import Observation, PhoneFix, average_fixes, nearest_fix, suggest_clock_offset
from .supabase_io import Cancelled as UploadCancelled
from .supabase_io import SupabaseClient, SupabaseError

UTC = timezone.utc

STAGES = ("track", "match", "stops", "frames", "proxy", "upload", "rows")


class PipelineError(Exception):
    """A failure that stops this file but not the batch."""


class Cancelled(Exception):
    """Raised when the operator cancels."""


# --------------------------------------------------------------------------
# Progress
# --------------------------------------------------------------------------

@dataclass
class Progress:
    file: str
    stage: str
    current: int
    total: int
    message: str = ""

    @property
    def fraction(self) -> float:
        return min(1.0, self.current / self.total) if self.total else 0.0


ProgressFn = Callable[[Progress], None]
CancelFn = Callable[[], bool]


def _noop(_: Progress) -> None:
    pass


def _never() -> bool:
    return False


# --------------------------------------------------------------------------
# Job and result
# --------------------------------------------------------------------------

@dataclass
class IngestJob:
    """One video file and everything needed to process it.

    Observations and phone fixes are passed in already parsed: the campaign CSV
    is the same for all 17 chapters, and re-reading it per file would be waste.
    """

    video: Path
    settings: Settings
    observations: Sequence[Observation] = ()
    phone_fixes: Sequence[PhoneFix] = ()
    client: SupabaseClient | None = None
    frame_bucket: str = "frames"
    proxy_bucket: str = "proxies"
    force: bool = False

    @property
    def campaign(self) -> str:
        return self.settings.campaign or "campaign"


@dataclass
class IngestResult:
    """What happened to one file. Rendered into the batch summary."""

    file: str
    session_id: str
    status: str = "pending"          # done | skipped | failed | cancelled
    error: str = ""

    duration_s: float = 0.0
    started_utc: datetime | None = None
    ended_utc: datetime | None = None
    device: str | None = None

    matched: int = 0
    out_of_range: int = 0
    snapped: int = 0
    stops: int = 0
    frames: int = 0
    proxy_bytes: int = 0
    proxy_source: str = ""
    lrv_found: bool = False
    uploaded: bool = False
    elapsed_s: float = 0.0
    hint: str = ""

    @property
    def snap_ratio(self) -> float:
        return self.snapped / self.matched if self.matched else 0.0

    def as_row(self) -> dict:
        data = asdict(self)
        for key in ("started_utc", "ended_utc"):
            if data[key] is not None:
                data[key] = data[key].isoformat()
        data["snap_ratio"] = round(self.snap_ratio, 4)
        return data


# --------------------------------------------------------------------------
# Deterministic identity
# --------------------------------------------------------------------------

def session_id_for(campaign: str, filename: str, size: int) -> uuid.UUID:
    """A session ID that depends only on which file this is.

    Re-running a file without one used to mint a fresh UUID and duplicate
    everything — unacceptable in a batch GUI where a partial run gets retried.
    """
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{campaign}/{filename}/{size}")


def observation_id_for(session_id: uuid.UUID, external_id: str) -> uuid.UUID:
    """Derived from the session, so a re-ingest keeps reviews attached."""
    return uuid.uuid5(session_id, external_id)


def frame_id_for(observation_id: uuid.UUID, seq: int) -> uuid.UUID:
    return uuid.uuid5(observation_id, f"frame/{seq}")


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

@dataclass
class Match:
    """An observation placed on one file's timeline."""

    observation: Observation
    observation_id: uuid.UUID
    offset_s: float
    window_start_s: float
    window_end_s: float
    window_mid_s: float
    stop: gpmf.Stop | None = None

    gopro_lat: float | None = None
    gopro_lon: float | None = None
    phone_lat: float | None = None
    phone_lon: float | None = None
    phone_fix_count: int = 0
    gps_disagreement_m: float | None = None
    gps_dop: float | None = None
    gps_fix: int | None = None
    frames: list[tuple[int, float, float, Path]] = field(default_factory=list)

    @property
    def snapped(self) -> bool:
        return self.stop is not None

    @property
    def position(self) -> tuple[float | None, float | None, str | None]:
        """Best available position, and where it came from."""
        if self.phone_lat is not None:
            return self.phone_lat, self.phone_lon, "phone"
        if self.gopro_lat is not None:
            return self.gopro_lat, self.gopro_lon, "gopro"
        obs = self.observation
        if obs.lat is not None and obs.lon is not None:
            return obs.lat, obs.lon, "tag"
        return None, None, None


def match_observations(observations: Sequence[Observation], samples: Sequence[gpmf.GpsSample],
                       stops: Sequence[gpmf.Stop], session_id: uuid.UUID,
                       settings: Settings,
                       phone_fixes: Sequence[PhoneFix] = ()) -> tuple[list[Match], int]:
    """Place the campaign's observations onto this file's timeline.

    Returns the matches and the number of observations that fell outside this
    file's window. Each file is handed the whole campaign CSV and keeps only
    what belongs to it, so "out of range" here is almost always just "belongs
    to another chapter" — it is the batch total that matters, not this one.
    """
    offset = timedelta(seconds=settings.clock_offset_s)
    matches: list[Match] = []
    out_of_range = 0

    for observation in observations:
        adjusted = observation.utc + offset
        video_offset = gpmf.utc_to_offset(samples, adjusted)
        if video_offset is None:
            out_of_range += 1
            continue

        stop = gpmf.stop_for_offset(stops, video_offset, settings.stop_tolerance_s)
        if stop is not None:
            start_s, end_s, mid_s = stop.start_s, stop.end_s, stop.mid_s
        else:
            # No stop found: fall back to a fixed window around the tag.
            half = settings.fallback_window_s
            start_s, end_s, mid_s = video_offset - half, video_offset + half, video_offset
        start_s = max(0.0, start_s)

        match = Match(
            observation=observation,
            observation_id=observation_id_for(session_id, observation.external_id),
            offset_s=video_offset,
            window_start_s=start_s,
            window_end_s=end_s,
            window_mid_s=mid_s,
            stop=stop,
        )

        gopro = gpmf.offset_to_latlon(samples, mid_s)
        if gopro is not None:
            match.gopro_lat, match.gopro_lon = gopro
        nearby = [s for s in samples if start_s <= s.offset_s <= end_s]
        if nearby:
            match.gps_dop = min((s.dop for s in nearby if not math.isnan(s.dop)), default=None)
            match.gps_fix = max(s.fix for s in nearby)

        _attach_phone_position(match, phone_fixes, adjusted, samples, offset)
        matches.append(match)

    matches.sort(key=lambda m: m.offset_s)
    return matches, out_of_range


def _attach_phone_position(match: Match, phone_fixes: Sequence[PhoneFix],
                           adjusted_utc: datetime, samples: Sequence[gpmf.GpsSample],
                           clock_offset: timedelta) -> None:
    """Average the phone's fixes across the stop and record the disagreement.

    The phone is the better receiver, so its position wins where it exists. The
    distance between the two is kept as gps_disagreement_m: a large value means
    one of them had poor reception, and the reviewer should not trust the pin.
    """
    if not phone_fixes:
        return

    # The phone log is in true UTC; the window is in camera time, so convert
    # the window back through the same clock offset that got us here.
    start_utc = _offset_to_utc(samples, match.window_start_s)
    end_utc = _offset_to_utc(samples, match.window_end_s)
    averaged = None
    if start_utc and end_utc:
        averaged = average_fixes(phone_fixes, start_utc - clock_offset, end_utc - clock_offset)

    if averaged is not None:
        match.phone_lat, match.phone_lon, match.phone_fix_count = averaged
    else:
        single = nearest_fix(phone_fixes, adjusted_utc - clock_offset)
        if single is not None:
            match.phone_lat, match.phone_lon, match.phone_fix_count = single.lat, single.lon, 1

    if match.phone_lat is not None and match.gopro_lat is not None:
        match.gps_disagreement_m = gpmf.haversine_m(
            match.phone_lat, match.phone_lon, match.gopro_lat, match.gopro_lon)


def _offset_to_utc(samples: Sequence[gpmf.GpsSample], offset_s: float) -> datetime | None:
    """Inverse of utc_to_offset, for turning a stop window back into a clock window."""
    usable = [s for s in samples if s.utc]
    if not usable:
        return None
    nearest = min(usable, key=lambda s: abs(s.offset_s - offset_s))
    return nearest.utc + timedelta(seconds=offset_s - nearest.offset_s)


# --------------------------------------------------------------------------
# The pipeline
# --------------------------------------------------------------------------

def ingest_file(job: IngestJob, on_progress: ProgressFn = _noop,
                should_cancel: CancelFn = _never) -> IngestResult:
    """Process one GoPro file end to end."""
    started = time.monotonic()
    video = Path(job.video)
    settings = job.settings
    name = video.name

    def report(stage: str, current: int, total: int, message: str = "") -> None:
        on_progress(Progress(file=name, stage=stage, current=current,
                             total=total, message=message))

    def check() -> None:
        if should_cancel():
            raise Cancelled(f"{name}: cancelled")

    if not video.exists():
        raise PipelineError(f"{name}: file does not exist")

    size = video.stat().st_size
    session_uuid = session_id_for(job.campaign, name, size)
    result = IngestResult(file=name, session_id=str(session_uuid))

    # -- resume check ----------------------------------------------------
    if not job.force and job.client is not None:
        existing = _completed_session(job.client, session_uuid)
        if existing is not None:
            result.status = "skipped"
            result.matched = existing.get("observation_count") or 0
            result.frames = existing.get("frame_count") or 0
            result.proxy_bytes = existing.get("proxy_bytes") or 0
            result.elapsed_s = time.monotonic() - started
            report("rows", 1, 1, "already ingested — skipped (use --force to redo)")
            return result

    work_dir = Path(settings.work_dir) / job.campaign / video.stem
    frame_dir = work_dir / "frames"
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        # -- 1. telemetry ------------------------------------------------
        check()
        report("track", 0, 1, "reading GoPro telemetry")
        try:
            telemetry = gpmf.parse_telemetry(video)
        except gpmf.GpmfError as exc:
            raise PipelineError(str(exc)) from exc
        samples = telemetry.samples
        result.device = telemetry.device
        result.started_utc = telemetry.first_utc
        result.ended_utc = telemetry.last_utc
        info = media.probe(video)
        result.duration_s = info.duration_s or telemetry.duration_s
        report("track", 1, 1,
               f"{len(samples)} fixes over {result.duration_s:.0f}s"
               + (f", {telemetry.dropped_samples} dropped below fix quality 2"
                  if telemetry.dropped_samples else ""))

        # -- 2. stops ----------------------------------------------------
        check()
        report("stops", 0, 1, "detecting stops")
        stops = gpmf.detect_stops(samples, speed_threshold=settings.stop_speed_mps,
                                  min_duration_s=settings.stop_min_duration_s)
        result.stops = len(stops)
        report("stops", 1, 1, f"{len(stops)} stops")

        # -- 3. match ----------------------------------------------------
        check()
        report("match", 0, 1, "matching observations")
        matches, out_of_range = match_observations(
            job.observations, samples, stops, session_uuid, settings, job.phone_fixes)
        result.matched = len(matches)
        result.out_of_range = out_of_range
        result.snapped = sum(1 for m in matches if m.snapped)
        report("match", 1, 1,
               f"{len(matches)} matched, {out_of_range} outside this file, "
               f"{result.snapped} snapped to a stop")

        if not matches and job.observations and result.started_utc and result.ended_utc:
            # Nothing matched at all. The most likely cause by far is the clock
            # offset, so check whether a whole-hour shift would have worked.
            guess = suggest_clock_offset(job.observations, result.started_utc,
                                         result.ended_utc)
            if guess:
                result.hint = (f"nothing matched, but --clock-offset {guess:+.0f} "
                               f"({guess / 3600:+.0f} h) would put tags inside this file")

        # -- 4. frames ---------------------------------------------------
        result.frames = _extract_all_frames(job, matches, frame_dir, report, check)

        # -- 5. proxy ----------------------------------------------------
        proxy_path = _build_proxy(job, work_dir, report, check, result)

        # -- 6 & 7. upload and rows -------------------------------------
        if settings.upload and job.client is not None:
            _upload_and_write(job, session_uuid, matches, samples, stops, proxy_path,
                              result, report, check)
            result.uploaded = True
        else:
            report("upload", 1, 1, "upload disabled — media left in the work folder")

        result.status = "done"

    except Cancelled:
        result.status = "cancelled"
        raise
    except UploadCancelled as exc:
        result.status = "cancelled"
        raise Cancelled(str(exc)) from exc
    finally:
        result.elapsed_s = time.monotonic() - started
        _write_local_summary(work_dir, result)

    return result


def _completed_session(client: SupabaseClient, session_uuid: uuid.UUID) -> dict | None:
    """Return the session row if this file has already been ingested in full."""
    try:
        rows = client.select("sessions", {
            "id": f"eq.{session_uuid}",
            "select": "id,ingest_status,observation_count,frame_count,proxy_bytes",
        })
    except SupabaseError:
        # A resume check is a convenience; never let it stop a run.
        return None
    if not rows or rows[0].get("ingest_status") != "complete":
        return None
    return rows[0]


def _extract_all_frames(job: IngestJob, matches: Sequence[Match], frame_dir: Path,
                        report, check) -> int:
    """Cut evidence frames for every match, reporting per frame across the file."""
    settings = job.settings
    plans: list[tuple[Match, list[float]]] = []
    for match in matches:
        targets = media.frame_times(match.window_start_s, match.window_end_s,
                                    settings.frame_interval_s, settings.max_frames)
        plans.append((match, targets))
    total = sum(len(t) for _, t in plans)
    if total == 0:
        report("frames", 1, 1, "no observations in this file")
        return 0

    report("frames", 0, total, f"extracting {total} frames from {len(plans)} observations")
    done = 0
    for match, targets in plans:
        check()
        out_dir = frame_dir / str(match.observation_id)
        written = media.extract_frames(
            job.video, targets, out_dir, prefix="f",
            width=settings.frame_width, quality=settings.frame_quality,
            should_cancel=lambda: _cancelled(check))
        for seq, (actual_s, path) in enumerate(written):
            match.frames.append((seq, actual_s - match.window_mid_s, actual_s, path))
        done += len(written)
        report("frames", done, total,
               f"{match.observation.external_id}: {len(written)} frames")
    return done


def _build_proxy(job: IngestJob, work_dir: Path, report, check,
                 result: IngestResult) -> Path | None:
    """Build the playback proxy, from the .LRV where that is allowed and present."""
    settings = job.settings
    out_path = work_dir / f"{Path(job.video).stem}_proxy.mp4"
    lrv = media.find_lrv(job.video)
    result.lrv_found = lrv is not None

    if out_path.exists():
        result.proxy_bytes = out_path.stat().st_size
        result.proxy_source = "cached"
        report("proxy", 1, 1, f"proxy already built ({_human_bytes(result.proxy_bytes)})")
        return out_path

    check()
    use_lrv = lrv is not None and settings.proxy_source in ("lrv", "auto")
    try:
        if use_lrv:
            report("proxy", 0, 1, f"remuxing {lrv.name} (stream copy)")
            media.remux_proxy(lrv, out_path)
            result.proxy_source = "lrv"
        else:
            if settings.proxy_source == "lrv":
                report("proxy", 0, 1, "no .LRV found — transcoding from HD instead")
            media.build_proxy(
                job.video, out_path,
                height=settings.proxy_height, bitrate_kbps=settings.proxy_bitrate_kbps,
                on_progress=lambda pos, dur: report(
                    "proxy", int(pos), int(dur) or 1,
                    f"transcoding to {settings.proxy_height}p"),
                should_cancel=lambda: _cancelled(check))
            result.proxy_source = "hd"
    except media.Cancelled as exc:
        raise Cancelled(str(exc)) from exc
    except media.MediaError as exc:
        raise PipelineError(str(exc)) from exc

    result.proxy_bytes = out_path.stat().st_size
    report("proxy", 1, 1, f"proxy built ({_human_bytes(result.proxy_bytes)})")
    return out_path


def _cancelled(check) -> bool:
    """Adapt the raising `check` to the boolean callback media wants."""
    try:
        check()
    except Cancelled:
        return True
    return False


def _upload_and_write(job: IngestJob, session_uuid: uuid.UUID, matches: Sequence[Match],
                      samples: Sequence[gpmf.GpsSample], stops: Sequence[gpmf.Stop],
                      proxy_path: Path | None, result: IngestResult, report, check) -> None:
    """Push media to storage and rows to the database.

    Order matters: the session row goes in first so children have a parent, and
    it is only marked 'complete' at the very end. A run that dies midway leaves
    the session 'pending', which is what makes the resume check meaningful.
    """
    client = job.client
    assert client is not None
    settings = job.settings

    client.ensure_bucket(job.frame_bucket)
    client.ensure_bucket(job.proxy_bucket)

    session_row = _session_row(job, session_uuid, result, status="pending")
    client.upsert("sessions", [session_row])

    # -- frames ----------------------------------------------------------
    frame_rows: list[dict] = []
    total_frames = sum(len(m.frames) for m in matches)
    sent = 0
    if total_frames:
        report("upload", 0, total_frames, f"uploading {total_frames} frames")
    for match in matches:
        check()
        for seq, delta_s, offset_s, path in match.frames:
            storage_path = (f"{job.campaign}/{session_uuid}/"
                            f"{match.observation_id}/{seq:02d}.jpg")
            client.upload(job.frame_bucket, storage_path, path, "image/jpeg")
            width, height = _image_size(path)
            frame_rows.append({
                "id": str(frame_id_for(match.observation_id, seq)),
                "observation_id": str(match.observation_id),
                "session_id": str(session_uuid),
                "seq": seq,
                "delta_s": round(delta_s, 3),
                "offset_s": round(offset_s, 3),
                "storage_path": storage_path,
                "public_url": client.public_url(job.frame_bucket, storage_path),
                "width": width,
                "height": height,
                "bytes": path.stat().st_size,
            })
            sent += 1
            if sent % 5 == 0 or sent == total_frames:
                report("upload", sent, total_frames, f"frames {sent}/{total_frames}")

    # -- proxy -----------------------------------------------------------
    proxy_storage_path = None
    if proxy_path is not None and proxy_path.exists():
        check()
        proxy_storage_path = f"{job.campaign}/{session_uuid}/{proxy_path.name}"
        total_bytes = proxy_path.stat().st_size
        report("upload", 0, total_bytes,
               f"uploading proxy ({_human_bytes(total_bytes)}, resumable)")
        upload = client.upload_resumable(
            job.proxy_bucket, proxy_storage_path, proxy_path,
            on_progress=lambda done, total: report(
                "upload", done, total,
                f"proxy {_human_bytes(done)} / {_human_bytes(total)}"),
            should_cancel=lambda: _cancelled(check))
        if upload.skipped:
            report("upload", total_bytes, total_bytes, "proxy already uploaded")

    # -- rows ------------------------------------------------------------
    check()
    report("rows", 0, 3, "writing observations")
    observation_rows = [_observation_row(job, session_uuid, m) for m in matches]
    client.upsert("observations", observation_rows)

    report("rows", 1, 3, "writing frames")
    client.upsert("frames", frame_rows, returning="minimal")

    report("rows", 2, 3, "writing track")
    track_rows = _track_rows(session_uuid, samples)
    # Replace rather than merge: a re-ingest with different settings can
    # produce a different number of points, and stale ones would linger.
    client.delete("track_points", {"session_id": f"eq.{session_uuid}"})
    client.upsert("track_points", track_rows, on_conflict="session_id,seq",
                  returning="minimal")

    result.frames = len(frame_rows)
    final_row = _session_row(job, session_uuid, result, status="complete",
                             proxy_storage_path=proxy_storage_path)
    client.upsert("sessions", [final_row])
    report("rows", 3, 3, "done")


def _session_row(job: IngestJob, session_uuid: uuid.UUID, result: IngestResult,
                 status: str, proxy_storage_path: str | None = None) -> dict:
    client = job.client
    return {
        "id": str(session_uuid),
        "campaign": job.campaign,
        "filename": result.file,
        "file_size": Path(job.video).stat().st_size,
        "device": result.device,
        "duration_s": round(result.duration_s, 3),
        "started_utc": result.started_utc.isoformat() if result.started_utc else None,
        "ended_utc": result.ended_utc.isoformat() if result.ended_utc else None,
        "clock_offset_s": job.settings.clock_offset_s,
        "proxy_path": proxy_storage_path,
        "proxy_url": (client.public_url(job.proxy_bucket, proxy_storage_path)
                      if proxy_storage_path and client else None),
        "proxy_bytes": result.proxy_bytes or None,
        "proxy_source": result.proxy_source or None,
        "observation_count": result.matched,
        "frame_count": result.frames,
        "stop_count": result.stops,
        "snapped_count": result.snapped,
        "ingest_status": status,
        "ingested_at": datetime.now(UTC).isoformat() if status == "complete" else None,
    }


def _observation_row(job: IngestJob, session_uuid: uuid.UUID, match: Match) -> dict:
    obs = match.observation
    lat, lon, source = match.position
    return {
        "id": str(match.observation_id),
        "session_id": str(session_uuid),
        "campaign": job.campaign,
        "external_id": obs.external_id,
        "source": "ingest",
        "observed_utc": obs.utc.isoformat(),
        "video_offset_s": round(match.offset_s, 3),
        "stop_index": match.stop.index if match.stop else None,
        "stop_start_s": round(match.window_start_s, 3),
        "stop_end_s": round(match.window_end_s, 3),
        "snapped": match.snapped,
        "category": obs.category or None,
        "note": obs.note or None,
        "lat": lat,
        "lon": lon,
        "position_source": source,
        "gopro_lat": match.gopro_lat,
        "gopro_lon": match.gopro_lon,
        "phone_lat": match.phone_lat,
        "phone_lon": match.phone_lon,
        "phone_fix_count": match.phone_fix_count or None,
        "gps_disagreement_m": (round(match.gps_disagreement_m, 2)
                               if match.gps_disagreement_m is not None else None),
        "gps_dop": match.gps_dop,
        "gps_fix": match.gps_fix,
    }


def _track_rows(session_uuid: uuid.UUID, samples: Sequence[gpmf.GpsSample],
                interval_s: float = 1.0) -> list[dict]:
    """Decimate the track to about 1 Hz — plenty for a map line."""
    rows: list[dict] = []
    last = -math.inf
    for sample in samples:
        if sample.offset_s - last < interval_s:
            continue
        last = sample.offset_s
        rows.append({
            "session_id": str(session_uuid),
            "seq": len(rows),
            "offset_s": round(sample.offset_s, 3),
            "utc": sample.utc.isoformat() if sample.utc else None,
            "lat": sample.lat,
            "lon": sample.lon,
            "speed_mps": round(sample.speed_2d, 3),
        })
    return rows


def _image_size(path: Path) -> tuple[int | None, int | None]:
    try:
        from PIL import Image
        with Image.open(path) as image:
            return image.width, image.height
    except Exception:
        return None, None


def _write_local_summary(work_dir: Path, result: IngestResult) -> None:
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "result.json").write_text(
            json.dumps(result.as_row(), indent=2, default=str) + "\n")
    except OSError:
        pass


def _human_bytes(value: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(value) < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


# --------------------------------------------------------------------------
# Batch summary
# --------------------------------------------------------------------------

@dataclass
class BatchSummary:
    """The end-of-run report.

    The number that matters is total matched against rows in the observation
    CSV. If the CSV has 340 rows and 312 matched, 28 observations vanished into
    chapter gaps or GPS dropouts — and nobody will notice unless the tool says
    so out loud.
    """

    campaign: str = ""
    csv_rows: int = 0
    results: list[IngestResult] = field(default_factory=list)

    def add(self, result: IngestResult) -> None:
        self.results.append(result)

    @property
    def total_matched(self) -> int:
        return sum(r.matched for r in self.results)

    @property
    def total_frames(self) -> int:
        return sum(r.frames for r in self.results)

    @property
    def total_proxy_bytes(self) -> int:
        return sum(r.proxy_bytes for r in self.results)

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for result in self.results:
            counts[result.status] = counts.get(result.status, 0) + 1
        return counts

    def render(self) -> str:
        lines: list[str] = []
        header = f"{'file':<22} {'status':<9} {'obs':>5} {'snap':>6} {'frames':>7} {'proxy':>9}"
        lines.append(header)
        lines.append("-" * len(header))
        for r in self.results:
            snap = f"{100 * r.snap_ratio:.0f}%" if r.matched else "-"
            lines.append(
                f"{r.file[:22]:<22} {r.status:<9} {r.matched:>5} {snap:>6} "
                f"{r.frames:>7} {_human_bytes(r.proxy_bytes):>9}")
            if r.error:
                lines.append(f"    error: {r.error}")
            if r.hint:
                lines.append(f"    hint:  {r.hint}")
        lines.append("-" * len(header))

        counts = self.counts()
        status = ", ".join(f"{n} {name}" for name, n in sorted(counts.items()))
        lines.append(f"{len(self.results)} files: {status}")
        lines.append(f"frames extracted: {self.total_frames}")
        lines.append(f"proxy total:      {_human_bytes(self.total_proxy_bytes)}")

        if self.csv_rows:
            missing = self.csv_rows - self.total_matched
            lines.append(f"observations:     {self.total_matched} matched "
                         f"of {self.csv_rows} rows in the CSV")
            if missing > 0:
                lines.append(
                    f"*** {missing} observation(s) matched no file. They fell into a "
                    "chapter gap or a GPS dropout, or the clock offset is wrong. ***")
            elif missing < 0:
                lines.append(
                    f"*** {-missing} more matches than CSV rows — observations matched "
                    "more than one file, check for overlapping chapters. ***")
        return "\n".join(lines)

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "campaign": self.campaign,
            "generated_at": datetime.now(UTC).isoformat(),
            "csv_rows": self.csv_rows,
            "total_matched": self.total_matched,
            "total_frames": self.total_frames,
            "total_proxy_bytes": self.total_proxy_bytes,
            "unmatched": max(0, self.csv_rows - self.total_matched),
            "files": [r.as_row() for r in self.results],
        }, indent=2, default=str) + "\n")
        return path
