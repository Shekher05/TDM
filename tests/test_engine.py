import json
import os
import threading
from itertools import pairwise
from pathlib import Path

import pytest
import requests

from tdm import engine, netcheck
from tdm.engine import (
    CHUNK,
    Cancelled,
    InsufficientSpace,
    IntegrityError,
    RangeNotSupported,
    _download_segmented,
    _download_single,
    _load_progress,
    _meta_path,
    _part_path,
    _probe,
    _save_progress,
    derive_filename,
    download,
    split_ranges,
)
from tdm.netcheck import BlockedURLError
from tdm.netcheck import assert_allowed_url as _real_assert_allowed_url


class _FakeResp:
    def __init__(self, headers):
        self.headers = headers


def test_derive_filename_from_url():
    assert derive_filename("http://h/a/b/file.zip", _FakeResp({})) == "file.zip"


def test_derive_filename_from_content_disposition():
    resp = _FakeResp({"Content-Disposition": 'attachment; filename="real.tar.gz"'})
    assert derive_filename("http://h/", resp) == "real.tar.gz"


def test_derive_filename_fallback():
    assert derive_filename("http://h/", _FakeResp({})) == "download.bin"


def test_derive_filename_strips_traversal():
    assert derive_filename("http://h/..%2f..%2fetc%2fpasswd", _FakeResp({})) == "passwd"


def test_derive_filename_strips_ads_colon():
    assert derive_filename("http://h/report.pdf:evil", _FakeResp({})) == "report.pdf"
    resp = _FakeResp({"Content-Disposition": 'filename="data.zip:$DATA"'})
    assert derive_filename("http://h/", resp) == "data.zip"


def test_derive_filename_strips_control_chars():
    resp = _FakeResp({"Content-Disposition": 'filename="a\x00b\x1f.zip"'})
    assert derive_filename("http://h/", resp) == "ab.zip"


def test_derive_filename_strips_trailing_dot_and_space():
    resp = _FakeResp({"Content-Disposition": 'filename="evil.  "'})
    assert derive_filename("http://h/", resp) == "evil"


def test_derive_filename_rejects_dotdot():
    assert derive_filename("http://h/%2e%2e", _FakeResp({})) == "download.bin"


def test_derive_filename_rejects_windows_reserved():
    assert derive_filename("http://h/CON", _FakeResp({})) == "download.bin"
    assert derive_filename("http://h/nul.txt", _FakeResp({})) == "download.bin"
    resp = _FakeResp({"Content-Disposition": 'filename="COM1"'})
    assert derive_filename("http://h/", resp) == "download.bin"


def test_extension_hint_none_for_a_known_extension():
    assert engine.extension_hint("movie.mp4") is None
    assert engine.extension_hint("photo.png") is None


def test_extension_hint_fires_on_derive_filenames_fallback():
    hint = engine.extension_hint("download.bin")
    assert hint is not None
    assert ".mp4" in hint and ".jpeg" in hint


def test_extension_hint_fires_on_no_extension_at_all():
    assert engine.extension_hint("report") is not None


def test_split_ranges_even():
    assert split_ranges(100, 4) == [(0, 24), (25, 49), (50, 74), (75, 99)]


def test_split_ranges_remainder_on_last():
    r = split_ranges(103, 4)
    assert r[0] == (0, 24)
    assert r[-1][1] == 102
    for a, b in pairwise(r):
        assert b[0] == a[1] + 1


def test_split_ranges_more_segments_than_bytes():
    assert split_ranges(3, 8) == [(0, 0), (1, 1), (2, 2)]


def test_split_ranges_rejects_zero():
    with pytest.raises(ValueError):
        split_ranges(0, 4)


def test_sidecar_paths():
    final = Path("/tmp/movie.mkv")
    assert _part_path(final).name == "movie.mkv.part"
    assert _meta_path(final).name == "movie.mkv.tdm.json"


def test_progress_round_trip(tmp_path):
    meta = tmp_path / "f.tdm.json"
    _save_progress(meta, "http://h/f", 100, '"e1"', [10, 20, 0])
    assert _load_progress(meta, "http://h/f", 100, '"e1"') == [10, 20, 0]


def test_progress_rejected_on_mismatch(tmp_path):
    meta = tmp_path / "f.tdm.json"
    _save_progress(meta, "http://h/f", 100, '"e1"', [10, 20, 0])
    assert _load_progress(meta, "http://h/f", 999, '"e1"') is None
    assert _load_progress(meta, "http://h/OTHER", 100, '"e1"') is None
    assert _load_progress(meta, "http://h/f", 100, '"e2"') is None


def test_progress_rejected_without_strong_validator(tmp_path):
    meta = tmp_path / "f.tdm.json"
    _save_progress(meta, "http://h/f", 100, "", [10, 20, 0])
    assert _load_progress(meta, "http://h/f", 100, "") is None


