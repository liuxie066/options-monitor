from __future__ import annotations

import builtins
import importlib
import json
import os
import subprocess
import sys
from concurrent.futures import CancelledError
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from domain.domain.risk_capacity import evaluate_cash_snapshot
from src.application.longbridge_portfolio_context import fetch_longbridge_portfolio_context
from src.infrastructure.longbridge_gateway import LongbridgeGateway, credential_binding_fingerprint


def config():
    return {"accounts": ["demo"], "_resolved": {"market": "us"}, "account_settings": {
        "demo": {"type": "longbridge", "longbridge": {"account_id": "expected-123",
            "credentials_env_prefix": "OM_LB_DEMO", "region": "ap", "trd_env": "REAL"}}}}


def stock(symbol="AAPL.US", quantity="10.5", available="8.25", cost="150"):
    return SimpleNamespace(symbol=symbol, symbol_name="Apple", quantity=Decimal(quantity),
        available_quantity=Decimal(available) if available is not None else None,
        cost_price=Decimal(cost) if cost is not None else None,
        currency="HKD" if symbol.endswith(".HK") else "USD",
        market="HK" if symbol.endswith(".HK") else "US")


class Client:
    def __init__(self, *, rows=None, cash=None):
        self.rows = [stock()] if rows is None else rows
        self.cash = [SimpleNamespace(currency="USD", available_cash=Decimal("-250.5"),
            withdraw_cash=Decimal("-250.5"), frozen_cash=Decimal("500"), settling_cash=Decimal("1000"))] if cash is None else cash
        self.calls = []

    def account_balance(self):
        self.calls.append("balance")
        return [SimpleNamespace(currency="HKD", total_cash=Decimal("999999"), buy_power=Decimal("9999999"),
            max_finance_amount=Decimal("8888888"), cash_infos=self.cash)]

    def stock_positions(self):
        self.calls.append("positions")
        return SimpleNamespace(channels=[SimpleNamespace(account_channel="lb", positions=self.rows)])


def fetch(client=None, **kwargs):
    return fetch_longbridge_portfolio_context(cfg=config(), account="demo", client=client or Client(), **kwargs)


def test_sdk_shaped_response_retains_signed_cash_fractional_stock_cost_and_identity_gap():
    client = Client()
    result = fetch(client)
    assert client.calls == ["balance", "positions"]
    assert result["portfolio_source_name"] == "longbridge"
    assert result["cash_by_currency"] == {"USD": -250.5}
    assert result["cash_power_by_currency"] == {}
    assert result["cash_balance_reliable"] is False
    assert result["cash_source_observed_at"]
    assert result["source_account_identifiers"] == []
    authority = result["capacity_authority"]
    assert {key: authority[key] for key in ("schema_version", "status", "logical_account", "broker_id",
        "broker_account_id", "trd_env", "market", "source", "source_observed_at")} == {"schema_version": "physical_account_capacity_authority.v2",
        "status": "unavailable", "logical_account": "demo", "broker_id": "longbridge",
        "broker_account_id": None, "trd_env": "REAL", "market": "us", "source": "longbridge",
        "source_observed_at": authority["source_observed_at"]}
    position = result["stocks_by_symbol"]["AAPL"]
    assert (position["shares"], position["can_sell_qty"], position["avg_cost"]) == (10.5, 8.25, 150)
    assert position["eligible_underlying_shares"] is None
    assert position["capacity_authority_status"] == "unavailable"
    snapshot = result["position_snapshot_input"]
    assert snapshot["schema_version"] == "position_snapshot.v1"
    assert snapshot["rows"][0]["quantity"] == "10.5"
    assert snapshot["rows"][0]["sellable_quantity"] == "8.25"
    assert snapshot["broker_account_ref"]["external_account_id"] is None
    assert snapshot["source_evidence"][0]["evidence_id"] in snapshot["evidence_refs"]
    assert "longbridge_account_identity_unverified" in snapshot["errors"]
    assert "expected-123" not in json.dumps(result)
    assert "futu_account_id" not in json.dumps(result)
    verdict = evaluate_cash_snapshot(result, expected_authority={"logical_account": "demo",
        "broker_id": "longbridge", "broker_account_id": "expected-123", "trd_env": "REAL", "market": "us"},
        evaluated_at=datetime.now(timezone.utc), max_age_sec=300)
    assert verdict["status"] != "fresh"


def binding_env():
    return {"OM_LB_DEMO_APP_KEY": "fixture-key", "OM_LB_DEMO_APP_SECRET": "fixture-secret",
            "OM_LB_DEMO_ACCESS_TOKEN": "fixture-token"}


def bound_config():
    cfg = config()
    settings = cfg["account_settings"]["demo"]["longbridge"]
    settings["credential_binding_sha256"] = credential_binding_fingerprint(settings, environ=binding_env())
    return cfg


