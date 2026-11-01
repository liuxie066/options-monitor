from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from domain.domain.option_position_identity import normalize_account


STRICT_CLOSE_POLICY_VERSION = "remaining_yield_capture.v3"
STRICT_MIN_NET_CAPTURE_RATIO = 0.80
STRICT_MAX_REMAINING_ANNUALIZED_RETURN = 0.10
EXPIRY_HOLD_MAX_TRADING_SESSIONS = 3
EXPIRY_HOLD_MAX_ABS_DELTA = 0.05

RECOMMENDATION_CLOSE = "close"
RECOMMENDATION_HOLD = "hold"
RECOMMENDATION_NOT_EVALUABLE = "not_evaluable"

DECISION_EVIDENCE_COMPLETE = "complete"
DECISION_EVIDENCE_NOT_EVALUABLE = "not_evaluable"

FEE_USABLE_STATUSES = frozenset(
    {"schedule_estimate", "conservative_estimate"}
)


def safe_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        if value in (None, ""):
            return None
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def safe_int(value: Any) -> int | None:
    parsed = safe_float(value)
    if parsed is None or not parsed.is_integer():
        return None
    return int(parsed)


@dataclass(frozen=True)
class CloseAdviceInput:
    account: str
    position_lot_id: str | None
    symbol: str
    option_type: str
    side: str
    expiration: str | None
    strike: float | None
    contracts_open: int | None
    premium: float | None
    bid: float | None = None
    ask: float | None = None
    dte: int | None = None
    multiplier: float | None = None
    spot: float | None = None
    currency: str | None = None
    original_dte: int | None = None
    estimated_open_fee: float | None = None
    estimated_close_fee: float | None = None
    fee_calc_status: str | None = None
    fee_calc_basis: str | None = None
    delta: float | None = None
    remaining_trading_sessions: int | None = None
    remaining_trading_sessions_min: int | None = None
    remaining_trading_sessions_max: int | None = None
    input_source: str = "ledger"
    input_id: str | None = None
    input_as_of_utc: str | None = None


class PositionAdviceUnavailableError(RuntimeError):
    """The optional evaluator is absent or cannot be loaded."""


def require_position_advice_plugin() -> Any:
    """Import only an installed package; never add bundled source to sys.path."""
    from importlib import import_module

    try:
        plugin = import_module("om_position_advice_plugin")
        if not callable(plugin.evaluate_close_advice) or not callable(plugin.format_close_advice_reason):
            raise ImportError("Position Advice package has no compatible policy/presentation API")
        return plugin
    except (ImportError, AttributeError) as exc:
        raise PositionAdviceUnavailableError(
            "POSITION_ADVICE_UNAVAILABLE: optional package om-position-advice-plugin "
            "is not installed or could not be imported"
        ) from exc


def evaluate_close_advice(inp: CloseAdviceInput) -> dict[str, Any]:
    """Evaluate through the installed optional policy, loaded on first use."""
    return require_position_advice_plugin().evaluate_close_advice(inp)


def sort_advice_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows or [],
        key=lambda row: (
            0
            if str(row.get("recommendation_state") or "").strip().lower()
            == RECOMMENDATION_CLOSE
            else 1
            if str(row.get("recommendation_state") or "").strip().lower()
            == RECOMMENDATION_HOLD
            else 2,
            safe_float(row.get("remaining_max_annualized_return"))
            if safe_float(row.get("remaining_max_annualized_return")) is not None
            else math.inf,
            -(safe_float(row.get("net_capture_ratio")) or 0.0),
            str(row.get("account") or ""),
            str(row.get("symbol") or ""),
            str(row.get("position_lot_id") or ""),
            str(row.get("input_id") or ""),
        ),
    )


def has_complete_close_metrics(row: dict[str, Any]) -> bool:
    basis = safe_float(row.get("capital_basis"))
    annualized = safe_float(row.get("remaining_max_annualized_return"))
    capture = safe_float(row.get("net_capture_ratio"))
    return (
        basis is not None
        and basis > 0
        and annualized is not None
        and annualized >= 0
        and capture is not None
    )


def select_close_advice_notification_rows(
    rows: list[dict[str, Any]],
    *,
    max_items_per_account: int = 5,
) -> list[dict[str, Any]]:
    """Select only current-policy CLOSE rows with complete decision metrics."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in sort_advice_rows(rows):
        if "assessment_mode" in row and row["assessment_mode"] != "current":
            continue
        if str(row.get("policy_version") or "").strip() != STRICT_CLOSE_POLICY_VERSION:
            continue
        if str(row.get("evaluation_status") or "").strip().lower() != "priced":
            continue
        if (
            str(row.get("decision_evidence_status") or "").strip().lower()
            != DECISION_EVIDENCE_COMPLETE
        ):
            continue
        if (
            str(row.get("recommendation_state") or "").strip().lower()
            != RECOMMENDATION_CLOSE
        ):
            continue
        if not has_complete_close_metrics(row):
            continue
        account = normalize_account(row.get("account")) or "当前账户"
        grouped.setdefault(account, []).append(row)

    selected: list[dict[str, Any]] = []
    for account_rows in grouped.values():
        selected.extend(
            account_rows[:max_items_per_account]
            if max_items_per_account > 0
            else account_rows
        )
    return selected
