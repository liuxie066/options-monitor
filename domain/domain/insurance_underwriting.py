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
    min_strike: float | None = None
    max_strike: float | None = None
    min_dte: int | None = None
    max_dte: int | None = None
    max_spread_ratio: float = 0.40

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
    from domain.domain.engine.candidate_engine import evaluate_opening_candidate_policy

    decision = evaluate_opening_candidate_policy(
        {**row, **fields},
        mode=mode_norm,
        min_dte=cfg.min_dte,
        max_dte=cfg.max_dte,
        min_strike=_first_float(row, "effective_min_strike", "policy_min_strike")
        if mode_norm == "call"
        else cfg.min_strike,
        max_strike=cfg.max_strike,
        min_annualized_return=cfg.min_annualized_return,
        min_net_premium_cny=cfg.min_net_income,
        min_iv_rv_ratio=cfg.min_iv_rv_ratio,
        min_iv_minus_rv=cfg.min_iv_minus_rv,
        max_spread_ratio=cfg.max_spread_ratio,
        require_earnings_evidence=True,
        reject_known_earnings=True,
    )
    if bool(decision.get("accepted")):
        return {
            "accepted": True,
            "rule": "candidate_engine_policy_accepted",
            "fields": fields,
        }
    reject = dict((decision.get("rejects") or [{}])[0])
    return _reject(
        str(reject.get("reason") or "candidate_engine_policy_rejected"),
        reject.get("metric_value"),
        reject.get("threshold"),
        fields,
        message=str(reject.get("message") or "Candidate Engine policy rejected candidate"),
    )


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
        "policy_min_dte": cfg.min_dte,
        "policy_max_dte": cfg.max_dte,
        "policy_min_strike": cfg.min_strike,
        "policy_max_strike": cfg.max_strike,
        "policy_max_spread_ratio": cfg.max_spread_ratio,
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
    rv = _first_float(row, "term_matched_rv")
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


def _mode(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text == "call":
        return "call"
    return "put"
