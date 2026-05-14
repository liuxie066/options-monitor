from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, cast

import pandas as pd

from domain.domain.engine import (
    CANDIDATE_REJECT_REASON_RULE_MAP,
    CandidateScoreWeights,
    evaluate_candidate_hard_constraints,
    evaluate_candidate_return_floor,
    evaluate_candidate_risk_filter,
    rank_candidate_rows,
)
from domain.domain.engine import (
    empty_reject_log_dataframe,
)
from src.application.candidate_models import CandidateBaseValues, CandidateContractInput
from src.application.candidate_filter_trace import (
    append_candidate_filter_trace_rows,
    build_candidate_filter_trace_row,
    build_candidate_filter_trace_rows_from_decision,
    candidate_trace_path_for_output,
    infer_trace_scope_from_path,
    trace_function_for_mode,
)


@dataclass(frozen=True)
class CandidateScanConfig:
    mode: str
    symbols: list[str]
    input_root: Path
    output: Path
    empty_output_columns: list[str]
    min_dte: int
    max_dte: int
    min_strike: float | None
    max_strike: float | None
    min_open_interest: float
    min_volume: float
    max_spread_ratio: float | None
    min_annualized_net_return: float | None
    min_net_income: float
    score_weights: CandidateScoreWeights | None = None
    reject_stage: str = "step3_risk_gate"
    trace_output: Path | None = None
    quiet: bool = False


@dataclass(frozen=True)
class CandidateScanDependencies:
    compute_metrics_fn: Callable[[CandidateContractInput], dict[str, Any] | None]
    build_row_fn: Callable[[CandidateContractInput, CandidateBaseValues, dict[str, Any]], dict[str, Any] | None]
    build_hard_constraint_kwargs_fn: Callable[[CandidateContractInput], dict[str, Any]]
    annualized_return_value_fn: Callable[[dict[str, Any]], float | None]
    annotate_event_risk_fn: Callable[[pd.DataFrame, Path, dict[str, Any] | None], pd.DataFrame]
    print_summary_fn: Callable[[pd.DataFrame, Path, Path], None]
    metric_reject_reason_fn: Callable[[CandidateContractInput], dict[str, Any] | None] | None = None


def resolve_candidate_score_weights(raw: CandidateScoreWeights | dict[str, Any] | None) -> CandidateScoreWeights | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, CandidateScoreWeights):
        return raw
    if not isinstance(raw, dict):
        raise ValueError("score_weights must be an object")

    defaults = CandidateScoreWeights()
    allowed = {"annualized_return", "net_income", "liquidity", "risk_distance"}
    unsupported = [str(key) for key in raw.keys() if key not in allowed]
    if unsupported:
        raise ValueError(f"score_weights has unsupported keys: {', '.join(unsupported)}")

    def _weight(name: str, default: float) -> float:
        value = raw.get(name, default)
        try:
            parsed = float(value)
        except Exception as exc:
            raise ValueError(f"score_weights.{name} must be numeric") from exc
        if parsed < 0:
            raise ValueError(f"score_weights.{name} must be >= 0")
        return parsed

    return CandidateScoreWeights(
        annualized_return=_weight("annualized_return", defaults.annualized_return),
        net_income=_weight("net_income", defaults.net_income),
        liquidity=_weight("liquidity", defaults.liquidity),
        risk_distance=_weight("risk_distance", defaults.risk_distance),
    )


def _load_required_data_rows(*, input_root: Path, symbol: str, mode: str) -> pd.DataFrame:
    path = Path(input_root) / "parsed" / f"{symbol}_required_data.csv"
    try:
        df = pd.read_csv(path)
    except FileNotFoundError:
        df = pd.DataFrame()
    except pd.errors.EmptyDataError:
        df = pd.DataFrame()
    if df.empty or ("option_type" not in df.columns):
        return pd.DataFrame()
    return cast(pd.DataFrame, df.loc[df["option_type"] == mode].copy())


def _spread_values(contract: CandidateContractInput) -> tuple[float | None, float | None]:
    bid_value = contract.bid
    ask_value = contract.ask
    mid_value = contract.mid
    if bid_value is None or ask_value is None or bid_value <= 0 or ask_value <= 0 or ask_value < bid_value:
        return None, None
    spread = ask_value - bid_value
    if mid_value is None or mid_value <= 0:
        return spread, None
    return spread, spread / mid_value