def test_progress_rejected_on_out_of_range_entry(tmp_path):
    meta = tmp_path / "f.tdm.json"
    # split_ranges(100, 3)[0] is (0, 32) -> length 33; 999 and -1 are impossible.
    meta.write_text(json.dumps({"url": "http://h/f", "size": 100, "etag": "e", "progress": [999, 0, 0]}))
    assert _load_progress(meta, "http://h/f", 100, "e") is None
    meta.write_text(json.dumps({"url": "http://h/f", "size": 100, "etag": "e", "progress": [-1, 0, 0]}))
    assert _load_progress(meta, "http://h/f", 100, "e") is None
    meta.write_text(json.dumps({"url": "http://h/f", "size": 100, "etag": "e", "progress": "notalist"}))
    assert _load_progress(meta, "http://h/f", 100, "e") is None


def test_progress_missing_or_corrupt_file(tmp_path):
    assert _load_progress(tmp_path / "nope.json", "u", 1, "e") is None
    bad = tmp_path / "bad.tdm.json"
    bad.write_text("{not json")
    assert _load_progress(bad, "u", 1, "e") is None


def test_save_progress_is_atomic(tmp_path):
    meta = tmp_path / "f.tdm.json"
    _save_progress(meta, "http://h/f", 100, "e", [1, 2, 3])
    assert not (tmp_path / "f.tdm.json.tmp").exists()


def test_stale_tmp_does_not_affect_load(tmp_path):
    meta = tmp_path / "f.tdm.json"
    _save_progress(meta, "http://h/f", 100, "e", [10, 20, 0])
    (tmp_path / "f.tdm.json.tmp").write_text("garbage from a crash")
    assert _load_progress(meta, "http://h/f", 100, "e") == [10, 20, 0]


def test_sidecar_never_persists_credentials(tmp_path):
    meta = tmp_path / "f.tdm.json"
    _save_progress(meta, "http://user:s3cret@h/f", 100, "e", [0])
    raw = meta.read_text()
    assert "s3cret" not in raw
    assert "user" not in raw
    # a load with the credentialed URL still matches the stripped stored URL
    assert _load_progress(meta, "http://user:s3cret@h/f", 100, "e") == [0]


def test_rebind_progress_updates_identity_keeps_progress(tmp_path):
    """A presigned URL's signature rotates (host+path unchanged, query
    differs) - rebind must point the sidecar at the fresh url/etag while
    leaving the saved per-segment progress untouched, so the next
    `engine.download()` call trusts it and resumes instead of restarting."""
    final = tmp_path / "f.bin"
    meta = _meta_path(final)
    _save_progress(meta, "http://h/f?sig=old", 100, '"old-etag"', [10, 20, 0])
    engine.rebind_progress(final, "http://h/f?sig=new", '"new-etag"')
    assert _load_progress(meta, "http://h/f?sig=new", 100, '"new-etag"') == [10, 20, 0]
    assert _load_progress(meta, "http://h/f?sig=old", 100, '"old-etag"') is None


def test_rebind_progress_strips_credentials_from_new_url(tmp_path):
    final = tmp_path / "f.bin"
    meta = _meta_path(final)
    _save_progress(meta, "http://h/f", 100, '"e1"', [5])
    engine.rebind_progress(final, "http://user:s3cret@h/f?sig=new", '"e2"')
    assert "s3cret" not in meta.read_text()
    assert _load_progress(meta, "http://user:s3cret@h/f?sig=new", 100, '"e2"') == [5]


def test_rebind_progress_missing_sidecar_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        engine.rebind_progress(tmp_path / "nope.bin", "http://h/f", '"e"')


# --- make_server fixture behaviour (Task 3) --------------------------------


def test_fixture_serves_full_and_ranged(make_server):
    blob = bytes(range(256)) * 40  # 10240 bytes
    server = make_server(blob)

    full = requests.get(server.url, timeout=5)
    assert full.status_code == 200
    assert full.content == blob
    assert full.headers["ETag"] == '"test-etag"'

    ranged = requests.get(server.url, headers={"Range": "bytes=100-199"}, timeout=5)
    assert ranged.status_code == 206
    assert ranged.headers["Content-Range"] == f"bytes 100-199/{len(blob)}"
    assert ranged.content == blob[100:200]
    assert server.served_bytes == len(blob) + 100


def test_fixture_no_etag_toggle(make_server):
    server = make_server(b"x" * 64, no_etag=True)
    assert "ETag" not in requests.get(server.url, timeout=5).headers


def test_fixture_drop_after_toggle(make_server):
    blob = b"y" * 4000
    server = make_server(blob, drop_after=1000)

    with pytest.raises(requests.exceptions.RequestException):
        _ = requests.get(server.url, timeout=5).content  # first response is truncated

    assert requests.get(server.url, timeout=5).content == blob  # then normal


# --- _probe (Task 4) ------------------------------------------------------


def test_probe_range_server(make_server):
    blob = os.urandom(4096)
    server = make_server(blob)
    r = _probe(server.url)
    assert r.size == 4096
    assert r.accept_ranges is True
    assert r.validator == '"test-etag"'
    assert r.filename == "file.bin"
    assert r.resolved_url == server.url


