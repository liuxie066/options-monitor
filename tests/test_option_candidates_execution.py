"""Offline execution-contract tests; domain and earnings projection stay real."""
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
import threading
from types import SimpleNamespace

import pytest

from src.application import earnings_calendar, option_candidates as app, read_execution
from src.application.opening_quote_evidence import normalize_underlier_observation
from src.application.tool_execution import execute_tool
from src.infrastructure import exchange_rates
from test_candidate_engine_phase2_contract import _opening_row


NOW = datetime(2026, 9, 30, 4, tzinfo=timezone.utc)


def _query(symbol="NVDA", strategy="short_put", **kwargs):
    return {"symbol": symbol, "strategy": strategy, "dte": [1, 30], **kwargs}


@pytest.fixture
def execution(monkeypatch, tmp_path):
    state = SimpleNamespace(
        now=NOW, monotonic=100.0, root=tmp_path, dtes=(6, 13),
        market_state="MORNING", row_overrides={}, bundle_overrides={}, missing_meta=set(),
        prepares=[], fetches=[], fx_calls=[], gateways=[], on_earnings=None, on_close=None,
        events=[
            {"security": "US.NVDA", "earnings_date": "2026-10-02"},
            {"security": "US.MSFT", "earnings_date": "2026-10-03"},
            {"security": "HK.00700", "earnings_date": "2026-10-04"},
        ],
    )
    clock = SimpleNamespace(monotonic=lambda: state.monotonic)
    monkeypatch.setattr(app, "time", clock)
    monkeypatch.setattr(read_execution, "time", clock)
    monkeypatch.setattr(earnings_calendar, "monotonic", clock.monotonic)
    monkeypatch.setattr(app, "_now", lambda: state.now)

    def unexpected(*args, **kwargs):
        pytest.fail("execution test reached an unmocked external I/O boundary")

    monkeypatch.setattr(app, "load_runtime_config", unexpected)
    monkeypatch.setattr(exchange_rates.urllib_request, "urlopen", unexpected)

    def prepare(query, context, now):
        state.prepares.append(dict(query))
        assert context.runtime_root == tmp_path
        resolver = (app.resolve_sell_put_underwriting_config if query["strategy"] == "short_put"
                    else app.resolve_covered_call_underwriting_config)
        policy = resolver({})
        policy = replace(policy, min_dte=query["dte"][0], max_dte=query["dte"][1])
        if "strike" in query:
            policy = replace(policy, min_strike=query["strike"], max_strike=query["strike"])
        return app.PreparedQuery(
            dict(query), policy,
            {"strike": query.get("strike"), "min_strike": policy.min_strike, "max_strike": policy.max_strike,
             "dte": [policy.min_dte, policy.max_dte], "strike_source": "request" if "strike" in query else "config",
             "dte_source": "request"},
            (f"{query['market'].lower()}.fixture.invalid", 11111),
            earnings_calendar.earnings_calendar_scan_date(query["market"], now),
        )

    class Gateway:
        def __init__(self, host, port):
            self.endpoint = (host, port)
            self.calls = []
            self.close_calls = 0
            self.closed = threading.Event()

        def event_rows(self, request):
            return [dict(row) for row in state.events
                    if row["security"].startswith(request["market"] + ".")
                    and request["begin_date"] <= row["earnings_date"] <= request["end_date"]]

        def get_earnings_calendar(self, **kwargs):
            self.calls.append(dict(kwargs))
            if state.on_earnings is not None:
                return state.on_earnings(self, kwargs)
            return self.event_rows(kwargs)

        def close(self):
            self.close_calls += 1
            if state.on_close is not None:
                state.on_close(self)
            self.closed.set()

    def gateway(*, host, port, **kwargs):
        result = Gateway(host, port)
        state.gateways.append(result)
        return result

    def fetch(request):
        state.fetches.append(request)
        market = "HK" if request.symbol.endswith(".HK") else "US"
        code = app.calibrate_symbol(request.symbol).futu_code
        rows = []
        for mode in request.option_types.split(","):
            for dte in state.dtes:
                expiry = (NOW.date() + timedelta(days=dte)).isoformat()
                strike = 100 if mode == "put" else 120
                contract_root = "HK.TCH" if market == "HK" else code
                contract = f"{contract_root}{date.fromisoformat(expiry):%y%m%d}{mode[0].upper()}{strike * 1000}"
                row = _opening_row(
                    mode=mode, currency="HKD" if market == "HK" else "USD",
                    symbol=request.symbol, stock_owner=code, expiration=expiry, dte=dte,
                    contract_symbol=contract, strike=strike, bid=3.0, ask=3.1,
                    snapshot_received_at_utc=NOW.isoformat(), spot_observed_at_utc=NOW.isoformat(),
                    max_new_contracts=0, covered_contracts_available=0,
                )
                row.update(state.row_overrides)
                rows.append(row)
        observation = normalize_underlier_observation(
            code=code, market=market, now_utc=NOW,
            snapshot_row={"code": code, "last_price": 110, "update_time": NOW.isoformat(),
                          "sec_status": "NORMAL", "suspension": False},
            market_state_row={"code": code, "market_state": state.market_state},
        ).to_dict()
        expirations = sorted({row["expiration"] for row in rows})
        meta = {"source_outcome": "success_rows" if rows else "success_empty",
                "underlier_observation": observation, "expiration_enumeration_complete": True,
                "query_scope_validated": True, "available_expirations": expirations,
                "expiration_statuses": {expiry: "fetched" for expiry in expirations}}
        meta.update(state.bundle_overrides)
        for field in state.missing_meta:
            meta.pop(field, None)
        return {"underlier_code": code, "rows": rows, "meta": meta}

    def fx(**kwargs):
        state.fx_calls.append(kwargs)
        return {"schema_version": 2, "pairs": {
            pair: {"rate": rate, "source": "tencent_quote", "quote_at_utc": NOW.isoformat(),
                   "observed_at_utc": NOW.isoformat()}
            for pair, rate in (("USDCNY", 7.2), ("HKDCNY", 0.92))
        }}

    def execute(queries, **context):
        with app.option_candidates_context(runtime_root=tmp_path, **context):
            return execute_tool("option_candidates", {"queries": queries})

    state.execute = execute
    monkeypatch.setattr(app, "_prepare", prepare)
    monkeypatch.setattr(app, "build_ready_futu_quote_gateway", gateway)
    monkeypatch.setattr(app, "fetch_symbol_request", fetch)
    monkeypatch.setattr(app, "current_exchange_rate_snapshot", fx)
    return state


