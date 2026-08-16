from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


DEFAULT_TIER_A_TARGET_BASIS = {
    "AMZN": 232.0,
    "SPCX": 120.0,
}
DEFAULT_INDEX_SYMBOLS = {"SPY", "QQQ", "RSP", "SMH"}
SPCX_LOCKUP_RELEASE = date(2026, 9, 24)


@dataclass(frozen=True)
class SellPutPolicy:
    target_basis_by_symbol: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_TIER_A_TARGET_BASIS))
    index_symbols: set[str] = field(default_factory=lambda: set(DEFAULT_INDEX_SYMBOLS))
    min_days_to_expiration: int = 21
    max_days_to_expiration: int = 60
    min_out_of_money_pct: float = 0.15
    max_abs_delta: float = 0.30
    min_open_interest: int = 50
    min_volume: int = 0
    max_bid_ask_spread_pct: float = 0.20
    min_iv: float = 0.40
    original_min_annualized_return: float = 0.08
    stress_drop_pcts: tuple[float, ...] = (0.20, 0.35, 0.50)
    profit_take_pct: float = 0.75
    time_exit_dte: int = 14
    reunderwrite_delta: float = 0.35
    close_or_acquire_delta: float = 0.40


@dataclass(frozen=True)
class SellPutCandidate:
    symbol: str
    name: str | None
    current_price: float | None
    strike: float | None
    expiration: str | None
    bid: float | None
    ask: float | None
    mid: float | None
    delta: float | None
    iv: float | None
    volume: int | None
    open_interest: int | None
    contract_symbol: str | None = None
    option_type: str | None = None


