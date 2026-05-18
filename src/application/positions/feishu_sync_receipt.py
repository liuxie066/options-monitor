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

_SYNC_RECEIPT_STATE_NAME = "option_positions_feishu_sync_receipts.json"
_SYNC_RECEIPT_STATE_MAX_ITEMS = 200
_DEFAULT_BUSINESS_TIMEZONE = "Asia/Shanghai"


def resolve_option_positions_feishu_sync_receipt_config(value: Any) -> dict[str, bool]:
    if value is None:
        src: dict[str, Any] = {}
    elif isinstance(value, dict):
        src = value
    else:
        raise ValueError("option_positions.sync_to_feishu.receipt must be an object")
    return {
        "enabled": _bool_from_config(src, "enabled", default=True),
        "notify_applied": _bool_from_config(src, "notify_applied", default=True),
        "notify_failed": _bool_from_config(src, "notify_failed", default=True),
        "notify_conflict": _bool_from_config(src, "notify_conflict", default=True),
        "notify_noop": _bool_from_config(src, "notify_noop", default=False),
        "notify_dry_run": _bool_from_config(src, "notify_dry_run", default=False),
        "retry_unconfirmed": _bool_from_config(src, "retry_unconfirmed", default=True),
    }


def resolve_option_positions_feishu_sync_receipt_config_from_runtime_config(
    config: dict[str, Any] | None,
) -> dict[str, bool]:
    data = config if isinstance(config, dict) else {}
    option_positions = data.get("option_positions")
    option_positions = option_positions if isinstance(option_positions, dict) else {}
    sync_to_feishu = option_positions.get("sync_to_feishu")
    sync_to_feishu = sync_to_feishu if isinstance(sync_to_feishu, dict) else {}
    return resolve_option_positions_feishu_sync_receipt_config(sync_to_feishu.get("receipt"))


def decide_option_positions_feishu_sync_receipt(
    *,
    receipt_config: dict[str, Any] | None,
    dry_run: bool,
    result: dict[str, Any],
    prior_receipt: dict[str, Any] | None = None,
    receipt_key: str | None = None,
) -> dict[str, Any]:
    cfg = resolve_option_positions_feishu_sync_receipt_config(receipt_config)
    if cfg.get("enabled", True) is False:
        return {"should_send": False, "reason": "skipped_disabled"}

    status = _sync_result_status(result)
    if dry_run or str(result.get("mode") or "").strip().lower() == "dry_run":
        decision = {"should_send": bool(cfg.get("notify_dry_run", False)), "reason": "dry_run"}
    elif status in {"failed", "partial_failed"}:
        decision = {"should_send": bool(cfg.get("notify_failed", True)), "reason": status}
    elif status in {"conflict", "partial_conflict"}:
        decision = {"should_send": bool(cfg.get("notify_conflict", True)), "reason": status}
    elif status == "applied":
        decision = {"should_send": bool(cfg.get("notify_applied", True)), "reason": "applied"}
    else:
        decision = {"should_send": bool(cfg.get("notify_noop", False)), "reason": "noop"}

    return _dedupe_receipt_decision(
        decision,
        receipt_config=cfg,
        prior_receipt=prior_receipt,
        receipt_key=receipt_key,
    )


