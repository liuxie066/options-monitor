from __future__ import annotations

from datetime import date
import math
from typing import Any, Iterable, Mapping

from domain.domain.symbol_identity import canonical_symbol, symbol_currency


def project_one_contract(
    *,
    candidate: Mapping[str, Any],
    strategy_mode: str,
    portfolio: Mapping[str, Any],
    option_positions: Mapping[str, Any],
    position_rows: Iterable[Mapping[str, Any]],
    portfolio_total_cny: Any,
    shares_by_symbol: Mapping[str, Any],
    cny_per_currency: Mapping[str, Any],
) -> dict[str, Any]:
    """Project the deterministic marginal effect of exactly one contract.

    Absolute PM values, share quantities and FX are calculation-only inputs.
    The returned object contains current weights, ratios and contract counts;
    it never contains assignment notional, share quantity or an invented
    after-trade portfolio weight.
    """

    candidate_id = str(candidate.get("candidate_id") or "").strip()
    symbol = canonical_symbol(candidate.get("symbol")) or str(
        candidate.get("symbol") or ""
    ).strip().upper()
    mode = str(strategy_mode or "").strip().lower()
    if mode not in {"put", "call"}:
        raise ValueError("projection strategy mode must be put or call")

    currency = str(
        candidate.get("currency") or symbol_currency(symbol) or ""
    ).strip().upper()
    asset_weights = portfolio.get("asset_weights")
    asset_weight_map = (
        asset_weights if isinstance(asset_weights, Mapping) else {}
    )
    currency_weights = portfolio.get("currency_weights")
    currency_weight_map = (
        currency_weights if isinstance(currency_weights, Mapping) else {}
    )
    portfolio_complete = str(portfolio.get("status") or "") in {
        "ready",
        "degraded",
    }
    current_symbol_weight = (
        _finite_float(asset_weight_map.get(symbol, 0.0))
        if portfolio_complete and isinstance(asset_weights, Mapping)
        else None
    )
    current_currency_weight = (
        _finite_float(currency_weight_map.get(currency, 0.0))
        if portfolio_complete and isinstance(currency_weights, Mapping)
        else None
    )
    cash_and_mmf_weight = (
        _finite_float(portfolio.get("cash_and_mmf_weight"))
        if portfolio_complete
        else None
    )

    context_gaps = _gap_list(portfolio.get("gaps"))
    context_gaps.extend(_gap_list(option_positions.get("gaps")))
    calculation_gaps: list[str] = []

    strike = _positive_float(candidate.get("strike"))
    multiplier = _positive_float(candidate.get("multiplier"))
    expiry = _date(candidate.get("expiry"))
    if strike is None:
        calculation_gaps.append("candidate_strike_missing")
    if multiplier is None:
        calculation_gaps.append("candidate_multiplier_missing")
    if not currency:
        calculation_gaps.append("candidate_currency_missing")
    if expiry is None:
        calculation_gaps.append("candidate_expiry_missing")

    assignment_exposure_ratio: float | None = None
    call_away_fraction: float | None = None
    if mode == "put":
        total_cny = _positive_float(portfolio_total_cny)
        if total_cny is None:
            calculation_gaps.append("portfolio_total_cny_missing")
        cny_rate = _positive_float(cny_per_currency.get(currency))
        if currency and cny_rate is None:
            calculation_gaps.append(f"fx_rate_missing:{currency}")
        if (
            strike is not None
            and multiplier is not None
            and total_cny is not None
            and cny_rate is not None
        ):
            assignment_exposure_ratio = _rounded_ratio(
                strike * multiplier * cny_rate,
                total_cny,
            )
    else:
        held_shares = _positive_float(shares_by_symbol.get(symbol))
        if held_shares is None:
            calculation_gaps.append("covered_call_shares_missing")
        if multiplier is not None and held_shares is not None:
            call_away_fraction = _rounded_ratio(multiplier, held_shares)

    overlaps: dict[str, int | None]
    if str(option_positions.get("status") or "") != "ready":
        overlaps = _unavailable_overlays()
        calculation_gaps.append("option_positions_unavailable")
    elif expiry is None:
        overlaps = _unavailable_overlays()
    else:
        try:
            overlaps = _option_overlays(
                position_rows,
                symbol=symbol,
                mode=mode,
                expiry=expiry,
            )
        except ValueError:
            overlaps = _unavailable_overlays()
            calculation_gaps.append("option_positions_invalid")

    verified_structures = [
        dict(item)
        for item in option_positions.get("verified_structures") or []
        if isinstance(item, Mapping)
        and str(item.get("symbol") or "") == symbol
    ]
    gaps = sorted(set((*context_gaps, *calculation_gaps)))
    return {
        "candidate_id": candidate_id,
        "symbol": symbol,
        "strategy_mode": mode,
        "direction": (
            "assignment_adds_shares"
            if mode == "put"
            else "call_away_removes_shares"
        ),
        "current_symbol_weight": current_symbol_weight,
        "current_currency": currency or None,
        "current_currency_weight": current_currency_weight,
        "cash_and_mmf_weight": cash_and_mmf_weight,
        "assignment_exposure_ratio": assignment_exposure_ratio,
        "call_away_fraction": call_away_fraction,
        "same_obligation_current_contracts": overlaps[
            "same_obligation_current_contracts"
        ],
        "same_obligation_after_add_contracts": overlaps[
            "same_obligation_after_add_contracts"
        ],
        "long_call_current_contracts": overlaps[
            "long_call_current_contracts"
        ],
        "long_put_current_contracts": overlaps[
            "long_put_current_contracts"
        ],
        "expiry": expiry.isoformat() if expiry is not None else None,
        "exact_expiry_current_contracts": overlaps[
            "exact_expiry_current_contracts"
        ],
        "exact_expiry_after_add_contracts": overlaps[
            "exact_expiry_after_add_contracts"
        ],
        "near_expiry_7d_current_contracts": overlaps[
            "near_expiry_7d_current_contracts"
        ],
        "near_expiry_7d_after_add_contracts": overlaps[
            "near_expiry_7d_after_add_contracts"
        ],
        "verified_structures": verified_structures,
        "calculation_complete": not calculation_gaps,
        "scope_ceiling": "needs_review" if gaps else None,
        "gaps": gaps,
    }


