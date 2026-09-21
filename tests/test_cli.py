import os

from tdm import __main__ as cli
from tdm import client
from tdm.__main__ import _redact, main


def test_cli_downloads_file(make_server, tmp_path, capsys):
    blob = os.urandom(1024 * 1024)
    server = make_server(blob)
    rc = main([server.url, "-o", str(tmp_path), "-n", "4"])
    assert rc == 0
    assert "Saved to" in capsys.readouterr().out
    assert (tmp_path / "file.bin").read_bytes() == blob


def test_cli_shows_extension_hint_for_a_nameless_download(make_server, tmp_path, capsys):
    server = make_server(os.urandom(1024))
    root_url = f"http://127.0.0.1:{server.server_address[1]}/"  # no path segment -> no filename
    rc = main([root_url, "-o", str(tmp_path), "-n", "1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Saved to" in out
    assert "no file extension" in out


def test_cli_bad_url_returns_2(tmp_path, capsys):
    rc = main(["http://127.0.0.1:1/nope", "-o", str(tmp_path)])
    assert rc == 2


def test_cli_redacts_credentials_in_errors(monkeypatch, capsys):
    def boom(url, *a, **k):
        raise RuntimeError(f"connection failed for {url}")

    monkeypatch.setattr(cli, "download", boom)
    rc = main(["http://alice:hunter2@example.invalid/f.bin"])
    assert rc == 2
    captured = capsys.readouterr()
    blob = captured.out + captured.err
    assert "hunter2" not in blob
    assert "example.invalid" in captured.err  # host still shown, only the secret is hidden


def test_redact_leaves_plain_urls_alone():
    assert _redact("failed for http://example.com/f") == "failed for http://example.com/f"


def test_redact_catches_credentials_glued_to_punctuation():
    assert "pw" not in _redact("failed:http://user:pw@h/x")
    assert "pw" not in _redact("(http://user:pw@h/x)")


# --- service sub-commands (Milestone 2 Task 10) -----------------------------


def test_add_without_service_exits_cleanly(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("tdm.service.read_endpoint", lambda: None)
    rc = main(["add", "http://x/"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "tdm serve" in err
    assert "Traceback" not in err


def test_catch_without_extension_id_exits_cleanly(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("tdm.paths.state_dir", lambda: tmp_path)
    rc = main(["catch"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "--extension-id" in err
    assert "Traceback" not in err


def test_status_renders(monkeypatch, capsys):
    canned = [
        {"id": "abc", "state": "running", "done": 50, "total": 100, "filename": "f.bin"},
        {"id": "def", "state": "queued", "done": 0, "total": 0, "filename": None},
    ]
    monkeypatch.setattr(client, "request", lambda method, path, body=None: (200, canned))
    rc = main(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "abc" in out and "running" in out
    assert "def" in out and "queued" in out


def test_cancel_wiring(monkeypatch, capsys):
    calls = []

    def fake_request(method, path, body=None):
        calls.append((method, path))
        return 202, {}

    monkeypatch.setattr(client, "request", fake_request)
    rc = main(["cancel", "abc"])
    assert rc == 0
    assert calls == [("POST", "/downloads/abc/cancel")]
    assert "ok" in capsys.readouterr().out
