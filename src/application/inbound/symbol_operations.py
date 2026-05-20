from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from src.application.account_config import normalize_accounts
from src.application.agent_tool_config import resolve_runtime_config_path
from src.application.agent_tool_contracts import AgentToolError, build_response, mask_path
from src.application.config_loader import resolve_watchlist_config, set_watchlist_config
from src.application.config_validator import validate_config
from src.application.inbound.contracts import InboundIntent, InboundRequest
from src.application.inbound.operation_policy import enforce_symbol_write_allowed
from src.application.inbound.operation_store import InboundOperationStore, operation_is_expired
from src.application.runtime_config_paths import write_json_atomic
from src.application.symbol_mutations import add_symbol_entry, edit_symbol_entry, remove_symbol_entry


LIST_INTENTS = frozenset({"symbol_list"})
PREVIEW_INTENTS = frozenset({"symbol_add", "symbol_edit", "symbol_remove"})
CONFIRM_INTENTS = frozenset({"symbol_confirm", "symbol_cancel"})
SYMBOL_OPERATION_TYPES = PREVIEW_INTENTS


def is_symbol_operation_intent(intent: InboundIntent) -> bool:
    return intent.name in LIST_INTENTS or intent.name in PREVIEW_INTENTS or intent.name in CONFIRM_INTENTS


def handle_symbol_operation(
    intent: InboundIntent,
    request: InboundRequest,
    *,
    command_id: str,
    store: InboundOperationStore,
) -> dict[str, Any]:
    if intent.name == "symbol_list":
        return _list_symbols(request)
    policy = enforce_symbol_write_allowed(channel=request.channel, sender_id=request.sender_id)
    if intent.name in PREVIEW_INTENTS:
        payload = _build_operation_payload(intent.name, dict(intent.arguments), request=request)
        return _preview_and_save(payload, request=request, command_id=command_id, store=store, ttl_seconds=policy.confirm_ttl_seconds)
    if intent.name == "symbol_confirm":
        return _confirm_operation(operation_id=_optional_text(intent.arguments.get("operation_id")), request=request, store=store)
    if intent.name == "symbol_cancel":
        return _cancel_operation(operation_id=_optional_text(intent.arguments.get("operation_id")), request=request, store=store)
    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported symbol operation intent: {intent.name}")


def _list_symbols(request: InboundRequest) -> dict[str, Any]:
    config_path, cfg = _load_config(request)
    rows = _symbol_rows(cfg)
    text = render_symbol_response(status="listed", operation_id="", payload={}, preview={"symbols": rows, "config_path": str(config_path)})
    return build_response(tool_name="inbound.symbols", ok=True, data={"status": "listed", "config_path": str(config_path), "symbols": rows, "symbol_count": len(rows), "response_text": text})


def _preview_and_save(
    payload: dict[str, Any],
    *,
    request: InboundRequest,
    command_id: str,
    store: InboundOperationStore,
    ttl_seconds: int,
) -> dict[str, Any]:
    preview = _preview_operation(payload)
    payload_hash = hash_operation_payload(payload)
    operation = store.save_preview(
        operation_id=command_id,
        command_id=command_id,
        channel=request.channel,
        sender_id=request.sender_id,
        conversation_id=request.conversation_id,
        operation_type=str(payload["operation_type"]),
        payload_hash=payload_hash,
        payload=payload,
        preview=preview,
        ttl_seconds=ttl_seconds,
    )
    text = render_symbol_response(status="previewed", operation_id=command_id, payload=payload, preview=preview, expires_at=str(operation.get("expires_at") or ""))
    return build_response(
        tool_name="inbound.symbols",
        ok=True,
        data={
            "operation_id": command_id,
            "operation_type": payload["operation_type"],
            "status": "previewed",
            "payload_hash": payload_hash,
            "payload": payload,
            "preview": preview,
            "expires_at": operation.get("expires_at"),
            "response_text": text,
        },
        meta={"audit_db": mask_path(store.path)},
    )


def _confirm_operation(*, operation_id: str | None, request: InboundRequest, store: InboundOperationStore) -> dict[str, Any]:
    operation_id, operation, operation_resolution = _resolve_symbol_operation(
        operation_id=operation_id,
        request=request,
        store=store,
        allow_expired=False,
        action="确认",
    )
    if operation_is_expired(operation):
        result = {"operation_id": operation_id, "status": "expired"}
        store.mark_expired(operation_id, result=result)
        raise AgentToolError(code="NEEDS_CLARIFICATION", message="这条监控标的变更确认已过期，未写入配置。", hint="请重新发送监控标的命令生成新的预览。", details={**result, **operation_resolution})
    payload = dict(operation["payload"])
    stored_hash = str(operation.get("payload_hash") or "")
    current_hash = hash_operation_payload(payload)
    if stored_hash != current_hash:
        result = {"operation_id": operation_id, "status": "failed", "reason": "payload_hash_mismatch"}
        store.mark_failed(operation_id, result=result)
        raise AgentToolError(code="INTERNAL_ERROR", message="pending symbol operation payload hash mismatch; refusing to write config", details=result)
    if not store.mark_confirmed(operation_id):
        current = store.get(operation_id) or {}
        current_status = str(current.get("status") or "-")
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"这条监控标的变更不能再次确认，当前状态：{current_status}。",
            details={
                "operation_id": operation_id,
                "status": current_status,
                "reason": "operation_not_previewed",
                **operation_resolution,
            },
        )
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
        data={"operation_id": operation_id, **operation_resolution, "operation_type": payload["operation_type"], "status": "applied", "payload_hash": current_hash, "payload": payload, "preview": preview, "result": result, "response_text": text},
        meta={"audit_db": mask_path(store.path)},
    )


