from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from domain.domain.multi_tick import resolve_notification_route_from_config
from domain.storage.json_io import atomic_write_json, read_json
from domain.storage.repositories import state_repo
from src.infrastructure.external_services import select_notification_delivery_adapter

_AUTO_CLOSE_RECEIPT_STATE_NAME = "auto_close_receipts.json"
_AUTO_CLOSE_RECEIPT_STATE_MAX_ITEMS = 200


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
        "retry_unconfirmed": _bool_from_config(src, "retry_unconfirmed", default=True),
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
    prior_receipt: dict[str, Any] | None = None,
    receipt_key: str | None = None,
    receipt_key_fields: dict[str, Any] | None = None,
    send_fn: Callable[..., Any] | None = None,
    normalize_fn: Callable[..., dict[str, Any]] | None = None,
    route_resolver: Callable[..., dict[str, Any]] = resolve_notification_route_from_config,
    adapter_selector: Callable[[Any], Any] = select_notification_delivery_adapter,
) -> dict[str, Any]:
    cfg = resolve_auto_close_receipt_config(receipt_config)
    decision = decide_auto_close_receipt(
        receipt_config=cfg,
        dry_run=dry_run,
        result=result,
        prior_receipt=prior_receipt,
        receipt_key=receipt_key,
    )
    if not decision["should_send"]:
        out = {
            "enabled": bool(cfg.get("enabled", True)),
            "status": "skipped",
            "reason": decision["reason"],
            "delivery_confirmed": False,
            "message_id": None,
        }
        _attach_receipt_identity(out, receipt_key=receipt_key, receipt_key_fields=receipt_key_fields)
        return out

    route = route_resolver(config=config or {})
    provider = route.get("provider")
    channel = route.get("channel")
    target = route.get("target")
    if not str(target or "").strip():
        out = {
            "enabled": True,
            "status": "skipped",
            "reason": "skipped_no_route",
            "decision_reason": decision["reason"],
            "provider": provider,
            "channel": channel,
            "target_set": False,
            "delivery_confirmed": False,
            "message_id": None,
        }
        _attach_receipt_identity(out, receipt_key=receipt_key, receipt_key_fields=receipt_key_fields)
        return out

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
    out = {
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
        "attempt_count": _next_attempt_count(prior_receipt),
        "updated_at": _utc_now(),
    }
    _attach_receipt_identity(out, receipt_key=receipt_key, receipt_key_fields=receipt_key_fields)
    return out


def safe_send_auto_close_receipt(
    *,
    base: Path,
    config: dict[str, Any] | None,
    dry_run: bool,
    result: dict[str, Any],
) -> dict[str, Any]:
    try:
        receipt_config = resolve_auto_close_receipt_config_from_runtime_config(config)
        identity = build_auto_close_receipt_identity(config=config, result=result)
        prior_receipt = _load_prior_auto_close_receipt(
            base=base,
            account=_optional_str(result.get("account")),
            receipt_key=identity.get("receipt_key"),
        )
        receipt = send_auto_close_receipt(
            base=base,
            config=config,
            receipt_config=receipt_config,
            dry_run=dry_run,
            result=result,
            prior_receipt=prior_receipt,
            receipt_key=_optional_str(identity.get("receipt_key")),
            receipt_key_fields=identity.get("receipt_key_fields") if isinstance(identity.get("receipt_key_fields"), dict) else None,
        )
        persist_auto_close_receipt_state(
            base=base,
            account=_optional_str(result.get("account")),
            result=result,
            receipt=receipt,
        )
        return receipt
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
    prior_receipt: dict[str, Any] | None = None,
    receipt_key: str | None = None,
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
        return _dedupe_auto_close_receipt_decision(
            {"should_send": should_send, "reason": "dry_run"},
            receipt_config=cfg,
            prior_receipt=prior_receipt,
            receipt_key=receipt_key,
        )
    if errors:
        reason = "partial_failed" if applied > 0 else "failed"
        return _dedupe_auto_close_receipt_decision(
            {"should_send": bool(cfg.get("notify_failed", True)), "reason": reason},
            receipt_config=cfg,
            prior_receipt=prior_receipt,
            receipt_key=receipt_key,
        )
    if applied > 0:
        return _dedupe_auto_close_receipt_decision(
            {"should_send": bool(cfg.get("notify_applied", True)), "reason": "applied"},
            receipt_config=cfg,
            prior_receipt=prior_receipt,
            receipt_key=receipt_key,
        )
    return _dedupe_auto_close_receipt_decision(
        {"should_send": bool(cfg.get("notify_noop", False)), "reason": "noop"},
        receipt_config=cfg,
        prior_receipt=prior_receipt,
        receipt_key=receipt_key,
    )


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


