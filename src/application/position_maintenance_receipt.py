from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable

from domain.domain.multi_tick import resolve_notification_route_from_config
from src.infrastructure.external_services import select_notification_delivery_adapter


def resolve_auto_close_receipt_config(value: Any) -> dict[str, bool]:
    if value is None:
        src: dict[str, Any] = {}
    elif isinstance(value, dict):
        src = value
    else:
        raise ValueError("option_positions.auto_close.receipt must be an object")
    return {
        "enabled": _bool_from_config(src, "enabled", default=True),
        "notify_applied": _bool_from_config(src, "notify_applied", default=True),
        "notify_failed": _bool_from_config(src, "notify_failed", default=True),
        "notify_noop": _bool_from_config(src, "notify_noop", default=False),
        "notify_dry_run": _bool_from_config(src, "notify_dry_run", default=False),
    }


def resolve_auto_close_receipt_config_from_runtime_config(config: dict[str, Any] | None) -> dict[str, bool]:
    cfg = config if isinstance(config, dict) else {}
    option_positions = cfg.get("option_positions")
    option_positions = option_positions if isinstance(option_positions, dict) else {}
    auto_close = option_positions.get("auto_close")
    auto_close = auto_close if isinstance(auto_close, dict) else {}
    return resolve_auto_close_receipt_config(auto_close.get("receipt"))


def send_auto_close_receipt(
    *,
    base: Path,
    config: dict[str, Any] | None,
    receipt_config: dict[str, Any] | None,
    dry_run: bool,
    result: dict[str, Any],
    send_fn: Callable[..., Any] | None = None,
    normalize_fn: Callable[..., dict[str, Any]] | None = None,
    route_resolver: Callable[..., dict[str, Any]] = resolve_notification_route_from_config,
    adapter_selector: Callable[[Any], Any] = select_notification_delivery_adapter,
) -> dict[str, Any]:
    cfg = resolve_auto_close_receipt_config(receipt_config)
    decision = decide_auto_close_receipt(receipt_config=cfg, dry_run=dry_run, result=result)
    if not decision["should_send"]:
        return {
            "enabled": bool(cfg.get("enabled", True)),
            "status": "skipped",
            "reason": decision["reason"],
            "delivery_confirmed": False,
            "message_id": None,
        }

    route = route_resolver(config=config or {})
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

    message = build_auto_close_receipt_message(result=result, dry_run=dry_run)
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


def safe_send_auto_close_receipt(
    *,
    base: Path,
    config: dict[str, Any] | None,
    dry_run: bool,
    result: dict[str, Any],
) -> dict[str, Any]:
    try:
        receipt_config = resolve_auto_close_receipt_config_from_runtime_config(config)
        return send_auto_close_receipt(
            base=base,
            config=config,
            receipt_config=receipt_config,
            dry_run=dry_run,
            result=result,
        )
    except Exception as exc:
        return {
            "enabled": True,
            "status": "failed",
            "reason": "receipt_exception",
            "delivery_confirmed": False,
            "message_id": None,
            "error_code": "RECEIPT_EXCEPTION",
            "send_message": f"{type(exc).__name__}: {exc}",
        }


def decide_auto_close_receipt(
    *,
    receipt_config: dict[str, Any] | None,
    dry_run: bool,
    result: dict[str, Any],
) -> dict[str, Any]:
    cfg = resolve_auto_close_receipt_config(receipt_config)
    if cfg.get("enabled", True) is False:
        return {"should_send": False, "reason": "skipped_disabled"}

    errors = _errors(result)
    applied = _int_value(result.get("applied_closed"))
    candidates = _int_value(result.get("candidates_should_close"))
    mode = str(result.get("mode") or "").strip().lower()
    if mode == "skipped":
        reason = str(result.get("reason") or "skipped").strip() or "skipped"
        return {"should_send": False, "reason": f"skipped_{reason}"}

    if dry_run or mode == "dry_run":
        should_send = bool(cfg.get("notify_dry_run", False)) and (candidates > 0 or bool(errors))
        return {"should_send": should_send, "reason": "dry_run"}
    if errors:
        reason = "partial_failed" if applied > 0 else "failed"
        return {"should_send": bool(cfg.get("notify_failed", True)), "reason": reason}
    if applied > 0:
        return {"should_send": bool(cfg.get("notify_applied", True)), "reason": "applied"}
    return {"should_send": bool(cfg.get("notify_noop", False)), "reason": "noop"}


def build_auto_close_receipt_message(
    *,
    result: dict[str, Any],
    dry_run: bool,
) -> str:
    errors = _errors(result)
    applied = _int_value(result.get("applied_closed"))
    candidates = _int_value(result.get("candidates_should_close"))
    mode = str(result.get("mode") or "").strip().lower()

    if dry_run or mode == "dry_run":
        title = "[预览] 过期自动平仓未写入 option_positions"
        status_text = "预览"
    elif errors and applied > 0:
        title = "[未完全记录] 过期自动平仓部分写入 option_positions"
        status_text = "部分失败"
    elif errors:
        title = "[未记录] 过期自动平仓未写入 option_positions"
        status_text = "失败"
    elif applied > 0:
        title = "[已记录] 过期自动平仓已写入 option_positions"
        status_text = "已记录"
    else:
        title = "[无变更] 过期自动平仓未写入 option_positions"
        status_text = "无变更"

    lines = [
        title,
        "",
        f"账户：{_display(result.get('account'))}",
        f"券商：{_display(result.get('broker'))}",
        f"规则：到期 + {_display(result.get('grace_days'))} 天",
        f"状态：{status_text}",
        f"平仓：{applied}/{candidates}",
        f"错误：{len(errors)}",
    ]
    as_of = _optional_str(result.get("as_of_utc"))
    if as_of:
        lines.append(f"时间：{as_of}")

    applied_items = [item for item in list(result.get("applied") or []) if isinstance(item, dict)]
    if applied_items:
        lines.extend(["", "明细："])
        for item in applied_items[:6]:
            lines.append(_applied_line(item))
        if len(applied_items) > 6:
            lines.append(f"- 另有 {len(applied_items) - 6} 条已省略")

    if errors:
        lines.extend(["", "错误："])
        for error in errors[:5]:
            lines.append(f"- {error}")
        if len(errors) > 5:
            lines.append(f"- 另有 {len(errors) - 5} 条错误已省略")

    return "\n".join(lines).strip()


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


def _bool_from_config(src: dict[str, Any], key: str, *, default: bool) -> bool:
    value = src.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"option_positions.auto_close.receipt.{key} must be a boolean")
    return bool(value)


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _errors(result: dict[str, Any]) -> list[str]:
    raw = result.get("errors")
    if isinstance(raw, list):
        return [str(item) for item in raw]
    if raw:
        return [str(raw)]
    return []


def _applied_line(item: dict[str, Any]) -> str:
    record_id = _display(item.get("record_id"))
    position_id = _display(item.get("position_id"))
    expiration = _display(item.get("expiration_ymd") or item.get("expiration_ms"))
    return f"- {record_id} | {position_id} | exp={expiration}"


def _display(value: Any) -> str:
    text = _optional_str(value)
    return text if text is not None else "-"


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