def test_confirmed_binding_enables_real_cash_and_stock_capacity_without_broker_echo():
    cfg = bound_config()
    result = fetch_longbridge_portfolio_context(cfg=cfg, account="demo", client=Client(), environ=binding_env())
    authority = result["capacity_authority"]
    assert authority["status"] == "available"
    assert authority["broker_account_id"] == "expected-123"
    assert authority["binding_method"] == "user_confirmed_credentials_sha256"
    assert authority["broker_echoed"] is False
    assert authority["credential_binding_sha256"] == cfg["account_settings"]["demo"]["longbridge"]["credential_binding_sha256"]
    assert result["account_identity_evidence"]["status"] == "user_bound"
    assert result["source_account_identifiers"] == ["expected-123"]
    assert result["cash_balance_reliable"] is True
    assert result["cash_capacity_by_currency"]["USD"]["amount"] == -250.5
    assert result["cash_capacity_by_currency"]["USD"]["capacity_authority_status"] == "available"
    row = result["stocks_by_symbol"]["AAPL"]
    assert row["eligible_underlying_shares"] == 8.25
    assert result["position_snapshot_input"]["errors"] == []
    assert result["position_snapshot_input"]["completeness"] == "complete"
    assert result["position_snapshot_input"]["broker_account_ref"]["external_account_id"] == "expected-123"
    verdict = evaluate_cash_snapshot(result, expected_authority={"logical_account": "demo",
        "broker_id": "longbridge", "broker_account_id": "expected-123", "trd_env": "REAL", "market": "us",
        "credential_binding_sha256": authority["credential_binding_sha256"]},
        evaluated_at=datetime.now(timezone.utc), max_age_sec=300)
    assert verdict["status"] == "fresh"


@pytest.mark.parametrize("field", ["app_key", "app_secret", "access_token", "account_id"])
def test_binding_mismatch_fails_closed(field):
    cfg, env = bound_config(), binding_env()
    if field == "account_id":
        cfg["account_settings"]["demo"]["longbridge"][field] = "different-account"
    else:
        env[f"OM_LB_DEMO_{field.upper()}"] = "changed-fixture"
    result = fetch_longbridge_portfolio_context(cfg=cfg, account="demo", client=Client(), environ=env)
    assert result["capacity_authority"]["status"] == "unavailable"
    assert result["source_account_identifiers"] == []
    assert "longbridge_credential_binding_mismatch" in result["errors"]
    assert result["cash_balance_reliable"] is False
    assert result["cash_valuation"]["status"] == "unavailable"


def test_total_cash_valuation_is_separate_from_capacity_without_double_counting():
    result = fetch_longbridge_portfolio_context(cfg=bound_config(), account="demo", client=Client(), environ=binding_env())
    assert result["cash_by_currency"] == {"USD": -250.5}
    valuation = result["cash_valuation"]
    assert valuation["amount"] == 999999
    assert valuation["currency"] == "HKD"
    assert valuation["source"] == "longbridge.account_balance.total_cash"
    assert valuation["additive_with_cash_by_currency"] is False
    assert valuation["status"] == "available"
    assert valuation["errors"] == []


def test_offline_fingerprint_preview_has_no_credentials_or_sdk_import():
    env = {**os.environ, **binding_env()}
    code = "import sys; from src.infrastructure.longbridge_gateway import main; main(); assert 'longbridge.openapi' not in sys.modules"
    result = subprocess.run([sys.executable, "-c", code, "--account-id", "expected-123",
        "--credentials-env-prefix", "OM_LB_DEMO", "--region", "ap", "--trd-env", "REAL"],
        env=env, text=True, capture_output=True, check=True)
    payload = json.loads(result.stdout)
    assert payload["credential_binding_sha256"] == bound_config()["account_settings"]["demo"]["longbridge"]["credential_binding_sha256"]
    assert payload["broker_echoed"] is False
    assert all(secret not in result.stdout + result.stderr for secret in binding_env().values())


@pytest.mark.parametrize("value", ["NaN", "Infinity", None, True, "bad"])
def test_invalid_cash_never_becomes_zero(value):
    result = fetch(Client(cash=[SimpleNamespace(currency="USD", available_cash=value)]))
    assert result["cash_by_currency"] == {}
    assert "longbridge_cash_amount_or_currency_invalid" in result["errors"]
    json.dumps(result, allow_nan=False)


def test_zero_cash_missing_cost_and_missing_sellable_are_distinct():
    result = fetch(Client(rows=[stock(cost=None, available=None)],
        cash=[SimpleNamespace(currency="USD", available_cash=Decimal("0"))]))
    assert result["cash_by_currency"] == {"USD": 0}
    row = result["stocks_by_symbol"]["AAPL"]
    assert row["can_sell_qty"] is None
    assert row["avg_cost"] is None
    assert row["cost_unknown_shares"] == 10.5
    assert row["cost_basis_complete"] is False


