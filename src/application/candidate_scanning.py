from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, cast

import pandas as pd

from domain.domain.engine import (
    CANDIDATE_REJECT_REASON_RULE_MAP,
    CandidateScoreWeights,
    attach_opening_decision_provenance,
    build_candidate_decision,
    build_replacement_candidate_decision,
    evaluate_candidate_hard_constraints,
    evaluate_candidate_invariants,
    evaluate_candidate_non_resource_hard_constraints,
    evaluate_candidate_return_floor,
    evaluate_candidate_risk_filter,
    rank_candidate_rows,
)
from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.engine import (
    empty_reject_log_dataframe,
)
from domain.domain.candidate_defaults import normalize_event_risk_mode
from domain.domain.insurance_underwriting import INSURANCE_UNDERWRITING_PROFILE
from src.application.candidate_models import CandidateBaseValues, CandidateContractInput
from src.application.earnings_calendar import annotate_candidates_with_earnings_evidence
from src.application.candidate_filter_trace import (
    append_candidate_filter_trace_rows,
    build_candidate_filter_trace_row,
    build_candidate_filter_trace_rows_from_decision,
    build_candidate_replay_fields,
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
    min_open_interest: float | None
    min_volume: float | None
    max_spread_ratio: float | None
    min_annualized_net_return: float | None
    min_net_income: float
    score_weights: CandidateScoreWeights | None = None
    reject_stage: str = "step3_risk_gate"
    trace_output: Path | None = None
    strategy_family: str | None = None
    strategy_profile: str | None = None
    quiet: bool = False
    risk_policy_version: str | None = None
    quote_snapshot_id: str | None = None


@dataclass(frozen=True)
class CandidateScanDependencies:
    compute_metrics_fn: Callable[[CandidateContractInput], dict[str, Any] | None]
    build_row_fn: Callable[[CandidateContractInput, CandidateBaseValues, dict[str, Any]], dict[str, Any] | None]
    build_hard_constraint_kwargs_fn: Callable[[CandidateContractInput], dict[str, Any]]
    annualized_return_value_fn: Callable[[dict[str, Any]], float | None]
    annotate_event_risk_fn: Callable[[pd.DataFrame, Path, dict[str, Any] | None], pd.DataFrame]
    print_summary_fn: Callable[[pd.DataFrame, Path, Path], None]
    metric_reject_reason_fn: Callable[[CandidateContractInput], dict[str, Any] | None] | None = None
    all_decisions_sink_fn: Callable[[list[dict[str, Any]]], None] | None = None
    event_reject_flag_fn: Callable[[dict[str, Any]], bool] | None = None


CANDIDATE_ALL_DECISIONS_SCHEMA = "candidate_all_decisions.v1"


def resolve_candidate_score_weights(raw: CandidateScoreWeights | dict[str, Any] | None) -> CandidateScoreWeights | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, CandidateScoreWeights):
        return raw
    if not isinstance(raw, dict):
        raise ValueError("score_weights must be an object")

    defaults = CandidateScoreWeights()
    allowed = {
        "annualized_return",
        "net_income",
        "liquidity",
        "risk_distance",
        "vol_edge",
        "delta_target",
        "concentration",
        "path_risk",
    }
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
        vol_edge=_weight("vol_edge", defaults.vol_edge),
        delta_target=_weight("delta_target", defaults.delta_target),
        concentration=_weight("concentration", defaults.concentration),
        path_risk=_weight("path_risk", defaults.path_risk),
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


def _contract_replay_fields(
    contract: CandidateContractInput,
    base_values: CandidateBaseValues | None = None,
    metrics: dict[str, Any] | None = None,
    *,
    annualized_return: Any = None,
) -> dict[str, Any]:
    source = contract.to_gate_payload()
    if base_values is not None:
        source.update(
            {
                "dte": base_values.dte,
                "strike": base_values.strike,
                "open_interest": base_values.open_interest,
                "volume": base_values.volume,
                "spread_ratio": base_values.spread_ratio,
            }
        )
    return build_candidate_replay_fields(source, metrics, annualized_return=annualized_return)


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
        open_interest=contract.open_interest,
        volume=contract.volume,
        spread=spread,
        spread_ratio=spread_ratio,
    )


def _build_non_resource_base_values(
    contract: CandidateContractInput,
    *,
    min_dte: int,
    max_dte: int,
    min_strike: float | None,
    max_strike: float | None,
) -> tuple[dict[str, Any], CandidateBaseValues | None]:
    gate = evaluate_candidate_non_resource_hard_constraints(
        contract.to_gate_payload(),
        mode=contract.mode,
        min_dte=min_dte,
        max_dte=max_dte,
        min_strike=min_strike,
        max_strike=max_strike,
        extra_required_fields=(),
    )
    if not bool(gate.get("accepted")):
        return gate, None
    spread, spread_ratio = _spread_values(contract)
    return gate, CandidateBaseValues(
        dte=int(contract.dte or 0),
        strike=float(contract.strike or 0.0),
        open_interest=contract.open_interest,
        volume=contract.volume,
        spread=spread,
        spread_ratio=spread_ratio,
    )


