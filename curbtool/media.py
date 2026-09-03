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

# Frame extraction walks the file forwards, seeking only when the next frame it
# wants is far enough ahead to be worth a keyframe re-sync. A seek costs about
# SEEK_PREROLL_S of re-decoded video plus latency — roughly 1.5 s of decode
# equivalent — so the threshold sits at twice that. Below it, decoding through
# is cheaper; measured, a naive linear pass over the whole file is slower than
# the per-observation seeking it would replace.
SEEK_GAP_S = 3.0
SEEK_PREROLL_S = 1.0


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
    # Clockwise degrees needed to show the picture the right way up; see
    # display_rotation(). width/height stay as the frames are *stored*.
    rotation: int = 0

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def display_size(self) -> tuple[int, int]:
        """Width and height as a viewer sees them, once rotation is applied."""
        if self.rotation in (90, 270):
            return self.height, self.width
        return self.width, self.height


def _normalise_rotation(degrees) -> int:
    """Round a rotation to the nearest quarter turn, or 0 if it is not one."""
    try:
        value = int(round(float(degrees))) % 360
    except (TypeError, ValueError):
        return 0
    return value if value in (90, 180, 270) else 0


def display_rotation(frame=None, stream=None) -> int:
    """Clockwise degrees needed to show the picture the right way up.

    A GoPro mounted upside down records upside-down pixels and writes a
    display matrix saying so. Every player honours that matrix; a decoder does
    not. So frames pulled straight out of PyAV come out inverted, and the
    rotation has to be applied by hand — this is the value to apply.

    ``VideoFrame.rotation`` is FFmpeg's ``av_display_rotation_get``: the
    counter-clockwise angle the *matrix* turns the frame through. What a
    viewer needs is the opposite, which is also what FFmpeg's own ``rotate``
    tag and ffplay's ``get_rotation`` report — hence the negation.

    The stream-level ``rotate`` tag is only a fallback: modern FFmpeg exports
    the matrix as frame side data and stops writing the tag, and older PyAV
    (before ``VideoFrame.rotation`` existed) only had the tag. Between them
    every supported version is covered.
    """
    if frame is not None:
        raw = getattr(frame, "rotation", None)
        if raw:
            return _normalise_rotation(-float(raw))
    if stream is not None:
        try:
            tag = stream.metadata.get("rotate")
        except Exception:
            tag = None
        if tag:
            return _normalise_rotation(tag)
    return 0


# Tried in order when proxy_encoder is "auto". PyAV's bundled FFmpeg has all
# of these compiled in; whether one actually opens depends on the GPU and
# drivers present, which is why they are probed rather than assumed.
HARDWARE_ENCODERS = ("h264_qsv", "h264_nvenc", "h264_amf", "h264_videotoolbox")
_ENCODER_CACHE: dict[str, str] = {}


def available_encoder(preference: str = "auto") -> tuple[str, dict]:
    """The best H.264 encoder this machine will actually open, and its options.

    Probed once and cached. Hardware encoding is several times faster than
    libx264 and costs nothing when it works, but a codec being compiled in is
    not the same as a GPU being present — so each candidate is opened for real
    and the first that succeeds wins.

    Returns ``(codec_name, options)``. Falls back to libx264 silently.
    """
    if preference and preference not in ("auto", "software"):
        return preference, {}
    if preference == "software":
        return "h264", {"preset": "veryfast", "profile": "high"}
    if "auto" in _ENCODER_CACHE:
        name = _ENCODER_CACHE["auto"]
        return name, ({} if name != "h264" else {"preset": "veryfast", "profile": "high"})

    import av

    chosen = "h264"
    for candidate in HARDWARE_ENCODERS:
        try:
            codec = av.Codec(candidate, "w")
        except Exception:
            continue
        # Opening a real context is the only honest test: the codec can be
        # present and still fail for want of a device.
        try:
            context = av.codec.context.CodecContext.create(codec, "w")
            context.width, context.height = 640, 360
            context.pix_fmt = "yuv420p"
            context.time_base = fractions.Fraction(1, 30)
            context.open()
            context.close()
        except Exception:
            continue
        chosen = candidate
        break

    _ENCODER_CACHE["auto"] = chosen
    return chosen, ({} if chosen != "h264" else {"preset": "veryfast", "profile": "high"})


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
                rotation=_probe_rotation(container, stream),
            )
    except MediaError:
        raise
    except Exception as exc:  # PyAV raises a family of errors; treat them alike
        raise MediaError(f"{path.name}: cannot open ({exc})") from exc


