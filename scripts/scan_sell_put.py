#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path
import pandas as pd

repo_base = Path(__file__).resolve().parents[1]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

from scripts.event_risk_filter import annotate_candidates_with_event_risk
from scripts.candidate_defaults import (
    DEFAULT_CANDIDATE_LIQUIDITY,
    DEFAULT_SELL_PUT_WINDOW,
    resolve_event_risk_config,
)
from scripts.sell_put_config import validate_min_annualized_net_return
from src.application.candidate_scanning import (
    CandidateScanConfig,
    CandidateScanDependencies,
    run_candidate_scan,
)

SELL_PUT_EMPTY_OUTPUT_COLUMNS = [
    "symbol",
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
    "mid",
    "open_interest",
    "volume",
    "implied_volatility",
    "delta",
    "spread",
    "spread_ratio",
    "gross_income",
    "futu_fee",
    "net_income",
    "otm_pct",
    "cash_basis",
    "breakeven",
    "annualized_net_return_on_strike",
    "annualized_net_return_on_cash_basis",
    "event_flag",
    "event_types",
    "event_dates",
    "reject_stage_candidate",
]

from scripts.fee_calc import calc_futu_option_fee, safe_float
from src.application.candidate_models import CandidateBaseValues, CandidateContractInput


def _normalize_contract_input(raw: CandidateContractInput | pd.Series) -> CandidateContractInput:
    if isinstance(raw, CandidateContractInput):
        return raw
    return CandidateContractInput.from_row(raw, mode="put")


def compute_metrics(contract: CandidateContractInput | pd.Series) -> dict | None:
    contract = _normalize_contract_input(contract)
    mid = contract.mid
    strike = contract.strike
    spot = contract.spot
    dte = contract.dte

    if mid is None or strike is None or spot is None or dte <= 0:
        return None
    if mid <= 0 or strike <= 0 or spot <= 0:
        return None
    if strike >= spot:
        return None

    multiplier = contract.multiplier
    m = int(multiplier) if multiplier and multiplier > 0 else None
    if not m:
        return None

    gross_income = mid * m
    fee = calc_futu_option_fee(
        contract.currency,
        mid,
        contracts=1,
        multiplier=m,
        is_sell=True,
    )
    net_income = gross_income - fee
    if net_income <= 0:
        return None

    otm_pct = (spot - strike) / spot
    cash_basis = strike * m - net_income
    if cash_basis <= 0:
        return None

    annualized_net_return_on_cash_basis = (net_income / cash_basis) * (365 / dte)
    annualized_net_return_on_strike = (net_income / (strike * m)) * (365 / dte)
    breakeven = strike - net_income / m

    return {
        "gross_income": round(gross_income, 6),
        "futu_fee": round(fee, 6),
        "net_income": round(net_income, 6),
        "otm_pct": round(otm_pct, 6),
        "cash_basis": round(cash_basis, 6),
        "breakeven": round(breakeven, 6),
        "annualized_net_return_on_strike": round(annualized_net_return_on_strike, 6),
        "annualized_net_return_on_cash_basis": round(annualized_net_return_on_cash_basis, 6),
    }


