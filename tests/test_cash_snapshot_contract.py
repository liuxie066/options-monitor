from __future__ import annotations

from concurrent.futures import CancelledError
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os

import pytest

from domain.domain.risk_capacity import evaluate_cash_snapshot
from src.application.config_defaults import cash_snapshot_ttl_sec
from src.application.futu_portfolio_context import build_futu_portfolio_context
from src.application.portfolio_context_service import cash_snapshot_is_usable, load_account_portfolio_context

NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)
AUTHORITY = {"logical_account": "lx", "futu_account_id": "123", "trd_env": "REAL", "market": "us"}
CONFIG = {
    "_resolved": {"market": "us"},
    "account_settings": {"lx": {"futu": {"account_id": "123", "trd_env": "REAL", "host": "offline", "port": 11111}}},
    "portfolio": {"source": "futu", "base_currency": "CNY"},
}


def cash_context(observed=NOW, **changes):
    result = build_futu_portfolio_context(
        balance_rows=[{"us_cash": 0, "cn_cash": -5}], position_rows=[], account="lx",
        source_observed_at=NOW.isoformat(), cash_source_observed_at=observed.isoformat(),
        broker_account_identifiers={"123"}, futu_account_id="123", trd_env="REAL", capacity_market="us",
    )
    result.update(changes)
    return result


def verdict(context, ttl=900):
    return evaluate_cash_snapshot(context, expected_authority=AUTHORITY, evaluated_at=NOW, max_age_sec=ttl)


@pytest.mark.parametrize(("age", "status", "reason"), [
    (0, "fresh", None), (900, "fresh", None), (901, "stale", "CASH_OBSERVATION_STALE"),
    (-1, "unknown", "CASH_OBSERVATION_IN_FUTURE"),
])
def test_cash_age_uses_independent_source_time(age, status, reason):
    ctx = cash_context(NOW - timedelta(seconds=age))
    result = verdict(ctx)
    assert result["status"] == status
    assert result["reason_codes"] == ([reason] if reason else [])
    assert ctx["cash_by_currency"] == {"USD": 0, "CNY": -5}


@pytest.mark.parametrize("ttl", [0, -1, True, 1.5, "900", None])
def test_invalid_ttl_never_becomes_force_refresh(ttl):
    with pytest.raises(ValueError, match="positive integer"):
        cash_snapshot_ttl_sec({"runtime": {"portfolio_context_ttl_sec": ttl}})
    assert "CASH_TTL_INVALID" in verdict(cash_context(), ttl)["reason_codes"]
    assert cash_snapshot_ttl_sec({}) == 900


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("cash_source_observed_at", None, "CASH_OBSERVATION_MISSING"),
    ("cash_source_observed_at", "2026-10-03T00:00:00", "CASH_OBSERVATION_MISSING"),
    ("cash_balance_reliable", None, "CASH_BALANCE_UNRELIABLE"),
    ("cash_by_currency", {"USD": float("inf")}, "CASH_AMOUNT_INVALID"),
    ("cash_by_currency", {"USD": float("nan")}, "CASH_AMOUNT_INVALID"),
    ("cash_by_currency", {"USD": True}, "CASH_AMOUNT_INVALID"),
    ("cash_by_currency", {}, "CASH_AMOUNT_INVALID"),
    ("filters", {"account": "sy"}, "CASH_IDENTITY_MISMATCH"),
    ("source_account_identifiers", ["999"], "CASH_IDENTITY_MISMATCH"),
    ("portfolio_source_name", "holdings", "CASH_SOURCE_INVALID"),
])
def test_invalid_cash_evidence_wins_over_staleness(field, value, reason):
    result = verdict(cash_context(NOW-timedelta(seconds=901), **{field: value}))
    assert result["status"] == "unknown"
    assert reason in result["reason_codes"]


@pytest.mark.parametrize("field", ["logical_account", "futu_account_id", "trd_env", "market"])
def test_each_authority_dimension_is_bound(field):
    ctx = cash_context()
    ctx["capacity_authority"][field] = "wrong"
    assert "CASH_IDENTITY_MISMATCH" in verdict(ctx)["reason_codes"]


