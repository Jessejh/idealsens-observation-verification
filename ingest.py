#!/usr/bin/env python3
"""curbtool CLI — a thin wrapper over curbtool.pipeline.

    python ingest.py web                                          # buttons for all of it
    python ingest.py timecheck FOLDER --observations tags.csv     # do the clocks agree?
    python ingest.py check  FOLDER --observations tags.csv        # always start here
    python ingest.py ingest FOLDER --campaign helsinki-2024 --observations tags.csv
    python ingest.py track  GX010042.MP4 --geojson route.json
    python ingest.py backfill FOLDER --campaign helsinki-2024
    python ingest.py gui

Run one file first and check that its target sits near delta_s = 0 before
batching seventeen of them; a systematic clock error is fixed for the whole
campaign with --clock-offset.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path

from curbtool import gpmf
from curbtool.batch import find_videos, load_inputs, make_client, run_batch
from curbtool.config import Settings, SupabaseConfig
from curbtool.media import find_lrv, probe
from curbtool.observations import ObservationError
from curbtool.pipeline import IngestJob, Progress, ingest_file
from curbtool.supabase_io import SupabaseError
from curbtool.verify import check_campaign


def log(message: str) -> None:
    """Progress goes to stderr so stdout stays pipeable."""
    print(message, file=sys.stderr, flush=True)


class Interrupt:
    """Turns the first Ctrl-C into a clean cancel, the second into a quit."""

    def __init__(self) -> None:
        self.cancelled = False
        signal.signal(signal.SIGINT, self._handle)

    def _handle(self, *_args) -> None:
        if self.cancelled:
            log("\ninterrupted twice — quitting now")
            raise SystemExit(130)
        self.cancelled = True
        log("\ncancelling after the current file — press Ctrl-C again to quit now")

    def __call__(self) -> bool:
        return self.cancelled


class ProgressPrinter:
    """One updating line per stage, so a long transcode never looks like a hang.

    On a terminal the line is rewritten in place. Redirected to a file or a
    pipe it prints once per stage instead — carriage returns in a log turn a
    transcode into several hundred lines of noise.
    """

    def __init__(self, quiet: bool = False) -> None:
        self.quiet = quiet
        self.interactive = sys.stderr.isatty()
        self.last_key: tuple[str, str] | None = None

    def __call__(self, progress: Progress) -> None:
        if self.quiet:
            return
        key = (progress.file, progress.stage)
        if not self.interactive:
            if key != self.last_key:
                sys.stderr.write(f"    {progress.stage:<7} {progress.message}\n")
                sys.stderr.flush()
                self.last_key = key
            return

        bar = ""
        if progress.total > 1:
            filled = int(24 * progress.fraction)
            bar = f" [{'#' * filled}{'.' * (24 - filled)}] {100 * progress.fraction:3.0f}%"
        line = f"    {progress.stage:<7}{bar} {progress.message}"
        same_stage = key == self.last_key
        sys.stderr.write(("\r" if same_stage else "") + line.ljust(96)
                         + ("" if same_stage else "\n"))
        sys.stderr.flush()
        self.last_key = key


# --------------------------------------------------------------------------
# Settings assembly
# --------------------------------------------------------------------------

def settings_from_args(args: argparse.Namespace) -> Settings:
    """Saved settings, with any flag given on the command line winning."""
    settings = Settings() if getattr(args, "no_saved_settings", False) else Settings.load()
    return settings.merged(
        observations_csv=getattr(args, "observations", None),
        gnss_csv=getattr(args, "gnss", None),
        campaign=getattr(args, "campaign", None),
        timezone=getattr(args, "timezone", None),
        clock_offset_s=getattr(args, "clock_offset", None),
        stop_speed_mps=getattr(args, "stop_speed", None),
        stop_min_duration_s=getattr(args, "stop_min_duration", None),
        frame_width=getattr(args, "frame_width", None),
        max_frames=getattr(args, "max_frames", None),
        frame_interval_s=getattr(args, "frame_interval", None),
        proxy_height=getattr(args, "proxy_height", None),
        proxy_bitrate_kbps=getattr(args, "proxy_bitrate", None),
        proxy_source=getattr(args, "proxy_source", None),
        work_dir=getattr(args, "work_dir", None),
        upload=False if getattr(args, "no_upload", False) else None,
    )


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--campaign", help="campaign name, part of the derived session ID")
    parser.add_argument("--observations", metavar="CSV",
                        help="campaign-wide observation CSV from the tagging app")
    parser.add_argument("--gnss", metavar="CSV", help="optional phone GNSS log")
    parser.add_argument("--timezone", metavar="ZONE",
                        help="IANA zone (e.g. Europe/Tallinn) for observation "
                             "timestamps that carry no zone of their own")
    parser.add_argument("--clock-offset", type=float, metavar="SECONDS",
                        help="seconds to add to every observation timestamp")
    parser.add_argument("--stop-speed", type=float, metavar="M/S",
                        help="speed at or below which the scooter counts as stopped")
    parser.add_argument("--stop-min-duration", type=float, metavar="SECONDS",
                        help="shortest stationary period that counts as a stop")
    parser.add_argument("--frame-width", type=int, help="width of extracted frames")
    parser.add_argument("--frame-interval", type=float, metavar="SECONDS",
                        help="spacing between frames within a stop window")
    parser.add_argument("--max-frames", type=int,
                        help="cap on frames per observation")
    parser.add_argument("--proxy-height", type=int, help="proxy height in pixels")
    parser.add_argument("--proxy-bitrate", type=int, metavar="KBPS",
                        help="proxy video bitrate")
    parser.add_argument("--proxy-source", choices=("none", "hd", "lrv", "auto"),
                        help="none skips video entirely (frames only); hd transcodes; "
                             "lrv/auto remux the .LRV companion where there is one")
    parser.add_argument("--no-proxy", dest="proxy_source", action="store_const",
                        const="none",
                        help="shorthand for --proxy-source none: extract frames and "
                             "skip the slow transcode")
    parser.add_argument("--work-dir", help="where frames and proxies are written")
    parser.add_argument("--no-upload", action="store_true",
                        help="process locally without touching Supabase")
    parser.add_argument("--no-saved-settings", action="store_true",
                        help="ignore ~/.curbtool.json")
    parser.add_argument("--quiet", action="store_true", help="no progress bars")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_ingest(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    supabase = SupabaseConfig.from_env()

    videos = []
    for target in args.target:
        videos.extend(find_videos(target))
    if not videos:
        log(f"no .MP4 files found in {', '.join(str(t) for t in args.target)}")
        return 1
    if not settings.campaign:
        log("--campaign is required: it is part of the derived session ID, and "
            "changing it later re-ingests everything as new sessions")
        return 2

    log(settings.describe())
    log(supabase.describe() if settings.upload else "Supabase: uploads disabled")

    summary = run_batch(
        videos, settings, supabase, force=args.force,
        reuse_media=getattr(args, "reuse_media", False),
        on_progress=ProgressPrinter(args.quiet),
        on_log=log,
        should_cancel=Interrupt(),
    )

    print()
    print(summary.render())
    path = summary.save(Path(settings.work_dir) / settings.campaign / "summary.json")
    log(f"\nsummary written to {path}")

    if args.save_settings:
        settings.save()
        log("settings saved to ~/.curbtool.json")

    failed = summary.counts().get("failed", 0)
    return 1 if failed else 0


def cmd_backfill(args: argparse.Namespace) -> int:
    """Re-upload and re-write rows from an existing work folder.

    For when the media is already extracted and only Supabase needs catching
    up — a schema change, a bucket emptied, a run that died during upload.
    Re-running is safe: the media is reused from the work folder rather than
    re-decoded, and derived IDs mean the same rows are updated.
    """
    args.force = True
    args.reuse_media = True
    log("backfill: reusing frames and proxies already in the work folder")
    return cmd_ingest(args)


def cmd_timecheck(args: argparse.Namespace) -> int:
    """Prove the export and the footage refer to the same hours.

    Run this before anything else when the timestamps come from a new app or a
    new camera. It reads telemetry and a CSV; it decodes nothing and writes
    nothing.
    """
    from curbtool.timecheck import audit

    settings = settings_from_args(args)
    videos: list[Path] = []
    for target in args.target:
        videos.extend(find_videos(target))

    csv_path = settings.observations_csv or None
    if not videos and not csv_path:
        log("give me some footage, an --observations CSV, or both")
        return 2

    result = audit(videos, csv_path, timezone_name=settings.timezone or None,
                   on_progress=lambda m: log(f"    {m}"))
    print()
    print(result.render())
    return 0 if result.ok else 1


def cmd_check(args: argparse.Namespace) -> int:
    """Dry-run the matching across the whole campaign. Decodes nothing, writes nothing.

    Run this before every full ingest. It costs a couple of minutes and it is
    the only cheap way to find out that the clock offset is wrong.
    """
    settings = settings_from_args(args)
    videos: list[Path] = []
    for target in args.target:
        videos.extend(find_videos(target))
    if not videos:
        log(f"no .MP4 files found in {', '.join(str(t) for t in args.target)}")
        return 1
    if not settings.observations_csv:
        log("--observations is required: there is nothing to check against without it")
        return 2

    observations, _ = load_inputs(settings)
    log(f"{len(videos)} file(s), {len(observations)} observation(s) in the CSV")
    log(settings.describe())

    result = check_campaign(videos, observations, settings,
                            on_progress=lambda m: log(f"    {m}"))
    print()
    print(result.render())
    return 0 if result.ready else 1


def cmd_track(args: argparse.Namespace) -> int:
    """Dump one file's telemetry — the first thing to run against a real GoPro file."""
    settings = settings_from_args(args)
    video = Path(args.video)
    try:
        telemetry = gpmf.parse_telemetry(video)
    except gpmf.GpmfError as exc:
        log(str(exc))
        return 1

    samples = telemetry.samples
    stops = gpmf.detect_stops(samples, speed_threshold=settings.stop_speed_mps,
                              min_duration_s=settings.stop_min_duration_s)
    info = probe(video)
    lrv = find_lrv(video)

    print(f"file       : {video.name} ({info.size_bytes / 1e9:.2f} GB)")
    print(f"video      : {info.width}x{info.height} @ {info.fps:.2f} fps, "
          f"{info.duration_s:.1f} s")
    print(f"device     : {telemetry.device or 'unknown'}")
    print(f"lrv        : {lrv.name if lrv else 'not found'}")
    print(f"fixes      : {len(samples)} usable"
          + (f", {telemetry.dropped_samples} dropped below fix quality 2"
             if telemetry.dropped_samples else ""))
    print(f"utc window : {telemetry.first_utc} .. {telemetry.last_utc}")
    print(f"stops      : {len(stops)}")
    for stop in stops:
        print(f"  {stop.index:>3}  {stop.start_s:7.1f} - {stop.end_s:7.1f} s  "
              f"({stop.duration_s:5.1f} s)  {stop.lat:.6f}, {stop.lon:.6f}")

    if args.geojson:
        feature = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "LineString",
                                 "coordinates": [[s.lon, s.lat] for s in samples]},
                    "properties": {"filename": video.name,
                                   "device": telemetry.device,
                                   "duration_s": info.duration_s},
                },
                *[
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [stop.lon, stop.lat]},
                        "properties": {"stop_index": stop.index,
                                       "start_s": stop.start_s,
                                       "end_s": stop.end_s,
                                       "duration_s": stop.duration_s},
                    }
                    for stop in stops
                ],
            ],
        }
        Path(args.geojson).write_text(json.dumps(feature, indent=2, default=str))
        log(f"route written to {args.geojson}")

    if args.csv:
        import csv as csv_module
        with open(args.csv, "w", newline="") as handle:
            writer = csv_module.writer(handle)
            writer.writerow(["offset_s", "utc", "lat", "lon", "speed_mps", "fix", "dop"])
            for s in samples:
                writer.writerow([f"{s.offset_s:.3f}", s.utc.isoformat() if s.utc else "",
                                 f"{s.lat:.7f}", f"{s.lon:.7f}", f"{s.speed_2d:.3f}",
                                 s.fix, f"{s.dop:.2f}"])
        log(f"samples written to {args.csv}")
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    """Serve the browser UI on this machine only."""
    from curbtool.webui import main as web_main
    return web_main(port=args.port, open_browser=not args.no_browser)


