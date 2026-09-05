#!/usr/bin/env python3
"""Time each stage of one chapter, so speed talk is about your footage.

Everything the tool claims about speed was measured on a small synthetic clip.
Your chapters are 2.7K/4K and several gigabytes; decode cost in particular does
not transfer at all. Point this at one real chapter and it will say where the
time actually goes on your machine.

    python tools/bench.py D:\\footage\\GX010042.MP4
    python tools/bench.py D:\\footage\\GX010042.MP4 --proxy

Nothing is uploaded and nothing is written outside the scratch folder, which is
deleted afterwards.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from curbtool import gpmf, media  # noqa: E402


class Stage:
    """Times a block and prints a line for it."""

    rows: list[tuple[str, float, str]] = []

    def __init__(self, label: str):
        self.label = label
        self.note = ""

    interactive = sys.stdout.isatty()

    def __enter__(self):
        self.start = time.perf_counter()
        if Stage.interactive:
            # Rewritten in place on a terminal; redirected to a file, the
            # carriage return would just leave both halves on the line.
            print(f"  {self.label:<34} ...", end="", flush=True)
        return self

    def __exit__(self, *exc):
        elapsed = time.perf_counter() - self.start
        Stage.rows.append((self.label, elapsed, self.note))
        prefix = "\r" if Stage.interactive else ""
        print(f"{prefix}  {self.label:<34} {elapsed:7.2f}s   {self.note}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("video", help="one GoPro chapter")
    parser.add_argument("--observations", type=int, default=20,
                        help="how many observations to simulate (default 20)")
    parser.add_argument("--max-frames", type=int, default=9)
    parser.add_argument("--frame-width", type=int, default=1280)
    parser.add_argument("--proxy", action="store_true",
                        help="also time a proxy transcode — this is the slow one")
    parser.add_argument("--proxy-seconds", type=float, default=60.0,
                        help="transcode only this many seconds, and extrapolate")
    args = parser.parse_args()

    video = Path(args.video)
    if not video.exists():
        print(f"{video} does not exist")
        return 1

    work = Path(tempfile.mkdtemp(prefix="curbtool-bench-"))
    try:
        size_gb = video.stat().st_size / 1e9
        print(f"\n{video.name}  ({size_gb:.2f} GB)\n")

        with Stage("open + read header") as stage:
            info = media.probe(video)
            stage.note = (f"{info.width}x{info.height} {info.fps:.0f}fps "
                          f"{info.duration_s / 60:.1f} min")

        with Stage("telemetry (demux, no decode)") as stage:
            try:
                telemetry = gpmf.parse_telemetry(video)
                stage.note = (f"{len(telemetry.samples)} fixes, {telemetry.kind}, "
                              f"{telemetry.device or 'unknown'}")
                samples = telemetry.samples
            except gpmf.GpmfError as exc:
                stage.note = f"none ({exc})"
                samples = []

        # Spread the simulated observations over the chapter, as a real drive does.
        duration = info.duration_s or 600.0
        step = duration / (args.observations + 1)
        requests = []
        for i in range(args.observations):
            middle = step * (i + 1)
            targets = media.frame_times(max(0.0, middle - 4), min(duration, middle + 4),
                                        1.0, args.max_frames)
            requests.append(media.FrameRequest(targets, work / f"obs{i:03d}", "f"))
        planned = sum(len(r.targets) for r in requests)

        with Stage(f"frames ({planned} from {len(requests)} obs)") as stage:
            cut = media.extract_frames_multi(video, requests, width=args.frame_width)
            written = sum(len(group) for group in cut)
            sizes = [p.stat().st_size for group in cut for _, p in group]
            stage.note = (f"{written} written, "
                          f"{sum(sizes) / max(1, len(sizes)) / 1024:.0f} KiB each")
        frames_s = Stage.rows[-1][1]

        if args.proxy:
            encoder, _ = media.available_encoder("auto")
            print(f"\n  hardware encoder probe: {encoder}"
                  f"{'  (software fallback)' if encoder == 'h264' else '  <- hardware'}")
            with Stage(f"proxy, first {args.proxy_seconds:.0f}s") as stage:
                used = _partial_proxy(video, work / "proxy.mp4", args.proxy_seconds)
                stage.note = f"encoder={used}"
            part = Stage.rows[-1][1]
            if duration > args.proxy_seconds:
                full = part * duration / args.proxy_seconds
                print(f"  -> a whole chapter would take about {full / 60:.0f} min")

        print("\n" + "-" * 62)
        total = sum(row[1] for row in Stage.rows)
        print(f"  {'total':<34} {total:7.2f}s")
        if not args.proxy:
            print(f"\n  A frames-only ingest of this chapter costs about "
                  f"{total:.0f}s.\n  Across 17 chapters: ~{total * 17 / 60:.0f} min.")
            print("  Add --proxy to see what turning video on would cost.")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _partial_proxy(source: Path, out: Path, seconds: float) -> str:
    """Transcode only the opening seconds, so a benchmark is not a coffee break."""
    stop = {"at": False}

    def should_cancel() -> bool:
        return stop["at"]

    watcher = {"seen": 0.0}

    def on_progress(position: float, _duration: float) -> None:
        watcher["seen"] = position
        if position >= seconds:
            stop["at"] = True

    try:
        return media.build_proxy(source, out, height=720, bitrate_kbps=2500,
                                 on_progress=on_progress, should_cancel=should_cancel)
    except media.Cancelled:
        return media.available_encoder("auto")[0]


if __name__ == "__main__":
    sys.exit(main())
