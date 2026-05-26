from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

from domain.domain.symbol_identity import canonical_symbol


ShortVolMode = Literal["put", "call"]


@dataclass(frozen=True)
class ShortVolAssessmentConfig:
    min_iv_rv_ratio: float = 1.15
    min_iv_minus_rv: float = 0.05
    min_abs_delta: float = 0.15
    max_abs_delta: float = 0.30
    target_abs_delta: float = 0.20
    reject_event_risk: bool = True
    event_source_fail_closed: bool = True
    enable_stress_check: bool = True
    stress_down_sigma_multiple: float = 2.0
    max_put_sigma_stress_loss_nav_pct: float = 0.02
    gap_down_pct: float = 0.10
    max_put_gap_down_loss_nav_pct: float = 0.03
    call_gap_up_pct: float = 0.10
    max_call_gap_up_opportunity_cost_nav_pct: float = 0.02
    max_call_gap_up_opportunity_cost_to_premium: float = 3.0
    max_single_trade_nav_pct: float = 0.08
    max_symbol_nav_pct: float = 0.20
    max_total_short_put_nav_pct: float = 0.50


@dataclass(frozen=True)
class ShortVolPortfolioContext:
    nav_cny: float | None
    stock_value_cny_by_symbol: dict[str, float]
    short_put_assignment_cny_by_symbol: dict[str, float]
    short_put_assignment_total_cny: float | None
    unavailable_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def assess_short_vol_candidate(
    row: dict[str, Any],
    *,
    mode: ShortVolMode,
    cfg: ShortVolAssessmentConfig,
    risk_ctx: ShortVolPortfolioContext,
) -> dict[str, Any]:
    fields = short_vol_assessment_fields(row, mode=mode, cfg=cfg, risk_ctx=risk_ctx)

    if fields["realized_volatility_estimate"] is None:
        return _reject_decision("volatility_estimate_missing", fields, None, "rv_est", "realized volatility estimate missing")
    if fields["implied_volatility"] is None:
        return _reject_decision("implied_volatility_missing", fields, None, "iv", "implied volatility missing")
    if fields["iv_rv_ratio"] is None or fields["iv_rv_ratio"] < cfg.min_iv_rv_ratio:
        return _reject_decision(
            "vol_edge_ratio_below_min",
            fields,
            fields["iv_rv_ratio"],
            cfg.min_iv_rv_ratio,
            "IV/RV ratio below short-vol threshold",
        )
    if fields["iv_minus_rv"] is None or fields["iv_minus_rv"] < cfg.min_iv_minus_rv:
        return _reject_decision(
            "vol_edge_spread_below_min",
            fields,
            fields["iv_minus_rv"],
            cfg.min_iv_minus_rv,
            "IV-RV spread below short-vol threshold",
        )

    abs_delta = fields["abs_delta"]
    if abs_delta is None:
        return _reject_decision("delta_missing", fields, None, [cfg.min_abs_delta, cfg.max_abs_delta], "delta missing")
    if abs_delta < cfg.min_abs_delta:
        return _reject_decision("delta_below_target_band", fields, abs_delta, cfg.min_abs_delta, "abs(delta) below band")
    if abs_delta > cfg.max_abs_delta:
        return _reject_decision("delta_above_target_band", fields, abs_delta, cfg.max_abs_delta, "abs(delta) above band")

    if cfg.event_source_fail_closed and fields["event_source_status"] != "ok":
        return _reject_decision(
            "event_source_unavailable",
            fields,
            fields["event_source_status"],
            "ok",
            "event risk source unavailable for short-vol assessment",
        )
    if cfg.reject_event_risk and fields["event_risk_flag"] is True:
        return _reject_decision(
            "event_risk_within_expiry",
            fields,
            fields["event_risk_dates"],
            "no event before expiry",
            "event risk exists before option expiry",
        )

    if fields["concentration_evaluable"] is not True:
        return _reject_decision(
            "concentration_not_evaluable",
            fields,
            None,
            "holdings_nav",
            ";".join(risk_ctx.unavailable_reasons) or "portfolio concentration unavailable",
        )
    if fields["single_trade_concentration"] is None or fields["single_trade_concentration"] > cfg.max_single_trade_nav_pct:
        return _reject_decision(
            "single_trade_concentration_exceeded",
            fields,
            fields["single_trade_concentration"],
            cfg.max_single_trade_nav_pct,
            "single candidate short-vol notional exceeds NAV cap",
        )
    if fields["symbol_concentration_after"] is None or fields["symbol_concentration_after"] > cfg.max_symbol_nav_pct:
        return _reject_decision(
            "symbol_concentration_exceeded",
            fields,
            fields["symbol_concentration_after"],
            cfg.max_symbol_nav_pct,
            "symbol concentration after candidate exceeds NAV cap",
        )
    if mode == "put":
        if (
            fields["total_short_put_concentration_after"] is None
            or fields["total_short_put_concentration_after"] > cfg.max_total_short_put_nav_pct
        ):
            return _reject_decision(
                "total_short_put_concentration_exceeded",
                fields,
                fields["total_short_put_concentration_after"],
                cfg.max_total_short_put_nav_pct,
                "total short-put assignment obligation exceeds NAV cap",
            )

    if cfg.enable_stress_check and mode == "put":
        if fields["path_stress_evaluable"] is not True:
            return _reject_decision(
                "path_stress_inputs_missing",
                fields,
                fields["path_stress_unavailable_reason"],
                "spot,strike,dte,rv,net_income_cny,option_contract_point_value_cny,nav",
                "path stress inputs missing for short-vol assessment",
            )
        if (
            fields["put_stress_down_loss_nav_pct"] is None
            or fields["put_stress_down_loss_nav_pct"] > cfg.max_put_sigma_stress_loss_nav_pct
        ):
            return _reject_decision(
                "put_sigma_stress_loss_exceeded",
                fields,
                fields["put_stress_down_loss_nav_pct"],
                cfg.max_put_sigma_stress_loss_nav_pct,
                "sell-put sigma stress loss exceeds NAV cap",
            )
        if (
            fields["put_gap_down_loss_nav_pct"] is None
            or fields["put_gap_down_loss_nav_pct"] > cfg.max_put_gap_down_loss_nav_pct
        ):
            return _reject_decision(
                "put_gap_down_stress_loss_exceeded",
                fields,
                fields["put_gap_down_loss_nav_pct"],
                cfg.max_put_gap_down_loss_nav_pct,
                "sell-put gap-down stress loss exceeds NAV cap",
            )

    if cfg.enable_stress_check and mode == "call":
        if fields["path_stress_evaluable"] is not True:
            return _reject_decision(
                "path_stress_inputs_missing",
                fields,
                fields["path_stress_unavailable_reason"],
                "spot,strike,dte,rv,net_income_cny,option_contract_point_value_cny,nav",
                "path stress inputs missing for short-vol assessment",
            )
        if (
            fields["call_gap_up_opportunity_cost_nav_pct"] is None
            or fields["call_gap_up_opportunity_cost_nav_pct"] > cfg.max_call_gap_up_opportunity_cost_nav_pct
        ):
            return _reject_decision(
                "call_gap_up_opportunity_cost_nav_exceeded",
                fields,
                fields["call_gap_up_opportunity_cost_nav_pct"],
                cfg.max_call_gap_up_opportunity_cost_nav_pct,
                "covered-call gap-up opportunity cost exceeds NAV cap",
            )
        if (
            fields["call_gap_up_opportunity_cost_to_premium"] is None
            or fields["call_gap_up_opportunity_cost_to_premium"] > cfg.max_call_gap_up_opportunity_cost_to_premium
        ):
            return _reject_decision(
                "call_gap_up_opportunity_cost_premium_exceeded",
                fields,
                fields["call_gap_up_opportunity_cost_to_premium"],
                cfg.max_call_gap_up_opportunity_cost_to_premium,
                "covered-call gap-up opportunity cost is too large versus premium",
            )

    return {"accepted": True, "rule": "short_vol_candidate_accepted", "fields": fields}


