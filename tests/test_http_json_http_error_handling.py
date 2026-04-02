"""Regression: http_json must not crash on HTTP 4xx/5xx and should return structured error."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def _make_http_error(status: int, body: str | bytes | None):
    """Create a minimal HTTPError-like object for tests."""
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


def test_http_json_404_non_json_body_returns_error_dict() -> None:
    """HTTP 404 with non-JSON body should return a dict with http_error=True."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "option_positions", BASE / "scripts" / "option_positions.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    http_json = mod.http_json

    fake_error = _make_http_error(404, "Not Found")

    with patch("urllib.request.urlopen", side_effect=fake_error):
        result = http_json("GET", "https://example.com/notfound")
        assert isinstance(result, dict)
        assert result.get("http_error") is True
        assert result.get("http_status") == 404
        assert result.get("code") == 404
        assert "Not Found" in result.get("body", "")


def test_http_json_500_json_body_merges_fields() -> None:
    """HTTP 5xx with JSON body should merge http_status/code/error into parsed JSON."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "option_positions", BASE / "scripts" / "option_positions.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    http_json = mod.http_json

    payload = {"message": "internal error", "detail": "db down"}
    body_text = json.dumps(payload)
    fake_error = _make_http_error(500, body_text)

    with patch("urllib.request.urlopen", side_effect=fake_error):
        result = http_json("POST", "https://example.com/fail")
        assert isinstance(result, dict)
        assert result.get("http_error") is True
        assert result.get("http_status") == 500
        assert result.get("code") == 500
        assert result.get("message") == "internal error"
        assert result.get("detail") == "db down"
        # merged fields should include body when not already present
        assert isinstance(result.get("body"), str)


def test_http_json_urlerror_returns_structured_error() -> None:
    """URLError should return a dict with http_error=True and code=-1."""
    import importlib.util
    import urllib.error

    spec = importlib.util.spec_from_file_location(
        "option_positions", BASE / "scripts" / "option_positions.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    http_json = mod.http_json

    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("network unreachable"),
    ):
        result = http_json("GET", "https://example.com/unreachable")
        assert isinstance(result, dict)
        assert result.get("http_error") is True
        assert result.get("code") == -1
        assert result.get("error_type") == "URLError"
        assert "network unreachable" in result.get("error", "")


def test_http_json_socket_timeout_returns_structured_error() -> None:
    """socket.timeout should return a dict with http_error=True and code=-1."""
    import importlib.util
    import socket

    spec = importlib.util.spec_from_file_location(
        "option_positions", BASE / "scripts" / "option_positions.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    http_json = mod.http_json

    with patch(
        "urllib.request.urlopen", side_effect=socket.timeout("read timed out")
    ):
        result = http_json("GET", "https://example.com/timeout")
        assert isinstance(result, dict)
        assert result.get("http_error") is True
        assert result.get("code") == -1
        assert result.get("error_type") == "timeout"
        assert "timed out" in result.get("error", "")


if __name__ == "__main__":
    test_http_json_404_non_json_body_returns_error_dict()
    test_http_json_500_json_body_merges_fields()
    test_http_json_urlerror_returns_structured_error()
    test_http_json_socket_timeout_returns_structured_error()
    print("OK (4 tests)")
