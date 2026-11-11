"""Strict five-method request contracts, budget and explicit API settings."""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

import pytest

from om_group_query_bot.budget import (
    AdmissionRejected,
    BudgetExhausted,
    RunBudget,
    SlidingWindowLimiter,
)
from om_group_query_bot.config import load_api_settings
from om_group_query_bot.contracts import (
    ContractError,
    ROUTES,
    VerifiedPrincipal,
    error_envelope,
    normalize_request,
)

NOW = datetime(2026, 11, 10, 4, 0, tzinfo=timezone.utc)


def _context(thread=None):
    return {"tenant_key": "tenant", "chat_id": "chat", "sender_open_id": "sender",
            "thread_id": thread}


def _body(parameters, *, method="query_quotes", deadline=60, **extra):
    body = {
        "schema_version": "group_query_request.v1",
        "request_id": "req-1",
        "request_deadline_utc": (NOW + timedelta(seconds=deadline)).isoformat(),
        "request_context": _context(),
        "parameters": parameters,
    }
    body.update(extra)
    return body


def _scenario_body(parameters, *, input_id="input-1", input_as_of_utc=None, **extra):
    body = _body(parameters, **extra)
    body["input_id"] = input_id
    body["input_as_of_utc"] = (input_as_of_utc or
                               (NOW - timedelta(seconds=30)).isoformat())
    return body


def _contract(option_type="put"):
    return {"option_type": option_type, "expiration": "2026-12-18", "strike": 180.0}


def _position(option_type="put"):
    return {"symbol": "US.NVDA", "contract": _contract(option_type),
            "side": "short", "contracts": 2}


def _reject(method, body):
    with pytest.raises(ContractError) as caught:
        normalize_request(method, body, now=NOW)
    assert caught.value.code == "INPUT_ERROR"
    return caught.value


def test_routes_are_frozen_to_five_methods():
    assert ROUTES == {
        "query_quotes": ("/quotes", "market:read"),
        "query_candidates": ("/candidates", "candidates:evaluate"),
        "evaluate_close": ("/scenarios/close", "scenario:close"),
        "evaluate_roll_down": ("/scenarios/roll-down", "scenario:roll-down"),
        "evaluate_roll_up": ("/scenarios/roll-up", "scenario:roll-up"),
    }


def test_quotes_accepts_user_symbol_forms_and_optional_contract():
    request = normalize_request("query_quotes",
                                _body({"symbol": "us.nvda"}), now=NOW)
    assert request.parameters == {"symbol": "NVDA"}
    assert request.market == "US"
    assert request.input_id is None
    by_prefix = normalize_request(
        "query_quotes", _body({"symbol": "US.NVDA"}), now=NOW)
    assert by_prefix.parameters == {"symbol": "NVDA"}
    with_contract = normalize_request(
        "query_quotes", _body({"symbol": "NVDA", "contract": _contract()}), now=NOW)
    assert with_contract.parameters["contract"] == {
        "option_type": "put", "expiration": "2026-12-18", "strike": 180.0}
    _reject("query_quotes", _body({"symbol": "HK.NVDA"}))
    _reject("query_quotes", _body({"symbol": "SH.600000"}))
    _reject("query_quotes", _body({"symbol": "US.NVDA270115C190000"}))
    _reject("query_quotes", _body({"symbol": "NVDA", "contract": "not-an-object"}))
    _reject("query_quotes", _body({"symbol": "NVDA", "account": "lx"}))
    _reject("query_quotes", _body({"symbol": "NVDA", "contract": {
        "option_type": "put", "expiration": "2026-12-18", "strike": True}}))