def test_probe_no_range_server(make_server):
    server = make_server(b"z" * 200, support_range=False)
    r = _probe(server.url)
    assert r.accept_ranges is False
    assert r.size == 200


def test_probe_no_strong_validator(make_server):
    server = make_server(b"z" * 200, no_etag=True)
    assert _probe(server.url).validator == ""


# --- forwarded headers (Approach #2) ---------------------------------------


def test_probe_forwards_extra_headers(make_server):
    server = make_server(b"z" * 200, require_header=("Cookie", "session=abc"))
    with pytest.raises(requests.exceptions.HTTPError):
        _probe(server.url)
    r = _probe(server.url, headers={"Cookie": "session=abc"})
    assert r.size == 200


def test_download_segmented_forwards_headers(make_server, tmp_path):
    blob = os.urandom(64 * 1024)
    server = make_server(blob, require_header=("Cookie", "session=abc"))
    p = _probe(server.url, headers={"Cookie": "session=abc"})
    out = _download_segmented(
        p.resolved_url, tmp_path / p.filename, p.size, p.validator, 4, None,
        headers={"Cookie": "session=abc"},
    )
    assert out.read_bytes() == blob


def test_download_single_forwards_headers(make_server, tmp_path):
    blob = os.urandom(1024)
    server = make_server(blob, support_range=False, require_header=("Cookie", "session=abc"))
    p = _probe(server.url, headers={"Cookie": "session=abc"})
    out = _download_single(
        p.resolved_url, tmp_path / p.filename, p.size, None,
        headers={"Cookie": "session=abc"},
    )
    assert out.read_bytes() == blob


def test_download_end_to_end_forwards_headers(make_server, tmp_path):
    blob = os.urandom(64 * 1024)
    server = make_server(blob, require_header=("Cookie", "session=abc"))
    out = download(server.url, tmp_path, segments=2, headers={"Cookie": "session=abc"})
    assert out.read_bytes() == blob


def test_forwarded_headers_do_not_leak_into_connection_errors():
    """The hardening requirement from NewChanges.md #2: a forwarded secret
    must never end up embedded in an exception's string representation -
    that string is what `service.py` persists into `dl.error`."""
    secret = "s3cr3t-cookie-value"
    with pytest.raises(requests.exceptions.RequestException) as exc_info:
        _probe("http://127.0.0.1:1/unreachable", headers={"Cookie": f"session={secret}"})
    assert secret not in str(exc_info.value)


class _Hop:
    """Throwaway stand-in for one `requests` response in a redirect chain."""

    def __init__(self, url, status=200, headers=None):
        self.url, self.status_code, self.headers = url, status, headers or {}

    def raise_for_status(self):
        pass

    def close(self):
        pass


def _stub_hops(monkeypatch, hops):
    """Make `requests.Session.get` answer from `{url: _Hop}`; returns the call log of
    `(url, headers)`. An unscripted URL raises KeyError - a request that should
    never have been made fails loudly."""
    calls = []

    def fake_get(_self, url, headers=None, **_kw):
        calls.append((url, headers))
        return hops[url]

    monkeypatch.setattr(requests.Session, "get", fake_get)
    return calls


_PUBLIC = "http://93.184.216.34"  # public IP literal: the real guard needs no DNS
_OK = {"Content-Range": "bytes 0-0/10", "ETag": '"e"'}


def test_probe_rejects_non_http_redirect(monkeypatch):
    calls = _stub_hops(
        monkeypatch,
        {f"{_PUBLIC}/redirect-away": _Hop(f"{_PUBLIC}/redirect-away", 302, {"Location": "ftp://evil/data"})},
    )
    monkeypatch.setattr(netcheck, "assert_allowed_url", _real_assert_allowed_url)
    with pytest.raises(ValueError, match="non-http"):
        _probe(f"{_PUBLIC}/redirect-away")
    assert [u for u, _ in calls] == [f"{_PUBLIC}/redirect-away"]  # ftp hop never requested


def test_probe_rejects_private_redirect(monkeypatch):
    calls = _stub_hops(
        monkeypatch,
        {
            f"{_PUBLIC}/redirect-away": _Hop(
                f"{_PUBLIC}/redirect-away", 302, {"Location": "http://127.0.0.1:9/evil"}
            )
        },
    )
    monkeypatch.setattr(netcheck, "assert_allowed_url", _real_assert_allowed_url)
    with pytest.raises(BlockedURLError):
        _probe(f"{_PUBLIC}/redirect-away")
    assert [u for u, _ in calls] == [f"{_PUBLIC}/redirect-away"]  # loopback hop never requested


