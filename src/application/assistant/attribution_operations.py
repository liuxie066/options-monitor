from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from src.application.agent_tool_contracts import AgentToolError, build_response
from src.application.assistant.contracts import AssistantRequest, ControlCommand
from src.application.assistant.operation_lifecycle import build_previewed_operation_response
from src.application.assistant.operation_policy import enforce_trade_write_allowed
from src.application.assistant.renderer import render_attribution_preview
from src.application.assistant.operation_signature import hash_operation_payload, verify_operation_signature
from src.application.assistant.operation_store import InboundOperationStore, operation_is_expired
from src.application.ledger.api import (ledger_resource_identity, open_wheel_activation_repository,
    read_trade_attribution_facts, record_trade_ordinary_attribution, with_sqlite_repo_writer_lock)
from src.application.trades.attribution import (attribution_runtime, build_trade_attribution_view,
    read_attribution_combo_evidence, apply_trade_attribution, read_trade_attribution_snapshot)
from src.application.trades.account_mapping import combo_reconciliation_mode_for_account
from src.application.futu_quote_routing import runtime_config_market
from src.application.wheel.capacity import observe_trade_attribution_capacity


def _fact(repo: Any, *, account: str, execution_key: str) -> dict[str, Any]:
    matches = [row for row in read_trade_attribution_facts(repo, account=account) if row["execution_key"] == execution_key]
    if len(matches) != 1:
        raise AgentToolError(code="INPUT_ERROR", message="成交身份无法唯一定位，请重新查询。")
    return matches[0]


def _response(operation_id: str, status: str, result: dict[str, Any]) -> dict[str, Any]:
    target = "普通单腿" if result.get("status") == "ordinary" else "Wheel" if result.get("wheel_branch_id") else "Combo"
    text = {"applied": f"已确认归属：{target}；成交金额和数量不变。", "cancelled": "已取消本次预览，未改变成交归属。",
            "failed": "本次归属未执行，请重新查询并预览。", "expired": "预览已过期，请重新预览。"}.get(status, "归属结果待核实。")
    return build_response(tool_name="inbound.attribution", ok=status in {"applied", "cancelled"},
                          data={"operation_id": operation_id, "operation_type": "trade_attribution",
                                "status": status, "result": result, "response_text": text})


def _strategy_context(repo: Any, *, config: dict[str, Any], authority: dict[str, Any], account: str) -> dict[str, Any]:
    # Provider I/O is outside both the audit claim and the ledger writer lock.
    observation = observe_trade_attribution_capacity(config=config, account=account)
    market = runtime_config_market(config).lower()
    rows = read_trade_attribution_snapshot(repo, account=account, market=market)
    evidence = read_attribution_combo_evidence(rows, account=account, runtime_root=Path(authority["runtime_root"]), now_ms=int(time.time() * 1000))
    mode = combo_reconciliation_mode_for_account(config, account=account)
    view = build_trade_attribution_view(rows, config=config, account=account, market=market, now_ms=int(time.time() * 1000),
        combo_evidence=evidence, capacity_observation=observation, combo_mode=mode)
    return {"config": config, "market": market, "combo_evidence": evidence, "capacity_observation": observation,
            "combo_mode": mode, "view": view}


def _decision_matches(repo: Any, *, payload: dict[str, Any], fact: dict[str, Any]) -> bool:
    if payload["action"] == "ordinary":
        return fact["status"] == "ordinary" and fact["origin"] == "manual"
    facts = {row["lot_id"]: row for row in read_trade_attribution_facts(repo, account=payload["account"])}
    members = payload.get("members") or []
    if {row["lot_id"] for row in members} != set(payload["member_ids"]):
        return False
    for member in members:
        row = facts.get(member["lot_id"], {})
        target = "wheel:" + row["wheel_branch_id"] if row.get("wheel_branch_id") else "combo:" + str(row.get("strategy_group_id"))
        if (any(row.get(key) != value for key, value in member.items()) or row.get("status") != "linked"
                or row.get("origin") != "manual" or target != payload["candidate_id"]):
            return False
    return bool(members)


