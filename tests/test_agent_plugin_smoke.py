from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pandas as pd

import src.application.ledger.manual_trades as ledger_manual_trades
import src.application.ledger.repository as ledger_repository

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def _minimal_cfg(*, market: str = "us") -> dict[str, Any]:
    return {
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
        "accounts": ["user1"],
        "portfolio": {
            "broker": "富途",
            "source": "futu",
        },
        "templates": {
            "put_base": {
                "sell_put": {
                    "min_annualized_net_return": 0.1,
                    "min_net_income": 50,
                    "min_open_interest": 10,
                    "min_volume": 1,
                    "max_spread_ratio": 0.3,
                }
            }
        },
        "symbols": [
            {
                "symbol": "NVDA",
                "market": "US",
                "fetch": {"source": "futu", "limit_expirations": 8},
                "use": ["put_base"],
                "sell_put": {
                    "enabled": True,
                    "min_dte": 20,
                    "max_dte": 45,
                    "min_strike": 100,
                    "max_strike": 120,
                },
                "sell_call": {"enabled": False},
            }
        ],
    }


def _public_cfg_with_futu(data_config_ref: str, *, market: str = "us") -> dict[str, Any]:
    cfg = _minimal_cfg(market=market)
    cfg["account_settings"] = {
        "user1": {
            "type": "futu",
        }
    }
    cfg["portfolio"]["account"] = "user1"
    cfg["portfolio"]["source_by_account"] = {"user1": "futu"}
    cfg["portfolio"]["data_config"] = data_config_ref
    cfg["trade_intake"] = {
        "enabled": True,
        "mode": "dry-run",
        "account_mapping": {
            "futu": {
                "281756479859383816": "user1",
            }
        },
    }
    cfg["symbols"][0]["fetch"] = {
        "source": "futu",
        "host": "127.0.0.1",
        "port": 11111,
        "limit_expirations": 8,
    }
    return cfg


def _public_cfg_with_futu_auto_source(data_config_ref: str, *, market: str = "us") -> dict[str, Any]:
    cfg = _public_cfg_with_futu(data_config_ref, market=market)
    cfg["account_settings"]["user1"]["holdings_account"] = "lx"
    cfg["portfolio"]["source"] = "auto"
    cfg["portfolio"]["source_by_account"]["user1"] = "auto"
    return cfg


def _public_cfg_with_external_holdings(data_config_ref: str, *, market: str = "us") -> dict[str, Any]:
    cfg = _public_cfg_with_futu(data_config_ref, market=market)
    cfg["accounts"] = ["user1", "ext1"]
    cfg["account_settings"]["ext1"] = {
        "type": "external_holdings",
        "holdings_account": "Feishu EXT",
    }
    cfg["portfolio"]["source_by_account"]["ext1"] = "holdings"
    return cfg


