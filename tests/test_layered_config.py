from __future__ import annotations

import json
from pathlib import Path

from src.application.config_validator import validate_config
from src.application.layered_config import build_layered_runtime_config, explain_layered_runtime_config_key
from src.application.runtime_config_freshness import GENERATED_KEY, check_runtime_config_freshness


REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def test_layered_config_builds_minimal_us_user_config(tmp_path: Path) -> None:
    user_path = _write_json(
        tmp_path / "user.us.json",
        {
            "account_settings": {
                "lx": {
                    "type": "futu",
                    "futu": {"account_id": "REAL_12345678"},
                }
            },
            "symbols": [
                {
                    "symbol": "NVDA",
                    "market": "US",
                    "sell_put": {"min_strike": 150, "max_strike": 160},
                    "yield_enhancement": {"enabled": True},
                }
            ],
        },
    )

    cfg, meta = build_layered_runtime_config(repo_root=REPO_ROOT, market="us", user_config_path=user_path)

    assert meta["market"] == "us"
    assert cfg["accounts"] == ["lx"]
    assert cfg["account_settings"]["lx"]["type"] == "futu"
    assert cfg["portfolio"]["account"] == "lx"
    assert "data_config" not in cfg["portfolio"]
    assert cfg["portfolio"]["futu"] == {"host": "127.0.0.1", "port": 11111}
    assert cfg["portfolio"]["source_by_account"] == {"lx": "futu"}
    assert cfg["trade_intake"]["mode"] == "apply"
    assert cfg["trade_intake"]["account_mapping"]["futu"] == {"REAL_12345678": "lx"}
    assert cfg["schedule"]["timezone"] == "America/New_York"
    assert cfg["schedule"]["cron_interval_min"] == 10
    assert cfg["schedule"]["run_window"] == {"start": "09:30", "end": "16:00", "breaks": []}
    assert cfg["schedule"]["run_points"] == {"start_plus_min": 10, "hourly_minute": 0, "end_minus_min": 10}
    assert cfg["schedule"]["gates"] == [
        {
            "type": "before",
            "timezone": "Asia/Shanghai",
            "time": "02:00",
            "day_offset_from_window_start": 1,
        }
    ]
    assert cfg["intake"]["symbol_aliases"]["英伟达"] == "NVDA"
    assert cfg["symbols"][0]["broker"] == "US"
    assert "market" not in cfg["symbols"][0]
    assert cfg["symbols"][0]["fetch"]["source"] == "futu"
    assert cfg["symbols"][0]["fetch"]["limit_expirations"] == 10
    assert cfg["symbols"][0]["sell_put"]["min_dte"] == 20
    assert cfg["symbols"][0]["sell_put"]["max_strike"] == 160
    assert cfg["symbols"][0]["yield_enhancement"]["enabled"] is True
    assert cfg.get("notifications", {}).get("channel") is None
    assert cfg["notifications"]["opend_alert_cooldown_sec"] == 600
    assert cfg[GENERATED_KEY]["market"] == "us"
    assert [item["role"] for item in cfg[GENERATED_KEY]["sources"]] == ["system", "common_user", "market_user"]

    validate_config(json.loads(json.dumps(cfg)))


def test_layered_config_derives_external_holdings_defaults(tmp_path: Path) -> None:
    user_path = _write_json(
        tmp_path / "user.hk.json",
        {
            "account_settings": {
                "lx": {
                    "type": "futu",
                    "futu": {"account_id": "REAL_87654321"},
                },
                "sy": {
                    "type": "external_holdings",
                    "holdings_account": "sy",
                },
            },
            "symbols": [
                {
                    "symbol": "0700.HK",
                    "sell_put": {"min_strike": 350, "max_strike": 420},
                }
            ],
        },
    )

    cfg, _meta = build_layered_runtime_config(repo_root=REPO_ROOT, market="hk", user_config_path=user_path)

    assert cfg["accounts"] == ["lx", "sy"]
    assert "data_config" not in cfg["portfolio"]
    assert cfg["portfolio"]["source_by_account"] == {"lx": "futu", "sy": "holdings"}
    assert cfg["trade_intake"]["mode"] == "apply"
    assert cfg["trade_intake"]["account_mapping"]["futu"] == {"REAL_87654321": "lx"}
    assert cfg["schedule"]["timezone"] == "Asia/Hong_Kong"
    assert cfg["schedule"]["run_window"]["breaks"] == [{"start": "12:00", "end": "13:00"}]
    assert cfg["templates"]["put_base"]["sell_put"]["min_volume"] == 0
    assert cfg["symbols"][0]["broker"] == "HK"
    assert cfg["symbols"][0]["sell_put"]["max_dte"] == 90

    validate_config(json.loads(json.dumps(cfg)))