def short_vol_assessment_fields(
    row: dict[str, Any],
    *,
    mode: ShortVolMode,
    cfg: ShortVolAssessmentConfig,
    risk_ctx: ShortVolPortfolioContext,
) -> dict[str, Any]:
    symbol = canonical_symbol(row.get("symbol"))
    iv = _float(row.get("implied_volatility"))
    rv = _first_float(
        row,
        "realized_volatility_estimate",
        "rv_estimate",
        "rv_est",
        "rv_60",
        "realized_volatility_60",
    )
    delta = _float(row.get("delta"))
    abs_delta = abs(delta) if delta is not None else None
    iv_rv_ratio = (iv / rv) if (iv is not None and rv is not None and rv > 0) else None
    iv_minus_rv = (iv - rv) if (iv is not None and rv is not None) else None

    assignment = _first_float(row, "assignment_notional_cny", "cash_required_cny")
    covered_notional = _first_float(row, "covered_notional_cny", "underlying_notional_cny")
    candidate_notional = assignment if mode == "put" else covered_notional
    nav = risk_ctx.nav_cny
    existing_stock = risk_ctx.stock_value_cny_by_symbol.get(symbol or "", 0.0)
    existing_short_put = risk_ctx.short_put_assignment_cny_by_symbol.get(symbol or "", 0.0)
    existing_total_short_put = risk_ctx.short_put_assignment_total_cny

    concentration_evaluable = bool(
        nav is not None
        and nav > 0
        and candidate_notional is not None
        and candidate_notional > 0
        and not risk_ctx.unavailable_reasons
    )
    if mode == "put":
        concentration_evaluable = bool(concentration_evaluable and existing_total_short_put is not None)

    single_trade = (candidate_notional / nav) if concentration_evaluable and nav else None
    if mode == "put":
        symbol_after = (
            ((existing_stock + existing_short_put + (assignment or 0.0)) / nav)
            if concentration_evaluable and nav
            else None
        )
        total_after = (
            (((existing_total_short_put or 0.0) + (assignment or 0.0)) / nav)
            if concentration_evaluable and nav
            else None
        )
    else:
        symbol_exposure = max(existing_stock, covered_notional or 0.0)
        symbol_after = (symbol_exposure / nav) if concentration_evaluable and nav else None
        total_after = ((existing_total_short_put or 0.0) / nav) if concentration_evaluable and nav else None

    delta_quality = None
    if abs_delta is not None:
        tolerance = max(cfg.max_abs_delta - cfg.min_abs_delta, 0.000001)
        delta_quality = max(0.0, 1.0 - (abs(abs_delta - cfg.target_abs_delta) / tolerance))

    vol_edge_score = None
    if iv_rv_ratio is not None and iv_minus_rv is not None:
        ratio_score = min(2.0, max(0.0, iv_rv_ratio - 1.0))
        spread_score = min(2.0, max(0.0, iv_minus_rv))
        vol_edge_score = ratio_score + spread_score

    concentration_score = None
    if symbol_after is not None and total_after is not None:
        concentration_score = max(0.0, 1.0 - max(symbol_after, total_after))

    equity_delta_equivalent = None
    if delta is not None:
        if mode == "put":
            equity_delta_equivalent = abs(delta)
        else:
            equity_delta_equivalent = max(0.0, 1.0 - abs(delta))

    event_fields = _event_risk_fields(row)
    stress_fields = _path_stress_fields(
        row,
        cfg=cfg,
        mode=mode,
        nav_cny=nav,
        rv=rv,
    )

    return {
        "strategy_profile": "short_vol",
        "short_vol_mode": mode,
        "short_gamma_profile": "short_gamma",
        "short_vega_profile": "short_vega",
        "implied_volatility": _round_optional(iv),
        "realized_volatility_estimate": _round_optional(rv),
        "iv_rv_ratio": _round_optional(iv_rv_ratio),
        "iv_minus_rv": _round_optional(iv_minus_rv),
        "abs_delta": _round_optional(abs_delta),
        "equity_delta_equivalent": _round_optional(equity_delta_equivalent),
        "delta_target_score": _round_optional(delta_quality),
        "vol_edge_score": _round_optional(vol_edge_score),
        "portfolio_nav_cny": _round_optional(nav),
        "assignment_notional_cny": _round_optional(assignment),
        "covered_notional_cny": _round_optional(covered_notional),
        "existing_stock_value_cny_symbol": _round_optional(existing_stock),
        "existing_short_put_assignment_cny_symbol": _round_optional(existing_short_put),
        "existing_short_put_assignment_cny_total": _round_optional(existing_total_short_put),
        "single_trade_concentration": _round_optional(single_trade),
        "symbol_concentration_after": _round_optional(symbol_after),
        "total_short_put_concentration_after": _round_optional(total_after),
        "concentration_score": _round_optional(concentration_score),
        "concentration_evaluable": concentration_evaluable,
        "concentration_unavailable_reason": ";".join(risk_ctx.unavailable_reasons) or None,
        "portfolio_risk_warnings": ";".join(risk_ctx.warnings) or None,
        **event_fields,
        **stress_fields,
    }


