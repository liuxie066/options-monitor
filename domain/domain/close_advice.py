from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from domain.domain.option_position_identity import normalize_account


STRICT_CLOSE_POLICY_VERSION = "strict_profit_capture.v1"
STRICT_MIN_NET_CAPTURE_RATIO = 0.90
STRICT_MIN_DTE = 14
STRICT_MIN_REMAINING_TERM_RATIO = 0.50
STRICT_MAX_CLOSE_COST_TO_STRIKE_RATIO = 0.001
STRICT_MAX_SPREAD_RATIO = 0.30

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


def evaluate_close_advice(inp: CloseAdviceInput) -> dict[str, Any]:
    """Evaluate the only live policy: strict, early, all-in profit capture."""

    option_type = str(inp.option_type or "").strip().lower()
    side = str(inp.side or "").strip().lower()
    premium = safe_float(inp.premium)
    bid = safe_float(inp.bid)
    ask = safe_float(inp.ask)
    dte = safe_int(inp.dte)
    original_dte = safe_int(inp.original_dte)
    multiplier = safe_float(inp.multiplier)
    contracts_open = safe_int(inp.contracts_open)
    strike = safe_float(inp.strike)
    spot = safe_float(inp.spot)
    open_fee = safe_float(inp.estimated_open_fee)
    close_fee = safe_float(inp.estimated_close_fee)
    fee_status = str(inp.fee_calc_status or "").strip().lower()
    fee_basis = str(inp.fee_calc_basis or "").strip()
    currency = str(inp.currency or "").strip().upper()

    flags: list[str] = []
    if not normalize_account(inp.account):
        flags.append("missing_account")
    if not str(inp.position_lot_id or "").strip():
        flags.append("missing_position_lot_id")
    if not str(inp.symbol or "").strip():
        flags.append("missing_symbol")
    if side != "short" or option_type not in {"put", "call"}:
        flags.append("unsupported_position")
    if not str(inp.expiration or "").strip():
        flags.append("missing_expiration")
    _require_positive(premium, flags, "premium")
    _require_positive(multiplier, flags, "multiplier")
    _require_positive(strike, flags, "strike")
    _require_positive(spot, flags, "spot")
    if currency not in {"USD", "HKD"}:
        flags.append("unsupported_currency" if currency else "missing_currency")
    if contracts_open is None:
        flags.append("missing_contracts_open")
    elif contracts_open <= 0:
        flags.append("invalid_contracts_open")
    if dte is None:
        flags.append("missing_dte")
    elif dte < 0:
        flags.append("invalid_dte")
    if original_dte is None:
        flags.append("missing_original_dte")
    elif original_dte <= 0:
        flags.append("invalid_original_dte")
    elif dte is not None and dte > original_dte:
        flags.append("inconsistent_position_dates")
    if bid is None:
        flags.append("missing_bid")
    elif bid < 0:
        flags.append("invalid_bid")
    if ask is None:
        flags.append("missing_ask")
    elif ask <= 0:
        flags.append("invalid_ask")
    if bid is not None and ask is not None and ask < bid:
        flags.append("invalid_spread")
    if (
        open_fee is None
        or close_fee is None
        or fee_status not in FEE_USABLE_STATUSES
        or not fee_basis
    ):
        flags.append("fee_evidence_unavailable")
    elif open_fee < 0 or close_fee < 0:
        flags.append("invalid_fee_estimate")

    if flags:
        return _result(
            inp,
            recommendation=RECOMMENDATION_NOT_EVALUABLE,
            reason="必要的持仓、日期、双边报价或手续费证据不完整，当前无法评估",
            flags=flags,
        )

    assert premium is not None
    assert bid is not None
    assert ask is not None
    assert dte is not None
    assert original_dte is not None
    assert multiplier is not None
    assert contracts_open is not None
    assert strike is not None
    assert spot is not None
    assert open_fee is not None
    assert close_fee is not None

    mid = (bid + ask) / 2.0
    spread_ratio = (ask - bid) / mid if mid > 0 else None
    opening_gross_credit = premium * multiplier * contracts_open
    opening_net_credit = opening_gross_credit - open_fee
    all_in_close_cost = ask * multiplier * contracts_open + close_fee
    strike_notional = strike * multiplier * contracts_open
    if opening_net_credit <= 0 or strike_notional <= 0 or spread_ratio is None:
        return _result(
            inp,
            recommendation=RECOMMENDATION_NOT_EVALUABLE,
            reason="开仓净权利金或名义本金无效，当前无法评估",
            flags=["invalid_economic_denominator"],
            spread_ratio=spread_ratio,
        )

    net_capture_ratio = 1.0 - all_in_close_cost / opening_net_credit
    close_cost_ratio = all_in_close_cost / strike_notional
    remaining_term_ratio = dte / original_dte
    is_otm = spot > strike if option_type == "put" else spot < strike

    failed_gates: list[str] = []
    if not is_otm:
        failed_gates.append("option_not_otm")
    if net_capture_ratio < STRICT_MIN_NET_CAPTURE_RATIO:
        failed_gates.append("net_capture_below_threshold")
    if dte < STRICT_MIN_DTE:
        failed_gates.append("dte_below_threshold")
    if remaining_term_ratio < STRICT_MIN_REMAINING_TERM_RATIO:
        failed_gates.append("remaining_term_below_threshold")
    if close_cost_ratio > STRICT_MAX_CLOSE_COST_TO_STRIKE_RATIO:
        failed_gates.append("close_cost_ratio_above_threshold")
    if spread_ratio > STRICT_MAX_SPREAD_RATIO:
        failed_gates.append("spread_too_wide")

    recommendation = (
        RECOMMENDATION_HOLD if failed_gates else RECOMMENDATION_CLOSE
    )
    return _result(
        inp,
        recommendation=recommendation,
        reason=(
            "已在合约前半程净锁定至少 90% 权利金，且全成本买回与报价质量均达到严格阈值"
            if recommendation == RECOMMENDATION_CLOSE
            else "未同时满足严格收益捕获条件，继续持有"
        ),
        flags=failed_gates,
        spread_ratio=spread_ratio,
        opening_gross_credit=opening_gross_credit,
        opening_net_credit=opening_net_credit,
        all_in_close_cost=all_in_close_cost,
        net_capture_ratio=net_capture_ratio,
        close_cost_ratio=close_cost_ratio,
        remaining_term_ratio=remaining_term_ratio,
        is_otm=is_otm,
    )


