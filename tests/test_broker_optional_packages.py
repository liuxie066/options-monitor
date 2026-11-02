"""Offline capability boundaries; run with both official broker wheels installed."""
from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

from src.application import plugin_runtime as runtime
from src.application.broker_provider import resolve_broker_portfolio_provider
from src.application.portfolio_context_service import load_account_portfolio_context
from tests.cash_evidence_helpers import cash_config, cash_portfolio
from tests.test_broker_provider import config as longbridge_config, observation as longbridge_observation, NOW, CREDENTIALS

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        pytest.fail("broker package tests must never connect")
    monkeypatch.setattr(socket.socket, "connect", deny)
    for key, value in CREDENTIALS.items():
        monkeypatch.setenv(key, value)


def absent(monkeypatch, *ids):
    installed = runtime.plugin_installed
    monkeypatch.setattr(runtime, "plugin_installed", lambda plugin_id: plugin_id not in ids and installed(plugin_id))


@pytest.mark.parametrize("broker", ["futu", "longbridge"])
def test_missing_broker_rejects_even_valid_cash_cache_before_reading(tmp_path, monkeypatch, broker):
    if broker == "futu":
        cfg, account = cash_config(), "lx"
        cached = cash_portfolio({"cash_by_currency": {"USD": 1000}, "cash_source_observed_at": NOW.isoformat()}, evaluated_at=NOW)
    else:
        cfg, account, cached = longbridge_config(), "lb", longbridge_observation()
    # Prove this is a cache that the installed broker accepts.
    common = dict(market="富途" if broker == "futu" else "长桥", account=account,
        state_dir=tmp_path, log=lambda _: None, runtime_config=cfg,
        portfolio_source="auto", write_cache=False, now_utc=NOW,
        fetch_futu_portfolio_context_fn=lambda **_: pytest.fail("must use valid cache"))
    assert load_account_portfolio_context(**common, load_json_fn=lambda _: cached)["context_source"] == "account_cache"
    absent(monkeypatch, f"broker-{broker}")
    result = load_account_portfolio_context(**common,
        load_json_fn=lambda _: pytest.fail("missing capability must precede cache read"))
    assert result["context_source"] == "unavailable"
    assert result["cash_snapshot"]["status"] == "unknown"
    assert result["cash_provider_error"] == "PluginUnavailableError"
    assert "cash_by_currency" not in result and "stocks_by_symbol" not in result
    assert f"om plugins install broker-{broker}" in result["errors"][0]


@pytest.mark.parametrize("missing", ["futu", "longbridge"])
def test_accounts_select_only_their_own_installed_broker(monkeypatch, missing):
    from src.application.longbridge_portfolio_context import fetch_longbridge_portfolio_context
    cfg = cash_config()
    cfg["portfolio"]["source"] = "auto"
    cfg["accounts"] = ["lx", "lb"]
    cfg["account_settings"].update(longbridge_config()["account_settings"])
    futu = lambda **_: {"source": "injected-futu"}
    assert resolve_broker_portfolio_provider(cfg, account="lx", futu_provider=futu) is futu
    assert resolve_broker_portfolio_provider(cfg, account="lb", futu_provider=futu) is fetch_longbridge_portfolio_context
    absent(monkeypatch, f"broker-{missing}")
    unavailable, available = ("lx", "lb") if missing == "futu" else ("lb", "lx")
    assert resolve_broker_portfolio_provider(cfg, account=available, futu_provider=futu) is (
        fetch_longbridge_portfolio_context if available == "lb" else futu)
    with pytest.raises(runtime.PluginUnavailableError, match=f"broker-{missing}"):
        resolve_broker_portfolio_provider(cfg, account=unavailable, futu_provider=futu)