def test_symbol_entrypoint_canonicalizes_us_hk_and_aliases():
    for raw, expected, market in (
        ("NVDA", "NVDA", "US"),
        ("nvda", "NVDA", "US"),
        ("US.NVDA", "NVDA", "US"),
        ("0700", "0700.HK", "HK"),
        ("700", "0700.HK", "HK"),
        ("0700.HK", "0700.HK", "HK"),
        ("HK.0700", "0700.HK", "HK"),
        ("腾讯", "0700.HK", "HK"),
        ("US.BRK.B", "BRK.B", "US"),
        ("US.MET", "MET.US", "US"),
    ):
        request = normalize_request("query_quotes", _body({"symbol": raw}), now=NOW)
        assert request.parameters == {"symbol": expected}
        assert request.market == market


def test_queries_reject_scenario_identity_and_server_reserved_fields():
    for extra in ({"input_id": "i"}, {"input_as_of_utc": NOW.isoformat()},
                  {"principal_id": "lx"}, {"assessment_id": "a"},
                  {"input_sha256": "0" * 64}, {"binding_sha256": "0" * 64},
                  {"contract_evidence": {}}, {"parameters": {"symbol": "US.NVDA",
                                                            "opened_at": "2026-01-01"}}):
        body = _body({"symbol": "US.NVDA"})
        body.update(extra)
        _reject("query_quotes", body)


def test_scenarios_require_input_identity_and_reject_query_fields():
    parameters = {"position": _position(),
                  "opening_premium": {"amount": 2.5, "unit": "per_share", "currency": "USD"}}
    request = normalize_request("evaluate_close", _scenario_body(parameters), now=NOW)
    assert request.market == "US" and request.symbol == "NVDA"
    assert request.input_id == "input-1"
    assert request.input_as_of_utc == NOW - timedelta(seconds=30)
    body = _body(parameters)
    with pytest.raises(ContractError) as caught:
        normalize_request("evaluate_close", body, now=NOW)
    assert caught.value.code == "INPUT_ERROR"
    _reject("evaluate_close", _scenario_body(
        parameters, input_id=""))
    _reject("evaluate_close", _scenario_body(
        parameters, input_as_of_utc=(NOW + timedelta(seconds=5)).isoformat()))
    _reject("evaluate_close", _scenario_body(
        {**parameters, "search_bounds": {"max_new_expiration": "2027-01-15"}}))


def test_candidates_validate_delta_range_dte_limit_and_unknown_fields():
    parameters = {"symbol": "US.NVDA", "strategy": "short_put",
                  "abs_delta_range": [0.18, 0.22], "dte": [20, 60],
                  "strike": 180, "limit": 5}
    request = normalize_request("query_candidates", _body(parameters), now=NOW)
    assert request.parameters == {"symbol": "NVDA", "strategy": "short_put",
                                  "abs_delta_range": [0.18, 0.22], "dte": [20, 60],
                                  "strike": 180.0, "limit": 5}
    assert normalize_request(
        "query_candidates", _body({"symbol": "US.NVDA", "strategy": "short_put"}),
        now=NOW).parameters["limit"] == 10
    invalids = [
        {"symbol": "US.NVDA", "strategy": "wheel"},
        {"symbol": "US.NVDA", "strategy": "short_put", "abs_delta_range": [0.3, 0.2]},
        {"symbol": "US.NVDA", "strategy": "short_put", "abs_delta_range": [-0.1, 0.2]},
        {"symbol": "US.NVDA", "strategy": "short_put", "abs_delta_range": [0.1, 1.1]},
        {"symbol": "US.NVDA", "strategy": "short_put", "dte": [60, 20]},
        {"symbol": "US.NVDA", "strategy": "short_put", "dte": [0, 20]},
        {"symbol": "US.NVDA", "strategy": "short_put", "limit": 0},
        {"symbol": "US.NVDA", "strategy": "short_put", "limit": True},
        {"symbol": "US.NVDA", "strategy": "short_put", "limit": 51},
        {"symbol": "US.NVDA", "strategy": "short_put", "strike": 0},
        {"symbol": "US.NVDA", "strategy": "short_put", "account": "lx"},
        {"symbol": "US.NVDA", "strategy": "short_put", "delta": 0.2},
    ]
    for parameters in invalids:
        _reject("query_candidates", _body(parameters))