def _probe_rotation(container, stream) -> int:
    """The display rotation, decoding one frame for it if the tag is absent.

    FFmpeg carries the display matrix as *frame* side data, so on a modern
    build there is no way to read it without decoding — and build_proxy has to
    size its output stream before it decodes anything. One keyframe is a few
    milliseconds against a transcode measured in minutes.

    A file that will not decode is not this function's problem to report:
    probe() answers geometry, and the real decode raises with a better message.
    """
    tag = display_rotation(stream=stream)
    if tag:
        return tag
    try:
        for frame in container.decode(stream):
            return display_rotation(frame=frame)
    except Exception:
        return 0
    return 0


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


def _to_jpeg_image(frame, width: int, rotation: int = 0):
    """An RGB image, downscaled by swscale rather than by PIL, turned upright.

    ``to_image()`` converts the whole plane to RGB before PIL ever sees it, and
    a PIL LANCZOS resize of a 4K RGB image costs ~200 ms. Handing the target
    size to ``to_image`` instead — it forwards keyword arguments to reformat
    and forces rgb24 itself — fuses the scale and the colour conversion into
    one C pass: ~9 ms for the same frame, measured.

    *width* is the width of the **finished** image, so a camera mounted on its
    side still yields the frame width that was asked for; the scale is worked
    out in display orientation and applied in stored orientation.

    The width guard matters: without it the default 1920 would *upscale* a
    960-wide .LRV.

    The rotation is passed to PIL as a positive counter-clockwise angle
    because ``Image.rotate`` only takes its transpose fast path for exactly
    90, 180 and 270 — ``rotate(-180)`` would resample the whole image for a
    turn that is a memory copy.
    """
    display_w = frame.height if rotation in (90, 270) else frame.width
    if width and display_w > width:
        scale = width / display_w
        image = frame.to_image(width=max(2, round(frame.width * scale)),
                               height=max(2, round(frame.height * scale)),
                               interpolation="LANCZOS")
    else:
        image = frame.to_image()
    if rotation:
        image = image.rotate((360 - rotation) % 360, expand=True)
    return image


@dataclass
class FrameRequest:
    """One observation's worth of frames: where to cut, and where to write."""

    targets: Sequence[float]
    out_dir: Path
    prefix: str = "frame"
    # False writes "<prefix>.jpg" instead of "<prefix>_00.jpg", for the
    # one-image-per-observation mode where the sequence number carries no
    # information and only gets in the way of matching a file to a CSV row.
    numbered: bool = True

    def path_for(self, seq: int) -> Path:
        name = f"{self.prefix}_{seq:02d}.jpg" if self.numbered else f"{self.prefix}.jpg"
        return Path(self.out_dir) / name


def _plan_segments(requests: Sequence[FrameRequest],
                   seek_gap_s: float) -> list[list[tuple[float, int, int]]]:
    """Cut every request's targets into runs that are worth decoding through.

    Segmentation is done on the *flattened* target list rather than per
    observation, which handles both awkward cases at once: windows that overlap
    merge into one run, and a long stop whose nine frames are spread ten
    seconds apart stops being decoded end to end.
    """
    items: list[tuple[float, int, int]] = []
    for request_index, request in enumerate(requests):
        for seq, target in enumerate(sorted(t for t in request.targets if t >= 0)):
            items.append((target, request_index, seq))
    items.sort()

    segments: list[list[tuple[float, int, int]]] = []
    for item in items:
        if segments and item[0] - segments[-1][-1][0] <= seek_gap_s:
            segments[-1].append(item)      # decoding through is cheaper than a seek
        else:
            segments.append([item])
    return segments


