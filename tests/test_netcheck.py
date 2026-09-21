import socket

import pytest

from tdm.netcheck import BlockedURLError, assert_allowed_url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://10.0.0.5/x",
        "http://192.168.1.1/x",
        "http://[::1]/x",
        "http://169.254.169.254/latest/meta-data",
        "ftp://h/x",
        "http://0.0.0.0/x",
    ],
)
def test_blocks_disallowed_urls(url):
    with pytest.raises(BlockedURLError):
        assert_allowed_url(url)


def test_allows_public_hostname(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port: [(socket.AF_INET, None, None, None, ("93.184.216.34", 0))],
    )
    assert_allowed_url("http://example.com/x")


def test_resolution_failure_is_blocked(monkeypatch):
    def _raise(host, port):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", _raise)
    with pytest.raises(BlockedURLError):
        assert_allowed_url("http://nope.invalid/x")


def test_hostname_resolving_to_private_ip_is_blocked(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port: [(socket.AF_INET, None, None, None, ("10.1.2.3", 0))],
    )
    with pytest.raises(BlockedURLError):
        assert_allowed_url("http://internal.example/x")