def test_probe_follows_same_host_redirect_and_resolves_final_url(monkeypatch):
    calls = _stub_hops(
        monkeypatch,
        {
            f"{_PUBLIC}/a": _Hop(f"{_PUBLIC}/a", 302, {"Location": "/b/file.zip"}),
            f"{_PUBLIC}/b/file.zip": _Hop(f"{_PUBLIC}/b/file.zip", 206, _OK),
        },
    )
    r = _probe(f"{_PUBLIC}/a")
    assert r.resolved_url == f"{_PUBLIC}/b/file.zip"
    assert r.filename == "file.zip"
    assert (r.size, r.accept_ranges, r.validator) == (10, True, '"e"')
    assert len(calls) == 2


def test_probe_keeps_cookie_same_host_drops_it_cross_host(monkeypatch):
    other = "http://93.184.216.35"
    calls = _stub_hops(
        monkeypatch,
        {
            f"{_PUBLIC}/a": _Hop(f"{_PUBLIC}/a", 302, {"Location": "/b"}),  # same host
            f"{_PUBLIC}/b": _Hop(f"{_PUBLIC}/b", 307, {"Location": f"{other}/c"}),  # cross host
            f"{other}/c": _Hop(f"{other}/c", 206, _OK),
        },
    )
    secrets_ = {"Cookie": "session=abc", "authorization": "Bearer t", "X-Keep": "1"}
    r = _probe(f"{_PUBLIC}/a", headers=secrets_)
    assert r.resolved_url == f"{other}/c"
    by_url = dict(calls)
    assert by_url[f"{_PUBLIC}/a"]["Cookie"] == "session=abc"
    assert by_url[f"{_PUBLIC}/b"]["Cookie"] == "session=abc"  # same-host hop keeps it
    last = by_url[f"{other}/c"]
    assert "Cookie" not in last and "authorization" not in last  # dropped on leaving the host
    assert last["X-Keep"] == "1"
    assert last["Range"] == "bytes=0-0" and last["Accept-Encoding"] == "identity"


def test_probe_keeps_cookie_across_drive_to_usercontent(monkeypatch):
    calls = _stub_hops(
        monkeypatch,
        {
            "http://drive.google.com/a": _Hop("http://drive.google.com/a", 302, {"Location": "http://drive.usercontent.google.com/c"}),
            "http://drive.usercontent.google.com/c": _Hop("http://drive.usercontent.google.com/c", 206, _OK),
        },
    )
    secrets_ = {"Cookie": "download_warning=abc"}
    r = _probe("http://drive.google.com/a", headers=secrets_)
    assert r.resolved_url == "http://drive.usercontent.google.com/c"
    by_url = dict(calls)
    assert by_url["http://drive.usercontent.google.com/c"]["Cookie"] == "download_warning=abc"


@pytest.mark.parametrize(
    "start,dest",
    [
        ("bank.co.uk", "attacker.co.uk"),  # shared public suffix, unrelated sites
        ("google.com", "evilgoogle.com"),  # unbounded endswith
        ("foo.example.com", "bar.example.com"),  # no suffix matching: exact host or Drive only
        ("drive.google.com", "evil.google.com"),
    ],
)
def test_probe_drops_cookie_to_look_alike_host(monkeypatch, start, dest):
    calls = _stub_hops(
        monkeypatch,
        {
            f"http://{start}/a": _Hop(f"http://{start}/a", 302, {"Location": f"http://{dest}/c"}),
            f"http://{dest}/c": _Hop(f"http://{dest}/c", 206, _OK),
        },
    )
    r = _probe(f"http://{start}/a", headers={"Cookie": "s=1", "Authorization": "Bearer t"})
    last = dict(calls)[f"http://{dest}/c"]
    assert "Cookie" not in last and "Authorization" not in last
    assert r.headers == {}


def test_probe_redirect_loop_raises(monkeypatch):
    loop = f"{_PUBLIC}/loop"
    calls = _stub_hops(monkeypatch, {loop: _Hop(loop, 302, {"Location": loop})})
    with pytest.raises(RuntimeError, match="redirect loop detected"):
        _probe(loop)
    assert len(calls) == 1


def test_probe_reports_headers_as_they_stand_at_the_final_host(monkeypatch):
    other = "http://93.184.216.35"
    _stub_hops(
        monkeypatch,
        {
            f"{_PUBLIC}/a": _Hop(f"{_PUBLIC}/a", 302, {"Location": f"{other}/c"}),
            f"{other}/c": _Hop(f"{other}/c", 206, _OK),
            f"{_PUBLIC}/same": _Hop(f"{_PUBLIC}/same", 206, _OK),
        },
    )
    sent = {"Cookie": "session=abc", "authorization": "Bearer t", "X-Keep": "1"}
    assert _probe(f"{_PUBLIC}/a", headers=sent).headers == {"X-Keep": "1"}
    assert _probe(f"{_PUBLIC}/same", headers=sent).headers == sent  # no host change, no drop
    assert _probe(f"{_PUBLIC}/same").headers == {}


def _record_transfer_headers(monkeypatch, seen):
    """Replace both transfer paths with a recorder of the `headers` they were
    handed (it is the last positional argument of each)."""

    def _rec(*a, **_kw):
        seen.append(a[-1])
        return Path("recorded")

    monkeypatch.setattr(engine, "_download_segmented", _rec)
    monkeypatch.setattr(engine, "_download_single", _rec)


