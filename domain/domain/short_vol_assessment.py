from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from domain.domain.performance.models import normalize_currency, to_decimal
from domain.domain.symbol_identity import canonical_symbol


ShortVolMode = Literal["put", "call"]
EVENT_SOURCE_OK_STATUSES = {"ok", "ok_with_fallback"}
OPTION_MARKET_CONCENTRATION_METRIC_VERSION = (
    "option_market_concentration_after.v1"
)


@dataclass(frozen=True)
class ShortVolAssessmentConfig:
    min_iv_rv_ratio: float = 1.10
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


def resolve_short_vol_assessment_config(raw: dict[str, Any] | None) -> ShortVolAssessmentConfig:
    """Resolve legacy short-vol settings for offline opening-strategy research.

    Current opening configs keep the IV/RV and event fields at the strategy
    top level. Historical replay inputs may still carry the broader thesis
    under ``short_vol`` and ``concentration``. This compatibility is not part
    of Close Advice, whose only live policy is ``strict_profit_capture.v1``.
    """

    cfg = raw if isinstance(raw, dict) else {}
    short_vol = cfg.get("short_vol") if isinstance(cfg.get("short_vol"), dict) else {}
    concentration = cfg.get("concentration") if isinstance(cfg.get("concentration"), dict) else {}

    return ShortVolAssessmentConfig(
        min_iv_rv_ratio=_float_setting_from_sources("min_iv_rv_ratio", 1.10, cfg, short_vol),
        min_iv_minus_rv=_float_setting_from_sources("min_iv_minus_rv", 0.05, cfg, short_vol),
        min_abs_delta=_float_setting(short_vol, "min_abs_delta", 0.15),
        max_abs_delta=_float_setting(short_vol, "max_abs_delta", 0.30),
        target_abs_delta=_float_setting(short_vol, "target_abs_delta", 0.20),
        reject_event_risk=_bool_setting_from_sources("reject_event_risk", True, cfg, short_vol),
        event_source_fail_closed=_bool_setting_from_sources("event_source_fail_closed", True, cfg, short_vol),
        enable_stress_check=_bool_setting(short_vol, "enable_stress_check", True),
        stress_down_sigma_multiple=_float_setting(short_vol, "stress_down_sigma_multiple", 2.0),
        max_put_sigma_stress_loss_nav_pct=_float_setting(short_vol, "max_put_sigma_stress_loss_nav_pct", 0.02),
        gap_down_pct=_float_setting(short_vol, "gap_down_pct", 0.10),
        max_put_gap_down_loss_nav_pct=_float_setting(short_vol, "max_put_gap_down_loss_nav_pct", 0.03),
        call_gap_up_pct=_float_setting(short_vol, "call_gap_up_pct", 0.10),
        max_call_gap_up_opportunity_cost_nav_pct=_float_setting(
            short_vol,
            "max_call_gap_up_opportunity_cost_nav_pct",
            0.02,
        ),
        max_call_gap_up_opportunity_cost_to_premium=_float_setting(
            short_vol,
            "max_call_gap_up_opportunity_cost_to_premium",
            3.0,
        ),
        max_single_trade_nav_pct=_float_setting(concentration, "max_single_trade_nav_pct", 0.08),
        max_symbol_nav_pct=_float_setting(concentration, "max_symbol_nav_pct", 0.20),
        max_total_short_put_nav_pct=_float_setting(concentration, "max_total_short_put_nav_pct", 0.50),
    )


def short_vol_assessment_fields(
    row: dict[str, Any],
    *,
    mode: ShortVolMode,
    cfg: ShortVolAssessmentConfig,
    risk_ctx: ShortVolPortfolioContext,
) -> dict[str, Any]:
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

    nav = risk_ctx.nav_cny
    concentration_fields = portfolio_concentration_fields(row, mode=mode, risk_ctx=risk_ctx)

    delta_quality = None
    if abs_delta is not None:
        tolerance = max(cfg.max_abs_delta - cfg.min_abs_delta, 0.000001)
        delta_quality = max(0.0, 1.0 - (abs(abs_delta - cfg.target_abs_delta) / tolerance))

    vol_edge_score = None
    if iv_rv_ratio is not None and iv_minus_rv is not None:
        ratio_score = min(2.0, max(0.0, iv_rv_ratio - 1.0))
        spread_score = min(2.0, max(0.0, iv_minus_rv))
        vol_edge_score = ratio_score + spread_score

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
        **concentration_fields,
        **event_fields,
        **stress_fields,
    }