def _seek(container, stream, seconds: float, time_base: float) -> bool:
    """Seek, reporting failure rather than raising.

    A file whose index will not seek should still yield correct frames — just
    more slowly, by decoding onwards from wherever we are.
    """
    try:
        container.seek(int(seconds / time_base), stream=stream, backward=True)
        return True
    except Exception:
        return False


def _extract_pass(video: Path, requests: Sequence[FrameRequest], width: int,
                  quality: int, optimize: bool, progressive: bool,
                  seek_gap_s: float, on_frame, should_cancel
                  ) -> tuple[list[list[tuple[float, Path]]], bool]:
    """One open, one forward decode, every request's frames written."""
    import av

    counts = []
    for request in requests:
        Path(request.out_dir).mkdir(parents=True, exist_ok=True)
        counts.append(sum(1 for t in request.targets if t >= 0))
    # Pre-allocated per request and keyed by seq, so a request's frames come
    # back in time order however the segments interleave, and the filenames
    # stay f_00.jpg, f_01.jpg … for _existing_frames to find on a backfill.
    slots: list[list[tuple[float, Path] | None]] = [[None] * n for n in counts]
    total = sum(counts)

    segments = _plan_segments(requests, seek_gap_s)
    if not segments:
        return [[] for _ in requests], False

    done = 0
    cancelled = False
    try:
        with av.open(str(video)) as container:
            stream = next((s for s in container.streams if s.type == "video"), None)
            if stream is None:
                raise MediaError(f"{video.name}: no video stream")
            stream.thread_type = "AUTO"
            time_base = float(stream.time_base) if stream.time_base else 1 / 30.0

            frames = None                   # the live decode generator
            position = -math.inf            # time of the last decoded frame
            # Resolved from the first frame actually saved, then reused: the
            # display matrix is per-frame side data, but it does not change
            # within a chapter, and reading it is not free.
            rotation: int | None = display_rotation(stream=stream) or None

            for segment in segments:
                if cancelled:
                    break
                first = segment[0][0]
                # Forward only, and only when decoding through would cost more
                # than a keyframe re-sync. Seeking backwards could loop.
                if frames is None or first - position > seek_gap_s:
                    if _seek(container, stream, max(0.0, first - SEEK_PREROLL_S),
                             time_base):
                        frames = container.decode(stream)
                        position = -math.inf
                    elif frames is None:
                        frames = container.decode(stream)

                index = 0
                for frame in frames:
                    if should_cancel is not None and should_cancel():
                        cancelled = True
                        break
                    at = frame.time
                    if at is None:
                        continue
                    position = at
                    if at < segment[index][0]:
                        continue            # nothing due yet: the cheap path
                    image = None
                    while index < len(segment) and at >= segment[index][0]:
                        target, request_index, seq = segment[index]
                        # One frame can satisfy several targets, including
                        # targets belonging to different observations. Scale it
                        # once and save it as many times as it is wanted.
                        if image is None:
                            if rotation is None:
                                rotation = display_rotation(frame=frame, stream=stream)
                            image = _to_jpeg_image(frame, width, rotation)
                        request = requests[request_index]
                        path = request.path_for(seq)
                        image.save(path, "JPEG", quality=quality,
                                   optimize=optimize, progressive=progressive)
                        slots[request_index][seq] = (at, path)
                        done += 1
                        if on_frame is not None:
                            on_frame(request_index, done, total, target, path)
                        index += 1
                    if index >= len(segment):
                        break               # keep the generator for the next segment
    except MediaError:
        raise
    except Exception as exc:
        # errors.py keys E302 on the words "frame extraction" — keep them.
        raise MediaError(f"{video.name}: frame extraction failed ({exc})") from exc

    return [[hit for hit in per_request if hit is not None] for per_request in slots], cancelled


