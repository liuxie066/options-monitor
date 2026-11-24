"""Pure same-symbol ordering; I/O and cross-symbol selection stay with callers."""
from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

StrategyMode = Literal["put", "call"]
OPENING_CANDIDATE_NEAR_RETURN_THRESHOLD = 0.002

def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    try:
        return bool(value != value)
    except Exception:
        return False


def _coerce_float(value: Any) -> float | None:
    if _is_missing(value):
        return None
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except Exception:
        return None
    return parsed if math.isfinite(parsed) else None


def _first_float(src: dict[str, Any], *names: str) -> float | None:
    for name in names:
        value = _coerce_float(src.get(name))
        if value is not None:
            return value
    return None


def _candidate_tie_break_margin(src: dict[str, Any], *, mode: StrategyMode) -> float:
    if mode == "put":
        explicit = _first_float(src, "net_assignment_discount_pct")
        if explicit is not None:
            return float(explicit)
        spot = _first_float(src, "spot")
        breakeven = _first_float(src, "breakeven")
        if spot is not None and spot > 0 and breakeven is not None:
            return float((spot - breakeven) / spot)
        return 0.0
    return float(
        _first_float(
            src,
            "strike_upside_margin_pct",
            "strike_above_spot_pct",
            "otm_pct",
        )
        or 0.0
    )


def _sell_put_period_return(src: dict[str, Any]) -> float | None:
    explicit = _first_float(
        src,
        "period_net_return_on_cash_basis",
        "period_net_return",
    )
    if explicit is not None:
        return explicit
    net_income = _first_float(src, "net_income")
    cash_basis = _first_float(src, "cash_basis")
    if net_income is not None and cash_basis is not None and cash_basis > 0:
        return net_income / cash_basis
    annualized = _first_float(src, "annualized_net_return_on_cash_basis")
    dte = _first_float(src, "dte")
    if annualized is not None and dte is not None and dte > 0:
        return annualized * dte / 365.0
    return None


def _covered_call_period_return(src: dict[str, Any]) -> float | None:
    explicit = _first_float(
        src,
        "period_net_premium_return",
        "period_net_return",
    )
    if explicit is not None:
        return explicit
    net_premium = _first_float(src, "net_income")
    current_market_value = _first_float(src, "current_market_value")
    if net_premium is not None and current_market_value is not None and current_market_value > 0:
        return net_premium / current_market_value
    annualized = _first_float(src, "annualized_net_premium_return")
    dte = _first_float(src, "dte")
    if annualized is not None and dte is not None and dte > 0:
        return annualized * dte / 365.0
    # Compatibility for historical diagnostic rows. Formal Phase-2 candidate
    # rows always carry the period return directly.
    if annualized is not None:
        return annualized
    return None


def _candidate_period_return(src: dict[str, Any], *, mode: StrategyMode) -> float | None:
    return _sell_put_period_return(src) if mode == "put" else _covered_call_period_return(src)


def _known_low_sort(value: float | None) -> tuple[bool, float]:
    return (value is None, 0.0 if value is None else float(value))


def _known_high_sort(value: float | None) -> tuple[bool, float]:
    return (value is None, 0.0 if value is None else -float(value))


def _sell_put_within_symbol_tie_key(src: dict[str, Any]) -> tuple[Any, ...]:
    return (
        -_candidate_tie_break_margin(src, mode="put"),
        *_known_low_sort(_first_float(src, "spread_ratio")),
        *_known_high_sort(_first_float(src, "open_interest")),
        -float(_first_float(src, "net_income_cny", "net_income") or 0.0),
        str(src.get("contract_symbol") or src.get("option_symbol") or ""),
    )


