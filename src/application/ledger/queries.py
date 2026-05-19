from __future__ import annotations

from pathlib import Path
from typing import Any

from src.application.ledger.publisher import project_stored_trade_events_to_position_lots
from src.application.ledger.projection_verify import load_projection_verify_state
from src.application.ledger.repository import (
    require_option_positions_event_write_repo,
)
from src.application.ledger.risk_context import summarize_ledger_shadow_status
from src.application.ledger.service import load_option_positions_repo
from src.application.ledger.views import PositionLotSnapshot, RiskPositionView


def open_position_ledger(data_config: Any) -> Any:
    return load_option_positions_repo(data_config)


def open_position_ledger_from_data_config(*, base: Path, data_config: str | Path | None) -> tuple[Path, Any]:
    from src.application.ledger.read_model import resolve_position_repo as _impl

    return _impl(base=base, data_config=data_config)


def resolve_position_data_config_path(
    *,
    base: Path,
    cfg: dict[str, Any] | None = None,
    data_config: str | Path | None = None,
    config_path: str | Path | None = None,
) -> Path:
    from src.application.ledger.read_model import resolve_position_data_config_path as _impl

    return _impl(base=base, cfg=cfg, data_config=data_config, config_path=config_path)


def open_position_ledger_from_runtime_config(
    *,
    base: Path,
    cfg: dict[str, Any] | None,
    data_config: str | Path | None = None,
    config_path: str | Path | None = None,
    runtime_root: str | Path | None = None,
) -> tuple[Path, Any]:
    from src.application.ledger.read_model import resolve_position_repo_from_config as _impl

    resolved_data_config, repo = _impl(
        base=base,
        cfg=cfg,
        data_config=data_config,
        config_path=config_path,
        runtime_root=runtime_root,
    )
    apply_position_ledger_runtime_config(repo, cfg)
    return resolved_data_config, repo


def normalize_position_lot_fields(fields: dict[str, Any]) -> dict[str, Any]:
    from src.application.ledger.read_model import canonicalize_position_lot_fields as _impl

    return _impl(fields)


def normalize_position_lot_snapshot(item: dict[str, Any]) -> dict[str, Any]:
    from src.application.ledger.read_model import canonicalize_position_lot_record as _impl

    return _impl(item)


def position_lot_snapshot(item: dict[str, Any]) -> PositionLotSnapshot:
    return PositionLotSnapshot.from_record(normalize_position_lot_snapshot(item))


def list_position_lot_snapshots(repo: Any, *, base: Path | None = None) -> list[dict[str, Any]]:
    from src.application.ledger.read_model import load_position_lot_records as _impl

    return _impl(repo, base=base)


def list_position_lot_sync_snapshots(repo: Any, *, base: Path | None = None) -> list[dict[str, Any]]:
    from src.application.ledger.read_model import load_canonical_position_lot_records as _impl

    return _impl(repo, base=base)


def list_canonical_position_lot_snapshots(repo: Any, *, base: Path | None = None) -> list[dict[str, Any]]:
    from src.application.ledger.read_model import load_canonical_position_lot_records as _impl

    return _impl(repo, base=base)


def list_position_rows(
    repo: Any,
    *,
    broker: str,
    account: str | None = None,
    status: str = "open",
    limit: int = 50,
    expiration_within_days: int | None = None,
    as_of_ms: int | None = None,
) -> list[dict[str, Any]]:
    from src.application.ledger.read_model import list_position_rows as _impl

    return _impl(
        repo,
        broker=broker,
        account=account,
        status=status,
        limit=limit,
        expiration_within_days=expiration_within_days,
        as_of_ms=as_of_ms,
    )


def resolve_position_lot_snapshots(*, base: Path, data_config: str | Path | None) -> tuple[Path, Any, list[dict[str, Any]]]:
    from src.application.ledger.read_model import resolve_position_lot_records as _impl

    return _impl(base=base, data_config=data_config)


def position_lot_context_view(
    item: dict[str, Any],
    *,
    as_of_date: Any = None,
) -> dict[str, Any]:
    from src.application.ledger.read_model import build_position_lot_view as _impl

    return _impl(item, as_of_date=as_of_date)


def position_lot_risk_view(
    item: dict[str, Any],
    *,
    as_of_date: Any = None,
) -> RiskPositionView:
    return RiskPositionView.from_view(position_lot_context_view(item, as_of_date=as_of_date))


def position_monthly_income_report(
    repo: Any,
    *,
    base: Path,
    broker: str,
    account: str | None = None,
    month: str | None = None,
) -> dict[str, Any]:
    from src.application.ledger.read_model import build_position_monthly_income_report as _impl

    return _impl(repo, base=base, broker=broker, account=account, month=month)


def format_position_money(value: float | int | None, currency: str) -> str:
    from src.application.ledger.read_model import format_position_money as _impl

    return _impl(value, currency)


def format_position_cash_secured(value: Any, currency: str) -> str:
    from src.application.ledger.read_model import format_cash_secured_amount as _impl

    return _impl(value, currency)


def summarize_position_lot_shadow_status(records: list[dict[str, Any]]) -> dict[str, Any]:
    return summarize_ledger_shadow_status(records)


def apply_position_ledger_runtime_config(repo: Any, cfg: dict[str, Any] | None) -> Any:
    _ = cfg
    return repo


def trade_event_log(repo: Any) -> list[dict[str, Any]]:
    sqlite_repo = require_option_positions_event_write_repo(repo)
    events = sqlite_repo.list_trade_events()
    return events if isinstance(events, list) else []


def project_trade_event_log(events: list[dict[str, Any]]) -> Any:
    return project_stored_trade_events_to_position_lots(events)


def trade_event_projection_preview(events: list[dict[str, Any]]) -> dict[str, Any]:
    projection = project_trade_event_log(events)
    return {
        "trade_event_count": int(len(events)),
        "position_lot_count": int(len(projection.lots)),
        "projection_diagnostic_count": int(len(projection.diagnostics)),
        "projection_diagnostics": [item.to_dict() for item in projection.diagnostics],
    }


def position_projection_verify_state(base: Path) -> dict[str, Any]:
    return load_projection_verify_state(base=base)


__all__ = [
    "PositionLotSnapshot",
    "RiskPositionView",
    "apply_position_ledger_runtime_config",
    "format_position_cash_secured",
    "format_position_money",
    "list_canonical_position_lot_snapshots",
    "list_position_lot_snapshots",
    "list_position_lot_sync_snapshots",
    "list_position_rows",
    "normalize_position_lot_fields",
    "normalize_position_lot_snapshot",
    "open_position_ledger",
    "open_position_ledger_from_data_config",
    "open_position_ledger_from_runtime_config",
    "position_lot_context_view",
    "position_lot_risk_view",
    "position_lot_snapshot",
    "position_monthly_income_report",
    "position_projection_verify_state",
    "project_trade_event_log",
    "resolve_position_data_config_path",
    "resolve_position_lot_snapshots",
    "summarize_position_lot_shadow_status",
    "trade_event_log",
    "trade_event_projection_preview",
]
