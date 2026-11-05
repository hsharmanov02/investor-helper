"""Small HTTP helper. GET only: this app never sends orders or writes to any API."""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_session = requests.Session()
_last_call: dict[str, float] = {}


def get(url: str, *, headers: dict[str, str] | None = None, params: dict[str, Any] | None = None,
        timeout: float = 20, min_interval: float = 0.0, auth: Any = None,
        retries: int = 2) -> requests.Response:
    """HTTP GET with polite per-host rate limiting and simple retries.

    There is deliberately no post/put/delete helper in this module.
    """
    host = url.split("/")[2]
    wait = _last_call.get(host, 0) + min_interval - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    h = {"User-Agent": BROWSER_UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = _session.get(url, headers=h, params=params, timeout=timeout, auth=auth)
            _last_call[host] = time.monotonic()
            if resp.status_code == 429 and attempt < retries:
                time.sleep(2 ** (attempt + 1))
                continue
            return resp
        except requests.RequestException as exc:  # network blip
            last_exc = exc
            time.sleep(1 + attempt)
    assert last_exc is not None
    raise last_exc
