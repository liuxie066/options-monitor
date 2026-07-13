from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from domain.domain.risk_capacity import (
    compute_short_call_locked_shares,
    compute_short_put_cash_secured,
)
from domain.domain.symbol_identity import symbol_market
from domain.domain.trade_contract_identity import normalize_contract_expiration
from src.infrastructure.io_utils import read_json, safe_read_csv


OUTPUT_COLUMNS = [
    "account",
    "position_lot_id",
    "symbol",
    "option_type",
    "expiration",
    "strike",
    "strategy_family",
    "strategy_profile",
    "formal_close_action",
    "formal_tier",
    "reallocation_status",
    "reallocation_reason",
    "replacement_symbol",
    "replacement_contract_symbol",
    "replacement_expiration",
    "replacement_strike",
    "replacement_rank",
    "current_annualized_return",
    "replacement_annualized_return",
    "replacement_annualized_return_after_slippage",
    "current_daily_yield",
    "replacement_daily_yield",
    "daily_yield_advantage",
    "capacity_unit",
    "capacity_before",
    "released_capacity",
    "replacement_capacity_required",
    "additional_capacity_beyond_release",
    "post_release_headroom",
    "close_fee",
    "replacement_open_fee",
    "replacement_spread_slippage",
    "switch_cost",
    "recovery_days",
    "recovery_horizon_days",
]


def write_close_advice_reallocation_shadow(
    *,
    report_dir: Path,
    context_path: Path,
    account: str,
) -> dict[str, Any]:
    report_dir = Path(report_dir)
    close_path = report_dir / "close_advice.csv"
    capacity_path = report_dir / "portfolio_capacity_shadow.csv"
    output_path = report_dir / "close_advice_reallocation_shadow.csv"

    close_rows = safe_read_csv(close_path).to_dict("records")
    candidate_rows = safe_read_csv(capacity_path).to_dict("records")
    context = read_json(context_path, default={})
    positions = context.get("open_positions_min") if isinstance(context, dict) else None
    position_rows = positions if isinstance(positions, list) else []

    rows = [
        _evaluate_row(
            close_row,
            candidates=candidate_rows,
            positions=position_rows,
            context=context if isinstance(context, dict) else {},
            account=account,
            capacity_evidence_available=capacity_path.exists(),
        )
        for close_row in close_rows
    ]
    pd.DataFrame(rows, columns=OUTPUT_COLUMNS).to_csv(output_path, index=False)
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("reallocation_status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return {
        "rows": len(rows),
        "status_counts": dict(sorted(counts.items())),
        "csv": str(output_path),
        "shadow_only": True,
    }


def _evaluate_row(
    close_row: dict[str, Any],
    *,
    candidates: list[dict[str, Any]],
    positions: list[Any],
    context: dict[str, Any],
    account: str,
    capacity_evidence_available: bool,
) -> dict[str, Any]:
    family = _family(close_row)
    result = _base_result(close_row, family=family, account=account)
    if _is_combo_leg(close_row):
        return _finish(result, "not_evaluable", "combo_yield_single_leg_reallocation_unsupported")
    if str(close_row.get("evaluation_status") or "").strip().lower() != "priced":
        return _finish(result, "not_evaluable", "formal_close_advice_not_priced")
    if family not in {"sell_put", "sell_call"}:
        return _finish(result, "not_evaluable", "strategy_family_unsupported")
    if not capacity_evidence_available:
        return _finish(result, "not_evaluable", "portfolio_capacity_shadow_missing")

    position_matches = [row for row in positions if isinstance(row, dict) and _same_contract(row, close_row)]
    if len(position_matches) != 1:
        reason = "position_context_missing" if not position_matches else "position_context_ambiguous"
        return _finish(result, "not_evaluable", reason)
    position = position_matches[0]
    released, unit = _released_capacity(position, family=family, context=context)
    if released is None:
        return _finish(result, "not_evaluable", "released_capacity_not_evaluable")
    result["capacity_unit"] = unit
    result["released_capacity"] = released

    replacement, selection_reason = _select_replacement(
        close_row,
        candidates=candidates,
        family=family,
        released_capacity=released,
    )
    if replacement is None:
        if selection_reason == "candidate_capacity_not_evaluable":
            return _finish(result, "not_evaluable", selection_reason)
        status = "exit_without_replacement" if _formal_close_is_actionable(close_row) else "no_feasible_replacement"
        return _finish(result, status, selection_reason)

    result.update(_replacement_identity(replacement))
    before = _number(replacement.get("capacity_before"))
    required = _number(replacement.get("capacity_required"))
    assert before is not None and required is not None
    result.update(
        capacity_before=before,
        replacement_capacity_required=required,
        additional_capacity_beyond_release=max(0.0, required - released),
        post_release_headroom=max(0.0, before + released - required),
    )

    economics = _switch_economics(close_row, replacement)
    result.update(economics)
    daily_yield_advantage = economics.get("daily_yield_advantage")
    recovery_horizon_days = economics.get("recovery_horizon_days")
    if daily_yield_advantage is None or recovery_horizon_days is None:
        return _finish(result, "not_evaluable", "switch_economics_incomplete")
    recovery_days = economics.get("recovery_days")
    if (
        float(daily_yield_advantage) > 0
        and recovery_days is not None
        and float(recovery_days) <= float(recovery_horizon_days)
    ):
        return _finish(result, "review_switch", "higher_efficiency_recovers_switch_cost_within_horizon")
    return _finish(result, "hold_more_efficient", "switch_cost_not_recovered_within_horizon")


