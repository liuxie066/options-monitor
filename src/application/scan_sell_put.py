#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

repo_base = Path(__file__).resolve().parents[2]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

from domain.domain.engine import CandidateCalculationError, calculate_opening_candidate_metrics
from domain.domain.candidate_defaults import (
    DEFAULT_CANDIDATE_LIQUIDITY,
    DEFAULT_SELL_PUT_WINDOW,
)
from domain.domain.sell_put_config import validate_min_annualized_net_return
from src.application.candidate_scanning import (
    CandidateScanConfig,
    CandidateScanDependencies,
    run_candidate_scan,
)

SELL_PUT_EMPTY_OUTPUT_COLUMNS = [
    "symbol",
    "market",
    "expiration",
    "dte",
    "contract_symbol",
    "multiplier",
    "currency",
    "strike",
    "spot",
    "bid",
    "ask",
    "last_price",
    "raw_mid",
    "mid",
    "raw_spread",
    "price_tick",
    "sell_limit",
    "quote_update_time",
    "quote_observed_at_utc",
    "quote_age_seconds",
    "quote_freshness_status",
    "open_interest",
    "volume",
    "implied_volatility",
    "realized_volatility_20",
    "realized_volatility_60",
    "realized_volatility_120",
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
    "assignment_notional",
    "net_cash_basis",
    "period_net_return",
    "otm_pct",
    "cash_basis",
    "period_net_return_on_cash_basis",
    "breakeven",
    "annualized_net_return_on_strike",
    "annualized_net_return_on_cash_basis",
    "earnings_evidence_status",
    "earnings_reason_code",
    "earnings_has_event",
    "earnings_event_dates",
    "earnings_snapshot_hash",
    "earnings_artifact_path",
    "reject_stage_candidate",
]

from src.application.candidate_models import CandidateBaseValues, CandidateContractInput


def _normalize_contract_input(raw: CandidateContractInput | pd.Series) -> CandidateContractInput:
    if isinstance(raw, CandidateContractInput):
        return raw
    return CandidateContractInput.from_row(raw, mode="put")


def compute_metrics(
    contract: CandidateContractInput | pd.Series,
    *,
    now_utc: datetime | None = None,
) -> dict[str, Any] | None:
    contract = _normalize_contract_input(contract)
    del now_utc
    try:
        metrics = calculate_opening_candidate_metrics(
            contract.to_gate_payload(),
            mode="put",
        )
    except CandidateCalculationError:
        return None
    metrics.update(
        {
            "quote_observed_at_utc": contract.quote_observed_at_utc,
            "quote_age_seconds": contract.quote_age_seconds,
            "quote_freshness_status": contract.opening_contract_status,
        }
    )
    return metrics


def explain_metrics_rejection(
    contract: CandidateContractInput | pd.Series,
    *,
    now_utc: datetime | None = None,
) -> dict[str, Any] | None:
    contract = _normalize_contract_input(contract)
    del now_utc
    try:
        calculate_opening_candidate_metrics(contract.to_gate_payload(), mode="put")
    except CandidateCalculationError as exc:
        return exc.to_payload()
    return {"rule": "candidate_metrics_unavailable", "message": "candidate metrics unavailable"}


def _build_candidate_row(
    contract: CandidateContractInput,
    base_values: CandidateBaseValues,
    metrics: dict[str, Any],
) -> dict[str, Any] | None:
    payload = contract.to_gate_payload()
    payload.pop("mode", None)
    payload["dte"] = base_values.dte
    payload["open_interest"] = base_values.open_interest
    payload["volume"] = base_values.volume
    payload.update(metrics)
    return payload


def _print_summary(out: pd.DataFrame, out_path: Path, reject_out_path: Path) -> None:
    print(f"[DONE] sell put scan -> {out_path}")
    print(f"[DONE] reject log -> {reject_out_path}")
    print(f"[DONE] candidates: {len(out)}")
    if not out.empty:
        display_cols = [
            "symbol",
            "expiration",
            "dte",
            "strike",
            "spot",
            "mid",
            "futu_fee",
            "net_income",
            "otm_pct",
            "annualized_net_return_on_cash_basis",
        ]
        print(out[display_cols].head(20).to_string(index=False))


