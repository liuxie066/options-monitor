from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.application.ledger.publisher import project_stored_trade_events_to_position_lots
from src.application.ledger.projection_verify import load_projection_verify_state
from src.application.ledger.bootstrap import load_option_positions_repo
from src.application.ledger.repository import (
    require_option_positions_event_write_repo,
)
from src.application.ledger.risk_context import summarize_ledger_shadow_status
from src.application.ledger.views import PositionLotSnapshot, RiskPositionView


@dataclass(frozen=True)
class AssignedStockEventLog:
    events: tuple[dict[str, Any], ...]
    diagnostics: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "events": [dict(item) for item in self.events],
            "diagnostics": [dict(item) for item in self.diagnostics],
        }


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


def open_performance_evidence_repository(repo: Any) -> Any:
    from src.application.ledger.read_model import open_performance_evidence_repository as _impl

    return _impl(repo)


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
    symbol: str | None = None,
    option_type: str | None = None,
    side: str | None = None,
    strike: float | None = None,
    expiration_exact: str | None = None,
    expiration_month: str | None = None,
    expiration_before: str | None = None,
    expiration_after: str | None = None,
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
        symbol=symbol,
        option_type=option_type,
        side=side,
        strike=strike,
        expiration_exact=expiration_exact,
        expiration_month=expiration_month,
        expiration_before=expiration_before,
        expiration_after=expiration_after,
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


def assigned_stock_event_log(repo: Any) -> AssignedStockEventLog:
    candidate = getattr(repo, "primary_repo", repo)
    list_events = getattr(candidate, "list_assigned_stock_events", None)
    if not callable(list_events):
        return AssignedStockEventLog(
            events=(),
            diagnostics=(
                {
                    "context": "assigned_stock",
                    "code": "assigned_stock_event_log_unavailable",
                    "message": "ledger repository does not expose assigned-stock events",
                },
            ),
        )
    try:
        raw_events = list_events()
    except Exception as exc:
        return AssignedStockEventLog(
            events=(),
            diagnostics=(
                {
                    "context": "assigned_stock",
                    "code": "assigned_stock_event_log_read_failed",
                    "message": str(exc),
                },
            ),
        )
    if not isinstance(raw_events, list):
        return AssignedStockEventLog(
            events=(),
            diagnostics=(
                {
                    "context": "assigned_stock",
                    "code": "assigned_stock_event_log_invalid_payload",
                    "message": "assigned-stock repository returned a non-list payload",
                },
            ),
        )
    events: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for index, item in enumerate(raw_events):
        if isinstance(item, dict):
            events.append(dict(item))
            continue
        diagnostics.append(
            {
                "context": "assigned_stock",
                "code": "assigned_stock_event_invalid_row",
                "message": "assigned-stock event row is not an object",
                "row_index": index,
            }
        )
    return AssignedStockEventLog(events=tuple(events), diagnostics=tuple(diagnostics))


def trade_event_log(repo: Any) -> list[dict[str, Any]]:
    sqlite_repo = require_option_positions_event_write_repo(repo)
    events = sqlite_repo.list_trade_events()
    return events if isinstance(events, list) else []


def list_trade_lifecycle_cases(
    repo: Any,
    *,
    status: str | None = None,
    account: str | None = None,
    symbol: str | None = None,
) -> list[dict[str, Any]]:
    candidate = getattr(repo, "primary_repo", repo)
    list_fn = getattr(candidate, "list_trade_lifecycle_cases", None)
    if not callable(list_fn):
        return []
    rows = list_fn(status=status) if status else list_fn()
    out: list[dict[str, Any]] = []
    account_filter = str(account or "").strip().lower()
    symbol_filter = str(symbol or "").strip().upper()
    for row in list(rows or []):
        if not isinstance(row, dict):
            continue
        if account_filter and str(row.get("account") or "").strip().lower() != account_filter:
            continue
        if symbol_filter and str(row.get("symbol") or "").strip().upper() != symbol_filter:
            continue
        out.append(dict(row))
    return out


def list_trade_lifecycle_evidence(
    repo: Any,
    *,
    case_id: str | None = None,
    account: str | None = None,
    symbol: str | None = None,
) -> list[dict[str, Any]]:
    candidate = getattr(repo, "primary_repo", repo)
    list_fn = getattr(candidate, "list_trade_lifecycle_evidence", None)
    if not callable(list_fn):
        return []
    rows = list_fn(case_id=case_id, account=account, symbol=symbol)
    return [dict(row) for row in list(rows or []) if isinstance(row, dict)]


def project_trade_event_log(events: list[dict[str, Any]]) -> Any:
    return project_stored_trade_events_to_position_lots(events)



def trade_event_economic_allocations(repo: Any) -> list[Any]:
    projection = project_trade_event_log(trade_event_log(repo))
    return list(projection.ledger_projection.allocations)

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
    "AssignedStockEventLog",
    "assigned_stock_event_log",
    "PositionLotSnapshot",
    "RiskPositionView",
    "apply_position_ledger_runtime_config",
    "format_position_cash_secured",
    "format_position_money",
    "list_canonical_position_lot_snapshots",
    "list_position_lot_snapshots",
    "list_position_lot_sync_snapshots",
    "list_position_rows",
    "list_trade_lifecycle_cases",
    "list_trade_lifecycle_evidence",
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
    "trade_event_economic_allocations",
    "trade_event_log",
    "trade_event_projection_preview",
]