@pytest.mark.parametrize(
    "final_status,final_headers",
    [(206, _OK), (200, {"Content-Length": "10"})],  # segmented path, single-connection path
    ids=["segmented", "single"],
)
def test_download_withholds_cookie_from_a_redirect_target(
    monkeypatch, tmp_path, final_status, final_headers
):
    other = "http://93.184.216.35"
    _stub_hops(
        monkeypatch,
        {
            f"{_PUBLIC}/a": _Hop(f"{_PUBLIC}/a", 302, {"Location": f"{other}/c"}),
            f"{other}/c": _Hop(f"{other}/c", final_status, final_headers),
        },
    )
    seen = []
    _record_transfer_headers(monkeypatch, seen)
    download(
        f"{_PUBLIC}/a", tmp_path, headers={"Cookie": "session=abc", "X-Keep": "1"}
    )
    assert seen == [{"X-Keep": "1"}]  # the start host's Cookie never reaches the CDN


def test_download_keeps_cookie_when_redirect_stays_on_the_host(monkeypatch, tmp_path):
    _stub_hops(
        monkeypatch,
        {
            f"{_PUBLIC}/a": _Hop(f"{_PUBLIC}/a", 302, {"Location": "/b"}),
            f"{_PUBLIC}/b": _Hop(f"{_PUBLIC}/b", 206, _OK),
        },
    )
    seen = []
    _record_transfer_headers(monkeypatch, seen)
    download(f"{_PUBLIC}/a", tmp_path, headers={"Cookie": "session=abc"})
    assert seen == [{"Cookie": "session=abc"}]


def test_segment_rejects_a_redirect_response_without_writing_it(monkeypatch, tmp_path):
    calls = []

    class _Resp:
        status_code = 302
        headers = {"Location": "http://elsewhere/f"}  # noqa: RUF012

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass  # requests does not raise for 3xx

        def iter_content(self, n):
            yield b"<html>redirect page</html>"

    def fake_get(*a, **k):
        calls.append(1)
        return _Resp()

    monkeypatch.setattr(requests.Session, "get", lambda _self, *a, **k: fake_get(*a, **k))
    part = tmp_path / "f.part"
    part.write_bytes(b"\x00" * 11)
    progress = [0]
    with pytest.raises(RuntimeError, match="302"):
        engine._download_segment("http://h/f", part, 0, 10, progress, 0)
    assert len(calls) == 1  # not retried
    assert progress == [0]
    assert part.read_bytes() == b"\x00" * 11  # the redirect body never hit the file


# --- _download_segmented (Task 5) ----------------------------------------


def _seg(server, tmp_path, segments, cb=None):
    p = _probe(server.url)
    return _download_segmented(
        p.resolved_url, tmp_path / p.filename, p.size, p.validator, segments, cb
    )


def test_segmented_end_to_end(make_server, tmp_path):
    blob = os.urandom(5 * 1024 * 1024)
    server = make_server(blob)
    out = _seg(server, tmp_path, 4)
    assert out.read_bytes() == blob
    assert not _part_path(out).exists()
    assert not _meta_path(out).exists()


def test_segmented_progress_reaches_total(make_server, tmp_path):
    blob = os.urandom(1024 * 1024)
    server = make_server(blob)
    seen = []
    _seg(server, tmp_path, 4, cb=lambda d, t, s: seen.append((d, t)))
    assert seen and seen[-1] == (len(blob), len(blob))


def test_segmented_retries_past_a_dropped_connection(make_server, tmp_path):
    blob = os.urandom(512 * 1024)
    server = make_server(blob, drop_after=1000)
    assert _seg(server, tmp_path, 4).read_bytes() == blob


def test_segmented_retries_past_a_silent_clean_close(make_server, tmp_path):
    """A server that closes the connection cleanly mid-segment - no
    Content-Length promise broken, no exception raised by `requests` - must
    not be mistaken for a successful, short segment. `_download_segment`
    must detect the short write and retry it until the segment is
    byte-complete, the same way it already does for a dropped connection."""
    blob = os.urandom(512 * 1024)
    server = make_server(blob, silent_drop_after=1000)
    assert _seg(server, tmp_path, 4).read_bytes() == blob


def test_segmented_clamps_a_server_that_ignores_the_range_end(make_server, tmp_path):
    # Server honours `start` but sends the whole tail for every segment request.
    # Each segment must write only into its own slot, or it overruns the next one.
    blob = os.urandom(512 * 1024)
    server = make_server(blob, ignore_range_end=True)
    out = _seg(server, tmp_path, 4)
    assert out.read_bytes() == blob
    assert not _part_path(out).exists()
    assert not _meta_path(out).exists()


