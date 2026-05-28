from unittest.mock import patch
from urllib.error import HTTPError, URLError

import pytest

from optimizarr import http
from optimizarr.http import ArrClient


class _FakeResponse:
    def __init__(self, body: bytes = b"[]"):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    # Zero-out retry sleeps so tests don't actually wait.
    monkeypatch.setattr(http, "CONNECT_RETRY_DELAYS_SEC", (0, 0, 0, 0, 0))


def test_retries_then_succeeds_on_connection_refused():
    attempts = []

    def fake_urlopen(req, timeout):
        attempts.append(1)
        if len(attempts) < 3:
            raise URLError(ConnectionRefusedError(111, "Connection refused"))
        return _FakeResponse(b'{"ok": true}')

    with patch.object(http, "urlopen", fake_urlopen):
        result = ArrClient("http://x", "k").get("/foo")

    assert result == {"ok": True}
    assert len(attempts) == 3


def test_gives_up_after_max_attempts():
    def fake_urlopen(req, timeout):
        raise URLError(ConnectionRefusedError(111, "Connection refused"))

    with (
        patch.object(http, "urlopen", fake_urlopen),
        pytest.raises(RuntimeError, match="Connection refused"),
    ):
        ArrClient("http://x", "k").get("/foo")


def test_http_errors_do_not_retry():
    attempts = []

    def fake_urlopen(req, timeout):
        attempts.append(1)
        raise HTTPError("http://x/foo", 401, "Unauthorized", {}, None)  # type: ignore[arg-type]

    with (
        patch.object(http, "urlopen", fake_urlopen),
        pytest.raises(RuntimeError, match="HTTP 401"),
    ):
        ArrClient("http://x", "k").get("/foo")

    assert len(attempts) == 1
