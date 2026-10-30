from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import json
import math
from pathlib import Path

import pandas as pd
import pytest

from src.application import short_vol_metrics as rv


DAY = date(2026, 10, 30)
NEAR, JAN, FEB, MAR = "2026-12-30", "2027-01-28", "2027-02-25", "2027-03-30"
HOLIDAYS = {date(2027, 1, 1), date(2027, 2, 8), date(2027, 2, 9), date(2027, 3, 26), date(2027, 3, 29)}


class CalendarQuoteFixture:
    def __init__(self):
        self.calendar_calls = []
        self.history_calls = []
        self.future_rows = []
        self.future_failure = None
        self.prices = {}
        value = 100.0
        for offset in range(-240, 0):
            day = DAY + timedelta(days=offset)
            if day.weekday() < 5:
                value *= 1.008 if len(self.prices) % 2 == 0 else 0.995
                self.prices[day] = round(value, 8)

    def get_trading_days(self, **kwargs):
        self.calendar_calls.append(kwargs)
        start, end = date.fromisoformat(kwargs["start"]), date.fromisoformat(kwargs["end"])
        if start.year == 2027:
            if self.future_failure:
                raise self.future_failure
            return list(self.future_rows)
        rows = []
        while start <= end:
            if start.weekday() < 5:
                rows.append({"time": start.isoformat(), "trade_date_type": "WHOLE"})
            start += timedelta(days=1)
        return rows

    def request_history_kline(self, **kwargs):
        self.history_calls.append(kwargs)
        start, end = date.fromisoformat(kwargs["start"]), date.fromisoformat(kwargs["end"])
        return {
            "data": [
                {"code": "HK.00123", "time_key": day.isoformat(), "close": price}
                for day, price in sorted(self.prices.items())
                if start <= day <= end
            ],
            "page_req_key": None,
        }


def fetch(gateway, root, expirations=(NEAR, JAN), **kwargs):
    return rv.fetch_realized_volatility_snapshot(
        gateway,
        market="HK",
        underlier_code="HK.00123",
        trading_day=DAY,
        expirations=expirations,
        base_dir=root,
        **kwargs,
    )


def test_cross_year_exact_session_rv_oracle_and_earlier_expiry_unchanged(tmp_path):
    control = fetch(CalendarQuoteFixture(), tmp_path / "control", (NEAR,))
    gateway = CalendarQuoteFixture()
    result = fetch(gateway, tmp_path / "mixed", (NEAR, JAN, FEB, MAR, "2028-01-28"))
    assert result.term_matched[NEAR] == control.term_matched[NEAR]
    for expiry in (JAN, FEB, MAR):
        end = date.fromisoformat(expiry)
        expected = [
            DAY + timedelta(days=i)
            for i in range((end - DAY).days + 1)
            if (DAY + timedelta(days=i)).weekday() < 5 and DAY + timedelta(days=i) not in HOLIDAYS
        ]
        term = result.term_matched[expiry]
        assert term.status == "ok" and term.remaining_sessions == len(expected)
        closes = list(gateway.prices.values())[-(len(expected) + 1) :]
        returns = [math.log(b / a) for a, b in zip(closes, closes[1:])]
        mean = sum(returns) / len(returns)
        oracle = math.sqrt(sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)) * math.sqrt(252)
        assert term.term_matched_rv == pytest.approx(oracle, abs=0.0000006)
    assert result.term_matched["2028-01-28"].reason == "calendar_coverage_incomplete"
    evidence = result.trading_calendar_evidence
    assert evidence["status"] == "ok" and evidence["coverage_end"] == "2027-12-31"
    assert evidence["future_opend"]["status"] == "empty"
    official = evidence["sources"][1]
    assert official["scheduled_session_count"] == 248 and official["session_type_granularity"] == "full_or_half"
    assert (
        official["source_document"]["pdf_sha256"] == "dce85f7ecb6b130341de740868a88a9fdc5f098d4445e1b5980147af528e95d0"
    )