def extract_frames_multi(video: str | Path, requests: Sequence[FrameRequest],
                         width: int = 1920, quality: int = 88,
                         optimize: bool = True, progressive: bool = False,
                         seek_gap_s: float = SEEK_GAP_S,
                         on_frame: Callable[[int, int, int, float, Path], None] | None = None,
                         should_cancel: Callable[[], bool] | None = None
                         ) -> list[list[tuple[float, Path]]]:
    """Cut every request's frames in one open and one forward decode.

    Called once per chapter rather than once per observation: a chapter with
    twenty observations used to open and partially decode the same 3 GB file
    twenty times.

    Returns one ``[(actual_offset_s, path), ...]`` list per request, in the
    order the requests were given, each in time order. Raises :class:`Cancelled`
    rather than returning early, so a cancel during the last observation stops
    the run rather than passing quietly.
    """
    video = Path(video)
    results, cancelled = _extract_pass(video, requests, width, quality, optimize,
                                       progressive, seek_gap_s, on_frame, should_cancel)
    if cancelled:
        raise Cancelled(f"{video.name}: frame extraction cancelled")
    return results


def extract_frames(video: str | Path, targets: Sequence[float], out_dir: str | Path,
                   prefix: str = "frame", width: int = 1920, quality: int = 88,
                   optimize: bool = True, progressive: bool = False,
                   on_frame: Callable[[int, int, float, Path], None] | None = None,
                   should_cancel: Callable[[], bool] | None = None) -> list[tuple[float, Path]]:
    """Cut JPEGs from *video* at each offset in *targets*.

    The single-observation entry point, kept for callers that only want one
    window. Returns ``(actual_offset_s, path)`` for each frame written, in time
    order, and returns what it managed on a cancel rather than raising.
    """
    adapted = None
    if on_frame is not None:
        def adapted(_request_index, done, total, target, path):
            on_frame(done, total, target, path)

    results, _cancelled = _extract_pass(
        Path(video), [FrameRequest(targets, Path(out_dir), prefix)],
        width=width, quality=quality, optimize=optimize, progressive=progressive,
        seek_gap_s=SEEK_GAP_S, on_frame=adapted, should_cancel=should_cancel)
    return results[0]