def portfolio_concentration_fields(
    row: dict[str, Any],
    *,
    mode: ShortVolMode,
    risk_ctx: ShortVolPortfolioContext,
) -> dict[str, Any]:
    """Project candidate concentration facts without imposing an opening gate."""

    symbol = canonical_symbol(row.get("symbol"))
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

    concentration_score = None
    if symbol_after is not None and total_after is not None:
        concentration_score = max(0.0, 1.0 - max(symbol_after, total_after))

    return {
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
    }


def calculate_option_market_concentration_after(
    *,
    candidate: dict[str, Any],
    open_option_positions: list[dict[str, Any]],
    valuation_mark_facts: list[dict[str, Any]],
    fx_rate_facts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Calculate the frozen absolute option-market-value concentration."""

    marks = _unique_by(valuation_mark_facts, "instrument_key")
    rates = _unique_by(fx_rate_facts, "base_currency")

    def to_cny(amount: Decimal, currency: Any) -> tuple[Decimal, str | None]:
        normalized = normalize_currency(currency)
        if normalized == "CNY":
            return amount, None
        fact = rates.get(normalized)
        if fact is None or normalize_currency(fact.get("quote_currency")) != "CNY":
            raise ValueError(f"FX evidence is missing for {normalized}")
        return amount * to_decimal(fact.get("rate"), field_name="rate"), str(
            fact.get("fact_id") or ""
        )

    existing_total = Decimal(0)
    existing_symbol = Decimal(0)
    candidate_symbol = canonical_symbol(candidate.get("symbol"))
    if not candidate_symbol:
        raise ValueError("candidate symbol is required")
    mark_refs: set[str] = set()
    fx_refs: set[str] = set()
    position_refs: list[str] = []
    for position in open_option_positions:
        instrument_key = str(position.get("instrument_key") or "")
        mark = marks.get(instrument_key)
        if mark is None:
            raise ValueError(f"valuation mark is missing for {instrument_key}")
        native = abs(
            to_decimal(mark.get("price"), field_name="mark.price")
            * to_decimal(position.get("multiplier"), field_name="multiplier")
            * to_decimal(position.get("contracts_open"), field_name="contracts_open")
        )
        value_cny, fx_ref = to_cny(native, position.get("currency"))
        existing_total += value_cny
        if canonical_symbol(position.get("symbol")) == candidate_symbol:
            existing_symbol += value_cny
        mark_refs.add(str(mark.get("fact_id") or ""))
        position_refs.append(str(position.get("lot_id") or ""))
        if fx_ref:
            fx_refs.add(fx_ref)

    candidate_native = abs(
        to_decimal(candidate.get("sell_limit"), field_name="sell_limit")
        * to_decimal(candidate.get("multiplier"), field_name="multiplier")
    )
    candidate_cny, candidate_fx_ref = to_cny(
        candidate_native,
        candidate.get("currency"),
    )
    if candidate_fx_ref:
        fx_refs.add(candidate_fx_ref)
    total_after = existing_total + candidate_cny
    if total_after <= 0:
        raise ValueError("option market value after candidate must be positive")
    return {
        "metric_version": OPTION_MARKET_CONCENTRATION_METRIC_VERSION,
        "option_market_concentration_after": float(
            (existing_symbol + candidate_cny) / total_after
        ),
        "option_market_value_cny": float(candidate_cny),
        "evidence_refs": {
            "position_lot_ids": sorted(position_refs),
            "valuation_mark_fact_ids": sorted(mark_refs),
            "fx_rate_fact_ids": sorted(fx_refs),
        },
    }


def _unique_by(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        identity = str(row.get(key) or "").strip().upper() if key == "base_currency" else str(row.get(key) or "")
        if not identity or identity in indexed:
            raise ValueError(f"{key} evidence must be present and unique")
        indexed[identity] = row
    return indexed


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


def _float_setting(raw: dict[str, Any], key: str, default: float) -> float:
    parsed = _float(raw.get(key, default))
    return float(default) if parsed is None else float(parsed)


def _float_setting_from_sources(key: str, default: float, *sources: dict[str, Any]) -> float:
    for source in sources:
        if isinstance(source, dict) and key in source:
            return _float_setting(source, key, default)
    return float(default)


def _bool_setting(raw: dict[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return bool(default)


def _bool_setting_from_sources(key: str, default: bool, *sources: dict[str, Any]) -> bool:
    for source in sources:
        if isinstance(source, dict) and key in source:
            return _bool_setting(source, key, default)
    return bool(default)


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