def test_segmented_integrity_gate_trips_on_short_download(make_server, tmp_path, monkeypatch):
    # No real backoff delay: every attempt hits the same permanently-short body.
    monkeypatch.setattr(engine.time, "sleep", lambda *_: None)
    blob = os.urandom(64 * 1024)
    server = make_server(blob)
    p = _probe(server.url)
    final = tmp_path / p.filename
    # Claim 100 bytes more than the server has: the segment can never fill,
    # so it retries to exhaustion and raises _ShortRead - the same defect
    # the aggregate integrity gate used to be the only thing catching, now
    # caught per-segment instead (see test_segmented_retries_past_a_silent_clean_close).
    with pytest.raises(engine._ShortRead):
        _download_segmented(p.resolved_url, final, p.size + 100, p.validator, 1, None)
    assert _part_path(final).exists()
    assert _meta_path(final).exists()


# --- _download_single + download() wiring (Task 6) ----------------------


def test_download_single_end_to_end(make_server, tmp_path):
    blob = os.urandom(300 * 1024)
    server = make_server(blob, support_range=False)
    final = tmp_path / "file.bin"
    seen = []
    _download_single(server.url, final, len(blob), lambda d, t, s: seen.append((d, t)))
    assert final.read_bytes() == blob
    assert not _part_path(final).exists()
    assert not _meta_path(final).exists()
    assert seen and seen[-1] == (len(blob), len(blob))


def test_download_single_restarts_from_zero(make_server, tmp_path):
    blob = os.urandom(256 * 1024)
    server = make_server(blob, support_range=False)
    final = tmp_path / "file.bin"
    _download_single(server.url, final, len(blob), None)
    _download_single(server.url, final, len(blob), None)  # no half-resume
    assert final.read_bytes() == blob


def test_download_falls_back_when_no_range(make_server, tmp_path):
    blob = os.urandom(400 * 1024)
    server = make_server(blob, support_range=False)
    out = download(server.url, dest_dir=tmp_path, segments=8)
    assert out.read_bytes() == blob
    assert not _part_path(out).exists()
    assert not _meta_path(out).exists()


def test_download_segmented_path(make_server, tmp_path):
    blob = os.urandom(1024 * 1024)
    server = make_server(blob)
    out = download(server.url, dest_dir=tmp_path / "sub", segments=999)  # clamped
    assert out.read_bytes() == blob


def test_download_segmented_reports_per_segment_progress(make_server, tmp_path):
    blob = os.urandom(512 * 1024)
    server = make_server(blob)
    frames = []
    out = download(server.url, dest_dir=tmp_path, segments=4, segment_cb=frames.append)
    assert out.read_bytes() == blob
    assert frames  # at least the final call
    for frame in frames:
        assert len(frame) == 4
    last = frames[-1]
    assert all(done == total for done, total in last)


def test_download_single_reports_one_segment(make_server, tmp_path):
    blob = os.urandom(64 * 1024)
    server = make_server(blob, support_range=False)
    frames = []
    out = download(server.url, dest_dir=tmp_path, segment_cb=frames.append)
    assert out.read_bytes() == blob
    assert frames
    assert frames[-1] == [(len(blob), len(blob))]


def test_download_recovers_from_midflight_range_not_supported(make_server, tmp_path, monkeypatch):
    blob = os.urandom(200 * 1024)
    server = make_server(blob)

    def boom(resolved_url, final, *a, **k):
        _part_path(final).write_bytes(b"stale partial")
        _meta_path(final).write_text("{}")
        raise RangeNotSupported(resolved_url)

    monkeypatch.setattr(engine, "_download_segmented", boom)
    out = download(server.url, dest_dir=tmp_path, segments=4)
    assert out.read_bytes() == blob
    assert not _part_path(out).exists()
    assert not _meta_path(out).exists()


# --- resume proof (Task 7) ---------------------------------------------


def _preseed(server, tmp_path, done_segments, segments=4, validator=None):
    """Write a `.part` with `done_segments` filled from the real blob and a
    sidecar that matches. Returns (final, ranges, blob)."""
    blob = server.data
    p = _probe(server.url)
    final = tmp_path / p.filename
    ranges = split_ranges(p.size, segments)
    progress = [0] * segments
    with open(_part_path(final), "wb") as f:
        f.truncate(p.size)
        for i in done_segments:
            s, e = ranges[i]
            f.seek(s)
            f.write(blob[s : e + 1])
            progress[i] = e - s + 1
    _save_progress(
        _meta_path(final), p.resolved_url, p.size,
        p.validator if validator is None else validator, progress,
    )
    return final, ranges, blob


def test_download_resumes_only_the_remainder(make_server, tmp_path):
    blob = os.urandom(4 * 1024 * 1024)
    server = make_server(blob)
    _, ranges, _ = _preseed(server, tmp_path, done_segments=(0, 1), segments=4)

    server.served_bytes = 0
    out = download(server.url, dest_dir=tmp_path, segments=4)
    assert out.read_bytes() == blob

    remaining = sum(ranges[i][1] - ranges[i][0] + 1 for i in (2, 3))
    # probe costs 1 byte; each in-flight segment may retry once (<= one CHUNK slack).
    assert remaining <= server.served_bytes <= remaining + 1 + 2 * CHUNK