@pytest.mark.parametrize("method", [
    "ensure_broker_ready", "get_account_metadata", "get_account_candidates", "get_positions",
    "get_account_balance", "get_funds", "get_order_list", "get_order_list_with_receipt",
    "get_deal_list", "get_history_orders", "get_history_deals", "get_order_fees", "get_positions_with_receipt",
])
def test_market_only_gateway_cannot_access_accounts_even_with_injected_client(monkeypatch, method):
    from src.infrastructure.futu_gateway import FutuGateway
    assert runtime.plugin_installed("market-opend")
    absent(monkeypatch, "broker-futu")
    def deny(*args, **kwargs):
        pytest.fail("account client was touched")
    backend = SimpleNamespace(_ensure_trade_client=deny)
    client = SimpleNamespace(**{method: deny})
    gateway = FutuGateway(client=client, backend=backend, host="offline.invalid", port=1)
    kwargs = {"expected_account_ids": ["123"], "trd_env": "REAL"} if method == "ensure_broker_ready" else {}
    if method == "get_account_metadata":
        kwargs = {"expected_account_id": "123", "trd_env": "REAL", "expected_market": "US"}
    if method == "get_account_candidates":
        kwargs = {"trd_env": "REAL", "expected_market": "US"}
    with pytest.raises(runtime.PluginUnavailableError, match="om plugins install broker-futu"):
        getattr(gateway, method)(**kwargs)


def test_missing_broker_blocks_trade_context_before_sdk_and_longbridge_factory(monkeypatch):
    from src.infrastructure import futu_gateway as futu
    from src.infrastructure.longbridge_gateway import LongbridgeGateway
    absent(monkeypatch, "broker-futu", "broker-longbridge")
    monkeypatch.setattr(futu, "_ensure_futu_api_importable", lambda: pytest.fail("SDK touched"))
    with pytest.raises(runtime.PluginUnavailableError, match="broker-futu"):
        futu.build_futu_trade_context(host="offline.invalid", port=1)
    backend = futu._FutuAPIBackend(host="offline.invalid", port=1)
    backend._trade_client = object()
    for call in (
        backend._ensure_trade_client,
        futu._FutuAPIClient(backend, is_option_chain_cache_enabled=False)._trade,
        lambda: futu.build_ready_futu_broker_gateway(expected_account_ids=["123"], trd_env="REAL"),
    ):
        with pytest.raises(runtime.PluginUnavailableError, match="broker-futu"):
            call()
    with pytest.raises(runtime.PluginUnavailableError, match="broker-longbridge"):
        LongbridgeGateway({}, client=object(), context_factory=lambda **_: pytest.fail("factory touched"))


def test_quote_readiness_works_without_broker_and_does_not_probe_it(monkeypatch):
    from src.infrastructure.futu_gateway import build_ready_futu_quote_gateway
    absent(monkeypatch, "broker-futu", "broker-longbridge")
    class Backend:
        def __init__(self, **kwargs):
            self._quote_client = SimpleNamespace(get_global_state=lambda: (0, {
                "program_status_type": "READY", "qot_logined": True}))
        def _ensure_quote_client(self):
            return self._quote_client
        def _ensure_trade_client(self):
            pytest.fail("quote-only flow touched broker")
    gateway = build_ready_futu_quote_gateway(backend_cls=Backend,
        client_cls=lambda *args, **kwargs: None)
    assert gateway.ensure_quote_ready()["qot_logined"] is True


