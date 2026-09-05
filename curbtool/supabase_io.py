"""Supabase access: PostgREST rows and Storage objects.

Frames are small and go up on the standard storage endpoint. Proxies are
several hundred megabytes each, so they go up over TUS at
``/storage/v1/upload/resumable`` in 6 MB chunks: a dropped connection then
resumes from the last acknowledged offset instead of restarting the file. The
upload URL is written to disk, so a resume survives the tool being closed and
reopened, not merely a retry inside one run.

Everything here authenticates with the ``service_role`` key, which bypasses RLS
entirely. It stays on the operator's machine — never in the repo, never in
Lovable.
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import requests

# Supabase's TUS implementation requires exactly this chunk size for every
# chunk but the last.
TUS_CHUNK_SIZE = 6 * 1024 * 1024
TUS_VERSION = "1.0.0"

RETRY_STATUS = {408, 409, 423, 425, 429, 500, 502, 503, 504}
MAX_ATTEMPTS = 5


class SupabaseError(Exception):
    """Raised when Supabase rejects a request, or is unreachable after retries."""


class Cancelled(Exception):
    """Raised when the operator cancels during an upload."""


def _backoff(attempt: int, base: float = 2.0) -> float:
    return min(16.0, base ** (attempt + 1))


@dataclass
class UploadResult:
    path: str
    size_bytes: int
    resumed: bool = False
    skipped: bool = False


class SupabaseClient:
    """A small, synchronous Supabase client — only what the pipeline needs."""

    def __init__(self, url: str, service_key: str, timeout: float = 60.0,
                 state_dir: str | Path = "work/uploads",
                 max_attempts: int = MAX_ATTEMPTS, backoff_base: float = 2.0) -> None:
        if not url or not service_key:
            raise SupabaseError("SUPABASE_URL and SUPABASE_SERVICE_KEY must both be set")
        self.url = url.rstrip("/")
        self.service_key = service_key
        self.timeout = timeout
        self.state_dir = Path(state_dir)
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self.session = requests.Session()
        self.session.headers.update({
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
        })

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    def _request(self, method: str, url: str, *, retries: int | None = None,
                 **kwargs) -> requests.Response:
        """Issue a request, retrying transient failures with exponential backoff."""
        retries = self.max_attempts if retries is None else retries
        kwargs.setdefault("timeout", self.timeout)
        last: Exception | None = None
        for attempt in range(retries):
            try:
                response = self.session.request(method, url, **kwargs)
            except requests.RequestException as exc:
                last = exc
            else:
                if response.status_code < 400 or response.status_code not in RETRY_STATUS:
                    return response
                last = SupabaseError(
                    f"{method} {url} -> {response.status_code}: {response.text[:400]}")
            if attempt < retries - 1:
                time.sleep(_backoff(attempt, self.backoff_base))
        raise SupabaseError(f"{method} {url} failed after {retries} attempts: {last}")

    @staticmethod
    def _check(response: requests.Response, what: str) -> requests.Response:
        if response.status_code >= 400:
            raise SupabaseError(f"{what} -> {response.status_code}: {response.text[:600]}")
        return response

    # ------------------------------------------------------------------
    # PostgREST
    # ------------------------------------------------------------------

    def select(self, table: str, params: dict[str, str] | None = None) -> list[dict]:
        response = self._check(
            self._request("GET", f"{self.url}/rest/v1/{table}", params=params or {}),
            f"select {table}")
        return response.json()

    def upsert(self, table: str, rows: Sequence[dict], on_conflict: str = "id",
               chunk_size: int = 500, returning: str = "representation") -> list[dict]:
        """Insert or update rows, in batches PostgREST will not choke on.

        Pass ``returning="minimal"`` for bulk writes nobody reads back — a
        thousand track points echoed over the wire is pure waste.
        """
        if not rows:
            return []
        out: list[dict] = []
        for start in range(0, len(rows), chunk_size):
            batch = list(rows[start:start + chunk_size])
            response = self._check(self._request(
                "POST", f"{self.url}/rest/v1/{table}",
                params={"on_conflict": on_conflict},
                headers={
                    "Content-Type": "application/json",
                    "Prefer": f"resolution=merge-duplicates,return={returning}",
                },
                data=json.dumps(batch, default=str),
            ), f"upsert {table}")
            if returning == "representation" and response.content:
                out.extend(response.json())
        return out

    def insert(self, table: str, rows: Sequence[dict]) -> list[dict]:
        if not rows:
            return []
        response = self._check(self._request(
            "POST", f"{self.url}/rest/v1/{table}",
            headers={"Content-Type": "application/json", "Prefer": "return=representation"},
            data=json.dumps(list(rows), default=str),
        ), f"insert {table}")
        return response.json() if response.content else []

    def delete(self, table: str, filters: dict[str, str]) -> None:
        if not filters:
            raise SupabaseError(f"refusing to delete from {table} with no filter")
        self._check(self._request("DELETE", f"{self.url}/rest/v1/{table}", params=filters),
                    f"delete {table}")

    def count(self, table: str, filters: dict[str, str] | None = None) -> int:
        """Row count via a HEAD request, without pulling the rows themselves."""
        response = self._check(self._request(
            "HEAD", f"{self.url}/rest/v1/{table}",
            params={**(filters or {}), "select": "id"},
            headers={"Prefer": "count=exact", "Range-Unit": "items", "Range": "0-0"},
        ), f"count {table}")
        content_range = response.headers.get("content-range", "")
        total = content_range.rsplit("/", 1)[-1]
        return int(total) if total.isdigit() else 0

    # ------------------------------------------------------------------
    # Storage — standard endpoint (frames)
    # ------------------------------------------------------------------

    def ensure_bucket(self, name: str, public: bool = True,
                      file_size_limit: int | None = None) -> None:
        """Create the bucket if it is missing. Safe to call on every run."""
        response = self._request("GET", f"{self.url}/storage/v1/bucket/{name}")
        if response.status_code < 400:
            return
        if response.status_code != 404:
            self._check(response, f"inspect bucket {name}")
        payload: dict[str, Any] = {"id": name, "name": name, "public": public}
        if file_size_limit:
            payload["file_size_limit"] = file_size_limit
        created = self._request(
            "POST", f"{self.url}/storage/v1/bucket",
            headers={"Content-Type": "application/json"}, data=json.dumps(payload))
        if created.status_code >= 400 and "already exists" not in created.text.lower():
            self._check(created, f"create bucket {name}")

    def upload(self, bucket: str, path: str, data: bytes | Path,
               content_type: str | None = None, upsert: bool = True) -> UploadResult:
        """Upload a small object in one request. Used for frames."""
        if isinstance(data, Path):
            content_type = content_type or _guess_type(data)
            payload = data.read_bytes()
        else:
            payload = data
            content_type = content_type or "application/octet-stream"

        response = self._request(
            "POST", f"{self.url}/storage/v1/object/{bucket}/{_quote(path)}",
            headers={"Content-Type": content_type,
                     "x-upsert": "true" if upsert else "false",
                     "cache-control": "max-age=31536000"},
            data=payload)
        self._check(response, f"upload {bucket}/{path}")
        return UploadResult(path=path, size_bytes=len(payload))

    def public_url(self, bucket: str, path: str) -> str:
        return f"{self.url}/storage/v1/object/public/{bucket}/{_quote(path)}"

    def object_size(self, bucket: str, path: str) -> int | None:
        """Size of an existing object, or None if it is not there."""
        response = self._request(
            "HEAD", f"{self.url}/storage/v1/object/info/{bucket}/{_quote(path)}", retries=2)
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            return None
        length = response.headers.get("content-length")
        return int(length) if length and length.isdigit() else None

    # ------------------------------------------------------------------
    # Storage — TUS resumable endpoint (proxies)
    # ------------------------------------------------------------------

    def _state_path(self, bucket: str, path: str, size: int) -> Path:
        key = hashlib.sha256(f"{self.url}|{bucket}|{path}|{size}".encode()).hexdigest()[:32]
        return self.state_dir / f"{key}.json"

    def upload_resumable(
        self, bucket: str, path: str, file_path: str | Path,
        content_type: str | None = None, upsert: bool = True,
        chunk_size: int = TUS_CHUNK_SIZE,
        on_progress: Callable[[int, int], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        skip_if_same_size: bool = True,
    ) -> UploadResult:
        """Upload a large object over TUS, resuming a partial upload if one exists.

        *on_progress* is called with ``(bytes_sent, total_bytes)`` after each
        acknowledged chunk — per chunk, not per file, so a 400 MB proxy shows
        movement rather than sitting at zero for ten minutes.
        """
        file_path = Path(file_path)
        if not file_path.exists():
            raise SupabaseError(f"{file_path} does not exist")
        total = file_path.stat().st_size
        content_type = content_type or _guess_type(file_path)

        if skip_if_same_size and self.object_size(bucket, path) == total:
            if on_progress is not None:
                on_progress(total, total)
            return UploadResult(path=path, size_bytes=total, skipped=True)

        state_path = self._state_path(bucket, path, total)
        upload_url, offset = self._resume_point(state_path)
        resumed = upload_url is not None and offset > 0

        if upload_url is None:
            upload_url = self._create_tus_upload(bucket, path, total, content_type, upsert)
            offset = 0
            self._save_state(state_path, upload_url, total)

        if on_progress is not None:
            on_progress(offset, total)

        with file_path.open("rb") as handle:
            while offset < total:
                if should_cancel is not None and should_cancel():
                    # The upload URL stays on disk, so the next run picks up
                    # from here rather than starting the file again.
                    raise Cancelled(f"upload of {path} cancelled at {offset}/{total} bytes")

                handle.seek(offset)
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                try:
                    offset = self._patch_chunk(upload_url, offset, chunk, content_type)
                except SupabaseError:
                    # The server is the authority on how much it has. Ask, and
                    # retry from there; if the upload is gone, start over.
                    server_offset = self._head_offset(upload_url)
                    if server_offset is None:
                        state_path.unlink(missing_ok=True)
                        raise
                    offset = server_offset
                    continue
                self._save_state(state_path, upload_url, total, offset)
                if on_progress is not None:
                    on_progress(offset, total)

        state_path.unlink(missing_ok=True)
        return UploadResult(path=path, size_bytes=total, resumed=resumed)

    def _create_tus_upload(self, bucket: str, path: str, size: int,
                           content_type: str, upsert: bool) -> str:
        metadata = _tus_metadata({
            "bucketName": bucket,
            "objectName": path,
            "contentType": content_type,
            "cacheControl": "3600",
        })
        response = self._request(
            "POST", f"{self.url}/storage/v1/upload/resumable",
            headers={
                "Tus-Resumable": TUS_VERSION,
                "Upload-Length": str(size),
                "Upload-Metadata": metadata,
                "x-upsert": "true" if upsert else "false",
            })
        self._check(response, f"create resumable upload for {bucket}/{path}")
        location = response.headers.get("Location") or response.headers.get("location")
        if not location:
            raise SupabaseError(
                f"resumable upload for {bucket}/{path} returned no Location header")
        if location.startswith("/"):
            location = f"{self.url}{location}"
        return location

    def _patch_chunk(self, upload_url: str, offset: int, chunk: bytes,
                     content_type: str) -> int:
        response = self._request(
            "PATCH", upload_url,
            headers={
                "Tus-Resumable": TUS_VERSION,
                "Upload-Offset": str(offset),
                "Content-Type": "application/offset+octet-stream",
            },
            data=chunk)
        self._check(response, f"upload chunk at offset {offset}")
        new_offset = response.headers.get("Upload-Offset") or response.headers.get("upload-offset")
        if new_offset is None or not new_offset.isdigit():
            raise SupabaseError(f"chunk at offset {offset} returned no Upload-Offset")
        return int(new_offset)

    def _head_offset(self, upload_url: str) -> int | None:
        """Ask the server how many bytes it already holds."""
        try:
            response = self._request(
                "HEAD", upload_url, retries=2,
                headers={"Tus-Resumable": TUS_VERSION})
        except SupabaseError:
            return None
        if response.status_code >= 400:
            return None
        value = response.headers.get("Upload-Offset") or response.headers.get("upload-offset")
        return int(value) if value and value.isdigit() else None

    def _resume_point(self, state_path: Path) -> tuple[str | None, int]:
        """Recover a stored upload URL and the server's true offset."""
        if not state_path.exists():
            return None, 0
        try:
            state = json.loads(state_path.read_text())
        except (OSError, ValueError):
            state_path.unlink(missing_ok=True)
            return None, 0

        upload_url = state.get("upload_url")
        if not upload_url:
            return None, 0
        # Trust the server, not the note we left ourselves: the last chunk may
        # have landed after the process died.
        server_offset = self._head_offset(upload_url)
        if server_offset is None:
            state_path.unlink(missing_ok=True)
            return None, 0
        return upload_url, server_offset

    def _save_state(self, state_path: Path, upload_url: str, total: int,
                    offset: int = 0) -> None:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({
            "upload_url": upload_url,
            "total": total,
            "offset": offset,
            "saved_at": time.time(),
        }))


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _tus_metadata(pairs: dict[str, str]) -> str:
    """Encode TUS Upload-Metadata: ``key <base64 value>``, comma separated."""
    return ",".join(
        f"{key} {base64.b64encode(str(value).encode()).decode()}"
        for key, value in pairs.items())


def _guess_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _quote(path: str) -> str:
    from urllib.parse import quote
    return quote(path.lstrip("/"), safe="/")
