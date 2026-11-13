"""Tool-to-provider contracts for closed-market reference queries and request Delta caps."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json

import pandas as pd
import pytest

from domain.domain.engine.candidate_engine import (
    CandidateCalculationError, calculate_opening_candidate_metrics, evaluate_opening_candidate_policy,
)
from src.application import option_candidates as app, opend_symbol_fetching as fetch
from src.application import opening_quote_evidence as quotes, opend_market_snapshot_fetching as snapshots
from src.application.agent_tool_contracts import AgentToolError
from src.application.option_chain_fetching import FileRateLimiter
from src.application.short_vol_metrics import RealizedVolatilitySnapshot, TermMatchedRVObservation
from src.interfaces.agent import cli


@pytest.fixture
def closed_query(monkeypatch, tmp_path):
    # Sunday HK: Friday's session is the most recent valid pricing date.
    state = {
        "now": datetime(2026, 11, 8, 15, tzinfo=timezone.utc),
        "source": datetime(2026, 11, 6, 8, 8, tzinfo=timezone.utc),
        "market_state": "CLOSED", "rows": {}, "calendar_complete": True,
        "calls": [], "rv_dates": [], "on_close": None, "bundles": [],
    }
    expiry = "2026-11-27"
    symbols = {"HK.TCH261127P380000": ("PUT", 380, -.24), "HK.TCH261127C420000": ("CALL", 420, .23)}
    config = {"_generated": {"market": "hk"}, "templates": {"base": {
        "fetch": {"source": "futu", "host": "fixture.invalid", "port": 11111},
        "sell_put": {"max_abs_delta": .4}, "sell_call": {"max_abs_delta": .4}}},
        "symbols": [{"symbol": "0700.HK", "use": "base"}]}
    original = deepcopy(config)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = state["now"]
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    for module in (fetch, quotes, snapshots):
        monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(app, "_now", lambda: state["now"])
    monkeypatch.setattr(app, "ensure_runtime_config_freshness", lambda *a, **kw: None)
    monkeypatch.setattr(app, "load_runtime_config", lambda **kw: (tmp_path / "config.hk.json", config))
    monkeypatch.setattr(FileRateLimiter, "acquire", lambda *a, **kw: 0.)
    monkeypatch.setattr(fetch, "get_trading_date", lambda market: state["now"].astimezone(quotes._MARKET_TIMEZONES[market]).date())

    class Gateway:
        def get_snapshot(self, codes, *, deadline=None, cancelled=None):
            state["calls"].append(("snapshot", list(codes)))
            rows = []
            for code in codes:
                if code == "HK.00700":
                    row = {"code": code, "last_price": 400, "update_time": state["source"].isoformat(),
                           "sec_status": "NORMAL", "suspension": False}
                else:
                    direction, strike, delta = symbols[code]
                    row = {"code": code, "stock_owner": "HK.00700", "strike_time": expiry,
                           "bid_price": 10, "ask_price": 10.2, "last_price": 10,
                           "bid_vol": 10, "ask_vol": 10, "price_spread": .01,
                           "sec_status": "NORMAL", "suspension": False, "option_valid": True,
                           "option_contract_size": 100, "option_contract_multiplier": 100,
                           "option_delta": delta, "option_implied_volatility": 40,
                           "volume": 100, "option_open_interest": 1000,
                           "update_time": state["source"].isoformat()}
                row.update(state["rows"].get(code, {}))
                rows.append(row)
            return pd.DataFrame(rows)

        def get_market_state(self, codes, *, deadline=None, cancelled=None):
            state["calls"].append(("state", list(codes)))
            return pd.DataFrame([{"code": code, "market_state": state["market_state"]} for code in codes])

        def get_trading_days_with_receipt(self, **kwargs):
            state["calls"].append(("calendar", kwargs))
            start, end = date.fromisoformat(kwargs["start"]), date.fromisoformat(kwargs["end"])
            dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
            return {"retcode": 0, "coverage_complete": state["calendar_complete"], "pagination_complete": True,
                    "page_count": 1, "rows": [{"time": day.isoformat(), "trade_date_type": "WHOLE"}
                    for day in dates if day.weekday() < 5]}

        def get_option_expiration_dates(self, code, *, deadline=None, cancelled=None):
            state["calls"].append(("expirations", code))
            return pd.DataFrame([{"strike_time": expiry}])

        def get_option_chain(self, **kwargs):
            state["calls"].append(("chain", kwargs))
            return pd.DataFrame([{"code": code, "strike_time": expiry, "strike_price": strike,
                "stock_owner": "HK.00700", "stock_type": "DRVT", "option_type": direction,
                "option_standard_type": "STANDARD", "suspension": False, "lot_size": 100}
                for code, (direction, strike, _) in symbols.items()])

        def get_earnings_calendar(self, **kwargs):
            return pd.DataFrame()

        def close(self):
            state["calls"].append(("close", None))
            if state["on_close"]:
                state["on_close"]()

    monkeypatch.setattr(app, "build_ready_futu_quote_gateway", lambda **kw: Gateway())
    real_fetch = fetch.fetch_symbol_request

    def fetch_and_record(request):
        bundle = real_fetch(request)
        state["bundles"].append(bundle)
        return bundle

    monkeypatch.setattr(app, "fetch_symbol_request", fetch_and_record)

    def rv(*args, **kwargs):
        state["rv_dates"].append(kwargs["trading_day"])
        term = TermMatchedRVObservation("term_matched_rv.v1", expiry, "ok", None, .2, 15, 20,
                                       "2026-10-01", "2026-11-05", 21, 20, "a" * 64)
        return RealizedVolatilitySnapshot(status="ok", term_matched={expiry: term})

    monkeypatch.setattr(fetch, "fetch_realized_volatility_snapshot", rv)
    monkeypatch.setattr(app, "current_exchange_rate_snapshot", lambda **kw: {"fixture": True})
    monkeypatch.setattr(app, "rates_for_purpose", lambda *a, **kw: {"HKDCNY": .9})
    state["config"] = config
    state["expiry"] = expiry
    state["symbols"] = symbols
    state["root"] = tmp_path
    yield state
    assert config == original


def run_query(state, queries=None):
    queries = queries or [{"symbol": "0700.HK", "strategy": "short_put", "max_abs_delta": .25},
                          {"symbol": "0700.HK", "strategy": "short_call", "max_abs_delta": .25}]
    with app.option_candidates_context(runtime_root=state["root"]):
        return app.option_candidates_tool({"queries": queries})[0]


def test_closed_weekend_both_directions_use_one_bundle_and_original_source_times(closed_query):
    state = closed_query
    result = run_query(state)
    assert [row["outcome"] for row in result["results"]] == ["found", "found"]
    assert result["completion"] == "complete"
    assert len([call for call in state["calls"] if call[0] == "chain"]) == 1
    assert len([call for call in state["calls"] if call[0] == "calendar"]) == 1
    assert state["rv_dates"] == [date(2026, 11, 6)]
    for row in result["results"]:
        assert row["coverage"]["status"] == "complete"
        assert row["effective_filters"]["max_abs_delta"] == .25
        assert row["effective_filters"]["max_abs_delta_source"] == "request"
        assert row["data_quality"]["mode"] == "closed_market_reference"
        assert row["data_quality"]["reference_date"] == "2026-11-06"
        assert row["data_quality"]["underlying_observed_at"] == state["source"].isoformat()
        assert row["data_quality"]["valid_until"] == "2026-11-09T01:30:00+00:00"
        candidate = row["candidates"][0]
        assert abs(candidate["delta"]) <= .25
        assert candidate["dte"] == 19 and candidate["reference_dte"] == 21
        assert candidate["annualized_net_return"] == candidate["evidence"]["annualized_net_return"]
        assert candidate["annualized_net_return"] == pytest.approx(candidate["period_net_return"] * 365 / 21)
        assert candidate["annualized_net_return"] != pytest.approx(candidate["period_net_return"] * 365 / 19)
        assert candidate["evidence"]["snapshot_received_at"] == state["now"].isoformat()
        assert candidate["evidence"]["underlying_observed_at"] == state["source"].isoformat()
    for raw in state["bundles"][0]["rows"]:
        # The same row cannot enter ordinary live calculations without opt-in.
        with pytest.raises(CandidateCalculationError):
            calculate_opening_candidate_metrics(raw, mode=raw["option_type"], now_utc=state["now"])
        metrics = calculate_opening_candidate_metrics(raw, mode=raw["option_type"], now_utc=state["now"],
                                                     allow_closed_market_reference=True)
        key = "annualized_net_return" if raw["option_type"] == "put" else "annualized_net_premium_return"
        assert metrics[key] == pytest.approx(metrics["period_net_return"] * 365 / 21)


@pytest.mark.parametrize("code,override", [
    ("HK.TCH261127P380000", {"bid_price": 0}),
    ("HK.TCH261127P380000", {"ask_price": 0}),
    ("HK.TCH261127P380000", {"bid_price": None}),
    ("HK.TCH261127P380000", {"option_delta": None}),
    ("HK.TCH261127P380000", {"option_delta": .2}),
    ("HK.TCH261127P380000", {"option_implied_volatility": None}),
    ("HK.TCH261127P380000", {"option_contract_size": None, "option_contract_multiplier": None}),
    ("HK.TCH261127P380000", {"stock_owner": "HK.03690"}),
])
def test_closed_snapshot_gaps_do_not_become_none_or_borrow_other_side(closed_query, code, override):
    closed_query["rows"][code] = override
    result = run_query(closed_query)
    put, call = result["results"]
    assert put["outcome"] == "unable" and put["coverage"]["status"] == "unavailable"
    assert put["coverage"]["unassessed"] == 1
    assert call["outcome"] == "found" and call["coverage"]["status"] == "complete"
    assert result["completion"] == "partial"


@pytest.mark.parametrize("strategy", ["short_put", "short_call"])
def test_request_cap_overrides_config_and_different_caps_share_facts(closed_query, strategy):
    result = run_query(closed_query, [{"symbol": "0700.HK", "strategy": strategy, "max_abs_delta": .25},
                                    {"symbol": "0700.HK", "strategy": strategy, "max_abs_delta": .20},
                                    {"symbol": "0700.HK", "strategy": strategy}])
    assert [row["outcome"] for row in result["results"]] == ["found", "none", "found"]
    assert result["results"][1]["exclusion_summary"] == [{"code": "risk_delta", "count": 1}]
    assert result["results"][2]["effective_filters"]["max_abs_delta"] == .4
    assert result["results"][2]["effective_filters"]["max_abs_delta_source"] == "config"
    assert len([call for call in closed_query["calls"] if call[0] == "chain"]) == 1


@pytest.mark.parametrize("override", [None, True, "0.25", -.1, 1.1, float("nan"), float("inf")])
def test_invalid_delta_rejected_before_provider_io(closed_query, override):
    with pytest.raises(AgentToolError) as caught:
        run_query(closed_query, [{"symbol": "0700.HK", "strategy": "short_put", "max_abs_delta": override}])
    assert caught.value.code == "INPUT_ERROR"
    assert not closed_query["calls"]


@pytest.mark.parametrize("case", ["partial_calendar", "previous_session", "bad_security", "already_open"])
def test_unverifiable_reference_stops_before_chain(closed_query, case):
    if case == "partial_calendar":
        closed_query["calendar_complete"] = False
    elif case == "previous_session":
        closed_query["source"] -= timedelta(days=1)
    elif case == "bad_security":
        closed_query["rows"]["HK.00700"] = {"sec_status": "DELISTED"}
    else:
        closed_query["now"] = datetime(2026, 11, 9, 1, 30, tzinfo=timezone.utc)
    result = run_query(closed_query)
    assert all(row["outcome"] == "unable" for row in result["results"])
    assert not [call for call in closed_query["calls"] if call[0] in {"chain", "expirations"}]


@pytest.mark.parametrize("late", ["stale_receipt", "reopen"])
def test_final_response_rechecks_reference_cutoff_and_snapshot_receipt(closed_query, late):
    def advance():
        closed_query["now"] = (closed_query["now"] + timedelta(seconds=301) if late == "stale_receipt"
                               else datetime(2026, 11, 9, 1, 30, tzinfo=timezone.utc))
    closed_query["on_close"] = advance
    result = run_query(closed_query)
    assert all(row["outcome"] == "unable" and not row["candidates"] for row in result["results"])


def test_reference_annualized_gate_does_not_gain_two_weekend_days(closed_query):
    run_query(closed_query)
    for raw in closed_query["bundles"][0]["rows"]:
        mode = raw["option_type"]
        metrics = calculate_opening_candidate_metrics(raw, mode=mode, now_utc=closed_query["now"],
                                                     allow_closed_market_reference=True)
        period = metrics["period_net_return"]
        # This threshold would pass if Sunday DTE=19 replaced Friday DTE=21.
        threshold = (period * 365 / 21 + period * 365 / 19) / 2
        decision = evaluate_opening_candidate_policy({**raw, **metrics, "decision_now_utc": closed_query["now"].isoformat()},
            mode=mode, min_annualized_return=threshold, check_capacity=False,
            require_earnings_evidence=False, reject_known_earnings=False)
        assert decision["accepted"] is False
        assert "return_annualized" in [item["reason"] for item in decision["rejects"]]


def test_reference_fields_survive_output_budget_without_evidence(closed_query):
    result = run_query(closed_query)
    for row in result["results"]:
        row["candidates"][0]["evidence"]["large_fixture"] = "x" * 20000
    bounded, _, _ = app._bound_response(result, [], {})
    for row in bounded["results"]:
        assert row["candidates"][0]["evidence"] is None
        assert abs(row["candidates"][0]["delta"]) <= row["effective_filters"]["max_abs_delta"]
        assert row["data_quality"]["mode"] == "closed_market_reference"
        assert row["data_quality"]["reference_date"] == "2026-11-06"
        assert row["candidates"][0]["snapshot_received_at"] == closed_query["now"].isoformat()


def test_tool_gateway_json_schema_and_output_show_delta_and_reference(closed_query, capsys):
    payload = {"queries": [{"symbol": "0700.HK", "strategy": "short_put", "max_abs_delta": .25}]}
    with app.option_candidates_context(runtime_root=closed_query["root"]):
        exit_code = cli.main(["run", "--tool", "option_candidates", "--input-json", json.dumps(payload), "--no-local-env-file"])
    assert exit_code == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is True
    result = envelope["data"]["results"][0]
    assert result["outcome"] == "found"
    assert result["candidates"][0]["delta"] == -.24
    assert result["data_quality"]["mode"] == "closed_market_reference"
    assert "休市参考" in result["data_quality"]["notice"]