def test_layered_config_auto_loads_common_user_config_for_default_user_path(tmp_path: Path) -> None:
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    _write_json(
        config_dir / "user.common.json",
        {
            "watchdog": {"retry_enabled": False},
            "runtime": {"portfolio_context_ttl_sec": 1200},
            "option_positions": {"auto_close": {"enabled": False}},
            "symbol_defaults": {
                "fetch": {"limit_expirations": 7},
                "sell_put": {"min_dte": 25},
            },
            "account_settings": {
                "lx": {
                    "type": "futu",
                    "futu": {"account_id": "REAL_12345678"},
                }
            },
        },
    )
    _write_json(
        config_dir / "user.us.json",
        {
            "symbols": [{"symbol": "NVDA", "sell_put": {"max_strike": 160}}],
        },
    )

    cfg, meta = build_layered_runtime_config(
        repo_root=tmp_path,
        market="us",
        system_config_path=REPO_ROOT / "configs" / "system.json",
    )

    assert cfg["watchdog"]["retry_enabled"] is False
    assert cfg["runtime"]["portfolio_context_ttl_sec"] == 1200
    assert cfg["option_positions"]["auto_close"]["enabled"] is False
    assert cfg["symbols"][0]["fetch"]["limit_expirations"] == 7
    assert cfg["symbols"][0]["sell_put"]["min_dte"] == 25
    assert cfg["accounts"] == ["lx"]
    assert cfg["trade_intake"]["account_mapping"]["futu"] == {"REAL_12345678": "lx"}
    assert meta["common_user_config_loaded"] is True
    assert meta["common_user_config_path"] == str((config_dir / "user.common.json").resolve())
    validate_config(json.loads(json.dumps(cfg)))


def test_layered_config_explicit_user_path_does_not_auto_load_common_user_config(tmp_path: Path) -> None:
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    _write_json(config_dir / "user.common.json", {"watchdog": {"retry_enabled": False}})
    user_path = _write_json(
        tmp_path / "explicit.user.us.json",
        {
            "account_settings": {
                "lx": {
                    "type": "futu",
                    "futu": {"account_id": "REAL_12345678"},
                }
            },
            "symbols": [{"symbol": "NVDA", "sell_put": {"max_strike": 160}}],
        },
    )

    cfg, meta = build_layered_runtime_config(
        repo_root=tmp_path,
        market="us",
        system_config_path=REPO_ROOT / "configs" / "system.json",
        user_config_path=user_path,
    )

    assert cfg["watchdog"]["retry_enabled"] is True
    assert meta["common_user_config_loaded"] is False
    assert "common_user_config_path" not in meta


def test_layered_config_market_user_overrides_explicit_common_user_config(tmp_path: Path) -> None:
    common_path = _write_json(
        tmp_path / "user.common.json",
        {
            "watchdog": {"retry_enabled": False},
            "account_settings": {
                "lx": {
                    "type": "futu",
                    "futu": {"account_id": "REAL_12345678"},
                }
            },
        },
    )
    user_path = _write_json(
        tmp_path / "user.us.json",
        {
            "watchdog": {"retry_enabled": True},
            "symbols": [{"symbol": "NVDA", "sell_put": {"max_strike": 160}}],
        },
    )

    cfg, meta = build_layered_runtime_config(
        repo_root=REPO_ROOT,
        market="us",
        common_user_config_path=common_path,
        user_config_path=user_path,
    )

    assert cfg["watchdog"]["retry_enabled"] is True
    assert cfg["accounts"] == ["lx"]
    assert meta["common_user_config_loaded"] is True
    assert meta["common_user_config_path"] == str(common_path.resolve())
    validate_config(json.loads(json.dumps(cfg)))


def test_layered_config_market_user_symbol_defaults_override_common_symbol_defaults(tmp_path: Path) -> None:
    common_path = _write_json(
        tmp_path / "user.common.json",
        {
            "account_settings": {
                "lx": {
                    "type": "futu",
                    "futu": {"account_id": "REAL_12345678"},
                }
            },
            "symbol_defaults": {
                "fetch": {"limit_expirations": 6},
                "sell_put": {"min_dte": 24},
            },
        },
    )
    user_path = _write_json(
        tmp_path / "user.us.json",
        {
            "symbol_defaults": {
                "fetch": {"limit_expirations": 4},
            },
            "symbols": [{"symbol": "NVDA", "sell_put": {"max_strike": 160}}],
        },
    )

    cfg, _meta = build_layered_runtime_config(
        repo_root=REPO_ROOT,
        market="us",
        common_user_config_path=common_path,
        user_config_path=user_path,
    )

    assert cfg["symbols"][0]["fetch"]["limit_expirations"] == 4
    assert cfg["symbols"][0]["sell_put"]["min_dte"] == 24
    assert "symbol_defaults" not in cfg
    validate_config(json.loads(json.dumps(cfg)))