def _require_positive(
    value: float | None,
    flags: list[str],
    name: str,
) -> None:
    if value is None:
        flags.append(f"missing_{name}")
    elif value <= 0:
        flags.append(f"invalid_{name}")


def _result(
    inp: CloseAdviceInput,
    *,
    recommendation: str,
    reason: str,
    flags: list[str],
    spread_ratio: float | None = None,
    opening_gross_credit: float | None = None,
    opening_net_credit: float | None = None,
    all_in_close_cost: float | None = None,
    net_capture_ratio: float | None = None,
    close_cost_ratio: float | None = None,
    remaining_term_ratio: float | None = None,
    is_otm: bool | None = None,
) -> dict[str, Any]:
    is_close = recommendation == RECOMMENDATION_CLOSE
    is_evaluable = recommendation != RECOMMENDATION_NOT_EVALUABLE
    bid = safe_float(inp.bid)
    ask = safe_float(inp.ask)
    close_mid = (
        (bid + ask) / 2.0 if bid is not None and ask is not None else None
    )
    estimated_pnl = (
        opening_net_credit - all_in_close_cost
        if opening_net_credit is not None and all_in_close_cost is not None
        else None
    )
    evidence_status = (
        DECISION_EVIDENCE_COMPLETE
        if is_evaluable
        else DECISION_EVIDENCE_NOT_EVALUABLE
    )
    basis = flags or ["strict_profit_capture_all_gates_passed"]
    return {
        "account": normalize_account(inp.account),
        "position_lot_id": str(inp.position_lot_id or "").strip() or None,
        "symbol": str(inp.symbol or "").strip().upper(),
        "option_type": str(inp.option_type or "").strip().lower(),
        "expiration": inp.expiration,
        "strike": safe_float(inp.strike),
        "contracts_open": safe_int(inp.contracts_open),
        "premium": safe_float(inp.premium),
        "bid": bid,
        "ask": ask,
        "close_mid": close_mid,
        "dte": safe_int(inp.dte),
        "original_dte": safe_int(inp.original_dte),
        "multiplier": safe_float(inp.multiplier),
        "spot": safe_float(inp.spot),
        "currency": str(inp.currency or "").strip().upper() or None,
        "is_otm": is_otm,
        "spread_ratio": spread_ratio,
        "opening_gross_credit": opening_gross_credit,
        "estimated_open_fee": safe_float(inp.estimated_open_fee),
        "opening_net_credit": opening_net_credit,
        "estimated_close_fee": safe_float(inp.estimated_close_fee),
        "all_in_close_cost": all_in_close_cost,
        "net_capture_ratio": net_capture_ratio,
        "close_cost_ratio": close_cost_ratio,
        "remaining_term_ratio": remaining_term_ratio,
        "estimated_pnl_if_close_net": estimated_pnl,
        "fee_calc_status": inp.fee_calc_status,
        "fee_calc_basis": inp.fee_calc_basis,
        "reason": reason,
        "recommendation_state": recommendation,
        "policy_version": STRICT_CLOSE_POLICY_VERSION,
        "decision_basis": ";".join(basis),
        "decision_evidence_status": evidence_status,
        "evaluation_status": "priced" if is_evaluable else "not_evaluable",
        "quote_status": "priced" if is_evaluable else "not_evaluable",
        "data_quality_flags": ";".join(flags),
    }


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
            -(safe_float(row.get("net_capture_ratio")) or 0.0),
            safe_float(row.get("all_in_close_cost")) or math.inf,
            str(row.get("symbol") or ""),
        ),
    )


def select_close_advice_notification_rows(
    rows: list[dict[str, Any]],
    *,
    max_items_per_account: int = 5,
) -> list[dict[str, Any]]:
    """Select only strict CLOSE rows."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in sort_advice_rows(rows):
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
