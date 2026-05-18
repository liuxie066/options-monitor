from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, cast

from src.application.config_loader import resolve_data_config_path
from domain.domain.ledger.position_fields import (
    effective_contracts_open,
    normalize_account,
    normalize_broker,
    normalize_status,
)
from src.application.ledger.api import (
    ledger_store_payload,
    list_expiry_close_position_lots,
    open_position_ledger,
    plan_expired_position_closes,
    record_expired_position_closes,
    refresh_position_lot_projection,
)
from src.application.positions.sync_config import apply_option_positions_runtime_config
from src.application.positions.maintenance_receipt import (
    resolve_auto_close_receipt_config,
    safe_send_auto_close_receipt,
)


def _bool_config(data: dict[str, Any], key: str, default: bool) -> bool:
    if key not in data or data.get(key) is None:
        return default
    value = data.get(key)
    if isinstance(value, bool):
        return value
    raise ValueError(f"option_positions.auto_close.{key} must be a boolean")


def _int_config(data: dict[str, Any], key: str, default: int, *, min_value: int) -> int:
    if key not in data or data.get(key) in (None, ""):
        value = default
    else:
        value = data.get(key)
    if isinstance(value, bool):
        raise ValueError(f"option_positions.auto_close.{key} must be an integer")
    if isinstance(value, int):
        resolved = value
    elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
        resolved = int(value.strip())
    else:
        raise ValueError(f"option_positions.auto_close.{key} must be an integer")
    if resolved < int(min_value):
        raise ValueError(f"option_positions.auto_close.{key} must be >= {min_value}")
    return resolved


def _auto_close_config(cfg: dict[str, Any]) -> dict[str, Any]:
    option_positions = cfg.get("option_positions") if isinstance(cfg, dict) else {}
    raw = option_positions.get("auto_close") if isinstance(option_positions, dict) else {}
    data = raw if isinstance(raw, dict) else {}
    return {
        "enabled": _bool_config(data, "enabled", True),
        "grace_days": _int_config(data, "grace_days", 1, min_value=0),
        "max_close": _int_config(
            data,
            "max_close_per_run" if "max_close_per_run" in data else "max_close",
            20,
            min_value=1,
        ),
        "receipt": resolve_auto_close_receipt_config(data.get("receipt")),
    }


def _open_positions_for_account(
    records: list[dict[str, Any]],
    *,
    account: str | None,
    broker: str | None,
) -> list[dict[str, Any]]:
    normalized_account = normalize_account(account) if account else None
    normalized_broker = normalize_broker(broker) if broker else None
    out: list[dict[str, Any]] = []
    for item in records:
        if not isinstance(item, dict):
            continue
        fields = item.get("fields") or item
        if not isinstance(fields, dict):
            continue
        if normalized_account and normalize_account(fields.get("account")) != normalized_account:
            continue
        if normalized_broker and normalize_broker(fields.get("broker") or fields.get("market")) != normalized_broker:
            continue
        if normalize_status(fields.get("status")) != "open":
            continue
        if effective_contracts_open(fields) <= 0:
            continue
        record_id = str(item.get("record_id") or fields.get("record_id") or "").strip()
        row = dict(fields)
        if record_id:
            row["record_id"] = record_id
        out.append(row)
    return out


def _load_expiry_close_position_lots(repo: Any) -> list[dict[str, Any]]:
    return list_expiry_close_position_lots(repo)


def format_auto_close_summary(result: dict[str, Any]) -> str:
    candidates = int(result.get("candidates_should_close") or 0)
    applied = int(result.get("applied_closed") or 0)
    skipped_already_closed = int(result.get("skipped_already_closed") or 0)
    errors = list(result.get("errors") or [])
    if candidates <= 0 and applied <= 0 and not errors:
        return ""

    lines = [
        f"Auto-close expired positions (grace_days={result.get('grace_days')})",
        f"as_of_utc: {result.get('as_of_utc')}",
        f"mode: {result.get('mode')}",
        f"account: {result.get('account') or ''}",
        f"broker: {result.get('broker') or ''}",
        f"candidates_should_close: {candidates}",
        f"applied_closed: {applied}",
    ]
    if skipped_already_closed > 0:
        lines.append(f"skipped_already_closed: {skipped_already_closed}")
    lines.append(f"ERRORS: {len(errors)}")
    projection_refresh = result.get("projection_refresh")
    if isinstance(projection_refresh, dict):
        trade_event_count = projection_refresh.get("trade_event_count")
        position_lot_count = projection_refresh.get("position_lot_count")
        if trade_event_count is not None and position_lot_count is not None:
            lines.append(
                f"projection_refresh: trade_events={trade_event_count}, "
                f"position_lots={position_lot_count}"
            )
    if errors:
        lines.append("")
        lines.append("Error details:")
        for error in errors[:20]:
            lines.append(f"- {error}")
    if applied:
        lines.append("")
        lines.append("Closed:")
        for item in list(result.get("applied") or [])[:50]:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"- {item.get('record_id')} | {item.get('position_id')} | "
                f"exp={item.get('expiration_ymd') or item.get('expiration_ms')}"
            )

    return "\n".join(lines).strip()