def test_config_explain_reports_common_user_override_for_regular_key(tmp_path: Path) -> None:
    common_path = _write_json(
        tmp_path / "user.common.json",
        {
            "watchdog": {"retry_enabled": False},
            "account_settings": {
                "lx": {
                    "type": "futu",
                    "futu": {"account_id": "REAL_12345678"},
                }
            },
        },
    )
    user_path = _write_json(
        tmp_path / "user.us.json",
        {
            "symbols": [{"symbol": "NVDA", "sell_put": {"max_strike": 160}}],
        },
    )

    out = explain_layered_runtime_config_key(
        repo_root=REPO_ROOT,
        market="us",
        key="watchdog.retry_enabled",
        common_user_config_path=common_path,
        user_config_path=user_path,
    )

    assert out["value"] is False
    assert out["source"] == "common_user_config"
    assert [item["source"] for item in out["trace"]] == ["system.defaults", "common_user_config"]


def test_config_explain_reports_symbol_defaults_override_chain(tmp_path: Path) -> None:
    common_path = _write_json(
        tmp_path / "user.common.json",
        {
            "account_settings": {
                "lx": {
                    "type": "futu",
                    "futu": {"account_id": "REAL_12345678"},
                }
            },
            "symbol_defaults": {"fetch": {"limit_expirations": 6}},
        },
    )
    user_path = _write_json(
        tmp_path / "user.us.json",
        {
            "symbol_defaults": {"fetch": {"limit_expirations": 4}},
            "symbols": [{"symbol": "NVDA", "sell_put": {"max_strike": 160}}],
        },
    )

    out = explain_layered_runtime_config_key(
        repo_root=REPO_ROOT,
        market="us",
        key="symbol_defaults.fetch.limit_expirations",
        common_user_config_path=common_path,
        user_config_path=user_path,
    )

    assert out["value"] == 4
    assert out["source"] == "user_config"
    assert out["runtime_path"] is None
    assert out["applies_to"] == "symbols[].fetch.limit_expirations"
    assert [item["source"] for item in out["trace"]] == ["system.market", "common_user_config", "user_config"]


def test_config_explain_reports_symbol_value_from_symbol_defaults(tmp_path: Path) -> None:
    user_path = _write_json(
        tmp_path / "user.us.json",
        {
            "account_settings": {
                "lx": {
                    "type": "futu",
                    "futu": {"account_id": "REAL_12345678"},
                }
            },
            "symbol_defaults": {"fetch": {"limit_expirations": 4}},
            "symbols": [{"symbol": "NVDA", "sell_put": {"max_strike": 160}}],
        },
    )

    out = explain_layered_runtime_config_key(
        repo_root=REPO_ROOT,
        market="us",
        key="symbols.0.fetch.limit_expirations",
        user_config_path=user_path,
    )

    assert out["value"] == 4
    assert out["source"] == "user_config"
    assert out["runtime_path"] == "symbols.0.fetch.limit_expirations"
    assert "symbol_defaults" in out["notes"][0]


def test_tracked_layered_examples_validate() -> None:
    for market in ("us", "hk"):
        cfg, meta = build_layered_runtime_config(
            repo_root=REPO_ROOT,
            market=market,
            user_config_path=REPO_ROOT / "configs" / "examples" / f"user.example.{market}.json",
        )
        assert meta["symbols"]
        validate_config(json.loads(json.dumps(cfg)))


def test_runtime_config_freshness_detects_market_user_config_change(tmp_path: Path) -> None:
    user_path = _write_json(
        tmp_path / "user.us.json",
        {
            "account_settings": {
                "lx": {
                    "type": "futu",
                    "futu": {"account_id": "REAL_12345678"},
                }
            },
            "symbols": [{"symbol": "NVDA", "sell_put": {"max_strike": 160}}],
        },
    )
    cfg, _meta = build_layered_runtime_config(repo_root=REPO_ROOT, market="us", user_config_path=user_path)

    fresh = check_runtime_config_freshness(
        cfg,
        repo_root=REPO_ROOT,
        market="us",
        runtime_config_path=tmp_path / "config.us.json",
    )
    assert fresh["ok"] is True

    _write_json(
        user_path,
        {
            "account_settings": {
                "lx": {
                    "type": "futu",
                    "futu": {"account_id": "REAL_12345678"},
                }
            },
            "symbols": [{"symbol": "NVDA", "sell_put": {"max_strike": 170}}],
        },
    )

    stale = check_runtime_config_freshness(
        cfg,
        repo_root=REPO_ROOT,
        market="us",
        runtime_config_path=tmp_path / "config.us.json",
    )
    assert stale["ok"] is False
    assert stale["errors"][0]["code"] == "source_changed"
    assert stale["errors"][0]["role"] == "market_user"
    assert "--user-config" in stale["rebuild_command"]


