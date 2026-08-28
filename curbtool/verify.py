"""A dry run of the matching, with no decoding and nothing written.

This exists because of the shape of the risk. Ingesting seventeen chapters
costs hours; discovering afterwards that the clock offset was wrong and nothing
matched costs those hours twice. Reading telemetry does not need the video
decoded at all — only the metadata track demuxed — so the whole campaign can be
checked in about as long as it takes to read the files off the card.

Answer three questions before spending an afternoon:

  1. Does every observation land inside some chapter?
  2. Does it land during a stop, where the target is actually framed?
  3. If not, would a different clock offset fix it?
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Sequence

from . import gpmf
from .config import Settings
from .observations import Observation, suggest_clock_offset


@dataclass
class FileCheck:
    file: str
    ok: bool = True
    error: str = ""
    first_utc: datetime | None = None
    last_utc: datetime | None = None
    duration_s: float = 0.0
    fixes: int = 0
    dropped: int = 0
    stops: int = 0
    matched: list[str] = field(default_factory=list)
    snapped: list[str] = field(default_factory=list)

    @property
    def snap_ratio(self) -> float:
        return len(self.snapped) / len(self.matched) if self.matched else 0.0


@dataclass
class CampaignCheck:
    csv_rows: int = 0
    files: list[FileCheck] = field(default_factory=list)
    unmatched: list[Observation] = field(default_factory=list)
    duplicated: dict[str, list[str]] = field(default_factory=dict)
    clock_offset_hint: float | None = None
    clock_offset_rescues: int = 0
    settings: Settings | None = None

    @property
    def matched_count(self) -> int:
        return self.csv_rows - len(self.unmatched)

    @property
    def total_snapped(self) -> int:
        return sum(len(f.snapped) for f in self.files)

    @property
    def ready(self) -> bool:
        """Safe to commit an afternoon to a full ingest?"""
        return (not self.unmatched
                and any(f.ok for f in self.files)
                and all(f.ok for f in self.files))

    def render(self) -> str:
        lines: list[str] = []
        header = f"{'file':<22} {'window (UTC)':<19} {'fixes':>6} {'stops':>6} {'obs':>5} {'snap':>6}"
        lines.append(header)
        lines.append("-" * len(header))
        for check in self.files:
            if not check.ok:
                lines.append(f"{check.file[:22]:<22} {'UNREADABLE':<19}  {check.error[:44]}")
                continue
            window = (f"{check.first_utc:%H:%M:%S}-{check.last_utc:%H:%M:%S}"
                      if check.first_utc and check.last_utc else "?")
            snap = f"{100 * check.snap_ratio:.0f}%" if check.matched else "-"
            lines.append(f"{check.file[:22]:<22} {window:<19} {check.fixes:>6} "
                         f"{check.stops:>6} {len(check.matched):>5} {snap:>6}")
        lines.append("-" * len(header))

        lines.append(f"observations: {self.matched_count} matched of {self.csv_rows} "
                     f"rows in the CSV")
        if self.matched_count:
            lines.append(f"of those:     {self.total_snapped} landed during a detected stop "
                         f"({100 * self.total_snapped / self.matched_count:.0f}%)")

        # A low snap ratio is not a blocker — a tag dropped while still rolling
        # falls back to a fixed window and still gets frames — but across a
        # whole campaign it means stop detection is mistuned, and those frames
        # are guesses rather than the moment the operator framed the target.
        if self.matched_count and self.total_snapped <= self.matched_count / 2:
            lines.append("")
            lines.append(
                f"note: only {100 * self.total_snapped / self.matched_count:.0f}% of "
                "matched observations landed during a detected stop. Those that did "
                "not get a fixed window around the tag instead of the stationary "
                "period. Try raising --stop-speed or lowering --stop-min-duration.")

        if self.unmatched:
            lines.append("")
            lines.append(f"*** {len(self.unmatched)} observation(s) match no chapter: ***")
            for observation in self.unmatched[:12]:
                lines.append(f"      {observation.external_id:<14} {observation.utc:%Y-%m-%d %H:%M:%S} "
                             f"{observation.category}")
            if len(self.unmatched) > 12:
                lines.append(f"      … and {len(self.unmatched) - 12} more")

        if self.clock_offset_hint:
            hours = self.clock_offset_hint / 3600
            lines.append("")
            lines.append(f"*** Try --clock-offset {self.clock_offset_hint:+.0f} "
                         f"({hours:+.0f} h): it would rescue {self.clock_offset_rescues} "
                         f"of the {len(self.unmatched)} missing. The tagging app most "
                         "likely exported local time rather than UTC. ***")

        if self.duplicated:
            lines.append("")
            lines.append(f"note: {len(self.duplicated)} observation(s) fall inside more than "
                         "one chapter — check for overlapping recordings.")

        lines.append("")
        lines.append("READY: a full ingest should account for every observation."
                     if self.ready else
                     "NOT READY: fix the above before spending hours on a full ingest.")
        return "\n".join(lines)


def check_campaign(videos: Sequence[Path], observations: Sequence[Observation],
                   settings: Settings,
                   on_progress: Callable[[str], None] = lambda m: None) -> CampaignCheck:
    """Match the campaign against every chapter without decoding a single frame."""
    result = CampaignCheck(csv_rows=len(observations), settings=settings)
    offset = timedelta(seconds=settings.clock_offset_s)
    seen: dict[str, list[str]] = {}

    for video in videos:
        on_progress(f"reading {video.name}")
        check = FileCheck(file=video.name)
        try:
            telemetry = gpmf.parse_telemetry(video)
        except (gpmf.GpmfError, OSError) as exc:
            check.ok = False
            check.error = str(exc)
            result.files.append(check)
            continue

        samples = telemetry.samples
        stops = gpmf.detect_stops(samples, speed_threshold=settings.stop_speed_mps,
                                  min_duration_s=settings.stop_min_duration_s)
        check.first_utc = telemetry.first_utc
        check.last_utc = telemetry.last_utc
        check.duration_s = telemetry.duration_s
        check.fixes = len(samples)
        check.dropped = telemetry.dropped_samples
        check.stops = len(stops)

        for observation in observations:
            video_offset = gpmf.utc_to_offset(samples, observation.utc + offset)
            if video_offset is None:
                continue
            check.matched.append(observation.external_id)
            seen.setdefault(observation.external_id, []).append(video.name)
            if gpmf.stop_for_offset(stops, video_offset, settings.stop_tolerance_s):
                check.snapped.append(observation.external_id)

        result.files.append(check)

    result.unmatched = [o for o in observations if o.external_id not in seen]
    result.duplicated = {k: v for k, v in seen.items() if len(v) > 1}

    # A clock offset is a systematic error: it puts *every* observation out by
    # the same amount. Advising a campaign-wide shift because one stray tag sits
    # four hours away would break everything that currently matches, so the hint
    # only appears when most of the campaign is unmatched and the shift rescues
    # most of what is missing.
    if len(result.unmatched) > result.csv_rows / 2:
        windows = [(f.first_utc, f.last_utc) for f in result.files
                   if f.ok and f.first_utc and f.last_utc]
        if windows:
            guess = suggest_clock_offset(
                result.unmatched, min(w[0] for w in windows), max(w[1] for w in windows))
            if guess and guess[1] > len(result.unmatched) / 2:
                result.clock_offset_hint, result.clock_offset_rescues = guess
    return result
