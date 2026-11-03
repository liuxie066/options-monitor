"""Public valuation and Close Advice consumers select the market capability offline."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.application import close_advice_required_data as close_data
from src.application import portfolio_assignment_scenario as scenario
from src.application.positions import assigned_stock_quotes as assigned
from test_market_data_provider import RecordingProvider, install, no_live_io  # noqa: F401
from test_option_candidates_contract import NOW


class ConsumerProvider(RecordingProvider):
    def __init__(self, *, complete=True, stale=True, price=120):
        super().__init__(None)
        self.complete, self.stale, self.price = complete, stale, price

    def resolve_quote_route(self, config, **kwargs):
        self.calls.append(("route", kwargs))
        return SimpleNamespace(ok=True, status="ok", host="offline.invalid", port=12345)

    def get_underlier_observations(self, gateway, codes, **kwargs):
        assert gateway is self.gateway
        self.calls.append(("underliers", kwargs))
        age = 86400 if self.stale else 10
        return {code: SimpleNamespace(
            code=code, market="US", sec_status="NORMAL", suspension=False,
            status="market_closed" if self.stale else "ready", reason_code=None,
            last_price=self.price, age_seconds=age,
            observed_at_utc=(NOW - timedelta(seconds=age)).isoformat(),
        ) for code in codes}

    def get_trading_days_with_receipt(self, gateway, **kwargs):
        assert gateway is self.gateway
        self.calls.append(("calendar", kwargs))
        return {"retcode": 0, "coverage_complete": self.complete,
                "pagination_complete": True, "page_count": 1,
                "rows": [{"time": kwargs["start"], "trade_date_type": "WHOLE"}]}

    def get_market_state(self, gateway, codes):
        assert gateway is self.gateway
        self.calls.append(("state", list(codes)))
        return [{"code": code, "market_state": "MORNING"} for code in codes]

    def get_stock_spot(self, gateway, code, **kwargs):
        assert gateway is self.gateway
        self.calls.append(("spot", (code, kwargs)))
        return self.price


@pytest.fixture(autouse=True)
def legacy_market_reads_forbidden(no_live_io, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("alternate provider fell through to a legacy OpenD read")
    monkeypatch.setattr("src.application.opend_market_snapshot_fetching.get_spot_opend", forbidden)
    monkeypatch.setattr("src.application.opend_market_snapshot_fetching.get_underlier_observations_opend", forbidden)
    monkeypatch.setattr("src.application.futu_quote_routing.resolve_futu_quote_route", forbidden)


@pytest.mark.parametrize("entry", ["quote_reader", "prepared_risk"])
@pytest.mark.parametrize("complete,stale,price", [(True, True, 120), (False, True, 120),
                                               (True, False, 120), (True, False, None)])
def test_scenario_actual_price_reads_use_provider_and_keep_freshness(
    monkeypatch, tmp_path, entry, complete, stale, price,
):
    from cash_evidence_helpers import cash_config, cash_portfolio

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(scenario, "datetime", Clock)
    monkeypatch.setattr(scenario, "repo_base", lambda: tmp_path)
    provider = install(monkeypatch, ConsumerProvider(complete=complete, stale=stale, price=price))
    config = {**cash_config(), "market_data": {"provider": "fixture"}, "symbols": {"NVDA": {}}}
    fx = {"rates": {"USDCNY": 7.2}, "source": "fixture_fx", "timestamp": NOW.isoformat()}
    context = cash_portfolio({"source_observed_at": NOW.isoformat(), "cash_by_currency": {"CNY": 100}},
                             evaluated_at=NOW)
    if entry == "quote_reader":
        quotes, warnings = scenario._read_futu_quotes(["NVDA"], runtime_config=config,
            contexts={"lx": context}, fx_observation=fx)
    else:
        monkeypatch.setattr(scenario, "project_exchange_rate_snapshot", lambda value, **kw: value)
        evidence = scenario.prepare_position_sizing_evidence(context=context, runtime_config=config,
            account="lx", fx_observation=fx)["position_sizing_evidence"]
        quotes, warnings = evidence["quotes"], evidence["warnings"]
    assert provider.closed == [True]
    assert [name for name, _ in provider.calls][:3] == ["route", "open", "underliers"]
    observation_args = provider.calls[2][1]
    assert observation_args["market"] == "US" and observation_args["base_dir"] == tmp_path
    assert observation_args["snapshot_batch_size"] > 0 and observation_args["snapshot_limit"].max_calls > 0
    if price is None:
        assert quotes == [] and any("quote unavailable" in warning for warning in warnings)
        return
    quote = quotes[0]
    assert quote["source"] == "fixture_market_snapshot"
    assert quote["price_native"] == "120" and quote["price_cny"] == "864.0"
    assert quote["observed_at"] == (NOW - timedelta(seconds=86400 if stale else 10)).isoformat()
    assert quote["is_stale"] is (stale and not complete)
    if stale and complete:
        assert quote["valuation_quality"] == "closed_market_carried"
        assert quote["trading_calendar"]["coverage_complete"] is True
    if stale and not complete:
        assert any("calendar unavailable" in warning for warning in warnings)
        assert any("quote is dated" in warning for warning in warnings)


@pytest.mark.parametrize("price", [123.5, None])
def test_assigned_stock_public_refresh_uses_provider_and_keeps_limits(monkeypatch, tmp_path, price):
    provider = install(monkeypatch, ConsumerProvider(price=price))
    result = assigned.refresh_assigned_stock_quote_snapshots(
        [{"symbol": "NVDA", "shares_remaining": 100}],
        cfg={"market_data": {"provider": "fixture"}}, base_dir=tmp_path,
        state_base_dir=tmp_path / "state", now_ms=lambda: 1780000000000,
    )
    assert [name for name, _ in provider.calls] == ["route", "open", "spot"]
    code, args = provider.calls[-1][1]
    assert code == "US.NVDA" and args["base_dir"] == tmp_path / "state"
    assert args["snapshot_max_calls"] > 0 and args["snapshot_max_wait_sec"] >= 0
    assert provider.closed == [True]
    assert result.diagnostics["quote_source"] == "fixture_realtime"
    if price is None:
        assert result.quote_snapshots == []
        assert result.diagnostics["status"] == "missing_quote" and result.warnings
    else:
        assert result.quote_snapshots == [{"symbol": "NVDA", "spot": price,
            "quote_time_ms": 1780000000000, "quote_source": "fixture_realtime", "quote_status": "fresh"}]


@pytest.mark.parametrize("complete", [True, False])
def test_close_advice_bounded_entry_keeps_provider_in_sealed_plan(monkeypatch, tmp_path, complete):
    from test_close_advice_required_data import _config, _plan, _position

    provider = install(monkeypatch, ConsumerProvider(complete=complete))
    config = {**_config(account="lx"), "market_data": {"provider": "fixture"}}
    plan = _plan(account_configs={"lx": config}, base_config=config,
        position_records_by_account={"lx": [_position(account="lx", lot_id="lot-lx")]})
    path = tmp_path / "plan.json"
    close_data.publish_close_advice_required_data_plan(path=path, payload=plan)

    def run(command, **kwargs):
        assert command[1:3] == ["-m", "src.application.close_advice_required_data"]
        assert kwargs["timeout"] == 30 and kwargs["check"] is True
        # Isolate process launching while exercising the subprocess's real entry.
        close_data.enrich_close_advice_required_data_plan(plan_path=path, expected_run_id=command[-1])

    monkeypatch.setattr(close_data.subprocess, "run", run)
    assert close_data.enrich_close_advice_required_data_plan_bounded(plan_path=path,
        expected_run_id="run-1", python=tmp_path / "python", repo_root=tmp_path) is None
    loaded = close_data.load_close_advice_required_data_plan(path=path, expected_run_id="run-1")
    assert loaded["market_data_provider"] == "fixture"
    assert loaded["content_sha256"] != plan["content_sha256"]
    requirement = loaded["accounts"]["lx"]["requirements"][0]
    assert requirement["fetch_binding"]["source"] == "fixture"
    from src.application.required_data_prefetch_planning import merge_close_advice_requirements_into_prefetch_config
    merged, merged_plan = merge_close_advice_requirements_into_prefetch_config(
        candidate_config=config, requirements_plan=loaded)
    assert merged_plan["accounts"]["lx"]["requirements"][0]["planning_status"] == "ready"
    assert merged["symbols"][0]["_close_advice_position_requirements"]
    assert requirement["trading_calendar_status"] == ("ok" if complete else "unavailable")
    if complete:
        assert requirement["trading_calendar_receipt"]["coverage_complete"] is True
        assert requirement["trading_calendar_input_hash"]
    assert requirement["market_state_after_snapshot"] == "MORNING"
    assert requirement["market_state_received_at_utc"]
    assert [name for name, _ in provider.calls] == ["open", "calendar", "state"]
    assert provider.closed == [True]


def test_old_close_advice_plan_without_provider_keeps_opend_factory_patchpoint(monkeypatch, tmp_path):
    from test_close_advice_required_data import _config, _plan, _position

    config = _config(account="lx")
    plan = _plan(account_configs={"lx": config}, base_config=config,
        position_records_by_account={"lx": [_position(account="lx", lot_id="lot-lx")]})
    del plan["market_data_provider"]
    path = tmp_path / "plan.json"
    close_data.publish_close_advice_required_data_plan(path=path, payload=plan)
    gateway = SimpleNamespace(
        get_trading_days_with_receipt=lambda **kw: {"retcode": 0, "coverage_complete": True,
            "pagination_complete": True, "page_count": 1,
            "rows": [{"time": kw["start"], "trade_date_type": "WHOLE"}]},
        get_market_state=lambda codes: [{"code": codes[0], "market_state": "CLOSED"}],
        close=lambda: None,
    )
    enriched = close_data.enrich_close_advice_required_data_plan(plan_path=path,
        expected_run_id="run-1", gateway_factory=lambda **kw: gateway)
    requirement = enriched["accounts"]["lx"]["requirements"][0]
    assert requirement["trading_calendar_status"] == "ok"
    assert requirement["market_state_after_snapshot"] == "CLOSED"


def test_scenario_provider_failure_closes_without_opend_fallback(monkeypatch, tmp_path):
    provider = install(monkeypatch, ConsumerProvider())
    def fail(*args, **kwargs):
        raise TimeoutError("fixture quote deadline")
    monkeypatch.setattr(provider, "get_underlier_observations", fail)
    quotes, warnings = scenario._read_futu_quotes(["NVDA"],
        runtime_config={"market_data": {"provider": "fixture"}}, contexts={"lx": {}})
    assert not quotes and any("fixture quote deadline" in warning for warning in warnings)
    assert provider.closed == [True]


def test_scenario_alternate_missing_route_never_uses_broker_endpoint(monkeypatch):
    provider = install(monkeypatch, ConsumerProvider())
    monkeypatch.setattr(provider, "resolve_quote_route", lambda config: SimpleNamespace(status="missing"))
    monkeypatch.setattr(scenario, "infer_futu_portfolio_settings",
                        lambda *a, **kw: pytest.fail("alternate used broker endpoint"))
    quotes, warnings = scenario._read_futu_quotes(["NVDA"],
        runtime_config={"market_data": {"provider": "fixture"}}, contexts={"lx": {}})
    assert not quotes and warnings == ["fixture quote route unavailable"]
    assert provider.closed == []