def send_option_positions_feishu_sync_receipt(
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
    cfg = resolve_option_positions_feishu_sync_receipt_config(receipt_config)
    decision = decide_option_positions_feishu_sync_receipt(
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

    message = build_option_positions_feishu_sync_receipt_message(result=result, dry_run=dry_run)
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


def safe_send_option_positions_feishu_sync_receipt(
    *,
    base: Path,
    config: dict[str, Any] | None,
    dry_run: bool,
    result: dict[str, Any],
) -> dict[str, Any]:
    try:
        receipt_config = resolve_option_positions_feishu_sync_receipt_config_from_runtime_config(config)
        identity = build_option_positions_feishu_sync_receipt_identity(result=result)
        prior_receipt = load_prior_option_positions_feishu_sync_receipt(
            base=base,
            receipt_key=identity.get("receipt_key"),
        )
        receipt = send_option_positions_feishu_sync_receipt(
            base=base,
            config=config,
            receipt_config=receipt_config,
            dry_run=dry_run,
            result=result,
            prior_receipt=prior_receipt,
            receipt_key=_optional_str(identity.get("receipt_key")),
            receipt_key_fields=identity.get("receipt_key_fields") if isinstance(identity.get("receipt_key_fields"), dict) else None,
        )
        persist_option_positions_feishu_sync_receipt_state(base=base, result=result, receipt=receipt)
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
            "updated_at": _utc_now(),
        }


def skipped_option_positions_feishu_sync_receipt(
    *,
    result: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    identity = build_option_positions_feishu_sync_receipt_identity(result=result)
    out = {
        "enabled": True,
        "status": "skipped",
        "reason": reason,
        "delivery_confirmed": False,
        "message_id": None,
        "updated_at": _utc_now(),
    }
    _attach_receipt_identity(
        out,
        receipt_key=_optional_str(identity.get("receipt_key")),
        receipt_key_fields=identity.get("receipt_key_fields") if isinstance(identity.get("receipt_key_fields"), dict) else None,
    )
    return out


def build_option_positions_feishu_sync_receipt_message(
    *,
    result: dict[str, Any],
    dry_run: bool,
) -> str:
    status = _sync_result_status(result)
    if status in {"failed", "partial_failed"}:
        title = "[异常] option_positions 同步 Feishu 异常"
    elif status in {"conflict", "partial_conflict"}:
        title = "[冲突] option_positions 同步 Feishu 存在冲突"
    elif status == "applied":
        title = "[已同步] option_positions 已同步到 Feishu"
    else:
        title = "[无变更] option_positions 同步 Feishu 无需写入"

    summary = _summary(result)
    filters_raw = result.get("filters")
    filters: dict[str, Any] = filters_raw if isinstance(filters_raw, dict) else {}
    lines = [
        title,
        "",
        f"模式：{'dry-run' if dry_run else str(result.get('mode') or '-')}",
        f"结果：create={_int_value(summary.get('create'))} update={_int_value(summary.get('update'))} delete={_int_value(summary.get('delete'))} skip={_int_value(summary.get('skip'))} conflict={_int_value(summary.get('conflict'))} failed={_int_value(summary.get('failed'))}",
        f"扫描：{_int_value(summary.get('scanned'))} 条",
    ]
    scope = _filters_text(filters)
    if scope:
        lines.append(f"范围：{scope}")
    error_text = _error_text(result)
    if error_text:
        lines.append(f"错误：{error_text}")
    for label, rows in (("失败", _rows_by_action(result, "failed")), ("冲突", _rows_by_action(result, "conflict"))):
        for item in rows[:5]:
            record_id = _optional_str(item.get("record_id") or item.get("remote_record_id") or item.get("remote_local_record_id")) or "-"
            reason = _optional_str(item.get("reason")) or "-"
            lines.append(f"{label}：{record_id} | {reason}")
    return "\n".join(lines).strip()


def build_option_positions_feishu_sync_receipt_identity(*, result: dict[str, Any]) -> dict[str, Any]:
    fields = _receipt_key_fields(result=result)
    raw = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "receipt_key": sha256(raw.encode("utf-8")).hexdigest(),
        "receipt_key_fields": fields,
    }


def persist_option_positions_feishu_sync_receipt_state(
    *,
    base: Path,
    result: dict[str, Any],
    receipt: dict[str, Any],
) -> dict[str, Any] | None:
    key = _optional_str(receipt.get("receipt_key"))
    if not key:
        return None
    path = option_positions_feishu_sync_receipt_state_path(base)
    state = _load_sync_receipt_state(path)
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


def persist_option_positions_feishu_sync_last_run(
    *,
    base: Path,
    result: dict[str, Any],
) -> Path:
    return state_repo.write_shared_state(base, "option_positions_feishu_sync.json", result)


def option_positions_feishu_sync_receipt_state_path(base: Path) -> Path:
    return (state_repo.shared_state_dir(base) / _SYNC_RECEIPT_STATE_NAME).resolve()


def load_prior_option_positions_feishu_sync_receipt(
    *,
    base: Path,
    receipt_key: Any,
) -> dict[str, Any] | None:
    key = _optional_str(receipt_key)
    if not key:
        return None
    state = _load_sync_receipt_state(option_positions_feishu_sync_receipt_state_path(base))
    receipts = state.get("receipts")
    item = receipts.get(key) if isinstance(receipts, dict) else None
    if not isinstance(item, dict):
        return None
    receipt = item.get("receipt")
    return receipt if isinstance(receipt, dict) else None


def _receipt_key_fields(*, result: dict[str, Any]) -> dict[str, Any]:
    summary = _summary(result)
    filters_raw = result.get("filters")
    filters: dict[str, Any] = filters_raw if isinstance(filters_raw, dict) else {}
    rows_raw = result.get("rows")
    rows: list[Any] = rows_raw if isinstance(rows_raw, list) else []
    interesting_rows = [
        _row_signature(item)
        for item in rows
        if isinstance(item, dict) and str(item.get("action") or "").strip().lower() in {"create", "update", "delete", "failed", "conflict"}
    ]
    return {
        "kind": "option_positions_feishu_sync_receipt",
        "business_date": _business_date(result.get("finished_at") or result.get("started_at"), timezone_name=_DEFAULT_BUSINESS_TIMEZONE),
        "business_timezone": _DEFAULT_BUSINESS_TIMEZONE,
        "mode": str(result.get("mode") or "-"),
        "status": _sync_result_status(result),
        "data_config": _optional_str(result.get("data_config_path")) or "-",
        "table_ref_hash": _optional_str(result.get("table_ref_hash")) or "-",
        "filters": {
            "only_record_id": _optional_str(filters.get("only_record_id")) or None,
            "only_open": bool(filters.get("only_open")),
            "since_updated_ms": filters.get("since_updated_ms"),
            "limit": filters.get("limit"),
            "prune_remote_missing_local": bool(filters.get("prune_remote_missing_local")),
        },
        "summary": {
            "create": _int_value(summary.get("create")),
            "update": _int_value(summary.get("update")),
            "delete": _int_value(summary.get("delete")),
            "skip": _int_value(summary.get("skip")),
            "conflict": _int_value(summary.get("conflict")),
            "failed": _int_value(summary.get("failed")),
        },
        "rows": sorted(interesting_rows, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True)),
        "error": _error_signature(result),
    }


