from __future__ import annotations

import json
import plistlib
from datetime import date
from pathlib import Path

import pytest

from src.application.agent_tool_contracts import AgentToolError


def _service_accounts(content: str, *, target: str) -> list[str]:
    if target == "launchd":
        argv = plistlib.loads(content.encode("utf-8"))["ProgramArguments"]
    else:
        argv = next(line for line in content.splitlines() if line.startswith("ExecStart=")).split()
    index = argv.index("--accounts") + 1
    out: list[str] = []
    for item in argv[index:]:
        if item.startswith("--"):
            break
        out.append(item)
    return out


@pytest.mark.parametrize("target", ["systemd", "launchd"])
def test_service_units_intersect_accounts_with_each_market_config(tmp_path: Path, target: str) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    repo.mkdir()
    us_config = tmp_path / "config.us.json"
    hk_config = tmp_path / "config.hk.json"
    us_config.write_text(json.dumps({"accounts": ["lx", "sy"]}), encoding="utf-8")
    hk_config.write_text(json.dumps({"accounts": ["lx"]}), encoding="utf-8")

    bundle = render_service_bundle(
        target=target,
        repo_root=repo,
        runtime_root=tmp_path / "runtime",
        accounts=["lx", "sy"],
        markets=["us", "hk"],
        config_paths={"us": us_config, "hk": hk_config},
    )
    files = {item["relative_path"]: item["content"] for item in bundle["files"]}
    if target == "systemd":
        tick_us = files["systemd/options-monitor-tick-us.service"]
        tick_hk = files["systemd/options-monitor-tick-hk.service"]
        close_hk = files["systemd/options-monitor-auto-close-hk.service"]
    else:
        tick_us = files["launchd/com.options-monitor.tick-us.plist"]
        tick_hk = files["launchd/com.options-monitor.tick-hk.plist"]
        close_hk = files["launchd/com.options-monitor.auto-close-hk.plist"]

    assert _service_accounts(tick_us, target=target) == ["lx", "sy"]
    assert _service_accounts(tick_hk, target=target) == ["lx"]
    assert _service_accounts(close_hk, target=target) == ["lx"]


