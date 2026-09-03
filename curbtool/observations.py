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
# observation_type before label: the machine key groups and filters reliably,
# where the human sentence is display text.
CATEGORY_ALIASES = ("observationtype", "obstype", "category", "type", "tag", "class",
                    "kind", "label", "luokka", "tyyppi")
NOTE_ALIASES = ("note", "notes", "description", "comment", "comments", "remark",
                "huomio", "kuvaus", "label")
# Matched exactly, never by containment: "session_id" and "device_id" are shared
# by hundreds of rows, and treating one as an observation's identity collapses
# the whole campaign onto a handful of database rows.
ID_ALIASES = ("id", "uuid", "externalid", "observationid", "obsid", "pointid",
              "featureid", "tunnus")
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


def parse_timestamp(value: str | None, zone=None) -> datetime | None:
    """Parse a timestamp into an aware UTC datetime.

    Accepts ISO 8601 (with ``T`` or a space, with ``Z``, an offset, or neither)
    and epoch seconds/milliseconds.

    A timestamp with no zone is read in *zone* when one is given, and as UTC
    otherwise — so if the tagging app exported local time and no zone is set,
    the whole campaign is off by a constant. That is what ``--timezone`` and
    ``--clock-offset`` exist to correct, and what :func:`suggest_clock_offset`
    exists to spot.
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
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC)
    return (parsed.replace(tzinfo=zone).astimezone(UTC) if zone is not None
            else parsed.replace(tzinfo=UTC))


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
                override: str | None = None, exact_only: bool = False) -> str | None:
    """Pick the header matching one of *aliases*, preferring an exact match.

    *exact_only* disables the containment fallback. Use it wherever a wrong
    guess is worse than no guess — identifiers especially, where "session_id"
    would otherwise satisfy a search for "id".
    """
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
    if exact_only:
        return None
    # Fall back to a containment match ("gps_latitude" for "latitude").
    for alias in aliases:
        for norm, header in lookup.items():
            if alias in norm:
                return header
    return None


# --------------------------------------------------------------------------
# Choosing the time column
# --------------------------------------------------------------------------

# Names that mark a column as local wall-clock time rather than UTC. Matching
# one is disqualifying, not merely unhelpful: reading local time as UTC shifts
# every frame in the campaign by the whole offset.
LOCAL_MARKERS = ("local", "eest", "eet", "cest", "cet", "eddt", "paikallinen",
                 "kohalik", "wallclock")


def score_time_column(header: str, samples: Sequence[str]) -> tuple[int, str]:
    """Rank a candidate time column. Higher is better; the reason is for the log.

    An export often carries the same instant three ways — local, UTC and epoch.
    Which one is picked decides whether every frame lands on the right second,
    so the choice is scored explicitly rather than left to whichever column
    happens to come first.
    """
    name = _normalise(header)
    values = [v for v in samples if v and str(v).strip()][:20]
    if not values:
        return -1, "no values"
    parsed = [parse_timestamp(v) for v in values]
    if not any(parsed):
        return -1, "nothing parses as a time"

    aware = sum(1 for v in values if _looks_aware(str(v)))
    epoch = sum(1 for v in values if re.fullmatch(r"-?\d{9,13}(\.\d+)?", str(v).strip()))

    if epoch == len(values):
        return 90, "epoch time, unambiguous"
    if aware == len(values) and "utc" in name:
        return 100, "named UTC and carries a zone"
    if aware == len(values):
        return 80, "carries an explicit zone"
    if "utc" in name or name.endswith("z"):
        return 60, "named UTC but no zone in the values"
    if any(marker in name for marker in LOCAL_MARKERS):
        return 10, "looks like local wall-clock time"
    return 40, "a time column with no zone"


def _looks_aware(value: str) -> bool:
    text = value.strip()
    return bool(text.endswith(("Z", "z"))
                or re.search(r"[+-]\d{2}:?\d{2}$", text))


def choose_time_column(headers: Sequence[str], rows: Sequence[dict],
                       override: str | None = None) -> tuple[str, list[tuple[str, int, str]]]:
    """The best time column, plus every candidate and why it scored as it did."""
    if override:
        column = find_column(headers, (), override)
        return column, [(column, 100, "chosen explicitly")]

    candidates: list[tuple[str, int, str]] = []
    for header in headers:
        name = _normalise(header)
        if not any(alias in name for alias in TIME_ALIASES):
            continue
        score, reason = score_time_column(header, [r.get(header, "") for r in rows])
        if score >= 0:
            candidates.append((header, score, reason))

    if not candidates:
        raise ObservationError(
            "no timestamp column found. Headers: " + ", ".join(headers)
            + ". Pass --time-column to name it explicitly.")
    candidates.sort(key=lambda c: (-c[1], headers.index(c[0])))
    return candidates[0][0], candidates


class ObservationSet(list):
    """The loaded observations, carrying what the loader had to decide.

    A plain list everywhere it is used; the extra fields exist so a caller that
    cares — the pre-flight check, the UI — can show how the columns were read
    and what was thrown away.
    """

    columns: dict[str, str | None]
    time_candidates: list[tuple[str, int, str]]
    warnings: list[str]
    unparsed_rows: int


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
                      id_column: str | None = None,
                      timezone_name: str | None = None) -> ObservationSet:
    """Load the campaign-wide observation CSV.

    Every run is given the same campaign-wide CSV; each video matches only the
    rows that fall inside its own time window.

    *timezone_name* is an IANA zone (``Europe/Tallinn``) used to interpret
    timestamps that carry no zone of their own. Without it, a naive timestamp
    is read as UTC.
    """
    path = Path(path)
    headers, rows = _open_rows(path)

    time_col, candidates = choose_time_column(headers, rows, time_column)
    lat_col = find_column(headers, LAT_ALIASES, lat_column)
    lon_col = find_column(headers, LON_ALIASES, lon_column)
    cat_col = find_column(headers, CATEGORY_ALIASES, category_column)
    note_col = find_column(headers, NOTE_ALIASES, note_column)
    if note_col is not None and note_col == cat_col:
        note_col = None      # one column cannot be both
    id_col = find_column(headers, ID_ALIASES, id_column, exact_only=True)

    warnings: list[str] = []
    zone = _load_zone(timezone_name, warnings) if timezone_name else None

    observations = ObservationSet()
    unparsed = 0
    for number, row in enumerate(rows, start=2):      # row 1 is the header
        utc = parse_timestamp(row.get(time_col), zone)
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

    # An identifier that repeats is not an identifier. Observation IDs are
    # derived from it, so duplicates would quietly merge distinct observations
    # into one database row and lose the rest.
    if id_col:
        seen = {o.external_id for o in observations}
        if len(seen) != len(observations):
            warnings.append(
                f"column {id_col!r} is not unique ({len(observations)} rows, "
                f"{len(seen)} distinct values) — falling back to row numbers so "
                "no observation is lost.")
            id_col = None
            for observation in observations:
                observation.external_id = f"row{observation.row_number}"

    chosen_score = next((c for c in candidates if c[0] == time_col), None)
    if chosen_score and chosen_score[1] <= 40:
        warnings.append(
            f"time column {time_col!r} {chosen_score[2]}; if it is local time, set "
            "a timezone or a clock offset before ingesting.")
    if unparsed:
        warnings.append(f"{unparsed} row(s) had no readable timestamp and were skipped.")

    observations.columns = {"time": time_col, "lat": lat_col, "lon": lon_col,
                            "category": cat_col, "note": note_col, "id": id_col}
    observations.time_candidates = candidates
    observations.warnings = warnings
    observations.unparsed_rows = unparsed
    return observations


def _load_zone(name: str, warnings: list[str]):
    """Resolve an IANA timezone, explaining the Windows case if it is missing."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception as exc:
        warnings.append(
            f"timezone {name!r} could not be loaded ({exc}); timestamps without a "
            "zone are being read as UTC. On Windows this usually means the tzdata "
            "package is missing — pip install tzdata.")
        return None


def load_phone_track(path: str | Path, time_column: str | None = None,
                     lat_column: str | None = None, lon_column: str | None = None,
                     accuracy_column: str | None = None,
                     timezone_name: str | None = None) -> list[PhoneFix]:
    """Load the phone GNSS log, sorted by time.

    The phone is multi-constellation with sensor fusion; a HERO5 is a weak
    single-constellation receiver. Where this log exists its fixes are averaged
    across each stop and used as the observation's position.
    """
    path = Path(path)
    headers, rows = _open_rows(path)

    try:
        time_col, _ = choose_time_column(headers, rows, time_column)
    except ObservationError:
        time_col = None
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

    zone = _load_zone(timezone_name, []) if timezone_name else None
    fixes: list[PhoneFix] = []
    for row in rows:
        utc = parse_timestamp(row.get(time_col), zone)
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
