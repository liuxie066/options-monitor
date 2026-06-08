from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from src.application.agent_tool_contracts import AgentToolError
from src.application.config_defaults import DEFAULT_CONFIG, DEFAULT_CONFIG_REF
from src.application.config_validator import validate_config
from src.application.config_yaml import (
    RESOLVED_KEY,
    build_yaml_assistant_config_file,
    explain_yaml_config_key,
    resolve_yaml_assistant_config,
    resolve_yaml_runtime_config,
)
from src.application.config_yaml_init import init_yaml_config
from src.application.config_yaml_symbols import set_yaml_symbol_config
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

assistant:
  enabled: true
  context_window_messages: 6
  default_market_scope: us
  planner:
    enabled: true
  llm:
    provider: ""
    base_url: ""
    model: ""
    api_key_env: OM_LLM_API_KEY
    confidence_min: 0.75
    timeout_seconds: 20
    max_output_tokens: 512

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
        covered_call:
          enabled: true
          dte: [20, 60]
          strike: [90, 120]
        combo_yield: true

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
    assert "assistant" not in cfg
    assert "inbound" not in cfg
    assert cfg["symbols"][0]["symbol"] == "NVDA"
    assert cfg["symbols"][0]["sell_put"]["min_dte"] == 20
    futu = cfg["symbols"][1]
    assert futu["symbol"] == "FUTU"
    assert futu["sell_put"]["min_dte"] == 20
    assert futu["sell_put"]["max_dte"] == 45
    assert futu["sell_put"]["min_strike"] == 55
    assert futu["sell_put"]["max_strike"] == 85
    assert "covered_call" not in futu
    assert futu["sell_call"]["enabled"] is True
    assert futu["sell_call"]["min_dte"] == 20
    assert futu["sell_call"]["max_dte"] == 60
    assert futu["sell_call"]["min_strike"] == 90
    assert futu["sell_call"]["max_strike"] == 120
    assert futu["combo_yield"]["enabled"] is True
    sell_put_template = cfg["templates"]["put_base"]["sell_put"]
    sell_call_template = cfg["templates"]["call_base"]["sell_call"]
    for side_cfg in (sell_put_template, sell_call_template):
        assert side_cfg["strategy"] == "insurance_underwriting"
        assert "concentration" not in side_cfg
        assert "score_weights" not in side_cfg
        assert "short_vol" not in side_cfg
        assert side_cfg["min_iv_rv_ratio"] == 1.10
        assert side_cfg["min_iv_minus_rv"] == 0.05
        assert side_cfg["reject_event_risk"] is True
        assert side_cfg["event_source_fail_closed"] is True
    for side_cfg in (futu["sell_put"], futu["sell_call"]):
        assert "concentration" not in side_cfg
        assert "score_weights" not in side_cfg
        assert "short_vol" not in side_cfg
    assert cfg[GENERATED_KEY]["source_format"] == "yaml"
    assert cfg[GENERATED_KEY]["sources"][0]["inline"] is True
    assert cfg[GENERATED_KEY]["sources"][0]["ref"] == DEFAULT_CONFIG_REF
    assert cfg[RESOLVED_KEY]["market"] == "us"
    assert cfg[RESOLVED_KEY]["default_source"] == DEFAULT_CONFIG_REF

    validate_config(json.loads(json.dumps(cfg)))


def test_yaml_config_accepts_legacy_sell_call_authoring_key(tmp_path: Path) -> None:
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
      NVDA:
        sell_call:
          enabled: true
          dte: [20, 45]
          strike: [150, 180]
""",
    )

    cfg, _meta = resolve_yaml_runtime_config(repo_root=REPO_ROOT, market="us", config_path=config_path)

    assert cfg["symbols"][0]["sell_call"]["enabled"] is True
    assert cfg["symbols"][0]["sell_call"]["min_dte"] == 20
    assert cfg["symbols"][0]["sell_call"]["max_strike"] == 180
    validate_config(json.loads(json.dumps(cfg)))


def test_yaml_config_rejects_covered_call_and_sell_call_conflict(tmp_path: Path) -> None:
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
      NVDA:
        covered_call:
          enabled: false
        sell_call:
          enabled: false
""",
    )

    with pytest.raises(AgentToolError, match="cannot define both covered_call and sell_call"):
        resolve_yaml_runtime_config(repo_root=REPO_ROOT, market="us", config_path=config_path)


