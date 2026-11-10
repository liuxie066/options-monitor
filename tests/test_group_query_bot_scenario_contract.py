"""Account-free scenario plans, assessment evidence and input bindings."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from om_group_query_bot.assessment import (
    AssessmentError,
    build_binding,
    canonical_json_bytes,
    conversation_scope_sha256,
    input_sha256,
    load_assessment,
    new_assessment_id,
    purge_expired_assessments,
    sha256_hex,
    write_assessment,
)
from om_group_query_bot.contracts import RequestContext
from om_group_query_bot.store import DeliveryStore, InputIdentityConflict
from src.application import opend_utils
from src.application.close_advice_required_data import (
    CloseAdviceRequiredDataPlanError,
    ScenarioInputError,
    build_user_provided_scenario_plan,
    scenario_requirement_index,
    validate_user_provided_scenario_plan,
)
from src.application.opend_symbol_chain_fetching import OptionExpirationDiscoveryResult
from src.application.opening_quote_evidence import (
    OPENING_UNDERLIER_OBSERVATION_SCHEMA,
    OpeningUnderlierObservation,
)
from src.application.required_data_planning import _expiration_discovery_cache_key

NOW = datetime(2026, 11, 10, 6, 0, tzinfo=timezone.utc)
POLICY_SHA = "a" * 64
CONTEXT = RequestContext("tenant", "chat", "sender", None)
EXPIRATIONS = ["2026-12-18", "2027-01-15"]


def _scenario(**overrides):
    payload = {
        "operation": "close",
        "input_id": "input-1",
        "input_as_of_utc": (NOW - timedelta(seconds=30)).isoformat(),
        "position": {
            "symbol": "US.NVDA",
            "contract": {"option_type": "put", "expiration": "2026-12-18", "strike": 180.0},
            "side": "short", "contracts": 2,
        },
        "opening_premium": {"amount": 2.5, "unit": "per_share", "currency": "USD"},
    }
    payload.update(overrides)
    return payload


def _caches(now=NOW):
    trading_date = opend_utils.get_trading_date("US").isoformat()
    key = _expiration_discovery_cache_key(
        symbol="NVDA", source="futu", host="127.0.0.1", port=11111,
        trading_date=trading_date)
    discovery = OptionExpirationDiscoveryResult(
        outcome="success_rows", reason_code=None, expirations=list(EXPIRATIONS),
        observed_at_utc=now.isoformat(), completed_at_utc=now.isoformat(),
        request_identity={"symbol": "NVDA", "underlier": "US.NVDA", "source": "futu",
                          "host": "127.0.0.1", "port": 11111,
                          "trading_date": trading_date},
        error=None)
    spot = OpeningUnderlierObservation(
        schema_version=OPENING_UNDERLIER_OBSERVATION_SCHEMA, code="US.NVDA",
        market="US", last_price=185.0, update_time=now.isoformat(),
        observed_at_utc=now.isoformat(), age_seconds=1.0, market_state="NORMAL",
        sec_status="NORMAL", suspension=False, status="ready", reason_code=None)
    return {key: discovery}, {key: spot}


def _build(tmp_path, scenario, *, now=NOW):
    discovery, spot = _caches(now)
    return build_user_provided_scenario_plan(
        scenario=scenario, policy_sha256=POLICY_SHA,
        strategy_version="remaining_yield_capture.v3",
        fee_model_id="futu_option_fee", fee_model_version="futu_option_sell_fee.v1",
        quote_route={"source": "futu", "host": "127.0.0.1", "port": 11111},
        runtime_root=tmp_path / "output_runs" / "a1",
        required_data_dir=tmp_path / "output_runs" / "a1" / "required_data",
        now_utc=now, expiration_discovery_cache=discovery,
        spot_observation_cache=spot,
    )


def _reject(tmp_path, scenario, *, now=NOW):
    with pytest.raises(CloseAdviceRequiredDataPlanError) as caught:
        _build(tmp_path, scenario, now=now)
    assert isinstance(caught.value, ScenarioInputError)


def test_close_plan_rejects_missing_premium_and_wrong_contract(tmp_path):
    scenario = dict(_scenario())
    del scenario["opening_premium"]
    _reject(tmp_path, scenario)
    _reject(tmp_path, _scenario(opening_premium={"amount": 0, "unit": "per_share",
                                                 "currency": "USD"}))
    _reject(tmp_path, _scenario(opening_premium={"amount": 1, "unit": "per_share",
                                                 "currency": "CNY"}))
    _reject(tmp_path, _scenario(opening_premium=None))
    _reject(tmp_path, {**_scenario(), "operation": "roll-down"})


def test_roll_plan_rejects_wrong_direction_bounds_and_premium(tmp_path):
    base = _scenario(operation="roll-down")
    base.pop("opening_premium")
    base["search_bounds"] = {"max_new_expiration": "2027-01-15"}
    _reject(tmp_path, {**base, "position": {**base["position"], "contract": {
        "option_type": "call", "expiration": "2026-12-18", "strike": 180.0}}})
    _reject(tmp_path, {**base, "search_bounds": {"max_new_expiration": "2026-12-01"}})
    _reject(tmp_path, {**base, "opening_premium": {"amount": 1, "unit": "per_share",
                                                   "currency": "USD"}})
    _reject(tmp_path, {**base, "opened_at": "2026-11-01"})
    _reject(tmp_path, {**base, "position": {**base["position"], "side": "long"}})
    _reject(tmp_path, {**base, "position": {**base["position"], "symbol": "U.S.NVDA"}})
    _reject(tmp_path, {**base, "position": {**base["position"], "contracts": 0}})
    _reject(tmp_path, {**base, "limit": 0})
    _reject(tmp_path, {**base, "limit": 51})
    _reject(tmp_path, {**base, "search_bounds": {
        "max_new_expiration": "2027-01-15", "min_new_strike": 180.0}})
    _reject(tmp_path, {**base, "search_bounds": {
        "max_new_expiration": "2027-01-15", "min_new_strike": 200.0}})
    _reject(tmp_path, {**base, "search_bounds": {
        "max_new_expiration": "2027-01-15", "max_new_strike": 150.0}})


def test_close_plan_rejects_expired_contract_and_search_bounds(tmp_path):
    expired = {**_scenario(), "position": {**_scenario()["position"], "contract": {
        "option_type": "put", "expiration": "2026-11-01", "strike": 180.0}}}
    _reject(tmp_path, expired)
    _reject(tmp_path, {**_scenario(), "search_bounds": {"max_new_expiration": "2027-01-15"}})


def test_offline_plan_is_strict_and_carries_no_account_scope(tmp_path):
    plan = _build(tmp_path, _scenario())
    validated = validate_user_provided_scenario_plan(plan)
    assert validated["operation"] == "close"
    assert validated["symbol"] == "US.NVDA"
    assert validated["fetch_symbol"] == "NVDA"
    assert validated["as_of_market_date"] == "2026-11-10"
    assert validated["original_contract"] == {
        "option_type": "put", "expiration": "2026-12-18", "strike": 180.0}
    assert "accounts" not in plan and "portfolio" not in plan
    for key in list(plan):
        partial = dict(plan)
        del partial[key]
        with pytest.raises(ScenarioInputError):
            validate_user_provided_scenario_plan(partial)
    unknown = {**plan, "accounts": {}}
    with pytest.raises(ScenarioInputError):
        validate_user_provided_scenario_plan(unknown)
    tampered = {**plan, "policy_sha256": "not-a-sha"}
    with pytest.raises(ScenarioInputError):
        validate_user_provided_scenario_plan(tampered)
    tampered = {**plan, "global_required_data_plan": {
        **plan["global_required_data_plan"], "plan_id": "0" * 64}}
    with pytest.raises(ScenarioInputError):
        validate_user_provided_scenario_plan(tampered)
    tampered = {**plan, "requirements": []}
    with pytest.raises(ScenarioInputError):
        validate_user_provided_scenario_plan(tampered)
    for field, value in (("as_of_market_date", "2020-13-40"),
                         ("limit", 10_000),
                         ("limit", 0),
                         ("input_id", ""),
                         ("input_as_of_utc", "2026-11-10T05:59:30"),
                         ("opening_premium", None),
                         ("opening_premium", {"amount": -1, "unit": "per_share",
                                              "currency": "USD"}),
                         ("search_bounds", {"max_new_expiration": "2027-01-15"}),
                         ("quote_route", {"source": "futu", "host": "127.0.0.1",
                                          "port": 0}),
                         ("fetch_host", "10.0.0.1")):
        with pytest.raises(ScenarioInputError):
            validate_user_provided_scenario_plan({**plan, field: value})
    for opened_at in ("2026-12-19", "2026-11-11"):
        with pytest.raises(ScenarioInputError):
            validate_user_provided_scenario_plan({**plan, "opened_at": opened_at})


def test_roll_plan_carries_bounded_replacement_scope(tmp_path):
    scenario = _scenario(operation="roll-down")
    scenario.pop("opening_premium")
    scenario["search_bounds"] = {"max_new_expiration": "2027-01-15",
                                 "min_new_strike": 120.0}
    plan = validate_user_provided_scenario_plan(_build(tmp_path, scenario))
    assert plan["search_bounds"] == {"max_new_expiration": "2027-01-15",
                                     "min_new_strike": 120.0}
    side_plans = plan["fetch_plan"]["side_plans"]
    assert side_plans and side_plans[0]["option_type"] == "put"
    assert side_plans[0]["min_dte"] == 38
    assert side_plans[0]["max_dte"] == 66
    # The recall window is capped at the old strike and the platform's 20%
    # near-bound band, so a looser user floor cannot widen it.
    assert side_plans[0]["min_strike"] == 144.0
    assert side_plans[0]["max_strike"] == 180.0
    assert plan["fetch_plan"]["merged_requests"]
    tighter = _scenario(operation="roll-down")
    tighter.pop("opening_premium")
    tighter["search_bounds"] = {"max_new_expiration": "2027-01-15",
                                "min_new_strike": 170.0}
    tighter_side = validate_user_provided_scenario_plan(
        _build(tmp_path, tighter))["fetch_plan"]["side_plans"][0]
    assert tighter_side["min_strike"] == 170.0
    assert tighter_side["max_strike"] == 180.0
    upstream = _scenario(operation="roll-up")
    upstream.pop("opening_premium")
    upstream["position"] = {**upstream["position"], "contract": {
        "option_type": "call", "expiration": "2026-12-18", "strike": 200.0}}
    upstream["search_bounds"] = {"max_new_expiration": "2027-01-15",
                                 "max_new_strike": 260.0}
    call_plan = validate_user_provided_scenario_plan(_build(tmp_path, upstream))
    call_side = call_plan["fetch_plan"]["side_plans"][0]
    assert call_side["option_type"] == "call"
    assert call_side["min_strike"] == 200.0
    # A looser user ceiling is still capped by the platform's 20% band.
    assert call_side["max_strike"] == 240.0


def test_requirement_index_returns_the_exact_fetch_identity(tmp_path):
    plan = _build(tmp_path, _scenario())
    index = scenario_requirement_index(plan)
    assert index["symbol"] == "US.NVDA"
    assert index["plan_id"] == plan["global_required_data_plan"]["plan_id"]
    assert index["fetch_binding"] == plan["expected_fetch_contract"]["fetch_binding"]
    assert "accounts" not in index


def test_assessment_roundtrip_scope_expiry_and_tamper(tmp_path):
    assessment_id = new_assessment_id()
    plan = _build(tmp_path, _scenario())
    scope = conversation_scope_sha256("instance", CONTEXT)
    binding = build_binding(
        principal_id="group-query-bot", scope_sha256=scope, input_id="input-1",
        input_sha256_value="b" * 64,
        input_as_of_utc=(NOW - timedelta(seconds=30)).isoformat(),
        assessment_id=assessment_id, assessed_at_utc=NOW, plan=plan,
        snapshot_manifest_sha256=None, receipts=[],
        payloads=[{"payload_sha256": "c" * 64}],
        policy_sha256=POLICY_SHA, strategy_version="v3",
        fee_model_id="fee", fee_model_version="v1",
    )
    write_assessment(data_dir=tmp_path, assessment_id=assessment_id,
                     scope_sha256=scope, created_at_utc=NOW, plan=plan,
                     binding=binding)
    loaded = load_assessment(data_dir=tmp_path, assessment_id=assessment_id,
                             scope_sha256=scope, now_utc=NOW + timedelta(hours=23))
    assert loaded["binding"]["binding_sha256"] == binding["binding_sha256"]
    assert loaded["binding"]["plan_sha256"] == sha256_hex(canonical_json_bytes(plan))
    with pytest.raises(AssessmentError):
        load_assessment(data_dir=tmp_path, assessment_id=assessment_id,
                        scope_sha256="d" * 64, now_utc=NOW)
    with pytest.raises(AssessmentError):
        load_assessment(data_dir=tmp_path, assessment_id=assessment_id,
                        scope_sha256=scope, now_utc=NOW + timedelta(hours=24))
    path = tmp_path / "assessments" / assessment_id / "binding.json"
    tampered = json.loads(path.read_text())
    tampered["input_sha256"] = "e" * 64
    path.write_text(json.dumps(tampered))
    with pytest.raises(AssessmentError):
        load_assessment(data_dir=tmp_path, assessment_id=assessment_id,
                        scope_sha256=scope, now_utc=NOW)


def test_expired_batches_are_purged_without_touching_fresh_ones(tmp_path):
    scope = conversation_scope_sha256("instance", CONTEXT)
    plan = _build(tmp_path, _scenario())
    fresh, stale = new_assessment_id(), new_assessment_id()
    for assessment_id, created in ((fresh, NOW), (stale, NOW - timedelta(hours=25))):
        binding = build_binding(
            principal_id="p", scope_sha256=scope, input_id="i",
            input_sha256_value="b" * 64, input_as_of_utc=NOW.isoformat(),
            assessment_id=assessment_id, assessed_at_utc=created,
            plan=plan, snapshot_manifest_sha256=None, receipts=[],
            payloads=[], policy_sha256=POLICY_SHA, strategy_version="v3",
            fee_model_id="fee", fee_model_version="v1",
        )
        write_assessment(data_dir=tmp_path, assessment_id=assessment_id,
                         scope_sha256=scope, created_at_utc=created,
                         plan=plan, binding=binding)
    assert purge_expired_assessments(tmp_path, NOW) == 1
    assert (tmp_path / "assessments" / fresh).is_dir()
    assert not (tmp_path / "assessments" / stale).exists()
    stranger = tmp_path / "assessments" / "not-evidence"
    stranger.mkdir(parents=True)
    assert purge_expired_assessments(tmp_path, NOW) == 0
    assert stranger.is_dir()


def test_input_binding_rejects_same_id_with_different_facts(tmp_path):
    store = DeliveryStore(tmp_path)
    scope = conversation_scope_sha256("instance", CONTEXT)
    assert store.bind_input(scope, "input-1", "1" * 64, NOW) is True
    assert store.bind_input(scope, "input-1", "1" * 64, NOW) is False
    with pytest.raises(InputIdentityConflict):
        store.bind_input(scope, "input-1", "2" * 64, NOW)
    assert store.bind_input("f" * 64, "input-1", "2" * 64, NOW) is True
    assert store.bind_input(scope, "input-2", "3" * 64, NOW) is True
    store.cleanup(NOW + timedelta(days=2))
    assert store.bind_input(scope, "input-1", "9" * 64, NOW + timedelta(days=2)) is True


def test_input_digest_excludes_tracking_fields_and_includes_contract_evidence():
    parameters = {"position": {"symbol": "US.NVDA"}, "opening_premium": {"amount": 2.5}}
    first = input_sha256(operation="close", parameters=parameters,
                         contract_evidence={"currency": "USD", "multiplier": 100})
    second = input_sha256(operation="close", parameters=parameters,
                          contract_evidence={"currency": "USD", "multiplier": 100})
    assert first == second
    assert first != input_sha256(operation="close", parameters=parameters,
                                 contract_evidence={"currency": "USD", "multiplier": 10})
    assert first != input_sha256(operation="close",
                                 parameters={**parameters, "opened_at": "2026-01-01"},
                                 contract_evidence={"currency": "USD", "multiplier": 100})


def test_input_digest_changes_for_every_economic_field():
    import copy

    parameters = {
        "position": {"symbol": "US.NVDA", "contract": {
            "option_type": "put", "expiration": "2026-12-18", "strike": 180.0},
            "side": "short", "contracts": 2},
        "opening_premium": {"amount": 2.5, "unit": "per_share", "currency": "USD"},
        "search_bounds": {"max_new_expiration": "2027-01-15", "min_new_strike": 170.0},
        "limit": 10,
    }
    evidence = {"symbol": "US.NVDA", "market": "US", "currency": "USD",
                "multiplier": 100, "delivery": "standard",
                "contract": {"option_type": "put", "expiration": "2026-12-18",
                             "strike": 180.0}}
    baseline = input_sha256(operation="close", parameters=parameters,
                            contract_evidence=evidence)
    assert baseline == input_sha256(operation="close", parameters=parameters,
                                    contract_evidence=evidence)
    mutations = [
        ("close", parameters, {**evidence, "currency": "HKD"}),
        ("close", parameters, {**evidence, "multiplier": 10}),
        ("close", parameters, {**evidence, "symbol": "US.AMD"}),
        ("close", parameters, {**evidence, "market": "HK"}),
        ("close", parameters, {**evidence, "delivery": "non_standard"}),
        ("close", parameters, {**evidence, "contract": {
            **evidence["contract"], "strike": 175.0}}),
        ("roll-down", parameters, evidence),
        ("close", {**parameters, "position": {
            **parameters["position"], "symbol": "US.AMD"}}, evidence),
        ("close", {**parameters, "position": {
            **parameters["position"], "contract": {
                **parameters["position"]["contract"], "option_type": "call"}}}, evidence),
        ("close", {**parameters, "position": {
            **parameters["position"], "contract": {
                **parameters["position"]["contract"], "expiration": "2027-01-15"}}}, evidence),
        ("close", {**parameters, "position": {
            **parameters["position"], "contract": {
                **parameters["position"]["contract"], "strike": 175.0}}}, evidence),
        ("close", {**parameters, "position": {
            **parameters["position"], "side": "long"}}, evidence),
        ("close", {**parameters, "position": {
            **parameters["position"], "contracts": 3}}, evidence),
        ("close", {**parameters, "opening_premium": {
            **parameters["opening_premium"], "amount": 2.6}}, evidence),
        ("close", {**parameters, "opening_premium": {
            **parameters["opening_premium"], "unit": "per_contract"}}, evidence),
        ("close", {**parameters, "opening_premium": {
            **parameters["opening_premium"], "currency": "HKD"}}, evidence),
        ("close", {**parameters, "search_bounds": {
            **parameters["search_bounds"], "max_new_expiration": "2027-02-19"}}, evidence),
        ("close", {**parameters, "search_bounds": {
            **parameters["search_bounds"], "min_new_strike": 165.0}}, evidence),
        ("close", {**parameters, "search_bounds": None}, evidence),
        ("close", {**parameters, "limit": 5}, evidence),
    ]
    for operation, mutated_parameters, mutated_evidence in mutations:
        digest = input_sha256(operation=operation,
                              parameters=copy.deepcopy(mutated_parameters),
                              contract_evidence=copy.deepcopy(mutated_evidence))
        assert digest != baseline
