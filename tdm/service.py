"""In-process download queue: persistent `Download` records plus a scheduler
that runs up to `max_concurrent` downloads at once via the M1 engine.

# ponytail: one manager lock + condition - fine at 3 concurrent downloads.
Upgrade path: per-download locks if the scheduler itself becomes a bottleneck.
"""

import hmac
import json
import os
import queue
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import requests

from tdm import __version__, engine, netcheck, paths
from tdm.redact import redact, strip_credentials

PERSIST_FIELDS = ("id", "url", "dest_dir", "segments", "state", "filename", "error")
_TERMINAL_STATES = {"done", "error", "cancelled"}
_KEEP_TERMINAL = 50
_NO_RETRY_EXCEPTIONS = (engine.InsufficientSpace, ValueError)  # ValueError covers BlockedURLError
_MAX_BODY = 64 * 1024
_EXT_ROUTES = {"/ext/health", "/ext/flag"}
CATCH_PORT = 8765
_PENDING_MAX = 100


@dataclass
class Download:
    id: str
    url: str
    dest_dir: str
    segments: int
    state: str
    filename: str | None = None
    total: int = 0
    done: int = 0
    bps: float = 0.0
    error: str | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    mode: str | None = field(default=None, repr=False)
    demoted: bool = field(default=False, repr=False)
    seg_progress: list[tuple[int, int]] | None = field(default=None, repr=False)
    # RAM-only, deliberately excluded from PERSIST_FIELDS: forwarded auth
    # headers (e.g. Cookie) must never reach queue.json.
    headers: dict[str, str] | None = field(default=None, repr=False)