@pytest.mark.parametrize("changes", [{}, {"cash_snapshot": {"status": "fresh"}}, {"cash_balance_reliable": True}])
def test_consumer_never_guesses_a_missing_verdict(changes):
    assert not cash_snapshot_is_usable(changes)


def load(tmp_path, fetch, **overrides):
    params = dict(market="富途", account="lx", state_dir=tmp_path,
                  log=lambda message: None, runtime_config=deepcopy(CONFIG), portfolio_source="futu",
                  fetch_futu_portfolio_context_fn=fetch,
                  load_json_fn=lambda path: json.loads(path.read_text()), write_cache=False, now_utc=NOW)
    params.update(overrides)
    return load_account_portfolio_context(**params)


def test_cache_reuse_binds_current_fx_without_mutating_source(tmp_path):
    ctx = cash_context(exchange_rates={"old": True})
    path = tmp_path / "portfolio_context.json"
    path.write_text(json.dumps(ctx)); before = path.read_bytes()
    os.utime(path, (1, 1))
    fx = {"rates": {"USDCNY": 7}}
    for provided in (fx, {}, None):
        result = load(tmp_path, lambda **_: pytest.fail("fresh source reused"), exchange_rate_observation=provided)
        assert result["context_source"] == "account_cache"
        assert result["exchange_rates"] is None if provided is None else result["exchange_rates"]["rates"] == {}
        assert result["cash_by_currency"] == ctx["cash_by_currency"]
        assert cash_snapshot_is_usable(result)
        assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["portfolio_context.json"]


@pytest.mark.parametrize("old", [cash_context(NOW-timedelta(seconds=901)), cash_context(cash_source_observed_at=None)])
def test_stale_or_legacy_cache_refreshes_once_even_when_mtime_new(tmp_path, old, monkeypatch):
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    (tmp_path / "portfolio_context.json").write_text(json.dumps(old))
    calls = []
    def fetch(**kwargs):
        calls.append(kwargs)
        return cash_context()
    result = load(tmp_path, fetch)
    assert len(calls) == 1
    assert calls[0]["write_cache"] is False
    assert calls[0]["exchange_rate_cache_path"] == tmp_path / "output_shared" / "state" / "rate_cache.json"
    assert cash_snapshot_is_usable(result)
    assert result["context_source"] == "futu_direct"


def test_refresh_failure_never_returns_stale_money(tmp_path):
    (tmp_path / "portfolio_context.json").write_text(json.dumps(cash_context(NOW-timedelta(days=1))))
    calls = []
    def fetch(**kwargs):
        calls.append(kwargs)
        raise ValueError("offline provider unavailable")
    result = load(tmp_path, fetch)
    assert len(calls) == 1
    assert "cash_by_currency" not in result
    assert "CASH_PROVIDER_UNAVAILABLE" in result["cash_snapshot"]["reason_codes"]


@pytest.mark.parametrize("error", [TimeoutError, CancelledError])
def test_cancel_and_timeout_propagate_without_retry(tmp_path, error):
    calls = []
    def fetch(**kwargs):
        calls.append(kwargs)
        raise error()
    with pytest.raises(error):
        load(tmp_path, fetch)
    assert len(calls) == 1
    assert list(tmp_path.iterdir()) == []


def test_invalid_config_performs_no_provider_io(tmp_path):
    config = {**CONFIG, "runtime": {"portfolio_context_ttl_sec": 0}}
    out = load(tmp_path, lambda **_: pytest.fail("invalid TTL must not fetch"), runtime_config=config)
    assert "CASH_TTL_INVALID" in out["cash_snapshot"]["reason_codes"]


@pytest.mark.parametrize(("rows", "reliable", "amounts"), [
    ([{"us_cash": 0, "hk_cash": "N/A"}], True, {"USD": 0}),
    ([{"us_cash": 1}, {"cn_cash": "N/A"}], False, {"USD": 1}),
    ([{"us_cash": 1}, "bad"], False, {"USD": 1}),
    ([{"us_cash": 1, "hk_cash": "bad"}], False, {"USD": 1}),
    ([{"acc_id": "123", "us_cash": 1}, {"acc_id": "123", "us_cash": 2}], False, {"USD": 1}),
    ([{"acc_id": "123", "us_cash": 1}, {"acc_id": "123", "us_cash": "1"}], True, {"USD": 1}),
    ([{"fund_assets": 1, "mmf_assets": 2}], False, {"CNY": 1}),
    ([{"fund_assets": 1, "mmf_assets": 1}], True, {"CNY": 1}),
])
def test_builder_validates_rows_and_preserves_explicit_zero(rows, reliable, amounts):
    ctx = build_futu_portfolio_context(balance_rows=rows, position_rows=[], account="lx")
    assert ctx["cash_balance_reliable"] is reliable
    assert ctx["cash_by_currency"] == amounts
    assert ctx["cash_source_observed_at"] is None  # builder cannot invent a timestamp


