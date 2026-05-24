from __future__ import annotations

from collections import Counter
from typing import Any

from src.application.strategy_lab.contracts import (
    BacktestResult,
    CandidateSnapshot,
    MetricSet,
    StrategyExperiment,
    StrategyLabEvidence,
    StrategyPolicy,
)


def run_replay_backtest(experiment: StrategyExperiment, evidence: StrategyLabEvidence) -> BacktestResult:
    universe = _strategy_universe(evidence, strategy_type=experiment.strategy_type, account=experiment.account)
    baseline = _select(experiment.baseline_policy, universe)
    candidate = _select(experiment.candidate_policy, universe)
    baseline_metrics = _metric_set(baseline, universe=universe, policy=experiment.baseline_policy, strategy_type=experiment.strategy_type)
    candidate_metrics = _metric_set(candidate, universe=universe, policy=experiment.candidate_policy, strategy_type=experiment.strategy_type)
    comparison = _compare(baseline_metrics, candidate_metrics)
    conclusion = _conclusion(comparison, candidate_metrics)
    warnings = tuple(
        item
        for item in (
            *evidence.warnings,
            *_metric_warnings("baseline", baseline_metrics),
            *_metric_warnings("candidate", candidate_metrics),
        )
        if item
    )
    return BacktestResult(
        experiment=experiment,
        baseline_metrics=baseline_metrics,
        candidate_metrics=candidate_metrics,
        comparison=comparison,
        conclusion=conclusion,
        evidence=evidence,
        warnings=warnings,
    )


def _strategy_universe(evidence: StrategyLabEvidence, *, strategy_type: str, account: str | None) -> list[CandidateSnapshot]:
    account_norm = str(account or "").strip().lower() or None
    rows: list[CandidateSnapshot] = []
    for row in (*evidence.candidates, *evidence.reject_logs):
        if account_norm and row.account and row.account != account_norm:
            continue
        if _matches_strategy(row, strategy_type):
            rows.append(row)
    return rows


def _matches_strategy(row: CandidateSnapshot, strategy_type: str) -> bool:
    if row.strategy_type == strategy_type:
        return True
    if strategy_type == "sell_put":
        return row.option_type == "put" and row.side in {"short", "sell"}
    if strategy_type == "sell_call":
        return row.option_type == "call" and row.side in {"short", "sell"}
    return False


def _select(policy: StrategyPolicy, universe: list[CandidateSnapshot]) -> list[CandidateSnapshot]:
    params = dict(policy.params)
    source = str(params.get("selection_source") or "rules").strip().lower()
    if source == "existing":
        selected = [row for row in universe if row.evidence_ref.kind == "candidate" and row.selected is not False]
    elif source == "rules":
        selected = [row for row in universe if _passes_rules(row, params)]
    else:
        raise ValueError(f"unsupported strategy policy selection_source: {source}")
    max_candidates = _as_int(params.get("max_candidates"))
    if max_candidates is not None:
        selected = selected[: max(0, max_candidates)]
    return selected


def _passes_rules(row: CandidateSnapshot, params: dict[str, Any]) -> bool:
    symbols = _symbol_set(params.get("symbols") or params.get("symbol_scope"))
    if symbols and (row.symbol or "").upper() not in symbols:
        return False
    if not _between(row.dte, low=_as_int(params.get("min_dte")), high=_as_int(params.get("max_dte"))):
        return False
    abs_delta = abs(row.delta) if row.delta is not None else None
    if not _between(abs_delta, low=_as_float(params.get("min_abs_delta") or params.get("min_delta")), high=_as_float(params.get("max_abs_delta") or params.get("max_delta"))):
        return False
    if not _between(row.premium, low=_as_float(params.get("min_premium")), high=_as_float(params.get("max_premium"))):
        return False
    if not _between(row.strike, low=_as_float(params.get("min_strike")), high=_as_float(params.get("max_strike"))):
        return False
    excluded_reasons = {str(item).strip() for item in _list(params.get("exclude_reject_reasons")) if str(item).strip()}
    if excluded_reasons and any(reason in excluded_reasons for reason in row.reject_reasons):
        return False
    return True