def test_duplicate_cash_currency_does_not_double_account_balance():
    result = fetch(Client(cash=[SimpleNamespace(currency="CNH", available_cash="100"),
        SimpleNamespace(currency="CNY", available_cash="100")]))
    assert result["cash_by_currency"] == {}
    assert "longbridge_duplicate_cash_currency" in result["errors"]


def test_hk_symbol_canonicalization_and_short_positions():
    cfg = config()
    cfg["_resolved"]["market"] = "hk"
    result = fetch_longbridge_portfolio_context(cfg=cfg, account="demo", client=Client(rows=[
        stock("700.HK", "100", "80"), stock("5.HK", "-50", None)]))
    assert set(result["stocks_by_symbol"]) == {"0700.HK"}
    assert result["stocks_by_symbol"]["0700.HK"]["shares"] == 100
    short = result["position_snapshot_input"]["rows"][1]
    assert (short["instrument_ref"]["symbol"], short["position_side"], short["quantity"]) == ("0005.HK", "short", "50")


def test_sdk_market_enum_string_is_normalized():
    # longbridge 5.3.0 native Market.US has no .name/.value; str() is Market.US.
    class Market:
        def __str__(self):
            return "Market.US"
    row = stock()
    row.market = Market()
    result = fetch(Client(rows=[row]))
    assert "AAPL" in result["stocks_by_symbol"]


@pytest.mark.parametrize("available", ["-450", "11", "NaN"])
def test_invalid_sellable_is_unavailable_not_clamped_or_absolute(available):
    result = fetch(Client(rows=[stock(quantity="10", available=available)]))
    assert result["stocks_by_symbol"]["AAPL"]["can_sell_qty"] is None
    assert "longbridge_position_sellable_quantity_unavailable" in result["errors"]


def test_options_never_claim_complete_identity_or_multiplier():
    result = fetch(Client(rows=[stock("AAPL261120C150000.US", "1", "1")]), include_options=True)
    snapshot = result["position_snapshot_input"]
    assert snapshot["completeness"] == "partial"
    assert snapshot["rows"] == []
    assert "longbridge_option_contract_identity_unsupported" in snapshot["errors"]


def test_empty_success_is_not_failed_query():
    result = fetch(Client(rows=[]))
    assert result["stocks_by_symbol"] == {}
    assert result["source_observed_at"]
    assert not any("positions_failed" in error for error in result["errors"])


def test_provider_failure_keeps_other_observation_and_redacts_error():
    class Failed(Client):
        def account_balance(self):
            raise RuntimeError("secret-access-token")
    result = fetch(Failed())
    assert result["cash_by_currency"] == {}
    assert result["cash_source_observed_at"] is None
    assert result["cash_provider_error"] == "longbridge_balance_failed:RuntimeError"
    assert result["stocks_by_symbol"]["AAPL"]["shares"] == 10.5
    assert "secret-access-token" not in json.dumps(result)


def test_malformed_positions_are_explicit_failure():
    class Failed(Client):
        def stock_positions(self):
            return SimpleNamespace(channels=None)
    result = fetch(Failed())
    assert result["cash_by_currency"] == {"USD": -250.5}
    assert "longbridge_positions_failed:ValueError" in result["errors"]


@pytest.mark.parametrize("exception", [TimeoutError, CancelledError])
def test_timeout_and_cancellation_propagate(exception):
    class Failed(Client):
        def stock_positions(self):
            raise exception()
    with pytest.raises(exception):
        fetch(Failed())


def test_unsupported_region_environment_fail_before_client_calls():
    for field, value in (("region", "us"), ("trd_env", "SIMULATE")):
        cfg = config()
        cfg["account_settings"]["demo"]["longbridge"][field] = value
        client = Client()
        with pytest.raises(ValueError):
            fetch_longbridge_portfolio_context(cfg=cfg, account="demo", client=client)
        assert client.calls == []


def test_optional_sdk_lazy_and_missing_credentials_precede_import(monkeypatch):
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        assert not name.startswith("longbridge"), "must not import optional SDK"
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    importlib.reload(sys.modules["src.infrastructure.longbridge_gateway"])
    with pytest.raises(ValueError, match="environment variable missing"):
        LongbridgeGateway(config()["account_settings"]["demo"]["longbridge"], environ={})


