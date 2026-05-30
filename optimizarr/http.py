import json
import logging
import socket
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger("optimizarr")

TIMEOUT_SEC = 30

# Retry on transient connection errors only (the *arr container starting up,
# brief DNS hiccup, etc.). HTTP-level errors (401, 404, 500) still fail fast.
CONNECT_RETRY_DELAYS_SEC = (5, 10, 20, 40, 60)


_TRANSIENT_REASONS = (
    ConnectionRefusedError,
    TimeoutError,
    socket.gaierror,
    socket.timeout,
)


class ArrTimeout(RuntimeError):
    """The server accepted the connection but didn't respond within the timeout (e.g. a slow
    interactive indexer search). Distinct from a hard failure so callers can treat it as an
    expected, retry-later condition rather than an error."""


def _is_transient(exc: URLError) -> bool:
    return isinstance(exc.reason, _TRANSIENT_REASONS)


class ArrClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url
        self.api_key = api_key

    def _request(
        self,
        method: str,
        path: str,
        body: dict | list | None = None,
        *,
        timeout: int | None = None,
        retry: bool = True,
    ) -> Any:
        """`timeout=None` uses the default TIMEOUT_SEC. `retry=False` disables the transient
        backoff loop — use it for slow-but-responsive endpoints (e.g. manualimport, which
        runs MediaInfo and can take minutes) where retry-on-timeout just compounds latency."""
        url = f"{self.base_url}{path}"
        headers = {
            "X-Api-Key": self.api_key,
            "Accept": "application/json",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = Request(url, data=data, headers=headers, method=method)
        effective_timeout = TIMEOUT_SEC if timeout is None else timeout
        attempts = (len(CONNECT_RETRY_DELAYS_SEC) + 1) if retry else 1
        for attempt in range(1, attempts + 1):
            try:
                with urlopen(req, timeout=effective_timeout) as response:
                    raw = response.read()
                    if not raw:
                        return None
                    return json.loads(raw)
            except HTTPError as e:
                detail = e.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"{method} {url} -> HTTP {e.code}: {detail}") from e
            except TimeoutError as e:
                # Read-phase timeout (connection made, no response in time). Raised bare, not
                # wrapped in URLError, so it needs its own clause. Don't retry — retrying a
                # slow-by-nature endpoint just compounds latency.
                raise ArrTimeout(f"{method} {url} timed out after {effective_timeout}s") from e
            except URLError as e:
                if attempt < attempts and _is_transient(e):
                    delay = CONNECT_RETRY_DELAYS_SEC[attempt - 1]
                    logger.warning(
                        "[http] %s %s: %s — retrying in %ds (attempt %d/%d)",
                        method,
                        url,
                        e.reason,
                        delay,
                        attempt,
                        attempts,
                    )
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"{method} {url} failed: {e.reason}") from e

    def get(self, path: str, *, timeout: int | None = None, retry: bool = True) -> Any:
        return self._request("GET", path, timeout=timeout, retry=retry)

    def put(
        self,
        path: str,
        body: dict | list,
        *,
        timeout: int | None = None,
        retry: bool = True,
    ) -> Any:
        return self._request("PUT", path, body, timeout=timeout, retry=retry)

    def post(
        self,
        path: str,
        body: dict | list,
        *,
        timeout: int | None = None,
        retry: bool = True,
    ) -> Any:
        return self._request("POST", path, body, timeout=timeout, retry=retry)
