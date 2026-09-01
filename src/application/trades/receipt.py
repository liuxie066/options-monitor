from __future__ import annotations

import subprocess
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, cast

from domain.domain.multi_tick import resolve_notification_route_from_config
from src.application.notification_delivery_route import resolve_notification_delivery_route
from src.application.trade_time_format import format_trade_time_beijing
from src.application.notification_delivery_adapter import (
    normalize_notification_delivery_result,
    select_notification_delivery_adapter,
)
from src.application.notification_shells import render_receipt
from src.application.trades.deal_identity import broker_deal_key
from src.application.trades.lifecycle_outbox import (
    BATCH_RENDERER_VERSION,
    build_notification_batch_route,
)


MAX_LIFECYCLE_BATCH_DISPLAY_ITEMS = 12


def send_trade_intake_receipt(
    *,
    base: Path,
    config: dict[str, Any] | None,
    receipt_config: dict[str, Any] | None,
    apply_changes: bool,
    state: dict[str, Any] | None,
    deal: Any,
    result: dict[str, Any],
    payload: dict[str, Any] | None = None,
    send_fn: Callable[..., Any] | None = None,
    normalize_fn: Callable[..., dict[str, Any]] | None = None,
    route_resolver: Callable[..., dict[str, Any]] = resolve_notification_route_from_config,
    adapter_selector: Callable[[Any], Any] = select_notification_delivery_adapter,
) -> dict[str, Any]:
    cfg = dict(receipt_config or {})
    decision = decide_trade_intake_receipt(
        receipt_config=cfg,
        apply_changes=apply_changes,
        state=state,
        deal_id=(
            str(broker_deal_key(deal) or "").strip()
            or _deal_id(deal, result, payload)
        ),
        result=result,
    )
    if not decision["should_send"]:
        return {
            "enabled": bool(cfg.get("enabled", True)),
            "status": "skipped",
            "reason": decision["reason"],
            "delivery_confirmed": False,
            "message_id": None,
        }

    route = resolve_notification_delivery_route(config=config or {}, route_resolver=route_resolver)
    provider = route.get("provider")
    channel = route.get("channel")
    target = route.get("target")
    if not str(target or "").strip():
        return {
            "enabled": True,
            "status": "skipped",
            "reason": "skipped_no_route",
            "provider": provider,
            "channel": channel,
            "target_set": False,
            "delivery_confirmed": False,
            "message_id": None,
        }

    message = build_trade_intake_receipt_message(deal=deal, result=result, payload=payload)
    try:
        if send_fn is None or normalize_fn is None:
            adapter = adapter_selector(provider)
            resolved_send_fn = send_fn or adapter.send_fn
            resolved_normalize_fn = normalize_fn or adapter.normalize_fn
        else:
            resolved_send_fn = send_fn
            resolved_normalize_fn = normalize_fn
        send_result = resolved_send_fn(
            base=base,
            channel=str(channel),
            target=str(target),
            message=message,
            notifications=route.get("notifications") or {},
        )
        normalized = normalize_notification_delivery_result(send_result, normalize_fn=resolved_normalize_fn)
    except subprocess.TimeoutExpired as exc:
        normalized = {
            "ok": False,
            "command_ok": False,
            "delivery_confirmed": False,
            "returncode": 124,
            "message": f"TimeoutExpired: {exc}",
            "error_code": "SEND_TIMEOUT",
        }
    except Exception as exc:
        normalized = {
            "ok": False,
            "command_ok": False,
            "delivery_confirmed": False,
            "returncode": 1,
            "message": f"{type(exc).__name__}: {exc}",
            "error_code": "SEND_EXCEPTION",
        }

    message_id = _optional_str(normalized.get("message_id"))
    command_ok = bool(normalized.get("command_ok") or normalized.get("ok"))
    delivery_confirmed = bool(normalized.get("delivery_confirmed") or (normalized.get("ok") and message_id))
    status = "sent" if delivery_confirmed else ("unconfirmed" if command_ok else "failed")
    return {
        "enabled": True,
        "status": status,
        "reason": decision["reason"],
        "provider": provider,
        "channel": channel,
        "target_set": True,
        "delivery_confirmed": delivery_confirmed,
        "message_id": message_id,
        "command_ok": command_ok,
        "returncode": int(normalized.get("returncode") or (0 if command_ok else 1)),
        "error_code": normalized.get("error_code"),
        "message_len": len(message),
        "send_message": _optional_str(normalized.get("message")),
    }


