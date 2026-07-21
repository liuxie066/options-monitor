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
        deal_id=_deal_id(deal, result, payload),
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
        if _receipt_needs_retry(state, deal_id):
            return {"should_send": True, "reason": "unresolved_retry_unconfirmed_receipt"}
        return {"should_send": True, "reason": "unresolved"}
    if status == "failed":
        if not bool(cfg.get("notify_failed", True)):
            return {"should_send": False, "reason": "failed_disabled"}
        if _receipt_delivered(state, deal_id):
            return {"should_send": False, "reason": "skipped_failed_already_notified"}
        if _receipt_needs_retry(state, deal_id):
            return {"should_send": True, "reason": "failed_retry_unconfirmed_receipt"}
        return {"should_send": True, "reason": "failed"}
    if status == "skipped" and reason == "not_option_deal":
        return {"should_send": False, "reason": "skipped_not_option_deal"}
    if status == "skipped" and reason == "duplicate_deal_id":
        if bool(cfg.get("notify_duplicate", False)):
            return {"should_send": True, "reason": "duplicate"}
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

    lines = [
        f"# OM · 成交回执 · {account}",
        "",
        f"状态｜{status_text}",
        f"动作｜{action}",
        f"标的｜{symbol}",
    ]
    contract_parts = [part for part in (expiration, strike, _option_type_text(option_type)) if part not in (None, "")]
    if contract_parts:
        lines.append(f"合约｜{' '.join(str(part) for part in contract_parts)}")
    if contracts not in (None, ""):
        lines.append(f"数量｜{contracts} 张")
    if price not in (None, ""):
        lines.append(f"成交｜{price}")
    funds = _premium_cashflow_text(deal, result, payload)
    if funds:
        lines.append(f"资金｜{funds}")
    if trade_time:
        lines.append(f"时间｜{trade_time}")
    if projection_status:
        lines.append(f"投影｜{projection_status}")
    if first_check:
        lines.append(
            "目标持仓｜"
            f"{first_check.get('contracts_open_before')} → {first_check.get('actual_contracts_open_after')}"
            f" · 预期 {first_check.get('expected_contracts_open_after')}"
        )
    if ledger_store:
        lines.append(f"账本｜{ledger_store.get('sqlite_path') or '-'}")
    if _combo_yield_relation_pending(diagnostics):
        lines.append("组合｜关系待确认；未提供 pair_intent_id，当前按单腿记录，未自动归入 Combo Yield 组。")
    lines.append(f"诊断｜{reason}")
    if needs_lot_confirmation:
        candidate_lines = _assigned_stock_candidate_lines(diagnostics)
        if candidate_lines:
            lines.extend(["说明｜需要确认卖出对应的 assigned-stock lot；确认前不会自动写入。", "", "## 可选批次"])
            lines.extend(line.replace("：", "｜", 1) for line in candidate_lines)
            lines.append("下一步｜回复“选择 A”")
    lines.append(f"编号｜`{deal_id}`")
    return "\n".join(lines)


def _combo_yield_relation_pending(diagnostics: dict[str, Any]) -> bool:
    for key in ("combo_yield_enrichment", "position_effect_inference"):
        item = diagnostics.get(key)
        if isinstance(item, dict) and bool(item.get("combination_relation_pending")):
            return True
    return False


def _receipt_needs_retry(state: dict[str, Any] | None, deal_id: str | None) -> bool:
    key = str(deal_id or "").strip()
    if not key or not isinstance(state, dict):
        return False
    for bucket_name in ("processed_deal_ids", "failed_deal_ids", "unresolved_deal_ids"):
        bucket = state.get(bucket_name)
        if not isinstance(bucket, dict):
            continue
        item = bucket.get(key)
        if not isinstance(item, dict):
            continue
        receipt = item.get("receipt")
        if not isinstance(receipt, dict):
            return True
        return not bool(receipt.get("delivery_confirmed"))
    return False


def _receipt_delivered(state: dict[str, Any] | None, deal_id: str | None) -> bool:
    key = str(deal_id or "").strip()
    if not key or not isinstance(state, dict):
        return False
    for bucket_name in ("processed_deal_ids", "failed_deal_ids", "unresolved_deal_ids"):
        bucket = state.get(bucket_name)
        if not isinstance(bucket, dict):
            continue
        item = bucket.get(key)
        if not isinstance(item, dict):
            continue
        receipt = item.get("receipt")
        return isinstance(receipt, dict) and bool(receipt.get("delivery_confirmed"))
    return False


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