def test_download_restarts_clean_on_stale_validator(make_server, tmp_path):
    blob = os.urandom(1024 * 1024)
    server = make_server(blob)
    # Sidecar says segments 0-1 are done, but under a validator that no longer matches.
    _preseed(server, tmp_path, done_segments=(0, 1), segments=4, validator='"old-etag"')
    out = download(server.url, dest_dir=tmp_path, segments=4)
    assert out.read_bytes() == blob  # stale sidecar ignored, no corruption
    assert not _meta_path(out).exists()


# --- code-review fixes -------------------------------------------------


def test_download_restarts_clean_when_part_file_is_missing(make_server, tmp_path):
    # Sidecar validates against the live probe but the .part bytes are gone.
    blob = os.urandom(1024 * 1024)
    server = make_server(blob)
    final, _, _ = _preseed(server, tmp_path, done_segments=(0, 1), segments=4)
    _part_path(final).unlink()
    out = download(server.url, dest_dir=tmp_path, segments=4)
    assert out.read_bytes() == blob
    assert not _meta_path(out).exists()


def test_download_restarts_clean_when_part_file_is_wrong_size(make_server, tmp_path):
    blob = os.urandom(1024 * 1024)
    server = make_server(blob)
    final, _, _ = _preseed(server, tmp_path, done_segments=(0, 1), segments=4)
    _part_path(final).write_bytes(b"truncated")  # sidecar still claims 2 segments done
    out = download(server.url, dest_dir=tmp_path, segments=4)
    assert out.read_bytes() == blob


def test_probe_survives_unknown_content_range_total(monkeypatch):
    class _Resp:
        url = "http://h/f.bin"
        status_code = 206
        headers = {"Content-Range": "bytes 0-0/*", "Content-Length": "0"}  # noqa: RUF012

        def raise_for_status(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(requests.Session, "get", lambda *a, **k: _Resp())
    assert _probe("http://h/f.bin").size == 0  # no crash on int("*")


def test_download_single_rejects_a_truncated_stream(monkeypatch, tmp_path):
    class _Resp:
        status_code = 200
        headers = {}  # noqa: RUF012

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, n):
            yield b"only a few bytes"

    monkeypatch.setattr(requests.Session, "get", lambda *a, **k: _Resp())
    final = tmp_path / "f.bin"
    with pytest.raises(IntegrityError):
        _download_single("http://h/f.bin", final, 1_000_000, None)
    assert not final.exists()  # never promoted


def test_segment_does_not_retry_client_errors(monkeypatch, tmp_path):
    calls = []

    class _Resp:
        status_code = 404
        headers = {}  # noqa: RUF012

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            raise requests.HTTPError(response=self)

        def iter_content(self, n):
            return iter(())

    def fake_get(*a, **k):
        calls.append(1)
        return _Resp()

    monkeypatch.setattr(requests.Session, "get", lambda _self, *a, **k: fake_get(*a, **k))
    part = tmp_path / "f.part"
    part.write_bytes(b"\x00" * 11)
    with pytest.raises(requests.HTTPError):
        engine._download_segment("http://h/f", part, 0, 10, [0], 0)
    assert len(calls) == 1  # 404 is fatal, not retried


# --- cooperative cancellation (Milestone 2 Task 2) -----------------------


def test_download_segmented_honours_cancel(make_server, tmp_path):
    blob = os.urandom(4 * 1024 * 1024)
    server = make_server(blob)
    p = _probe(server.url)
    final = tmp_path / p.filename
    cancel = threading.Event()

    def on_progress(done, total, speed):
        if done > 0:
            cancel.set()

    with pytest.raises(Cancelled):
        _download_segmented(
            p.resolved_url, final, p.size, p.validator, 4, on_progress, cancel=cancel
        )
    assert _part_path(final).exists()
    assert _meta_path(final).exists()


def test_download_single_honours_cancel(make_server, tmp_path):
    blob = os.urandom(300 * 1024)
    server = make_server(blob, support_range=False)
    final = tmp_path / "file.bin"
    cancel = threading.Event()
    cancel.set()  # already set before the first chunk
    with pytest.raises(Cancelled):
        _download_single(server.url, final, len(blob), None, cancel=cancel)
    assert _part_path(final).exists()


def test_cancel_none_is_noop(make_server, tmp_path):
    blob = os.urandom(400 * 1024)
    server = make_server(blob, support_range=False)
    out = download(server.url, dest_dir=tmp_path, segments=8, cancel=None)
    assert out.read_bytes() == blob


# --- safety guards: free space, netcheck, output path (Milestone 2 Task 3) --

_NO_SPACE = type("_NoSpace", (), {"free": 10})()


