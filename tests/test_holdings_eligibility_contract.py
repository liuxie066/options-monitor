"""Offline holdings qualification and portfolio-fetch boundary measurements."""
from concurrent.futures import CancelledError
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest

from src.application import portfolio_context_service as service
from src.application.prefilters import apply_prefilters
from src.application.wheel import capacity
from tests.cash_evidence_helpers import cash_config, cash_portfolio

NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)
OPTION_CONTEXT = {"decision_snapshot_status": "trusted", "locked_shares_status": "available",
                  "locked_shares_by_symbol": {}}


def portfolio(age=0, *, account="lx", account_id="123", cash_age=0):
    observed = NOW - timedelta(seconds=age)
    result = cash_portfolio({"source_observed_at": observed.isoformat(),
        "stocks_by_symbol": {"NVDA": {"shares": 100, "can_sell_qty": 100, "avg_cost": 100}}},
        account=account, account_id=account_id, evaluated_at=NOW)
    result["cash_source_observed_at"] = (NOW - timedelta(seconds=cash_age)).isoformat()
    result["cash_snapshot"] = service.evaluate_account_cash_snapshot(
        result, config=cash_config(account, account_id), account=account, evaluated_at=NOW)
    return result


def load(path, fetch, *, account="lx", account_id="123", **kwargs):
    return service.load_account_portfolio_context(market="富途", account=account,
        state_dir=path, log=lambda _: None, runtime_config=cash_config(account, account_id),
        portfolio_source="futu", fetch_futu_portfolio_context_fn=fetch,
        write_cache=kwargs.pop("write_cache", False), now_utc=NOW, required_position_asset_types=kwargs.pop("required_position_asset_types", ("stock",)),
        **kwargs)


def coverage(context):
    return capacity.build_shared_coverage_facts(account="lx", portfolio_context=context,
        option_context=OPTION_CONTEXT, wheel_read_model={"batches": []})[0]


def call_allowed(context):
    return apply_prefilters(symbol="NVDA", sp={"enabled": False}, cc={"enabled": True},
        want_put=False, want_call=True, portfolio_ctx=context).want_call


@pytest.mark.parametrize(("age", "usable"), [(0, True), (300, True), (301, False), (-1, False)])
def test_shared_consumer_stock_age_boundary(age, usable):
    ctx = portfolio(age)
    assert bool(service.portfolio_position_snapshot_errors(ctx)) is not usable
    assert call_allowed(ctx) is usable
    assert (coverage(ctx)["status"] == "available") is usable
    assert service.cash_snapshot_is_usable(ctx)


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("broker_account_ref", {"account_label": "sy"}, "snapshot_account_mismatch"),
    ("broker_account_ref", {"external_account_id": "999"}, "snapshot_physical_account_mismatch"),
    ("broker_account_ref", {"environment": "SIMULATE"}, "snapshot_environment_mismatch"),
    ("scope", {"markets": ["HK"]}, "snapshot_scope_mismatch"),
    ("scope", {"asset_types": ["option"]}, "snapshot_scope_mismatch"),
    ("scope", {"filtered": True}, "snapshot_scope_incomplete"),
    ("completeness", "partial", "snapshot_scope_incomplete"),
    ("quality", {"status": "unavailable"}, "snapshot_quality_unavailable"),
    ("source_as_of_utc", (NOW - timedelta(seconds=301)).isoformat(), "snapshot_source_as_of_utc_stale_or_future"),
])
def test_scope_failures_block_cached_and_fetched_holdings(tmp_path, field, value, reason):
    ctx = portfolio()
    snapshot = ctx["position_snapshot_input"]
    snapshot[field] = {**snapshot[field], **value} if isinstance(value, dict) else value
    assert reason in service.portfolio_position_snapshot_errors(ctx)
    assert not call_allowed(ctx)
    assert coverage(ctx)["status"] == "unavailable"
    (tmp_path / "portfolio_context.json").write_text(json.dumps(ctx))
    calls = []
    def fetch(**kwargs):
        calls.append(kwargs)
        return deepcopy(ctx)
    result = load(tmp_path, fetch)
    assert len(calls) == 1
    assert reason in service.portfolio_position_snapshot_errors(result)
    assert result["context_source"] == "futu_direct"
    assert service.cash_snapshot_is_usable(result)


@pytest.mark.parametrize("invalid", [None, {}, {"filters": ["lx"]}, {"capacity_authority": {"logical_account": "sy"}}])
def test_missing_or_malformed_authority_is_unavailable(invalid):
    ctx = portfolio()
    if invalid is None or invalid == {}:
        ctx["position_snapshot_input"] = invalid
    else:
        ctx.update(invalid)
        if "filters" in invalid:
            ctx["portfolio_source_account"] = None
            ctx["capacity_authority"]["logical_account"] = None
    assert service.portfolio_position_snapshot_errors(ctx)
    assert not call_allowed(ctx)


@pytest.mark.parametrize(("cash_age", "cash_fresh"), [(0, True), (901, False)])
def test_failed_holdings_refresh_preserves_only_independently_fresh_cash(tmp_path, cash_age, cash_fresh):
    (tmp_path / "portfolio_context.json").write_text(json.dumps(portfolio(301, cash_age=cash_age)))
    calls = []
    def failed(**kwargs):
        calls.append(kwargs)
        raise OSError("offline fixture")
    result = load(tmp_path, failed)
    assert len(calls) == 1
    assert service.cash_snapshot_is_usable(result) is cash_fresh
    assert not call_allowed(result)
    if cash_fresh:
        assert result["cash_by_currency"] == {"CNY": 0}
        assert "position_snapshot_refresh_unavailable" in service.portfolio_position_snapshot_errors(result)
        assert coverage(result)["status"] == "unavailable"
    else:
        assert "cash_by_currency" not in result


