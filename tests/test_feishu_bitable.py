"""Unit tests for scripts/feishu_bitable.py.

Focus:
- retry behavior for transient/rate limit
- token cache refresh behavior
- error classification from HTTPError with JSON body

We avoid importing the whole app; only the module under test.
"""

from __future__ import annotations

import json
import threading
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
    fb._token_cache.clear()

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


def test_get_tenant_access_token_isolated_by_app_credentials() -> None:
    from scripts import feishu_bitable as fb

    fb._token_cache.clear()

    class FakeResp:
        def __init__(self, token: str):
            self._body = json.dumps({"code": 0, "tenant_access_token": token, "expire": 7200}).encode("utf-8")
            self.status = 200

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def side_effect(req, **_kwargs):
        body = json.loads(req.data.decode("utf-8"))
        app_id = body["app_id"]
        return FakeResp(f"token-{app_id}")

    with patch("urllib.request.urlopen", side_effect=side_effect) as urlopen_mock:
        assert fb.get_tenant_access_token("app_a", "secret_a") == "token-app_a"
        assert fb.get_tenant_access_token("app_b", "secret_b") == "token-app_b"
        assert fb.get_tenant_access_token("app_a", "secret_a") == "token-app_a"

    assert urlopen_mock.call_count == 2


def test_get_tenant_access_token_reuses_cache_under_concurrency() -> None:
    from scripts import feishu_bitable as fb

    fb._token_cache.clear()

    class FakeResp:
        def __init__(self):
            self._body = json.dumps({"code": 0, "tenant_access_token": "token-app_a", "expire": 7200}).encode("utf-8")
            self.status = 200

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    results: list[str] = []

    def _worker() -> None:
        results.append(fb.get_tenant_access_token("app_a", "secret_a"))

    with patch("urllib.request.urlopen", return_value=FakeResp()) as urlopen_mock:
        threads = [threading.Thread(target=_worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert results == ["token-app_a"] * 4
    assert urlopen_mock.call_count == 1


def test_with_tenant_token_retry_refreshes_once_on_auth_error() -> None:
    from scripts import feishu_bitable as fb

    calls: list[tuple[str, bool]] = []

    def fake_get_tenant_access_token(app_id: str, app_secret: str, *, force_refresh: bool = False) -> str:
        calls.append((f"{app_id}:{app_secret}", force_refresh))
        return "fresh-token" if force_refresh else "stale-token"

    attempts = {"n": 0}

    def fn(token: str) -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise fb.FeishuAuthError("expired")
        return token

    with patch.object(fb, "get_tenant_access_token", side_effect=fake_get_tenant_access_token):
        out = fb.with_tenant_token_retry("app", "secret", fn)

    assert out == "fresh-token"
    assert calls == [("app:secret", False), ("app:secret", True)]
    assert attempts["n"] == 2


def test_with_tenant_token_retry_does_not_refresh_non_auth_errors() -> None:
    from scripts import feishu_bitable as fb

    attempts = {"n": 0}

    def fn(_token: str) -> str:
        attempts["n"] += 1
        raise fb.FeishuPermissionError("denied")

    with patch.object(fb, "get_tenant_access_token", return_value="token") as token_mock:
        try:
            fb.with_tenant_token_retry("app", "secret", fn)
            assert False, "should raise"
        except fb.FeishuPermissionError:
            pass

    token_mock.assert_called_once_with("app", "secret")
    assert attempts["n"] == 1


def test_http_json_logs_warn_retries_when_rate_limited() -> None:
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
    logs = []

    def logger(entry):
        logs.append(entry)

    def side_effect(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise fake_429
        return FakeResp(ok_body)

    with patch("urllib.request.urlopen", side_effect=side_effect), patch("time.sleep"):
        res = fb.http_json("GET", "https://example.com/path?a=b", retry_max_attempts=3, log_fn=logger)
        assert res["code"] == 0

    assert calls["n"] == 2
    assert any(item.get("level") == "warn" for item in logs)
    warn = next(item for item in logs if item.get("level") == "warn")
    assert warn["category"] == "rate_limit"
    assert warn["http_status"] == 429
    assert warn["feishu_code"] == 99991400
    assert warn["url_path"] == "example.com/path"


def test_http_json_logs_error_when_all_retries_fail() -> None:
    from scripts import feishu_bitable as fb

    err_body = json.dumps({"code": 0, "msg": "server error"})
    fake_500 = _make_http_error(500, err_body)

    calls = {"n": 0}
    logs = []

    def logger(entry):
        logs.append(entry)

    def side_effect(*args, **kwargs):
        calls["n"] += 1
        raise fake_500

    with patch("urllib.request.urlopen", side_effect=side_effect), patch("time.sleep"):
        try:
            fb.http_json("GET", "https://example.com/other", retry_max_attempts=2, log_fn=logger)
            assert False, "should raise"
        except fb.FeishuTransientError:
            pass

    assert calls["n"] == 2
    assert any(item.get("level") == "error" for item in logs)
    err = next(item for item in logs if item.get("level") == "error")
    assert err["attempt"] == 2
    assert err["max_attempts"] == 2
    assert err["category"] == "transient"
    assert err["url_path"] == "example.com/other"
