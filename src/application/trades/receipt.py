from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable

from domain.domain.multi_tick import resolve_notification_route_from_config
from src.application.notification_delivery_route import resolve_notification_delivery_route
from src.application.trade_time_format import format_trade_time_beijing
from src.application.notification_delivery_adapter import select_notification_delivery_adapter


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
        normalized = _normalize_delivery(send_result, normalize_fn=resolved_normalize_fn)
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
        return {"should_send": bool(cfg.get("notify_unresolved", True)), "reason": "unresolved"}
    if status == "failed":
        return {"should_send": bool(cfg.get("notify_failed", True)), "reason": "failed"}
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
    applied = status == "applied"
    title = "[已记录] 成交已写入 option_positions" if applied else "[未记录] 成交未写入 option_positions"
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
    reason = str(result.get("reason") or "").strip() or "-"

    lines = [
        title,
        "",
        f"账户：{account}",
        f"动作：{action}",
        f"标的：{symbol}",
    ]
    contract_parts = [part for part in (expiration, strike, _option_type_text(option_type)) if part not in (None, "")]
    if contract_parts:
        lines.append(f"合约：{' '.join(str(part) for part in contract_parts)}")
    if contracts not in (None, ""):
        lines.append(f"数量：{contracts} 张")
    if price not in (None, ""):
        lines.append(f"成交价：{price}")
    if trade_time:
        lines.append(f"成交时间：{trade_time}")
    lines.append(f"状态：{'已记录' if applied else '未记录'}")
    lines.append(f"原因：{reason}")
    lines.append(f"deal_id：{deal_id}")
    return "\n".join(lines)


def _normalize_delivery(send_result: Any, *, normalize_fn: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    if isinstance(send_result, dict) and ("delivery_confirmed" in send_result or "command_ok" in send_result):
        return dict(send_result)
    try:
        return normalize_fn(send_result=getattr(send_result, "raw", send_result))
    except TypeError:
        return normalize_fn(
            returncode=int(getattr(send_result, "returncode", 0) or 0),
            stdout=str(getattr(send_result, "stdout", "") or ""),
            stderr=str(getattr(send_result, "stderr", "") or ""),
        )


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


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
