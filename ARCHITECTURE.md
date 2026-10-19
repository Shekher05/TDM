# TDM — Architecture & Workflow Guide

> **Purpose of this file:** a single entry point so a human or an AI system can
> understand the whole project without reading every file first. Read this, then
> open the specific module you need. Written from a full read of every source,
> test, and doc file in the repo (version `0.1.0`).
>
> **Snapshot:** 2026-10-13, branch `gm1` on top of commit `11a9715`; `pytest` = 187 passed,
> 1 skipped (symlink privilege), `ruff check .` clean. Read sections 12-13
> before changing code. **Not captured here:** the design *rationale* in the
> gitignored planning docs (`PROJECT.md`, `CONSTRAINTS.md`,
> `docs/milestone-2-security.md`, `tasks/`) — if you have them, they outrank
> this file on "why".

---

## 1. What this project is

**TDM** is a small, personal, *learning-oriented* **segmented HTTP(S) download
manager** written in Python 3.11+ with a single runtime dependency (`requests`).
It is explicitly **not** meant to compete with aria2 / Motrix; it exists to
understand segmented downloading and resume.

It has three layers, built as three milestones:

| Milestone | Capability | Main code |
|---|---|---|
| **M1 — Engine + CLI** | One file, N parallel `Range` connections, resumable via a JSON sidecar, integrity-checked before promotion | `tdm/engine.py`, `tdm/__main__.py` |
| **M2 — Background service** | Local HTTP API (127.0.0.1, bearer token), persistent FIFO queue, pause/resume/cancel, host demotion, restart recovery | `tdm/service.py`, `tdm/client.py`, `tdm/paths.py` |
| **M3 — Browser "catch" mode** | Chrome MV3 extension intercepts a browser download, forwards URL + cookies to a foreground terminal that asks `y/n`, then TDM downloads it | `extension/`, `tdm/service.py` (`run_catch`) |

Milestone 4 (GUI, clipboard capture, `.exe` packaging) is **deliberately not built**.

**Scope boundary:** one URL that resolves directly to file bytes. No JavaScript
execution, no login flows, no HLS/DASH, no FTP/torrent. See `limitations.md`.

---

## 2. Repository layout

```
TDM/
├── tdm/                    # the Python package (all production code)
│   ├── __init__.py         # __version__ = "0.1.0"
│   ├── __main__.py         # CLI entry: `python -m tdm ...` (argparse, rendering only)
│   ├── engine.py           # segmented download engine + resume sidecar  (the core)
│   ├── service.py          # Manager (queue/scheduler) + HTTP API + catch mode + process mgmt
│   ├── client.py           # thin `requests` wrapper the CLI uses to call the service
│   ├── netcheck.py         # SSRF guard (blocks private/loopback/link-local/etc.)
│   ├── paths.py            # state dir resolution + atomic file write helpers
│   └── redact.py           # credential redaction / stripping for URLs
├── extension/              # Chrome MV3 extension ("TDM Catch")
│   ├── manifest.json       # perms: downloads, cookies; host: http://127.0.0.1:8765/*
│   └── background.js       # service worker: onCreated -> flag URL to TDM -> cancel Chrome's download
├── tests/                  # pytest suite, no real network (local HTTP fixture)
│   ├── conftest.py         # make_server fixture (range-capable HTTP server w/ fault injection)
│   ├── test_engine.py      # engine unit + integration (largest)
│   ├── test_service.py     # Manager, HTTP layer, catch mode, rendering (largest)
│   ├── test_cli.py         # CLI wiring, exit codes, redaction
│   ├── test_netcheck.py    # SSRF guard
│   └── test_paths.py       # state dir / atomic write
├── README.md               # user-facing docs + manual smoke-test results
├── limitations.md          # what TDM cannot download, and why (by design)
├── requirements.txt        # requests>=2.31
├── ARCHITECTURE.md         # (this file)
└── .gitignore
```

**Gitignored / not in the repo** (referenced by docs but absent): `docs/`
(e.g. `docs/milestone-2-security.md`), `tasks/`, `PROJECT.md`, `CONSTRAINTS.md`,
`SESSION_GUIDE.md`, `Document.md`, `change.md`, and `CLAUDE.md`. Code comments and
`README.md` link to some of these; they are local-only planning files.
`CLAUDE.md` (local, untracked) contains generic LLM coding-behavior guidelines
(think before coding, simplicity first, surgical changes, goal-driven execution).

---

## 3. Component diagram

