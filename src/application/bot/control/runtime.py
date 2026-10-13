from __future__ import annotations

from datetime import date
from dataclasses import replace
import time
from typing import Any, Callable

from src.application.bot.control.settings import BotSettings, request_with_default_market_scope as _request_with_default_market_scope
from src.application.bot.control.contracts import BotInboundRequest, BotTurnResult
from src.application.bot.control.audit import InboundAuditStore
from src.application.bot.control.inbound_service import ExecuteToolFn, handle_bot_request
from src.application.bot.control.turn_result import (
    bot_turn_result_from_response_payload,
    with_bot_turn_result,
)
from src.application.tool_execution import execute_tool


def handle_bot_turn(
    request: BotInboundRequest,
    *,
    audit_store: InboundAuditStore | None = None,
    execute_tool_fn: ExecuteToolFn = execute_tool,
    allowed_senders: str | None = None,
    now_fn: Callable[[], date] | None = None,
    settings: BotSettings | None = None,
) -> BotTurnResult:
    if request.received_monotonic is None:
        request = replace(request, received_monotonic=time.monotonic())
    runtime_settings = settings or BotSettings()
    if not runtime_settings.enabled:
        return BotTurnResult(
            response_text="Bot 已禁用。",
            render_route="disabled",
            ok=False,
            status="disabled",
            tool_name="bot.handle",
            error={"code": "BOT_DISABLED", "message": "Bot is disabled"},
            trace={"route": "disabled"},
            meta={"bot": {"enabled": False, "route": "disabled"}},
        )
    response = _run_bot_turn_response(
        request,
        audit_store=audit_store,
        execute_tool_fn=execute_tool_fn,
        allowed_senders=allowed_senders,
        now_fn=now_fn,
        settings=runtime_settings,
    )
    return bot_turn_result_from_response_payload(response)


def _run_bot_turn_response(
    request: BotInboundRequest,
    *,
    audit_store: InboundAuditStore | None = None,
    execute_tool_fn: ExecuteToolFn = execute_tool,
    allowed_senders: str | None = None,
    now_fn: Callable[[], date] | None = None,
    settings: BotSettings | None = None,
) -> dict[str, Any]:
    runtime_settings = settings or BotSettings()
    request = _request_with_default_market_scope(request, runtime_settings)
    store = audit_store or InboundAuditStore(request.audit_db, deadline_monotonic=(request.received_monotonic if request.received_monotonic is not None else time.monotonic()) + 180)
    response = handle_bot_request(
        request,
        audit_store=store,
        execute_tool_fn=execute_tool_fn,
        allowed_senders=allowed_senders,
        now_fn=now_fn,
    )
    recorded_response = response
    route = _response_route(response)
    response = _with_bot_meta(
        response,
        route=route,
        settings=runtime_settings,
    )
    response = with_bot_turn_result(response, route=route)
    _update_audit_response(store=store, response=response, expected_response=recorded_response)
    return response


def _update_audit_response(*, store: InboundAuditStore, response: dict[str, Any], expected_response: dict[str, Any]) -> None:
    error = response.get("error")
    if isinstance(error, dict) and error.get("code") == "PERMISSION_DENIED":
        # A current denial must not overwrite a prior authorized response.
        return
    meta = response.get("meta")
    if isinstance(meta, dict) and bool(meta.get("idempotent_replay")):
        return
    data = response.get("data")
    command_id = data.get("command_id") if isinstance(data, dict) else None
    if not command_id:
        return
    store.update_response(command_id=str(command_id), response=response, expected_response=expected_response)


def _with_bot_meta(
    response: dict[str, Any],
    *,
    route: str,
    settings: BotSettings,
) -> dict[str, Any]:
    meta_raw = response.get("meta")
    meta = dict(meta_raw) if isinstance(meta_raw, dict) else {}
    bot_meta = {
        "enabled": bool(settings.enabled),
        "route": route,
    }
    bot_meta["decision"] = {
        "route": route,
        "source": "bot" if route == "bot" else "deterministic_control",
    }
    meta["bot"] = bot_meta
    return {**response, "meta": meta}


def _response_route(response: dict[str, Any]) -> str:
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    decision = data.get("decision") if isinstance(data.get("decision"), dict) else {}
    if str(decision.get("reason") or "") == "bot_freeform":
        return "bot"
    return "deterministic_control"
