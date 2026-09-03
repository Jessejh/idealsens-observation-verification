"""A local web UI for the pipeline.

Why a server at all: a browser cannot read a drive or start a transcode, so the
page is a front end onto a small HTTP server running on this machine. Nothing
leaves the machine, and the server exists only while the window is open.

Standard library only — no Flask, no Node. The operator's machine has software
restrictions that make every extra installer a negotiation, and the whole tool
is meant to run from a folder.

Safety, since this server runs local work and holds a key that bypasses every
database rule:

* it binds to 127.0.0.1, so nothing outside this machine can reach it;
* every request must carry a token minted at startup, so another program or a
  web page open in the same browser cannot drive it;
* the Host header must be a loopback name, which blocks DNS rebinding;
* the service_role key is never sent to the page — only whether one is set.
"""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import threading
import time
import webbrowser
from dataclasses import asdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from . import errors
from .batch import find_videos, load_inputs, make_client
from .config import Settings, SupabaseConfig, describe_inputs
from .media import MediaError, find_lrv, probe
from .observations import ObservationError
from .pipeline import (STAGES, BatchSummary, Cancelled, IngestJob, IngestResult,
                       Progress, ingest_file)
from .supabase_io import SupabaseError
from .verify import check_campaign

WEB_ROOT = Path(__file__).parent / "web"
MAX_EVENTS = 4000
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


# --------------------------------------------------------------------------
# Application state — the single source of truth the page renders
# --------------------------------------------------------------------------

