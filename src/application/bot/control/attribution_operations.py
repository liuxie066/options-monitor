from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from src.application.agent_tool_contracts import AgentToolError, build_response
from src.application.bot.control.contracts import BotInboundRequest, ControlCommand
from src.application.bot.control.operation_lifecycle import build_previewed_operation_response
from src.application.bot.control.operation_policy import enforce_trade_write_allowed
from src.application.bot.control.renderer import render_attribution_preview
from src.application.bot.control.operation_signature import hash_operation_payload, verify_operation_signature
from src.application.bot.control.operation_store import InboundOperationStore, operation_is_expired
from src.application.ledger.api import (ledger_resource_identity, open_wheel_activation_repository,
    read_trade_attribution_facts, read_trade_attribution_decision, with_sqlite_repo_writer_lock)
from src.application.trades.attribution import (attribution_runtime, build_trade_attribution_view,
    read_attribution_combo_evidence, apply_trade_attribution, read_trade_attribution_snapshot)
from src.application.trades.account_mapping import combo_reconciliation_mode_for_account
from src.application.futu_quote_routing import runtime_config_market, resolve_futu_quote_route
from src.application.futu_portfolio_context import infer_futu_portfolio_settings, resolve_futu_account_ids
from src.application.config_defaults import cash_snapshot_ttl_sec
from src.application.wheel.config import resolve_wheel_config
from src.application.wheel.capacity import observe_trade_attribution_capacity
from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.bot.control.command_parser import validate_attribution_batch
from domain.domain.wheel_call_allocation import parse_wheel_call_allocations


def _fact(repo: Any, *, account: str, execution_key: str) -> dict[str, Any]:
    matches = [row for row in read_trade_attribution_facts(repo, account=account) if row["execution_key"] == execution_key]
    if len(matches) != 1:
        raise AgentToolError(code="INPUT_ERROR", message="成交身份无法唯一定位，请重新查询。")
    return matches[0]


def _response(operation_id: str, status: str, result: dict[str, Any]) -> dict[str, Any]:
    target = "普通单腿" if result.get("status") == "ordinary" else "Wheel" if (
        result.get("wheel_branch_id") or result.get("wheel_call_allocations")) else "Combo"
    members = (result.get("decision") or {}).get("members") or []
    if len(members) > 1:
        target = f"{len(members)} 笔成交"
    text = {"applied": f"已确认归属：{target}；成交金额和数量不变。", "cancelled": "已取消本次预览，未改变成交归属。",
            "failed": "归属未完成，请查看失败原因后再处理。", "expired": "预览已过期，请重新预览。"}.get(status, "归属结果待核实。")
    return build_response(tool_name="inbound.attribution", ok=status in {"applied", "cancelled"},
                          data={"operation_id": operation_id, "operation_type": "trade_attribution",
                                "status": status, "result": result, "response_text": text})


def _render_complete_preview(operation: dict[str, Any], *, request: BotInboundRequest,
                             store: InboundOperationStore) -> str:
    text = render_attribution_preview(operation)
    limit = int((request.reply_context or {}).get("max_reply_chars") or 0)
    if limit > 0 and len(text) > limit:
        store.mark_cancelled(operation["operation_id"], result={"reason": "attribution_preview_too_large"},
                             expected_payload_hash=operation["payload_hash"])
        raise AgentToolError(code="INPUT_ERROR", message="完整归属预览超过渠道回复上限，请换用可展示全部成员的渠道；本次预览已取消。")
    return text


def _admission_config_hash(config: dict[str, Any], *, account: str) -> str:
    market = runtime_config_market(config).lower()
    wheel = resolve_wheel_config(config, account, market=market)
    portfolio = infer_futu_portfolio_settings(config, account=account)
    quote = resolve_futu_quote_route(config, market=market)
    return canonical_sha256({
        "market": market,
        "wheel": {key: wheel[key] for key in (
            "account_configured", "activation_descriptor", "policy_hash", "enabled_for_new_lifecycle")},
        "combo_mode": combo_reconciliation_mode_for_account(config, account=account),
        "cash_max_age_sec": cash_snapshot_ttl_sec(config),
        "physical_accounts": resolve_futu_account_ids(config, account=account),
        "portfolio": {key: portfolio.get(key) for key in ("host", "port", "trd_env")},
        "quote": {"status": quote.status, "host": quote.host, "port": quote.port},
    })


