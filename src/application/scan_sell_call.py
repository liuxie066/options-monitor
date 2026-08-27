#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import pandas as pd

repo_base = Path(__file__).resolve().parents[2]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

from domain.domain.engine import CandidateCalculationError, calculate_opening_candidate_metrics
from domain.domain.candidate_defaults import (
    DEFAULT_CANDIDATE_LIQUIDITY,
    DEFAULT_SELL_CALL_WINDOW,
)
from domain.domain.sell_call_risk_bands import classify_sell_call_risk
from domain.domain.sell_call_config import (
    resolve_effective_sell_call_min_strike,
    validate_min_annualized_net_premium_return,
    validate_min_strike_cost_multiplier,
)
from domain.domain.strategy_vocab import STRATEGY_COVERED_CALL, strategy_display_name
from domain.domain.risk_capacity import compute_sell_call_share_capacity
from src.application.candidate_scanning import (
    CandidateScanConfig,
    CandidateScanDependencies,
    run_candidate_scan,
)

from src.application.candidate_models import CandidateBaseValues, CandidateContractInput


COVERED_CALL_DISPLAY = strategy_display_name(STRATEGY_COVERED_CALL)


def _normalize_contract_input(raw: CandidateContractInput | pd.Series) -> CandidateContractInput:
    if isinstance(raw, CandidateContractInput):
        return raw
    return CandidateContractInput.from_row(raw, mode="call")


def compute_metrics(
    contract: CandidateContractInput | pd.Series,
    avg_cost: float,
    *,
    now_utc: datetime | None = None,
) -> dict[str, Any] | None:
    contract = _normalize_contract_input(contract)
    try:
        metrics = calculate_opening_candidate_metrics(
            contract.to_gate_payload(),
            mode="call",
            avg_cost=avg_cost,
            now_utc=now_utc,
        )
    except CandidateCalculationError:
        return None
    risk_band = classify_sell_call_risk(float(metrics["strike_above_spot_pct"]))
    metrics["cc_band"] = risk_band.band
    metrics["risk_label"] = risk_band.risk_label
    return metrics


def explain_metrics_rejection(
    contract: CandidateContractInput | pd.Series,
    avg_cost: float,
    *,
    now_utc: datetime | None = None,
) -> dict[str, Any] | None:
    contract = _normalize_contract_input(contract)
    try:
        calculate_opening_candidate_metrics(
            contract.to_gate_payload(),
            mode="call",
            avg_cost=avg_cost,
            now_utc=now_utc,
        )
    except CandidateCalculationError as exc:
        return exc.to_payload()
    return {"rule": "candidate_metrics_unavailable", "message": "candidate metrics unavailable"}


def _make_compute_metrics(
    avg_cost: float,
    now_utc: datetime | None = None,
) -> Callable[[CandidateContractInput], dict[str, Any] | None]:
    def _compute(contract: CandidateContractInput) -> dict[str, Any] | None:
        return compute_metrics(contract, avg_cost, now_utc=now_utc)

    return _compute


def _make_explain_metrics_rejection(
    avg_cost: float,
    now_utc: datetime | None = None,
) -> Callable[[CandidateContractInput], dict[str, Any] | None]:
    def _explain(contract: CandidateContractInput) -> dict[str, Any] | None:
        return explain_metrics_rejection(contract, avg_cost, now_utc=now_utc)

    return _explain


def _resolve_sell_call_contract_capacity(
    *,
    multiplier: float | None,
    shares: int,
    shares_can_sell: int,
    shares_locked: int,
) -> tuple[int, int, bool]:
    capacity = compute_sell_call_share_capacity(
        shares_total=shares,
        shares_can_sell=shares_can_sell,
        shares_locked=shares_locked,
        multiplier=multiplier,
    )
    return (
        int(capacity.shares_available_for_cover),
        int(capacity.covered_contracts_available),
        bool(capacity.is_fully_covered_available),
    )


