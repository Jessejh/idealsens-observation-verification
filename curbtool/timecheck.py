"""Prove the timezones line up, before extracting a single frame.

The whole pipeline rests on one assumption: that a timestamp in the tagging
app's export and a moment in the footage refer to the same instant. Get that
wrong by an hour and every frame is of the wrong place, while everything still
*looks* like it worked. Nothing else in the tool can detect that; only this can.

Two sides, and they fail differently.

**The camera.** GoPro telemetry takes its time from the GPS satellites, so
GPSU (GPS5) and the per-sample stamps in GPS9 are always UTC, whatever the
camera's clock or timezone is set to. The trap is elsewhere: the MP4
container's ``creation_time`` is written in the camera's *local* time while
being labelled with a "Z". Read that instead of the telemetry and you inherit
the camera's timezone setting. This module reads both and reports the gap —
which is the camera's local offset, and is expected to be non-zero.

**The export.** A tagging app may give local time, UTC, epoch milliseconds, or
several at once. Where it gives more than one, they can be checked against each
other and the offset proven from the file itself, with nothing to assume.

The decisive test is the last one: do the export's UTC window and the footage's
UTC window overlap? If they are offset by a whole number of hours, someone read
local time as UTC.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from . import gpmf
from .observations import (ObservationSet, ObservationError, choose_time_column,
                           parse_timestamp, score_time_column, _open_rows)

UTC = timezone.utc


@dataclass
class ColumnTime:
    name: str
    score: int
    reason: str
    first: datetime | None
    last: datetime | None
    parsed: int
    total: int


@dataclass
class CsvAudit:
    path: str
    chosen: str = ""
    columns: list[ColumnTime] = field(default_factory=list)
    offsets: list[tuple[str, str, float]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    centroid: tuple[float, float] | None = None
    # Every parsed instant from the chosen column. Window overlap alone cannot
    # settle the question — a two-day export slid across a twenty-minute
    # chapter overlaps at almost any shift — so the test counts how many
    # individual observations land inside the footage.
    stamps: list[datetime] = field(default_factory=list)

    @property
    def window(self) -> tuple[datetime, datetime] | None:
        for column in self.columns:
            if column.name == self.chosen and column.first and column.last:
                return column.first, column.last
        return None


@dataclass
class VideoAudit:
    file: str
    ok: bool = True
    error: str = ""
    kind: str = ""
    device: str | None = None
    gpmf_first: datetime | None = None
    gpmf_last: datetime | None = None
    container_creation: datetime | None = None
    container_raw: str = ""
    centroid: tuple[float, float] | None = None

    @property
    def camera_offset_h(self) -> float | None:
        """Container time minus telemetry time — the camera's timezone setting."""
        if self.container_creation is None or self.gpmf_first is None:
            return None
        return (self.container_creation - self.gpmf_first).total_seconds() / 3600.0