def handle_attribution_operation(intent: ControlCommand, request: AssistantRequest, *,
                                 command_id: str, store: InboundOperationStore) -> dict[str, Any]:
    policy = enforce_trade_write_allowed(channel=request.channel, sender_id=request.sender_id)
    conversation = str(request.conversation_id or "").strip()
    if not conversation or conversation != request.conversation_id:
        raise AgentToolError(code="PERMISSION_DENIED", message="归属确认需要可信且完整的当前对话身份。")
    if intent.intent_name == "attribution_preview":
        args = dict(intent.arguments)
        account = str(args.get("account") or "").strip().lower()
        repo, config, authority, mapping = attribution_runtime(config_key=request.config_key, config_path=request.config_path, account=account)
        fact = _fact(repo, account=account, execution_key=str(args.get("execution_key") or ""))
        action = args.get("action")
        candidate = None
        context = None
        if action == "ordinary":
            if not fact["ordinary_previewable"]:
                raise AgentToolError(code="NEEDS_CLARIFICATION", message="该成交不满足普通单腿确认条件。", details={"reason_codes": fact["reason_codes"]})
        elif action in {"wheel", "combo"}:
            if fact["status"] == "linked":
                raise AgentToolError(code="NEEDS_CLARIFICATION", message="该成交已有归属，请先核对现有关系。")
            target = str(args.get("target_id") or "").strip()
            if not target:
                raise AgentToolError(code="NEEDS_CLARIFICATION", message="请先查询待归属成交，选择具体的目标 ID。")
            candidate_id = target if target.startswith(action + ":") else action + ":" + target
            context = _strategy_context(repo, config=config, authority=authority, account=account)
            view = context.pop("view")
            fact = next(row for row in view["rows"] if row["execution_key"] == fact["execution_key"])
            candidate = next((row for row in fact["candidates"] if row["candidate_id"] == candidate_id), None)
            if candidate is None:
                raise AgentToolError(code="NEEDS_CLARIFICATION", message="所选目标已不可用，请重新查询。")
            apply_trade_attribution(open_wheel_activation_repository(repo.db_path), account=account,
                execution_key=fact["execution_key"], candidate_id=candidate_id, expected_input_hash=fact["input_hash"],
                request_id=f"control:{command_id}", actor=f"{request.channel}:{request.sender_id}", manual=True,
                apply_changes=False, **context)
        else:
            raise AgentToolError(code="INPUT_ERROR", message="未知的归属选项。")
        ref = fact["broker_account_ref"]
        if (ref["broker_id"] != "futu" or mapping["physical_account_ids"] != [ref["external_account_id"]]
                or mapping["environment"] != ref["environment"]):
            raise AgentToolError(code="PERMISSION_DENIED", message="成交账户身份与当前配置不符。")
        payload = {"operation_type": "trade_attribution", "account": account, "action": action,
                   "execution_key": fact["execution_key"], "open_event_id": fact["open_event_id"],
                   "member_ids": candidate["member_lot_ids"] if candidate else [fact["lot_id"]], "broker_account_ref": ref, "authority_scope": authority,
                   "input_hash": fact["input_hash"], "policy_version": fact["policy_version"],
                   "actor": f"{request.channel}:{request.sender_id}", "request_id": f"control:{command_id}",
                   "economics": {key: fact[key] for key in ("contract_key", "contracts", "price", "currency", "multiplier")}}
        if candidate:
            payload.update(candidate_id=candidate["candidate_id"],
                branch_generation_hash=candidate.get("branch_generation_hash"),
                members=[{key: row[key] for key in ("lot_id", "execution_key", "open_event_id", "broker_account_ref")}
                         for row in view["rows"] if row["lot_id"] in payload["member_ids"]])
        target_label = "普通单腿" if action == "ordinary" else ("Wheel 批次 " if action == "wheel" else "Combo Yield ") + candidate["candidate_id"].split(":", 1)[1]
        preview_members = [row for row in view["rows"] if row["lot_id"] in payload["member_ids"]] if candidate else [fact]
        return build_previewed_operation_response(
            tool_name="inbound.attribution", operation_id=command_id, request=request, store=store,
            payload=payload, preview={"current": fact, "members": preview_members, "target": target_label, "economics_unchanged": True},
            ttl_seconds=policy.confirm_ttl_seconds,
            response_text=render_attribution_preview,
        )
    operation_id = str(intent.arguments.get("operation_id") or "").strip()
    operation = store.get(operation_id) or {}
    if (operation.get("operation_type") != "trade_attribution" or operation.get("channel") != request.channel
            or operation.get("sender_id") != request.sender_id or operation.get("conversation_id") != conversation):
        raise AgentToolError(code="PERMISSION_DENIED", message="只能在原渠道、原发送人和原对话确认此预览。")
    payload = operation.get("payload") or {}
    if hash_operation_payload(payload) != operation.get("payload_hash"):
        raise AgentToolError(code="PERMISSION_DENIED", message="归属预览内容已变化，请重新预览。")
    verify_operation_signature(operation)
    repo, config, authority, _mapping = attribution_runtime(config_key=request.config_key, config_path=request.config_path, account=payload["account"])
    if authority != payload.get("authority_scope"):
        raise AgentToolError(code="PERMISSION_DENIED", message="配置、账户映射或账本资源已变化，请在原范围核对结果。")
    if intent.intent_name == "attribution_cancel":
        store.mark_cancelled(operation_id, result={"status": "cancelled"}, expected_payload_hash=operation["payload_hash"])
        return _finish_attribution_operation(repo, store=store, operation=operation, apply_changes=False)
    if intent.intent_name != "attribution_confirm":
        raise AgentToolError(code="INPUT_ERROR", message="未知的归属操作。")
    context = None
    claimed = False
    if operation["status"] == "previewed":
        if operation_is_expired(operation):
            store.mark_expired(operation_id, result={"status": "expired"})
            return _response(operation_id, "expired", {})
        if payload["action"] != "ordinary":
            context = _strategy_context(repo, config=config, authority=authority, account=payload["account"])
            context.pop("view")
        claimed = store.mark_confirmed(operation_id, expected_payload_hash=operation["payload_hash"])
    return _finish_attribution_operation(repo, store=store, operation=operation, apply_changes=claimed, context=context)