def evaluate_sell_put_candidate(
    candidate: SellPutCandidate,
    *,
    policy: SellPutPolicy | None = None,
    portfolio_nav: float | None = None,
) -> dict[str, Any]:
    policy = policy or SellPutPolicy()
    symbol = candidate.symbol.strip().upper()
    current = candidate.current_price
    strike = candidate.strike
    mid = candidate.mid
    dte = _days_to_expiration(candidate.expiration)
    hard_vetoes: list[str] = []
    warnings: list[str] = []

    conviction_tier = _conviction_tier(symbol, policy)
    target_basis = policy.target_basis_by_symbol.get(symbol)
    if conviction_tier == "unauthorized":
        hard_vetoes.append("conviction_not_authorized_for_cash_secured_put")
    if str(candidate.option_type or "put").lower() != "put":
        hard_vetoes.append("not_a_put_contract")
    if current is None or current <= 0:
        hard_vetoes.append("missing_underlying_price")
    if strike is None or strike <= 0:
        hard_vetoes.append("missing_strike")
    if mid is None or mid <= 0:
        hard_vetoes.append("missing_positive_premium")
    if dte is None:
        hard_vetoes.append("missing_expiration")
    elif dte < policy.min_days_to_expiration:
        hard_vetoes.append("too_close_to_expiration")
    elif dte > policy.max_days_to_expiration:
        hard_vetoes.append("too_far_to_expiration")

    effective_basis = (strike - mid) if strike is not None and mid is not None else None
    otm_pct = _out_of_money_pct(current=current, strike=strike)
    effective_discount_pct = _effective_discount_pct(current=current, effective_basis=effective_basis)
    annualized_return = _annualized_return_on_cash(mid=mid, strike=strike, dte=dte)
    spread = _spread(candidate.bid, candidate.ask)
    spread_pct = (spread / mid) if spread is not None and mid and mid > 0 else None
    cash_required = strike * 100 if strike is not None else None

    if otm_pct is None:
        hard_vetoes.append("missing_out_of_money_distance")
    elif otm_pct < policy.min_out_of_money_pct:
        hard_vetoes.append("strike_too_close_to_spot")
    if candidate.delta is not None and abs(candidate.delta) > policy.max_abs_delta:
        hard_vetoes.append("delta_too_high")
    if candidate.open_interest is None or candidate.open_interest < policy.min_open_interest:
        hard_vetoes.append("open_interest_too_low")
    if candidate.volume is not None and candidate.volume < policy.min_volume:
        hard_vetoes.append("volume_too_low")
    if candidate.iv is None:
        hard_vetoes.append("missing_implied_volatility")
    elif candidate.iv < policy.min_iv:
        hard_vetoes.append("implied_volatility_below_threshold")
    if spread_pct is None:
        warnings.append("spread_pct_unavailable")
    elif spread_pct > policy.max_bid_ask_spread_pct:
        hard_vetoes.append("bid_ask_spread_too_wide")

    if target_basis is not None and effective_basis is not None and effective_basis > target_basis:
        hard_vetoes.append("effective_basis_above_authorized_target")
    if conviction_tier == "tier_a" and target_basis is None:
        hard_vetoes.append("missing_authorized_target_basis")

    stress_tests = _stress_tests(current=current, effective_basis=effective_basis, policy=policy)
    portfolio_guardrail = _portfolio_guardrail(
        portfolio_nav=portfolio_nav,
        cash_required=cash_required,
        stress_tests=stress_tests,
        policy=policy,
    )
    hard_vetoes.extend(portfolio_guardrail["hard_vetoes"])
    warnings.extend(portfolio_guardrail["warnings"])

    catalyst = _known_catalyst(symbol=symbol, expiration=candidate.expiration)
    if catalyst:
        if conviction_tier == "tier_a":
            warnings.append(catalyst)
        else:
            hard_vetoes.append(catalyst)

    original = _original_framework_verdict(
        annualized_return=annualized_return,
        dte=dte,
        policy=policy,
        hard_vetoes=hard_vetoes,
    )
    score_components = _score_components(
        conviction_tier=conviction_tier,
        target_basis=target_basis,
        current=current,
        effective_basis=effective_basis,
        otm_pct=otm_pct,
        annualized_return=annualized_return,
        iv=candidate.iv,
        portfolio_nav=portfolio_nav,
        portfolio_guardrail=portfolio_guardrail,
        spread_pct=spread_pct,
        catalyst_warning=bool(catalyst),
    )
    mature_score = round(sum(score_components.values()), 2)
    mature_band = _mature_band(mature_score)
    final_decision = _final_decision(
        hard_vetoes=hard_vetoes,
        warnings=warnings,
        mature_score=mature_score,
        conviction_tier=conviction_tier,
    )

    return {
        "strategy": "sell_put",
        "symbol": symbol,
        "name": candidate.name,
        "conviction_tier": conviction_tier,
        "contract_symbol": candidate.contract_symbol,
        "option_type": "put",
        "expiration": candidate.expiration,
        "dte": dte,
        "underlying_price": current,
        "strike": strike,
        "bid": candidate.bid,
        "ask": candidate.ask,
        "mid": mid,
        "delta": candidate.delta,
        "iv": candidate.iv,
        "volume": candidate.volume,
        "open_interest": candidate.open_interest,
        "bid_ask_spread": spread,
        "bid_ask_spread_pct": _round_or_none(spread_pct, 6),
        "out_of_money_pct": _round_or_none(otm_pct, 6),
        "effective_basis": _round_or_none(effective_basis, 4),
        "authorized_target_basis": target_basis,
        "effective_discount_pct": _round_or_none(effective_discount_pct, 6),
        "cash_required": _round_or_none(cash_required, 2),
        "cash_assumption": "unlimited_cash",
        "premium_per_contract": _round_or_none((mid * 100) if mid is not None else None, 2),
        "annualized_return_on_cash": _round_or_none(annualized_return, 6),
        "stress_tests": stress_tests,
        "portfolio_nav": portfolio_nav,
        "assignment_pct_nav": portfolio_guardrail["assignment_pct_nav"],
        "max_stress_loss_pct_nav": portfolio_guardrail["max_stress_loss_pct_nav"],
        "original_framework": original,
        "mature_score": mature_score,
        "mature_band": mature_band,
        "score_components": score_components,
        "hard_vetoes": hard_vetoes,
        "warnings": warnings,
        "final_decision": final_decision,
        "underwriting_answer": _underwriting_answer(final_decision, hard_vetoes, warnings),
        "exit_plan": {
            "profit_take_pct": policy.profit_take_pct,
            "buy_to_close_at_or_below": _round_or_none((mid * (1 - policy.profit_take_pct)) if mid is not None else None, 4),
            "time_exit_dte": policy.time_exit_dte,
            "reunderwrite_delta": policy.reunderwrite_delta,
            "close_or_acquire_delta": policy.close_or_acquire_delta,
            "rule": "Close for premium harvest by profit target or 14 DTE; if thesis remains intact and acquisition is desired, switch to acquisition mode instead of panicking near strike.",
        },
    }


