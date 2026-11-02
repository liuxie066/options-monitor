"""Installed market capability through public facades, using only offline evidence."""
from pathlib import Path
from types import SimpleNamespace
import sys
import tomllib

import pytest

from src.application import market_data_provider as market
from src.application import plugin_runtime
from src.application.plugin_runtime import PluginUnavailableError


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("market plugin regression attempted network I/O")

    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("urllib.request.urlopen", forbidden)


def disable(monkeypatch, *plugin_ids):
    installed = plugin_runtime.plugin_installed
    monkeypatch.setattr(plugin_runtime, "plugin_installed",
                        lambda key: key not in plugin_ids and installed(key))


def test_lazy_class_keeps_static_public_declaration():
    from scripts.guardrails_check import declared_public_names, present_public_names

    source = Path(market.__file__).read_text()
    assert "OpenDMarketDataProvider" in declared_public_names(source)
    assert "OpenDMarketDataProvider" in present_public_names(source)


def test_package_metadata_and_lazy_class_export():
    import om_market_opend

    metadata = tomllib.loads((Path(__file__).resolve().parents[1] /
        "plugins/market-opend/pyproject.toml").read_text())["project"]
    assert metadata["name"] == "om-market-opend-plugin"
    assert metadata["version"] == "0.1.0"
    assert metadata["requires-python"] == ">=3.12"
    assert metadata["dependencies"] == []
    assert market.OpenDMarketDataProvider is om_market_opend.OpenDMarketDataProvider
    assert market.require_market_data_provider({}).__class__ is om_market_opend.OpenDMarketDataProvider


@pytest.mark.parametrize("operation", [
    lambda: market.open_quote_gateway(),
    lambda: market.fetch_symbol("NVDA"),
    lambda: market.fetch_symbol_request(SimpleNamespace(market_data_provider="opend")),
    lambda: market.resolve_quote_route({}),
    lambda: market.get_stock_spot(None, "US.NVDA"),
    lambda: market.get_trading_days_with_receipt(None, market="US"),
    lambda: market.get_market_state(None, ["US.NVDA"]),
    lambda: market.get_quote_rights(None, market="US"),
    lambda: market.get_underlier_observation("NVDA"),
    lambda: market.get_underlier_observations(None, ["US.NVDA"]),
    lambda: market.discover_option_expirations("NVDA"),
    lambda: market.fetch_option_chains(),
    lambda: market.prefetch_market_earnings_calendars(),
])
def test_missing_market_disables_live_facades_even_with_sdk(monkeypatch, operation):
    disable(monkeypatch, "market-opend")
    monkeypatch.setitem(sys.modules, "futu", SimpleNamespace(OpenQuoteContext=lambda **kw:
                        pytest.fail("SDK presence cannot replace a market plugin")))
    with pytest.raises(PluginUnavailableError, match="install market-opend"):
        operation()


def test_missing_market_admission_precedes_prefetch_and_calendar_io(monkeypatch, tmp_path):
    from src.application.multi_tick.required_data_prefetch import prefetch_required_data
    from src.application.multi_account_tick import _is_trading_day_guard_for_market

    disable(monkeypatch, "market-opend")
    with pytest.raises(PluginUnavailableError, match="market-opend"):
        prefetch_required_data(vpy=tmp_path / "unused", base=tmp_path, cfg={},
                               shared_required=tmp_path / "required")
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(PluginUnavailableError, match="market-opend"):
        _is_trading_day_guard_for_market({}, "US")


def test_no_broker_still_evaluates_real_opend_public_quote_facade(monkeypatch, tmp_path):
    from src.application import option_candidates as candidates
    from src.application import opend_symbol_fetching as opend
    from src.application.tool_execution import execute_tool
    from test_option_candidates_contract import NOW, _bundle, _row

    disable(monkeypatch, "broker-futu", "broker-longbridge")
    config = {"symbols": [{"symbol": "NVDA", "fetch": {
        "source": "futu", "host": "offline.invalid", "port": 11111}}]}
    payload = _bundle([_row()], source="opend")
    calls, closed = [], []
    gateway = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(candidates, "load_runtime_config", lambda **kw:
                        (tmp_path / "config.us.json", config))
    monkeypatch.setattr(candidates, "ensure_runtime_config_freshness", lambda *a, **kw: None)
    monkeypatch.setattr(candidates, "_now", lambda: NOW)
    monkeypatch.setattr(candidates, "current_exchange_rate_snapshot", lambda **kw: {})
    monkeypatch.setattr(candidates, "rates_for_purpose", lambda *a, **kw: {"USDCNY": 7.2})
    monkeypatch.setattr("src.infrastructure.futu_gateway.build_ready_futu_quote_gateway",
                        lambda **kw: gateway)
    monkeypatch.setattr(opend, "fetch_symbol_request", lambda request, **kw:
                        calls.append(request) or payload)
    monkeypatch.setattr("src.application.earnings_calendar.fetch_market_earnings_calendar", lambda **kw: None)
    with candidates.option_candidates_context(runtime_root=tmp_path):
        response = execute_tool("option_candidates", {"queries": [{"symbol": "NVDA", "strategy": "short_put"}]})
    assert response["ok"], response
    assert response["data"]["results"][0]["outcome"] == "found"
    assert response["data"]["results"][0]["candidates"][0]["evidence"]["iv_source"] == "opend"
    assert response["data"]["account_checks"] == "not_evaluated"
    assert calls[0].gateway is gateway and calls[0].market_data_provider == "opend"
    assert closed == [True]