def run_sell_put_scan(
    *,
    symbols: list[str],
    input_root: Path,
    output: Path | None,
    min_dte: int = DEFAULT_SELL_PUT_WINDOW.min_dte,
    max_dte: int = DEFAULT_SELL_PUT_WINDOW.max_dte,
    min_annualized_net_return: float | None = None,
    min_net_income: float = 50.0,
    min_strike: float | None = None,
    max_strike: float | None = None,
    min_open_interest: float | None = None,
    min_volume: float | None = None,
    max_spread_ratio: float | None = DEFAULT_CANDIDATE_LIQUIDITY.max_spread_ratio,
    reject_log_output: Path | None = None,
    strategy_family: str | None = None,
    strategy_profile: str | None = None,
    quiet: bool = False,
    quote_freshness_now_utc: datetime | None = None,
) -> pd.DataFrame:
    """执行卖出看跌期权扫描并写出候选 CSV。"""
    # Kept in the public Python/CLI surface for compatibility only. Sell Put
    # deliberately treats OI as ranking evidence and volume as display-only;
    # neither value is a hard eligibility gate.
    del min_open_interest, min_volume
    threshold = validate_min_annualized_net_return(
        min_annualized_net_return,
        source="--min-annualized-net-return",
    )

    scan_now_utc = quote_freshness_now_utc or datetime.now(timezone.utc)
    return run_candidate_scan(
        config=CandidateScanConfig(
            mode="put",
            symbols=symbols,
            input_root=Path(input_root),
            output=(Path(output) if output is not None else None),
            empty_output_columns=SELL_PUT_EMPTY_OUTPUT_COLUMNS,
            min_dte=int(min_dte),
            max_dte=int(max_dte),
            min_strike=min_strike,
            max_strike=max_strike,
            min_open_interest=None,
            min_volume=None,
            max_spread_ratio=max_spread_ratio,
            min_annualized_net_return=threshold,
            min_net_income=float(min_net_income),
            strategy_family=strategy_family,
            strategy_profile=strategy_profile,
            quiet=bool(quiet),
        ),
        deps=CandidateScanDependencies(
            compute_metrics_fn=lambda contract: compute_metrics(
                contract,
                now_utc=scan_now_utc,
            ),
            build_row_fn=_build_candidate_row,
            print_summary_fn=_print_summary,
            metric_reject_reason_fn=lambda contract: explain_metrics_rejection(
                contract,
                now_utc=scan_now_utc,
            ),
        ),
        reject_log_output=reject_log_output,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Sell Put scan on required_data CSV files")
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--min-dte", type=int, default=DEFAULT_SELL_PUT_WINDOW.min_dte)
    parser.add_argument("--max-dte", type=int, default=DEFAULT_SELL_PUT_WINDOW.max_dte)
    parser.add_argument("--min-annualized-net-return", type=float, default=None, help="required; min annualized net return in [0,1]")
    parser.add_argument("--min-net-income", type=float, default=50.0)
    parser.add_argument("--min-strike", type=float, default=None)
    parser.add_argument("--max-strike", type=float, default=None)
    parser.add_argument("--min-open-interest", type=float, default=None, help="deprecated compatibility option; ignored by Sell Put")
    parser.add_argument("--min-volume", type=float, default=None, help="deprecated compatibility option; ignored by Sell Put")
    parser.add_argument("--max-spread-ratio", type=float, default=DEFAULT_CANDIDATE_LIQUIDITY.max_spread_ratio)
    parser.add_argument("--quiet", action="store_true", help="quiet mode: suppress human-friendly prints")
    parser.add_argument("--output", default=None, help="Output CSV path (default: output_shared/reports/sell_put_candidates.csv)")
    parser.add_argument("--reject-log-output", default=None, help="Reject log CSV path (default: <output>_reject_log.csv)")
    parser.add_argument("--input-root", default=None, help="Input root containing parsed/ required_data CSVs (default: output_shared/required_data)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    base = Path(__file__).resolve().parents[2]
    input_root = Path(args.input_root).resolve() if args.input_root else (base / "output_shared" / "required_data").resolve()
    out_path = Path(args.output).resolve() if args.output else (base / "output_shared" / "reports" / "sell_put_candidates.csv")

    try:
        run_sell_put_scan(
            symbols=args.symbols,
            input_root=input_root,
            output=out_path,
            min_dte=args.min_dte,
            max_dte=args.max_dte,
            min_annualized_net_return=args.min_annualized_net_return,
            min_net_income=args.min_net_income,
            min_strike=args.min_strike,
            max_strike=args.max_strike,
            min_open_interest=args.min_open_interest,
            min_volume=args.min_volume,
            max_spread_ratio=args.max_spread_ratio,
            reject_log_output=(Path(args.reject_log_output).resolve() if args.reject_log_output else None),
            quiet=args.quiet,
        )
    except ValueError as e:
        raise SystemExit(f"[ARG_ERROR] {e}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