def parse_target_basis(values: list[str] | tuple[str, ...] | None) -> dict[str, float]:
    out = dict(DEFAULT_TIER_A_TARGET_BASIS)
    for raw in values or []:
        if "=" not in str(raw):
            raise ValueError("--target-basis must use SYMBOL=PRICE")
        symbol, value = str(raw).split("=", 1)
        normalized = symbol.strip().upper()
        if not normalized:
            raise ValueError("--target-basis symbol is required")
        try:
            price = float(value)
        except ValueError as exc:
            raise ValueError(f"--target-basis price for {normalized} must be numeric") from exc
        if price <= 0:
            raise ValueError(f"--target-basis price for {normalized} must be positive")
        out[normalized] = price
    return out


def _conviction_tier(symbol: str, policy: SellPutPolicy) -> str:
    if symbol in policy.target_basis_by_symbol:
        return "tier_a"
    if symbol in policy.index_symbols:
        return "index"
    return "unauthorized"


def _original_framework_verdict(
    *,
    annualized_return: float | None,
    dte: int | None,
    policy: SellPutPolicy,
    hard_vetoes: list[str],
) -> dict[str, Any]:
    failures: list[str] = []
    unresolved = ["macro_liquidity", "vix", "fear_greed", "single_stock_iv_percentile", "earnings_calendar"]
    if dte is None or dte < 14 or dte > 45:
        failures.append("dte_not_14_to_45")
    if annualized_return is None or annualized_return < policy.original_min_annualized_return:
        failures.append("annualized_return_below_8pct")
    blocking = [item for item in hard_vetoes if item not in {"too_far_to_expiration"}]
    if blocking:
        failures.extend(blocking)
    verdict = "NO_GO" if failures else "UNRESOLVED_NEEDS_MARKET_GATES"
    return {"verdict": verdict, "failures": failures, "unresolved": unresolved}


def _score_components(
    *,
    conviction_tier: str,
    target_basis: float | None,
    current: float | None,
    effective_basis: float | None,
    otm_pct: float | None,
    annualized_return: float | None,
    iv: float | None,
    portfolio_nav: float | None,
    portfolio_guardrail: dict[str, Any],
    spread_pct: float | None,
    catalyst_warning: bool,
) -> dict[str, float]:
    acquisition = 0.0
    if target_basis is not None and effective_basis is not None:
        if effective_basis <= target_basis:
            acquisition = 25.0
        else:
            acquisition = max(0.0, 25.0 * (target_basis / effective_basis))
    elif conviction_tier == "index" and otm_pct is not None:
        acquisition = min(20.0, max(0.0, otm_pct / 0.15 * 20.0))

    vol = min(20.0, max(0.0, (annualized_return or 0.0) / 0.20 * 12.0 + (iv or 0.0) / 0.80 * 8.0))

    if portfolio_nav is None:
        portfolio = 20.0
    else:
        stress_pct = portfolio_guardrail.get("max_stress_loss_pct_nav")
        if stress_pct is None:
            portfolio = 8.0
        else:
            portfolio = max(0.0, 20.0 * (1.0 - min(1.0, stress_pct / 0.10)))

    conviction = {"tier_a": 15.0, "index": 12.0, "unauthorized": 0.0}.get(conviction_tier, 0.0)
    market_regime = 5.0
    execution = 10.0
    if spread_pct is None:
        execution -= 2.0
    elif spread_pct > 0.10:
        execution -= min(5.0, (spread_pct - 0.10) / 0.10 * 5.0)
    if catalyst_warning:
        execution -= 3.0

    return {
        "acquisition_price": round(max(0.0, min(25.0, acquisition)), 2),
        "volatility_compensation": round(max(0.0, min(20.0, vol)), 2),
        "portfolio_stress": round(max(0.0, min(20.0, portfolio)), 2),
        "fundamental_conviction": round(max(0.0, min(15.0, conviction)), 2),
        "market_regime": round(market_regime, 2),
        "execution_and_catalysts": round(max(0.0, min(10.0, execution)), 2),
    }