def test_insufficient_space_raises_segmented(make_server, tmp_path, monkeypatch):
    blob = os.urandom(4096)
    server = make_server(blob)
    p = _probe(server.url)
    final = tmp_path / p.filename
    monkeypatch.setattr(engine.shutil, "disk_usage", lambda path: _NO_SPACE)
    with pytest.raises(InsufficientSpace):
        _download_segmented(p.resolved_url, final, p.size, p.validator, 2, None)


def test_insufficient_space_raises_single(make_server, tmp_path, monkeypatch):
    blob = os.urandom(4096)
    server = make_server(blob, support_range=False)
    final = tmp_path / "file.bin"
    monkeypatch.setattr(engine.shutil, "disk_usage", lambda path: _NO_SPACE)
    with pytest.raises(InsufficientSpace):
        _download_single(server.url, final, len(blob), None)


def test_insufficient_space_accounts_for_existing_part_segmented(
    make_server, tmp_path, monkeypatch
):
    """A resumed .part has already reserved `size` bytes on disk - the check
    must not demand `size + FREE_SLACK` free on top of that."""
    blob = os.urandom(4096)
    server = make_server(blob)
    p = _probe(server.url)
    final = tmp_path / p.filename
    with open(_part_path(final), "wb") as f:
        f.truncate(p.size)
    free = type("_Free", (), {"free": engine.FREE_SLACK})()
    monkeypatch.setattr(engine.shutil, "disk_usage", lambda path: free)
    out = _download_segmented(p.resolved_url, final, p.size, p.validator, 2, None)
    assert out.read_bytes() == blob


def test_insufficient_space_accounts_for_existing_part_single(
    make_server, tmp_path, monkeypatch
):
    blob = os.urandom(4096)
    server = make_server(blob, support_range=False)
    final = tmp_path / "file.bin"
    with open(_part_path(final), "wb") as f:
        f.truncate(len(blob))  # e.g. a stale .part from an earlier segmented attempt
    free = type("_Free", (), {"free": engine.FREE_SLACK})()
    monkeypatch.setattr(engine.shutil, "disk_usage", lambda path: free)
    out = _download_single(server.url, final, len(blob), None)
    assert out.read_bytes() == blob


def test_download_segment_rechecks_netcheck(make_server, tmp_path, monkeypatch):
    blob = os.urandom(4096)
    server = make_server(blob)
    p = _probe(server.url)
    final = tmp_path / p.filename

    def _blocked(url):
        raise BlockedURLError("blocked")

    monkeypatch.setattr(netcheck, "assert_allowed_url", _blocked)
    with pytest.raises(BlockedURLError):
        _download_segmented(p.resolved_url, final, p.size, p.validator, 2, None)


def test_download_single_rechecks_netcheck(make_server, tmp_path, monkeypatch):
    blob = os.urandom(4096)
    server = make_server(blob, support_range=False)
    final = tmp_path / "file.bin"

    def _blocked(url):
        raise BlockedURLError("blocked")

    monkeypatch.setattr(netcheck, "assert_allowed_url", _blocked)
    with pytest.raises(BlockedURLError):
        _download_single(server.url, final, len(blob), None)


def test_output_path_escape_rejected(make_server, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    blob = os.urandom(1024)
    server = make_server(blob, support_range=False)  # fixture always names it file.bin
    try:
        os.symlink(outside / "evil.bin", dest / "file.bin")
    except OSError:
        pytest.skip("symlink creation not permitted on this platform")
    with pytest.raises(ValueError, match="escapes"):
        download(server.url, dest_dir=dest)


def test_download_does_not_reuse_cookies_between_downloads(monkeypatch, tmp_path):
    sent = []

    class _Resp:
        status_code = 206
        headers = {"Content-Range": "bytes 0-0/1", "Set-Cookie": "leak=1"}  # noqa: RUF012

        def raise_for_status(self):
            pass

        def close(self):
            pass

    def fake_send(self, request, **_kw):
        sent.append(request.headers.get("Cookie"))
        self.cookies.set("leak", "1")  # as requests would after a Set-Cookie response
        return _Resp()

    monkeypatch.setattr(requests.Session, "send", fake_send)
    monkeypatch.setattr(engine, "_download_segmented", lambda *a, **k: Path("x"))
    monkeypatch.setattr(engine, "_download_single", lambda *a, **k: Path("x"))
    download("http://h/f.bin", tmp_path)
    download("http://h/f.bin", tmp_path)
    assert sent == [None, None]  # the first download's cookie is not sent on the second


def test_drive_403_gets_fresh_link_hint_and_keeps_status():
    class _R:
        status_code = 403
        url = "https://drive.usercontent.google.com/download?id=x"

        def raise_for_status(self):
            raise requests.HTTPError("403 Client Error", response=self)

    with pytest.raises(requests.HTTPError, match="tdm catch") as ei:
        engine._raise_for_status(_R())
    assert ei.value.response.status_code == 403
    _R.url = "https://example.com/f"
    with pytest.raises(requests.HTTPError) as ei:
        engine._raise_for_status(_R())
    assert "tdm catch" not in str(ei.value)