def test_runtime_config_freshness_detects_auto_common_user_config_appearing(tmp_path: Path) -> None:
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    _write_json(
        config_dir / "user.us.json",
        {
            "account_settings": {
                "lx": {
                    "type": "futu",
                    "futu": {"account_id": "REAL_12345678"},
                }
            },
            "symbols": [{"symbol": "NVDA", "sell_put": {"max_strike": 160}}],
        },
    )
    cfg, _meta = build_layered_runtime_config(
        repo_root=tmp_path,
        market="us",
        system_config_path=REPO_ROOT / "configs" / "system.json",
    )

    _write_json(config_dir / "user.common.json", {"watchdog": {"retry_enabled": False}})

    stale = check_runtime_config_freshness(
        cfg,
        repo_root=tmp_path,
        market="us",
        runtime_config_path=tmp_path / "config.us.json",
    )
    assert stale["ok"] is False
    assert stale["errors"][0]["code"] == "optional_source_appeared"
    assert stale["errors"][0]["role"] == "common_user"
    assert "--common-user-config" in stale["rebuild_command"]
    assert "configs/user.common.json" in stale["rebuild_command"]


def test_init_runtime_config_includes_inline_generation_metadata(tmp_path: Path) -> None:
    from src.application.agent_tool_init_local import init_local_config

    config_path = tmp_path / "config.us.json"
    data_config_path = tmp_path / "portfolio.runtime.json"

    init_local_config(
        repo_root=REPO_ROOT,
        market="us",
        futu_acc_id="12345678",
        account_label="lx",
        config_path=config_path,
        data_config_path=data_config_path,
        symbols=["NVDA"],
    )

    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    generated = cfg[GENERATED_KEY]
    market_user = next(item for item in generated["sources"] if item["role"] == "market_user")

    assert market_user["inline"] is True
    assert market_user["ref"] == "init_local_config"
    assert generated["rebuild_command"].startswith("./om init runtime --market us")
    assert check_runtime_config_freshness(
        cfg,
        repo_root=REPO_ROOT,
        market="us",
        runtime_config_path=config_path,
    )["ok"] is True


def test_config_build_cli_writes_output(tmp_path: Path, capsys) -> None:
    from src.interfaces.cli.main import main

    user_path = _write_json(
        tmp_path / "user.us.json",
        {
            "account_settings": {
                "lx": {
                    "type": "futu",
                    "futu": {"account_id": "REAL_12345678"},
                }
            },
            "symbols": [{"symbol": "NVDA", "sell_put": {"max_strike": 160}}],
        },
    )
    output_path = tmp_path / "config.us.json"

    rc = main([
        "config",
        "build",
        "--market",
        "us",
        "--user-config",
        str(user_path),
        "--output",
        str(output_path),
    ])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["write_applied"] is True
    assert output_path.exists()
    validate_config(json.loads(output_path.read_text(encoding="utf-8")))
    cfg = json.loads(output_path.read_text(encoding="utf-8"))
    assert cfg[GENERATED_KEY]["market"] == "us"


def test_config_build_cli_dry_run_does_not_write_output(tmp_path: Path, capsys) -> None:
    from src.interfaces.cli.main import main

    user_path = _write_json(
        tmp_path / "user.us.json",
        {
            "account_settings": {
                "lx": {
                    "type": "futu",
                    "futu": {"account_id": "REAL_12345678"},
                }
            },
            "symbols": [{"symbol": "NVDA", "sell_put": {"max_strike": 160}}],
        },
    )
    output_path = tmp_path / "config.us.json"

    rc = main([
        "config",
        "build",
        "--market",
        "us",
        "--user-config",
        str(user_path),
        "--output",
        str(output_path),
        "--dry-run",
    ])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["write_applied"] is False
    assert not output_path.exists()


