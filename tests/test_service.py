import json
import os
import queue
import threading
import time
from pathlib import Path

import pytest
import requests

from tdm import netcheck, paths, service
from tdm.engine import InsufficientSpace
from tdm.netcheck import BlockedURLError
from tdm.netcheck import assert_allowed_url as _real_assert_allowed_url
from tdm.service import PERSIST_FIELDS, Manager, serve_in_thread


def _poll(mgr, id_, states, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = mgr.snapshot_one(id_)
        if snap and snap["state"] in states:
            return snap
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {states}; last snapshot: {mgr.snapshot_one(id_)}")


def test_add_queues_and_persists(tmp_path, make_server):
    server = make_server(os.urandom(1024))
    mgr = Manager(tmp_path / "queue.json", download_dir=tmp_path / "dl")
    id_ = mgr.add(server.url)

    assert mgr.snapshot_one(id_)["state"] == "queued"
    data = json.loads((tmp_path / "queue.json").read_text())
    assert len(data["downloads"]) == 1
    entry = data["downloads"][0]
    assert set(entry) == set(PERSIST_FIELDS)
    assert entry["id"] == id_
    assert entry["state"] == "queued"


def test_add_never_persists_credentials(tmp_path):
    mgr = Manager(tmp_path / "queue.json", download_dir=tmp_path / "dl")
    mgr.add("http://alice:s3cr3t@example.com/file.zip")
    contents = (tmp_path / "queue.json").read_text()
    assert "s3cr3t" not in contents
    assert "s3cr3t" not in json.dumps(mgr.snapshot())


def test_add_rejects_private_url(tmp_path, monkeypatch):
    monkeypatch.setattr(netcheck, "assert_allowed_url", _real_assert_allowed_url)
    mgr = Manager(tmp_path / "queue.json")
    with pytest.raises(BlockedURLError):
        mgr.add("http://127.0.0.1:9/x")
    assert not (tmp_path / "queue.json").exists()


# --- forwarded headers (Approach #2) ---------------------------------------


def test_manager_add_accepts_and_forwards_headers(tmp_path, make_server):
    blob = os.urandom(64 * 1024)
    server = make_server(blob, require_header=("Cookie", "session=abc"))
    dl_dir = tmp_path / "dl"
    mgr = Manager(tmp_path / "queue.json", download_dir=dl_dir)
    id_ = mgr.add(server.url, headers={"Cookie": "session=abc"})
    mgr.start()
    try:
        snap = _poll(mgr, id_, {"done", "error"}, timeout=15)
        assert snap["state"] == "done"
        out = dl_dir / snap["filename"]
        assert out.read_bytes() == blob
    finally:
        mgr.shutdown()


def test_manager_add_never_persists_headers(tmp_path, make_server):
    server = make_server(os.urandom(1024))
    mgr = Manager(tmp_path / "queue.json", download_dir=tmp_path / "dl")
    mgr.add(server.url, headers={"Cookie": "session=s3cr3t"})
    assert "s3cr3t" not in (tmp_path / "queue.json").read_text()
    assert "s3cr3t" not in json.dumps(mgr.snapshot())


def test_manager_error_state_never_leaks_forwarded_headers(tmp_path):
    mgr = Manager(tmp_path / "queue.json", download_dir=tmp_path / "dl")
    id_ = mgr.add("http://127.0.0.1:1/unreachable", headers={"Cookie": "session=s3cr3t"})
    mgr.start()
    try:
        snap = _poll(mgr, id_, {"error"}, timeout=10)
        assert snap["state"] == "error"
        assert "s3cr3t" not in json.dumps(snap)
        assert "s3cr3t" not in (tmp_path / "queue.json").read_text()
    finally:
        mgr.shutdown()


def test_ext_flag_queues_url_and_headers_as_tuple(ext_http):
    _mgr, base, pending = ext_http
    r = requests.post(
        base + "/ext/flag",
        json={"url": "http://h/f", "headers": {"Cookie": "session=abc"}},
        headers={"Origin": EXT_ORIGIN, "X-Ext-Token": EXT_TOKEN},
        timeout=5,
    )
    assert r.status_code == 202
    assert pending.get_nowait() == ("http://h/f", {"Cookie": "session=abc"})


def test_ext_flag_without_headers_still_queues_bare_url(ext_http):
    """Backward compatible with the pre-existing plain-string queue item
    shape when the extension sends no headers (older extension build, or a
    site that needs no auth)."""
    _mgr, base, pending = ext_http
    r = requests.post(
        base + "/ext/flag",
        json={"url": "http://h/f"},
        headers={"Origin": EXT_ORIGIN, "X-Ext-Token": EXT_TOKEN},
        timeout=5,
    )
    assert r.status_code == 202
    assert pending.get_nowait() == "http://h/f"


def test_ext_flag_rejects_non_dict_headers(ext_http):
    _mgr, base, pending = ext_http
    r = requests.post(
        base + "/ext/flag",
        json={"url": "http://h/f", "headers": "not-a-dict"},
        headers={"Origin": EXT_ORIGIN, "X-Ext-Token": EXT_TOKEN},
        timeout=5,
    )
    assert r.status_code == 400
    assert pending.empty()


def test_confirm_loop_forwards_headers_from_tuple_items(tmp_path, monkeypatch):
    mgr = Manager(tmp_path / "queue.json", download_dir=tmp_path / "dl")
    pending = queue.Queue()
    pending.put(("http://h/keep", {"Cookie": "session=abc"}))
    pending.put(None)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    service._confirm_loop(mgr, pending)
    dl = next(iter(mgr._downloads.values()))
    assert dl.headers == {"Cookie": "session=abc"}


def test_scheduler_promotes_up_to_max(tmp_path, make_server):
    server = make_server(os.urandom(4 * 1024 * 1024))
    mgr = Manager(tmp_path / "queue.json", max_concurrent=3)
    ids = [mgr.add(server.url, dest_dir=str(tmp_path / f"dl{i}")) for i in range(5)]
    mgr.start()
    try:
        deadline = time.monotonic() + 5
        running_ids = set()
        while time.monotonic() < deadline:
            snaps = {s["id"]: s["state"] for s in mgr.snapshot()}
            running_ids = {i for i in ids if snaps.get(i) == "running"}
            if len(running_ids) == 3:
                break
            time.sleep(0.02)
        assert len(running_ids) == 3
        assert sum(1 for i in ids if i not in running_ids) == 2

        for i in ids:
            snap = _poll(mgr, i, {"done", "error"}, timeout=15)
            assert snap["state"] == "done"
    finally:
        mgr.shutdown()


def test_download_completes_and_checksum_matches(tmp_path, make_server):
    blob = os.urandom(512 * 1024)
    server = make_server(blob)
    dl_dir = tmp_path / "dl"
    mgr = Manager(tmp_path / "queue.json", download_dir=dl_dir)
    id_ = mgr.add(server.url)
    mgr.start()
    try:
        snap = _poll(mgr, id_, {"done", "error"})
        assert snap["state"] == "done"
        out = dl_dir / snap["filename"]
        assert out.read_bytes() == blob
        assert not out.with_name(out.name + ".part").exists()
        assert not out.with_name(out.name + ".tdm.json").exists()
    finally:
        mgr.shutdown()


def test_manager_populates_seg_progress(tmp_path, make_server):
    blob = os.urandom(512 * 1024)
    server = make_server(blob)
    dl_dir = tmp_path / "dl"
    mgr = Manager(tmp_path / "queue.json", download_dir=dl_dir)
    id_ = mgr.add(server.url, segments=4)
    mgr.start()
    try:
        snap = _poll(mgr, id_, {"done", "error"})
        assert snap["state"] == "done"
        segs = mgr.segments_of(id_)
        assert segs is not None
        assert len(segs) == 4
        assert all(done == total for done, total in segs)
    finally:
        mgr.shutdown()


def test_hard_error_sets_error_state(tmp_path, make_server, monkeypatch):
    server = make_server(os.urandom(1024))
    mgr = Manager(tmp_path / "queue.json", download_dir=tmp_path / "dl")

    def boom(*a, **k):
        raise InsufficientSpace("no room")

    monkeypatch.setattr("tdm.engine.download", boom)
    id_ = mgr.add(server.url)
    mgr.start()
    try:
        snap = _poll(mgr, id_, {"error"})
        assert snap["state"] == "error"
        assert snap["error"]
    finally:
        mgr.shutdown()


def test_error_message_is_redacted(tmp_path, make_server, monkeypatch):
    server = make_server(os.urandom(1024))
    mgr = Manager(tmp_path / "queue.json", download_dir=tmp_path / "dl")

    def boom(*a, **k):
        raise RuntimeError("http://user:pw@h/x failed")

    monkeypatch.setattr("tdm.engine.download", boom)
    id_ = mgr.add(server.url)
    mgr.start()
    try:
        snap = _poll(mgr, id_, {"error"})
        assert "pw" not in snap["error"]
    finally:
        mgr.shutdown()


# --- pause, resume, cancel, host demotion (Milestone 2 Task 6) -------------


def _wrap_cb_with_trigger(monkeypatch, mgr, on_progress):
    """Patch Manager._cb so the returned progress callback also invokes
    `on_progress(mgr, dl.id)` once `done > 0` - synchronous, same thread as the
    engine's own cancel check, so there is no cross-thread timing race."""
    orig_cb = Manager._cb
    fired = threading.Event()

    def _cb(self, dl):
        inner = orig_cb(self, dl)

        def wrapped(done, total, bps):
            inner(done, total, bps)
            if done > 0 and not fired.is_set():
                fired.set()
                on_progress(mgr, dl.id)

        return wrapped

    monkeypatch.setattr(Manager, "_cb", _cb)


def test_pause_keeps_part_then_resume_completes(tmp_path, make_server, monkeypatch):
    blob = os.urandom(4 * 1024 * 1024)
    server = make_server(blob)
    dl_dir = tmp_path / "dl"
    mgr = Manager(tmp_path / "queue.json", download_dir=dl_dir)
    id_ = mgr.add(server.url)
    _wrap_cb_with_trigger(monkeypatch, mgr, lambda m, i: m.pause(i))

    mgr.start()
    try:
        _poll(mgr, id_, {"paused"})
        assert (dl_dir / "file.bin.part").exists()

        server.served_bytes = 0
        mgr.resume(id_)
        snap = _poll(mgr, id_, {"done", "error"}, timeout=15)
        assert snap["state"] == "done"
        out = dl_dir / snap["filename"]
        assert out.read_bytes() == blob
        assert server.served_bytes < len(blob)
    finally:
        mgr.shutdown()


def test_cancel_deletes_files(tmp_path, make_server, monkeypatch):
    blob = os.urandom(4 * 1024 * 1024)
    server = make_server(blob)
    dl_dir = tmp_path / "dl"
    mgr = Manager(tmp_path / "queue.json", download_dir=dl_dir)
    id_ = mgr.add(server.url)
    _wrap_cb_with_trigger(monkeypatch, mgr, lambda m, i: m.cancel(i))

    mgr.start()
    try:
        snap = _poll(mgr, id_, {"cancelled"})
        assert snap["state"] == "cancelled"
        assert not (dl_dir / "file.bin.part").exists()
        assert not (dl_dir / "file.bin.tdm.json").exists()
    finally:
        mgr.shutdown()


def test_pause_queued_never_starts(tmp_path, make_server):
    blob = os.urandom(4 * 1024 * 1024)
    server = make_server(blob)
    mgr = Manager(tmp_path / "queue.json", max_concurrent=3)
    ids = [mgr.add(server.url, dest_dir=str(tmp_path / f"dl{i}")) for i in range(4)]
    target = ids[-1]
    assert mgr.pause(target)
    assert mgr.snapshot_one(target)["state"] == "paused"

    mgr.start()
    try:
        for i in ids[:-1]:
            snap = _poll(mgr, i, {"done", "error"}, timeout=15)
            assert snap["state"] == "done"
        assert mgr.snapshot_one(target)["state"] == "paused"
    finally:
        mgr.shutdown()


def test_host_demotion_retries_with_two_segments(tmp_path, make_server):
    blob = os.urandom(512 * 1024)
    server = make_server(blob, segment_status=403)
    dl_dir = tmp_path / "dl"
    mgr = Manager(tmp_path / "queue.json", download_dir=dl_dir)
    id_ = mgr.add(server.url, segments=8)
    mgr.start()
    try:
        snap = _poll(mgr, id_, {"done", "error"}, timeout=15)
        assert snap["state"] == "done"
        out = dl_dir / snap["filename"]
        assert out.read_bytes() == blob
    finally:
        mgr.shutdown()


# --- resume across presigned-URL rotation (find_resumable / rebind) -------


def test_manager_learns_filename_before_pausing(tmp_path, make_server, monkeypatch):
    """A paused/errored download must already have its filename recorded -
    rebind needs it to find the .part/sidecar on disk, and it can't wait for
    "done" since a paused download never reaches that state."""
    blob = os.urandom(4 * 1024 * 1024)
    server = make_server(blob)
    dl_dir = tmp_path / "dl"
    mgr = Manager(tmp_path / "queue.json", download_dir=dl_dir)
    id_ = mgr.add(server.url)
    _wrap_cb_with_trigger(monkeypatch, mgr, lambda m, i: m.pause(i))

    mgr.start()
    try:
        _poll(mgr, id_, {"paused"})
        assert mgr.snapshot_one(id_)["filename"] == "file.bin"
    finally:
        mgr.shutdown()


def test_find_resumable_matches_host_and_path_ignores_query(tmp_path, make_server):
    server = make_server(os.urandom(1024))
    mgr = Manager(tmp_path / "queue.json", download_dir=tmp_path / "dl")
    id_ = mgr.add(server.url)
    assert mgr.pause(id_)  # queued -> paused directly, no scheduler needed
    matches = mgr.find_resumable(server.url + "?sig=fresh")
    assert [m.id for m in matches] == [id_]


def test_find_resumable_ignores_a_different_path(tmp_path, make_server):
    server = make_server(os.urandom(1024))
    mgr = Manager(tmp_path / "queue.json", download_dir=tmp_path / "dl")
    id_ = mgr.add(server.url)
    mgr.pause(id_)
    other_path = server.url.replace("/file.bin", "/other.bin")
    assert mgr.find_resumable(other_path) == []


def test_find_resumable_scoped_to_paused_and_error_only(tmp_path, make_server):
    server = make_server(os.urandom(1024))
    mgr = Manager(tmp_path / "queue.json", download_dir=tmp_path / "dl")
    mgr.add(server.url)  # left "queued" - not a resume candidate
    assert mgr.find_resumable(server.url + "?sig=fresh") == []


def test_rebind_rejects_non_resumable_state(tmp_path, make_server):
    server = make_server(os.urandom(1024))
    mgr = Manager(tmp_path / "queue.json", download_dir=tmp_path / "dl")
    id_ = mgr.add(server.url)  # queued, not paused/error
    with pytest.raises(ValueError):
        mgr.rebind(id_, server.url + "?sig=x", server.url + "?sig=x", '"e"')


def test_rebind_unknown_id_raises(tmp_path):
    mgr = Manager(tmp_path / "queue.json", download_dir=tmp_path / "dl")
    with pytest.raises(ValueError):
        mgr.rebind("nope", "http://h/f", "http://h/f", '"e"')


def test_rebind_resumes_using_existing_bytes_after_url_rotation(
    tmp_path, make_server, monkeypatch
):
    """The end-to-end scenario Approach #3 exists for: a presigned URL
    expires mid-download, the user re-clicks to get a fresh one (same
    host+path, different signature query), and TDM resumes from the exact
    byte offset instead of restarting."""
    from tdm.engine import _probe

    blob = os.urandom(4 * 1024 * 1024)
    server = make_server(blob)
    dl_dir = tmp_path / "dl"
    mgr = Manager(tmp_path / "queue.json", download_dir=dl_dir)
    id_ = mgr.add(server.url)
    _wrap_cb_with_trigger(monkeypatch, mgr, lambda m, i: m.pause(i))

    mgr.start()
    try:
        _poll(mgr, id_, {"paused"})
        assert (dl_dir / "file.bin.part").exists()

        fresh_url = server.url + "?sig=fresh"
        candidates = mgr.find_resumable(fresh_url)
        assert [c.id for c in candidates] == [id_]

        probe = _probe(fresh_url)
        mgr.rebind(id_, fresh_url, probe.resolved_url, probe.validator)
        assert mgr.snapshot_one(id_)["state"] == "queued"

        server.served_bytes = 0
        snap = _poll(mgr, id_, {"done", "error"}, timeout=15)
        assert snap["state"] == "done"
        out = dl_dir / snap["filename"]
        assert out.read_bytes() == blob
        assert server.served_bytes < len(blob)  # resumed, not re-downloaded from scratch
    finally:
        mgr.shutdown()


# --- restart recovery (Milestone 2 Task 7) ---------------------------------


def _write_queue(state_path, entries):
    state_path.write_text(json.dumps({"downloads": entries}))


def test_restart_converts_running_and_paused_to_queued(tmp_path):
    state_path = tmp_path / "queue.json"
    _write_queue(state_path, [
        {"id": "r1", "url": "http://h/f", "dest_dir": ".", "segments": 8,
         "state": "running", "filename": None, "error": None},
        {"id": "p1", "url": "http://h/f", "dest_dir": ".", "segments": 8,
         "state": "paused", "filename": None, "error": None},
    ])
    mgr = Manager(state_path)
    assert mgr.snapshot_one("r1")["state"] == "queued"
    assert mgr.snapshot_one("p1")["state"] == "queued"


def test_restart_keeps_done_entries(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("engine.download should not run for a done entry")

    monkeypatch.setattr("tdm.engine.download", boom)
    state_path = tmp_path / "queue.json"
    _write_queue(state_path, [
        {"id": "d1", "url": "http://h/f", "dest_dir": ".", "segments": 8,
         "state": "done", "filename": "f.bin", "error": None},
    ])
    mgr = Manager(state_path)
    mgr.start()
    try:
        time.sleep(0.2)  # give the scheduler a moment; it must not touch a done entry
        assert mgr.snapshot_one("d1")["state"] == "done"
    finally:
        mgr.shutdown()


def test_restart_on_corrupt_state(tmp_path):
    state_path = tmp_path / "queue.json"
    state_path.write_text("{ not json")
    mgr = Manager(state_path)  # must not raise
    assert mgr.snapshot() == []


def test_restart_resumes_running_download(tmp_path, make_server, monkeypatch):
    blob = os.urandom(4 * 1024 * 1024)
    server = make_server(blob)
    dl_dir = tmp_path / "dl"
    state_path = tmp_path / "queue.json"

    mgr_a = Manager(state_path, download_dir=dl_dir)
    id_ = mgr_a.add(server.url)
    _wrap_cb_with_trigger(monkeypatch, mgr_a, lambda m, i: m.pause(i))
    mgr_a.start()
    _poll(mgr_a, id_, {"paused"})
    assert (dl_dir / "file.bin.part").exists()
    mgr_a.shutdown()

    server.served_bytes = 0
    mgr_b = Manager(state_path, download_dir=dl_dir)
    assert mgr_b.snapshot_one(id_)["state"] == "queued"  # paused -> queued on load
    mgr_b.start()
    try:
        snap = _poll(mgr_b, id_, {"done", "error"}, timeout=15)
        assert snap["state"] == "done"
        out = dl_dir / snap["filename"]
        assert out.read_bytes() == blob
        assert server.served_bytes < len(blob)
    finally:
        mgr_b.shutdown()


# --- HTTP layer (Milestone 2 Task 8) ---------------------------------------

TOKEN = "test-token-" + "x" * 20


@pytest.fixture
def http(tmp_path):
    mgr = Manager(tmp_path / "queue.json", download_dir=tmp_path / "dl")
    server, port = serve_in_thread(mgr, TOKEN)
    mgr.start()
    base = f"http://127.0.0.1:{port}"
    try:
        yield mgr, base
    finally:
        mgr.shutdown()
        server.shutdown()
        server.server_close()


def _auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


ALL_ROUTES = [
    ("GET", "/downloads"),
    ("GET", "/downloads/nope"),
    ("GET", "/health"),
    ("POST", "/downloads"),
    ("POST", "/downloads/nope/pause"),
    ("POST", "/downloads/nope/resume"),
    ("POST", "/downloads/nope/cancel"),
    ("POST", "/shutdown"),
]


@pytest.mark.parametrize("method,path", ALL_ROUTES)
def test_http_requires_auth(http, method, path):
    _mgr, base = http
    r = requests.request(method, base + path, timeout=5)
    assert r.status_code == 401
    r = requests.request(method, base + path, headers=_auth("wrong-token"), timeout=5)
    assert r.status_code == 401


def test_post_downloads_creates_and_lists(http, make_server):
    mgr, base = http
    server = make_server(os.urandom(1024))
    r = requests.post(base + "/downloads", json={"url": server.url}, headers=_auth(), timeout=5)
    assert r.status_code == 201
    id_ = r.json()["id"]

    r = requests.get(base + "/downloads", headers=_auth(), timeout=5)
    assert r.status_code == 200
    entries = r.json()
    assert any(e["id"] == id_ for e in entries)
    entry = next(e for e in entries if e["id"] == id_)
    assert set(entry) == {
        "id", "url", "filename", "dest_dir", "state", "done", "total", "bps", "error",
    }

    r = requests.get(f"{base}/downloads/{id_}", headers=_auth(), timeout=5)
    assert r.status_code == 200
    assert r.json()["id"] == id_

    r = requests.get(f"{base}/downloads/nope", headers=_auth(), timeout=5)
    assert r.status_code == 404

    # let the background download finish before make_server tears down -
    # otherwise a still-running fetch hangs against a now-dead server.
    _poll(mgr, id_, {"done", "error"}, timeout=5)


def test_post_downloads_rejects_private_url(http, monkeypatch):
    monkeypatch.setattr(netcheck, "assert_allowed_url", _real_assert_allowed_url)
    _mgr, base = http
    r = requests.post(
        base + "/downloads", json={"url": "http://127.0.0.1:9/x"}, headers=_auth(), timeout=5
    )
    assert r.status_code == 400
    assert r.json()["error"] == "blocked"


def test_post_downloads_clamps_segments(http, make_server):
    mgr, base = http
    server = make_server(os.urandom(1024))
    r = requests.post(
        base + "/downloads",
        json={"url": server.url, "segments": 999},
        headers=_auth(),
        timeout=5,
    )
    assert r.status_code == 201
    _poll(mgr, r.json()["id"], {"done", "error"}, timeout=5)


def test_health_endpoint(http):
    _mgr, base = http
    r = requests.get(base + "/health", headers=_auth(), timeout=5)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["downloads"] == 0
    assert requests.get(base + "/health", timeout=5).status_code == 401


def test_pause_resume_cancel_over_http(http, make_server):
    mgr, base = http
    blob = os.urandom(4 * 1024 * 1024)
    server = make_server(blob)
    r = requests.post(base + "/downloads", json={"url": server.url}, headers=_auth(), timeout=5)
    id_ = r.json()["id"]

    _poll(mgr, id_, {"running", "paused", "done", "error"}, timeout=5)
    r = requests.post(f"{base}/downloads/{id_}/pause", headers=_auth(), timeout=5)
    assert r.status_code == 202
    snap = _poll(mgr, id_, {"paused", "done"}, timeout=10)
    if snap["state"] == "paused":
        r = requests.post(f"{base}/downloads/{id_}/resume", headers=_auth(), timeout=5)
        assert r.status_code == 202
        snap = _poll(mgr, id_, {"done", "error"}, timeout=15)
        assert snap["state"] == "done"

    r = requests.post(base + "/downloads", json={"url": server.url}, headers=_auth(), timeout=5)
    id2 = r.json()["id"]
    r = requests.post(f"{base}/downloads/{id2}/cancel", headers=_auth(), timeout=5)
    assert r.status_code == 202
    snap = _poll(mgr, id2, {"cancelled", "done"}, timeout=15)

    r = requests.post(f"{base}/downloads/nope/pause", headers=_auth(), timeout=5)
    assert r.status_code == 404


def test_full_flow_add_poll_done_over_http(http, make_server):
    mgr, base = http
    blob = os.urandom(512 * 1024)
    server = make_server(blob)
    r = requests.post(base + "/downloads", json={"url": server.url}, headers=_auth(), timeout=5)
    id_ = r.json()["id"]

    deadline = time.monotonic() + 15
    snap = None
    while time.monotonic() < deadline:
        r = requests.get(f"{base}/downloads/{id_}", headers=_auth(), timeout=5)
        snap = r.json()
        if snap["state"] in ("done", "error"):
            break
        time.sleep(0.02)
    assert snap["state"] == "done"
    out = mgr.download_dir / snap["filename"]
    assert out.read_bytes() == blob


# --- /ext/* routes (Milestone 3 Task 1) ------------------------------------

EXT_ORIGIN = "chrome-extension://abcdefghijklmnopqrstuvwxyzabcdef"
EXT_TOKEN = "ext-token-" + "y" * 20


@pytest.fixture
def ext_http(tmp_path):
    mgr = Manager(tmp_path / "queue.json", download_dir=tmp_path / "dl")
    pending = queue.Queue(maxsize=100)
    server, port = serve_in_thread(
        mgr, TOKEN, allowed_origin=EXT_ORIGIN, ext_token=EXT_TOKEN, pending=pending
    )
    mgr.start()
    base = f"http://127.0.0.1:{port}"
    try:
        yield mgr, base, pending
    finally:
        mgr.shutdown()
        server.shutdown()
        server.server_close()


def test_ext_routes_401_without_allowed_origin(http):
    """`http` fixture has `allowed_origin=None` - the default, unmodified
    behavior for the normal detached service. `/ext/*` must 401 there
    regardless of Origin, since the service was never put in catch mode."""
    _mgr, base = http
    r = requests.post(base + "/ext/health", headers={"Origin": EXT_ORIGIN}, timeout=5)
    assert r.status_code == 401
    r = requests.post(
        base + "/ext/flag",
        json={"url": "http://h/f"},
        headers={"Origin": EXT_ORIGIN, "X-Ext-Token": EXT_TOKEN},
        timeout=5,
    )
    assert r.status_code == 401


def test_ext_flag_queues_with_matching_origin_and_token(ext_http):
    _mgr, base, pending = ext_http
    r = requests.post(
        base + "/ext/flag",
        json={"url": "http://h/f"},
        headers={"Origin": EXT_ORIGIN, "X-Ext-Token": EXT_TOKEN},
        timeout=5,
    )
    assert r.status_code == 202
    assert pending.get_nowait() == "http://h/f"


def test_ext_flag_rejects_mismatched_origin(ext_http):
    _mgr, base, pending = ext_http
    r = requests.post(
        base + "/ext/flag",
        json={"url": "http://h/f"},
        headers={"Origin": "chrome-extension://wrong", "X-Ext-Token": EXT_TOKEN},
        timeout=5,
    )
    assert r.status_code == 401
    assert pending.empty()


def test_ext_flag_rejects_missing_or_mismatched_token(ext_http):
    _mgr, base, pending = ext_http
    r = requests.post(
        base + "/ext/flag",
        json={"url": "http://h/f"},
        headers={"Origin": EXT_ORIGIN},
        timeout=5,
    )
    assert r.status_code == 401
    r = requests.post(
        base + "/ext/flag",
        json={"url": "http://h/f"},
        headers={"Origin": EXT_ORIGIN, "X-Ext-Token": "wrong-token"},
        timeout=5,
    )
    assert r.status_code == 401
    assert pending.empty()


def test_ext_flag_rejects_private_url(ext_http, monkeypatch):
    monkeypatch.setattr(netcheck, "assert_allowed_url", _real_assert_allowed_url)
    _mgr, base, pending = ext_http
    r = requests.post(
        base + "/ext/flag",
        json={"url": "http://127.0.0.1:9/x"},
        headers={"Origin": EXT_ORIGIN, "X-Ext-Token": EXT_TOKEN},
        timeout=5,
    )
    assert r.status_code == 400
    assert r.json()["error"] == "blocked"
    assert pending.empty()


def test_ext_health_returns_configured_token(ext_http):
    _mgr, base, _pending = ext_http
    r = requests.post(base + "/ext/health", headers={"Origin": EXT_ORIGIN}, timeout=5)
    assert r.status_code == 200
    assert r.json()["token"] == EXT_TOKEN


def test_shutdown_stops_server(http):
    _mgr, base = http
    r = requests.post(base + "/shutdown", headers=_auth(), timeout=5)
    assert r.status_code == 202
    deadline = time.monotonic() + 2
    stopped = False
    while time.monotonic() < deadline:
        try:
            requests.get(base + "/health", headers=_auth(), timeout=0.5)
        except requests.exceptions.RequestException:
            stopped = True
            break
        time.sleep(0.05)
    assert stopped


# --- process management (Milestone 2 Task 9) --------------------------------


def test_run_service_writes_state_and_serves(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "state_dir", lambda: tmp_path)
    t = threading.Thread(target=service.run_service, daemon=True)
    t.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not (tmp_path / "service.port").exists():
            time.sleep(0.02)
        assert (tmp_path / "service.port").exists()

        base_url, token = service.read_endpoint()
        r = requests.get(
            f"{base_url}/health", headers={"Authorization": f"Bearer {token}"}, timeout=5
        )
        assert r.status_code == 200

        r = requests.post(
            f"{base_url}/shutdown", headers={"Authorization": f"Bearer {token}"}, timeout=5
        )
        assert r.status_code == 202
    finally:
        t.join(timeout=5)

    for name in ("service.port", "service.token", "service.pid"):
        assert not (tmp_path / name).exists()


def test_spawn_detached_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "state_dir", lambda: tmp_path)
    (tmp_path / "service.port").write_text("12345")
    (tmp_path / "service.token").write_text("tok")

    class _Resp:
        status_code = 200

    monkeypatch.setattr(service.requests, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(
        service.subprocess, "Popen", lambda *a, **k: pytest.fail("should not spawn")
    )
    assert "already running" in service.spawn_detached()


def test_stale_state_is_cleared(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "state_dir", lambda: tmp_path)
    (tmp_path / "service.port").write_text("1")  # nothing listens on it

    def fake_get(*a, **k):
        raise requests.RequestException("connection refused")

    def fake_popen(*a, **k):
        paths.write_atomic(tmp_path / "service.port", "54321")
        paths.write_atomic(tmp_path / "service.token", "tok")

    monkeypatch.setattr(service.requests, "get", fake_get)
    monkeypatch.setattr(service.subprocess, "Popen", fake_popen)
    assert service.spawn_detached() == "http://127.0.0.1:54321"


# --- catch-mode helpers (Milestone 3 Task 2) --------------------------------


def test_resolve_origin_persists_and_reuses(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "state_dir", lambda: tmp_path)
    origin = service.resolve_origin("abc123")
    assert origin == "chrome-extension://abc123"
    assert service.resolve_origin(None) == "chrome-extension://abc123"


def test_resolve_origin_without_id_or_stored_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "state_dir", lambda: tmp_path)
    with pytest.raises(SystemExit, match="extension id"):
        service.resolve_origin(None)


def test_resolve_ext_token_persists_across_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "state_dir", lambda: tmp_path)
    first = service.resolve_ext_token()
    second = service.resolve_ext_token()
    assert first == second
    assert (tmp_path / "ext_token").read_text() == first