def test_cash_time_precedes_slow_fx_and_failed_positions(tmp_path, monkeypatch):
    import src.application.futu_portfolio_context as fc
    clock = [NOW]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]
    class Gateway:
        closed = False
        def get_account_balance(self, **kwargs):
            return [{"us_cash": 123}]
        def get_positions(self, **kwargs):
            raise ValueError("positions unavailable")
        def close(self):
            self.closed = True
    gateway = Gateway()
    monkeypatch.setattr(fc, "datetime", Clock)
    monkeypatch.setattr(fc, "build_ready_futu_broker_gateway", lambda **_: gateway)
    def slow_fx(**kwargs):
        assert kwargs == {"cache_path": tmp_path / "rates.json", "write_cache": False}
        clock[0] += timedelta(seconds=901)
        raise ValueError("FX unavailable")
    monkeypatch.setattr(fc, "_fetch_market_exchange_rate_observation", slow_fx)
    context = fc.fetch_futu_portfolio_context(cfg=CONFIG, account="lx", write_cache=False,
                                              exchange_rate_cache_path=tmp_path / "rates.json")
    assert gateway.closed
    assert context["cash_source_observed_at"] == NOW.isoformat()
    assert context["cash_balance_reliable"] is True
    assert context["position_snapshot_input"]["completeness"] == "partial"
    assert context["position_snapshot_input"]["quality"]["status"] == "unavailable"
    assert "position_query_failed" in context["position_snapshot_input"]["errors"]
    assert evaluate_cash_snapshot(context, expected_authority=AUTHORITY, evaluated_at=clock[0], max_age_sec=900)["status"] == "stale"
    assert list(tmp_path.iterdir()) == []


def test_source_funds_response_does_not_drop_malformed_rows(monkeypatch):
    import src.application.futu_portfolio_context as fc
    class Gateway:
        def get_account_balance(self, **kwargs):
            return [{"us_cash": 1}, "bad"]
        def get_positions(self, **kwargs):
            pytest.fail("must fail before positions")
        def close(self):
            pass
    monkeypatch.setattr(fc, "build_ready_futu_broker_gateway", lambda **_: Gateway())
    with pytest.raises(ValueError, match="get_account_balance failed"):
        fc.fetch_futu_portfolio_context(cfg=CONFIG, account="lx", exchange_rate_observation=None)


def test_partial_positions_do_not_create_cash_only_nav():
    from src.application.short_vol_risk_context import build_portfolio_risk_context
    from src.infrastructure.exchange_rates import CurrencyConverter
    ctx = cash_context(cash_by_currency={"CNY": 100}, position_snapshot_input={
        "completeness": "partial", "quality": {"status": "unavailable"}, "errors": ["position_query_failed"],
    })
    out = build_portfolio_risk_context(portfolio_ctx=ctx, exchange_rate_converter=CurrencyConverter(rates={}))
    assert out.nav_cny is None
    assert "broker_positions_unavailable" in out.unavailable_reasons