def _build_candidate_row_factory(
    *,
    avg_cost: float,
    shares: int,
    shares_can_sell: int,
    shares_locked: int,
    capacity_facts: Mapping[str, Any] | None,
) -> Callable[[CandidateContractInput, CandidateBaseValues, dict[str, Any]], dict[str, Any] | None]:
    def _build(
        contract: CandidateContractInput,
        base_values: CandidateBaseValues,
        metrics: dict[str, Any],
    ) -> dict[str, Any] | None:
        available, covered_contracts_available, is_fully_covered_available = _resolve_sell_call_contract_capacity(
            multiplier=contract.multiplier,
            shares=shares,
            shares_can_sell=shares_can_sell,
            shares_locked=shares_locked,
        )
        shares_total = int(shares)
        shares_locked_value = int(shares_locked)
        payload = contract.to_gate_payload()
        payload.pop("mode", None)
        payload.update(
            {
            "dte": base_values.dte,
            "strike": base_values.strike,
            "avg_cost": avg_cost,
            "shares_total": shares_total,
            "shares_can_sell": int(shares_can_sell),
            "shares_eligible": min(shares_total, int(shares_can_sell)),
            "shares_locked": shares_locked_value,
            "shares_available_for_cover": available,
            "covered_contracts_available": covered_contracts_available,
            "max_new_contracts": covered_contracts_available,
            "is_fully_covered_available": is_fully_covered_available,
            "shares": shares_total,
            **dict(capacity_facts or {}),
            "open_interest": base_values.open_interest,
            "volume": base_values.volume,
            **metrics,
            }
        )
        return payload

    return _build


