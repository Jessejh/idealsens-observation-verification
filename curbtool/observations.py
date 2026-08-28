"""Loaders for the phone tagging app's observation CSV and the phone GNSS log.

The exact column names the tagging app exports are not pinned down, so both
loaders detect columns from a list of aliases and let the caller override any
of them. A CSV that cannot be understood fails loudly at load time with the
headers it did find, rather than silently matching nothing.

European exports are handled too: semicolon delimiters and decimal commas both
turn up in Finnish tooling, and a file where latitude reads ``60,1701`` must
not parse as the integer 60.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence

UTC = timezone.utc

TIME_ALIASES = ("utc", "timestamp", "time", "datetime", "date_time", "recorded_at",
                "created_at", "tagged_at", "observed_at", "aika", "aikaleima")
LAT_ALIASES = ("lat", "latitude", "y", "wgs84_lat", "leveysaste")
LON_ALIASES = ("lon", "lng", "long", "longitude", "x", "wgs84_lon", "pituusaste")
CATEGORY_ALIASES = ("category", "type", "tag", "class", "label", "kind", "luokka", "tyyppi")
NOTE_ALIASES = ("note", "notes", "description", "comment", "comments", "remark",
                "huomio", "kuvaus")
ID_ALIASES = ("id", "uuid", "external_id", "observation_id", "obs_id", "point_id", "tunnus")
ACCURACY_ALIASES = ("accuracy", "accuracy_m", "hacc", "horizontal_accuracy", "acc",
                    "precision", "tarkkuus")


class ObservationError(Exception):
    """Raised when a CSV cannot be read as observations."""


@dataclass
class Observation:
    """One tag dropped by the operator, before it is matched to any footage."""

    external_id: str
    utc: datetime
    lat: float | None
    lon: float | None
    category: str = ""
    note: str = ""
    row_number: int = 0
    raw: dict = field(default_factory=dict)


@dataclass
class PhoneFix:
    """One position from the phone's GNSS log."""

    utc: datetime
    lat: float
    lon: float
    accuracy_m: float | None = None


# --------------------------------------------------------------------------
# Value parsing
# --------------------------------------------------------------------------

# Normalise the fractional seconds of an ISO stamp to the 6 digits
# fromisoformat accepts. Anchored on the time so a dotted date such as
# 01.06.2024 keeps its year.
_FRACTION = re.compile(r"(\d{1,2}:\d{2}:\d{2})[.,](\d+)")


def _normalise_fraction(match: re.Match) -> str:
    return f"{match.group(1)}.{match.group(2)[:6].ljust(6, '0')}"


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse a timestamp into an aware UTC datetime.

    Accepts ISO 8601 (with ``T`` or a space, with ``Z``, an offset, or neither)
    and epoch seconds/milliseconds. A timestamp with no zone is taken as UTC —
    if the tagging app actually exported local time the whole campaign will be
    off by a constant, which is what ``--clock-offset`` exists to correct, and
    what :func:`suggest_clock_offset` exists to spot.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    if re.fullmatch(r"-?\d{9,13}(\.\d+)?", text):
        number = float(text)
        if abs(number) > 1e11:  # milliseconds
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None

    candidate = text.replace("/", "-")
    if candidate.endswith("Z") or candidate.endswith("z"):
        candidate = candidate[:-1] + "+00:00"
    candidate = _FRACTION.sub(_normalise_fraction, candidate)
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                    "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M",
                    "%Y%m%d %H%M%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                parsed = datetime.strptime(candidate, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def parse_number(value: str | None) -> float | None:
    """Parse a float, tolerating a decimal comma and thousands spaces."""
    if value is None:
        return None
    text = str(value).strip().replace(" ", "").replace(" ", "")
    if not text:
        return None
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    elif "," in text and "." in text:
        # 1.234,56 -> 1234.56 ; 1,234.56 -> 1234.56
        text = (text.replace(".", "").replace(",", ".")
                if text.rfind(",") > text.rfind(".") else text.replace(",", ""))
    try:
        return float(text)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Column detection
# --------------------------------------------------------------------------

def _normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").strip().lower())


def find_column(headers: Sequence[str], aliases: Sequence[str],
                override: str | None = None) -> str | None:
    """Pick the header matching one of *aliases*, preferring an exact match."""
    if override:
        for header in headers:
            if _normalise(header) == _normalise(override):
                return header
        raise ObservationError(
            f"column {override!r} is not in the CSV. Headers: {', '.join(headers)}")

    lookup = {_normalise(h): h for h in headers}
    for alias in aliases:
        if alias in lookup:
            return lookup[alias]
    # Fall back to a containment match ("gps_latitude" for "latitude").
    for alias in aliases:
        for norm, header in lookup.items():
            if alias in norm:
                return header
    return None


def _open_rows(path: Path) -> tuple[list[str], list[dict]]:
    """Read a CSV, sniffing the delimiter and stripping any BOM."""
    if not path.exists():
        raise ObservationError(f"{path} does not exist")
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if not text.strip():
        raise ObservationError(f"{path} is empty")

    sample = "\n".join(text.splitlines()[:20])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        # Sniffer gives up on single-column or very short files; guess by count.
        header = text.splitlines()[0]
        delimiter = max(",;\t|", key=header.count)

    reader = csv.DictReader(text.splitlines(), delimiter=delimiter)
    headers = [h for h in (reader.fieldnames or []) if h is not None]
    if not headers:
        raise ObservationError(f"{path} has no header row")
    return headers, list(reader)


# --------------------------------------------------------------------------
# Loaders
# --------------------------------------------------------------------------

