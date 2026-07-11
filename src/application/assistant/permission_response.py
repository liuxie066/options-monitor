from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.contracts import AssistantRequest, ControlCommand
from src.application.assistant.operation_lifecycle import resolve_pending_operation_or_raise
from src.application.assistant.operation_status_text import operation_candidate_hint
from src.application.assistant.operation_store import InboundOperationStore


_OPERATION_ID_RE = re.compile(r"\bin_[A-Za-z0-9_.:-]+\b")


@dataclass(frozen=True)
class _PermissionFamily:
    key: str
    operation_types: frozenset[str]
    confirm_intent: str
    cancel_intent: str
    subject: str
    aliases: frozenset[str]
    retry_hint: str
    wrong_family_message: str
    not_found_message: str


_FAMILIES: tuple[_PermissionFamily, ...] = (
    _PermissionFamily(
        key="trade",
        operation_types=frozenset({"manual_open", "manual_close", "manual_assignment", "manual_expiry"}),
        confirm_intent="manual_trade_confirm",
        cancel_intent="manual_trade_cancel",
        subject="交易记录",
        aliases=frozenset({"记录", "交易", "trade", "record", "records", "manual"}),
        retry_hint="请先生成交易记录预览，或使用 /pending 查看待确认操作。",
        wrong_family_message="这不是交易记录操作，不能用确认记录/取消记录处理。",
        not_found_message="找不到待确认的交易记录。",
    ),
    _PermissionFamily(
        key="symbol",
        operation_types=frozenset({"symbol_add", "symbol_edit", "symbol_remove"}),
        confirm_intent="symbol_confirm",
        cancel_intent="symbol_cancel",
        subject="监控标的变更",
        aliases=frozenset({"监控", "标的", "symbol", "symbols", "monitor"}),
        retry_hint="请先生成监控标的变更预览，或使用 /pending 查看待确认操作。",
        wrong_family_message="这不是监控标的变更，不能用确认监控/取消监控处理。",
        not_found_message="找不到待确认的监控标的变更。",
    ),
    _PermissionFamily(
        key="upgrade",
        operation_types=frozenset({"upgrade_now"}),
        confirm_intent="upgrade_confirm",
        cancel_intent="upgrade_cancel",
        subject="升级操作",
        aliases=frozenset({"升级", "upgrade"}),
        retry_hint="请先发送升级请求生成预览，或使用 /pending 查看待确认操作。",
        wrong_family_message="这不是升级操作，不能用确认升级/取消升级处理。",
        not_found_message="找不到待确认的升级操作。",
    ),
    _PermissionFamily(
        key="model",
        operation_types=frozenset({"model_use"}),
        confirm_intent="model_confirm",
        cancel_intent="model_cancel",
        subject="模型切换",
        aliases=frozenset({"模型", "model", "models"}),
        retry_hint="请先使用 /model use <name> 生成预览，或使用 /pending 查看待确认操作。",
        wrong_family_message="这不是模型切换操作，不能用确认模型/取消模型处理。",
        not_found_message="找不到待确认的模型切换操作。",
    ),
    _PermissionFamily(
        key="monitor-run",
        operation_types=frozenset({"monitor_run_now"}),
        confirm_intent="monitor_run_confirm",
        cancel_intent="monitor_run_cancel",
        subject="监控执行",
        aliases=frozenset({"运行监控", "跑监控", "执行监控", "tick", "monitor-run", "monitor_run", "run-monitor"}),
        retry_hint="请先生成监控执行预览，或使用 /pending 查看待确认操作。",
        wrong_family_message="这不是监控执行，不能用确认运行监控/取消运行监控处理。",
        not_found_message="找不到待确认的监控执行。",
    ),
)
_FAMILY_BY_OPERATION_TYPE = {
    operation_type: family
    for family in _FAMILIES
    for operation_type in family.operation_types
}