def test_factory_uses_named_environment_credentials_and_closes_owned_context():
    settings = config()["account_settings"]["demo"]["longbridge"]
    calls = []
    client = Client()
    client.close = lambda: calls.append("closed")
    def factory(**kwargs):
        calls.append(kwargs)
        return client
    gateway = LongbridgeGateway(settings, environ={"OM_LB_DEMO_APP_KEY": "fixture-key",
        "OM_LB_DEMO_APP_SECRET": "fixture-secret", "OM_LB_DEMO_ACCESS_TOKEN": "fixture-token"}, context_factory=factory)
    gateway.account_balance()
    gateway.stock_positions()
    gateway.close()
    assert calls == [{"app_key": "fixture-key", "app_secret": "fixture-secret",
        "access_token": "fixture-token", "enable_papertrading": False}, "closed"]
    assert client.calls == ["balance", "positions"]


def test_sdk_env_override_cannot_silently_redirect_scope():
    env = {"OM_LB_DEMO_APP_KEY": "fixture-key", "OM_LB_DEMO_APP_SECRET": "fixture-secret",
        "OM_LB_DEMO_ACCESS_TOKEN": "fixture-token", "LONGBRIDGE_PAPERTRADING": "true"}
    with pytest.raises(ValueError, match="overrides are unsupported"):
        LongbridgeGateway(config()["account_settings"]["demo"]["longbridge"], environ=env,
            context_factory=lambda **kwargs: pytest.fail("must not connect"))


@pytest.mark.parametrize("process_credentials", [False, True])
def test_env_file_credentials_match_preview_and_gateway_without_global_secret_injection(
    tmp_path, monkeypatch, process_credentials,
):
    from src.application.settings import bootstrap_process_env

    env_file = tmp_path / "longbridge.env"
    env_file.write_text("\n".join(f"{key}={value}" for key, value in binding_env().items()))
    monkeypatch.setenv("OM_ENV_FILE", str(env_file))
    monkeypatch.setenv("OM_SECRET_BACKEND", "auto")
    for key in binding_env():
        if process_credentials:
            monkeypatch.setenv(key, "older-process-credential")
        else:
            monkeypatch.delenv(key, raising=False)
    bootstrap_process_env(include_local_env_file=False)
    process_before = dict(os.environ)
    settings = bound_config()["account_settings"]["demo"]["longbridge"]
    assert credential_binding_fingerprint(settings) == settings["credential_binding_sha256"]
    calls = []
    gateway = LongbridgeGateway(settings, context_factory=lambda **kwargs: calls.append(kwargs) or Client())
    assert gateway.bound_account_id == settings["account_id"]
    assert calls == [{"app_key": "fixture-key", "app_secret": "fixture-secret",
                      "access_token": "fixture-token", "enable_papertrading": False}]
    gateway.close()
    preview = subprocess.run([sys.executable, "-B", "-m", "src.infrastructure.longbridge_gateway",
        "--account-id", settings["account_id"], "--credentials-env-prefix", settings["credentials_env_prefix"]],
        env=dict(os.environ), text=True, capture_output=True, check=True)
    assert json.loads(preview.stdout)["credential_binding_sha256"] == settings["credential_binding_sha256"]
    assert all(secret not in preview.stdout + preview.stderr for secret in binding_env().values())
    assert dict(os.environ) == process_before
    for key in ("OM_LB_DEMO_APP_SECRET", "OM_LB_DEMO_ACCESS_TOKEN"):
        assert os.environ.get(key) == ("older-process-credential" if process_credentials else None)


def test_explicit_credential_environment_does_not_overlay_selected_file(tmp_path, monkeypatch):
    env_file = tmp_path / "other-instance.env"
    env_file.write_text("\n".join(f"{key}=other-instance" for key in binding_env()))
    monkeypatch.setenv("OM_ENV_FILE", str(env_file))
    settings = bound_config()["account_settings"]["demo"]["longbridge"]
    assert credential_binding_fingerprint(settings, environ=binding_env()) == settings["credential_binding_sha256"]
    gateway = LongbridgeGateway(settings, environ=binding_env(), client=Client())
    assert gateway.bound_account_id == settings["account_id"]
    with pytest.raises(ValueError, match="environment variable missing"):
        credential_binding_fingerprint(settings, environ={})


@pytest.mark.parametrize("explicit_snapshot", [False, True])
def test_sdk_process_override_cannot_be_hidden_by_selected_environment(tmp_path, monkeypatch, explicit_snapshot):
    env_file = tmp_path / "longbridge.env"
    env_file.write_text("\n".join(f"{key}={value}" for key, value in binding_env().items())
                        + "\nLONGBRIDGE_TRADE_WS_URL=\n")
    monkeypatch.setenv("OM_ENV_FILE", str(env_file))
    monkeypatch.setenv("LONGBRIDGE_TRADE_WS_URL", "wss://offline.invalid")
    with pytest.raises(ValueError, match="overrides are unsupported"):
        LongbridgeGateway(bound_config()["account_settings"]["demo"]["longbridge"],
            environ=binding_env() if explicit_snapshot else None,
            context_factory=lambda **kwargs: pytest.fail("must reject before creating SDK context"))