def test_config_build_cli_accepts_explicit_common_user_config(tmp_path: Path, capsys) -> None:
    from src.interfaces.cli.main import main

    common_path = _write_json(
        tmp_path / "user.common.json",
        {
            "watchdog": {"retry_enabled": False},
            "account_settings": {
                "lx": {
                    "type": "futu",
                    "futu": {"account_id": "REAL_12345678"},
                }
            },
        },
    )
    user_path = _write_json(
        tmp_path / "user.us.json",
        {
            "symbols": [{"symbol": "NVDA", "sell_put": {"max_strike": 160}}],
        },
    )
    output_path = tmp_path / "config.us.json"

    rc = main([
        "config",
        "build",
        "--market",
        "us",
        "--common-user-config",
        str(common_path),
        "--user-config",
        str(user_path),
        "--output",
        str(output_path),
    ])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["common_user_config_loaded"] is True
    cfg = json.loads(output_path.read_text(encoding="utf-8"))
    assert cfg["watchdog"]["retry_enabled"] is False
    assert cfg["accounts"] == ["lx"]
    validate_config(json.loads(json.dumps(cfg)))


def test_config_validate_market_rejects_stale_runtime_config(tmp_path: Path, capsys) -> None:
    from src.interfaces.cli.main import main

    user_path = _write_json(
        tmp_path / "user.us.json",
        {
            "account_settings": {
                "lx": {
                    "type": "futu",
                    "futu": {"account_id": "REAL_12345678"},
                }
            },
            "symbols": [{"symbol": "NVDA", "sell_put": {"max_strike": 160}}],
        },
    )
    output_path = tmp_path / "config.us.json"
    build_rc = main([
        "config",
        "build",
        "--market",
        "us",
        "--user-config",
        str(user_path),
        "--output",
        str(output_path),
    ])
    assert build_rc == 0
    capsys.readouterr()

    _write_json(
        user_path,
        {
            "account_settings": {
                "lx": {
                    "type": "futu",
                    "futu": {"account_id": "REAL_12345678"},
                }
            },
            "symbols": [{"symbol": "NVDA", "sell_put": {"max_strike": 170}}],
        },
    )

    validate_rc = main([
        "config",
        "validate",
        "--config-path",
        str(output_path),
        "--market",
        "us",
    ])
    payload = json.loads(capsys.readouterr().out)

    assert validate_rc == 2
    assert payload["ok"] is False
    assert payload["error"]["code"] == "CONFIG_ERROR"
    assert "runtime config is stale" in payload["error"]["message"]
    assert payload["error"]["details"]["errors"][0]["role"] == "market_user"


def test_config_validate_market_wraps_schedule_contract_error_as_json(tmp_path: Path, capsys) -> None:
    from src.interfaces.cli.main import main

    user_path = _write_json(
        tmp_path / "user.hk.json",
        {
            "account_settings": {
                "lx": {
                    "type": "futu",
                    "futu": {"account_id": "REAL_87654321"},
                }
            },
            "symbols": [{"symbol": "0700.HK", "sell_put": {"max_strike": 420}}],
        },
    )
    output_path = tmp_path / "config.hk.json"
    build_rc = main([
        "config",
        "build",
        "--market",
        "hk",
        "--user-config",
        str(user_path),
        "--output",
        str(output_path),
    ])
    assert build_rc == 0
    capsys.readouterr()

    cfg = json.loads(output_path.read_text(encoding="utf-8"))
    cfg["schedule"]["timezone"] = "America/New_York"
    output_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    validate_rc = main([
        "config",
        "validate",
        "--config-path",
        str(output_path),
        "--market",
        "hk",
    ])
    payload = json.loads(capsys.readouterr().out)

    assert validate_rc == 2
    assert payload["ok"] is False
    assert payload["error"]["code"] == "CONFIG_ERROR"
    assert "runtime schedule timezone does not match market" in payload["error"]["message"]
    assert payload["error"]["details"]["market"] == "hk"


def test_config_explain_cli_outputs_source_trace(tmp_path: Path, capsys) -> None:
    from src.interfaces.cli.main import main

    common_path = _write_json(
        tmp_path / "user.common.json",
        {
            "option_positions": {"auto_close": {"enabled": False}},
            "account_settings": {
                "lx": {
                    "type": "futu",
                    "futu": {"account_id": "REAL_12345678"},
                }
            },
        },
    )
    user_path = _write_json(
        tmp_path / "user.us.json",
        {
            "symbols": [{"symbol": "NVDA", "sell_put": {"max_strike": 160}}],
        },
    )

    rc = main([
        "config",
        "explain",
        "--market",
        "us",
        "--key",
        "option_positions.auto_close.enabled",
        "--common-user-config",
        str(common_path),
        "--user-config",
        str(user_path),
    ])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["value"] is False
    assert payload["source"] == "common_user_config"
    assert [item["source"] for item in payload["trace"]] == ["system.defaults", "common_user_config"]