def _finish_attribution_operation(repo: Any, *, store: InboundOperationStore,
                                  operation: dict[str, Any], apply_changes: bool,
                                  context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Serialize recovery and the worker, including audit state and ledger readback."""
    operation_id = operation["operation_id"]
    payload = operation["payload"]
    active = open_wheel_activation_repository(repo.db_path)
    with with_sqlite_repo_writer_lock(active):
        if ledger_resource_identity(active) != payload["authority_scope"]["ledger"]:
            raise AgentToolError(code="PERMISSION_DENIED", message="账本文件已变化，请重新预览。")
        current = store.get(operation_id) or {}
        if current.get("payload_hash") != operation["payload_hash"]:
            raise AgentToolError(code="PERMISSION_DENIED", message="归属请求身份已变化。")
        fact = _fact(active, account=payload["account"], execution_key=payload["execution_key"])
        if (fact["open_event_id"] != payload["open_event_id"] or fact["lot_id"] not in payload["member_ids"]
                or fact["broker_account_ref"] != payload["broker_account_ref"]):
            raise AgentToolError(code="PERMISSION_DENIED", message="成交绑定身份已变化。")
        if _decision_matches(active, payload=payload, fact=fact):
            if current.get("status") in {"confirmed", "running", "applied", "failed"}:
                store.mark_applied(operation_id, result=fact)
                return _response(operation_id, "applied", fact)
        if current.get("status") == "applied":
            return _response(operation_id, "conflict", {**fact, "reason_codes": [*fact["reason_codes"], "attribution_effect_changed"]})
        if current.get("status") not in {"confirmed", "running"}:
            return _response(operation_id, str(current.get("status") or "unknown"), current.get("result") or {})
        if not apply_changes:
            # A worker waiting for this lock must re-read the failed state before writing.
            store.mark_failed(operation_id, result={"status": "failed", "reason": "attribution_not_committed"})
            return _response(operation_id, "failed", fact)
        try:
            if payload["action"] == "ordinary":
                result = record_trade_ordinary_attribution(active, account=payload["account"], execution_key=payload["execution_key"],
                    expected_input_hash=payload["input_hash"], request_id=payload["request_id"], actor=payload["actor"],
                    now_ms=int(time.time() * 1000), apply_changes=True)
            elif context is not None:
                result = apply_trade_attribution(active, account=payload["account"], execution_key=payload["execution_key"],
                    candidate_id=payload["candidate_id"], expected_input_hash=payload["input_hash"],
                    request_id=payload["request_id"], actor=payload["actor"], manual=True, **context)
            else:
                raise ValueError("claimed operation has no fresh confirmation evidence")
            if not _decision_matches(active, payload=payload, fact=result):
                raise ValueError("manual attribution readback differs from preview")
        except Exception:
            # Both readback and the delayed writer are serialized by the ledger lock.
            result = _fact(active, account=payload["account"], execution_key=payload["execution_key"])
            if not _decision_matches(active, payload=payload, fact=result):
                store.mark_failed(operation_id, result={"status": "failed", "reason": "attribution_not_committed"})
                return _response(operation_id, "failed", result)
        store.mark_applied(operation_id, result=result)
        return _response(operation_id, "applied", result)


def recover_attribution_operations(*, config_key: str | None, config_path: str | None,
                                   store: InboundOperationStore, stop_event: Any = None,
                                   cursor: str = "") -> dict[str, Any]:
    """Read back claimed operations; never renew authorization or apply a ledger effect."""
    if not store.path.is_file():
        return {"checked": 0, "applied": 0, "failed": 0, "unresolved": 0, "next_cursor": ""}
    summary = {"checked": 0, "applied": 0, "failed": 0, "unresolved": 0, "next_cursor": cursor}
    operations = store.list_attribution_recovery_operations(after=cursor)
    for operation in operations:
        if stop_event is not None and stop_event.is_set():
            break
        summary["next_cursor"] = operation["operation_id"]
        payload = operation.get("payload") or {}
        try:
            if (not operation.get("conversation_id") or hash_operation_payload(payload) != operation.get("payload_hash")):
                raise ValueError("invalid attribution recovery identity")
            verify_operation_signature(operation)
            repo, _config, authority, _mapping = attribution_runtime(
                config_key=config_key, config_path=config_path, account=payload["account"])
            if authority != payload.get("authority_scope"):
                continue  # Another market/store's worker owns this operation.
            result = _finish_attribution_operation(repo, store=store, operation=operation, apply_changes=False)
            status = result["data"]["status"]
            summary[status if status in {"applied", "failed"} else "unresolved"] += 1
        except Exception:
            # Unreadable evidence is not proof of a failed commit.
            summary["unresolved"] += 1
        summary["checked"] += 1
    else:
        if len(operations) < 100:
            summary["next_cursor"] = ""
    return summary
