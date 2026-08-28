"""Build a synthetic GoPro-like MP4 with a real GPMF telemetry track.

PyAV cannot mux an arbitrary timed-metadata track, so the video and the
telemetry are produced as a matched pair: a real MP4 alongside the payload list
its GPMF track would have yielded. `patch_read_payloads` swaps that list in, so
the pipeline exercises real decoding, real frame extraction and real transcoding
against telemetry it believes came from the file.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

from tests import klvfixtures as fx

UTC = timezone.utc


def drive_plan(start_utc: datetime, stops_at: list[tuple[float, float]],
               duration_s: float, hz: int = 1):
    """Positions and speeds for a drive that halts at each (start, end) in stops_at."""
    rows = []
    lat, lon = 60.17000, 24.94000
    step = 1.0 / hz
    t = 0.0
    while t < duration_s:
        moving = not any(start <= t < end for start, end in stops_at)
        speed = 4.5 if moving else 0.05
        rows.append((t, start_utc + timedelta(seconds=t), lat, lon, speed))
        if moving:
            lat += 4.0e-5 * step   # roughly 4.5 m/s northwards
        t += step
    return rows


def telemetry_payloads(rows, hz: int = 1):
    """Group per-second GPS rows into the one-payload-per-second GoPro writes."""
    payloads = []
    per_payload = max(1, hz)
    for start in range(0, len(rows), per_payload):
        group = rows[start:start + per_payload]
        offset_s = group[0][0]
        stamp = group[0][1].strftime("%y%m%d%H%M%S.") + f"{group[0][1].microsecond // 1000:03d}"
        gps_rows = [(lat, lon, 15.0, speed, speed) for _, _, lat, lon, speed in group]
        payloads.append((offset_s, 1.0, fx.gps5_payload(gps_rows, stamp=stamp)))
    return payloads


def write_clip(path: Path, duration_s: float, fps: int = 30,
               width: int = 960, height: int = 540) -> Path:
    """A small real MP4 — kept modest so the suite stays quick."""
    import av
    import numpy as np

    path.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(path), "w") as container:
        stream = container.add_stream("h264", rate=fps)
        stream.width, stream.height, stream.pix_fmt = width, height, "yuv420p"
        stream.bit_rate = 2_000_000
        stream.options = {"preset": "ultrafast", "g": "15"}
        total = int(duration_s * fps)
        for i in range(total):
            image = np.zeros((height, width, 3), dtype=np.uint8)
            image[:, :, 0] = (i * 5) % 256
            image[:, :, 1] = 60
            bar = int((i / max(1, total)) * width)
            image[height // 2 - 30:height // 2 + 30, max(0, bar - 20):bar + 20] = 255
            frame = av.VideoFrame.from_ndarray(image, format="rgb24")
            frame.pts = i
            frame.time_base = Fraction(1, fps)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return path


@contextlib.contextmanager
def patch_read_payloads(payloads):
    """Make gpmf.read_payloads return *payloads* for any file."""
    from curbtool import gpmf

    original = gpmf.read_payloads
    gpmf.read_payloads = lambda path: list(payloads)
    try:
        yield
    finally:
        gpmf.read_payloads = original
