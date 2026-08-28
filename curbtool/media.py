"""Frame extraction and proxy building, via PyAV.

Never an ffmpeg binary: AppLocker group policy on the operator's machine
blocks executables, which is why this pipeline uses PyAV's linked FFmpeg
libraries throughout.

The split between the two functions here is the central storage decision of the
project. The campaign is ~80 GB of source footage. Uploading that would cost a
workday and burn Supabase egress the moment officials start scrubbing. So
evidence frames are cut from the HD original at full quality (~2 GB for the
campaign), playback runs off a downscaled proxy (~6 GB), and the HD stays on a
drive.
"""

from __future__ import annotations

import fractions
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

LRV_SUFFIXES = (".LRV", ".lrv")


class MediaError(Exception):
    """Raised when a video file cannot be opened or decoded."""


class Cancelled(Exception):
    """Raised when the operator cancels a batch mid-item."""


@dataclass
class VideoInfo:
    path: Path
    duration_s: float
    width: int
    height: int
    fps: float
    size_bytes: int

    @property
    def name(self) -> str:
        return self.path.name


def _container_format(out_path: Path) -> str:
    """The muxer to use for *out_path*.

    Output is written to a ``.part`` file first, and FFmpeg cannot infer a
    format from that suffix, so it is always named explicitly.
    """
    return {".mov": "mov", ".mkv": "matroska", ".webm": "webm"}.get(
        out_path.suffix.lower(), "mp4")


def probe(path: str | Path) -> VideoInfo:
    """Read duration and geometry without decoding."""
    import av

    path = Path(path)
    try:
        with av.open(str(path)) as container:
            stream = next((s for s in container.streams if s.type == "video"), None)
            if stream is None:
                raise MediaError(f"{path.name}: no video stream")
            duration = 0.0
            if stream.duration is not None and stream.time_base:
                duration = float(stream.duration * stream.time_base)
            elif container.duration:
                duration = container.duration / 1_000_000.0
            fps = float(stream.average_rate) if stream.average_rate else 0.0
            return VideoInfo(
                path=path,
                duration_s=duration,
                width=stream.codec_context.width,
                height=stream.codec_context.height,
                fps=fps,
                size_bytes=path.stat().st_size,
            )
    except MediaError:
        raise
    except Exception as exc:  # PyAV raises a family of errors; treat them alike
        raise MediaError(f"{path.name}: cannot open ({exc})") from exc


def find_lrv(path: str | Path) -> Path | None:
    """Locate the low-resolution companion GoPro writes beside each chapter.

    GX010042.MP4 pairs with GX010042.LRV, and on older cameras GOPR0042.MP4
    pairs with GOPR0042.LRV.
    """
    path = Path(path)
    for suffix in LRV_SUFFIXES:
        candidate = path.with_suffix(suffix)
        if candidate.exists():
            return candidate
    return None


def frame_times(start_s: float, end_s: float, interval_s: float,
                max_frames: int) -> list[float]:
    """Timestamps to grab across a window, capped at *max_frames*.

    A long stop must not produce hundreds of images, so once the cap is hit the
    frames are spread evenly across the whole window rather than truncating it
    — the operator may have framed the target at either end.
    """
    if end_s < start_s:
        start_s, end_s = end_s, start_s
    span = end_s - start_s
    if max_frames <= 1 or span <= 0:
        return [start_s + span / 2.0]

    count = int(span / interval_s) + 1 if interval_s > 0 else max_frames
    count = max(1, min(count, max_frames))
    if count == 1:
        return [start_s + span / 2.0]
    step = span / (count - 1)
    return [start_s + i * step for i in range(count)]


def extract_frames(video: str | Path, targets: Sequence[float], out_dir: str | Path,
                   prefix: str = "frame", width: int = 1280, quality: int = 88,
                   on_frame: Callable[[int, int, float, Path], None] | None = None,
                   should_cancel: Callable[[], bool] | None = None) -> list[tuple[float, Path]]:
    """Cut JPEGs from *video* at each offset in *targets*.

    One seek for the whole group, then a forward decode picking off targets as
    they pass. Seeking per frame would be several times slower over a window of
    a dozen frames, and the targets in a stop window are always close together.

    Returns ``(actual_offset_s, path)`` for each frame written, in time order.
    """
    import av
    from PIL import Image

    video = Path(video)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    wanted = sorted(t for t in targets if t >= 0)
    if not wanted:
        return []

    written: list[tuple[float, Path]] = []
    try:
        with av.open(str(video)) as container:
            stream = next((s for s in container.streams if s.type == "video"), None)
            if stream is None:
                raise MediaError(f"{video.name}: no video stream")
            stream.thread_type = "AUTO"
            time_base = float(stream.time_base) if stream.time_base else 1 / 30.0

            # Seek slightly before the first target: seeking lands on the
            # preceding keyframe, and GoPro's GOP is well under a second.
            seek_to = max(0.0, wanted[0] - 1.0)
            container.seek(int(seek_to / time_base), stream=stream, backward=True)

            index = 0
            for frame in container.decode(stream):
                if should_cancel is not None and should_cancel():
                    break
                position = frame.time
                if position is None:
                    continue
                # A frame can satisfy several targets if they are closer
                # together than the frame interval.
                while index < len(wanted) and position >= wanted[index]:
                    target = wanted[index]
                    image = frame.to_image()
                    if width and image.width > width:
                        height = round(image.height * width / image.width)
                        image = image.resize((width, height), Image.LANCZOS)
                    path = out_dir / f"{prefix}_{index:02d}.jpg"
                    image.save(path, "JPEG", quality=quality, optimize=True,
                               progressive=True)
                    written.append((position, path))
                    if on_frame is not None:
                        on_frame(index + 1, len(wanted), target, path)
                    index += 1
                if index >= len(wanted):
                    break
    except MediaError:
        raise
    except Exception as exc:
        raise MediaError(f"{video.name}: frame extraction failed ({exc})") from exc

    return written