def _build_base_values(
    contract: CandidateContractInput,
    *,
    min_dte: int,
    max_dte: int,
    min_strike: float | None,
    max_strike: float | None,
    extra_hard_kwargs: dict[str, Any],
) -> tuple[dict[str, Any], CandidateBaseValues | None]:
    gate = evaluate_candidate_hard_constraints(
        contract.to_gate_payload(),
        mode=contract.mode,
        min_dte=min_dte,
        max_dte=max_dte,
        min_strike=min_strike,
        max_strike=max_strike,
        extra_required_fields=(),
        **(extra_hard_kwargs or {}),
    )
    if not bool(gate.get("accepted")):
        return gate, None

    spread, spread_ratio = _spread_values(contract)
    return gate, CandidateBaseValues(
        dte=int(contract.dte or 0),
        strike=float(contract.strike or 0.0),
        open_interest=float(contract.open_interest or 0.0),
        volume=float(contract.volume or 0.0),
        spread=spread,
        spread_ratio=spread_ratio,
    )


def run_candidate_scan(
    *,
    config: CandidateScanConfig,
    deps: CandidateScanDependencies,
    event_risk_cfg: dict[str, Any] | None,
    base_dir: Path,
    reject_log_output: Path | None = None,
) -> pd.DataFrame:
    out_path = Path(config.output).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    reject_out_path = (
        Path(reject_log_output).resolve()
        if reject_log_output is not None
        else out_path.with_name(f"{out_path.stem}_reject_log.csv")
    )
    reject_out_path.parent.mkdir(parents=True, exist_ok=True)
    trace_out_path = Path(config.trace_output).resolve() if config.trace_output is not None else candidate_trace_path_for_output(out_path)
    trace_rows: list[dict[str, Any]] = []
    trace_function = trace_function_for_mode(config.mode)
    trace_scope = infer_trace_scope_from_path(out_path)
    config_values = _trace_config_values(config)

    rows: list[dict[str, Any]] = []
    reject_rows: list[dict[str, Any]] = []
    for symbol in config.symbols:
        df = _load_required_data_rows(input_root=config.input_root, symbol=symbol, mode=config.mode)
        if df.empty:
            trace_rows.append(
                build_candidate_filter_trace_row(
                    run_id=trace_scope.get("run_id"),
                    account=trace_scope.get("account"),
                    symbol=symbol,
                    function=trace_function,
                    mode=config.mode,
                    status="rejected",
                    stage="fetch_visibility",
                    rule=f"required_data_missing_{config.mode}_chain",
                    message=f"required_data has no {config.mode} option rows for symbol",
                    evidence_path=f"{symbol}_required_data.csv",
                    config_values=config_values,
                )
            )
            continue
        for _, row in df.iterrows():
            contract = CandidateContractInput.from_row(row, mode=config.mode)
            hard_kwargs = deps.build_hard_constraint_kwargs_fn(contract)
            stage1, base_values = _build_base_values(
                contract,
                min_dte=config.min_dte,
                max_dte=config.max_dte,
                min_strike=config.min_strike,
                max_strike=config.max_strike,
                extra_hard_kwargs=hard_kwargs,
            )
            if base_values is None:
                reject_rows.extend(
                    _decision_reject_log_rows(
                        decision=stage1,
                        reject_stage=config.reject_stage,
                    )
                )
                trace_rows.extend(
                    build_candidate_filter_trace_rows_from_decision(
                        decision=stage1,
                        function=trace_function,
                        status="rejected",
                        reject_stage=config.reject_stage,
                        evidence_path=reject_out_path.name,
                        config_values=config_values,
                        output_path=out_path,
                    )
                )
                continue
            metrics = deps.compute_metrics_fn(contract)
            if not metrics:
                reason: dict[str, Any] = {}
                if deps.metric_reject_reason_fn is not None:
                    try:
                        reason = deps.metric_reject_reason_fn(contract) or {}
                    except Exception:
                        reason = {}
                trace_rows.append(
                    build_candidate_filter_trace_row(
                        run_id=trace_scope.get("run_id"),
                        account=trace_scope.get("account"),
                        symbol=contract.symbol,
                        function=trace_function,
                        mode=config.mode,
                        status="rejected",
                        stage="metrics",
                        rule=str(reason.get("rule") or "candidate_metrics_unavailable"),
                        metric_value=reason.get("metric_value"),
                        threshold=reason.get("threshold"),
                        contract_symbol=contract.contract_symbol,
                        expiration=contract.expiration,
                        strike=contract.strike,
                        message=str(reason.get("message") or "candidate metrics unavailable"),
                        evidence_path=out_path.name,
                        config_values=config_values,
                    )
                )
                continue
            stage2 = evaluate_candidate_return_floor(
                stage1,
                min_annualized_return=config.min_annualized_net_return,
                min_net_income=config.min_net_income,
                annualized_return=deps.annualized_return_value_fn(metrics),
                net_income=metrics.get("net_income"),
            )
            stage3 = evaluate_candidate_risk_filter(
                stage2,
                min_open_interest=config.min_open_interest,
                min_volume=config.min_volume,
                max_spread_ratio=config.max_spread_ratio,
                open_interest=base_values.open_interest,
                volume=base_values.volume,
                spread_ratio=base_values.spread_ratio,
            )
            reject_rows.extend(
                _decision_reject_log_rows(
                    decision=stage3,
                    reject_stage=config.reject_stage,
                )
            )
            trace_rows.extend(
                build_candidate_filter_trace_rows_from_decision(
                    decision=stage3,
                    function=trace_function,
                    status="rejected",
                    reject_stage=config.reject_stage,
                    evidence_path=reject_out_path.name,
                    config_values=config_values,
                    output_path=out_path,
                )
            )
            if not bool(stage3.get("accepted")):
                continue
            candidate = deps.build_row_fn(contract, base_values, metrics)
            if candidate:
                rows.append(candidate)
                trace_rows.append(
                    build_candidate_filter_trace_row(
                        run_id=trace_scope.get("run_id"),
                        account=trace_scope.get("account"),
                        symbol=candidate.get("symbol") or contract.symbol,
                        function=trace_function,
                        mode=config.mode,
                        status="accepted",
                        stage="stage4_ranking",
                        rule="candidate_accepted",
                        metric_value=deps.annualized_return_value_fn(metrics),
                        threshold=config.min_annualized_net_return,
                        contract_symbol=candidate.get("contract_symbol") or contract.contract_symbol,
                        expiration=candidate.get("expiration") or contract.expiration,
                        strike=candidate.get("strike") or contract.strike,
                        message="candidate passed scan filters",
                        evidence_path=out_path.name,
                        config_values=config_values,
                    )
                )

    out = pd.DataFrame(rows)
    reject_log = pd.DataFrame(reject_rows)
    if not out.empty:
        ranked_rows = rank_candidate_rows(
            out.to_dict("records"),
            mode=config.mode,
            score_weights=config.score_weights,
        )
        out = pd.DataFrame(ranked_rows)
        out = deps.annotate_event_risk_fn(out, base_dir, event_risk_cfg)
        if "_strategy_score" in out.columns:
            out = out.drop(columns=["_strategy_score"])

    if out.empty:
        pd.DataFrame(columns=config.empty_output_columns).to_csv(out_path, index=False)
    else:
        out.to_csv(out_path, index=False)

    if reject_log.empty:
        empty_reject_log_dataframe().to_csv(reject_out_path, index=False)
    else:
        reject_log.to_csv(reject_out_path, index=False)

    append_candidate_filter_trace_rows(trace_out_path, trace_rows)

    if not config.quiet:
        deps.print_summary_fn(out, out_path, reject_out_path)

    return out