```
                        ┌──────────────────────────────────────────────┐
  Chrome                │  extension/background.js                     │
  download starts ─────►│  1. POST :8765/ext/health  -> gets ext token │
                        │  2. read cookies/referrer/UA for the URL     │
                        │  3. POST :8765/ext/flag {url, headers}       │
                        │  4. on 202: cancel + erase Chrome's download │
                        └───────────────────┬──────────────────────────┘
                                            │ HTTP, Origin: chrome-extension://<id>
                                            ▼
┌────────────────┐   HTTP + Bearer   ┌──────────────────────────────────────────────┐
│ CLI subcommands│ ────────────────► │ tdm/service.py                               │
│ add/status/    │   (client.py)     │   _Server (ThreadingHTTPServer, 127.0.0.1)   │
│ pause/resume/  │                   │     └─ _Handler: auth + routes               │
│ cancel/stop    │                   │   Manager: queue + scheduler thread          │
└────────────────┘                   │     └─ ThreadPoolExecutor(max_concurrent=3)  │
                                     │          └─ engine.download(...) per item    │
`python -m tdm <url>`  ─────────────►│   run_catch(): foreground y/n loop + redraw  │
(one-shot, no service)  engine direct└──────────────────────┬───────────────────────┘
                                                            ▼
                                     ┌──────────────────────────────────────────────┐
                                     │ tdm/engine.py                                │
                                     │   _probe -> split_ranges -> N × _download_   │
                                     │   segment (threads) -> integrity gate ->     │
                                     │   os.replace(.part -> final)                 │
                                     │   guards: netcheck, free-space, path contain │
                                     └──────────────────────────────────────────────┘
```

**Dependency direction (no cycles):**
`__main__ → engine, redact` (and lazily `client`, `service`);
`client → service, requests`; `service → engine, netcheck, paths, redact`;
`engine → netcheck`; `netcheck`, `paths`, `redact` are leaves.

---

## 4. Module reference

### 4.1 `tdm/engine.py` — the download engine (core)

**Public entry:** `download(url, dest_dir=".", segments=8, progress_cb=None, cancel=None, segment_cb=None, on_probed=None, headers=None) -> Path`

Flow inside `download()`:
1. Clamp `segments` to `[1, MAX_SEGMENTS=32]`; resolve + `mkdir` `dest_dir`.
2. `_probe(url, headers)` — a `GET` with `Range: bytes=0-0` and
   `Accept-Encoding: identity`. Returns `ProbeResult(size, accept_ranges,
   validator, filename, resolved_url)`.
   - **Redirects are followed by hand** (`allow_redirects=False`, max
     `_MAX_REDIRECTS=20`, else `RuntimeError("too many redirects")`).
     `netcheck.assert_allowed_url` vets **every hop** (start URL and each
     `Location`) *before* it is requested, so a blocked hop is never contacted.
     `Cookie`/`Authorization` are dropped via `_should_drop_auth` whenever the
     redirect leaves the exact hostname, except between `drive.google.com` and
     `drive.usercontent.google.com` (`_DRIVE_HOSTS`). No suffix matching.
   - `accept_ranges = (status == 206)`; size from `Content-Range: .../<total>`,
     falling back to `Content-Length`.
   - `validator = ETag or Last-Modified or ""`.
   - `resolved_url` is the final hop URL; `ProbeResult.headers` is the caller's
     forwarded headers *as they stand at that host* (Cookie/Authorization dropped
     only if the chain left the exact host / Drive pair). `download()` hands **these**, not
     the original `headers`, to the segment/single transfer paths.
   - Filename via `derive_filename` (Content-Disposition → URL path → `download.bin`),
     sanitized: basename only, NTFS `:` ADS suffix dropped, control chars and
     trailing dots/spaces stripped, Windows reserved names (`CON`, `NUL`, `COM1`…)
     → `download.bin`. RFC 5987 `filename*=` intentionally **not** handled.
3. Output-path containment: `final` and its `.part` must resolve *inside* `dest`,
   else `ValueError("output path escapes dest_dir")` (symlink-escape guard).
4. `on_probed(filename)` callback fires (lets the service record the filename
   before the download can be paused).
5. If `not accept_ranges or size <= 0` → `_download_single` (one stream, no resume).
   Otherwise → `_download_segmented`. A mid-flight `RangeNotSupported` deletes
   `.part`/sidecar and falls back to `_download_single` once.

**`_download_segmented`:**
- `_check_free_space` (needs `size - existing_part + FREE_SLACK(1 MiB)`; raises `InsufficientSpace`).
- `split_ranges(size, n)` → contiguous inclusive `(start,end)` ranges; last gets the remainder.
- Loads sidecar progress via `_load_progress`; trusts it only if the `.part` exists
  at **exactly full size**; otherwise zeroes progress and preallocates `.part` via `truncate(size)`.
- Spawns a `ThreadPoolExecutor` (one worker per unfinished segment). The main loop
  does `wait(timeout=1s, FIRST_EXCEPTION)` each tick: saves sidecar, calls
  `progress_cb(done, total, bps)` and `segment_cb([(done,total), ...])`, re-raises
  the first worker exception.