def test_rolls_require_direction_search_bounds_and_reject_other_side():
    down = _scenario_body({"position": _position("put"),
                           "search_bounds": {"max_new_expiration": "2027-01-15",
                                             "min_new_strike": 120}})
    request = normalize_request("evaluate_roll_down", down, now=NOW)
    assert request.parameters["search_bounds"] == {
        "max_new_expiration": "2027-01-15", "min_new_strike": 120.0}
    assert request.parameters["limit"] == 10
    minimal = _scenario_body({"position": _position("put"),
                              "search_bounds": {"max_new_expiration": "2027-01-15"},
                              "limit": 5})
    assert normalize_request("evaluate_roll_down", minimal,
                             now=NOW).parameters["limit"] == 5
    _reject("evaluate_roll_down", _scenario_body({
        "position": _position("call"),
        "search_bounds": {"max_new_expiration": "2027-01-15"}}))
    _reject("evaluate_roll_up", _scenario_body({
        "position": _position("put"),
        "search_bounds": {"max_new_expiration": "2027-01-15"}}))
    _reject("evaluate_roll_down", _scenario_body({
        "position": _position("put"), "search_bounds": {"max_new_expiration": "2027-1-5"}}))
    _reject("evaluate_roll_down", _scenario_body({
        "position": _position("put"), "opening_premium": {"amount": 2, "unit": "per_share",
                                                          "currency": "USD"},
        "search_bounds": {"max_new_expiration": "2027-01-15"}}))
    _reject("evaluate_roll_down", _scenario_body({
        "position": _position("put"),
        "search_bounds": {"max_new_expiration": "2027-01-15",
                          "min_new_strike": 180}}))
    _reject("evaluate_roll_down", _scenario_body({
        "position": _position("put"),
        "search_bounds": {"max_new_expiration": "2027-01-15",
                          "max_new_strike": 150}}))
    _reject("evaluate_roll_up", _scenario_body({
        "position": _position("call"),
        "search_bounds": {"max_new_expiration": "2027-01-15",
                          "max_new_strike": 180}}))
    _reject("evaluate_roll_down", _scenario_body({
        "position": _position("put"), "limit": 0,
        "search_bounds": {"max_new_expiration": "2027-01-15"}}))
    _reject("evaluate_roll_down", _scenario_body({
        "position": _position("put"), "limit": 51,
        "search_bounds": {"max_new_expiration": "2027-01-15"}}))


def test_request_id_must_be_a_bounded_identifier():
    for invalid in ("../escape", "a/b", "with space", "x" * 129, ".."):
        body = _body({"symbol": "US.NVDA"})
        body["request_id"] = invalid
        _reject("query_quotes", body)
    body = _body({"symbol": "US.NVDA"})
    body["request_id"] = "turn-01:abc_DEF.2"
    assert normalize_request("query_quotes", body, now=NOW).request_id == "turn-01:abc_DEF.2"


def test_premium_units_and_contract_whitelist_are_strict():
    base = {"position": _position()}
    for unit in ("per_share", "per_contract", "total"):
        normalize_request("evaluate_close", _scenario_body(
            {**base, "opening_premium": {"amount": 2.5, "unit": unit, "currency": "USD"}}),
            now=NOW)
    for premium in ({"amount": 0, "unit": "per_share", "currency": "USD"},
                    {"amount": math.nan, "unit": "per_share", "currency": "USD"},
                    {"amount": 2.5, "unit": "lot", "currency": "USD"},
                    {"amount": 2.5, "unit": "per_share", "currency": "CNY"},
                    {"amount": 2.5, "unit": "per_share"}):
        _reject("evaluate_close", _scenario_body({**base, "opening_premium": premium}))
    _reject("evaluate_close", _scenario_body({
        **base, "opening_premium": {"amount": 2.5, "unit": "per_share", "currency": "USD"},
        "position": {"symbol": "US.NVDA", "contract": _contract(), "side": "long",
                     "contracts": 1}}))
    _reject("evaluate_close", _scenario_body({
        **base, "position": {"symbol": "US.NVDA", "contract": _contract(), "side": "short",
                             "contracts": 1, "multiplier": 100},
        "opening_premium": {"amount": 2.5, "unit": "per_share", "currency": "USD"}}))