def _build_candidate_row(contract: CandidateContractInput, base_values: CandidateBaseValues, metrics: dict) -> dict | None:
    return {
        "symbol": contract.symbol,
        "expiration": contract.expiration,
        "dte": base_values.dte,
        "contract_symbol": contract.contract_symbol,
        "multiplier": contract.multiplier,
        "currency": contract.currency,
        "strike": contract.strike,
        "spot": contract.spot,
        "bid": contract.bid,
        "ask": contract.ask,
        "last_price": contract.last_price,
        "mid": contract.mid,
        "open_interest": base_values.open_interest,
        "volume": base_values.volume,
        "implied_volatility": contract.implied_volatility,
        "delta": contract.delta,
        "spread": base_values.spread,
        "spread_ratio": base_values.spread_ratio,
        **metrics,
    }


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
    output: Path,
    min_dte: int = DEFAULT_SELL_PUT_WINDOW.min_dte,
    max_dte: int = DEFAULT_SELL_PUT_WINDOW.max_dte,
    min_annualized_net_return: float | None = None,
    min_net_income: float = 50.0,
    min_strike: float | None = None,
    max_strike: float | None = None,
    min_open_interest: float = DEFAULT_CANDIDATE_LIQUIDITY.min_open_interest,
    min_volume: float = DEFAULT_CANDIDATE_LIQUIDITY.min_volume,
    max_spread_ratio: float | None = DEFAULT_CANDIDATE_LIQUIDITY.max_spread_ratio,
    event_risk_cfg: dict | None = None,
    reject_log_output: Path | None = None,
    quiet: bool = False,
) -> pd.DataFrame:
    """执行卖出看跌期权扫描并写出候选 CSV。"""
    threshold = validate_min_annualized_net_return(
        min_annualized_net_return,
        source="--min-annualized-net-return",
    )

    return run_candidate_scan(
        config=CandidateScanConfig(
            mode="put",
            symbols=symbols,
            input_root=Path(input_root),
            output=Path(output),
            empty_output_columns=SELL_PUT_EMPTY_OUTPUT_COLUMNS,
            min_dte=int(min_dte),
            max_dte=int(max_dte),
            min_strike=min_strike,
            max_strike=max_strike,
            min_open_interest=float(min_open_interest),
            min_volume=float(min_volume),
            max_spread_ratio=max_spread_ratio,
            min_annualized_net_return=threshold,
            min_net_income=float(min_net_income),
            quiet=bool(quiet),
        ),
        deps=CandidateScanDependencies(
            compute_metrics_fn=compute_metrics,
            build_row_fn=_build_candidate_row,
            build_hard_constraint_kwargs_fn=lambda _contract: {},
            annualized_return_value_fn=lambda metrics: metrics.get("annualized_net_return_on_cash_basis"),
            event_risk_flag_fn=lambda _row: False,
            event_risk_mode_fn=lambda cfg: str((cfg or {}).get("mode") or "warn"),
            annotate_event_risk_fn=lambda df, base_dir, cfg: annotate_candidates_with_event_risk(
                df,
                base_dir=base_dir,
                event_risk_cfg=cfg,
            ),
            print_summary_fn=_print_summary,
        ),
        event_risk_cfg=event_risk_cfg,
        base_dir=Path(__file__).resolve().parents[1],
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
    parser.add_argument("--min-open-interest", type=float, default=DEFAULT_CANDIDATE_LIQUIDITY.min_open_interest)
    parser.add_argument("--min-volume", type=float, default=DEFAULT_CANDIDATE_LIQUIDITY.min_volume)
    parser.add_argument("--max-spread-ratio", type=float, default=DEFAULT_CANDIDATE_LIQUIDITY.max_spread_ratio)
    parser.add_argument("--event-risk-enabled", dest="event_risk_enabled", action="store_true", default=None)
    parser.add_argument("--no-event-risk-enabled", dest="event_risk_enabled", action="store_false")
    parser.add_argument("--event-risk-mode", dest="event_risk_mode", type=str, default="warn")
    parser.add_argument("--quiet", action="store_true", help="quiet mode: suppress human-friendly prints")
    parser.add_argument("--output", default=None, help="Output CSV path (default: output/reports/sell_put_candidates.csv)")
    parser.add_argument("--reject-log-output", default=None, help="Reject log CSV path (default: <output>_reject_log.csv)")
    parser.add_argument("--input-root", default=None, help="Input root containing parsed/ required_data CSVs (default: ./output)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    base = Path(__file__).resolve().parents[1]
    input_root = Path(args.input_root).resolve() if args.input_root else (base / "output").resolve()
    out_path = Path(args.output).resolve() if args.output else (base / "output" / "reports" / "sell_put_candidates.csv")

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
