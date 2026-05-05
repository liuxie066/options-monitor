#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable
import pandas as pd

repo_base = Path(__file__).resolve().parents[1]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

from scripts.event_risk_filter import annotate_candidates_with_event_risk
from scripts.candidate_defaults import (
    DEFAULT_CANDIDATE_LIQUIDITY,
    DEFAULT_SELL_CALL_WINDOW,
    resolve_event_risk_config,
)
from scripts.sell_call_risk_bands import classify_sell_call_risk
from scripts.sell_call_config import validate_min_annualized_net_premium_return
from src.application.candidate_scanning import (
    CandidateScanConfig,
    CandidateScanDependencies,
    run_candidate_scan,
)

SELL_CALL_EMPTY_OUTPUT_COLUMNS = [
    "symbol",
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
    "annualized_net_premium_return",
    "if_exercised_total_return",
    "strike_above_spot_pct",
    "strike_above_cost_pct",
    "cc_band",
    "risk_label",
    "event_flag",
    "event_types",
    "event_dates",
    "reject_stage_candidate",
]

from scripts.fee_calc import calc_futu_option_fee
from src.application.candidate_models import CandidateBaseValues, CandidateContractInput


SHARES_MIN_ERROR = "shares 必须至少 100，sell call 才有意义。"


def _normalize_contract_input(raw: CandidateContractInput | pd.Series) -> CandidateContractInput:
    if isinstance(raw, CandidateContractInput):
        return raw
    return CandidateContractInput.from_row(raw, mode="call")


def compute_metrics(contract: CandidateContractInput | pd.Series, avg_cost: float):
    contract = _normalize_contract_input(contract)
    mid = contract.mid
    strike = contract.strike
    spot = contract.spot
    dte = contract.dte
    if None in (mid, strike, spot) or dte <= 0 or avg_cost <= 0:
        return None
    if mid <= 0 or strike <= 0 or spot <= 0:
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

    annualized_net_premium_return = (net_income / (spot * m)) * (365 / dte)
    if_exercised_total_return = (((strike - avg_cost) * m) + net_income) / (avg_cost * m)
    strike_above_spot_pct = (strike - spot) / spot
    strike_above_cost_pct = (strike - avg_cost) / avg_cost
    risk_band = classify_sell_call_risk(strike_above_spot_pct)

    return {
        "gross_income": round(gross_income, 6),
        "futu_fee": round(fee, 6),
        "net_income": round(net_income, 6),
        "annualized_net_premium_return": round(annualized_net_premium_return, 6),
        "if_exercised_total_return": round(if_exercised_total_return, 6),
        "strike_above_spot_pct": round(strike_above_spot_pct, 6),
        "strike_above_cost_pct": round(strike_above_cost_pct, 6),
        "cc_band": risk_band.band,
        "risk_label": risk_band.risk_label,
    }


def _make_compute_metrics(avg_cost: float) -> Callable[[CandidateContractInput], dict | None]:
    def _compute(contract: CandidateContractInput) -> dict | None:
        return compute_metrics(contract, avg_cost)

    return _compute


def _resolve_covered_contracts(*, multiplier: float | None, shares: int, shares_locked: int, shares_available_for_cover: int | None) -> tuple[int, int, bool]:
    m = multiplier
    m_int = int(m) if m is not None and m > 0 else 0
    shares_total = int(shares)
    shares_locked_value = int(shares_locked or 0)
    available = shares_available_for_cover
    try:
        if available is not None:
            available = int(available)
    except Exception:
        available = None
    if available is None:
        available = max(0, shares_total - shares_locked_value)
    covered_contracts_available = 0
    is_fully_covered_available = False
    try:
        m_int = int(m) if m is not None and m > 0 else 0
        if m_int > 0:
            covered_contracts_available = max(0, int(available)) // m_int
            is_fully_covered_available = covered_contracts_available >= 1
    except Exception:
        covered_contracts_available = 0
        is_fully_covered_available = False
    return int(available), covered_contracts_available, is_fully_covered_available


