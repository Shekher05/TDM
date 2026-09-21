import sys

import pytest

import tdm.paths as paths_module
from tdm.paths import default_download_dir, read_or_none, state_dir, write_atomic


def test_state_dir_uses_localappdata_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    d = state_dir()
    assert d == tmp_path / "tdm"
    assert d.is_dir()


def test_state_dir_uses_xdg_state_home_elsewhere(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    d = state_dir()
    assert d == tmp_path / "tdm"
    assert d.is_dir()


def test_write_atomic_round_trips_and_leaves_no_tmp(tmp_path):
    p = tmp_path / "file.txt"
    write_atomic(p, "hello")
    assert read_or_none(p) == "hello"
    assert not p.with_name(p.name + ".tmp").exists()
    write_atomic(p, "world")  # overwrite
    assert read_or_none(p) == "world"


def test_read_or_none_missing_file(tmp_path):
    assert read_or_none(tmp_path / "nope.txt") is None


def test_write_atomic_retries_transient_permission_error(tmp_path, monkeypatch):
    """Windows can raise a transient PermissionError from os.replace() when
    another thread/process briefly has the destination open for reading -
    write_atomic must retry rather than crash the caller (a real crash this
    surfaced in: a Manager scheduler thread writing queue.json while a
    reader had it open)."""
    p = tmp_path / "file.txt"
    calls = {"n": 0}
    real_replace = paths_module.os.replace

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("Access is denied")
        return real_replace(src, dst)

    monkeypatch.setattr(paths_module.os, "replace", flaky_replace)
    write_atomic(p, "hello")
    assert read_or_none(p) == "hello"
    assert calls["n"] == 3


def test_write_atomic_gives_up_after_persistent_permission_error(tmp_path, monkeypatch):
    def always_fails(src, dst):
        raise PermissionError("Access is denied")

    monkeypatch.setattr(paths_module.os, "replace", always_fails)
    with pytest.raises(PermissionError):
        write_atomic(tmp_path / "file.txt", "hello")


def test_default_download_dir_not_created_on_import(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    d = default_download_dir()
    assert d == tmp_path / "Downloads" / "tdm"
    assert not d.exists()