class Manager:
    def __init__(
        self, state_path: Path, max_concurrent: int = 3, download_dir: Path | None = None
    ) -> None:
        self.state_path = Path(state_path)
        self.max_concurrent = max_concurrent
        self.download_dir = download_dir
        self._cond = threading.Condition()
        self._downloads: dict[str, Download] = {}
        self._host_caps: dict[str, int] = {}
        self._pool = ThreadPoolExecutor(max_workers=max_concurrent)
        self._running = False
        self._scheduler_thread: threading.Thread | None = None
        self._load()

    def _load(self) -> None:
        """Read an existing state file. Missing or corrupt -> start empty.
        `running`/`paused` mean the previous process stopped mid-download (a
        clean shutdown or a hard kill look the same on disk) - re-queue them
        so the scheduler re-runs `engine.download`, which resumes from its own
        sidecar. `done`/`error`/`cancelled` are kept as-is."""
        text = paths.read_or_none(self.state_path)
        if not text:
            return
        try:
            data = json.loads(text)
        except ValueError:
            return
        for entry in data.get("downloads", []):
            try:
                state = "queued" if entry["state"] in ("running", "paused") else entry["state"]
                dl = Download(
                    id=entry["id"],
                    url=entry["url"],
                    dest_dir=entry["dest_dir"],
                    segments=entry["segments"],
                    state=state,
                    filename=entry.get("filename"),
                    error=entry.get("error"),
                )
            except KeyError:
                continue
            self._downloads[dl.id] = dl

    def add(
        self,
        url: str,
        dest_dir: str | None = None,
        segments: int = 8,
        headers: dict[str, str] | None = None,
    ) -> str:
        netcheck.assert_allowed_url(url)
        dl = Download(
            id=secrets.token_hex(8),
            url=url,
            dest_dir=str(dest_dir if dest_dir is not None else (self.download_dir or ".")),
            segments=segments,
            state="queued",
            headers=headers,
        )
        with self._cond:
            self._downloads[dl.id] = dl
            self._persist_locked()
            self._cond.notify_all()
        return dl.id

    def snapshot(self) -> list[dict]:
        with self._cond:
            return [self._to_snapshot(dl) for dl in self._downloads.values()]

    def snapshot_one(self, id: str) -> dict | None:
        with self._cond:
            dl = self._downloads.get(id)
            return self._to_snapshot(dl) if dl else None

    @staticmethod
    def _to_snapshot(dl: Download) -> dict:
        return {
            "id": dl.id,
            "url": strip_credentials(dl.url),
            "filename": dl.filename,
            "dest_dir": dl.dest_dir,
            "state": dl.state,
            "done": dl.done,
            "total": dl.total,
            "bps": dl.bps,
            "error": dl.error,
        }

    def pause(self, id: str) -> bool:
        with self._cond:
            dl = self._downloads.get(id)
            if dl is None or dl.state not in ("running", "queued"):
                return False
            if dl.state == "running":
                dl.mode = "pause"
                dl.cancel_event.set()
            else:
                dl.state = "paused"
                self._persist_locked()
            return True

    def resume(self, id: str) -> bool:
        with self._cond:
            dl = self._downloads.get(id)
            if dl is None or dl.state not in ("paused", "error"):
                return False
            dl.state = "queued"
            dl.demoted = False
            dl.error = None
            dl.cancel_event = threading.Event()
            dl.mode = None
            self._persist_locked()
            self._cond.notify_all()
            return True

    @staticmethod
    def _url_key(url: str) -> tuple[str | None, int | None, str]:
        """``(hostname, port, path)`` - the parts of a URL that stay fixed
        across a presigned URL's signature rotating (only the query string
        changes)."""
        parts = urlsplit(url)
        return (parts.hostname, parts.port, parts.path)

    def find_resumable(self, url: str) -> list[Download]:
        """Paused/errored downloads whose url has the same
        ``(host, port, path)`` as ``url`` - the identity a presigned URL
        keeps across a signature rotation. Scoped to non-terminal-but-stalled
        downloads only, not the whole queue, so this is a targeted match
        rather than an open-ended scan."""
        key = self._url_key(url)
        with self._cond:
            return [
                dl for dl in self._downloads.values()
                if dl.state in ("paused", "error") and self._url_key(dl.url) == key
            ]

    def rebind(self, id: str, new_url: str, new_resolved_url: str, new_validator: str) -> None:
        """Point a paused/errored download at a fresh URL (same resource, a
        rotated signature) and requeue it. Rewrites the on-disk resume
        sidecar first via ``engine.rebind_progress`` so the requeued run
        resumes from the existing bytes instead of restarting from zero."""
        with self._cond:
            dl = self._downloads.get(id)
            if dl is None or dl.state not in ("paused", "error"):
                raise ValueError(f"not resumable: {id!r}")
            if dl.filename is None:
                raise ValueError(f"unknown filename for {id!r} - cannot rebind sidecar")
            final = Path(dl.dest_dir).resolve() / dl.filename
            engine.rebind_progress(final, new_resolved_url, new_validator)
            dl.url = new_url
            dl.state = "queued"
            dl.demoted = False
            dl.error = None
            dl.cancel_event = threading.Event()
            dl.mode = None
            self._persist_locked()
            self._cond.notify_all()

    def cancel(self, id: str) -> bool:
        with self._cond:
            dl = self._downloads.get(id)
            if dl is None or dl.state in _TERMINAL_STATES:
                return False
            if dl.state == "running":
                dl.mode = "cancel"
                dl.cancel_event.set()
            else:
                dl.state = "cancelled"
                self._persist_locked()
            return True

    def start(self) -> None:
        self._running = True
        self._scheduler_thread = threading.Thread(target=self._scheduler, daemon=True)
        self._scheduler_thread.start()

    def shutdown(self, timeout: float = 15.0) -> None:
        with self._cond:
            self._running = False
            for dl in self._downloads.values():
                if dl.state == "running":
                    dl.mode = "pause"
                    dl.cancel_event.set()
            self._cond.notify_all()
        self._pool.shutdown(wait=True, cancel_futures=False)
        if self._scheduler_thread is not None:
            self._scheduler_thread.join(timeout=timeout)
        with self._cond:
            self._persist_locked()

    def _scheduler(self) -> None:
        with self._cond:
            while self._running:
                running = sum(1 for dl in self._downloads.values() if dl.state == "running")
                queued = next(
                    (dl for dl in self._downloads.values() if dl.state == "queued"), None
                )
                if running < self.max_concurrent and queued is not None:
                    queued.state = "running"
                    self._persist_locked()
                    self._pool.submit(self._run, queued)
                    continue
                self._cond.wait(timeout=0.5)

    def _on_probed(self, dl: Download):
        def _set_filename(filename: str) -> None:
            with self._cond:
                dl.filename = filename
                self._persist_locked()

        return _set_filename

    def _run(self, dl: Download) -> None:
        host = urlsplit(dl.url).hostname or ""
        segs = min(self._host_caps.get(host, dl.segments), engine.MAX_SEGMENTS)
        try:
            final = engine.download(
                dl.url,
                dl.dest_dir,
                segs,
                progress_cb=self._cb(dl),
                cancel=dl.cancel_event,
                segment_cb=self._seg_cb(dl),
                on_probed=self._on_probed(dl),
                headers=dl.headers,
            )
        except engine.Cancelled:
            self._on_cancelled(dl)
            return
        except _NO_RETRY_EXCEPTIONS as exc:
            with self._cond:
                dl.state = "error"
                dl.error = redact(str(exc))
                self._persist_locked()
                self._cond.notify_all()
            return
        except Exception as exc:  # noqa: BLE001 - transient failure -> demote-and-retry once
            with self._cond:
                if host not in self._host_caps and not dl.demoted:
                    self._host_caps[host] = 2
                    dl.demoted = True
                    dl.state = "queued"
                else:
                    dl.state = "error"
                    dl.error = redact(str(exc))
                self._persist_locked()
                self._cond.notify_all()
            return
        with self._cond:
            dl.state = "done"
            dl.filename = final.name
            self._persist_locked()
            self._cond.notify_all()

    def _on_cancelled(self, dl: Download) -> None:
        if dl.mode == "cancel":
            self._cleanup_files(dl)
            with self._cond:
                dl.state = "cancelled"
                dl.mode = None
                self._persist_locked()
                self._cond.notify_all()
        else:  # paused - .part and the sidecar are kept for a later resume
            with self._cond:
                dl.state = "paused"
                dl.cancel_event = threading.Event()
                dl.mode = None
                self._persist_locked()
                self._cond.notify_all()

    @staticmethod
    def _cleanup_files(dl: Download) -> None:
        """Best-effort: re-probe to learn the filename `engine.download` would
        have used, then delete its `.part` and sidecar. A cancel that lands
        while the server is unreachable simply leaves them for a later add."""
        try:
            final = Path(dl.dest_dir) / engine._probe(dl.url).filename
            engine._part_path(final).unlink(missing_ok=True)
            engine._meta_path(final).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001, S110 - best-effort cleanup, see docstring
            pass

    def _cb(self, dl: Download):
        def _progress(done: int, total: int, bps: float) -> None:
            dl.done, dl.total, dl.bps = done, total, bps

        return _progress

    def _seg_cb(self, dl: Download):
        def _seg_progress(segments: list[tuple[int, int]]) -> None:
            dl.seg_progress = segments

        return _seg_progress

    def segments_of(self, id_: str) -> list[tuple[int, int]] | None:
        with self._cond:
            dl = self._downloads.get(id_)
            return dl.seg_progress if dl else None

    def _keep(self) -> list[Download]:
        active = [dl for dl in self._downloads.values() if dl.state not in _TERMINAL_STATES]
        terminal = [dl for dl in self._downloads.values() if dl.state in _TERMINAL_STATES]
        return active + terminal[-_KEEP_TERMINAL:]

    def _persist_locked(self) -> None:
        downloads = []
        for dl in self._keep():
            entry = {k: getattr(dl, k) for k in PERSIST_FIELDS}
            entry["url"] = strip_credentials(entry["url"])
            downloads.append(entry)
        paths.write_atomic(self.state_path, json.dumps({"downloads": downloads}))