def _base_result(close_row: dict[str, Any], *, family: str, account: str) -> dict[str, Any]:
    return {
        "account": _text(close_row.get("account")) or str(account).strip().lower(),
        "position_lot_id": _text(close_row.get("position_lot_id")),
        "symbol": _text(close_row.get("symbol")),
        "option_type": _text(close_row.get("option_type")),
        "expiration": _text(close_row.get("expiration")),
        "strike": _number(close_row.get("strike")),
        "strategy_family": family,
        "strategy_profile": _text(close_row.get("strategy_profile")),
        "formal_close_action": _text(close_row.get("close_action")),
        "formal_tier": _text(close_row.get("tier")),
    }


def _finish(result: dict[str, Any], status: str, reason: str) -> dict[str, Any]:
    result["reallocation_status"] = status
    result["reallocation_reason"] = reason
    return result


def _replacement_identity(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "replacement_symbol": _text(row.get("symbol")),
        "replacement_contract_symbol": _text(row.get("contract_symbol") or row.get("code")),
        "replacement_expiration": _text(row.get("expiration") or row.get("expiration_ymd")),
        "replacement_strike": _number(row.get("strike")),
        "replacement_rank": _integer(row.get("allocation_rank")),
    }


def _select_replacement(
    current: dict[str, Any],
    *,
    candidates: list[dict[str, Any]],
    family: str,
    released_capacity: float,
) -> tuple[dict[str, Any] | None, str]:
    current_account = _text(current.get("account")).lower()
    current_symbol = _text(current.get("symbol")).upper()
    current_profile = _text(current.get("strategy_profile")).lower()
    current_market = symbol_market(current_symbol)
    current_currency = _text(current.get("currency")).upper()
    eligible_found = False
    capacity_gap = False
    for candidate in candidates:
        if _text(candidate.get("account")).lower() != current_account:
            continue
        if _family(candidate) != family:
            continue
        candidate_symbol = _text(candidate.get("symbol")).upper()
        if family == "sell_call" and candidate_symbol != current_symbol:
            continue
        if symbol_market(candidate_symbol) != current_market:
            continue
        if _text(candidate.get("strategy_profile")).lower() != current_profile:
            continue
        candidate_currency = _text(candidate.get("currency")).upper()
        if current_currency and candidate_currency and candidate_currency != current_currency:
            continue
        if _same_contract(candidate, current):
            continue
        eligible_found = True
        before = _number(candidate.get("capacity_before"))
        required = _number(candidate.get("capacity_required"))
        if before is None or required is None or required <= 0:
            capacity_gap = True
            continue
        if before + released_capacity + 1e-9 >= required:
            return candidate, "replacement_found"
    if capacity_gap:
        return None, "candidate_capacity_not_evaluable"
    if eligible_found:
        return None, "capacity_insufficient_after_release"
    return None, "no_matching_filtered_candidate"


def _released_capacity(position: dict[str, Any], *, family: str, context: dict[str, Any]) -> tuple[float | None, str]:
    if family == "sell_call":
        released = compute_short_call_locked_shares(
            contracts_open=position.get("contracts_open"),
            contracts_total=position.get("contracts"),
            multiplier=position.get("multiplier"),
            underlying_share_locked=position.get("underlying_share_locked"),
        )
        return (float(released), "shares") if released is not None and released > 0 else (None, "shares")

    native = compute_short_put_cash_secured(
        contracts_open=position.get("contracts_open"),
        contracts_total=position.get("contracts"),
        cash_secured_amount=position.get("cash_secured_amount"),
        strike=position.get("strike"),
        multiplier=position.get("multiplier"),
    )
    rate = _cny_rate(position.get("currency"), context.get("exchange_rates"))
    if native is None or native <= 0 or rate is None:
        return None, "CNY"
    return float(native) * rate, "CNY"


