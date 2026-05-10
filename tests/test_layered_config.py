from __future__ import annotations

import json
from pathlib import Path

from scripts.validate_config import validate_config
from src.application.layered_config import build_layered_runtime_config


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
    assert cfg["portfolio"]["data_config"] == "secrets/portfolio.sqlite.json"
    assert cfg["portfolio"]["futu"] == {"host": "127.0.0.1", "port": 11111}
    assert cfg["portfolio"]["source_by_account"] == {"lx": "futu"}
    assert cfg["trade_intake"]["mode"] == "apply"
    assert cfg["trade_intake"]["account_mapping"]["futu"] == {"REAL_12345678": "lx"}
    assert cfg["schedule"]["market_timezone"] == "America/New_York"
    assert cfg["schedule"]["market_close"] == "14:00"
    assert cfg["schedule"]["final_notify_before_close_min"] == 0
    assert cfg["intake"]["symbol_aliases"]["英伟达"] == "NVDA"
    assert cfg["symbols"][0]["broker"] == "US"
    assert "market" not in cfg["symbols"][0]
    assert cfg["symbols"][0]["fetch"]["source"] == "futu"
    assert cfg["symbols"][0]["fetch"]["limit_expirations"] == 10
    assert cfg["symbols"][0]["sell_put"]["min_dte"] == 20
    assert cfg["symbols"][0]["sell_put"]["max_strike"] == 160
    assert cfg["symbols"][0]["yield_enhancement"]["enabled"] is True
    assert "notifications" not in cfg

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
    assert cfg["portfolio"]["data_config"] == "secrets/portfolio.sqlite.json"
    assert cfg["portfolio"]["source_by_account"] == {"lx": "futu", "sy": "holdings"}
    assert cfg["trade_intake"]["mode"] == "apply"
    assert cfg["trade_intake"]["account_mapping"]["futu"] == {"REAL_87654321": "lx"}
    assert cfg["schedule"]["market_timezone"] == "Asia/Hong_Kong"
    assert cfg["schedule"]["market_break_start"] == "12:00"
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


def test_tracked_layered_examples_validate() -> None:
    for market in ("us", "hk"):
        cfg, meta = build_layered_runtime_config(
            repo_root=REPO_ROOT,
            market=market,
            user_config_path=REPO_ROOT / "configs" / "examples" / f"user.example.{market}.json",
        )
        assert meta["symbols"]
        validate_config(json.loads(json.dumps(cfg)))


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