# --- HTTP layer --------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # silence request logging in tests
        pass

    def _authed(self) -> bool:
        presented = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not presented.startswith(prefix):
            return False
        return hmac.compare_digest(self.server.token, presented[len(prefix) :])

    def _allowed(self, method: str) -> bool:
        """Bearer token always works. Otherwise, only a POST to an `/ext/*`
        route with a matching `Origin` (and, for `/ext/flag`, a matching
        `X-Ext-Token`) is let through - this is what lets the browser
        extension reach those two routes without ever seeing the Bearer
        token."""
        if self._authed():
            return True
        if method != "POST" or self.path not in _EXT_ROUTES:
            return False
        if self.server.allowed_origin is None:
            return False
        if self.headers.get("Origin") != self.server.allowed_origin:
            return False
        if self.path == "/ext/flag":
            presented = self.headers.get("X-Ext-Token", "")
            if not self.server.ext_token or not hmac.compare_digest(
                self.server.ext_token, presented
            ):
                return False
        return True

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > _MAX_BODY:
            raise ValueError("request body too large")
        raw = self.rfile.read(length)
        return json.loads(raw) if raw else {}

    def do_GET(self) -> None:
        if not self._authed():
            self._send_json(401, {"error": "unauthorized"})
            return
        manager: Manager = self.server.manager
        path = self.path
        if path == "/health":
            self._send_json(
                200, {"ok": True, "version": __version__, "downloads": len(manager.snapshot())}
            )
        elif path == "/downloads":
            self._send_json(200, manager.snapshot())
        elif path.startswith("/downloads/"):
            snap = manager.snapshot_one(path[len("/downloads/") :])
            self._send_json(200, snap) if snap is not None else self._send_json(
                404, {"error": "not found"}
            )
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._allowed("POST"):
            self._send_json(401, {"error": "unauthorized"})
            return
        manager: Manager = self.server.manager
        path = self.path
        if path == "/ext/health":
            self._send_json(
                200, {"ok": True, "version": __version__, "token": self.server.ext_token}
            )
        elif path == "/ext/flag":
            self._handle_flag()
        elif path == "/downloads":
            self._handle_add(manager)
        elif path == "/shutdown":
            self._send_json(202, {"ok": True})
            threading.Thread(target=self._shutdown, args=(self.server,), daemon=True).start()
        elif path.startswith("/downloads/"):
            self._handle_action(manager, path[len("/downloads/") :])
        else:
            self._send_json(404, {"error": "not found"})

    def _handle_flag(self) -> None:
        try:
            body = self._read_json_body()
        except ValueError:
            self._send_json(400, {"error": "bad request"})
            return
        url = body.get("url")
        if not isinstance(url, str) or not url:
            self._send_json(400, {"error": "missing url"})
            return
        headers = body.get("headers")
        if headers is not None and (
            not isinstance(headers, dict)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items())
        ):
            self._send_json(400, {"error": "bad headers"})
            return
        try:
            netcheck.assert_allowed_url(url)
        except netcheck.BlockedURLError:
            self._send_json(400, {"error": "blocked"})
            return
        if self.server.pending is None:
            self._send_json(404, {"error": "not found"})
            return
        item = (url, headers) if headers else url
        try:
            self.server.pending.put_nowait(item)
        except queue.Full:
            self._send_json(503, {"error": "busy"})
            return
        self._send_json(202, {"ok": True})

    def _handle_add(self, manager: "Manager") -> None:
        try:
            body = self._read_json_body()
        except ValueError:
            self._send_json(400, {"error": "bad request"})
            return
        url = body.get("url")
        if not isinstance(url, str) or not url:
            self._send_json(400, {"error": "missing url"})
            return
        try:
            segments = max(1, min(int(body.get("segments", 8)), engine.MAX_SEGMENTS))
        except (TypeError, ValueError):
            self._send_json(400, {"error": "bad request"})
            return
        try:
            id_ = manager.add(url, dest_dir=body.get("dest_dir"), segments=segments)
        except netcheck.BlockedURLError:
            self._send_json(400, {"error": "blocked"})
            return
        self._send_json(201, {"id": id_})

    def _handle_action(self, manager: "Manager", rest: str) -> None:
        parts = rest.split("/")
        if len(parts) != 2 or parts[1] not in ("pause", "resume", "cancel"):
            self._send_json(404, {"error": "not found"})
            return
        id_, action = parts
        if manager.snapshot_one(id_) is None:
            self._send_json(404, {"error": "not found"})
            return
        getattr(manager, action)(id_)
        self._send_json(202, {"ok": True})

    @staticmethod
    def _shutdown(server: "_Server") -> None:
        server.manager.shutdown()
        server.shutdown()
        if server.pending is not None:
            server.pending.put(None)  # unblock run_catch()'s _confirm_loop


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        manager: Manager,
        token: str,
        host: str = "127.0.0.1",
        port: int = 0,
        allowed_origin: str | None = None,
        ext_token: str | None = None,
        pending: "queue.Queue | None" = None,
    ):
        # set before super().__init__() - it calls server_bind(), which reads
        # self.pending to decide whether to harden the socket for catch mode.
        self.manager = manager
        self.token = token
        self.allowed_origin = allowed_origin
        self.ext_token = ext_token
        self.pending = pending
        super().__init__((host, port), _Handler)

    def server_bind(self) -> None:
        # Windows hardening for catch mode's fixed, predictable CATCH_PORT:
        # stops another process from silently binding it out from under a
        # running `tdm catch` (see milestone-3-plan.md Risks). SO_EXCLUSIVEADDRUSE
        # and SO_REUSEADDR conflict on Windows, so this also turns the latter
        # off for this instance - fine here since catch mode never needs the
        # TIME_WAIT-reuse behavior `run_service()`'s ephemeral port relies on.
        if sys.platform == "win32" and self.pending is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            self.allow_reuse_address = False
        super().server_bind()


