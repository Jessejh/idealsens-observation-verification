"""A stand-in Supabase server: enough of PostgREST, Storage and TUS to test against.

Deliberately in-process and real over HTTP rather than a mocked requests
session, because the things worth testing here — the Location header, the
Upload-Offset handshake, resuming after a dropped connection — are protocol
behaviour, not call shapes.
"""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse


class FakeSupabase:
    def __init__(self):
        self.objects: dict[str, bytes] = {}       # "bucket/path" -> bytes
        self.uploads: dict[str, dict] = {}        # upload id -> {buffer, total, meta}
        self.tables: dict[str, list[dict]] = {}
        self.buckets: set[str] = set()
        self.requests: list[tuple[str, str]] = []
        # Set to a byte count to fail the PATCH that would cross it, once.
        self.fail_patch_after: int | None = None
        self._next_id = 0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_port}"

    def start(self) -> "FakeSupabase":
        state = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # silence the default stderr spew
                pass

            # -- helpers -------------------------------------------------
            def _body(self) -> bytes:
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def _send(self, code: int, body: bytes = b"", headers: dict | None = None):
                headers = headers or {}
                self.send_response(code)
                for key, value in headers.items():
                    self.send_header(key, str(value))
                # A HEAD reply carries the size of the body it would have sent,
                # so only supply a default when the route did not set one.
                if not any(k.lower() == "content-length" for k in headers):
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _json(self, code: int, payload):
                self._send(code, json.dumps(payload).encode(),
                           {"Content-Type": "application/json"})

            # -- routes --------------------------------------------------
            def do_POST(self):
                path = urlparse(self.path).path
                state.requests.append(("POST", path))
                body = self._body()

                if path == "/storage/v1/upload/resumable":
                    meta = {}
                    for pair in (self.headers.get("Upload-Metadata") or "").split(","):
                        if " " in pair:
                            key, value = pair.strip().split(" ", 1)
                            meta[key] = base64.b64decode(value).decode()
                    state._next_id += 1
                    upload_id = f"u{state._next_id}"
                    state.uploads[upload_id] = {
                        "buffer": bytearray(),
                        "total": int(self.headers.get("Upload-Length") or 0),
                        "meta": meta,
                    }
                    return self._send(201, b"", {
                        "Location": f"{state.url}/storage/v1/upload/resumable/{upload_id}",
                        "Tus-Resumable": "1.0.0",
                    })

                if path.startswith("/storage/v1/object/"):
                    key = unquote(path[len("/storage/v1/object/"):])
                    state.objects[key] = body
                    return self._json(200, {"Key": key})

                if path == "/storage/v1/bucket":
                    state.buckets.add(json.loads(body or b"{}").get("name", ""))
                    return self._json(200, {"name": "ok"})

                if path.startswith("/rest/v1/"):
                    table = path[len("/rest/v1/"):]
                    rows = json.loads(body or b"[]")
                    rows = rows if isinstance(rows, list) else [rows]
                    existing = state.tables.setdefault(table, [])
                    for row in rows:
                        match = next((r for r in existing if r.get("id") == row.get("id")), None)
                        if match is not None and "on_conflict" in urlparse(self.path).query:
                            match.update(row)
                        else:
                            existing.append(row)
                    return self._json(201, rows)

                return self._send(404)

            def do_PATCH(self):
                path = urlparse(self.path).path
                state.requests.append(("PATCH", path))
                body = self._body()
                upload_id = path.rsplit("/", 1)[-1]
                upload = state.uploads.get(upload_id)
                if upload is None:
                    return self._send(404)

                offset = int(self.headers.get("Upload-Offset") or 0)
                if offset != len(upload["buffer"]):
                    return self._send(409, b"offset mismatch")

                if (state.fail_patch_after is not None
                        and offset + len(body) > state.fail_patch_after):
                    # Simulate a connection dropped mid-chunk: the server keeps
                    # what it had and the client must ask where it got to.
                    state.fail_patch_after = None
                    return self._send(400, b"simulated connection drop")

                upload["buffer"].extend(body)
                new_offset = len(upload["buffer"])
                if new_offset >= upload["total"]:
                    meta = upload["meta"]
                    key = f"{meta.get('bucketName', '')}/{meta.get('objectName', '')}"
                    state.objects[key] = bytes(upload["buffer"])
                return self._send(204, b"", {"Upload-Offset": str(new_offset),
                                             "Tus-Resumable": "1.0.0"})

            def do_HEAD(self):
                path = urlparse(self.path).path
                state.requests.append(("HEAD", path))

                if path.startswith("/storage/v1/upload/resumable/"):
                    upload = state.uploads.get(path.rsplit("/", 1)[-1])
                    if upload is None:
                        return self._send(404)
                    return self._send(200, b"", {"Upload-Offset": str(len(upload["buffer"])),
                                                 "Upload-Length": str(upload["total"])})

                if path.startswith("/storage/v1/object/info/"):
                    key = unquote(path[len("/storage/v1/object/info/"):])
                    if key not in state.objects:
                        return self._send(404)
                    return self._send(200, b"", {"Content-Length": str(len(state.objects[key]))})

                if path.startswith("/rest/v1/"):
                    table = path[len("/rest/v1/"):]
                    rows = state.tables.get(table, [])
                    return self._send(200, b"", {"Content-Range": f"0-0/{len(rows)}"})

                return self._send(404)

            def do_GET(self):
                path = urlparse(self.path).path
                state.requests.append(("GET", path))
                if path.startswith("/storage/v1/bucket/"):
                    name = path.rsplit("/", 1)[-1]
                    if name in state.buckets:
                        return self._json(200, {"name": name})
                    return self._json(404, {"error": "not found"})
                if path.startswith("/rest/v1/"):
                    return self._json(200, state.tables.get(path[len("/rest/v1/"):], []))
                return self._send(404)

            def do_DELETE(self):
                path = urlparse(self.path).path
                state.requests.append(("DELETE", path))
                return self._json(200, [])

        # Threading: requests keeps connections alive, and a single-threaded
        # server deadlocks the moment a second connection is opened.
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