def test_outer_envelope_and_deadline_rules():
    _reject("query_quotes", {"schema_version": "v0"})
    expired = _body({"symbol": "US.NVDA"}, deadline=-1)
    with pytest.raises(ContractError) as caught:
        normalize_request("query_quotes", expired, now=NOW)
    assert caught.value.code == "UPSTREAM_TIMEOUT"
    naive = _body({"symbol": "US.NVDA"}, request_deadline_utc="2026-11-10T04:01:00")
    _reject("query_quotes", naive)
    with pytest.raises(ContractError) as caught:
        normalize_request("unknown_method", _body({"symbol": "US.NVDA"}), now=NOW)
    assert caught.value.code == "NOT_FOUND"
    _reject("query_quotes", _body({"symbol": "US.NVDA"},
                                  request_context={**_context(), "extra": 1}))
    _reject("query_quotes", _body({"symbol": "US.NVDA"}, request_id=""))


def test_verified_principal_has_no_account_scope_and_explicit_grants():
    principal = VerifiedPrincipal("group-query-bot", "instance", "app",
                                  frozenset({"market:read"}), frozenset({"US"}))
    assert principal.allows("market:read", "US")
    assert not principal.allows("market:read", "HK")
    assert not principal.allows("scenario:close", "US")
    with pytest.raises(ValueError):
        VerifiedPrincipal("p", "i", "a", frozenset({"market:read"}), frozenset({"US"}),
                          account_scope=("lx",))
    with pytest.raises(ValueError):
        VerifiedPrincipal("p", "i", "a", frozenset({"accounts:read"}), frozenset({"US"}))


def test_error_envelope_is_bounded_and_has_no_internal_details():
    envelope = error_envelope("req-1", "INPUT_ERROR", "bad input")
    assert envelope == {"schema_version": "group_query_response.v1",
                        "request_id": "req-1", "ok": False, "data": None,
                        "warnings": [],
                        "error": {"code": "INPUT_ERROR", "message": "bad input",
                                  "retryable": False, "retry_after_seconds": None}}


def test_run_budget_takes_the_smaller_deadline_and_never_extends():
    clock = [100.0]
    budget = RunBudget.from_request(
        deadline_utc=NOW + timedelta(seconds=60), now_utc=NOW,
        server_max_seconds=30, clock=lambda: clock[0])
    assert budget.remaining_seconds() == 30
    budget.begin_api_call()
    budget.begin_model_call()
    clock[0] += 10
    assert round(budget.remaining_seconds(), 3) == 20
    budget.stop("cancelled")
    with pytest.raises(BudgetExhausted):
        budget.begin_api_call()
    assert budget.stop_events == ["cancelled"]
    with pytest.raises(BudgetExhausted):
        RunBudget.from_request(deadline_utc=NOW, now_utc=NOW, clock=lambda: 0.0)


def test_run_budget_counts_calls_tokens_and_reserve():
    clock = [0.0]
    budget = RunBudget.from_request(deadline_utc=NOW + timedelta(seconds=60), now_utc=NOW,
                                    clock=lambda: clock[0])
    for _ in range(3):
        budget.begin_api_call()
    with pytest.raises(BudgetExhausted):
        budget.begin_api_call()
    for _ in range(4):
        budget.begin_model_call()
    with pytest.raises(BudgetExhausted):
        budget.begin_model_call()
    budget.reserve_tokens(32000)
    with pytest.raises(BudgetExhausted):
        budget.reserve_tokens(1)
    budget.record_output_tokens(2001, limit=2000)
    assert budget.stopped_reason == "output_token_limit"
    assert budget.public_state()["api_calls"] == 3


