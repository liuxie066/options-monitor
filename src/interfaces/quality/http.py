from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from src.application.quality.model import SCHEMA_VERSION
from src.application.quality.service import OMQualityService


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _read_token() -> str:
    return str(os.environ.get("OM_QUALITY_READ_TOKEN") or "").strip()


def build_quality_handler(service: OMQualityService) -> type[BaseHTTPRequestHandler]:
    class QualityHandler(BaseHTTPRequestHandler):
        server_version = "OMQualityHTTP/1"

        def log_message(self, fmt: str, *args: Any) -> None:
            # Never log Authorization headers or response bodies.
            super().log_message(fmt, *args)

        def do_GET(self) -> None:  # noqa: N802
            request_id = f"req-{uuid.uuid4().hex}"
            if self.path == "/health":
                self._send_json(
                    200,
                    {"status": "ok", "service": "options-monitor"},
                    request_id=request_id,
                )
                return
            if self.path != "/quality/status":
                self._error(404, "QUALITY_NOT_FOUND", "quality endpoint not found", request_id)
                return
            token = _read_token()
            supplied = self.headers.get("Authorization", "")
            expected = f"Bearer {token}" if token else ""
            if not expected or not hmac.compare_digest(supplied, expected):
                self._error(
                    401,
                    "QUALITY_AUTH_FAILED",
                    "quality endpoint authentication failed",
                    request_id,
                )
                return
            payload = service.read_published()
            if payload is None:
                self._error(
                    503,
                    "QUALITY_STATUS_UNAVAILABLE",
                    "quality status is unavailable",
                    request_id,
                )
                return
            body = _json_bytes(payload)
            etag = f'"sha256:{hashlib.sha256(body).hexdigest()}"'
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self._common_headers(request_id)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            self.send_response(200)
            self._common_headers(request_id)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _common_headers(self, request_id: str) -> None:
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Request-Id", request_id)
            self.send_header("X-Quality-Schema-Version", SCHEMA_VERSION)

        def _send_json(
            self,
            status: int,
            payload: dict[str, Any],
            *,
            request_id: str,
        ) -> None:
            body = _json_bytes(payload)
            self.send_response(status)
            self._common_headers(request_id)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: int, code: str, message: str, request_id: str) -> None:
            self._send_json(
                status,
                {
                    "error": {
                        "code": code,
                        "message": message,
                        "request_id": request_id,
                    }
                },
                request_id=request_id,
            )

    return QualityHandler


def serve_quality_http(
    *,
    service: OMQualityService,
    host: str = "127.0.0.1",
    port: int = 8792,
) -> None:
    allow_remote = str(os.environ.get("OM_QUALITY_ALLOW_REMOTE_BIND") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not _is_loopback(host) and not allow_remote:
        raise ValueError("quality endpoint refuses non-loopback bind without OM_QUALITY_ALLOW_REMOTE_BIND")
    if not _read_token():
        raise ValueError("OM_QUALITY_READ_TOKEN is required before serving /quality/status")
    server = ThreadingHTTPServer((host, int(port)), build_quality_handler(service))
    server.serve_forever()


__all__ = ["build_quality_handler", "serve_quality_http"]
