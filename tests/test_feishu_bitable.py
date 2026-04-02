"""Unit tests for scripts/feishu_bitable.py.

Focus:
- retry behavior for transient/rate limit
- token cache refresh behavior
- error classification from HTTPError with JSON body

We avoid importing the whole app; only the module under test.
"""

from __future__ import annotations

import json
from unittest.mock import patch


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


def test_http_json_retries_on_429_then_succeeds() -> None:
    from scripts import feishu_bitable as fb

    ok_body = json.dumps({"code": 0, "msg": "ok", "data": {"x": 1}}).encode("utf-8")

    class FakeResp:
        def __init__(self, body: bytes):
            self._body = body
            self.status = 200

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    err_body = json.dumps({"code": 99991400, "msg": "rate limit"})
    fake_429 = _make_http_error(429, err_body)

    calls = {"n": 0}

    def side_effect(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise fake_429
        return FakeResp(ok_body)

    with patch("urllib.request.urlopen", side_effect=side_effect), patch("time.sleep") as sleep_mock:
        res = fb.http_json("GET", "https://example.com", retry_max_attempts=3)
        assert res["code"] == 0
        assert calls["n"] == 2
        sleep_mock.assert_called_once()


def test_http_json_does_not_retry_on_permission() -> None:
    from scripts import feishu_bitable as fb

    err_body = json.dumps({"code": 99991401, "msg": "no permission"})
    fake_403 = _make_http_error(403, err_body)

    with patch("urllib.request.urlopen", side_effect=fake_403), patch("time.sleep") as sleep_mock:
        try:
            fb.http_json("GET", "https://example.com", retry_max_attempts=3)
            assert False, "should raise"
        except fb.FeishuPermissionError:
            pass

        sleep_mock.assert_not_called()


def test_get_tenant_access_token_cache_and_force_refresh() -> None:
    from scripts import feishu_bitable as fb

    # reset cache
    fb._token_cache["token"] = None
    fb._token_cache["expire_at"] = None

    body1 = json.dumps({"code": 0, "tenant_access_token": "t1", "expire": 7200}).encode("utf-8")
    body2 = json.dumps({"code": 0, "tenant_access_token": "t2", "expire": 7200}).encode("utf-8")

    class FakeResp:
        def __init__(self, body: bytes):
            self._body = body
            self.status = 200

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    calls = {"n": 0}

    def side_effect(*args, **kwargs):
        calls["n"] += 1
        return FakeResp(body1 if calls["n"] == 1 else body2)

    with patch("urllib.request.urlopen", side_effect=side_effect):
        t = fb.get_tenant_access_token("a", "s")
        assert t == "t1"
        # cached
        t_again = fb.get_tenant_access_token("a", "s")
        assert t_again == "t1"
        assert calls["n"] == 1
        # force refresh
        t2 = fb.get_tenant_access_token("a", "s", force_refresh=True)
        assert t2 == "t2"
        assert calls["n"] == 2