class AppState:
    """Everything the page shows. Guarded by one lock; the worker never draws."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.settings = Settings.load()
        self.supabase = SupabaseConfig.from_env()
        self.files: list[dict] = []
        self.events: list[dict] = []
        self.cursor = 0
        self.status = "idle"          # idle | checking | running
        self.activity = ""
        self.progress = 0.0
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.problems: list[dict] = []
        self.summary: dict | None = None
        self.check_result: dict | None = None

    # -- events ---------------------------------------------------------

    def log(self, message: str, kind: str = "info") -> None:
        with self.lock:
            self.cursor += 1
            self.events.append({
                "seq": self.cursor,
                "at": datetime.now().strftime("%H:%M:%S"),
                "kind": kind,
                "text": message,
            })
            if len(self.events) > MAX_EVENTS:
                del self.events[:len(self.events) - MAX_EVENTS]

    def problem(self, described: dict, where: str = "") -> None:
        """Record a coded failure and echo it into the log."""
        with self.lock:
            entry = {**described, "where": where, "seq": self.cursor + 1}
            self.problems.append(entry)
        self.log(f"{described['code']} · {described['title']}"
                 + (f" — {where}" if where else ""), kind="error")
        self.log(f"    {described['detail']}", kind="detail")

    def clear_run(self) -> None:
        with self.lock:
            self.problems = []
            self.summary = None
            self.check_result = None
            self.progress = 0.0

    # -- files ----------------------------------------------------------

    def add_paths(self, paths: list[str]) -> int:
        existing = {f["path"] for f in self.files}
        added = 0
        for raw in paths:
            for video in find_videos(raw):
                key = str(video)
                if key in existing:
                    continue
                existing.add(key)
                self.files.append({
                    "path": key, "name": video.name, "duration_s": None,
                    "lrv": None, "status": "queued", "fraction": 0.0, "detail": "",
                })
                added += 1
            folder = Path(raw)
            self.settings = self.settings.merged(
                last_folder=str(folder if folder.is_dir() else folder.parent))
        if added:
            threading.Thread(target=self._probe_all, daemon=True).start()
        return added

    def _probe_all(self) -> None:
        for entry in list(self.files):
            if entry["duration_s"] is not None:
                continue
            try:
                entry["duration_s"] = probe(entry["path"]).duration_s
            except Exception:
                # A length is a convenience. Never let it kill the thread.
                entry["duration_s"] = 0.0
            try:
                entry["lrv"] = find_lrv(entry["path"]) is not None
            except OSError:
                entry["lrv"] = False

    def set_file(self, name: str, **fields) -> None:
        with self.lock:
            for entry in self.files:
                if entry["name"] == name:
                    entry.update(fields)
                    return

    def busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def snapshot(self, since: int = 0) -> dict:
        with self.lock:
            return {
                "status": self.status,
                "activity": self.activity,
                "progress": round(self.progress, 4),
                "busy": self.busy(),
                "cursor": self.cursor,
                "settings": asdict(self.settings),
                "supabase": {
                    "url": self.supabase.url,
                    "configured": self.supabase.configured,
                    "frame_bucket": self.supabase.frame_bucket,
                    "proxy_bucket": self.supabase.proxy_bucket,
                },
                "files": list(self.files),
                "events": [e for e in self.events if e["seq"] > since],
                "problems": list(self.problems),
                "summary": self.summary,
                "check": self.check_result,
                "stages": list(STAGES),
            }


# --------------------------------------------------------------------------
# The two long-running actions
# --------------------------------------------------------------------------

def run_check(state: AppState) -> None:
    """Dry-run the matching. Decodes nothing, writes nothing, uploads nothing."""
    settings = state.settings
    try:
        if not settings.observations_csv:
            state.problem({**asdict(errors.get("E201")),
                           "detail": "No observation CSV chosen."}, "settings")
            return
        try:
            observations, _ = load_inputs(settings)
        except (ObservationError, OSError) as exc:
            state.problem(errors.describe(exc), "observation CSV")
            return

        state.log(f"checking {len(state.files)} file(s) against "
                  f"{len(observations)} observation(s) — nothing is written")
        result = check_campaign(
            [Path(f["path"]) for f in state.files], observations, settings,
            on_progress=lambda m: setattr(state, "activity", m),
            should_cancel=state.cancel_event.is_set)

        for line in result.render().splitlines():
            state.log(line, kind="report")

        with state.lock:
            state.check_result = {
                "ready": result.ready,
                "cancelled": result.cancelled,
                "csv_rows": result.csv_rows,
                "matched": result.matched_count,
                "snapped": result.total_snapped,
                "unmatched": [
                    {"id": o.external_id, "utc": o.utc.isoformat(),
                     "category": o.category} for o in result.unmatched[:50]],
                "clock_offset_hint": result.clock_offset_hint,
                "clock_offset_rescues": result.clock_offset_rescues,
            }

        for check in result.files:
            if not check.ok:
                # check_campaign keeps the failure as text, so classify from that.
                code = errors.classify_text("GpmfError", check.error) or errors.get("E900")
                state.problem({**asdict(code), "detail": check.error}, check.file)

        if result.clock_offset_hint:
            code = errors.get("E110")
            state.problem({
                **asdict(code),
                "detail": (f"Set the clock offset to {result.clock_offset_hint:+.0f} s "
                           f"({result.clock_offset_hint / 3600:+.0f} h) — it would "
                           f"rescue {result.clock_offset_rescues} of the "
                           f"{len(result.unmatched)} unmatched observations."),
            }, "matching")
        elif result.unmatched:
            state.problem({
                **asdict(errors.get("E111")),
                "detail": f"{len(result.unmatched)} of {result.csv_rows} "
                          "observations match no chapter.",
            }, "matching")
    except Exception as exc:
        state.problem(errors.describe(exc), "check")


def run_ingest(state: AppState, force: bool) -> None:
    """The real work: frames, optional proxies, upload, rows."""
    settings = state.settings
    summary = BatchSummary(campaign=settings.campaign)

    if not settings.campaign:
        state.problem({**asdict(errors.get("E001")), "detail": "Campaign is empty."},
                      "settings")
        return
    try:
        observations, phone_fixes = load_inputs(settings)
        client = make_client(state.supabase, settings)
    except (ObservationError, SupabaseError, OSError) as exc:
        state.problem(errors.describe(exc), "inputs")
        return

    summary.csv_rows = len(observations)
    state.log(f"{len(state.files)} file(s), {len(observations)} observation(s)"
              + (f", {len(phone_fixes)} phone fixes" if phone_fixes else ""))

    for index, entry in enumerate(list(state.files), start=1):
        if state.cancel_event.is_set():
            state.log("cancelled — stopping before the next file", kind="warn")
            break

        name = entry["name"]
        state.set_file(name, status="running", fraction=0.0, detail="")
        state.log(f"[{index}/{len(state.files)}] {name}")

        def on_progress(p: Progress) -> None:
            state.activity = f"{p.file} — {p.stage}: {p.message}"
            state.progress = _weighted(p)
            state.set_file(p.file, fraction=state.progress, detail=p.stage)

        job = IngestJob(video=Path(entry["path"]), settings=settings,
                        observations=observations, phone_fixes=phone_fixes,
                        client=client, frame_bucket=state.supabase.frame_bucket,
                        proxy_bucket=state.supabase.proxy_bucket, force=force)
        try:
            result = ingest_file(job, on_progress=on_progress,
                                 should_cancel=state.cancel_event.is_set)
        except Cancelled as exc:
            state.set_file(name, status="cancelled", fraction=0.0)
            state.log(f"    cancelled: {exc}", kind="warn")
            summary.add(IngestResult(file=name, session_id="", status="cancelled",
                                     error=str(exc)))
            break
        except Exception as exc:
            described = errors.describe(exc)
            state.set_file(name, status="failed", fraction=0.0,
                           detail=described["code"])
            state.problem(described, name)
            summary.add(IngestResult(file=name, session_id="", status="failed",
                                     error=f"{described['code']}: {exc}"))
            continue        # one bad file must not take the other sixteen

        summary.add(result)
        state.set_file(name, status=result.status, fraction=1.0,
                       detail=f"{result.matched} obs · {result.frames} frames")
        if result.status == "skipped":
            state.log("    already ingested — skipped")
        else:
            state.log(f"    {result.matched} observations, {result.frames} frames, "
                      f"{result.elapsed_s:.0f}s")
        if result.hint:
            state.log(f"    hint: {result.hint}", kind="warn")

    for line in summary.render().splitlines():
        state.log(line, kind="report")

    missing = summary.csv_rows - summary.total_matched
    if missing > 0:
        state.problem({
            **asdict(errors.get("E111")),
            "detail": f"{summary.total_matched} of {summary.csv_rows} observations "
                      "in the CSV matched a chapter.",
        }, "campaign")

    try:
        path = summary.save(Path(settings.work_dir)
                            / (settings.campaign or "campaign") / "summary.json")
        state.log(f"summary written to {path}")
    except OSError as exc:
        state.problem(errors.describe(exc), "summary")

    with state.lock:
        state.summary = {
            "csv_rows": summary.csv_rows,
            "matched": summary.total_matched,
            "frames": summary.total_frames,
            "proxy_bytes": summary.total_proxy_bytes,
            "counts": summary.counts(),
            "files": [r.as_row() for r in summary.results],
        }


def _weighted(progress: Progress) -> float:
    """Weight stages so the bar tracks real time, not step count."""
    weights = {"track": 0.03, "stops": 0.02, "match": 0.02,
               "frames": 0.30, "proxy": 0.45, "upload": 0.15, "rows": 0.03}
    if progress.stage not in weights:
        return progress.fraction
    before = sum(weights[s] for s in STAGES[:STAGES.index(progress.stage)])
    return round(min(1.0, before + weights[progress.stage] * progress.fraction), 6)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Server:
    def __init__(self, state: AppState, host: str = "127.0.0.1", port: int = 0) -> None:
        self.state = state
        self.token = secrets.token_urlsafe(24)
        self.httpd = ThreadingHTTPServer((host, port), _make_handler(self))
        self.httpd.daemon_threads = True

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _make_handler(server: Server):
    state = server.state

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "curbtool"

        def log_message(self, *args):
            pass        # the page has its own log; the console stays readable

        # -- helpers ----------------------------------------------------

        def _authorised(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            if host not in LOOPBACK_HOSTS:
                return False        # blocks DNS rebinding
            query = parse_qs(urlparse(self.path).query)
            token = (self.headers.get("X-Curbtool-Token")
                     or (query.get("t") or [""])[0])
            return secrets.compare_digest(token, server.token)

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            try:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # The page polls constantly; a reload or a closed tab cuts a
                # response mid-flight. Routine, and not worth a traceback in
                # the operator's terminal.
                pass

        def handle_one_request(self):
            try:
                super().handle_one_request()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        def _json(self, payload: Any, code: int = 200) -> None:
            self._send(code, json.dumps(payload, default=str).encode(),
                       "application/json; charset=utf-8")

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return {}

        def _start(self, target, *args) -> None:
            """Run one action on the worker thread, whatever it does."""
            state.cancel_event.clear()
            state.clear_run()

            def wrapper():
                try:
                    target(state, *args)
                except Exception as exc:
                    state.problem(errors.describe(exc), "worker")
                finally:
                    state.status = "idle"
                    state.activity = ""
                    state.progress = 0.0

            state.worker = threading.Thread(target=wrapper, daemon=True)
            state.worker.start()

        # -- routes -----------------------------------------------------

        def do_GET(self):
            if not self._authorised():
                return self._json({"error": "unauthorised"}, 403)
            path = urlparse(self.path).path
            query = parse_qs(urlparse(self.path).query)

            if path in ("/", "/index.html"):
                page = (WEB_ROOT / "index.html").read_bytes()
                return self._send(200, page, "text/html; charset=utf-8")

            if path == "/api/state":
                since = int((query.get("since") or ["0"])[0])
                return self._json(state.snapshot(since))

            if path == "/api/browse":
                return self._json(_browse((query.get("path") or [""])[0],
                                          (query.get("kind") or ["video"])[0]))

            asset = WEB_ROOT / path.lstrip("/")
            if asset.is_file() and WEB_ROOT in asset.resolve().parents:
                kind, _ = mimetypes.guess_type(asset.name)
                return self._send(200, asset.read_bytes(), kind or "application/octet-stream")
            return self._json({"error": "not found"}, 404)

        def do_POST(self):
            if not self._authorised():
                return self._json({"error": "unauthorised"}, 403)
            path = urlparse(self.path).path
            body = self._body()

            if path == "/api/settings":
                known = set(asdict(state.settings))
                values = {k: v for k, v in body.items() if k in known}
                state.settings = state.settings.merged(**_coerce(state.settings, values))
                try:
                    state.settings.save()
                except OSError as exc:
                    state.problem(errors.describe(exc), "settings")
                return self._json({"settings": asdict(state.settings)})

            if path == "/api/files/add":
                added = state.add_paths(body.get("paths") or [])
                if not added and body.get("paths"):
                    state.problem({**asdict(errors.get("E003")),
                                   "detail": f"No .MP4 files under {body['paths'][0]}"},
                                  "files")
                else:
                    state.log(f"added {added} file(s)")
                return self._json({"files": state.files, "added": added})

            if path == "/api/files/clear":
                if state.busy():
                    return self._json({"error": "busy"}, 409)
                state.files = []
                return self._json({"files": []})

            if path == "/api/files/remove":
                if state.busy():
                    return self._json({"error": "busy"}, 409)
                drop = set(body.get("paths") or [])
                state.files = [f for f in state.files if f["path"] not in drop]
                return self._json({"files": state.files})

            if path == "/api/check":
                if state.busy():
                    return self._json({"error": "busy"}, 409)
                state.status = "checking"
                for entry in state.files:
                    entry.update(status="queued", fraction=0.0, detail="")
                self._start(run_check)
                return self._json({"started": True})

            if path == "/api/ingest":
                if state.busy():
                    return self._json({"error": "busy"}, 409)
                state.status = "running"
                for entry in state.files:
                    entry.update(status="queued", fraction=0.0, detail="")
                self._start(run_ingest, bool(body.get("force")))
                return self._json({"started": True})

            if path == "/api/cancel":
                state.cancel_event.set()
                state.log("cancel requested — stopping after the current step",
                          kind="warn")
                return self._json({"cancelling": True})

            if path == "/api/open":
                target = _resolve_open(state, body.get("what", "work"))
                ok, message = _open_in_file_manager(target)
                if not ok:
                    state.log(f"could not open {target}: {message}", kind="warn")
                return self._json({"path": str(target), "opened": ok, "message": message})

            if path == "/api/quit":
                state.cancel_event.set()
                threading.Thread(target=_delayed_shutdown, args=(server,),
                                 daemon=True).start()
                return self._json({"stopping": True})

            return self._json({"error": "not found"}, 404)

    return Handler


def _coerce(settings: Settings, values: dict) -> dict:
    """Match the page's strings to the types the settings actually hold."""
    out: dict[str, Any] = {}
    for key, raw in values.items():
        current = getattr(settings, key)
        if isinstance(current, bool):
            out[key] = bool(raw)
        elif isinstance(current, int):
            try:
                out[key] = int(float(str(raw).replace(",", ".")))
            except (TypeError, ValueError):
                out[key] = current
        elif isinstance(current, float):
            try:
                out[key] = float(str(raw).replace(",", "."))
            except (TypeError, ValueError):
                out[key] = current
        else:
            out[key] = str(raw).strip()
    return out