def serve_in_thread(
    manager: Manager,
    token: str,
    host: str = "127.0.0.1",
    port: int = 0,
    allowed_origin: str | None = None,
    ext_token: str | None = None,
    pending: "queue.Queue | None" = None,
) -> tuple[_Server, int]:
    """Start `_Server` on its own daemon thread; returns the server (so the
    caller can `.shutdown()` it) and the bound port."""
    server = _Server(manager, token, host, port, allowed_origin, ext_token, pending)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


# --- process management -------------------------------------------------------

_STATE_FILES = ("service.port", "service.token", "service.pid")


def read_endpoint() -> tuple[str, str] | None:
    """`(base_url, token)` for the running service, or `None` if not running."""
    d = paths.state_dir()
    port = paths.read_or_none(d / "service.port")
    token = paths.read_or_none(d / "service.token")
    if not port or not token:
        return None
    return f"http://127.0.0.1:{port}", token


def _clear_state_files(d: Path) -> None:
    for name in _STATE_FILES:
        (d / name).unlink(missing_ok=True)


def run_service() -> None:
    """Blocking: run the HTTP service in the foreground until shutdown (via
    `POST /shutdown` or SIGTERM/SIGBREAK). This is what `python -m tdm
    _serve` execs as a detached child."""
    d = paths.state_dir()
    token = secrets.token_urlsafe(32)
    manager = Manager(d / "queue.json", download_dir=paths.default_download_dir())
    server = _Server(manager, token)
    paths.write_atomic(d / "service.port", str(server.server_address[1]))
    paths.write_atomic(d / "service.token", token)
    paths.write_atomic(d / "service.pid", str(os.getpid()))
    manager.start()

    def _stop() -> None:
        manager.shutdown()
        server.shutdown()

    def _on_signal(signum, frame) -> None:
        threading.Thread(target=_stop, daemon=True).start()

    try:
        signal.signal(signal.SIGTERM, _on_signal)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, _on_signal)
    except ValueError:
        pass  # not the main thread (e.g. run under a test) - /shutdown still works

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    thread.join()
    manager.shutdown()
    server.server_close()
    _clear_state_files(d)


