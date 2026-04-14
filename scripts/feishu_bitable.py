#!/usr/bin/env python3
"""Feishu Bitable helper module.

Goal:
- Deduplicate repeated Feishu API helpers across scripts.
- Provide consistent:
  - HTTP error handling + response body decoding
  - Error classification (Feishu business code + HTTP status)
  - Retry with exponential backoff (transient + rate limit only)
  - Tenant access token caching + proactive refresh

Design constraints:
- Keep consumer function signatures backward compatible.
- Keep style: pure functions + module-level cache (no classes for client objects).
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlsplit


# -----------------
# Error hierarchy
# -----------------


class FeishuError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None, response: dict | None = None):
        super().__init__(message)
        self.code = code
        self.response = response or {}


class FeishuAuthError(FeishuError):
    """token 过期 / 凭据无效"""


class FeishuRateLimitError(FeishuError):
    """限流"""


class FeishuPermissionError(FeishuError):
    """无权限"""


class FeishuTransientError(FeishuError):
    """5xx / 超时 / 网络断连"""


class FeishuPermanentError(FeishuError):
    """其他非零 code"""


_AUTH_BIZ_CODES = {99991661, 99991663, 99991668}
_RATE_LIMIT_BIZ_CODES = {99991400}
_PERMISSION_BIZ_CODES = {99991401, 99991402}


# -----------------
# Token cache
# -----------------


_token_cache: dict[str, Any] = {
    "token": None,
    "expire_at": None,  # datetime
}
_token_lock = threading.Lock()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _decode_body(raw: bytes | None) -> str:
    if not raw:
        return ""
    try:
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _try_parse_json(text: str) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _classify_error(*, http_status: int | None, body_text: str, parsed: Any, url: str) -> FeishuError:
    # Prefer Feishu business code if present
    feishu_code = None
    response_dict: dict | None = None
    if isinstance(parsed, dict):
        response_dict = parsed
        if isinstance(parsed.get("code"), int):
            feishu_code = parsed.get("code")

    # Auth errors
    if http_status == 401 or (feishu_code in _AUTH_BIZ_CODES):
        return FeishuAuthError(
            f"feishu auth error (http={http_status}, code={feishu_code})",
            code=feishu_code,
            response=response_dict or {"http_status": http_status, "body": body_text, "url": url},
        )

    # Rate limit
    if http_status == 429 or (feishu_code in _RATE_LIMIT_BIZ_CODES):
        return FeishuRateLimitError(
            f"feishu rate limited (http={http_status}, code={feishu_code})",
            code=feishu_code,
            response=response_dict or {"http_status": http_status, "body": body_text, "url": url},
        )

    # Permission
    if feishu_code in _PERMISSION_BIZ_CODES or http_status in (403,):
        return FeishuPermissionError(
            f"feishu permission error (http={http_status}, code={feishu_code})",
            code=feishu_code,
            response=response_dict or {"http_status": http_status, "body": body_text, "url": url},
        )

    # Transient: http 5xx, network
    if http_status is not None and 500 <= http_status <= 599:
        return FeishuTransientError(
            f"feishu transient http error (http={http_status}, code={feishu_code})",
            code=feishu_code,
            response=response_dict or {"http_status": http_status, "body": body_text, "url": url},
        )

    # Permanent fallback
    return FeishuPermanentError(
        f"feishu permanent error (http={http_status}, code={feishu_code})",
        code=feishu_code,
        response=response_dict or {"http_status": http_status, "body": body_text, "url": url},
    )


def _log_record(level: str, *, category: str, http_status: int | None, feishu_code: int | None, attempt: int, max_attempts: int, url_path: str, sleep_s: float | None = None) -> dict:
    rec = {
        "level": level,
        "category": category,
        "http_status": http_status,
        "feishu_code": feishu_code,
        "attempt": attempt,
        "max_attempts": max_attempts,
        "url_path": url_path,
    }
    if sleep_s is not None:
        rec["sleep_s"] = sleep_s
    return rec


def _emit_log(log_fn: Callable[[dict], Any], record: dict, *, default_label: str = "INFO") -> None:
    if log_fn is not None:
        log_fn(record)
        return
    print(f"[{default_label}] {record}")


def _log_path(url: str) -> str:
    p = urlsplit(url)
    path = p.path or "/"
    if p.netloc:
        return f"{p.netloc}{path}"
    return path


def _error_category(err: Exception) -> str:
    if isinstance(err, FeishuRateLimitError):
        return "rate_limit"
    if isinstance(err, FeishuTransientError):
        return "transient"
    if isinstance(err, FeishuAuthError):
        return "auth"
    if isinstance(err, FeishuPermissionError):
        return "permission"
    if isinstance(err, FeishuPermanentError):
        return "permanent"
    return err.__class__.__name__


def _extract_http_status(err: Exception) -> int | None:
    if isinstance(err, FeishuError):
        return err.response.get("http_status") if isinstance(err.response, dict) else None
    return None


def _extract_feishu_code(err: Exception) -> int | None:
    if isinstance(err, FeishuError):
        return err.code
    return None


def http_json(
    method: str,
    url: str,
    payload: dict | None = None,
    headers: dict | None = None,
    *,
    timeout: int = 20,
    retry_max_attempts: int = 3,
    log_fn: Callable[[dict], Any] | None = None,
) -> dict:
    """Unified HTTP JSON helper.

    Returns dict on success.

    Raises:
      FeishuError subclasses for Feishu API failures (HTTP error / business code).
    """

    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)

    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    last_err: Exception | None = None
    url_path = _log_path(url)

    for attempt in range(1, retry_max_attempts + 1):
        req = urllib.request.Request(url, data=data, method=method, headers=req_headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body_text = _decode_body(resp.read())
                parsed = _try_parse_json(body_text)
                if not isinstance(parsed, dict):
                    raise FeishuPermanentError(
                        "invalid json response",
                        code=None,
                        response={"http_status": getattr(resp, "status", None), "body": body_text, "url": url},
                    )

                # Feishu convention: code==0 means ok
                code = parsed.get("code")
                if isinstance(code, int) and code != 0:
                    raise _classify_error(http_status=getattr(resp, "status", None), body_text=body_text, parsed=parsed, url=url)

                return parsed

        except urllib.error.HTTPError as e:
            body_text = ""
            try:
                body_text = _decode_body(e.read())
            except Exception:
                body_text = ""
            parsed = _try_parse_json(body_text)
            err = _classify_error(http_status=getattr(e, "code", None), body_text=body_text, parsed=parsed, url=url)
            last_err = err

        except (urllib.error.URLError, socket.timeout) as e:
            err = FeishuTransientError(
                f"network error: {type(e).__name__}: {e}",
                code=None,
                response={"http_status": None, "body": "", "url": url, "error_type": type(e).__name__, "error": str(e)},
            )
            last_err = err

        except FeishuError as e:
            last_err = e

        # retry gate
        should_retry = isinstance(last_err, (FeishuTransientError, FeishuRateLimitError))
        if not should_retry or attempt >= retry_max_attempts:
            if last_err is not None:
                record = _log_record(
                    "error",
                    category=_error_category(last_err),
                    http_status=_extract_http_status(last_err),
                    feishu_code=_extract_feishu_code(last_err),
                    attempt=attempt,
                    max_attempts=retry_max_attempts,
                    url_path=url_path,
                )
                _emit_log(log_fn, record, default_label="ERROR")
            raise last_err

        sleep_s = 2 ** (attempt - 1)
        record = _log_record(
            "warn",
            category=_error_category(last_err),
            http_status=_extract_http_status(last_err),
            feishu_code=_extract_feishu_code(last_err),
            attempt=attempt,
            max_attempts=retry_max_attempts,
            url_path=url_path,
            sleep_s=sleep_s,
        )
        _emit_log(log_fn, record, default_label="WARN")
        time.sleep(sleep_s)

    raise last_err or FeishuPermanentError("unknown error")


def get_tenant_access_token(app_id: str, app_secret: str, *, force_refresh: bool = False) -> str:
    """Get Feishu tenant_access_token with module-level cache.

    Cache refresh rule:
      - if force_refresh=True
      - or missing token
      - or token expires in < 5 minutes
    """

    expire_at: datetime | None = _token_cache.get("expire_at")
    token: str | None = _token_cache.get("token")

    with _token_lock:
        # Re-check under lock (another thread may have refreshed)
        expire_at = _token_cache.get("expire_at")
        token = _token_cache.get("token")

        if not force_refresh and token and expire_at:
            if expire_at - _now_utc() >= timedelta(minutes=5):
                return token

        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal/"
        res = http_json("POST", url, {"app_id": app_id, "app_secret": app_secret}, timeout=20, retry_max_attempts=3)

        # res is expected: {code, msg, tenant_access_token, expire}
        if res.get("code") != 0:
            raise FeishuAuthError(f"feishu auth failed: {res}", code=res.get("code"), response=res)

        token = res["tenant_access_token"]
        expire_s = int(res.get("expire", 0) or 0)
        expire_at = _now_utc() + timedelta(seconds=expire_s)

        _token_cache["token"] = token
        _token_cache["expire_at"] = expire_at

        return token


# -----------------
# Bitable operations (backward compatible signatures)
# -----------------


def bitable_search_records(tenant_token: str, app_token: str, table_id: str, page_size: int = 500, *, max_pages: int = 50) -> list[dict]:
    base = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/search"
    headers = {
        "Authorization": f"Bearer {tenant_token}",
        "Content-Type": "application/json; charset=utf-8",
    }

    page_token = None
    out: list[dict] = []
    for _ in range(max_pages):
        url = f"{base}?page_size={page_size}" + (f"&page_token={page_token}" if page_token else "")
        try:
            res = http_json("POST", url, payload={}, headers=headers)
        except FeishuAuthError:
            raise

        if res.get("code") != 0:
            raise FeishuPermanentError(f"bitable search records failed: {res}", code=res.get("code"), response=res)

        data = res.get("data", {}) or {}
        out.extend(data.get("items", []) or [])
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
        if not page_token:
            break

    return out


def bitable_list_records(tenant_token: str, app_token: str, table_id: str, page_size: int = 500, *, max_pages: int = 50) -> list[dict]:
    base = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
    headers = {"Authorization": f"Bearer {tenant_token}"}
    out: list[dict] = []
    page_token = None
    for _ in range(max_pages):
        url = f"{base}?page_size={page_size}" + (f"&page_token={page_token}" if page_token else "")
        res = http_json("GET", url, None, headers=headers)
        if res.get("code") != 0:
            raise FeishuPermanentError(f"bitable list records failed: {res}", code=res.get("code"), response=res)

        data = res.get("data", {}) or {}
        out.extend(data.get("items", []) or [])
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
        if not page_token:
            break

    return out


def bitable_create_record(tenant_token: str, app_token: str, table_id: str, fields: dict) -> dict:
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
    headers = {"Authorization": f"Bearer {tenant_token}"}
    res = http_json("POST", url, {"fields": fields}, headers=headers)
    if res.get("code") != 0:
        raise FeishuPermanentError(f"bitable create record failed: {res}", code=res.get("code"), response=res)
    return res.get("data") or {}


def bitable_update_record(tenant_token: str, app_token: str, table_id: str, record_id: str, fields: dict) -> dict:
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}"
    headers = {"Authorization": f"Bearer {tenant_token}"}
    res = http_json("PUT", url, {"fields": fields}, headers=headers)
    if res.get("code") != 0:
        raise FeishuPermanentError(f"bitable update record failed: {res}", code=res.get("code"), response=res)
    return res.get("data") or {}


def bitable_fields(tenant_token: str, app_token: str, table_id: str) -> list[dict]:
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
    res = http_json("GET", url, None, {"Authorization": f"Bearer {tenant_token}"})
    if res.get("code") != 0:
        raise FeishuPermanentError(f"bitable fields failed: {res}", code=res.get("code"), response=res)
    return res.get("data", {}).get("items", []) or []


# -----------------
# Small utilities (dedup)
# -----------------


def safe_float(x):
    try:
        if x is None or x == "":
            return None
        return float(x)
    except Exception:
        return None


def parse_note_kv(note: str, key: str) -> str:
    if not note:
        return ""
    s = str(note)
    for part in s.replace(",", ";").split(";"):
        part = part.strip()
        if not part:
            continue
        if part.startswith(key + "="):
            return part.split("=", 1)[1].strip()
    return ""


def merge_note(note: str | None, kv: dict[str, str]) -> str:
    base = (note or "").strip()
    parts = []
    if base:
        parts.append(base)
    for k, v in kv.items():
        if v is None or v == "":
            continue
        parts.append(f"{k}={v}")
    return ";".join(parts)