def parse_permission_response(
    text: str,
    *,
    request: AssistantRequest,
    store: InboundOperationStore,
) -> ControlCommand | None:
    parsed = _parse_permission_text(text)
    if parsed is None:
        return None
    action, family, operation_id = parsed
    resolved_family = family
    if resolved_family is None and operation_id:
        operation = store.get(operation_id) or {}
        operation_type = str(operation.get("operation_type") or "").strip()
        resolved_family = _FAMILY_BY_OPERATION_TYPE.get(operation_type)
        if resolved_family is None:
            raise AgentToolError(
                code="NEEDS_CLARIFICATION",
                message="找不到可确认或取消的待确认操作。",
                hint="请使用 /pending 查看当前待确认操作，或使用 /confirm <type> <operation_id>。",
                details={"operation_id": operation_id},
            )
    if resolved_family is None:
        resolved_family = _resolve_unique_family_for_bare_response(
            action=action,
            request=request,
            store=store,
        )
    resolved_operation_id = _resolve_operation_id(
        action=action,
        family=resolved_family,
        operation_id=operation_id,
        request=request,
        store=store,
    )
    intent_name = resolved_family.confirm_intent if action == "confirm" else resolved_family.cancel_intent
    return ControlCommand(
        intent_name=intent_name,
        arguments={
            "operation_id": resolved_operation_id,
            "operation_resolution": "permission_response",
        },
        source="permission_response",
        confidence=1.0,
    )


def _parse_permission_text(text: str) -> tuple[str, _PermissionFamily | None, str | None] | None:
    raw = str(text or "").strip()
    if not raw or raw.startswith("/"):
        return None
    operation_id = _extract_operation_id(raw)
    without_id = _OPERATION_ID_RE.sub("", raw).strip()
    compact = re.sub(r"[\s,，。.!！?？:：;；]+", "", without_id).lower()
    spaced = re.sub(r"\s+", " ", without_id).strip().lower()

    for action, cn, en in (("confirm", "确认", "confirm"), ("cancel", "取消", "cancel")):
        if compact in {cn, f"{cn}执行"} or spaced == en:
            return action, None, operation_id
        for family in _FAMILIES:
            for alias in family.aliases:
                alias_lower = alias.lower()
                if compact == f"{cn}{alias_lower}" or spaced == f"{en} {alias_lower}":
                    return action, family, operation_id
    return None


def _resolve_unique_family_for_bare_response(
    *,
    action: str,
    request: AssistantRequest,
    store: InboundOperationStore,
) -> _PermissionFamily:
    matches: list[_PermissionFamily] = []
    for family in _FAMILIES:
        operations = store.list_pending_operations(
            channel=request.channel,
            sender_id=request.sender_id,
            conversation_id=request.conversation_id,
            operation_types=family.operation_types,
            limit=2,
        )
        if operations:
            matches.append(family)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise AgentToolError(
            code="NEEDS_CLARIFICATION",
            message=f"有多类待{_action_label(action)}操作，请说明要确认或取消哪一类。",
            hint="请使用 /pending 查看当前待确认操作，或使用 /confirm <type> <operation_id>。",
            details={"families": [item.key for item in matches]},
        )
    raise AgentToolError(
        code="NEEDS_CLARIFICATION",
        message=f"没有可{_action_label(action)}的待确认操作。",
        hint="请使用 /pending 查看当前待确认操作。",
    )


def _resolve_operation_id(
    *,
    action: str,
    family: _PermissionFamily,
    operation_id: str | None,
    request: AssistantRequest,
    store: InboundOperationStore,
) -> str:
    if operation_id is None and family.key == "trade":
        pending = store.list_pending_operations(
            channel=request.channel,
            sender_id=request.sender_id,
            conversation_id=request.conversation_id,
            operation_types=family.operation_types,
        )
        command_ids = {str(item.get("command_id") or "") for item in pending}
        if len(pending) > 1 and {item.get("operation_type") for item in pending} == {"manual_expiry"} and len(command_ids) == 1:
            return command_ids.pop()
    resolved_operation_id, _operation, _resolution = resolve_pending_operation_or_raise(
        operation_id=operation_id,
        request=request,
        store=store,
        operation_types=family.operation_types,
        allow_expired=False,
        action=_action_label(action),
        subject=family.subject,
        expired_message=f"这条{family.subject}确认已过期，未执行。",
        expired_hint=family.retry_hint,
        none_hint=family.retry_hint,
        wrong_family_message=family.wrong_family_message,
        not_found_message=family.not_found_message,
        not_found_hint=family.retry_hint,
        candidate_hint=lambda action_label, candidates: operation_candidate_hint(
            _canonical_command(action=action, family=family),
            candidates,
            heading=f"候选{family.subject}",
        ),
    )
    return resolved_operation_id


def _canonical_command(*, action: str, family: _PermissionFamily) -> str:
    prefix = "/confirm" if action == "confirm" else "/cancel"
    return f"{prefix} {family.key}"


def _action_label(action: str) -> str:
    return "确认" if action == "confirm" else "取消"


def _extract_operation_id(text: str) -> str | None:
    match = _OPERATION_ID_RE.search(text)
    return match.group(0) if match else None


__all__ = ["parse_permission_response"]
