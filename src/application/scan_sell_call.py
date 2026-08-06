#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable

import pandas as pd

repo_base = Path(__file__).resolve().parents[2]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

from src.application.event_risk_filter import annotate_candidates_with_event_risk
from domain.domain.engine import CandidateCalculationError, calculate_opening_candidate_metrics
from domain.domain.candidate_defaults import (
    DEFAULT_CANDIDATE_LIQUIDITY,
    DEFAULT_SELL_CALL_WINDOW,
    resolve_event_risk_config,
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
    resolve_candidate_score_weights,
    run_candidate_scan,
)

SELL_CALL_EMPTY_OUTPUT_COLUMNS = [
    "symbol",
    "market",
    "expiration",
    "dte",
    "contract_symbol",
    "multiplier",
    "currency",
    "strike",
    "spot",
    "avg_cost",
    "shares_total",
    "shares_locked",
    "shares_available_for_cover",
    "covered_contracts_available",
    "is_fully_covered_available",
    "shares",
    "bid",
    "ask",
    "last_price",
    "raw_mid",
    "mid",
    "raw_spread",
    "price_tick",
    "sell_limit",
    "open_interest",
    "volume",
    "implied_volatility",
    "realized_volatility_20",
    "realized_volatility_60",
    "realized_volatility_120",
    "realized_volatility_estimate",
    "term_matched_rv",
    "term_matched_rv_status",
    "term_matched_rv_reason",
    "term_matched_rv_input_hash",
    "iv_rv_ratio",
    "iv_minus_rv",
    "delta",
    "spread",
    "spread_ratio",
    "gross_premium",
    "gross_income",
    "estimated_full_sell_fees",
    "futu_fee",
    "fee_schedule_version",
    "fee_basis",
    "fee_schedule_url",
    "net_premium",
    "net_income",
    "net_premium_cny",
    "current_market_value",
    "period_net_premium_return",
    "period_net_return",
    "annualized_net_premium_return",
    "if_exercised_total_return",
    "strike_above_spot_pct",
    "strike_above_cost_pct",
    "cc_band",
    "risk_label",
    "earnings_evidence_status",
    "earnings_reason_code",
    "earnings_has_event",
    "earnings_event_dates",
    "earnings_snapshot_hash",
    "earnings_artifact_path",
    "event_flag",
    "event_types",
    "event_dates",
    "event_source_status",
    "event_source_error",
    "reject_stage_candidate",
]

from src.application.candidate_models import CandidateBaseValues, CandidateContractInput


COVERED_CALL_DISPLAY = strategy_display_name(STRATEGY_COVERED_CALL)


def _normalize_contract_input(raw: CandidateContractInput | pd.Series) -> CandidateContractInput:
    if isinstance(raw, CandidateContractInput):
        return raw
    return CandidateContractInput.from_row(raw, mode="call")


def compute_metrics(contract: CandidateContractInput | pd.Series, avg_cost: float) -> dict[str, Any] | None:
    contract = _normalize_contract_input(contract)
    try:
        metrics = calculate_opening_candidate_metrics(
            contract.to_gate_payload(),
            mode="call",
            avg_cost=avg_cost,
        )
    except CandidateCalculationError:
        return None
    risk_band = classify_sell_call_risk(float(metrics["strike_above_spot_pct"]))
    metrics["cc_band"] = risk_band.band
    metrics["risk_label"] = risk_band.risk_label
    return metrics


def explain_metrics_rejection(contract: CandidateContractInput | pd.Series, avg_cost: float) -> dict[str, Any] | None:
    contract = _normalize_contract_input(contract)
    try:
        calculate_opening_candidate_metrics(
            contract.to_gate_payload(),
            mode="call",
            avg_cost=avg_cost,
        )
    except CandidateCalculationError as exc:
        return exc.to_payload()
    return {"rule": "candidate_metrics_unavailable", "message": "candidate metrics unavailable"}