def _healthy(endpoint: tuple[str, str] | None) -> bool:
    """`GET /health` with a short timeout; `True` only on a clean 200."""
    if endpoint is None:
        return False
    base_url, token = endpoint
    try:
        r = requests.get(
            f"{base_url}/health", headers={"Authorization": f"Bearer {token}"}, timeout=2
        )
        return r.status_code == 200
    except requests.RequestException:
        return False


def resolve_origin(extension_id: str | None) -> str:
    """The extension's `Origin` header value (`chrome-extension://<id>`),
    persisting a newly-given id to `state_dir()/extension_id` so a later bare
    `tdm catch` reuses it. Raises `SystemExit` when neither a fresh id nor a
    stored one is available."""
    path = paths.state_dir() / "extension_id"
    if extension_id:
        paths.write_atomic(path, extension_id)
    else:
        extension_id = paths.read_or_none(path)
    if not extension_id:
        raise SystemExit("no extension id - run: tdm catch --extension-id <id>")
    return f"chrome-extension://{extension_id}"


def resolve_ext_token() -> str:
    """A random per-install secret for `/ext/flag`, persisted to
    `state_dir()/ext_token` so it survives across `tdm catch` runs - the
    extension picks it up automatically from `/ext/health`, no user action
    needed."""
    path = paths.state_dir() / "ext_token"
    token = paths.read_or_none(path)
    if token:
        return token
    token = secrets.token_hex(16)
    paths.write_atomic(path, token)
    return token