def _candidate_id(
    *,
    mode: str,
    normalized_input: dict[str, Any],
) -> str:
    return canonical_sha256(
        {
            "schema": "options-monitor.candidate-id.v1",
            "mode": str(mode),
            "symbol": normalized_input.get("symbol"),
            "contract_symbol": normalized_input.get("contract_symbol"),
            "expiration": normalized_input.get("expiration"),
            "strike": normalized_input.get("strike"),
        }
    )


def _build_all_decision_context(
    *,
    contract: CandidateContractInput,
    base_values: CandidateBaseValues,
    metrics: dict[str, Any],
    opening_stage1: dict[str, Any],
    config: CandidateScanConfig,
    deps: CandidateScanDependencies,
    context_index: int,
) -> dict[str, Any]:
    """Build one sidecar context from the opening path's exact computed facts."""

    annualized_return = deps.annualized_return_value_fn(metrics)
    normalized_input = {
        **contract.to_gate_payload(),
        "dte": base_values.dte,
        "strike": base_values.strike,
        "open_interest": base_values.open_interest,
        "volume": base_values.volume,
        "spread": base_values.spread,
        "spread_ratio": base_values.spread_ratio,
        **metrics,
    }
    opening_stage2 = evaluate_candidate_return_floor(
        opening_stage1,
        min_annualized_return=config.min_annualized_net_return,
        min_net_income=config.min_net_income,
        annualized_return=annualized_return,
        net_income=metrics.get("net_income"),
    )
    opening_stage3 = evaluate_candidate_risk_filter(
        opening_stage2,
        min_open_interest=config.min_open_interest,
        min_volume=config.min_volume,
        max_spread_ratio=config.max_spread_ratio,
        open_interest=base_values.open_interest,
        volume=base_values.volume,
        spread_ratio=(
            metrics.get("spread_ratio")
            if metrics.get("spread_ratio") is not None
            else base_values.spread_ratio
        ),
    )
    return {
        "candidate_id": _candidate_id(
            mode=config.mode,
            normalized_input=normalized_input,
        ),
        "normalized_input": normalized_input,
        "annualized_return": annualized_return,
        "opening_pre_event": opening_stage3,
        "event_row": {
            **normalized_input,
            "__all_decisions_index": context_index,
        },
    }


