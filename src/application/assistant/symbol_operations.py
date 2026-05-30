from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from src.application.account_config import normalize_accounts
from src.application.agent_tool_config import resolve_runtime_config_path
from src.application.agent_tool_contracts import AgentToolError, build_response, mask_path
from src.application.config_loader import resolve_watchlist_config, set_watchlist_config
from src.application.config_validator import validate_config
from src.application.runtime_config_freshness import infer_runtime_config_market
from src.application.assistant.contracts import AssistantRequest, PerceptionResult
from src.application.assistant.operation_lifecycle import (
    build_cancelled_operation_response,
    build_previewed_operation_response,
    confirm_previewed_operation_or_raise,
    resolve_pending_operation_or_raise,
)
from src.application.assistant.operation_policy import enforce_symbol_write_allowed
from src.application.assistant.operation_store import InboundOperationStore
from src.application.assistant.operation_status_text import operation_candidate_hint
from src.application.runtime_config_paths import write_json_atomic
from src.application.symbol_calibration import calibrate_symbol
from src.application.symbol_mutations import add_symbol_entry, edit_symbol_entry, remove_symbol_entry


LIST_INTENTS = frozenset({"symbol_list"})
PREVIEW_INTENTS = frozenset({"symbol_add", "symbol_edit", "symbol_remove"})
CONFIRM_INTENTS = frozenset({"symbol_confirm", "symbol_cancel"})
SYMBOL_OPERATION_TYPES = PREVIEW_INTENTS


def handle_symbol_operation(
    intent: PerceptionResult,
    request: AssistantRequest,
    *,
    command_id: str,
    store: InboundOperationStore,
) -> dict[str, Any]:
    if intent.intent_name == "symbol_list":
        return _list_symbols(request)
    policy = enforce_symbol_write_allowed(channel=request.channel, sender_id=request.sender_id)
    if intent.intent_name in PREVIEW_INTENTS:
        arguments = dict(intent.arguments)
        config_path, _cfg, config_key = _load_config_for_symbol_request(request, arguments=arguments)
        payload = _build_operation_payload(intent.intent_name, arguments, request=request, config_path=config_path, config_key=config_key)
        return _preview_and_save(payload, request=request, command_id=command_id, store=store, ttl_seconds=policy.confirm_ttl_seconds)
    if intent.intent_name == "symbol_confirm":
        return _confirm_operation(operation_id=_optional_text(intent.arguments.get("operation_id")), request=request, store=store)
    if intent.intent_name == "symbol_cancel":
        return _cancel_operation(operation_id=_optional_text(intent.arguments.get("operation_id")), request=request, store=store)
    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported symbol operation intent: {intent.intent_name}")


def _list_symbols(request: AssistantRequest) -> dict[str, Any]:
    config_path, cfg = _load_config(request)
    rows = _symbol_rows(cfg)
    text = render_symbol_response(status="listed", operation_id="", payload={}, preview={"symbols": rows, "config_path": str(config_path)})
    return build_response(tool_name="inbound.symbols", ok=True, data={"status": "listed", "config_path": str(config_path), "symbols": rows, "symbol_count": len(rows), "response_text": text})


def _preview_and_save(
    payload: dict[str, Any],
    *,
    request: AssistantRequest,
    command_id: str,
    store: InboundOperationStore,
    ttl_seconds: int,
) -> dict[str, Any]:
    preview = _preview_operation(payload)
    return build_previewed_operation_response(
        tool_name="inbound.symbols",
        operation_id=command_id,
        request=request,
        store=store,
        payload=payload,
        preview=preview,
        ttl_seconds=ttl_seconds,
        response_text=lambda operation: render_symbol_response(
            status="previewed",
            operation_id=command_id,
            payload=payload,
            preview=preview,
            expires_at=str(operation.get("expires_at") or ""),
        ),
    )