def test_yaml_config_explain_maps_covered_call_authoring_key(tmp_path: Path) -> None:
    config_path = _write_yaml(tmp_path / "config.yaml", _minimal_yaml())

    out = explain_yaml_config_key(
        repo_root=REPO_ROOT,
        market="us",
        key="symbols.1.covered_call.min_dte",
        config_path=config_path,
    )

    assert out["exists"] is True
    assert out["value"] == 20
    assert out["runtime_path"] == "symbols.1.sell_call.min_dte"
    assert any("covered_call" in item and "sell_call" in item for item in out["notes"])


def test_yaml_config_maps_covered_call_passthrough_authoring_keys(tmp_path: Path) -> None:
    config_path = _write_yaml(
        tmp_path / "config.yaml",
        """\
accounts:
  lx:
    type: futu
templates:
  call_base:
    covered_call:
      min_strike_cost_multiplier: 1.05
symbol_defaults:
  covered_call:
    enabled: false
alert_policy:
  covered_call:
    medium_annual: 0.07
markets:
  us:
    accounts: [lx]
    symbols: [NVDA]
""",
    )

    cfg, _meta = resolve_yaml_runtime_config(repo_root=REPO_ROOT, market="us", config_path=config_path)

    assert "covered_call" not in cfg["templates"]["call_base"]
    assert cfg["templates"]["call_base"]["sell_call"]["min_strike_cost_multiplier"] == 1.05
    assert "covered_call" not in cfg["symbols"][0]
    assert cfg["symbols"][0]["sell_call"]["enabled"] is False
    assert "covered_call" not in cfg["alert_policy"]
    assert cfg["alert_policy"]["sell_call"]["medium_annual"] == 0.07


def test_yaml_symbol_set_adds_hk_call_only_symbol_as_dry_run(tmp_path: Path) -> None:
    config_path = _write_yaml(tmp_path / "config.yaml", _minimal_yaml())
    before = config_path.read_text(encoding="utf-8")

    out = set_yaml_symbol_config(
        repo_root=REPO_ROOT,
        market="hk",
        symbol="09898",
        config_path=config_path,
        covered_call_min_strike=85,
        apply=False,
    )

    assert out["dry_run"] is True
    assert out["write_applied"] is False
    assert out["summary"]["canonical_symbol"] == "9898.HK"
    assert out["summary"]["symbol_added"] is True
    assert out["summary"]["entry"] == {
        "sell_put": {"enabled": False},
        "covered_call": {"enabled": True, "min_strike": 85.0},
        "use": ["call_base"],
    }
    assert out["validation"]["hk"]["ok"] is True
    assert config_path.read_text(encoding="utf-8") == before


def test_yaml_symbol_set_apply_rebuilds_runtime_configs(tmp_path: Path) -> None:
    config_path = _write_yaml(tmp_path / "config.yaml", _minimal_yaml())
    runtime_root = tmp_path / "runtime"

    out = set_yaml_symbol_config(
        repo_root=REPO_ROOT,
        market="hk",
        symbol="09898",
        config_path=config_path,
        covered_call_min_strike=85,
        apply=True,
        rebuild_runtime_root=runtime_root,
    )

    assert out["dry_run"] is False
    assert out["write_applied"] is True
    assert out["backup_path"]
    assert Path(out["backup_path"]).exists()
    doc = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "9898.HK" in doc["markets"]["hk"]["symbols"]
    assert doc["markets"]["hk"]["overrides"]["9898.HK"] == {
        "sell_put": {"enabled": False},
        "covered_call": {"enabled": True, "min_strike": 85.0},
        "use": ["call_base"],
    }
    hk_runtime = json.loads((runtime_root / "config.hk.json").read_text(encoding="utf-8"))
    item = next(row for row in hk_runtime["symbols"] if row["symbol"] == "9898.HK")
    assert item["sell_put"]["enabled"] is False
    assert item["sell_call"]["enabled"] is True
    assert item["sell_call"]["min_strike"] == 85.0
    assert (runtime_root / "config.us.json").exists()
    assert (runtime_root / "resolved" / "config.assistant.json").exists()


def test_yaml_symbol_set_preserves_existing_legacy_sell_call_key(tmp_path: Path) -> None:
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
  hk:
    accounts: [lx]
    symbols: [0700.HK]
    overrides:
      0700.HK:
        use:
        - call_base
        sell_call:
          enabled: true
          min_strike: 550