def _strategy_context(repo: Any, *, config: dict[str, Any], authority: dict[str, Any], account: str,
                      open_event_id: str | None) -> dict[str, Any]:
    # Provider I/O is outside both the audit claim and the ledger writer lock.
    observation = observe_trade_attribution_capacity(config=config, account=account, runtime_root=Path(authority["runtime_root"]))
    market = runtime_config_market(config).lower()
    rows = read_trade_attribution_snapshot(repo, account=account, market=market)
    evidence = read_attribution_combo_evidence(rows, account=account, runtime_root=Path(authority["runtime_root"]),
        now_ms=int(time.time() * 1000), focus_open_event_id=open_event_id)
    mode = combo_reconciliation_mode_for_account(config, account=account)
    view = build_trade_attribution_view(rows, config=config, account=account, market=market, now_ms=int(time.time() * 1000),
        combo_evidence=evidence, capacity_observation=observation, combo_mode=mode)
    return {"config": config, "market": market, "combo_evidence": evidence, "capacity_observation": observation,
            "combo_mode": mode, "view": view}


def _member_decision(member: dict[str, Any], *, contracts: int) -> dict[str, Any]:
    action = member.get("action")
    target = str(member.get("target_id") or "")
    allocations = member.get("wheel_call_allocations")
    branches = []
    if allocations is not None:
        parsed = parse_wheel_call_allocations(allocations)
        if sum(count for _stock, _branch, count in parsed) != contracts:
            raise ValueError("分配张数与原成交不一致")
        branches = [branch for _stock, branch, count in parsed for _ in range(count)]
    elif action == "wheel" and "," in target:
        branches = [value.removeprefix("wheel:") for value in target.split(",")]
        if any(not value or value.strip() != value for value in branches):
            raise ValueError("多分支 ID 无效")
    if action == "ordinary":
        if target or allocations is not None:
            raise ValueError("普通单腿不接受目标")
        candidate = "ordinary"
    elif action in {"wheel", "combo"} and (target or branches):
        candidate = ("wheel-multi:" + canonical_sha256(sorted(branches))[:24] if branches else
                     target if target.startswith(action + ":") else action + ":" + target)
    else:
        raise ValueError("请提供有效归属及目标 ID")
    return {"execution_key": member["execution_key"], "candidate_id": candidate, "wheel_branch_ids": branches,
            **({"wheel_call_allocations": allocations} if allocations is not None else {})}


def _target_label(plan: dict[str, Any]) -> str:
    allocations = plan["patch"].get("wheel_call_allocations")
    if allocations:
        return "；".join(f"Wheel 分支 {row['wheel_branch_id']}：{row['contracts']} 张（股票批次 {row['stock_lot_id']}）" for row in allocations)
    if plan["action"] == "ordinary":
        return "普通单腿"
    return ("Wheel 批次 " if plan["action"] == "wheel" else "Combo Yield ") + plan["candidate_id"].split(":", 1)[1]