def _bar(frac: float, width: int = 20) -> str:
    return "#" * int(max(0.0, min(1.0, frac)) * width)


def _frame_rows(manager: Manager, watched: dict[str, int]) -> list[dict]:
    """Build the data for one redraw tick from `watched` (id -> phase: 0 while
    active, 1 once a terminal state's "combining" line has been shown). Mutates
    `watched` - advances or drops an id - but does no I/O; `_format_frame` turns
    the result into printable lines."""
    rows = []
    for id_ in list(watched):
        snap = manager.snapshot_one(id_)
        if snap is None:
            del watched[id_]
            continue
        name = snap.get("filename") or "..."
        state = snap["state"]
        if state in _TERMINAL_STATES:
            if watched[id_] == 0:
                segs = manager.segments_of(id_) or []
                rows.append({"phase": "combining", "name": name, "segments": segs})
                watched[id_] = 1
            else:
                rows.append({
                    "phase": state, "name": name,
                    "error": snap.get("error"), "dest_dir": snap.get("dest_dir"),
                })
                del watched[id_]
        else:
            rows.append(
                {
                    "phase": "active",
                    "name": name,
                    "done": snap["done"],
                    "total": snap["total"],
                    "segments": manager.segments_of(id_),
                }
            )
    return rows


def _format_frame(rows: list[dict]) -> list[str]:
    """Pure: turn `_frame_rows()` output into printable lines - no I/O, so this
    is the part unit tests exercise directly."""
    lines: list[str] = []
    for row in rows:
        phase = row["phase"]
        if phase == "combining":
            lines.append(f"combining {len(row['segments'])} segments -> {row['name']}")
        elif phase == "done":
            hint = engine.extension_hint(row["name"])
            if hint is not None:
                lines.append(hint)
            dest_dir = row.get("dest_dir")
            saved_path = str(Path(dest_dir) / row["name"]) if dest_dir else row["name"]
            lines.append(f"Saved to {saved_path}")
            lines.append("Ready for a new link.")
        elif phase == "error":
            lines.append(f"error: {row.get('error') or ''} -> {row['name']}")
            lines.append("Ready for a new link.")
        elif phase == "cancelled":
            lines.append(f"cancelled -> {row['name']}")
            lines.append("Ready for a new link.")
        else:
            for i, (d, t) in enumerate(row.get("segments") or []):
                frac = d / t if t else 1.0
                lines.append(f"  seg {i}: [{_bar(frac):<20}] {frac * 100:3.0f}%")
            frac = row["done"] / row["total"] if row["total"] else 0.0
            lines.append(f"{row['name']} [{_bar(frac):<20}] {frac * 100:3.0f}%")
    return lines


