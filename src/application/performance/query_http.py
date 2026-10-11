"""An operator-bound loopback adapter for the canonical query; no write endpoints."""

from __future__ import annotations

import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import socket

from src.application.account_config import accounts_from_config
from src.application.agent_tool_config import load_runtime_config, repo_base
from src.application.agent_tool_contracts import AgentToolError, build_error_payload
from src.application.agent_tools.runtime_helpers import normalize_broker, resolve_public_data_config_path
from src.application.ledger.api import open_performance_query_ledger_from_data_config
from src.application.performance.query import normalize_option_performance_request, option_performance_report_tool
from src.application.performance.service import build_option_period_performance
from src.infrastructure.option_performance_client import (
    API_VERSION,
    QUERY_PATH,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    ERROR_STATUS,
    validate_service_token,
    decode_protocol_json,
)


CONNECTION_TIMEOUT_SECONDS = 5


def create_performance_query_server(
    *, config_key: str, config_path: str | Path, token: str, data_config: str | Path | None = None, port: int = 8766
) -> HTTPServer:
    validate_service_token(token)
    if config_key not in {"us", "hk"} or type(port) is not int or not 0 <= port <= 65535:
        raise AgentToolError("INPUT_ERROR", "Choose config_key=us|hk and a valid port.")
    bound_path, cfg = load_runtime_config(config_key=config_key, config_path=config_path)
    accounts = accounts_from_config(cfg, fallback=())
    if not accounts:
        raise AgentToolError("SCOPE_ERROR", "Performance service requires a configured account scope.")
    portfolio = cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {}
    bound_data = resolve_public_data_config_path({"data_config": data_config}, portfolio)
    _resolved, reader = open_performance_query_ledger_from_data_config(base=repo_base(), data_config=bound_data)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(CONNECTION_TIMEOUT_SECONDS)

        def log_message(self, format, *args):
            # Bearer tokens and query bodies never enter access logs.
            pass

        def _send(self, status, *, data=None, warnings=None, meta=None, error=None):
            body = json.dumps(
                {
                    "schema_version": API_VERSION,
                    "ok": error is None,
                    "data": data,
                    "warnings": warnings or [],
                    "meta": meta or {},
                    "error": build_error_payload(error) if error else None,
                },
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            if len(body) > MAX_RESPONSE_BYTES:
                status = 503
                body = json.dumps(
                    {
                        "schema_version": API_VERSION,
                        "ok": False,
                        "error": {
                            "code": "READ_ERROR",
                            "message": "Performance response exceeds the transport limit; use a paged query.",
                        },
                    }
                ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            try:
                if self.path != QUERY_PATH:
                    self._send(404, error=AgentToolError("INPUT_ERROR", "Unknown performance service route."))
                    return
                authorization = self.headers.get_all("Authorization", [])
                if len(authorization) != 1 or not hmac.compare_digest(
                    authorization[0].encode(), ("Bearer " + token).encode()
                ):
                    self._send(401, error=AgentToolError("AUTH_ERROR", "Performance service authentication failed."))
                    return
                lengths = self.headers.get_all("Content-Length", [])
                if (
                    self.headers.get("Transfer-Encoding")
                    or len(lengths) != 1
                    or len(lengths[0]) > 6
                    or not lengths[0].isascii()
                    or not lengths[0].isdigit()
                ):
                    raise AgentToolError("INPUT_ERROR", "A single bounded Content-Length is required.")
                size = int(lengths[0])
                if not 0 < size <= MAX_REQUEST_BYTES or self.headers.get_content_type() != "application/json":
                    raise AgentToolError("INPUT_ERROR", "Send a bounded application/json query object.")
                try:
                    raw = self.rfile.read(size)
                    if len(raw) != size:
                        raise ValueError
                    payload = decode_protocol_json(raw)
                except (ValueError, RecursionError) as exc:
                    raise AgentToolError("INPUT_ERROR", "Performance query must be valid JSON.") from exc
                if not isinstance(payload, dict):
                    raise AgentToolError("INPUT_ERROR", "Performance query must be an object.")
                for name, value in payload.items():
                    if (
                        name not in {"include_rows", "limit"}
                        and value is not None
                        and not isinstance(value, str)
                        and not (name == "year" and type(value) is int)
                    ):
                        raise AgentToolError("INPUT_ERROR", "Performance query field types are invalid.")
                if any(payload.get(name) not in (None, "") for name in ("config_path", "data_config")):
                    raise AgentToolError(
                        "INPUT_ERROR", "Service mode does not accept config_path or data_config overrides."
                    )
                request, _window = normalize_option_performance_request(payload, normalize_broker=normalize_broker)
                if request["config_key"] != config_key or (request["account"] and request["account"] not in accounts):
                    raise AgentToolError("SCOPE_ERROR", "Performance query is outside the bound service scope.")
                data, warnings, meta = option_performance_report_tool(
                    payload,
                    load_runtime_config=lambda **kw: (bound_path, cfg),
                    resolve_public_data_config_path=lambda *args: bound_data,
                    normalize_broker=normalize_broker,
                    resolve_option_positions_repo=lambda **kw: (_resolved, reader),
                    build_option_period_performance=build_option_period_performance,
                    repo_base=repo_base,
                    mask_path=lambda path: "<bound-runtime>",
                )
                self._send(200, data=data, warnings=warnings, meta=meta)
            except AgentToolError as exc:
                self._send(ERROR_STATUS.get(exc.code, 500), error=exc)
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                # shortcut: client cancellation ends delivery; in-progress reads finish, add cancellation only if query cost warrants it.
                return
            except Exception:
                self._send(500, error=AgentToolError("INTERNAL_ERROR", "Performance query failed."))

        def do_GET(self):
            self._send(
                405 if self.path == QUERY_PATH else 404,
                error=AgentToolError("INPUT_ERROR", "Use POST on the performance query route."),
            )

        do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = do_TRACE = do_CONNECT = do_GET

    return HTTPServer(("127.0.0.1", port), Handler)