def _cancel_operation(*, operation_id: str | None, request: InboundRequest, store: InboundOperationStore) -> dict[str, Any]:
    operation_id, operation, operation_resolution = _resolve_symbol_operation(
        operation_id=operation_id,
        request=request,
        store=store,
        allow_expired=True,
        action="取消",
    )
    result = {"operation_id": operation_id, "status": "cancelled"}
    store.mark_cancelled(operation_id, result=result)
    text = f"监控标的变更已取消，未写入配置。\ncommand_id: {operation_id}"
    return build_response(tool_name="inbound.symbols", ok=True, data={"operation_id": operation_id, **operation_resolution, "operation_type": operation.get("operation_type"), "status": "cancelled", "result": result, "response_text": text}, meta={"audit_db": mask_path(store.path)})


def _resolve_symbol_operation(
    *,
    operation_id: str | None,
    request: InboundRequest,
    store: InboundOperationStore,
    allow_expired: bool,
    action: str,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    resolution = store.resolve_pending_operation(
        channel=request.channel,
        sender_id=request.sender_id,
        operation_types=SYMBOL_OPERATION_TYPES,
        conversation_id=request.conversation_id,
        explicit_operation_id=operation_id,
        allow_expired=allow_expired,
    )
    details = _operation_resolution_details(resolution)
    status = str(resolution.get("status") or "")
    resolved_operation_id = str(resolution.get("operation_id") or operation_id or "").strip()
    operation_raw = resolution.get("operation")
    operation = operation_raw if isinstance(operation_raw, dict) else {}
    if status == "resolved" and resolved_operation_id and operation:
        return resolved_operation_id, operation, details
    if status == "expired":
        result = {"operation_id": resolved_operation_id, "status": "expired"}
        if resolved_operation_id:
            store.mark_expired(resolved_operation_id, result=result)
        raise AgentToolError(code="NEEDS_CLARIFICATION", message="这条监控标的变更确认已过期，未写入配置。", hint="请重新发送监控标的命令生成新的预览。", details={**result, **details})
    if status == "ambiguous":
        raise AgentToolError(
            code="NEEDS_CLARIFICATION",
            message=f"有多条待{action}的监控标的变更，请带 operation_id。",
            hint=_candidate_hint("确认监控" if action == "确认" else "取消监控", details.get("candidate_operations")),
            details=details,
        )
    if status == "none":
        raise AgentToolError(code="NEEDS_CLARIFICATION", message=f"没有可{action}的监控标的变更。", hint="请先发送监控标的变更命令生成预览。", details=details)
    if status == "forbidden":
        raise AgentToolError(code="PERMISSION_DENIED", message=f"只能由创建该预览的同一 sender/对话 {action}。", details=details)
    if status == "wrong_family":
        raise AgentToolError(code="INPUT_ERROR", message="这不是监控标的变更，不能用确认监控/取消监控处理。", details=details)
    if status == "invalid_status":
        current_status = str(operation.get("status") or "-")
        raise AgentToolError(code="INPUT_ERROR", message=f"这条监控标的变更不能再次{action}，当前状态：{current_status}。", details=details)
    raise AgentToolError(code="INPUT_ERROR", message="找不到待确认的监控标的变更。", hint="请检查 operation_id，或重新发送监控标的命令。", details=details)


def _operation_resolution_details(resolution: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation_resolution": resolution.get("operation_resolution"),
        "resolved_operation_id": resolution.get("operation_id"),
        "candidate_operations": resolution.get("candidate_operations") or [],
    }


def _candidate_hint(prefix: str, candidates: Any) -> str:
    rows = candidates if isinstance(candidates, list) else []
    lines: list[str] = []
    for idx, item_raw in enumerate(rows[:5], start=1):
        if not isinstance(item_raw, dict):
            continue
        operation_id = str(item_raw.get("operation_id") or "").strip()
        if not operation_id:
            continue
        summary = str(item_raw.get("summary") or item_raw.get("operation_type") or "-").strip()
        lines.append(f"{idx}. {operation_id} | {summary} | 回复：{prefix} {operation_id}")
    if not lines:
        return f"请回复：{prefix} <operation_id>"
    return "\n候选变更：\n" + "\n".join(lines)


def _build_operation_payload(operation_type: str, arguments: dict[str, Any], *, request: InboundRequest) -> dict[str, Any]:
    return {"schema_version": "1.0", "operation_type": operation_type, "arguments": arguments, "config": {"config_key": request.config_key, "config_path": request.config_path}}


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
            use=str(args.get("use") or "put_base"),
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
        return edit_symbol_entry(cfg, symbol=_required_text(args.get("symbol"), "symbol"), sets=sets, error_factory=_input_error).public_payload()
    if operation_type == "symbol_remove":
        return remove_symbol_entry(cfg, symbol=_required_text(args.get("symbol"), "symbol"), error_factory=_input_error).public_payload()
    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported symbol operation_type: {operation_type}")


def _load_config_for_payload(payload: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    return _load_config(InboundRequest(text="", sender_id="", channel="local", config_key=str(config.get("config_key") or "us"), config_path=config.get("config_path")))


def _load_config(request: InboundRequest) -> tuple[Any, dict[str, Any]]:
    config_path = resolve_runtime_config_path(config_key=request.config_key or "us", config_path=request.config_path)
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AgentToolError(code="CONFIG_ERROR", message=f"failed to load runtime config: {config_path.name}", details={"error": f"{type(exc).__name__}: {exc}"}) from exc
    if not isinstance(data, dict):
        raise AgentToolError(code="CONFIG_ERROR", message="runtime config must be a JSON object")
    return config_path, data


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


def hash_operation_payload(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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