def _render_active(manager: Manager, watched: dict[str, int], last_count: int) -> int:
    """I/O wrapper: prints any newly-terminal ("combining"/done/error/
    cancelled) lines permanently via a normal scrolling `print()` - never
    erased - then erases and redraws only the still-active downloads' live
    progress lines (ANSI cursor-up + clear-to-end, stdlib escape codes - no
    dependency). Returns the new live-region line count so the next call
    knows how far to erase. Keeping the two separate is what stops a
    just-finished download's completion message from being wiped the moment
    its id drops out of `watched` on the next tick."""
    rows = _frame_rows(manager, watched)
    permanent_rows = [r for r in rows if r["phase"] != "active"]
    live_rows = [r for r in rows if r["phase"] == "active"]

    if permanent_rows:
        if last_count:
            sys.stdout.write(f"\x1b[{last_count}A\x1b[J")
            last_count = 0
        for line in _format_frame(permanent_rows):
            print(line)

    lines = _format_frame(live_rows)
    if last_count:
        sys.stdout.write(f"\x1b[{last_count}A\x1b[J")
    if lines:
        sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()
    return len(lines)


def _try_resume(manager: Manager, url: str, headers: dict[str, str] | None) -> str | bool | None:
    """If `url` looks like a fresh link for a paused/errored download (same
    host+port+path per `Manager.find_resumable`, confirmed by a live probe's
    size and Accept-Ranges matching the paused download's), prompt to resume
    it instead of the plain new-download prompt. Returns the resumed
    download's id on acceptance, ``False`` if the user explicitly declined a
    real match (the caller should skip the link entirely, not fall through
    to a plain add), or ``None`` if there was no safe match (the caller
    should fall back to a normal `manager.add`)."""
    candidates = manager.find_resumable(url)
    if not candidates:
        return None
    if len(candidates) > 1:
        print("Multiple paused downloads match this link:")
        for c in candidates:
            print(f"  {c.id}: {c.filename or '...'} ({c.dest_dir})")
        choice = input("  resume which id (blank to start a new download instead)? ").strip()
        match = next((c for c in candidates if c.id == choice), None)
        if match is None:
            return None
    else:
        match = candidates[0]
    try:
        probe = engine._probe(url, headers)
    except Exception:  # noqa: BLE001 - any probe failure -> fall back to a normal add
        return None
    if probe.size != match.total or not probe.accept_ranges:
        return None
    print(f"Found paused download {match.filename or match.id} "
          f"({match.done}/{match.total} bytes done).")
    if input("  resume with this link? [y/n] ").strip().lower() != "y":
        return False
    manager.rebind(match.id, url, probe.resolved_url, probe.validator)
    return match.id