def _build_candidate_row_factory(*, avg_cost: float, shares: int, shares_locked: int, shares_available_for_cover: int | None) -> Callable[[CandidateContractInput, CandidateBaseValues, dict], dict | None]:
    def _build(contract: CandidateContractInput, base_values: CandidateBaseValues, metrics: dict) -> dict | None:
        available, covered_contracts_available, is_fully_covered_available = _resolve_covered_contracts(
            multiplier=contract.multiplier,
            shares=shares,
            shares_locked=shares_locked,
            shares_available_for_cover=shares_available_for_cover,
        )
        if covered_contracts_available < 1:
            return None

        shares_total = int(shares)
        shares_locked_value = int(shares_locked or 0)
        return {
            "symbol": contract.symbol,
            "expiration": contract.expiration,
            "dte": base_values.dte,
            "contract_symbol": contract.contract_symbol,
            "multiplier": contract.multiplier,
            "currency": contract.currency,
            "strike": base_values.strike,
            "spot": contract.spot,
            "avg_cost": avg_cost,
            "shares_total": shares_total,
            "shares_locked": shares_locked_value,
            "shares_available_for_cover": available,
            "covered_contracts_available": covered_contracts_available,
            "is_fully_covered_available": is_fully_covered_available,
            "shares": shares_total,
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

    return _build


def _print_summary(out: pd.DataFrame, out_path: Path, reject_out_path: Path) -> None:
    print(f"[DONE] sell call scan -> {out_path}")
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
    min_annualized_net_return: float | None = None,
    min_net_income: float = 50.0,
    min_open_interest: float = DEFAULT_CANDIDATE_LIQUIDITY.min_open_interest,
    min_volume: float = DEFAULT_CANDIDATE_LIQUIDITY.min_volume,
    max_spread_ratio: float | None = DEFAULT_CANDIDATE_LIQUIDITY.max_spread_ratio,
    event_risk_cfg: dict | None = None,
    reject_log_output: Path | None = None,
    quiet: bool = False,
) -> pd.DataFrame:
    """执行卖出看涨期权扫描并写出候选 CSV。"""
    if shares < 100:
        raise ValueError(SHARES_MIN_ERROR)

    threshold = validate_min_annualized_net_premium_return(
        min_annualized_net_return,
        source="--min-annualized-net-return",
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
            compute_metrics_fn=_make_compute_metrics(avg_cost),
            build_row_fn=_build_candidate_row_factory(
                avg_cost=avg_cost,
                shares=shares,
                shares_locked=shares_locked,
                shares_available_for_cover=shares_available_for_cover,
            ),
            build_hard_constraint_kwargs_fn=lambda contract: {
                "call_covered_contracts_available": _resolve_covered_contracts(
                    multiplier=contract.multiplier,
                    shares=shares,
                    shares_locked=shares_locked,
                    shares_available_for_cover=shares_available_for_cover,
                )[1]
            },
            annualized_return_value_fn=lambda metrics: metrics.get("annualized_net_premium_return"),
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
    parser = argparse.ArgumentParser(description="Run Sell Call scan on required_data CSV files")
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--avg-cost", type=float, required=True, help="Average holding cost per share")
    parser.add_argument("--shares", type=int, default=100)
    parser.add_argument("--shares-locked", type=int, default=0)
    parser.add_argument("--shares-available-for-cover", type=int, default=None)
    parser.add_argument("--min-dte", type=int, default=DEFAULT_SELL_CALL_WINDOW.min_dte)
    parser.add_argument("--max-dte", type=int, default=DEFAULT_SELL_CALL_WINDOW.max_dte)
    parser.add_argument("--min-strike", type=float, default=None)
    parser.add_argument("--max-strike", type=float, default=None)
    parser.add_argument("--min-annualized-net-return", type=float, default=None, help="required; min annualized net premium return in [0,1]")
    parser.add_argument("--min-net-income", type=float, default=50.0)
    parser.add_argument("--min-open-interest", type=float, default=DEFAULT_CANDIDATE_LIQUIDITY.min_open_interest)
    parser.add_argument("--min-volume", type=float, default=DEFAULT_CANDIDATE_LIQUIDITY.min_volume)
    parser.add_argument("--max-spread-ratio", type=float, default=DEFAULT_CANDIDATE_LIQUIDITY.max_spread_ratio)
    parser.add_argument("--event-risk-enabled", dest="event_risk_enabled", action="store_true", default=None)
    parser.add_argument("--no-event-risk-enabled", dest="event_risk_enabled", action="store_false")
    parser.add_argument("--event-risk-mode", dest="event_risk_mode", type=str, default="warn")
    parser.add_argument("--quiet", action="store_true", help="quiet mode: suppress human-friendly prints")
    parser.add_argument("--output", default=None, help="Output CSV path (default: output/reports/sell_call_candidates.csv)")
    parser.add_argument("--reject-log-output", default=None, help="Reject log CSV path (default: <output>_reject_log.csv)")
    parser.add_argument("--input-root", default=None, help="Input root containing parsed/ required_data CSVs (default: ./output)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    base = Path(__file__).resolve().parents[1]
    input_root = Path(args.input_root).resolve() if args.input_root else (base / "output").resolve()
    out_path = Path(args.output).resolve() if args.output else (base / "output" / "reports" / "sell_call_candidates.csv")

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
        msg = str(e)
        if msg == SHARES_MIN_ERROR:
            raise SystemExit(msg)
        raise SystemExit(f"[ARG_ERROR] {msg}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