def _confirm_operation(*, operation_id: str | None, request: AssistantRequest, store: InboundOperationStore) -> dict[str, Any]:
    operation_id, operation, operation_resolution = _resolve_symbol_operation(
        operation_id=operation_id,
        request=request,
        store=store,
        allow_expired=False,
        action="确认",
    )
    confirmed = confirm_previewed_operation_or_raise(
        operation_id=operation_id,
        operation=operation,
        operation_resolution=operation_resolution,
        store=store,
        subject="监控标的变更",
        expired_message="这条监控标的变更确认已过期，未写入配置。",
        expired_hint="请重新发送监控标的命令生成新的预览。",
        hash_mismatch_message="pending symbol operation payload hash mismatch; refusing to write config",
    )
    operation_id = confirmed.operation_id
    operation_resolution = confirmed.operation_resolution
    payload = confirmed.payload
    try:
        preview = _preview_operation(payload)
        result = _apply_operation(payload)
    except AgentToolError as exc:
        store.mark_failed(operation_id, result={"operation_id": operation_id, "status": "failed", "error": exc.code, "message": exc.message})
        raise
    except Exception as exc:
        failed = {"operation_id": operation_id, "status": "failed", "error": type(exc).__name__, "message": str(exc)}
        store.mark_failed(operation_id, result=failed)
        raise AgentToolError(code="INTERNAL_ERROR", message="symbol operation failed before config write could be confirmed", details=failed) from exc
    store.mark_applied(operation_id, result=result)
    text = render_symbol_response(status="applied", operation_id=operation_id, payload=payload, preview=preview, result=result)
    return build_response(
        tool_name="inbound.symbols",
        ok=True,
        data={
            "operation_id": operation_id,
            **operation_resolution,
            "operation_type": payload["operation_type"],
            "status": "applied",
            "payload_hash": confirmed.payload_hash,
            "payload": payload,
            "preview": preview,
            "result": result,
            "response_text": text,
        },
        meta={"audit_db": mask_path(store.path)},
    )


def _cancel_operation(*, operation_id: str | None, request: AssistantRequest, store: InboundOperationStore) -> dict[str, Any]:
    operation_id, operation, operation_resolution = _resolve_symbol_operation(
        operation_id=operation_id,
        request=request,
        store=store,
        allow_expired=True,
        action="取消",
    )
    text = f"监控标的变更已取消，未写入配置。\ncommand_id: {operation_id}"
    return build_cancelled_operation_response(
        tool_name="inbound.symbols",
        operation_id=operation_id,
        operation=operation,
        operation_resolution=operation_resolution,
        store=store,
        response_text=text,
    )


