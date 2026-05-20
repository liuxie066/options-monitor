from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from src.application.agent_tool_contracts import AgentToolError
from src.application.config_defaults import DEFAULT_CONFIG, DEFAULT_CONFIG_REF
from src.application.config_validator import validate_config
from src.application.config_yaml import RESOLVED_KEY, resolve_yaml_runtime_config
from src.application.runtime_config_freshness import GENERATED_KEY


REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_yaml(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _minimal_yaml() -> str:
    return """\
accounts:
  lx:
    type: futu
    futu_account_id: "REAL_12345678"
  sy:
    type: external_holdings
    holdings_account: sy

features:
  close_advice: false

markets:
  us:
    accounts: [lx, sy]
    symbols:
      - NVDA
      - FUTU
    overrides:
      FUTU:
        sell_put:
          dte: [20, 45]
          strike: [55, 85]
        yield_enhancement: true

  hk:
    accounts: [lx]
    symbols:
      - "0700.HK"

inbound:
  feishu_ws:
    ack_reaction: THUMBSUP
"""


def _write_migration_sources(tmp_path: Path) -> tuple[Path, Path, Path]:
    common_path = tmp_path / "user.common.json"
    common_path.write_text(
        json.dumps(
            {"account_settings": {"lx": {"type": "futu", "futu": {"account_id": "REAL_12345678"}}}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    us_path = tmp_path / "user.us.json"
    us_path.write_text(json.dumps({"symbols": [{"symbol": "NVDA"}]}, ensure_ascii=False), encoding="utf-8")
    hk_path = tmp_path / "user.hk.json"
    hk_path.write_text(json.dumps({"symbols": [{"symbol": "0700.HK"}]}, ensure_ascii=False), encoding="utf-8")
    return common_path, us_path, hk_path


def test_yaml_config_resolves_user_overrides_and_defaults(tmp_path: Path) -> None:
    config_path = _write_yaml(tmp_path / "config.yaml", _minimal_yaml())

    cfg, meta = resolve_yaml_runtime_config(repo_root=REPO_ROOT, market="us", config_path=config_path)

    assert meta["source_format"] == "yaml"
    assert cfg["accounts"] == ["lx", "sy"]
    assert cfg["account_settings"]["lx"]["futu"]["account_id"] == "REAL_12345678"
    assert cfg["account_settings"]["sy"] == {"type": "external_holdings", "holdings_account": "sy"}
    assert cfg["portfolio"]["source_by_account"] == {"lx": "futu", "sy": "holdings"}
    assert cfg["close_advice"]["enabled"] is False
    assert cfg["inbound"]["feishu_ws"]["ack_reaction"] == "THUMBSUP"
    assert cfg["symbols"][0]["symbol"] == "NVDA"
    assert cfg["symbols"][0]["sell_put"]["min_dte"] == 20
    futu = cfg["symbols"][1]
    assert futu["symbol"] == "FUTU"
    assert futu["sell_put"]["min_dte"] == 20
    assert futu["sell_put"]["max_dte"] == 45
    assert futu["sell_put"]["min_strike"] == 55
    assert futu["sell_put"]["max_strike"] == 85
    assert futu["yield_enhancement"]["enabled"] is True
    assert cfg[GENERATED_KEY]["source_format"] == "yaml"
    assert cfg[GENERATED_KEY]["sources"][0]["inline"] is True
    assert cfg[GENERATED_KEY]["sources"][0]["ref"] == DEFAULT_CONFIG_REF
    assert cfg[RESOLVED_KEY]["market"] == "us"
    assert cfg[RESOLVED_KEY]["default_source"] == DEFAULT_CONFIG_REF

    validate_config(json.loads(json.dumps(cfg)))


def test_default_config_matches_legacy_system_json() -> None:
    system_json = json.loads((REPO_ROOT / "configs" / "system.json").read_text(encoding="utf-8"))

    assert DEFAULT_CONFIG == system_json


def test_yaml_config_requires_explicit_market(tmp_path: Path) -> None:
    config_path = _write_yaml(
        tmp_path / "config.yaml",
        """\
accounts:
  lx:
    type: futu
markets:
  us:
    accounts: [lx]
    symbols: [NVDA]
""",
    )

    with pytest.raises(AgentToolError, match="markets.hk is required"):
        resolve_yaml_runtime_config(repo_root=REPO_ROOT, market="hk", config_path=config_path)


def test_yaml_config_rejects_tabs(tmp_path: Path) -> None:
    config_path = _write_yaml(
        tmp_path / "config.yaml",
        "accounts:\n\tlx:\n    type: futu\n",
    )

    with pytest.raises(AgentToolError, match="must use spaces"):
        resolve_yaml_runtime_config(repo_root=REPO_ROOT, market="us", config_path=config_path)


def test_yaml_config_rejects_global_yield_enhancement_switch(tmp_path: Path) -> None:
    config_path = _write_yaml(
        tmp_path / "config.yaml",
        """\
accounts:
  lx:
    type: futu
features:
  yield_enhancement: true
markets:
  us:
    accounts: [lx]
    symbols: [NVDA]
""",
    )

    with pytest.raises(AgentToolError, match="not a global feature switch"):
        resolve_yaml_runtime_config(repo_root=REPO_ROOT, market="us", config_path=config_path)


def test_yaml_config_rejects_write_gates(tmp_path: Path) -> None:
    config_path = _write_yaml(
        tmp_path / "config.yaml",
        """\
accounts:
  lx:
    type: futu
writes:
  feishu: true
markets:
  us:
    accounts: [lx]
    symbols: [NVDA]
""",
    )

    with pytest.raises(AgentToolError, match="is not a config.yaml field"):
        resolve_yaml_runtime_config(repo_root=REPO_ROOT, market="us", config_path=config_path)


def test_yaml_config_rejects_trade_intake_write_policy(tmp_path: Path) -> None:
    config_path = _write_yaml(
        tmp_path / "config.yaml",
        """\
accounts:
  lx:
    type: futu
trade_intake:
  mode: apply
markets:
  us:
    accounts: [lx]
    symbols: [NVDA]
""",
    )

    with pytest.raises(AgentToolError, match="trade_intake is not supported"):
        resolve_yaml_runtime_config(repo_root=REPO_ROOT, market="us", config_path=config_path)


def test_yaml_config_rejects_override_for_symbol_not_in_market(tmp_path: Path) -> None:
    config_path = _write_yaml(
        tmp_path / "config.yaml",
        """\
accounts:
  lx:
    type: futu
markets:
  us:
    accounts: [lx]
    symbols: [NVDA]
    overrides:
      FUTU:
        sell_put:
          dte: [20, 45]
""",
    )

    with pytest.raises(AgentToolError, match="must also appear in symbols"):
        resolve_yaml_runtime_config(repo_root=REPO_ROOT, market="us", config_path=config_path)


def test_config_build_cli_supports_yaml_source(tmp_path: Path, capsys) -> None:
    from src.interfaces.cli.main import main

    config_path = _write_yaml(tmp_path / "config.yaml", _minimal_yaml())
    output_path = tmp_path / "resolved" / "config.us.json"

    rc = main([
        "config",
        "build",
        "--source",
        "yaml",
        "--market",
        "us",
        "--config-yaml",
        str(config_path),
        "--output",
        str(output_path),
    ])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["source_format"] == "yaml"
    assert payload["write_applied"] is True
    assert output_path.exists()
    cfg = json.loads(output_path.read_text(encoding="utf-8"))
    assert cfg[GENERATED_KEY]["source_format"] == "yaml"
    assert cfg[RESOLVED_KEY]["config_yaml_path"].endswith("config.yaml")
    validate_config(cfg)


def test_config_validate_cli_supports_yaml_source(tmp_path: Path, capsys) -> None:
    from src.interfaces.cli.main import main

    config_path = _write_yaml(tmp_path / "config.yaml", _minimal_yaml())

    rc = main([
        "config",
        "validate",
        "--source",
        "yaml",
        "--market",
        "us",
        "--config-yaml",
        str(config_path),
    ])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["source_format"] == "yaml"


def test_config_migrate_yaml_preview_generates_valid_yaml(tmp_path: Path) -> None:
    from src.application.config_yaml_migration import preview_config_yaml_migration

    common_path = tmp_path / "user.common.json"
    common_path.write_text(
        json.dumps(
            {
                "account_settings": {
                    "lx": {"type": "futu", "futu": {"account_id": "REAL_12345678"}},
                    "sy": {"type": "external_holdings", "holdings_account": "sy"},
                },
                "inbound": {"feishu_ws": {"ack_reaction": "THUMBSUP"}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    us_path = tmp_path / "user.us.json"
    us_path.write_text(
        json.dumps(
            {
                "symbols": [
                    {"symbol": "NVDA", "sell_put": {"max_strike": 150.0}},
                    {"symbol": "PDD", "yield_enhancement": {"enabled": True}},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    hk_path = tmp_path / "user.hk.json"
    hk_path.write_text(
        json.dumps({"symbols": [{"symbol": "0700.HK", "sell_put": {"max_strike": 450}}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    output_path = tmp_path / "config.yaml"

    out = preview_config_yaml_migration(
        repo_root=REPO_ROOT,
        common_user_config_path=common_path,
        us_user_config_path=us_path,
        hk_user_config_path=hk_path,
        output_config_yaml_path=output_path,
    )

    assert out["ok"] is True
    assert out["dry_run"] is True
    assert out["write_applied"] is False
    assert not output_path.exists()
    assert out["validation"]["us"]["equivalent_to_legacy_runtime"] is True
    assert out["validation"]["hk"]["equivalent_to_legacy_runtime"] is True
    assert out["validation"]["us"]["legacy_accounts"] == ["lx", "sy"]
    assert any("markets.us.accounts inferred" in item for item in out["warnings"])

    payload = yaml.safe_load(out["yaml"])
    assert payload["accounts"]["lx"]["futu_account_id"] == "REAL_12345678"
    assert payload["markets"]["us"]["symbols"] == ["NVDA", "PDD"]
    assert payload["markets"]["us"]["overrides"]["PDD"]["yield_enhancement"] is True

    migrated_path = tmp_path / "generated.yaml"
    migrated_path.write_text(out["yaml"], encoding="utf-8")
    cfg, _meta = resolve_yaml_runtime_config(repo_root=REPO_ROOT, market="us", config_path=migrated_path)
    validate_config(json.loads(json.dumps(cfg)))


def test_config_migrate_yaml_preview_can_override_market_accounts(tmp_path: Path) -> None:
    from src.application.config_yaml_migration import preview_config_yaml_migration

    common_path = tmp_path / "user.common.json"
    common_path.write_text(
        json.dumps(
            {
                "account_settings": {
                    "lx": {"type": "futu", "futu": {"account_id": "REAL_12345678"}},
                    "sy": {"type": "external_holdings", "holdings_account": "sy"},
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    us_path = tmp_path / "user.us.json"
    us_path.write_text(json.dumps({"symbols": [{"symbol": "NVDA"}]}, ensure_ascii=False), encoding="utf-8")
    hk_path = tmp_path / "user.hk.json"
    hk_path.write_text(json.dumps({"symbols": [{"symbol": "0700.HK"}]}, ensure_ascii=False), encoding="utf-8")

    out = preview_config_yaml_migration(
        repo_root=REPO_ROOT,
        common_user_config_path=common_path,
        us_user_config_path=us_path,
        hk_user_config_path=hk_path,
        hk_accounts=["lx"],
    )

    assert out["ok"] is True
    assert out["validation"]["hk"]["legacy_accounts"] == ["lx", "sy"]
    assert out["validation"]["hk"]["accounts"] == ["lx"]
    assert out["validation"]["hk"]["equivalent_to_legacy_runtime"] is False
    assert any("markets.hk.accounts overridden from lx, sy to lx" in item for item in out["warnings"])
    payload = yaml.safe_load(out["yaml"])
    assert payload["markets"]["hk"]["accounts"] == ["lx"]


def test_config_migrate_yaml_cli_is_dry_run(tmp_path: Path, capsys) -> None:
    from src.interfaces.cli.main import main

    common_path, us_path, hk_path = _write_migration_sources(tmp_path)
    output_path = tmp_path / "config.yaml"

    rc = main([
        "config",
        "migrate-yaml",
        "--common-user-config",
        str(common_path),
        "--us-user-config",
        str(us_path),
        "--hk-user-config",
        str(hk_path),
        "--hk-accounts",
        "lx",
        "--output",
        str(output_path),
    ])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["dry_run"] is True
    assert out["write_applied"] is False
    assert not output_path.exists()
    assert "markets:" in out["yaml"]
    assert out["validation"]["hk"]["accounts"] == ["lx"]


def test_config_migrate_yaml_cli_apply_writes_backup_and_validates(tmp_path: Path, capsys) -> None:
    from src.interfaces.cli.main import main

    common_path, us_path, hk_path = _write_migration_sources(tmp_path)
    output_path = tmp_path / "config.yaml"
    output_path.write_text("old: true\n", encoding="utf-8")

    rc = main([
        "config",
        "migrate-yaml",
        "--common-user-config",
        str(common_path),
        "--us-user-config",
        str(us_path),
        "--hk-user-config",
        str(hk_path),
        "--output",
        str(output_path),
        "--apply",
    ])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["dry_run"] is False
    assert out["write_applied"] is True
    assert out["backup_path"]
    backup_path = Path(out["backup_path"])
    assert backup_path.exists()
    assert backup_path.read_text(encoding="utf-8") == "old: true\n"
    assert output_path.exists()
    payload = yaml.safe_load(output_path.read_text(encoding="utf-8"))
    assert payload["markets"]["us"]["symbols"] == ["NVDA"]
    assert payload["markets"]["hk"]["symbols"] == ["0700.HK"]
    assert out["post_write_validation"]["us"]["ok"] is True
    assert out["post_write_validation"]["us"]["dry_run"] is True
    assert out["post_write_validation"]["us"]["write_applied"] is False
    assert out["post_write_validation"]["hk"]["ok"] is True


def test_config_migrate_yaml_cli_apply_can_skip_backup(tmp_path: Path, capsys) -> None:
    from src.interfaces.cli.main import main

    common_path, us_path, hk_path = _write_migration_sources(tmp_path)
    output_path = tmp_path / "config.yaml"
    output_path.write_text("old: true\n", encoding="utf-8")

    rc = main([
        "config",
        "migrate-yaml",
        "--common-user-config",
        str(common_path),
        "--us-user-config",
        str(us_path),
        "--hk-user-config",
        str(hk_path),
        "--output",
        str(output_path),
        "--apply",
        "--no-backup",
    ])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is False
    assert out["write_applied"] is True
    assert out["backup_path"] is None
    assert not list(tmp_path.glob("config.yaml.bak.*"))