def _covered_call_within_symbol_tie_key(src: dict[str, Any]) -> tuple[Any, ...]:
    # Not merged with _sell_put_within_symbol_tie_key: element 0 is a
    # mode-specific quantity (negative assignment discount vs negative strike),
    # not one quantity read through a differently named field.
    return (
        -float(_first_float(src, "strike") or 0.0),
        *_known_low_sort(_first_float(src, "spread_ratio")),
        *_known_high_sort(_first_float(src, "open_interest")),
        -float(_first_float(src, "net_income_cny", "net_income") or 0.0),
        str(src.get("contract_symbol") or src.get("option_symbol") or ""),
    )


def _rank_return_bands(
    rows: list[dict[str, Any]],
    *,
    period_return_fn: Any,
    tie_key: Any,
    near_return_threshold: float = OPENING_CANDIDATE_NEAR_RETURN_THRESHOLD,
) -> list[dict[str, Any]]:
    usable = []
    missing = []
    for index, row in enumerate(rows):
        value = period_return_fn(row)
        if value is None:
            missing.append((index, row))
        else:
            usable.append((value, index, row))
    usable.sort(key=lambda item: item[0], reverse=True)

    ranked: list[dict[str, Any]] = []
    start = 0
    while start < len(usable):
        # Anchor each band to its maximum, never to the previous adjacent return.
        floor = usable[start][0] - near_return_threshold
        end = start + 1
        while end < len(usable) and usable[end][0] >= floor:
            end += 1
        ranked.extend(
            row for _value, _index, row in sorted(
                usable[start:end], key=lambda item: (tie_key(item[2]), item[1]),
            )
        )
        start = end
    ranked.extend(
        row for _index, row in sorted(
            missing, key=lambda item: (tie_key(item[1]), item[0]),
        )
    )
    return ranked


def rank_same_symbol_candidates(
    rows: list[dict[str, Any]], *, mode: StrategyMode,
    near_return_threshold: float = OPENING_CANDIDATE_NEAR_RETURN_THRESHOLD,
) -> list[dict[str, Any]]:
    old_order = _rank_return_bands(
        rows,
        period_return_fn=_sell_put_period_return if mode == "put" else _covered_call_period_return,
        tie_key=_sell_put_within_symbol_tie_key if mode == "put" else _covered_call_within_symbol_tie_key,
        near_return_threshold=near_return_threshold,
    )
    evaluation = evaluate_net_delivery_group(rows, mode=mode)
    if not evaluation["applied"]:
        return old_order
    def priority(row):
        key = str(row.get("contract_symbol") or row.get("option_symbol") or "")
        return evaluation["candidates"][key]["priority"]
    return [row for row in old_order if priority(row)] + [row for row in old_order if not priority(row)]


def net_delivery_policy(rows: list[dict[str, Any]], *, mode: str) -> float | None:
    """Only the current ordinary template owns this rule; legacy/Wheel stay unchanged."""
    policies = []
    for row in rows:
        identity = row.get("strategy_core") or {}
        if (identity.get("template") != ("csp_baseline" if mode == "put" else "cc_baseline")
                or identity.get("template_version") != "0.2.1"):
            return None
        policies.append(identity["effective_parameters"]["min_net_delivery_step_pct"])
    if not policies:
        return None
    if any(value != policies[0] for value in policies):
        raise ValueError("mixed same-symbol net delivery policy")
    return policies[0]