def build_trade_lifecycle_notification_message(
    payload: dict[str, Any],
) -> str:
    frozen = dict(payload or {})
    transition = str(
        frozen.get("transition_type") or ""
    ).strip().lower()
    reason = str(frozen.get("close_reason") or "").strip().lower()
    reason_text = {
        "trade_close": "主动交易平仓",
        "assignment": "被指派",
        "exercise": "已行权",
        "expiration_no_settlement": "到期未行权",
        "expire_close": "到期未行权",
    }.get(reason, reason or "待确认")
    status_text = {
        "option_leg_closed": "⏳ 期权腿已平仓",
        "resolution_confirmed": "✅ 平仓结果已确认",
        "needs_review": "⚠️ 平仓原因待复核",
        "conflict": "⚠️ 平仓证据冲突",
        "resolution_corrected": "✅ 平仓结果已更正",
    }.get(transition, "期权平仓状态更新")
    fields: list[tuple[str, object]] = [
        ("状态", status_text),
        ("标的", frozen.get("symbol") or "-"),
    ]
    contract = " ".join(
        str(item)
        for item in (
            frozen.get("expiration_ymd"),
            frozen.get("strike"),
            _option_type_text(_optional_str(frozen.get("option_type"))),
        )
        if item not in (None, "")
    )
    if contract:
        fields.append(("合约", contract))
    if transition == "resolution_confirmed":
        fields.append(("原因", reason_text))
    total_contracts = frozen.get("total_contracts")
    if total_contracts is None:
        total_contracts = sum(
            int(item.get("contracts") or 0)
            for item in list(frozen.get("allocations") or [])
            if isinstance(item, dict)
        )
    if total_contracts:
        fields.append(("数量", f"{total_contracts} 张"))
    reason_codes = [
        str(item)
        for item in list(frozen.get("reason_codes") or [])
        if str(item or "").strip()
    ]
    if reason_codes:
        fields.append(("诊断", ", ".join(reason_codes)))
    fields.append(
        (
            "案件",
            f"`{str(frozen.get('case_id') or '').strip()}`",
        )
    )
    return render_receipt(
        account=str(frozen.get("account") or "-"),
        receipt_type="期权平仓",
        status=status_text,
        fields=fields,
    )


def _batch_member_payload(member: dict[str, Any]) -> dict[str, Any]:
    return (
        dict(member.get("payload") or {})
        if isinstance(member.get("payload"), dict)
        else {}
    )


def _batch_member_transition(member: dict[str, Any]) -> str:
    payload = _batch_member_payload(member)
    return str(
        member.get("transition_type")
        or payload.get("transition_type")
        or ""
    ).strip().lower()


def _lifecycle_transition_priority(transition: str) -> int:
    return {
        "conflict": 5,
        "needs_review": 4,
        "resolution_corrected": 3,
        "resolution_confirmed": 2,
        "option_leg_closed": 1,
    }.get(str(transition or "").strip().lower(), 0)


def _lifecycle_transition_text(transition: str) -> str:
    return {
        "conflict": "⚠️ 证据冲突",
        "needs_review": "⚠️ 原因待复核",
        "resolution_corrected": "✅ 结果已更正",
        "resolution_confirmed": "✅ 结果已确认",
        "option_leg_closed": "⏳ 期权腿已平仓",
    }.get(str(transition or "").strip().lower(), "状态更新")