- After workers finish, **integrity gate**: `sum(progress) == size` and every
  segment exactly full, else `IntegrityError` (keeps `.part` + sidecar).
- Success: save sidecar → `os.replace(part, final)` → delete sidecar.

**`_download_segment`** (per thread; sole writer of `progress[idx]` and its file
region): up to `SEGMENT_RETRIES=5` attempts with exponential backoff + jitter
(`min(2**attempt, 8) + random()`), resuming from `start + progress[idx]`. Requests
use `allow_redirects=False` on the already-resolved URL and re-run
`netcheck.assert_allowed_url` every attempt.
- `200` response → `RangeNotSupported`.
- `3xx` response → `RuntimeError` (a redirect body is not segment bytes; not retried).
- Any `Content-Encoding` header → `RuntimeError` (decoded bytes would desync offsets).
- Extra bytes beyond the segment's range are clamped (server ignoring `Range` end).
- Clean short close (no exception) → internal `_ShortRead`, retried like a drop.
- 4xx other than 408/429 is **fatal** (not retried).

**`_download_single`:** always restarts from zero (`"wb"`), saves sidecar with
validator `""` (so it can never be trusted for resume), rejects redirect bodies
and `Content-Encoding`, and raises `IntegrityError` if a known size isn't reached.

**Resume sidecar** (`<name>.tdm.json`, next to `<name>.part`):
```json
{"url": "<credential-stripped url>", "size": 123, "etag": "\"abc\"", "progress": [bytes_done_seg0, ...]}
```
Trusted only if: url (credential-stripped) matches, size matches, **non-empty**
validator matches, `progress` is a list of 1..1024 non-negative ints each ≤ its
segment's length, and count equals `split_ranges` count. Written atomically
(temp + `os.replace`, **no fsync** — survives Ctrl-C/process kill, not power loss).

**Other helpers:** `rebind_progress(final, new_url, new_validator)` rewrites an
existing sidecar's url/etag keeping progress (used for presigned-URL rotation);
`extension_hint(filename)` returns a "no file extension" tip for `download.bin`/suffix-less
names; `_emit` computes speed as the slope between consecutive callbacks.

**Exceptions:** `RangeNotSupported`, `IntegrityError`, `Cancelled`,
`InsufficientSpace`, internal `_ShortRead`.

**Per-download session:** `download()` makes a fresh `requests.Session`
(`_new_session()`, Chrome-like User-Agent/Accept headers - helps vs. naive UA
filtering only; does not defeat TLS fingerprinting) and passes it to the probe and
every segment, so cookies never carry over to another download. A 401/403 from a
Drive host (`_raise_for_status`) keeps its status and adds a "run `tdm catch`,
click Download again" hint.

### 4.2 `tdm/service.py` — queue, HTTP API, catch mode, process management

