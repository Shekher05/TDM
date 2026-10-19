# Known limitations — what TDM cannot download

TDM is a segmented HTTP(S) downloader: it needs one URL that resolves
directly to file bytes (optionally with `Range` support for parallel
segments). Everything it can't handle traces back to that one boundary —
no JavaScript execution, no cookie jar/login flow, no non-HTTP protocol,
no streaming-media extractor. Concretely, it cannot download from:

- **Google Drive share links (large files, outside catch mode)** — Drive serves an HTML "can't
  scan this file for viruses" interstitial for anything past a size
  threshold, which requires extracting a `confirm=` token from a cookie or
  the page body before the real file URL is reachable. When using the CLI
  directly, TDM has no HTML parsing or cookie jar. However, in Catch mode,
  clicking "Download anyway" in Chrome passes the direct download link and
  session cookies through to TDM seamlessly.
- **MediaFire and most consumer file lockers** — the real "Download now"
  link is generated client-side by JavaScript after the page loads (often
  behind a countdown timer); the initial HTML response TDM would fetch
  never contains a direct file URL. No JS engine = no real link to hand
  the download engine.
- **Link-protector / gateway pages** (e.g. `filecrypt.cc`-style link
  shorteners with ads/countdowns/CAPTCHAs in front of the real link) —
  same root cause as MediaFire: it's an HTML page to click through, not a
  fetchable resource.
- **Sites behind bot-detection or CAPTCHA challenges** (Cloudflare "checking
  your browser", hCaptcha/reCAPTCHA) — TDM makes a plain HTTP request with
  no JS execution or CAPTCHA solving, so it receives the challenge page
  (or a block response) instead of the file.
- **Authenticated/session-gated downloads (outside catch mode)** — no cookie
  jar, no login flow, no OAuth. `python -m tdm <url>` and `tdm add <url>` send
  no credentials, so a URL that only works inside an active browser session
  (private Drive/Dropbox files, paywalled downloads) fails there. Catch mode
  (`tdm catch` + the Chrome extension) is the exception: it forwards that
  tab's `Cookie`, `Referer` and `User-Agent` for that one download. Those
  headers are kept in memory only, so after a service restart the download
  is left `paused` and you re-click the link in Chrome to resume it with
  fresh cookies. TDM still has no login flow of its own.
- **Streaming/adaptive media** (YouTube, Twitch VODs, HLS `.m3u8` / DASH
  `.mpd` manifests) — the engine expects one URL with a
  `Content-Length`/`Range`-capable response; these are a manifest plus many
  small segments needing a dedicated extractor (yt-dlp-style logic), which
  is a different problem than segmented HTTP download and out of scope.
- **Non-HTTP(S) protocols** — FTP/SFTP, BitTorrent/magnet links, `s3://`,
  etc. `_probe`/`derive_filename` assume `http`/`https` throughout.
- **Expired presigned URLs** — no re-signing/refresh logic; a stale
  presigned link (S3, CDN token URLs) just fails and has to be re-added
  with a freshly generated link.
- **Plain landing pages mistaken for files** — confirmed in manual testing:
  `fortnite.com/download` correctly returned `403`, because it's a
  JS-driven installer picker, not a direct binary. TDM has no HTML
  scraping to go find "the real link" on a page like that — by design,
  since doing so reliably is effectively re-implementing a browser.

**Framing:** these aren't bugs — TDM deliberately stayed inside "segmented
HTTP(S) downloader with a local service + browser hook," which is the
actual project scope (see `PROJECT.md`/`CONSTRAINTS.md`). Supporting any of
the above would mean embedding a headless browser (for JS-rendered links
and CAPTCHA-adjacent challenges) or a dedicated media extractor (for
streaming formats) — each a materially different, much larger project than
a download *manager*, and explicitly out of scope per this repo's YAGNI
stance.

## Other documented limitations

- **Symlink privilege gap on Windows** — `test_output_path_escape_rejected`
  is skipped on machines without Developer Mode or admin privileges,
  because `os.symlink()` requires elevated permissions there. Not a code
  defect; the path-traversal guard itself is still covered by CI on
  platforms where symlinks are unprivileged.
- **TOCTOU window in output-path containment check** — a known, low-
  exploitability race between checking and using the resolved output path.
- **No "resubmit incomplete segment" logic** — if a segment ends up short
  (not full), there is currently no automatic "not full -> resubmit" retry;
  add this only if real-world data shows it's needed.
