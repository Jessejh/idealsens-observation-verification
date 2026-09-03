"""GPMF (GoPro Metadata Format) KLV parser, GPS extraction and stop detection.

Self-contained: everything below the payload level works on plain bytes, so the
parser is testable against synthetic KLV without a camera. Only
:func:`read_payloads` touches PyAV, and only to demux the ``gpmd`` timed
metadata track out of the MP4.

The GPMF container is a stream of KLV records:

    +--------+--------+--------+--------+
    | FourCC key            (4 bytes)   |
    +--------+--------+-----------------+
    | type   | struct | repeat (uint16) |
    | (1)    | size(1)|                 |
    +--------+--------+-----------------+
    | payload: struct_size * repeat bytes, padded to a 4-byte boundary
    +----------------------------------------------------------------

A ``type`` of NUL means the payload is itself a stream of KLV records; that is
how DEVC (device) and STRM (stream) containers nest.

Why telemetry timing matters: the GPMF track lives inside the video file, so
video-time to UTC has zero clock-sync error. That mapping is what lets an
observation tagged on a phone at 10:31:07 UTC become "14.2 seconds into
GX010042.MP4".
"""

from __future__ import annotations

import math
import struct
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Sequence

__all__ = [
    "GpmfError",
    "KLV",
    "GpsSample",
    "Stop",
    "Telemetry",
    "iter_klv",
    "klv_values",
    "parse_payload",
    "parse_payloads",
    "read_payloads",
    "parse_telemetry",
    "detect_stops",
    "stop_for_offset",
    "utc_to_offset",
    "offset_to_latlon",
    "mean_position",
    "haversine_m",
]


class GpmfError(Exception):
    """Raised when a file has no usable telemetry, or a payload is malformed."""


NESTED = "\x00"

# GPMF type char -> struct format char. Everything in GPMF is big-endian.
_STRUCT_CHARS = {
    "b": "b", "B": "B",
    "s": "h", "S": "H",
    "l": "i", "L": "I",
    "j": "q", "J": "Q",
    "f": "f", "d": "d",
}

# GPS9 counts days and seconds from this instant.
_GPS_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)

# Below this fix quality the camera is reporting a position from its internal
# RTC and dead reckoning rather than a satellite solution. Those samples carry
# timestamps that can be minutes wrong, so they are dropped rather than trusted;
# the resulting observations turn up as unmatched, which is loud, instead of
# silently landing on the wrong second of video.
MIN_FIX = 2

EARTH_RADIUS_M = 6371008.8


# --------------------------------------------------------------------------
# KLV layer
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class KLV:
    key: str
    type: str
    struct_size: int
    repeat: int
    payload: bytes

    @property
    def is_nested(self) -> bool:
        return self.type == NESTED

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"KLV({self.key!r}, type={self.type!r}, "
                f"{self.struct_size}x{self.repeat}, {len(self.payload)}B)")


def iter_klv(data: bytes) -> Iterator[KLV]:
    """Yield the KLV records in *data*, skipping 4-byte alignment padding."""
    pos = 0
    end = len(data)
    while pos + 8 <= end:
        key = data[pos:pos + 4]
        type_char = chr(data[pos + 4])
        struct_size = data[pos + 5]
        repeat = int.from_bytes(data[pos + 6:pos + 8], "big")
        body = pos + 8
        length = struct_size * repeat
        if body + length > end:
            # Truncated tail. Stop rather than raise: a partially written final
            # payload is a real thing on cards pulled mid-record, and the rest
            # of the file is still worth having.
            return
        yield KLV(
            key=key.decode("latin-1"),
            type=type_char,
            struct_size=struct_size,
            repeat=repeat,
            payload=data[body:body + length],
        )
        pos = body + length + (-length % 4)


def _struct_format(item: KLV, complex_type: str | None) -> str | None:
    """Big-endian struct format for one repeat of *item*, or None if not numeric."""
    if item.type == "?":
        if not complex_type:
            raise GpmfError(f"{item.key} is a complex type but the stream has no TYPE")
        try:
            fmt = "".join(_STRUCT_CHARS[c] for c in complex_type)
        except KeyError as exc:
            raise GpmfError(f"TYPE {complex_type!r} has unsupported field {exc}") from None
    else:
        char = _STRUCT_CHARS.get(item.type)
        if char is None:
            return None
        count = item.struct_size // struct.calcsize(char)
        if count < 1:
            return None
        fmt = char * count
    return ">" + fmt


