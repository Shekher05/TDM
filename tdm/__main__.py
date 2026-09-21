"""``python -m tdm <url> [-n SEGMENTS] [-o OUTPUT_DIR]`` - the CLI front end.

Rendering only. The engine reports progress through
``progress_cb(done, total, bytes_per_sec)`` and this module formats it; there is
no second speed calculation here, and no engine logic (see ``CONSTRAINTS.md``).
"""
from __future__ import annotations

import argparse
import json
import sys

from tdm.engine import download, extension_hint
from tdm.redact import redact

_redact = redact  # kept for tests/test_cli.py's existing import name


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _progress(done: int, total: int, bps: float) -> None:
    if total:
        frac = max(0.0, min(1.0, done / total))
        eta = (total - done) / bps if bps > 0 else 0.0
        bar = "#" * int(frac * 30)
        line = f"\r[{bar:<30}] {frac * 100:5.1f}%  {_human(bps)}/s  ETA {eta:4.0f}s"
    else:
        line = f"\r{_human(done)}  {_human(bps)}/s"
    sys.stderr.write(line)
    sys.stderr.flush()


_SERVICE_SUBCOMMANDS = {
    "serve", "_serve", "stop", "add", "status", "cancel", "pause", "resume", "catch",
}


def _cmd_add(rest: list[str]) -> int:
    from tdm import client

    p = argparse.ArgumentParser(prog="tdm add")
    p.add_argument("url")
    p.add_argument("-n", "--segments", type=int, default=8)
    p.add_argument("-o", "--output-dir", default=None)
    args = p.parse_args(rest)

    body = {"url": args.url, "segments": args.segments}
    if args.output_dir is not None:
        body["dest_dir"] = args.output_dir
    status, data = client.request("POST", "/downloads", body)
    if status == 201:
        print(data["id"])
        return 0
    sys.stderr.write(f"error: {data}\n")
    return 1


def _cmd_status(rest: list[str]) -> int:
    from tdm import client

    p = argparse.ArgumentParser(prog="tdm status")
    p.add_argument("id", nargs="?")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(rest)

    path = f"/downloads/{args.id}" if args.id else "/downloads"
    status, data = client.request("GET", path)
    if status == 404:
        sys.stderr.write("not found\n")
        return 1
    if args.json:
        print(json.dumps(data))
        return 0
    for row in data if isinstance(data, list) else [data]:
        pct = f"{row['done'] / row['total'] * 100:3.0f}%" if row.get("total") else "  - "
        print(f"{row['id']}  {row['state']:<10}{pct}  {row.get('filename') or ''}")
    return 0


def _cmd_action(cmd: str, rest: list[str]) -> int:
    from tdm import client

    p = argparse.ArgumentParser(prog=f"tdm {cmd}")
    p.add_argument("id")
    args = p.parse_args(rest)

    status, _data = client.request("POST", f"/downloads/{args.id}/{cmd}")
    if status == 202:
        print("ok")
        return 0
    print("not found")
    return 1


def _cmd_catch(rest: list[str]) -> int:
    from tdm import service

    p = argparse.ArgumentParser(prog="tdm catch")
    p.add_argument("--extension-id", default=None)
    args = p.parse_args(rest)

    service.run_catch(service.resolve_origin(args.extension_id))
    return 0


def _run_service_subcommand(cmd: str, rest: list[str]) -> int:
    from tdm import service

    try:
        if cmd == "serve":
            print(service.spawn_detached())
        elif cmd == "_serve":
            service.run_service()
        elif cmd == "stop":
            print(service.stop_service())
        elif cmd == "add":
            return _cmd_add(rest)
        elif cmd == "status":
            return _cmd_status(rest)
        elif cmd == "catch":
            return _cmd_catch(rest)
        else:  # cancel / pause / resume
            return _cmd_action(cmd, rest)
    except SystemExit as e:
        sys.stderr.write(f"{e}\n")
        return 1
    except Exception as e:  # noqa: BLE001 - deliberate top-level CLI guard
        sys.stderr.write(f"error: {_redact(str(e))}\n")
        return 2
    return 0


def _one_shot(argv: list[str] | None) -> int:
    p = argparse.ArgumentParser(
        prog="tdm", description="Segmented HTTP downloader with resume."
    )
    p.add_argument("url")
    p.add_argument(
        "-n", "--segments", type=int, default=8,
        help="parallel connections (default: 8)",
    )
    p.add_argument(
        "-o", "--output-dir", default=".",
        help="destination directory (default: current)",
    )
    args = p.parse_args(argv)

    try:
        path = download(args.url, args.output_dir, args.segments, _progress)
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted - re-run the same command to resume\n")
        return 1
    except Exception as e:  # noqa: BLE001 - deliberate top-level CLI guard
        sys.stderr.write(f"\nerror: {_redact(str(e))}\n")
        return 2
    sys.stderr.write("\n")
    print(f"Saved to {path}")
    hint = extension_hint(path.name)
    if hint is not None:
        print(hint)
    return 0


def main(argv: list[str] | None = None) -> int:
    args_list = sys.argv[1:] if argv is None else argv
    if args_list and args_list[0] in _SERVICE_SUBCOMMANDS:
        return _run_service_subcommand(args_list[0], args_list[1:])
    return _one_shot(argv)


if __name__ == "__main__":
    sys.exit(main())
