from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd


BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_sell_put_scan_writes_candidate_filter_trace(tmp_path: Path) -> None:
    from src.application.scan_sell_put import run_sell_put_scan

    parsed = tmp_path / "input" / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-06-19",
                "contract_symbol": "PASS",
                "currency": "USD",
                "dte": 30,
                "strike": 100,
                "spot": 110,
                "bid": 1.0,
                "ask": 1.2,
                "last_price": 1.1,
                "mid": 1.1,
                "open_interest": 100,
                "volume": 50,
                "implied_volatility": 0.3,
                "delta": -0.2,
                "multiplier": 100,
            },
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-06-19",
                "contract_symbol": "FAIL_LIQUIDITY",
                "currency": "USD",
                "dte": 30,
                "strike": 98,
                "spot": 110,
                "bid": 1.0,
                "ask": 1.2,
                "last_price": 1.1,
                "mid": 1.1,
                "open_interest": 1,
                "volume": 0,
                "implied_volatility": 0.3,
                "delta": -0.2,
                "multiplier": 100,
            },
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-06-19",
                "contract_symbol": "FAIL_METRICS",
                "currency": "USD",
                "dte": 30,
                "strike": 96,
                "spot": 110,
                "bid": 1.0,
                "ask": 1.2,
                "last_price": 1.1,
                "mid": 0.0,
                "open_interest": 100,
                "volume": 50,
                "implied_volatility": 0.3,
                "delta": -0.2,
                "multiplier": 100,
            },
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)

    out_path = tmp_path / "reports" / "nvda_sell_put_candidates.csv"
    out = run_sell_put_scan(
        symbols=["NVDA"],
        input_root=tmp_path / "input",
        output=out_path,
        min_dte=7,
        max_dte=60,
        min_annualized_net_return=0.01,
        min_net_income=1,
        min_open_interest=10,
        min_volume=10,
        max_spread_ratio=1.0,
        quiet=True,
    )

    assert list(out["contract_symbol"]) == ["PASS"]
    trace_rows = _read_jsonl(out_path.parent / "candidate_filter_trace.jsonl")
    rules = {row["rule"] for row in trace_rows}
    assert "candidate_accepted" in rules
    assert "risk_open_interest" in rules
    assert "risk_volume" in rules
    assert "metrics_mid_non_positive" in rules
    assert {row["function"] for row in trace_rows} == {"sell_put"}


def test_candidate_scan_traces_missing_required_data_chain(tmp_path: Path) -> None:
    from src.application.scan_sell_call import run_sell_call_scan

    (tmp_path / "input" / "parsed").mkdir(parents=True)
    out_path = tmp_path / "reports" / "nvda_sell_call_candidates.csv"

    out = run_sell_call_scan(
        symbols=["NVDA"],
        input_root=tmp_path / "input",
        output=out_path,
        avg_cost=100,
        shares=100,
        min_annualized_net_return=0.01,
        quiet=True,
    )

    assert out.empty
    trace_rows = _read_jsonl(out_path.parent / "candidate_filter_trace.jsonl")
    assert trace_rows[0]["function"] == "sell_call"
    assert trace_rows[0]["stage"] == "fetch_visibility"
    assert trace_rows[0]["rule"] == "required_data_missing_call_chain"


def test_sell_put_cash_filter_writes_cash_reserve_trace(tmp_path: Path) -> None:
    from src.application.sell_put_steps import _enrich_and_filter_sell_put_cash
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    out_path = tmp_path / "output_runs" / "run-1" / "accounts" / "lx" / "nvda_sell_put_candidates_labeled.csv"
    out_path.parent.mkdir(parents=True)
    df = pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "contract_symbol": "NVDA_PUT",
                "expiration": "2026-06-19",
                "strike": 100,
                "multiplier": 100,
                "currency": "USD",
            }
        ]
    )

    filtered = _enrich_and_filter_sell_put_cash(
        df_labeled=df,
        symbol="NVDA",
        portfolio_ctx={"cash_by_currency": {"USD": 100.0}},
        exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14)),
        out_path=out_path,
    )

    assert filtered.empty
    trace_rows = _read_jsonl(out_path.parent / "candidate_filter_trace.jsonl")
    assert trace_rows[0]["function"] == "cash_reserve"
    assert trace_rows[0]["account"] == "lx"
    assert trace_rows[0]["run_id"] == "run-1"
    assert trace_rows[0]["rule"] == "usd_cash_insufficient"


def test_candidate_filter_explain_reads_trace_path(tmp_path: Path) -> None:
    from src.application.candidate_filter_trace import (
        append_candidate_filter_trace_rows,
        build_candidate_filter_trace_row,
    )
    from src.application.tool_execution import execute_tool as run_tool

    trace_path = tmp_path / "candidate_filter_trace.jsonl"
    append_candidate_filter_trace_rows(
        trace_path,
        [
            build_candidate_filter_trace_row(
                run_id="run-1",
                account="lx",
                symbol="NVDA",
                function="cash_reserve",
                mode="put",
                status="post_filtered",
                stage="post_filter",
                rule="usd_cash_insufficient",
                metric_value=10000,
                threshold=100,
                message="cash not enough",
                evidence_path="nvda_sell_put_candidates_labeled.csv",
            )
        ],
    )

    out = run_tool(
        "candidate_filter_explain",
        {"trace_path": str(trace_path), "account": "lx", "symbol": "NVDA"},
    )

    assert out["ok"] is True
    assert out["data"]["trace_count"] == 1
    cash = next(item for item in out["data"]["functions"] if item["function"] == "cash_reserve")
    assert cash["status"] == "post_filtered"
    assert cash["reason_counts"]["usd_cash_insufficient"] == 1
    assert out["meta"]["source_files"][0]["rows"] == 1