def _metric_set(selected: list[CandidateSnapshot], *, universe: list[CandidateSnapshot], policy: StrategyPolicy, strategy_type: str) -> MetricSet:
    premium_values = [_premium_value(row) for row in selected]
    realized_pnl_values = [_realized_pnl_value(row) for row in selected]
    locked_cash_values = [_locked_cash(row, strategy_type=strategy_type) for row in selected]
    locked_cash_days_values = [
        cash * max(1, row.dte or 0)
        for row, cash in zip(selected, locked_cash_values, strict=False)
        if cash is not None
    ]
    net_cash_inflow = _sum_known(premium_values)
    locked_cash = _sum_known(locked_cash_values)
    locked_cash_days = _sum_known(locked_cash_days_values)
    return_per_locked_cash_day = (
        net_cash_inflow / locked_cash_days
        if net_cash_inflow is not None and locked_cash_days not in (None, 0)
        else None
    )
    tail_loss_values = [
        premium - cash
        for premium, cash in zip(premium_values, locked_cash_values, strict=False)
        if premium is not None and cash is not None
    ] if strategy_type == "sell_put" else [_tail_loss_value(row) for row in selected]
    tail_loss_values = [value for value in tail_loss_values if value is not None]
    worst_trade_pnl = min(tail_loss_values) if tail_loss_values else None
    tail_loss_scenario = sum(tail_loss_values) if tail_loss_values else None
    avg_holding_days = _avg([row.dte for row in selected if row.dte is not None])
    min_sample = _as_int(policy.params.get("min_sample")) or 5
    selected_count = len(selected)
    warnings = []
    if selected_count < min_sample:
        warnings.append("sample_size_below_minimum")
    if any(_locked_cash(row, strategy_type=strategy_type) is None for row in selected):
        warnings.append("locked_cash_incomplete")
    if selected and return_per_locked_cash_day is None:
        warnings.append("return_per_locked_cash_day_unavailable")
    if strategy_type in {"sell_call", "yield_enhancement"} and selected and not any(value is not None for value in locked_cash_values):
        warnings.append("capital_basis_unavailable")
    if strategy_type == "close_advice" and selected and not any(value is not None for value in realized_pnl_values):
        warnings.append("realized_pnl_incomplete")
    return MetricSet(
        returns={
            "net_cash_inflow": net_cash_inflow,
            "net_premium": net_cash_inflow,
            "realized_pnl": _sum_known(realized_pnl_values),
            "premium_capture_rate": None,
            "annualized_return_on_locked_cash": return_per_locked_cash_day * 365 if return_per_locked_cash_day is not None else None,
        },
        capital={
            "locked_cash_days": locked_cash_days,
            "return_per_locked_cash_day": return_per_locked_cash_day,
            "margin_utilization_peak": None,
            "cash_buffer_min": None,
        },
        risk={
            "assignment_rate": _rate_from_raw(selected, "assigned", "assignment", "assigned_at_expiry"),
            "strike_breach_rate": _rate_from_raw(selected, "strike_breached", "breached_strike"),
            "worst_trade_pnl": worst_trade_pnl,
            "tail_loss_scenario": tail_loss_scenario,
            "max_drawdown_proxy": _max_drawdown_proxy(selected),
            "concentration_by_symbol": _concentration(row.symbol for row in selected),
            "concentration_by_expiry": _concentration(row.expiry for row in selected),
        },
        execution={
            "candidate_count": len(universe),
            "selected_count": selected_count,
            "reject_reason_distribution": _reject_reason_distribution(universe),
            "avg_holding_days": avg_holding_days,
            "turnover": selected_count,
        },
        decision={
            "sample_size": selected_count,
            "confidence_level": "insufficient" if selected_count < min_sample else "low",
            "overfit_warning": selected_count < min_sample,
        },
        warnings=tuple(warnings),
    )


def _compare(baseline: MetricSet, candidate: MetricSet) -> dict[str, Any]:
    baseline_cash = _number(baseline.returns.get("net_cash_inflow"))
    candidate_cash = _number(candidate.returns.get("net_cash_inflow"))
    baseline_pnl = _number(baseline.returns.get("realized_pnl"))
    candidate_pnl = _number(candidate.returns.get("realized_pnl"))
    baseline_eff = _number(baseline.capital.get("return_per_locked_cash_day"))
    candidate_eff = _number(candidate.capital.get("return_per_locked_cash_day"))
    baseline_tail = _number(baseline.risk.get("tail_loss_scenario"))
    candidate_tail = _number(candidate.risk.get("tail_loss_scenario"))
    return {
        "net_cash_inflow_lift": _diff(candidate_cash, baseline_cash),
        "realized_pnl_lift": _diff(candidate_pnl, baseline_pnl),
        "return_per_locked_cash_day_lift": _diff(candidate_eff, baseline_eff),
        "selected_count_lift": int(candidate.execution.get("selected_count") or 0) - int(baseline.execution.get("selected_count") or 0),
        "tail_loss_scenario_change": _diff(candidate_tail, baseline_tail),
        "risk_worsening": bool(candidate_tail is not None and baseline_tail is not None and candidate_tail < baseline_tail),
    }


def _conclusion(comparison: dict[str, Any], candidate: MetricSet) -> str:
    if int(candidate.execution.get("selected_count") or 0) <= 0:
        return "reject"
    if bool(candidate.decision.get("overfit_warning")):
        return "watch"
    lift = (
        _number(comparison.get("return_per_locked_cash_day_lift"))
        or _number(comparison.get("realized_pnl_lift"))
        or _number(comparison.get("net_cash_inflow_lift"))
    )
    if lift is not None and lift > 0 and not comparison.get("risk_worsening"):
        return "shadow"
    if lift is not None and lift > 0:
        return "watch"
    return "reject"