def _results(response):
    assert response["ok"], response
    assert response["error"] is None
    assert response["tool_name"] == "option_candidates"
    assert response["data"]["account_checks"] == "not_evaluated"
    json.dumps(response, allow_nan=False)
    return response["data"]["results"]


def test_execute_found_batch_reuses_symbol_group_and_one_fx_snapshot(execution):
    response = execution.execute([_query(), _query(strategy="short_call"), _query(strike=100)])
    results = _results(response)
    assert response["data"]["completion"] == "complete"
    assert [row["query_index"] for row in results] == [0, 1, 2]
    assert [row["strategy"] for row in results] == ["short_put", "short_call", "short_put"]
    for result in results:
        assert result["outcome"] == "found"
        assert result["coverage"] == {"status": "complete", "enumeration_complete": True,
                                      "discovered": 2, "evaluated": 2, "matched": 2, "excluded": 0, "unassessed": 0}
        assert result["displayed"] == 2
        assert result["earnings_coverage"] == "complete"
        for candidate in result["candidates"]:
            assert candidate["net_premium"] > 0
            assert candidate["period_net_return"] > 0
            assert all(item["status"] == "pass" for item in candidate["evidence"]["conditions"])
            assert candidate["earnings"]["events"] == [{
                "date": "2026-10-02", "days_before_expiry": candidate["dte"] - 2,
            }]
    # Reused facts retain each query's independent strike-condition evidence.
    assert [{k: v for k, v in row.items() if k != "evidence"} for row in results[0]["candidates"]] == [
        {k: v for k, v in row.items() if k != "evidence"} for row in results[2]["candidates"]
    ]
    assert results[0]["effective_filters"]["strike_source"] == "config"
    assert results[2]["effective_filters"]["strike_source"] == "request"
    assert len(execution.fetches) == len(execution.fx_calls) == len(execution.gateways) == 1
    request = execution.fetches[0]
    assert set(request.option_types.split(",")) == {"put", "call"}
    assert len(request.query_filters) == 3
    assert request.query_cache is not None
    assert request.base_dir == execution.root
    fx_call = execution.fx_calls[0]
    assert fx_call["write_cache"] is False
    assert fx_call["cache_path"] == execution.root / "output_shared/state/rate_cache.json"
    assert fx_call["deadline"] == request.deadline
    assert len(execution.gateways[0].calls) == 2  # One merged 14-day calendar, not three.
    assert execution.gateways[0].close_calls == 1
    assert not list(execution.root.iterdir())