def project_all_candidates(
    *,
    candidates: Mapping[str, Any],
    portfolio: Mapping[str, Any],
    option_positions: Mapping[str, Any],
    position_rows: Iterable[Mapping[str, Any]],
    portfolio_total_cny: Any,
    shares_by_symbol: Mapping[str, Any],
    cny_per_currency: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Project every accepted candidate without changing its sealed order."""

    materialized_positions = [dict(row) for row in position_rows]
    out: dict[str, dict[str, Any]] = {}
    for mode_key, mode in (("sell_put", "put"), ("covered_call", "call")):
        rows = candidates.get(mode_key)
        if not isinstance(rows, list):
            raise ValueError(f"candidate family {mode_key} must be an array")
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError(f"candidate family {mode_key} row is invalid")
            candidate_id = str(row.get("candidate_id") or "").strip()
            if not candidate_id or candidate_id in out:
                raise ValueError("candidate projection identity is missing or duplicated")
            out[candidate_id] = project_one_contract(
                candidate=row,
                strategy_mode=mode,
                portfolio=portfolio,
                option_positions=option_positions,
                position_rows=materialized_positions,
                portfolio_total_cny=portfolio_total_cny,
                shares_by_symbol=shares_by_symbol,
                cny_per_currency=cny_per_currency,
            )
    return out


def _option_overlays(
    rows: Iterable[Mapping[str, Any]],
    *,
    symbol: str,
    mode: str,
    expiry: date,
) -> dict[str, int]:
    same_obligation = 0
    long_call = 0
    long_put = 0
    exact_expiry = 0
    near_expiry = 0
    matching_type = "put" if mode == "put" else "call"
    for row in rows:
        row_symbol = str(row.get("symbol") or "")
        option_type = str(row.get("option_type") or "")
        side = str(row.get("side") or "")
        contracts = _positive_integer(row.get("contracts"))
        row_expiry = _date(row.get("expiry"))
        if contracts is None or row_expiry is None:
            raise ValueError("aggregated option position is invalid")
        if row_symbol == symbol:
            if option_type == matching_type and side == "short":
                same_obligation += contracts
            if option_type == "call" and side == "long":
                long_call += contracts
            if option_type == "put" and side == "long":
                long_put += contracts
        distance = abs((row_expiry - expiry).days)
        if distance == 0:
            exact_expiry += contracts
        elif distance <= 7:
            near_expiry += contracts
    return {
        "same_obligation_current_contracts": same_obligation,
        "same_obligation_after_add_contracts": same_obligation + 1,
        "long_call_current_contracts": long_call,
        "long_put_current_contracts": long_put,
        "exact_expiry_current_contracts": exact_expiry,
        "exact_expiry_after_add_contracts": exact_expiry + 1,
        "near_expiry_7d_current_contracts": near_expiry,
        "near_expiry_7d_after_add_contracts": near_expiry,
    }


def _unavailable_overlays() -> dict[str, None]:
    return {
        "same_obligation_current_contracts": None,
        "same_obligation_after_add_contracts": None,
        "long_call_current_contracts": None,
        "long_put_current_contracts": None,
        "exact_expiry_current_contracts": None,
        "exact_expiry_after_add_contracts": None,
        "near_expiry_7d_current_contracts": None,
        "near_expiry_7d_after_add_contracts": None,
    }


def _gap_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "").strip()]


def _date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _positive_integer(value: Any) -> int | None:
    parsed = _positive_float(value)
    if parsed is None or not parsed.is_integer():
        return None
    return int(parsed)


def _positive_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _finite_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return round(parsed, 8) if math.isfinite(parsed) else None


def _rounded_ratio(numerator: float, denominator: float) -> float:
    return round(numerator / denominator, 8)


__all__ = ["project_all_candidates", "project_one_contract"]
