from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
import json
from pathlib import Path

import pandas as pd
import pytest

from candidate_evidence_helpers import earnings_evidence, delta_evidence
from test_candidate_engine_phase2_contract import _opening_row
from domain.domain.engine import calculate_opening_candidate_metrics, rank_candidate_rows
from domain.domain.insurance_underwriting import (
    InsuranceUnderwritingConfig,
    rank_underwriting_candidates,
    underwriting_fields,
)
from src.application import earnings_calendar as calendar
from src.application import opening_candidate_snapshot as snapshot
from src.application import scan_sell_call
from src.application.covered_call_strategy_risk import enrich_and_filter_covered_call_underwriting
from src.application.report_summaries import summarize_sell_call
from src.application.sell_call_steps import run_sell_call_scan_and_summarize
from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates


NOW = datetime(2026, 10, 16, 15, tzinfo=timezone.utc)
EXPIRATION = "2026-11-13"
CONVERTER = CurrencyConverter(ExchangeRates(usd_per_cny=0.14))


def _quote(symbol="NVDA", strike=110.0, spot=100.0):
    return _opening_row(
        mode="call", symbol=symbol, stock_owner=f"US.{symbol}",
        contract_symbol=f"{symbol}261113C{int(strike * 1000):08d}",
        strike=strike, spot=spot, bid=2.0, ask=2.1, price_tick=0.01,
        expiration=EXPIRATION, dte=28, open_interest=100, volume=20,
        spot_observed_at_utc=NOW.isoformat(),
        **delta_evidence(mode="call", observed_at=NOW.isoformat()),
    )


def _metrics(symbol="NVDA", strike=110.0, spot=100.0, cost=105.0):
    raw = _quote(symbol, strike, spot)
    return {
        **raw,
        **calculate_opening_candidate_metrics(raw, mode="call", avg_cost=cost, now_utc=NOW),
        **earnings_evidence(expiration=EXPIRATION, market_date=NOW.date().isoformat()),
        "avg_cost": cost,
    }


def _write_calendar(root: Path):
    payload = {
        "schema_version": calendar.EARNINGS_CALENDAR_SCHEMA_VERSION,
        "source": "opend", "policy_version": calendar.EARNINGS_NEAR_EXPIRY_POLICY_VERSION,
        "window_days": calendar.EARNINGS_NEAR_EXPIRY_WINDOW_DAYS,
        "market": "US", "scan_date": NOW.date().isoformat(), "scan_at_utc": NOW.isoformat(),
        "coverage_start": NOW.date().isoformat(), "coverage_end": EXPIRATION,
        "status": "ready", "absence_authoritative": True, "events": [],
        "expirations_by_underlier": {f"US.{s}": [EXPIRATION] for s in ("NVDA", "MSFT")},
        "intervals": [
            {"start": start.isoformat(), "end": end.isoformat(), "status": "ok",
             "reason_code": None, "error": None, "observed_at_utc": NOW.isoformat(),
             "row_count": 0, "result_hash": calendar._payload_hash([])}
            for start, end in calendar.earnings_calendar_intervals(NOW.date(), date.fromisoformat(EXPIRATION))
        ],
    }
    payload["evidence_by_underlier"] = {
        code: {EXPIRATION: calendar._project_earnings_for_expiry(
            payload, underlier_code=code, expiration=EXPIRATION,
        )}
        for code in payload["expirations_by_underlier"]
    }
    payload["snapshot_hash"] = calendar._payload_hash(payload)
    calendar.validate_earnings_calendar_snapshot(payload, expected_market="US")
    directory = root / "earnings_calendar"
    directory.mkdir()
    (directory / "US.json").write_text(json.dumps(payload), encoding="utf-8")