def build_proxy(source: str | Path, out_path: str | Path, height: int = 720,
                bitrate_kbps: int = 2500, encoder: str = "auto",
                fps: int = 0, audio: bool = False, info: "VideoInfo | None" = None,
                on_progress: Callable[[float, float], None] | None = None,
                should_cancel: Callable[[], bool] | None = None) -> str:
    """Transcode a downscaled playback proxy. Returns the encoder used.

    Still the slowest thing in the pipeline by a wide margin, so it is worth
    the knobs: *encoder* picks hardware where the machine has it, *fps* caps the
    output frame rate, and *audio* is off by default — a reviewer scrubbing for
    kerb damage does not need scooter noise, and decoding and re-encoding the
    AAC track for a whole chapter is not free.

    *on_progress* gets ``(position_s, duration_s)``, throttled: it used to fire
    once per frame, which is ~14,000 dataclass allocations on a 4K chapter.
    """
    import av

    source = Path(source)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".part")

    # The caller has usually probed already; probing again reopens the file for
    # nothing.
    info = info or probe(source)
    # Sized in display orientation, so "720p" means 720 lines as a viewer sees
    # them even when the camera was mounted on its side.
    display_w, display_h = info.display_size
    target_h = min(height, display_h) if display_h else height
    target_h -= target_h % 2
    if display_h:
        target_w = round(display_w * target_h / display_h)
    else:
        target_w = round(target_h * 16 / 9)
    target_w -= target_w % 2

    rotation = info.rotation
    # The frames are scaled before they are turned, so swscale is given the
    # size in stored orientation.
    if rotation in (90, 270):
        scale_w, scale_h = target_h, target_w
    else:
        scale_w, scale_h = target_w, target_h

    cancelled = False
    used_encoder = "h264"
    try:
        with av.open(str(source)) as src, \
                av.open(str(tmp_path), "w", format=_container_format(out_path)) as dst:
            in_stream = next((s for s in src.streams if s.type == "video"), None)
            if in_stream is None:
                raise MediaError(f"{source.name}: no video stream")
            in_stream.thread_type = "AUTO"
            in_stream.codec_context.thread_count = 0     # 0 = one per core

            source_rate = in_stream.average_rate or fractions.Fraction(30, 1)
            rate = fractions.Fraction(fps, 1) if fps else source_rate
            codec_name, codec_options = available_encoder(encoder)
            used_encoder = codec_name
            out_stream = dst.add_stream(codec_name, rate=rate)
            # Copying the source's display matrix onto the output costs nothing
            # and every player honours it. Only where PyAV is too old to write
            # one does the rotation get baked into the pixels, which means an
            # RGB round trip per frame — correct, but noticeably slower, so it
            # is the fallback rather than the rule.
            bake_rotation = bool(rotation)
            if rotation and hasattr(out_stream, "set_display_rotation"):
                # Negated back into the counter-clockwise convention the
                # matrix itself uses; see display_rotation().
                out_stream.set_display_rotation(-rotation)
                bake_rotation = False
            if bake_rotation:
                out_stream.width, out_stream.height = target_w, target_h
            else:
                out_stream.width, out_stream.height = scale_w, scale_h
            out_stream.pix_fmt = "yuv420p"
            out_stream.bit_rate = bitrate_kbps * 1000
            if codec_options:
                out_stream.options = codec_options
            # The decoder was already threaded; the encoder was not. libx264
            # defaults to slice threading, and frame threading is measurably
            # faster across cores.
            out_stream.codec_context.thread_type = "AUTO"
            out_stream.codec_context.thread_count = 0

            audio_in = next((s for s in src.streams if s.type == "audio"), None)
            audio_out = None
            if audio and audio_in is not None:
                audio_out = dst.add_stream("aac", rate=audio_in.rate)
                audio_out.bit_rate = 96_000

            duration = info.duration_s
            # Only decode audio if it is going somewhere.
            streams = [in_stream] + ([audio_in] if audio_out is not None else [])
            frame_step = float(source_rate) / float(rate) if fps else 1.0
            next_wanted = 0.0
            seen = 0
            kept = 0
            last_report = -1.0

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
                seen += 1
                if seen - 1 < next_wanted:
                    continue                    # dropped to reach the target fps
                next_wanted += frame_step

                if bake_rotation:
                    import av as _av
                    # target_w, not scale_w: _to_jpeg_image sizes the finished
                    # picture, after the turn.
                    image = _to_jpeg_image(frame, target_w, rotation)
                    turned = _av.VideoFrame.from_image(image)
                    turned.pts, turned.time_base = frame.pts, frame.time_base
                    frame = turned.reformat(format="yuv420p")
                else:
                    frame = frame.reformat(width=scale_w, height=scale_h,
                                           format="yuv420p")
                if fps:
                    # Dropping frames leaves gaps in the original timestamps, so
                    # the kept ones are renumbered at the output rate. Left alone
                    # the proxy would play at the source speed with stutters.
                    frame.pts = kept
                    frame.time_base = fractions.Fraction(1, int(rate))
                kept += 1
                for packet in out_stream.encode(frame):
                    dst.mux(packet)
                if (on_progress is not None and position is not None
                        and position - last_report >= 0.5):
                    last_report = position
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
    return used_encoder


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