def _final_decision(
    *,
    hard_vetoes: list[str],
    warnings: list[str],
    mature_score: float,
    conviction_tier: str,
) -> str:
    if hard_vetoes:
        return "NO_GO"
    if mature_score >= 80:
        return "GO_NORMAL_SIZE"
    if mature_score >= 70:
        return "GO_SMALL_SIZE" if warnings or conviction_tier == "tier_a" else "GO_REDUCED_SIZE"
    if mature_score >= 60:
        return "WATCH"
    if mature_score >= 50:
        return "WAIT"
    return "NO_GO"


def _mature_band(score: float) -> str:
    if score >= 80:
        return "GO"
    if score >= 70:
        return "GO_REDUCED"
    if score >= 60:
        return "WATCH"
    if score >= 50:
        return "WAIT"
    return "NO_GO"


def _underwriting_answer(final_decision: str, hard_vetoes: list[str], warnings: list[str]) -> str:
    if hard_vetoes:
        return "NO: hard guardrails failed: " + ", ".join(hard_vetoes)
    if final_decision.startswith("GO"):
        suffix = f" Warnings: {', '.join(warnings)}." if warnings else ""
        return "YES: compensation appears sufficient after guardrails, subject to limit-order execution." + suffix
    return "NO: compensation or score is not strong enough for a new cash-secured put."


def _portfolio_guardrail(
    *,
    portfolio_nav: float | None,
    cash_required: float | None,
    stress_tests: list[dict[str, Any]],
    policy: SellPutPolicy,
) -> dict[str, Any]:
    hard_vetoes: list[str] = []
    warnings: list[str] = []
    assignment_pct_nav = None
    max_stress_loss_pct_nav = None
    if portfolio_nav is None or portfolio_nav <= 0:
        return {
            "hard_vetoes": hard_vetoes,
            "warnings": warnings,
            "assignment_pct_nav": assignment_pct_nav,
            "max_stress_loss_pct_nav": max_stress_loss_pct_nav,
        }
    if cash_required is not None:
        assignment_pct_nav = cash_required / portfolio_nav
    losses = [abs(float(item["loss_dollars"])) for item in stress_tests if item.get("loss_dollars") is not None]
    if losses:
        max_stress_loss_pct_nav = max(losses) / portfolio_nav
    return {
        "hard_vetoes": hard_vetoes,
        "warnings": warnings,
        "assignment_pct_nav": _round_or_none(assignment_pct_nav, 6),
        "max_stress_loss_pct_nav": _round_or_none(max_stress_loss_pct_nav, 6),
    }


def _stress_tests(
    *,
    current: float | None,
    effective_basis: float | None,
    policy: SellPutPolicy,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for drop in policy.stress_drop_pcts:
        stress_price = current * (1.0 - drop) if current is not None else None
        loss = ((stress_price - effective_basis) * 100.0) if stress_price is not None and effective_basis is not None else None
        out.append(
            {
                "underlying_drop_pct": drop,
                "stress_price": _round_or_none(stress_price, 4),
                "loss_dollars": _round_or_none(loss, 2),
            }
        )
    return out


def _known_catalyst(*, symbol: str, expiration: str | None) -> str | None:
    if symbol != "SPCX" or not expiration:
        return None
    exp = _parse_date(expiration)
    if exp is not None and exp >= SPCX_LOCKUP_RELEASE:
        return "known_catalyst_spcx_lockup_release_before_or_at_expiration"
    return None


def _out_of_money_pct(*, current: float | None, strike: float | None) -> float | None:
    if current is None or strike is None or current <= 0:
        return None
    return (current - strike) / current


def _effective_discount_pct(*, current: float | None, effective_basis: float | None) -> float | None:
    if current is None or effective_basis is None or current <= 0:
        return None
    return (current - effective_basis) / current


def _annualized_return_on_cash(*, mid: float | None, strike: float | None, dte: int | None) -> float | None:
    if mid is None or strike is None or dte is None or strike <= 0 or dte <= 0:
        return None
    return (mid / strike) * (365.0 / dte)


def _spread(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None:
        return None
    return max(0.0, ask - bid)


def _days_to_expiration(value: str | None) -> int | None:
    exp = _parse_date(value)
    if exp is None:
        return None
    return max(0, (exp - date.today()).days)


def _parse_date(value: str | None) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def _round_or_none(value: float | None, digits: int) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


__all__ = [
    "DEFAULT_TIER_A_TARGET_BASIS",
    "SellPutCandidate",
    "SellPutPolicy",
    "evaluate_sell_put_candidate",
    "parse_target_basis",
]
