"""Pure v3 policy; all I/O and observation eligibility stay with OM."""
from __future__ import annotations

import math
from typing import Any

from domain.domain.close_advice import (
    CloseAdviceInput,
    DECISION_EVIDENCE_COMPLETE,
    DECISION_EVIDENCE_NOT_EVALUABLE,
    EXPIRY_HOLD_MAX_ABS_DELTA,
    EXPIRY_HOLD_MAX_TRADING_SESSIONS,
    FEE_USABLE_STATUSES,
    RECOMMENDATION_CLOSE,
    RECOMMENDATION_HOLD,
    RECOMMENDATION_NOT_EVALUABLE,
    STRICT_CLOSE_POLICY_VERSION,
    STRICT_MAX_REMAINING_ANNUALIZED_RETURN,
    STRICT_MIN_NET_CAPTURE_RATIO,
    safe_float,
    safe_int,
)
from domain.domain.option_position_identity import normalize_account
from domain.domain.trade_contract_identity import contract_share_quantity

from .presentation import format_close_advice_reason


def evaluate_close_advice(inp: CloseAdviceInput) -> dict[str, Any]:
    """Evaluate whether the maximum remaining option return merits holding."""

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
    delta = safe_float(inp.delta)
    trading_sessions = safe_int(inp.remaining_trading_sessions)
    sessions_min = safe_int(inp.remaining_trading_sessions_min)
    sessions_max = safe_int(inp.remaining_trading_sessions_max)
    if sessions_min is None and sessions_max is None:
        sessions_min = sessions_max = trading_sessions
    sessions_valid = (
        sessions_min is not None and sessions_max is not None
        and 0 <= sessions_min <= sessions_max
    )

    flags: list[str] = []
    if not normalize_account(inp.account):
        flags.append("missing_account")
    if inp.input_source == "ledger":
        if not str(inp.position_lot_id or "").strip():
            flags.append("missing_position_lot_id")
    elif inp.input_source == "user_provided":
        if not str(inp.input_id or "").strip():
            flags.append("missing_input_id")
        if str(inp.position_lot_id or "").strip():
            flags.append("manual_position_has_ledger_lot")
    else:
        flags.append("invalid_input_source")
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
    elif dte <= 0:
        flags.append("invalid_dte")
    if inp.original_dte is not None and original_dte is None:
        flags.append("invalid_original_dte")
    elif original_dte is not None and original_dte <= 0:
        flags.append("invalid_original_dte")
    elif original_dte is not None and dte is not None and dte > original_dte:
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

    shares = None
    if contracts_open is not None and contracts_open > 0 and multiplier is not None and multiplier > 0:
        try:
            shares = contract_share_quantity(inp.contracts_open, inp.multiplier)
        except ValueError:
            flags.append("invalid_contract_units")

    if flags:
        spread_ratio = None
        if bid is not None and ask is not None and 0 <= bid <= ask and ask > 0:
            mid = (bid + ask) / 2.0
            if math.isfinite(mid) and mid > 0:
                spread_ratio = (ask - bid) / mid
        is_otm = None
        if (
            option_type in {"put", "call"}
            and strike is not None and strike > 0
            and spot is not None and spot > 0
        ):
            is_otm = spot > strike if option_type == "put" else spot < strike
        return _result(
            inp,
            recommendation=RECOMMENDATION_NOT_EVALUABLE,
            flags=flags,
            spread_ratio=spread_ratio,
            is_otm=is_otm,
        )

    assert shares is not None
    assert premium is not None
    assert bid is not None
    assert ask is not None
    assert dte is not None
    assert multiplier is not None
    assert contracts_open is not None
    assert strike is not None
    assert spot is not None
    assert open_fee is not None
    assert close_fee is not None

    mid = (bid + ask) / 2.0
    spread_ratio = (ask - bid) / mid if mid > 0 else None
    opening_gross_credit = premium * shares
    opening_net_credit = opening_gross_credit - open_fee
    all_in_close_cost = ask * shares + close_fee
    strike_notional = strike * shares
    capital_basis = (strike if option_type == "put" else spot) * shares
    remaining_term_ratio = dte / original_dte if original_dte is not None else None
    if (
        opening_net_credit <= 0
        or capital_basis <= 0
        or not all(
            math.isfinite(value)
            for value in (
                opening_gross_credit,
                opening_net_credit,
                all_in_close_cost,
                strike_notional,
                capital_basis,
                mid,
                spread_ratio,
            )
        )
    ):
        return _result(
            inp,
            recommendation=RECOMMENDATION_NOT_EVALUABLE,
            flags=["invalid_economic_denominator"],
        )

    net_capture_ratio = 1.0 - all_in_close_cost / opening_net_credit
    close_cost_ratio = all_in_close_cost / strike_notional
    remaining_max_annualized_return = all_in_close_cost / capital_basis * 365 / dte
    if not all(
        math.isfinite(value)
        for value in (
            net_capture_ratio,
            close_cost_ratio,
            remaining_max_annualized_return,
        )
    ):
        return _result(
            inp,
            recommendation=RECOMMENDATION_NOT_EVALUABLE,
            flags=["invalid_economic_result"],
        )
    is_otm = spot > strike if option_type == "put" else spot < strike

    failed_gates: list[str] = []
    if not is_otm:
        failed_gates.append("option_not_otm")
    if net_capture_ratio < STRICT_MIN_NET_CAPTURE_RATIO:
        failed_gates.append("net_capture_below_threshold")
    if remaining_max_annualized_return > STRICT_MAX_REMAINING_ANNUALIZED_RETURN:
        failed_gates.append("remaining_annualized_above_threshold")
    if failed_gates:
        recommendation = RECOMMENDATION_HOLD
    elif (delta is not None and abs(delta) <= 1 and abs(delta) > EXPIRY_HOLD_MAX_ABS_DELTA) or (
        sessions_valid and sessions_min > EXPIRY_HOLD_MAX_TRADING_SESSIONS
    ):
        recommendation = RECOMMENDATION_CLOSE
    elif (
        delta is not None
        and abs(delta) <= EXPIRY_HOLD_MAX_ABS_DELTA
        and sessions_valid
        and sessions_max <= EXPIRY_HOLD_MAX_TRADING_SESSIONS
    ):
        recommendation = RECOMMENDATION_HOLD
        failed_gates.append("near_expiry_far_otm_hold")
    else:
        recommendation = RECOMMENDATION_NOT_EVALUABLE
        failed_gates.append("expiry_hold_evidence_unavailable")
    return _result(
        inp,
        recommendation=recommendation,
        flags=failed_gates,
        spread_ratio=spread_ratio,
        opening_gross_credit=opening_gross_credit,
        opening_net_credit=opening_net_credit,
        all_in_close_cost=all_in_close_cost,
        net_capture_ratio=net_capture_ratio,
        close_cost_ratio=close_cost_ratio,
        remaining_term_ratio=remaining_term_ratio,
        capital_basis=capital_basis,
        remaining_max_annualized_return=remaining_max_annualized_return,
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
    flags: list[str],
    spread_ratio: float | None = None,
    opening_gross_credit: float | None = None,
    opening_net_credit: float | None = None,
    all_in_close_cost: float | None = None,
    net_capture_ratio: float | None = None,
    close_cost_ratio: float | None = None,
    remaining_term_ratio: float | None = None,
    capital_basis: float | None = None,
    remaining_max_annualized_return: float | None = None,
    is_otm: bool | None = None,
) -> dict[str, Any]:
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
    basis = flags or ["remaining_yield_capture_all_gates_passed"]
    return {
        "account": normalize_account(inp.account),
        "position_lot_id": str(inp.position_lot_id or "").strip() or None,
        "input_source": inp.input_source,
        "input_id": inp.input_id,
        "input_as_of_utc": inp.input_as_of_utc,
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
        "delta": safe_float(inp.delta),
        "remaining_trading_sessions": safe_int(inp.remaining_trading_sessions),
        "remaining_trading_sessions_min": safe_int(inp.remaining_trading_sessions_min),
        "remaining_trading_sessions_max": safe_int(inp.remaining_trading_sessions_max),
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
        "capital_basis": capital_basis,
        "remaining_max_annualized_return": remaining_max_annualized_return,
        "estimated_pnl_if_close_net": estimated_pnl,
        "fee_calc_status": inp.fee_calc_status,
        "fee_calc_basis": inp.fee_calc_basis,
        "reason": format_close_advice_reason(recommendation=recommendation, flags=flags),
        "recommendation_state": recommendation,
        "policy_version": STRICT_CLOSE_POLICY_VERSION,
        "decision_basis": ";".join(basis),
        "decision_evidence_status": evidence_status,
        "evaluation_status": "priced" if is_evaluable else "not_evaluable",
        "quote_status": "priced" if is_evaluable else "not_evaluable",
        "data_quality_flags": ";".join(flags),
    }