def build_proxy(source: str | Path, out_path: str | Path, height: int = 720,
                bitrate_kbps: int = 2500,
                on_progress: Callable[[float, float], None] | None = None,
                should_cancel: Callable[[], bool] | None = None) -> Path:
    """Transcode a downscaled playback proxy.

    *on_progress* is called with ``(position_s, duration_s)`` as encoding
    advances. This is the slowest stage in the pipeline by a wide margin, so it
    reports real per-frame position rather than a spinner.
    """
    import av

    source = Path(source)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".part")

    info = probe(source)
    target_h = min(height, info.height) if info.height else height
    target_h -= target_h % 2
    if info.height:
        target_w = round(info.width * target_h / info.height)
    else:
        target_w = round(target_h * 16 / 9)
    target_w -= target_w % 2

    cancelled = False
    try:
        with av.open(str(source)) as src, \
                av.open(str(tmp_path), "w", format=_container_format(out_path)) as dst:
            in_stream = next((s for s in src.streams if s.type == "video"), None)
            if in_stream is None:
                raise MediaError(f"{source.name}: no video stream")
            in_stream.thread_type = "AUTO"

            rate = in_stream.average_rate or fractions.Fraction(30, 1)
            out_stream = dst.add_stream("h264", rate=rate)
            out_stream.width = target_w
            out_stream.height = target_h
            out_stream.pix_fmt = "yuv420p"
            out_stream.bit_rate = bitrate_kbps * 1000
            out_stream.options = {"preset": "veryfast", "profile": "high"}

            audio_in = next((s for s in src.streams if s.type == "audio"), None)
            audio_out = None
            if audio_in is not None:
                # Officials scrubbing footage expect sound; AAC at 96k is free
                # next to the video budget.
                audio_out = dst.add_stream("aac", rate=audio_in.rate)
                audio_out.bit_rate = 96_000

            duration = info.duration_s
            streams = [in_stream] + ([audio_in] if audio_in is not None else [])

            for frame in src.decode(*streams):
                if should_cancel is not None and should_cancel():
                    cancelled = True
                    break
                if frame.__class__.__name__ == "AudioFrame":
                    if audio_out is not None:
                        frame.pts = None
                        for packet in audio_out.encode(frame):
                            dst.mux(packet)
                    continue

                # Read the position before encoding: the encoder rescales the
                # frame's pts into the output stream's time base, so asking
                # afterwards reports nonsense.
                position = frame.time
                frame = frame.reformat(width=target_w, height=target_h, format="yuv420p")
                for packet in out_stream.encode(frame):
                    dst.mux(packet)
                if on_progress is not None and position is not None:
                    on_progress(position, duration)

            for packet in out_stream.encode():
                dst.mux(packet)
            if audio_out is not None:
                for packet in audio_out.encode():
                    dst.mux(packet)
    except MediaError:
        tmp_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        raise MediaError(f"{source.name}: proxy transcode failed ({exc})") from exc

    if cancelled:
        tmp_path.unlink(missing_ok=True)
        raise Cancelled(f"{source.name}: proxy transcode cancelled")

    # Only now does the finished file take its real name, so a crashed or
    # cancelled run never leaves a half-written proxy that looks complete.
    tmp_path.replace(out_path)
    return out_path


def _add_stream_from(container, template):
    """Add an output stream copying *template*'s codec parameters.

    PyAV spelled this ``add_stream(template=...)`` until it grew a dedicated
    ``add_stream_from_template`` in v14 and dropped the keyword in v15. Both
    spellings are tried so the tool is not pinned to one PyAV generation.
    """
    if hasattr(container, "add_stream_from_template"):
        return container.add_stream_from_template(template)
    return container.add_stream(template=template)


def remux_proxy(source: str | Path, out_path: str | Path) -> Path:
    """Stream-copy an .LRV into an .mp4 container.

    GoPro's low-resolution companion is already H.264 in an MP4-alike box, so
    when it is good enough for review there is no reason to spend an hour
    re-encoding: copy the packets and rename the container.

    add_stream(template=...) needs a reasonably modern PyAV; see requirements.txt.
    """
    import av

    source = Path(source)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".part")

    try:
        with av.open(str(source)) as src, \
                av.open(str(tmp_path), "w", format=_container_format(out_path)) as dst:
            mapping = {}
            for stream in src.streams:
                if stream.type in ("video", "audio"):
                    mapping[stream.index] = _add_stream_from(dst, stream)
            if not mapping:
                raise MediaError(f"{source.name}: nothing to remux")
            for packet in src.demux(*[s for s in src.streams if s.index in mapping]):
                if packet.dts is None:
                    continue  # flush packet
                packet.stream = mapping[packet.stream.index]
                dst.mux(packet)
    except MediaError:
        tmp_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        raise MediaError(f"{source.name}: LRV remux failed ({exc})") from exc

    tmp_path.replace(out_path)
    return out_path