@dataclass
class TimeAudit:
    csv: CsvAudit | None = None
    videos: list[VideoAudit] = field(default_factory=list)

    @property
    def video_window(self) -> tuple[datetime, datetime] | None:
        stamps = [v.gpmf_first for v in self.videos if v.gpmf_first]
        ends = [v.gpmf_last for v in self.videos if v.gpmf_last]
        return (min(stamps), max(ends)) if stamps and ends else None

    @property
    def windows(self) -> list[tuple[datetime, datetime]]:
        return [(v.gpmf_first, v.gpmf_last) for v in self.videos
                if v.gpmf_first and v.gpmf_last]

    @property
    def footage_seconds(self) -> float:
        return sum((b - a).total_seconds() for a, b in self.windows)

    def inside(self, shift_hours: float = 0.0) -> int:
        """Observations landing inside some chapter, after shifting by *shift_hours*."""
        if self.csv is None or not self.windows:
            return 0
        shift = timedelta(hours=shift_hours)
        windows = self.windows
        return sum(1 for stamp in self.csv.stamps
                   if any(a <= stamp + shift <= b for a, b in windows))

    @property
    def shift_scores(self) -> list[tuple[float, int]]:
        """Every whole-hour shift and how many observations it places in shot."""
        scores = [(float(h), self.inside(float(h))) for h in range(-14, 15)]
        scores.sort(key=lambda item: (-item[1], abs(item[0])))
        return scores

    @property
    def best_shift(self) -> tuple[float, int]:
        """The whole-hour shift placing the most observations inside the footage."""
        return self.shift_scores[0]

    @property
    def shift_is_clear(self) -> bool:
        """Is the clocks-are-wrong signal strong enough to act on?

        Judged against how things stand, not against the runner-up. Where the
        footage is continuous, neighbouring shifts score within a few percent
        of each other and picking between them is noise — but a shift that
        triples what lands in shot is not noise, it is an hours-out clock.
        """
        best = self.best_shift[1]
        return best >= 3 and best >= 3 * max(1, self.inside(0.0))

    @property
    def near_best_shifts(self) -> list[tuple[float, int]]:
        """Shifts scoring within 10% of the best — the ones it cannot separate."""
        best = self.best_shift[1]
        if not best:
            return []
        return [(h, c) for h, c in self.shift_scores if c >= best * 0.9][:4]

    @property
    def separation_m(self) -> float | None:
        """Distance between where the export says it was and where the camera was.

        Kilometres apart means the wrong CSV has been paired with the wrong
        footage — a live risk when a campaign is seven phone sessions over two
        days. Cheap to check, and invisible otherwise.
        """
        if self.csv is None or self.csv.centroid is None:
            return None
        centroids = [v.centroid for v in self.videos if v.centroid]
        if not centroids:
            return None
        lat = sum(c[0] for c in centroids) / len(centroids)
        lon = sum(c[1] for c in centroids) / len(centroids)
        return gpmf.haversine_m(self.csv.centroid[0], self.csv.centroid[1], lat, lon)

    @property
    def ok(self) -> bool:
        if (self.separation_m or 0) > 5000:
            return False
        if self.csv is None or not self.windows:
            return False
        if not all(v.ok for v in self.videos):
            return False
        # No whole-hour shift may beat leaving the clocks alone.
        if self.inside(0.0) <= 0:
            return False
        # A better shift only condemns the run when the footage can support it.
        return self.best_shift[0] == 0.0 or not self.shift_is_clear

    def render(self) -> str:
        lines: list[str] = []

        if self.csv:
            lines.append(f"OBSERVATIONS  {Path(self.csv.path).name}")
            lines.append("")
            head = f"  {'column':<20} {'score':>5}  {'window (UTC)':<41} rows"
            lines.append(head)
            lines.append("  " + "-" * (len(head) - 2))
            for column in self.csv.columns:
                window = (f"{column.first:%Y-%m-%d %H:%M} .. {column.last:%Y-%m-%d %H:%M}"
                          if column.first and column.last else "unparsable")
                mark = " <-" if column.name == self.csv.chosen else ""
                lines.append(f"  {column.name:<20} {column.score:>5}  {window:<41} "
                             f"{column.parsed}/{column.total}{mark}")
            lines.append(f"  using {self.csv.chosen!r} — {self._reason(self.csv.chosen)}")

            if self.csv.offsets:
                lines.append("")
                lines.append("  the export states the same instant more than once:")
                for a, b, hours in self.csv.offsets:
                    verdict = ("identical" if abs(hours) < 1 / 60 else
                               f"{hours:+.2f} h apart")
                    lines.append(f"    {a} vs {b}: {verdict}")
                lines.append("  Consistent offsets prove the zone from the file itself — "
                             "nothing is assumed.")
            for warning in self.csv.warnings:
                lines.append(f"  note: {warning}")
            lines.append("")

        if self.videos:
            lines.append("FOOTAGE")
            lines.append("")
            head = (f"  {'file':<20} {'tlm':<5} {'telemetry UTC (from GPS)':<37} "
                    f"{'container says':<20} camera TZ")
            lines.append(head)
            lines.append("  " + "-" * (len(head) - 2))
            for video in self.videos:
                if not video.ok:
                    lines.append(f"  {video.file[:20]:<20} {'—':<5} {video.error[:60]}")
                    continue
                window = (f"{video.gpmf_first:%Y-%m-%d %H:%M} .. {video.gpmf_last:%H:%M}"
                          if video.gpmf_first else "?")
                container = (f"{video.container_creation:%Y-%m-%d %H:%M}"
                             if video.container_creation else "not set")
                offset = video.camera_offset_h
                tz = f"UTC{offset:+.0f}" if offset is not None else "—"
                lines.append(f"  {video.file[:20]:<20} {video.kind:<5} {window:<37} "
                             f"{container:<20} {tz}")
            lines.append("")
            lines.append("  Telemetry UTC comes from the satellites and is authoritative.")
            lines.append("  The container's creation_time is the camera's local clock, even")
            lines.append("  though it is written with a 'Z'. A non-zero camera TZ above is")
            lines.append("  normal and harmless — the pipeline never reads that field.")
            lines.append("")

        separation = self.separation_m
        if separation is not None:
            lines.append("PLACE")
            lines.append("")
            if separation > 5000:
                lines.append(f"  *** The export and the footage are {separation / 1000:.0f} km "
                             "apart. ***")
                lines.append("  These are almost certainly not the same campaign. Check which")
                lines.append("  CSV goes with which folder of footage before going further.")
            else:
                lines.append(f"  Export and footage agree on place to within "
                             f"{separation:.0f} m. Same campaign.")
            lines.append("")

        lines.append("VERDICT")
        lines.append("")
        if self.csv is None or not self.windows:
            lines.append("  Not enough to compare — supply both a CSV and footage.")
        else:
            total = len(self.csv.stamps)
            now = self.inside(0.0)
            shift, shifted = self.best_shift
            lines.append(f"  Footage supplied covers {_span(self.footage_seconds)} "
                         f"across {len(self.windows)} chapter(s).")
            lines.append(f"  Observations landing inside it as things stand: {now} of {total}.")
            if shift == 0.0 and now > 0:
                lines.append("")
                lines.append("  No whole-hour shift does better, which is what agreeing")
                lines.append("  clocks look like. Frames will be cut at the right moment.")
                if now < total:
                    lines.append(f"  The other {total - now} are simply outside the chapters")
                    lines.append("  supplied here — check the whole campaign with `check`.")
            elif shift != 0.0 and self.shift_is_clear:
                lines.append("")
                lines.append(f"  *** Shifting by {shift:+.0f} h would raise that to {shifted}. ***")
                lines.append("  That is the signature of local time being read as UTC.")
                near = self.near_best_shifts
                if len(near) > 1:
                    spread = ", ".join(f"{h:+.0f} h -> {c}" for h, c in near)
                    lines.append(f"  Shifts scoring alike: {spread}")
                    lines.append("  Continuous footage cannot separate neighbours; `check`")
                    lines.append("  matches each observation individually and will pin it down.")
                lines.append(f"  Start with: --clock-offset {shift * 3600:+.0f}")
                lines.append("  or name the export's real zone, e.g. --timezone Europe/Tallinn")
            elif shift != 0.0:
                lines.append("")
                lines.append(f"  A {shift:+.0f} h shift would put {shifted} in shot rather than")
                lines.append(f"  {now}, which is too small a difference to act on. The clocks")
                lines.append("  look right; `check` will confirm across the whole campaign.")
            else:
                lines.append("")
                lines.append("  *** Nothing lands inside the footage, and no whole-hour shift")
                lines.append("  helps. Check these are the same campaign. ***")
        lines.append("")
        lines.append("  Numbers agreeing is not proof the frames show the right thing.")
        lines.append("  Cut one file and look at the pictures before running the rest.")
        return "\n".join(lines)

    def _reason(self, name: str) -> str:
        for column in (self.csv.columns if self.csv else []):
            if column.name == name:
                return column.reason
        return ""