def _resolve_symbol_operation(
    *,
    operation_id: str | None,
    request: AssistantRequest,
    store: InboundOperationStore,
    allow_expired: bool,
    action: str,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    return resolve_pending_operation_or_raise(
        operation_id=operation_id,
        request=request,
        store=store,
        operation_types=SYMBOL_OPERATION_TYPES,
        allow_expired=allow_expired,
        action=action,
        subject="监控标的变更",
        expired_message="这条监控标的变更确认已过期，未写入配置。",
        expired_hint="请重新发送监控标的命令生成新的预览。",
        none_hint="请先发送监控标的变更命令生成预览。",
        wrong_family_message="这不是监控标的变更，不能用确认监控/取消监控处理。",
        not_found_message="找不到待确认的监控标的变更。",
        not_found_hint="请检查 operation_id，或重新发送监控标的命令。",
        candidate_hint=_symbol_candidate_hint,
    )


def _candidate_hint(prefix: str, candidates: Any) -> str:
    return operation_candidate_hint(prefix, candidates, heading="候选变更")


def _symbol_candidate_hint(action: str, candidates: Any) -> str:
    return _candidate_hint("确认监控" if action == "确认" else "取消监控", candidates)


def _build_operation_payload(
    operation_type: str,
    arguments: dict[str, Any],
    *,
    request: AssistantRequest,
    config_path: Any | None = None,
    config_key: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "operation_type": operation_type,
        "arguments": arguments,
        "config": {
            "config_key": config_key if config_key is not None else request.config_key,
            "config_path": str(config_path) if config_path else request.config_path,
        },
    }


def _preview_operation(payload: dict[str, Any]) -> dict[str, Any]:
    config_path, cfg = _load_config_for_payload(payload)
    mutated = deepcopy(cfg)
    summary = _apply_symbol_payload(mutated, payload)
    _validate_symbols_config(mutated)
    return {"config_path": str(config_path), "summary": summary, "symbol_count_before": len(_symbol_rows(cfg)), "symbol_count_after": len(_symbol_rows(mutated)), "symbols": _symbol_rows(mutated)}


def _apply_operation(payload: dict[str, Any]) -> dict[str, Any]:
    config_path, cfg = _load_config_for_payload(payload)
    summary = _apply_symbol_payload(cfg, payload)
    canonical = set_watchlist_config(cfg, resolve_watchlist_config(cfg))
    _validate_symbols_config(canonical)
    write_json_atomic(config_path, canonical)
    return {"status": "applied", "config_path": str(config_path), "summary": summary, "symbol_count": len(_symbol_rows(canonical))}


def _apply_symbol_payload(cfg: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    operation_type = str(payload.get("operation_type") or "")
    args = dict(payload.get("arguments") or {})
    if operation_type == "symbol_add":
        summary = add_symbol_entry(
            cfg,
            symbol=_required_text(args.get("symbol"), "symbol"),
            use=str(args["use"]) if args.get("use") is not None else None,
            limit_expirations=int(args.get("limit_expirations") or 8),
            sell_put_enabled=bool(args.get("sell_put_enabled", False)),
            sell_call_enabled=bool(args.get("sell_call_enabled", False)),
            accounts=args.get("accounts") if isinstance(args.get("accounts"), list) else None,
            normalize_accounts=lambda value: normalize_accounts(value, fallback=()),
            error_factory=_input_error,
        )
        return summary.public_payload()
    if operation_type == "symbol_edit":
        sets = args.get("set")
        if not isinstance(sets, dict) or not sets:
            raise AgentToolError(code="NEEDS_CLARIFICATION", message="修改监控标的需要提供 field=value。")
        if any(str(key).strip() == "symbol" or str(key).strip().startswith("symbol.") for key in sets):
            raise AgentToolError(code="INPUT_ERROR", message="不能通过 edit 修改 symbol 本身；请删除后重新新增。")
        ensure_use = args.get("ensure_use") if isinstance(args.get("ensure_use"), list) else None
        return edit_symbol_entry(
            cfg,
            symbol=_required_text(args.get("symbol"), "symbol"),
            sets=sets,
            ensure_use=ensure_use,
            error_factory=_input_error,
        ).public_payload()
    if operation_type == "symbol_remove":
        return remove_symbol_entry(cfg, symbol=_required_text(args.get("symbol"), "symbol"), error_factory=_input_error).public_payload()
    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported symbol operation_type: {operation_type}")


def _load_config_for_payload(payload: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    config_key = str(config.get("config_key") or "").strip().lower() or None
    return _load_config(AssistantRequest(text="", sender_id="", channel="local", config_key=config_key, config_path=config.get("config_path")))


def _load_config_for_symbol_request(request: AssistantRequest, *, arguments: dict[str, Any]) -> tuple[Any, dict[str, Any], str | None]:
    _require_runtime_config_scope(request)
    current_path, current_cfg = _load_config(request)
    target_market = _symbol_market_from_arguments(arguments, config=current_cfg)
    if target_market is None:
        return current_path, current_cfg, request.config_key

    current_market = infer_runtime_config_market(
        config_key=request.config_key,
        config_path=current_path,
        config=current_cfg,
    )
    if current_market == target_market:
        return current_path, current_cfg, target_market

    target_path = _runtime_config_path_for_market(request=request, current_path=current_path, target_market=target_market)
    target_cfg = _read_runtime_config(target_path)
    return target_path, target_cfg, target_market


def _load_config(request: AssistantRequest) -> tuple[Any, dict[str, Any]]:
    _require_runtime_config_scope(request)
    config_path = resolve_runtime_config_path(config_key=request.config_key, config_path=request.config_path)
    return config_path, _read_runtime_config(config_path)


def _read_runtime_config(config_path: Any) -> dict[str, Any]:
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AgentToolError(code="CONFIG_ERROR", message=f"failed to load runtime config: {config_path.name}", details={"error": f"{type(exc).__name__}: {exc}"}) from exc
    if not isinstance(data, dict):
        raise AgentToolError(code="CONFIG_ERROR", message="runtime config must be a JSON object")
    return data


def _symbol_market_from_arguments(arguments: dict[str, Any], *, config: dict[str, Any]) -> str | None:
    raw_symbol = str(arguments.get("symbol") or "").strip()
    if not raw_symbol:
        return None
    calibrated = calibrate_symbol(raw_symbol, config=config)
    if calibrated.status != "ok":
        calibrated = calibrate_symbol(raw_symbol)
    market = str(calibrated.market or "").strip().upper()
    if market == "US":
        return "us"
    if market == "HK":
        return "hk"
    return None


def _runtime_config_path_for_market(
    *,
    request: AssistantRequest,
    current_path: Any,
    target_market: str,
) -> Any:
    if request.config_path:
        sibling = current_path.with_name(f"config.{target_market}.json")
        if sibling.exists():
            return sibling
        raise AgentToolError(
            code="NEEDS_CLARIFICATION",
            message=f"监控标的属于 {target_market.upper()}，但没有找到对应 runtime config。",
            hint=f"请传入 config.{target_market}.json 的 --config-path，或先构建对应市场配置。",
            details={"requested_market": target_market, "current_config_path": str(current_path)},
        )
    return resolve_runtime_config_path(config_key=target_market, config_path=None)


def _require_runtime_config_scope(request: AssistantRequest) -> None:
    if request.config_path or request.config_key:
        return
    raise AgentToolError(
        code="NEEDS_CLARIFICATION",
        message="监控标的操作前需要先指定市场。",
        hint="请明确美股或港股，或通过 --config-key us/hk、--config-path、assistant.default_market_scope 配置默认市场。",
        details={"required": "config_key_or_config_path"},
    )


def _validate_symbols_config(cfg: dict[str, Any]) -> None:
    try:
        validate_config(dict(cfg))
    except SystemExit as exc:
        raise AgentToolError(code="CONFIG_ERROR", message=str(exc)) from exc


def _symbol_rows(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "symbol": item.get("symbol"),
            "use": item.get("use"),
            "accounts": item.get("accounts"),
            "sell_put_enabled": bool((item.get("sell_put") or {}).get("enabled", False)),
            "sell_call_enabled": bool((item.get("sell_call") or {}).get("enabled", False)),
        }
        for item in resolve_watchlist_config(cfg)
    ]


def render_symbol_response(
    *,
    status: str,
    operation_id: str,
    payload: dict[str, Any],
    preview: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    expires_at: str | None = None,
) -> str:
    del result
    if status == "listed":
        rows = preview.get("symbols") if isinstance(preview, dict) else []
        if not isinstance(rows, list) or not rows:
            return "当前没有配置监控标的。"
        lines = [f"当前监控标的：{len(rows)} 个"]
        for row in rows[:20]:
            if isinstance(row, dict):
                modes = []
                if row.get("sell_put_enabled"):
                    modes.append("put")
                if row.get("sell_call_enabled"):
                    modes.append("call")
                lines.append(f"- {row.get('symbol') or '-'} | {','.join(modes) if modes else 'off'} | use={row.get('use') or '-'}")
        return "\n".join(lines)
    if status == "cancelled":
        return f"监控标的变更已取消，未写入配置。\ncommand_id: {operation_id}"
    summary = preview.get("summary") if isinstance(preview, dict) and isinstance(preview.get("summary"), dict) else {}
    cal = summary.get("calibration") if isinstance(summary.get("calibration"), dict) else {}
    action = str(summary.get("action") or str(payload.get("operation_type") or "").removeprefix("symbol_"))
    action_label = {"add": "新增", "edit": "修改", "remove": "删除"}.get(action, action)
    title = "监控标的变更预览" if status == "previewed" else "监控标的变更已写入配置"
    lines = [
        f"{title}：{action_label}",
        f"输入：{summary.get('raw_symbol') or '-'}",
        f"校准为：{summary.get('canonical_symbol') or '-'}",
        f"市场：{cal.get('market') or '-'}",
        f"Futu code：{cal.get('futu_code') or '-'}",
        f"来源：{cal.get('source_kind') or '-'}",
    ]
    changed_paths = summary.get("changed_paths")
    if isinstance(changed_paths, list) and changed_paths:
        lines.append("变更：" + "、".join(str(item) for item in changed_paths))
    if isinstance(preview, dict) and preview.get("config_path"):
        lines.append(f"配置：{preview.get('config_path')}")
    if status == "previewed":
        lines.extend(
            [
                "",
                "未写入配置。",
                "确认写入请回复：确认监控",
                "取消请回复：取消监控",
                f"operation_id：{operation_id}",
                f"如同时有多条待确认，请回复：确认监控 {operation_id}",
            ]
        )
        if expires_at:
            lines.append("有效期：10 分钟。")
    else:
        lines.append(f"command_id：{operation_id}")
    return "\n".join(str(line) for line in lines)


def _input_error(message: str) -> AgentToolError:
    return AgentToolError(code="INPUT_ERROR", message=message)


def _required_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise AgentToolError(code="NEEDS_CLARIFICATION", message=f"{field_name} is required")
    return text


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None