SUFFIXES = {"video": (".mp4",), "csv": (".csv", ".tsv", ".txt"), "dir": ()}


def _browse(raw: str, kind: str = "video") -> dict:
    """List a folder so the page can offer a picker.

    A browser hands the page a file's contents, never its path, and this tool
    needs paths — it reads 80 GB off a drive. So the folder listing comes from
    the server instead.
    """
    path = Path(unquote(raw)).expanduser() if raw else Path.home()
    payload = {
        "kind": kind,
        "roots": [str(p) for p in _drive_roots()],
    }
    if not path.is_dir():
        payload.update({"error": f"{path} is not a folder", "path": str(path),
                        "parent": "", "dirs": [], "files": []})
        return payload
    payload.update({
        "path": str(path),
        "parent": str(path.parent) if path.parent != path else "",
        "dirs": _safe_dirs(path),
        "files": _safe_files(path, SUFFIXES.get(kind, ())),
    })
    return payload


def _drive_roots() -> list[Path]:
    if os.name == "nt":
        return [Path(f"{letter}:\\") for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ"
                if Path(f"{letter}:\\").exists()]
    return [Path("/"), Path.home()]


def _safe_dirs(path: Path) -> list[dict]:
    try:
        entries = sorted(p for p in path.iterdir() if p.is_dir())
    except OSError:
        return []
    return [{"name": p.name, "path": str(p)} for p in entries
            if not p.name.startswith(".")][:400]