@pytest.mark.parametrize(
    "rows",
    [
        [{"time": "2027-01-04", "trade_date_type": "NON_TRADING"}],
        [{"time": "2027-02-05", "trade_date_type": "WHOLE"}],
        [{"time": "2027-01-04", "trade_date_type": "N/A"}],
        [{"time": "2027-01-04", "trade_date_type": "WHOLE"}, {"time": "2027-01-04", "trade_date_type": "MORNING"}],
    ],
)
def test_future_conflicts_preserve_current_year_and_typed_cache(tmp_path, rows):
    gateway = CalendarQuoteFixture()
    gateway.future_rows = rows
    cache = {}
    result = fetch(gateway, tmp_path, (NEAR, FEB), facts_cache=cache)
    assert result.term_matched[NEAR].status == "ok"
    assert result.term_matched[FEB].reason == "calendar_source_conflict"
    assert result.trading_calendar_evidence["status"] == "ok"
    assert cache["calendar_future"]["rows"] == rows
    assert all(row["time"].startswith("2026") for row in cache["calendar"]["rows"])


@pytest.mark.parametrize("kind", ["MORNING", "AFTERNOON"])
def test_official_half_days_match_full_half_granularity(tmp_path, kind):
    gateway = CalendarQuoteFixture()
    gateway.future_rows = [
        {"time": "2027-02-05", "trade_date_type": kind},
        {"time": "2027-02-08", "trade_date_type": "NON_TRADING"},
    ]
    result = fetch(gateway, tmp_path, (FEB,))
    assert result.term_matched[FEB].status == "ok"


def test_corrupt_source_failure_and_future_query_failure_remain_explicit(tmp_path, monkeypatch):
    gateway = CalendarQuoteFixture()
    gateway.future_failure = OSError("synthetic")
    result = fetch(gateway, tmp_path / "provider")
    assert result.term_matched[NEAR].status == "ok"
    assert result.term_matched[JAN].reason == "trading_calendar_error:OSError"
    assert result.trading_calendar_evidence["future_opend"]["status"] == "error"
    path = tmp_path / "bad-source.json"
    path.write_text("{}")
    monkeypatch.setattr(rv, "_HKEX_2027_PATH", path)
    result = fetch(CalendarQuoteFixture(), tmp_path / "source")
    assert result.term_matched[NEAR].status == "ok"
    assert result.term_matched[JAN].reason == "calendar_source_integrity_error"
    assert result.trading_calendar_evidence["coverage_end"] == "2026-12-31"


def test_run_cache_recomputes_provenance_across_reuse_extension_and_year(tmp_path):
    cache = {}
    gateway = CalendarQuoteFixture()
    near = fetch(gateway, tmp_path, (NEAR,), facts_cache=cache)
    january = fetch(gateway, tmp_path, (NEAR, JAN), facts_cache=cache)
    repeated = fetch(gateway, tmp_path, (NEAR, JAN), facts_cache=cache)
    march = fetch(gateway, tmp_path, (NEAR, JAN, MAR), facts_cache=cache)
    assert near.term_matched[NEAR] == january.term_matched[NEAR] == march.term_matched[NEAR]
    assert repeated.trading_calendar_evidence == january.trading_calendar_evidence
    assert len(january.trading_calendar_evidence["sources"]) == len(march.trading_calendar_evidence["sources"]) == 2
    assert cache["calendar_future"]["rows"] == []
    # Entering 2027 must re-query observed sessions, not reuse the prior-year supplement.
    rv.fetch_realized_volatility_snapshot(
        gateway,
        market="HK",
        underlier_code="HK.00123",
        trading_day=date(2027, 1, 4),
        expirations=(FEB,),
        base_dir=tmp_path,
        facts_cache=cache,
    )
    assert cache["calendar"]["trading_year"] == 2027