def test_confirm_loop_accepts_y_skips_n(tmp_path, monkeypatch):
    mgr = Manager(tmp_path / "queue.json", download_dir=tmp_path / "dl")
    pending = queue.Queue()
    pending.put("http://h/keep")
    pending.put("http://h/skip")
    pending.put(None)

    answers = iter(["y", "n"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    service._confirm_loop(mgr, pending)

    assert len(mgr.snapshot()) == 1
    assert mgr.snapshot()[0]["url"] == "http://h/keep"


def test_confirm_loop_resumes_matching_paused_download(tmp_path, make_server, monkeypatch):
    """The #3 catch-mode UX: a flagged link matching a paused download's
    host+path gets a resume prompt instead of a plain new-download prompt,
    and accepting it rebinds and requeues rather than starting over."""
    blob = os.urandom(4 * 1024 * 1024)
    server = make_server(blob)
    dl_dir = tmp_path / "dl"
    mgr = Manager(tmp_path / "queue.json", download_dir=dl_dir)
    id_ = mgr.add(server.url)
    _wrap_cb_with_trigger(monkeypatch, mgr, lambda m, i: m.pause(i))

    mgr.start()
    try:
        _poll(mgr, id_, {"paused"})

        pending = queue.Queue()
        pending.put(server.url + "?sig=fresh")
        pending.put(None)
        monkeypatch.setattr("builtins.input", lambda *a: "y")  # accept the resume prompt
        server.served_bytes = 0
        service._confirm_loop(mgr, pending)

        assert len(mgr.snapshot()) == 1  # rebound in place, not a second download
        snap = _poll(mgr, id_, {"done", "error"}, timeout=15)
        assert snap["state"] == "done"
        assert (dl_dir / snap["filename"]).read_bytes() == blob
        assert server.served_bytes < len(blob)  # resumed, not re-downloaded from scratch
    finally:
        mgr.shutdown()


def test_confirm_loop_skips_link_when_resume_declined(tmp_path, make_server, monkeypatch):
    blob = os.urandom(4 * 1024 * 1024)
    server = make_server(blob)
    dl_dir = tmp_path / "dl"
    mgr = Manager(tmp_path / "queue.json", download_dir=dl_dir)
    id_ = mgr.add(server.url)
    _wrap_cb_with_trigger(monkeypatch, mgr, lambda m, i: m.pause(i))

    mgr.start()
    try:
        _poll(mgr, id_, {"paused"})

        pending = queue.Queue()
        pending.put(server.url + "?sig=fresh")
        pending.put(None)
        monkeypatch.setattr("builtins.input", lambda *a: "n")  # decline the resume prompt
        service._confirm_loop(mgr, pending)

        assert len(mgr.snapshot()) == 1  # no second download queued
        assert mgr.snapshot_one(id_)["state"] == "paused"  # left untouched
    finally:
        mgr.shutdown()


def test_confirm_loop_falls_back_to_new_download_with_no_resumable_match(
    tmp_path, make_server, monkeypatch
):
    server = make_server(os.urandom(1024))
    mgr = Manager(tmp_path / "queue.json", download_dir=tmp_path / "dl")
    pending = queue.Queue()
    pending.put(server.url)
    pending.put(None)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    service._confirm_loop(mgr, pending)
    assert len(mgr.snapshot()) == 1
    assert mgr.snapshot()[0]["url"] == server.url


def test_confirm_loop_survives_bad_url(tmp_path, monkeypatch):
    monkeypatch.setattr(netcheck, "assert_allowed_url", _real_assert_allowed_url)
    mgr = Manager(tmp_path / "queue.json", download_dir=tmp_path / "dl")
    pending = queue.Queue()
    pending.put("http://127.0.0.1:9/blocked")
    pending.put(None)

    monkeypatch.setattr("builtins.input", lambda *a: "y")
    service._confirm_loop(mgr, pending)  # must not raise

    assert mgr.snapshot() == []


# --- run_catch() (Milestone 3 Task 3) ---------------------------------------


def test_run_catch_ext_flag_flow_and_shutdown(tmp_path, make_server, monkeypatch):
    monkeypatch.setattr(paths, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(paths, "default_download_dir", lambda: tmp_path / "dl")
    origin = service.resolve_origin("test-ext-id")
    monkeypatch.setattr("builtins.input", lambda *a: "y")

    t = threading.Thread(target=service.run_catch, args=(origin,), daemon=True)
    t.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not (tmp_path / "service.port").exists():
            time.sleep(0.02)
        assert (tmp_path / "service.port").exists()

        base = f"http://127.0.0.1:{service.CATCH_PORT}"
        r = requests.post(base + "/ext/health", headers={"Origin": origin}, timeout=5)
        assert r.status_code == 200
        ext_token = r.json()["token"]

        server = make_server(os.urandom(1024))
        r = requests.post(
            base + "/ext/flag",
            json={"url": server.url},
            headers={"Origin": origin, "X-Ext-Token": ext_token},
            timeout=5,
        )
        assert r.status_code == 202

        deadline = time.monotonic() + 15
        done = False
        while time.monotonic() < deadline:
            text = paths.read_or_none(tmp_path / "queue.json")
            if text:
                data = json.loads(text)
                if any(e["state"] == "done" for e in data["downloads"]):
                    done = True
                    break
            time.sleep(0.05)
        assert done

        service_token = paths.read_or_none(tmp_path / "service.token")
        r = requests.post(
            base + "/shutdown",
            headers={"Authorization": f"Bearer {service_token}"},
            timeout=5,
        )
        assert r.status_code == 202
    finally:
        # best-effort - a fixed-port server left running by an earlier
        # assertion failure must never survive the test: it would squat
        # CATCH_PORT and take out every later run of this test.
        service_token = paths.read_or_none(tmp_path / "service.token")
        if service_token:
            try:
                requests.post(
                    f"http://127.0.0.1:{service.CATCH_PORT}/shutdown",
                    headers={"Authorization": f"Bearer {service_token}"},
                    timeout=5,
                )
            except requests.RequestException:
                pass
        t.join(timeout=10)

    for name in ("service.port", "service.token", "service.pid"):
        assert not (tmp_path / name).exists()
    assert (tmp_path / "extension_id").exists()
    assert (tmp_path / "ext_token").exists()


# --- live progress display (Milestone 3 Task 4) -----------------------------


def test_format_frame_renders_active_segments_and_aggregate():
    rows = [
        {
            "phase": "active",
            "name": "file.bin",
            "done": 40,
            "total": 100,
            "segments": [(50, 50), (10, 50)],
        }
    ]
    lines = service._format_frame(rows)
    assert len(lines) == 3
    assert "100%" in lines[0]
    assert " 20%" in lines[1]
    assert lines[2].startswith("file.bin ")
    assert " 40%" in lines[2]


def test_format_frame_appends_extension_hint_for_a_nameless_done_download():
    lines = service._format_frame(
        [{"phase": "done", "name": "download.bin", "dest_dir": "/out"}]
    )
    assert "no file extension" in lines[0]
    assert lines[1] == f"Saved to {Path('/out') / 'download.bin'}"
    assert lines[2] == "Ready for a new link."


def test_format_frame_terminal_phases():
    assert service._format_frame(
        [{"phase": "done", "name": "a.bin", "dest_dir": "/out"}]
    ) == [f"Saved to {Path('/out') / 'a.bin'}", "Ready for a new link."]
    assert service._format_frame(
        [{"phase": "error", "name": "a.bin", "error": "boom"}]
    ) == ["error: boom -> a.bin", "Ready for a new link."]
    assert service._format_frame([{"phase": "cancelled", "name": "a.bin"}]) == [
        "cancelled -> a.bin", "Ready for a new link."
    ]
    assert service._format_frame(
        [{"phase": "combining", "name": "a.bin", "segments": [(1, 1), (1, 1)]}]
    ) == ["combining 2 segments -> a.bin"]


def test_render_active_never_erases_a_just_announced_completion(capsys):
    """Approach: completion lines (hint/Saved to/Ready-for-a-new-link) must
    scroll permanently, not live inside the erasable progress region - or the
    next redraw tick wipes them the moment the id drops out of `watched`."""
    mgr = _FakeManager(
        {"a": {"state": "done", "filename": "a.bin", "dest_dir": "/out", "error": None}}
    )
    watched = {"a": 1}  # already past the one-tick "combining" phase
    last_count = service._render_active(mgr, watched, last_count=0)

    assert watched == {}  # announced and dropped
    assert last_count == 0  # nothing live left - the erasable region is empty
    out = capsys.readouterr().out
    assert f"Saved to {Path('/out') / 'a.bin'}" in out
    assert "Ready for a new link." in out


class _FakeManager:
    def __init__(self, snapshots, segments=None):
        self._snapshots = snapshots
        self._segments = segments or {}

    def snapshot_one(self, id_):
        return self._snapshots.get(id_)

    def segments_of(self, id_):
        return self._segments.get(id_)


def test_frame_rows_holds_combining_one_extra_tick_then_drops():
    mgr = _FakeManager(
        {"a": {"state": "done", "filename": "a.bin"}}, segments={"a": [(10, 10)]}
    )
    watched = {"a": 0}

    rows1 = service._frame_rows(mgr, watched)
    assert rows1 == [{"phase": "combining", "name": "a.bin", "segments": [(10, 10)]}]
    assert watched == {"a": 1}

    rows2 = service._frame_rows(mgr, watched)
    assert rows2 == [{"phase": "done", "name": "a.bin", "error": None, "dest_dir": None}]
    assert watched == {}


def test_frame_rows_drops_id_once_manager_forgets_it():
    mgr = _FakeManager({})
    watched = {"gone": 0}
    assert service._frame_rows(mgr, watched) == []
    assert watched == {}