def test_service_render_rejects_empty_market_account_intersection(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    repo.mkdir()
    us_config = tmp_path / "config.us.json"
    hk_config = tmp_path / "config.hk.json"
    us_config.write_text(json.dumps({"accounts": ["lx", "sy"]}), encoding="utf-8")
    hk_config.write_text(json.dumps({"accounts": ["lx"]}), encoding="utf-8")

    with pytest.raises(ValueError, match="empty for markets: hk"):
        render_service_bundle(
            target="systemd",
            repo_root=repo,
            runtime_root=tmp_path / "runtime",
            accounts=["sy"],
            markets=["us", "hk"],
            config_paths={"us": us_config, "hk": hk_config},
        )


def test_service_render_rejects_empty_runtime_account_config(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = tmp_path / "config.hk.json"
    config_path.write_text('{"accounts":[]}', encoding="utf-8")

    with pytest.raises(ValueError, match="accounts must contain at least one"):
        render_service_bundle(
            target="systemd",
            repo_root=repo,
            runtime_root=tmp_path / "runtime",
            accounts=["lx"],
            markets=["hk"],
            config_paths={"hk": config_path},
        )


def test_tick_rejects_unconfigured_account_before_run_artifacts(monkeypatch, tmp_path: Path) -> None:
    from types import SimpleNamespace
    from src.application import multi_account_tick as mod

    config_path = tmp_path / "config.hk.json"
    config_path.write_text('{"accounts":["lx"],"symbols":[]}', encoding="utf-8")
    monkeypatch.setattr(mod, "resolve_runtime_root", lambda **_kwargs: SimpleNamespace(runtime_root=tmp_path, source="test"))
    monkeypatch.setattr(mod, "resolve_config_contract", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(mod, "ensure_runtime_canonical_config", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(mod, "ensure_runtime_config_identity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(mod, "ensure_runtime_schedule_matches_market", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(mod, "RunLogger", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("run logger must not start")))

    with pytest.raises(SystemExit, match="accounts are not configured: sy"):
        mod.main(["--config", str(config_path), "--accounts", "sy"])
    assert not (tmp_path / "output_runs").exists()


def test_auto_close_rejects_unconfigured_account_before_run_artifacts(monkeypatch, tmp_path: Path) -> None:
    from src.application.positions import auto_close as mod

    base = tmp_path / "repo"
    base.mkdir()
    config_path = base / "config.hk.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(mod, "load_config", lambda **_kwargs: {"accounts": ["lx"], "portfolio": {}})
    monkeypatch.setattr(mod, "RunLogger", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("run logger must not start")))

    with pytest.raises(SystemExit, match="accounts are not configured: sy"):
        mod.run_auto_close_expired(
            base=base,
            config_path=config_path,
            data_config=None,
            accounts=["sy"],
            broker=None,
            apply_mode=True,
            no_send=True,
        )
    assert not (base / "output_runs").exists()


def test_assistant_parsers_use_configured_account_labels() -> None:
    from src.application.assistant.command_parser import parse_assistant_command
    from src.application.assistant.position_query import PositionQuery, parse_position_query_text

    positions = parse_assistant_command(
        "/positions christina",
        now_fn=lambda: date(2026, 8, 21),
        accounts=["christina"],
    )
    income = parse_assistant_command(
        "/income christina ytd",
        now_fn=lambda: date(2026, 8, 21),
        accounts=["christina"],
    )
    monitor_run = parse_assistant_command(
        "/monitor-run hk christina",
        now_fn=lambda: date(2026, 8, 21),
        accounts=["christina"],
    )

    assert positions is not None and positions.arguments["account"] == "christina"
    assert income is not None and income.arguments == {"account": "christina", "period": "ytd"}
    assert monitor_run is not None and monitor_run.arguments == {"market": "hk", "accounts": ["christina"]}
    assert parse_position_query_text(
        "christina 持仓",
        today=date(2026, 8, 21),
        accounts=["christina"],
    ).account == "christina"
    hyphen_account = parse_position_query_text(
        "ops-team 持仓",
        today=date(2026, 8, 21),
        accounts=["ops", "ops-team"],
    )
    assert hyphen_account.account == "ops-team"
    assert hyphen_account.symbol is None
    assert PositionQuery.from_payload({"account": "christina"}).account == "christina"


def test_inbound_positions_preserves_runtime_config_account(monkeypatch, tmp_path: Path) -> None:
    from src.application.agent_tool_contracts import build_response
    from src.application.assistant.contracts import AssistantRequest
    from src.application.assistant.inbound_service import handle_assistant_request

    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "src.application.assistant.inbound_service.load_runtime_config",
        lambda **_kwargs: (tmp_path / "config.us.json", {"accounts": ["christina"]}),
    )

    def execute(tool_name: str, payload: dict) -> dict:
        calls.append((tool_name, payload))
        return build_response(tool_name=tool_name, ok=True, data={})

    response = handle_assistant_request(
        AssistantRequest(
            text="/positions christina",
            sender_id="local",
            message_id="custom-account-position",
            config_key="us",
            audit_db=str(tmp_path / "inbound.sqlite3"),
        ),
        execute_tool_fn=execute,
    )

    assert response["ok"] is True
    assert calls == [("option_positions_read", {"config_key": "us", "action": "list", "query": {"account": "christina", "status": "open", "limit": 50}})]


def test_inbound_monitor_run_uses_target_market_accounts(monkeypatch, tmp_path: Path) -> None:
    from src.application.assistant.contracts import AssistantRequest
    from src.application.assistant.inbound_service import handle_assistant_request

    monkeypatch.setenv("OM_INBOUND_OPERATIONS_ENABLED", "1")
    monkeypatch.setenv("OM_INBOUND_MONITOR_RUN_ENABLED", "1")
    monkeypatch.setenv("OM_INBOUND_ADMIN_OPEN_IDS", "feishu:ou_1")
    monkeypatch.setenv("OM_INBOUND_OPERATION_HMAC_KEY", "test-operation-hmac-key")
    for market, accounts in (("us", ["sy"]), ("hk", ["christina"])):
        (tmp_path / f"config.{market}.json").write_text(
            json.dumps(
                {
                    "_generated": {
                        "schema_version": "1.0",
                        "generator": "options-monitor",
                        "source_format": "yaml",
                        "market": market,
                    },
                    "_resolved": {
                        "source_format": "yaml",
                        "market": market,
                        "runtime_schema": "config-json-v1",
                    },
                    "accounts": accounts,
                    "symbols": [],
                }
            ),
            encoding="utf-8",
        )

    response = handle_assistant_request(
        AssistantRequest(
            text="/monitor-run hk christina",
            sender_id="ou_1",
            channel="feishu",
            message_id="custom-account-monitor-run",
            config_path=str(tmp_path / "config.us.json"),
            audit_db=str(tmp_path / "inbound.sqlite3"),
        ),
        allowed_senders="feishu:ou_1",
    )

    assert response["ok"] is True
    assert response["data"]["payload"]["arguments"]["accounts"] == ["christina"]


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "list", "query": {"account": "sy"}},
        {"action": "list", "account": "sy", "query": {"account": ""}},
    ],
)
def test_position_read_rejects_unknown_account_before_opening_ledger(
    tmp_path: Path,
    payload: dict,
) -> None:
    from src.application.agent_tools.operations_impl import option_positions_read_tool

    def unexpected(*_args, **_kwargs):
        raise AssertionError("ledger must not be opened")

    with pytest.raises(AgentToolError, match="accounts are not configured: sy"):
        option_positions_read_tool(
            payload,
            load_runtime_config=lambda **_kwargs: (tmp_path / "config.us.json", {"accounts": ["christina"]}),
            resolve_public_data_config_path=unexpected,
            normalize_broker=unexpected,
            normalize_account=lambda value: str(value).strip().lower(),
            refresh_assigned_stock_quotes=unexpected,
            resolve_option_positions_repo=unexpected,
            list_position_rows=unexpected,
            build_lot_event_history=unexpected,
            inspect_projection_state=unexpected,
            repo_base=lambda: tmp_path,
            mask_path=str,
        )


@pytest.mark.parametrize("dry_run", [True, False])
def test_config_init_rejects_same_account_roles_before_writes(tmp_path: Path, dry_run: bool) -> None:
    from src.application.config_yaml_init import init_yaml_config

    output = tmp_path / "config.yaml"
    runtime = tmp_path / "runtime"
    with pytest.raises(AgentToolError, match="must use different labels") as exc_info:
        init_yaml_config(
            repo_root=Path(__file__).resolve().parents[1],
            output_config_yaml_path=output,
            runtime_output_dir=runtime,
            account_label="lx",
            external_holdings_account="LX",
            dry_run=dry_run,
        )
    assert exc_info.value.code == "INPUT_ERROR"
    assert not output.exists()
    assert not runtime.exists()