def _confirm_loop(manager: Manager, pending: "queue.Queue") -> None:
    """Runs on the main thread: ask `y/n` for each flagged URL, in arrival
    order, and redraw the live per-segment/aggregate progress of every
    confirmed-but-unfinished download on each tick. A `None` sentinel exits the
    loop (used at shutdown). `get(timeout=...)` rather than a bare blocking
    `get` so Ctrl-C can interrupt it on Windows. Before the plain
    new-download prompt, `_try_resume` checks whether the link is a fresh
    URL for an existing paused/errored download (Approach #3)."""
    watched: dict[str, int] = {}
    last_count = 0
    while True:
        try:
            item = pending.get(timeout=0.5)
        except queue.Empty:
            last_count = _render_active(manager, watched, last_count)
            continue
        if item is None:
            return
        url, headers = item if isinstance(item, tuple) else (item, None)
        print(redact(url))
        resumed_id = _try_resume(manager, url, headers)
        if resumed_id is False:
            pass  # user declined a real resume match - skip this link entirely
        elif resumed_id is not None:
            watched[resumed_id] = 0
        elif input("  [y/n]? ").strip().lower() == "y":
            try:
                watched[manager.add(url, headers=headers)] = 0
            except Exception as exc:  # noqa: BLE001 - one bad link must not kill the loop
                print(redact(str(exc)))
        last_count = _render_active(manager, watched, last_count)


def run_catch(origin: str) -> None:
    """Blocking: run in the foreground with browser-flagged downloads confirmed
    `y/n` on the main thread before they queue. This is what `python -m tdm
    catch` runs directly (never detaches, unlike `run_service()`)."""
    if _healthy(read_endpoint()):
        raise SystemExit("tdm service already running - run: tdm stop")
    d = paths.state_dir()
    _clear_state_files(d)
    token = secrets.token_urlsafe(32)
    ext_token = resolve_ext_token()
    pending: queue.Queue = queue.Queue(maxsize=_PENDING_MAX)
    manager = Manager(d / "queue.json", download_dir=paths.default_download_dir())
    server = _Server(
        manager,
        token,
        port=CATCH_PORT,
        allowed_origin=origin,
        ext_token=ext_token,
        pending=pending,
    )
    paths.write_atomic(d / "service.port", str(server.server_address[1]))
    paths.write_atomic(d / "service.token", token)
    paths.write_atomic(d / "service.pid", str(os.getpid()))
    manager.start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if sys.platform == "win32":
        os.system("")  # enable ANSI/VT processing so _render_active's escapes work in cmd.exe
    try:
        _confirm_loop(manager, pending)
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        while not pending.empty():
            leftover = pending.get_nowait()
            if leftover is not None:
                url = leftover[0] if isinstance(leftover, tuple) else leftover
                print(f"unanswered: {redact(url)}")
        manager.shutdown()
        server.shutdown()
        server.server_close()
        _clear_state_files(d)


def spawn_detached() -> str:
    """Start `run_service()` in a detached child process if one isn't already
    healthy; returns a one-line status message."""
    endpoint = read_endpoint()
    if _healthy(endpoint):
        return f"already running on {endpoint[0]}"

    d = paths.state_dir()
    _clear_state_files(d)
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    with open(d / "service.log", "ab") as log_fh:
        subprocess.Popen(
            [sys.executable, "-m", "tdm", "_serve"],
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            close_fds=True,
            **kwargs,
        )
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        endpoint = read_endpoint()
        if endpoint is not None:
            return endpoint[0]
        time.sleep(0.1)
    raise RuntimeError("service did not start; see service.log")


def stop_service() -> str:
    """Ask the running service to shut down (HTTP first, then a hard kill if
    it doesn't within 10s). Returns a one-line status message."""
    endpoint = read_endpoint()
    if endpoint is None:
        return "not running"
    base_url, token = endpoint
    d = paths.state_dir()
    try:
        requests.post(
            f"{base_url}/shutdown", headers={"Authorization": f"Bearer {token}"}, timeout=5
        )
    except requests.RequestException:
        pass
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and (d / "service.port").exists():
        time.sleep(0.2)
    if (d / "service.port").exists():
        pid = paths.read_or_none(d / "service.pid")
        if pid:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True, check=False)
            else:
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except OSError:
                    pass
    _clear_state_files(d)
    return "stopped"