def _reject_decision(rule: str, fields: dict[str, Any], metric_value: Any, threshold: Any, message: str) -> dict[str, Any]:
    return {
        "accepted": False,
        "rule": rule,
        "metric_value": metric_value,
        "threshold": threshold,
        "message": message,
        "fields": fields,
    }


def _first_float(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _float(row.get(key))
        if value is not None:
            return value
    return None


def _float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        parsed = float(value)
    except Exception:
        return None
    try:
        if parsed != parsed:
            return None
    except Exception:
        pass
    return parsed


def _event_risk_fields(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_risk_flag": _bool(row.get("event_flag")),
        "event_risk_types": _text(row.get("event_types")),
        "event_risk_dates": _text(row.get("event_dates")),
        "event_source_status": _text(row.get("event_source_status")).lower(),
        "event_source_error": _text(row.get("event_source_error")) or None,
    }


def _path_stress_fields(
    row: dict[str, Any],
    *,
    cfg: ShortVolAssessmentConfig,
    mode: ShortVolMode,
    nav_cny: float | None,
    rv: float | None,
) -> dict[str, Any]:
    spot = _first_float(row, "spot", "underlying_price", "stock_price")
    strike = _float(row.get("strike"))
    dte = _first_float(row, "dte", "days_to_expiry")
    premium_cny = _first_float(row, "net_income_cny", "premium_cny", "net_credit_cny")
    point_value_cny = _first_float(row, "option_contract_point_value_cny", "contract_point_value_cny")

    missing: list[str] = []
    for name, value in (
        ("spot", spot),
        ("strike", strike),
        ("dte", dte),
        ("rv", rv),
        ("net_income_cny", premium_cny),
        ("option_contract_point_value_cny", point_value_cny),
        ("nav", nav_cny),
    ):
        if value is None or value <= 0:
            missing.append(name)

    sigma_move_pct = None
    if rv is not None and dte is not None and rv >= 0 and dte >= 0:
        sigma_move_pct = rv * math.sqrt(dte / 365.0)

    stress_evaluable = not missing and sigma_move_pct is not None
    sigma_multiple = max(0.0, float(cfg.stress_down_sigma_multiple))
    gap_down_pct = max(0.0, float(cfg.gap_down_pct))
    call_gap_up_pct = max(0.0, float(cfg.call_gap_up_pct))

    put_sigma_price = None
    put_sigma_loss_cny = None
    put_sigma_loss_nav_pct = None
    put_gap_price = None
    put_gap_loss_cny = None
    put_gap_loss_nav_pct = None
    call_gap_price = None
    call_gap_opportunity_cost_cny = None
    call_gap_opportunity_cost_nav_pct = None
    call_gap_opportunity_cost_to_premium = None

    if stress_evaluable:
        assert spot is not None
        assert strike is not None
        assert premium_cny is not None
        assert point_value_cny is not None
        assert nav_cny is not None
        assert sigma_move_pct is not None
        put_sigma_price = max(0.0, spot * (1.0 - sigma_multiple * sigma_move_pct))
        put_sigma_loss_cny = _loss_after_premium_cny(
            intrinsic_native=max(0.0, strike - put_sigma_price),
            point_value_cny=point_value_cny,
            premium_cny=premium_cny,
        )
        put_sigma_loss_nav_pct = put_sigma_loss_cny / nav_cny

        put_gap_price = max(0.0, spot * (1.0 - gap_down_pct))
        put_gap_loss_cny = _loss_after_premium_cny(
            intrinsic_native=max(0.0, strike - put_gap_price),
            point_value_cny=point_value_cny,
            premium_cny=premium_cny,
        )
        put_gap_loss_nav_pct = put_gap_loss_cny / nav_cny

        call_gap_price = spot * (1.0 + call_gap_up_pct)
        call_gap_opportunity_cost_cny = _loss_after_premium_cny(
            intrinsic_native=max(0.0, call_gap_price - strike),
            point_value_cny=point_value_cny,
            premium_cny=premium_cny,
        )
        call_gap_opportunity_cost_nav_pct = call_gap_opportunity_cost_cny / nav_cny
        call_gap_opportunity_cost_to_premium = call_gap_opportunity_cost_cny / premium_cny

    return {
        "path_stress_evaluable": stress_evaluable,
        "path_stress_unavailable_reason": ",".join(missing) or None,
        "stress_premium_cny": _round_optional(premium_cny),
        "option_contract_point_value_cny": _round_optional(point_value_cny),
        "stress_sigma_move_pct": _round_optional(sigma_move_pct),
        "stress_down_sigma_multiple": _round_optional(sigma_multiple),
        "put_stress_down_price": _round_optional(put_sigma_price),
        "put_stress_down_loss_cny": _round_optional(put_sigma_loss_cny),
        "put_stress_down_loss_nav_pct": _round_optional(put_sigma_loss_nav_pct),
        "put_gap_down_pct": _round_optional(gap_down_pct),
        "put_gap_down_price": _round_optional(put_gap_price),
        "put_gap_down_loss_cny": _round_optional(put_gap_loss_cny),
        "put_gap_down_loss_nav_pct": _round_optional(put_gap_loss_nav_pct),
        "call_gap_up_pct": _round_optional(call_gap_up_pct),
        "call_gap_up_price": _round_optional(call_gap_price),
        "call_gap_up_opportunity_cost_cny": _round_optional(call_gap_opportunity_cost_cny),
        "call_gap_up_opportunity_cost_nav_pct": _round_optional(call_gap_opportunity_cost_nav_pct),
        "call_gap_up_opportunity_cost_to_premium": _round_optional(call_gap_opportunity_cost_to_premium),
    }


def _loss_after_premium_cny(*, intrinsic_native: float, point_value_cny: float, premium_cny: float) -> float:
    return max(0.0, intrinsic_native * point_value_cny - premium_cny)


def _text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if value != value:
            return ""
    except Exception:
        pass
    return str(value).strip()


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    text = _text(value).lower()
    return text in {"1", "true", "yes", "y", "on", "event_warn", "event_reject"}


def _round_optional(value: Any) -> float | None:
    parsed = _float(value)
    if parsed is None:
        return None
    return round(float(parsed), 6)