def cmd_gui(args: argparse.Namespace) -> int:
    try:
        from curbtool.gui import main as gui_main
    except ImportError as exc:
        log(f"cannot start the GUI: {exc}")
        log("Tkinter ships with python.org and most Windows builds of Python; "
            "on Linux install python3-tk.")
        return 1
    return gui_main()


def cmd_settings(args: argparse.Namespace) -> int:
    """Show or save the persisted settings."""
    settings = settings_from_args(args)
    if args.save:
        settings.save()
        log("saved to ~/.curbtool.json")
    print(json.dumps(settings.__dict__, indent=2, sort_keys=True))
    return 0


# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ingest.py",
        description="Ingest GoPro footage and phone-tagged observations into Supabase.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="process a file or folder end to end")
    p_ingest.add_argument("target", nargs="+", help="video file(s) or a folder of them")
    p_ingest.add_argument("--force", action="store_true",
                          help="re-process files already marked complete")
    p_ingest.add_argument("--save-settings", action="store_true",
                          help="persist these options to ~/.curbtool.json")
    add_common_arguments(p_ingest)
    p_ingest.set_defaults(func=cmd_ingest)

    p_backfill = sub.add_parser(
        "backfill", help="re-upload and rewrite rows from an existing work folder")
    p_backfill.add_argument("target", nargs="+", help="video file(s) or a folder")
    p_backfill.add_argument("--save-settings", action="store_true")
    add_common_arguments(p_backfill)
    p_backfill.set_defaults(func=cmd_backfill)

    p_check = sub.add_parser(
        "check",
        help="dry-run the matching across a campaign — decodes nothing, writes nothing")
    p_check.add_argument("target", nargs="+", help="video file(s) or a folder of them")
    add_common_arguments(p_check)
    p_check.set_defaults(func=cmd_check)

    p_timecheck = sub.add_parser(
        "timecheck",
        help="prove the CSV and the footage refer to the same hours (timezones)")
    p_timecheck.add_argument("target", nargs="*", default=[],
                             help="video file(s) or a folder of them")
    add_common_arguments(p_timecheck)
    p_timecheck.set_defaults(func=cmd_timecheck)

    p_track = sub.add_parser(
        "track", help="dump one file's telemetry, stops and route (verify first)")
    p_track.add_argument("video")
    p_track.add_argument("--geojson", help="write the route and stops as GeoJSON")
    p_track.add_argument("--csv", help="write every GPS sample as CSV")
    add_common_arguments(p_track)
    p_track.set_defaults(func=cmd_track)

    p_web = sub.add_parser(
        "web", help="open the browser UI (recommended — no Tkinter needed)")
    p_web.add_argument("--port", type=int, default=0,
                       help="port to listen on (default: any free port)")
    p_web.add_argument("--no-browser", action="store_true",
                       help="print the address instead of opening a browser")
    p_web.set_defaults(func=cmd_web)

    p_gui = sub.add_parser("gui", help="open the desktop window (Tkinter)")
    p_gui.set_defaults(func=cmd_gui)

    p_settings = sub.add_parser("settings", help="show or save persisted settings")
    p_settings.add_argument("--save", action="store_true")
    add_common_arguments(p_settings)
    p_settings.set_defaults(func=cmd_settings)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ObservationError, SupabaseError) as exc:
        log(f"error: {exc}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
