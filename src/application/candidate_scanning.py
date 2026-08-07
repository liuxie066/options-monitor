from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, cast

import pandas as pd

from domain.domain.engine import (
    STAGE_INPUT_NORMALIZATION,
    build_candidate_decision,
)
from domain.domain.engine.candidate_engine import REJECT_INPUT_INVALID
from src.application.candidate_models import CandidateBaseValues, CandidateContractInput
from src.application.earnings_calendar import annotate_candidates_with_earnings_evidence


_REJECT_LOG_COLUMNS = (
    "reject_stage",
    "reject_rule",
    "metric_value",
    "threshold",
    "symbol",
    "contract_symbol",
    "expiration",
    "strike",
    "mode",
    "message",
)


@dataclass(frozen=True)
class CandidateScanConfig:
    """Application inputs for building a calculable opening-candidate universe.

    Strategy thresholds remain in this compatibility-shaped object because the
    human scan CLI still accepts them.  The scanner deliberately does not apply
    them: the sole formal gate and ranking live in Candidate Engine and are
    invoked after earnings, currency and account-capacity facts are attached.
    """

    mode: str
    symbols: list[str]
    input_root: Path
    output: Path | None
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
    reject_stage: str = "candidate_calculation"
    strategy_family: str | None = None
    strategy_profile: str | None = None
    quiet: bool = False


@dataclass(frozen=True)
class CandidateScanDependencies:
    compute_metrics_fn: Callable[[CandidateContractInput], dict[str, Any] | None]
    build_row_fn: Callable[
        [CandidateContractInput, CandidateBaseValues, dict[str, Any]],
        dict[str, Any] | None,
    ]
    print_summary_fn: Callable[[pd.DataFrame, Path, Path], None]
    metric_reject_reason_fn: Callable[[CandidateContractInput], dict[str, Any] | None] | None = None


def _load_required_data_rows(*, input_root: Path, symbol: str, mode: str) -> pd.DataFrame:
    path = Path(input_root) / "parsed" / f"{symbol}_required_data.csv"
    try:
        df = pd.read_csv(path)
    except (FileNotFoundError, pd.errors.EmptyDataError):
        return pd.DataFrame()
    if df.empty or "option_type" not in df.columns:
        return pd.DataFrame()
    return cast(pd.DataFrame, df.loc[df["option_type"] == mode].copy())


def _base_values(
    contract: CandidateContractInput,
    metrics: dict[str, Any],
) -> CandidateBaseValues | None:
    if contract.dte is None or contract.strike is None:
        return None
    return CandidateBaseValues(
        dte=int(contract.dte),
        strike=float(contract.strike),
        open_interest=contract.open_interest,
        volume=contract.volume,
        spread=_optional_float(metrics.get("spread")),
        spread_ratio=_optional_float(metrics.get("spread_ratio")),
    )


def _optional_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


def _calculation_reject_row(
    *,
    contract: CandidateContractInput,
    config: CandidateScanConfig,
    reason: dict[str, Any] | None,
) -> dict[str, Any]:
    detail = dict(reason or {})
    return {
        "reject_stage": config.reject_stage,
        "reject_rule": str(detail.get("rule") or "candidate_metrics_unavailable"),
        "metric_value": detail.get("metric_value"),
        "threshold": detail.get("threshold"),
        "symbol": contract.symbol,
        "contract_symbol": contract.contract_symbol,
        "expiration": contract.expiration,
        "strike": contract.strike,
        "mode": config.mode,
        "message": str(detail.get("message") or "candidate metrics unavailable"),
    }


def _calculation_decision_record(
    *,
    contract: CandidateContractInput,
    config: CandidateScanConfig,
    reason: dict[str, Any] | None,
) -> dict[str, Any]:
    detail = dict(reason or {})
    specific_reason = str(
        detail.get("rule") or "candidate_metrics_unavailable"
    )
    normalized_input = contract.to_gate_payload()
    opening_decision = build_candidate_decision(
        mode=config.mode,
        symbol=contract.symbol,
        contract_symbol=contract.contract_symbol,
        accepted=False,
        rejects=[
            {
                "stage": STAGE_INPUT_NORMALIZATION,
                "reason": REJECT_INPUT_INVALID,
                "message": str(
                    detail.get("message") or "candidate metrics unavailable"
                ),
                "metric_value": {
                    "reason_code": specific_reason,
                    "metric_value": detail.get("metric_value"),
                },
                "threshold": detail.get("threshold"),
            }
        ],
        normalized_input=normalized_input,
    )
    return {
        "normalized_input": normalized_input,
        "opening_decision": opening_decision,
    }


def run_candidate_scan(
    *,
    config: CandidateScanConfig,
    deps: CandidateScanDependencies,
    reject_log_output: Path | None = None,
    calculation_decision_sink_fn: (
        Callable[[list[dict[str, Any]]], None] | None
    ) = None,
) -> pd.DataFrame:
    """Build normalized, calculable rows; do not filter or rank strategy policy."""

    out_path = Path(config.output).resolve() if config.output is not None else None
    reject_out_path = (
        Path(reject_log_output).resolve()
        if reject_log_output is not None
        else (
            out_path.with_name(f"{out_path.stem}_reject_log.csv")
            if out_path is not None
            else None
        )
    )
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    if reject_out_path is not None:
        reject_out_path.parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    reject_rows: list[dict[str, Any]] = []
    calculation_decisions: list[dict[str, Any]] = []
    for symbol in config.symbols:
        data = _load_required_data_rows(
            input_root=config.input_root,
            symbol=symbol,
            mode=config.mode,
        )
        for _, raw_row in data.iterrows():
            contract = CandidateContractInput.from_row(raw_row, mode=config.mode)
            metrics = deps.compute_metrics_fn(contract)
            base_values = _base_values(contract, metrics or {})
            if not metrics or base_values is None:
                detail: dict[str, Any] | None = None
                if deps.metric_reject_reason_fn is not None:
                    try:
                        detail = deps.metric_reject_reason_fn(contract)
                    except Exception:
                        detail = None
                reject_rows.append(
                    _calculation_reject_row(
                        contract=contract,
                        config=config,
                        reason=detail,
                    )
                )
                calculation_decisions.append(
                    _calculation_decision_record(
                        contract=contract,
                        config=config,
                        reason=detail,
                    )
                )
                continue
            candidate = deps.build_row_fn(contract, base_values, metrics)
            if candidate is not None:
                rows.append(candidate)

    out = pd.DataFrame(rows)
    if not out.empty:
        out = annotate_candidates_with_earnings_evidence(
            out,
            input_root=config.input_root,
        )

    if out_path is not None:
        if out.empty:
            pd.DataFrame(columns=config.empty_output_columns).to_csv(out_path, index=False)
        else:
            out.to_csv(out_path, index=False)
    if reject_out_path is not None:
        reject_log = pd.DataFrame(reject_rows)
        if reject_log.empty:
            pd.DataFrame(columns=_REJECT_LOG_COLUMNS).to_csv(reject_out_path, index=False)
        else:
            reject_log.to_csv(reject_out_path, index=False)
    if not config.quiet and out_path is not None and reject_out_path is not None:
        deps.print_summary_fn(out, out_path, reject_out_path)
    if calculation_decision_sink_fn is not None:
        calculation_decision_sink_fn(calculation_decisions)
    return out
