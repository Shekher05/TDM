"""Local range-capable HTTP server fixture for the engine tests.

`make_server(data)` starts a real `ThreadingHTTPServer` on `127.0.0.1:0` serving
a fixed byte blob. It honours `Range` (206 + `Content-Range`), reports an `ETag`,
and counts body bytes actually written. Two failure-injection toggles, per the
milestone-1 plan (Revision 2):

- `no_etag=True`   — responses carry no `ETag` (resume must refuse to trust the
  sidecar and restart clean).
- `drop_after=<n>` — the first response that would exceed `n` body bytes writes
  only `n` and drops the connection, then the server behaves normally
  (exercises the segment retry loop).
- `ignore_range_end=True` — a `Range` request is answered `206` from the
  requested start but with the whole tail of the file, not just the requested
  slice (a server that honours `start` and ignores `end` — each segment must
  clamp its own write or it overruns the next segment).
- `segment_status=<code>` — the first `Range` request the server sees answers
  with `<code>` and an empty body (one-shot), then normal service resumes
  (exercises Manager host demotion).
- `silent_drop_after=<n>` — the first `Range` response writes only `n` body
  bytes and omits `Content-Length` entirely, then the connection closes
  (one-shot; normal service resumes after). Unlike `drop_after` (which still
  declares the full `Content-Length` and so makes `requests` raise on the
  mismatch), this is a *clean* short close with no length promise broken -
  the real-world "server FIN's the socket early, no exception raised" case.
- `require_header=(name, value)` — every request must carry header `name`
  set to exactly `value` or the server answers `403` with no body; used to
  prove a caller-supplied `headers` dict actually reaches the wire.
"""
import http.server
import threading

import pytest

from tdm import netcheck


@pytest.fixture(autouse=True)
def _allow_loopback_for_local_test_server(monkeypatch):
    """`make_server` binds to 127.0.0.1, which `netcheck.assert_allowed_url`
    (correctly) rejects as loopback. Bypass it for the whole suite so the real
    fixture keeps working; tests that exercise the real SSRF check re-install
    the real function for themselves (see test_engine.py's
    `_real_assert_allowed_url` import)."""
    monkeypatch.setattr(netcheck, "assert_allowed_url", lambda url: None)


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # connection closes at handler return -> a short write reads as a drop

    def log_message(self, *args):  # silence test output
        pass

    def _shared_headers(self):
        if not self.server.no_etag:
            self.send_header("ETag", '"test-etag"')
        if self.server.support_range:
            self.send_header("Accept-Ranges", "bytes")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.server.data)))
        self._shared_headers()
        self.end_headers()

    def do_GET(self):
        req = self.server.require_header
        if req is not None and self.headers.get(req[0]) != req[1]:
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = self.server.data
        rng = self.headers.get("Range")
        if rng and self.server.support_range:
            status = self.server.take_segment_status()
            if status is not None:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self._shared_headers()
                self.end_headers()
                return
            spec = rng.split("=", 1)[1]
            start_s, _, end_s = spec.partition("-")
            start = int(start_s)
            end = int(end_s) if end_s else len(data) - 1
            if self.server.ignore_range_end:
                end = len(data) - 1  # honours start, ignores the requested end
            body = data[start : end + 1]
            is_probe = start == 0 and end == 0  # the 1-byte accept-ranges probe - not a real segment
            silent = None if is_probe else self.server.take_silent_drop()
            if silent is not None:
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                self._shared_headers()  # no Content-Length: body ends when the socket closes
                self.end_headers()
                self.wfile.write(body[:silent])
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        else:
            body = data
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self._shared_headers()
        self.end_headers()
        self.wfile.write(self.server.take_body(body))


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, data, support_range, no_etag, drop_after, ignore_range_end,
                 segment_status=None, silent_drop_after=None, require_header=None):
        super().__init__(("127.0.0.1", 0), _RangeHandler)
        self.data = data
        self.support_range = support_range
        self.no_etag = no_etag
        self.ignore_range_end = ignore_range_end
        self._drop_after = drop_after
        self.segment_status = segment_status
        self._silent_drop_after = silent_drop_after
        self.require_header = require_header
        self._lock = threading.Lock()
        self.served_bytes = 0

    def take_segment_status(self):
        """One-shot: the first Range request after this is armed gets this
        status code (and an empty body), then normal service resumes."""
        with self._lock:
            status, self.segment_status = self.segment_status, None
            return status

    def take_silent_drop(self):
        """One-shot: the first Range request after this is armed gets a
        Content-Length-less short write (clean close, no exception), then
        normal service resumes."""
        with self._lock:
            n, self._silent_drop_after = self._silent_drop_after, None
            return n

    def take_body(self, body):
        """Bytes to actually write for one response: a one-shot `drop_after`
        truncates the first over-limit response, then normal service resumes.
        Also accumulates the served-bytes total."""
        with self._lock:
            if self._drop_after is not None and len(body) > self._drop_after:
                body = body[: self._drop_after]
                self._drop_after = None
            self.served_bytes += len(body)
        return body

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/file.bin"


@pytest.fixture
def make_server():
    servers = []

    def _make(data: bytes, support_range: bool = True, no_etag: bool = False,
              drop_after: int | None = None, ignore_range_end: bool = False,
              segment_status: int | None = None, silent_drop_after: int | None = None,
              require_header: tuple[str, str] | None = None) -> _Server:
        server = _Server(data, support_range, no_etag, drop_after, ignore_range_end,
                          segment_status, silent_drop_after, require_header)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server

    yield _make
    for server in servers:
        server.shutdown()