def _write_healthcheck_config(tmp_path: Path) -> Path:
    cfg_path = tmp_path / "config.us.json"
    data_cfg_path = tmp_path / "portfolio.runtime.json"
    data_cfg_path.write_text(
        json.dumps({"option_positions": {"sqlite_path": "output_shared/state/option_positions.sqlite3"}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cfg_path.write_text(
        json.dumps(_public_cfg_with_futu("portfolio.runtime.json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return cfg_path


def _futu_doctor_ok(**kwargs: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "sdk": {"ok": True},
        "watchdog": {"ok": True},
    }


def _patch_agent_tool_context(monkeypatch, **overrides: Any) -> None:
    from dataclasses import replace

    import src.application.tool_execution as tool_execution

    ctx = tool_execution.build_default_agent_tool_context()
    monkeypatch.setattr(
        tool_execution,
        "build_default_agent_tool_context",
        lambda: replace(ctx, **overrides),
    )


def _patch_healthcheck_context(monkeypatch, **overrides: Any) -> None:
    deps = {"run_futu_doctor": _futu_doctor_ok}
    deps.update(overrides)
    _patch_agent_tool_context(monkeypatch, **deps)


def test_healthcheck_works_with_explicit_config_path(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    data_cfg_path = tmp_path / "portfolio.runtime.json"
    data_cfg_path.write_text(
        json.dumps({"option_positions": {"sqlite_path": "output_shared/state/option_positions.sqlite3"}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cfg_path.write_text(
        json.dumps(_public_cfg_with_futu("portfolio.runtime.json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    _patch_healthcheck_context(monkeypatch)

    out = run_tool("healthcheck", {"config_path": str(cfg_path)})

    assert out["ok"] is True
    assert out["data"]["config"]["accounts"] == ["user1"]
    assert out["data"]["account_paths"]["user1"]["primary"]["source"] == "futu"
    assert out["data"]["account_paths"]["user1"]["primary"]["ok"] is True
    assert "fallback" not in out["data"]["account_paths"]["user1"]
    assert out["meta"]["config_path"] == ".../config.us.json"
    assert "runtime_runs" in out["data"]["tools"]
    assert "candidate_filter_explain" in out["data"]["tools"]
    assert "research" not in out["data"]["tools"]
    assert out["data"]["side_lanes"]["research_shadow_replay"]["agent_tool"] is False
    assert any(item["name"] == "opend_readiness" and item["status"] == "ok" for item in out["data"]["checks"])
    assert any(item["name"] == "account_mapping" and item["status"] == "ok" for item in out["data"]["checks"])
    primary = next(item for item in out["data"]["checks"] if item["name"] == "account_primary_paths")
    assert primary["status"] == "ok"
    assert primary["value"]["user1"]["source"] == "futu"
    assert any(item["name"] == "starter_symbols" and item["status"] == "warn" for item in out["data"]["checks"])
    assert any("starter account label 'user1'" in item for item in out["warnings"])


def test_healthcheck_does_not_warn_when_production_watchlist_contains_starter_symbol(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = _write_healthcheck_config(tmp_path)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    msft = dict(cfg["symbols"][0])
    msft["symbol"] = "MSFT"
    cfg["symbols"].append(msft)
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    _patch_healthcheck_context(monkeypatch)

    out = run_tool("healthcheck", {"config_path": str(cfg_path)})

    assert out["ok"] is True
    assert all(item["name"] != "starter_symbols" for item in out["data"]["checks"])
    assert not any("Replace example starter symbols" in item for item in out["warnings"])


def test_healthcheck_reports_candidate_evidence_diagnostic(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = _write_healthcheck_config(tmp_path)
    candidate_path = tmp_path / "sell_put_candidates.csv"
    candidate_path.write_text("symbol,dte\nNVDA,30\nMSFT,31\n", encoding="utf-8")
    trace_path = tmp_path / "candidate_filter_trace.jsonl"
    trace_path.write_text('{"symbol":"NVDA","result":"accepted"}\n', encoding="utf-8")

    _patch_healthcheck_context(monkeypatch)

    out = run_tool(
        "healthcheck",
        {
            "config_path": str(cfg_path),
            "candidate_paths": [str(candidate_path)],
            "candidate_trace_paths": [str(trace_path)],
            "candidate_evidence_min_sample": 2,
        },
    )

    assert out["ok"] is True
    check = next(item for item in out["data"]["checks"] if item["name"] == "candidate_evidence")
    assert check["status"] == "ok"
    assert check["value"]["evaluable"] is True
    assert check["value"]["row_counts"]["candidates"] == 2
    assert check["value"]["row_counts"]["traces"] == 1


def test_healthcheck_reports_feishu_inbound_audit_ready(monkeypatch, tmp_path: Path) -> None:
    from src.application.assistant.audit import InboundAuditStore
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = _write_healthcheck_config(tmp_path)
    audit_db = tmp_path / "inbound.sqlite3"
    InboundAuditStore(audit_db).record_result(
        {
            "command_id": "in_healthcheck_ready",
            "channel": "feishu",
            "sender_id": "ou_1",
            "conversation_id": "feishu:chat_1:ou_1",
            "message_id": "omsg_1",
            "raw_text": "状态",
            "parser": "deterministic",
            "intent_name": "runtime_status",
            "tool_name": "runtime_status",
            "decision": "allowed",
            "result_ok": True,
            "response": {"data": {"response_text": "ok"}},
        }
    )
    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "cli_1")
    monkeypatch.setenv("OM_FEISHU_BOT_APP_SECRET", "secret_1")
    monkeypatch.setenv("OM_FEISHU_BOT_ALLOWED_OPEN_IDS", "ou_1")
    _patch_healthcheck_context(monkeypatch)

    out = run_tool("healthcheck", {"config_path": str(cfg_path), "audit_db": str(audit_db)})
    checks = {item["name"]: item for item in out["data"]["checks"]}

    assert out["ok"] is True
    assert checks["feishu_inbound"]["status"] == "ok"
    assert checks["feishu_inbound"]["value"]["latest_event"]["sender_id"] == "ou_1"
    assert checks["feishu_inbound"]["value"]["latest_event"]["conversation_id"] == "feishu:chat_1:ou_1"
    assert checks["feishu_inbound"]["value"]["pending_store"]["readable"] is True


def test_healthcheck_uses_explicit_env_file_for_feishu_inbound(monkeypatch, tmp_path: Path) -> None:
    from src.application.assistant.audit import InboundAuditStore
    from src.application.tool_execution import execute_tool as run_tool

    for name in (
        "OM_ENV_FILE",
        "OM_FEISHU_BOT_APP_ID",
        "OM_FEISHU_BOT_APP_SECRET",
        "OM_FEISHU_BOT_ALLOWED_OPEN_IDS",
        "OM_INBOUND_AUDIT_DB",
    ):
        monkeypatch.delenv(name, raising=False)
    cfg_path = _write_healthcheck_config(tmp_path)
    audit_db = tmp_path / "inbound.sqlite3"
    InboundAuditStore(audit_db).record_result(
        {
            "command_id": "in_healthcheck_env_file",
            "channel": "feishu",
            "sender_id": "ou_file",
            "conversation_id": "feishu:chat_file:ou_file",
            "message_id": "omsg_file",
            "raw_text": "状态",
            "parser": "deterministic",
            "intent_name": "runtime_status",
            "tool_name": "runtime_status",
            "decision": "allowed",
            "result_ok": True,
            "response": {"data": {"response_text": "ok"}},
        }
    )
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text(
        "\n".join(
            [
                "OM_FEISHU_BOT_APP_ID=cli_file",
                "OM_FEISHU_BOT_APP_SECRET=secret_file",
                "OM_FEISHU_BOT_ALLOWED_OPEN_IDS=ou_file",
                f"OM_INBOUND_AUDIT_DB={audit_db}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _patch_healthcheck_context(monkeypatch)

    out = run_tool(
        "healthcheck",
        {"config_path": str(cfg_path), "env_file": str(env_file)},
    )
    checks = {item["name"]: item for item in out["data"]["checks"]}
    env = out["data"]["environment"]

    assert checks["feishu_inbound"]["status"] == "ok"
    assert checks["feishu_inbound"]["value"]["audit_db_exists"] is True
    assert checks["feishu_inbound"]["value"]["latest_event"]["sender_id"] == "ou_file"
    assert checks["feishu_inbound"]["value"]["credentials_configured"] is True
    assert checks["feishu_inbound"]["value"]["allowed_open_ids_count"] == 1
    assert env["env_file"] == ".../options-monitor.env"
    assert env["env_file_loaded"] is True
    assert env["entries"]["OM_FEISHU_BOT_APP_ID"]["source"] == "env_file:.../options-monitor.env"
    assert env["entries"]["OM_INBOUND_AUDIT_DB"]["source"] == "env_file:.../options-monitor.env"


def test_healthcheck_warns_when_feishu_latest_sender_not_allowed(monkeypatch, tmp_path: Path) -> None:
    from src.application.assistant.audit import InboundAuditStore
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = _write_healthcheck_config(tmp_path)
    audit_db = tmp_path / "inbound.sqlite3"
    InboundAuditStore(audit_db).record_result(
        {
            "command_id": "in_healthcheck_denied_sender",
            "channel": "feishu",
            "sender_id": "ou_1",
            "conversation_id": "feishu:chat_1:ou_1",
            "message_id": "omsg_1",
            "raw_text": "状态",
            "parser": "deterministic",
            "intent_name": "runtime_status",
            "tool_name": "runtime_status",
            "decision": "allowed",
            "result_ok": True,
            "response": {"data": {"response_text": "ok"}},
        }
    )
    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "cli_1")
    monkeypatch.setenv("OM_FEISHU_BOT_APP_SECRET", "secret_1")
    monkeypatch.setenv("OM_FEISHU_BOT_ALLOWED_OPEN_IDS", "ou_2")
    _patch_healthcheck_context(monkeypatch)

    out = run_tool("healthcheck", {"config_path": str(cfg_path), "audit_db": str(audit_db)})
    check = next(item for item in out["data"]["checks"] if item["name"] == "feishu_inbound")

    assert out["ok"] is True
    assert check["status"] == "warn"
    assert "OM_FEISHU_BOT_ALLOWED_OPEN_IDS" in check["message"]


def test_healthcheck_rejects_placeholder_futu_mapping(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (tmp_path / "portfolio.runtime.json").write_text(
        json.dumps({"option_positions": {"sqlite_path": "output_shared/state/option_positions.sqlite3"}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cfg = _public_cfg_with_futu("portfolio.runtime.json")
    cfg["trade_intake"]["account_mapping"]["futu"] = {"REAL_12345678": "user1"}
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    _patch_healthcheck_context(monkeypatch)

    out = run_tool("healthcheck", {"config_path": str(cfg_path)})

    assert out["ok"] is True
    assert out["data"]["summary"]["ok"] is False
    assert out["data"]["account_paths"]["user1"]["primary"]["ok"] is False
    check = next(item for item in out["data"]["checks"] if item["name"] == "account_primary_paths")
    assert check["status"] == "error"
    assert "placeholder futu acc_id" in check["message"]


def test_healthcheck_accepts_futu_auto_source_without_fallback_checks(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (tmp_path / "portfolio.runtime.json").write_text(
        json.dumps({"option_positions": {"sqlite_path": "output_shared/state/option_positions.sqlite3"}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cfg_path.write_text(
        json.dumps(_public_cfg_with_futu_auto_source("portfolio.runtime.json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    _patch_healthcheck_context(monkeypatch)

    out = run_tool("healthcheck", {"config_path": str(cfg_path)})

    assert out["ok"] is True
    assert out["data"]["summary"]["ok"] is True
    assert out["data"]["account_paths"]["user1"]["primary"]["ok"] is True
    assert "fallback" not in out["data"]["account_paths"]["user1"]
    primary = next(item for item in out["data"]["checks"] if item["name"] == "account_primary_paths")
    assert primary["status"] == "ok"
    assert all(item["name"] != "account_fallback_paths" for item in out["data"]["checks"])
    assert not any("holdings fallback configured" in item for item in out["warnings"])


def test_healthcheck_rejects_account_settings_acc_id_missing_from_trade_intake_mapping(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (tmp_path / "portfolio.runtime.json").write_text(
        json.dumps({"option_positions": {"sqlite_path": "output_shared/state/option_positions.sqlite3"}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cfg = _public_cfg_with_futu("portfolio.runtime.json")
    cfg["account_settings"]["user1"]["futu"] = {"account_id": "999999999999999999"}
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    _patch_healthcheck_context(monkeypatch)

    out = run_tool("healthcheck", {"config_path": str(cfg_path)})

    assert out["ok"] is True
    assert out["data"]["summary"]["ok"] is False
    primary = next(item for item in out["data"]["checks"] if item["name"] == "account_primary_paths")
    assert primary["status"] == "error"
    assert "missing from trade_intake.account_mapping.futu" in primary["message"]


def test_healthcheck_accepts_external_holdings_account_without_futu_mapping(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    monkeypatch.setenv("OM_FEISHU_APP_ID", "cli_xxx")
    monkeypatch.setenv("OM_FEISHU_APP_SECRET", "secret_xxx")
    monkeypatch.setenv("OM_FEISHU_HOLDINGS_TABLE", "app_token/table_id")
    (tmp_path / "portfolio.runtime.json").write_text(
        json.dumps(
            {
                "option_positions": {"sqlite_path": "output_shared/state/option_positions.sqlite3"},
                "feishu": {
                    "app_id_env": "OM_FEISHU_APP_ID",
                    "app_secret_env": "OM_FEISHU_APP_SECRET",
                    "tables": {"holdings_env": "OM_FEISHU_HOLDINGS_TABLE"},
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    cfg_path.write_text(
        json.dumps(_public_cfg_with_external_holdings("portfolio.runtime.json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    _patch_healthcheck_context(monkeypatch)

    out = run_tool("healthcheck", {"config_path": str(cfg_path)})

    assert out["ok"] is True
    assert out["data"]["account_paths"]["ext1"]["primary"]["source"] == "external_holdings"
    assert out["data"]["account_paths"]["ext1"]["primary"]["ok"] is True
    assert "fallback" not in out["data"]["account_paths"]["ext1"]
    primary = next(item for item in out["data"]["checks"] if item["name"] == "account_primary_paths")
    assert primary["status"] == "ok"
    assert primary["value"]["ext1"]["type"] == "external_holdings"
    assert primary["value"]["ext1"]["holdings_account"] == "Feishu EXT"
    assert primary["value"]["ext1"]["ready"] is True
    assert all(item["name"] != "account_fallback_paths" for item in out["data"]["checks"])


def test_healthcheck_reports_option_positions_repo_load_degraded(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (tmp_path / "portfolio.runtime.json").write_text(
        json.dumps({"option_positions": {"sqlite_path": "output_shared/state/option_positions.sqlite3"}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cfg_path.write_text(
        json.dumps(_public_cfg_with_futu("portfolio.runtime.json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    class _Repo:
        bootstrap_status = "degraded_option_positions_repo_load_failed"
        bootstrap_message = "option positions repo load failed: sqlite unavailable"

    _patch_healthcheck_context(monkeypatch, load_option_positions_repo=lambda _path: _Repo())

    out = run_tool("healthcheck", {"config_path": str(cfg_path)})

    bootstrap = next(item for item in out["data"]["checks"] if item["name"] == "option_positions_bootstrap")
    assert bootstrap["status"] == "warn"
    assert bootstrap["value"]["status"] == "degraded_option_positions_repo_load_failed"
    assert "sqlite unavailable" in bootstrap["message"]
    assert out["data"]["summary"]["warning_count"] >= 1


def test_healthcheck_reports_option_positions_bootstrap_ok_for_sqlite_only(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (tmp_path / "portfolio.runtime.json").write_text(
        json.dumps({"option_positions": {"sqlite_path": "output_shared/state/option_positions.sqlite3"}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cfg_path.write_text(
        json.dumps(_public_cfg_with_futu("portfolio.runtime.json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    class _Repo:
        bootstrap_status = "sqlite_only_no_feishu_bootstrap"
        bootstrap_message = "feishu option_positions bootstrap is not used; local trade_events remain source of truth"

    _patch_healthcheck_context(monkeypatch, load_option_positions_repo=lambda _path: _Repo())

    out = run_tool("healthcheck", {"config_path": str(cfg_path)})

    bootstrap = next(item for item in out["data"]["checks"] if item["name"] == "option_positions_bootstrap")
    assert bootstrap["status"] == "ok"
    assert bootstrap["value"]["status"] == "sqlite_only_no_feishu_bootstrap"


def test_healthcheck_warns_on_notification_placeholder_values(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    data_cfg_path = tmp_path / "portfolio.runtime.json"
    data_cfg_path.write_text(
        json.dumps({"option_positions": {"sqlite_path": "output_shared/state/option_positions.sqlite3"}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "cli_xxx")
    monkeypatch.setenv("OM_FEISHU_BOT_APP_SECRET", "xxx")
    monkeypatch.setenv("OM_FEISHU_BOT_USER_OPEN_ID", "ou_xxx")
    cfg = _public_cfg_with_futu("portfolio.runtime.json")
    cfg["notifications"] = {
        "provider": "feishu_app",
    }
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    _patch_healthcheck_context(monkeypatch)

    out = run_tool("healthcheck", {"config_path": str(cfg_path)})

    assert out["ok"] is True
    assert any(item["name"] == "notification_target_placeholder" and item["status"] == "warn" for item in out["data"]["checks"])
    assert any(item["name"] == "notification_credentials_placeholder" and item["status"] == "warn" for item in out["data"]["checks"])
    assert any("example Feishu bot user open_id" in item for item in out["warnings"])
    assert any("example Feishu bot credentials" in item for item in out["warnings"])


def test_healthcheck_reports_unified_channel_health(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = _write_healthcheck_config(tmp_path)
    _patch_healthcheck_context(monkeypatch)
    assistant_config = tmp_path / "resolved" / "config.assistant.json"
    assistant_config.parent.mkdir()
    assistant_config.write_text(
        json.dumps(
            {
                "inbound": {
                    "wechat_clawbot": {
                        "label": "ops",
                        "allowed_senders": "wechat:user_1",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    state_dir = tmp_path / "output_shared" / "state" / "channels" / "wechat_clawbot" / "ops"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text(
        json.dumps({"bot_token": "bot_secret_1", "base_url": "https://example.invalid"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (state_dir / "bindings.json").write_text(
        json.dumps(
            {
                "bindings": {
                    "ops": {
                        "to_user_id": "wx_user_1",
                        "context_token": "ctx_secret_1",
                        "last_message_id": "msg_1",
                        "updated_at_utc": "2026-06-18T01:00:00+00:00",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    profile_path = tmp_path / "service.profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "runtime_root": str(tmp_path),
                "assistant_config_path": str(assistant_config),
                "wechat_clawbot": {
                    "enabled": True,
                    "label": "ops",
                    "state_dir": str(state_dir),
                    "assistant_config_path": str(assistant_config),
                    "allowed_senders_configured": True,
                    "allowed_senders_source": "config_yaml",
                },
                "services": [{"name": "options-monitor-wechat-clawbot.service"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    out = run_tool("healthcheck", {"config_path": str(cfg_path), "profile_path": str(profile_path)})

    checks = {item["name"]: item for item in out["data"]["checks"]}
    assert checks["channel_health"]["status"] == "ok"
    assert out["data"]["channel_health"]["wechat_clawbot"]["available"] is True
    assert out["data"]["channel_health"]["wechat_clawbot"]["allowed_senders_configured"] is True
    assert "bot_secret_1" not in json.dumps(out, ensure_ascii=False)


def test_get_portfolio_context_allows_futu_source_without_explicit_data_config(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    cfg = _minimal_cfg()
    cfg["portfolio"]["account"] = "user1"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    def _fake_load_portfolio_context(**kwargs):  # type: ignore[no-untyped-def]
        assert str(kwargs["data_config"]).endswith("portfolio.runtime.json")
        return {
            "portfolio_source_name": "futu",
            "cash_by_currency": {"USD": 1000.0},
            "stocks_by_symbol": {},
        }

    _patch_agent_tool_context(monkeypatch, load_portfolio_context=_fake_load_portfolio_context)
    out = run_tool("get_portfolio_context", {"config_path": str(cfg_path), "account": "user1"})

    assert out["ok"] is True
    assert out["data"]["portfolio_source_name"] == "futu"


def test_get_portfolio_context_rejects_stale_external_holdings_cache_for_wrong_account(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool
    import src.application.pipeline_context as pipeline_context
    import src.application.portfolio_context_service as pcs

    cfg_path = tmp_path / "config.hk.json"
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    monkeypatch.setenv("OM_FEISHU_APP_ID", "cli_xxx")
    monkeypatch.setenv("OM_FEISHU_APP_SECRET", "secret_xxx")
    monkeypatch.setenv("OM_FEISHU_HOLDINGS_TABLE", "app_token/table_id")
    (tmp_path / "portfolio.runtime.json").write_text(
        json.dumps(
            {
                "option_positions": {"sqlite_path": "output_shared/state/option_positions.sqlite3"},
                "feishu": {
                    "app_id_env": "OM_FEISHU_APP_ID",
                    "app_secret_env": "OM_FEISHU_APP_SECRET",
                    "tables": {"holdings_env": "OM_FEISHU_HOLDINGS_TABLE"},
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    cfg = _public_cfg_with_futu("portfolio.runtime.json", market="hk")
    cfg["accounts"] = ["lx", "sy"]
    cfg["account_settings"]["lx"] = {"type": "futu"}
    cfg["account_settings"]["sy"] = {"type": "external_holdings", "holdings_account": "sy"}
    cfg["portfolio"]["account"] = "sy"
    cfg["portfolio"]["source"] = "auto"
    cfg["portfolio"]["source_by_account"] = {"lx": "futu", "sy": "holdings"}
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    shared_ctx = {
        "as_of_utc": "2026-04-14T00:00:00+00:00",
        "filters": {"broker": "富途", "account": None},
        "all_accounts": {
            "filters": {"broker": "富途", "account": None},
            "cash_by_currency": {},
            "stocks_by_symbol": {},
            "raw_selected_count": 0,
        },
        "by_account": {
            "sy": {
                "as_of_utc": "2026-04-14T00:00:00+00:00",
                "filters": {"broker": "富途", "account": "sy"},
                "cash_by_currency": {"HKD": 10000.0},
                "stocks_by_symbol": {
                    "0700.HK": {
                        "symbol": "0700.HK",
                        "shares": 1100,
                        "avg_cost": 420.0,
                        "currency": "HKD",
                        "account": "sy",
                    }
                },
                "raw_selected_count": 1,
            }
        },
    }

    def _is_fresh(path: Path, ttl_sec: int) -> bool:
        return path.name in {"portfolio_context.json", "portfolio_context.shared.json"}

    def _load_cached(path: Path):  # type: ignore[no-untyped-def]
        if path.name == "portfolio_context.json":
            return {
                "as_of_utc": "2026-04-14T00:00:00+00:00",
                "filters": {"broker": "富途", "account": "lx"},
                "cash_by_currency": {"HKD": 8000.0},
                "stocks_by_symbol": {
                    "0700.HK": {
                        "symbol": "0700.HK",
                        "shares": 100,
                        "avg_cost": 410.0,
                        "currency": "HKD",
                        "account": "lx",
                    }
                },
                "raw_selected_count": 1,
                "portfolio_source_name": "external_holdings",
            }
        if path.name == "portfolio_context.shared.json":
            return shared_ctx
        return None

    monkeypatch.setattr(pipeline_context, "is_fresh", _is_fresh)
    monkeypatch.setattr(pipeline_context, "load_cached_json", _load_cached)
    monkeypatch.setattr(pcs, "load_holdings_portfolio_shared_context", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("should reuse shared cache")))  # type: ignore[assignment]

    out_root = tmp_path / "output_shared" / "agent_tools"
    out = run_tool(
        "get_portfolio_context",
        {
            "config_path": str(cfg_path),
            "account": "sy",
            "output_dir": str(out_root),
            "ttl_sec": 3600,
        },
    )

    assert out["ok"] is True
    assert out["data"]["filters"]["account"] == "sy"
    assert out["data"]["stocks_by_symbol"]["0700.HK"]["account"] == "sy"
    assert out["data"]["stocks_by_symbol"]["0700.HK"]["shares"] == 1100
    state_path = out_root / "portfolio_context_state" / "portfolio_context.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["filters"]["account"] == "sy"
    assert payload["stocks_by_symbol"]["0700.HK"]["account"] == "sy"


def test_spec_exposes_broker_as_public_field() -> None:
    from src.application.tool_execution import build_tool_manifest as build_spec

    spec = build_spec()
    query_tool = next(item for item in spec["tools"] if item["name"] == "query_cash_headroom")
    assert "broker" in query_tool["input_schema"]
    assert "market" not in query_tool["input_schema"]
    assert "data_config" in query_tool["input_schema"]
    assert "pm_config" not in query_tool["input_schema"]


def test_monthly_income_report_returns_agent_summary(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool
    from domain.domain.option_position_lots import OpenPositionCommand, parse_exp_to_ms

    def _ms(value: str) -> int:
        out = parse_exp_to_ms(value)
        assert out is not None
        return out

    sqlite_path = tmp_path / "output_shared" / "state" / "option_positions.sqlite3"
    data_cfg_path = tmp_path / "portfolio.runtime.json"
    data_cfg_path.parent.mkdir(parents=True, exist_ok=True)
    data_cfg_path.write_text(
        json.dumps({"option_positions": {"sqlite_path": str(sqlite_path)}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(
        json.dumps(_public_cfg_with_futu(str(data_cfg_path)), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    repo = ledger_repository.SQLiteOptionPositionsRepository(sqlite_path)
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="user1",
            symbol="NVDA",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=100.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            premium_per_share=2.5,
            opened_at_ms=_ms("2026-04-03"),
        ),
    )
    lot = repo.list_position_lots()[0]
    ledger_manual_trades.persist_manual_close_event(
        repo,
        record_id=lot["record_id"],
        fields=lot["fields"],
        contracts_to_close=1,
        close_price=1.0,
        close_reason="manual_buy_to_close",
        as_of_ms=_ms("2026-04-20"),
    )

    rate_calls: list[dict[str, Any]] = []

    def _fake_get_exchange_rates(**kwargs):
        rate_calls.append(kwargs)
        return {"rates": {"USDCNY": 7.2, "HKDCNY": 0.92}}

    _patch_agent_tool_context(monkeypatch, get_exchange_rates=_fake_get_exchange_rates)

    out = run_tool(
        "monthly_income_report",
        {
            "config_path": str(cfg_path),
            "account": "user1",
            "month": "2026-04",
            "include_rows": True,
        },
    )

    assert out["ok"] is True
    assert Path(rate_calls[0]["cache_path"]) == tmp_path / "output_shared" / "state" / "rate_cache.json"
    assert out["warnings"] == []
    assert out["data"]["row_count"] == 1
    assert out["data"]["premium_row_count"] == 1
    assert out["data"]["calculation_method"] == "trade_events"
    assert len(out["data"]["summary"]) == 1
    row = out["data"]["summary"][0]
    assert {key: row.get(key) for key in {
        "month",
        "account",
        "currency",
        "net_cashflow_gross",
        "realized_pnl_gross",
        "open_basis_lifecycle_pnl_gross",
        "realized_gross",
        "realized_gross_cny",
        "closed_contracts",
        "positions",
        "premium_received_gross",
        "premium_received_gross_cny",
        "premium_contracts",
        "premium_positions",
    }} == {
        "month": "2026-04",
        "account": "user1",
        "currency": "USD",
        "net_cashflow_gross": 150.0,
        "realized_pnl_gross": 150.0,
        "open_basis_lifecycle_pnl_gross": 150.0,
        "realized_gross": 150.0,
        "realized_gross_cny": 1080.0,
        "closed_contracts": 1,
        "positions": 1,
        "premium_received_gross": 250.0,
        "premium_received_gross_cny": 1800.0,
        "premium_contracts": 1,
        "premium_positions": 1,
    }
    assert out["data"]["rows"][0]["realized_gross"] == 150.0
    assert out["data"]["premium_rows"][0]["premium_received_gross"] == 250.0
    assert out["data"]["cashflow_rows"][0]["net_cashflow_gross"] == 250.0
    assert out["data"]["cashflow_rows"][1]["net_cashflow_gross"] == -100.0
    assert out["meta"]["data_config"] == ".../portfolio.runtime.json"


def test_version_check_returns_agent_diagnostic(monkeypatch) -> None:
    from dataclasses import replace

    import src.application.tool_execution as tool_execution

    ctx = tool_execution.build_default_agent_tool_context()

    def _check_version_update(**kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "current_version": "1.0.9",
            "latest_version": "1.0.9",
            "update_available": False,
            "remote_name": kwargs["remote_name"],
            "release_tag": "v1.0.9",
            "checked_at": "2026-05-05T00:00:00Z",
            "message": "当前已是最新版本 1.0.9",
            "error": None,
        }

    monkeypatch.setattr(
        tool_execution,
        "build_default_agent_tool_context",
        lambda: replace(ctx, check_version_update=_check_version_update),
    )

    out = tool_execution.execute_tool("version_check", {"remote_name": "origin"})

    assert out["ok"] is True
    assert out["warnings"] == []
    assert out["data"]["current_version"] == "1.0.9"
    assert out["data"]["remote_name"] == "origin"


def test_version_update_defaults_to_dry_run(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    (tmp_path / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    _patch_agent_tool_context(monkeypatch, repo_base=lambda: tmp_path)

    out = run_tool("version_update", {"bump": "patch"})

    assert out["ok"] is True
    assert out["warnings"] == ["dry-run only; pass apply=true to write VERSION"]
    assert out["data"]["mode"] == "dry_run"
    assert out["data"]["current_version"] == "1.0.0"
    assert out["data"]["target_version"] == "1.0.1"
    assert out["data"]["would_change"] is True
    assert out["data"]["changed"] is False
    assert out["meta"]["version_path"] == ".../VERSION"
    assert (tmp_path / "VERSION").read_text(encoding="utf-8").strip() == "1.0.0"


def test_version_update_apply_writes_version(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    (tmp_path / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    _patch_agent_tool_context(monkeypatch, repo_base=lambda: tmp_path)
    monkeypatch.setenv("OM_AGENT_ENABLE_WRITE_TOOLS", "true")

    out = run_tool("version_update", {"target_version": "1.1.0", "apply": True, "confirm": True})

    assert out["ok"] is True
    assert out["warnings"] == []
    assert out["data"]["mode"] == "applied"
    assert out["data"]["target_version"] == "1.1.0"
    assert out["data"]["changed"] is True
    assert (tmp_path / "VERSION").read_text(encoding="utf-8").strip() == "1.1.0"


def test_version_update_apply_requires_write_gate(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    (tmp_path / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    _patch_agent_tool_context(monkeypatch, repo_base=lambda: tmp_path)

    blocked = run_tool("version_update", {"target_version": "1.1.0", "apply": True})
    assert blocked["ok"] is False
    assert blocked["error"]["code"] == "PERMISSION_DENIED"
    assert (tmp_path / "VERSION").read_text(encoding="utf-8").strip() == "1.0.0"

    monkeypatch.setenv("OM_AGENT_ENABLE_WRITE_TOOLS", "true")
    needs_confirm = run_tool("version_update", {"target_version": "1.1.0", "apply": True})
    assert needs_confirm["ok"] is False
    assert needs_confirm["error"]["code"] == "CONFIRMATION_REQUIRED"
    assert (tmp_path / "VERSION").read_text(encoding="utf-8").strip() == "1.0.0"


def test_version_update_rejects_removed_version_alias(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    (tmp_path / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    _patch_agent_tool_context(monkeypatch, repo_base=lambda: tmp_path)

    out = run_tool("version_update", {"version": "1.1.0"})

    assert out["ok"] is False
    assert out["error"]["code"] == "INPUT_ERROR"
    assert "target_version" in out["error"]["message"]


def test_config_validate_runs_without_opend(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    cfg = _minimal_cfg()
    cfg["notifications"] = {
        "channel": "wechat_clawbot",
        "target": "clawbot:test-room",
    }
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    out = run_tool("config_validate", {"config_path": str(cfg_path)})

    assert out["ok"] is True
    assert out["data"]["ok"] is True
    assert out["data"]["account_count"] == 1
    assert out["data"]["symbol_count"] == 1
    assert out["meta"]["config_path"] == ".../config.us.json"


def test_scheduler_status_reads_decision_without_writing_state(tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg = _minimal_cfg()
    cfg["schedule"] = {
        "enabled": True,
        "timezone": "America/New_York",
        "cron_interval_min": 10,
        "run_window": {"start": "09:30", "end": "16:00", "breaks": []},
        "run_points": {"start_plus_min": 10, "hourly_minute": 0, "end_minus_min": 10},
    }
    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    state_path = tmp_path / "state" / "scheduler_state.json"

    out = run_tool(
        "scheduler_status",
        {
            "config_path": str(cfg_path),
            "state": str(state_path),
            "account": "user1",
        },
    )

    assert out["ok"] is True
    assert out["data"]["decision"]["schedule_key"] == "schedule"
    assert out["data"]["decision"]["schedule_enabled"] is True
    assert out["data"]["filters"]["account"] == "user1"
    assert out["meta"]["state_path"] == ".../scheduler_state.json"
    assert not state_path.exists()


def test_option_positions_read_lists_events_history_and_inspect(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool
    from domain.domain.option_position_lots import OpenPositionCommand, parse_exp_to_ms
    from src.application.ledger.commands import record_manual_assignment
    from src.application.positions.assigned_stock_quotes import AssignedStockQuoteRefreshResult

    def _ms(value: str) -> int:
        out = parse_exp_to_ms(value)
        assert out is not None
        return out

    sqlite_path = tmp_path / "output_shared" / "state" / "option_positions.sqlite3"
    data_cfg_path = tmp_path / "portfolio.runtime.json"
    data_cfg_path.parent.mkdir(parents=True, exist_ok=True)
    data_cfg_path.write_text(
        json.dumps({"option_positions": {"sqlite_path": str(sqlite_path)}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(
        json.dumps(_public_cfg_with_futu(str(data_cfg_path)), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    repo = ledger_repository.SQLiteOptionPositionsRepository(sqlite_path)
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="user1",
            symbol="NVDA",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=100.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            premium_per_share=2.5,
            opened_at_ms=_ms("2026-04-03"),
        ),
    )
    lot = repo.list_position_lots()[0]
    record_id = str(lot["record_id"])

    listed = run_tool(
        "option_positions_read",
        {
            "config_path": str(cfg_path),
            "action": "list",
            "account": "user1",
            "status": "open",
        },
    )
    events = run_tool(
        "option_positions_read",
        {
            "config_path": str(cfg_path),
            "action": "events",
            "account": "user1",
            "limit": 5,
        },
    )
    history = run_tool(
        "option_positions_read",
        {
            "config_path": str(cfg_path),
            "action": "history",
            "record_id": record_id,
        },
    )
    inspected = run_tool(
        "option_positions_read",
        {
            "config_path": str(cfg_path),
            "action": "inspect",
            "record_id": record_id,
        },
    )

    assert listed["ok"] is True
    assert listed["data"]["row_count"] == 1
    assert listed["data"]["rows"][0]["record_id"] == record_id
    assert events["ok"] is True
    assert events["data"]["row_count"] == 1
    assert events["data"]["rows"][0]["symbol"] == "NVDA"
    assert history["ok"] is True
    assert history["data"]["event_count"] == 1
    assert inspected["ok"] is True
    assert inspected["data"]["matched_record_ids"] == [record_id]
    assert inspected["meta"]["data_config"] == ".../portfolio.runtime.json"

    record_manual_assignment(
        repo,
        record_id=record_id,
        contracts_to_close=1,
        stock_side="buy",
        stock_qty=100,
        stock_price=100.0,
        as_of_ms=_ms("2026-05-15"),
    )
    assignment_event = [item for item in repo.list_trade_events() if item.get("event_type") == "assignment"][0]
    stock_lot_id = f"assigned-stock-{assignment_event['event_id']}"

    assigned_stock = run_tool(
        "option_positions_read",
        {
            "config_path": str(cfg_path),
            "action": "assigned-stock",
            "account": "user1",
            "symbol": "NVDA",
            "quote_snapshots": [{"symbol": "NVDA", "spot": 98.0, "quote_time_ms": _ms("2026-05-16")}],
        },
    )

    assert assigned_stock["ok"] is True
    assert assigned_stock["data"]["row_count"] == 1
    assigned_stock_row = assigned_stock["data"]["rows"][0]
    assert assigned_stock_row["stock_lot_id"] == stock_lot_id
    assert assigned_stock_row["stock_cost_per_share"] == 100.0
    assert assigned_stock_row["assigned_stock_unrealized_pnl"] == -200.0
    assert assigned_stock_row["option_premium_attribution"] == 250.0
    assert assigned_stock_row["assignment_lifecycle_pnl"] == 50.0

    list_wrapped_action = run_tool(
        "option_positions_read",
        {
            "config_path": str(cfg_path),
            "action": ["assigned-stock"],
            "account": "user1",
            "status": ["open"],
            "symbol": "NVDA",
            "quote_snapshots": [{"symbol": "NVDA", "spot": 98.0, "quote_time_ms": _ms("2026-05-16")}],
        },
    )

    assert list_wrapped_action["ok"] is True
    assert list_wrapped_action["data"]["action"] == "assigned-stock"
    assert list_wrapped_action["data"]["row_count"] == 1

    quote_refresh_calls: list[dict[str, Any]] = []

    def _refresh_assigned_stock_quotes(rows: list[dict[str, Any]], **kwargs: Any) -> AssignedStockQuoteRefreshResult:
        quote_refresh_calls.append({"rows": rows, **kwargs})
        return AssignedStockQuoteRefreshResult(
            quote_snapshots=[
                {
                    "symbol": "NVDA",
                    "spot": 99.0,
                    "quote_time_ms": _ms("2026-05-17"),
                    "quote_source": "opend_realtime",
                    "quote_status": "fresh",
                }
            ],
            diagnostics={
                "enabled": True,
                "status": "ok",
                "quote_source": "opend_realtime",
                "requested_symbols": ["NVDA"],
                "refreshed_symbols": ["NVDA"],
                "missing_symbols": [],
                "errors": [],
                "quote_count": 1,
            },
            warnings=[],
        )

    _patch_agent_tool_context(monkeypatch, refresh_assigned_stock_quotes=_refresh_assigned_stock_quotes)

    refreshed_assigned_stock = run_tool(
        "option_positions_read",
        {
            "config_path": str(cfg_path),
            "action": "assigned-stock",
            "account": "user1",
            "symbol": "NVDA",
            "refresh_quotes": True,
        },
    )

    assert refreshed_assigned_stock["ok"] is True
    assert quote_refresh_calls[0]["account"] == "user1"
    assert Path(quote_refresh_calls[0]["base_dir"]).resolve() == BASE.resolve()
    assert Path(quote_refresh_calls[0]["state_base_dir"]).resolve() == tmp_path.resolve()
    assert quote_refresh_calls[0]["rows"][0]["stock_lot_id"] == stock_lot_id
    refreshed_row = refreshed_assigned_stock["data"]["rows"][0]
    assert refreshed_row["spot"] == 99.0
    assert refreshed_row["quote_source"] == "opend_realtime"
    assert refreshed_row["assigned_stock_unrealized_pnl"] == -100.0
    assert refreshed_row["assignment_lifecycle_pnl"] == 150.0
    assert refreshed_assigned_stock["data"]["quote_refresh"]["status"] == "ok"

    skipped_historical_refresh = run_tool(
        "option_positions_read",
        {
            "config_path": str(cfg_path),
            "action": "assigned-stock",
            "account": "user1",
            "symbol": "NVDA",
            "refresh_quotes": True,
            "as_of_ms": _ms("2026-05-16"),
        },
    )

    assert skipped_historical_refresh["ok"] is True
    assert len(quote_refresh_calls) == 1
    assert skipped_historical_refresh["data"]["quote_refresh"]["status"] == "skipped_historical_as_of"
    assert skipped_historical_refresh["warnings"] == [
        "refresh_quotes ignored because as_of_ms was provided; historical as-of requires supplied quote_snapshots"
    ]

    skipped_no_match_refresh = run_tool(
        "option_positions_read",
        {
            "config_path": str(cfg_path),
            "action": "assigned-stock",
            "account": "user1",
            "symbol": "MSFT",
            "refresh_quotes": True,
        },
    )

    assert skipped_no_match_refresh["ok"] is True
    assert len(quote_refresh_calls) == 1
    assert skipped_no_match_refresh["data"]["row_count"] == 0
    assert skipped_no_match_refresh["data"]["quote_refresh"]["status"] == "skipped_no_matching_assigned_stock"


def test_option_positions_read_open_assigned_stock_includes_partially_sold(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool
    from domain.domain.option_position_lots import OpenPositionCommand, parse_exp_to_ms
    from src.application.ledger.commands import record_manual_assignment
    from src.application.positions.workflows import execute_manual_assigned_stock_sale

    def _ms(value: str) -> int:
        out = parse_exp_to_ms(value)
        assert out is not None
        return out

    sqlite_path = tmp_path / "output_shared" / "state" / "option_positions.sqlite3"
    data_cfg_path = tmp_path / "portfolio.runtime.json"
    data_cfg_path.parent.mkdir(parents=True, exist_ok=True)
    data_cfg_path.write_text(
        json.dumps({"option_positions": {"sqlite_path": str(sqlite_path)}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(
        json.dumps(_public_cfg_with_futu(str(data_cfg_path)), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    repo = ledger_repository.SQLiteOptionPositionsRepository(sqlite_path)
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="user1",
            symbol="NVDA",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=100.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            premium_per_share=2.5,
            opened_at_ms=_ms("2026-04-03"),
        ),
    )
    lot = repo.list_position_lots()[0]
    record_manual_assignment(
        repo,
        record_id=str(lot["record_id"]),
        contracts_to_close=1,
        stock_side="buy",
        stock_qty=100,
        stock_price=100.0,
        as_of_ms=_ms("2026-05-15"),
    )
    assignment_event = [item for item in repo.list_trade_events() if item.get("event_type") == "assignment"][0]
    stock_lot_id = f"assigned-stock-{assignment_event['event_id']}"
    execute_manual_assigned_stock_sale(
        repo,
        target_stock_lot_id=stock_lot_id,
        account="user1",
        broker="富途",
        symbol="NVDA",
        currency="USD",
        shares=40,
        price=105.0,
        trade_time_ms=_ms("2026-06-01"),
        dry_run=False,
    )

    quote_snapshots = [{"symbol": "NVDA", "spot": 98.0, "quote_time_ms": _ms("2026-06-02")}]
    open_rows = run_tool(
        "option_positions_read",
        {
            "config_path": str(cfg_path),
            "action": "assigned-stock",
            "account": "user1",
            "status": "open",
            "quote_snapshots": quote_snapshots,
        },
    )
    partially_sold_rows = run_tool(
        "option_positions_read",
        {
            "config_path": str(cfg_path),
            "action": "assigned-stock",
            "account": "user1",
            "status": "partially_sold",
            "quote_snapshots": quote_snapshots,
        },
    )
    closed_rows = run_tool(
        "option_positions_read",
        {
            "config_path": str(cfg_path),
            "action": "assigned-stock",
            "account": "user1",
            "status": "closed",
            "quote_snapshots": quote_snapshots,
        },
    )

    assert open_rows["ok"] is True
    assert open_rows["data"]["row_count"] == 1
    open_row = open_rows["data"]["rows"][0]
    assert open_row["stock_lot_id"] == stock_lot_id
    assert open_row["status"] == "partially_sold"
    assert open_row["shares_remaining"] == 60
    assert open_row["shares_sold"] == 40
    assert partially_sold_rows["ok"] is True
    assert partially_sold_rows["data"]["row_count"] == 1
    assert closed_rows["ok"] is True
    assert closed_rows["data"]["row_count"] == 0


def test_runtime_status_summarizes_openclaw_runtime_files(tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    cfg = _minimal_cfg()
    cfg["notifications"] = {
        "channel": "wechat_clawbot",
        "target": "clawbot:test-room",
    }
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    state_dir = tmp_path / "output_shared" / "state"
    report_dir = tmp_path / "output_shared" / "reports"
    shared_state_dir = tmp_path / "output_shared" / "state"
    accounts_root = tmp_path / "output_accounts"
    runs_root = tmp_path / "output_runs"
    for path in (state_dir, report_dir, shared_state_dir, accounts_root / "user1" / "state", accounts_root / "user1" / "reports"):
        path.mkdir(parents=True, exist_ok=True)

    (shared_state_dir / "last_run.json").write_text(json.dumps({"status": "ok", "run_id": "run-1"}), encoding="utf-8")
    (state_dir / "auto_trade_intake_status.json").write_text(
        json.dumps(
            {
                "status": "listening",
                "stage": "deal_processed",
                "last_heartbeat_utc": "2026-01-01T00:00:00+00:00",
                "last_push_received_utc": "2026-01-01T00:01:00+00:00",
                "last_push_deal_id": "deal-1",
                "last_backfill_check_utc": "2026-01-01T00:05:00+00:00",
                "last_backfill_window_start_utc": "2026-01-01T00:00:00+00:00",
                "last_backfill_window_end_utc": "2026-01-01T00:05:00+00:00",
                "last_backfill_deal_count": 2,
                "last_backfill_applied_count": 1,
                "last_backfill_skipped_duplicate_count": 1,
                "missed_push_backfill_count": 1,
                "last_deal_result": {"status": "applied", "deal_id": "deal-1"},
                "last_backfill_result": {"status": "skipped", "deal_id": "deal-0"},
                "last_receipt_result": {"status": "sent", "delivery_confirmed": True},
            }
        ),
        encoding="utf-8",
    )
    (state_dir / "auto_trade_intake_state.json").write_text(
        json.dumps(
            {
                "processed_deal_ids": {
                    "deal-1": {
                        "status": "applied",
                        "receipt": {"status": "sent", "delivery_confirmed": True},
                    }
                },
                "failed_deal_ids": {},
                "unresolved_deal_ids": {},
            }
        ),
        encoding="utf-8",
    )
    (state_dir / "auto_trade_intake_audit.jsonl").write_text('{"phase":"receipt_sent"}\n', encoding="utf-8")
    (state_dir / "option_positions_context.json").write_text(
        json.dumps(
            {
                "ledger": {
                    "status": "ok",
                    "reason": "ledger_shadow_ok",
                    "read_model": "ledger_shadow",
                    "fail_closed": False,
                    "source_record_count": 1,
                    "imported_event_count": 1,
                    "lot_count": 1,
                    "open_lot_count": 1,
                    "view_count": 1,
                },
                "open_positions_min": [],
            }
        ),
        encoding="utf-8",
    )
    projection_verify_dir = shared_state_dir / "option_positions" / "current"
    projection_verify_dir.mkdir(parents=True, exist_ok=True)
    (projection_verify_dir / "projection_verify.latest.json").write_text(
        json.dumps({"ok": True, "mode_used": "checkpoint_reuse", "summary": {"matched": 1}}),
        encoding="utf-8",
    )
    (tmp_path / "upgrade_status.json").write_text(
        json.dumps({"status": "upgraded", "target_version": "1.2.99"}),
        encoding="utf-8",
    )
    (report_dir / "symbols_notification.txt").write_text("shared notification\n", encoding="utf-8")
    (accounts_root / "user1" / "state" / "last_run.json").write_text(json.dumps({"status": "account_ok"}), encoding="utf-8")
    (accounts_root / "user1" / "reports" / "symbols_notification.txt").write_text("account notification\n", encoding="utf-8")

    run_dir = runs_root / "run-1"
    (run_dir / "state").mkdir(parents=True, exist_ok=True)
    (run_dir / "accounts" / "user1" / "state").mkdir(parents=True, exist_ok=True)
    (shared_state_dir / "last_run_dir.txt").write_text(str(run_dir), encoding="utf-8")
    (run_dir / "state" / "tick_metrics.json").write_text(
        json.dumps(
            {
                "scheduler_decision": {
                    "should_run_scan": True,
                    "is_notify_window_open": True,
                    "reason": "到达运行点 11:00：执行扫描并允许通知。",
                },
                "notify_summary": {
                    "account_messages_count": 1,
                    "send_attempted_count": 1,
                    "send_confirmed_count": 1,
                    "send_failed_count": 0,
                },
                "sent_accounts": ["user1"],
                "reason": "sent",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (run_dir / "accounts" / "user1" / "symbols_notification.txt").write_text("run account notification\n", encoding="utf-8")
    (run_dir / "accounts" / "user1" / "state" / "required_data_prefetch_summary.json").write_text(
        json.dumps(
            {
                "to_fetch": 3,
                "deduped_count": 1,
                "errors": 0,
                "run_fetch_summary": {
                    "bottleneck": "option_chain_rate_gate",
                    "opend_calls": {
                        "total": 6,
                        "option_chain": 4,
                        "option_expiration": 1,
                        "market_snapshot": 1,
                    },
                    "cache": {
                        "option_chain_hits": 2,
                        "option_expiration_hits": 3,
                    },
                    "rate_gate_wait_sec": {
                        "option_chain": 12.5,
                    },
                    "snapshot": {
                        "requested_codes": 20,
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (run_dir / "accounts" / "user1" / "state" / "expired_position_maintenance.json").write_text(
        json.dumps(
            {
                "mode": "applied",
                "applied_closed": 1,
                "receipt": {
                    "status": "sent",
                    "delivery_confirmed": True,
                    "message_id": "msg-auto-1",
                    "attempt_count": 1,
                    "receipt_key": "receipt-key-1",
                    "updated_at": "2026-05-15T16:10:00+00:00",
                },
            }
        ),
        encoding="utf-8",
    )

    out = run_tool(
        "runtime_status",
        {
            "config_path": str(cfg_path),
            "state_dir": str(state_dir),
            "report_dir": str(report_dir),
            "shared_state_dir": str(shared_state_dir),
            "accounts_root": str(accounts_root),
            "runs_root": str(runs_root),
        },
    )

    assert out["ok"] is True
    assert out["warnings"] == []
    assert out["data"]["summary"]["ok"] is True
    assert out["data"]["summary"]["latest_status"] == "ok"
    assert out["data"]["shared"]["notification"]["text"] == "shared notification\n"
    assert out["data"]["accounts"]["user1"]["notification"]["text"] == "account notification\n"
    assert out["data"]["latest_run"]["state"]["tick_metrics"]["json"]["notify_summary"]["send_confirmed_count"] == 1
    assert out["data"]["option_positions_context"]["ledger"]["status"] == "ok"
    assert out["data"]["summary"]["ledger_status"] == "ok"
    assert out["data"]["summary"]["ledger_fail_closed"] is False
    assert out["data"]["ledger_store"]["runtime_root"] == str(tmp_path.resolve())
    assert out["data"]["ledger_store"]["sqlite_path"] == str((tmp_path / "output_shared" / "state" / "option_positions.sqlite3").resolve())
    assert out["data"]["summary"]["ledger_sqlite_path"] == out["data"]["ledger_store"]["sqlite_path"]
    assert out["data"]["projection_verify"]["json"]["ok"] is True
    assert out["data"]["summary"]["projection_verify_ok"] is True
    assert out["data"]["summary"]["projection_verify_mode"] == "checkpoint_reuse"
    assert out["data"]["service_upgrade"]["json"]["status"] == "upgraded"
    assert out["data"]["summary"]["service_upgrade_status"] == "upgraded"
    assert out["data"]["summary"]["service_upgrade_target_version"] == "1.2.99"
    assert out["data"]["notification_diagnosis"]["status"] == "sent"
    assert out["data"]["notification_diagnosis"]["scheduler_should_run_scan"] is True
    assert out["data"]["notification_diagnosis"]["send_confirmed_count"] == 1
    assert out["data"]["latest_run"]["accounts"]["user1"]["notification"]["text"] == "run account notification\n"
    assert out["data"]["latest_run"]["accounts"]["user1"]["required_data_prefetch"]["exists"] is True
    assert out["data"]["latest_run"]["accounts"]["user1"]["expired_position_maintenance"]["json"]["receipt"]["status"] == "sent"
    assert out["data"]["latest_run"]["accounts"]["user1"]["auto_close_receipt"]["receipt_key"] == "receipt-key-1"
    assert out["data"]["latest_run"]["accounts"]["user1"]["auto_close_receipt"]["attempt_count"] == 1
    assert out["data"]["summary"]["prefetch_available"] is True
    assert out["data"]["summary"]["prefetch_bottleneck"] == "option_chain_rate_gate"
    assert out["data"]["required_data_prefetch"]["total_opend_calls"] == 6
    assert out["data"]["required_data_prefetch"]["total_rate_gate_wait_sec"] == 12.5
    assert out["data"]["required_data_prefetch"]["accounts"]["user1"]["deduped_count"] == 1
    assert out["data"]["required_data_prefetch"]["accounts"]["user1"]["cache"]["option_expiration_hits"] == 3
    assert out["data"]["trade_intake"]["summary"]["listener_status"] == "listening"
    assert out["data"]["trade_intake"]["summary"]["last_push_received_utc"] == "2026-01-01T00:01:00+00:00"
    assert out["data"]["trade_intake"]["summary"]["last_push_deal_id"] == "deal-1"
    assert out["data"]["trade_intake"]["summary"]["last_backfill_check_utc"] == "2026-01-01T00:05:00+00:00"
    assert out["data"]["trade_intake"]["summary"]["last_backfill_applied_count"] == 1
    assert out["data"]["trade_intake"]["summary"]["missed_push_backfill_count"] == 1
    assert out["data"]["trade_intake"]["summary"]["processed_count"] == 1
    assert out["data"]["trade_intake"]["summary"]["receipt_confirmed_count"] == 1
    assert out["data"]["trade_intake"]["audit"]["exists"] is True
    assert "option_positions_feishu_sync" not in out["data"]
    assert "option_positions_feishu_sync_status" not in out["data"]["summary"]
    assert "option_positions_feishu_sync_receipt_status" not in out["data"]["summary"]


def test_runtime_status_reports_config_authority(tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    cfg = _minimal_cfg()
    cfg["_generated"]["sources"] = [
        {"role": "system", "loaded": True, "inline": True, "sha256": "system-sha"},
        {"role": "common_user", "loaded": False, "optional": True, "enabled": False},
        {"role": "market_user", "loaded": True, "inline": True, "sha256": "yaml-sha"},
    ]
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    out = run_tool(
        "runtime_status",
        {
            "config_path": str(cfg_path),
            "state_dir": str(tmp_path / "state"),
            "report_dir": str(tmp_path / "reports"),
            "shared_state_dir": str(tmp_path / "state"),
            "accounts_root": str(tmp_path / "accounts"),
            "runs_root": str(tmp_path / "runs"),
        },
    )

    assert out["ok"] is True
    authority = out["data"]["config_authority"]
    assert authority["ok"] is True
    assert authority["authoring_source"] == "config.yaml"
    assert authority["source_format"] == "yaml"
    assert authority["config_yaml_sha256"] == "yaml-sha"
    assert authority["system_config_sha256"] == "system-sha"
    assert authority["identity"]["ok"] is True
    assert authority["freshness"]["ok"] is True
    assert out["data"]["summary"]["config_authority_ok"] is True


def test_runtime_status_reports_config_authority_for_legacy_runtime_config(tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    cfg = _minimal_cfg()
    cfg["_generated"].pop("source_format")
    cfg["_generated"]["sources"] = [
        {"role": "system", "loaded": True, "inline": True, "sha256": "system-sha"},
        {"role": "market_user", "loaded": True, "inline": True, "sha256": "legacy-json-sha"},
    ]
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    out = run_tool(
        "runtime_status",
        {
            "config_path": str(cfg_path),
            "state_dir": str(tmp_path / "state"),
            "report_dir": str(tmp_path / "reports"),
            "shared_state_dir": str(tmp_path / "state"),
            "accounts_root": str(tmp_path / "accounts"),
            "runs_root": str(tmp_path / "runs"),
        },
    )

    assert out["ok"] is True
    authority = out["data"]["config_authority"]
    assert authority["ok"] is False
    assert authority["source_format"] is None
    assert authority["identity"]["ok"] is False
    assert authority["stale_or_invalid_reason"] == "runtime config generation metadata is missing source_format"
    assert "--market us" in authority["rebuild_command"]
    assert out["data"]["summary"]["config_authority_ok"] is False


def _runtime_status_upgrade_fixture(tmp_path: Path, *, target_version: str = "1.2.82") -> dict[str, Any]:
    (tmp_path / "VERSION").write_text("1.2.82\n", encoding="utf-8")
    data_config = tmp_path / "portfolio.runtime.json"
    data_config.write_text("{}", encoding="utf-8")
    cfg_path = tmp_path / "config.us.json"
    cfg = {
        "accounts": ["user1"],
        "portfolio": {"data_config": str(data_config)},
        "notifications": {"provider": "wechat_clawbot", "target": "route"},
    }
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    (tmp_path / "output_shared" / "state").mkdir(parents=True)
    (tmp_path / "output_shared" / "state" / "last_run.json").write_text(json.dumps({"status": "ok"}), encoding="utf-8")
    (tmp_path / "output_shared" / "reports").mkdir(parents=True)
    (tmp_path / "output_shared" / "reports" / "symbols_notification.txt").write_text("ok\n", encoding="utf-8")
    (tmp_path / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "runtime_root": str(tmp_path),
                "services": [
                    {"name": "options-monitor-trade-intake.service"},
                    {"name": "options-monitor-feishu-ws.service"},
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "upgrade_status.json").write_text(
        json.dumps(
            {
                "ok": False,
                "status": "failed",
                "current_version": "1.2.81",
                "target_version": target_version,
                "changed": True,
                "symlink_switched": True,
                "error": "ServiceRestartError: failed to restart options-monitor-trade-intake.service",
                "restart_failed_services": ["options-monitor-trade-intake.service"],
            }
        ),
        encoding="utf-8",
    )
    return {"cfg_path": cfg_path, "cfg": cfg}


def _call_runtime_status_for_upgrade(tmp_path: Path, cfg_path: Path, cfg: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    from src.application.agent_tool_runtime_status import runtime_status_tool

    def _read_json(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    return runtime_status_tool(
        {"config_path": str(cfg_path)},
        load_runtime_config=lambda **_kwargs: (cfg_path, cfg),
        normalize_accounts=lambda value, fallback=(): list(value or fallback),
        accounts_from_config=lambda loaded: list(loaded.get("accounts") or []),
        read_json_object_or_empty=_read_json,
        repo_base=lambda: tmp_path,
        mask_path=lambda path: str(path),
    )


def test_runtime_status_reports_assistant_llm_and_latest_agent_route(monkeypatch, tmp_path: Path) -> None:
    from src.application.assistant.audit import InboundAuditStore

    fixture = _runtime_status_upgrade_fixture(tmp_path)
    assistant_dir = tmp_path / "resolved"
    assistant_dir.mkdir()
    (assistant_dir / "config.assistant.json").write_text(
        json.dumps(
            {
                "assistant": {
                    "context_window_messages": 6,
                    "default_market_scope": "us",
                    "llm": {
                        "provider": "deepseek",
                        "base_url": "https://api.deepseek.com",
                        "model": "deepseek-v4-flash",
                        "api_key_env": "DEEPSEEK_API_KEY",
                    },
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    audit_db = tmp_path / "inbound.sqlite3"
    monkeypatch.setenv("OM_INBOUND_AUDIT_DB", str(audit_db))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    InboundAuditStore(audit_db).record_result(
        {
            "command_id": "in_runtime_status_agent_route",
            "channel": "feishu",
            "sender_id": "ou_1",
            "conversation_id": "feishu:chat_1:ou_1",
            "message_id": "omsg_1",
            "raw_text": "系统怎么样",
            "parser": "llm",
            "intent_name": "runtime_status",
            "tool_name": "runtime_status",
            "decision": "allowed",
            "result_ok": True,
            "response": {
                "meta": {
                    "assistant": {
                        "route": "agent_loop",
                        "llm": {"attempted": True, "reason": "accepted"},
                        "context": {"provided": True, "recent_count": 1, "pending_count": 0},
                    }
                }
            },
        }
    )

    data, _warnings, _meta = _call_runtime_status_for_upgrade(tmp_path, fixture["cfg_path"], fixture["cfg"])

    assert data["assistant_runtime"]["config"]["enabled"] is True
    assert data["assistant_runtime"]["config"]["planner"]["enabled"] is True
    assert data["assistant_runtime"]["llm"]["enabled"] is True
    assert data["assistant_runtime"]["llm"]["provider"] == "deepseek"
    assert data["assistant_runtime"]["llm"]["endpoint_url"] == "https://api.deepseek.com/chat/completions"
    assert data["assistant_runtime"]["llm"]["api_key_configured"] is True
    assert data["assistant_runtime"]["audit"]["latest"]["route"] == "agent_loop"
    assert data["assistant_runtime"]["audit"]["latest"]["llm_reason"] == "accepted"
    assert data["summary"]["assistant_enabled"] is True
    assert data["summary"]["assistant_latest_route"] == "agent_loop"


def test_assistant_trace_agent_tool_reports_missing_store(tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    out = run_tool("assistant_trace", {"audit_db": str(tmp_path / "missing.sqlite3"), "limit": 5})

    assert out["ok"] is True
    assert out["data"]["schema_version"] == "om-assistant-trace-v1"
    assert out["data"]["trace_count"] == 0
    assert out["warnings"] == ["audit_db_missing"]
    assert "没有匹配的 Agent session" in out["data"]["response_text"]


def test_runtime_status_does_not_report_llm_endpoint_when_llm_disabled(tmp_path: Path) -> None:
    fixture = _runtime_status_upgrade_fixture(tmp_path)

    data, _warnings, _meta = _call_runtime_status_for_upgrade(tmp_path, fixture["cfg_path"], fixture["cfg"])

    assert data["assistant_runtime"]["config"]["enabled"] is True
    assert data["assistant_runtime"]["llm"]["enabled"] is False
    assert data["assistant_runtime"]["llm"]["provider"] == ""
    assert data["assistant_runtime"]["llm"]["endpoint_url"] is None


def test_runtime_status_uses_service_profile_assistant_config_and_env_file(tmp_path: Path) -> None:
    from src.application.assistant.audit import InboundAuditStore

    fixture = _runtime_status_upgrade_fixture(tmp_path)
    assistant_path = tmp_path / "assistant" / "config.assistant.json"
    assistant_path.parent.mkdir()
    assistant_path.write_text(
        json.dumps(
            {
                "assistant": {
                    "llm": {
                        "provider": "deepseek",
                        "base_url": "https://api.deepseek.com",
                        "model": "deepseek-v4-flash",
                        "api_key_env": "DEEPSEEK_API_KEY",
                    },
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    audit_db = tmp_path / "profile-audit.sqlite3"
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text(
        f"DEEPSEEK_API_KEY=sk-profile\nOM_INBOUND_AUDIT_DB={audit_db}\n",
        encoding="utf-8",
    )
    (tmp_path / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "runtime_root": str(tmp_path),
                "env_file": str(env_file),
                "assistant_config_path": str(assistant_path),
                "feishu_ws": {
                    "assistant_config_path": str(assistant_path),
                    "audit_db": str(audit_db),
                },
                "services": [{"name": "options-monitor-feishu-ws.service"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    InboundAuditStore(audit_db).record_result(
        {
            "command_id": "in_profile_runtime_status_agent_route",
            "channel": "feishu",
            "sender_id": "ou_1",
            "conversation_id": "feishu:chat_1:ou_1",
            "message_id": "omsg_profile",
            "raw_text": "系统怎么样",
            "parser": "llm",
            "intent_name": "runtime_status",
            "tool_name": "runtime_status",
            "decision": "allowed",
            "result_ok": True,
            "response": {
                "meta": {
                    "assistant": {
                        "route": "agent_loop",
                        "llm": {"attempted": True, "reason": "accepted"},
                    }
                }
            },
        }
    )

    data, _warnings, _meta = _call_runtime_status_for_upgrade(tmp_path, fixture["cfg_path"], fixture["cfg"])

    assert data["assistant_runtime"]["config"]["path"] == str(assistant_path)
    assert data["assistant_runtime"]["config"]["enabled"] is True
    assert data["assistant_runtime"]["llm"]["api_key_configured"] is True
    assert data["assistant_runtime"]["llm"]["env_file"] == str(env_file)
    assert data["assistant_runtime"]["llm"]["env_file_loaded"] is True
    assert data["assistant_runtime"]["audit"]["path"] == str(audit_db)
    assert data["assistant_runtime"]["audit"]["latest"]["route"] == "agent_loop"
    assert data["environment"]["env_file"] == str(env_file)
    assert data["environment"]["env_file_loaded"] is True
    assert data["environment"]["entries"]["DEEPSEEK_API_KEY"]["configured"] is True
    assert data["environment"]["entries"]["DEEPSEEK_API_KEY"]["source"] == f"env_file:{env_file}"
    assert data["summary"]["env_file_loaded"] is True


def test_runtime_status_ignores_unreadable_profile_env_file_when_env_is_injected(monkeypatch, tmp_path: Path) -> None:
    fixture = _runtime_status_upgrade_fixture(tmp_path)
    assistant_path = tmp_path / "assistant" / "config.assistant.json"
    assistant_path.parent.mkdir()
    assistant_path.write_text(
        json.dumps(
            {
                "assistant": {
                    "llm": {
                        "provider": "deepseek",
                        "base_url": "https://api.deepseek.com",
                        "model": "deepseek-v4-flash",
                        "api_key_env": "DEEPSEEK_API_KEY",
                    },
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    env_file = tmp_path / "options-monitor.env"
    env_file.write_text("DEEPSEEK_API_KEY=sk-profile\n", encoding="utf-8")
    (tmp_path / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "runtime_root": str(tmp_path),
                "env_file": str(env_file),
                "assistant_config_path": str(assistant_path),
                "services": [{"name": "options-monitor-wechat-clawbot.service"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-systemd")

    original_read_text = Path.read_text

    def _read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == env_file:
            raise PermissionError("Permission denied")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _read_text)

    data, warnings, _meta = _call_runtime_status_for_upgrade(tmp_path, fixture["cfg_path"], fixture["cfg"])

    assert not any("failed to read env file" in item for item in warnings)
    assert "ENV_FILE" not in data["summary"]["warning_codes"]
    assert data["environment"]["warnings"] == []
    assert data["environment"]["env_file_loaded"] is False
    assert data["environment"]["entries"]["DEEPSEEK_API_KEY"]["configured"] is True
    assert data["environment"]["entries"]["DEEPSEEK_API_KEY"]["source"] == "process_env"
    assert data["assistant_runtime"]["llm"]["api_key_configured"] is True


def test_runtime_status_reports_wechat_clawbot_channel_health(tmp_path: Path) -> None:
    fixture = _runtime_status_upgrade_fixture(tmp_path)
    assistant_path = tmp_path / "assistant" / "config.assistant.json"
    assistant_path.parent.mkdir()
    assistant_path.write_text(
        json.dumps(
            {
                "inbound": {
                    "wechat_clawbot": {
                        "label": "ops",
                        "allowed_senders": "wechat:user_1",
                        "reply_enabled": False,
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    state_dir = tmp_path / "output_shared" / "state" / "channels" / "wechat_clawbot" / "ops"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text(
        json.dumps({"bot_token": "bot_secret_1", "base_url": "https://example.invalid"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (state_dir / "bindings.json").write_text(
        json.dumps(
            {
                "bindings": {
                    "ops": {
                        "to_user_id": "wx_user_1",
                        "context_token": "ctx_secret_1",
                        "last_message_id": "msg_1",
                        "updated_at_utc": "2026-06-18T01:00:00+00:00",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "runtime_root": str(tmp_path),
                "assistant_config_path": str(assistant_path),
                "wechat_clawbot": {
                    "enabled": True,
                    "label": "ops",
                    "state_dir": str(state_dir),
                    "assistant_config_path": str(assistant_path),
                },
                "services": [{"name": "options-monitor-wechat-clawbot.service"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    data, _warnings, _meta = _call_runtime_status_for_upgrade(tmp_path, fixture["cfg_path"], fixture["cfg"])

    health = data["channel_health"]["wechat_clawbot"]
    assert health["configured"] is True
    assert health["available"] is True
    assert health["label"] == "ops"
    assert health["allowed_senders_configured"] is True
    assert health["bot_token_configured"] is True
    assert health["binding_count"] == 1
    assert health["bindings"]["ops"]["has_context_token"] is True
    assert health["reply_enabled"] is False
    assert data["summary"]["wechat_clawbot_available"] is True
    assert "bot_secret_1" not in json.dumps(data, ensure_ascii=False)
    assert "ctx_secret_1" not in json.dumps(data, ensure_ascii=False)


def test_runtime_status_auto_loads_runtime_service_profile_paths(tmp_path: Path) -> None:
    from src.application.agent_tool_runtime_status import runtime_status_tool

    release_root = tmp_path / "release"
    runtime_root = tmp_path / "runtime"
    release_root.mkdir()
    runtime_root.mkdir()
    (release_root / "VERSION").write_text("1.2.82\n", encoding="utf-8")

    cfg_path = runtime_root / "config.us.json"
    data_config = runtime_root / "portfolio.runtime.json"
    data_config.write_text("{}", encoding="utf-8")
    cfg = {
        "accounts": ["user1"],
        "portfolio": {"data_config": str(data_config)},
        "notifications": {"provider": "wechat_clawbot", "target": "route"},
    }
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    shared_state_dir = runtime_root / "output_shared" / "state"
    report_dir = runtime_root / "output_shared" / "reports"
    shared_state_dir.mkdir(parents=True)
    report_dir.mkdir(parents=True)
    (shared_state_dir / "last_run.json").write_text(json.dumps({"status": "ok"}), encoding="utf-8")
    (report_dir / "symbols_notification.txt").write_text("ready\n", encoding="utf-8")
    (runtime_root / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "runtime_root": str(runtime_root),
                "accounts": ["user1"],
                "paths": {
                    "report_dir": str(report_dir),
                    "state_dir": str(runtime_root / "output_shared" / "state"),
                    "shared_state_dir": str(shared_state_dir),
                    "accounts_root": str(runtime_root / "output_accounts"),
                    "runs_root": str(runtime_root / "output_runs"),
                },
                "config_paths": {"us": str(cfg_path)},
                "services": [{"name": "options-monitor-feishu-ws.service"}],
            }
        ),
        encoding="utf-8",
    )

    data, warnings, _meta = runtime_status_tool(
        {"config_path": str(cfg_path)},
        load_runtime_config=lambda **_kwargs: (cfg_path, cfg),
        normalize_accounts=lambda value, fallback=(): list(value or fallback),
        accounts_from_config=lambda loaded: list(loaded.get("accounts") or []),
        read_json_object_or_empty=lambda path: json.loads(path.read_text(encoding="utf-8")) if path.exists() else {},
        repo_base=lambda: release_root,
        mask_path=lambda path: str(path),
    )

    assert data["shared"]["last_run"]["exists"] is True
    assert data["shared"]["notification"]["exists"] is True
    assert str(data["shared"]["last_run"]["path"]).endswith("last_run.json")
    assert str(data["shared"]["notification"]["path"]).endswith("symbols_notification.txt")
    assert data["openclaw_profile"]["loaded"] is True
    assert data["service_profile"]["loaded"] is True
    assert "No last_run.json found under output_shared/state or output_shared/state." not in warnings
    assert "No symbols_notification.txt found under output_shared/reports or output_accounts/<account>/reports." not in warnings


def test_runtime_status_does_not_expect_scan_notification_for_auto_close_run(tmp_path: Path) -> None:
    from src.application.agent_tool_runtime_status import runtime_status_tool

    release_root = tmp_path / "release"
    runtime_root = tmp_path / "runtime"
    release_root.mkdir()
    runtime_root.mkdir()

    cfg_path = runtime_root / "config.hk.json"
    cfg = {
        "accounts": ["user1"],
        "portfolio": {"broker": "富途"},
    }
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    shared_state_dir = runtime_root / "output_shared" / "state"
    shared_state_dir.mkdir(parents=True)
    run_dir = runtime_root / "output_runs" / "20260529T213013Z-db952f"
    account_state = run_dir / "accounts" / "user1" / "state"
    account_state.mkdir(parents=True)
    (shared_state_dir / "last_run.json").write_text(json.dumps({"status": "ok"}), encoding="utf-8")
    (shared_state_dir / "last_run_dir.txt").write_text(str(run_dir), encoding="utf-8")
    (account_state / "expired_position_maintenance.json").write_text(
        json.dumps(
            {
                "mode": "error",
                "reason": "missing_data_config",
                "applied_closed": 0,
                "errors": ["missing_data_config: /var/lib/options-monitor/portfolio.runtime.json"],
                "receipt": {"status": "sent"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    data, warnings, _meta = runtime_status_tool(
        {
            "config_path": str(cfg_path),
            "state_dir": str(shared_state_dir),
            "shared_state_dir": str(shared_state_dir),
            "report_dir": str(runtime_root / "output_shared" / "reports"),
            "accounts_root": str(runtime_root / "output_accounts"),
            "runs_root": str(runtime_root / "output_runs"),
        },
        load_runtime_config=lambda **_kwargs: (cfg_path, cfg),
        normalize_accounts=lambda value, fallback=(): list(value or fallback),
        accounts_from_config=lambda loaded: list(loaded.get("accounts") or []),
        read_json_object_or_empty=lambda path: json.loads(path.read_text(encoding="utf-8")) if path.exists() else {},
        repo_base=lambda: release_root,
        mask_path=lambda path: str(path),
    )

    assert "No symbols_notification.txt found for latest scanned run or legacy report paths." not in warnings
    assert "Auto-close user1 failed: missing_data_config." in warnings
    assert data["summary"]["warning_codes"] == ["AUTO_CLOSE_FAILED"]


def test_runtime_status_service_profile_does_not_default_to_us_when_market_is_ambiguous(tmp_path: Path) -> None:
    from src.application.agent_tool_runtime_status import runtime_status_tool

    profile_path = tmp_path / "service.profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "config_paths": {
                    "us": str(tmp_path / "config.us.json"),
                    "hk": str(tmp_path / "config.hk.json"),
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    calls: list[dict[str, Any]] = []

    def _load_runtime_config(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("stop after config-scope capture")

    try:
        runtime_status_tool(
            {"profile_path": str(profile_path)},
            load_runtime_config=_load_runtime_config,
            normalize_accounts=lambda value, fallback=(): list(value or fallback),
            accounts_from_config=lambda loaded: list(loaded.get("accounts") or []),
            read_json_object_or_empty=lambda path: json.loads(path.read_text(encoding="utf-8")) if path.exists() else {},
            repo_base=lambda: tmp_path,
            mask_path=lambda path: str(path),
        )
    except RuntimeError as exc:
        assert str(exc) == "stop after config-scope capture"
    else:
        raise AssertionError("expected load_runtime_config sentinel")

    assert calls == [{"config_key": None, "config_path": None, "require_identity": False}]


def test_runtime_status_service_profile_resolves_config_key_to_profile_config_path(tmp_path: Path) -> None:
    from src.application.agent_tool_runtime_status import runtime_status_tool

    us_path = tmp_path / "config.us.json"
    hk_path = tmp_path / "config.hk.json"
    profile_path = tmp_path / "service.profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "config_paths": {
                    "us": str(us_path),
                    "hk": str(hk_path),
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    calls: list[dict[str, Any]] = []

    def _load_runtime_config(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("stop after config-scope capture")

    try:
        runtime_status_tool(
            {"profile_path": str(profile_path), "config_key": "hk"},
            load_runtime_config=_load_runtime_config,
            normalize_accounts=lambda value, fallback=(): list(value or fallback),
            accounts_from_config=lambda loaded: list(loaded.get("accounts") or []),
            read_json_object_or_empty=lambda path: json.loads(path.read_text(encoding="utf-8")) if path.exists() else {},
            repo_base=lambda: tmp_path,
            mask_path=lambda path: str(path),
        )
    except RuntimeError as exc:
        assert str(exc) == "stop after config-scope capture"
    else:
        raise AssertionError("expected load_runtime_config sentinel")

    assert calls == [{"config_key": "hk", "config_path": str(hk_path), "require_identity": False}]


def test_runtime_status_marks_remediated_upgrade_failure(monkeypatch, tmp_path: Path) -> None:
    import src.application.agent_tool_runtime_status as runtime_status

    fixture = _runtime_status_upgrade_fixture(tmp_path)

    def _service_status(profile: dict[str, Any], *, include_status: bool = False) -> dict[str, Any]:
        services_raw = profile.get("services")
        services = services_raw if isinstance(services_raw, list) else []
        return {
            "provider": profile.get("service_provider"),
            "services": [{**item, "status": "ok", "returncode": 0} for item in services if isinstance(item, dict)],
            "status_checked": include_status,
        }

    monkeypatch.setattr(runtime_status, "service_status_from_profile", _service_status)

    data, warnings, _meta = _call_runtime_status_for_upgrade(tmp_path, fixture["cfg_path"], fixture["cfg"])

    assert data["service_upgrade"]["evaluation"]["status"] == "remediated"
    assert data["service_upgrade"]["evaluation"]["runtime_failed"] is False
    assert data["summary"]["service_upgrade_status"] == "remediated"
    assert data["summary"]["service_upgrade_historical_status"] == "failed"
    assert data["summary"]["service_upgrade_runtime_failed"] is False
    assert "SERVICE_UPGRADE_REMEDIATED" in data["summary"]["warning_codes"]
    assert "SERVICE_DRIFT_REQUIRED_UNIT_MISSING" in data["summary"]["warning_codes"]
    assert "Service upgrade previously failed but current release and restart services look remediated." in warnings
    assert "Service drift detected: required maintenance units are missing: options-monitor-projection-verify.timer." in warnings


def test_runtime_status_normalizes_v_prefixed_upgrade_target(monkeypatch, tmp_path: Path) -> None:
    import src.application.agent_tool_runtime_status as runtime_status

    fixture = _runtime_status_upgrade_fixture(tmp_path, target_version="v1.2.82")

    def _service_status(profile: dict[str, Any], *, include_status: bool = False) -> dict[str, Any]:
        services_raw = profile.get("services")
        services = services_raw if isinstance(services_raw, list) else []
        return {
            "provider": profile.get("service_provider"),
            "services": [{**item, "status": "ok", "returncode": 0} for item in services if isinstance(item, dict)],
            "status_checked": include_status,
        }

    monkeypatch.setattr(runtime_status, "service_status_from_profile", _service_status)

    data, warnings, _meta = _call_runtime_status_for_upgrade(tmp_path, fixture["cfg_path"], fixture["cfg"])

    assert data["service_upgrade"]["evaluation"]["target_version"] == "1.2.82"
    assert data["summary"]["service_upgrade_status"] == "remediated"
    assert data["summary"]["service_upgrade_target_version"] == "1.2.82"
    assert "Service upgrade status still indicates an unrecovered runtime failure." not in warnings


def test_runtime_status_keeps_upgrade_failed_when_service_still_failed(monkeypatch, tmp_path: Path) -> None:
    import src.application.agent_tool_runtime_status as runtime_status

    fixture = _runtime_status_upgrade_fixture(tmp_path)

    def _service_status(profile: dict[str, Any], *, include_status: bool = False) -> dict[str, Any]:
        services_raw = profile.get("services")
        services = services_raw if isinstance(services_raw, list) else []
        out = []
        for item in services:
            if not isinstance(item, dict):
                continue
            status = "warn" if item.get("name") == "options-monitor-trade-intake.service" else "ok"
            out.append({**item, "status": status, "returncode": 3 if status == "warn" else 0})
        return {"provider": profile.get("service_provider"), "services": out, "status_checked": include_status}

    monkeypatch.setattr(runtime_status, "service_status_from_profile", _service_status)

    data, warnings, _meta = _call_runtime_status_for_upgrade(tmp_path, fixture["cfg_path"], fixture["cfg"])

    assert data["service_upgrade"]["evaluation"]["status"] == "failed"
    assert data["summary"]["service_upgrade_runtime_failed"] is True
    assert "SERVICE_UPGRADE_FAILED" in data["summary"]["warning_codes"]
    assert "SERVICE_DRIFT_REQUIRED_UNIT_MISSING" in data["summary"]["warning_codes"]
    assert "Service upgrade status still indicates an unrecovered runtime failure." in warnings
    assert "Service drift detected: required maintenance units are missing: options-monitor-projection-verify.timer." in warnings


def test_runtime_status_treats_older_failed_upgrade_as_historical(tmp_path: Path) -> None:
    fixture = _runtime_status_upgrade_fixture(tmp_path, target_version="1.2.81")

    data, warnings, _meta = _call_runtime_status_for_upgrade(tmp_path, fixture["cfg_path"], fixture["cfg"])

    assert data["service_upgrade"]["evaluation"]["status"] == "historical_failed"
    assert data["summary"]["service_upgrade_status"] == "historical_failed"
    assert data["summary"]["service_upgrade_runtime_failed"] is False
    assert "SERVICE_UPGRADE_HISTORICAL_FAILED" in data["summary"]["warning_codes"]
    assert "SERVICE_DRIFT_REQUIRED_UNIT_MISSING" in data["summary"]["warning_codes"]
    assert "Service upgrade status file contains a historical failure for a non-current target version." in warnings
    assert "Service drift detected: required maintenance units are missing: options-monitor-projection-verify.timer." in warnings


def test_runtime_status_keeps_newer_failed_upgrade_as_runtime_failure(tmp_path: Path) -> None:
    fixture = _runtime_status_upgrade_fixture(tmp_path, target_version="1.2.83")

    data, warnings, _meta = _call_runtime_status_for_upgrade(tmp_path, fixture["cfg_path"], fixture["cfg"])

    assert data["service_upgrade"]["evaluation"]["status"] == "failed"
    assert data["service_upgrade"]["evaluation"]["runtime_failed"] is True
    assert data["service_upgrade"]["evaluation"]["reason"] == "upgrade_target_version_not_active"
    assert data["summary"]["service_upgrade_status"] == "failed"
    assert data["summary"]["service_upgrade_runtime_failed"] is True
    assert "SERVICE_UPGRADE_FAILED" in data["summary"]["warning_codes"]
    assert "Service upgrade status still indicates an unrecovered runtime failure." in warnings


def test_runtime_status_can_inspect_scanned_run_after_skipped_latest(tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    def write_json(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    cfg_path = tmp_path / "config.us.json"
    cfg = _minimal_cfg()
    cfg["accounts"] = ["user1", "user2"]
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    state_dir = tmp_path / "output_shared" / "state"
    report_dir = tmp_path / "output_shared" / "reports"
    shared_state_dir = tmp_path / "output_shared" / "state"
    accounts_root = tmp_path / "output_accounts"
    runs_root = tmp_path / "output_runs"
    for path in (state_dir, report_dir, shared_state_dir, runs_root):
        path.mkdir(parents=True, exist_ok=True)
    (report_dir / "symbols_notification.txt").write_text("shared notification\n", encoding="utf-8")
    write_json(shared_state_dir / "last_run.json", {"status": "ok", "run_id": "run-skip"})

    run_scan = runs_root / "run-scan"
    run_skip = runs_root / "run-skip"
    write_json(
        run_scan / "state" / "tick_metrics.json",
        {
            "accounts": {
                "user1": {"ran_scan": True, "pipeline_ms": 1234, "reason": "force: bypass guard"},
                "user2": {"ran_scan": True, "pipeline_ms": 987, "reason": "force: bypass guard"},
            }
        },
    )
    write_json(
        run_skip / "state" / "tick_metrics.json",
        {
            "accounts": {
                "user1": {"ran_scan": False, "pipeline_ms": None, "reason": "业务运行窗口外"},
                "user2": {"ran_scan": False, "pipeline_ms": None, "reason": "业务运行窗口外"},
            }
        },
    )
    for account in ("user1", "user2"):
        write_json(run_scan / "accounts" / account / "state" / "last_run.json", {"ran_scan": True, "status": "ok"})
        write_json(run_skip / "accounts" / account / "state" / "last_run.json", {"ran_scan": False, "status": "skipped"})
        (run_scan / "accounts" / account / "symbols_notification.txt").write_text("持仓扫描结果\n", encoding="utf-8")

    write_json(
        run_scan / "accounts" / "user1" / "state" / "required_data_prefetch_summary.json",
        {
            "errors": 0,
            "cached_unique_symbols": 0,
            "deduped_count": 0,
            "skipped": 0,
            "force_refresh": True,
        },
    )
    (shared_state_dir / "last_run_dir.txt").write_text(str(run_skip), encoding="utf-8")

    payload = {
        "config_path": str(cfg_path),
        "state_dir": str(state_dir),
        "report_dir": str(report_dir),
        "shared_state_dir": str(shared_state_dir),
        "accounts_root": str(accounts_root),
        "runs_root": str(runs_root),
    }
    out = run_tool("runtime_status", payload)

    assert out["ok"] is True
    assert out["warnings"] == []
    data = out["data"]
    assert data["latest_run"]["path"].endswith("run-skip")
    assert data["latest_run_selection"]["source"] == "last_run_dir_or_mtime"
    assert data["latest_scanned_run"]["path"].endswith("run-scan")
    assert data["summary"]["latest_scanned_run_path"].endswith("run-scan")
    assert data["required_data_prefetch"]["available"] is False

    scanned_prefetch = data["latest_scanned_run_required_data_prefetch"]
    assert scanned_prefetch["available"] is True
    assert scanned_prefetch["available_account_count"] == 1
    assert scanned_prefetch["missing_account_count"] == 1
    assert scanned_prefetch["force_refresh_account_count"] == 1
    assert scanned_prefetch["shared_run_summary"] is True
    assert scanned_prefetch["shared_summary_account"] == "user1"
    assert scanned_prefetch["opend_calls_reported_account_count"] == 0
    assert scanned_prefetch["total_opend_calls"] == 0
    assert scanned_prefetch["total_cached_unique_symbols"] == 0
    assert scanned_prefetch["accounts"]["user1"]["force_refresh"] is True
    assert scanned_prefetch["accounts"]["user1"]["opend_calls_reported"] is False

    out_by_id = run_tool("runtime_status", {**payload, "run_id": "run-scan"})
    assert out_by_id["ok"] is True
    assert out_by_id["data"]["latest_run_selection"]["source"] == "run_id"
    assert out_by_id["data"]["latest_run_selection"]["found"] is True
    assert out_by_id["data"]["latest_run"]["path"].endswith("run-scan")
    assert out_by_id["data"]["required_data_prefetch"]["available"] is True

    out_by_dir = run_tool("runtime_status", {**payload, "run_dir": str(run_scan)})
    assert out_by_dir["ok"] is True
    assert out_by_dir["data"]["latest_run_selection"]["source"] == "run_dir"
    assert out_by_dir["data"]["latest_run_selection"]["found"] is True
    assert out_by_dir["data"]["latest_run"]["path"].endswith("run-scan")


def test_runtime_status_latest_scanned_run_respects_config_market(tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    def write_json(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    cfg_path = tmp_path / "config.us.json"
    cfg = _minimal_cfg()
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    report_dir = tmp_path / "output_shared" / "reports"
    shared_state_dir = tmp_path / "output_shared" / "state"
    runs_root = tmp_path / "output_runs"
    for path in (report_dir, shared_state_dir, runs_root):
        path.mkdir(parents=True, exist_ok=True)
    (report_dir / "symbols_notification.txt").write_text("shared notification\n", encoding="utf-8")
    write_json(shared_state_dir / "last_run.json", {"status": "ok", "run_id": "run-hk"})

    run_us = runs_root / "run-us"
    run_hk = runs_root / "run-hk"
    write_json(
        run_us / "state" / "tick_metrics.json",
        {
            "ran_scan": True,
            "markets_to_run": ["US"],
            "scheduler_markets": ["US"],
            "accounts": {"user1": {"ran_scan": True}},
        },
    )
    write_json(
        run_hk / "state" / "tick_metrics.json",
        {
            "ran_scan": True,
            "markets_to_run": ["HK"],
            "scheduler_markets": ["HK"],
            "accounts": {"user1": {"ran_scan": True}},
        },
    )
    os.utime(run_us, (1_000_000, 1_000_000))
    os.utime(run_hk, (2_000_000, 2_000_000))

    out = run_tool(
        "runtime_status",
        {
            "config_key": "us",
            "config_path": str(cfg_path),
            "report_dir": str(report_dir),
            "shared_state_dir": str(shared_state_dir),
            "runs_root": str(runs_root),
        },
    )

    assert out["ok"] is True
    assert out["data"]["latest_run"]["path"].endswith("run-us")
    assert out["data"]["latest_run_selection"]["market_filter"] == "US"
    assert out["data"]["latest_run_selection"]["skipped_market_mismatch_count"] == 1
    selection = out["data"]["latest_scanned_run_selection"]
    assert out["data"]["latest_scanned_run"]["path"].endswith("run-us")
    assert selection["market_filter"] == "US"
    assert selection["skipped_market_mismatch_count"] == 1


def test_runtime_status_does_not_warn_missing_notification_for_expected_skip(tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    def write_json(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(json.dumps(_minimal_cfg(), ensure_ascii=False, indent=2), encoding="utf-8")
    shared_state_dir = tmp_path / "output_shared" / "state"
    runs_root = tmp_path / "output_runs"
    run_skip = runs_root / "run-skip"
    shared_state_dir.mkdir(parents=True)
    write_json(shared_state_dir / "last_run.json", {"status": "skipped", "run_id": "run-skip"})
    write_json(
        run_skip / "state" / "tick_metrics.json",
        {
            "ran_scan": False,
            "scheduler_decision": {
                "should_run_scan": False,
                "should_notify": False,
                "is_notify_window_open": False,
                "reason": "业务运行窗口内，当前没有待执行运行点。",
            },
            "accounts": [{"account": "user1", "status": "skipped", "ran_scan": False}],
        },
    )
    write_json(run_skip / "accounts" / "user1" / "state" / "last_run.json", {"status": "skipped", "ran_scan": False})
    (shared_state_dir / "last_run_dir.txt").write_text(str(run_skip), encoding="utf-8")

    out = run_tool(
        "runtime_status",
        {
            "config_key": "us",
            "config_path": str(cfg_path),
            "shared_state_dir": str(shared_state_dir),
            "runs_root": str(runs_root),
            "report_dir": str(tmp_path / "output_shared" / "reports"),
            "accounts_root": str(tmp_path / "output_accounts"),
        },
    )

    assert out["ok"] is True
    assert out["warnings"] == []
    assert out["data"]["summary"]["ok"] is True
    assert out["data"]["latest_run"]["path"].endswith("run-skip")


def test_runtime_status_notification_diagnosis_uses_shared_last_run_counts(tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    def write_json(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(json.dumps(_minimal_cfg(), ensure_ascii=False, indent=2), encoding="utf-8")
    shared_state_dir = tmp_path / "output_shared" / "state"
    runs_root = tmp_path / "output_runs"
    latest_run = runs_root / "run-audit-only"
    write_json(
        shared_state_dir / "last_run.json",
        {
            "sent": True,
            "sent_accounts": ["user1", "user2"],
            "notify_summary": {
                "account_messages_count": 2,
                "send_attempted_count": 2,
                "send_confirmed_count": 2,
                "send_failed_count": 0,
            },
        },
    )
    write_json(latest_run / "state" / "audit_events.json", {"status": "ok"})
    (shared_state_dir / "last_run_dir.txt").write_text(str(latest_run), encoding="utf-8")

    out = run_tool(
        "runtime_status",
        {
            "config_key": "us",
            "config_path": str(cfg_path),
            "shared_state_dir": str(shared_state_dir),
            "runs_root": str(runs_root),
            "report_dir": str(tmp_path / "output_shared" / "reports"),
            "accounts_root": str(tmp_path / "output_accounts"),
        },
    )

    diagnosis = out["data"]["notification_diagnosis"]
    assert diagnosis["status"] == "sent"
    assert diagnosis["account_messages_count"] == 2
    assert diagnosis["send_attempted_count"] == 2
    assert diagnosis["send_confirmed_count"] == 2


def test_runtime_status_loads_openclaw_profile_and_masks_external_paths(tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(json.dumps(_minimal_cfg(), ensure_ascii=False, indent=2), encoding="utf-8")
    report_dir = tmp_path / "reports"
    shared_state_dir = tmp_path / "state"
    accounts_root = tmp_path / "accounts"
    runs_root = tmp_path / "runs"
    for path in (report_dir, shared_state_dir, accounts_root / "user1" / "state", accounts_root / "user1" / "reports", runs_root):
        path.mkdir(parents=True, exist_ok=True)
    (shared_state_dir / "last_run.json").write_text(json.dumps({"status": "ok"}), encoding="utf-8")
    (accounts_root / "user1" / "state" / "last_run.json").write_text(json.dumps({"status": "account_ok"}), encoding="utf-8")
    (accounts_root / "user1" / "reports" / "symbols_notification.txt").write_text("account notification\n", encoding="utf-8")

    profile_path = tmp_path / "openclaw.profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "config_path": str(cfg_path),
                "accounts": ["user1"],
                "paths": {
                    "report_dir": str(report_dir),
                    "shared_state_dir": str(shared_state_dir),
                    "accounts_root": str(accounts_root),
                    "runs_root": str(runs_root),
                },
                "trigger_source": "om_direct",
                "trigger_job_id": "hk-direct-11",
                "delivery": {"mode": "none"},
                "timeoutSeconds": 700,
                "max_run_age_minutes": 30,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    out = run_tool("runtime_status", {"profile_path": str(profile_path)})

    assert out["ok"] is True
    assert out["warnings"] == ["Outer delivery.mode is none; the task runner will not announce run output."]
    assert out["data"]["openclaw_profile"]["loaded"] is True
    assert out["data"]["trigger_context"]["source"] == "om_direct"
    assert out["data"]["trigger_context"]["job_id"] == "hk-direct-11"
    assert out["data"]["trigger_context"]["delivery_mode"] == "none"
    assert out["data"]["trigger_context"]["announce_expected"] is False
    assert out["data"]["trigger_context"]["timeout_seconds"] == 700
    assert out["data"]["config"]["config_path"] == ".../config.us.json"
    assert out["data"]["paths"]["report_dir"] == ".../reports"
    assert out["data"]["account_summary"]["accounts"]["user1"]["last_status"] == "account_ok"
    assert out["data"]["freshness"]["status"] == "fresh"


def test_runtime_runs_agent_tool_lists_and_selects_runs(tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    runs_root = tmp_path / "output_runs"
    run_dir = runs_root / "run-1"
    (run_dir / "state").mkdir(parents=True, exist_ok=True)
    (run_dir / "state" / "tick_metrics.json").write_text(
        json.dumps(
            {
                "ran_scan": True,
                "sent": True,
                "accounts": [{"account": "lx", "ran_scan": True}],
                "reason": "sent",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    listed = run_tool("runtime_runs", {"runs_root": str(runs_root), "limit": 5})
    selected = run_tool("runtime_runs", {"runs_root": str(runs_root), "run_id": "run-1"})

    assert listed["ok"] is True
    assert listed["data"]["schema_version"] == "runtime_runs.v1"
    assert listed["data"]["summary"]["total_count"] == 1
    assert listed["data"]["runs"][0]["run_id"] == "run-1"
    assert listed["data"]["runs"][0]["ran_scan"] is True
    assert listed["meta"]["runs_root"] == ".../output_runs"
    assert selected["ok"] is True
    assert selected["data"]["summary"]["requested_found"] is True
    assert selected["data"]["selected_run"]["run_id"] == "run-1"


def test_runtime_logs_agent_tool_tails_run_audit_and_file(tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    runs_root = tmp_path / "output_runs"
    audit = runs_root / "run-1" / "state" / "audit_events.jsonl"
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text('{"message":"first"}\n{"message":"second"}\n', encoding="utf-8")
    service_log = tmp_path / "service.log"
    service_log.write_text("one\ntwo\nthree\n", encoding="utf-8")

    audit_out = run_tool(
        "runtime_logs",
        {"runs_root": str(runs_root), "run_id": "run-1", "kind": "audit", "lines": 1},
    )
    file_out = run_tool("runtime_logs", {"log_file": str(service_log), "lines": 2})

    assert audit_out["ok"] is True
    assert audit_out["data"]["schema_version"] == "runtime_logs.v1"
    assert audit_out["data"]["summary"]["requested_run_found"] is True
    assert audit_out["data"]["files"][0]["path"].endswith("audit_events.jsonl")
    assert audit_out["data"]["files"][0]["tail"] == ['{"message":"second"}']
    assert audit_out["meta"]["runs_root"] == ".../output_runs"
    assert file_out["ok"] is True
    assert file_out["data"]["files"][0]["tail"] == ["two", "three"]


def test_runtime_logs_rejects_removed_file_alias(tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    service_log = tmp_path / "service.log"
    service_log.write_text("one\n", encoding="utf-8")

    out = run_tool("runtime_logs", {"file": str(service_log), "lines": 1})

    assert out["ok"] is False
    assert out["error"]["code"] == "INPUT_ERROR"
    assert "log_file" in out["error"]["message"]


def test_openclaw_readiness_combines_status_and_healthcheck(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (tmp_path / "portfolio.runtime.json").write_text(
        json.dumps({"option_positions": {"sqlite_path": "output_shared/state/option_positions.sqlite3"}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cfg_path.write_text(
        json.dumps(_public_cfg_with_futu("portfolio.runtime.json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    shared_state_dir = tmp_path / "output_shared" / "state"
    report_dir = tmp_path / "output_shared" / "reports"
    accounts_root = tmp_path / "output_accounts"
    for path in (shared_state_dir, report_dir, accounts_root / "user1" / "reports"):
        path.mkdir(parents=True, exist_ok=True)
    (shared_state_dir / "last_run.json").write_text(json.dumps({"status": "ok"}), encoding="utf-8")
    (report_dir / "symbols_notification.txt").write_text("ready\n", encoding="utf-8")

    _patch_healthcheck_context(monkeypatch)

    out = run_tool(
        "openclaw_readiness",
        {
            "config_path": str(cfg_path),
            "shared_state_dir": str(shared_state_dir),
            "report_dir": str(report_dir),
            "accounts_root": str(accounts_root),
        },
    )

    assert out["ok"] is True
    assert out["data"]["summary"]["ready"] is True
    checks = {item["name"]: item for item in out["data"]["checks"]}
    assert checks["runtime_status"]["status"] == "ok"
    assert checks["healthcheck"]["status"] == "warn"
    assert checks["openclaw_binary"]["status"] in {"ok", "warn"}


def test_openclaw_readiness_reports_profile_cron_notification_and_next_actions(tmp_path: Path) -> None:
    from src.application.agent_tool_openclaw import openclaw_readiness_tool

    def _runtime_status(_payload):
        return (
            {
                "config": {"config_path": ".../config.us.json"},
                "summary": {"ok": True},
                "freshness": {"status": "fresh", "stale": False},
            },
            [],
            {},
        )

    def _healthcheck(_payload):
        return ({"summary": {"ok": True}}, [], {})

    def _load_runtime_config(**_kwargs):
        return (
            tmp_path / "config.us.json",
            {"notifications": {"channel": "wechat_clawbot", "target": "clawbot:test-room"}},
        )

    class _Proc:
        def __init__(self, stdout: str):
            self.returncode = 0
            self.stdout = stdout
            self.stderr = ""

    def _run_cmd(cmd, **_kwargs):
        if cmd[-1] == "list":
            return _Proc("job-1 options-monitor auto tick enabled")
        return _Proc("last run ok")

    data, warnings, meta = openclaw_readiness_tool(
        {
            "config_key": "us",
            "cron_jobs": [{"id": "job-1", "name": "options-monitor auto tick"}],
            "include_cron_status": True,
        },
        runtime_status_tool_fn=_runtime_status,
        healthcheck_tool_fn=_healthcheck,
        load_runtime_config=_load_runtime_config,
        repo_base=lambda: tmp_path,
        which=lambda _name: "/usr/local/bin/openclaw",
        run_cmd=_run_cmd,
    )

    checks = {item["name"]: item for item in data["checks"]}
    assert warnings == []
    assert meta["config_path"] == ".../config.us.json"
    assert checks["openclaw_binary"]["value"]["path"] == ".../openclaw"
    assert checks["openclaw_cron"]["status"] == "ok"
    assert checks["openclaw_cron"]["value"]["configured_jobs"][0]["found"] is True
    assert checks["notification_route"]["status"] == "ok"
    assert checks["notification_route"]["value"]["transport_channel"] == "wechat_clawbot"
    assert data["next_actions"]["safe_next_actions"][0]["action"] == "no_read_only_followup_needed"


def test_openclaw_readiness_next_actions_preserve_profile_path(tmp_path: Path) -> None:
    from src.application.agent_tool_openclaw import openclaw_readiness_tool

    profile_path = tmp_path / "openclaw.profile.json"
    profile_path.write_text(json.dumps({"config_key": "hk", "accounts": ["lx"]}), encoding="utf-8")

    def _runtime_status(_payload):
        return (
            {
                "config": {"config_path": ".../config.hk.json"},
                "summary": {"ok": False},
                "freshness": {"status": "stale", "stale": True},
            },
            ["runtime output is missing"],
            {},
        )

    def _healthcheck(_payload):
        return ({"summary": {"ok": True}}, [], {})

    data, warnings, _meta = openclaw_readiness_tool(
        {"profile_path": str(profile_path)},
        runtime_status_tool_fn=_runtime_status,
        healthcheck_tool_fn=_healthcheck,
        repo_base=lambda: tmp_path,
        which=lambda _name: None,
    )

    safe_actions = data["next_actions"]["safe_next_actions"]
    inspect_action = next(item for item in safe_actions if item["action"] == "inspect_runtime_status")
    input_json = json.loads(inspect_action["command"][-1])
    assert input_json == {"profile_path": str(profile_path), "config_key": "hk"}
    assert data["next_actions"]["blocked_actions"][0]["command"] == [
        "./om",
        "run",
        "tick",
        "--config",
        "config.hk.json",
        "--accounts",
        "lx",
    ]
    assert "runtime output is missing" in warnings


def test_close_advice_reads_cached_context_and_required_data(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    cfg = _minimal_cfg()
    cfg["close_advice"] = {"enabled": True}
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    out_root = tmp_path / "output_shared" / "agent_tools"
    state_dir = out_root / "state"
    required_dir = out_root / "required_data"
    state_dir.mkdir(parents=True)
    required_dir.mkdir(parents=True)
    (state_dir / "option_positions_context.json").write_text(
        json.dumps({"open_positions_min": []}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    reports_dir = out_root / "reports"
    reports_dir.mkdir(parents=True)
    (reports_dir / "close_advice.csv").write_text("account,symbol,tier,tier_label,realized_if_close\n", encoding="utf-8")
    (reports_dir / "close_advice.txt").write_text("", encoding="utf-8")

    def _fake_run_close_advice(**kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["context_path"] == (state_dir / "option_positions_context.json")
        assert kwargs["required_data_root"] == required_dir
        assert kwargs["output_dir"] == (out_root / "reports")
        return {
            "enabled": True,
            "rows": 0,
            "notify_rows": 0,
            "csv": str((out_root / "reports" / "close_advice.csv")),
            "text": str((out_root / "reports" / "close_advice.txt")),
        }

    _patch_agent_tool_context(monkeypatch, run_close_advice=_fake_run_close_advice)
    out = run_tool("close_advice", {"config_path": str(cfg_path), "output_dir": str(out_root)})

    assert out["ok"] is True
    assert out["data"]["enabled"] is True
    assert out["data"]["summary"]["row_count"] == 0
    assert out["data"]["top_rows"] == []
    assert out["meta"]["context_path"] == ".../option_positions_context.json"
    assert out["meta"]["required_data_root"] == ".../required_data"


def test_close_advice_read_filters_existing_run_report(tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(json.dumps(_minimal_cfg(), ensure_ascii=False, indent=2), encoding="utf-8")
    report_dir = tmp_path / "output_runs" / "run-1" / "accounts" / "lx"
    report_dir.mkdir(parents=True)
    (report_dir / "config.override.json").write_text(
        json.dumps(_minimal_cfg(market="us"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    pd.DataFrame(
        [
            {
                "account": "lx",
                "symbol": "9992",
                "option_type": "call",
                "position_side": "buy",
                "leg_role": "enhancement_call",
                "expiration": "2026-08-21",
                "strike": 152.45,
                "evaluation_status": "priced",
                "close_action": "hold_call_as_convexity",
                "tier": "none",
                "tier_label": "继续持有",
                "reason": "long call 仍保留右尾 convexity，可继续持有",
                "realized_if_close": 320,
                "long_call_cost_basis": 500,
                "long_call_current_value": 820,
                "long_call_value_ratio": 1.64,
                "capture_ratio": 0.42,
            },
            {
                "account": "lx",
                "symbol": "FUTU",
                "option_type": "put",
                "position_side": "short",
                "expiration": "2026-06-19",
                "strike": 100,
                "evaluation_status": "priced",
                "close_action": "hold",
                "tier": "none",
                "tier_label": "继续持有",
                "reason": "short-vol thesis intact",
            },
        ]
    ).to_csv(report_dir / "close_advice.csv", index=False)

    out = run_tool(
        "close_advice_read",
        {
            "config_path": str(cfg_path),
            "runs_root": str(tmp_path / "output_runs"),
            "run_id": "run-1",
            "query": {"symbol": "9992.HK", "option_type": "call", "side": "long", "status": "open"},
        },
    )

    assert out["ok"] is True
    assert out["data"]["row_count"] == 2
    assert out["data"]["matched_count"] == 1
    assert out["data"]["source"]["run_id"] == "run-1"
    row = out["data"]["rows"][0]
    assert row["symbol"] == "9992.HK"
    assert row["side"] == "long"
    assert row["close_action"] == "hold_call_as_convexity"
    assert row["realized_if_close"] == 320
    assert row["long_call_cost_basis"] == 500
    assert row["long_call_current_value"] == 820
    assert row["long_call_value_ratio"] == 1.64


def test_close_advice_read_uses_symbol_market_over_default_config(tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(json.dumps(_minimal_cfg(market="us"), ensure_ascii=False, indent=2), encoding="utf-8")
    runs_root = tmp_path / "output_runs"

    us_report = runs_root / "run-us" / "accounts" / "lx"
    us_report.mkdir(parents=True)
    (us_report / "config.override.json").write_text(
        json.dumps(_minimal_cfg(market="us"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    pd.DataFrame(
        [
            {
                "account": "lx",
                "symbol": "FUTU",
                "option_type": "call",
                "position_side": "short",
                "expiration": "2026-08-21",
                "strike": 152.45,
                "evaluation_status": "priced",
                "tier": "none",
            },
        ]
    ).to_csv(us_report / "close_advice.csv", index=False)

    hk_report = runs_root / "run-hk" / "accounts" / "sy"
    hk_report.mkdir(parents=True)
    (hk_report / "config.override.json").write_text(
        json.dumps(_minimal_cfg(market="hk"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    pd.DataFrame(
        [
            {
                "account": "sy",
                "symbol": "9992.HK",
                "option_type": "call",
                "position_side": "long",
                "expiration": "2026-07-30",
                "strike": 172.5,
                "evaluation_status": "priced",
                "close_action": "hold_call",
                "tier": "none",
            },
        ]
    ).to_csv(hk_report / "close_advice.csv", index=False)

    out = run_tool(
        "close_advice_read",
        {
            "config_path": str(cfg_path),
            "runs_root": str(runs_root),
            "query": {"symbol": "9992.HK", "option_type": "call", "side": "long", "status": "open"},
        },
    )

    assert out["ok"] is True
    assert out["data"]["matched_count"] == 1
    assert out["data"]["source"]["run_id"] == "run-hk"
    assert out["data"]["rows"][0]["account"] == "sy"
    assert out["meta"]["market_filter"] == "HK"
    assert out["meta"]["market_filter_source"] == "query_symbol"
    assert out["meta"]["config_market_filter"] == "US"


def test_close_advice_read_all_market_scope_reads_recent_runs_and_recovers_side_from_context(tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(json.dumps(_minimal_cfg(market="us"), ensure_ascii=False, indent=2), encoding="utf-8")
    runs_root = tmp_path / "output_runs"

    us_report = runs_root / "run-us" / "accounts" / "lx"
    us_report.mkdir(parents=True)
    (us_report / "config.override.json").write_text(
        json.dumps(_minimal_cfg(market="us"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    pd.DataFrame(
        [
            {
                "account": "lx",
                "symbol": "FUTU",
                "option_type": "call",
                "position_side": "short",
                "expiration": "2026-08-21",
                "strike": 152.45,
                "evaluation_status": "priced",
                "tier": "none",
            },
        ]
    ).to_csv(us_report / "close_advice.csv", index=False)

    hk_report = runs_root / "run-hk" / "accounts" / "sy"
    (hk_report / "state").mkdir(parents=True)
    (hk_report / "config.override.json").write_text(
        json.dumps(_minimal_cfg(market="hk"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (hk_report / "state" / "option_positions_context.json").write_text(
        json.dumps(
            {
                "open_positions_min": [
                    {
                        "account": "sy",
                        "symbol": "9992.HK",
                        "option_type": "call",
                        "side": "long",
                        "expiration": 1785369600000,
                        "strike": 172.5,
                        "contracts_open": 1,
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    pd.DataFrame(
        [
            {
                "account": "sy",
                "symbol": "9992.HK",
                "option_type": "call",
                "expiration": "2026-07-30",
                "strike": 172.5,
                "evaluation_status": "priced",
                "close_action": "hold_call",
                "tier": "none",
            },
        ]
    ).to_csv(hk_report / "close_advice.csv", index=False)

    out = run_tool(
        "close_advice_read",
        {
            "config_path": str(cfg_path),
            "market_scope": "all",
            "runs_root": str(runs_root),
            "query": {"option_type": "call", "side": "long", "status": "open"},
        },
    )

    assert out["ok"] is True
    assert out["data"]["matched_count"] == 1
    assert out["data"]["source"]["run_ids"] == ["run-hk", "run-us"]
    assert "fallback" not in out["data"]
    assert out["meta"]["market_scope"] == "all"
    assert out["data"]["rows"][0]["side"] == "long"


def test_close_advice_read_respects_config_market_when_selecting_latest_run(tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(json.dumps(_minimal_cfg(market="us"), ensure_ascii=False, indent=2), encoding="utf-8")
    runs_root = tmp_path / "output_runs"
    run_us = runs_root / "run-us" / "accounts" / "lx"
    run_hk = runs_root / "run-hk" / "accounts" / "lx"
    for account_dir, market, symbol, realized in (
        (run_us, "us", "NVDA", 100),
        (run_hk, "hk", "0700.HK", 999),
    ):
        account_dir.mkdir(parents=True)
        (account_dir / "config.override.json").write_text(
            json.dumps(_minimal_cfg(market=market), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        pd.DataFrame(
            [
                {
                    "account": "lx",
                    "symbol": symbol,
                    "option_type": "call",
                    "position_side": "long",
                    "expiration": "2026-08-21",
                    "strike": 100,
                    "evaluation_status": "priced",
                    "close_action": "hold_call",
                    "tier": "none",
                    "tier_label": "继续持有",
                    "realized_if_close": realized,
                }
            ]
        ).to_csv(account_dir / "close_advice.csv", index=False)

    os.utime(runs_root / "run-us", (1_000_000, 1_000_000))
    os.utime(runs_root / "run-hk", (2_000_000, 2_000_000))

    out = run_tool(
        "close_advice_read",
        {
            "config_key": "us",
            "config_path": str(cfg_path),
            "runs_root": str(runs_root),
            "query": {"option_type": "call", "side": "long"},
        },
    )

    assert out["ok"] is True
    assert out["data"]["source"]["run_id"] == "run-us"
    assert out["meta"]["market_filter"] == "US"
    assert out["data"]["row_count"] == 1
    assert out["data"]["rows"][0]["symbol"] == "NVDA"
    assert out["data"]["rows"][0]["realized_if_close"] == 100


def test_close_advice_read_default_agent_report_prefers_runtime_root(monkeypatch, tmp_path: Path) -> None:
    from src.application.agent_tool_close_advice_read import close_advice_read_tool

    release_root = tmp_path / "release"
    runtime_root = tmp_path / "runtime"
    cfg_path = release_root / "config.us.json"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(json.dumps(_minimal_cfg(market="us"), ensure_ascii=False, indent=2), encoding="utf-8")

    for root, symbol, realized in (
        (runtime_root, "NVDA", 100),
        (release_root, "PDD", 999),
    ):
        report_dir = root / "output_shared" / "agent_tools" / "reports"
        report_dir.mkdir(parents=True)
        pd.DataFrame(
            [
                {
                    "account": "lx",
                    "symbol": symbol,
                    "option_type": "call",
                    "position_side": "long",
                    "expiration": "2026-08-21",
                    "strike": 100,
                    "evaluation_status": "priced",
                    "close_action": "hold_call",
                    "tier": "none",
                    "tier_label": "继续持有",
                    "realized_if_close": realized,
                }
            ]
        ).to_csv(report_dir / "close_advice.csv", index=False)

    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime_root))

    data, warnings, meta = close_advice_read_tool(
        {"config_key": "us", "query": {"option_type": "call", "side": "long"}},
        load_runtime_config=lambda **_kwargs: (cfg_path, _minimal_cfg(market="us")),
        resolve_output_root=lambda _output_dir=None: release_root / "output_shared" / "agent_tools",
        repo_base=lambda: release_root,
        mask_path=lambda path: f".../{Path(path).name}",
    )

    assert warnings == []
    assert meta["market_filter"] == "US"
    assert data["row_count"] == 1
    assert data["source"]["type"] == "agent_tool"
    assert data["rows"][0]["symbol"] == "NVDA"
    assert data["rows"][0]["realized_if_close"] == 100


def test_close_advice_summary_uses_domain_tier_order_for_optional(tmp_path: Path) -> None:
    from src.infrastructure.io_utils import safe_read_csv
    from src.application.agent_tool_runtime import as_float
    from src.application.agent_tool_scan import close_advice_rows_summary

    csv_path = tmp_path / "close_advice.csv"
    text_path = tmp_path / "close_advice.txt"
    pd.DataFrame(
        [
            {"account": "lx", "symbol": "WEAK", "tier": "weak", "tier_label": "可观察平仓", "evaluation_status": "priced", "realized_if_close": 300},
            {"account": "lx", "symbol": "OPT", "tier": "optional", "tier_label": "低价买回可选", "evaluation_status": "priced", "realized_if_close": 100},
            {"account": "lx", "symbol": "MED", "tier": "medium", "tier_label": "建议平仓", "evaluation_status": "priced", "realized_if_close": 200},
        ]
    ).to_csv(csv_path, index=False)
    text_path.write_text("", encoding="utf-8")

    summary = close_advice_rows_summary(csv_path, text_path, safe_read_csv=safe_read_csv, as_float=as_float)

    assert [row["tier"] for row in summary["top_rows"]] == ["medium", "optional", "weak"]


def test_scan_summary_rows_normalizes_account_labels() -> None:
    from src.application.agent_tool_scan import scan_summary_rows

    summary = scan_summary_rows(
        [
            {"account": " LX ", "symbol": "NVDA", "side": "sell_put", "net_income": 100},
            {"account_label": "lx", "symbol": "TSLA", "side": "sell_call", "net_income": 50},
        ],
        as_float=lambda value: float(value) if value not in (None, "") else None,
    )

    assert summary["account_counts"] == {"lx": 2}
    assert [item["account"] for item in summary["top_candidates"]] == ["lx", "lx"]


def test_close_advice_requires_cached_inputs(tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    cfg = _minimal_cfg()
    cfg["close_advice"] = {"enabled": True}
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    out = run_tool("close_advice", {"config_path": str(cfg_path), "output_dir": str(tmp_path / "output_shared" / "agent_tools")})

    assert out["ok"] is False
    assert out["error"]["code"] == "DEPENDENCY_MISSING"


def test_prepare_close_advice_inputs_builds_context_and_required_data(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (tmp_path / "portfolio.runtime.json").write_text(
        json.dumps({"option_positions": {"sqlite_path": "output_shared/state/option_positions.sqlite3"}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cfg = _public_cfg_with_futu("portfolio.runtime.json")
    cfg["close_advice"] = {"enabled": True}
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    def _fake_load_option_positions_context(**kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["account"] == "user1"
        return ({
            "open_positions_min": [
                {"symbol": "NVDA", "option_type": "put", "strike": 100, "expiration": "2026-06-19"},
                {"symbol": "NVDA", "option_type": "call", "strike": 120, "expiration": "2026-07-17"},
            ]
        }, True)

    def _fake_fetch_symbol_opend(symbol, **kwargs):  # type: ignore[no-untyped-def]
        assert symbol == "NVDA"
        assert kwargs["explicit_expirations"] == ["2026-06-19", "2026-07-17"]
        assert kwargs["option_types"] == "call,put"
        assert kwargs["min_strike"] == 100
        assert kwargs["max_strike"] == 120
        return {"rows": [{"symbol": "NVDA"}], "expiration_count": 2}

    def _fake_save_required_data_opend(base, symbol, payload, *, output_root):  # type: ignore[no-untyped-def]
        parsed = output_root / "parsed"
        parsed.mkdir(parents=True, exist_ok=True)
        csv_path = parsed / f"{symbol}_required_data.csv"
        csv_path.write_text(
            "symbol,option_type,expiration,strike\n"
            "NVDA,put,2026-06-19,100\n"
            "NVDA,call,2026-07-17,120\n",
            encoding="utf-8",
        )
        return output_root / "raw" / f"{symbol}_required_data.json", csv_path

    _patch_agent_tool_context(
        monkeypatch,
        load_option_positions_context=_fake_load_option_positions_context,
        fetch_symbol_opend=_fake_fetch_symbol_opend,
        save_required_data_opend=_fake_save_required_data_opend,
    )
    out = run_tool("prepare_close_advice_inputs", {"config_path": str(cfg_path), "output_dir": str(tmp_path / "output_shared" / "agent_tools")})

    assert out["ok"] is True
    assert out["data"]["account"] == "user1"
    assert out["data"]["symbol_count"] == 1
    assert out["data"]["symbols"][0]["symbol"] == "NVDA"
    assert out["data"]["symbols"][0]["position_coverage_ok"] is True
    assert out["data"]["coverage_summary"]["covered_symbol_count"] == 1
    assert out["meta"]["required_data_root"] == ".../required_data"


def test_prepare_close_advice_inputs_reuses_cached_required_data_when_coverage_is_complete(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (tmp_path / "portfolio.runtime.json").write_text(
        json.dumps({"option_positions": {"sqlite_path": "output_shared/state/option_positions.sqlite3"}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cfg = _public_cfg_with_futu("portfolio.runtime.json")
    cfg["close_advice"] = {"enabled": True}
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    required_root = (tmp_path / "output_shared" / "agent_tools" / "required_data" / "parsed")
    required_root.mkdir(parents=True, exist_ok=True)
    (required_root / "NVDA_required_data.csv").write_text(
        "symbol,option_type,expiration,strike\n"
        "NVDA,put,2026-06-19,100\n"
        "NVDA,call,2026-07-17,120\n",
        encoding="utf-8",
    )

    def _fake_load_option_positions_context(**kwargs):  # type: ignore[no-untyped-def]
        return ({
            "open_positions_min": [
                {"symbol": "NVDA", "option_type": "put", "strike": 100, "expiration": "2026-06-19"},
                {"symbol": "NVDA", "option_type": "call", "strike": 120, "expiration": "2026-07-17"},
            ]
        }, True)

    def _fail_fetch_symbol_opend(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("fetch_symbol_opend should not be called when cached coverage is complete")

    def _fail_save_required_data_opend(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("save_required_data_opend should not be called when cached coverage is complete")

    _patch_agent_tool_context(
        monkeypatch,
        load_option_positions_context=_fake_load_option_positions_context,
        fetch_symbol_opend=_fail_fetch_symbol_opend,
        save_required_data_opend=_fail_save_required_data_opend,
    )
    out = run_tool(
        "prepare_close_advice_inputs",
        {"config_path": str(cfg_path), "output_dir": str(tmp_path / "output_shared" / "agent_tools")},
    )

    assert out["ok"] is True
    assert out["data"]["symbols"][0]["position_coverage_ok"] is True
    assert out["data"]["symbols"][0]["rows"] == 2
    assert out["data"]["symbols"][0]["expiration_count"] == 2


def test_prepare_close_advice_inputs_reports_missing_required_expirations(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (tmp_path / "portfolio.runtime.json").write_text(
        json.dumps({"option_positions": {"sqlite_path": "output_shared/state/option_positions.sqlite3"}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cfg = _public_cfg_with_futu("portfolio.runtime.json")
    cfg["symbols"][0]["symbol"] = "9992.HK"
    cfg["symbols"][0]["fetch"]["limit_expirations"] = 1
    cfg["close_advice"] = {"enabled": True}
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    def _fake_load_option_positions_context(**kwargs):  # type: ignore[no-untyped-def]
        return ({
            "open_positions_min": [
                {"symbol": "9992.HK", "option_type": "put", "strike": 135, "expiration": "2026-04-29"},
                {"symbol": "9992.HK", "option_type": "call", "strike": 200, "expiration": "2026-06-29"},
            ]
        }, True)

    def _fake_fetch_symbol_opend(symbol, **kwargs):  # type: ignore[no-untyped-def]
        assert symbol == "9992.HK"
        assert kwargs["explicit_expirations"] == ["2026-04-29", "2026-06-29"]
        return {"rows": [{"symbol": "9992.HK"}], "expiration_count": 1}

    def _fake_save_required_data_opend(base, symbol, payload, *, output_root):  # type: ignore[no-untyped-def]
        parsed = output_root / "parsed"
        parsed.mkdir(parents=True, exist_ok=True)
        csv_path = parsed / f"{symbol}_required_data.csv"
        csv_path.write_text(
            "symbol,option_type,expiration,strike\n"
            "9992.HK,put,2026-05-28,135\n",
            encoding="utf-8",
        )
        return output_root / "raw" / f"{symbol}_required_data.json", csv_path

    _patch_agent_tool_context(
        monkeypatch,
        load_option_positions_context=_fake_load_option_positions_context,
        fetch_symbol_opend=_fake_fetch_symbol_opend,
        save_required_data_opend=_fake_save_required_data_opend,
    )
    out = run_tool("prepare_close_advice_inputs", {"config_path": str(cfg_path), "output_dir": str(tmp_path / "output_shared" / "agent_tools")})

    assert out["ok"] is True
    assert out["data"]["symbols"][0]["missing_expirations"] == ["2026-04-29", "2026-06-29"]
    assert out["data"]["symbols"][0]["position_coverage_ok"] is False
    assert out["data"]["coverage_summary"]["positions_missing_coverage"] == 2
    assert "missing required expirations" in out["warnings"][0]


def test_prepare_close_advice_inputs_reports_expiration_near_miss_without_silent_rewrite(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (tmp_path / "portfolio.runtime.json").write_text(
        json.dumps({"option_positions": {"sqlite_path": "output_shared/state/option_positions.sqlite3"}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cfg = _public_cfg_with_futu("portfolio.runtime.json")
    cfg["symbols"][0]["symbol"] = "0700.HK"
    cfg["close_advice"] = {"enabled": True}
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    def _fake_load_option_positions_context(**kwargs):  # type: ignore[no-untyped-def]
        return ({
            "open_positions_min": [
                {"symbol": "0700.HK", "option_type": "put", "strike": 450, "expiration": "2026-05-27"},
            ]
        }, True)

    def _fake_fetch_symbol_opend(symbol, **kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["chain_cache_force_refresh"] is True
        return {"rows": [{"symbol": "0700.HK"}], "expiration_count": 1}

    def _fake_save_required_data_opend(base, symbol, payload, *, output_root):  # type: ignore[no-untyped-def]
        parsed = output_root / "parsed"
        parsed.mkdir(parents=True, exist_ok=True)
        csv_path = parsed / f"{symbol}_required_data.csv"
        csv_path.write_text(
            "symbol,option_type,expiration,strike\n"
            "0700.HK,put,2026-05-28,450\n",
            encoding="utf-8",
        )
        return output_root / "raw" / f"{symbol}_required_data.json", csv_path

    _patch_agent_tool_context(
        monkeypatch,
        load_option_positions_context=_fake_load_option_positions_context,
        fetch_symbol_opend=_fake_fetch_symbol_opend,
        save_required_data_opend=_fake_save_required_data_opend,
    )
    out = run_tool(
        "prepare_close_advice_inputs",
        {
            "config_path": str(cfg_path),
            "output_dir": str(tmp_path / "output_shared" / "agent_tools"),
            "force_required_data_refresh": True,
        },
    )

    assert out["ok"] is True
    assert out["data"]["symbols"][0]["position_coverage_ok"] is False
    assert out["data"]["symbols"][0]["missing_expirations"] == ["2026-05-27"]
    assert out["data"]["symbols"][0]["expiration_near_misses"] == [
        {
            "symbol": "0700.HK",
            "option_type": "put",
            "strike": 450.0,
            "requested_expiration": "2026-05-27",
            "matched_expiration": "2026-05-28",
            "quote_key": "0700.HK|put|2026-05-27|450.000000",
        }
    ]
    assert out["data"]["coverage_summary"]["expiration_near_miss_count"] == 1
    assert any("expiration near miss 2026-05-27 -> 2026-05-28" in item for item in out["warnings"])


def test_prepare_close_advice_inputs_normalizes_timestamp_expirations(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (tmp_path / "portfolio.runtime.json").write_text(
        json.dumps({"option_positions": {"sqlite_path": "output_shared/state/option_positions.sqlite3"}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cfg = _public_cfg_with_futu("portfolio.runtime.json")
    cfg["symbols"][0]["symbol"] = "FUTU"
    cfg["close_advice"] = {"enabled": True}
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    def _fake_load_option_positions_context(**kwargs):  # type: ignore[no-untyped-def]
        return ({
            "open_positions_min": [
                {"symbol": "FUTU", "option_type": "put", "strike": 120, "expiration": 1777420800000},
                {"symbol": "FUTU", "option_type": "call", "strike": 130, "expiration": 1781740800},
            ]
        }, True)

    def _fake_fetch_symbol_opend(symbol, **kwargs):  # type: ignore[no-untyped-def]
        assert symbol == "FUTU"
        assert kwargs["explicit_expirations"] == ["2026-04-29", "2026-06-18"]
        return {"rows": [{"symbol": "FUTU"}], "expiration_count": 2}

    def _fake_save_required_data_opend(base, symbol, payload, *, output_root):  # type: ignore[no-untyped-def]
        parsed = output_root / "parsed"
        parsed.mkdir(parents=True, exist_ok=True)
        csv_path = parsed / f"{symbol}_required_data.csv"
        csv_path.write_text(
            "symbol,option_type,expiration,strike\n"
            "FUTU,put,2026-04-29,120\n"
            "FUTU,call,2026-06-18,130\n",
            encoding="utf-8",
        )
        return output_root / "raw" / f"{symbol}_required_data.json", csv_path

    _patch_agent_tool_context(
        monkeypatch,
        load_option_positions_context=_fake_load_option_positions_context,
        fetch_symbol_opend=_fake_fetch_symbol_opend,
        save_required_data_opend=_fake_save_required_data_opend,
    )
    out = run_tool("prepare_close_advice_inputs", {"config_path": str(cfg_path), "output_dir": str(tmp_path / "output_shared" / "agent_tools")})

    assert out["ok"] is True
    assert out["data"]["symbols"][0]["position_coverage_ok"] is True


def test_prepare_close_advice_inputs_uses_expiration_ymd_for_position_requirements(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (tmp_path / "portfolio.runtime.json").write_text(
        json.dumps({"option_positions": {"sqlite_path": "output_shared/state/option_positions.sqlite3"}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cfg = _public_cfg_with_futu("portfolio.runtime.json")
    cfg["symbols"][0]["symbol"] = "FUTU"
    cfg["close_advice"] = {"enabled": True}
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    def _fake_load_option_positions_context(**kwargs):  # type: ignore[no-untyped-def]
        return ({
            "open_positions_min": [
                {"symbol": "FUTU", "option_type": "put", "strike": 120, "expiration": None, "expiration_ymd": "2026-04-29"},
            ]
        }, True)

    def _fake_fetch_symbol_opend(symbol, **kwargs):  # type: ignore[no-untyped-def]
        assert symbol == "FUTU"
        assert kwargs["explicit_expirations"] == ["2026-04-29"]
        assert kwargs["option_types"] == "put"
        assert kwargs["min_strike"] == 120
        assert kwargs["max_strike"] == 120
        return {"rows": [{"symbol": "FUTU"}], "expiration_count": 1}

    def _fake_save_required_data_opend(base, symbol, payload, *, output_root):  # type: ignore[no-untyped-def]
        parsed = output_root / "parsed"
        parsed.mkdir(parents=True, exist_ok=True)
        csv_path = parsed / f"{symbol}_required_data.csv"
        csv_path.write_text(
            "symbol,option_type,expiration,strike\n"
            "FUTU,put,2026-04-29,120\n",
            encoding="utf-8",
        )
        return output_root / "raw" / f"{symbol}_required_data.json", csv_path

    _patch_agent_tool_context(
        monkeypatch,
        load_option_positions_context=_fake_load_option_positions_context,
        fetch_symbol_opend=_fake_fetch_symbol_opend,
        save_required_data_opend=_fake_save_required_data_opend,
    )
    out = run_tool("prepare_close_advice_inputs", {"config_path": str(cfg_path), "output_dir": str(tmp_path / "output_shared" / "agent_tools")})

    assert out["ok"] is True
    assert out["data"]["symbols"][0]["requested_expirations"] == ["2026-04-29"]
    assert out["data"]["symbols"][0]["position_coverage_ok"] is True


def test_prepare_close_advice_inputs_returns_empty_result_when_context_has_no_positions(tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    cfg = _minimal_cfg()
    cfg["close_advice"] = {"enabled": True}
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    out = run_tool("prepare_close_advice_inputs", {"config_path": str(cfg_path)})

    assert out["ok"] is True
    assert out["data"]["context_rows"] == 0
    assert out["data"]["symbol_count"] == 0


def test_get_close_advice_runs_prepare_then_render(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool
    import src.application.agent_tools.materialization as tools

    cfg_path = tmp_path / "config.us.json"
    cfg = _minimal_cfg()
    cfg["close_advice"] = {"enabled": True}
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    calls: list[str] = []
    old_prepare = tools._prepare_close_advice_inputs_tool
    old_close = tools._close_advice_tool
    try:
        def _fake_prepare(ctx, payload):  # type: ignore[no-untyped-def]
            calls.append("prepare")
            assert payload["config_path"] == str(cfg_path)
            return (
                {"symbol_count": 1, "symbols": [{"symbol": "NVDA"}]},
                ["prepare_warn"],
                {"required_data_root": ".../required_data"},
            )

        def _fake_close(ctx, payload):  # type: ignore[no-untyped-def]
            calls.append("close")
            assert payload["config_path"] == str(cfg_path)
            return (
                {
                    "enabled": True,
                    "rows": 2,
                    "notify_rows": 1,
                    "summary": {"row_count": 2, "tier_counts": {"strong": 1, "medium": 1}},
                    "top_rows": [{"symbol": "NVDA", "tier": "strong"}],
                    "notification_preview": "### [user1] 平仓建议",
                },
                ["close_warn"],
                {"output_dir": ".../reports"},
            )

        tools._prepare_close_advice_inputs_tool = _fake_prepare  # type: ignore[assignment]
        tools._close_advice_tool = _fake_close  # type: ignore[assignment]
        out = run_tool("get_close_advice", {"config_path": str(cfg_path)})
    finally:
        tools._prepare_close_advice_inputs_tool = old_prepare  # type: ignore[assignment]
        tools._close_advice_tool = old_close  # type: ignore[assignment]

    assert out["ok"] is True
    assert calls == ["prepare", "close"]
    assert out["data"]["prepared"]["symbol_count"] == 1
    assert out["data"]["close_advice"]["rows"] == 2
    assert out["data"]["summary"]["advice_row_count"] == 2
    assert out["data"]["top_rows"][0]["symbol"] == "NVDA"
    assert "平仓建议" in out["data"]["notification_preview"]
    assert out["warnings"] == ["prepare_warn", "close_warn"]


def test_scan_opportunities_returns_summary_fields(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(json.dumps(_minimal_cfg(), ensure_ascii=False, indent=2), encoding="utf-8")

    import src.application.config_loader as config_loader
    import src.application.config_profiles as config_profiles
    import src.application.pipeline_symbol as pipeline_symbol
    import src.application.pipeline_context as pipeline_context
    import src.application.pipeline_watchlist as pipeline_watchlist
    import src.application.report_builders as report_builders
    monkeypatch.setattr(config_loader, "load_config", lambda **kwargs: _minimal_cfg())
    monkeypatch.setattr(config_profiles, "apply_profiles", lambda cfg, **kwargs: cfg)
    monkeypatch.setattr(pipeline_watchlist, "run_watchlist_pipeline", lambda **kwargs: [
        {"symbol": "NVDA", "account": "user1", "side": "sell_put", "net_income": 320, "annualized_net_return": 0.18, "strike": 100, "expiration": "2026-06-19"},
        {"symbol": "TSLA", "account": "user1", "side": "sell_call", "net_income": 210, "annualized_net_return": 0.11, "strike": 320, "expiration": "2026-06-26"},
    ])
    monkeypatch.setattr(pipeline_symbol, "process_symbol", lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline_context, "build_pipeline_context", lambda **kwargs: {})
    monkeypatch.setattr(report_builders, "build_symbols_summary", lambda *args, **kwargs: None)
    monkeypatch.setattr(report_builders, "build_symbols_digest", lambda *args, **kwargs: None)

    out = run_tool("scan_opportunities", {"config_path": str(cfg_path), "output_dir": str(tmp_path / "output_shared" / "agent_tools")})

    assert out["ok"] is True
    assert out["data"]["summary"]["row_count"] == 2
    assert out["data"]["summary"]["strategy_counts"]["sell_put"] == 1
    assert out["data"]["summary"]["strategy_counts"]["sell_call"] == 1
    assert out["data"]["top_candidates"][0]["symbol"] == "NVDA"


def test_candidate_rank_explain_reads_existing_candidate_csv(tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    candidate_path = tmp_path / "sell_put_candidates_labeled.csv"
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "contract_symbol": "NVDA_PUT_WIDE",
                "option_type": "put",
                "expiration": "2026-06-19",
                "strike": 100,
                "annualized_net_return_on_cash_basis": 0.120,
                "net_income": 100,
                "spread_ratio": 0.95,
                "open_interest": 1,
                "volume": 0,
                "delta": -0.20,
                "otm_pct": 0.08,
                "dte": 30,
            },
            {
                "symbol": "NVDA",
                "contract_symbol": "NVDA_PUT_LIQUID",
                "option_type": "put",
                "expiration": "2026-06-19",
                "strike": 95,
                "annualized_net_return_on_cash_basis": 0.115,
                "net_income": 100,
                "spread_ratio": 0.05,
                "open_interest": 500,
                "volume": 20,
                "delta": -0.15,
                "otm_pct": 0.10,
                "dte": 30,
            },
        ]
    ).to_csv(candidate_path, index=False)

    out = run_tool(
        "candidate_rank_explain",
        {
            "candidate_path": str(candidate_path),
            "mode": "put",
            "top_n": 1,
            "score_weights": {"liquidity": 0.02},
            "compare_baseline": True,
        },
    )

    assert out["ok"] is True
    assert out["data"]["row_count"] == 2
    assert out["data"]["ranked"][0]["contract_symbol"] == "NVDA_PUT_LIQUID"
    assert out["data"]["ranked"][0]["score_components"]["liquidity"] > 0
    assert "流动性" in out["data"]["ranked"][0]["primary_driver_labels"]
    assert out["data"]["groups"][0]["baseline"]["changes"][0]["contract_symbol"] == "NVDA_PUT_LIQUID"
    assert out["meta"]["source_files"][0]["path"].endswith("sell_put_candidates_labeled.csv")


def test_manage_symbols_list_and_dry_run_add(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(json.dumps(_minimal_cfg(), ensure_ascii=False, indent=2), encoding="utf-8")

    out_list = run_tool("manage_symbols", {"config_path": str(cfg_path), "action": "list"})
    assert out_list["ok"] is True
    assert out_list["data"]["symbol_count"] == 1
    assert out_list["data"]["symbols"][0]["symbol"] == "NVDA"
    assert out_list["data"]["symbols"][0]["broker"] == "US"
    assert "market" not in out_list["data"]["symbols"][0]

    out_dry = run_tool(
        "manage_symbols",
        {
            "config_path": str(cfg_path),
            "action": "add",
            "symbol": "TSLA",
            "sell_put_enabled": True,
            "sell_put_min_dte": 20,
            "sell_put_max_dte": 45,
            "sell_put_min_strike": 100,
            "sell_put_max_strike": 120,
            "dry_run": True,
        },
    )
    assert out_dry["ok"] is True
    assert out_dry["data"]["dry_run"] is True
    assert out_dry["data"]["symbol_count"] == 2
    added = next(item for item in out_dry["data"]["symbols"] if item["symbol"] == "TSLA")
    assert "market" not in added

    current = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert [x["symbol"] for x in current["symbols"]] == ["NVDA"]


def test_symbol_config_read_resolves_alias_and_reports_missing_field(tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg = _minimal_cfg(market="hk")
    cfg["symbols"][0]["symbol"] = "9992.HK"
    cfg["symbols"][0]["sell_put"]["max_strike"] = 145
    cfg_path = tmp_path / "config.hk.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    out = run_tool(
        "symbol_config_read",
        {"config_path": str(cfg_path), "symbol": "泡泡玛特", "strategy": "sell_put", "field": "max_strike"},
    )

    assert out["ok"] is True
    assert out["data"]["found"] is True
    assert out["data"]["symbol"] == "泡泡玛特"
    assert out["data"]["canonical_symbol"] == "9992.HK"
    assert out["data"]["strategy"] == "sell_put"
    assert out["data"]["path"] == "sell_put.max_strike"
    assert out["data"]["value"] == 145
    assert out["meta"]["config_path"].endswith("config.hk.json")

    missing = run_tool(
        "symbol_config_read",
        {"config_path": str(cfg_path), "symbol": "泡泡玛特", "strategy": "sell_put", "field": "min_delta"},
    )

    assert missing["ok"] is True
    assert missing["data"]["found"] is False
    assert missing["data"]["missing_reason"] == "field_not_configured"
    assert "sell_put.min_delta" in missing["data"]["message"]


def test_manage_symbols_write_requires_gate_and_confirm(tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(json.dumps(_minimal_cfg(), ensure_ascii=False, indent=2), encoding="utf-8")

    blocked = run_tool(
        "manage_symbols",
        {
            "config_path": str(cfg_path),
            "action": "add",
            "symbol": "TSLA",
        },
    )
    assert blocked["ok"] is False
    assert blocked["error"]["code"] == "PERMISSION_DENIED"


def test_manage_symbols_write_applies_when_enabled(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(json.dumps(_minimal_cfg(), ensure_ascii=False, indent=2), encoding="utf-8")
    monkeypatch.setenv("OM_AGENT_ENABLE_WRITE_TOOLS", "true")

    out = run_tool(
        "manage_symbols",
        {
            "config_path": str(cfg_path),
            "action": "add",
            "symbol": "TSLA",
            "broker": "US",
            "sell_put_enabled": True,
            "sell_put_min_dte": 20,
            "sell_put_max_dte": 45,
            "sell_put_min_strike": 100,
            "sell_put_max_strike": 120,
            "confirm": True,
        },
    )
    assert out["ok"] is True
    assert out["meta"]["write_applied"] is True

    current = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert [x["symbol"] for x in current["symbols"]] == ["NVDA", "TSLA"]
    added = next(item for item in current["symbols"] if item["symbol"] == "TSLA")
    assert added["broker"] == "US"
    assert "market" not in added


def test_manage_symbols_add_calibrates_symbol_before_write(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.hk.json"
    cfg = _minimal_cfg(market="hk")
    cfg["symbols"] = [{"symbol": "NVDA"}]
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    monkeypatch.setenv("OM_AGENT_ENABLE_WRITE_TOOLS", "true")

    out = run_tool(
        "manage_symbols",
        {
            "config_path": str(cfg_path),
            "action": "add",
            "symbol": "HK.00700",
            "sell_put_enabled": True,
            "sell_put_min_dte": 20,
            "sell_put_max_dte": 45,
            "confirm": True,
        },
    )

    assert out["ok"] is True
    current = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert [item["symbol"] for item in current["symbols"]] == ["NVDA", "0700.HK"]


def test_manage_symbols_add_allows_single_near_bound_modes(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(json.dumps(_minimal_cfg(), ensure_ascii=False, indent=2), encoding="utf-8")
    monkeypatch.setenv("OM_AGENT_ENABLE_WRITE_TOOLS", "true")

    out = run_tool(
        "manage_symbols",
        {
            "config_path": str(cfg_path),
            "action": "add",
            "symbol": "TSLA",
            "broker": "US",
            "sell_put_enabled": True,
            "sell_put_min_dte": 20,
            "sell_put_max_dte": 45,
            "sell_put_max_strike": 120,
            "sell_call_enabled": True,
            "sell_call_min_dte": 20,
            "sell_call_max_dte": 45,
            "sell_call_min_strike": 140,
            "confirm": True,
        },
    )
    assert out["ok"] is True

    current = json.loads(cfg_path.read_text(encoding="utf-8"))
    added = next(item for item in current["symbols"] if item["symbol"] == "TSLA")
    assert added["sell_put"]["max_strike"] == 120
    assert "min_strike" not in added["sell_put"]
    assert added["sell_call"]["min_strike"] == 140
    assert "max_strike" not in added["sell_call"]


def test_preview_notification_is_read_only() -> None:
    from src.application.tool_execution import execute_tool as run_tool

    alerts = """# Symbols Alerts

## 高优先级
- NVDA | sell_put | 2026-06-18 156P | 年化 10.00% | 净收入 100.0 | DTE 30 | Strike 156 | 中性 | ccy USD | mid 1.000 | cash_req $15,600 | 通过准入后，收益/风险组合较强，值得优先看。
"""
    out = run_tool("preview_notification", {"alerts_text": alerts, "account_label": "user1"})

    assert out["ok"] is True
    assert "### Put" in out["data"]["notification_text"]
    assert "🟢 卖Put NVDA 156P @ 06-18" in out["data"]["notification_text"]