def test_read_only_real_fx_owner_creates_neither_cache_nor_lock(tmp_path, monkeypatch):
    import src.application.futu_portfolio_context as fc
    from src.infrastructure import exchange_rates as fx
    quoted = datetime(2026, 9, 30, 3, tzinfo=timezone.utc).isoformat()
    monkeypatch.setattr(fx, "_utc_now", lambda: datetime(2026, 9, 30, 3, tzinfo=timezone.utc))
    monkeypatch.setattr(fx, "fetch_market_exchange_rates", lambda: {"schema_version": 2, "pairs": {
        "USDCNY": {"rate": 7.2, "source": "tencent_quote", "quote_at_utc": quoted, "observed_at_utc": quoted},
        "HKDCNY": {"rate": 0.92, "source": "tencent_quote", "quote_at_utc": quoted, "observed_at_utc": quoted},
    }})
    class Gateway:
        def get_account_balance(self, **kwargs):
            return [{"us_cash": 5}]
        def get_positions(self, **kwargs):
            return []
        def close(self):
            pass
    monkeypatch.setattr(fc, "build_ready_futu_broker_gateway", lambda **_: Gateway())
    result = load(tmp_path, fc.fetch_futu_portfolio_context, now_utc=None)
    assert cash_snapshot_is_usable(result)
    assert result["exchange_rates"]["rates"]["USDCNY"] == 7.2
    assert list(tmp_path.rglob("*")) == []


def test_option_terms_failure_keeps_cash_and_marks_positions_partial(monkeypatch):
    from types import SimpleNamespace
    import src.application.futu_portfolio_context as fc
    class Gateway:
        def get_account_balance(self, **kwargs):
            return [{"us_cash": 5}]
        def get_positions(self, **kwargs):
            return [{"code": "US.NVDA261120P100000", "qty": -1, "currency": "USD"}]
        def close(self):
            pass
    monkeypatch.setattr(fc, "build_ready_futu_broker_gateway", lambda **_: Gateway())
    monkeypatch.setattr(fc, "resolve_futu_quote_route", lambda *_a, **_k: SimpleNamespace(ok=False))
    result = fc.fetch_futu_portfolio_context(cfg=CONFIG, account="lx", include_options=True, exchange_rate_observation=None)
    assert result["cash_by_currency"] == {"USD": 5}
    assert result["cash_balance_reliable"] is True
    assert result["position_snapshot_input"]["completeness"] == "partial"
    assert "position_option_terms_failed" in result["position_snapshot_input"]["errors"]


def test_required_positions_refresh_once_without_invalidating_cash(tmp_path):
    (tmp_path / "portfolio_context.json").write_text(json.dumps(cash_context()))
    calls = []
    def fetch(**kwargs):
        calls.append(kwargs)
        return cash_context(position_snapshot_input={"completeness": "partial"})
    result = load(tmp_path, fetch, include_options=True)
    assert len(calls) == 1
    assert calls[0]["include_options"] is True
    assert cash_snapshot_is_usable(result)
    assert result["position_snapshot_input"]["completeness"] == "partial"