**`Download` dataclass:** `id, url, dest_dir, segments, state, filename, total,
done, bps, error, needs_headers` + RAM-only fields `cancel_event, mode
("pause"|"cancel"|None), demoted, seg_progress, headers`. `headers` (forwarded
cookies etc.) is **never persisted**; only the boolean `needs_headers` ("this
download was queued with forwarded auth headers") is.

**`Manager`** (one `threading.Condition` guards everything):
- `add(url, dest_dir=None, segments=8, headers=None)` → runs `netcheck`, creates a
  `queued` entry (id = `secrets.token_hex(8)`), persists, notifies scheduler.
- `_scheduler` thread: while running, if `running < max_concurrent(3)` and a
  `queued` entry exists → mark `running`, submit `_run` to the pool; else
  `cond.wait(0.5)`. The entry chosen is the **first `queued` one in dict insertion
  order**, i.e. order of first being *added* — not of being queued. A demoted or
  resumed older download keeps its slot and runs before newer queued items.
- `_run(dl)`: calls `engine.download` with `segs = min(host_caps.get(host, dl.segments), 32)`.
  Outcomes:
  - `engine.Cancelled` → `_on_cancelled`: `mode=="cancel"` → delete `.part`+sidecar
    (re-probes the URL to learn the filename — best-effort) → `cancelled`;
    otherwise → `paused` with files kept.
  - `InsufficientSpace` / `ValueError` (covers `BlockedURLError`, path-escape) →
    `error` immediately, no retry.
  - any other exception → **host demotion**: first failure for a host sets
    `_host_caps[host]=2` and re-queues once (`dl.demoted=True`); a second failure → `error`.
  - success → `done`, `filename` set.
- `pause/resume/cancel(id)` return `bool` (False = transition refused);
  `find_resumable(url)` matches paused/errored downloads by `(hostname, port,
  path)` (query string ignored — presigned-URL identity);
  `rebind(id, new_url, resolved_url, validator, headers=None)` rewrites the
  sidecar, **replaces** the download's forwarded headers with `headers` (the fresh
  link's auth state; `None` = header-less) and re-queues.
- `shutdown()`: sets `pause` on all running, waits for the pool, joins scheduler, persists.
- Persistence: `queue.json` via `paths.write_atomic`; fields in `PERSIST_FIELDS =
  (id,url,dest_dir,segments,state,filename,error,needs_headers)`; URLs run
  through `strip_credentials`; keeps all non-terminal + last 50 terminal entries.
- `_load()`: corrupt/missing file → empty; `running`/`paused` entries → `queued`
  (so they resume via the engine's own sidecar on next start). **Exception:** an
  entry with `needs_headers` true that was running/queued/paused loads as
  `paused` — its cookies died with the old process, so it must not auto-run
  header-less; it stays matchable by `find_resumable`, and re-clicking the link
  in Chrome (catch mode) offers a resume that supplies fresh headers.
  Legacy `queue.json` files without the key behave as before.

**State machine:**
```
            add                 scheduler               engine ok
 (new) ───────────► queued ───────────────► running ───────────────► done
                     ▲  │                    │  │
          resume     │  │ pause              │  │ cancel event(mode=cancel) ─► cancelled
  paused / error ────┘  ▼                    │  └ cancel event(mode=pause)  ─► paused
                      paused ◄───────────────┘
                     (queued --cancel--> cancelled)
 running --exception--> queued (once, host capped at 2 segs) --exception--> error
 process restart: running|paused -> queued
```

**HTTP layer** (`_Server` = `ThreadingHTTPServer`, `_Handler`): bound to
`127.0.0.1` only; ephemeral port (service) or fixed `CATCH_PORT=8765` (catch).
Max request body 64 KiB. Auth is `Authorization: Bearer <token>` compared with
`hmac.compare_digest`. `/ext/*` POST routes are an exception: allowed with a
matching `Origin` header (+ `X-Ext-Token` for `/ext/flag`), and only when the
server was started with `allowed_origin` (catch mode).

| Method & path | Auth | Request | Response |
|---|---|---|---|
| `GET /health` | Bearer | – | `200 {ok, version, downloads}` |
| `GET /downloads` | Bearer | – | `200 [snapshot,...]` |
| `GET /downloads/<id>` | Bearer | – | `200 snapshot` / `404` |
| `POST /downloads` | Bearer | `{url, segments?, dest_dir?}` | `201 {id}` / `400 blocked\|missing url\|bad request` |
| `POST /downloads/<id>/{pause\|resume\|cancel}` | Bearer | – | `202 {ok}` / `404` (unknown id) / `409 {error:"invalid state"}` (transition refused, e.g. cancelling a `done` item) |
| `POST /shutdown` | Bearer | – | `202`, then graceful stop in a thread |
| `POST /ext/health` | Origin match | – | `200 {ok, version, token: <ext_token>}` |
| `POST /ext/flag` | Origin + `X-Ext-Token` | `{url, headers?}` | `202` / `400 missing url\|bad headers\|blocked` / `404` / `503 busy` |

Snapshot shape: `{id, url (creds stripped), filename, dest_dir, state, done, total, bps, error}`.

**Process management:**
- `run_service()` — blocking foreground service; writes `service.port`,
  `service.token`, `service.pid`; handles SIGTERM/SIGBREAK; clears those files on exit.
- `spawn_detached()` — idempotent: if `/health` is OK returns "already running";
  else launches `python -m tdm _serve` detached (`DETACHED_PROCESS|CREATE_NO_WINDOW`
  on Windows, `start_new_session` elsewhere), logs to `service.log`, polls up to 5 s
  for the port file.
- `stop_service()` — `POST /shutdown`, waits ≤10 s, then `taskkill /F` / `SIGTERM` by pid.
- `read_endpoint()` → `(base_url, token)` from the state dir, or `None`.

**Catch mode** (`run_catch(origin)`): refuses to start if a healthy service is
already running; clears stale state files; starts a `_Server` on `CATCH_PORT` with
`allowed_origin`, persistent `ext_token`, and a bounded `pending` queue (100);
then runs `_confirm_loop` on the main thread:
- pulls flagged `(url, headers)` items; calls `_try_resume` (if a paused/errored
  download matches host+port+path **and** a live probe's size/accept-ranges match
  → offer resume via `rebind`; declined → skip link; else plain `[y/n]` → `manager.add`);
- every 0.5 s redraws per-segment bars with ANSI cursor-up/clear (`_render_active`
  → `_frame_rows` data → `_format_frame` pure text). Terminal results
  (combining / Saved to / error / cancelled) are printed permanently, never erased.
- Windows: `SO_EXCLUSIVEADDRUSE` on the fixed port so another process can't squat
  it; `os.system("")` enables VT/ANSI in `cmd.exe`.
- `resolve_origin(extension_id)` persists the id to `<state>/extension_id`;
  `resolve_ext_token()` persists a random token to `<state>/ext_token`.

### 4.3 `tdm/__main__.py` — CLI

Rendering/dispatch only; no engine logic and no second speed calculation.
`main(argv)`: if `argv[0]` ∈ `{serve,_serve,stop,add,status,cancel,pause,resume,catch}`
→ service subcommands; else treat as a one-shot URL download.

| Command | Behavior |
|---|---|
| `python -m tdm <url> [-n N] [-o dir]` | foreground `engine.download` with a `\r` progress bar; prints `Saved to <path>` (+ extension hint) |
| `serve` / `stop` | `spawn_detached()` / `stop_service()` |
| `add <url> [-n] [-o]` | `POST /downloads`, prints id |
| `status [id] [--json]` | list or single |
| `pause|resume|cancel <id>` | `POST /downloads/<id>/<action>`; prints `ok` (202), `not found` (404) or `not allowed in the download's current state` (409); exit 1 on the latter two |
| `catch [--extension-id ID]` | `run_catch(resolve_origin(id))` |
| `_serve` | internal: what the detached child runs |

Exit codes: one-shot `0` ok / `1` Ctrl-C / `2` other error. Service subcommands:
`SystemExit` (e.g. "service not running") → `1`, other exception → `2`. Errors are
passed through `redact()`.

### 4.4 Small modules

- **`client.py`** — `endpoint()` (raises `SystemExit("tdm service not running - run: tdm serve")`),
  `request(method, path, body)` → `(status, json|None)`, 10 s timeout.
- **`netcheck.py`** — `assert_allowed_url(url)`: only `http`/`https`; rejects IP
  literals or *any* resolved address that is loopback, private, link-local,
  reserved, multicast, or unspecified. Resolution failure ⇒ blocked.
  Raises `BlockedURLError(ValueError)`.
- **`paths.py`** — `state_dir()` (`%LOCALAPPDATA%\tdm` on Windows, else
  `$XDG_STATE_HOME/tdm` or `~/.local/state/tdm`; created on call),
  `write_atomic()` (temp + `os.replace` with ≤5 retries on Windows
  `PermissionError`, best-effort `chmod 600`), `read_or_none()`,
  `default_download_dir()` = `~/Downloads/tdm` (not created here).
- **`redact.py`** — `redact(text)` masks `scheme://user:pass@` → `user:***@` anywhere
  in text; `strip_credentials(url)` removes userinfo entirely (for anything persisted).

### 4.5 `extension/` — Chrome MV3 "TDM Catch"

`background.js` listens on `chrome.downloads.onCreated`. For `http(s)` URLs:
(1) `POST /ext/health` (400 ms timeout) to confirm TDM catch is running and fetch
`ext_token`; (2) gather `Cookie` (via `chrome.cookies.getAll({url: item.url})` only, so Chrome's own
scope rules apply and duplicate names are kept), `Referer`,
`User-Agent`; (3) `POST /ext/flag`; (4) only if `202`, cancel and erase Chrome's own download.
**Any failure → return silently** so Chrome's normal download proceeds (fail-open).
Hard-coded `BASE = http://127.0.0.1:8765`. Manifest `host_permissions` allows
`http://*/*` and `https://*/*` for reading site cookies. The extension id Chrome assigns
must be registered with `python -m tdm catch --extension-id <id>`.

---

## 5. Workflows (end to end)

### 5.1 One-shot CLI download
`python -m tdm URL` → `engine.download` → probe → (segmented | single) → integrity
gate → promote `.part` → delete sidecar → print path. Re-running after Ctrl-C resumes
from the sidecar.

### 5.2 Background-service download
`tdm serve` (detached) → `tdm add URL` → `POST /downloads` → `Manager.add` (SSRF
check, persist `queued`) → scheduler picks it up (≤3 concurrent) → `_run` →
`engine.download` with callbacks updating `done/total/bps/seg_progress` →
`done` → `tdm status`. `tdm stop` pauses running items and persists; next `serve`
re-queues them and the engine resumes from sidecars.

### 5.3 Browser catch mode
User runs `tdm catch` (foreground) → clicks a download in Chrome → extension flags
URL + cookies to `127.0.0.1:8765/ext/flag` → URL lands in `pending` queue →
terminal prints URL and asks `[y/n]` (or offers to resume a matching paused
download) → `Manager.add(url, headers=...)` → downloads with forwarded headers →
live per-segment bars → "Saved to … / Ready for a new link."

### 5.4 Resume across a rotated presigned URL
Same host+port+path, different query/signature → `find_resumable` match →
live probe must report same size and range support → user confirms →
`rebind_progress` rewrites sidecar url/etag, progress untouched → download requeued
and only missing bytes are fetched.

### 5.5 Pause / cancel
Running: set `dl.mode` and `cancel_event`; engine raises `Cancelled` at the next
chunk/tick; `_on_cancelled` keeps files (pause) or deletes them (cancel).
Queued: state flips directly with no worker involved.

---

## 6. Data & files on disk

| Where | File | Contents |
|---|---|---|
| Next to output | `<name>.part` | preallocated bytes, written at true offsets |
| Next to output | `<name>.tdm.json` | resume sidecar (see 4.1) |
| State dir | `queue.json` | `{"downloads":[{id,url,dest_dir,segments,state,filename,error,needs_headers}]}` |
| State dir | `service.port` / `service.token` / `service.pid` | live endpoint info (deleted on clean stop) |
| State dir | `service.log` | detached service stdout/stderr |
| State dir | `extension_id` / `ext_token` | catch-mode config, persisted across runs |

---

## 7. Security model (as implemented)

- **Loopback only**, bearer token (`secrets.token_urlsafe(32)`) on every
  non-extension request. State-dir files written via `paths.write_atomic` get a
  best-effort `chmod 600` (the `.part` / `.tdm.json` files next to the output do
  not; and on Windows `chmod` only toggles the read-only bit, so real protection
  there is the user-profile ACL on `%LOCALAPPDATA%`). Any local process that can
  read the state dir can read the token — documented as an accepted trust boundary.
- **SSRF guard** (`netcheck`) applied: on `Manager.add`, `/ext/flag`, **every
  hop** of the probe's redirect chain (before it is requested), and before every
  segment/single request.
  *Remaining caveat:* DNS is resolved by the guard separately from the actual
  connect (TOCTOU / DNS-rebinding window).
- **Output containment:** sanitized filename + resolved-path-inside-`dest_dir` check.
  (`dest_dir` itself is caller-controlled — any authenticated client may choose it.)
- **Credential hygiene:** `user:pass@` stripped from sidecar and `queue.json`,
  masked in error text; forwarded headers (cookies) live in RAM only and are
  excluded from snapshots, `queue.json`, and error messages.
- **Extension channel:** Origin match + per-install `X-Ext-Token`; the token is
  handed out by `/ext/health` to any caller presenting the right `Origin`
  (spoofable by non-browser local processes — same local trust boundary).
- **Integrity:** never promotes a file unless every byte is accounted for;
  refuses `Content-Encoding` responses.

---

## 8. Concurrency model

- Engine: per-download `ThreadPoolExecutor`; each segment thread is the sole
  writer of its `progress[idx]` slot and its file region (ownership, no locks).
  The coordinating thread is the only one that saves the sidecar.
- Service: one `threading.Condition` guards `_downloads`/`_host_caps`;
  scheduler thread + `ThreadPoolExecutor(3)` for runs + one handler thread per
  HTTP request (`daemon_threads`). Progress callbacks mutate `Download` fields
  without the lock (each is a plain int/float assigned whole, so readers see an
  old or new value; `done/total/bps` are three separate stores, so a snapshot can
  mix them). Judged benign from reading the code — not stress-tested.
- Cooperative cancellation only: a `threading.Event` checked per chunk and per tick.

---

## 9. Tunables / constants

| Constant | Value | Where |
|---|---|---|
| `CHUNK` | 65536 | engine |
| `SEGMENT_RETRIES` | 5 | engine |
| `SAVE_INTERVAL` | 1.0 s | engine |
| `MAX_SEGMENTS` | 32 | engine |
| `FREE_SLACK` | 1 MiB | engine |
| `_MAX_SIDECAR_SEGMENTS` | 1024 | engine |
| default segments | 8 | CLI / service |
| `max_concurrent` | 3 | `Manager` |
| `_KEEP_TERMINAL` | 50 | service |
| `_MAX_BODY` | 64 KiB | service |
| `CATCH_PORT` | 8765 | service (also hard-coded in the extension) |
| `_PENDING_MAX` | 100 | service |
| host-demotion segments | 2 | service |

---

## 10. Testing

Run: `pip install pytest ruff && python -m pytest -q && ruff check .` (no external network).

`tests/conftest.py::make_server(data, **toggles)` starts a real
`ThreadingHTTPServer` on `127.0.0.1:0` with fault injection: `support_range`,
`no_etag`, `drop_after`, `ignore_range_end`, `segment_status` (one-shot status
code), `silent_drop_after` (clean short close, no Content-Length),
`require_header=(name,value)`; tracks `served_bytes` to prove resume fetched only
the remainder.

**Important convention:** an autouse fixture monkeypatches
`netcheck.assert_allowed_url` to a no-op (the fixture server is loopback).
Production code therefore **must call `netcheck.assert_allowed_url(...)` via the
module attribute** (not `from netcheck import assert_allowed_url`) or the patch
won't apply; tests that exercise the real guard re-install the real function.

Other test notes: `test_output_path_escape_rejected` is skipped without symlink
privilege (Windows without Developer Mode); `test_run_catch_ext_flag_flow_and_shutdown`
binds the fixed port 8765, so don't run it while a real `tdm catch` is open.
Manager tests drive pause/cancel deterministically by wrapping `Manager._cb`.

---

## 11. Conventions & gotchas for contributors

- **`# ponytail:` comments** mark deliberate simplifications with a known ceiling
  and upgrade path (e.g. static equal segmentation, single manager lock, no
  connection-pool tuning). Grep for them to find intentional tech debt.
- **Minimal-diff, YAGNI style** (see `limitations.md`). Don't add features beyond
  the stated scope; CLI is "rendering only".
- `__main__._redact` is an alias kept only for an existing test import.
- Python ≥ 3.11 syntax/stdlib (`X | None`, `zip(strict=True)`, `itertools.pairwise`).
- `service._cleanup_files` and `_try_resume` reach into engine privates
  (`engine._probe`, `_part_path`, `_meta_path`).
- Cross-platform, Windows-first: `DETACHED_PROCESS`, `taskkill`,
  `SO_EXCLUSIVEADDRUSE`, `os.replace` retry, `LOCALAPPDATA`.

### Behaviors worth knowing

1. **Forwarded headers never survive a restart (by design).** Cookies are RAM-only.
   A download queued with headers is persisted with `needs_headers: true` and
   reloads as `paused`; re-click the link in Chrome under `tdm catch` and accept
   the resume prompt to continue it with fresh cookies. A manual `tdm resume <id>`
   on such an item runs header-less and will likely fail (403).
2. **Host demotion state is in-memory and per-process** and applies to *all*
   later downloads from that host until the service restarts.
3. **`POST /downloads` ignores any `headers` field**; forwarded headers are only
   reachable through catch mode.
4. **Cookie scope is exact-host plus the Drive pair.** Only a redirect that stays on
   the same hostname, or moves between `drive.google.com` and
   `drive.usercontent.google.com`, keeps forwarded `Cookie`/`Authorization` headers;
   any other host (including sibling subdomains) drops them to prevent credential
   leakage.
5. `limitations.md` was corrected: "no cookie jar / session downloads" applies to
   `add`/one-shot use; catch mode forwards the tab's cookies for that download.
6. `README.md` links to `docs/milestone-2-security.md`, which is gitignored and
   not present in a fresh clone.

---

## 12. Invariants — do not break these

Each is enforced by code and covered by at least one test; a change that
weakens one needs an explicit decision, not a drive-by.

**Data integrity**
1. A file is promoted (`os.replace(.part -> final)`) **only** after every byte is
   accounted for: segmented = `sum(progress)==size` and every segment full;
   single = `done==size` when size is known. Otherwise `IntegrityError`, `.part` kept.
2. The sidecar is trusted only if url (credential-stripped) **and** size **and a
   non-empty** validator match, `progress` is well-formed, **and** `.part` exists
   at exactly `size`. `_download_single` writes validator `""` so its sidecar can
   never be trusted.
3. Each segment thread is the sole writer of `progress[idx]` and its disjoint file
   region; only the coordinating thread saves the sidecar. No locks in the engine.
4. Segment writes are clamped to the segment's own length (a server ignoring the
   `Range` end must not overrun the next segment).
5. `Content-Encoding` responses are refused and requests send `Accept-Encoding:
   identity` — decoded bytes would desync offsets.
6. Cancellation is cooperative (`threading.Event`); the engine raises `Cancelled`
   and leaves `.part` + sidecar; the *caller* decides to keep or delete them.

**Security / secrets**
7. Every outbound request is preceded by `netcheck.assert_allowed_url(...)`,
   called **through the module attribute** (tests monkeypatch it). This includes
   each redirect hop in `_probe`.
8. The output path must resolve inside `dest_dir`; filenames are reduced to a
   sanitized basename (`derive_filename`).
9. Forwarded headers (cookies) live in `Download.headers` (RAM) only: never in
   `queue.json`, snapshots, sidecars, or error strings. Only the boolean
   `needs_headers` is persisted. `Cookie`/`Authorization` are never sent to an
   host other than the original or the Drive pair (segments use
   `ProbeResult.headers`).
10. Anything persisted goes through `strip_credentials`; anything user-facing
    goes through `redact`.
11. The service binds `127.0.0.1` only (no `--host`); every non-`/ext/*` request
    needs the bearer token (`hmac.compare_digest`).
12. State files are written with `paths.write_atomic` (temp + `os.replace`).

**Behavioral contracts**
13. `Manager` state mutations happen under `self._cond`; engine callbacks do not
    take the lock.
14. The snapshot key set `{id,url,filename,dest_dir,state,done,total,bps,error}` is
    a public contract (a test asserts it exactly). Persisted fields live in
    `PERSIST_FIELDS` and legacy `queue.json` files (missing keys) must still load.
15. `InsufficientSpace` and `ValueError` (incl. `BlockedURLError`, path-escape) are
    **not** retried by the service; any other exception gets one host demotion.
16. `__main__.py` is rendering/dispatch only — no engine logic, no second speed
    calculation. Exit codes: one-shot `0/1/2`; service subcommands `1` for
    `SystemExit`/refused action, `2` for other exceptions.
17. HTTP status semantics: `202` accepted, `404` unknown id, `409` refused
    transition, `400` bad input/blocked, `401` unauthorized, `503` flag queue full.

---

## 13. How to change things (recipes)

**Always:** write/adjust a test first, then `python -m pytest -q` and
`ruff check .`. Don't run the suite while a real `tdm catch` is open (the
catch test binds port 8765).