def build_auto_close_receipt_identity(
    *,
    config: dict[str, Any] | None,
    result: dict[str, Any],
) -> dict[str, Any]:
    fields = _auto_close_receipt_key_fields(config=config, result=result)
    raw = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "receipt_key": sha256(raw.encode("utf-8")).hexdigest(),
        "receipt_key_fields": fields,
    }


def persist_auto_close_receipt_state(
    *,
    base: Path,
    account: str | None,
    result: dict[str, Any],
    receipt: dict[str, Any],
) -> dict[str, Any] | None:
    key = _optional_str(receipt.get("receipt_key"))
    acct = _optional_str(account or result.get("account"))
    if not key or not acct:
        return None
    path = _auto_close_receipt_state_path(base=base, account=acct)
    state = _load_auto_close_receipt_state(path)
    receipts = state.get("receipts")
    receipts = receipts if isinstance(receipts, dict) else {}
    current = receipts.get(key)
    current = current if isinstance(current, dict) else {}

    status = str(receipt.get("status") or "").strip().lower()
    reason = str(receipt.get("reason") or "").strip().lower()
    if status == "skipped" and reason in {"skipped_duplicate_confirmed", "skipped_duplicate_unconfirmed"}:
        if current:
            current["last_skip"] = _receipt_state_entry(result=result, receipt=receipt).get("receipt")
            current["updated_at_utc"] = _utc_now()
            receipts[key] = current
    else:
        receipts[key] = _receipt_state_entry(result=result, receipt=receipt)

    state["receipts"] = _trim_receipt_state_items(receipts)
    state["updated_at_utc"] = _utc_now()
    atomic_write_json(path, state)
    return state


def _dedupe_auto_close_receipt_decision(
    decision: dict[str, Any],
    *,
    receipt_config: dict[str, Any],
    prior_receipt: dict[str, Any] | None,
    receipt_key: str | None,
) -> dict[str, Any]:
    if not bool(decision.get("should_send")):
        return decision
    if not _optional_str(receipt_key) or not isinstance(prior_receipt, dict):
        return decision
    if bool(prior_receipt.get("delivery_confirmed")):
        return {"should_send": False, "reason": "skipped_duplicate_confirmed"}
    if bool(receipt_config.get("retry_unconfirmed", True)):
        return {"should_send": True, "reason": f"{decision.get('reason')}_retry_unconfirmed_receipt"}
    return {"should_send": False, "reason": "skipped_duplicate_unconfirmed"}


def _auto_close_receipt_key_fields(
    *,
    config: dict[str, Any] | None,
    result: dict[str, Any],
) -> dict[str, Any]:
    tz_name = _market_timezone(config=config, result=result)
    business_date = _business_date(result.get("as_of_utc"), timezone_name=tz_name)
    applied_items = [item for item in list(result.get("applied") or []) if isinstance(item, dict)]
    applied_record_ids = sorted(_optional_str(item.get("record_id")) or "" for item in applied_items)
    applied_record_ids = [item for item in applied_record_ids if item]
    return {
        "kind": "auto_close_receipt",
        "account": _optional_str(result.get("account")) or "-",
        "broker": _optional_str(result.get("broker")) or "-",
        "business_date": business_date,
        "timezone": tz_name,
        "mode": _auto_close_business_status(result),
        "grace_days": _optional_str(result.get("grace_days")) or "-",
        "candidates_should_close": _int_value(result.get("candidates_should_close")),
        "applied_closed": _int_value(result.get("applied_closed")),
        "error_count": len(_errors(result)),
        "applied_record_ids": applied_record_ids,
    }


def _auto_close_business_status(result: dict[str, Any]) -> str:
    errors = _errors(result)
    applied = _int_value(result.get("applied_closed"))
    mode = str(result.get("mode") or "").strip().lower()
    if mode == "dry_run":
        return "dry_run"
    if mode == "skipped":
        return f"skipped:{str(result.get('reason') or '').strip() or 'unknown'}"
    if errors and applied > 0:
        return "partial_failed"
    if errors:
        return "failed"
    if applied > 0:
        return "applied"
    return "noop"