def handle_attribution_operation(intent: ControlCommand, request: BotInboundRequest, *,
                                 command_id: str, store: InboundOperationStore) -> dict[str, Any]:
    policy = enforce_trade_write_allowed(channel=request.channel, sender_id=request.sender_id)
    conversation = str(request.conversation_id or "").strip()
    if not conversation or conversation != request.conversation_id:
        raise AgentToolError(code="PERMISSION_DENIED", message="归属确认需要可信且完整的当前对话身份。")
    if intent.intent_name == "attribution_preview":
        args = dict(intent.arguments)
        account = str(args.get("account") or "").strip().lower()
        batch = "members" in args or "conflict_event_ids" in args
        try:
            if batch:
                validate_attribution_batch({key: value for key, value in args.items() if key != "account"})
                members = args["members"]
            else:
                if set(args) - {"account", "execution_key", "action", "target_id"}:
                    raise ValueError("未知归属字段")
                members = [args]
        except (ValueError, KeyError, TypeError) as exc:
            raise AgentToolError(code="INPUT_ERROR", message=str(exc)) from exc
        repo, config, authority, mapping = attribution_runtime(config_key=request.config_key, config_path=request.config_path, account=account)
        context = _strategy_context(repo, config=config, authority=authority, account=account, open_event_id=None)
        view = context.pop("view")
        try:
            by_key = {row["execution_key"]: row for row in view["rows"]}
            decisions = [_member_decision(member, contracts=by_key[member["execution_key"]]["contracts"])
                         for member in members]
        except (ValueError, KeyError, TypeError) as exc:
            raise AgentToolError(code="INPUT_ERROR", message=f"成交身份或分配无效：{exc}") from exc
        primary = decisions[0]
        matches = [row for row in view["rows"] if row["execution_key"] == primary["execution_key"]]
        if len(matches) != 1:
            raise AgentToolError(code="INPUT_ERROR", message="成交身份无法唯一定位。")
        fact = matches[0]
        conflicts = (args["conflict_event_ids"] if batch else [key for key, value in view["conflict_statuses"].items()
            if not value["resolved"] and fact["execution_key"] in value["execution_keys"]])
        apply_args = {"account": account, "execution_key": fact["execution_key"], "candidate_id": primary["candidate_id"],
            "expected_input_hash": fact["input_hash"], "request_id": f"control:{command_id}",
            "actor": f"{request.channel}:{request.sender_id}", "manual": True,
            "wheel_branch_ids": tuple(primary["wheel_branch_ids"]), "conflict_event_ids": tuple(conflicts),
            "member_decisions": tuple(decisions) if batch else ()}
        try:
            planned = apply_trade_attribution(open_wheel_activation_repository(repo.db_path),
                apply_changes=False, **apply_args, **context)
        except ValueError as exc:
            raise AgentToolError(code="NEEDS_CLARIFICATION", message=f"归属预览未通过：{exc}。多成员冲突请使用 /attribute <账户> batch '<JSON>'。") from exc
        if not planned.get("members"):
            raise AgentToolError(code="NEEDS_CLARIFICATION", message="该成交已有归属，无待执行的决定。")
        plans = planned["members"]
        for plan in plans:
            ref = plan["fact"]["broker_account_ref"]
            if (ref["broker_id"] != "futu" or mapping["physical_account_ids"] != [ref["external_account_id"]]
                    or mapping["environment"] != ref["environment"]):
                raise AgentToolError(code="PERMISSION_DENIED", message="成交账户身份与当前配置不符。")
        payload = {"operation_type": "trade_attribution", "account": account, "action": "batch" if batch else args["action"],
            **apply_args, "authority_scope": authority, "market": context["market"],
            "input_hash": fact["input_hash"], "policy_version": fact["policy_version"],
            "open_event_id": fact["open_event_id"], "broker_account_ref": fact["broker_account_ref"],
            "member_ids": [plan["fact"]["lot_id"] for plan in plans], "members": plans,
            "request_content": planned["request_content"], "branch_generations": planned["branch_generations"]}
        if planned.get("wheel_call_allocations"):
            payload["wheel_call_allocations"] = planned["wheel_call_allocations"]
        remaining = [key for key, value in view["conflict_statuses"].items() if not value["resolved"] and key not in conflicts]
        return build_previewed_operation_response(tool_name="inbound.attribution", operation_id=command_id,
            request=request, store=store, payload=payload,
            preview={"current": fact, "members": [{**plan["fact"], "reason_codes": next(row["reason_codes"] for row in view["rows"] if row["lot_id"] == plan["fact"]["lot_id"])} for plan in plans],
                "targets": {plan["fact"]["lot_id"]: _target_label(plan) for plan in plans},
                "conflict_event_ids": conflicts, "remaining_conflict_event_ids": remaining, "economics_unchanged": True},
            ttl_seconds=policy.confirm_ttl_seconds,
            response_text=lambda operation: _render_complete_preview(operation, request=request, store=store))
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
        return _finish_attribution_operation(repo, store=store, operation=operation,
                                             apply_changes=False, cancel_requested=True)
    if intent.intent_name != "attribution_confirm":
        raise AgentToolError(code="INPUT_ERROR", message="未知的归属操作。")
    context, claimed = None, False
    if operation["status"] == "previewed":
        if operation_is_expired(operation):
            store.mark_expired(operation_id, result={"status": "expired"})
            return _response(operation_id, "expired", {})
        admission_config_hash = _admission_config_hash(config, account=payload["account"])
        context = _strategy_context(repo, config=config, authority=authority, account=payload["account"], open_event_id=None)
        context.pop("view")
        def before_commit() -> None:
            enforce_trade_write_allowed(channel=request.channel, sender_id=request.sender_id)
            current = store.get(operation_id) or {}
            if (current.get("status") not in {"confirmed", "running"}
                    or current.get("payload_hash") != operation["payload_hash"] or operation_is_expired(current)):
                raise ValueError("归属授权已取消、过期或改变")
            _repo, _config, live_authority, _mapping = attribution_runtime(
                config_key=request.config_key, config_path=request.config_path, account=payload["account"])
            if live_authority != authority:
                raise ValueError("归属配置或账户范围已改变")
            if _admission_config_hash(_config, account=payload["account"]) != admission_config_hash:
                raise ValueError("归属准入配置或策略已改变，请重新预览")
        context["before_commit"] = before_commit
        claimed = store.mark_confirmed(operation_id, expected_payload_hash=operation["payload_hash"])
    return _finish_attribution_operation(repo, store=store, operation=operation, apply_changes=claimed, context=context)