**Add a CLI subcommand that talks to the service**
1. `tdm/__main__.py`: add the name to `_SERVICE_SUBCOMMANDS`; add a branch in
   `_run_service_subcommand` (the trailing `else` already handles
   `cancel`/`pause`/`resume`); write `_cmd_<name>(rest) -> int` using a lazy
   `from tdm import client` and `client.request(method, path, body)`.
2. Test in `tests/test_cli.py` by monkeypatching `client.request` (see
   `test_cancel_wiring`).

**Add an authenticated HTTP route**
1. `tdm/service.py::_Handler`: add a branch in `do_GET`/`do_POST` — place it
   *before* the generic `path.startswith("/downloads/")` branch. For a new
   per-download action, extend the `("pause","resume","cancel")` tuple in
   `_handle_action` and add a `Manager` method that returns `bool`.
2. Add `(method, path)` to `ALL_ROUTES` in `tests/test_service.py` so the
   401-without-token test covers it.
3. Update the API table in section 4.2.

**Add a browser-extension (`/ext/*`) route**
Add to `_EXT_ROUTES`, handle in `do_POST`, and decide whether it also needs the
`X-Ext-Token` check in `_allowed` (the `/ext/flag` special case). The extension's
`host_permissions` already cover `127.0.0.1:8765`.