@pytest.mark.parametrize("method,kwargs", [
    ("ensure_quote_ready", {}), ("get_option_expiration_dates", {"code": "US.AAPL"}),
    ("get_option_chain", {}), ("get_snapshot", {"codes": ["US.AAPL"]}),
    ("get_stock_basicinfo", {"market": "US", "codes": ["US.AAPL"]}),
    ("get_market_state", {"codes": ["US.AAPL"]}), ("get_quote_rights", {"market": "US"}),
    ("get_trading_days", {}), ("get_trading_days_with_receipt", {}),
    ("get_earnings_calendar", {"market": "US", "begin_date": "2026-11-01", "end_date": "2026-11-02"}),
    ("request_history_kline", {}),
])
def test_broker_package_cannot_grant_raw_quote_access(monkeypatch, method, kwargs):
    from src.infrastructure.futu_gateway import FutuGateway
    absent(monkeypatch, "market-opend")
    assert runtime.plugin_installed("broker-futu")
    def deny(*args, **kwargs):
        pytest.fail("missing market must precede quote client access")
    backend = SimpleNamespace(_ensure_quote_client=deny)
    gateway = FutuGateway(client=SimpleNamespace(**{method: deny}), backend=backend, host="offline.invalid", port=1)
    with pytest.raises(runtime.PluginUnavailableError, match="om plugins install market-opend"):
        getattr(gateway, method)(**kwargs)


def test_raw_quote_transport_and_cached_clients_require_market_package(monkeypatch):
    from src.infrastructure import futu_gateway as futu
    absent(monkeypatch, "market-opend")
    monkeypatch.setattr(futu, "_ensure_futu_api_importable", lambda: pytest.fail("SDK touched"))
    backend = futu._FutuAPIBackend(host="offline.invalid", port=1)
    backend._quote_client = object()
    for call in (
        lambda: futu.build_futu_quote_context(host="offline.invalid", port=1),
        backend._ensure_quote_client,
        futu._FutuAPIClient(backend, is_option_chain_cache_enabled=False)._quote,
    ):
        with pytest.raises(runtime.PluginUnavailableError, match="market-opend"):
            call()


def test_longbridge_package_uses_core_environment_owner_after_import(monkeypatch):
    import om_broker_longbridge  # Load before patching to detect a captured alias.
    from src.infrastructure import longbridge_gateway as core
    from tests.test_longbridge_portfolio_context import bound_config, binding_env, Client
    monkeypatch.setattr(core, "build_effective_env", lambda: SimpleNamespace(values=binding_env()))
    settings = bound_config()["account_settings"]["demo"]["longbridge"]
    client = Client()
    gateway = core.LongbridgeGateway(settings, client=client)
    assert isinstance(gateway, om_broker_longbridge.LongbridgeGateway)
    assert gateway.bound_account_id == settings["account_id"]
    assert gateway.identity_error is None
    gateway.close()
    assert gateway._client is client  # Injected clients remain caller owned.


@pytest.mark.parametrize("failure", [RuntimeError("not ready"), TimeoutError("deadline")])
def test_futu_package_builder_preserves_injection_and_closes_on_failure(monkeypatch, failure):
    from src.infrastructure import futu_gateway
    from src.infrastructure.futu_gateway import build_ready_futu_broker_gateway
    calls = []
    gateway = SimpleNamespace(ensure_broker_ready=lambda **kw: calls.append(kw), close=lambda: calls.append("close"))
    def fail(**kwargs):
        calls.append(kwargs)
        raise failure
    gateway.ensure_broker_ready = fail
    monkeypatch.setattr(futu_gateway, "build_futu_gateway", lambda **kwargs: gateway)
    with pytest.raises(type(failure), match=str(failure)):
        build_ready_futu_broker_gateway(expected_account_ids=["123"], trd_env="REAL")
    assert calls == [{"expected_account_ids": ["123"], "trd_env": "REAL"}, "close"]