def test_execute_mixed_markets_separates_fetch_and_earnings_but_shares_fx(execution):
    results = _results(execution.execute([_query(), _query("00700"), _query("US.NVDA", "short_call")]))
    assert [row["symbol"] for row in results] == ["NVDA", "0700.HK", "NVDA"]
    assert [row["currency"] for row in results] == ["USD", "HKD", "USD"]
    assert all(row["outcome"] == "found" and row["earnings_coverage"] == "complete" for row in results)
    assert len(execution.fetches) == len(execution.gateways) == 2
    assert len(execution.fx_calls) == 1
    assert execution.fetches[0].query_cache is execution.fetches[1].query_cache
    assert execution.fetches[0].gateway is not execution.fetches[1].gateway
    for gateway in execution.gateways:
        market = gateway.endpoint[0].split(".")[0].upper()
        assert {call["market"] for call in gateway.calls} == {market}
        assert len(gateway.calls) == 2
        assert gateway.close_calls == 1
    assert results[1]["candidates"][0]["earnings"]["events"][0]["date"] == "2026-10-04"


def test_same_market_symbols_share_calendar_intervals_but_not_financial_bundles(execution):
    results = _results(execution.execute([_query(), _query("MSFT")]))
    assert all(result["outcome"] == "found" for result in results)
    assert len(execution.fetches) == 2
    assert len(execution.fx_calls) == len(execution.gateways) == 1
    assert len(execution.gateways[0].calls) == 2
    assert execution.fetches[0].gateway is execution.fetches[1].gateway
    for result, expected in zip(results, ("2026-10-02", "2026-10-03")):
        assert all(row["earnings"]["events"][0]["date"] == expected for row in result["candidates"])


def test_earnings_only_fetches_expirations_of_displayed_top_three(execution):
    execution.dtes = (6, 13, 20, 27)
    result, = _results(execution.execute([_query()]))
    assert result["coverage"]["matched"] == 4
    assert result["displayed"] == 3
    assert result["omitted"] == 1
    assert execution.gateways[0].calls[-1]["end_date"] == max(row["expiry"] for row in result["candidates"])


def test_earnings_block_retains_financials_and_frozen_first_interval_then_closes_late(execution):
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    responses = []

    def earnings(gateway, request):
        if len(gateway.calls) == 2:
            entered.set()
            assert release.wait(3), "test failed to release blocked SDK"
            return [{"security": "US.NVDA", "earnings_date": request["begin_date"]}]
        return gateway.event_rows(request)

    execution.on_earnings = earnings

    def run():
        try:
            responses.append(execution.execute([_query()]))
        finally:
            returned.set()

    caller = threading.Thread(target=run, daemon=True)
    caller.start()
    try:
        assert entered.wait(3), responses
        gateway = execution.gateways[0]
        execution.monotonic += 6  # Exhaust the annotation budget, not the financial budget.
        assert returned.wait(3), "coordinator kept waiting for the blocked SDK"
        result, = _results(responses[0])
        assert result["outcome"] == "found"
        assert result["coverage"]["status"] == "complete"
        assert result["coverage"]["matched"] == 2
        assert result["earnings_coverage"] == "partial"
        by_dte = {row["dte"]: row for row in result["candidates"]}
        assert by_dte[6]["earnings"]["coverage"] == "complete"
        assert by_dte[13]["earnings"] == {
            "coverage": "partial", "events": [{"date": "2026-10-02", "days_before_expiry": 11}],
            "omitted_events": 0,
        }
        assert "earnings_incomplete" in result["reason_codes"]
        assert gateway.close_calls == 0  # The coordinator must not close in-flight SDK I/O.
        frozen = deepcopy(responses[0])
    finally:
        release.set()
        caller.join(3)
        if execution.gateways:
            assert execution.gateways[0].closed.wait(3)
    assert len(gateway.calls) == 2
    assert gateway.close_calls == 1
    assert responses[0] == frozen


@pytest.mark.parametrize("stale", ["underlier", "option_receipt"])
def test_final_stale_recheck_removes_candidates_and_recounts_without_refetch(execution, stale):
    if stale == "option_receipt":
        execution.row_overrides["snapshot_received_at_utc"] = (NOW - timedelta(seconds=299)).isoformat()
    elapsed = 2 if stale == "option_receipt" else 301
    execution.on_close = lambda _gateway: setattr(execution, "now", NOW + timedelta(seconds=elapsed))
    result, = _results(execution.execute([_query()]))
    assert execution.gateways[0].calls  # Financial candidates existed before the final recheck.
    assert result["outcome"] == "unable"
    assert result["candidates"] == []
    assert result["displayed"] == result["omitted"] == 0
    assert result["coverage"]["matched"] == result["coverage"]["excluded"] == result["coverage"]["evaluated"] == 0
    assert result["coverage"]["discovered"] == 2
    assert result["coverage"]["status"] == "unavailable"
    expected = "evidence_unavailable" if stale == "option_receipt" else "underlier_quote_stale"
    assert expected in result["reason_codes"]
    if stale == "option_receipt":
        assert result["coverage"]["enumeration_complete"] is True
        assert result["coverage"]["unassessed"] == 2
    assert len(execution.fetches) == len(execution.fx_calls) == 1


