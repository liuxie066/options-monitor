#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

repo_base = Path(__file__).resolve().parents[2]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

from src.application.event_risk_filter import annotate_candidates_with_event_risk
from domain.domain.candidate_defaults import (
    DEFAULT_CANDIDATE_LIQUIDITY,
    DEFAULT_SELL_PUT_WINDOW,
    resolve_event_risk_config,
)
from domain.domain.sell_put_config import validate_min_annualized_net_return
from domain.domain.quote_freshness import evaluate_option_quote_freshness
from src.application.candidate_scanning import (
    CandidateScanConfig,
    CandidateScanDependencies,
    resolve_candidate_score_weights,
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
    "mid",
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
    "realized_volatility_estimate",
    "iv_rv_ratio",
    "iv_minus_rv",
    "delta",
    "spread",
    "spread_ratio",
    "gross_income",
    "futu_fee",
    "net_income",
    "otm_pct",
    "cash_basis",
    "period_net_return_on_cash_basis",
    "breakeven",
    "annualized_net_return_on_strike",
    "annualized_net_return_on_cash_basis",
    "event_flag",
    "event_types",
    "event_dates",
    "event_source_status",
    "event_source_error",
    "event_earnings_coverage_status",
    "event_earnings_coverage_error",
    "reject_stage_candidate",
]

from domain.domain.fee_calc import calc_futu_option_fee
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
    quote_freshness = evaluate_option_quote_freshness(
        market=contract.market,
        update_time=contract.quote_update_time,
        now_utc=now_utc,
        max_age_seconds=300,
    )
    if not quote_freshness.rank_eligible:
        return None
    mid = contract.mid
    strike = contract.strike
    spot = contract.spot
    dte = contract.dte

    if mid is None or strike is None or spot is None or dte is None:
        return None
    if dte <= 0:
        return None
    if mid <= 0 or strike <= 0 or spot <= 0:
        return None
    if strike > spot:
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
    period_net_return_on_cash_basis = net_income / cash_basis
    annualized_net_return_on_strike = (net_income / (strike * m)) * (365 / dte)
    breakeven = strike - net_income / m

    return {
        "quote_observed_at_utc": quote_freshness.observed_at_utc,
        "quote_age_seconds": round(float(quote_freshness.age_seconds or 0.0), 3),
        "quote_freshness_status": quote_freshness.status,
        "gross_income": round(gross_income, 6),
        "futu_fee": round(fee, 6),
        "net_income": round(net_income, 6),
        "otm_pct": round(otm_pct, 6),
        "cash_basis": round(cash_basis, 6),
        "breakeven": round(breakeven, 6),
        "period_net_return_on_cash_basis": round(period_net_return_on_cash_basis, 6),
        "annualized_net_return_on_strike": round(annualized_net_return_on_strike, 6),
        "annualized_net_return_on_cash_basis": round(annualized_net_return_on_cash_basis, 6),
    }


def explain_metrics_rejection(
    contract: CandidateContractInput | pd.Series,
    *,
    now_utc: datetime | None = None,
) -> dict[str, Any] | None:
    contract = _normalize_contract_input(contract)
    quote_freshness = evaluate_option_quote_freshness(
        market=contract.market,
        update_time=contract.quote_update_time,
        now_utc=now_utc,
        max_age_seconds=300,
    )
    if not quote_freshness.rank_eligible:
        return {
            "rule": quote_freshness.status,
            "metric_value": quote_freshness.age_seconds,
            "threshold": 300,
            "message": "OpenD quote is not eligible for active-session ranking",
        }
    mid = contract.mid
    strike = contract.strike
    spot = contract.spot
    dte = contract.dte
    if mid is None or strike is None or spot is None or dte is None:
        return {"rule": "metrics_input_missing", "message": "mid, strike, spot, or dte missing"}
    if dte <= 0:
        return {"rule": "metrics_dte_non_positive", "metric_value": dte, "threshold": 0, "message": "dte must be positive"}
    if mid <= 0:
        return {"rule": "metrics_mid_non_positive", "metric_value": mid, "threshold": 0, "message": "mid must be positive"}
    if strike <= 0:
        return {"rule": "metrics_strike_non_positive", "metric_value": strike, "threshold": 0, "message": "strike must be positive"}
    if spot <= 0:
        return {"rule": "metrics_spot_non_positive", "metric_value": spot, "threshold": 0, "message": "spot must be positive"}
    if strike > spot:
        return {"rule": "metrics_put_strike_above_spot", "metric_value": strike, "threshold": spot, "message": "put strike must not exceed spot"}

    multiplier = contract.multiplier
    m = int(multiplier) if multiplier and multiplier > 0 else None
    if not m:
        return {"rule": "metrics_multiplier_invalid", "metric_value": multiplier, "threshold": 0, "message": "multiplier must be positive"}

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
        return {"rule": "metrics_net_income_non_positive", "metric_value": net_income, "threshold": 0, "message": "net income must be positive"}

    cash_basis = strike * m - net_income
    if cash_basis <= 0:
        return {"rule": "metrics_cash_basis_non_positive", "metric_value": cash_basis, "threshold": 0, "message": "cash basis must be positive"}
    return {"rule": "candidate_metrics_unavailable", "message": "candidate metrics unavailable"}