def run_sell_call_scan(
    *,
    symbols: list[str],
    input_root: Path,
    avg_cost: float,
    shares: int,
    shares_can_sell: int,
    shares_locked: int,
    capacity_facts: Mapping[str, Any] | None = None,
    min_dte: int = DEFAULT_SELL_CALL_WINDOW.min_dte,
    max_dte: int = DEFAULT_SELL_CALL_WINDOW.max_dte,
    min_strike: float | None = None,
    max_strike: float | None = None,
    min_strike_cost_multiplier: float = 1.02,
    min_annualized_net_return: float | None = None,
    min_net_income: float = 50.0,
    min_open_interest: float | None = None,
    min_volume: float | None = None,
    max_spread_ratio: float | None = DEFAULT_CANDIDATE_LIQUIDITY.max_spread_ratio,
    strategy_family: str | None = None,
    strategy_profile: str | None = None,
    calculation_decision_sink_fn: (
        Callable[[list[dict[str, Any]]], None] | None
    ) = None,
    quote_freshness_now_utc: datetime | None = None,
    required_data_frames: Mapping[str, pd.DataFrame] | None = None,
) -> pd.DataFrame:
    """计算 Covered Call 候选，并返回类型化内存结果。"""
    # OI is a formal tie-break only; volume and delta remain display evidence.
    del min_open_interest, min_volume
    share_facts = {
        "shares": shares,
        "shares_can_sell": shares_can_sell,
        "shares_locked": shares_locked,
    }
    invalid_share_facts = [
        name
        for name, value in share_facts.items()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0
    ]
    if invalid_share_facts:
        raise ValueError(
            f"{', '.join(invalid_share_facts)} must be non-negative integers"
        )
    if shares_locked > min(shares, shares_can_sell):
        raise ValueError("shares_locked exceeds eligible underlying shares")
    threshold = validate_min_annualized_net_premium_return(
        min_annualized_net_return,
        source="--min-annualized-net-return",
    )
    cost_multiplier = validate_min_strike_cost_multiplier(
        min_strike_cost_multiplier,
        source="--min-strike-cost-multiplier",
    )
    effective_min_strike = resolve_effective_sell_call_min_strike(
        min_strike=min_strike,
        avg_cost=avg_cost,
        cost_multiplier=cost_multiplier,
    )
    scan_now_utc = quote_freshness_now_utc or datetime.now(timezone.utc)
    return run_candidate_scan(
        config=CandidateScanConfig(
            mode="call",
            symbols=symbols,
            input_root=Path(input_root),
            min_dte=int(min_dte),
            max_dte=int(max_dte),
            min_strike=effective_min_strike,
            max_strike=max_strike,
            min_open_interest=None,
            min_volume=None,
            max_spread_ratio=max_spread_ratio,
            min_annualized_net_return=threshold,
            min_net_income=float(min_net_income),
            strategy_family=strategy_family,
            strategy_profile=strategy_profile,
            required_data_frames=required_data_frames,
        ),
        deps=CandidateScanDependencies(
            compute_metrics_fn=_make_compute_metrics(avg_cost, now_utc=scan_now_utc),
            build_row_fn=_build_candidate_row_factory(
                avg_cost=avg_cost,
                shares=shares,
                shares_can_sell=shares_can_sell,
                shares_locked=shares_locked,
                capacity_facts=capacity_facts,
            ),
            metric_reject_reason_fn=_make_explain_metrics_rejection(avg_cost, now_utc=scan_now_utc),
        ),
        calculation_decision_sink_fn=calculation_decision_sink_fn,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=f"Run {COVERED_CALL_DISPLAY} scan on required_data CSV files")
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--avg-cost", type=float, required=True, help="Average holding cost per share")
    parser.add_argument("--shares", type=int, required=True, help="Observed holding shares")
    parser.add_argument("--shares-can-sell", type=int, required=True, help="Observed shares available to sell")
    parser.add_argument("--shares-locked", type=int, required=True, help="Shares already reserved by open calls")
    parser.add_argument("--min-dte", type=int, default=DEFAULT_SELL_CALL_WINDOW.min_dte)
    parser.add_argument("--max-dte", type=int, default=DEFAULT_SELL_CALL_WINDOW.max_dte)
    parser.add_argument("--min-strike", type=float, default=None)
    parser.add_argument("--max-strike", type=float, default=None)
    parser.add_argument("--min-strike-cost-multiplier", type=float, default=1.02, help="effective min strike also floors at avg_cost multiplied by this value")
    parser.add_argument("--min-annualized-net-return", type=float, default=None, help="required; min annualized net premium return in [0,1]")
    parser.add_argument("--min-net-income", type=float, default=50.0)
    parser.add_argument("--min-open-interest", type=float, default=None, help="deprecated compatibility option; ignored by Covered Call")
    parser.add_argument("--min-volume", type=float, default=None, help="deprecated compatibility option; ignored by Covered Call")
    parser.add_argument("--max-spread-ratio", type=float, default=DEFAULT_CANDIDATE_LIQUIDITY.max_spread_ratio)
    parser.add_argument("--input-root", default=None, help="Input root containing parsed/ required_data CSVs (default: output_shared/required_data)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    base = Path(__file__).resolve().parents[2]
    input_root = Path(args.input_root).resolve() if args.input_root else (base / "output_shared" / "required_data").resolve()
    try:
        out = run_sell_call_scan(
            symbols=args.symbols,
            input_root=input_root,
            avg_cost=args.avg_cost,
            shares=args.shares,
            shares_can_sell=args.shares_can_sell,
            shares_locked=args.shares_locked,
            min_dte=args.min_dte,
            max_dte=args.max_dte,
            min_strike=args.min_strike,
            max_strike=args.max_strike,
            min_strike_cost_multiplier=args.min_strike_cost_multiplier,
            min_annualized_net_return=args.min_annualized_net_return,
            min_net_income=args.min_net_income,
            min_open_interest=args.min_open_interest,
            min_volume=args.min_volume,
            max_spread_ratio=args.max_spread_ratio,
        )
    except ValueError as e:
        raise SystemExit(f"[ARG_ERROR] {e}")

    print(out.to_json(orient="records", force_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
