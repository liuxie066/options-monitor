from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from domain.domain.candidate_defaults import CandidateLiquidityDefaults, CandidateWindowDefaults
from src.application.combo_yield_steps import run_combo_yield_scan_and_summarize
from src.application.yield_enhancement_config import derive_yield_enhancement_policy
from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates


def _candidate(**overrides) -> dict:
    row = {
        "symbol": "NVDA",
        "expiration": "2026-08-21",
        "dte": 35,
        "contract_symbol": "NVDA260821P00100000",
        "multiplier": 100,
        "currency": "USD",
        "strike": 100.0,
        "spot": 110.0,
        "bid": 2.0,
        "ask": 2.1,
        "mid": 2.05,
        "open_interest": 500,
        "volume": 50,
        "delta": -0.2,
        "event_flag": False,
        "event_source_status": "ok",
    }
    row.update(overrides)
    return row


def _run(
    tmp_path: Path,
    *,
    candidates: list[dict],
    find_pairs_fn,
):
    report_dir = tmp_path / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    captured: dict[str, pd.DataFrame] = {}

    def run_put_scan_fn(**kwargs):
        pd.DataFrame(candidates).to_csv(kwargs["output"], index=False)

    def label_put_candidates_fn(_base, input_path, output_path):
        Path(output_path).write_bytes(Path(input_path).read_bytes())

    def capture_pairs(**kwargs):
        captured["df"] = kwargs["df_candidates"].copy()
        return find_pairs_fn(**kwargs)

    policy = derive_yield_enhancement_policy({"enabled": True}, {"strategy": "return_first"})
    run_combo_yield_scan_and_summarize(
        base=tmp_path,
        sym="NVDA",
        symbol="NVDA",
        symbol_lower="nvda",
        symbol_cfg={"symbol": "NVDA", "combo_yield": {"enabled": True}},
        yield_enhancement_cfg={"enabled": True},
        yield_sp={"strategy": "return_first", "reject_event_risk": True, "event_source_fail_closed": True},
        yield_enhancement_policy=policy,
        df_sell_put_labeled=pd.DataFrame(),
        sell_put_labeled_path=report_dir / "nvda_sell_put_candidates_labeled.csv",
        required_data_dir=tmp_path / "required_data",
        report_dir=report_dir,
        yield_window=CandidateWindowDefaults(min_dte=7, max_dte=60),
        liquidity=CandidateLiquidityDefaults(),
        event_risk={"enabled": True, "mode": "warn"},
        exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14)),
        portfolio_ctx=None,
        top_n=3,
        is_scheduled=True,
        run_put_scan_fn=run_put_scan_fn,
        label_put_candidates_fn=label_put_candidates_fn,
        find_pairs_fn=capture_pairs,
        select_pairs_fn=lambda df: df,
        cash_filter_put_candidates_fn=None,
    )
    trace = [
        json.loads(line)
        for line in (report_dir / "candidate_filter_trace.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return captured["df"], trace


def test_combo_yield_event_gate_fails_closed_without_iv_rv_underwriting(tmp_path: Path) -> None:
    captured, trace = _run(
        tmp_path,
        candidates=[
            _candidate(event_source_status="error"),
            _candidate(contract_symbol="NVDA260821P00095000", strike=95.0, event_flag=True),
        ],
        find_pairs_fn=lambda **_kwargs: pd.DataFrame(),
    )

    assert captured.empty
    rules = {row["rule"] for row in trace if row["stage"] == "combo_event_risk"}
    assert rules == {"event_source_unavailable", "event_risk_within_expiry"}
    assert "combo_yield_put_event_filtered" in {row["rule"] for row in trace}


def test_combo_yield_writes_pair_rejection_aggregates_to_trace(tmp_path: Path) -> None:
    def rejected_pairs(**_kwargs):
        out = pd.DataFrame()
        out.attrs["reject_counts"] = {"min_net_credit_retention": 3}
        return out

    captured, trace = _run(
        tmp_path,
        candidates=[_candidate()],
        find_pairs_fn=rejected_pairs,
    )

    assert len(captured) == 1
    pair_rows = [row for row in trace if row["stage"] == "combo_pair_filter"]
    assert len(pair_rows) == 1
    assert pair_rows[0]["rule"] == "min_net_credit_retention"
    assert pair_rows[0]["metric_value"] == 3
