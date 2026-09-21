"""State-directory resolution and small atomic-file helpers shared by the
service and client."""

import os
import sys
import time
from pathlib import Path


def state_dir() -> Path:
    """Where the service keeps its queue, port, token, and pid files."""
    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA")
        d = Path(local) / "tdm" if local else Path.home() / ".tdm"
    else:
        base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
        d = Path(base) / "tdm"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_atomic(path: Path, text: str) -> None:
    """Temp file + ``os.replace``, then best-effort ``chmod 0o600``.

    On Windows, ``os.replace`` can transiently raise ``PermissionError``
    ("Access is denied") if another thread/process has ``path`` open for
    reading at that exact instant - a real race for a file like `queue.json`
    that a scheduler thread writes while a client reads it concurrently.
    A short bounded retry absorbs that without changing the atomicity
    guarantee (the retry is on the rename itself, not a partial write).
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            break
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.01 * (attempt + 1))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def read_or_none(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def default_download_dir() -> Path:
    """Not created here - the caller (the download itself) creates it on use."""
    return Path.home() / "Downloads" / "tdm"
