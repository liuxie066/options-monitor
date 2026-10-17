"""Ordinary CSP/CC calculations over resolved, offline facts.

Callers retain their prefilter/cash/underwriting phase order. Evaluation accepts
only rows reaching underwriting; prior-stage rejects stay with the caller.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime
import math
from typing import Any, Callable

from domain.domain.candidate_defaults import DEFAULT_SELL_CALL_WINDOW, DEFAULT_SELL_PUT_WINDOW
from domain.domain.currency_conversion import (
    CurrencyConverter, ExchangeRates, covered_notional_cny, enrich_short_vol_contract_cny_fields,
)
from domain.domain.decision_state_fingerprint import canonicalize_decision_value
from domain.domain.engine.candidate_engine import (
    CandidateCalculationError, calculate_opening_candidate_metrics, rank_candidate_rows, explain_candidate_rank,
    build_candidate_decision, validate_candidate_decision_payload, opening_qualification_rejects, merge_opening_rejects,
)
from domain.domain.insurance_underwriting import (
    INSURANCE_UNDERWRITING_PROFILE, InsuranceUnderwritingConfig, evaluate_underwriting_candidate,
    underwriting_fields, build_underwriting_result,
)
from domain.domain.option_position_identity import normalize_currency
from domain.domain.risk_capacity import (
    compute_sell_call_share_capacity, compute_sell_put_cash_capacity, compute_sell_put_effective_cash,
)
from domain.domain.sell_call_config import resolve_effective_sell_call_min_strike
from domain.domain.short_vol_assessment import ShortVolPortfolioContext, _float, portfolio_concentration_fields
from domain.domain.trade_contract_identity import require_option_multiplier
from domain.domain.strategy_identity import (
    IDENTITY_FORMAT, implementation_policy_hash, strategy_policy_hash, candidate_ranking_profile,
    validate_strategy_identity as _validated_strategy_identity,
)

__version__ = "0.1.0"


def _mode(mode: str) -> str:
    if mode not in ("put", "call"):
        raise ValueError("mode must be put or call")
    return mode


@dataclass(frozen=True)
class StrategyTemplate:
    template_id: str
    version: str
    mode: str
    constraints: Callable[..., dict[str, Any]]
    evaluate: Callable[..., dict[str, Any]]
    rank: Callable[..., list[dict[str, Any]]]
    explain: Callable[..., dict[str, Any]]


def _baseline_constraints(row, *, mode, cfg):
    if mode == "put":
        return {}
    effective = resolve_effective_sell_call_min_strike(
        min_strike=cfg.min_strike, avg_cost=row.get("avg_cost"), cost_multiplier=cfg.min_strike_cost_multiplier,
    )
    spot = _float(row.get("spot"))
    if spot is not None:
        effective = spot if effective is None else max(effective, spot)
    return {"effective_min_strike": effective} if effective is not None else {}


def _baseline_evaluate(row, *, mode, cfg):
    return evaluate_underwriting_candidate(row, mode=mode, cfg=cfg)["opening_decision"]


def _baseline_rank(rows, *, mode, ranking_profile):
    return rank_candidate_rows(rows, mode=mode, sell_put_ranking_profile=ranking_profile)


def _baseline_explain(row, *, mode, cfg):
    return explain_candidate_rank(row, mode=mode)


# Source-distributed implementations only; no registration API or serialized callbacks.
_TEMPLATES = {
    (name, "0.1.0"): StrategyTemplate(name, "0.1.0", mode, _baseline_constraints,
                                    _baseline_evaluate, _baseline_rank, _baseline_explain)
    for name, mode in (("csp_baseline", "put"), ("cc_baseline", "call"))
}


def _lookup_template(template_id: str, version: str, mode: str) -> StrategyTemplate:
    descriptor = _TEMPLATES.get((template_id, version))
    if descriptor is None or (descriptor.template_id, descriptor.version, descriptor.mode) != (template_id, version, mode):
        raise ValueError(f"unavailable strategy template: {template_id}@{version} for {mode}")
    return descriptor


@dataclass(frozen=True)
class ResolvedStrategy:
    mode: str
    config: InsuranceUnderwritingConfig
    ranking_profile: str = "current_tie_break"
    account: str | None = None
    market: str | None = None
    template_id: str | None = field(default=None, compare=False)
    template_version: str = "0.1.0"
    descriptor: StrategyTemplate = field(init=False, repr=False)

    def __post_init__(self) -> None:
        _mode(self.mode)
        name = self.template_id or f"{'csp' if self.mode == 'put' else 'cc'}_baseline"
        object.__setattr__(self, "descriptor", _lookup_template(name, self.template_version, self.mode))
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
            if value < 0 or (name in ("min_strike", "max_strike", "min_strike_cost_multiplier") and value == 0):
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
        identity = {
            "identity_format": IDENTITY_FORMAT,
            "core_version": __version__,
            "template": self.descriptor.template_id,
            "template_version": self.descriptor.version,
            "policy_kind": (
                "baseline" if self.descriptor.template_id in ("csp_baseline", "cc_baseline")
                and parameters == baseline_parameters and self.ranking_profile == "current_tie_break"
                else "custom"
            ),
            "strike_inputs": strike_inputs,
            "effective_parameters": parameters,
            "ranking_profile": self.ranking_profile,
            "scope": {"account": self.account, "market": self.market, "mode": self.mode},
        }

        identity["effective_policy_hash"] = implementation_policy_hash(identity)
        return identity


def resolve_strategy(
    *, mode: str, config: InsuranceUnderwritingConfig,
    ranking_profile: str = "current_tie_break", account: str | None = None, market: str | None = None,
    template_id: str | None = None, template_version: str = "0.1.0",
) -> ResolvedStrategy:
    """Bind already-merged policy, never read or interpret legacy configuration."""
    return ResolvedStrategy(mode, config, ranking_profile, account, market, template_id, template_version)


def validate_strategy_identity(
    row: dict[str, Any], *, mode: str, account: str | None = None, market: str | None = None,
) -> ResolvedStrategy | None:
    """Restore a validated resolution at the evaluation/application boundary."""
    identity = _validated_strategy_identity(row, mode=mode, account=account, market=market)
    if identity is None:
        return None
    try:
        scope = identity["scope"]
        strategy = resolve_strategy(
            mode=scope["mode"], account=scope["account"], market=scope["market"],
            config=InsuranceUnderwritingConfig(**identity["effective_parameters"], **identity["strike_inputs"]),
            ranking_profile=identity["ranking_profile"],
            template_id=identity["template"], template_version=identity["template_version"],
        )
    except (KeyError, TypeError) as exc:
        raise ValueError("invalid strategy policy identity") from exc
    if identity.get("identity_format"):
        if {key: identity[key] for key in strategy.identity} != strategy.identity:
            raise ValueError("strategy identity does not match effective policy")
    else:
        legacy = strategy.identity
        legacy.pop("identity_format")
        legacy["effective_policy_hash"] = strategy_policy_hash(legacy["effective_parameters"], legacy["ranking_profile"])
        if {key: identity[key] for key in legacy} != legacy:
            raise ValueError("unsupported legacy strategy implementation")
    return strategy


def calculate_metrics(
    row: dict[str, Any], *, mode: str, now_utc: datetime,
    avg_cost: float | None = None, exchange_rates: ExchangeRates | None = None,
) -> dict[str, Any]:
    """Return metric fields with the old post-rounding CNY supplement."""
    _mode(mode)
    if not isinstance(now_utc, datetime) or now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError("now_utc must be a timezone-aware datetime")
    metrics = calculate_opening_candidate_metrics(row, mode=mode, avg_cost=avg_cost, now_utc=now_utc)
    if mode == "call" and (
        isinstance(avg_cost, bool) or not isinstance(avg_cost, (int, float))
        or not math.isfinite(avg_cost) or avg_cost <= 0
    ):
        raise CandidateCalculationError("avg_cost_invalid", "CC requires explicit positive finite avg_cost")
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


def calculate_capacity(
    row: dict[str, Any], *, mode: str, capacity_inputs: PutCapacityInputs | CallCapacityInputs,
    exchange_rate_converter: CurrencyConverter,
) -> dict[str, Any]:
    """Pure early capacity sub-entry; adapters retain evidence qualification."""
    _mode(mode)
    out: dict[str, Any] = {}
    if mode == "call":
        if not isinstance(capacity_inputs, CallCapacityInputs):
            raise TypeError("CC requires CallCapacityInputs")
        capacity = compute_sell_call_share_capacity(**asdict(capacity_inputs), multiplier=row.get("multiplier"))
        out.update({key: value for key, value in asdict(capacity).items() if key not in ("accepted", "reason")})
        out["share_capacity_reason"] = capacity.reason
    else:
        if not isinstance(capacity_inputs, PutCapacityInputs):
            raise TypeError("CSP requires PutCapacityInputs")
        required = float(row["strike"]) * require_option_multiplier(row.get("multiplier"))
        native = normalize_currency(row.get("currency"))
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
    return out


def calculate_constraints(
    row: dict[str, Any], *, mode: str, risk_ctx: ShortVolPortfolioContext,
    exchange_rate_converter: CurrencyConverter, min_strike: float | None = None,
    min_strike_cost_multiplier: float = 1.02,
    capacity_inputs: PutCapacityInputs | CallCapacityInputs | None = None,
    strategy: ResolvedStrategy | None = None,
) -> dict[str, Any]:
    """Return a new enriched row; frozen evidence remains owned by the caller.

    Capacity facts have already passed account/snapshot qualification in the
    adapter. An unavailable collateral context must carry unavailable_reason.
    """
    _mode(mode)
    out = dict(row)
    if mode == "call":
        out.setdefault("covered_notional_cny", covered_notional_cny(out, exchange_rate_converter=exchange_rate_converter))
    if capacity_inputs is not None:
        out.update(calculate_capacity(out, mode=mode, capacity_inputs=capacity_inputs,
                                      exchange_rate_converter=exchange_rate_converter))
    out.update(enrich_short_vol_contract_cny_fields(out, exchange_rate_converter=exchange_rate_converter))
    out.update(portfolio_concentration_fields(out, mode=mode, risk_ctx=risk_ctx))
    if strategy is not None and strategy.mode != mode:
        raise ValueError("constraint strategy mode mismatch")
    cfg = strategy.config if strategy else InsuranceUnderwritingConfig(
        min_strike=min_strike, min_strike_cost_multiplier=min_strike_cost_multiplier,
    )
    descriptor = _execution_template(strategy) if strategy else _lookup_template(
        f"{'csp' if mode == 'put' else 'cc'}_baseline", "0.1.0", mode,
    )
    annotated = {**out, **underwriting_fields(out, mode=mode, cfg=cfg)}
    fields = _constraint_fields(descriptor, annotated, mode=mode, cfg=cfg)
    out.update(fields)
    return out


def _constraint_fields(descriptor, row, *, mode, cfg):
    fields = _invoke(descriptor.constraints, row, mode=mode, cfg=cfg)
    if not isinstance(fields, dict) or any(
        not isinstance(key, str) or (key != "effective_min_strike" and not key.startswith("policy_"))
        or not (value is None or isinstance(value, (str, bool, int, float)))
        or (isinstance(value, float) and not math.isfinite(value))
        for key, value in fields.items()
    ):
        raise ValueError("template constraints may only return scalar policy fields")
    return fields


def evaluate_candidates(rows: list[dict[str, Any]], *, strategy: ResolvedStrategy) -> dict[str, Any]:
    """Evaluate the underwriting-stage universe, keeping every decision and pass."""
    descriptor = _execution_template(strategy)
    decisions = []
    accepted = []
    for row in rows:
        transported = validate_strategy_identity(row, mode=strategy.mode)
        if transported is not None and transported != strategy:
            raise ValueError("candidate identity conflicts with evaluated strategy")
        for field, expected in (("account", strategy.account), ("market", strategy.market), ("option_type", strategy.mode)):
            actual = row.get(field)
            if expected is not None and actual is not None and str(actual).lower() != expected:
                raise ValueError(f"candidate {field} conflicts with resolved strategy")
        fields = underwriting_fields(row, mode=strategy.mode, cfg=strategy.config)
        fields = {**{key: value for key, value in row.items() if key.startswith("policy_")}, **fields}
        canonical = {**row, **fields}
        # Reproject policy scalars after shared annotations; never recompute capacity here.
        constraints = _constraint_fields(descriptor, canonical, mode=strategy.mode, cfg=strategy.config)
        fields.update({key: value for key, value in constraints.items() if key.startswith("policy_")})
        canonical.update(fields)
        policy = _invoke(descriptor.evaluate, canonical, mode=strategy.mode, cfg=strategy.config)
        policy = validate_candidate_decision_payload(policy)
        if (_stable(policy.get("normalized_input")) != _stable(canonical)
                or policy["mode"] != strategy.mode
                or policy.get("symbol") != str(row.get("symbol") or "").strip().upper()
                or policy.get("contract_symbol") != _contract_id(row)
                or policy["accepted"] != (not policy["rejects"])):
            raise ValueError("template evaluator changed canonical input or decision scope")
        rejects = merge_opening_rejects(policy["rejects"], opening_qualification_rejects(
            canonical, mode=strategy.mode, min_net_premium_cny=strategy.config.min_net_income,
        ))
        opening = build_candidate_decision(
            mode=strategy.mode, symbol=policy["symbol"], contract_symbol=policy["contract_symbol"],
            accepted=not rejects, rejects=rejects, normalized_input=canonical,
        )
        decision = build_underwriting_result(fields, opening)
        decisions.append(decision)
        if decision["accepted"]:
            accepted.append({**row, **decision["fields"]})
    accepted = rank_candidates(accepted, mode=strategy.mode, strategy=strategy)
    # The engine puts each symbol's representative first, including experiments.
    seen = set()
    representatives = []
    for row in accepted:
        key = str(row.get("symbol") or "").strip().upper() or str(row.get("contract_symbol") or row.get("option_symbol") or "")
        if key not in seen:
            seen.add(key)
            representatives.append(row)
    return {"decisions": decisions, "accepted": accepted, "representatives": representatives, "identity": strategy.identity}


def _execution_template(strategy: ResolvedStrategy) -> StrategyTemplate:
    descriptor = _lookup_template(strategy.descriptor.template_id, strategy.descriptor.version, strategy.mode)
    if descriptor != strategy.descriptor:
        raise ValueError("resolved template implementation changed")
    return descriptor


def _stable(value: Any) -> Any:
    # Compare every field, including display/provenance fields excluded by fingerprints.
    if isinstance(value, dict):
        return tuple((key, _stable(item)) for key, item in sorted(value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_stable(item) for item in value)
    if (isinstance(value, float) and math.isnan(value)) or type(value).__name__ == "NAType":
        return None
    return canonicalize_decision_value(value)


def _invoke(operation, facts, **kwargs):
    supplied, arguments = deepcopy(facts), deepcopy(kwargs)
    before, settings = _stable(supplied), deepcopy(arguments)
    result = operation(supplied, **arguments)
    if _stable(supplied) != before or arguments != settings:
        raise ValueError("template mutated frozen facts or configuration")
    return result


def _contract_id(row):
    return str(row.get("contract_symbol") or row.get("option_symbol") or "").strip()


def rank_candidates(rows: list[dict[str, Any]], *, mode: str,
                    strategy: ResolvedStrategy | None = None) -> list[dict[str, Any]]:
    """Dispatch evaluated rows without screening again or changing baseline tail order."""
    candidate_ranking_profile(rows, mode=mode)
    resolutions = [validate_strategy_identity(row, mode=mode) for row in rows]
    bound = [item for item in resolutions if item is not None]
    if strategy is not None:
        if strategy.mode != mode or any(
            item.descriptor != strategy.descriptor or item.ranking_profile != strategy.ranking_profile
            or (item.account, item.market) != (strategy.account, strategy.market) for item in bound
        ):
            raise ValueError("ranking strategy conflicts with candidate identity")
    elif bound:
        strategy = bound[0]
    if strategy is None:
        return rank_candidate_rows(rows, mode=mode)
    descriptor = _execution_template(strategy)
    if not rows:
        return []
    originals = {_contract_id(row): _stable(row) for row in rows}
    if len(originals) != len(rows) or "" in originals:
        raise ValueError("duplicate or missing ranking contract identity")
    ranked = _invoke(descriptor.rank, rows, mode=mode, ranking_profile=strategy.ranking_profile)
    if not isinstance(ranked, list) or any(not isinstance(row, dict) for row in ranked):
        raise ValueError("template ranking must be a permutation")
    actual = {_contract_id(row): _stable(row) for row in ranked}
    if len(ranked) != len(rows) or len(actual) != len(ranked) or actual != originals:
        raise ValueError("template ranking changed candidate membership or facts")
    return ranked


def explain_rank(row: dict[str, Any], *, mode: str) -> dict[str, Any]:
    strategy = validate_strategy_identity(row, mode=mode)
    if strategy is None:
        return explain_candidate_rank(row, mode=mode)
    result = _invoke(_execution_template(strategy).explain, row, mode=mode, cfg=strategy.config)
    if (not isinstance(result, dict) or not isinstance(result.get("primary_drivers"), list)
            or not all(isinstance(item, str) for item in result["primary_drivers"])
            or not isinstance(result.get("rank_reason"), str)):
        raise ValueError("invalid template rank explanation")
    for key in result.keys() & row.keys():
        if _stable(result[key]) != _stable(row[key]):
            raise ValueError("template explanation changed candidate facts")
    return result
