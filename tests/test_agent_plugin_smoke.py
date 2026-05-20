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


def _minimal_cfg() -> dict[str, Any]:
    return {
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


def _public_cfg_with_futu(data_config_ref: str) -> dict[str, Any]:
    cfg = _minimal_cfg()
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


def _public_cfg_with_futu_auto_source(data_config_ref: str) -> dict[str, Any]:
    cfg = _public_cfg_with_futu(data_config_ref)
    cfg["account_settings"]["user1"]["holdings_account"] = "lx"
    cfg["portfolio"]["source"] = "auto"
    cfg["portfolio"]["source_by_account"]["user1"] = "auto"
    return cfg


def _public_cfg_with_external_holdings(data_config_ref: str) -> dict[str, Any]:
    cfg = _public_cfg_with_futu(data_config_ref)
    cfg["accounts"] = ["user1", "ext1"]
    cfg["account_settings"]["ext1"] = {
        "type": "external_holdings",
        "holdings_account": "Feishu EXT",
    }
    cfg["portfolio"]["source_by_account"]["ext1"] = "holdings"
    return cfg


def test_healthcheck_works_with_explicit_config_path(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool
    import src.application.agent_tool_handlers as tools

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

    monkeypatch.setattr(
        tools,
        "_run_futu_doctor",
        lambda **kwargs: {
            "ok": True,
            "sdk": {"ok": True},
            "watchdog": {"ok": True},
        },
    )

    out = run_tool("healthcheck", {"config_path": str(cfg_path)})

    assert out["ok"] is True
    assert out["data"]["config"]["accounts"] == ["user1"]
    assert out["data"]["account_paths"]["user1"]["primary"]["source"] == "futu"
    assert out["data"]["account_paths"]["user1"]["primary"]["ok"] is True
    assert "fallback" not in out["data"]["account_paths"]["user1"]
    assert out["meta"]["config_path"] == ".../config.us.json"
    assert any(item["name"] == "opend_readiness" and item["status"] == "ok" for item in out["data"]["checks"])
    assert any(item["name"] == "account_mapping" and item["status"] == "ok" for item in out["data"]["checks"])
    primary = next(item for item in out["data"]["checks"] if item["name"] == "account_primary_paths")
    assert primary["status"] == "ok"
    assert primary["value"]["user1"]["source"] == "futu"
    assert any("starter account label 'user1'" in item for item in out["warnings"])


def test_healthcheck_rejects_placeholder_futu_mapping(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool
    import src.application.agent_tool_handlers as tools

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

    monkeypatch.setattr(
        tools,
        "_run_futu_doctor",
        lambda **kwargs: {
            "ok": True,
            "sdk": {"ok": True},
            "watchdog": {"ok": True},
        },
    )

    out = run_tool("healthcheck", {"config_path": str(cfg_path)})

    assert out["ok"] is True
    assert out["data"]["summary"]["ok"] is False
    assert out["data"]["account_paths"]["user1"]["primary"]["ok"] is False
    check = next(item for item in out["data"]["checks"] if item["name"] == "account_primary_paths")
    assert check["status"] == "error"
    assert "placeholder futu acc_id" in check["message"]


def test_healthcheck_accepts_futu_auto_source_without_fallback_checks(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool
    import src.application.agent_tool_handlers as tools

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

    monkeypatch.setattr(
        tools,
        "_run_futu_doctor",
        lambda **kwargs: {
            "ok": True,
            "sdk": {"ok": True},
            "watchdog": {"ok": True},
        },
    )

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
    import src.application.agent_tool_handlers as tools

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

    monkeypatch.setattr(
        tools,
        "_run_futu_doctor",
        lambda **kwargs: {
            "ok": True,
            "sdk": {"ok": True},
            "watchdog": {"ok": True},
        },
    )

    out = run_tool("healthcheck", {"config_path": str(cfg_path)})

    assert out["ok"] is True
    assert out["data"]["summary"]["ok"] is False
    primary = next(item for item in out["data"]["checks"] if item["name"] == "account_primary_paths")
    assert primary["status"] == "error"
    assert "missing from trade_intake.account_mapping.futu" in primary["message"]


def test_healthcheck_accepts_external_holdings_account_without_futu_mapping(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool
    import src.application.agent_tool_handlers as tools

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

    monkeypatch.setattr(
        tools,
        "_run_futu_doctor",
        lambda **kwargs: {
            "ok": True,
            "sdk": {"ok": True},
            "watchdog": {"ok": True},
        },
    )

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
    import src.application.agent_tool_handlers as tools

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

    monkeypatch.setattr(
        tools,
        "_run_futu_doctor",
        lambda **kwargs: {
            "ok": True,
            "sdk": {"ok": True},
            "watchdog": {"ok": True},
        },
    )

    class _Repo:
        bootstrap_status = "degraded_option_positions_repo_load_failed"
        bootstrap_message = "option positions repo load failed: sqlite unavailable"

    monkeypatch.setattr(tools, "open_position_ledger", lambda _path: _Repo())

    out = run_tool("healthcheck", {"config_path": str(cfg_path)})

    bootstrap = next(item for item in out["data"]["checks"] if item["name"] == "option_positions_bootstrap")
    assert bootstrap["status"] == "warn"
    assert bootstrap["value"]["status"] == "degraded_option_positions_repo_load_failed"
    assert "sqlite unavailable" in bootstrap["message"]
    assert out["data"]["summary"]["warning_count"] >= 1


def test_healthcheck_reports_option_positions_bootstrap_ok_for_sqlite_only(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool
    import src.application.agent_tool_handlers as tools

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

    monkeypatch.setattr(
        tools,
        "_run_futu_doctor",
        lambda **kwargs: {
            "ok": True,
            "sdk": {"ok": True},
            "watchdog": {"ok": True},
        },
    )

    class _Repo:
        bootstrap_status = "sqlite_only_no_feishu_bootstrap"
        bootstrap_message = "feishu option_positions bootstrap is not used; local trade_events remain source of truth"

    monkeypatch.setattr(tools, "open_position_ledger", lambda _path: _Repo())

    out = run_tool("healthcheck", {"config_path": str(cfg_path)})

    bootstrap = next(item for item in out["data"]["checks"] if item["name"] == "option_positions_bootstrap")
    assert bootstrap["status"] == "ok"
    assert bootstrap["value"]["status"] == "sqlite_only_no_feishu_bootstrap"


def test_healthcheck_warns_on_notification_placeholder_values(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool
    import src.application.agent_tool_handlers as tools

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

    monkeypatch.setattr(
        tools,
        "_run_futu_doctor",
        lambda **kwargs: {
            "ok": True,
            "sdk": {"ok": True},
            "watchdog": {"ok": True},
        },
    )

    out = run_tool("healthcheck", {"config_path": str(cfg_path)})

    assert out["ok"] is True
    assert any(item["name"] == "notification_target_placeholder" and item["status"] == "warn" for item in out["data"]["checks"])
    assert any(item["name"] == "notification_credentials_placeholder" and item["status"] == "warn" for item in out["data"]["checks"])
    assert any("example Feishu bot user open_id" in item for item in out["warnings"])
    assert any("example Feishu bot credentials" in item for item in out["warnings"])


def test_get_portfolio_context_allows_futu_source_without_explicit_data_config(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool
    import src.application.agent_tool_handlers as tools

    cfg_path = tmp_path / "config.us.json"
    cfg = _minimal_cfg()
    cfg["portfolio"]["account"] = "user1"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    old_load = tools.load_portfolio_context
    try:
        def _fake_load_portfolio_context(**kwargs):  # type: ignore[no-untyped-def]
            assert str(kwargs["data_config"]).endswith("portfolio.runtime.json")
            return {
                "portfolio_source_name": "futu",
                "cash_by_currency": {"USD": 1000.0},
                "stocks_by_symbol": {},
            }

        tools.load_portfolio_context = _fake_load_portfolio_context  # type: ignore[assignment]
        out = run_tool("get_portfolio_context", {"config_path": str(cfg_path), "account": "user1"})
    finally:
        tools.load_portfolio_context = old_load  # type: ignore[assignment]

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
    cfg = _public_cfg_with_futu("portfolio.runtime.json")
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

    out_root = tmp_path / "output" / "agent_plugin"
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
    import src.application.agent_tool_handlers as tools
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

    monkeypatch.setattr(
        tools,
        "_get_cached_exchange_rates",
        lambda **_kwargs: {"rates": {"USDCNY": 7.2, "HKDCNY": 0.92}},
    )

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
    from src.application.tool_execution import execute_tool as run_tool
    import src.application.agent_tool_handlers as tools

    monkeypatch.setattr(
        tools,
        "check_version_update",
        lambda **kwargs: {
            "ok": True,
            "current_version": "1.0.9",
            "latest_version": "1.0.9",
            "update_available": False,
            "remote_name": kwargs["remote_name"],
            "release_tag": "v1.0.9",
            "checked_at": "2026-05-05T00:00:00Z",
            "message": "当前已是最新版本 1.0.9",
            "error": None,
        },
    )

    out = run_tool("version_check", {"remote_name": "origin"})

    assert out["ok"] is True
    assert out["warnings"] == []
    assert out["data"]["current_version"] == "1.0.9"
    assert out["data"]["remote_name"] == "origin"


def test_version_update_defaults_to_dry_run(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool
    import src.application.agent_tool_handlers as handlers

    (tmp_path / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    monkeypatch.setattr(handlers, "repo_base", lambda: tmp_path)

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
    import src.application.agent_tool_handlers as handlers

    (tmp_path / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    monkeypatch.setattr(handlers, "repo_base", lambda: tmp_path)
    monkeypatch.setenv("OM_AGENT_ENABLE_WRITE_TOOLS", "true")

    out = run_tool("version_update", {"version": "1.1.0", "apply": True, "confirm": True})

    assert out["ok"] is True
    assert out["warnings"] == []
    assert out["data"]["mode"] == "applied"
    assert out["data"]["target_version"] == "1.1.0"
    assert out["data"]["changed"] is True
    assert (tmp_path / "VERSION").read_text(encoding="utf-8").strip() == "1.1.0"


def test_version_update_apply_requires_write_gate(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool
    import src.application.agent_tool_handlers as handlers

    (tmp_path / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    monkeypatch.setattr(handlers, "repo_base", lambda: tmp_path)

    blocked = run_tool("version_update", {"version": "1.1.0", "apply": True})
    assert blocked["ok"] is False
    assert blocked["error"]["code"] == "PERMISSION_DENIED"
    assert (tmp_path / "VERSION").read_text(encoding="utf-8").strip() == "1.0.0"

    monkeypatch.setenv("OM_AGENT_ENABLE_WRITE_TOOLS", "true")
    needs_confirm = run_tool("version_update", {"version": "1.1.0", "apply": True})
    assert needs_confirm["ok"] is False
    assert needs_confirm["error"]["code"] == "CONFIRMATION_REQUIRED"
    assert (tmp_path / "VERSION").read_text(encoding="utf-8").strip() == "1.0.0"


def test_config_validate_runs_without_opend(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    cfg = _minimal_cfg()
    cfg["notifications"] = {
        "provider": "openclaw",
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


def test_option_positions_read_lists_events_history_and_inspect(tmp_path: Path) -> None:
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


def test_runtime_status_summarizes_openclaw_runtime_files(tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    cfg = _minimal_cfg()
    cfg["notifications"] = {
        "provider": "openclaw",
        "channel": "wechat_clawbot",
        "target": "clawbot:test-room",
    }
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    state_dir = tmp_path / "output" / "state"
    report_dir = tmp_path / "output" / "reports"
    shared_state_dir = tmp_path / "output_shared" / "state"
    accounts_root = tmp_path / "output_accounts"
    runs_root = tmp_path / "output_runs"
    for path in (state_dir, report_dir, shared_state_dir, accounts_root / "user1" / "state", accounts_root / "user1" / "reports"):
        path.mkdir(parents=True, exist_ok=True)

    (shared_state_dir / "last_run.json").write_text(json.dumps({"status": "ok", "run_id": "run-1"}), encoding="utf-8")
    (state_dir / "last_run.json").write_text(json.dumps({"status": "legacy_ok"}), encoding="utf-8")
    (state_dir / "auto_trade_intake_status.json").write_text(
        json.dumps(
            {
                "status": "listening",
                "stage": "deal_processed",
                "last_heartbeat_utc": "2026-01-01T00:00:00+00:00",
                "last_deal_result": {"status": "applied", "deal_id": "deal-1"},
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
    assert out["data"]["trade_intake"]["summary"]["processed_count"] == 1
    assert out["data"]["trade_intake"]["summary"]["receipt_confirmed_count"] == 1
    assert out["data"]["trade_intake"]["audit"]["exists"] is True
    assert "option_positions_feishu_sync" not in out["data"]
    assert "option_positions_feishu_sync_status" not in out["data"]["summary"]
    assert "option_positions_feishu_sync_receipt_status" not in out["data"]["summary"]


def _runtime_status_upgrade_fixture(tmp_path: Path, *, target_version: str = "1.2.82") -> dict[str, Any]:
    (tmp_path / "VERSION").write_text("1.2.82\n", encoding="utf-8")
    data_config = tmp_path / "portfolio.runtime.json"
    data_config.write_text("{}", encoding="utf-8")
    cfg_path = tmp_path / "config.us.json"
    cfg = {
        "accounts": ["user1"],
        "portfolio": {"data_config": str(data_config)},
        "notifications": {"provider": "openclaw", "target": "route"},
    }
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    (tmp_path / "output_shared" / "state").mkdir(parents=True)
    (tmp_path / "output_shared" / "state" / "last_run.json").write_text(json.dumps({"status": "ok"}), encoding="utf-8")
    (tmp_path / "output" / "reports").mkdir(parents=True)
    (tmp_path / "output" / "reports" / "symbols_notification.txt").write_text("ok\n", encoding="utf-8")
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
    from src.application.agent_tool_openclaw import runtime_status_tool

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


def test_runtime_status_marks_remediated_upgrade_failure(monkeypatch, tmp_path: Path) -> None:
    import src.application.agent_tool_openclaw as openclaw

    fixture = _runtime_status_upgrade_fixture(tmp_path)

    def _service_status(profile: dict[str, Any], *, include_status: bool = False) -> dict[str, Any]:
        services = profile.get("services") if isinstance(profile.get("services"), list) else []
        return {
            "provider": profile.get("service_provider"),
            "services": [{**item, "status": "ok", "returncode": 0} for item in services if isinstance(item, dict)],
            "status_checked": include_status,
        }

    monkeypatch.setattr(openclaw, "service_status_from_profile", _service_status)

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


def test_runtime_status_keeps_upgrade_failed_when_service_still_failed(monkeypatch, tmp_path: Path) -> None:
    import src.application.agent_tool_openclaw as openclaw

    fixture = _runtime_status_upgrade_fixture(tmp_path)

    def _service_status(profile: dict[str, Any], *, include_status: bool = False) -> dict[str, Any]:
        services = profile.get("services") if isinstance(profile.get("services"), list) else []
        out = []
        for item in services:
            if not isinstance(item, dict):
                continue
            status = "warn" if item.get("name") == "options-monitor-trade-intake.service" else "ok"
            out.append({**item, "status": status, "returncode": 3 if status == "warn" else 0})
        return {"provider": profile.get("service_provider"), "services": out, "status_checked": include_status}

    monkeypatch.setattr(openclaw, "service_status_from_profile", _service_status)

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


def test_runtime_status_can_inspect_scanned_run_after_skipped_latest(tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    def write_json(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    cfg_path = tmp_path / "config.us.json"
    cfg = _minimal_cfg()
    cfg["accounts"] = ["user1", "user2"]
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    state_dir = tmp_path / "output" / "state"
    report_dir = tmp_path / "output" / "reports"
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

    report_dir = tmp_path / "output" / "reports"
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
    selection = out["data"]["latest_scanned_run_selection"]
    assert out["data"]["latest_scanned_run"]["path"].endswith("run-us")
    assert selection["market_filter"] == "US"
    assert selection["skipped_market_mismatch_count"] == 1


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
    file_out = run_tool("runtime_logs", {"file": str(service_log), "lines": 2})

    assert audit_out["ok"] is True
    assert audit_out["data"]["schema_version"] == "runtime_logs.v1"
    assert audit_out["data"]["summary"]["requested_run_found"] is True
    assert audit_out["data"]["files"][0]["path"].endswith("audit_events.jsonl")
    assert audit_out["data"]["files"][0]["tail"] == ['{"message":"second"}']
    assert audit_out["meta"]["runs_root"] == ".../output_runs"
    assert file_out["ok"] is True
    assert file_out["data"]["files"][0]["tail"] == ["two", "three"]


def test_openclaw_readiness_combines_status_and_healthcheck(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool
    import src.application.agent_tool_handlers as tools

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
    report_dir = tmp_path / "output" / "reports"
    accounts_root = tmp_path / "output_accounts"
    for path in (shared_state_dir, report_dir, accounts_root / "user1" / "reports"):
        path.mkdir(parents=True, exist_ok=True)
    (shared_state_dir / "last_run.json").write_text(json.dumps({"status": "ok"}), encoding="utf-8")
    (report_dir / "symbols_notification.txt").write_text("ready\n", encoding="utf-8")

    monkeypatch.setattr(
        tools,
        "_run_futu_doctor",
        lambda **kwargs: {
            "ok": True,
            "sdk": {"ok": True},
            "watchdog": {"ok": True},
        },
    )

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
    assert checks["notification_route"]["value"]["transport_channel"] == "openclaw-weixin"
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
    import src.application.agent_tool_handlers as tools

    cfg_path = tmp_path / "config.us.json"
    cfg = _minimal_cfg()
    cfg["close_advice"] = {"enabled": True}
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    out_root = tmp_path / "output" / "agent_plugin"
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

    old_run = tools.run_close_advice
    try:
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

        tools.run_close_advice = _fake_run_close_advice  # type: ignore[assignment]
        out = run_tool("close_advice", {"config_path": str(cfg_path), "output_dir": str(out_root)})
    finally:
        tools.run_close_advice = old_run  # type: ignore[assignment]

    assert out["ok"] is True
    assert out["data"]["enabled"] is True
    assert out["data"]["summary"]["row_count"] == 0
    assert out["data"]["top_rows"] == []
    assert out["meta"]["context_path"] == ".../option_positions_context.json"
    assert out["meta"]["required_data_root"] == ".../required_data"


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

    out = run_tool("close_advice", {"config_path": str(cfg_path), "output_dir": str(tmp_path / "output" / "agent_plugin")})

    assert out["ok"] is False
    assert out["error"]["code"] == "DEPENDENCY_MISSING"


def test_prepare_close_advice_inputs_builds_context_and_required_data(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool
    import src.application.agent_tool_handlers as tools

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

    old_load = tools.load_option_positions_context
    old_opend = tools.fetch_symbol_opend
    old_save = tools.save_required_data_opend
    try:
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

        tools.load_option_positions_context = _fake_load_option_positions_context  # type: ignore[assignment]
        tools.fetch_symbol_opend = _fake_fetch_symbol_opend  # type: ignore[assignment]
        tools.save_required_data_opend = _fake_save_required_data_opend  # type: ignore[assignment]
        out = run_tool("prepare_close_advice_inputs", {"config_path": str(cfg_path), "output_dir": str(tmp_path / "output" / "agent_plugin")})
    finally:
        tools.load_option_positions_context = old_load  # type: ignore[assignment]
        tools.fetch_symbol_opend = old_opend  # type: ignore[assignment]
        tools.save_required_data_opend = old_save  # type: ignore[assignment]

    assert out["ok"] is True
    assert out["data"]["account"] == "user1"
    assert out["data"]["symbol_count"] == 1
    assert out["data"]["symbols"][0]["symbol"] == "NVDA"
    assert out["data"]["symbols"][0]["position_coverage_ok"] is True
    assert out["data"]["coverage_summary"]["covered_symbol_count"] == 1
    assert out["meta"]["required_data_root"] == ".../required_data"


def test_prepare_close_advice_inputs_reuses_cached_required_data_when_coverage_is_complete(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool
    import src.application.agent_tool_handlers as tools

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

    required_root = (tmp_path / "output" / "agent_plugin" / "required_data" / "parsed")
    required_root.mkdir(parents=True, exist_ok=True)
    (required_root / "NVDA_required_data.csv").write_text(
        "symbol,option_type,expiration,strike\n"
        "NVDA,put,2026-06-19,100\n"
        "NVDA,call,2026-07-17,120\n",
        encoding="utf-8",
    )

    old_load = tools.load_option_positions_context
    old_opend = tools.fetch_symbol_opend
    old_save = tools.save_required_data_opend
    try:
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

        tools.load_option_positions_context = _fake_load_option_positions_context  # type: ignore[assignment]
        tools.fetch_symbol_opend = _fail_fetch_symbol_opend  # type: ignore[assignment]
        tools.save_required_data_opend = _fail_save_required_data_opend  # type: ignore[assignment]
        out = run_tool(
            "prepare_close_advice_inputs",
            {"config_path": str(cfg_path), "output_dir": str(tmp_path / "output" / "agent_plugin")},
        )
    finally:
        tools.load_option_positions_context = old_load  # type: ignore[assignment]
        tools.fetch_symbol_opend = old_opend  # type: ignore[assignment]
        tools.save_required_data_opend = old_save  # type: ignore[assignment]

    assert out["ok"] is True
    assert out["data"]["symbols"][0]["position_coverage_ok"] is True
    assert out["data"]["symbols"][0]["rows"] == 2
    assert out["data"]["symbols"][0]["expiration_count"] == 2


def test_prepare_close_advice_inputs_reports_missing_required_expirations(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool
    import src.application.agent_tool_handlers as tools

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

    old_load = tools.load_option_positions_context
    old_opend = tools.fetch_symbol_opend
    old_save = tools.save_required_data_opend
    try:
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

        tools.load_option_positions_context = _fake_load_option_positions_context  # type: ignore[assignment]
        tools.fetch_symbol_opend = _fake_fetch_symbol_opend  # type: ignore[assignment]
        tools.save_required_data_opend = _fake_save_required_data_opend  # type: ignore[assignment]
        out = run_tool("prepare_close_advice_inputs", {"config_path": str(cfg_path), "output_dir": str(tmp_path / "output" / "agent_plugin")})
    finally:
        tools.load_option_positions_context = old_load  # type: ignore[assignment]
        tools.fetch_symbol_opend = old_opend  # type: ignore[assignment]
        tools.save_required_data_opend = old_save  # type: ignore[assignment]

    assert out["ok"] is True
    assert out["data"]["symbols"][0]["missing_expirations"] == ["2026-04-29", "2026-06-29"]
    assert out["data"]["symbols"][0]["position_coverage_ok"] is False
    assert out["data"]["coverage_summary"]["positions_missing_coverage"] == 2
    assert "missing required expirations" in out["warnings"][0]


def test_prepare_close_advice_inputs_reports_expiration_near_miss_without_silent_rewrite(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool
    import src.application.agent_tool_handlers as tools

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

    old_load = tools.load_option_positions_context
    old_opend = tools.fetch_symbol_opend
    old_save = tools.save_required_data_opend
    try:
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

        tools.load_option_positions_context = _fake_load_option_positions_context  # type: ignore[assignment]
        tools.fetch_symbol_opend = _fake_fetch_symbol_opend  # type: ignore[assignment]
        tools.save_required_data_opend = _fake_save_required_data_opend  # type: ignore[assignment]
        out = run_tool(
            "prepare_close_advice_inputs",
            {
                "config_path": str(cfg_path),
                "output_dir": str(tmp_path / "output" / "agent_plugin"),
                "force_required_data_refresh": True,
            },
        )
    finally:
        tools.load_option_positions_context = old_load  # type: ignore[assignment]
        tools.fetch_symbol_opend = old_opend  # type: ignore[assignment]
        tools.save_required_data_opend = old_save  # type: ignore[assignment]

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
    import src.application.agent_tool_handlers as tools

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

    old_load = tools.load_option_positions_context
    old_opend = tools.fetch_symbol_opend
    old_save = tools.save_required_data_opend
    try:
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

        tools.load_option_positions_context = _fake_load_option_positions_context  # type: ignore[assignment]
        tools.fetch_symbol_opend = _fake_fetch_symbol_opend  # type: ignore[assignment]
        tools.save_required_data_opend = _fake_save_required_data_opend  # type: ignore[assignment]
        out = run_tool("prepare_close_advice_inputs", {"config_path": str(cfg_path), "output_dir": str(tmp_path / "output" / "agent_plugin")})
    finally:
        tools.load_option_positions_context = old_load  # type: ignore[assignment]
        tools.fetch_symbol_opend = old_opend  # type: ignore[assignment]
        tools.save_required_data_opend = old_save  # type: ignore[assignment]

    assert out["ok"] is True
    assert out["data"]["symbols"][0]["position_coverage_ok"] is True


def test_prepare_close_advice_inputs_uses_expiration_ymd_for_position_requirements(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool
    import src.application.agent_tool_handlers as tools

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

    old_load = tools.load_option_positions_context
    old_opend = tools.fetch_symbol_opend
    old_save = tools.save_required_data_opend
    try:
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

        tools.load_option_positions_context = _fake_load_option_positions_context  # type: ignore[assignment]
        tools.fetch_symbol_opend = _fake_fetch_symbol_opend  # type: ignore[assignment]
        tools.save_required_data_opend = _fake_save_required_data_opend  # type: ignore[assignment]
        out = run_tool("prepare_close_advice_inputs", {"config_path": str(cfg_path), "output_dir": str(tmp_path / "output" / "agent_plugin")})
    finally:
        tools.load_option_positions_context = old_load  # type: ignore[assignment]
        tools.fetch_symbol_opend = old_opend  # type: ignore[assignment]
        tools.save_required_data_opend = old_save  # type: ignore[assignment]

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
    import src.application.agent_tool_handlers as tools

    cfg_path = tmp_path / "config.us.json"
    cfg = _minimal_cfg()
    cfg["close_advice"] = {"enabled": True}
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    calls: list[str] = []
    old_prepare = tools._prepare_close_advice_inputs_tool
    old_close = tools._close_advice_tool
    try:
        def _fake_prepare(payload):  # type: ignore[no-untyped-def]
            calls.append("prepare")
            assert payload["config_path"] == str(cfg_path)
            return (
                {"symbol_count": 1, "symbols": [{"symbol": "NVDA"}]},
                ["prepare_warn"],
                {"required_data_root": ".../required_data"},
            )

        def _fake_close(payload):  # type: ignore[no-untyped-def]
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
    import src.application.agent_tool_handlers as tools

    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(json.dumps(_minimal_cfg(), ensure_ascii=False, indent=2), encoding="utf-8")

    old_load_config = None
    old_run_watchlist_pipeline = None
    old_apply_profiles = None
    old_process_symbol = None
    old_build_pipeline_context = None
    old_build_symbols_summary = None
    old_build_symbols_digest = None
    import src.application.config_loader as config_loader
    import src.application.config_profiles as config_profiles
    import src.application.pipeline_symbol as pipeline_symbol
    import src.application.pipeline_context as pipeline_context
    import src.application.pipeline_watchlist as pipeline_watchlist
    import src.application.report_builders as report_builders
    old_load_config = tools.__dict__.get("load_config")
    try:
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

        out = run_tool("scan_opportunities", {"config_path": str(cfg_path), "output_dir": str(tmp_path / "output" / "agent_plugin")})
    finally:
        if old_load_config is not None:
            tools.__dict__["load_config"] = old_load_config

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


def test_strategy_replay_analyze_reads_existing_replay_csv(tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    replay_path = tmp_path / "strategy_replay.csv"
    pd.DataFrame(
        [
            {"symbol": "NVDA", "dte": 20, "delta": -0.18, "predicted_return": 0.04, "actual_return": 0.05, "max_drawdown": -0.04, "accepted": True},
            {"symbol": "NVDA", "dte": 24, "delta": -0.19, "predicted_return": 0.03, "actual_return": 0.04, "max_drawdown": -0.05, "accepted": True},
            {"symbol": "AAPL", "dte": 12, "delta": -0.14, "predicted_return": 0.02, "actual_return": -0.03, "max_drawdown": -0.20, "filter_reason": "max_spread_ratio", "accepted": False},
            {"symbol": "AAPL", "dte": 13, "delta": -0.16, "predicted_return": 0.02, "actual_return": -0.02, "max_drawdown": -0.18, "filter_reason": "max_spread_ratio", "accepted": False},
        ]
    ).to_csv(replay_path, index=False)

    out = run_tool(
        "strategy_replay_analyze",
        {"replay_path": str(replay_path), "min_sample": 2, "bad_drawdown_threshold": -0.15},
    )

    assert out["ok"] is True
    assert out["data"]["summary"]["row_count"] == 4
    assert out["data"]["dte_effectiveness"]["best_ranges"][0]["range"] == "15-30"
    assert out["data"]["filter_value"][0]["filter"] == "max_spread_ratio"
    assert out["data"]["filter_value"][0]["status"] == "valuable"
    assert out["meta"]["source_files"][0]["path"].endswith("strategy_replay.csv")


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
    cfg = _minimal_cfg()
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
