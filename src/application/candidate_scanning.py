from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pandas as pd

from domain.domain.engine import (
    evaluate_candidate_hard_constraints,
    evaluate_candidate_return_floor,
    evaluate_candidate_risk_filter,
    rank_candidate_rows,
)
from domain.domain.engine import (
    empty_reject_log_dataframe,
)
from domain.domain.engine.candidate_engine import (
    REJECT_RETURN_ANNUALIZED,
    REJECT_RETURN_NET_INCOME,
    REJECT_RISK_SPREAD,
)
from src.application.candidate_models import CandidateBaseValues, CandidateContractInput


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
    reject_stage: str = "step3_risk_gate"
    quiet: bool = False


@dataclass(frozen=True)
class CandidateScanDependencies:
    compute_metrics_fn: Callable[[CandidateContractInput], dict | None]
    build_row_fn: Callable[[CandidateContractInput, CandidateBaseValues, dict], dict | None]
    build_hard_constraint_kwargs_fn: Callable[[CandidateContractInput], dict]
    annualized_return_value_fn: Callable[[dict], float | None]
    event_risk_flag_fn: Callable[[dict], bool]
    event_risk_mode_fn: Callable[[dict | None], str]
    annotate_event_risk_fn: Callable[[pd.DataFrame, Path, dict | None], pd.DataFrame]
    print_summary_fn: Callable[[pd.DataFrame, Path, Path], None]


def _load_required_data_rows(*, input_root: Path, symbol: str, mode: str) -> pd.DataFrame:
    path = Path(input_root) / "parsed" / f"{symbol}_required_data.csv"
    try:
        df = pd.read_csv(path)
    except pd.errors.EmptyDataError:
        df = pd.DataFrame()
    if df.empty or ("option_type" not in df.columns):
        return pd.DataFrame()
    return df[df["option_type"] == mode].copy()


def _spread_values(contract: CandidateContractInput) -> tuple[float | None, float | None]:
    bid_value = contract.bid
    ask_value = contract.ask
    mid_value = contract.mid
    if bid_value is None or ask_value is None or ask_value < bid_value:
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
    extra_hard_kwargs: dict,
) -> tuple[dict[str, object], CandidateBaseValues | None]:
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
    event_risk_cfg: dict | None,
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

    rows: list[dict] = []
    reject_rows: list[dict] = []
    for symbol in config.symbols:
        df = _load_required_data_rows(input_root=config.input_root, symbol=symbol, mode=config.mode)
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
                continue
            metrics = deps.compute_metrics_fn(contract)
            if not metrics:
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
                event_flag=False,
                event_mode=deps.event_risk_mode_fn(event_risk_cfg),
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
            if not bool(stage3.get("accepted")):
                continue
            candidate = deps.build_row_fn(contract, base_values, metrics)
            if candidate:
                rows.append(candidate)

    out = pd.DataFrame(rows)
    reject_log = pd.DataFrame(reject_rows)
    if not out.empty:
        ranked_rows = rank_candidate_rows(out.to_dict("records"), mode=config.mode)
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

    if not config.quiet:
        deps.print_summary_fn(out, out_path, reject_out_path)

    return out


def _decision_reject_log_rows(*, decision: dict, reject_stage: str) -> list[dict]:
    rows: list[dict] = []
    normalized = dict(decision.get("normalized_input") or {})
    for reject in list(decision.get("rejects") or []):
        reason = str(reject.get("reason") or "")
        if reason == REJECT_RETURN_ANNUALIZED:
            rule = "min_annualized_return"
        elif reason == REJECT_RETURN_NET_INCOME:
            rule = "min_net_income"
        elif reason == REJECT_RISK_SPREAD:
            rule = "max_spread_ratio"
        else:
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
