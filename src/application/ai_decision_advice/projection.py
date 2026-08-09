from __future__ import annotations

from typing import Any, Iterable, Mapping

from domain.domain.symbol_identity import canonical_symbol, symbol_currency


def project_one_contract(
    *,
    candidate: Mapping[str, Any],
    strategy_mode: str,
    portfolio: Mapping[str, Any],
    option_positions: Mapping[str, Any],
) -> dict[str, Any]:
    """Deterministic marginal projection of adding one contract (docs 8).

    The model never computes these values; it only cites them. v1 covers
    symbol / currency / expiry concentration and same-direction option
    overlays. No industry dimension (no reliable source in v1).

    Only values that are deterministic at this layer are emitted. Absolute
    after-trade weights cannot be derived from relative weight inputs, so the
    projection reports current concentration plus the marginal contract facts
    (direction, shares, assignment notional) instead of fabricating a number.
    """

    symbol = canonical_symbol(candidate.get("symbol")) or str(candidate.get("symbol") or "")
    mode = str(strategy_mode or "").strip().lower()
    rows = [dict(row) for row in (portfolio.get("symbol_weights") or []) if isinstance(row, Mapping)]
    current_weight = 0.0
    for row in rows:
        if str(row.get("symbol") or "") == symbol:
            current_weight = float(row.get("weight") or 0)
            break

    strike = _float(candidate.get("strike"))
    multiplier = _float(candidate.get("multiplier")) or 1.0
    contract_shares = int(multiplier)

    if mode == "put":
        direction = "assignment_adds_shares"
    elif mode == "call":
        direction = "call_away_removes_shares"
    else:
        direction = "unknown"

    currency = str(candidate.get("currency") or symbol_currency(symbol) or "")
    currency_weight = sum(
        float(row.get("weight") or 0)
        for row in rows
        if str(row.get("currency") or "") == currency
    )

    expiry = candidate.get("expiry")
    overlays = _option_overlays(
        option_positions.get("open_positions") or [],
        symbol=symbol,
        mode=mode,
        expiry=expiry,
    )
    return {
        "symbol": symbol,
        "strategy_mode": mode,
        "direction": direction,
        "symbol_concentration": {
            "current_weight": current_weight,
            "already_holds_symbol": current_weight > 0,
        },
        "currency_concentration": {
            "currency": currency,
            "current_weight": round(currency_weight, 6),
        },
        "expiry": expiry,
        "expiry_overlap_count": overlays["same_expiry_count"],
        "same_direction_overlay_count": overlays["same_direction_count"],
        "contract_shares": contract_shares,
        "assignment_notional": (strike * multiplier) if (strike is not None and mode == "put") else None,
        "call_away_shares": contract_shares if mode == "call" else None,
    }


def project_all_candidates(
    *,
    candidates: Mapping[str, Any],
    portfolio: Mapping[str, Any],
    option_positions: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Project every accepted candidate, keyed by candidate_id."""

    out: dict[str, dict[str, Any]] = {}
    for mode_key, mode in (("sell_put", "put"), ("covered_call", "call")):
        for row in candidates.get(mode_key) or []:
            if not isinstance(row, Mapping):
                continue
            candidate_id = str(row.get("candidate_id") or "")
            if not candidate_id:
                continue
            out[candidate_id] = project_one_contract(
                candidate=row,
                strategy_mode=mode,
                portfolio=portfolio,
                option_positions=option_positions,
            )
    return out


def _option_overlays(
    open_positions: Iterable[Mapping[str, Any]],
    *,
    symbol: str,
    mode: str,
    expiry: Any,
) -> dict[str, int]:
    same_expiry = 0
    same_direction = 0
    # Sell Put adds downside exposure: existing short puts on the same
    # underlying stack in the same direction. Covered Call adds upside
    # call-away: existing short calls stack.
    matching_type = "put" if mode == "put" else "call"
    for row in open_positions:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("symbol") or "") != symbol:
            continue
        if expiry and str(row.get("expiry") or "") == str(expiry):
            same_expiry += 1
        if (
            str(row.get("option_type") or "") == matching_type
            and str(row.get("side") or "") == "short"
        ):
            same_direction += 1
    return {"same_expiry_count": same_expiry, "same_direction_count": same_direction}


def _float(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
