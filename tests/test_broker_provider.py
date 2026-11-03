from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from domain.domain.position_snapshot import normalize_position_snapshot_input
from domain.domain.option_position_identity import normalize_broker
from src.application.account_config import (
    build_account_portfolio_source_plan, build_account_runtime_config,
    build_account_runtime_plan, resolve_account_broker,
)
from src.application.config_validator import validate_config
from src.application.config_yaml import yaml_to_market_user_config
from src.application.portfolio_context_service import (
    evaluate_account_cash_snapshot, load_account_portfolio_context,
    portfolio_position_snapshot_errors,
)
from src.infrastructure.longbridge_gateway import credential_binding_fingerprint


NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
SETTINGS = {"account_id": "LB123", "credentials_env_prefix": "OM_LB", "region": "ap", "trd_env": "REAL"}
CREDENTIALS = {"OM_LB_APP_KEY": "fixture-key", "OM_LB_APP_SECRET": "fixture-secret", "OM_LB_ACCESS_TOKEN": "fixture-token"}
BINDING = credential_binding_fingerprint(SETTINGS, environ=CREDENTIALS)


@pytest.fixture(autouse=True)
def bound_credentials(monkeypatch):
    for key, value in CREDENTIALS.items():
        monkeypatch.setenv(key, value)


def config():
    return {"accounts": ["lb"], "_resolved": {"market": "us"},
            "portfolio": {"source": "auto", "base_currency": "CNY"},
            "account_settings": {"lb": {"type": "longbridge", "longbridge": {
                "account_id": "LB123", "credentials_env_prefix": "OM_LB",
                "region": "ap", "trd_env": "REAL", "credential_binding_sha256": BINDING}}}}


def observation():
    authority = {"schema_version": "physical_account_capacity_authority.v2", "status": "available",
                 "logical_account": "lb", "broker_id": "longbridge", "broker_account_id": "LB123",
                 "trd_env": "REAL", "market": "us", "source": "longbridge",
                 "credential_binding_sha256": BINDING,
                 "binding_method": "user_confirmed_credentials_sha256", "broker_echoed": False}
    snapshot = normalize_position_snapshot_input({
        "schema_version": "position_snapshot.v1", "snapshot_id": "lb-1", "source_id": "longbridge.positions",
        "broker_account_ref": {"broker_account_id": "longbridge:REAL:LB123", "broker_id": "longbridge",
                               "external_account_id": "LB123", "environment": "REAL", "account_label": "lb"},
        "scope": {"markets": ["US", "HK"], "asset_types": ["stock"], "filtered": False},
        "completeness": "complete", "quality": {"status": "ready"},
        "observed_at_utc": NOW.isoformat(), "rows": [],
    })
    return {"portfolio_source_name": "longbridge", "capacity_authority": authority,
            "source_account_identifiers": ["LB123"], "filters": {"account": "lb", "broker": "长桥"},
            "cash_source_observed_at": NOW.isoformat(), "cash_balance_reliable": True,
            "cash_balance_unavailable_by_row": {}, "cash_by_currency": {"USD": 1000, "HKD": -100},
            "position_snapshot_input": snapshot, "stocks_by_symbol": {}}


def verdict(ctx):
    return evaluate_account_cash_snapshot(ctx, config=config(), account="lb", evaluated_at=NOW)


def test_longbridge_plan_and_scoped_broker_do_not_enable_futu_intake(tmp_path):
    cfg = config()
    assert build_account_portfolio_source_plan(cfg, account="lb").primary_source == "longbridge"
    plan = build_account_runtime_plan(cfg, account="lb")
    assert plan.portfolio_source == "longbridge" and not plan.trade_intake_enabled
    assert plan.futu_account_id is None
    scoped = build_account_runtime_config(base_cfg=cfg, cfg_path=tmp_path / "config.json", account="lb", markets_to_run=[])
    assert scoped["portfolio"]["broker"] == "长桥"
    assert resolve_account_broker(cfg, account="lb") == "长桥"
    assert normalize_broker("Longbridge") == normalize_broker("长桥")


def test_longbridge_cash_and_holdings_are_qualified_by_broker():
    ctx = observation()
    assert verdict(ctx)["status"] == "fresh"
    assert portfolio_position_snapshot_errors(ctx, account="lb", now_utc=NOW) == []
    ctx["position_snapshot_input"]["broker_account_ref"]["broker_id"] = "futu"
    assert "snapshot_broker_mismatch" in portfolio_position_snapshot_errors(ctx, account="lb", now_utc=NOW)