def _net_price(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        price = Decimal(str(value))
        return price.quantize(Decimal("0.000001")) if price.is_finite() and price > 0 else None
    except (InvalidOperation, ValueError):
        return None


def evaluate_net_delivery_group(
    rows: list[dict[str, Any]], *, mode: str, complete: bool = True,
) -> dict[str, Any]:
    """Evaluate one frozen accepted group. A lone diagnostic row proves no universe."""
    step = net_delivery_policy(rows, mode=mode)
    context = rows[0].get("same_symbol_ranking_context") if rows else None
    if any(row.get("same_symbol_ranking_context") != context for row in rows):
        raise ValueError("mixed same-symbol position context")
    if rows and any(row.get("symbol") != rows[0].get("symbol") for row in rows):
        raise ValueError("mixed symbols in net delivery group")
    result = {
        "mode": mode, "min_step_pct": step, "baseline_net_price": None,
        "threshold_net_price": None, "currency": None, "applied": False,
        "reason": "not_applicable", "reason_detail": None, "candidates": {},
    }
    baseline = threshold = None
    if step is not None:
        result["reason"] = "positions_unavailable"
        if isinstance(context, dict):
            result["currency"] = context.get("currency")
            result["reason_detail"] = context.get("reason")
            for row in rows:
                scope = row.get("strategy_core", {}).get("scope", {})
                for key, expected in (("symbol", row.get("symbol")), ("mode", mode),
                                      ("account", scope.get("account")), ("market", scope.get("market")),
                                      ("currency", row.get("currency"))):
                    if expected is not None and context.get(key) != expected:
                        raise ValueError(f"net delivery context {key} conflicts with candidate")
            if context.get("status") == "not_applicable":
                result["reason"] = "not_applicable"
            elif context.get("status") == "available":
                prices = context.get("prices")
                if isinstance(prices, list):
                    values = [_net_price(item.get("net_price")) if isinstance(item, dict) else None for item in prices]
                    if not values:
                        result["reason"] = "no_positions"
                    elif any(value is None for value in values):
                        result["reason"] = "position_net_price_missing"
                    else:
                        baseline = min(values) if mode == "put" else max(values)
                        threshold = baseline * (Decimal(1) + (Decimal(-1) if mode == "put" else Decimal(1)) * Decimal(str(step)))
                        result.update(baseline_net_price=float(baseline), threshold_net_price=float(threshold))
    qualifications = {}
    prices_complete = True
    for row in rows:
        contract = str(row.get("contract_symbol") or row.get("option_symbol") or "")
        price = _net_price(row.get("breakeven" if mode == "put" else "net_sale_price"))
        qualifies = None
        if price is None:
            prices_complete = False
        elif threshold is not None:
            qualifies = price <= threshold if mode == "put" else price >= threshold
        qualifications[contract] = {
            "candidate_net_price": float(price) if price is not None else None,
            "qualifies": qualifies, "priority": False,
        }
    if threshold is not None:
        result["reason"] = ("candidate_net_price_missing" if not prices_complete else
                            "new_tier" if any(item["qualifies"] for item in qualifications.values()) else "no_new_tier")
        result["applied"] = result["reason"] == "new_tier"
    if not complete:
        result.update(applied=None, reason="group_not_provided")
    for item in qualifications.values():
        item["priority"] = bool(result["applied"] and item["qualifies"]) if complete else None
    result["candidates"] = qualifications
    return result


_NET_DELIVERY_REASONS = {
    "not_applicable": "未提供账户持仓上下文，沿用原排序",
    "positions_unavailable": "无法判断档位：持仓成本证据不足，沿用原排序",
    "position_net_price_missing": "无法判断档位：持仓净价不完整，沿用原排序",
    "no_positions": "无对应普通开放仓位，沿用原排序",
    "candidate_net_price_missing": "本组候选净价不完整，沿用原排序",
    "no_new_tier": "没有合格新档位，沿用原排序",
    "group_not_provided": "未提供完整候选组，无法判断是否采用优先规则",
}


def net_delivery_explanation(row: dict[str, Any], *, mode: str, evaluation=None) -> dict[str, Any] | None:
    if net_delivery_policy([row], mode=mode) is None:
        return None
    group = evaluate_net_delivery_group([row], mode=mode, complete=False) if evaluation is None else evaluation
    contract = str(row.get("contract_symbol") or row.get("option_symbol") or "")
    detail = {key: value for key, value in group.items() if key != "candidates"}
    detail.update(group["candidates"][contract])
    detail["price_basis"] = "current_limit_estimate_opening_fees_only"
    detail["message"] = (_NET_DELIVERY_REASONS.get(group["reason"]) or
                         ("新档位优先" if detail["priority"] else "未达到新档位，排在优先组之后"))
    return detail
