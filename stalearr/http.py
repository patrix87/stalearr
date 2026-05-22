import json
import logging
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger("stalearr")

TIMEOUT_SEC = 30


class ArrClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url
        self.api_key = api_key

    def _request(self, method: str, path: str, body: dict | None = None) -> Any:
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
        try:
            with urlopen(req, timeout=TIMEOUT_SEC) as response:
                raw = response.read()
                if not raw:
                    return None
                return json.loads(raw)
        except HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {url} -> HTTP {e.code}: {detail}") from e
        except URLError as e:
            raise RuntimeError(f"{method} {url} failed: {e.reason}") from e

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def put(self, path: str, body: dict) -> Any:
        return self._request("PUT", path, body)