""",
    )

    out = set_yaml_symbol_config(
        repo_root=REPO_ROOT,
        market="hk",
        symbol="700",
        config_path=config_path,
        covered_call_min_strike=560,
        apply=False,
    )

    assert out["summary"]["entry"]["sell_call"]["min_strike"] == 560.0
    assert out["summary"]["entry"]["sell_put"]["enabled"] is False
    assert "covered_call" not in out["summary"]["entry"]


def test_yaml_symbol_set_rejects_empty_setting(tmp_path: Path) -> None:
    config_path = _write_yaml(tmp_path / "config.yaml", _minimal_yaml())

    with pytest.raises(AgentToolError, match="at least one symbol setting is required"):
        set_yaml_symbol_config(
            repo_root=REPO_ROOT,
            market="hk",
            symbol="09898",
            config_path=config_path,
            apply=False,
        )


def test_yaml_assistant_config_merges_system_defaults(tmp_path: Path) -> None:
    config_path = _write_yaml(
        tmp_path / "config.yaml",
        """\
accounts:
  lx:
    type: external_holdings
    holdings_account: lx
markets:
  us:
    accounts: [lx]
    symbols: [FUTU]
inbound:
  feishu_ws:
    ack_reaction: THUMBSUP
  wechat_clawbot:
    allowed_senders: wechat:user_1
    poll_interval_sec: 0.5
""",
    )

    cfg, _meta = resolve_yaml_assistant_config(repo_root=REPO_ROOT, config_path=config_path)

    assert cfg["assistant"]["enabled"] is True
    assert cfg["assistant"]["planner"]["enabled"] is True
    assert cfg["assistant"]["llm"]["api_key_env"] == "OM_LLM_API_KEY"
    assert cfg["inbound"]["feishu_ws"]["reply_enabled"] is True
    assert cfg["inbound"]["feishu_ws"]["queue_size"] == 100
    assert cfg["inbound"]["feishu_ws"]["ack_reaction"] == "THUMBSUP"
    assert cfg["inbound"]["wechat_clawbot"]["label"] == "default"
    assert cfg["inbound"]["wechat_clawbot"]["allowed_senders"] == "wechat:user_1"
    assert cfg["inbound"]["wechat_clawbot"]["reply_enabled"] is True
    assert cfg["inbound"]["wechat_clawbot"]["max_reply_chars"] == 3500
    assert cfg["inbound"]["wechat_clawbot"]["poll_interval_sec"] == 0.5


def test_yaml_assistant_config_unwraps_explicit_system_defaults(tmp_path: Path) -> None:
    config_path = _write_yaml(
        tmp_path / "config.yaml",
        """\
accounts:
  lx:
    type: external_holdings
    holdings_account: lx
markets:
  us:
    accounts: [lx]
    symbols: [FUTU]
""",
    )
    system_path = tmp_path / "system.json"
    system_path.write_text(
        json.dumps(
            {
                "defaults": {
                    "assistant": {
                        "enabled": True,
                        "planner": {"enabled": True},
                        "context_window_messages": 3,
                        "default_market_scope": "hk",
                        "llm": {"provider": "openai"},
                    },
                    "inbound": {"feishu_ws": {"ack_reaction": "SMILE", "queue_size": 7}},
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    cfg, _meta = resolve_yaml_assistant_config(
        repo_root=REPO_ROOT,
        config_path=config_path,
        system_config_path=system_path,
    )

    assert cfg["assistant"]["enabled"] is True
    assert cfg["assistant"]["planner"]["enabled"] is True
    assert cfg["assistant"]["context_window_messages"] == 3
    assert cfg["assistant"]["default_market_scope"] == "hk"
    assert cfg["assistant"]["llm"]["provider"] == "openai"
    assert cfg["inbound"]["feishu_ws"]["ack_reaction"] == "SMILE"
    assert cfg["inbound"]["feishu_ws"]["queue_size"] == 7

    output_path = tmp_path / "config.assistant.json"
    build_yaml_assistant_config_file(
        repo_root=REPO_ROOT,
        config_path=config_path,
        system_config_path=system_path,
        output_config_path=output_path,
    )
    generated = json.loads(output_path.read_text(encoding="utf-8"))
    assert f"--system-config {system_path}" in generated[GENERATED_KEY]["rebuild_command"]


def test_yaml_assistant_config_resolves_active_model_profile(tmp_path: Path) -> None:
    config_path = _write_yaml(
        tmp_path / "config.yaml",
        """\
accounts:
  lx:
    type: external_holdings
    holdings_account: lx
markets:
  us:
    accounts: [lx]
    symbols: [FUTU]
assistant:
  enabled: true
  planner:
    enabled: true
  active_model: deepseek-default
  models:
    deepseek-default:
      provider: deepseek
      model: deepseek-chat
      api_key_env: DEEPSEEK_API_KEY
    openai-default:
      provider: openai
      model: gpt-5.2
      api_key_env: OM_LLM_API_KEY
