from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def _minimal_cfg() -> dict:
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
                "fetch": {"source": "yahoo", "limit_expirations": 8},
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


def _public_cfg_with_futu(data_config_ref: str) -> dict:
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


def _public_cfg_with_futu_holdings_fallback(data_config_ref: str) -> dict:
    cfg = _public_cfg_with_futu(data_config_ref)
    cfg["account_settings"]["user1"]["holdings_account"] = "lx"
    cfg["portfolio"]["source"] = "auto"
    cfg["portfolio"]["source_by_account"]["user1"] = "auto"
    return cfg


def _public_cfg_with_external_holdings(data_config_ref: str) -> dict:
    cfg = _public_cfg_with_futu(data_config_ref)
    cfg["accounts"] = ["user1", "ext1"]
    cfg["account_settings"]["ext1"] = {
        "type": "external_holdings",
        "holdings_account": "Feishu EXT",
    }
    cfg["portfolio"]["source_by_account"]["ext1"] = "holdings"
    return cfg


def test_healthcheck_works_with_explicit_config_path(monkeypatch, tmp_path: Path) -> None:
    from scripts.agent_plugin.main import run_tool
    import scripts.agent_plugin.tools as tools

    cfg_path = tmp_path / "config.us.json"
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    data_cfg_path = secrets_dir / "portfolio.sqlite.json"
    data_cfg_path.write_text(
        json.dumps({"option_positions": {"sqlite_path": "output_shared/state/option_positions.sqlite3"}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cfg_path.write_text(
        json.dumps(_public_cfg_with_futu("secrets/portfolio.sqlite.json"), ensure_ascii=False, indent=2),
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
    assert out["data"]["account_paths"]["user1"]["fallback"]["enabled"] is False
    assert out["meta"]["config_path"] == ".../config.us.json"
    assert any(item["name"] == "opend_doctor" and item["status"] == "ok" for item in out["data"]["checks"])
    assert any(item["name"] == "account_mapping" and item["status"] == "ok" for item in out["data"]["checks"])
    primary = next(item for item in out["data"]["checks"] if item["name"] == "account_primary_paths")
    fallback = next(item for item in out["data"]["checks"] if item["name"] == "account_fallback_paths")
    assert primary["status"] == "ok"
    assert primary["value"]["user1"]["source"] == "futu"
    assert fallback["status"] == "ok"
    assert fallback["value"]["user1"]["enabled"] is False


def test_healthcheck_rejects_placeholder_futu_mapping(monkeypatch, tmp_path: Path) -> None:
    from scripts.agent_plugin.main import run_tool
    import scripts.agent_plugin.tools as tools

    cfg_path = tmp_path / "config.us.json"
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "portfolio.sqlite.json").write_text(
        json.dumps({"option_positions": {"sqlite_path": "output_shared/state/option_positions.sqlite3"}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cfg = _public_cfg_with_futu("secrets/portfolio.sqlite.json")
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


def test_healthcheck_warns_when_futu_holdings_fallback_is_configured_but_feishu_missing(monkeypatch, tmp_path: Path) -> None:
    from scripts.agent_plugin.main import run_tool
    import scripts.agent_plugin.tools as tools

    cfg_path = tmp_path / "config.us.json"
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "portfolio.sqlite.json").write_text(
        json.dumps({"option_positions": {"sqlite_path": "output_shared/state/option_positions.sqlite3"}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cfg_path.write_text(
        json.dumps(_public_cfg_with_futu_holdings_fallback("secrets/portfolio.sqlite.json"), ensure_ascii=False, indent=2),
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
    assert out["data"]["account_paths"]["user1"]["fallback"]["enabled"] is True
    assert out["data"]["account_paths"]["user1"]["fallback"]["ok"] is False
    primary = next(item for item in out["data"]["checks"] if item["name"] == "account_primary_paths")
    fallback = next(item for item in out["data"]["checks"] if item["name"] == "account_fallback_paths")
    assert primary["status"] == "ok"
    assert fallback["status"] == "warn"
    assert fallback["value"]["user1"]["holdings_account"] == "lx"
    assert fallback["value"]["user1"]["ready"] is False
    assert any("holdings fallback configured" in item for item in out["warnings"])


def test_healthcheck_accepts_external_holdings_account_without_futu_mapping(monkeypatch, tmp_path: Path) -> None:
    from scripts.agent_plugin.main import run_tool
    import scripts.agent_plugin.tools as tools

    cfg_path = tmp_path / "config.us.json"
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "portfolio.sqlite.json").write_text(
        json.dumps(
            {
                "option_positions": {"sqlite_path": "output_shared/state/option_positions.sqlite3"},
                "feishu": {
                    "app_id": "cli_xxx",
                    "app_secret": "secret_xxx",
                    "tables": {"holdings": "app_token/table_id"},
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    cfg_path.write_text(
        json.dumps(_public_cfg_with_external_holdings("secrets/portfolio.sqlite.json"), ensure_ascii=False, indent=2),
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
    assert out["data"]["account_paths"]["ext1"]["primary"]["source"] == "holdings"
    assert out["data"]["account_paths"]["ext1"]["primary"]["ok"] is True
    assert out["data"]["account_paths"]["ext1"]["fallback"]["enabled"] is True
    assert out["data"]["account_paths"]["ext1"]["fallback"]["ok"] is True
    primary = next(item for item in out["data"]["checks"] if item["name"] == "account_primary_paths")
    fallback = next(item for item in out["data"]["checks"] if item["name"] == "account_fallback_paths")
    assert primary["status"] == "ok"
    assert primary["value"]["ext1"]["type"] == "external_holdings"
    assert primary["value"]["ext1"]["holdings_account"] == "Feishu EXT"
    assert primary["value"]["ext1"]["ready"] is True
    assert fallback["value"]["ext1"]["enabled"] is True


def test_get_portfolio_context_allows_futu_source_without_pm_config(monkeypatch, tmp_path: Path) -> None:
    from scripts.agent_plugin.main import run_tool
    import scripts.agent_plugin.tools as tools

    cfg_path = tmp_path / "config.us.json"
    cfg = _minimal_cfg()
    cfg["portfolio"]["account"] = "user1"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    old_load = tools.load_portfolio_context
    try:
        def _fake_load_portfolio_context(**kwargs):  # type: ignore[no-untyped-def]
            assert str(kwargs["pm_config"]).endswith("secrets/portfolio.sqlite.json")
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


def test_spec_exposes_broker_as_public_field() -> None:
    from scripts.agent_plugin.main import build_spec

    spec = build_spec()
    query_tool = next(item for item in spec["tools"] if item["name"] == "query_cash_headroom")
    assert "broker" in query_tool["input_schema"]
    assert "market" in query_tool["input_schema"]
    assert "data_config" in query_tool["input_schema"]
    assert "pm_config" in query_tool["input_schema"]


def test_close_advice_reads_cached_context_and_required_data(monkeypatch, tmp_path: Path) -> None:
    from scripts.agent_plugin.main import run_tool
    import scripts.agent_plugin.tools as tools

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


def test_close_advice_requires_cached_inputs(tmp_path: Path) -> None:
    from scripts.agent_plugin.main import run_tool

    cfg_path = tmp_path / "config.us.json"
    cfg = _minimal_cfg()
    cfg["close_advice"] = {"enabled": True}
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    out = run_tool("close_advice", {"config_path": str(cfg_path), "output_dir": str(tmp_path / "output" / "agent_plugin")})

    assert out["ok"] is False
    assert out["error"]["code"] == "DEPENDENCY_MISSING"


def test_prepare_close_advice_inputs_builds_context_and_required_data(monkeypatch, tmp_path: Path) -> None:
    from scripts.agent_plugin.main import run_tool
    import scripts.agent_plugin.tools as tools

    cfg_path = tmp_path / "config.us.json"
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "portfolio.sqlite.json").write_text(
        json.dumps({"option_positions": {"sqlite_path": "output_shared/state/option_positions.sqlite3"}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cfg = _public_cfg_with_futu("secrets/portfolio.sqlite.json")
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
                    {"symbol": "NVDA", "option_type": "put", "strike": 100, "expiration": "2026-06-19"}
                ]
            }, True)

        def _fake_fetch_symbol_opend(symbol, **kwargs):  # type: ignore[no-untyped-def]
            assert symbol == "NVDA"
            return {"rows": [{"symbol": "NVDA"}], "expiration_count": 2}

        def _fake_save_required_data_opend(base, symbol, payload, *, output_root):  # type: ignore[no-untyped-def]
            parsed = output_root / "parsed"
            parsed.mkdir(parents=True, exist_ok=True)
            csv_path = parsed / f"{symbol}_required_data.csv"
            csv_path.write_text("symbol\nNVDA\n", encoding="utf-8")
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
    assert out["meta"]["required_data_root"] == ".../required_data"


def test_prepare_close_advice_inputs_returns_dependency_error_when_context_is_unavailable(tmp_path: Path) -> None:
    from scripts.agent_plugin.main import run_tool

    cfg_path = tmp_path / "config.us.json"
    cfg = _minimal_cfg()
    cfg["close_advice"] = {"enabled": True}
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    out = run_tool("prepare_close_advice_inputs", {"config_path": str(cfg_path)})

    assert out["ok"] is False
    assert out["error"]["code"] == "DEPENDENCY_MISSING"


def test_get_close_advice_runs_prepare_then_render(monkeypatch, tmp_path: Path) -> None:
    from scripts.agent_plugin.main import run_tool
    import scripts.agent_plugin.tools as tools

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
    from scripts.agent_plugin.main import run_tool
    import scripts.agent_plugin.tools as tools

    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(json.dumps(_minimal_cfg(), ensure_ascii=False, indent=2), encoding="utf-8")

    old_load_config = None
    old_run_watchlist_pipeline = None
    old_apply_profiles = None
    old_process_symbol = None
    old_build_pipeline_context = None
    old_build_symbols_summary = None
    old_build_symbols_digest = None
    import scripts.config_loader as config_loader
    import scripts.config_profiles as config_profiles
    import scripts.pipeline_symbol as pipeline_symbol
    import scripts.pipeline_context as pipeline_context
    import scripts.pipeline_watchlist as pipeline_watchlist
    import scripts.report_builders as report_builders
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


def test_manage_symbols_list_and_dry_run_add(monkeypatch, tmp_path: Path) -> None:
    from scripts.agent_plugin.main import run_tool

    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(json.dumps(_minimal_cfg(), ensure_ascii=False, indent=2), encoding="utf-8")

    out_list = run_tool("manage_symbols", {"config_path": str(cfg_path), "action": "list"})
    assert out_list["ok"] is True
    assert out_list["data"]["symbol_count"] == 1
    assert out_list["data"]["symbols"][0]["symbol"] == "NVDA"

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

    current = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert [x["symbol"] for x in current["symbols"]] == ["NVDA"]


def test_manage_symbols_write_requires_gate_and_confirm(tmp_path: Path) -> None:
    from scripts.agent_plugin.main import run_tool

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
    from scripts.agent_plugin.main import run_tool

    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(json.dumps(_minimal_cfg(), ensure_ascii=False, indent=2), encoding="utf-8")
    monkeypatch.setenv("OM_AGENT_ENABLE_WRITE_TOOLS", "true")

    out = run_tool(
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
            "confirm": True,
        },
    )
    assert out["ok"] is True
    assert out["meta"]["write_applied"] is True

    current = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert [x["symbol"] for x in current["symbols"]] == ["NVDA", "TSLA"]


def test_preview_notification_is_read_only() -> None:
    from scripts.agent_plugin.main import run_tool

    alerts = """# Symbols Alerts

## 高优先级
- NVDA | sell_put | 2026-06-18 156P | 年化 10.00% | 净收入 100.0 | DTE 30 | Strike 156 | 中性 | ccy USD | mid 1.000 | cash_req $15,600 | 通过准入后，收益/风险组合较强，值得优先看。
"""
    out = run_tool("preview_notification", {"alerts_text": alerts, "account_label": "user1"})

    assert out["ok"] is True
    assert "### [user1] NVDA · 卖Put" in out["data"]["notification_text"]
