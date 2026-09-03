"""Settings: the ingest knobs, persisted so the operator types paths once.

Two sources, kept apart on purpose:

* ``Settings`` — the ingest knobs, persisted to ``~/.curbtool.json``. Safe to
  write anywhere; it is paths and numbers.
* ``SupabaseConfig`` — credentials, read from the environment or ``.env``.
  Never persisted. ``SUPABASE_SERVICE_KEY`` bypasses RLS entirely, so it stays
  out of the settings file, out of the repo and out of Lovable.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

CONFIG_PATH = Path.home() / ".curbtool.json"

# The folder the tool ships in. curbtool/config.py -> the directory holding
# curbtool/, ingest.py and data/. Same idea as WEB_ROOT in webui.py, one level
# further out because data/ sits beside the package rather than inside it.
BUNDLE_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = BUNDLE_ROOT / "data"


def bundled_defaults(data_dir: Path | None = None) -> dict:
    """Settings discovered from the data/ folder shipped beside the tool.

    So that a fresh unpack can go straight to Check without anyone typing a
    path. Only keys whose files actually exist are returned, so a missing or
    emptied data/ degrades to plain defaults rather than to a broken path.

    ``data/campaign.json`` names the files explicitly and is the thing to edit
    when a different campaign is dropped in. Without it, a folder holding
    exactly one CSV is unambiguous enough to use; two or more is not, and
    guessing there would be worse than asking.
    """
    data_dir = DATA_DIR if data_dir is None else Path(data_dir)
    if not data_dir.is_dir():
        return {}

    manifest: dict = {}
    manifest_path = data_dir / "campaign.json"
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            manifest = {}

    def resolve(name) -> str:
        if not isinstance(name, str) or not name:
            return ""
        candidate = data_dir / name
        return str(candidate) if candidate.is_file() else ""

    observations = resolve(manifest.get("observations"))
    gnss = resolve(manifest.get("gnss"))
    campaign = manifest.get("campaign") if isinstance(manifest.get("campaign"), str) else ""

    if not observations:
        csvs = sorted(p for p in data_dir.glob("*.csv") if p.is_file())
        if len(csvs) == 1:
            observations = str(csvs[0])
            gnss = gnss or observations
            campaign = campaign or csvs[0].stem

    found = {}
    if observations:
        found["observations_csv"] = observations
    if gnss:
        found["gnss_csv"] = gnss
    if campaign:
        found["campaign"] = campaign
    return found


@dataclass
class Settings:
    """Everything the GUI's settings panel edits, and the CLI's defaults."""

    # Inputs
    observations_csv: str = ""
    gnss_csv: str = ""
    campaign: str = ""
    last_folder: str = ""

    # Matching
    # An IANA zone (Europe/Tallinn) used only for timestamps that carry no zone
    # of their own. Handles summer time correctly, which a fixed offset cannot.
    timezone: str = ""
    clock_offset_s: float = 0.0
    stop_speed_mps: float = 0.7
    stop_min_duration_s: float = 3.0
    stop_tolerance_s: float = 2.0
    # Used only where a tag falls outside every detected stop.
    fallback_window_s: float = 5.0

    # Frames
    frame_width: int = 1280
    frame_quality: int = 88
    frame_interval_s: float = 1.0
    max_frames: int = 9

    # Proxy
    # none | hd | lrv | auto.
    #
    # Defaults to "none" — frames only. Transcoding is by far the most
    # expensive thing this pipeline does (tens of minutes and several hundred
    # megabytes a chapter) and whether reviewers need video at all is unproven
    # until one of them tries to grade from the stills. Video is added later
    # with `backfill`, which reuses the frames already on disk and leaves
    # grading untouched, so nothing is lost by waiting.
    proxy_source: str = "none"
    proxy_height: int = 720
    proxy_bitrate_kbps: int = 2500
    # auto probes the machine's hardware encoders (Intel QSV, NVENC, AMF) and
    # falls back to libx264; "software" forces libx264; a codec name forces it.
    proxy_encoder: str = "auto"
    # 0 keeps the source frame rate. 15 is ample for "where was I" and roughly
    # halves the scale-and-encode work.
    proxy_fps: int = 0
    # Off: decoding and re-encoding the AAC track for a whole chapter is not
    # free, and nobody grades kerb damage by ear.
    proxy_audio: bool = False

    # Output
    work_dir: str = "work"
    # Off by default. .env does not ship — only .env.example — so uploading on
    # a fresh unpack fails for want of credentials, which is a setup step in
    # the way of the first thing worth doing: cutting frames and looking at
    # them. Tick it once Supabase is configured.
    upload: bool = False

    def merged(self, **overrides) -> "Settings":
        """A copy with non-None overrides applied — CLI flags beating the file."""
        data = asdict(self)
        for key, value in overrides.items():
            if value is not None and key in data:
                data[key] = value
        return Settings(**data)

    def describe(self) -> str:
        """The settings that change what lands in the database."""
        return (f"campaign={self.campaign or '(unset)'} "
                f"tz={self.timezone or 'UTC (naive stamps)'} "
                f"clock_offset={self.clock_offset_s:+g}s "
                f"stop<={self.stop_speed_mps:g}m/s>={self.stop_min_duration_s:g}s "
                f"frames={self.frame_width}px x{self.max_frames} "
                f"proxy={self.proxy_source}/{self.proxy_height}p@"
                f"{self.proxy_bitrate_kbps}k")

    def save(self, path: Path | None = None) -> None:
        # Resolved at call time, not bound as a default at import time: a
        # default argument would freeze the path and silently ignore any later
        # change to CONFIG_PATH.
        path = Path(path) if path is not None else CONFIG_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2, sort_keys=True) + "\n")

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        """Load saved settings, ignoring keys this version no longer knows.

        A settings file written by an older build must never stop the tool from
        starting; unknown keys are dropped and missing ones take their default.
        """
        path = Path(path) if path is not None else CONFIG_PATH
        bundled = bundled_defaults()
        if not path.exists():
            return cls(**bundled)
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return cls(**bundled)
        if not isinstance(data, dict):
            return cls(**bundled)

        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in data.items() if k in known}
        # A key that is present wins, including when it is empty: clearing a
        # field means "none", not "go and find one for me". Only keys the saved
        # file never mentions — a file written by an older build, or one that
        # predates data/ — pick up what the bundle ships with.
        for key, value in bundled.items():
            values.setdefault(key, value)
        return cls(**values)


def describe_inputs(settings: "Settings") -> str:
    """One line naming the observation CSV in use, and where it came from.

    Auto-detection that silently picks the wrong file is worse than none, so
    whatever was found gets said out loud at startup.
    """
    if not settings.observations_csv:
        if not DATA_DIR.is_dir():
            return "no observation CSV set — choose one in Settings"
        return ("no observation CSV set — put one in the data folder, or choose "
                "it in Settings")
    path = Path(settings.observations_csv)
    where = "shipped in data/" if path.parent == DATA_DIR else str(path.parent)
    if not path.is_file():
        return f"observation CSV not found: {path}"
    try:
        with path.open(encoding="utf-8-sig", errors="replace") as handle:
            rows = max(0, sum(1 for _ in handle) - 1)
        count = f"{rows} rows, "
    except OSError:
        count = ""
    return f"observations: {path.name} ({count}{where})"


@dataclass
class SupabaseConfig:
    url: str = ""
    service_key: str = ""
    frame_bucket: str = "frames"
    proxy_bucket: str = "proxies"

    @property
    def configured(self) -> bool:
        return bool(self.url and self.service_key)

    @classmethod
    def from_env(cls, env_file: str | Path | None = ".env") -> "SupabaseConfig":
        if env_file:
            _load_dotenv(Path(env_file))
        return cls(
            url=os.environ.get("SUPABASE_URL", "").rstrip("/"),
            service_key=os.environ.get("SUPABASE_SERVICE_KEY", ""),
            frame_bucket=os.environ.get("FRAME_BUCKET", "frames"),
            proxy_bucket=os.environ.get("PROXY_BUCKET", "proxies"),
        )

    def describe(self) -> str:
        """A one-line summary safe to print — never the key itself."""
        if not self.configured:
            return "Supabase: not configured (set SUPABASE_URL and SUPABASE_SERVICE_KEY)"
        return f"Supabase: {self.url} (buckets: {self.frame_bucket}, {self.proxy_bucket})"


def _load_dotenv(path: Path) -> None:
    """Populate os.environ from a .env file without overriding real env vars."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        pass
    else:
        if path.exists():
            load_dotenv(path, override=False)
        return

    # Minimal fallback so a missing python-dotenv is not fatal.
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
