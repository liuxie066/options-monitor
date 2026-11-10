"""Loopback HTTP transport for the fixed group-query routes.

Order of checks: bearer token, immutable principal, route/method, capability
and market, strict body contract, admission and deadline, then business.  No
unauthenticated request reaches quote or evidence I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import threading
from typing import Any, Callable

from src.application.secret_store.runtime import resolve_secret

from .budget import AdmissionRejected, BudgetExhausted, RunBudget, SlidingWindowLimiter
from .business import BusinessService, Dependencies
from .config import APISettings
from .contracts import (
    PATH_TO_METHOD,
    ContractError,
    VerifiedPrincipal,
    error_envelope,
    normalize_request,
)

MAX_BODY_BYTES = 64 * 1024
MAX_CONCURRENT_REQUESTS = 2


class AuthenticationError(RuntimeError):
    """The presented bearer token did not match the server credential."""


def resolve_api_token(settings: APISettings, *, environ: Any = None,
                      provider: Any = None) -> str:
    token = resolve_secret(settings.token_ref, environ=environ, provider=provider)
    if not token:
        raise RuntimeError("group-query API credential is unavailable")
    return token


@dataclass
class APIService:
    settings: APISettings
    token: str
    store: Any
    dependencies: Dependencies | None = None
    now: Callable[[], datetime] | None = None
    _slots: threading.BoundedSemaphore = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self._slots is None:
            self._slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
        self.business = BusinessService(
            settings=self.settings, store=self.store,
            dependencies=self.dependencies, now=self.now,
        )
        self.principal = VerifiedPrincipal(
            principal_id=self.settings.principal_id,
            instance_id=self.settings.instance_id,
            app_id=self.settings.app_id,
            capabilities=frozenset(self.settings.capabilities),
            markets=frozenset(self.settings.markets),
        )
        self._principal_limiter = SlidingWindowLimiter(
            self.settings.principal_requests_per_minute)

    def _now(self) -> datetime:
        return (self.now or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)

    def authenticate(self, authorization: str | None) -> None:
        if not authorization or not authorization.startswith("Bearer "):
            raise AuthenticationError("missing bearer credential")
        presented = authorization.removeprefix("Bearer ")
        if not hmac.compare_digest(presented, self.token):
            raise AuthenticationError("invalid bearer credential")

    def handle(self, method: str, body: Any) -> tuple[dict[str, Any], int]:
        from .contracts import ROUTES
        if ROUTES[method][1] not in self.principal.capabilities:
            return (error_envelope(_request_id(body), "PERMISSION_DENIED",
                                   "capability is not granted"), 403)
        current = self._now()
        request = None
        try:
            request = normalize_request(method, body, now=current)
        except ContractError as exc:
            return self.business.execute_error(_request_id(body), exc)
        try:
            self._principal_limiter.acquire(self.principal.principal_id)
        except AdmissionRejected as exc:
            return (error_envelope(request.request_id, "RATE_LIMITED",
                                   "principal request rate exceeded",
                                   retry_after_seconds=exc.retry_after_seconds), 429)
        if not self._slots.acquire(blocking=False):
            return (error_envelope(request.request_id, "RATE_LIMITED",
                                   "too many concurrent group-query requests",
                                   retry_after_seconds=1), 429)
        try:
            budget = RunBudget.from_request(
                deadline_utc=request.deadline_utc, now_utc=current,
                server_max_seconds=self.settings.max_budget_seconds,
            )
            data = self.business.execute(request, self.principal, budget)
            return data, 200
        except (BudgetExhausted, ContractError) as exc:
            return self.business.execute_error(request.request_id, exc)
        except Exception as exc:  # noqa: BLE001 - controlled envelope below
            return self.business.execute_error(request.request_id, exc)
        finally:
            self._slots.release()


def _request_id(body: Any) -> str | None:
    if isinstance(body, dict):
        value = body.get("request_id")
        if isinstance(value, str) and value.strip() == value and value:
            return value
    return None


class _Handler(BaseHTTPRequestHandler):
    server_version = "om-group-query-bot/1"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    @property
    def service(self) -> APIService:
        return self.server.service  # type: ignore[attr-defined]

    def do_POST(self) -> None:  # noqa: N802
        method = PATH_TO_METHOD.get(self.path)
        if method is None:
            self._send({"schema_version": "group_query_response.v1", "request_id": None,
                        "ok": False, "data": None,
                        "error": {"code": "NOT_FOUND", "message": "unknown group-query route"}}, 404)
            return
        try:
            self.service.authenticate(self.headers.get("Authorization"))
        except AuthenticationError as exc:
            self._send(error_envelope(None, "UNAUTHORIZED", str(exc)), 401)
            return
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else -1
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._send(error_envelope(None, "INPUT_ERROR", "request body size is invalid"), 400)
            return
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(error_envelope(None, "INPUT_ERROR", "request body is not valid JSON"), 400)
            return
        payload, status = self.service.handle(method, body)
        self._send(payload, status)

    def do_GET(self) -> None:  # noqa: N802
        self._send(error_envelope(None, "METHOD_NOT_ALLOWED",
                                  "only POST is supported"), 405)

    do_PUT = do_GET  # noqa: N815
    do_DELETE = do_GET  # noqa: N815
    do_PATCH = do_GET  # noqa: N815

    def _send(self, payload: dict[str, Any], status: int) -> None:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], service: APIService):
        self.service = service
        super().__init__(address, _Handler)


def create_server(settings: APISettings, *, token: str | None = None,
                  store: Any, dependencies: Dependencies | None = None,
                  now: Callable[[], datetime] | None = None,
                  port: int | None = None) -> _Server:
    """Bind the explicit loopback port and return a not-yet-serving server."""
    credential = token or resolve_api_token(settings)
    service = APIService(settings=settings, token=credential, store=store,
                         dependencies=dependencies, now=now)
    return _Server((settings.bind_host, port if port is not None else settings.bind_port),
                   service)


def serve(settings: APISettings, *, token: str | None = None, store: Any,
          dependencies: Dependencies | None = None) -> None:
    server = create_server(settings, token=token, store=store,
                           dependencies=dependencies)
    try:
        server.serve_forever()
    finally:
        server.server_close()


__all__ = [
    "APIService",
    "AuthenticationError",
    "create_server",
    "resolve_api_token",
    "serve",
]
