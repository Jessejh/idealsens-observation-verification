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


@dataclass
class Settings:
    """Everything the GUI's settings panel edits, and the CLI's defaults."""

    # Inputs
    observations_csv: str = ""
    gnss_csv: str = ""
    campaign: str = ""
    last_folder: str = ""

    # Matching
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
    # none | hd | lrv | auto. "none" skips proxy building entirely, which is
    # what makes a frames-first pass possible: minutes per file instead of
    # tens of minutes, and nothing to upload but the evidence stills.
    proxy_source: str = "hd"
    proxy_height: int = 720
    proxy_bitrate_kbps: int = 2500

    # Output
    work_dir: str = "work"
    upload: bool = True

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
                f"clock_offset={self.clock_offset_s:+g}s "
                f"stop<={self.stop_speed_mps:g}m/s>={self.stop_min_duration_s:g}s "
                f"frames={self.frame_width}px x{self.max_frames} "
                f"proxy={self.proxy_source}/{self.proxy_height}p@"
                f"{self.proxy_bitrate_kbps}k")

    def save(self, path: Path = CONFIG_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2, sort_keys=True) + "\n")

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "Settings":
        """Load saved settings, ignoring keys this version no longer knows.

        A settings file written by an older build must never stop the tool from
        starting; unknown keys are dropped and missing ones take their default.
        """
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


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
