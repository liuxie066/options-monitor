from __future__ import annotations

from dataclasses import dataclass
from typing import Any


INSURANCE_UNDERWRITING_PROFILE = "insurance_underwriting"


@dataclass(frozen=True)
class InsuranceUnderwritingConfig:
    strategy: str = INSURANCE_UNDERWRITING_PROFILE
    min_annualized_return: float = 0.10
    min_net_income: float = 50.0
    min_iv_rv_ratio: float = 1.10
    min_iv_minus_rv: float = 0.05
    reject_event_risk: bool = True
    event_source_fail_closed: bool = True
    premium_score_cap: float = 1.5
    min_strike: float | None = None
    max_strike: float | None = None

    @property
    def enabled(self) -> bool:
        return normalize_underwriting_strategy(self.strategy) == INSURANCE_UNDERWRITING_PROFILE


def normalize_underwriting_strategy(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text == INSURANCE_UNDERWRITING_PROFILE:
        return INSURANCE_UNDERWRITING_PROFILE
    return text


def evaluate_underwriting_candidate(
    row: dict[str, Any],
    *,
    mode: str,
    cfg: InsuranceUnderwritingConfig,
) -> dict[str, Any]:
    mode_norm = _mode(mode)
    fields = underwriting_fields(row, mode=mode_norm, cfg=cfg)

    annualized_return = _annualized_return(row, mode=mode_norm)
    if cfg.min_annualized_return > 0:
        if annualized_return is None:
            return _reject("annualized_return_missing", None, cfg.min_annualized_return, fields)
        if annualized_return < cfg.min_annualized_return:
            return _reject("annualized_return_below_min", annualized_return, cfg.min_annualized_return, fields)

    net_income = _net_income_for_threshold(row)
    if cfg.min_net_income > 0:
        if net_income is None:
            return _reject("net_income_missing", None, cfg.min_net_income, fields)
        if net_income < cfg.min_net_income:
            return _reject("net_income_below_min", net_income, cfg.min_net_income, fields)

    iv_rv_ratio = _float(fields.get("iv_rv_ratio"))
    iv_minus_rv = _float(fields.get("iv_minus_rv"))
    if cfg.min_iv_rv_ratio > 0:
        if iv_rv_ratio is None:
            return _reject("vol_edge_not_evaluable", None, cfg.min_iv_rv_ratio, fields)
        if iv_rv_ratio < cfg.min_iv_rv_ratio:
            return _reject("vol_edge_ratio_below_min", iv_rv_ratio, cfg.min_iv_rv_ratio, fields)
    if cfg.min_iv_minus_rv > 0:
        if iv_minus_rv is None:
            return _reject("vol_edge_not_evaluable", None, cfg.min_iv_minus_rv, fields)
        if iv_minus_rv < cfg.min_iv_minus_rv:
            return _reject("vol_edge_spread_below_min", iv_minus_rv, cfg.min_iv_minus_rv, fields)

    event_decision = evaluate_event_risk_candidate(
        row,
        reject_event_risk=cfg.reject_event_risk,
        event_source_fail_closed=cfg.event_source_fail_closed,
        fields=fields,
        required_event_type=("earnings" if mode_norm == "put" else None),
    )
    if not event_decision["accepted"]:
        return event_decision

    return {"accepted": True, "rule": "insurance_underwriting_candidate_accepted", "fields": fields}


def evaluate_event_risk_candidate(
    row: dict[str, Any],
    *,
    reject_event_risk: bool = True,
    event_source_fail_closed: bool = True,
    fields: dict[str, Any] | None = None,
    required_event_type: str | None = None,
) -> dict[str, Any]:
    decision_fields = dict(fields or {})
    event_status = str(row.get("event_source_status") or "").strip().lower()
    if event_source_fail_closed and event_status not in {"ok", "ok_with_fallback"}:
        return _reject(
            "event_source_unavailable",
            event_status or None,
            "ok",
            decision_fields,
            message="event source unavailable for underwriting",
        )
    event_type = str(required_event_type or "").strip().lower()
    if event_source_fail_closed and event_type:
        coverage_status = str(
            row.get(f"event_{event_type}_coverage_status") or ""
        ).strip().lower()
        if coverage_status != "complete":
            return _reject(
                f"event_{event_type}_coverage_incomplete",
                coverage_status or None,
                "complete",
                decision_fields,
                message=f"{event_type} event coverage is incomplete",
            )
    event_types = {
        item.strip().lower()
        for item in str(row.get("event_types") or "").split(",")
        if item.strip()
    }
    matching_event = not event_type or event_type in event_types
    if reject_event_risk and _truthy(row.get("event_flag")) and matching_event:
        return _reject(
            "event_risk_within_expiry",
            True,
            False,
            decision_fields,
            message="event risk before expiration",
        )
    return {"accepted": True, "rule": "event_risk_candidate_accepted", "fields": decision_fields}


def underwriting_fields(
    row: dict[str, Any],
    *,
    mode: str,
    cfg: InsuranceUnderwritingConfig,
) -> dict[str, Any]:
    mode_norm = _mode(mode)
    iv_rv_ratio, iv_minus_rv = _vol_edge(row)
    out: dict[str, Any] = {
        "strategy_profile": INSURANCE_UNDERWRITING_PROFILE,
        "insurance_underwriting_mode": mode_norm,
        "short_gamma_profile": "short_gamma",
        "short_vega_profile": "short_vega",
        "iv_rv_ratio": _round_or_none(iv_rv_ratio),
        "iv_minus_rv": _round_or_none(iv_minus_rv),
        "premium_edge_score": premium_edge_score(row, mode=mode_norm, cfg=cfg, iv_rv_ratio=iv_rv_ratio, iv_minus_rv=iv_minus_rv),
    }
    if mode_norm == "put":
        out["strike_safety_margin_pct"] = _strike_safety_margin(row, cfg=cfg)
        out["net_assignment_discount_pct"] = _net_assignment_discount(row)
    else:
        out["strike_upside_margin_pct"] = _strike_upside_margin(row, cfg=cfg)
    return out


def rank_underwriting_candidates(
    rows: list[dict[str, Any]],
    *,
    mode: str,
    cfg: InsuranceUnderwritingConfig | None = None,
) -> list[dict[str, Any]]:
    mode_norm = _mode(mode)
    enriched: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row)
        if cfg is not None:
            payload.update(underwriting_fields(payload, mode=mode_norm, cfg=cfg))
        enriched.append(payload)

    from domain.domain.engine.candidate_engine import rank_candidate_rows

    return rank_candidate_rows(enriched, mode=mode_norm)