def _safe_files(path: Path, suffixes: tuple[str, ...]) -> list[dict]:
    if not suffixes:
        return []
    try:
        return [{"name": p.name, "path": str(p)} for p in sorted(path.iterdir())
                if p.is_file() and p.suffix.lower() in suffixes][:400]
    except OSError:
        return []


def _resolve_open(state: AppState, what: str) -> Path:
    work = Path(state.settings.work_dir)
    if what == "frames":
        campaign = work / (state.settings.campaign or "campaign")
        done = [f for f in state.files if f["status"] in ("done", "skipped")]
        if done:
            candidate = campaign / Path(done[0]["name"]).stem / "frames"
            if candidate.is_dir():
                return candidate
        return campaign if campaign.is_dir() else work
    return work


def _open_in_file_manager(path: Path) -> tuple[bool, str]:
    import subprocess
    import sys
    try:
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(str(path))          # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return True, str(path)
    except Exception as exc:
        return False, str(exc)


def _delayed_shutdown(server: Server) -> None:
    time.sleep(0.4)     # let the response reach the page first
    server.shutdown()


def main(port: int = 0, open_browser: bool = True) -> int:
    state = AppState()
    try:
        server = Server(state, port=port)
    except OSError as exc:
        print(f"E005 · cannot start the local server: {exc}")
        return 1

    state.log("curbtool ready")
    state.log(describe_inputs(state.settings))
    state.log(state.supabase.describe()
              + ("" if state.settings.upload else " — uploads off"))
    print(f"curbtool web UI  →  {server.url}")
    print("This server is local to this machine. Close the window or press "
          "Ctrl-C to stop it.")
    if open_browser:
        threading.Thread(target=lambda: (time.sleep(0.3),
                                         webbrowser.open(server.url)),
                         daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        state.cancel_event.set()
    return 0