def _market_timezone(*, config: dict[str, Any] | None, result: dict[str, Any]) -> str:
    result_tz = _optional_str(result.get("market_timezone") or result.get("timezone"))
    if result_tz:
        return result_tz
    cfg = config if isinstance(config, dict) else {}
    schedule = cfg.get("schedule")
    schedule = schedule if isinstance(schedule, dict) else {}
    return _optional_str(schedule.get("timezone")) or "UTC"


def _business_date(value: Any, *, timezone_name: str) -> str:
    dt = _parse_datetime(value)
    tz = _zoneinfo(timezone_name)
    return dt.astimezone(tz).date().isoformat()


def _parse_datetime(value: Any) -> datetime:
    text = _optional_str(value)
    if not text:
        return datetime.now(timezone.utc)
    try:
        out = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return datetime.now(timezone.utc)
    if out.tzinfo is None:
        out = out.replace(tzinfo=timezone.utc)
    return out.astimezone(timezone.utc)


def _zoneinfo(name: str) -> ZoneInfo | timezone:
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


def _auto_close_receipt_state_path(*, base: Path, account: str) -> Path:
    return (state_repo.account_state_dir(base, account) / _AUTO_CLOSE_RECEIPT_STATE_NAME).resolve()


def _load_auto_close_receipt_state(path: Path) -> dict[str, Any]:
    raw = read_json(path, {})
    if not isinstance(raw, dict):
        raw = {}
    receipts = raw.get("receipts")
    receipts = receipts if isinstance(receipts, dict) else {}
    return {
        "schema_kind": "auto_close_receipt_state",
        "schema_version": "1.0",
        "receipts": receipts,
    }


def _load_prior_auto_close_receipt(
    *,
    base: Path,
    account: str | None,
    receipt_key: Any,
) -> dict[str, Any] | None:
    acct = _optional_str(account)
    key = _optional_str(receipt_key)
    if not acct or not key:
        return None
    state = _load_auto_close_receipt_state(_auto_close_receipt_state_path(base=base, account=acct))
    receipts = state.get("receipts")
    item = receipts.get(key) if isinstance(receipts, dict) else None
    if not isinstance(item, dict):
        return None
    receipt = item.get("receipt")
    return receipt if isinstance(receipt, dict) else None


def _receipt_state_entry(*, result: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        "receipt_key": receipt.get("receipt_key"),
        "receipt_key_fields": receipt.get("receipt_key_fields"),
        "result_summary": {
            "mode": result.get("mode"),
            "account": result.get("account"),
            "broker": result.get("broker"),
            "as_of_utc": result.get("as_of_utc"),
            "candidates_should_close": result.get("candidates_should_close"),
            "applied_closed": result.get("applied_closed"),
            "error_count": len(_errors(result)),
        },
        "receipt": dict(receipt),
        "updated_at_utc": _utc_now(),
    }


def _trim_receipt_state_items(receipts: dict[str, Any]) -> dict[str, Any]:
    items = list(receipts.items())
    if len(items) <= _AUTO_CLOSE_RECEIPT_STATE_MAX_ITEMS:
        return dict(items)

    def _updated_at(item: tuple[str, Any]) -> str:
        value = item[1]
        return str(value.get("updated_at_utc") or "") if isinstance(value, dict) else ""

    return dict(sorted(items, key=_updated_at)[-_AUTO_CLOSE_RECEIPT_STATE_MAX_ITEMS:])


def _next_attempt_count(prior_receipt: dict[str, Any] | None) -> int:
    if not isinstance(prior_receipt, dict):
        return 1
    try:
        return int(prior_receipt.get("attempt_count") or 0) + 1
    except Exception:
        return 1


def _attach_receipt_identity(
    out: dict[str, Any],
    *,
    receipt_key: str | None,
    receipt_key_fields: dict[str, Any] | None,
) -> None:
    key = _optional_str(receipt_key)
    if key:
        out["receipt_key"] = key
    if isinstance(receipt_key_fields, dict):
        out["receipt_key_fields"] = dict(receipt_key_fields)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