def test_current_prepared_cannot_bypass_absence_but_frozen_history_is_readable(tmp_path, monkeypatch):
    from src.application import prepared_portfolio_context as prepared
    from tests.test_prepared_portfolio_context import _state_dirs, _config_authorities, _prepare, _generation_kwargs
    shared, states = _state_dirs(tmp_path, "broker-history")
    authorities = _config_authorities(tmp_path, "broker-history")
    manifests = _prepare(tmp_path, shared, states, "broker-history", authorities=authorities)
    identity = _generation_kwargs(tmp_path, Path(manifests["lx"]["manifest_path"]),
        run_id="broker-history", authority=authorities["lx"], manifest_sha256=manifests["lx"]["manifest_sha256"])
    payload = prepared.load_prepared_portfolio_context(**identity)
    absent(monkeypatch, "broker-futu")
    for load in (prepared.load_prepared_portfolio_context, prepared.load_prepared_portfolio_context_receipt):
        with pytest.raises(prepared.PreparedPortfolioContextError, match="broker-futu"):
            load(**identity)
    identity.pop("expected_runtime_config")
    assert prepared.load_prepared_portfolio_context(**identity) == payload
    assert prepared.load_prepared_portfolio_context_receipt(**identity)["payload"] == payload


@pytest.mark.parametrize("broker", ["futu", "longbridge"])
def test_prepared_worker_absence_stops_before_cache_fx_or_valuation(tmp_path, monkeypatch, broker):
    from src.application import prepared_portfolio_context as prepared
    from src.application.tick_run_workspace import publish_account_run_config
    from tests.test_prepared_portfolio_context import _write_worker_request
    cfg = cash_config()
    if broker == "longbridge":
        cfg = longbridge_config()
        cfg["accounts"] = ["lx"]
        cfg["account_settings"]["lx"] = cfg["account_settings"].pop("lb")
    cfg["portfolio"]["account"] = "lx"
    authority = publish_account_run_config(base=tmp_path, run_id="worker-absence", account="lx", config=cfg)
    request_path, result_path = tmp_path / "request.json", tmp_path / "result.json"
    _write_worker_request(tmp_path, authority=authority, run_id="worker-absence", token="test-token",
        request_path=request_path, result_path=result_path)
    absent(monkeypatch, f"broker-{broker}")
    def deny(*args, **kwargs):
        pytest.fail("missing broker must not read cache, FX, providers or PM valuation")
    for name in ("load_cached_json", "load_run_fx_snapshot", "prepare_position_sizing_evidence", "fetch_futu_portfolio_context"):
        monkeypatch.setattr(prepared, name, deny)
    assert prepared.run_worker(request_path) == 0
    result = json.loads(result_path.read_text())
    assert result["status"] == "unavailable"
    assert result["error_type"] == "PluginUnavailableError"
    assert f"om plugins install broker-{broker}" in result["reason"]
    assert "portfolio_context" not in result


@pytest.mark.parametrize("broker", ["futu", "longbridge"])
def test_official_installed_broker_metadata_has_no_dependencies(broker):
    dist = importlib.metadata.distribution(f"om-broker-{broker}-plugin")
    assert dist.version == "0.1.0"
    assert dist.metadata["Requires-Python"] == ">=3.12"
    assert not dist.requires
    module = importlib.import_module(f"om_broker_{broker}")
    assert callable(module.resolve_portfolio_provider)
    assert callable(getattr(module, "LongbridgeGateway" if broker == "longbridge" else "build_ready_broker_gateway"))


def test_core_import_and_offline_longbridge_identity_never_import_plugin_or_sdk():
    result = subprocess.run([sys.executable, "-c", '''
import sys
from importlib.abc import MetaPathFinder
class Deny(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(("om_broker_", "longbridge", "futu")):
            raise AssertionError("core import loaded optional module: " + fullname)
sys.meta_path.insert(0, Deny())
from src.application import broker_provider, portfolio_context_service, prepared_portfolio_context
from src.infrastructure.longbridge_gateway import credential_binding_fingerprint
settings = dict(account_id="test", credentials_env_prefix="FIXTURE", region="ap", trd_env="REAL")
assert len(credential_binding_fingerprint(settings, environ=dict(FIXTURE_APP_KEY="key",
    FIXTURE_APP_SECRET="secret", FIXTURE_ACCESS_TOKEN="token"))) == 64
'''], cwd=ROOT, env={**os.environ, "OM_SECRET_BACKEND": "env"}, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