@pytest.mark.parametrize("field,value", [
    ("broker_id", "futu"), ("broker_account_id", "another"), ("logical_account", "other"),
    ("source", "opend"), ("trd_env", "SIMULATE"), ("market", "hk"),
    ("schema_version", "physical_account_capacity_authority.v1"),
])
def test_longbridge_cash_rejects_foreign_authority(field, value):
    ctx = observation()
    ctx["capacity_authority"][field] = value
    assert "CASH_IDENTITY_MISMATCH" in verdict(ctx)["reason_codes"]


def test_longbridge_cash_stale_and_partial_never_become_zero():
    ctx = observation()
    ctx["cash_source_observed_at"] = (NOW - timedelta(seconds=901)).isoformat()
    assert verdict(ctx)["status"] == "stale"
    ctx["cash_balance_unavailable_by_row"] = {"USD": "missing"}
    assert verdict(ctx)["status"] == "unknown"


def test_longbridge_loader_does_not_call_futu_or_reuse_its_cache(tmp_path, monkeypatch):
    import src.application.portfolio_context_service as service
    from tests.cash_evidence_helpers import cash_portfolio
    calls = []
    monkeypatch.setattr(service, "resolve_broker_portfolio_provider", lambda *args, **kwargs: lambda **params: calls.append(params) or observation())
    result = load_account_portfolio_context(
        market="富途", account="lb", state_dir=tmp_path, log=lambda _: None,
        runtime_config=config(), portfolio_source="auto", write_cache=False,
        required_position_asset_types=("stock",), now_utc=NOW,
        fetch_futu_portfolio_context_fn=lambda **kwargs: pytest.fail("Futu was called"),
        load_json_fn=lambda _: cash_portfolio({}, account="lb", account_id="LB123", evaluated_at=NOW),
    )
    assert calls[0]["market"] == "长桥"
    assert result["context_source"] == "longbridge_direct"
    assert result["cash_snapshot"]["status"] == "fresh"
    assert not (tmp_path / "portfolio_context.json").exists()


@pytest.mark.parametrize("changes", [{"access_token": "not-allowed"}, {"region": "us"}, {"trd_env": "SIMULATE"}, {"credentials_env_prefix": "secret-value"}, {"credential_binding_sha256": "invalid"}, {"credential_binding_sha256": None}])
def test_longbridge_config_rejects_unverified_settings_and_inline_secrets(changes):
    from src.application.config_defaults import default_config
    cfg = default_config()
    cfg.update(config())
    cfg["symbols"] = [{"symbol": "NVDA", "broker": "us"}]
    cfg["account_settings"]["lb"]["longbridge"].update(changes)
    with pytest.raises(SystemExit, match="longbridge"):
        validate_config(cfg)


def test_yaml_build_preserves_broker_mapping_and_market_plugin():
    cfg = config()
    raw = {"accounts": {"lb": deepcopy(cfg["account_settings"]["lb"])},
           "portfolio": cfg["portfolio"], "market_data": {"provider": "opend"},
           "markets": {"us": {"accounts": ["lb"], "symbols": ["NVDA"]}}}
    output = yaml_to_market_user_config(raw, market="us")
    assert output["account_settings"]["lb"]["longbridge"]["account_id"] == "LB123"
    assert output["account_settings"]["lb"]["longbridge"]["credential_binding_sha256"] == BINDING
    assert output["market_data"] == {"provider": "opend"}


def test_broker_mismatch_is_not_an_implicit_fallback():
    with pytest.raises(ValueError, match="does not match"):
        build_account_portfolio_source_plan(config(), account="lb", portfolio_source="futu")


@pytest.mark.parametrize("mutation", ["rotate", "missing", "unbound"])
def test_cached_longbridge_cash_invalidated_when_credentials_change(monkeypatch, mutation):
    cfg = config()
    ctx = observation()
    assert verdict(ctx)["status"] == "fresh"
    if mutation == "rotate":
        monkeypatch.setenv("OM_LB_ACCESS_TOKEN", "rotated-token")
    elif mutation == "missing":
        monkeypatch.delenv("OM_LB_ACCESS_TOKEN")
    else:
        cfg["account_settings"]["lb"]["longbridge"].pop("credential_binding_sha256")
    result = evaluate_account_cash_snapshot(ctx, config=cfg, account="lb", evaluated_at=NOW)
    assert result["status"] == "unknown"
    assert "CASH_IDENTITY_MISMATCH" in result["reason_codes"]


