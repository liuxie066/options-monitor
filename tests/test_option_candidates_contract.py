"""Offline request, financial projection, completeness and final-time contracts."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from domain.domain.engine.candidate_engine import (
    CandidateCalculationError,
    calculate_opening_candidate_metrics,
    evaluate_opening_candidate_policy,
    rank_candidate_rows,
    validate_opening_contract_evidence,
    validate_opening_quote_freshness,
)
from src.application import option_candidates as app
from src.application.agent_tool_contracts import AgentToolError, build_response
from src.application.bot.tools import conservative_json_tokens
from src.application.covered_call_strategy_risk import resolve_covered_call_underwriting_config
from src.application.opening_quote_evidence import normalize_underlier_observation
from src.application.sell_put_strategy_risk import resolve_sell_put_underwriting_config


NOW = datetime(2026, 10, 27, 14, 0, tzinfo=timezone.utc)
RATES = {"USDCNY": 7.2, "HKDCNY": 0.9}
FX = {"fixture": "request-scoped-snapshot"}
MINIMAL = {"symbol": "NVDA", "strategy": "short_put"}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("contract test attempted configuration/provider I/O")

    for name in ("load_runtime_config", "fetch_symbol_request", "build_ready_futu_quote_gateway", "current_exchange_rate_snapshot"):
        monkeypatch.setattr(app, name, forbidden)

    def rates(snapshot, *, purpose, now):
        assert snapshot is FX and purpose == "capacity" and now.tzinfo is not None
        return dict(RATES)

    monkeypatch.setattr(app, "rates_for_purpose", rates)


def _prepared(*, mode="put", market="US", strike=None, dte=None, policy=None):
    symbol = "0700.HK" if market == "HK" else "NVDA"
    query = {"symbol": symbol, "strategy": f"short_{mode}", "market": market}
    resolver = resolve_sell_put_underwriting_config if mode == "put" else resolve_covered_call_underwriting_config
    cfg = resolver(policy or {})
    if strike is not None:
        query["strike"] = strike
        cfg = replace(cfg, min_strike=strike, max_strike=strike)
    if dte is not None:
        query["dte"] = list(dte)
        cfg = replace(cfg, min_dte=dte[0], max_dte=dte[1])
    filters = {"strike": strike, "min_strike": cfg.min_strike, "max_strike": cfg.max_strike,
               "dte": [cfg.min_dte, cfg.max_dte], "strike_source": "request" if strike is not None else "config",
               "dte_source": "request" if dte is not None else "config"}
    return app.PreparedQuery(query, cfg, filters, ("offline.invalid", 11111), NOW.date())


def _row(*, mode="put", market="US", strike=None, dte=31, **overrides):
    owner = "HK.00700" if market == "HK" else "US.NVDA"
    strike = strike if strike is not None else (100.0 if mode == "put" else 120.0)
    expiry = NOW.date() + timedelta(days=dte)
    multiplier = 500 if market == "HK" else 100
    row = {
        "symbol": "0700.HK" if market == "HK" else "NVDA", "market": market,
        "currency": "HKD" if market == "HK" else "USD", "option_type": mode,
        "contract_symbol": f"{owner}{expiry:%y%m%d}{mode[0].upper()}{int(strike * 1000)}",
        "expiration": expiry.isoformat(), "dte": dte, "strike": strike, "spot": 110.0,
        "bid": 3.0, "ask": 3.10, "price_tick": 0.05, "last_price": None,
        "implied_volatility": 0.30, "term_matched_rv": 0.20, "term_matched_rv_status": "ok",
        "term_matched_rv_reason": None, "underlier_observation_status": "ready",
        "underlier_observation_reason_code": None, "option_standard_type": "STANDARD",
        "stock_owner": owner, "stock_type": "DRVT", "chain_multiplier": multiplier,
        "snapshot_multiplier": multiplier, "multiplier": multiplier,
        "opening_contract_status": "ready", "opening_contract_reason_codes": [],
        "snapshot_received_at_utc": NOW.isoformat(), "spot_observed_at_utc": NOW.isoformat(),
        "last_price_update_time": None, "delta": -0.3 if mode == "put" else 0.3,
        "open_interest": None, "volume": None,
    }
    row.update(overrides)
    return row


def _bundle(rows, *, market="US", now=NOW, state="MORNING", **meta):
    owner = "HK.00700" if market == "HK" else "US.NVDA"
    observation = normalize_underlier_observation(
        code=owner, market=market,
        snapshot_row={"code": owner, "last_price": 110, "update_time": now.isoformat(),
                      "sec_status": "NORMAL", "suspension": False},
        market_state_row={"code": owner, "market_state": state}, now_utc=now,
    )
    assert observation.status == ("ready" if state == "MORNING" else "market_closed" if state == "CLOSED" else "data_unavailable")
    expirations = sorted({row["expiration"] for row in rows})
    return {"rows": deepcopy(rows), "underlier_code": owner, "meta": {
        "underlier_observation": observation.to_dict(),
        "expiration_enumeration_complete": True, "query_scope_validated": True,
        "available_expirations": expirations,
        "expiration_statuses": {expiry: "fetched" for expiry in expirations},
        "source_outcome": "success_rows" if rows else "success_empty", **meta,
    }}


def _evaluate(rows, *, prepared=None, now=NOW, bundle=None):
    prepared = prepared or _prepared()
    return app._evaluate(prepared, bundle if bundle is not None else _bundle(rows, market=prepared.query["market"]), FX, 0, now)


def _assert_counts(result, *, matched, excluded, unassessed):
    coverage = result["coverage"]
    assert coverage["matched"] == matched
    assert coverage["excluded"] == excluded
    assert coverage["evaluated"] == matched + excluded
    assert coverage["unassessed"] == unassessed
    if coverage["enumeration_complete"]:
        assert coverage["discovered"] == matched + excluded + unassessed
    assert result["displayed"] == len(result["candidates"])
    assert result["omitted"] == matched - result["displayed"]


@pytest.mark.parametrize("field,value", [
    ("strike", value) for value in (True, False, None, "370", "370P", 0, -1, float("nan"), float("inf"), -float("inf"))
] + [("dte", value) for value in (None, [1], [1, 2, 3], [0, 30], [7, 366], [30, 7],
                                   [True, 30], [7, False], [7.0, 30], [7, "30"], [float("nan"), 30], [7, float("inf")])]
  + [("strategy", value) for value in (None, "put", "call", "buy_put", "long_call", "370P", "")]
  + [("symbol", value) for value in (None, "", "  ", "腾讯", "A" * 33, "SH.600000")])
def test_normalize_rejects_invalid_values_with_indexed_errors(field, value):
    with pytest.raises(AgentToolError) as caught:
        app.normalize_queries({"queries": [{**MINIMAL, field: value}]})
    assert caught.value.code == "INPUT_ERROR"
    errors = caught.value.details["schema_errors"]
    assert 1 <= len(errors) <= 10
    assert any(error["path"].startswith(f"queries[0].{field}") for error in errors)


@pytest.mark.parametrize("payload", [None, {}, {"queries": None}, {"queries": []}, {"queries": {}},
    {"queries": [None]}, {"queries": [{"symbol": "NVDA"}]}, {"queries": [{"strategy": "short_put"}]},
    {"queries": [MINIMAL] * 6}, {"queries": [MINIMAL], "config_key": "us"},
    {"queries": [{**MINIMAL, "account": "test"}]}, {"queries": [{**MINIMAL, "host": "evil"}]}])
def test_normalize_rejects_invalid_structure(payload):
    with pytest.raises(AgentToolError) as caught:
        app.normalize_queries(payload)
    assert caught.value.code == "INPUT_ERROR"


@pytest.mark.parametrize("alias,canonical,market", [("00700", "0700.HK", "HK"), ("0700.HK", "0700.HK", "HK"),
    ("HK.00700", "0700.HK", "HK"), (" US.NVDA ", "NVDA", "US"), ("NVDA", "NVDA", "US")])
def test_normalize_aliases_and_boundary_ranges(alias, canonical, market):
    queries = app.normalize_queries({"queries": [{"symbol": alias, "strategy": "short_call", "strike": 230.5, "dte": [1, 365]}]})
    assert queries == [{"symbol": canonical, "market": market, "strategy": "short_call", "strike": 230.5, "dte": [1, 365]}]


@pytest.mark.parametrize("symbols", [("00700", "HK.00700"), ("US.NVDA", "NVDA")])
def test_normalize_rejects_alias_and_numeric_duplicate_requests(symbols):
    with pytest.raises(AgentToolError) as caught:
        app.normalize_queries({"queries": [{**MINIMAL, "symbol": symbols[0], "strike": 370},
                                          {**MINIMAL, "symbol": symbols[1], "strike": 370.0}]})
    assert caught.value.code == "INPUT_ERROR"
    assert caught.value.details["schema_errors"][0]["path"] == "queries[1]"


def test_normalize_preserves_five_distinct_queries_and_omitted_fields():
    requests = [MINIMAL, {**MINIMAL, "strategy": "short_call"}, {**MINIMAL, "strike": 100},
                {**MINIMAL, "strike": 100.00000000000001}, {**MINIMAL, "dte": [7, 7]}]
    original = deepcopy(requests)
    normalized = app.normalize_queries({"queries": requests})
    assert len(normalized) == 5 and requests == original
    assert [{k: v for k, v in q.items() if k != "market"} for q in normalized] == requests


@pytest.mark.parametrize("mode", ["put", "call"])
@pytest.mark.parametrize("market", ["US", "HK"])
def test_evaluate_projects_canonical_financial_conditions_without_capacity(mode, market):
    prepared = _prepared(mode=mode, market=market)
    row = _row(mode=mode, market=market, max_new_contracts=0, covered_contracts_available=0)
    before = deepcopy(row)
    result = _evaluate([row], prepared=prepared)
    assert row == before and result["outcome"] == "found"
    assert result["coverage"]["status"] == "complete"
    _assert_counts(result, matched=1, excluded=0, unassessed=0)
    metrics = calculate_opening_candidate_metrics(row, mode=mode, cny_per_currency_unit=RATES["HKDCNY" if market == "HK" else "USDCNY"], now_utc=NOW)
    enriched = {**row, **metrics}
    policy = evaluate_opening_candidate_policy(enriched, mode=mode, min_dte=7, max_dte=60,
        check_capacity=False, require_earnings_evidence=False, reject_known_earnings=False)
    old = evaluate_opening_candidate_policy(enriched, mode=mode, require_earnings_evidence=False)
    assert [item["reason"] for item in old["rejects"]] == [f"hard_capacity_{mode}"]
    candidate = result["candidates"][0]
    assert candidate["evidence"]["conditions"] == policy["conditions"]
    assert 1 <= len(candidate["evidence"]["conditions"]) <= 10
    assert all(item["status"] == "pass" for item in candidate["evidence"]["conditions"])
    assert candidate["net_premium"] == metrics["net_income"]
    assert candidate["sell_limit"] == metrics["sell_limit"]
    assert candidate["period_net_return"] == metrics["period_net_return"]
    assert candidate["evidence"]["estimated_full_sell_fees"] == metrics["estimated_full_sell_fees"]
    assert candidate["evidence"]["annualized_net_return"] == metrics["annualized_net_return_on_cash_basis" if mode == "put" else "annualized_net_premium_return"]
    denominator = row["strike"] * row["multiplier"] - candidate["net_premium"] if mode == "put" else row["spot"] * row["multiplier"]
    assert candidate["period_net_return"] == pytest.approx(candidate["net_premium"] / denominator)
    expected_obligation = ({"kind": "net_acquisition_price", "price": metrics["breakeven"]} if mode == "put" else
                           {"kind": "sale_price", "price": row["strike"], "above_spot_ratio": metrics["strike_above_spot_pct"]})
    assert candidate["obligation"] == expected_obligation
    assert candidate["evidence"]["last"] is None and candidate["evidence"]["iv_source"] == "opend"
    assert candidate["earnings"]["coverage"] == "unavailable"
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("override,reason", [({"bid": 0.05, "ask": 0.10}, "return_annualized"),
    ({"bid": 2, "ask": 4}, "risk_spread"), ({"implied_volatility": 0.21}, "risk_iv_rv_ratio"),
    ({"implied_volatility": 0.24}, "risk_iv_minus_rv"), ({"opening_contract_status": "ineligible"}, "contract_ineligible")])
def test_evaluate_valid_rejections_are_none_with_canonical_reasons(override, reason):
    result = _evaluate([_row(**override)])
    assert result["outcome"] == "none"
    _assert_counts(result, matched=0, excluded=1, unassessed=0)
    assert {item["code"] for item in result["exclusion_summary"]} >= {reason}


@pytest.mark.parametrize("override", [{"bid": None}, {"price_tick": None}, {"multiplier": None},
    {"term_matched_rv_status": "data_unavailable", "term_matched_rv": None}, {"implied_volatility": None},
    {"snapshot_received_at_utc": (NOW - timedelta(seconds=301)).isoformat()}, {"opening_contract_status": "data_unavailable"}])
def test_missing_required_evidence_is_unassessed_never_none(override):
    result = _evaluate([_row(**override)])
    assert result["outcome"] == "unable" and result["reason_codes"]
    _assert_counts(result, matched=0, excluded=0, unassessed=1)


def test_empty_complete_chain_and_scope_filtered_empty_preserve_effective_range():
    prepared = _prepared(policy={"min_strike": 95, "max_strike": 100})
    for rows in ([], [_row(strike=101)], [_row(dte=61)], [_row(mode="call")]):
        result = _evaluate(rows, prepared=prepared)
        assert result["outcome"] == "none" and result["coverage"]["status"] == "complete"
        assert result["effective_filters"] == prepared.filters
        _assert_counts(result, matched=0, excluded=0, unassessed=0)


@pytest.mark.parametrize("missing", ["expiration_enumeration_complete", "query_scope_validated", "available_expirations", "expiration_statuses"])
def test_absent_enumeration_evidence_cannot_prove_none(missing):
    bundle = _bundle([])
    bundle["meta"].pop(missing)
    result = _evaluate([], bundle=bundle)
    assert result["outcome"] == "unable"
    assert result["coverage"]["enumeration_complete"] is False
    assert result["coverage"]["unassessed"] is None


@pytest.mark.parametrize("flag", ["expiration_enumeration_complete", "query_scope_validated"])
def test_negative_enumeration_attestation_cannot_be_replaced_by_success_empty(flag):
    result = _evaluate([], bundle=_bundle([], **{flag: False}))
    assert result["outcome"] == "unable"
    assert result["coverage"]["enumeration_complete"] is False


@pytest.mark.parametrize("status", ["fetched", "cache", "empty"])
def test_explicit_successful_empty_chain_segment_is_complete(status):
    expiry = (NOW.date() + timedelta(days=31)).isoformat()
    bundle = _bundle([], available_expirations=[expiry], expiration_statuses={expiry: status})
    result = _evaluate([], bundle=bundle)
    assert result["outcome"] == "none" and result["coverage"]["enumeration_complete"] is True
    _assert_counts(result, matched=0, excluded=0, unassessed=0)


@pytest.mark.parametrize("with_match", [False, True])
def test_partial_chain_keeps_unknown_denominator_and_completed_matches(with_match):
    rows = [_row()] if with_match else []
    bundle = _bundle(rows)
    missing_expiry = (NOW.date() + timedelta(days=45)).isoformat()
    bundle["meta"]["available_expirations"].append(missing_expiry)
    bundle["meta"]["expiration_statuses"][missing_expiry] = "failed"
    result = _evaluate(rows, bundle=bundle)
    assert result["outcome"] == ("found" if with_match else "unable")
    assert result["coverage"]["enumeration_complete"] is False
    _assert_counts(result, matched=int(with_match), excluded=0, unassessed=None)


def test_discovered_contracts_are_deduplicated_and_partitioned():
    good = _row()
    bad = _row(strike=101, implied_volatility=0.21)
    missing = _row(strike=102, multiplier=None)
    result = _evaluate([good, deepcopy(good), bad, missing])
    assert result["outcome"] == "found" and result["coverage"]["status"] == "partial"
    assert result["coverage"]["discovered"] == 3
    _assert_counts(result, matched=1, excluded=1, unassessed=1)


@pytest.mark.parametrize("state", ["CLOSED", "UNKNOWN"])
@pytest.mark.parametrize("has_rows", [False, True])
def test_closed_or_unknown_market_never_reports_none(state, has_rows):
    rows = [_row()] if has_rows else []
    result = _evaluate(rows, bundle=_bundle(rows, state=state))
    assert result["outcome"] == "unable" and result["candidates"] == []
    assert result["coverage"]["evaluated"] == 0


@pytest.mark.parametrize("mode,strike", [("put", 120), ("call", 100)])
def test_explicit_strike_outside_spot_policy_is_excluded_not_silently_filtered(mode, strike):
    prepared = _prepared(mode=mode, strike=strike)
    result = _evaluate([_row(mode=mode, strike=strike)], prepared=prepared)
    assert result["outcome"] == "none" and result["effective_filters"] == prepared.filters
    _assert_counts(result, matched=0, excluded=1, unassessed=0)
    assert result["exclusion_summary"] == [{"code": "hard_strike", "count": 1}]


def test_exact_strike_and_inclusive_dte_do_not_match_neighboring_contracts():
    prepared = _prepared(strike=100.5, dte=[7, 60])
    rows = [_row(strike=100.5, dte=days) for days in (6, 7, 60, 61)]
    rows.extend([_row(strike=100.50000000000001), _row(strike=100.49999999999999)])
    result = _evaluate(rows, prepared=prepared)
    assert result["coverage"]["discovered"] == 2
    assert {row["dte"] for row in result["candidates"]} == {7, 60}
    assert result["effective_filters"] == prepared.filters


@pytest.mark.parametrize("receipt", [None, "not-a-date", "2026-10-27T14:00:00", (NOW + timedelta(seconds=1)).isoformat(),
                                      (NOW - timedelta(seconds=301)).isoformat()])
def test_freshness_helper_preserves_fail_closed_receipt_validation(receipt):
    row = _row(snapshot_received_at_utc=receipt)
    with pytest.raises(CandidateCalculationError) as caught:
        validate_opening_quote_freshness(row, now_utc=NOW)
    assert caught.value.reason == "evidence_unavailable"


def test_freshness_helper_keeps_legacy_error_order_and_inclusive_age_limit():
    row = _row(snapshot_received_at_utc=(NOW - timedelta(seconds=300)).isoformat())
    assert validate_opening_quote_freshness(row, now_utc=NOW) is None
    with pytest.raises(CandidateCalculationError):
        validate_opening_quote_freshness(row, now_utc=NOW, max_snapshot_age_seconds=299)
    row.update(opening_contract_status="ineligible", snapshot_received_at_utc=(NOW - timedelta(seconds=301)).isoformat())
    with pytest.raises(CandidateCalculationError) as legacy:
        validate_opening_contract_evidence(row, mode="put", now_utc=NOW)
    assert legacy.value.reason == "contract_ineligible"
    with pytest.raises(CandidateCalculationError) as freshness:
        validate_opening_quote_freshness(row, now_utc=NOW)
    assert freshness.value.reason == "evidence_unavailable"


@pytest.mark.parametrize("override", [{}, {"implied_volatility": 0.21}, {"opening_contract_status": "ineligible"}])
def test_final_time_rechecks_matched_and_excluded_option_receipts(override):
    row = _row(snapshot_received_at_utc=(NOW - timedelta(seconds=299)).isoformat(), **override)
    bundle = _bundle([row])
    first = _evaluate([row], bundle=bundle)
    assert first["coverage"]["evaluated"] == 1
    final = _evaluate([row], bundle=bundle, now=NOW + timedelta(seconds=2))
    assert final["outcome"] == "unable"
    assert final["coverage"]["enumeration_complete"] is True
    _assert_counts(final, matched=0, excluded=0, unassessed=1)
    assert first["coverage"]["evaluated"] == 1  # Re-evaluation must not mutate the retained result.


def test_final_time_rechecks_underlier_without_erasing_known_chain_denominator():
    row = _row()
    bundle = _bundle([row], now=NOW - timedelta(seconds=299))
    assert _evaluate([row], bundle=bundle)["outcome"] == "found"
    result = _evaluate([row], bundle=bundle, now=NOW + timedelta(seconds=2))
    assert result["outcome"] == "unable" and "underlier_quote_stale" in result["reason_codes"]
    assert result["coverage"]["enumeration_complete"] is True
    _assert_counts(result, matched=0, excluded=0, unassessed=1)


def test_final_time_rechecks_fx_eligibility_without_refetch(monkeypatch):
    calls = []
    def rates(snapshot, *, purpose, now):
        calls.append((snapshot, purpose, now))
        return RATES if now == NOW else {}
    monkeypatch.setattr(app, "rates_for_purpose", rates)
    row, prepared = _row(), _prepared()
    bundle = _bundle([row])
    assert app._evaluate(prepared, bundle, FX, 0, NOW)["outcome"] == "found"
    final = app._evaluate(prepared, bundle, FX, 0, NOW + timedelta(seconds=2))
    assert final["outcome"] == "unable" and "fx_unavailable" in final["reason_codes"]
    _assert_counts(final, matched=0, excluded=0, unassessed=1)
    assert len(calls) == 2 and all(call[0] is FX for call in calls)


def test_final_time_refills_display_after_top_candidate_expires():
    rows = [_row(strike=100 + index, bid=4 - index * 0.1, ask=4.10 - index * 0.1) for index in range(4)]
    enriched = [{**row, **calculate_opening_candidate_metrics(row, mode="put", cny_per_currency_unit=7.2, now_utc=NOW)} for row in rows]
    winner = rank_candidate_rows(enriched, mode="put")[0]["contract_symbol"]
    for row in rows:
        if row["contract_symbol"] == winner:
            row["snapshot_received_at_utc"] = (NOW - timedelta(seconds=299)).isoformat()
    bundle = _bundle(rows)
    before = _evaluate(rows, bundle=bundle)
    assert before["candidates"][0]["contract"] == winner and before["omitted"] == 1
    after = _evaluate(rows, bundle=bundle, now=NOW + timedelta(seconds=2))
    _assert_counts(after, matched=3, excluded=0, unassessed=1)
    assert after["displayed"] == 3 and winner not in {candidate["contract"] for candidate in after["candidates"]}


def test_final_market_date_change_is_explicit_unavailable():
    result = _evaluate([_row()], now=NOW + timedelta(days=1))
    assert result["outcome"] == "unable" and result["coverage"]["evaluated"] == 0
    assert "date_changed" in result["reason_codes"]


def _envelope(data, warnings, meta):
    return build_response(tool_name="option_candidates", ok=True, data=data, warnings=warnings, meta=meta)


@pytest.mark.parametrize("include_empty", [False, True])
def test_five_query_response_bound_preserves_results_gaps_filters_and_earnings(include_empty):
    results = []
    for index in range(5):
        prepared = _prepared(policy={"min_strike": 95 + index, "max_strike": 110})
        rows = [_row(strike=100 + offset) for offset in range(4)]
        if include_empty and index == 4:
            rows = []
        result = app._evaluate(prepared, _bundle(rows), FX, index, NOW)
        for candidate in result["candidates"]:
            candidate["reasons"] = ["估算收益达到所选门槛，参考价格不保证成交。" * 5] * 3
            candidate["earnings"] = {"coverage": "partial", "events": [
                {"date": "2026-11-01", "days_before_expiry": 26},
                {"date": "2026-11-08", "days_before_expiry": 19},
                {"date": "2026-11-15", "days_before_expiry": 12}], "omitted_events": 2}
        if result["candidates"]:
            result["coverage"].update(status="partial", enumeration_complete=False, unassessed=None)
            result["reason_codes"] = ["earnings_incomplete", "enumeration_incomplete"]
            result["earnings_coverage"] = "partial"
        results.append(result)
    data = {"evaluation_scope": "contract_only", "account_checks": "not_evaluated", "completion": "partial",
            "limits": {"max_queries": 5, "max_dte": 365, "budget_seconds": 60, "max_candidates_per_query": 3}, "results": results}
    warnings = ["仅筛合约，未检查账户资金或持股；参考限价不保证成交。", "财报信息不完整。"]
    meta = {"started_at": NOW.isoformat(), "completed_at": NOW.isoformat(), "elapsed_ms": 0}
    original = deepcopy(data)
    assert conservative_json_tokens(_envelope(data, warnings, meta)) > 8000
    bounded, final_warnings, final_meta = app._bound_response(data, warnings, meta)
    envelope = _envelope(bounded, final_warnings, final_meta)
    encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    assert len(encoded) <= 16000 and conservative_json_tokens(envelope) <= 8000
    assert json.loads(encoded)["data"] == bounded and "财报信息不完整。" in final_warnings
    assert len(bounded["results"]) == 5
    for before, after in zip(original["results"], bounded["results"]):
        for key in ("query_index", "effective_filters", "outcome", "coverage"):
            assert after[key] == before[key]
        assert after["omitted"] == after["coverage"]["matched"] - after["displayed"]
        if not before["candidates"]:
            assert after["candidates"] == []
            continue
        assert after["displayed"] >= 1
        assert {"output_limited", "earnings_incomplete", "enumeration_incomplete"} <= set(after["reason_codes"])
        for candidate in after["candidates"]:
            assert candidate["evidence"] is None and candidate["earnings"]["coverage"] == "partial"
            assert len(candidate["earnings"]["events"]) + candidate["earnings"]["omitted_events"] == 5
            if after["displayed"] == 1:
                assert len(candidate["reasons"]) == 1
                assert len(candidate["reasons"][0].encode("utf-8")) <= 80


def test_small_response_keeps_full_evidence():
    data = {"results": [_evaluate([_row()])]}
    original = deepcopy(data)
    assert app._bound_response(data, [], {}) == (original, [], {})


def test_evidence_only_compression_keeps_reason_codes_within_eight():
    results = []
    for index in range(5):
        result = app._evaluate(_prepared(), _bundle([_row(strike=100 + offset) for offset in range(3)]), FX, index, NOW)
        result["coverage"].update(status="partial", enumeration_complete=False, unassessed=None)
        result["reason_codes"] = ["earnings_incomplete", "enumeration_incomplete", "fx_unavailable", "quote_missing",
                                  "snapshot_stale", "rv_unavailable", "multiplier_missing", "evidence_unavailable"]
        results.append(result)
    data = {"results": results}
    assert len(json.dumps(_envelope(data, [], {})).encode()) > 16000
    bounded, _, _ = app._bound_response(data, [], {})
    for result in bounded["results"]:
        assert len(result["reason_codes"]) <= 8
        assert {"earnings_incomplete", "enumeration_incomplete", "output_limited"} <= set(result["reason_codes"])
        assert result["displayed"] >= 1