def _batch_representatives(
    members: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for index, member in enumerate(members):
        payload = _batch_member_payload(member)
        case_id = str(
            member.get("case_id") or payload.get("case_id") or ""
        ).strip()
        group_key = case_id or str(
            member.get("outbox_id") or f"missing-{index}"
        )
        transition = _batch_member_transition(member)
        rank = (
            int(member.get("resolution_revision") or 0),
            int(member.get("created_at_ms") or 0),
            _lifecycle_transition_priority(transition),
            str(member.get("outbox_id") or ""),
        )
        previous = selected.get(group_key)
        if previous is None or rank > previous["_representative_rank"]:
            selected[group_key] = {
                **member,
                "_representative_rank": rank,
                "_case_group_key": group_key,
            }
    representatives = list(selected.values())
    representatives.sort(
        key=lambda member: (
            -_lifecycle_transition_priority(
                _batch_member_transition(member)
            ),
            str(
                _batch_member_payload(member).get("account") or ""
            ).strip().lower(),
            str(
                _batch_member_payload(member).get("symbol") or ""
            ).strip().upper(),
            str(
                _batch_member_payload(member).get("expiration_ymd")
                or ""
            ),
            str(_batch_member_payload(member).get("strike") or ""),
            str(member.get("_case_group_key") or ""),
            str(member.get("outbox_id") or ""),
        )
    )
    return representatives


def build_trade_lifecycle_notification_batch_message(
    payload: dict[str, Any],
) -> str:
    frozen = dict(payload or {})
    raw_members = frozen.get("members")
    if not isinstance(raw_members, list) or not raw_members:
        raise ValueError("lifecycle notification batch has no members")
    members = [
        dict(item)
        if isinstance(item, dict)
        else {
            "outbox_id": f"invalid-{index}",
            "case_id": f"invalid-{index}",
            "payload": {},
        }
        for index, item in enumerate(raw_members)
    ]
    representatives = _batch_representatives(members)
    if len(representatives) == 1:
        return build_trade_lifecycle_notification_message(
            _batch_member_payload(representatives[0])
        )
    accounts = sorted(
        {
            str(_batch_member_payload(member).get("account") or "-")
            .strip()
            .lower()
            or "-"
            for member in representatives
        }
    )
    highest_transition = (
        _batch_member_transition(representatives[0])
        if representatives
        else ""
    )
    rows: list[str] = []
    for index, member in enumerate(
        representatives[:MAX_LIFECYCLE_BATCH_DISPLAY_ITEMS],
        start=1,
    ):
        item = _batch_member_payload(member)
        transition = _batch_member_transition(member)
        contract = " ".join(
            str(value)
            for value in (
                item.get("expiration_ymd"),
                item.get("strike"),
                _option_type_text(_optional_str(item.get("option_type"))),
            )
            if value not in (None, "")
        )
        rows.append(
            " · ".join(
                (
                    f"{index}.",
                    _lifecycle_transition_text(transition),
                    str(item.get("account") or "-"),
                    str(item.get("symbol") or "-"),
                    contract or "合约待确认",
                    f"`{str(item.get('case_id') or member.get('case_id') or '-').strip() or '-'}`",
                )
            )
        )
    remaining = max(
        0,
        len(representatives) - MAX_LIFECYCLE_BATCH_DISPLAY_ITEMS,
    )
    if remaining:
        rows.append(f"另有 {remaining} 个案件未展开")
    return render_receipt(
        account=" / ".join(accounts),
        receipt_type="期权平仓批次",
        status=_lifecycle_transition_text(highest_transition),
        fields=(
            (
                "范围",
                f"{len(members)} 条意图 · {len(representatives)} 个案件",
            ),
            ("批次", f"`{str(frozen.get('batch_id') or '-').strip()}`"),
        ),
        sections=(("明细", rows),),
    )


def resolve_trade_lifecycle_notification_batch_route(
    *,
    config: dict[str, Any] | None,
    route_resolver: Callable[..., dict[str, Any]] = (
        resolve_notification_route_from_config
    ),
) -> dict[str, Any]:
    route = resolve_notification_delivery_route(
        config=config or {},
        route_resolver=route_resolver,
    )
    target = str(route.get("target") or "").strip()
    provider = str(route.get("provider") or "").strip()
    channel = str(route.get("channel") or "").strip()
    if not target or not provider or not channel:
        return {**route, "route_available": False}
    return {
        **route,
        **build_notification_batch_route(
            provider=provider,
            channel=channel,
            target=target,
        ),
        "route_available": True,
    }


def classify_trade_lifecycle_delivery_result(
    normalized: dict[str, Any],
) -> dict[str, Any]:
    result = dict(normalized or {})
    delivery_confirmed = bool(
        result.get("delivery_confirmed")
        or (result.get("ok") and result.get("message_id"))
    )
    command_ok = bool(result.get("command_ok") or result.get("ok"))
    ambiguous = bool(
        result.get("ambiguous_send")
        or result.get("duplicate_risk")
        or (
            result.get("fallback_used")
            and not delivery_confirmed
        )
    )
    http_status = result.get("http_status")
    provider_code = result.get("provider_response_code")
    if provider_code is None:
        provider_code = result.get("feishu_code")
    provider_rejected = bool(
        not ambiguous
        and isinstance(http_status, int)
        and (
            400 <= http_status <= 499
            or (
                200 <= http_status <= 299
                and provider_code not in (None, 0, "0")
            )
        )
    )
    pre_io_failure = bool(
        result.get("explicit_pre_acceptance_failure")
        or (
            result.get("local_error_code")
            and not result.get("http_attempts")
            and http_status is None
        )
    )
    if delivery_confirmed:
        outcome = "confirmed"
    elif ambiguous:
        outcome = "unknown"
    elif provider_rejected or pre_io_failure:
        outcome = "explicit_failed"
    elif command_ok:
        outcome = "accepted"
    else:
        outcome = "unknown"
    return {
        "outcome": outcome,
        "delivery_confirmed": delivery_confirmed,
        "command_ok": command_ok,
        "explicit_pre_acceptance_failure": (
            outcome == "explicit_failed"
        ),
        "classification_evidence": {
            "http_status": http_status,
            "provider_response_code": provider_code,
            "ambiguous_send": bool(result.get("ambiguous_send")),
            "duplicate_risk": bool(result.get("duplicate_risk")),
            "fallback_used": bool(result.get("fallback_used")),
            "local_error_code": result.get("local_error_code"),
            "idempotency_key": result.get("idempotency_key"),
            "effective_idempotency_key": result.get(
                "effective_idempotency_key"
            ),
            "http_attempt_count": len(
                result.get("http_attempts")
                if isinstance(result.get("http_attempts"), list)
                else []
            ),
        },
    }


def send_trade_lifecycle_outbox_payload(
    *,
    base: Path,
    config: dict[str, Any] | None,
    receipt_config: dict[str, Any] | None,
    payload: dict[str, Any],
    send_fn: Callable[..., Any] | None = None,
    normalize_fn: Callable[..., dict[str, Any]] | None = None,
    route_resolver: Callable[..., dict[str, Any]] = (
        resolve_notification_route_from_config
    ),
    adapter_selector: Callable[[Any], Any] = (
        select_notification_delivery_adapter
    ),
) -> dict[str, Any]:
    cfg = dict(receipt_config or {})
    if cfg.get("enabled", True) is False:
        return {
            "status": "explicit_failed",
            "explicit_pre_acceptance_failure": True,
            "error": "trade receipt delivery is disabled",
            "classification_evidence": {
                "preflight": "receipt_disabled"
            },
        }
    route = resolve_trade_lifecycle_notification_batch_route(
        config=config or {},
        route_resolver=route_resolver,
    )
    target = route.get("target")
    if not bool(route.get("route_available")):
        return {
            "status": "explicit_failed",
            "explicit_pre_acceptance_failure": True,
            "error": "notification route is unavailable",
            "classification_evidence": {
                "preflight": "route_unavailable"
            },
        }
    is_batch = (
        str(payload.get("schema_version") or "").strip()
        == BATCH_RENDERER_VERSION
        and isinstance(payload.get("members"), list)
    )
    batch_id = str(payload.get("batch_id") or "").strip()
    if is_batch:
        frozen_route = (
            dict(payload.get("route") or {})
            if isinstance(payload.get("route"), dict)
            else {}
        )
        mismatch = any(
            str(frozen_route.get(key) or "").strip()
            != str(route.get(key) or "").strip()
            for key in (
                "provider",
                "channel",
                "target_fingerprint",
                "route_fingerprint",
            )
        )
        if not batch_id or mismatch:
            return {
                "status": "explicit_failed",
                "explicit_pre_acceptance_failure": True,
                "error": (
                    "notification batch route changed before delivery"
                    if mismatch
                    else "notification batch identity is unavailable"
                ),
                "batch_id": batch_id or None,
                "classification_evidence": {
                    "preflight": (
                        "route_fingerprint_mismatch"
                        if mismatch
                        else "batch_identity_unavailable"
                    ),
                    "resolved_route_fingerprint": route.get(
                        "route_fingerprint"
                    ),
                    "frozen_route_fingerprint": frozen_route.get(
                        "route_fingerprint"
                    ),
                },
            }
    if send_fn is None or normalize_fn is None:
        try:
            adapter = adapter_selector(route.get("provider"))
        except ValueError as exc:
            return {
                "status": "explicit_failed",
                "explicit_pre_acceptance_failure": True,
                "error": f"{type(exc).__name__}: {exc}",
                "batch_id": batch_id or None,
                "classification_evidence": {
                    "preflight": "adapter_unavailable"
                },
            }
        resolved_send_fn = send_fn or adapter.send_fn
        resolved_normalize_fn = normalize_fn or adapter.normalize_fn
    else:
        resolved_send_fn = send_fn
        resolved_normalize_fn = normalize_fn
    message = (
        build_trade_lifecycle_notification_batch_message(payload)
        if is_batch
        else build_trade_lifecycle_notification_message(payload)
    )
    send_kwargs = {
        "base": base,
        "channel": str(route.get("channel") or ""),
        "target": str(target),
        "message": message,
        "notifications": route.get("notifications") or {},
    }
    if is_batch:
        send_kwargs["idempotency_key"] = batch_id
    send_result = resolved_send_fn(
        **send_kwargs,
    )
    normalized = normalize_notification_delivery_result(
        send_result,
        normalize_fn=resolved_normalize_fn,
    )
    message_id = _optional_str(normalized.get("message_id"))
    classification = classify_trade_lifecycle_delivery_result(normalized)
    delivery_confirmed = bool(classification["delivery_confirmed"])
    command_ok = bool(classification["command_ok"])
    outcome = str(classification["outcome"])
    return {
        "status": outcome,
        "delivery_confirmed": delivery_confirmed,
        "explicit_pre_acceptance_failure": bool(
            classification["explicit_pre_acceptance_failure"]
        ),
        "message_id": message_id,
        "command_ok": command_ok,
        "error_code": normalized.get("error_code"),
        "send_message": _optional_str(normalized.get("message")),
        "message_len": len(message),
        "batch_id": batch_id or None,
        "transport_idempotency_key": (
            batch_id if is_batch else None
        ),
        "classification_evidence": classification[
            "classification_evidence"
        ],
    }


def decide_trade_intake_receipt(
    *,
    receipt_config: dict[str, Any] | None,
    apply_changes: bool,
    state: dict[str, Any] | None,
    deal_id: str | None,
    result: dict[str, Any],
) -> dict[str, Any]:
    cfg = dict(receipt_config or {})
    if not apply_changes:
        return {"should_send": False, "reason": "skipped_dry_run"}
    if cfg.get("enabled", True) is False:
        return {"should_send": False, "reason": "skipped_disabled"}

    status = str(result.get("status") or "").strip().lower()
    reason = str(result.get("reason") or "").strip().lower()
    if status == "applied":
        return {"should_send": bool(cfg.get("notify_applied", True)), "reason": "applied"}
    if status == "unresolved":
        if not bool(cfg.get("notify_unresolved", True)):
            return {"should_send": False, "reason": "unresolved_disabled"}
        if _receipt_delivered(state, deal_id):
            return {"should_send": False, "reason": "skipped_unresolved_already_notified"}
        if _receipt_delivery_is_ambiguous(state, deal_id):
            return {
                "should_send": False,
                "reason": "skipped_unresolved_delivery_unknown",
            }
        if _receipt_needs_retry(state, deal_id):
            return {"should_send": True, "reason": "unresolved_retry_unconfirmed_receipt"}
        return {"should_send": True, "reason": "unresolved"}
    if status == "failed":
        if not bool(cfg.get("notify_failed", True)):
            return {"should_send": False, "reason": "failed_disabled"}
        if _receipt_delivered(state, deal_id):
            return {"should_send": False, "reason": "skipped_failed_already_notified"}
        if _receipt_delivery_is_ambiguous(state, deal_id):
            return {
                "should_send": False,
                "reason": "skipped_failed_delivery_unknown",
            }
        if _receipt_needs_retry(state, deal_id):
            return {"should_send": True, "reason": "failed_retry_unconfirmed_receipt"}
        return {"should_send": True, "reason": "failed"}
    if status == "skipped" and reason == "not_option_deal":
        return {"should_send": False, "reason": "skipped_not_option_deal"}
    if status == "skipped" and reason == "duplicate_deal_id":
        if bool(cfg.get("notify_duplicate", False)):
            return {"should_send": True, "reason": "duplicate"}
        if _receipt_delivery_is_ambiguous(state, deal_id):
            return {
                "should_send": False,
                "reason": "skipped_duplicate_delivery_unknown",
            }
        if bool(cfg.get("retry_unconfirmed_duplicate", True)) and _receipt_needs_retry(state, deal_id):
            return {"should_send": True, "reason": "duplicate_retry_unconfirmed_receipt"}
        return {"should_send": False, "reason": "skipped_duplicate"}
    return {"should_send": False, "reason": f"skipped_status:{status or 'unknown'}"}


def build_trade_intake_receipt_message(
    *,
    deal: Any,
    result: dict[str, Any],
    payload: dict[str, Any] | None = None,
) -> str:
    status = str(result.get("status") or "").strip().lower()
    reason = str(result.get("reason") or "").strip() or "-"
    applied = status == "applied"
    diagnostics_raw = result.get("diagnostics")
    diagnostics = cast(dict[str, Any], diagnostics_raw) if isinstance(diagnostics_raw, dict) else {}
    needs_lot_confirmation = status == "unresolved" and reason == "ambiguous_assigned_stock_sale"
    if status == "failed" and reason == "projection_verification_failed":
        status_text = "❌ 写入异常"
    elif needs_lot_confirmation:
        status_text = "⚠️ 待确认"
    elif applied:
        status_text = "✅ 已完成"
    else:
        status_text = "❌ 未记录"

    account = _value("account", deal, result, payload) or "-"
    symbol = _value("symbol", deal, result, payload) or "-"
    action = _action_text(deal, result, payload)
    option_type = _value("option_type", deal, result, payload)
    expiration = _value("expiration_ymd", deal, result, payload) or _value("expiration", deal, result, payload)
    strike = _value("strike", deal, result, payload)
    contracts = _value("contracts", deal, result, payload) or _value("qty", deal, result, payload)
    price = _value("price", deal, result, payload)
    trade_time = format_trade_time_beijing(_trade_time_ms(deal, result, payload))
    deal_id = _deal_id(deal, result, payload) or "-"
    ledger_store_raw = result.get("ledger_store")
    ledger_store = cast(dict[str, Any], ledger_store_raw) if isinstance(ledger_store_raw, dict) else {}
    projection_status = str(result.get("projection_status") or "").strip()
    verification_raw = diagnostics.get("post_write_projection_verification")
    verification = cast(dict[str, Any], verification_raw) if isinstance(verification_raw, dict) else {}
    checks_raw = verification.get("checks")
    checks = checks_raw if isinstance(checks_raw, list) else []
    first_check = cast(dict[str, Any], checks[0]) if checks and isinstance(checks[0], dict) else {}

    fields: list[tuple[str, object]] = [("动作", action), ("标的", symbol)]
    contract_parts = [part for part in (expiration, strike, _option_type_text(option_type)) if part not in (None, "")]
    if contract_parts:
        fields.append(("合约", " ".join(str(part) for part in contract_parts)))
    if contracts not in (None, ""):
        fields.append(("数量", f"{contracts} 张"))
    if price not in (None, ""):
        fields.append(("成交", price))
    funds = _premium_cashflow_text(deal, result, payload)
    if funds:
        fields.append(("资金", funds))
    if trade_time:
        fields.append(("时间", trade_time))
    if projection_status:
        fields.append(("投影", projection_status))
    if first_check:
        fields.append(
            (
                "目标持仓",
                f"{first_check.get('contracts_open_before')} → {first_check.get('actual_contracts_open_after')}"
                f" · 预期 {first_check.get('expected_contracts_open_after')}",
            )
        )
    if ledger_store:
        fields.append(("账本", ledger_store.get("sqlite_path") or "-"))
    if _matching_auto_combo_adoption(result):
        fields.append(("组合", "✅ 已自动归入 Combo Yield（Funding Put + Participation Call）"))
    elif _combo_yield_relation_pending(diagnostics):
        fields.append(("组合", "关系待确认；未提供 pair_intent_id，当前按单腿记录，未自动归入 Combo Yield 组。"))
    fields.append(("诊断", reason))
    sections: list[tuple[str, list[str]]] = []
    if needs_lot_confirmation:
        candidate_lines = _assigned_stock_candidate_lines(diagnostics)
        if candidate_lines:
            fields.append(("说明", "需要确认卖出对应的 assigned-stock lot；确认前不会自动写入。"))
            rows = [line.replace("：", "｜", 1) for line in candidate_lines]
            rows.append("下一步｜回复“选择 A”")
            rows.append(f"编号｜`{deal_id}`")
            sections.append(("可选批次", rows))
    if not sections:
        fields.append(("编号", f"`{deal_id}`"))
    return render_receipt(
        account=account,
        receipt_type="成交",
        status=status_text,
        fields=fields,
        sections=sections,
    )


def _combo_yield_relation_pending(diagnostics: dict[str, Any]) -> bool:
    for key in ("combo_yield_enrichment", "position_effect_inference"):
        item = diagnostics.get(key)
        if isinstance(item, dict) and bool(item.get("combination_relation_pending")):
            return True
    return False


def _matching_auto_combo_adoption(result: dict[str, Any]) -> bool:
    event_ids = {
        str(item.get("event_id") or "").strip()
        for item in result.get("operations") or []
        if isinstance(item, dict)
    }
    reconciliation = result.get("combo_reconciliation")
    if not event_ids or not isinstance(reconciliation, dict):
        return False
    for adoption in reconciliation.get("auto_adoptions") or []:
        if not isinstance(adoption, dict) or adoption.get("status") not in {
            "adopted",
            "already_confirmed",
        }:
            continue
        inference = adoption.get("inference")
        if isinstance(inference, dict) and event_ids & {
            str(inference.get("put_open_event_id") or "").strip(),
            str(inference.get("call_open_event_id") or "").strip(),
        }:
            return True
    return False


def _receipt_needs_retry(state: dict[str, Any] | None, deal_id: str | None) -> bool:
    receipt = _stored_receipt(state, deal_id)
    if receipt is None:
        return False
    if not receipt:
        return True
    if bool(receipt.get("delivery_confirmed")):
        return False
    status = str(receipt.get("status") or "").strip().lower()
    if status in {"unconfirmed", "outbox_managed"}:
        return False
    if bool(receipt.get("ambiguous_send")):
        return False
    return status == "failed"


def _receipt_delivery_is_ambiguous(
    state: dict[str, Any] | None,
    deal_id: str | None,
) -> bool:
    receipt = _stored_receipt(state, deal_id)
    if not receipt or bool(receipt.get("delivery_confirmed")):
        return False
    status = str(receipt.get("status") or "").strip().lower()
    return (
        status in {"unconfirmed", "outbox_managed"}
        or bool(receipt.get("command_ok"))
        or bool(receipt.get("ambiguous_send"))
    )


def _stored_receipt(
    state: dict[str, Any] | None,
    deal_id: str | None,
) -> dict[str, Any] | None:
    key = str(deal_id or "").strip()
    if not key or not isinstance(state, dict):
        return None
    for bucket_name in ("processed_deal_ids", "failed_deal_ids", "unresolved_deal_ids"):
        bucket = state.get(bucket_name)
        if not isinstance(bucket, dict):
            continue
        item = bucket.get(key)
        if not isinstance(item, dict):
            continue
        receipt = item.get("receipt")
        if not isinstance(receipt, dict):
            return {}
        return dict(receipt)
    return None


def _receipt_delivered(state: dict[str, Any] | None, deal_id: str | None) -> bool:
    receipt = _stored_receipt(state, deal_id)
    return bool(receipt and receipt.get("delivery_confirmed"))


def _deal_id(deal: Any, result: dict[str, Any], payload: dict[str, Any] | None) -> str | None:
    return (
        _optional_str(result.get("deal_id"))
        or _optional_str(getattr(deal, "deal_id", None))
        or _optional_str((payload or {}).get("deal_id"))
        or _optional_str((payload or {}).get("dealID"))
        or _optional_str((payload or {}).get("id"))
    )


def _trade_time_ms(deal: Any, result: dict[str, Any], payload: dict[str, Any] | None) -> Any:
    for value in (
        result.get("trade_time_ms"),
        getattr(deal, "trade_time_ms", None),
        (payload or {}).get("trade_time_ms"),
        (payload or {}).get("fill_time_ms"),
    ):
        if value not in (None, ""):
            return value
    return None


def _value(name: str, deal: Any, result: dict[str, Any], payload: dict[str, Any] | None) -> str | None:
    if name == "account":
        return _optional_str(result.get("account")) or _optional_str(getattr(deal, "internal_account", None))
    return _optional_str(getattr(deal, name, None)) or _optional_str((payload or {}).get(name))


def _action_text(deal: Any, result: dict[str, Any], payload: dict[str, Any] | None) -> str:
    effect = _optional_str(result.get("action")) or _optional_str(getattr(deal, "position_effect", None)) or _optional_str((payload or {}).get("position_effect"))
    side = _optional_str(getattr(deal, "side", None)) or _optional_str((payload or {}).get("side")) or _optional_str((payload or {}).get("trd_side"))
    effect_text = {"open": "开仓", "close": "平仓"}.get(str(effect or "").lower(), str(effect or "-"))
    side_text = {"sell": "卖出", "buy": "买入"}.get(str(side or "").lower(), str(side or ""))
    return " / ".join(part for part in (effect_text, side_text) if part)


def _option_type_text(value: str | None) -> str | None:
    raw = str(value or "").strip().lower()
    if raw == "put":
        return "Put"
    if raw == "call":
        return "Call"
    return _optional_str(value)


def _premium_cashflow_text(deal: Any, result: dict[str, Any], payload: dict[str, Any] | None) -> str | None:
    if str(_value("option_type", deal, result, payload) or "").lower() not in {"put", "call"}:
        return None
    side = str(_value("side", deal, result, payload) or "").lower()
    values = (
        _value("price", deal, result, payload),
        _value("contracts", deal, result, payload) or _value("qty", deal, result, payload),
        _value("multiplier", deal, result, payload),
    )
    currency = str(_value("currency", deal, result, payload) or "").upper()
    if side not in {"buy", "sell"} or not currency or any(value is None for value in values):
        return None
    try:
        amount = Decimal(values[0]) * Decimal(values[1]) * Decimal(values[2])
    except (InvalidOperation, TypeError, ValueError):
        return None
    direction = "流入" if side == "sell" else "流出"
    return f"权利金毛{direction} {currency} {amount:,.2f}"


def _assigned_stock_candidate_lines(diagnostics: dict[str, Any]) -> list[str]:
    candidates_raw = diagnostics.get("candidates")
    candidates = candidates_raw if isinstance(candidates_raw, list) else []
    lines: list[str] = []
    for idx, item in enumerate(candidates[:10]):
        if not isinstance(item, dict):
            continue
        label = chr(ord("A") + idx)
        symbol = _optional_str(item.get("symbol")) or "-"
        currency = _optional_str(item.get("currency")) or "-"
        shares = _optional_str(item.get("shares_remaining")) or "-"
        cost = _optional_str(item.get("stock_cost_per_share")) or _optional_str(item.get("assignment_price"))
        opened = format_trade_time_beijing(item.get("opened_at_ms"))
        lot_id = _optional_str(item.get("stock_lot_id")) or "-"
        parts = [f"{label}：{symbol} {currency}", f"剩余 {shares} 股"]
        if cost:
            parts.append(f"成本 {cost}/股")
        if opened:
            parts.append(f"指派时间 {opened}")
        parts.append(f"lot_id={lot_id}")
        reject_reasons_raw = item.get("reject_reasons")
        reject_reasons = [str(reason) for reason in reject_reasons_raw] if isinstance(reject_reasons_raw, list) else []
        if reject_reasons:
            parts.append(f"不符合：{', '.join(reject_reasons)}")
        lines.append("；".join(parts))
    return lines


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
