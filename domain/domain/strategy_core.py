"""Ordinary CSP/CC calculations over resolved, offline facts.

Callers retain their prefilter/cash/underwriting phase order. Evaluation accepts
only rows reaching underwriting; prior-stage rejects stay with the caller.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import math
from typing import Any

from domain.domain.candidate_defaults import DEFAULT_SELL_CALL_WINDOW, DEFAULT_SELL_PUT_WINDOW
from domain.domain.currency_conversion import (
    CurrencyConverter, ExchangeRates, covered_notional_cny, enrich_short_vol_contract_cny_fields,
)
from domain.domain.engine.candidate_engine import calculate_opening_candidate_metrics, rank_candidate_rows
from domain.domain.insurance_underwriting import (
    INSURANCE_UNDERWRITING_PROFILE, InsuranceUnderwritingConfig, evaluate_underwriting_candidate,
)
from domain.domain.option_position_identity import normalize_currency
from domain.domain.risk_capacity import (
    compute_sell_call_share_capacity, compute_sell_put_cash_capacity, compute_sell_put_effective_cash,
)
from domain.domain.sell_call_config import resolve_effective_sell_call_min_strike
from domain.domain.short_vol_assessment import ShortVolPortfolioContext, _float, portfolio_concentration_fields
from domain.domain.trade_contract_identity import require_option_multiplier

__version__ = "0.1.0"


def _mode(mode: str) -> str:
    if mode not in ("put", "call"):
        raise ValueError("mode must be put or call")
    return mode


@dataclass(frozen=True)
class ResolvedStrategy:
    mode: str
    config: InsuranceUnderwritingConfig
    ranking_profile: str = "current_tie_break"
    account: str | None = None
    market: str | None = None

    def __post_init__(self) -> None:
        _mode(self.mode)
        if type(self.config) is not InsuranceUnderwritingConfig:
            raise TypeError("config must be an effective InsuranceUnderwritingConfig")
        if self.config.strategy != INSURANCE_UNDERWRITING_PROFILE:
            raise ValueError("only insurance_underwriting is supported")
        if self.ranking_profile not in ("current_tie_break", "concentration_first"):
            raise ValueError("unsupported ranking profile")
        if self.mode == "call" and self.ranking_profile != "current_tie_break":
            raise ValueError("CC does not support CSP ranking profiles")
        if self.account is not None and (
            not isinstance(self.account, str) or not self.account or self.account != self.account.strip().lower()
        ):
            raise ValueError("account must be a nonempty lowercase label")
        if self.market is not None and self.market not in ("us", "hk"):
            raise ValueError("market must be us or hk")
        for name, value in asdict(self.config).items():
            if name == "strategy":
                continue
            if value is None and name in ("min_strike", "max_strike", "min_dte", "max_dte"):
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} must be finite numeric policy")
            if value < 0 or (name in ("min_strike", "max_strike") and value == 0):
                raise ValueError(f"{name} is out of range")
            if name in ("min_dte", "max_dte") and not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
            if name == "min_annualized_return" and value > 1:
                raise ValueError("min_annualized_return must be within [0, 1]")
        for low, high in ((self.config.min_dte, self.config.max_dte), (self.config.min_strike, self.config.max_strike)):
            if low is not None and high is not None and low > high:
                raise ValueError("policy minimum exceeds maximum")

    @property
    def identity(self) -> dict[str, Any]:
        window = DEFAULT_SELL_PUT_WINDOW if self.mode == "put" else DEFAULT_SELL_CALL_WINDOW
        baseline = InsuranceUnderwritingConfig(min_dte=window.min_dte, max_dte=window.max_dte)
        parameters = asdict(self.config)
        strike_inputs = {key: parameters.pop(key) for key in ("min_strike", "max_strike")}
        baseline_parameters = asdict(baseline)
        for key in strike_inputs:
            baseline_parameters.pop(key)
        policy_json = json.dumps(
            {"parameters": parameters, "ranking_profile": self.ranking_profile},
            sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
        return {
            "core_version": __version__,
            "template": f"{'csp' if self.mode == 'put' else 'cc'}_baseline",
            "template_version": __version__,
            "policy_kind": (
                "baseline" if parameters == baseline_parameters and self.ranking_profile == "current_tie_break"
                else "custom"
            ),
            "strike_inputs": strike_inputs,
            "effective_policy_hash": hashlib.sha256(policy_json.encode()).hexdigest(),
            "ranking_profile": self.ranking_profile,
            "scope": {"account": self.account, "market": self.market, "mode": self.mode},
        }


def resolve_strategy(
    *, mode: str, config: InsuranceUnderwritingConfig,
    ranking_profile: str = "current_tie_break", account: str | None = None, market: str | None = None,
) -> ResolvedStrategy:
    """Bind already-merged policy, never read or interpret legacy configuration."""
    return ResolvedStrategy(mode, config, ranking_profile, account, market)


def calculate_metrics(
    row: dict[str, Any], *, mode: str, now_utc: datetime,
    avg_cost: float | None = None, exchange_rates: ExchangeRates | None = None,
) -> dict[str, Any]:
    """Return metric fields with the old post-rounding CNY supplement."""
    _mode(mode)
    if not isinstance(now_utc, datetime) or now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError("now_utc must be a timezone-aware datetime")
    if mode == "call" and (
        isinstance(avg_cost, bool) or not isinstance(avg_cost, (int, float))
        or not math.isfinite(avg_cost) or avg_cost <= 0
    ):
        raise ValueError("CC requires explicit positive finite avg_cost")
    metrics = calculate_opening_candidate_metrics(row, mode=mode, avg_cost=avg_cost, now_utc=now_utc)
    metrics.update(enrich_short_vol_contract_cny_fields(
        {**row, **metrics}, exchange_rate_converter=CurrencyConverter(exchange_rates or ExchangeRates()),
    ))
    return metrics


@dataclass(frozen=True)
class PutCapacityInputs:
    cash_by_currency: tuple[tuple[str, float], ...]
    cash_secured_by_currency: tuple[tuple[str, float], ...] = ()
    fx_status: str | None = None
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class CallCapacityInputs:
    shares_total: int
    shares_can_sell: int | None
    shares_locked: int = 0


def calculate_constraints(
    row: dict[str, Any], *, mode: str, risk_ctx: ShortVolPortfolioContext,
    exchange_rate_converter: CurrencyConverter, min_strike: float | None = None,
    min_strike_cost_multiplier: float = 1.02,
    capacity_inputs: PutCapacityInputs | CallCapacityInputs | None = None,
) -> dict[str, Any]:
    """Return a new enriched row; frozen evidence remains owned by the caller.

    Capacity facts have already passed account/snapshot qualification in the
    adapter. An unavailable collateral context must carry unavailable_reason.
    """
    _mode(mode)
    out = dict(row)
    if mode == "call":
        effective = resolve_effective_sell_call_min_strike(
            min_strike=min_strike, avg_cost=out.get("avg_cost"), cost_multiplier=min_strike_cost_multiplier,
        )
        spot = _float(out.get("spot"))
        if spot is not None:
            effective = spot if effective is None else max(effective, spot)
        if effective is not None:
            out["effective_min_strike"] = effective
        out.setdefault("covered_notional_cny", covered_notional_cny(out, exchange_rate_converter=exchange_rate_converter))
    if capacity_inputs is not None:
        if mode == "call":
            if not isinstance(capacity_inputs, CallCapacityInputs):
                raise TypeError("CC requires CallCapacityInputs")
            capacity = compute_sell_call_share_capacity(**asdict(capacity_inputs), multiplier=out.get("multiplier"))
            out.update({key: value for key, value in asdict(capacity).items() if key not in ("accepted", "reason")})
            out["share_capacity_reason"] = capacity.reason
        else:
            if not isinstance(capacity_inputs, PutCapacityInputs):
                raise TypeError("CSP requires PutCapacityInputs")
            required = float(out["strike"]) * require_option_multiplier(out.get("multiplier"))
            native = normalize_currency(out.get("currency"))
            headroom = compute_sell_put_effective_cash(
                cash_by_currency=dict(capacity_inputs.cash_by_currency),
                cash_secured_by_currency=dict(capacity_inputs.cash_secured_by_currency),
                native_currency=native, cash_required_native=required, fx_status=capacity_inputs.fx_status,
                convert_currency=lambda amount, source, target: exchange_rate_converter.convert(
                    amount, from_ccy=source, to_ccy=target,
                ),
            )
            available = headroom.available and not capacity_inputs.unavailable_reason
            free = headroom.cash_free if available else None
            capacity = compute_sell_put_cash_capacity(
                cash_required_native=required, cash_free_effective_native=free, cash_native_currency=native,
            )
            out.update({
                "put_cash_required": required, "put_cash_free": free,
                "put_cash_capacity_available": bool(available),
                "put_cash_capacity_reason": capacity_inputs.unavailable_reason or headroom.reason,
                "cash_required_native": required, "cash_free_effective_native": free,
                "cash_native_currency": native, "cash_capacity_basis": capacity.basis,
                # The scan uses //; floor(free / required) differs at float boundaries.
                "max_new_contracts": int(free // required) if free is not None and required > 0 else 0,
            })
    out.update(enrich_short_vol_contract_cny_fields(out, exchange_rate_converter=exchange_rate_converter))
    out.update(portfolio_concentration_fields(out, mode=mode, risk_ctx=risk_ctx))
    return out


def evaluate_candidates(rows: list[dict[str, Any]], *, strategy: ResolvedStrategy) -> dict[str, Any]:
    """Evaluate the underwriting-stage universe, keeping every decision and pass."""
    decisions = []
    accepted = []
    for row in rows:
        for field, expected in (("account", strategy.account), ("market", strategy.market), ("option_type", strategy.mode)):
            actual = row.get(field)
            if expected is not None and actual is not None and str(actual).lower() != expected:
                raise ValueError(f"candidate {field} conflicts with resolved strategy")
        decision = evaluate_underwriting_candidate(dict(row), mode=strategy.mode, cfg=strategy.config)
        decisions.append(decision)
        if decision["accepted"]:
            accepted.append({**row, **decision["fields"]})
    accepted = rank_candidate_rows(accepted, mode=strategy.mode, sell_put_ranking_profile=strategy.ranking_profile)
    # The engine puts each symbol's representative first, including experiments.
    seen = set()
    representatives = []
    for row in accepted:
        key = str(row.get("symbol") or "").strip().upper() or str(row.get("contract_symbol") or row.get("option_symbol") or "")
        if key not in seen:
            seen.add(key)
            representatives.append(row)
    return {"decisions": decisions, "accepted": accepted, "representatives": representatives, "identity": strategy.identity}