**Add a persisted per-download field**
1. Add a defaulted field to `Download`; add its name to `PERSIST_FIELDS`.
2. Read it in `Manager._load` with `entry.get("<name>", <default>)` (old files).
3. Do **not** add it to `_to_snapshot` unless you also update the exact-key
   assertion in `test_post_downloads_creates_and_lists`.
4. Never persist secrets — persist a flag, like `needs_headers`.

**Add an engine option (like `headers`)**
Add a keyword parameter with a `None` default to `download()`, thread it through
`_download_segmented` / `_download_single` / `_download_segment` (and `_probe`
if it affects the probe), pass it from `Manager._run`, and keep the default such
that existing callers/tests are unaffected.

**Change what catch mode prints**
`_frame_rows` (data, mutates `watched`) → `_format_frame` (pure text) →
`_render_active` (I/O). Test `_format_frame`/`_frame_rows` directly with
`_FakeManager`; keep completion lines in the *permanent* (never erased) path.

**Add a failure mode to the test server**
Add a constructor arg + attribute to `_Server` **and** a keyword to
`make_server._make` in `tests/conftest.py`; prefer one-shot toggles via a
lock-guarded `take_*` method like `take_segment_status`. Remember the autouse
fixture no-ops `netcheck`; to test the real guard, monkeypatch
`netcheck.assert_allowed_url` back to the real function inside the test.

**Record a deliberate shortcut**
Leave a `# ponytail: <ceiling>; <upgrade path>` comment at the spot (grep-able).

---

## 14. Quick start

```bash
python -m venv .venv && .venv\Scripts\activate     # source .venv/bin/activate elsewhere
pip install -r requirements.txt                    # just `requests`
python -m tdm https://example.com/big.iso -n 8     # one-shot
python -m tdm serve && python -m tdm add <url>     # background service
python -m tdm catch --extension-id <chrome id>     # browser catch mode (load extension/ unpacked first)
python -m pytest -q                                # tests
```

**Where to start reading the code:** `tdm/engine.py::download` →
`_probe` → `_download_segmented` → `_download_segment`; then
`tdm/service.py::Manager._run` and `_Handler`; then `tests/conftest.py`.
