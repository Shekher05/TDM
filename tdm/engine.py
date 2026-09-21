"""Segmented HTTP download engine with resume support.

Resume state is flushed to a JSON sidecar with an atomic temp-file swap but no
``fsync``: an interrupted *process* resumes cleanly, while a hard power loss may
cost the last sub-second of progress. That is the intended durability scope.
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import threading
import time
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from pathlib import Path
from typing import NamedTuple
from urllib.parse import unquote, urlsplit, urlunsplit

import requests

from tdm import netcheck

CHUNK = 65536
SEGMENT_RETRIES = 5
SAVE_INTERVAL = 1.0
MAX_SEGMENTS = 32
FREE_SLACK = 1 << 20  # 1 MiB headroom required beyond the download's own size

# One session shared by the probe and every segment request.
# ponytail: plain Session, no mounted HTTPAdapter(pool_maxsize=MAX_SEGMENTS) yet.
# Upgrade path: mount one only if 32 concurrent segments log "connection pool is full".
_session = requests.Session()

# A modern browser User-Agent/Accept set, replacing requests' identifying
# default ("python-requests/x.y.z"). Helps only against naive User-Agent
# string filtering - it does not defeat TLS/JA3-fingerprint-based bot
# detection (Cloudflare and similar), which no amount of request headers
# can spoof from a plain requests/urllib3 session.
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}
_session.headers.update(_DEFAULT_HEADERS)

# Windows reserved device names: reserved regardless of extension, case-insensitive.
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def split_ranges(size: int, n: int) -> list[tuple[int, int]]:
    """Split [0, size) into at most n contiguous, inclusive (start, end) ranges.

    # ponytail: static equal segmentation - the slowest connection sets the
    # finish time and there is no rebalancing. Upgrade path: work-stealing /
    # dynamic re-segmentation (hand a stalled segment's tail to an idle worker).
    """
    if size <= 0:
        raise ValueError("size must be positive")
    n = max(1, min(n, size))
    step = size // n
    ranges: list[tuple[int, int]] = []
    start = 0
    for i in range(n):
        end = size - 1 if i == n - 1 else start + step - 1
        ranges.append((start, end))
        start = end + 1
    return ranges


def extension_hint(filename: str) -> str | None:
    """A one-line tip for a filename with no real extension - either
    ``derive_filename()``'s own ``download.bin`` fallback, or any other name
    that ends up with no suffix at all. It never claims to know the actual
    file type (no content sniffing, no renaming) - it only fires when the
    extension itself gives the OS nothing to open the file with."""
    if filename != "download.bin" and Path(filename).suffix:
        return None
    return (
        "Note: this file has no file extension, so your OS may not know how "
        "to open it. If it's a video, try renaming it with .mp4, .webm, "
        ".mkv, .mov, or .avi; if it's an image, try .jpeg, .png, .webp, "
        "or .gif."
    )


def derive_filename(url: str, resp) -> str:
    """Pick an output filename from Content-Disposition, then the URL, then a default.

    The result is a bare basename safe to join onto the output directory on the
    primary (Windows) platform: directory separators and NTFS alternate-data-stream
    colons are dropped, control characters and trailing dots/spaces stripped, and
    anything that resolves to empty, ``.``/``..``, or a reserved device name
    (``CON``, ``NUL``, ``COM1`` ...) falls back to ``download.bin``.

    RFC 5987 ``filename*=`` is intentionally not handled - out of scope here.
    """
    cd = resp.headers.get("Content-Disposition", "")
    name = ""
    if "filename=" in cd:
        name = cd.split("filename=", 1)[1].split(";")[0].strip().strip('"')
    if not name:
        name = unquote(urlsplit(url).path).rsplit("/", 1)[-1]
    # Keep only the basename: no separators, no ADS colon suffix.
    name = name.replace("\\", "/").rsplit("/", 1)[-1].split(":", 1)[0]
    name = _CONTROL_CHARS.sub("", name).rstrip(". ")
    if not name or name.split(".", 1)[0].upper() in _RESERVED_NAMES:
        return "download.bin"
    return name


class ProbeResult(NamedTuple):
    size: int
    accept_ranges: bool
    validator: str
    filename: str
    resolved_url: str


def _probe(url: str, headers: dict[str, str] | None = None) -> ProbeResult:
    """One ranged ``GET`` (``bytes=0-0``) that doubles as the probe.

    Returns the total size, whether the server honours ``Range``, a strong
    validator for resume (``ETag`` then ``Last-Modified`` then ``""``), the output
    filename, and the redirect-resolved URL that every segment request will reuse.
    A ``Range`` request that a server honours comes back ``206`` with a
    ``Content-Range`` whose ``/<total>`` is the real size; a server that ignores
    it answers ``200`` and ``accept_ranges`` is ``False``. ``headers``, if given
    (e.g. a forwarded ``Cookie``), is merged in - the probe's own ``Range``/
    ``Accept-Encoding`` always take precedence over a caller-supplied value
    for those two keys.
    """
    r = _session.get(
        url,
        headers={**(headers or {}), "Range": "bytes=0-0", "Accept-Encoding": "identity"},
        timeout=30,
        allow_redirects=True,
        stream=True,
    )
    try:
        r.raise_for_status()
        netcheck.assert_allowed_url(r.url)
        accept_ranges = r.status_code == 206
        content_range = r.headers.get("Content-Range", "")
        total = content_range.rsplit("/", 1)[1] if "/" in content_range else ""
        # `Content-Range: bytes 0-0/*` (unknown total) -> fall back to Content-Length.
        size = int(total) if total.isdigit() else int(r.headers.get("Content-Length", 0))
        validator = r.headers.get("ETag") or r.headers.get("Last-Modified") or ""
        return ProbeResult(size, accept_ranges, validator, derive_filename(r.url, r), r.url)
    finally:
        r.close()


# --- resume sidecar ---------------------------------------------------------
#
# Two files sit next to the output: ``<name>.part`` (the bytes, preallocated to
# full size) and ``<name>.tdm.json`` (this sidecar). The sidecar is exactly
# ``{"url": str, "size": int, "etag": str, "progress": [int, ...]}`` where each
# int is bytes completed for that segment. A load is trusted only when url, size
# and a *non-empty* etag all match the current probe.

_MAX_SIDECAR_SEGMENTS = 1024  # more entries than this is a tampered file, not us


def _part_path(final: Path) -> Path:
    return final.with_name(final.name + ".part")


def _meta_path(final: Path) -> Path:
    return final.with_name(final.name + ".tdm.json")


def _sidecar_url(url: str) -> str:
    """The URL as stored in the sidecar: any ``user:pass@`` stripped so credentials
    never reach disk. The credentialed URL is kept in memory for the request only."""
    parts = urlsplit(url)
    if parts.username or parts.password:
        netloc = parts.hostname or ""
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        parts = parts._replace(netloc=netloc)
    return urlunsplit(parts)


def _load_progress(meta: Path, url: str, size: int, etag: str) -> list[int] | None:
    """Return the saved per-segment byte counts, but only if the sidecar is
    trustworthy. Returns None (never raises) for a missing or unparseable file, a
    url/size mismatch, an absent/empty or mismatched etag, or a progress list that
    is not all non-negative ints each within its own segment's length."""
    if not etag:
        return None
    try:
        data = json.loads(meta.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if (
        data.get("url") != _sidecar_url(url)
        or data.get("size") != size
        or data.get("etag") != etag
    ):
        return None
    progress = data.get("progress")
    if not isinstance(progress, list) or not 1 <= len(progress) <= _MAX_SIDECAR_SEGMENTS:
        return None
    ranges = split_ranges(size, len(progress))
    if len(progress) != len(ranges):
        return None
    for done, (start, end) in zip(progress, ranges, strict=True):
        if type(done) is not int or not 0 <= done <= end - start + 1:
            return None
    return progress


def _save_progress(meta: Path, url: str, size: int, etag: str, progress: list[int]) -> None:
    """Atomically write the sidecar: temp file then ``os.replace`` (no fsync - see
    the module docstring). A crash mid-write cannot strand the whole download."""
    tmp = meta.with_name(meta.name + ".tmp")
    tmp.write_text(
        json.dumps({"url": _sidecar_url(url), "size": size, "etag": etag, "progress": progress})
    )
    os.replace(tmp, meta)


def rebind_progress(final: Path, new_url: str, new_validator: str) -> None:
    """Point an existing resume sidecar at a fresh url/etag, keeping its saved
    ``progress`` untouched. For a presigned URL whose signature has rotated:
    the sidecar's old ``url`` no longer matches the fresh one, so a plain
    ``engine.download()`` call would otherwise treat this as a brand-new
    download and discard all progress. Raises ``FileNotFoundError`` if
    ``final`` has no sidecar - callers must only rebind a genuinely paused or
    errored download."""
    meta = _meta_path(final)
    data = json.loads(meta.read_text())
    data["url"] = _sidecar_url(new_url)
    data["etag"] = new_validator
    tmp = meta.with_name(meta.name + ".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, meta)


# --- segmented download ----------------------------------------------------


class RangeNotSupported(Exception):
    """A server answered a ``Range`` request with ``200`` and the whole body."""


class IntegrityError(Exception):
    """The assembled ``.part`` does not account for every byte the probe promised.
    Raised *before* promotion so the ``.part`` and sidecar survive for a retry."""


class Cancelled(Exception):
    """Raised when a caller-supplied ``cancel`` event is set mid-download.
    ``.part`` and the sidecar are left in place - the caller decides their fate."""


class InsufficientSpace(Exception):
    """Raised before the first write when the destination volume does not have
    room for the download plus ``FREE_SLACK`` headroom."""


class _ShortRead(Exception):
    """Internal to ``_download_segment``: the response body ended - cleanly,
    no exception from ``requests`` - before ``need`` bytes were written (a
    mid-stream close with no ``Content-Length`` mismatch to flag it). Treated
    like a dropped connection: caught by the same retry-with-resume loop."""


def _check_free_space(part: Path, size: int) -> None:
    """Require ``size`` bytes of headroom beyond ``FREE_SLACK``, minus whatever
    ``part`` (e.g. from a resumed or stale prior run) has already reserved on
    disk - a resume should not be rejected for space it already holds."""
    if not size:
        return
    existing = part.stat().st_size if part.exists() else 0
    if shutil.disk_usage(part.parent).free < size - existing + FREE_SLACK:
        raise InsufficientSpace(f"not enough free space for {size} bytes")


def _emit(cb, done: int, total: int, speed: list[float]) -> None:
    """Call ``cb(done, total, bytes_per_sec)``. Speed is the slope between this
    call and the previous one - two stored floats, not a sample buffer."""
    if cb is None:
        return
    now = time.monotonic()
    dt = now - speed[0]
    rate = (done - speed[1]) / dt if dt > 0 else 0.0
    speed[0], speed[1] = now, done
    cb(done, total, rate)


def _write_segment_chunks(
    r,
    f,
    need: int,
    progress: list[int],
    idx: int,
    cancel: threading.Event | None,
) -> None:
    """Write ``r``'s body into ``f`` at its current position, advancing
    ``progress[idx]`` per chunk and stopping once ``need`` bytes are written."""
    for chunk in r.iter_content(CHUNK):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        room = need - progress[idx]
        if len(chunk) > room:
            chunk = chunk[:room]  # server ignored the range end
        f.write(chunk)
        progress[idx] += len(chunk)
        if progress[idx] >= need:
            break


def _download_segment(
    resolved_url: str,
    part: Path,
    start: int,
    end: int,
    progress: list[int],
    idx: int,
    cancel: threading.Event | None = None,
    headers: dict[str, str] | None = None,
) -> None:
    """Fetch ``[start + progress[idx] .. end]`` into ``part`` at its true offset.

    Single writer per slot: this thread is the only one that touches
    ``progress[idx]`` and this disjoint file region (ownership, not atomicity).
    Retries a dropped connection up to ``SEGMENT_RETRIES`` times, resuming from
    the current offset; a ``200`` response raises ``RangeNotSupported``. A set
    ``cancel`` event raises ``Cancelled`` instead of retrying or writing further.
    ``headers``, if given, is merged in the same way as ``_probe``'s.
    """
    need = end - start + 1
    for attempt in range(SEGMENT_RETRIES):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        netcheck.assert_allowed_url(resolved_url)
        pos = start + progress[idx]
        if pos > end:
            return
        try:
            with _session.get(
                resolved_url,
                headers={**(headers or {}), "Range": f"bytes={pos}-{end}", "Accept-Encoding": "identity"},
                stream=True,
                timeout=30,
                allow_redirects=False,
            ) as r:
                if r.status_code == 200:
                    raise RangeNotSupported(resolved_url)
                r.raise_for_status()
                if r.headers.get("Content-Encoding"):
                    raise RuntimeError(
                        f"segment {idx}: unexpected Content-Encoding "
                        f"{r.headers['Content-Encoding']!r} - byte offsets would desync"
                    )
                with open(part, "r+b") as f:
                    f.seek(pos)
                    _write_segment_chunks(r, f, need, progress, idx, cancel)
                if progress[idx] < need:
                    raise _ShortRead(
                        f"segment {idx}: closed after {progress[idx]} of {need} bytes"
                    )
            return
        except (RangeNotSupported, Cancelled):
            raise
        except (requests.RequestException, _ShortRead) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            fatal = status is not None and 400 <= status < 500 and status not in (408, 429)
            if fatal or attempt == SEGMENT_RETRIES - 1:
                raise
            time.sleep(min(2 ** attempt, 8) + random.random())


def _download_segmented(
    resolved_url: str,
    final: Path,
    size: int,
    validator: str,
    segments: int,
    cb,
    cancel: threading.Event | None = None,
    segment_cb=None,
    headers: dict[str, str] | None = None,
) -> Path:
    """Download ``resolved_url`` into ``final`` using ``segments`` parallel ranges,
    resuming from a trusted sidecar if one is present. Promotes ``.part`` to
    ``final`` only after every segment is byte-complete. A set ``cancel`` event
    stops the run and raises ``Cancelled`` before the integrity gate, leaving
    ``.part`` and the sidecar in place. ``segment_cb``, if given, is called with
    a ``[(done, total), ...]`` list (one entry per segment) on the same cadence
    as ``cb``, plus once more on completion. ``headers``, if given, is forwarded
    to every segment request (see ``_probe``)."""
    part, meta = _part_path(final), _meta_path(final)
    _check_free_space(part, size)
    ranges = split_ranges(size, segments)
    totals = [e - s + 1 for s, e in ranges]
    progress = _load_progress(meta, resolved_url, size, validator)
    # A trusted sidecar is only usable if the .part it describes is still there
    # at full length - otherwise "segment N done" points at bytes that don't exist.
    part_ready = part.is_file() and part.stat().st_size == size
    if progress is None or len(progress) != len(ranges) or not part_ready:
        progress = [0] * len(ranges)
        with open(part, "wb") as f:
            f.truncate(size)
    _save_progress(meta, resolved_url, size, validator, progress)

    speed = [time.monotonic(), 0.0]
    ex = ThreadPoolExecutor(max_workers=min(len(ranges), MAX_SEGMENTS))
    try:
        pending = {
            ex.submit(_download_segment, resolved_url, part, s, e, progress, i, cancel, headers)
            for i, (s, e) in enumerate(ranges)
            if progress[i] < e - s + 1
        }
        while pending and not (cancel is not None and cancel.is_set()):
            done, pending = wait(pending, timeout=SAVE_INTERVAL, return_when=FIRST_EXCEPTION)
            _save_progress(meta, resolved_url, size, validator, progress)
            _emit(cb, sum(progress), size, speed)
            if segment_cb is not None:
                segment_cb(list(zip(progress, totals, strict=True)))
            for fut in done:
                if fut.exception():
                    raise fut.exception()
    finally:
        ex.shutdown(wait=True, cancel_futures=True)

    if cancel is not None and cancel.is_set():
        raise Cancelled()

    if sum(progress) != size or any(
        progress[i] != e - s + 1 for i, (s, e) in enumerate(ranges)
    ):
        raise IntegrityError(
            f"{final.name}: assembled {sum(progress)} of {size} bytes; .part kept for retry"
        )

    _save_progress(meta, resolved_url, size, validator, progress)
    _emit(cb, size, size, speed)
    if segment_cb is not None:
        segment_cb(list(zip(progress, totals, strict=True)))
    os.replace(part, final)
    meta.unlink(missing_ok=True)
    return final


def _download_single(
    resolved_url: str,
    final: Path,
    size: int,
    cb,
    cancel: threading.Event | None = None,
    segment_cb=None,
    headers: dict[str, str] | None = None,
) -> Path:
    """One streamed ``GET`` into ``final``, always from zero - no resume.

    A server that ignored ``Range`` on the probe will ignore it on a re-run too,
    so there is nothing to resume: ``part`` is rewritten (``"wb"``) every call.
    Used for no-range or unknown-size sources, and as the fallback when a
    segmented run hits ``RangeNotSupported`` mid-flight. A set ``cancel`` event
    raises ``Cancelled``, leaving ``.part`` and the sidecar in place.
    ``segment_cb``, given, sees a single-element list so callers never have to
    special-case the fallback path. ``headers``, if given, is merged in the
    same way as ``_probe``'s.
    """
    part, meta = _part_path(final), _meta_path(final)
    _check_free_space(part, size)
    netcheck.assert_allowed_url(resolved_url)
    speed = [time.monotonic(), 0.0]
    with _session.get(
        resolved_url,
        headers={**(headers or {}), "Accept-Encoding": "identity"},
        stream=True,
        timeout=30,
        allow_redirects=False,
    ) as r:
        r.raise_for_status()
        if r.status_code >= 300:  # redirect body on the already-resolved URL - not the file
            raise RuntimeError(f"unexpected {r.status_code} response - no body to save")
        if r.headers.get("Content-Encoding"):
            raise RuntimeError(
                f"unexpected Content-Encoding {r.headers['Content-Encoding']!r} "
                f"- refusing to save a decoded body"
            )
        done = 0
        last_save = time.monotonic()
        with open(part, "wb") as f:
            for chunk in r.iter_content(CHUNK):
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                f.write(chunk)
                done += len(chunk)
                if time.monotonic() - last_save >= SAVE_INTERVAL:
                    _save_progress(meta, resolved_url, size, "", [done])
                    _emit(cb, done, size or done, speed)
                    if segment_cb is not None:
                        segment_cb([(done, size or done)])
                    last_save = time.monotonic()
    if size and done != size:  # clean EOF short of the promised length
        raise IntegrityError(f"{final.name}: got {done} of {size} bytes; not promoted")
    _emit(cb, done, done, speed)
    if segment_cb is not None:
        segment_cb([(done, done)])
    os.replace(part, final)
    meta.unlink(missing_ok=True)
    return final


def download(
    url: str,
    dest_dir: str | Path = ".",
    segments: int = 8,
    progress_cb=None,
    cancel: threading.Event | None = None,
    segment_cb=None,
    on_probed=None,
    headers: dict[str, str] | None = None,
) -> Path:
    """Download ``url`` into ``dest_dir``, returning the final path.

    Probes once; runs the segmented path when the server honours ``Range`` and
    the size is known, otherwise a single connection. A mid-flight
    ``RangeNotSupported`` discards the partial download and retries once via the
    single-connection path - it is never re-raised to the caller. A set
    ``cancel`` event raises ``Cancelled``, which propagates to the caller
    unchanged - it is not an integrity failure. ``segment_cb``, if given, is
    called with a ``[(done, total), ...]`` list (see ``_download_segmented`` and
    ``_download_single``); default ``None`` leaves every existing caller unaffected.
    ``on_probed``, if given, is called once with the resolved output filename
    right after probing, before any bytes are written - callers that need to
    know the filename of a download that may later be paused (which never
    reaches a "done" callback) should use this instead. ``headers``, if given
    (e.g. a forwarded ``Cookie``), is merged into every request this download
    makes - the probe's, every segment's, and the single-connection fallback's.
    """
    segments = max(1, min(segments, MAX_SEGMENTS))
    dest = Path(dest_dir).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    probe = _probe(url, headers)
    final = dest / probe.filename
    if dest not in final.resolve().parents or dest not in _part_path(final).resolve().parents:
        raise ValueError("output path escapes dest_dir")
    if on_probed is not None:
        on_probed(probe.filename)
    if not probe.accept_ranges or probe.size <= 0:
        return _download_single(
            probe.resolved_url, final, probe.size, progress_cb, cancel, segment_cb, headers
        )
    try:
        return _download_segmented(
            probe.resolved_url,
            final,
            probe.size,
            probe.validator,
            segments,
            progress_cb,
            cancel,
            segment_cb,
            headers,
        )
    except RangeNotSupported:
        _part_path(final).unlink(missing_ok=True)
        _meta_path(final).unlink(missing_ok=True)
        return _download_single(
            probe.resolved_url, final, probe.size, progress_cb, cancel, segment_cb, headers
        )