def load_observations(path: str | Path, time_column: str | None = None,
                      lat_column: str | None = None, lon_column: str | None = None,
                      category_column: str | None = None, note_column: str | None = None,
                      id_column: str | None = None) -> list[Observation]:
    """Load the campaign-wide observation CSV.

    Every run is given the same campaign-wide CSV; each video matches only the
    rows that fall inside its own time window.
    """
    path = Path(path)
    headers, rows = _open_rows(path)

    time_col = find_column(headers, TIME_ALIASES, time_column)
    if time_col is None:
        raise ObservationError(
            f"{path.name}: no timestamp column found. Headers: {', '.join(headers)}. "
            "Pass --time-column to name it explicitly.")
    lat_col = find_column(headers, LAT_ALIASES, lat_column)
    lon_col = find_column(headers, LON_ALIASES, lon_column)
    cat_col = find_column(headers, CATEGORY_ALIASES, category_column)
    note_col = find_column(headers, NOTE_ALIASES, note_column)
    id_col = find_column(headers, ID_ALIASES, id_column)

    observations: list[Observation] = []
    unparsed = 0
    for number, row in enumerate(rows, start=2):  # row 1 is the header
        utc = parse_timestamp(row.get(time_col))
        if utc is None:
            unparsed += 1
            continue
        external = (row.get(id_col) or "").strip() if id_col else ""
        observations.append(Observation(
            external_id=external or f"row{number}",
            utc=utc,
            lat=parse_number(row.get(lat_col)) if lat_col else None,
            lon=parse_number(row.get(lon_col)) if lon_col else None,
            category=(row.get(cat_col) or "").strip() if cat_col else "",
            note=(row.get(note_col) or "").strip() if note_col else "",
            row_number=number,
            raw={k: v for k, v in row.items() if k},
        ))

    if not observations:
        raise ObservationError(
            f"{path.name}: found {len(rows)} rows but none had a readable timestamp "
            f"in column {time_col!r}.")
    if unparsed:
        # Not fatal, but it must not pass unnoticed — these rows can never match.
        observations[0].raw.setdefault("_unparsed_rows", str(unparsed))
    return observations


def load_phone_track(path: str | Path, time_column: str | None = None,
                     lat_column: str | None = None, lon_column: str | None = None,
                     accuracy_column: str | None = None) -> list[PhoneFix]:
    """Load the phone GNSS log, sorted by time.

    The phone is multi-constellation with sensor fusion; a HERO5 is a weak
    single-constellation receiver. Where this log exists its fixes are averaged
    across each stop and used as the observation's position.
    """
    path = Path(path)
    headers, rows = _open_rows(path)

    time_col = find_column(headers, TIME_ALIASES, time_column)
    lat_col = find_column(headers, LAT_ALIASES, lat_column)
    lon_col = find_column(headers, LON_ALIASES, lon_column)
    acc_col = find_column(headers, ACCURACY_ALIASES, accuracy_column)
    missing = [name for name, col in
               (("timestamp", time_col), ("latitude", lat_col), ("longitude", lon_col))
               if col is None]
    if missing:
        raise ObservationError(
            f"{path.name}: phone GNSS log is missing {', '.join(missing)}. "
            f"Headers: {', '.join(headers)}")

    fixes: list[PhoneFix] = []
    for row in rows:
        utc = parse_timestamp(row.get(time_col))
        lat = parse_number(row.get(lat_col))
        lon = parse_number(row.get(lon_col))
        if utc is None or lat is None or lon is None:
            continue
        fixes.append(PhoneFix(utc=utc, lat=lat, lon=lon,
                              accuracy_m=parse_number(row.get(acc_col)) if acc_col else None))

    fixes.sort(key=lambda f: f.utc)
    if not fixes:
        raise ObservationError(f"{path.name}: phone GNSS log has no usable fixes")
    return fixes


def average_fixes(fixes: Sequence[PhoneFix], start_utc: datetime,
                  end_utc: datetime) -> tuple[float, float, int] | None:
    """Mean phone position across a stop window, with the fix count behind it."""
    inside = [f for f in fixes if start_utc <= f.utc <= end_utc]
    if not inside:
        return None
    return (sum(f.lat for f in inside) / len(inside),
            sum(f.lon for f in inside) / len(inside),
            len(inside))


def nearest_fix(fixes: Sequence[PhoneFix], utc: datetime,
                max_delta_s: float = 10.0) -> PhoneFix | None:
    """The phone fix closest in time to *utc*, if one is close enough."""
    if not fixes:
        return None
    best = min(fixes, key=lambda f: abs((f.utc - utc).total_seconds()))
    return best if abs((best.utc - utc).total_seconds()) <= max_delta_s else None


def suggest_clock_offset(observations: Iterable[Observation],
                         window_start: datetime,
                         window_end: datetime) -> tuple[float, int] | None:
    """Find the whole-hour shift that would rescue the most observations.

    A tagging app that exported local time instead of UTC puts every
    observation the same number of hours out — so the useful question is not
    "does some shift help one row" but "which shift helps the most rows". A
    single outlier four hours away is a stray tag, not a clock problem, and
    advising a campaign-wide shift to accommodate it would break everything
    that currently matches.

    Returns the offset in seconds and how many observations it would bring
    inside the window, or None if no whole hour helps.
    """
    stamps = [o.utc for o in observations]
    if not stamps:
        return None

    best_hours, best_count = 0, 0
    for hours in range(-14, 15):
        if hours == 0:
            continue
        shift = timedelta(hours=hours)
        count = sum(1 for stamp in stamps if window_start <= stamp + shift <= window_end)
        if count > best_count:
            best_hours, best_count = hours, count
    return (best_hours * 3600.0, best_count) if best_count else None