def underwriting_rank_key(row: dict[str, Any], *, mode: str) -> tuple[Any, ...]:
    """Canonical typed underwriting sort tuple used by ranking provenance."""

    mode_norm = _mode(mode)
    from domain.domain.engine.candidate_engine import build_candidate_rank_key

    return tuple(
        build_candidate_rank_key(row, mode=mode_norm)["sort_tuple"]
    )


def premium_edge_score(
    row: dict[str, Any],
    *,
    mode: str,
    cfg: InsuranceUnderwritingConfig,
    iv_rv_ratio: float | None = None,
    iv_minus_rv: float | None = None,
) -> float:
    mode_norm = _mode(mode)
    annualized_return = _annualized_return(row, mode=mode_norm)
    if iv_rv_ratio is None or iv_minus_rv is None:
        iv_rv_ratio, iv_minus_rv = _vol_edge(row)

    return_edge = _threshold_score(annualized_return, cfg.min_annualized_return, cap=cfg.premium_score_cap)
    iv_rv_edge = _threshold_score(iv_rv_ratio, cfg.min_iv_rv_ratio, cap=cfg.premium_score_cap)
    iv_minus_rv_edge = _threshold_score(iv_minus_rv, cfg.min_iv_minus_rv, cap=cfg.premium_score_cap)
    vol_pieces = [value for value in (iv_rv_edge, iv_minus_rv_edge) if value is not None]
    vol_edge = min(vol_pieces) if vol_pieces else None
    usable = [value for value in (return_edge, vol_edge) if value is not None]
    return round(sum(usable) / len(usable), 6) if usable else 0.0


def _reject(
    rule: str,
    metric_value: Any,
    threshold: Any,
    fields: dict[str, Any],
    *,
    message: str | None = None,
) -> dict[str, Any]:
    return {
        "accepted": False,
        "rule": rule,
        "metric_value": metric_value,
        "threshold": threshold,
        "fields": fields,
        "message": message or rule,
    }


def _annualized_return(row: dict[str, Any], *, mode: str) -> float | None:
    if mode == "call":
        return _first_float(row, "annualized_net_premium_return", "annualized_return")
    return _first_float(row, "annualized_net_return_on_cash_basis", "annualized_return")


def _net_income_for_threshold(row: dict[str, Any]) -> float | None:
    return _first_float(row, "net_income_cny")


def _vol_edge(row: dict[str, Any]) -> tuple[float | None, float | None]:
    ratio = _float(row.get("iv_rv_ratio"))
    spread = _float(row.get("iv_minus_rv"))
    iv = _first_float(row, "implied_volatility", "iv")
    rv = _first_float(row, "realized_volatility_estimate", "realized_volatility", "rv")
    if ratio is None and iv is not None and rv is not None and rv > 0:
        ratio = iv / rv
    if spread is None and iv is not None and rv is not None:
        spread = iv - rv
    return ratio, spread


def _strike_safety_margin(row: dict[str, Any], *, cfg: InsuranceUnderwritingConfig) -> float | None:
    max_strike = _first_float(row, "max_strike")
    if max_strike is None:
        max_strike = cfg.max_strike
    strike = _float(row.get("strike"))
    if max_strike is None or strike is None or max_strike <= 0:
        return None
    return round((max_strike - strike) / max_strike, 6)


def _net_assignment_discount(row: dict[str, Any]) -> float | None:
    spot = _first_float(row, "spot")
    breakeven = _first_float(row, "breakeven")
    if spot is None or spot <= 0 or breakeven is None:
        return None
    return round((spot - breakeven) / spot, 6)


def _strike_upside_margin(row: dict[str, Any], *, cfg: InsuranceUnderwritingConfig) -> float | None:
    min_strike = _first_float(row, "effective_min_strike", "min_strike")
    if min_strike is None:
        min_strike = cfg.min_strike
    strike = _float(row.get("strike"))
    if min_strike is None or strike is None or min_strike <= 0:
        return None
    return round((strike - min_strike) / min_strike, 6)


def _threshold_score(value: float | None, threshold: float, *, cap: float) -> float | None:
    if threshold <= 0:
        return None
    if value is None:
        return 0.0
    return min(max(float(value) / float(threshold), 0.0), cap)


def _first_float(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _float(row.get(key))
        if value is not None:
            return value
    return None


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        import math

        parsed = float(value)
        if not math.isfinite(parsed):
            return None
        return parsed
    except Exception:
        return None


def _round_or_none(value: float | None) -> float | None:
    return round(value, 6) if value is not None else None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    if text in {"", "0", "false", "no", "n", "none", "nan"}:
        return False
    return True


def _mode(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text == "call":
        return "call"
    return "put"
