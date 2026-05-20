from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, is_dataclass
from typing import Any, cast

from src.application.agent_tool_config import load_runtime_config, repo_base
from src.application.agent_tool_contracts import AgentToolError, build_response, mask_path
from src.application.inbound.contracts import InboundIntent, InboundRequest
from src.application.inbound.manual_trade_parser import build_manual_trade_draft
from src.application.inbound.operation_policy import enforce_trade_write_allowed
from src.application.inbound.operation_store import InboundOperationStore, operation_is_expired
from src.application.ledger.api import open_position_ledger_from_runtime_config
from src.application.positions.workflows import (
    ManualCloseMatchError,
    execute_manual_close,
    execute_manual_open,
)


PREVIEW_INTENTS = frozenset({"manual_trade_open", "manual_trade_close"})
CONFIRM_INTENTS = frozenset({"manual_trade_confirm", "manual_trade_cancel"})
UPDATE_INTENTS = frozenset({"manual_trade_update"})
MANUAL_TRADE_OPERATION_TYPES = frozenset({"manual_open", "manual_close"})
MANUAL_OPEN_UPDATE_FIELDS = frozenset(
    {
        "contracts",
        "strike",
        "multiplier",
        "expiration_ymd",
        "premium_per_share",
        "underlying_share_locked",
        "currency",
        "note",
    }
)
MANUAL_CLOSE_UPDATE_FIELDS = frozenset(
    {
        "record_id",
        "contracts",
        "contracts_to_close",
        "strike",
        "expiration_ymd",
        "close_price",
        "close_reason",
    }
)
FIELD_LABELS = {
    "contracts": "合约数",
    "contracts_to_close": "平仓数量",
    "strike": "Strike",
    "multiplier": "Multiplier",
    "expiration_ymd": "到期日",
    "premium_per_share": "Premium",
    "underlying_share_locked": "Locked",
    "currency": "币种",
    "note": "备注",
    "record_id": "record_id",
    "close_price": "平仓价",
    "close_reason": "平仓原因",
}


def is_manual_trade_operation_intent(intent: InboundIntent) -> bool:
    return intent.name in PREVIEW_INTENTS or intent.name in CONFIRM_INTENTS or intent.name in UPDATE_INTENTS


def handle_manual_trade_operation(
    intent: InboundIntent,
    request: InboundRequest,
    *,
    command_id: str,
    store: InboundOperationStore,
) -> dict[str, Any]:
    policy = enforce_trade_write_allowed(channel=request.channel, sender_id=request.sender_id)
    if intent.name == "manual_trade_open":
        config_path, cfg = _load_runtime_config_for_request(request)
        draft = build_manual_trade_draft(
            "manual_open",
            raw_text=_manual_trade_raw_text(intent, request),
            accounts=_accounts_from_runtime_config(cfg),
            config_key=request.config_key,
            config_path=config_path,
            runtime_config=cfg,
            repo_base=repo_base(),
            allow_opend_refresh=True,
        )
        payload = _build_operation_payload(
            "manual_open",
            _manual_open_args(draft["arguments"]),
            request=request,
            config_path=config_path,
            diagnostics=draft["diagnostics"],
        )
        return _preview_and_save(payload, request=request, command_id=command_id, store=store, ttl_seconds=policy.confirm_ttl_seconds)
    if intent.name == "manual_trade_close":
        config_path, cfg = _load_runtime_config_for_request(request)
        draft = build_manual_trade_draft(
            "manual_close",
            raw_text=_manual_trade_raw_text(intent, request),
            accounts=_accounts_from_runtime_config(cfg),
            config_key=request.config_key,
            config_path=config_path,
            runtime_config=cfg,
            repo_base=repo_base(),
            allow_opend_refresh=True,
        )
        payload = _build_operation_payload(
            "manual_close",
            _manual_close_args(draft["arguments"]),
            request=request,
            config_path=config_path,
            diagnostics=draft["diagnostics"],
        )
        return _preview_and_save(payload, request=request, command_id=command_id, store=store, ttl_seconds=policy.confirm_ttl_seconds)
    if intent.name == "manual_trade_confirm":
        return _confirm_operation(operation_id=_optional_text(intent.arguments.get("operation_id")), request=request, store=store)
    if intent.name == "manual_trade_cancel":
        return _cancel_operation(operation_id=_optional_text(intent.arguments.get("operation_id")), request=request, store=store)
    if intent.name == "manual_trade_update":
        return _update_operation(
            operation_id=_optional_text(intent.arguments.get("operation_id")),
            updates=dict(intent.arguments.get("updates") or {}),
            request=request,
            store=store,
        )
    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported manual trade operation intent: {intent.name}")


