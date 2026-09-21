"""Thin HTTP client the CLI sub-commands use to talk to the local service."""

import requests

from tdm import service


def endpoint() -> tuple[str, str]:
    ep = service.read_endpoint()
    if ep is None:
        raise SystemExit("tdm service not running - run: tdm serve")
    return ep


def request(method: str, path: str, body: dict | None = None) -> tuple[int, object]:
    base_url, token = endpoint()
    r = requests.request(
        method,
        base_url + path,
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    return r.status_code, (r.json() if r.content else None)