@pytest.mark.parametrize("stop", ["deadline", "cancelled"])
def test_late_financial_fetch_preserves_prior_query_and_closes_after_sdk_returns(execution, monkeypatch, stop):
    entered, release, returned, cancelled = (threading.Event() for _ in range(4))
    original_fetch = app.fetch_symbol_request
    responses = []

    def fetch(request):
        if request.symbol == "MSFT":
            entered.set()
            assert release.wait(3), "test failed to release blocked financial SDK"
        return original_fetch(request)

    monkeypatch.setattr(app, "fetch_symbol_request", fetch)

    def run():
        try:
            responses.append(execution.execute(
                [_query(), _query("MSFT"), _query("00700")], cancelled=cancelled.is_set,
            ))
        finally:
            returned.set()

    caller = threading.Thread(target=run, daemon=True)
    caller.start()
    try:
        assert entered.wait(3), responses
        if stop == "deadline":
            execution.monotonic += 59
        else:
            cancelled.set()
        assert returned.wait(3), "coordinator kept waiting for late financial data"
        results = _results(responses[0])
        assert results[0]["outcome"] == "found"
        assert results[0]["coverage"]["status"] == "complete"
        assert results[0]["earnings_coverage"] == "unavailable"
        assert results[1]["outcome"] == results[2]["outcome"] == "unable"
        assert "not_started" in results[2]["reason_codes"]
        assert responses[0]["data"]["completion"] == "partial"
        gateway, = execution.gateways
        assert gateway.close_calls == 0
        frozen = deepcopy(responses[0])
    finally:
        release.set()
        caller.join(3)
        if execution.gateways:
            assert execution.gateways[0].closed.wait(3)
    assert [request.symbol for request in execution.fetches] == ["NVDA", "MSFT"]
    assert len(execution.fx_calls) == 1
    assert not gateway.calls
    assert gateway.close_calls == 1
    assert responses[0] == frozen


@pytest.mark.parametrize("invalid", [
    _query(strategy="put"), _query(strike=0), _query(strike=True), _query(strike=float("nan")),
    _query(dte=[30, 1]), _query(dte=[1, 366]), _query(dte=[True, 30]),
    _query(symbol="腾讯"), _query(unexpected="no"),
])
def test_invalid_later_query_rejected_before_any_io(execution, invalid):
    response = execution.execute([_query(), invalid])
    assert response["ok"] is False
    assert response["error"]["code"] == "INPUT_ERROR"
    assert not execution.prepares
    assert not execution.fetches
    assert not execution.fx_calls
    assert not execution.gateways


def test_canonical_duplicate_batch_rejected_before_io(execution):
    response = execution.execute([_query("00700"), _query("HK.00700")])
    assert response["ok"] is False
    assert response["error"]["code"] == "INPUT_ERROR"
    assert not execution.prepares and not execution.gateways


def test_closed_market_never_fetches_fx_or_earnings(execution):
    execution.market_state = "CLOSED"
    result, = _results(execution.execute([_query()]))
    assert result["outcome"] == "unable"
    assert "market_closed" in result["reason_codes"]
    assert not result["candidates"]
    assert not execution.fx_calls
    assert not execution.gateways[0].calls
    assert execution.gateways[0].close_calls == 1


def test_explicit_complete_empty_enumeration_is_none_without_fx(execution):
    execution.dtes = ()
    result, = _results(execution.execute([_query()]))
    assert result["outcome"] == "none"
    assert result["coverage"] == {"status": "complete", "enumeration_complete": True,
                                  "discovered": 0, "evaluated": 0, "matched": 0, "excluded": 0, "unassessed": 0}
    assert not execution.fx_calls
    assert not execution.gateways[0].calls


@pytest.mark.parametrize("field", ["expiration_enumeration_complete", "query_scope_validated"])
@pytest.mark.parametrize("missing", [False, True])
def test_missing_enumeration_proof_cannot_produce_none(execution, field, missing):
    execution.dtes = ()
    if missing:
        execution.missing_meta.add(field)
    else:
        execution.bundle_overrides[field] = False
    result, = _results(execution.execute([_query()]))
    assert result["outcome"] == "unable"
    assert result["coverage"]["enumeration_complete"] is False
    assert result["coverage"]["status"] == "unavailable"
    assert not execution.fx_calls