def _finalize_all_decisions(
    *,
    contexts: list[dict[str, Any]],
    config: CandidateScanConfig,
    deps: CandidateScanDependencies,
    event_risk_cfg: dict[str, Any] | None,
    base_dir: Path,
) -> list[dict[str, Any]]:
    if not contexts:
        return []
    policy_version = str(config.risk_policy_version or "").strip()
    quote_snapshot_id = str(config.quote_snapshot_id or "").strip()
    if not policy_version or not quote_snapshot_id:
        raise ValueError(
            "risk_policy_version and quote_snapshot_id are required for all-decisions"
        )
    event_rows = pd.DataFrame([dict(item["event_row"]) for item in contexts])
    annotated = deps.annotate_event_risk_fn(event_rows, base_dir, event_risk_cfg)
    if "__all_decisions_index" not in annotated.columns:
        raise ValueError("event annotator did not preserve all-decisions identity")
    if len(annotated) != len(contexts):
        raise ValueError("event annotator changed all-decisions cardinality")
    annotated_by_index = {
        int(row["__all_decisions_index"]): row
        for row in annotated.to_dict("records")
    }
    if set(annotated_by_index) != set(range(len(contexts))):
        raise ValueError("event annotator changed all-decisions cardinality")

    event_mode = normalize_event_risk_mode((event_risk_cfg or {}).get("mode"))
    decisions: list[dict[str, Any]] = []
    for index, context in enumerate(contexts):
        annotated_row = dict(annotated_by_index[index])
        annotated_row.pop("__all_decisions_index", None)
        event_flag = (
            deps.event_reject_flag_fn(annotated_row)
            if deps.event_reject_flag_fn is not None
            else bool(annotated_row.get("event_flag"))
        )
        normalized_input = dict(context["normalized_input"])
        normalized_input.update(
            {
                key: annotated_row.get(key)
                for key in (
                    "event_flag",
                    "event_types",
                    "event_dates",
                    "event_source_status",
                    "event_source_error",
                    "event_earnings_coverage_status",
                    "event_earnings_coverage_error",
                )
                if key in annotated_row
            }
        )
        opening = evaluate_candidate_risk_filter(
            context["opening_pre_event"],
            event_flag=event_flag,
            event_mode=event_mode,
        )
        invariant = evaluate_candidate_invariants(
            normalized_input,
            mode=config.mode,
            risk_policy_version=policy_version,
            quote_snapshot_id=quote_snapshot_id,
            min_dte=config.min_dte,
            max_dte=config.max_dte,
            min_strike=config.min_strike,
            max_strike=config.max_strike,
            min_annualized_return=config.min_annualized_net_return,
            min_net_income=config.min_net_income,
            annualized_return=context["annualized_return"],
            net_income=normalized_input.get("net_income"),
            min_open_interest=config.min_open_interest,
            min_volume=config.min_volume,
            max_spread_ratio=config.max_spread_ratio,
            event_flag=event_flag,
            event_mode=event_mode,
            open_interest=normalized_input.get("open_interest"),
            volume=normalized_input.get("volume"),
            spread_ratio=normalized_input.get("spread_ratio"),
            extra_required_fields=(),
        )
        opening = attach_opening_decision_provenance(
            opening,
            risk_policy_version=policy_version,
            risk_policy_hash=str(invariant["risk_policy_hash"]),
            quote_snapshot_id=quote_snapshot_id,
            normalized_input=dict(invariant["normalized_input"]),
        )
        replacement = build_replacement_candidate_decision(
            candidate_id=str(context["candidate_id"]),
            opening_decision=opening,
            invariant_decision=invariant,
        )
        decisions.append(
            {
                "schema_version": CANDIDATE_ALL_DECISIONS_SCHEMA,
                "candidate_id": context["candidate_id"],
                "strategy_mode": config.mode,
                "normalized_input": invariant["normalized_input"],
                "normalized_input_hash": invariant["normalized_input_hash"],
                "risk_policy_version": policy_version,
                "risk_policy_hash": invariant["risk_policy_hash"],
                "quote_snapshot_id": quote_snapshot_id,
                "opening_decision": opening,
                "invariant_decision": invariant,
                "replacement_candidate_decision": replacement,
            }
        )
    return sorted(
        decisions,
        key=lambda item: (
            str(item["strategy_mode"]),
            str(item["candidate_id"]),
        ),
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
    all_decision_contexts: list[dict[str, Any]] = []
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
                    strategy_family=config.strategy_family,
                    strategy_profile=config.strategy_profile,
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
            metrics: dict[str, Any] | None = None
            metrics_computed = False
            if deps.all_decisions_sink_fn is not None:
                _non_resource_stage1, non_resource_base_values = (
                    _build_non_resource_base_values(
                        contract,
                        min_dte=config.min_dte,
                        max_dte=config.max_dte,
                        min_strike=config.min_strike,
                        max_strike=config.max_strike,
                    )
                )
                # The all-decisions universe begins only after basic
                # normalization and DTE/strike constraints, before capacity.
                if non_resource_base_values is not None:
                    metrics = deps.compute_metrics_fn(contract)
                    metrics_computed = True
                    if metrics:
                        all_decision_contexts.append(
                            _build_all_decision_context(
                                contract=contract,
                                base_values=non_resource_base_values,
                                metrics=metrics,
                                opening_stage1=stage1,
                                config=config,
                                deps=deps,
                                context_index=len(all_decision_contexts),
                            )
                        )
            if base_values is None:
                replay_fields = _contract_replay_fields(contract)
                reject_rows.extend(
                    _decision_reject_log_rows(
                        decision=stage1,
                        reject_stage=config.reject_stage,
                        replay_fields=replay_fields,
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
                        replay_fields=replay_fields,
                    )
                )
                continue
            if not metrics_computed:
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
                        strategy_family=config.strategy_family,
                        strategy_profile=config.strategy_profile,
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
                        replay_fields=_contract_replay_fields(contract, base_values),
                    )
                )
                continue
            annualized_return = deps.annualized_return_value_fn(metrics)
            stage2 = evaluate_candidate_return_floor(
                stage1,
                min_annualized_return=config.min_annualized_net_return,
                min_net_income=config.min_net_income,
                annualized_return=annualized_return,
                net_income=metrics.get("net_income"),
            )
            stage3 = evaluate_candidate_risk_filter(
                stage2,
                min_open_interest=config.min_open_interest,
                min_volume=config.min_volume,
                max_spread_ratio=config.max_spread_ratio,
                open_interest=base_values.open_interest,
                volume=base_values.volume,
                spread_ratio=(
                    metrics.get("spread_ratio")
                    if metrics.get("spread_ratio") is not None
                    else base_values.spread_ratio
                ),
            )
            replay_fields = _contract_replay_fields(
                contract,
                base_values,
                metrics,
                annualized_return=annualized_return,
            )
            reject_rows.extend(
                _decision_reject_log_rows(
                    decision=stage3,
                    reject_stage=config.reject_stage,
                    replay_fields=replay_fields,
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
                    replay_fields=replay_fields,
                )
            )
            if not bool(stage3.get("accepted")):
                continue
            candidate = deps.build_row_fn(contract, base_values, metrics)
            if candidate:
                rows.append(candidate)

    if deps.all_decisions_sink_fn is not None:
        deps.all_decisions_sink_fn(
            _finalize_all_decisions(
                contexts=all_decision_contexts,
                config=config,
                deps=deps,
                event_risk_cfg=event_risk_cfg,
                base_dir=base_dir,
            )
        )

    out = pd.DataFrame(rows)
    if not out.empty:
        out = annotate_candidates_with_earnings_evidence(
            out,
            input_root=config.input_root,
        )
        formal_opening = (
            str(config.strategy_profile or "").strip().lower()
            == INSURANCE_UNDERWRITING_PROFILE
        )
        if not formal_opening:
            out = deps.annotate_event_risk_fn(out, base_dir, event_risk_cfg)
        event_mode = normalize_event_risk_mode((event_risk_cfg or {}).get("mode"))
        if not formal_opening and event_mode == "reject":
            kept_rows: list[dict[str, Any]] = []
            out_columns = list(out.columns)
            for candidate in out.to_dict("records"):
                event_reject_flag = (
                    deps.event_reject_flag_fn(candidate)
                    if deps.event_reject_flag_fn is not None
                    else bool(candidate.get("event_flag"))
                )
                if not event_reject_flag:
                    kept_rows.append(candidate)
                    continue
                event_decision = evaluate_candidate_risk_filter(
                    build_candidate_decision(
                        mode=config.mode,
                        symbol=str(candidate.get("symbol") or ""),
                        contract_symbol=str(candidate.get("contract_symbol") or ""),
                        accepted=True,
                        normalized_input=candidate,
                    ),
                    event_flag=True,
                    event_mode=event_mode,
                )
                annualized_return = deps.annualized_return_value_fn(candidate)
                replay_fields = build_candidate_replay_fields(
                    candidate,
                    annualized_return=annualized_return,
                )
                reject_rows.extend(
                    _decision_reject_log_rows(
                        decision=event_decision,
                        reject_stage=config.reject_stage,
                        replay_fields=replay_fields,
                    )
                )
                trace_rows.extend(
                    build_candidate_filter_trace_rows_from_decision(
                        decision=event_decision,
                        function=trace_function,
                        status="rejected",
                        reject_stage=config.reject_stage,
                        evidence_path=reject_out_path.name,
                        config_values=config_values,
                        output_path=out_path,
                        replay_fields=replay_fields,
                    )
                )
            out = pd.DataFrame(kept_rows, columns=out_columns)

    should_rank_here = (
        str(config.strategy_profile or "").strip().lower()
        != INSURANCE_UNDERWRITING_PROFILE
    )
    if not out.empty and should_rank_here:
        ranked_rows = rank_candidate_rows(
            out.to_dict("records"),
            mode=config.mode,
            score_weights=config.score_weights,
        )
        out = pd.DataFrame(ranked_rows)
        if "_strategy_score" in out.columns:
            out = out.drop(columns=["_strategy_score"])
        for candidate in out.to_dict("records"):
            annualized_return = deps.annualized_return_value_fn(candidate)
            trace_rows.append(
                build_candidate_filter_trace_row(
                    run_id=trace_scope.get("run_id"),
                    account=trace_scope.get("account"),
                    symbol=candidate.get("symbol"),
                    function=trace_function,
                    mode=config.mode,
                    strategy_family=config.strategy_family,
                    strategy_profile=config.strategy_profile,
                    status="accepted",
                    stage="stage4_ranking",
                    rule="candidate_accepted",
                    metric_value=annualized_return,
                    threshold=config.min_annualized_net_return,
                    contract_symbol=candidate.get("contract_symbol"),
                    expiration=candidate.get("expiration"),
                    strike=candidate.get("strike"),
                    message="candidate passed scan filters",
                    evidence_path=out_path.name,
                    config_values=config_values,
                    replay_fields=build_candidate_replay_fields(
                        candidate,
                        annualized_return=annualized_return,
                    ),
                )
            )

    reject_log = pd.DataFrame(reject_rows)

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
        "strategy_family": config.strategy_family,
        "strategy_profile": config.strategy_profile,
    }


def _decision_reject_log_rows(
    *,
    decision: dict[str, Any],
    reject_stage: str,
    replay_fields: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    normalized = dict(decision.get("normalized_input") or {})
    replay_payload = build_candidate_replay_fields(normalized, replay_fields)
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
                **replay_payload,
                "engine_reject_stage": reject.get("stage"),
                "engine_reject_reason": reason,
            }
        )
    return rows
