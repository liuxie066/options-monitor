"""Public diagnostics must distinguish optional brokers from required market data."""
from copy import deepcopy

import pytest

from src.application import plugin_runtime
from src.application.tool_execution import execute_tool
from test_agent_plugin_smoke import (
    _patch_healthcheck_dependencies,
    _public_cfg_with_futu,
    _write_healthcheck_config,
)


@pytest.mark.parametrize("broker", ["futu", "longbridge"])
def test_healthcheck_without_broker_keeps_quote_readiness(monkeypatch, tmp_path, broker):
    cfg = deepcopy(_public_cfg_with_futu("portfolio.runtime.json"))
    if broker == "longbridge":
        cfg["portfolio"]["source"] = "auto"
        cfg["account_settings"]["user1"] = {
            "type": "longbridge", "longbridge": {
                "account_id": "LB123", "credentials_env_prefix": "OM_LB",
                "region": "ap", "trd_env": "REAL",
            },
        }
        cfg["trade_intake"] = {"enabled": False}
    path = _write_healthcheck_config(tmp_path, cfg=cfg)
    monkeypatch.setattr(plugin_runtime, "plugin_installed", lambda key: not key.startswith("broker-"), raising=False)
    calls = []

    def quote(**kwargs):
        calls.append(kwargs["required_capability"])
        return {"ok": True}

    def forbidden(**kwargs):
        pytest.fail("optional missing broker was probed")

    _patch_healthcheck_dependencies(monkeypatch, run_futu_doctor=quote,
                                   build_ready_futu_broker_gateway=forbidden)
    result = execute_tool("healthcheck", {"config_path": str(path)})
    assert result["ok"] is True
    assert calls == ["quote"]
    assert result["data"]["summary"]["ok"] is True
    primary = result["data"]["account_paths"]["user1"]["primary"]
    assert primary["ok"] is False
    assert primary["plugin_id"] == "broker-" + broker
    assert primary["status"] == "plugin_not_installed"
    check = next(row for row in result["data"]["checks"] if row["name"] == "broker_plugin_user1")
    assert check["status"] == "skipped"
    assert check["value"]["ready"] is False


def test_healthcheck_missing_market_never_probes_quote(monkeypatch, tmp_path):
    path = _write_healthcheck_config(tmp_path)
    monkeypatch.setattr(plugin_runtime, "plugin_installed", lambda key: key != "market-opend", raising=False)
    calls = []

    def quote(**kwargs):
        calls.append(kwargs)
        return {"ok": True}

    _patch_healthcheck_dependencies(monkeypatch, run_futu_doctor=quote)
    result = execute_tool("healthcheck", {"config_path": str(path)})
    assert calls == []
    assert result["data"]["summary"]["ok"] is False
    check = next(row for row in result["data"]["checks"] if row["name"] == "market_plugin")
    assert check["status"] == "error"
    assert "market-opend" in check["message"]
