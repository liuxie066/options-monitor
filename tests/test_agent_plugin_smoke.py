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


def _public_cfg_with_futu_auto_source(data_config_ref: str) -> dict:
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
    assert "fallback" not in out["data"]["account_paths"]["user1"]
    assert out["meta"]["config_path"] == ".../config.us.json"
    assert any(item["name"] == "opend_doctor" and item["status"] == "ok" for item in out["data"]["checks"])
    assert any(item["name"] == "account_mapping" and item["status"] == "ok" for item in out["data"]["checks"])
    primary = next(item for item in out["data"]["checks"] if item["name"] == "account_primary_paths")
    assert primary["status"] == "ok"
    assert primary["value"]["user1"]["source"] == "futu"


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


def test_healthcheck_accepts_futu_auto_source_without_fallback_checks(monkeypatch, tmp_path: Path) -> None:
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
        json.dumps(_public_cfg_with_futu_auto_source("secrets/portfolio.sqlite.json"), ensure_ascii=False, indent=2),
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
    assert out["data"]["account_paths"]["ext1"]["primary"]["source"] == "external_holdings"
    assert out["data"]["account_paths"]["ext1"]["primary"]["ok"] is True
    assert "fallback" not in out["data"]["account_paths"]["ext1"]
    primary = next(item for item in out["data"]["checks"] if item["name"] == "account_primary_paths")
    assert primary["status"] == "ok"
    assert primary["value"]["ext1"]["type"] == "external_holdings"
    assert primary["value"]["ext1"]["holdings_account"] == "Feishu EXT"
    assert primary["value"]["ext1"]["ready"] is True
    assert all(item["name"] != "account_fallback_paths" for item in out["data"]["checks"])


def test_healthcheck_reports_option_positions_bootstrap_degraded(monkeypatch, tmp_path: Path) -> None:
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

    class _Repo:
        bootstrap_status = "degraded_feishu_bootstrap_failed"
        bootstrap_message = "feishu bootstrap failed: upstream unavailable"

    monkeypatch.setattr(tools, "load_option_positions_repo", lambda _path: _Repo())

    out = run_tool("healthcheck", {"config_path": str(cfg_path)})

    bootstrap = next(item for item in out["data"]["checks"] if item["name"] == "option_positions_bootstrap")
    assert bootstrap["status"] == "warn"
    assert bootstrap["value"]["status"] == "degraded_feishu_bootstrap_failed"
    assert "upstream unavailable" in bootstrap["message"]
    assert out["data"]["summary"]["warning_count"] >= 1


def test_healthcheck_reports_option_positions_bootstrap_ok_for_sqlite_only(monkeypatch, tmp_path: Path) -> None:
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

    class _Repo:
        bootstrap_status = "sqlite_only_no_feishu_bootstrap"
        bootstrap_message = "no feishu option_positions bootstrap configured"

    monkeypatch.setattr(tools, "load_option_positions_repo", lambda _path: _Repo())

    out = run_tool("healthcheck", {"config_path": str(cfg_path)})

    bootstrap = next(item for item in out["data"]["checks"] if item["name"] == "option_positions_bootstrap")
    assert bootstrap["status"] == "ok"
    assert bootstrap["value"]["status"] == "sqlite_only_no_feishu_bootstrap"


def test_get_portfolio_context_allows_futu_source_without_explicit_data_config(monkeypatch, tmp_path: Path) -> None:
    from scripts.agent_plugin.main import run_tool
    import scripts.agent_plugin.tools as tools

    cfg_path = tmp_path / "config.us.json"
    cfg = _minimal_cfg()
    cfg["portfolio"]["account"] = "user1"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    old_load = tools.load_portfolio_context
    try:
        def _fake_load_portfolio_context(**kwargs):  # type: ignore[no-untyped-def]
            assert str(kwargs["data_config"]).endswith("secrets/portfolio.sqlite.json")
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
    from scripts.agent_plugin.main import run_tool
    import scripts.pipeline_context as pipeline_context
    import scripts.portfolio_context_service as pcs

    cfg_path = tmp_path / "config.hk.json"
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
    cfg = _public_cfg_with_futu("secrets/portfolio.sqlite.json")
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
    from scripts.agent_plugin.main import build_spec

    spec = build_spec()
    query_tool = next(item for item in spec["tools"] if item["name"] == "query_cash_headroom")
    assert "broker" in query_tool["input_schema"]
    assert "market" not in query_tool["input_schema"]
    assert "data_config" in query_tool["input_schema"]
    assert "pm_config" not in query_tool["input_schema"]


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


def test_prepare_close_advice_inputs_reports_missing_required_expirations(monkeypatch, tmp_path: Path) -> None:
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


def test_prepare_close_advice_inputs_returns_empty_result_when_context_has_no_positions(tmp_path: Path) -> None:
    from scripts.agent_plugin.main import run_tool

    cfg_path = tmp_path / "config.us.json"
    cfg = _minimal_cfg()
    cfg["close_advice"] = {"enabled": True}
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    out = run_tool("prepare_close_advice_inputs", {"config_path": str(cfg_path)})

    assert out["ok"] is True
    assert out["data"]["context_rows"] == 0
    assert out["data"]["symbol_count"] == 0


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


def test_manage_symbols_add_allows_single_near_bound_modes(monkeypatch, tmp_path: Path) -> None:
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
    from scripts.agent_plugin.main import run_tool

    alerts = """# Symbols Alerts

## 高优先级
- NVDA | sell_put | 2026-06-18 156P | 年化 10.00% | 净收入 100.0 | DTE 30 | Strike 156 | 中性 | ccy USD | mid 1.000 | cash_req $15,600 | 通过准入后，收益/风险组合较强，值得优先看。
"""
    out = run_tool("preview_notification", {"alerts_text": alerts, "account_label": "user1"})

    assert out["ok"] is True
    assert "### [user1] NVDA · 卖Put" in out["data"]["notification_text"]