""",
    )

    cfg, _meta = resolve_yaml_assistant_config(repo_root=REPO_ROOT, config_path=config_path)

    assistant = cfg["assistant"]
    assert "models" not in assistant
    assert "active_model" not in assistant
    assert assistant["llm"]["provider"] == "deepseek"
    assert assistant["llm"]["base_url"] == "https://api.deepseek.com"
    assert assistant["llm"]["model"] == "deepseek-chat"
    assert assistant["llm"]["api_key_env"] == "DEEPSEEK_API_KEY"
    resolved = cfg[RESOLVED_KEY]["assistant_models"]
    assert resolved["active_model"] == "deepseek-default"
    assert resolved["profile_count"] == 2
    assert resolved["resolved_profile"]["provider"] == "deepseek"


def test_yaml_assistant_config_rejects_unknown_active_model_profile(tmp_path: Path) -> None:
    config_path = _write_yaml(
        tmp_path / "config.yaml",
        """\
accounts:
  lx:
    type: external_holdings
    holdings_account: lx
markets:
  us:
    accounts: [lx]
    symbols: [FUTU]
assistant:
  enabled: true
  planner:
    enabled: true
  active_model: missing
  models:
    deepseek-default:
      provider: deepseek
      model: deepseek-chat
      api_key_env: DEEPSEEK_API_KEY
""",
    )

    with pytest.raises(AgentToolError, match="unknown model profile"):
        resolve_yaml_assistant_config(repo_root=REPO_ROOT, config_path=config_path)


def test_yaml_assistant_model_profiles_reject_inline_api_key(tmp_path: Path) -> None:
    config_path = _write_yaml(
        tmp_path / "config.yaml",
        """\
accounts:
  lx:
    type: external_holdings
    holdings_account: lx
markets:
  us:
    accounts: [lx]
    symbols: [FUTU]
assistant:
  enabled: true
  planner:
    enabled: true
  active_model: unsafe
  models:
    unsafe:
      provider: deepseek
      model: deepseek-chat
      api_key: sk-secret
""",
    )

    with pytest.raises(AgentToolError, match="must not store secret values"):
        resolve_yaml_assistant_config(repo_root=REPO_ROOT, config_path=config_path)


def test_default_config_matches_legacy_system_json() -> None:
    system_json = json.loads((REPO_ROOT / "configs" / "system.json").read_text(encoding="utf-8"))

    assert DEFAULT_CONFIG == system_json


def test_config_init_writes_starter_yaml_and_runtime_configs(tmp_path: Path) -> None:
    output_path = tmp_path / "config.yaml"
    runtime_dir = tmp_path / "runtime"

    out = init_yaml_config(
        repo_root=REPO_ROOT,
        output_config_yaml_path=output_path,
        runtime_output_dir=runtime_dir,
        futu_acc_id="12345678",
        account_label="lx",
    )

    assert out["ok"] is True
    assert out["write_applied"] is True
    assert output_path.exists()
    assert (runtime_dir / "config.us.json").exists()
    assert (runtime_dir / "config.hk.json").exists()
    payload = yaml.safe_load(output_path.read_text(encoding="utf-8"))
    assert payload["accounts"]["lx"]["futu_account_id"] == "12345678"
    assert payload["assistant"]["enabled"] is True
    assert payload["assistant"]["planner"]["enabled"] is True
    assert payload["assistant"]["context_window_messages"] == 8
    assert "default_market_scope" not in payload["assistant"]
    assert payload["assistant"]["active_model"] == "deepseek-default"
    assert payload["assistant"]["models"]["deepseek-default"]["model"] == "deepseek-chat"
    assert payload["assistant"]["models"]["deepseek-default"]["api_key_env"] == "DEEPSEEK_API_KEY"
    assert payload["assistant"]["models"]["openai-default"]["api_key_env"] == "OM_LLM_API_KEY"
    assert payload["markets"]["us"]["accounts"] == ["lx", "sy"]
    assert payload["markets"]["hk"]["symbols"] == ["0700.HK", "9992.HK"]
    us_cfg = json.loads((runtime_dir / "config.us.json").read_text(encoding="utf-8"))
    hk_cfg = json.loads((runtime_dir / "config.hk.json").read_text(encoding="utf-8"))
    assistant_cfg = json.loads((runtime_dir / "config.assistant.json").read_text(encoding="utf-8"))
    assert us_cfg[GENERATED_KEY]["source_format"] == "yaml"
    assert "assistant" not in us_cfg
    assert "inbound" not in us_cfg
    assert hk_cfg[GENERATED_KEY]["market"] == "hk"
    assert assistant_cfg["assistant"]["enabled"] is True
    assert assistant_cfg["assistant"]["planner"]["enabled"] is True
    assert assistant_cfg["assistant"]["context_window_messages"] == 8
    assert "default_market_scope" not in assistant_cfg["assistant"]
    assert "active_model" not in assistant_cfg["assistant"]
    assert "models" not in assistant_cfg["assistant"]
    assert assistant_cfg["assistant"]["llm"]["base_url"] == "https://api.deepseek.com"
    assert assistant_cfg["assistant"]["llm"]["api_key_env"] == "DEEPSEEK_API_KEY"
    assert assistant_cfg["assistant"]["llm"]["timeout_seconds"] == 20
    assert assistant_cfg["assistant"]["llm"]["max_output_tokens"] == 512
    assert assistant_cfg["inbound"]["feishu_ws"]["ack_reaction"] == "THUMBSUP"


def test_config_init_cli_supports_dry_run(tmp_path: Path, capsys) -> None:
    from src.interfaces.cli.main import main

    output_path = tmp_path / "config.yaml"
    runtime_dir = tmp_path / "runtime"

    rc = main([
        "config",
        "init",
        "--output",
        str(output_path),
        "--runtime-output-dir",
        str(runtime_dir),
        "--dry-run",
    ])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["dry_run"] is True
    assert out["write_applied"] is False
    assert "markets:" in out["yaml"]
    assert not output_path.exists()
    assert not runtime_dir.exists()


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


def test_yaml_config_rejects_global_combo_yield_switch(tmp_path: Path) -> None:
    config_path = _write_yaml(
        tmp_path / "config.yaml",
        """\