def _read_decision(repo: Any, payload: dict[str, Any]) -> dict[str, Any] | None:
    rows = read_trade_attribution_snapshot(repo, account=payload["account"], market=payload["market"])
    result = read_trade_attribution_decision(rows, account=payload["account"], request_id=payload["request_id"],
        request_content=payload["request_content"], now_ms=int(time.time() * 1000))
    if result is None:
        return None
    return {**_fact(repo, account=payload["account"], execution_key=payload["execution_key"]), **result}


def _finish_attribution_operation(repo: Any, *, store: InboundOperationStore,
                                  operation: dict[str, Any], apply_changes: bool,
                                  context: dict[str, Any] | None = None,
                                  cancel_requested: bool = False) -> dict[str, Any]:
    """Recover only a complete durable decision belonging to this signed request."""
    operation_id, payload = operation["operation_id"], operation["payload"]
    active = open_wheel_activation_repository(repo.db_path)
    with with_sqlite_repo_writer_lock(active):
        if ledger_resource_identity(active) != payload["authority_scope"]["ledger"]:
            raise AgentToolError(code="PERMISSION_DENIED", message="账本文件已变化，请重新预览。")
        current = store.get(operation_id) or {}
        if current.get("payload_hash") != operation["payload_hash"]:
            raise AgentToolError(code="PERMISSION_DENIED", message="归属请求身份已变化。")
        try:
            result = _read_decision(active, payload)
        except ValueError as exc:
            return _response(operation_id, "conflict", {"reason": str(exc), "reason_codes": ["attribution_effect_changed"]})
        if result is not None:
            store.mark_applied(operation_id, result=result)
            return _response(operation_id, "applied", result)
        if current.get("status") == "applied":
            return _response(operation_id, "conflict", {"reason": "attribution_proof_missing"})
        if cancel_requested and current.get("status") in {"previewed", "confirmed", "running"}:
            store.mark_cancelled(operation_id, result={"status": "cancelled"},
                                 expected_payload_hash=operation["payload_hash"],
                                 expected_statuses=("previewed", "confirmed", "running"))
            return _response(operation_id, "cancelled", {"status": "cancelled"})
        if current.get("status") not in {"confirmed", "running"}:
            return _response(operation_id, str(current.get("status") or "unknown"), current.get("result") or {})
        if not apply_changes:
            store.mark_failed(operation_id, result={"status": "failed", "reason": "attribution_not_committed"})
            return _response(operation_id, "failed", {})
        try:
            if context is None:
                raise ValueError("claimed operation has no fresh confirmation evidence")
            apply_trade_attribution(active, **{key: payload[key] for key in (
                "account", "execution_key", "candidate_id", "expected_input_hash", "request_id", "actor", "manual",
                "wheel_branch_ids", "conflict_event_ids", "member_decisions")}, **context)
            result = _read_decision(active, payload)
            if result is None:
                raise ValueError("manual attribution proof was not persisted")
        except Exception as exc:
            result = _read_decision(active, payload)
            if result is None:
                store.mark_failed(operation_id, result={"status": "failed", "reason": str(exc)})
                return _response(operation_id, str((store.get(operation_id) or {}).get("status") or "failed"), {"reason": str(exc)})
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