def _trace_config_values(config: CandidateScanConfig) -> dict[str, object]:
    return {
        "min_dte": config.min_dte,
        "max_dte": config.max_dte,
        "min_strike": config.min_strike,
        "max_strike": config.max_strike,
        "min_open_interest": config.min_open_interest,
        "min_volume": config.min_volume,
        "max_spread_ratio": config.max_spread_ratio,
        "min_annualized_net_return": config.min_annualized_net_return,
        "min_net_income": config.min_net_income,
    }


def _decision_reject_log_rows(*, decision: dict[str, Any], reject_stage: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    normalized = dict(decision.get("normalized_input") or {})
    for reject in list(decision.get("rejects") or []):
        reason = str(reject.get("reason") or "")
        rule = CANDIDATE_REJECT_REASON_RULE_MAP.get(reason)
        if not rule:
            continue
        rows.append(
            {
                "reject_stage": reject_stage,
                "reject_rule": rule,
                "metric_value": reject.get("metric_value"),
                "threshold": reject.get("threshold"),
                "symbol": decision.get("symbol"),
                "contract_symbol": decision.get("contract_symbol"),
                "expiration": normalized.get("expiration"),
                "strike": normalized.get("strike"),
                "mode": decision.get("mode"),
                "engine_reject_stage": reject.get("stage"),
                "engine_reject_reason": reason,
            }
        )
    return rows
