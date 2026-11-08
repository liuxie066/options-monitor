from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tools import diagnostics, healthcheck_impl
from src.application.tool_execution import execute_tool
from test_agent_plugin_smoke import (
    _patch_healthcheck_dependencies,
    _public_cfg_with_futu,
    _write_healthcheck_config,
)


@pytest.fixture
def healthcheck_payload(monkeypatch, tmp_path: Path) -> dict[str, Any]:
    cfg = _public_cfg_with_futu(str(tmp_path / "portfolio.runtime.json"))
    cfg["accounts"] = ["user1", "user2"]
    cfg["account_settings"]["user1"]["futu"]["trd_env"] = "REAL"
    cfg["account_settings"]["user2"] = deepcopy(cfg["account_settings"]["user1"])
    cfg["account_settings"]["user2"]["futu"]["account_id"] = "999000000000000002"
    cfg["account_settings"]["user2"]["futu"]["port"] = 11112
    cfg["trade_intake"]["account_mapping"]["futu"]["999000000000000002"] = "user2"
    config_path = _write_healthcheck_config(
        tmp_path,
        cfg=cfg,
        data_config={
            "option_positions": {"sqlite_path": str(tmp_path / "ledger.sqlite3")},
        },
    )
    build_environment = healthcheck_impl.build_effective_env_with_status
    monkeypatch.setattr(
        healthcheck_impl,
        "build_effective_env_with_status",
        lambda **kwargs: build_environment(repo_root=tmp_path, environ={}, **kwargs),
    )
    monkeypatch.setattr(healthcheck_impl, "repo_base", lambda: tmp_path)
    monkeypatch.setattr(healthcheck_impl.plugin_runtime, "plugin_installed", lambda _key: True)
    _patch_healthcheck_dependencies(monkeypatch)
    return {"config_path": str(config_path), "audit_db": str(tmp_path / "audit.sqlite3")}


def test_unknown_account_returns_input_error(healthcheck_payload) -> None:
    out = execute_tool("healthcheck", {**healthcheck_payload, "accounts": ["unknown"]})

    assert out["ok"] is False
    assert out["error"]["code"] == "INPUT_ERROR", out["error"]
    assert out["error"]["message"] == "accounts are not configured: unknown"


@pytest.mark.parametrize("accounts", [["unknown"], ["user1", "unknown"], [" UNKNOWN "]])
def test_unknown_account_stops_before_dependency_probes(
    monkeypatch, healthcheck_payload, accounts
) -> None:
    calls: dict[str, int] = {}

    def forbid(name):
        calls[name] = 0

        def probe(*_args, **_kwargs):
            calls[name] += 1
            raise AssertionError(f"unexpected dependency probe: {name}")

        return probe

    for owner, names in (
        (healthcheck_impl, (
            "build_effective_env_with_status",
            "build_channel_status",
            "ledger_store_payload",
            "build_wheel_activation_readiness",
            "_feishu_inbound_check",
            "_feishu_ws_service_check",
        )),
        (healthcheck_impl.plugin_runtime, ("plugin_installed",)),
        (diagnostics, (
            "read_json_object_or_empty",
            "list_account_config_views",
            "run_futu_doctor",
            "build_ready_futu_broker_gateway",
        )),
    ):
        for name in names:
            monkeypatch.setattr(owner, name, forbid(name))

    out = execute_tool("healthcheck", {**healthcheck_payload, "accounts": accounts})

    assert calls == dict.fromkeys(calls, 0), calls
    assert out["ok"] is False
    assert out["error"]["code"] == "INPUT_ERROR", out["error"]
    assert out["error"]["message"] == "accounts are not configured: unknown"


@pytest.mark.parametrize(
    ("selection", "expected"),
    [
        ({}, ["user1", "user2"]),
        ({"accounts": []}, ["user1", "user2"]),
        ({"accounts": [" USER2 ", "user2"]}, ["user2"]),
    ],
    ids=["default", "empty-default", "normalized-configured-account"],
)
def test_configured_account_selection_keeps_existing_behavior(
    healthcheck_payload, selection, expected
) -> None:
    out = execute_tool("healthcheck", {**healthcheck_payload, **selection})

    assert out["ok"] is True, out.get("error")
    assert out["data"]["config"]["accounts"] == expected
    assert list(out["data"]["account_paths"]) == expected
    for account in expected:
        primary = out["data"]["account_paths"][account]["primary"]
        assert primary["source"] == "futu"
        assert primary["ok"] is True
    assert out["data"]["summary"]["ok"] is True


def test_implicit_default_account_keeps_existing_behavior(
    healthcheck_payload, tmp_path: Path
) -> None:
    cfg = _public_cfg_with_futu(str(tmp_path / "portfolio.runtime.json"))
    cfg.pop("accounts")
    _write_healthcheck_config(
        tmp_path,
        cfg=cfg,
        data_config={
            "option_positions": {"sqlite_path": str(tmp_path / "ledger.sqlite3")},
        },
    )

    out = execute_tool("healthcheck", healthcheck_payload)

    assert out["ok"] is True, out.get("error")
    assert out["data"]["config"]["accounts"] == ["user1"]
    assert list(out["data"]["account_paths"]) == ["user1"]
    assert out["data"]["account_paths"]["user1"]["primary"]["ok"] is True
    assert out["data"]["summary"]["ok"] is True


@pytest.mark.parametrize(
    ("failure", "code", "message"),
    [
        (ValueError("quote dependency failed"), "INTERNAL_ERROR", "ValueError: quote dependency failed"),
        (
            AgentToolError(code="DEPENDENCY_ERROR", message="quote dependency unavailable"),
            "DEPENDENCY_ERROR",
            "quote dependency unavailable",
        ),
    ],
    ids=["unclassified-value-error", "classified-dependency-error"],
)
def test_valid_account_dependency_error_keeps_existing_classification(
    monkeypatch, healthcheck_payload, failure, code, message
) -> None:
    calls = 0

    def fail_quote(**_kwargs):
        nonlocal calls
        calls += 1
        raise failure

    monkeypatch.setattr(diagnostics, "run_futu_doctor", fail_quote)
    out = execute_tool("healthcheck", {**healthcheck_payload, "accounts": ["user2"]})

    assert calls == 1
    assert out["ok"] is False
    assert out["error"]["code"] == code
    assert out["error"]["message"] == message


def test_valid_account_quote_readiness_failure_remains_diagnostic(
    monkeypatch, healthcheck_payload
) -> None:
    monkeypatch.setattr(
        diagnostics,
        "run_futu_doctor",
        lambda **_kwargs: {"ok": False, "message": "quote unavailable"},
    )
    out = execute_tool("healthcheck", {**healthcheck_payload, "accounts": ["user2"]})

    assert out["ok"] is True, out.get("error")
    assert out["data"]["account_paths"]["user2"]["primary"]["ok"] is True
    assert out["data"]["summary"]["ok"] is False
    quote_check = next(
        item for item in out["data"]["checks"]
        if item["name"].startswith("opend_quote_readiness_")
    )
    assert quote_check["status"] == "error"
    assert quote_check["message"] == "quote unavailable"