def _parse_gpsu(raw: bytes) -> datetime | None:
    """Parse a GPSU stamp: ``YYMMDDHHMMSS.sss`` in UTC."""
    text = raw.decode("latin-1").strip().strip("\x00").rstrip("Z")
    if len(text) < 12:
        return None
    try:
        year = 2000 + int(text[0:2])
        month = int(text[2:4])
        day = int(text[4:6])
        hour = int(text[6:8])
        minute = int(text[8:10])
        second = int(text[10:12])
        micro = 0
        if len(text) > 13 and text[12] == ".":
            frac = text[13:].ljust(6, "0")[:6]
            micro = int(frac)
        return datetime(year, month, day, hour, minute, second, micro, tzinfo=timezone.utc)
    except ValueError:
        return None


def klv_values(item: KLV, complex_type: str | None = None) -> list:
    """Decode an item's payload.

    Numeric items come back as a list of *repeat* tuples, one per struct. Text
    ('c') comes back as a single string, and UTC stamps ('U') as datetimes.
    """
    if item.type == "c":
        return [item.payload.decode("latin-1").strip().strip("\x00")]
    if item.type == "U":
        size = item.struct_size or 16
        return [_parse_gpsu(item.payload[i:i + size])
                for i in range(0, len(item.payload), size)]
    if item.type == "F":
        return [item.payload[i:i + 4].decode("latin-1")
                for i in range(0, len(item.payload), 4)]

    fmt = _struct_format(item, complex_type)
    if fmt is None:
        return []
    size = struct.calcsize(fmt)
    if size == 0:
        return []
    return [struct.unpack_from(fmt, item.payload, i * size)
            for i in range(len(item.payload) // size)]


def _flat(values: list) -> list[float]:
    out: list[float] = []
    for row in values:
        if isinstance(row, tuple):
            out.extend(row)
        elif row is not None:
            out.append(row)
    return out


# --------------------------------------------------------------------------
# GPS samples
# --------------------------------------------------------------------------

@dataclass
class GpsSample:
    """One GPS fix, positioned on both the video timeline and the UTC clock."""

    offset_s: float
    utc: datetime | None
    lat: float
    lon: float
    alt_m: float
    speed_2d: float
    speed_3d: float
    fix: int
    dop: float


def _scaled(raw: Sequence[float], scal: Sequence[float], index: int) -> float:
    value = float(raw[index])
    if not scal:
        return value
    divisor = scal[index] if index < len(scal) else scal[-1] if len(scal) == 1 else 1.0
    return value / divisor if divisor else value


def _stream_items(data: bytes) -> Iterator[list[KLV]]:
    """Yield each STRM container's items, from a DEVC payload or a bare stream."""
    for item in iter_klv(data):
        if not item.is_nested:
            continue
        if item.key == "STRM":
            yield list(iter_klv(item.payload))
        else:
            # DEVC, or any other wrapper a future model introduces.
            yield from _stream_items(item.payload)


def parse_payload(data: bytes, offset_s: float, duration_s: float,
                  min_fix: int = MIN_FIX) -> list[GpsSample]:
    """Extract GPS samples from one DEVC payload.

    *offset_s* and *duration_s* place the payload on the video timeline; GPS
    samples inside it are spread evenly across that span, which is how the
    camera records them.
    """
    samples: list[GpsSample] = []
    span = duration_s if duration_s and duration_s > 0 else 1.0

    for items in _stream_items(data):
        scal: list[float] = []
        complex_type: str | None = None
        payload_utc: datetime | None = None
        payload_fix = -1
        payload_dop = float("nan")
        gps: KLV | None = None

        for item in items:
            if item.key == "SCAL":
                scal = [v for v in _flat(klv_values(item)) if v]
            elif item.key == "TYPE":
                values = klv_values(item)
                complex_type = values[0] if values else None
            elif item.key == "GPSU":
                values = klv_values(item)
                payload_utc = values[0] if values else None
            elif item.key == "GPSF":
                values = _flat(klv_values(item))
                payload_fix = int(values[0]) if values else -1
            elif item.key == "GPSP":
                values = _flat(klv_values(item))
                # GPSP is dilution of precision x100.
                payload_dop = values[0] / 100.0 if values else float("nan")
            elif item.key in ("GPS5", "GPS9"):
                gps = item

        if gps is None:
            continue

        rows = klv_values(gps, complex_type)
        rows = [r for r in rows if r]
        if not rows:
            continue

        step = span / len(rows)
        for index, row in enumerate(rows):
            t = offset_s + index * step
            lat = _scaled(row, scal, 0)
            lon = _scaled(row, scal, 1)
            alt = _scaled(row, scal, 2)
            speed_2d = _scaled(row, scal, 3)
            speed_3d = _scaled(row, scal, 4) if len(row) > 4 else speed_2d

            if gps.key == "GPS9" and len(row) >= 9:
                # GPS9 is self-describing: days since 2000, seconds into the
                # day, DOP and fix quality all travel with each sample.
                days = _scaled(row, scal, 5)
                secs = _scaled(row, scal, 6)
                utc = _GPS_EPOCH + timedelta(days=days, seconds=secs)
                dop = _scaled(row, scal, 7)
                fix = int(_scaled(row, scal, 8))
            else:
                # GPS5 carries one GPSU/GPSF/GPSP for the whole payload.
                utc = payload_utc + timedelta(seconds=index * step) if payload_utc else None
                dop = payload_dop
                fix = payload_fix

            if fix >= 0 and fix < min_fix:
                continue
            if lat == 0.0 and lon == 0.0:
                continue

            samples.append(GpsSample(
                offset_s=t, utc=utc, lat=lat, lon=lon, alt_m=alt,
                speed_2d=speed_2d, speed_3d=speed_3d, fix=fix, dop=dop,
            ))

    return samples


def parse_payloads(payloads: Iterable[tuple[float, float, bytes]],
                   min_fix: int = MIN_FIX) -> list[GpsSample]:
    """Parse a sequence of ``(offset_s, duration_s, data)`` payloads."""
    samples: list[GpsSample] = []
    for offset_s, duration_s, data in payloads:
        samples.extend(parse_payload(data, offset_s, duration_s, min_fix=min_fix))
    samples.sort(key=lambda s: s.offset_s)
    return samples


# --------------------------------------------------------------------------
# Reading payloads out of an MP4
# --------------------------------------------------------------------------

def read_payloads(path: str | Path) -> list[tuple[float, float, bytes]]:
    """Demux the GPMF track, returning ``(offset_s, duration_s, data)`` payloads.

    PyAV only — never an ffmpeg binary. AppLocker blocks executables on the
    operator's machine, and PyAV ships FFmpeg as linked libraries.
    """
    import av  # imported lazily so the parser stays unit-testable without PyAV

    grouped: dict[int, list[tuple[float | None, float | None, bytes]]] = {}
    with av.open(str(path)) as container:
        candidates = [s for s in container.streams if s.type == "data"]
        if not candidates:
            raise GpmfError(f"{Path(path).name}: no data streams — not a GoPro file?")

        # Prefer the track whose handler names it, but keep the rest as
        # fallbacks: the DEVC magic below is the real test.
        candidates.sort(
            key=lambda s: "gopro met" not in (s.metadata.get("handler_name", "") or "").lower()
        )

        for packet in container.demux(candidates):
            data = bytes(packet)
            if not data:
                continue  # flush packet at end of stream
            time_base = float(packet.time_base or packet.stream.time_base or 0) or None
            pts = packet.pts
            offset = float(pts) * time_base if pts is not None and time_base else None
            duration = (float(packet.duration) * time_base
                        if packet.duration and time_base else None)
            grouped.setdefault(packet.stream.index, []).append((offset, duration, data))

    for index in sorted(grouped, key=lambda i: 0 if grouped[i][0][2][:4] == b"DEVC" else 1):
        entries = grouped[index]
        if entries[0][2][:4] != b"DEVC":
            continue
        return _fill_timing(entries)

    raise GpmfError(f"{Path(path).name}: no GPMF (DEVC) telemetry track found")


def _fill_timing(entries: list[tuple[float | None, float | None, bytes]]
                 ) -> list[tuple[float, float, bytes]]:
    """Supply offsets/durations for payloads the demuxer did not timestamp.

    GoPro emits one payload per ~1 s of recording, so falling back to a
    uniform 1 s cadence is close enough to keep the file usable.
    """
    out: list[tuple[float, float, bytes]] = []
    cursor = 0.0
    for i, (offset, duration, data) in enumerate(entries):
        start = offset if offset is not None else cursor
        if duration is None:
            nxt = entries[i + 1][0] if i + 1 < len(entries) else None
            duration = (nxt - start) if nxt is not None and nxt > start else 1.0
        out.append((start, duration, data))
        cursor = start + duration
    return out


@dataclass
class Telemetry:
    """Everything the pipeline needs to know about one file's telemetry."""

    samples: list[GpsSample]
    payload_count: int
    dropped_samples: int
    device: str | None = None
    # GPS9 (HERO11 and later) carries UTC, DOP and fix quality per sample;
    # GPS5 carries one GPSU stamp for the whole payload. Both take their time
    # from the satellites, so neither is affected by the camera's timezone.
    kind: str = ""

    @property
    def duration_s(self) -> float:
        return self.samples[-1].offset_s if self.samples else 0.0

    @property
    def first_utc(self) -> datetime | None:
        for s in self.samples:
            if s.utc:
                return s.utc
        return None

    @property
    def last_utc(self) -> datetime | None:
        for s in reversed(self.samples):
            if s.utc:
                return s.utc
        return None


def parse_telemetry(path: str | Path, min_fix: int = MIN_FIX) -> Telemetry:
    """Read and parse the telemetry of one GoPro file."""
    payloads = read_payloads(path)
    samples = parse_payloads(payloads, min_fix=min_fix)
    total = len(parse_payloads(payloads, min_fix=0)) if min_fix > 0 else len(samples)
    device = None
    kind = ""
    for _, _, data in payloads[:4]:
        for items in _stream_items(data):
            for item in items:
                if item.key in ("GPS9", "GPS5"):
                    kind = item.key
                    break
            if kind:
                break
        if kind:
            break
    if payloads:
        for item in iter_klv(payloads[0][2]):
            if item.key == "DEVC" and item.is_nested:
                for sub in iter_klv(item.payload):
                    if sub.key == "DVNM":
                        values = klv_values(sub)
                        device = values[0] if values else None
                break
    if not samples:
        raise GpmfError(
            f"{Path(path).name}: telemetry track has no GPS fixes at quality >= {min_fix}. "
            "The camera probably never got a satellite lock for this chapter."
        )
    return Telemetry(samples=samples, payload_count=len(payloads),
                     dropped_samples=max(0, total - len(samples)), device=device,
                     kind=kind)


# --------------------------------------------------------------------------
# Geometry and time mapping
# --------------------------------------------------------------------------

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def _time_fit(samples: Sequence[GpsSample]) -> tuple[datetime, float, float] | None:
    """Least-squares fit of video offset against UTC.

    Both clocks are the camera's, so the true relationship is a straight line
    with slope 1. Fitting it lets a UTC instant map onto a video offset even
    where the GPS dropped out for a stretch, which local interpolation cannot
    do.
    """
    usable = [s for s in samples if s.utc]
    if len(usable) < 2:
        return None
    base = usable[0].utc
    xs = [(s.utc - base).total_seconds() for s in usable]
    ys = [s.offset_s for s in usable]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom
    return base, slope, mean_y - slope * mean_x


def utc_to_offset(samples: Sequence[GpsSample], utc: datetime,
                  max_gap_s: float = 5.0, tolerance_s: float = 1.0) -> float | None:
    """Map a UTC instant to an offset into the video, or None if it is outside.

    Between neighbouring fixes this interpolates. Across a GPS dropout wider
    than *max_gap_s* it falls back to the whole-file linear fit, because a
    missing position does not mean missing time.
    """
    usable = [s for s in samples if s.utc]
    if not usable:
        return None

    first, last = usable[0], usable[-1]
    if utc < first.utc - timedelta(seconds=tolerance_s):
        return None
    if utc > last.utc + timedelta(seconds=tolerance_s):
        return None

    times = [s.utc for s in usable]
    i = bisect_left(times, utc)
    if i == 0:
        return usable[0].offset_s + (utc - first.utc).total_seconds()
    if i >= len(usable):
        return usable[-1].offset_s + (utc - last.utc).total_seconds()

    lo, hi = usable[i - 1], usable[i]
    gap = (hi.utc - lo.utc).total_seconds()
    if gap <= max_gap_s:
        if gap <= 0:
            return lo.offset_s
        frac = (utc - lo.utc).total_seconds() / gap
        return lo.offset_s + frac * (hi.offset_s - lo.offset_s)

    fit = _time_fit(usable)
    if fit is None:
        return lo.offset_s
    base, slope, intercept = fit
    return slope * (utc - base).total_seconds() + intercept


def offset_to_latlon(samples: Sequence[GpsSample], offset_s: float,
                     max_gap_s: float = 5.0) -> tuple[float, float] | None:
    """Interpolate a position at *offset_s*, or None if telemetry does not cover it."""
    if not samples:
        return None
    offsets = [s.offset_s for s in samples]
    if offset_s < offsets[0] - max_gap_s or offset_s > offsets[-1] + max_gap_s:
        return None

    i = bisect_left(offsets, offset_s)
    if i == 0:
        return samples[0].lat, samples[0].lon
    if i >= len(samples):
        return samples[-1].lat, samples[-1].lon

    lo, hi = samples[i - 1], samples[i]
    span = hi.offset_s - lo.offset_s
    if span <= 0 or span > max_gap_s:
        nearer = lo if abs(offset_s - lo.offset_s) <= abs(hi.offset_s - offset_s) else hi
        return nearer.lat, nearer.lon
    frac = (offset_s - lo.offset_s) / span
    return lo.lat + frac * (hi.lat - lo.lat), lo.lon + frac * (hi.lon - lo.lon)


def mean_position(samples: Sequence[GpsSample], start_s: float,
                  end_s: float) -> tuple[float, float, int] | None:
    """Mean position of the fixes inside a window, with the count that fed it."""
    inside = [s for s in samples if start_s <= s.offset_s <= end_s]
    if not inside:
        return None
    return (sum(s.lat for s in inside) / len(inside),
            sum(s.lon for s in inside) / len(inside),
            len(inside))


# --------------------------------------------------------------------------
# Stop detection
# --------------------------------------------------------------------------

@dataclass
class Stop:
    """A stationary period, found from GPS speed."""

    index: int
    start_s: float
    end_s: float
    lat: float
    lon: float
    sample_count: int

    @property
    def mid_s(self) -> float:
        return (self.start_s + self.end_s) / 2.0

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


def detect_stops(samples: Sequence[GpsSample], speed_threshold: float = 0.7,
                 min_duration_s: float = 3.0, max_gap_s: float = 2.0) -> list[Stop]:
    """Find stationary periods.

    The operator stopped the scooter and framed each target on the camera
    screen before tagging it, so the target is visible for the whole stationary
    period. That is why frame windows follow the detected stop rather than a
    fixed span either side of the tag.

    *max_gap_s* tolerates the odd sample that jitters above the threshold while
    parked — GPS noise at standstill is routinely a metre per second — without
    splitting one stop into three.
    """
    stops: list[Stop] = []
    n = len(samples)
    i = 0
    while i < n:
        if samples[i].speed_2d > speed_threshold:
            i += 1
            continue

        last_slow = i
        k = i + 1
        while k < n:
            if samples[k].speed_2d <= speed_threshold:
                last_slow = k
                k += 1
            elif samples[k].offset_s - samples[last_slow].offset_s <= max_gap_s:
                k += 1
            else:
                break

        run = [s for s in samples[i:last_slow + 1] if s.speed_2d <= speed_threshold]
        start_s = samples[i].offset_s
        end_s = samples[last_slow].offset_s
        if run and (end_s - start_s) >= min_duration_s:
            stops.append(Stop(
                index=len(stops),
                start_s=start_s,
                end_s=end_s,
                lat=sum(s.lat for s in run) / len(run),
                lon=sum(s.lon for s in run) / len(run),
                sample_count=len(run),
            ))
        i = last_slow + 1

    return stops


def stop_for_offset(stops: Sequence[Stop], offset_s: float,
                    tolerance_s: float = 2.0) -> Stop | None:
    """The stop covering *offset_s*, allowing *tolerance_s* either side.

    A tag can land just outside the detected stop — the operator often taps the
    phone as the scooter is rolling away again — so the window is widened
    slightly before giving up. Where two stops both qualify, the one whose
    middle is nearest wins.
    """
    matches = [s for s in stops
               if s.start_s - tolerance_s <= offset_s <= s.end_s + tolerance_s]
    if not matches:
        return None
    return min(matches, key=lambda s: abs(s.mid_s - offset_s))
