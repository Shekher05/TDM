"""SSRF guard: reject URLs that would make the engine fetch a private/local
address (loopback, RFC1918, link-local, cloud metadata, etc.) on the caller's
behalf.
"""

import ipaddress
import socket
from urllib.parse import urlsplit


class BlockedURLError(ValueError):
    pass


def _is_blocked_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def assert_allowed_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedURLError(f"refusing non-http(s) scheme: {parts.scheme!r}")

    host = parts.hostname
    if not host:
        raise BlockedURLError("no host in URL")

    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if _is_blocked_ip(host):
            raise BlockedURLError(f"blocked IP literal: {host}")
        return

    # getaddrinfo has no timeout param; a stalled resolver is bounded by the OS.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise BlockedURLError(f"could not resolve host: {host}") from exc

    for _family, _type, _proto, _canonname, sockaddr in infos:
        ip = sockaddr[0]
        if _is_blocked_ip(ip):
            raise BlockedURLError(f"host resolves to a blocked address: {ip}")
