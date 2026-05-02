from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.config_loader import resolve_data_config_path
from scripts.option_positions_core.domain import (
    effective_contracts_open,
    normalize_account,
    normalize_broker,
    normalize_status,
)
from scripts.option_positions_core.service import (
    auto_close_expired_positions,
    build_expired_close_decisions,
    load_option_positions_repo,
    rebuild_position_lots_from_trade_events,
)
from src.application.option_positions_facade import load_option_position_records


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
        "run_on_tick": _bool_config(data, "run_on_tick", True),
        "grace_days": _int_config(data, "grace_days", 1, min_value=0),
        "max_close": _int_config(data, "max_close_per_run" if "max_close_per_run" in data else "max_close", 20, min_value=1),
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
    if int(count_trade_events() or 0) <= 0:
        return None
    return rebuild_position_lots_from_trade_events(candidate)


def _write_auto_close_summary(report_dir: Path, result: dict[str, Any]) -> str:
    text = format_auto_close_summary(result)
    if not text:
        return ""
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "auto_close_summary.txt").write_text(text + "\n", encoding="utf-8")
    return text


def run_expired_position_maintenance_for_account(
    *,
    base: Path,
    cfg: dict[str, Any],
    account: str | None,
    report_dir: Path,
    as_of_ms: int | None = None,
    broker: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    auto_cfg = _auto_close_config(cfg)
    if not auto_cfg["enabled"] or not auto_cfg["run_on_tick"]:
        return {"mode": "skipped", "reason": "auto_close_disabled"}

    portfolio_cfg = cfg.get("portfolio") if isinstance(cfg, dict) else {}
    effective_broker = broker if broker is not None else (
        portfolio_cfg.get("broker") if isinstance(portfolio_cfg, dict) else None
    )
    data_config_ref = portfolio_cfg.get("data_config") if isinstance(portfolio_cfg, dict) else None
    data_config = resolve_data_config_path(base=base, data_config=data_config_ref)
    if not data_config.exists():
        return {"mode": "skipped", "reason": "missing_data_config", "data_config": str(data_config)}

    repo = load_option_positions_repo(data_config)
    ts = int(as_of_ms if as_of_ms is not None else datetime.now(timezone.utc).timestamp() * 1000)
    projection_refresh = (
        None
        if dry_run
        else _refresh_position_projection_before_auto_close(repo)
    )
    positions = _open_positions_for_account(
        load_option_position_records(repo),
        account=account,
        broker=effective_broker,
    )
    if dry_run:
        decisions = build_expired_close_decisions(
            positions,
            as_of_ms=ts,
            grace_days=int(auto_cfg["grace_days"]),
        )
        applied: list[dict[str, Any]] = []
        errors: list[str] = []
    else:
        decisions, applied, errors = auto_close_expired_positions(
            repo,
            positions,
            as_of_ms=ts,
            grace_days=int(auto_cfg["grace_days"]),
            max_close=int(auto_cfg["max_close"]),
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
    result = {
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
    }
    if projection_refresh is not None:
        result["projection_refresh"] = projection_refresh
    result["summary_text"] = _write_auto_close_summary(report_dir, result)
    return result