def _metric_warnings(prefix: str, metrics: MetricSet) -> tuple[str, ...]:
    return tuple(f"{prefix}_{item}" for item in metrics.warnings)


def _premium_value(row: CandidateSnapshot) -> float | None:
    if row.premium is None:
        return None
    return row.premium * (_contracts(row) or 1) * (_multiplier(row) or 100.0)


def _realized_pnl_value(row: CandidateSnapshot) -> float | None:
    return _as_float(_first_raw(row, "realized_pnl", "realized_if_close", "pnl_if_close", "actual_pnl", "actual_return_value"))


def _tail_loss_value(row: CandidateSnapshot) -> float | None:
    return _as_float(_first_raw(row, "tail_loss_scenario", "tail_loss", "worst_trade_pnl", "worst_case_pnl"))


def _locked_cash(row: CandidateSnapshot, *, strategy_type: str) -> float | None:
    if row.locked_cash is not None:
        return row.locked_cash
    explicit = _as_float(_first_raw(row, "capital_basis", "capital_at_risk", "covered_value", "shares_market_value", "underlying_value"))
    if explicit is not None:
        return explicit
    if strategy_type != "sell_put":
        return None
    if row.strike is None:
        return None
    return row.strike * (_contracts(row) or 1) * (_multiplier(row) or 100.0)


def _contracts(row: CandidateSnapshot) -> int | None:
    return row.contracts if row.contracts is not None and row.contracts > 0 else 1


def _multiplier(row: CandidateSnapshot) -> float | None:
    return row.multiplier if row.multiplier is not None and row.multiplier > 0 else 100.0


def _rate_from_raw(rows: list[CandidateSnapshot], *keys: str) -> float | None:
    observed: list[bool] = []
    for row in rows:
        value = _first_raw(row, *keys)
        parsed = _as_bool(value)
        if parsed is not None:
            observed.append(parsed)
    if not observed:
        return None
    return sum(1 for item in observed if item) / len(observed)


def _max_drawdown_proxy(rows: list[CandidateSnapshot]) -> float | None:
    values = [_as_float(_first_raw(row, "max_drawdown_proxy", "max_drawdown", "mdd")) for row in rows]
    known = [value for value in values if value is not None]
    return min(known) if known else None


def _first_raw(row: CandidateSnapshot, *keys: str) -> Any:
    lower = {str(key).strip().lower(): value for key, value in row.raw.items()}
    for key in keys:
        normalized = str(key).strip().lower()
        if normalized in lower:
            return lower[normalized]
    return None


def _reject_reason_distribution(rows: list[CandidateSnapshot]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for row in rows:
        for reason in row.reject_reasons:
            counter[reason] += 1
    return dict(counter.most_common())


def _concentration(values: Any) -> dict[str, Any]:
    items = [str(value) for value in values if value]
    if not items:
        return {"max_key": None, "max_ratio": None, "counts": {}}
    counter = Counter(items)
    key, count = counter.most_common(1)[0]
    return {"max_key": key, "max_ratio": count / len(items), "counts": dict(counter.most_common())}


def _symbol_set(value: Any) -> set[str]:
    return {str(item).strip().upper() for item in _list(value) if str(item).strip()}


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    raw = str(value).strip()
    if not raw:
        return []
    return [part.strip() for part in raw.replace("|", ",").split(",") if part.strip()]


def _between(value: float | int | None, *, low: float | int | None, high: float | int | None) -> bool:
    if low is None and high is None:
        return True
    if value is None:
        return False
    if low is not None and value < low:
        return False
    if high is not None and value > high:
        return False
    return True


def _sum_known(values: list[float | None]) -> float | None:
    known = [value for value in values if value is not None]
    if not known:
        return None
    return sum(known)


def _avg(values: list[float | int]) -> float | None:
    return sum(float(value) for value in values) / len(values) if values else None


def _diff(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return left - right


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    raw = str(value).strip().replace(",", "")
    if not raw:
        return None
    if raw.endswith("%"):
        raw = raw[:-1].strip()
        try:
            return float(raw) / 100.0
        except Exception:
            return None
    try:
        return float(raw)
    except Exception:
        return None


def _as_int(value: Any) -> int | None:
    parsed = _as_float(value)
    if parsed is None:
        return None
    return int(parsed)


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    raw = str(value).strip().lower()
    if raw in {"1", "true", "yes", "y", "on", "assigned", "breached"}:
        return True
    if raw in {"0", "false", "no", "n", "off", "none", "null"}:
        return False
    return None
