"""Regression: legacy http_json behavior still available via feishu_bitable.http_json.

Now http_json is centralized and raises typed exceptions instead of returning error dict.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def _make_http_error(status: int, body: str | bytes | None):
    import urllib.error

    class FakeHTTPError(urllib.error.HTTPError):
        def __init__(self):
            self.code = status
            self.msg = ""
            self.hdrs = {}
            self.filename = None
            self._body = body

        def read(self):
            return self._body if isinstance(self._body, (bytes, type(None))) else self._body.encode("utf-8")

    return FakeHTTPError()


def test_http_json_404_non_json_body_raises_permanent_error() -> None:
    from scripts import feishu_bitable as fb

    fake_error = _make_http_error(404, "Not Found")

    with patch("urllib.request.urlopen", side_effect=fake_error):
        try:
            fb.http_json("GET", "https://example.com/notfound", retry_max_attempts=1)
            assert False, "should raise"
        except fb.FeishuPermanentError as e:
            assert "http=404" in str(e) or "404" in str(e)


def test_http_json_500_json_body_raises_transient_error() -> None:
    from scripts import feishu_bitable as fb

    payload = {"code": 123, "message": "internal error", "detail": "db down"}
    fake_error = _make_http_error(500, json.dumps(payload))

    with patch("urllib.request.urlopen", side_effect=fake_error):
        try:
            fb.http_json("POST", "https://example.com/fail", retry_max_attempts=1)
            assert False, "should raise"
        except fb.FeishuTransientError as e:
            assert "http=500" in str(e) or "500" in str(e)


def test_http_json_urlerror_raises_transient_error() -> None:
    from scripts import feishu_bitable as fb
    import urllib.error

    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("network unreachable")):
        try:
            fb.http_json("GET", "https://example.com/unreachable", retry_max_attempts=1)
            assert False, "should raise"
        except fb.FeishuTransientError as e:
            assert "network" in str(e).lower() or "URLError" in str(e)


def test_http_json_socket_timeout_raises_transient_error() -> None:
    from scripts import feishu_bitable as fb
    import socket

    with patch("urllib.request.urlopen", side_effect=socket.timeout("read timed out")):
        try:
            fb.http_json("GET", "https://example.com/timeout", retry_max_attempts=1)
            assert False, "should raise"
        except fb.FeishuTransientError as e:
            assert "timed out" in str(e)


if __name__ == "__main__":
    test_http_json_404_non_json_body_returns_error_dict()
    test_http_json_500_json_body_merges_fields()
    test_http_json_urlerror_returns_structured_error()
    test_http_json_socket_timeout_returns_structured_error()
    print("OK (4 tests)")