@pytest.mark.parametrize("exception", [TimeoutError, CancelledError])
def test_refresh_timeout_and_cancellation_propagate(tmp_path, exception):
    (tmp_path / "portfolio_context.json").write_text(json.dumps(portfolio(301)))
    def failed(**kwargs):
        raise exception()
    with pytest.raises(exception):
        load(tmp_path, failed)


@pytest.mark.parametrize(("age", "expected_fetches"), [(60, 0), (61, 1)])
def test_strict_attribution_window_remains_sixty_seconds(tmp_path, age, expected_fetches):
    (tmp_path / "portfolio_context.json").write_text(json.dumps(portfolio(age)))
    calls = []
    def fetch(**kwargs):
        calls.append(kwargs)
        return portfolio()
    result = load(tmp_path, fetch, include_options=True, required_position_asset_types=("stock", "option"))
    assert len(calls) == expected_fetches
    assert service.portfolio_position_snapshot_errors(result, max_age_seconds=60) == []


def test_frozen_replay_stays_stable_but_new_confirmation_requalifies(monkeypatch):
    ctx = portfolio()
    before = coverage(ctx)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW + timedelta(seconds=301)
    monkeypatch.setattr(capacity, "datetime", Clock)
    assert coverage(ctx) == before
    assert not capacity.broker_capacity_observation_is_fresh(before, account="lx", market="us")
    ctx["position_snapshot_input"]["observed_at_utc"] = (NOW + timedelta(seconds=301)).isoformat()
    ctx["cash_snapshot"]["evaluated_at"] = (NOW + timedelta(seconds=301)).isoformat()
    assert before["position_evidence"]["position_snapshot_input"]["observed_at_utc"] == NOW.isoformat().replace("+00:00", "Z")
    assert coverage(ctx)["status"] == "available"
    assert capacity.broker_capacity_observation_is_fresh(coverage(ctx), account="lx", market="us")
    assert not capacity.broker_capacity_observation_is_fresh(before, account="sy", market="us")


@pytest.mark.parametrize(("scenario", "expected_calls"), [
    ("same_scope_readonly_missing", 2), ("same_scope_write_cache", 1),
    ("fresh_cache", 0), ("different_path", 2), ("different_account", 2),
    ("stale_holdings", 1), ("partial_holdings", 2), ("failed_refresh", 2),
])
def test_portfolio_fetch_boundary_counts(tmp_path, scenario, expected_calls):
    """Counts one injected portfolio request, not SDK RPCs or production latency."""
    calls = []
    if scenario in {"fresh_cache", "stale_holdings", "failed_refresh"}:
        (tmp_path / "portfolio_context.json").write_text(json.dumps(portfolio(301 if scenario != "fresh_cache" else 0)))
    def fetch(**kwargs):
        calls.append((kwargs["account"], kwargs["include_options"] if "include_options" in kwargs else False))
        if scenario == "failed_refresh":
            raise OSError("offline fixture")
        result = portfolio(account=kwargs["account"])
        if scenario == "partial_holdings":
            result["position_snapshot_input"]["completeness"] = "partial"
        return result
    outcomes = []
    for index in range(2):
        account = "sy" if scenario == "different_account" and index else "lx"
        path = tmp_path / str(index) if scenario == "different_path" else tmp_path
        result = load(path, fetch, account=account,
                      write_cache=scenario in {"same_scope_write_cache", "different_account", "stale_holdings"})
        outcomes.append(result)
    assert len(calls) == expected_calls
    if scenario in {"partial_holdings", "failed_refresh"}:
        assert all(service.portfolio_position_snapshot_errors(result) for result in outcomes)
    else:
        assert all(service.portfolio_position_snapshot_errors(result, account=result["filters"]["account"]) == [] for result in outcomes)
    if scenario == "same_scope_readonly_missing":
        assert not (tmp_path / "portfolio_context.json").exists()


@pytest.mark.parametrize(("key", "value"), [("futu_account_id", "999"), ("trd_env", "SIMULATE"), ("market", "hk")])
def test_direct_source_self_consistency_cannot_replace_config_binding(tmp_path, key, value):
    ctx = portfolio()
    ctx["capacity_authority"][key] = value
    ref = ctx["position_snapshot_input"]["broker_account_ref"]
    if key == "futu_account_id":
        ref["external_account_id"] = value
    elif key == "trd_env":
        ref["environment"] = value
    result = load(tmp_path, lambda **_: ctx)
    assert "position_snapshot_authority_mismatch" in service.portfolio_position_snapshot_errors(result)
    assert not call_allowed(result)
    assert coverage(result)["status"] == "unavailable"


def test_stricter_asset_scope_is_necessary_separate_read(tmp_path):
    ctx = portfolio()
    ctx["position_snapshot_input"]["scope"]["asset_types"] = ["stock"]
    (tmp_path / "portfolio_context.json").write_text(json.dumps(ctx))
    calls = []
    def fetch(**kwargs):
        calls.append(kwargs)
        return portfolio()
    load(tmp_path, fetch)
    assert calls == []
    load(tmp_path, fetch, include_options=True, required_position_asset_types=("stock", "option"))
    assert len(calls) == 1
    assert calls[0]["include_options"] is True