def test_real_quote_adapter_uses_only_mocked_calendar_and_qfq_operations(tmp_path, monkeypatch):
    from src.infrastructure import futu_gateway

    fixture = CalendarQuoteFixture()
    forbidden = {"broker": 0, "network": 0}

    def deny(kind):
        def blocked(*args, **kwargs):
            forbidden[kind] += 1
            raise AssertionError(f"forbidden {kind}")

        return blocked

    class QuoteSDK:
        def request_trading_days(self, **kwargs):
            return 0, pd.DataFrame(fixture.get_trading_days(**kwargs))

        def request_history_kline(self, **kwargs):
            result = fixture.request_history_kline(**kwargs)
            return 0, pd.DataFrame(result["data"]), None

    monkeypatch.setattr(futu_gateway, "build_futu_trade_context", deny("broker"))
    monkeypatch.setattr(futu_gateway, "build_futu_quote_context", deny("network"))
    gateway = futu_gateway.build_futu_gateway(host="127.0.0.1", port=19999)
    gateway.backend._quote_client = QuoteSDK()
    result = fetch(gateway, tmp_path)
    assert result.term_matched[JAN].status == "ok" and len(fixture.calendar_calls) == 2
    assert fixture.history_calls and all(call["autype"] == "QFQ" for call in fixture.history_calls)
    assert forbidden == {"broker": 0, "network": 0}


def test_future_cancellation_propagates_instead_of_becoming_a_gap(tmp_path):
    fixture = CalendarQuoteFixture()
    cancelled = False
    original = fixture.get_trading_days

    def calendar(**kwargs):
        nonlocal cancelled
        if kwargs["start"].startswith("2027"):
            cancelled = True
        return original(**kwargs)

    fixture.get_trading_days = calendar
    with pytest.raises(TimeoutError, match="cancelled"):
        fetch(fixture, tmp_path, cancelled=lambda: cancelled)


def test_expiry_reason_projection_and_both_renderer_paths_deduplicate_only_matching_scope():
    from src.application.opening_candidate_snapshot import candidate_universe_summary
    from src.application.daily_decision_brief_renderer import (
        _fixed_report_error_reminders,
        _strategy_data_gap_reminders,
    )

    scopes = [
        {
            "scope": "strategy",
            "symbol": "0123.HK",
            "strategy_mode": "put",
            "status": "completed",
            "reason_code": "no_candidate",
        }
    ]
    for contract, expiry, reason in (
        ("test-a", JAN, "calendar_source_conflict"),
        ("test-b", JAN, "calendar_source_conflict"),
        ("test-c", FEB, "qfq_history_session_gap"),
        ("test-d", JAN, "calendar_coverage_incomplete"),
    ):
        scopes.append(
            {
                "scope": "contract",
                "symbol": "0123.HK",
                "strategy_mode": "put",
                "status": "rejected",
                "contract_symbol": contract,
                "expiration": expiry,
                "reason_codes": ["input_invalid"],
                "rejects": [
                    {
                        "reason": "input_invalid",
                        "metric_value": {
                            "reason_code": "term_matched_rv_unavailable",
                            "metric_value": {"status": "data_unavailable", "reason": reason},
                        },
                    }
                ],
            }
        )
    summary = candidate_universe_summary({"scope_results": scopes})
    gaps = [
        {
            **scope,
            "scope": "contract",
            "reason": "opening_candidate_strategy_partial_data",
            "strategy_family": "sell_put",
            "severity": "warning",
            "actionable": False,
        }
        for scope in summary["affected_scopes"]
    ]
    brief = {"data_gaps": gaps}
    normal = _strategy_data_gap_reminders(brief)
    degraded = _fixed_report_error_reminders(brief, strategy_failure_items=[])
    assert normal == degraded and len(normal) == 3
    assert sum(JAN in text and "来源冲突" in text for text in normal) == 1
    assert any(FEB in text and "前复权" in text for text in normal)
    assert any(JAN in text and "覆盖不足" in text for text in normal)