def _make_compute_metrics(avg_cost: float) -> Callable[[CandidateContractInput], dict[str, Any] | None]:
    def _compute(contract: CandidateContractInput) -> dict[str, Any] | None:
        return compute_metrics(contract, avg_cost)

    return _compute


def _make_explain_metrics_rejection(avg_cost: float) -> Callable[[CandidateContractInput], dict[str, Any] | None]:
    def _explain(contract: CandidateContractInput) -> dict[str, Any] | None:
        return explain_metrics_rejection(contract, avg_cost)

    return _explain


def _resolve_sell_call_contract_capacity(
    *,
    multiplier: float | None,
    shares: int,
    shares_locked: int,
    shares_available_for_cover: int | None,
) -> tuple[int, int, bool]:
    capacity = compute_sell_call_share_capacity(
        shares_total=shares,
        shares_locked=shares_locked,
        shares_available_for_cover=shares_available_for_cover,
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
    shares_locked: int,
    shares_available_for_cover: int | None,
) -> Callable[[CandidateContractInput, CandidateBaseValues, dict[str, Any]], dict[str, Any] | None]:
    def _build(
        contract: CandidateContractInput,
        base_values: CandidateBaseValues,
        metrics: dict[str, Any],
    ) -> dict[str, Any] | None:
        available, covered_contracts_available, is_fully_covered_available = _resolve_sell_call_contract_capacity(
            multiplier=contract.multiplier,
            shares=shares,
            shares_locked=shares_locked,
            shares_available_for_cover=shares_available_for_cover,
        )
        if covered_contracts_available < 1:
            return None

        shares_total = int(shares)
        shares_locked_value = int(shares_locked or 0)
        payload = contract.to_gate_payload()
        payload.pop("mode", None)
        payload.update(
            {
            "dte": base_values.dte,
            "strike": base_values.strike,
            "avg_cost": avg_cost,
            "shares_total": shares_total,
            "shares_locked": shares_locked_value,
            "shares_available_for_cover": available,
            "covered_contracts_available": covered_contracts_available,
            "is_fully_covered_available": is_fully_covered_available,
            "shares": shares_total,
            "open_interest": base_values.open_interest,
            "volume": base_values.volume,
            **metrics,
            }
        )
        return payload

    return _build


def _print_summary(out: pd.DataFrame, out_path: Path, reject_out_path: Path) -> None:
    print(f"[DONE] {COVERED_CALL_DISPLAY.lower()} scan -> {out_path}")
    print(f"[DONE] reject log -> {reject_out_path}")
    print(f"[DONE] candidates: {len(out)}")
    if not out.empty:
        cols = [
            "symbol",
            "expiration",
            "dte",
            "strike",
            "spot",
            "avg_cost",
            "mid",
            "net_income",
            "annualized_net_premium_return",
            "if_exercised_total_return",
            "strike_above_spot_pct",
            "risk_label",
        ]
        print(out[cols].head(20).to_string(index=False))