def test_missing_market_public_tool_explains_installation_before_fetch(monkeypatch, tmp_path):
    from src.application import option_candidates as candidates
    from src.application.tool_execution import execute_tool

    disable(monkeypatch, "market-opend")
    monkeypatch.setattr(candidates, "load_runtime_config", lambda **kw:
                        (tmp_path / "config.us.json", {}))
    monkeypatch.setattr(candidates, "ensure_runtime_config_freshness", lambda *a, **kw: None)
    with candidates.option_candidates_context(runtime_root=tmp_path):
        response = execute_tool("option_candidates", {"queries": [{"symbol": "NVDA", "strategy": "short_put"}]})
    assert not response["ok"]
    assert "install market-opend" in response["error"]["message"]


@pytest.mark.parametrize("provider,expected", [("fixture", set()),
    ("opend", {("quote.invalid", 12345, "quote")})])
def test_missing_broker_has_no_watchdog_or_phone_probe(monkeypatch, tmp_path, provider, expected):
    from src.application.tick_guard_flow import TickGuardRequest, run_tick_guard_flow
    from src.application import multi_tick_watchdog
    from test_market_provider_identity_guard import _config
    from test_market_data_provider import RecordingProvider, install

    install(monkeypatch, RecordingProvider(None))
    monkeypatch.setattr(multi_tick_watchdog, "plugin_installed", lambda key: False)
    calls, phone_checks = [], []
    quiet = lambda *a, **kw: None
    result = run_tick_guard_flow(TickGuardRequest(base=tmp_path, base_cfg=_config(provider, "futu"),
        accounts=["user"], default_account="user", market_config="auto", no_send=True,
        opend_phone_verify_continue=False, vpy=tmp_path / "unused", runlog=SimpleNamespace(safe_event=quiet),
        audit_helper=SimpleNamespace(audit=quiet, guard_mark_failure=quiet), complete_tick_idempotency_fn=quiet,
        allow_operational_side_effects=False, admit_project_run_fn=lambda *a: {"allowed": True},
        apply_project_load_shed_fn=lambda accounts, guard: accounts,
        is_opend_phone_verify_pending_fn=lambda *a: phone_checks.append(True) or False,
        run_opend_watchdog_fn=lambda **kw: calls.append(kw) or {"ok": True}))
    assert result.should_continue
    assert {(call["host"], call["port"], call["required_capability"]) for call in calls} == expected
    assert phone_checks == ([True] if expected else [])


def test_missing_market_tick_admission_blocks_all_probes(monkeypatch, tmp_path):
    from src.application.tick_guard_flow import TickGuardRequest, run_tick_guard_flow
    from test_market_provider_identity_guard import _config

    disable(monkeypatch, "market-opend")
    events, completed = [], []
    forbidden = lambda *a, **kw: pytest.fail("missing market started an OpenD probe")
    quiet = lambda *a, **kw: None
    result = run_tick_guard_flow(TickGuardRequest(base=tmp_path, base_cfg=_config("opend", "futu"),
        accounts=["user"], default_account="user", market_config="auto", no_send=True,
        opend_phone_verify_continue=False, vpy=tmp_path / "unused",
        runlog=SimpleNamespace(safe_event=lambda *a, **kw: events.append(kw)),
        audit_helper=SimpleNamespace(audit=quiet, guard_mark_failure=forbidden),
        complete_tick_idempotency_fn=lambda **kw: completed.append(kw),
        allow_operational_side_effects=False, admit_project_run_fn=lambda *a: {"allowed": True},
        apply_project_load_shed_fn=lambda accounts, guard: accounts,
        is_opend_phone_verify_pending_fn=forbidden, run_opend_watchdog_fn=forbidden))
    assert not result.should_continue and result.return_code == 2
    assert events[-1]["error_code"] == "PLUGIN_UNAVAILABLE"
    assert completed[-1]["error_code"] == "PLUGIN_UNAVAILABLE"


def test_missing_plugins_keep_cli_identity_and_frozen_history_readable(monkeypatch, tmp_path, capsys):
    from src.interfaces.cli.main import main
    from src.application.close_advice_runner import run_close_advice
    from test_close_advice_required_data import _frozen_workspace

    frozen = _frozen_workspace(tmp_path)
    disable(monkeypatch, "market-opend", "broker-futu", "broker-longbridge")
    assert market.resolve_market_data_provider({}).name == "opend"
    assert market.market_data_source("opend", "futu") == "futu"
    assert market.market_data_source("opend", "opend") == "opend"
    assert main(["help"]) == 0
    assert main(["tools", "spec"]) == 0
    capsys.readouterr()
    result = run_close_advice(**frozen.run_kwargs(tmp_path))
    assert result["snapshot_authority"] == "valid"
    assert result["quote_mode"] == "frozen_snapshot"
    assert result["quote_fetch_diagnostics"]["network_fetch_attempts"] == 0
