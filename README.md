# TDM

A tiny personal download accelerator, built to learn. For a real tool use
[Motrix](https://motrix.app), [aria2](https://aria2.github.io/), or a browser
extension — this one exists to understand how segmented downloading and resume
work, not to compete with them.

## What it does

Downloads one file over several parallel HTTP connections, each fetching a
different byte range, then writes them into one preallocated `.part` file at the
right offsets. Per-segment progress is flushed to a `<name>.tdm.json` sidecar
about once a second, so an interrupted download **resumes on re-run** instead of
starting over. If the server does not support range requests (or does not report
a size), it falls back to a single streamed connection.

The assembled file is promoted to its final name only after an integrity check
(`sum(bytes per segment) == size`, every segment full). A server that lies about
its size fails loudly and leaves the `.part` for a retry — it never produces a
silently corrupt file.

## Install

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows;  source .venv/bin/activate elsewhere
pip install -r requirements.txt
```

Python 3.11+ is required.

## Usage

```bash
python -m tdm https://example.com/big.iso              # 8 segments (default)
python -m tdm https://example.com/big.iso -n 16        # 16 parallel connections
python -m tdm https://example.com/big.iso -o downloads # write into ./downloads
```

A `\r`-updated line shows a bar, percent, speed, and ETA (speed comes straight
from the engine). On success it prints `Saved to <path>`.

Exit codes: `0` success, `1` you pressed Ctrl-C (re-run the same command to
resume), `2` any other error. Credentials in a `user:pass@host` URL are redacted
from error output.

## Command reference

| Command | Does |
|---|---|
| `python -m tdm <url> [-n N] [-o dir]` | one-shot download, blocks in the foreground, resumable by re-running |
| `python -m tdm serve` | start the background service (detached), idempotent |
| `python -m tdm stop` | stop the background service |
| `python -m tdm add <url> [-n N] [-o dir]` | queue a download on the running service, prints its id |
| `python -m tdm status [id] [--json]` | list all downloads, or one download's status |
| `python -m tdm pause <id>` / `resume <id>` / `cancel <id>` | control one queued/running download |
| `python -m tdm catch --extension-id <id>` | run catch mode, saving the given Chrome extension id for future runs |
| `python -m tdm catch` | run catch mode using the previously saved extension id |

`serve`/`add`/`status`/`pause`/`resume`/`cancel`/`stop` talk to the same
background service; `catch` runs its own foreground loop (see below) and can
be used independently of `serve`. See `-h` on any subcommand for its full
flag list.

## How resume works

- While downloading, each segment's byte count is written to
  `<name>.tdm.json` next to the output.
- On a re-run, that sidecar is trusted **only if** the URL, the total size, and
  a non-empty strong validator (`ETag`, else `Last-Modified`) all still match.
  If the file on the server changed, the sidecar is ignored and the download
  restarts clean — no corruption.
- Finished segments are not re-fetched; only the remaining bytes are pulled.
- On success both `<name>.part` and `<name>.tdm.json` are deleted.

**Durability scope:** resume survives Ctrl-C and a process restart. It does
**not** survive a hard power loss — the sidecar write is atomic (temp file +
`os.replace`) but not `fsync`-ed, so a crash can cost the last sub-second of
progress. That is a deliberate choice for a personal tool.

## Tests

```bash
pip install pytest ruff
python -m pytest -q          # no network — a local HTTP fixture stands in
ruff check .
```

## Running as a background service (Milestone 2)

For downloads that outlive a single `python -m tdm <url>` invocation — pause,
resume, cancel, or several queued at once — run TDM as a small local service
instead:

```bash
python -m tdm serve                              # starts a detached background process
python -m tdm add https://example.com/big.iso     # queues a download, prints its id
python -m tdm add https://example.com/big.iso -o downloads -n 16
python -m tdm status                              # table: id, state, percent, filename
python -m tdm status <id> --json                  # one download, raw JSON
python -m tdm pause <id>
python -m tdm resume <id>
python -m tdm cancel <id>
python -m tdm stop                                # graceful shutdown
```

`serve` is idempotent — running it again while a service is already up just
prints "already running on <url>" instead of starting a second one. Up to 3
downloads run concurrently; the rest queue in FIFO order. A host that starts
failing gets demoted to 2 connections and retried once before it's marked
`error`. The queue and each download's progress survive `stop` / a crash /
Ctrl-C: a `queued`/`running`/`paused` entry on disk is picked back up and
resumed (via the same segment-sidecar resume that powers the one-shot CLI) the
next time `serve` runs.

**Where state lives:** `%LOCALAPPDATA%\tdm` on Windows, else
`$XDG_STATE_HOME/tdm` (or `~/.local/state/tdm`) — `queue.json` (the
download list; URLs there always have any embedded credentials stripped),
`service.port`, `service.token`, `service.pid`, and `service.log` (the
detached process's stdout/stderr).

**Trust boundary:** the service binds `127.0.0.1` only and requires the Bearer
token from `service.token` on every request — but *any* local process, and any
page open in your browser, can reach a `127.0.0.1` port and read that token
file. Do not run this on a machine you share with people you don't trust, and
do not expose the port past loopback (no `--host`, on purpose).

**Presigned-URL limitation:** if a queued download's URL is a presigned link
that expires before its turn comes up (or before pause/resume gets back to
it), it will fail with whatever error the server returns for an expired
signature — the service does not re-sign or refresh URLs. Re-queue it with a
fresh link via `add`.

## Browser extension — catch mode (Milestone 3)

Chrome downloads normally use one connection and can't be resumed cleanly.
"Catch mode" hands them to TDM instead: start a download in Chrome as usual,
and it gets intercepted, confirmed in the terminal, and pulled with segmented
+ resumable connections like `add` would.

### One-time setup

1. Open `chrome://extensions`, turn on **Developer mode** (top right).
2. Click **Load unpacked** and select this repo's `extension/` folder.
3. Copy the **ID** Chrome shows for the loaded extension (a long lowercase
   string under its name).
4. Link that ID to TDM once:
   ```bash
   python -m tdm catch --extension-id <the id from chrome://extensions>
   ```
   This is saved to `%LOCALAPPDATA%\tdm\extension_id` (or the XDG state dir
   elsewhere), so every later run just needs:
   ```bash
   python -m tdm catch
   ```
   Re-passing `--extension-id` overwrites the saved id — use it again if you
   ever reload the extension somewhere that gives it a different id.

### Using it

Leave `python -m tdm catch` running in a terminal (it runs in the
foreground, unlike `serve` — it never detaches). Then just download
something in Chrome:

```bash
python -m tdm catch
https://example.com/big.iso
  [y/n]? y
```

- Chrome's own download is cancelled; TDM downloads the file instead, with
  live per-segment progress redrawn in the terminal.
- If the link matches a paused or errored download already in the queue
  (same host + path + size), you're prompted to **resume** it instead of
  starting over — useful when a presigned or Google Drive link expired:
  just revisit the page and download again.
- Cookies, `Referer`, and `User-Agent` from the Chrome tab are forwarded for
  that request only (kept in memory, never written to `queue.json`) — needed
  for session-gated links such as private files or Google Drive's
  `confirm=`/`uuid=`/`at=` direct-download tokens.
- Ctrl-C stops `catch` mode; `serve`/`add`/`status` still work independently
  of it.

## Known limitations

- Static equal segmentation: the slowest connection sets the finish time. A
  work-stealing / dynamic re-segmentation scheme is its own future milestone.
- The service queues downloads FIFO and demotes a failing host to 2
  connections once, but has no bandwidth throttling and no per-host priority.
- No GUI, clipboard capture, or drag-and-drop — those, plus a packaged `.exe`,
  are Milestone 4, deliberately deferred.
- See [`docs/milestone-2-security.md`](docs/milestone-2-security.md) for the
  full security write-up (SSRF/redirect validation, presigned-URL expiry,
  output-directory containment, the service's trust boundary).

## Manual smoke test (2026-09-10)

Run against `https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe`
(26,216,840 bytes, range-capable):

| Scenario | Result |
|---|---|
| 8 segments, clean | 4.3 s, sha256 `5ee42c4e…f5fdde` |
| 1 segment, clean | 3.9 s, same sha256 |
| interrupt at ~40%, re-run | resumes from sidecar, same sha256 |

On a fast CDN the 8-segment run is not faster than 1 segment — parallel segments
help on a per-connection-throttled server, not a link that is already saturated.

- Redirect chain (`github.com` → `codeload.github.com`, no range support):
  falls back to a single connection, 11 MB tarball downloaded intact.
- gzip endpoint (`Content-Encoding: gzip`): refused with
  `RuntimeError: unexpected Content-Encoding` — decoded bytes would desync
  offsets, so the engine will not save them.