@pytest.mark.parametrize("rotated", [False, True])
@pytest.mark.parametrize("credential_source", ["process", "env_file"])
def test_healthcheck_uses_longbridge_binding_without_futu_account_requirement(
    tmp_path, monkeypatch, rotated, credential_source,
):
    from test_agent_plugin_smoke import _public_cfg_with_futu, _write_healthcheck_config, _patch_healthcheck_dependencies
    from src.application.tool_execution import execute_tool

    cfg = _public_cfg_with_futu("portfolio.runtime.json")
    cfg.update(config())
    cfg["portfolio"].update(account="lb", data_config="portfolio.runtime.json")
    cfg["trade_intake"] = {"enabled": False}
    path = _write_healthcheck_config(tmp_path, cfg=cfg)
    _patch_healthcheck_dependencies(monkeypatch)
    if credential_source == "env_file":
        credentials = {**CREDENTIALS}
        if rotated:
            credentials["OM_LB_ACCESS_TOKEN"] = "rotated"
        env_file = tmp_path / "longbridge.env"
        env_file.write_text("\n".join(f"{key}={value}" for key, value in credentials.items()))
        monkeypatch.setenv("OM_ENV_FILE", str(env_file))
        for key in CREDENTIALS:
            monkeypatch.setenv(key, "older-process-credential")
    elif rotated:
        monkeypatch.setenv("OM_LB_ACCESS_TOKEN", "rotated")
    result = execute_tool("healthcheck", {"config_path": str(path)})
    assert result["ok"], result
    primary = next(row for row in result["data"]["checks"] if row["name"] == "account_primary_paths")
    assert primary["status"] == ("error" if rotated else "ok")
    assert "futu.account_id" not in primary["message"]
    binding = primary["value"]["lb"]
    assert binding["credential_binding_valid"] is (not rotated)
    assert binding["broker_echoed"] is False
    # A valid offline binding is not evidence that the asset endpoint is usable.
    readiness = next(row for row in result["data"]["checks"] if row["name"] == "longbridge_asset_readiness_lb")
    assert readiness["status"] == "skipped"
    assert result["data"]["account_paths"]["lb"]["primary"]["ok"] is False


@pytest.mark.parametrize("token", [CREDENTIALS["OM_LB_ACCESS_TOKEN"], "rotated", ""])
def test_cached_context_revalidates_current_env_file_over_process_credentials(tmp_path, monkeypatch, token):
    import src.application.portfolio_context_service as service

    env_file = tmp_path / "longbridge.env"
    credentials = {**CREDENTIALS, "OM_LB_ACCESS_TOKEN": token}
    env_file.write_text("\n".join(f"{key}={value}" for key, value in credentials.items()))
    monkeypatch.setenv("OM_ENV_FILE", str(env_file))
    monkeypatch.setenv("OM_SECRET_BACKEND", "auto")
    # The process retains the old, previously bound token throughout file rotation.
    ctx = observation()
    calls = []
    monkeypatch.setattr(service, "resolve_broker_portfolio_provider",
        lambda *args, **kwargs: lambda **params: calls.append(params) or {})
    result = load_account_portfolio_context(
        market="长桥", account="lb", state_dir=tmp_path, log=lambda _: None,
        runtime_config=config(), portfolio_source="auto", write_cache=False,
        required_position_asset_types=("stock",), now_utc=NOW,
        fetch_futu_portfolio_context_fn=lambda **kwargs: pytest.fail("Futu was called"),
        load_json_fn=lambda _: ctx,
    )
    unchanged = token == CREDENTIALS["OM_LB_ACCESS_TOKEN"]
    assert result["cash_snapshot"]["status"] == ("fresh" if unchanged else "unknown")
    assert result["context_source"] == ("account_cache" if unchanged else "longbridge_direct")
    assert len(calls) == (0 if unchanged else 1)
    assert ctx == observation()  # Cache validation never rewrites the input evidence.