def _refresh_position_projection_before_auto_close(repo: Any) -> dict[str, Any] | None:
    candidate = getattr(repo, "primary_repo", repo)
    count_trade_events = getattr(candidate, "count_trade_events", None)
    if not callable(count_trade_events):
        return None
    try:
        count_trade_events_fn = cast(Callable[[], Any], count_trade_events)
        trade_event_count = int(count_trade_events_fn())
    except Exception:
        return None
    if trade_event_count <= 0:
        return None
    result = refresh_position_lot_projection(candidate)
    if isinstance(result, dict):
        return dict(result)
    return result.to_dict()


def _write_auto_close_summary(report_dir: Path, result: dict[str, Any]) -> str:
    text = format_auto_close_summary(result)
    if not text:
        return ""
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "auto_close_summary.txt").write_text(text + "\n", encoding="utf-8")
    return text


def _payload(item: Any) -> dict[str, Any]:
    to_payload = getattr(item, "to_payload", None)
    if callable(to_payload):
        payload = to_payload()
        return dict(payload) if isinstance(payload, dict) else {}
    return dict(item) if isinstance(item, dict) else {}


def _expired_close_run_payloads(value: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    to_legacy_tuple = getattr(value, "to_legacy_tuple", None)
    if callable(to_legacy_tuple):
        decisions, applied, errors = cast(tuple[list[Any], list[Any], list[Any]], to_legacy_tuple())
        return (
            [_payload(item) for item in decisions],
            [_payload(item) for item in applied],
            [str(item) for item in errors],
        )
    decisions, applied, errors = cast(tuple[list[Any], list[Any], list[Any]], value)
    return (
        [_payload(item) for item in list(decisions or [])],
        [_payload(item) for item in list(applied or [])],
        [str(item) for item in list(errors or [])],
    )


def run_expired_position_maintenance_for_account(
    *,
    base: Path,
    cfg: dict[str, Any],
    account: str | None,
    report_dir: Path,
    as_of_ms: int | None = None,
    broker: str | None = None,
    dry_run: bool = False,
    send_receipt: bool = True,
) -> dict[str, Any]:
    auto_cfg = _auto_close_config(cfg)
    if not auto_cfg["enabled"]:
        return {"mode": "skipped", "reason": "auto_close_disabled"}

    portfolio_cfg = cfg.get("portfolio") if isinstance(cfg, dict) else {}
    effective_broker = broker if broker is not None else (
        portfolio_cfg.get("broker") if isinstance(portfolio_cfg, dict) else None
    )
    data_config_ref = portfolio_cfg.get("data_config") if isinstance(portfolio_cfg, dict) else None
    data_config = resolve_data_config_path(base=base, data_config=data_config_ref)
    if not data_config.exists():
        return {"mode": "skipped", "reason": "missing_data_config", "data_config": str(data_config)}

    repo = open_position_ledger(data_config)
    apply_option_positions_runtime_config(repo, cfg)
    ledger_store = ledger_store_payload(data_config, repo)
    ts = int(as_of_ms if as_of_ms is not None else datetime.now(timezone.utc).timestamp() * 1000)
    projection_refresh = (
        None
        if dry_run
        else _refresh_position_projection_before_auto_close(repo)
    )
    positions = _open_positions_for_account(
        _load_expiry_close_position_lots(repo),
        account=account,
        broker=effective_broker,
    )
    if dry_run:
        decisions = [
            _payload(item)
            for item in plan_expired_position_closes(
                positions,
                as_of_ms=ts,
                grace_days=int(auto_cfg["grace_days"]),
            )
        ]
        applied: list[dict[str, Any]] = []
        errors: list[str] = []
    else:
        decisions, applied, errors = _expired_close_run_payloads(
            record_expired_position_closes(
                repo,
                positions,
                as_of_ms=ts,
                grace_days=int(auto_cfg["grace_days"]),
                max_close=int(auto_cfg["max_close"]),
            )
        )
    to_close = [
        item
        for item in decisions
        if isinstance(item, dict) and bool(item.get("should_close")) and item.get("record_id")
    ]
    skipped_already_closed = [
        item
        for item in decisions
        if isinstance(item, dict) and item.get("skip_reason") == "already_closed_or_zero_open"
    ]
    result: dict[str, Any] = {
        "mode": "dry_run" if dry_run else "applied",
        "account": normalize_account(account) if account else None,
        "broker": normalize_broker(effective_broker) if effective_broker else None,
        "as_of_utc": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat(),
        "grace_days": int(auto_cfg["grace_days"]),
        "max_close": int(auto_cfg["max_close"]),
        "positions_checked": len(positions),
        "decisions": len(decisions),
        "candidates_should_close": len(to_close),
        "applied_closed": len(applied),
        "skipped_already_closed": len(skipped_already_closed),
        "errors": errors,
        "applied": applied,
        "ledger_store": ledger_store,
    }
    if projection_refresh is not None:
        result["projection_refresh"] = projection_refresh
    result["summary_text"] = _write_auto_close_summary(report_dir, result)
    if send_receipt:
        result["receipt"] = safe_send_auto_close_receipt(
            base=base,
            config=cfg,
            dry_run=dry_run,
            result=result,
        )
    else:
        result["receipt"] = {
            "enabled": bool(auto_cfg["receipt"].get("enabled", True)),
            "status": "skipped",
            "reason": "skipped_no_send",
            "delivery_confirmed": False,
            "message_id": None,
        }
    return result