def _switch_economics(current: dict[str, Any], replacement: dict[str, Any]) -> dict[str, Any]:
    current_ann = _number(current.get("remaining_annualized_return"))
    replacement_ann = _number(
        replacement.get("annualized_net_return_on_cash_basis")
        if _family(replacement) == "sell_put"
        else replacement.get("annualized_net_premium_return")
    )
    current_dte = _number(current.get("dte"))
    replacement_dte = _number(replacement.get("dte"))
    remaining_premium = _number(current.get("remaining_premium"))
    close_fee = _number(current.get("close_fee") or current.get("buy_to_close_fee"))
    contracts = max(1, _integer(replacement.get("allocated_contracts") or replacement.get("contracts")) or 1)
    open_fee_per_contract = _number(replacement.get("futu_fee"))
    gross_per_contract = _number(replacement.get("gross_income"))
    net_per_contract = _number(replacement.get("net_income"))
    mid = _number(replacement.get("mid"))
    bid = _number(replacement.get("bid"))
    multiplier = _number(replacement.get("multiplier"))
    if (
        current_ann is None
        or replacement_ann is None
        or current_dte is None
        or current_dte <= 0
        or replacement_dte is None
        or replacement_dte <= 0
        or remaining_premium is None
        or close_fee is None
        or open_fee_per_contract is None
        or gross_per_contract is None
        or net_per_contract is None
        or net_per_contract <= 0
        or mid is None
        or bid is None
        or multiplier is None
    ):
        return {
            "current_annualized_return": current_ann,
            "replacement_annualized_return": replacement_ann,
            "current_daily_yield": current_ann / 365.0 if current_ann is not None else None,
            "replacement_daily_yield": replacement_ann / 365.0 if replacement_ann is not None else None,
            "daily_yield_advantage": None,
            "recovery_days": None,
            "recovery_horizon_days": None,
        }

    open_fee = open_fee_per_contract * contracts
    gross_income = gross_per_contract * contracts
    net_income = net_per_contract * contracts
    slippage = max(0.0, mid - bid) * multiplier * contracts
    adjusted_ann = replacement_ann * max(0.0, net_income - slippage) / net_income
    current_daily_yield = current_ann / 365.0
    replacement_daily_yield = adjusted_ann / 365.0
    gross_daily_advantage = gross_income / replacement_dte - remaining_premium / current_dte
    switch_cost = close_fee + open_fee + slippage
    recovery_days = switch_cost / gross_daily_advantage if gross_daily_advantage > 0 else None
    return {
        "current_annualized_return": current_ann,
        "replacement_annualized_return": replacement_ann,
        "replacement_annualized_return_after_slippage": adjusted_ann,
        "current_daily_yield": current_daily_yield,
        "replacement_daily_yield": replacement_daily_yield,
        "daily_yield_advantage": replacement_daily_yield - current_daily_yield,
        "close_fee": close_fee,
        "replacement_open_fee": open_fee,
        "replacement_spread_slippage": slippage,
        "switch_cost": switch_cost,
        "recovery_days": recovery_days,
        "recovery_horizon_days": min(current_dte, replacement_dte),
    }


def _same_contract(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_lot_id = _text(left.get("position_lot_id") or left.get("record_id"))
    right_lot_id = _text(right.get("position_lot_id") or right.get("record_id"))
    if left_lot_id and right_lot_id:
        return left_lot_id == right_lot_id
    if _text(left.get("account")).lower() != _text(right.get("account")).lower():
        return False
    if _text(left.get("symbol")).upper() != _text(right.get("symbol")).upper():
        return False
    if _option_type(left) != _option_type(right):
        return False
    left_expiration = normalize_contract_expiration(left.get("expiration_ymd")) or normalize_contract_expiration(
        left.get("expiration")
    )
    right_expiration = normalize_contract_expiration(right.get("expiration_ymd")) or normalize_contract_expiration(
        right.get("expiration")
    )
    if left_expiration != right_expiration:
        return False
    left_strike = _number(left.get("strike"))
    right_strike = _number(right.get("strike"))
    return left_strike is not None and right_strike is not None and abs(left_strike - right_strike) <= 1e-6


def _family(row: dict[str, Any]) -> str:
    raw = _text(row.get("strategy_family")).lower()
    if raw in {"sell_put", "put"}:
        return "sell_put"
    if raw in {"sell_call", "covered_call", "call"}:
        return "sell_call"
    option_type = _option_type(row)
    return "sell_put" if option_type == "put" else "sell_call" if option_type == "call" else ""


def _option_type(row: dict[str, Any]) -> str:
    raw = _text(row.get("option_type") or row.get("mode")).lower()
    return "put" if raw in {"put", "p"} else "call" if raw in {"call", "c"} else raw


def _is_combo_leg(row: dict[str, Any]) -> bool:
    return bool(
        _text(row.get("strategy_group_id"))
        or _text(row.get("yield_enhancement_mode"))
        or _text(row.get("strategy")).lower() in {"yield_enhancement", "combo_yield"}
        or _text(row.get("strategy_family")).lower() == "combo_yield"
    )


def _formal_close_is_actionable(row: dict[str, Any]) -> bool:
    return _text(row.get("close_action")).lower() in {"close", "close_put_keep_call"}


def _cny_rate(currency: Any, raw_rates: Any) -> float | None:
    ccy = _text(currency).upper()
    if ccy == "CNY":
        return 1.0
    rates = raw_rates.get("rates") if isinstance(raw_rates, dict) and isinstance(raw_rates.get("rates"), dict) else raw_rates
    if not isinstance(rates, dict):
        return None
    return _number(rates.get("USDCNY" if ccy == "USD" else "HKDCNY" if ccy == "HKD" else ""))


def _text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


__all__ = ["write_close_advice_reallocation_shadow"]