def test_cache_write_failure_is_not_reported_as_success(tmp_path, monkeypatch):
    import src.application.portfolio_context_service as service
    def reject(*_args, **_kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(service, "atomic_write_json", reject)
    with pytest.raises(OSError, match="disk full"):
        load(tmp_path, lambda **_: cash_context(), write_cache=True)


def test_scoped_sdk_rows_without_repeated_account_id_deduplicate():
    out = build_futu_portfolio_context(
        balance_rows=[{"us_cash": 5}, {"us_cash": 5}], position_rows=[], account="lx",
        futu_account_id="123", broker_account_identifiers={"123"}, trd_env="REAL", capacity_market="us",
    )
    assert out["cash_by_currency"] == {"USD": 5}
    assert out["cash_balance_reliable"] is True


@pytest.mark.parametrize("ttl", [0, -1, True, "900", 1.5, None])
def test_real_config_validator_rejects_invalid_cash_ttl(ttl):
    from src.application.config_validator import validate_config
    config = {**deepcopy(CONFIG), "accounts": ["lx"],
              "runtime": {"portfolio_context_ttl_sec": ttl},
              "symbols": [{"symbol": "NVDA", "market": "US", "fetch": {"source": "futu"},
                           "sell_put": {"enabled": False}, "sell_call": {"enabled": False}}]}
    with pytest.raises(SystemExit, match="runtime.portfolio_context_ttl_sec must be a positive integer"):
        validate_config(config)


def test_malformed_cached_positions_trigger_one_refresh(tmp_path):
    (tmp_path / "portfolio_context.json").write_text(json.dumps(cash_context(position_snapshot_input="bad")))
    calls = []
    def fetch(**kwargs):
        calls.append(kwargs)
        return cash_context()
    assert cash_snapshot_is_usable(load(tmp_path, fetch, include_options=True))
    assert len(calls) == 1


def test_pipeline_explicit_missing_fx_clears_cache(tmp_path, monkeypatch):
    import src.application.pipeline_context as pipeline
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(pipeline, "load_cached_json", lambda _: cash_context(now, exchange_rates={"old": True}))
    monkeypatch.setattr(pipeline, "fetch_futu_portfolio_context", lambda **_: pytest.fail("cache hit should not fetch"))
    monkeypatch.setattr(pipeline, "_persist_source_snapshot", lambda *_: None)
    result = pipeline.load_portfolio_context(
        data_config="unused", market="富途", account="lx", base=tmp_path,
        state_dir=tmp_path, shared_state_dir=None, log=lambda _: None,
        runtime_config=CONFIG, portfolio_source="futu", exchange_rate_observation=None,
    )
    assert result["context_source"] == "account_cache"
    assert result["exchange_rates"] is None
    assert cash_snapshot_is_usable(result)


@pytest.mark.parametrize("raw", [float("nan"), float("inf"), float("-inf")])
def test_invalid_provider_number_remains_sealable_as_diagnostic(raw):
    from src.application.payload_helpers import readable_json_bytes
    context = build_futu_portfolio_context(balance_rows=[{"us_cash": raw}], position_rows=[], account="lx")
    assert context["cash_balance_reliable"] is False
    assert context["cash_source_rows"][0]["us_cash"] == repr(raw)
    assert json.loads(readable_json_bytes(context))["cash_by_currency"] == {}


@pytest.mark.parametrize("change", [{}, {"cash_source_observation_status": "stale"}, {"cash_balance_reliable": False}, {"cash_source_observed_at": None}])
def test_consumers_share_sealed_cash_verdict(change):
    from cash_evidence_helpers import cash_portfolio
    from src.application.sell_put_cash import sell_put_opening_capacity_inputs
    from src.application.short_vol_risk_context import build_portfolio_risk_context
    from src.application.daily_decision_brief_service import _build_funds
    from src.application.portfolio_assignment_scenario import _futu_context_error
    from src.application.wheel.capacity import build_shared_cash_capacity_fact
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    context = cash_portfolio({"cash_by_currency": {"CNY": 10000}, "source_observed_at": NOW.isoformat(), **change}, evaluated_at=NOW)
    option = {"as_of_utc": NOW.isoformat(), "decision_snapshot_status": "trusted",
              "cash_secured_by_symbol_by_ccy": {}, "cash_secured_total_by_ccy": {}, "cash_secured_unavailable_by_symbol": {}}
    context["option_ctx"] = option
    usable = cash_snapshot_is_usable(context)
    converter = CurrencyConverter(ExchangeRates())
    capacity = sell_put_opening_capacity_inputs(symbol="NVDA", strike=10, multiplier=100, currency="CNY",
        portfolio_ctx=context, exchange_rate_converter=converter)
    assert capacity["put_cash_capacity_available"] is usable
    risk = build_portfolio_risk_context(portfolio_ctx=context, exchange_rate_converter=converter)
    assert (risk.nav_cny == 10000) is usable
    funds, reliable = _build_funds(portfolio_context=context, option_positions_context=option, data_gaps=[])
    assert reliable is usable and funds["cash_snapshot"] == context["cash_snapshot"]
    fact = build_shared_cash_capacity_fact(account="lx", portfolio_context=context, option_context=option,
        wheel_read_model={}, fx_snapshot={})
    assert (fact["status"] == "available") is usable
    assert fact["cash_snapshot"] == context["cash_snapshot"]
    assert (_futu_context_error("lx", context) is None) is usable


def test_cash_fact_hash_ignores_evaluation_clock_but_binds_evidence_and_ttl():
    from cash_evidence_helpers import cash_portfolio
    from src.application.wheel.capacity import build_shared_cash_capacity_fact
    from src.application.portfolio_context_service import evaluate_account_cash_snapshot
    context = cash_portfolio({"cash_by_currency": {"USD": 100}, "source_observed_at": NOW.isoformat()}, evaluated_at=NOW)
    def fact(ctx):
        return build_shared_cash_capacity_fact(account="lx", portfolio_context=ctx,
            option_context={"decision_snapshot_status": "trusted", "cash_secured_total_by_ccy": {}}, wheel_read_model={}, fx_snapshot={})
    original = fact(context)
    context["cash_snapshot"] = evaluate_account_cash_snapshot(context, config=CONFIG, account="lx", evaluated_at=NOW+timedelta(seconds=901))
    expired = fact(context)
    assert expired["status"] == "unavailable"
    assert expired["capacity_identity_hash"] == original["capacity_identity_hash"]
    context["cash_snapshot"]["max_age_sec"] = 1000
    assert fact(context)["capacity_identity_hash"] != original["capacity_identity_hash"]
    context["cash_snapshot"]["max_age_sec"] = 900
    context["cash_balance_reliable"] = False
    assert fact(context)["capacity_identity_hash"] != original["capacity_identity_hash"]


def test_partial_positions_do_not_erase_cash_or_allow_nav():
    from cash_evidence_helpers import cash_portfolio
    from src.application.short_vol_risk_context import build_portfolio_risk_context
    from src.application.portfolio_assignment_scenario import _futu_context_error
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates
    context = cash_portfolio({"cash_by_currency": {"CNY": 10000}, "position_snapshot_input": {
        "completeness": "partial", "quality": {"status": "unavailable"}, "errors": ["provider failed"]}})
    assert cash_snapshot_is_usable(context)
    assert build_portfolio_risk_context(portfolio_ctx=context, exchange_rate_converter=CurrencyConverter(ExchangeRates())).nav_cny is None
    assert "stock snapshot" in _futu_context_error("lx", context)


def test_brief_replays_cash_without_using_todays_clock_or_legacy_flags(monkeypatch):
    from cash_evidence_helpers import cash_portfolio
    from src.application.daily_decision_brief_service import _build_funds
    context = cash_portfolio({"cash_by_currency": {"CNY": 0}, "source_observed_at": "2020-01-01T00:00:00+00:00"})
    option = {"as_of_utc": "2020-01-01T00:00:00+00:00", "cash_secured_total_by_ccy": {}, "decision_snapshot_status": "trusted"}
    first = _build_funds(portfolio_context=context, option_positions_context=option, data_gaps=[])
    assert first[1] and first[0]["cash_total_cny"] == 0
    assert _build_funds(portfolio_context=deepcopy(context), option_positions_context=option, data_gaps=[]) == first
    del context["cash_snapshot"]
    legacy = _build_funds(portfolio_context=context, option_positions_context=option, data_gaps=[])
    assert legacy[1] is False and legacy[0]["cash_total_cny"] is None


@pytest.mark.parametrize("entry", ["scan_adapter", "cash_cli", "cash_tool", "portfolio_tool"])
@pytest.mark.parametrize("case", ["fresh", "stale", "unknown"])
def test_public_cash_entries_share_reader_policy(tmp_path, monkeypatch, entry, case):
    import argparse
    import src.application.pipeline_context as pipeline
    import src.application.cash_headroom_query as query
    import src.application.portfolio_context_service as service
    import src.application.agent_tools.materialization as materialization
    from src.application.tool_execution import execute_tool
    from src.interfaces.cli.scheduler_ops import add_scheduler_commands, handle_scheduler_command

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(service, "datetime", Clock)
    cfg = deepcopy(CONFIG)
    cfg["runtime"] = {"portfolio_context_ttl_sec": 60}
    config_path = tmp_path / "config.us.json"
    config_path.write_text(json.dumps(cfg))
    context = cash_context(NOW - timedelta(seconds=61 if case == "stale" else 0),
                           cash_by_currency={"CNY": 100})
    if case == "unknown":
        context["cash_balance_reliable"] = False
    expected = verdict(context, ttl=60)
    calls = []
    def fetch(**kw):
        calls.append(kw)
        assert kw["cfg"]["runtime"]["portfolio_context_ttl_sec"] == 60
        return deepcopy(context)
    monkeypatch.setattr(query, "fetch_futu_portfolio_context", fetch)
    monkeypatch.setattr(pipeline, "fetch_futu_portfolio_context", fetch)
    monkeypatch.setattr(pipeline, "_persist_source_snapshot", lambda *_: None)
    monkeypatch.setattr(query, "_load_option_position_records", lambda *_: (object(), []))
    monkeypatch.setattr(query, "decision_state_snapshot", lambda *a, **kw: {})
    monkeypatch.setattr(query, "build_option_positions_context", lambda *a, **kw: {
        "decision_snapshot_status": "trusted", "cash_secured_total_by_ccy": {"CNY": 30}, "cash_secured_total_cny": 30,
        "cash_secured_by_symbol_by_ccy": {}, "cash_secured_unavailable_by_symbol": {}})
    monkeypatch.setattr(materialization, "load_runtime_config", lambda **kw: (config_path, deepcopy(cfg)))
    monkeypatch.setattr(materialization, "repo_base", lambda: tmp_path)
    monkeypatch.setattr(materialization, "resolve_output_root", lambda _: tmp_path)
    monkeypatch.setattr(materialization, "resolve_public_data_config_path", lambda *a: tmp_path / "fixture.json")
    state = tmp_path / ({"cash_tool": "query_cash_headroom", "portfolio_tool": "portfolio_context_state"}.get(entry, "state"))
    state.mkdir()
    cache = state / "portfolio_context.json"
    cache.write_text(json.dumps(context))
    before = cache.read_bytes()
    if entry == "scan_adapter":
        result = pipeline.load_portfolio_context(base=tmp_path, data_config="fixture.json", market="富途",
            account="lx", state_dir=state, shared_state_dir=None, log=lambda _: None,
            runtime_config=cfg, exchange_rate_observation=None)
    elif entry == "cash_cli":
        parser = argparse.ArgumentParser()
        add_scheduler_commands(parser.add_subparsers(dest="command"))
        args = parser.parse_args(["sell-put-cash", "--config", str(config_path), "--account", "lx",
            "--data-config", str(tmp_path / "fixture.json"), "--out-dir", str(state), "--no-exchange-rates", "--format", "json"])
        captured = []
        def capture(**kw):
            captured.append(query.query_sell_put_cash(**kw))
        assert handle_scheduler_command(args, repo_base_fn=lambda: tmp_path, query_sell_put_cash_fn=capture) == 0
        result = captured[0]
    else:
        tool = "query_cash_headroom" if entry == "cash_tool" else "get_portfolio_context"
        out = execute_tool(tool, {"config_path": str(config_path), "account": "lx", "no_exchange_rates": True})
        assert out["ok"], out
        result = out["data"]
    assert result["cash_snapshot"] == expected
    assert len(calls) == (0 if case == "fresh" else 1)
    if entry in {"cash_cli", "cash_tool"}:
        assert result["cash_available_cny"] == (100 if case == "fresh" else None)
        assert result["cash_free_cny"] == (70 if case == "fresh" else None)
    if entry == "cash_tool":
        assert cache.read_bytes() == before
        assert calls == [] or calls[0]["write_cache"] is False
        contract = materialization.QUERY_CASH_HEADROOM_TOOL.output_contract
        assert "cash_snapshot" in contract["model_value_fields"]


def test_portfolio_tool_rejects_retired_per_call_cash_ttl():
    from src.application.tool_execution import execute_tool, build_tool_manifest
    spec = next(tool for tool in build_tool_manifest()["tools"] if tool["name"] == "get_portfolio_context")
    assert "ttl_sec" not in spec["input_schema"]
    result = execute_tool("get_portfolio_context", {"ttl_sec": 0})
    assert result["ok"] is False
    assert result["error"]["code"] == "INPUT_ERROR"
    assert "runtime.portfolio_context_ttl_sec" in result["error"]["message"]


@pytest.mark.parametrize("authority", ["broken", ["broken"], 7])
@pytest.mark.parametrize("positions", [{}, {"required_position_asset_types": ("stock",)}, {"include_options": True}])
def test_malformed_cached_authority_refreshes_once(tmp_path, authority, positions):
    (tmp_path / "portfolio_context.json").write_text(json.dumps(cash_context(capacity_authority=authority)))
    calls = []
    def fetch(**kwargs):
        calls.append(kwargs)
        return cash_context()
    result = load(tmp_path, fetch, **positions)
    assert cash_snapshot_is_usable(result)
    assert len(calls) == 1