def _preview_and_save(
    payload: dict[str, Any],
    *,
    request: InboundRequest,
    command_id: str,
    store: InboundOperationStore,
    ttl_seconds: int,
) -> dict[str, Any]:
    preview = _preview_operation(payload)
    payload = _payload_with_preview_locked_values(payload, preview)
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
    text = render_manual_trade_response("previewed", command_id, payload, preview=preview, expires_at=str(operation.get("expires_at") or ""))
    return build_response(
        tool_name="inbound.manual_trade",
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
    operation_id, operation, operation_resolution = _resolve_manual_trade_operation(
        operation_id=operation_id,
        request=request,
        store=store,
        allow_expired=False,
        action="确认",
    )
    if operation_is_expired(operation):
        result = {"operation_id": operation_id, "status": "expired"}
        store.mark_expired(operation_id, result=result)
        raise AgentToolError(code="NEEDS_CLARIFICATION", message="这条交易记录确认已过期，未写入账本。", hint="请重新发送记录交易命令生成新的预览。", details={**result, **operation_resolution})
    payload = dict(operation["payload"])
    stored_hash = str(operation.get("payload_hash") or "")
    current_hash = hash_operation_payload(payload)
    if stored_hash != current_hash:
        result = {"operation_id": operation_id, "status": "failed", "reason": "payload_hash_mismatch"}
        store.mark_failed(operation_id, result=result)
        raise AgentToolError(code="INTERNAL_ERROR", message="pending operation payload hash mismatch; refusing to write ledger", details=result)
    if not store.mark_confirmed(operation_id):
        current = store.get(operation_id) or {}
        current_status = str(current.get("status") or "-")
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"这条交易记录不能再次确认，当前状态：{current_status}。",
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
        raise AgentToolError(code="INTERNAL_ERROR", message="manual trade operation failed before ledger write could be confirmed", details=failed) from exc
    store.mark_applied(operation_id, result=result)
    text = render_manual_trade_response("applied", operation_id, payload, preview=preview, result=result)
    return build_response(
        tool_name="inbound.manual_trade",
        ok=True,
        data={
            "operation_id": operation_id,
            **operation_resolution,
            "operation_type": payload["operation_type"],
            "status": "applied",
            "payload_hash": current_hash,
            "payload": payload,
            "preview": preview,
            "result": result,
            "response_text": text,
        },
        meta={"audit_db": mask_path(store.path)},
    )


def _cancel_operation(*, operation_id: str | None, request: InboundRequest, store: InboundOperationStore) -> dict[str, Any]:
    operation_id, operation, operation_resolution = _resolve_manual_trade_operation(
        operation_id=operation_id,
        request=request,
        store=store,
        allow_expired=True,
        action="取消",
    )
    result = {"operation_id": operation_id, "status": "cancelled"}
    store.mark_cancelled(operation_id, result=result)
    text = f"交易记录已取消，未写入账本。\ncommand_id: {operation_id}"
    return build_response(
        tool_name="inbound.manual_trade",
        ok=True,
        data={"operation_id": operation_id, **operation_resolution, "operation_type": operation.get("operation_type"), "status": "cancelled", "result": result, "response_text": text},
        meta={"audit_db": mask_path(store.path)},
    )


def _update_operation(*, operation_id: str | None, updates: dict[str, Any], request: InboundRequest, store: InboundOperationStore) -> dict[str, Any]:
    operation_id, operation, operation_resolution = _resolve_manual_trade_operation(
        operation_id=operation_id,
        request=request,
        store=store,
        allow_expired=False,
        action="修改",
    )
    patch = _normalize_manual_trade_patch(str(operation.get("operation_type") or ""), updates)
    payload = _apply_manual_trade_patch(dict(operation["payload"]), patch)
    preview = _preview_operation(payload)
    payload = _payload_with_preview_locked_values(payload, preview)
    preview = _preview_operation(payload)
    payload_hash = hash_operation_payload(payload)
    updated = store.update_preview(operation_id, payload_hash=payload_hash, payload=payload, preview=preview)
    text = render_manual_trade_response("updated", operation_id, payload, preview=preview, expires_at=str(updated.get("expires_at") or ""))
    if patch:
        text += "\n" + _format_patch_summary(patch)
    return build_response(
        tool_name="inbound.manual_trade",
        ok=True,
        data={
            "operation_id": operation_id,
            **operation_resolution,
            "operation_type": payload["operation_type"],
            "status": "previewed",
            "updated_fields": sorted(patch.keys()),
            "patch": patch,
            "payload_hash": payload_hash,
            "payload": payload,
            "preview": preview,
            "expires_at": updated.get("expires_at"),
            "response_text": text,
        },
        meta={"audit_db": mask_path(store.path)},
    )


def _resolve_manual_trade_operation(
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
        operation_types=MANUAL_TRADE_OPERATION_TYPES,
        conversation_id=request.conversation_id,
        explicit_operation_id=operation_id,
        allow_expired=allow_expired,
    )
    details = _operation_resolution_details(resolution)
    status = str(resolution.get("status") or "")
    resolved_operation_id = str(resolution.get("operation_id") or operation_id or "").strip()
    operation_raw = resolution.get("operation")
    operation = cast(dict[str, Any], operation_raw) if isinstance(operation_raw, dict) else {}
    if status == "resolved" and resolved_operation_id and operation:
        return resolved_operation_id, operation, details
    if status == "expired":
        result = {"operation_id": resolved_operation_id, "status": "expired"}
        if resolved_operation_id:
            store.mark_expired(resolved_operation_id, result=result)
        raise AgentToolError(code="NEEDS_CLARIFICATION", message="这条交易记录确认已过期，未写入账本。", hint="请重新发送记录交易命令生成新的预览。", details={**result, **details})
    if status == "ambiguous":
        hint = (
            _candidate_hint("确认记录" if action == "确认" else "取消记录", details.get("candidate_operations"))
            if action in {"确认", "取消"}
            else _update_candidate_hint(details.get("candidate_operations"))
        )
        raise AgentToolError(
            code="NEEDS_CLARIFICATION",
            message=f"有多条待{action}的交易记录，请带 operation_id。",
            hint=hint,
            details=details,
        )
    if status == "none":
        raise AgentToolError(code="NEEDS_CLARIFICATION", message=f"没有可{action}的交易记录。", hint="请先发送记录交易命令生成预览。", details=details)
    if status == "forbidden":
        raise AgentToolError(code="PERMISSION_DENIED", message=f"只能由创建该预览的同一 sender/对话 {action}。", details=details)
    if status == "wrong_family":
        raise AgentToolError(code="INPUT_ERROR", message="这不是交易记录操作，不能用确认记录/取消记录处理。", details=details)
    if status == "invalid_status":
        current_status = str(operation.get("status") or "-")
        raise AgentToolError(code="INPUT_ERROR", message=f"这条交易记录不能再次{action}，当前状态：{current_status}。", details=details)
    raise AgentToolError(code="INPUT_ERROR", message="找不到待确认的交易记录。", hint="请检查 operation_id，或重新发送记录交易命令。", details=details)


def _normalize_manual_trade_patch(operation_type: str, updates: dict[str, Any]) -> dict[str, Any]:
    raw_patch = {
        _manual_trade_patch_target_key(operation_type, str(key).strip()): value
        for key, value in updates.items()
        if str(key).strip()
    }
    if operation_type == "manual_open":
        allowed = MANUAL_OPEN_UPDATE_FIELDS
    elif operation_type == "manual_close":
        allowed = MANUAL_CLOSE_UPDATE_FIELDS
    else:
        raise AgentToolError(code="INPUT_ERROR", message=f"unsupported operation_type for update: {operation_type}")
    disallowed = sorted(key for key in raw_patch if key not in allowed)
    if disallowed:
        labels = "、".join(disallowed)
        raise AgentToolError(code="NEEDS_CLARIFICATION", message=f"这条交易记录不能修改字段：{labels}。", hint="请取消后重新记录，或只修改当前预览支持的字段。")
    patch: dict[str, Any] = {}
    for key, value in raw_patch.items():
        patch[key] = _normalize_patch_value(key, value)
    if not patch:
        raise AgentToolError(code="NEEDS_CLARIFICATION", message="没有识别出要修改的交易字段。", hint="例如：premium 改成 2.35，或 合约数改成 2。")
    return patch


def _manual_trade_patch_target_key(operation_type: str, key: str) -> str:
    if operation_type == "manual_close" and key == "contracts":
        return "contracts_to_close"
    if operation_type == "manual_close" and key == "premium_per_share":
        return "close_price"
    return key


def _normalize_patch_value(field_name: str, value: Any) -> Any:
    if field_name in {"contracts", "contracts_to_close", "underlying_share_locked"}:
        return _positive_int(value, field_name)
    if field_name in {"strike", "multiplier", "premium_per_share", "close_price"}:
        return _positive_float(value, field_name)
    if field_name == "expiration_ymd":
        text = _required_text(value, field_name)
        if not re_match_date(text):
            raise AgentToolError(code="INPUT_ERROR", message="expiration_ymd must be YYYY-MM-DD")
        return text
    if field_name == "currency":
        return _required_text(value, field_name).upper()
    return _required_text(value, field_name)


def _apply_manual_trade_patch(payload: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    args = dict(out.get("arguments") or {})
    args.update(patch)
    out["arguments"] = args
    diagnostics = out.get("diagnostics")
    if isinstance(diagnostics, dict):
        out["diagnostics"] = {**diagnostics, "updated_fields": sorted(patch.keys())}
    else:
        out["diagnostics"] = {"updated_fields": sorted(patch.keys())}
    return out


def _operation_resolution_details(resolution: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation_resolution": resolution.get("operation_resolution"),
        "resolved_operation_id": resolution.get("operation_id"),
        "candidate_operations": resolution.get("candidate_operations") or [],
    }


def _candidate_hint(prefix: str, candidates: Any) -> str:
    rows = candidates if isinstance(candidates, list) else []
    candidate_lines = _candidate_summary_lines(rows, prefix=prefix)
    if not candidate_lines:
        return f"请回复：{prefix} <operation_id>"
    return "\n候选交易：\n" + "\n".join(candidate_lines)


def _update_candidate_hint(candidates: Any) -> str:
    rows = candidates if isinstance(candidates, list) else []
    candidate_lines = _candidate_summary_lines(rows, prefix="")
    if not candidate_lines:
        return "请在修改内容后带 operation_id，例如：premium 改成 2.35 <operation_id>"
    return "请在修改内容后带 operation_id，例如：premium 改成 2.35 <operation_id>\n候选交易：\n" + "\n".join(candidate_lines)


def _candidate_summary_lines(rows: list[Any], *, prefix: str) -> list[str]:
    lines: list[str] = []
    for idx, item_raw in enumerate(rows[:5], start=1):
        if not isinstance(item_raw, dict):
            continue
        operation_id = str(item_raw.get("operation_id") or "").strip()
        if not operation_id:
            continue
        summary = str(item_raw.get("summary") or item_raw.get("operation_type") or "-").strip()
        command = f"{prefix} {operation_id}".strip()
        if command:
            lines.append(f"{idx}. {operation_id} | {summary} | 回复：{command}")
        else:
            lines.append(f"{idx}. {operation_id} | {summary}")
    return lines


def _format_patch_summary(patch: dict[str, Any]) -> str:
    parts = []
    for key in sorted(patch):
        label = FIELD_LABELS.get(key, key)
        parts.append(f"{label}={patch[key]}")
    return "已修改：" + "，".join(parts)


def _manual_trade_raw_text(intent: InboundIntent, request: InboundRequest) -> str:
    return str(intent.arguments.get("raw_text") or request.text or "").strip()


def _load_runtime_config_for_request(request: InboundRequest) -> tuple[Any, dict[str, Any]]:
    return load_runtime_config(config_key=request.config_key or "us", config_path=request.config_path)


def _accounts_from_runtime_config(cfg: dict[str, Any]) -> list[str]:
    raw = cfg.get("accounts")
    if isinstance(raw, list):
        return [str(item).strip().lower() for item in raw if str(item).strip()]
    if isinstance(raw, tuple):
        return [str(item).strip().lower() for item in raw if str(item).strip()]
    return []


def _build_operation_payload(
    operation_type: str,
    arguments: dict[str, Any],
    *,
    request: InboundRequest,
    config_path: Any | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "schema_version": "1.0",
        "operation_type": operation_type,
        "arguments": dict(arguments),
        "config": {"config_key": request.config_key, "config_path": str(config_path) if config_path else request.config_path},
    }
    if diagnostics:
        payload["diagnostics"] = dict(diagnostics)
    return payload


def _payload_with_preview_locked_values(payload: dict[str, Any], preview: dict[str, Any]) -> dict[str, Any]:
    if payload.get("operation_type") != "manual_open":
        return payload
    command = preview.get("command")
    command_payload = _json_safe(command)
    if not isinstance(command_payload, dict):
        return payload
    opened_at_ms = command_payload.get("opened_at_ms")
    if opened_at_ms is None:
        return payload
    out = dict(payload)
    args = dict(out.get("arguments") or {})
    args["opened_at_ms"] = int(opened_at_ms)
    out["arguments"] = args
    return out


def _preview_operation(payload: dict[str, Any]) -> dict[str, Any]:
    _data_config, repo = _open_repo_for_payload(payload)
    args = dict(payload.get("arguments") or {})
    try:
        if payload.get("operation_type") == "manual_open":
            out = execute_manual_open(repo, dry_run=True, **args)
        elif payload.get("operation_type") == "manual_close":
            out = execute_manual_close(repo, dry_run=True, **args)
        else:
            raise AgentToolError(code="INPUT_ERROR", message=f"unsupported operation_type: {payload.get('operation_type')}")
    except ManualCloseMatchError as exc:
        raise _manual_close_error(exc) from exc
    except ValueError as exc:
        raise AgentToolError(code="INPUT_ERROR", message=str(exc)) from exc
    return _json_safe(out)


def _apply_operation(payload: dict[str, Any]) -> dict[str, Any]:
    _data_config, repo = _open_repo_for_payload(payload)
    args = dict(payload.get("arguments") or {})
    try:
        if payload.get("operation_type") == "manual_open":
            out = execute_manual_open(repo, dry_run=False, **args)
        elif payload.get("operation_type") == "manual_close":
            out = execute_manual_close(repo, dry_run=False, **args)
        else:
            raise AgentToolError(code="INPUT_ERROR", message=f"unsupported operation_type: {payload.get('operation_type')}")
    except ManualCloseMatchError as exc:
        raise _manual_close_error(exc) from exc
    except ValueError as exc:
        raise AgentToolError(code="INPUT_ERROR", message=str(exc)) from exc
    return _json_safe(out)


def _open_repo_for_payload(payload: dict[str, Any]) -> tuple[Any, Any]:
    raw_config = payload.get("config")
    config = cast(dict[str, Any], raw_config) if isinstance(raw_config, dict) else {}
    config_path, cfg = load_runtime_config(config_key=str(config.get("config_key") or "us"), config_path=config.get("config_path"))
    return open_position_ledger_from_runtime_config(base=repo_base(), cfg=cfg, config_path=config_path)


def _manual_open_args(args: dict[str, Any]) -> dict[str, Any]:
    required = ("account", "symbol", "option_type", "side", "contracts", "strike", "multiplier", "expiration_ymd", "premium_per_share")
    _require_fields(args, required, action="记录开仓")
    return {
        "broker": str(args.get("broker") or "富途"),
        "account": _required_text(args.get("account"), "account"),
        "symbol": _required_text(args.get("symbol"), "symbol"),
        "option_type": _required_text(args.get("option_type"), "option_type"),
        "side": _required_text(args.get("side"), "side"),
        "contracts": _positive_int(args.get("contracts"), "contracts"),
        "currency": _optional_text(args.get("currency")),
        "strike": _positive_float(args.get("strike"), "strike"),
        "multiplier": _positive_float(args.get("multiplier"), "multiplier"),
        "expiration_ymd": _required_text(args.get("expiration_ymd"), "expiration_ymd"),
        "premium_per_share": _positive_float(args.get("premium_per_share"), "premium_per_share"),
        "underlying_share_locked": _optional_positive_int(args.get("underlying_share_locked"), "underlying_share_locked"),
        "note": _optional_text(args.get("note")),
        "opened_at_ms": _optional_positive_int(args.get("opened_at_ms"), "opened_at_ms"),
    }


def _manual_close_args(args: dict[str, Any]) -> dict[str, Any]:
    _require_fields(args, ("contracts_to_close", "close_price"), action="记录平仓")
    if not str(args.get("record_id") or "").strip():
        _require_fields(args, ("account", "symbol", "option_type", "side", "strike", "expiration_ymd"), action="记录平仓")
    return {
        "record_id": _optional_text(args.get("record_id")),
        "broker": str(args.get("broker") or "富途"),
        "account": _optional_text(args.get("account")),
        "symbol": _optional_text(args.get("symbol")),
        "option_type": _optional_text(args.get("option_type")),
        "position_side": _optional_text(args.get("side") or args.get("position_side")),
        "strike": _optional_positive_float(args.get("strike"), "strike"),
        "expiration_ymd": _optional_text(args.get("expiration_ymd")),
        "contracts_to_close": _positive_int(args.get("contracts_to_close"), "contracts_to_close"),
        "close_price": _positive_float(args.get("close_price"), "close_price"),
        "close_reason": str(args.get("close_reason") or "manual_buy_to_close"),
        "as_of_ms": _optional_positive_int(args.get("as_of_ms"), "as_of_ms"),
    }


def hash_operation_payload(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def render_manual_trade_response(
    status: str,
    operation_id: str,
    payload: dict[str, Any],
    *,
    preview: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    expires_at: str | None = None,
) -> str:
    del result
    operation_type = str(payload.get("operation_type") or "")
    if status == "cancelled":
        return f"交易记录已取消，未写入账本。\ncommand_id: {operation_id}"
    raw_args = payload.get("arguments")
    args = cast(dict[str, Any], raw_args) if isinstance(raw_args, dict) else {}
    preview_map = preview if isinstance(preview, dict) else {}
    raw_fields = preview_map.get("fields")
    fields = cast(dict[str, Any], raw_fields) if isinstance(raw_fields, dict) else {}
    if operation_type == "manual_open":
        title = "交易记录预览已更新：开仓" if status == "updated" else ("交易记录预览：开仓" if status == "previewed" else "交易已写入 OM 本地账本：开仓")
        lines = [
            title,
            f"账户：{fields.get('account') or args.get('account') or '-'}",
            f"合约：{fields.get('symbol') or args.get('symbol') or '-'} {fields.get('expiration_ymd') or args.get('expiration_ymd') or '-'} {fields.get('strike') or args.get('strike') or '-'}",
            f"方向：{fields.get('side') or args.get('side') or '-'} {fields.get('option_type') or args.get('option_type') or '-'}",
            f"数量：{args.get('contracts') or '-'} 张",
        ]
    else:
        title = "交易记录预览已更新：平仓" if status == "updated" else ("交易记录预览：平仓" if status == "previewed" else "交易已写入 OM 本地账本：平仓")
        raw_match = preview_map.get("match")
        match = cast(dict[str, Any], raw_match) if isinstance(raw_match, dict) else {}
        preview_record_id = preview_map.get("record_id")
        record_id = str(match.get("record_id") or args.get("record_id") or preview_record_id or "")
        lines = [
            title,
            f"record_id：{record_id or '-'}",
            f"账户：{fields.get('account') or args.get('account') or '-'}",
            f"合约：{fields.get('symbol') or args.get('symbol') or '-'} {fields.get('expiration_ymd') or args.get('expiration_ymd') or '-'} {fields.get('strike') or args.get('strike') or '-'}",
            f"平仓数量：{args.get('contracts_to_close') or '-'} 张",
        ]
    if status in {"previewed", "updated"}:
        lines.extend(
            [
                "",
                "未写入账本。",
                "确认写入请回复：确认记录",
                "取消请回复：取消记录",
                f"operation_id：{operation_id}",
                f"如同时有多条待确认，请回复：确认记录 {operation_id}",
            ]
        )
        if expires_at:
            lines.append("有效期：10 分钟。")
    else:
        lines.append(f"command_id：{operation_id}")
    return "\n".join(str(line) for line in lines)


def _manual_close_error(exc: ManualCloseMatchError) -> AgentToolError:
    if exc.code == "multiple_matches":
        lines = ["找到多条可匹配持仓，请指定 record_id。"]
        for idx, row in enumerate(exc.candidates[:10], start=1):
            lines.append(
                f"{idx}. {row.get('record_id')} | {row.get('account')} | {row.get('symbol')} | {row.get('side')} {row.get('option_type')} | exp {row.get('expiration_ymd') or '-'} | strike {row.get('strike') if row.get('strike') is not None else '-'} | open {row.get('contracts_open')}"
            )
        lines.append("请回复：记录平仓 record_id=<上面的 record_id> <张数>张 close <价格>")
        return AgentToolError(code="NEEDS_CLARIFICATION", message="\n".join(lines), details={"selector": exc.selector, "candidates": exc.candidates, "match_error_code": exc.code})
    return AgentToolError(code="NEEDS_CLARIFICATION", message=str(exc), details={"selector": exc.selector, "candidates": exc.candidates, "match_error_code": exc.code})


def _require_fields(args: dict[str, Any], keys: tuple[str, ...], *, action: str) -> None:
    missing = [key for key in keys if args.get(key) in (None, "")]
    if missing:
        raise AgentToolError(code="NEEDS_CLARIFICATION", message=f"{action}缺少字段：" + "、".join(missing), hint="示例：记录开仓 sy 0700.HK short put strike 450 exp 2026-05-28 6张 premium 2.35 multiplier 100")


def _required_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise AgentToolError(code="NEEDS_CLARIFICATION", message=f"{field_name} is required")
    return text


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _positive_int(value: Any, field_name: str) -> int:
    try:
        parsed = int(str(value).strip())
    except Exception:
        raise AgentToolError(code="INPUT_ERROR", message=f"{field_name} must be an integer") from None
    if parsed <= 0:
        raise AgentToolError(code="INPUT_ERROR", message=f"{field_name} must be > 0")
    return parsed


def _optional_positive_int(value: Any, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    return _positive_int(value, field_name)


def _positive_float(value: Any, field_name: str) -> float:
    try:
        parsed = float(str(value).strip())
    except Exception:
        raise AgentToolError(code="INPUT_ERROR", message=f"{field_name} must be numeric") from None
    if parsed <= 0:
        raise AgentToolError(code="INPUT_ERROR", message=f"{field_name} must be > 0")
    return float(parsed)


def _optional_positive_float(value: Any, field_name: str) -> float | None:
    if value in (None, ""):
        return None
    return _positive_float(value, field_name)


def re_match_date(text: str) -> bool:
    return re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])", str(text or "").strip()) is not None


def _json_safe(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value