def _api_config(tmp_path, **overrides):
    payload = {
        "instance_id": "gq",
        "data_dir": str(tmp_path / "data"),
        "api": {"bind_host": "127.0.0.1", "bind_port": 8765,
                "token_ref": "group_query_bot.query_api.token", "app_id": "cli_group"},
        "principal": {"principal_id": "group-query-bot",
                      "capabilities": ["market:read", "candidates:evaluate",
                                       "scenario:close", "scenario:roll-down",
                                       "scenario:roll-up"],
                      "markets": ["us", "hk"], "account_scope": []},
        "quote": {"source": "futu", "host": "127.0.0.1", "port": 11111},
        "max_budget_seconds": 45,
        "public_policy": {"policy": {"sell_put": {"min_dte": 20}},
                          "fee_model_id": "futu_option_fee",
                          "fee_model_version": "futu_option_sell_fee.v1",
                          "strategy_version": "remaining_yield_capture.v3"},
    }
    payload.update(overrides)
    path = tmp_path / "api.json"
    path.write_text(json.dumps(payload))
    return path


def test_api_settings_require_loopback_dedicated_credential_and_no_account_scope(tmp_path):
    settings = load_api_settings(_api_config(tmp_path))
    assert settings.bind_host == "127.0.0.1"
    assert settings.markets == ("HK", "US")
    assert settings.public_policy.canonical_sha256() == load_api_settings(
        _api_config(tmp_path)).public_policy.canonical_sha256()
    assert len(settings.public_digest()) == 64

    def reject(**overrides):
        with pytest.raises(ValueError):
            load_api_settings(_api_config(tmp_path, **overrides))

    reject(api={"bind_host": "0.0.0.0", "bind_port": 8765,
                "token_ref": "group_query_bot.query_api.token", "app_id": "x"})
    reject(api={"bind_host": "127.0.0.1", "bind_port": 8765,
                "token_ref": "some.other.token", "app_id": "x"})
    reject(principal={"principal_id": "group-query-bot",
                      "capabilities": ["market:read"], "markets": ["us"],
                      "account_scope": ["lx"]})
    reject(principal={"principal_id": "group-query-bot",
                      "capabilities": ["market:read"], "markets": [], "account_scope": []})
    reject(public_policy={"policy": {"accounts": {"lx": {}}},
                          "fee_model_id": "f", "fee_model_version": "v",
                          "strategy_version": "s"})
    reject(public_policy={"policy": {"portfolio": {}},
                          "fee_model_id": "f", "fee_model_version": "v",
                          "strategy_version": "s"})
    reject(max_budget_seconds=91)
    reject(quote={"source": "futu", "host": "127.0.0.1", "port": 0})


def test_api_settings_reject_a_top_level_non_object_document(tmp_path):
    path = tmp_path / "api-list.json"
    path.write_text(json.dumps([{"instance_id": "gq"}]))
    with pytest.raises(ValueError):
        load_api_settings(path)


def test_sliding_window_limiter_prunes_idle_keys():
    clock = [100.0]
    limiter = SlidingWindowLimiter(2, clock=lambda: clock[0])
    limiter.acquire("user-a")
    limiter.acquire("user-a")
    with pytest.raises(AdmissionRejected):
        limiter.acquire("user-a")
    assert list(limiter._times) == ["user-a"]
    limiter.check("user-b")
    assert list(limiter._times) == ["user-a"]
    clock[0] += 61
    limiter.check("user-a")
    assert limiter._times == {}
    limiter.acquire("user-b")
    assert list(limiter._times) == ["user-b"]
