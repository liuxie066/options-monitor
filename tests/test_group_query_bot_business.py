"""Public candidate policy and shared close economics without account reads."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

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


def test_spot_only_payload_rejects_unavailable_and_suspended_observations():
    from dataclasses import replace

    from om_group_query_bot.business import _spot_payloads
    from src.application.opening_quote_evidence import (
        OPENING_UNDERLIER_OBSERVATION_SCHEMA,
        OpeningUnderlierObservation,
    )

    class _Settings:
        quote_source = "futu"

    observation = OpeningUnderlierObservation(
        schema_version=OPENING_UNDERLIER_OBSERVATION_SCHEMA, code="NVDA",
        market="US", last_price=185.0, update_time="2026-11-10 18:32:10.786",
        observed_at_utc="2026-11-10T23:32:10.786000+00:00", age_seconds=1.0,
        market_state="AFTER_HOURS_BEGIN", sec_status="NORMAL", suspension=False,
        status="market_closed", reason_code="market_closed")
    assert _spot_payloads(_Settings(), "NVDA", observation) != []

    def reason(payloads):
        assert len(payloads) == 1
        assert payloads[0]["rows"] == []
        return payloads[0]["meta"]["underlier_unavailable_reason"]

    assert reason(_spot_payloads(
        _Settings(), "NVDA",
        replace(observation, status="data_unavailable",
                reason_code="underlier_quote_stale"))) == "underlier_quote_stale"
    assert reason(_spot_payloads(
        _Settings(), "NVDA", replace(observation, sec_status="HALT"))) \
        == "underlier_sec_status_not_normal"
    assert reason(_spot_payloads(
        _Settings(), "NVDA", replace(observation, suspension=True))) \
        == "underlier_suspended"
    assert reason(_spot_payloads(
        _Settings(), "NVDA", replace(observation, last_price=None))) \
        == "underlier_last_price_invalid"


def test_missing_underlier_quote_names_the_reason_instead_of_a_generic_field():
    from om_group_query_bot.business import _underlier_missing_reason

    assert _underlier_missing_reason(
        [], {"underlier_sec_status": "HALT"}) == "underlier_sec_status_not_normal"
    assert _underlier_missing_reason(
        [], {"underlier_suspension": True}) == "underlier_suspended"
    assert _underlier_missing_reason(
        [], {"underlier_observation_reason_code": "underlier_quote_stale"}) \
        == "underlier_quote_stale"
    assert _underlier_missing_reason(
        [{"meta": {"underlier_unavailable_reason": "underlier_last_price_invalid"}}],
        None) == "underlier_last_price_invalid"
    assert _underlier_missing_reason([], None) is None


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


def _spot_only_bundle(*, status="ready", market_state="NORMAL", price=185.0,
                      update_time=None, observed_at_utc=None):
    """The real spot-only plan shape: no fetch requests, one observation."""
    from src.application.opend_symbol_chain_fetching import (
        OptionExpirationDiscoveryResult,
    )
    from src.application.opening_quote_evidence import (
        OPENING_UNDERLIER_OBSERVATION_SCHEMA,
        OpeningUnderlierObservation,
    )
    from src.application.required_data_planning import RequiredDataFetchPlanBundle

    observation = OpeningUnderlierObservation(
        schema_version=OPENING_UNDERLIER_OBSERVATION_SCHEMA, code="US.NVDA",
        market="US", last_price=price, update_time=update_time or NOW.isoformat(),
        observed_at_utc=observed_at_utc or NOW.isoformat(), age_seconds=0.0,
        market_state=market_state,
        sec_status="NORMAL", suspension=False, status=status,
        reason_code=None if status == "ready" else status)
    discovery = OptionExpirationDiscoveryResult(
        outcome="success_rows", reason_code=None, expirations=["2026-12-18"],
        observed_at_utc=(NOW - timedelta(seconds=3)).isoformat(),
        completed_at_utc=(NOW - timedelta(seconds=2)).isoformat(),
        request_identity={"symbol": "NVDA", "underlier": "US.NVDA",
                          "source": "opend", "host": "127.0.0.1", "port": 11111,
                          "trading_date": NOW.date().isoformat()})
    return RequiredDataFetchPlanBundle(
        symbol="NVDA", spot_reference=price, side_plans=[], merged_specs=[],
        underlier_observation=observation, expiration_discovery=discovery,
        projection_outcome="success_rows", projected_expirations=["2026-12-18"])


def test_spot_only_plan_is_not_forced_through_the_fetch_contract(tmp_path, monkeypatch):
    from test_group_query_bot_api import _body, _service

    import om_group_query_bot.business as business
    from src.application.required_data_plan_identity import (
        build_required_data_expected_fetch_contract,
    )

    service, recorder = _service(tmp_path)
    bundle = _spot_only_bundle()
    # The shared validator rejects this real shape; the spot-only path must
    # bypass it rather than turn a valid quote into a 503.
    with pytest.raises(ValueError, match="success-rows required-data plan lacks fetch requests"):
        build_required_data_expected_fetch_contract(
            symbol="NVDA", fetch_plan=bundle.to_debug_dict(),
            source=business._fetch_source(service.settings),
            host=service.settings.quote_host, port=service.settings.quote_port)
    monkeypatch.setattr(business, "build_required_data_fetch_plan",
                        lambda **kwargs: bundle)
    envelope, status = service.handle("query_quotes", _body({"symbol": "US.NVDA"}))
    assert status == 200
    data = envelope["data"]
    assert data["underlier"]["spot"] == 185.0
    assert data["currency"] == "USD"
    assert data["underlier"]["currency"] == "USD"
    assert data["option"] is None
    assert data["quote_status"] == "current"
    plan_id = data["evidence"]["plan_id"]
    assert len(plan_id) == 64 and all(ch in "0123456789abcdef" for ch in plan_id)
    assert recorder.calls == []


def test_spot_quote_received_during_the_fetch_is_current_not_future(tmp_path, monkeypatch):
    from test_group_query_bot_api import _body, _service

    service, _recorder = _service(tmp_path)
    clock = [NOW]
    service.business._now = lambda: clock[0]
    plan = _fake_plan({"fetch_plan": {}, "expected_fetch_contract": {},
                       "global_required_data_plan": {"plan_id": "p"}})
    received = NOW + timedelta(seconds=1)
    def slow_fetch(**kwargs):
        clock[0] = NOW + timedelta(seconds=2)
        return ([{"symbol": "NVDA", "underlier_code": "NVDA", "meta": {},
                  "rows": [{"last_price": 185.0, "spot": 185.0,
                            "snapshot_received_at_utc": received.isoformat()}]}], plan)
    monkeypatch.setattr(service.business, "_fetch", slow_fetch)
    envelope, status = service.handle("query_quotes", _body({"symbol": "US.NVDA"}))
    assert status == 200
    data = envelope["data"]
    assert data["quote_status"] == "current"
    assert "quote_snapshot_future" not in data["missing_requirements"]
    assert data["requested_at_utc"] == NOW.isoformat()
    assert data["evaluated_at_utc"] == (NOW + timedelta(seconds=2)).isoformat()


def test_spot_quote_is_last_known_with_its_time_while_the_market_is_closed(tmp_path, monkeypatch):
    from test_group_query_bot_api import _body, _service
    import om_group_query_bot.business as business
    from om_group_query_bot.render import render_quotes

    service, recorder = _service(tmp_path)
    # 18:32 New York the previous evening: a genuinely stale observation that
    # must still render as last_known because no newer session exists.
    bundle = _spot_only_bundle(status="market_closed",
                               market_state="AFTER_HOURS_BEGIN", price=168.89,
                               update_time="2026-11-09 18:32:10.786",
                               observed_at_utc="2026-11-09T23:32:10.786000+00:00")
    monkeypatch.setattr(business, "build_required_data_fetch_plan",
                        lambda **kwargs: bundle)
    envelope, status = service.handle("query_quotes", _body({"symbol": "US.NVDA"}))
    assert status == 200
    data = envelope["data"]
    assert data["quote_status"] == "last_known"
    assert data["missing_requirements"] == []
    underlier = data["underlier"]
    assert underlier["last_price"] == 168.89 and underlier["spot"] == 168.89
    assert underlier["currency"] == "USD"
    assert underlier["market_state"] == "AFTER_HOURS_BEGIN"
    assert underlier["underlier_observation_status"] == "market_closed"
    assert underlier["last_price_update_time"] == "2026-11-09 18:32:10.786"
    text = render_quotes(data)
    assert "行情状态：最近已知" in text
    assert "最新 168.8900" in text
    assert "最新价时间 2026-11-09 18:32:10.786（纽约时间）" in text
    assert "市场状态 盘后（AFTER_HOURS_BEGIN）" in text
    assert "非当前可成交状态" in text
    assert recorder.calls == []


def test_quote_status_keeps_only_the_closed_market_as_last_known():
    from om_group_query_bot.business import _quote_status

    fresh = {"snapshot_received_at_utc": NOW.isoformat(),
             "last_price": 185.0,
             "last_price_update_time": "2026-11-10 18:32:10.786"}
    missing = []
    assert _quote_status(
        {**fresh, "underlier_observation_status": "market_closed"},
        NOW, missing) == "last_known"
    assert missing == []
    assert _quote_status(
        {**fresh, "opening_contract_status": "market_closed"},
        NOW, missing) == "last_known"
    assert _quote_status(
        {**fresh, "underlier_observation_status": "data_unavailable"},
        NOW, missing) == "unavailable"
    assert "underlier_observation_status_not_ready" in missing
    # A closed market stays last_known regardless of how old the observation
    # is: there is no newer session to wait for.
    stale = {"snapshot_received_at_utc": (NOW - timedelta(days=2)).isoformat(),
             "last_price": 185.0,
             "last_price_update_time": "2026-11-08 18:32:10.786"}
    assert _quote_status(
        {**stale, "underlier_observation_status": "market_closed"},
        NOW, missing) == "last_known"


def test_closed_market_without_a_displayable_price_is_unavailable():
    from om_group_query_bot.business import _quote_status

    fresh = {"snapshot_received_at_utc": NOW.isoformat(),
             "opening_contract_status": "market_closed"}
    missing = []
    assert _quote_status(fresh, NOW, missing) == "unavailable"
    assert "option_quote_unavailable" in missing
    # A zero price is a placeholder, not a last observation.
    missing = []
    assert _quote_status(
        {**fresh, "last_price": 0.0, "bid": 0.0, "ask": 0.0},
        NOW, missing) == "unavailable"
    assert "option_quote_unavailable" in missing
    # A last trade without its own clock cannot be displayed as last known,
    # but a live bid/ask side still can be.
    missing = []
    assert _quote_status(
        {**fresh, "last_price": 3.25, "last_price_update_time": "N/A"},
        NOW, missing) == "unavailable"
    assert "option_quote_unavailable" in missing
    missing = []
    assert _quote_status(
        {**fresh, "last_price": 3.25, "last_price_update_time": "N/A",
         "bid": 3.1, "ask": 3.4}, NOW, missing) == "last_known"
    assert missing == []


def test_quote_status_refuses_halted_or_suspended_instruments_when_closed():
    from om_group_query_bot.business import _quote_status

    fresh = {"snapshot_received_at_utc": NOW.isoformat()}
    missing = []
    assert _quote_status(
        {**fresh, "underlier_observation_status": "market_closed",
         "underlier_sec_status": "HALT"}, NOW, missing) == "unavailable"
    assert "underlier_sec_status_not_normal" in missing
    missing = []
    assert _quote_status(
        {**fresh, "opening_contract_status": "market_closed",
         "option_sec_status": "NORMAL", "option_suspension": True},
        NOW, missing) == "unavailable"
    assert "option_suspended" in missing


def test_closed_market_contract_quote_is_last_known_not_unavailable(tmp_path, monkeypatch):
    from test_group_query_bot_api import _body, _service

    service, _recorder = _service(tmp_path)
    plan = _fake_plan({"fetch_plan": {}, "expected_fetch_contract": {},
                       "global_required_data_plan": {"plan_id": "p"}})
    row = {
        "contract_symbol": "US.NVDA261218P00180000", "option_type": "put",
        "expiration": "2026-12-18", "strike": 180.0, "currency": "USD",
        "bid": 1.10, "ask": 1.20, "last_price": 1.15,
        "snapshot_received_at_utc": NOW.isoformat(),
        "last_price_update_time": "2026-11-10 18:00:00.000",
        "spot_update_time": "2026-11-10 18:32:10.786",
        "market_state": "AFTER_HOURS_BEGIN",
        "opening_contract_status": "market_closed",
        "underlier_observation_status": "market_closed",
        "spot": 185.0, "spot_observed_at_utc": NOW.isoformat(),
        "underlier_sec_status": "NORMAL", "underlier_suspension": False,
    }
    payload = {"symbol": "NVDA", "underlier_code": "NVDA", "meta": {},
               "rows": [row]}
    monkeypatch.setattr(service.business, "_fetch", lambda **kwargs: ([payload], plan))
    body = _body({"symbol": "US.NVDA", "contract": {
        "option_type": "put", "expiration": "2026-12-18", "strike": 180.0}})
    envelope, status = service.handle("query_quotes", body)
    assert status == 200
    data = envelope["data"]
    assert data["quote_status"] == "last_known"
    assert data["missing_requirements"] == []
    assert data["option"]["last_price"] == 1.15
    assert data["option"]["opening_contract_status"] == "market_closed"
    assert data["option"]["last_price_update_time"] == "2026-11-10 18:00:00.000"
    assert data["underlier"]["last_price"] == 185.0
    assert data["underlier"]["underlier_observation_status"] == "market_closed"
    # The underlier quote reports the underlier's own observation time, not
    # the option contract's last trade time from the same row.
    assert data["underlier"]["last_price_update_time"] == "2026-11-10 18:32:10.786"


def test_scenario_reports_the_closed_market_instead_of_a_generic_reason():
    from om_group_query_bot.business import _scenario_evidence_reason

    row = {"opening_contract_status": "market_closed",
           "underlier_observation_status": "market_closed",
           "snapshot_received_at_utc": NOW.isoformat()}
    assert _scenario_evidence_reason(
        row, option_type="put", quote_side="buy", now=NOW) == "market_closed"
    # After hours the option snapshot may be absent or older than the
    # decision window; the member still needs the market reason, not a
    # generic evidence failure.
    assert _scenario_evidence_reason(
        {k: v for k, v in row.items() if k != "snapshot_received_at_utc"},
        option_type="put", quote_side="buy", now=NOW) == "market_closed"
    assert _scenario_evidence_reason(
        {**row, "snapshot_received_at_utc": (NOW - timedelta(days=1)).isoformat()},
        option_type="put", quote_side="buy", now=NOW) == "market_closed"
    assert _scenario_evidence_reason(
        {**row, "opening_contract_status": "ineligible"},
        option_type="put", quote_side="buy", now=NOW) != "market_closed"


def test_scenario_names_a_suspended_or_halted_security_before_the_market_state():
    from om_group_query_bot.business import _scenario_evidence_reason

    row = {"opening_contract_status": "market_closed",
           "underlier_observation_status": "market_closed",
           "snapshot_received_at_utc": NOW.isoformat()}
    for override, expected in (
            ({"option_suspension": True}, "option_suspended"),
            ({"option_sec_status": "HALT"}, "option_sec_status_not_normal"),
            ({"underlier_suspension": True}, "underlier_suspended"),
            ({"underlier_sec_status": "HALT"}, "underlier_sec_status_not_normal")):
        assert _scenario_evidence_reason(
            {**row, **override}, option_type="put", quote_side="buy", now=NOW) \
            == expected
    # The same ordering holds while the market is open: a halted contract is
    # not reported as a market-state problem.
    live = {"opening_contract_status": "ready",
            "underlier_observation_status": "ready",
            "snapshot_received_at_utc": NOW.isoformat()}
    assert _scenario_evidence_reason(
        {**live, "option_sec_status": "HALT"},
        option_type="put", quote_side="buy", now=NOW) \
        == "option_sec_status_not_normal"


def test_underlier_quote_without_its_own_time_does_not_borrow_the_option_time():
    from om_group_query_bot.business import _underlier_quote

    quote = _underlier_quote({
        "spot": 185.0,
        "snapshot_received_at_utc": NOW.isoformat(),
        "spot_update_time": "2026-11-10 18:00:00.000",
        "underlier_sec_status": "NORMAL",
    }, "NVDA")
    assert quote["last_price"] == 185.0
    assert quote["last_price_update_time"] == "2026-11-10 18:00:00.000"
    # The option row's receipt is when the *option* snapshot arrived; it must
    # not become the underlier's observation time.
    assert quote["spot_observed_at_utc"] is None
    assert quote["snapshot_received_at_utc"] is None


def test_quote_status_folds_an_unavailable_leg_into_the_response(tmp_path, monkeypatch):
    from test_group_query_bot_api import _body, _service

    service, _recorder = _service(tmp_path)
    plan = _fake_plan({"fetch_plan": {}, "expected_fetch_contract": {},
                       "global_required_data_plan": {"plan_id": "p"}})
    row = {
        "contract_symbol": "US.NVDA261218P00180000", "option_type": "put",
        "expiration": "2026-12-18", "strike": 180.0, "currency": "USD",
        "bid": 1.10, "ask": 1.20, "last_price": 1.15,
        "snapshot_received_at_utc": NOW.isoformat(),
        "last_price_update_time": "2026-11-10 18:00:00.000",
        "spot_update_time": "2026-11-10 18:32:10.786",
        "market_state": "AFTER_HOURS_BEGIN",
        "opening_contract_status": "data_unavailable",
        "underlier_observation_status": "market_closed",
        "spot": 185.0, "spot_observed_at_utc": NOW.isoformat(),
        "underlier_sec_status": "NORMAL",
    }
    payload = {"symbol": "NVDA", "underlier_code": "NVDA", "meta": {},
               "rows": [row]}
    monkeypatch.setattr(service.business, "_fetch", lambda **kwargs: ([payload], plan))
    body = _body({"symbol": "US.NVDA", "contract": {
        "option_type": "put", "expiration": "2026-12-18", "strike": 180.0}})
    envelope, status = service.handle("query_quotes", body)
    assert status == 200
    data = envelope["data"]
    assert data["quote_status"] == "unavailable"
    assert "opening_contract_status_not_ready" in data["missing_requirements"]
    assert data["underlier"]["last_price_update_time"] == "2026-11-10 18:32:10.786"


def test_candidates_pass_the_closed_market_reference_into_evaluation(tmp_path, monkeypatch):
    from test_group_query_bot_api import _body, _service

    service, _recorder = _service(tmp_path)
    plan = _fake_plan({"fetch_plan": {}, "expected_fetch_contract": {},
                       "global_required_data_plan": {"plan_id": "p"}})
    reference = {"reference_date": "2026-11-10",
                 "valid_until_utc": "2026-11-11T14:30:00+00:00",
                 "calendar": {"sessions": {"2026-11-10": "WHOLE"}}}
    payload = {"symbol": "NVDA", "underlier_code": "NVDA",
               "meta": {"underlier_observation": {}, "source": "opend",
                        "closed_market_reference": reference},
               "rows": []}
    captured = {}
    def fake_evaluate(prepared, merged, fx, index, now):
        captured["merged"] = merged
        return {"outcome": "unable", "reason_codes": []}
    monkeypatch.setattr(service.business, "_fetch", lambda **kwargs: ([payload], plan))
    monkeypatch.setattr("om_group_query_bot.business.evaluate_public_candidates",
                        fake_evaluate)
    service.business.dependencies = Dependencies(
        fetch_symbol=service.business.dependencies.fetch_symbol,
        exchange_rates=lambda *args, **kwargs: {"USDCNY": {"rate": 7.1}})
    envelope, status = service.handle(
        "query_candidates",
        _body({"symbol": "US.NVDA", "strategy": "short_put", "limit": 5}))
    assert status == 200
    assert captured["merged"]["meta"]["closed_market_reference"] == reference


def test_candidates_request_closed_market_reference_from_the_fetch(tmp_path, monkeypatch):
    from test_group_query_bot_api import _body, _service
    from src.application.opend_symbol_chain_fetching import (
        OptionExpirationDiscoveryResult,
    )
    from src.application.required_data_planning import (
        RequiredDataFetchPlanBundle,
        RequiredDataFetchSpec,
    )

    service, _recorder = _service(tmp_path)
    captured = {}

    spec = RequiredDataFetchSpec(
        symbol="NVDA", limit_expirations=0, host="127.0.0.1", port=11111,
        option_types=("put",), explicit_expirations=["2026-12-18"],
        min_dte=20, max_dte=60, side_strike_windows={},
        include_realized_volatility=False, trading_date="2026-11-10")
    discovery = OptionExpirationDiscoveryResult(
        outcome="success_rows", reason_code=None, expirations=["2026-12-18"],
        observed_at_utc=(NOW - timedelta(seconds=3)).isoformat(),
        completed_at_utc=(NOW - timedelta(seconds=2)).isoformat(),
        request_identity={"symbol": "NVDA", "underlier": "US.NVDA",
                          "source": "opend", "host": "127.0.0.1", "port": 11111,
                          "trading_date": NOW.date().isoformat()})
    bundle = RequiredDataFetchPlanBundle(
        symbol="NVDA", spot_reference=185.0, side_plans=[],
        merged_specs=[spec], expiration_discovery=discovery,
        projection_outcome="success_rows", projected_expirations=["2026-12-18"])

    def fake_build_request(spec, *, allow_closed_market_reference=False):
        captured["allow_closed_market_reference"] = allow_closed_market_reference
        return object()

    def fake_fetch(base, request):
        return {"symbol": "NVDA", "underlier_code": "NVDA", "meta": {},
                "rows": []}

    monkeypatch.setattr("om_group_query_bot.business.build_required_data_fetch_plan",
                        lambda **kwargs: bundle)
    monkeypatch.setattr(
        "om_group_query_bot.business.build_required_data_expected_fetch_contract",
        lambda **kwargs: {"fetch_binding": {}})
    monkeypatch.setattr("om_group_query_bot.business.build_fetch_request_from_spec",
                        fake_build_request)
    service.business.dependencies = Dependencies(
        fetch_symbol=fake_fetch,
        exchange_rates=lambda *args, **kwargs: {"USDCNY": {"rate": 7.1}})
    monkeypatch.setattr("om_group_query_bot.business.evaluate_public_candidates",
                        lambda *args, **kwargs: {"outcome": "unable", "reason_codes": []})
    envelope, status = service.handle(
        "query_candidates",
        _body({"symbol": "US.NVDA", "strategy": "short_put", "limit": 5}))
    assert status == 200
    assert captured["allow_closed_market_reference"] is True


def test_candidates_judge_fx_and_candidates_after_their_fetches(tmp_path, monkeypatch):
    from test_group_query_bot_api import _body, _service
    from src.infrastructure.exchange_rates import rates_for_purpose
    from domain.domain.performance.cash_conversion import TENCENT_EXCHANGE_RATE_SOURCE

    service, _recorder = _service(tmp_path)
    clock = [NOW]
    service.business._now = lambda: clock[0]
    plan = _fake_plan({"fetch_plan": {}, "expected_fetch_contract": {},
                       "global_required_data_plan": {"plan_id": "p"}, "symbol": "NVDA"})
    captured = {}
    def slow_fetch(**kwargs):
        clock[0] = NOW + timedelta(seconds=2)
        return ([{"symbol": "NVDA", "underlier_code": "NVDA", "meta": {}, "rows": []}], plan)
    fx_quote_at = NOW + timedelta(seconds=1)
    def fake_rates(settings, now, budget):
        captured["rates_clock"] = now
        return {"pairs": {"USDCNY": {
            "rate": 7.1, "source": TENCENT_EXCHANGE_RATE_SOURCE,
            "quote_at_utc": fx_quote_at.isoformat(),
            "observed_at_utc": fx_quote_at.isoformat()}}}
    def fake_evaluate(prepared, merged, fx, index, now):
        captured["evaluate_clock"] = now
        captured["fx"] = fx
        return {"outcome": "unable", "reason_codes": []}
    monkeypatch.setattr(service.business, "_fetch", slow_fetch)
    service.business.dependencies = Dependencies(
        fetch_symbol=service.business.dependencies.fetch_symbol,
        exchange_rates=fake_rates)
    monkeypatch.setattr("om_group_query_bot.business.evaluate_public_candidates",
                        fake_evaluate)
    envelope, status = service.handle(
        "query_candidates",
        _body({"symbol": "US.NVDA", "strategy": "short_put", "limit": 5}))
    assert status == 200
    assert captured["rates_clock"] == NOW + timedelta(seconds=2)
    assert captured["evaluate_clock"] == NOW + timedelta(seconds=2)
    assert rates_for_purpose(captured["fx"], purpose="capacity",
                             now=captured["evaluate_clock"]) == {"USDCNY": 7.1}
    assert envelope["data"]["evaluated_at_utc"] == (NOW + timedelta(seconds=2)).isoformat()


def test_default_exchange_rates_lets_the_snapshot_sample_its_own_clock(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from test_group_query_bot_api import _settings
    import om_group_query_bot.business as business

    captured = {}
    monkeypatch.setattr(business, "current_exchange_rate_snapshot",
                        lambda **kwargs: captured.update(kwargs) or {})
    dependencies = Dependencies.defaults()
    settings = _settings(tmp_path)
    deps = dependencies.exchange_rates(
        settings, NOW, SimpleNamespace(deadline_monotonic=None, expired=lambda: False))
    assert deps == {}
    assert "now" not in captured
    assert captured["write_cache"] is False


def test_candidates_reprepare_after_a_fetch_crossing_market_midnight(tmp_path, monkeypatch):
    from test_group_query_bot_api import _body, _service

    service, _recorder = _service(tmp_path)
    request_clock = datetime(2026, 11, 11, 4, 59, 59, tzinfo=timezone.utc)  # 23:59:59 New York
    eval_clock = datetime(2026, 11, 11, 5, 0, 1, tzinfo=timezone.utc)       # 00:00:01 New York
    clock = [request_clock]
    service.business._now = lambda: clock[0]
    plan = _fake_plan({"fetch_plan": {}, "expected_fetch_contract": {},
                       "global_required_data_plan": {"plan_id": "p"}})
    def slow_fetch(**kwargs):
        clock[0] = eval_clock
        return ([{"symbol": "NVDA", "underlier_code": "NVDA", "meta": {},
                  "rows": []}], plan)
    monkeypatch.setattr(service.business, "_fetch", slow_fetch)
    service.business.dependencies = Dependencies(
        fetch_symbol=service.business.dependencies.fetch_symbol,
        exchange_rates=lambda settings, now, budget: {})
    envelope, status = service.handle(
        "query_candidates",
        _body({"symbol": "US.NVDA", "strategy": "short_put", "limit": 5}))
    assert status == 200
    assert "date_changed" not in envelope["data"]["reason_codes"]


def test_contract_quotes_still_fail_closed_without_fetch_requests(tmp_path, monkeypatch):
    from test_group_query_bot_api import _body, _service
    import om_group_query_bot.business as business

    service, _recorder = _service(tmp_path)
    monkeypatch.setattr(business, "build_required_data_fetch_plan",
                        lambda **kwargs: _spot_only_bundle())
    envelope, status = service.handle("query_quotes", _body({
        "symbol": "US.NVDA",
        "contract": {"option_type": "put", "expiration": "2026-12-18",
                     "strike": 180.0}}))
    assert status == 503
    assert envelope["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