def _sync_result_status(result: dict[str, Any]) -> str:
    explicit = str(result.get("status") or "").strip().lower()
    if explicit:
        return explicit
    summary = _summary(result)
    failed = _int_value(summary.get("failed"))
    conflict = _int_value(summary.get("conflict"))
    changed = _int_value(summary.get("create")) + _int_value(summary.get("update")) + _int_value(summary.get("delete"))
    if result.get("error"):
        return "failed"
    if failed > 0:
        return "partial_failed" if changed > 0 or conflict > 0 or _int_value(summary.get("skip")) > 0 else "failed"
    if conflict > 0:
        return "partial_conflict" if changed > 0 or _int_value(summary.get("skip")) > 0 else "conflict"
    if changed > 0:
        return "applied"
    return "noop"


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    value = result.get("summary")
    return value if isinstance(value, dict) else {}


def _dedupe_receipt_decision(
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


def _load_sync_receipt_state(path: Path) -> dict[str, Any]:
    raw = read_json(path, {})
    if not isinstance(raw, dict):
        raw = {}
    receipts = raw.get("receipts")
    receipts = receipts if isinstance(receipts, dict) else {}
    return {
        "schema_kind": "option_positions_feishu_sync_receipt_state",
        "schema_version": "1.0",
        "receipts": receipts,
    }


def _receipt_state_entry(*, result: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    summary = _summary(result)
    return {
        "receipt_key": receipt.get("receipt_key"),
        "receipt_key_fields": receipt.get("receipt_key_fields"),
        "result_summary": {
            "mode": result.get("mode"),
            "status": _sync_result_status(result),
            "started_at": result.get("started_at"),
            "finished_at": result.get("finished_at"),
            "scanned": summary.get("scanned"),
            "create": summary.get("create"),
            "update": summary.get("update"),
            "delete": summary.get("delete"),
            "skip": summary.get("skip"),
            "conflict": summary.get("conflict"),
            "failed": summary.get("failed"),
        },
        "receipt": dict(receipt),
        "updated_at_utc": _utc_now(),
    }


def _trim_receipt_state_items(receipts: dict[str, Any]) -> dict[str, Any]:
    items = list(receipts.items())
    if len(items) <= _SYNC_RECEIPT_STATE_MAX_ITEMS:
        return dict(items)

    def _updated_at(item: tuple[str, Any]) -> str:
        value = item[1]
        return str(value.get("updated_at_utc") or "") if isinstance(value, dict) else ""

    return dict(sorted(items, key=_updated_at)[-_SYNC_RECEIPT_STATE_MAX_ITEMS:])


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


def _rows_by_action(result: dict[str, Any], action: str) -> list[dict[str, Any]]:
    rows_raw = result.get("rows")
    rows: list[Any] = rows_raw if isinstance(rows_raw, list) else []
    out: list[dict[str, Any]] = []
    for item in rows:
        if isinstance(item, dict) and str(item.get("action") or "").strip().lower() == action:
            out.append(dict(item))
    return out


def _row_signature(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": _optional_str(item.get("action")) or "-",
        "record_id": _optional_str(item.get("record_id")) or _optional_str(item.get("remote_record_id")) or "-",
        "remote_local_record_id": _optional_str(item.get("remote_local_record_id")) or None,
        "reason": _optional_str(item.get("reason")) or "-",
    }


def _error_signature(result: dict[str, Any]) -> dict[str, Any] | None:
    error = result.get("error")
    if not isinstance(error, dict):
        return None
    return {
        "type": _optional_str(error.get("type")) or "-",
        "message": _optional_str(error.get("message")) or "-",
    }


def _error_text(result: dict[str, Any]) -> str | None:
    error = _error_signature(result)
    if not error:
        return None
    return f"{error.get('type')}: {error.get('message')}"


def _filters_text(filters: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("only_record_id", "only_open", "since_updated_ms", "limit", "prune_remote_missing_local"):
        value = filters.get(key)
        if value in (None, "", False):
            continue
        parts.append(f"{key}={value}")
    return ", ".join(parts)


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


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _bool_from_config(src: dict[str, Any], key: str, *, default: bool) -> bool:
    value = src.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"option_positions.sync_to_feishu.receipt.{key} must be a boolean")
    return bool(value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