accounts:
  lx:
    type: futu
features:
  combo_yield: true
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
                "agent": {
                    "runtime": {"enabled": True, "context_window_messages": 6},
                    "llm": {
                        "enabled": True,
                        "provider": "deepseek",
                        "base_url": "https://api.deepseek.com",
                        "model": "deepseek-v4-flash",
                        "api_key_env": "DEEPSEEK_API_KEY",
                    },
                },
                "inbound": {"feishu_ws": {"ack_reaction": "THUMBSUP"}},
                "alert_policy": {"sell_call": {"medium_annual": 0.07}},
                "templates": {"call_base": {"sell_call": {"min_strike_cost_multiplier": 1.05}}},
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
                    {
                        "symbol": "PDD",
                        "sell_call": {"enabled": True, "min_dte": 20, "max_dte": 45, "min_strike": 120},
                        "combo_yield": {"enabled": True},
                    },
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
    assert "agent" not in payload
    assert payload["assistant"]["enabled"] is True
    assert payload["assistant"]["planner"]["enabled"] is True
    assert payload["assistant"]["context_window_messages"] == 6
    assert payload["assistant"]["llm"] == {
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "api_key_env": "DEEPSEEK_API_KEY",
    }
    assert payload["markets"]["us"]["symbols"] == ["NVDA", "PDD"]
    assert "sell_call" not in payload["markets"]["us"]["overrides"]["PDD"]
    assert payload["markets"]["us"]["overrides"]["PDD"]["covered_call"]["min_strike"] == 120
    assert payload["markets"]["us"]["overrides"]["PDD"]["combo_yield"] is True
    assert "sell_call" not in payload["alert_policy"]
    assert payload["alert_policy"]["covered_call"]["medium_annual"] == 0.07
    assert "sell_call" not in payload["templates"]["call_base"]
    assert payload["templates"]["call_base"]["covered_call"]["min_strike_cost_multiplier"] == 1.05
    assert any("configs/user.common.json.agent migrated to assistant" in item for item in out["warnings"])

    migrated_path = tmp_path / "generated.yaml"
    migrated_path.write_text(out["yaml"], encoding="utf-8")
    cfg, _meta = resolve_yaml_runtime_config(repo_root=REPO_ROOT, market="us", config_path=migrated_path)
    validate_config(json.loads(json.dumps(cfg)))
    assistant_cfg, _meta = resolve_yaml_assistant_config(repo_root=REPO_ROOT, config_path=migrated_path)
    assert assistant_cfg["assistant"]["enabled"] is True
    assert assistant_cfg["assistant"]["planner"]["enabled"] is True
    assert "enabled" not in assistant_cfg["assistant"]["llm"]


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