def _span(seconds: float) -> str:
    """Readable at any scale — a 30-second clip is not '0.0 h'."""
    seconds = abs(seconds)
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


# --------------------------------------------------------------------------

def audit_csv(path: str | Path, time_column: str | None = None,
              timezone_name: str | None = None) -> CsvAudit:
    """Inspect every time-like column and compare them against each other."""
    path = Path(path)
    headers, rows = _open_rows(path)
    audit = CsvAudit(path=str(path))

    from .observations import TIME_ALIASES, _normalise
    candidates = [h for h in headers
                  if any(alias in _normalise(h) for alias in TIME_ALIASES)]
    if not candidates:
        raise ObservationError(
            f"{path.name}: no timestamp column found. Headers: {', '.join(headers)}")

    zone = None
    if timezone_name:
        try:
            from zoneinfo import ZoneInfo
            zone = ZoneInfo(timezone_name)
        except Exception as exc:
            audit.warnings.append(f"timezone {timezone_name!r} unavailable ({exc})")

    for header in candidates:
        values = [r.get(header, "") for r in rows]
        score, reason = score_time_column(header, values)
        parsed = [parse_timestamp(v, zone) for v in values]
        good = [p for p in parsed if p]
        audit.columns.append(ColumnTime(
            name=header, score=score, reason=reason,
            first=min(good) if good else None, last=max(good) if good else None,
            parsed=len(good), total=len(values)))

    audit.columns.sort(key=lambda c: (-c.score, headers.index(c.name)))
    audit.chosen, _ = choose_time_column(headers, rows, time_column)
    audit.stamps = [t for t in (parse_timestamp(r.get(audit.chosen), zone) for r in rows)
                    if t is not None]

    from .observations import LAT_ALIASES, LON_ALIASES, find_column, parse_number
    lat_col = find_column(headers, LAT_ALIASES)
    lon_col = find_column(headers, LON_ALIASES)
    if lat_col and lon_col:
        points = [(parse_number(r.get(lat_col)), parse_number(r.get(lon_col))) for r in rows]
        points = [(a, b) for a, b in points if a is not None and b is not None]
        if points:
            audit.centroid = (sum(p[0] for p in points) / len(points),
                              sum(p[1] for p in points) / len(points))

    # Pairwise offsets, using the median so one stray row cannot skew it.
    usable = [c for c in audit.columns if c.parsed]
    for i, a in enumerate(usable):
        for b in usable[i + 1:]:
            deltas = []
            for row in rows[:400]:
                pa = parse_timestamp(row.get(a.name), zone)
                pb = parse_timestamp(row.get(b.name), zone)
                if pa and pb:
                    deltas.append((pa - pb).total_seconds() / 3600.0)
            if deltas:
                deltas.sort()
                audit.offsets.append((a.name, b.name, deltas[len(deltas) // 2]))
    return audit


def audit_video(path: str | Path) -> VideoAudit:
    """Compare the footage's satellite-derived UTC against its container stamp."""
    import av

    path = Path(path)
    audit = VideoAudit(file=path.name)
    try:
        telemetry = gpmf.parse_telemetry(path)
        audit.kind = telemetry.kind
        audit.device = telemetry.device
        audit.gpmf_first = telemetry.first_utc
        audit.gpmf_last = telemetry.last_utc
        if telemetry.samples:
            audit.centroid = (
                sum(s.lat for s in telemetry.samples) / len(telemetry.samples),
                sum(s.lon for s in telemetry.samples) / len(telemetry.samples))
    except Exception as exc:
        audit.ok = False
        audit.error = str(exc)

    try:
        with av.open(str(path)) as container:
            raw = container.metadata.get("creation_time", "") or ""
    except Exception:
        raw = ""
    audit.container_raw = raw
    if raw:
        # Parsed without a zone on purpose: the "Z" GoPro writes here is a lie,
        # and treating it as UTC is exactly the mistake being measured.
        audit.container_creation = parse_timestamp(raw.rstrip("Zz"))
    return audit


def audit(videos: Sequence[Path], csv_path: str | Path | None = None,
          time_column: str | None = None, timezone_name: str | None = None,
          on_progress=lambda m: None) -> TimeAudit:
    """Audit both sides and report whether they refer to the same hours."""
    result = TimeAudit()
    if csv_path:
        on_progress(f"reading {Path(csv_path).name}")
        result.csv = audit_csv(csv_path, time_column, timezone_name)
    for video in videos:
        on_progress(f"reading {Path(video).name}")
        result.videos.append(audit_video(video))
    return result