def run_sell_call_scan(
    *,
    symbols: list[str],
    input_root: Path,
    output: Path,
    avg_cost: float,
    shares: int = 100,
    shares_locked: int = 0,
    shares_available_for_cover: int | None = None,
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
    event_risk_cfg: dict[str, Any] | None = None,
    score_weights: dict[str, Any] | None = None,
    reject_log_output: Path | None = None,
    strategy_family: str | None = None,
    strategy_profile: str | None = None,
    quiet: bool = False,
    risk_policy_version: str | None = None,
    quote_snapshot_id: str | None = None,
    all_decisions_sink_fn: Callable[[list[dict[str, Any]]], None] | None = None,
) -> pd.DataFrame:
    """执行 Covered Call 扫描并写出候选 CSV。"""
    # OI is a formal tie-break only; volume and delta remain display evidence.
    del min_open_interest, min_volume
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

    return run_candidate_scan(
        config=CandidateScanConfig(
            mode="call",
            symbols=symbols,
            input_root=Path(input_root),
            output=Path(output),
            empty_output_columns=SELL_CALL_EMPTY_OUTPUT_COLUMNS,
            min_dte=int(min_dte),
            max_dte=int(max_dte),
            min_strike=effective_min_strike,
            max_strike=max_strike,
            min_open_interest=None,
            min_volume=None,
            max_spread_ratio=max_spread_ratio,
            min_annualized_net_return=threshold,
            min_net_income=float(min_net_income),
            score_weights=resolve_candidate_score_weights(score_weights),
            strategy_family=strategy_family,
            strategy_profile=strategy_profile,
            quiet=bool(quiet),
            risk_policy_version=risk_policy_version,
            quote_snapshot_id=quote_snapshot_id,
        ),
        deps=CandidateScanDependencies(
            compute_metrics_fn=_make_compute_metrics(avg_cost),
            build_row_fn=_build_candidate_row_factory(
                avg_cost=avg_cost,
                shares=shares,
                shares_locked=shares_locked,
                shares_available_for_cover=shares_available_for_cover,
            ),
            build_hard_constraint_kwargs_fn=lambda contract: {
                "call_covered_contracts_available": _resolve_sell_call_contract_capacity(
                    multiplier=contract.multiplier,
                    shares=shares,
                    shares_locked=shares_locked,
                    shares_available_for_cover=shares_available_for_cover,
                )[1]
            },
            annualized_return_value_fn=lambda metrics: metrics.get("annualized_net_premium_return"),
            annotate_event_risk_fn=lambda df, base_dir, cfg: annotate_candidates_with_event_risk(
                df,
                base_dir=base_dir,
                event_risk_cfg=cfg,
            ),
            print_summary_fn=_print_summary,
            metric_reject_reason_fn=_make_explain_metrics_rejection(avg_cost),
            all_decisions_sink_fn=all_decisions_sink_fn,
        ),
        event_risk_cfg=event_risk_cfg,
        base_dir=Path(__file__).resolve().parents[2],
        reject_log_output=reject_log_output,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=f"Run {COVERED_CALL_DISPLAY} scan on required_data CSV files")
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--avg-cost", type=float, required=True, help="Average holding cost per share")
    parser.add_argument("--shares", type=int, default=100)
    parser.add_argument("--shares-locked", type=int, default=0)
    parser.add_argument("--shares-available-for-cover", type=int, default=None)
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
    parser.add_argument("--event-risk-enabled", dest="event_risk_enabled", action="store_true", default=None)
    parser.add_argument("--no-event-risk-enabled", dest="event_risk_enabled", action="store_false")
    parser.add_argument("--event-risk-mode", dest="event_risk_mode", type=str, default="warn")
    parser.add_argument("--quiet", action="store_true", help="quiet mode: suppress human-friendly prints")
    parser.add_argument("--output", default=None, help="Output CSV path (default: output_shared/reports/sell_call_candidates.csv)")
    parser.add_argument("--reject-log-output", default=None, help="Reject log CSV path (default: <output>_reject_log.csv)")
    parser.add_argument("--input-root", default=None, help="Input root containing parsed/ required_data CSVs (default: output_shared/required_data)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    base = Path(__file__).resolve().parents[2]
    input_root = Path(args.input_root).resolve() if args.input_root else (base / "output_shared" / "required_data").resolve()
    out_path = Path(args.output).resolve() if args.output else (base / "output_shared" / "reports" / "sell_call_candidates.csv")

    try:
        run_sell_call_scan(
            symbols=args.symbols,
            input_root=input_root,
            output=out_path,
            avg_cost=args.avg_cost,
            shares=args.shares,
            shares_locked=args.shares_locked,
            shares_available_for_cover=args.shares_available_for_cover,
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
            event_risk_cfg=resolve_event_risk_config(
                {
                    "enabled": True if args.event_risk_enabled is None else bool(args.event_risk_enabled),
                    "mode": args.event_risk_mode,
                }
            ),
            reject_log_output=(Path(args.reject_log_output).resolve() if args.reject_log_output else None),
            quiet=args.quiet,
        )
    except ValueError as e:
        raise SystemExit(f"[ARG_ERROR] {e}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