def _build_candidate_row(
    contract: CandidateContractInput,
    base_values: CandidateBaseValues,
    metrics: dict[str, Any],
) -> dict[str, Any] | None:
    return {
        "symbol": contract.symbol,
        "market": contract.market,
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
        "quote_update_time": contract.quote_update_time,
        "open_interest": base_values.open_interest,
        "volume": base_values.volume,
        "implied_volatility": contract.implied_volatility,
        "realized_volatility_20": contract.realized_volatility_20,
        "realized_volatility_60": contract.realized_volatility_60,
        "realized_volatility_120": contract.realized_volatility_120,
        "realized_volatility_estimate": contract.realized_volatility_estimate,
        "iv_rv_ratio": (
            round(contract.implied_volatility / contract.realized_volatility_estimate, 6)
            if (
                contract.implied_volatility is not None
                and contract.realized_volatility_estimate is not None
                and contract.realized_volatility_estimate > 0
            )
            else None
        ),
        "iv_minus_rv": (
            round(contract.implied_volatility - contract.realized_volatility_estimate, 6)
            if contract.implied_volatility is not None and contract.realized_volatility_estimate is not None
            else None
        ),
        "delta": contract.delta,
        "spread": base_values.spread,
        "spread_ratio": base_values.spread_ratio,
        **metrics,
    }


def _earnings_event_flag(row: dict[str, Any]) -> bool:
    if not bool(row.get("event_flag")):
        return False
    event_types = {
        item.strip().lower()
        for item in str(row.get("event_types") or "").split(",")
        if item.strip()
    }
    return "earnings" in event_types


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
    put_cash_capacity_fn: Callable[[CandidateContractInput], dict[str, Any]] | None = None,
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
            output=Path(output),
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
            score_weights=resolve_candidate_score_weights(score_weights),
            strategy_family=strategy_family,
            strategy_profile=strategy_profile,
            quiet=bool(quiet),
            risk_policy_version=risk_policy_version,
            quote_snapshot_id=quote_snapshot_id,
        ),
        deps=CandidateScanDependencies(
            compute_metrics_fn=lambda contract: compute_metrics(
                contract,
                now_utc=scan_now_utc,
            ),
            build_row_fn=_build_candidate_row,
            build_hard_constraint_kwargs_fn=(
                put_cash_capacity_fn
                if put_cash_capacity_fn is not None
                else (lambda _contract: {})
            ),
            annualized_return_value_fn=lambda metrics: metrics.get("annualized_net_return_on_cash_basis"),
            annotate_event_risk_fn=lambda df, base_dir, cfg: annotate_candidates_with_event_risk(
                df,
                base_dir=base_dir,
                event_risk_cfg=cfg,
            ),
            print_summary_fn=_print_summary,
            metric_reject_reason_fn=lambda contract: explain_metrics_rejection(
                contract,
                now_utc=scan_now_utc,
            ),
            all_decisions_sink_fn=all_decisions_sink_fn,
            event_reject_flag_fn=_earnings_event_flag,
        ),
        event_risk_cfg=event_risk_cfg,
        base_dir=Path(__file__).resolve().parents[2],
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
    parser.add_argument("--event-risk-enabled", dest="event_risk_enabled", action="store_true", default=None)
    parser.add_argument("--no-event-risk-enabled", dest="event_risk_enabled", action="store_false")
    parser.add_argument("--event-risk-mode", dest="event_risk_mode", type=str, default="warn")
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