def test_real_cc_scan_preserves_spot_distance_through_snapshot(tmp_path, monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.astimezone(tz)

    monkeypatch.setattr(scan_sell_call, "datetime", FixedDatetime)
    _write_calendar(tmp_path)
    finals, decisions = [], []
    for symbol, strike, cost in (("NVDA", 110.0, 105.0), ("MSFT", 105.0, 80.0)):
        summary = run_sell_call_scan_and_summarize(
            symbol=symbol, symbol_cfg={}, cc={"strategy": "insurance_underwriting"},
            required_data_dir=tmp_path, required_data_frame=pd.DataFrame([_quote(symbol, strike)]),
            stock={"shares": 100, "can_sell_qty": 100, "avg_cost": cost},
            portfolio_ctx={"portfolio_source_name": "futu", "capacity_authority": {"status": "available"}},
            locked_shares_status="available", locked_shares_by_symbol={},
            locked_shares_unavailable_by_symbol={}, exchange_rate_converter=CONVERTER,
            final_candidates_sink_fn=lambda mode, rows: finals.extend(rows),
            candidate_decisions_sink_fn=lambda mode, rows: decisions.extend(rows),
        )
        assert summary["candidate_count"] == 1
    assert len(decisions) == 2
    assert all(item["opening_decision"]["accepted"] for item in decisions)
    assert finals[0]["period_net_premium_return"] == finals[1]["period_net_premium_return"]
    assert all(pd.isna(row["symbol_concentration_after"]) for row in finals)
    assert [row["effective_min_strike"] for row in finals] == pytest.approx([107.1, 100.0])

    # Only bytes are isolated: the owner still hashes, ranks, validates and reads back.
    stored = {}

    def write_bytes(*, base, run_id, account, name, payload):
        assert base.resolve() == tmp_path.resolve()
        key = (run_id, account, name)
        if key in stored:
            assert stored[key] == payload
        stored[key] = payload

    def read_bytes(*, base, run_id, account, name):
        assert base.resolve() == tmp_path.resolve()
        return stored[(run_id, account, name)]

    monkeypatch.setattr(snapshot, "write_account_run_state_bytes_once_safely", write_bytes)
    monkeypatch.setattr(snapshot, "read_account_run_state_bytes_safely", read_bytes)
    (tmp_path / "output_runs" / "synthetic618" / "accounts" / "synthetic").mkdir(parents=True)
    sealed = snapshot.seal_opening_candidate_snapshot(
        base=tmp_path, run_id="synthetic618", account="synthetic", market="US",
        physical_account={"status": "available", "logical_account": "synthetic",
                          "futu_account_id": "synthetic-no-broker", "trd_env": "REAL", "market": "US", "source": "opend"},
        account_config_sha256="a" * 64, strategy_policy_sha256="b" * 64,
        dependencies=[snapshot.dependency_from_hash(kind=k, sha256="c" * 64)
                      for k in ("required_data", "portfolio", "ledger", "fx", "earnings_rv")],
        scan_statuses=[{"symbol": s, "strategy_mode": "call", "status": "completed"}
                       for s in ("NVDA", "MSFT")],
        final_candidates={"call": finals}, candidate_evaluations={"call": decisions},
        run_mode={"scan_mode": "standard", "executable": True}, sealed_at=NOW,
    )
    loaded = snapshot.load_opening_candidate_snapshot(
        base=tmp_path, run_id="synthetic618", account="synthetic", require_current_contract=True,
    )
    assert loaded == sealed
    ranked = snapshot.ranked_opening_candidates(loaded, mode="call")
    assert [row["facts"]["symbol"] for row in ranked] == ["NVDA", "MSFT"]
    for rows in (finals, [d["normalized_input"] for d in decisions], [r["facts"] for r in ranked]):
        by_symbol = {row["symbol"]: row for row in rows}
        for symbol, margin in (("NVDA", 0.10), ("MSFT", 0.05)):
            assert by_symbol[symbol]["strike_upside_margin_pct"] == margin
            assert by_symbol[symbol]["strike_above_spot_pct"] == margin
    assert summarize_sell_call(pd.DataFrame(finals), "NVDA")["top_contract"] == f"{EXPIRATION} 110C"


@pytest.mark.parametrize("minimum,cost,multiplier,strike,floor,accepted", [
    (108.0, 80.0, 1.02, 110.0, 108.0, True),
    (100.0, 80.0, 1.02, 110.0, 100.0, True),
    (90.0, 80.0, 1.02, 110.0, 100.0, True),
    (None, 105.0, 1.02, 110.0, 107.1, True),
    (None, 100.0, 1.20, 120.0, 120.0, True),
    (108.0, 80.0, 1.02, 108.0, 108.0, True),
    (108.0, 80.0, 1.02, 107.0, 108.0, False),
    (None, 105.0, 1.02, 106.0, 107.1, False),
    (None, 80.0, 1.02, 121.0, 100.0, False),
])
def test_cc_floor_gate_does_not_redefine_spot_distance(minimum, cost, multiplier, strike, floor, accepted):
    row = _metrics(strike=strike, cost=cost)
    captured = []
    filtered = enrich_and_filter_covered_call_underwriting(
        df_labeled=pd.DataFrame([row]), symbol="NVDA", portfolio_ctx=None,
        sell_call_cfg={"strategy": "insurance_underwriting", "min_strike": minimum,
                       "min_strike_cost_multiplier": multiplier},
        exchange_rate_converter=CONVERTER, decision_sink_fn=captured.extend,
    )
    assert len(filtered) == int(accepted)
    decision = captured[0]
    assert decision["opening_decision"]["accepted"] is accepted
    assert decision["normalized_input"]["effective_min_strike"] == pytest.approx(floor)
    assert decision["normalized_input"]["strike_upside_margin_pct"] == row["strike_above_spot_pct"]
    if not accepted:
        assert decision["opening_decision"]["rejects"][0]["reason"] == "hard_strike"


def test_repeat_underwriting_keeps_canonical_precision_and_inputs():
    rows = [_metrics(), _metrics("MSFT", 105.0, cost=80.0)]
    rows[0]["effective_min_strike"] = 107.1
    rows[1]["effective_min_strike"] = 100.0
    original = deepcopy(rows)
    cfg = InsuranceUnderwritingConfig()
    for _ in range(2):
        for row in rows:
            fields = underwriting_fields(row, mode="call", cfg=cfg)
            assert "strike_upside_margin_pct" not in fields
            row.update(fields)
        rows = rank_underwriting_candidates(rows, mode="call", cfg=cfg)
        assert [r["symbol"] for r in rows] == ["NVDA", "MSFT"]
        assert [r["strike_upside_margin_pct"] for r in rows] == [0.1, 0.05]
    ranked = rank_underwriting_candidates(original, mode="call", cfg=cfg)
    assert original == [{**_metrics(), "effective_min_strike": 107.1},
                        {**_metrics("MSFT", 105.0, cost=80.0), "effective_min_strike": 100.0}]
    assert ranked[0] is not original[0]
    precise = _metrics(strike=120.0, spot=110.0, cost=100.0)
    assert precise["strike_upside_margin_pct"] == 0.0909090909
    assert rank_underwriting_candidates([precise], mode="call", cfg=cfg)[0]["strike_upside_margin_pct"] == 0.0909090909


@pytest.mark.parametrize("margin,expected", [(None, "A"), ("invalid", "A"), (float("nan"), "A"), (float("inf"), "A"), (0.0, "B")])
def test_underwriting_preserves_existing_margin_fallback(margin, expected):
    rows = [
        {"symbol": "A", "contract_symbol": "A", "period_net_premium_return": .02,
         "strike_upside_margin_pct": margin, "strike_above_spot_pct": .10},
        {"symbol": "B", "contract_symbol": "B", "period_net_premium_return": .02,
         "strike_upside_margin_pct": .05, "strike_above_spot_pct": .05},
    ]
    assert rank_candidate_rows(rows, mode="call")[0]["symbol"] == expected
    assert rank_underwriting_candidates(rows, mode="call", cfg=InsuranceUnderwritingConfig())[0]["symbol"] == expected
