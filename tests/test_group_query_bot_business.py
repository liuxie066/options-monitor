"""Public candidate policy and shared close economics without account reads."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from domain.domain.close_advice import (
    CloseAdviceInput,
    UserProvidedCloseAdviceInput,
    evaluate_user_provided_close_advice,
)
from om_position_advice_plugin import evaluate_close_advice
from om_group_query_bot.business import (
    Dependencies,
    _evaluate_scenario,
    _contract_evidence,
)
from src.application.agent_tool_contracts import AgentToolError
from src.application.option_candidates import (
    evaluate_public_candidates,
    prepare_public_query,
)

NOW = datetime(2026, 11, 10, 6, 0, tzinfo=timezone.utc)
ROUTE = {"market_data_provider": "opend", "host": "127.0.0.1", "port": 11111}
POLICY = {"sell_put": {"min_dte": 20, "max_dte": 60, "max_abs_delta": 0.25}}
ECONOMICS = dict(
    symbol="US.NVDA", option_type="put", side="short", expiration="2026-12-18",
    strike=180.0, contracts_open=2, premium=2.5, bid=1.10, ask=1.20, dte=38,
    multiplier=100.0, spot=185.0, currency="USD", delta=-0.22,
    estimated_open_fee=1.3, estimated_close_fee=1.2,
    fee_calc_status="conservative_estimate",
    fee_calc_basis="sell+buy/per_share",
)
METRIC_KEYS = ("recommendation_state", "opening_net_credit", "all_in_close_cost",
               "net_capture_ratio", "close_cost_ratio", "capital_basis",
               "remaining_max_annualized_return")


def _prepare(query, policy=POLICY):
    return prepare_public_query({**query, "market": "US"}, policy, ROUTE, NOW)


def test_public_candidates_intersect_request_and_policy_delta():
    prepared = _prepare({"symbol": "US.NVDA", "strategy": "short_put",
                         "abs_delta_range": [0.2, 0.4], "limit": 5})
    assert prepared.filters["max_abs_delta"] == 0.25
    assert prepared.filters["min_abs_delta"] == 0.2
    assert prepared.filters["abs_delta_range"] == [0.2, 0.4]
    assert prepared.query["abs_delta_range"] == [0.2, 0.25]
    assert prepared.filters["max_abs_delta_source"] == "request"
    with pytest.raises(AgentToolError) as caught:
        _prepare({"symbol": "US.NVDA", "strategy": "short_put",
                  "abs_delta_range": [0.3, 0.4]})
    assert caught.value.code == "INPUT_ERROR"


def test_public_candidates_can_leave_the_vertical_cap_off():
    policy = {"sell_put": {"min_dte": 20, "max_dte": 60, "max_abs_delta": None}}
    prepared = _prepare({"symbol": "US.NVDA", "strategy": "short_put",
                         "abs_delta_range": [0.3, 0.4]}, policy)
    assert prepared.filters["max_abs_delta"] == 0.4
    assert prepared.filters["min_abs_delta"] == 0.3
    without_range = _prepare({"symbol": "US.NVDA", "strategy": "short_put"}, policy)
    assert without_range.filters["max_abs_delta"] is None


def test_public_candidates_reject_market_mismatch_and_account_policy():
    with pytest.raises(AgentToolError) as caught:
        prepare_public_query({"symbol": "HK.00700", "market": "US",
                              "strategy": "short_put"}, POLICY, ROUTE, NOW)
    assert caught.value.code == "INPUT_ERROR"
    with pytest.raises(AgentToolError):
        _prepare({"symbol": "US.NVDA", "strategy": "short_put"},
                 {"sell_put": {"min_dte": 20}, "accounts": {"lx": {}}})


def test_unavailable_market_data_is_a_business_outcome_not_an_error():
    prepared = _prepare({"symbol": "US.NVDA", "strategy": "short_put", "limit": 5})
    result = evaluate_public_candidates(prepared, {"rows": [], "meta": {}}, {}, 0, NOW)
    assert result["outcome"] == "unable"
    assert result["displayed"] == 0 and result["candidates"] == []
    assert "underlier_market_invalid" in result["reason_codes"]
    assert result["effective_filters"]["min_abs_delta"] is None
    assert "account" not in result and "portfolio" not in result


def test_user_provided_close_shares_economics_but_not_identity():
    formal = evaluate_close_advice(
        CloseAdviceInput(account="lx", position_lot_id="lot-1", **ECONOMICS))
    user = evaluate_user_provided_close_advice(
        UserProvidedCloseAdviceInput(
            input_id="input-1", input_as_of_utc="2026-11-10T05:59:30+00:00",
            **ECONOMICS))
    assert formal["recommendation_state"] == "hold"
    assert user["recommendation_state"] == formal["recommendation_state"]
    for key in METRIC_KEYS:
        assert user[key] == formal[key], key
    assert user["input_source"] == "user_provided"
    assert "account" not in user and "position_lot_id" not in user
    assert formal["account"] == "lx" and formal["position_lot_id"] == "lot-1"


def test_user_provided_close_never_returns_an_evaluable_state_without_identity():
    user = evaluate_user_provided_close_advice(
        UserProvidedCloseAdviceInput(**ECONOMICS))
    assert user["recommendation_state"] == "not_evaluable"
    assert "missing_input_id" in user["decision_basis"]
    assert "missing_input_as_of_utc" in user["decision_basis"]


def test_formal_close_still_requires_account_identity():
    formal = evaluate_close_advice(
        CloseAdviceInput(account="", position_lot_id=None, **ECONOMICS))
    assert formal["recommendation_state"] == "not_evaluable"
    assert "account" in formal


def _scenario_rows():
    old = {
        "option_type": "put", "expiration": "2026-12-18", "strike": 180.0,
        "currency": "USD", "multiplier": 100, "bid": 1.10, "ask": 1.20,
        "spot": 185.0, "delta": -0.22, "dte": 38,
        "option_standard_type": "STANDARD", "stock_owner": "NVDA",
        "price_tick": 0.01, "chain_multiplier": 100, "snapshot_multiplier": 100,
        "snapshot_received_at_utc": "2026-11-10T05:59:00+00:00",
        "spot_observed_at_utc": "2026-11-10T05:59:00+00:00",
        "option_sec_status": "NORMAL", "underlier_sec_status": "NORMAL",
        "market_state": "NORMAL", "quote_rights": "OK",
        "opening_contract_status": "ready", "opening_contract_reason_codes": [],
        "underlier_observation_status": "ready",
        "contract_symbol": "US.NVDA261218P180000",
    }
    newer = dict(old, expiration="2027-01-15", strike=160.0, bid=3.0, ask=3.2,
                 delta=-0.18, dte=66, contract_symbol="US.NVDA270115P160000")
    return old, newer


def _effective_scope(plan):
    bounds = plan.get("search_bounds") or {}
    return {"option_type": plan["original_contract"]["option_type"],
            "min_strike": None, "max_strike": None,
            "max_expiration": bounds.get("max_new_expiration")}


def test_scenario_evaluation_reports_close_coverage_and_no_identity(tmp_path):
    from test_group_query_bot_scenario_contract import _build, _scenario
    plan = _build(tmp_path, _scenario())
    old, _newer = _scenario_rows()
    result = _evaluate_scenario(
        plan=plan, rows=[old], original=old,
        contract_evidence=_contract_evidence(plan, old),
        now=NOW, limit=int(plan["limit"]),
        effective_scope=_effective_scope(plan))
    assert result["analysis_kind"] == "close"
    assert result["coverage"] == {"original_contract": "evaluated",
                                  "candidates_requested": 0}
    assert result["recommendation_state"] == "hold"
    assert result["evaluation_status"] in {"priced", "not_evaluable"}
    assert result["metrics"]["opening_gross_credit"] == 500.0
    assert 490 < result["metrics"]["opening_net_credit"] < 500
    assert result["fee_model"]["model_id"] == "futu_option_fee"
    assert result["fee_model"]["estimated_close_fee"] > 0
    assert result["quote"]["bid"] == 1.10 and result["quote"]["ask"] == 1.20
    assert result["symbol"] == "US.NVDA" and result["currency"] == "USD"
    assert "account" not in result and "position_lot_id" not in result
    assert result["position"]["symbol"] == "US.NVDA"
    assert result["position"]["contracts"] == 2


def test_roll_down_evaluation_returns_design_fields_and_bounds(tmp_path):
    from test_group_query_bot_scenario_contract import _build, _scenario
    scenario = _scenario(operation="roll-down")
    scenario.pop("opening_premium")
    scenario["search_bounds"] = {"max_new_expiration": "2027-01-15",
                                 "min_new_strike": 150.0}
    plan = _build(tmp_path, scenario)
    old, newer = _scenario_rows()
    result = _evaluate_scenario(
        plan=plan, rows=[old, newer], original=old,
        contract_evidence=_contract_evidence(plan, old),
        now=NOW, limit=int(plan["limit"]),
        effective_scope=_effective_scope(plan))
    assert result["analysis_kind"] == "roll_down"
    assert result["status"] == "success"
    assert result["outcome"] == "found"
    assert result["displayed"] == 1 and result["omitted"] == 0
    assert result["old_quote"] == {"ask": 1.20, "bid": 1.10}
    assert result["scope"] == {"max_new_expiration": "2027-01-15",
                               "strike_boundary": 150.0}
    assert result["fee_model"]["model_id"] == "futu_option_fee"
    candidate = result["candidates"][0]
    assert candidate["strike"] == 160.0
    assert candidate["contracts"] == 2
    assert "account" not in candidate and "position_lot_id" not in candidate


def test_roll_down_without_candidates_does_not_claim_none(tmp_path):
    from test_group_query_bot_scenario_contract import _build, _scenario
    scenario = _scenario(operation="roll-down")
    scenario.pop("opening_premium")
    scenario["search_bounds"] = {"max_new_expiration": "2027-01-15"}
    plan = _build(tmp_path, scenario)
    old, _newer = _scenario_rows()
    result = _evaluate_scenario(
        plan=plan, rows=[old], original=old,
        contract_evidence=_contract_evidence(plan, old),
        now=NOW, limit=int(plan["limit"]),
        effective_scope=_effective_scope(plan))
    assert result["candidates"] == []
    assert result["displayed"] == 0
    assert result["outcome"] == "unable"
    assert "search_scope_not_proven_complete" in result["reason_codes"]


def test_scenario_evaluation_rejects_missing_or_ambiguous_original_contract(tmp_path):
    from om_group_query_bot.business import BusinessUnavailable, _original_row
    from test_group_query_bot_scenario_contract import _build, _scenario
    plan = _build(tmp_path, _scenario())
    row = {"option_type": "put", "expiration": "2026-12-18", "strike": 180.0,
           "currency": "USD", "multiplier": 100}
    with pytest.raises(BusinessUnavailable):
        _original_row([], plan)
    with pytest.raises(BusinessUnavailable):
        _original_row([row, dict(row)], plan)
    with pytest.raises(BusinessUnavailable):
        _original_row([{**row, "currency": ""}], plan)
    with pytest.raises(BusinessUnavailable):
        _original_row([{**row, "multiplier": 0}], plan)


def test_default_dependencies_hold_no_network_until_invoked():
    dependencies = Dependencies.defaults()
    assert callable(dependencies.fetch_symbol)
    assert callable(dependencies.exchange_rates)
    assert not isinstance(dependencies.fetch_symbol, type(None))


def test_spot_only_payload_wraps_the_ready_observation():
    from om_group_query_bot.business import _spot_payloads
    from src.application.opening_quote_evidence import (
        OPENING_UNDERLIER_OBSERVATION_SCHEMA,
        OpeningUnderlierObservation,
    )

    class _Settings:
        quote_source = "futu"

    observation = OpeningUnderlierObservation(
        schema_version=OPENING_UNDERLIER_OBSERVATION_SCHEMA, code="NVDA",
        market="US", last_price=185.0, update_time="2026-11-10T05:59:00+00:00",
        observed_at_utc="2026-11-10T05:59:00+00:00", age_seconds=1.0,
        market_state="NORMAL", sec_status="NORMAL", suspension=False,
        status="ready", reason_code=None)
    payloads = _spot_payloads(_Settings(), "NVDA", observation)
    assert len(payloads) == 1
    row = payloads[0]["rows"][0]
    assert row["last_price"] == 185.0 and row["spot"] == 185.0
    assert row["snapshot_received_at_utc"] == "2026-11-10T05:59:00+00:00"
    assert payloads[0]["underlier_code"] == "NVDA"
    assert payloads[0]["meta"]["source"] == "futu"
    assert _spot_payloads(_Settings(), "NVDA", None) == []


def _fake_plan(plan):
    from om_group_query_bot.business import FetchPlan

    return FetchPlan(symbol="NVDA", fetch_plan=plan["fetch_plan"],
                     expected_contract=plan["expected_fetch_contract"],
                     global_plan=plan["global_required_data_plan"])


def test_quote_without_contract_returns_the_spot_payload(tmp_path, monkeypatch):
    from test_group_query_bot_api import _body, _service

    service, recorder = _service(tmp_path)
    plan = _fake_plan({"fetch_plan": {}, "expected_fetch_contract": {},
                       "global_required_data_plan": {"plan_id": "p"}})
    payload = {"symbol": "NVDA", "underlier_code": "NVDA", "meta": {},
               "rows": [{"last_price": 185.0, "spot": 185.0,
                         "snapshot_received_at_utc": NOW.isoformat()}]}
    monkeypatch.setattr(service.business, "_fetch", lambda **kwargs: ([payload], plan))
    envelope, status = service.handle("query_quotes", _body({"symbol": "US.NVDA"}))
    assert status == 200
    data = envelope["data"]
    assert data["underlier"]["spot"] == 185.0
    assert data["option"] is None
    assert data["quote_status"] == "current"
    assert data["effective_parameters"]["contract_requested"] is False
    assert recorder.calls == []


def test_quote_requested_contract_without_a_row_is_an_input_error(tmp_path, monkeypatch):
    from test_group_query_bot_api import _body, _service

    service, _recorder = _service(tmp_path)
    plan = _fake_plan({"fetch_plan": {}, "expected_fetch_contract": {},
                       "global_required_data_plan": {"plan_id": "p"}})
    payload = {"symbol": "NVDA", "underlier_code": "NVDA", "meta": {},
               "rows": [{"last_price": 185.0, "spot": 185.0,
                         "snapshot_received_at_utc": NOW.isoformat()}]}
    monkeypatch.setattr(service.business, "_fetch", lambda **kwargs: ([payload], plan))
    body = _body({"symbol": "US.NVDA", "contract": {
        "option_type": "put", "expiration": "2026-12-18", "strike": 180.0}})
    envelope, status = service.handle("query_quotes", body)
    assert status == 400
    assert envelope["error"]["code"] == "INPUT_ERROR"


def test_quote_without_underlier_is_unavailable_not_a_gateway_error(tmp_path, monkeypatch):
    from test_group_query_bot_api import _body, _service

    service, _recorder = _service(tmp_path)
    plan = _fake_plan({"fetch_plan": {}, "expected_fetch_contract": {},
                       "global_required_data_plan": {"plan_id": "p"}})
    monkeypatch.setattr(service.business, "_fetch", lambda **kwargs: ([], plan))
    envelope, status = service.handle("query_quotes", _body({"symbol": "US.NVDA"}))
    assert status == 200
    assert envelope["data"]["underlier"] is None
    assert envelope["data"]["quote_status"] == "unavailable"
    assert envelope["data"]["missing_requirements"] == ["underlier_quote"]


def test_quote_with_contract_separates_option_and_underlier(tmp_path, monkeypatch):
    from test_group_query_bot_api import _body, _service

    service, _recorder = _service(tmp_path)
    plan = _fake_plan({"fetch_plan": {}, "expected_fetch_contract": {},
                       "global_required_data_plan": {"plan_id": "p"}})
    row = {"option_type": "put", "expiration": "2026-12-18", "strike": 180.0,
           "contract_symbol": "US.NVDA261218P180000",
           "currency": "USD", "multiplier": 100, "bid": 1.10, "ask": 1.20,
           "last_price": 1.15, "spot": 185.0,
           "snapshot_received_at_utc": NOW.isoformat(),
           "spot_observed_at_utc": NOW.isoformat(),
           "opening_contract_status": "ready",
           "underlier_observation_status": "ready"}
    payload = {"symbol": "NVDA", "underlier_code": "NVDA", "meta": {},
               "rows": [row]}
    monkeypatch.setattr(service.business, "_fetch", lambda **kwargs: ([payload], plan))
    body = _body({"symbol": "US.NVDA", "contract": {
        "option_type": "put", "expiration": "2026-12-18", "strike": 180.0}})
    envelope, status = service.handle("query_quotes", body)
    assert status == 200
    data = envelope["data"]
    assert data["option"]["bid"] == 1.10 and data["option"]["option_type"] == "put"
    assert data["underlier"]["last_price"] == 185.0
    assert data["underlier"]["spot"] == 185.0
    assert data["underlier"]["contract_symbol"] == "NVDA"
    assert data["underlier"]["option_type"] is None
    assert data["underlier"]["strike"] is None
    assert data["underlier"]["delta"] is None


def test_scenario_response_carries_design_identity_and_evidence(tmp_path, monkeypatch):
    import om_group_query_bot.business as business_module
    from om_group_query_bot.assessment import load_assessment
    from test_group_query_bot_api import _scenario_body, _service
    from test_group_query_bot_scenario_contract import _build, _scenario

    service, _recorder = _service(tmp_path)
    plan = _build(tmp_path, _scenario())
    old, newer = _scenario_rows()
    payload = {"symbol": "NVDA", "underlier_code": "NVDA",
               "meta": {"observed_at_utc": NOW.isoformat(), "source": "futu"},
               "rows": [old]}
    monkeypatch.setattr(business_module, "build_user_provided_scenario_plan",
                        lambda **kwargs: plan)
    monkeypatch.setattr(service.business, "_fetch_scenario_payload",
                        lambda _plan, _dir, _budget: (
                            payload,
                            {"option_type": "put", "min_strike": None,
                             "max_strike": None, "max_expiration": None}))
    envelope, status = service.handle("evaluate_close", _scenario_body())
    assert status == 200
    data = envelope["data"]
    for field in ("market", "evaluated_at_utc", "public_policy_sha256",
                  "assessment_id", "assessment_mode", "input_id",
                  "input_source", "input_as_of_utc", "input", "input_sha256",
                  "binding_sha256", "evidence", "metrics", "quote",
                  "recommendation_state", "evaluation_status", "fee_model"):
        assert field in data, field
    assert data["assessment_mode"] == "current"
    assert data["input"]["operation"] == "close"
    assert len(data["input_sha256"]) == 64 and len(data["binding_sha256"]) == 64
    assert data["evidence"]["plan_id"] == plan["global_required_data_plan"]["plan_id"]
    assert data["currency"] == "USD"
    stored = load_assessment(data_dir=tmp_path / "data",
                             assessment_id=data["assessment_id"],
                             scope_sha256=__import__(
                                 "om_group_query_bot.assessment", fromlist=["x"]
                             ).conversation_scope_sha256(
                                 service.settings.instance_id,
                                 __import__("om_group_query_bot.contracts",
                                            fromlist=["x"]).RequestContext(
                                     "tenant", "chat", "sender", None)),
                             now_utc=NOW)
    assert stored["binding"]["binding_sha256"] == data["binding_sha256"]
