from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.settings import AssistantSettings
from src.application.config_defaults import DEFAULT_CONFIG, DEFAULT_CONFIG_REF
from src.application.config_yaml_accounts import mutate_yaml_account_config
from src.application.config_profiles import apply_profiles
from src.application.config_validator import validate_config
from src.application.config_yaml import (
    RESOLVED_KEY,
    build_yaml_runtime_config_file,
    build_yaml_assistant_config_file,
    explain_yaml_config_key,
    resolve_yaml_assistant_config,
    resolve_yaml_runtime_config,
)
from src.application.config_yaml_init import init_yaml_config
from src.application.config_yaml_symbols import set_yaml_symbol_config
from src.application.pipeline_watchlist import resolve_watchlist_item_runtime_config
from src.application.runtime_config_freshness import GENERATED_KEY


REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_yaml(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _contains_mapping_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(
            _contains_mapping_key(item, key) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_mapping_key(item, key) for item in value)
    return False


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
  copilot:
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


def test_yaml_config_rejects_opening_threshold_typo_before_runtime_build(tmp_path: Path) -> None:
    config_path = _write_yaml(
        tmp_path / "config.yaml",
        """\
accounts:
  lx:
    type: futu
    futu_account_id: "REAL_12345678"
markets:
  us:
    accounts: [lx]
    symbols: [NVDA]
    overrides:
      NVDA:
        sell_put:
          min_annualized_net_retur: 0.99
""",
    )

    with pytest.raises(AgentToolError) as exc_info:
        resolve_yaml_runtime_config(repo_root=REPO_ROOT, market="us", config_path=config_path)

    message = str(exc_info.value)
    assert "markets.us.overrides.NVDA.sell_put" in message
    assert "min_annualized_net_retur" in message
    assert "min_annualized_net_return" in message


def test_yaml_config_rejects_retired_ai_decision_advice_at_root(
    tmp_path: Path,
) -> None:
    config_path = _write_yaml(
        tmp_path / "config.yaml",
        _minimal_yaml()
        + """\
ai_decision_advice:
  enabled: false
""",
    )

    with pytest.raises(AgentToolError) as exc_info:
        resolve_yaml_runtime_config(
            repo_root=REPO_ROOT,
            market="us",
            config_path=config_path,
        )

    assert (
        str(exc_info.value)
        == "CONFIG_ERROR: config.yaml.ai_decision_advice is retired and must be removed"
    )


@pytest.mark.parametrize("market", ("us", "hk"))
def test_yaml_config_rejects_retired_ai_decision_advice_in_market(
    tmp_path: Path,
    market: str,
) -> None:
    config_path = _write_yaml(
        tmp_path / "config.yaml",
        _minimal_yaml().replace(
            f"  {market}:\n",
            f"  {market}:\n"
            "    ai_decision_advice:\n"
            "      enabled: true\n",
            1,
        ),
    )

    with pytest.raises(AgentToolError) as exc_info:
        resolve_yaml_runtime_config(
            repo_root=REPO_ROOT,
            market=market,
            config_path=config_path,
        )

    assert (
        str(exc_info.value)
        == f"CONFIG_ERROR: markets.{market}.ai_decision_advice is retired and must be removed"
    )


def test_yaml_config_keeps_generic_error_for_nearby_unknown_ai_key(
    tmp_path: Path,
) -> None:
    config_path = _write_yaml(
        tmp_path / "config.yaml",
        _minimal_yaml()
        + """\
ai_decision_advise:
  enabled: true
""",
    )

    with pytest.raises(AgentToolError) as exc_info:
        resolve_yaml_runtime_config(
            repo_root=REPO_ROOT,
            market="us",
            config_path=config_path,
        )

    assert (
        str(exc_info.value)
        == "CONFIG_ERROR: config.yaml.ai_decision_advise is not supported in config.yaml"
    )


def test_runtime_config_rejects_retired_ai_decision_advice_key() -> None:
    with pytest.raises(SystemExit) as exc_info:
        validate_config({"ai_decision_advice": {"enabled": False}})

    assert (
        str(exc_info.value)
        == "[CONFIG_ERROR] ai_decision_advice is retired and must be removed"
    )


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
    assert cfg["symbols"][0]["sell_put"]["min_dte"] == 7
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
        assert "reject_event_risk" not in side_cfg
        assert "event_source_fail_closed" not in side_cfg
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


def test_yaml_runtime_build_defaults_to_canonical_runtime_path(tmp_path: Path) -> None:
    config_path = _write_yaml(tmp_path / "config.yaml", _minimal_yaml())

    out = build_yaml_runtime_config_file(
        repo_root=REPO_ROOT,
        market="us",
        config_path=config_path,
        runtime_root=tmp_path / "runtime",
        dry_run=True,
    )

    assert out["output_config_path"] == str((tmp_path / "runtime" / "config.us.json").resolve())
    assert "/resolved/" not in out["output_config_path"]


def test_yaml_config_rejects_symbol_from_another_market(tmp_path: Path) -> None:
    config_path = _write_yaml(
        tmp_path / "config.yaml",
        """\
accounts:
  lx:
    type: futu
    futu_account_id: "REAL_12345678"
markets:
  us:
    accounts: [lx]
    symbols: ["0700.HK"]
""",
    )

    with pytest.raises(AgentToolError, match="resolves to HK but is configured under markets.us"):
        resolve_yaml_runtime_config(repo_root=REPO_ROOT, market="us", config_path=config_path)


@pytest.mark.parametrize(
    ("section", "expected"),
    (
        (
            """\
notifications:
  providre: feishu_app
  target: wechat:ops
""",
            "notifications contains unsupported keys: providre",
        ),
        (
            """\
notifications:
  provider: wechat_clawbot
  target: wechat:ops
  bot_token: plaintext-secret
""",
            "notifications.bot_token must not contain inline secret material",
        ),
        (
            """\
watchdog:
  retry_enabled: "false"
""",
            "watchdog.retry_enabled must be a boolean",
        ),
    ),
)
def test_yaml_config_rejects_unsafe_control_plane_values(
    tmp_path: Path,
    section: str,
    expected: str,
) -> None:
    config_path = _write_yaml(
        tmp_path / "config.yaml",
        f"""\
accounts:
  lx:
    type: futu
    futu_account_id: "REAL_12345678"
{section}
markets:
  us:
    accounts: [lx]
    symbols: [NVDA]
""",
    )

    with pytest.raises(AgentToolError, match=expected):
        resolve_yaml_runtime_config(repo_root=REPO_ROOT, market="us", config_path=config_path)


def test_yaml_combo_yield_keeps_only_authored_fields_explicit(tmp_path: Path) -> None:
    from src.application.yield_enhancement_config import derive_yield_enhancement_policy

    config_path = _write_yaml(
        tmp_path / "config.yaml",
        """\
accounts:
  lx:
    type: futu
    futu_account_id: "REAL_12345678"
markets:
  us:
    accounts: [lx]
    symbols: [NVDA, FUTU]
    overrides:
      NVDA:
        combo_yield: true
      FUTU:
        combo_yield:
          enabled: true
          min_net_credit_retention: 0.70
          call:
            min_delta: 0.12
""",
    )

    cfg, _meta = resolve_yaml_runtime_config(repo_root=REPO_ROOT, market="us", config_path=config_path)
    policies = {}
    for item in cfg["symbols"]:
        resolved = resolve_watchlist_item_runtime_config(
            item=item,
            profiles=cfg["templates"],
            apply_profiles_fn=apply_profiles,
        )
        policies[item["symbol"]] = derive_yield_enhancement_policy(
            resolved["combo_yield"],
            market="us",
        )

    defaulted = policies["NVDA"]
    assert defaulted.explicit_fields == ("enabled",)
    assert "output_mode" not in defaulted.config
    assert defaulted.config["min_net_credit_retention"] == 0.60
    assert defaulted.config["call"] == {"min_delta": 0.05, "max_delta": 0.20}

    overridden = policies["FUTU"]
    assert overridden.explicit_fields == ("enabled", "min_net_credit_retention", "call")
    assert overridden.config["min_net_credit_retention"] == 0.70
    assert overridden.config["call"] == {"min_delta": 0.12, "max_delta": 0.20}
    assert "output_mode" not in overridden.config


def test_yaml_config_keeps_explicit_sell_put_underwriting_thresholds(tmp_path: Path) -> None:
    config_path = _write_yaml(
        tmp_path / "config.yaml",
        """\
accounts:
  lx:
    type: futu
    futu_account_id: "REAL_12345678"
templates:
  put_base:
    sell_put:
      min_annualized_net_return: 0.10
      min_net_income: 50.0
markets:
  us:
    accounts: [lx]
    symbols: [NVDA]
    overrides:
      NVDA:
        use: put_base
        sell_put:
          max_strike: 150
""",
    )

    cfg, _meta = resolve_yaml_runtime_config(repo_root=REPO_ROOT, market="us", config_path=config_path)

    template = cfg["templates"]["put_base"]["sell_put"]
    assert template["min_annualized_net_return"] == 0.10
    assert template["min_net_income"] == 50.0

    resolved = resolve_watchlist_item_runtime_config(
        item=cfg["symbols"][0],
        profiles=cfg["templates"],
        apply_profiles_fn=apply_profiles,
    )
    assert resolved["sell_put"]["min_annualized_net_return"] == 0.10
    assert resolved["sell_put"]["min_net_income"] == 50.0
    assert "min_net_income" not in resolved["_global_sell_put_liquidity"]
    validate_config(json.loads(json.dumps(cfg)))


def test_yaml_config_accepts_legacy_sell_call_authoring_key(tmp_path: Path) -> None:
    config_path = _write_yaml(
        tmp_path / "config.yaml",
        """\
accounts:
  lx:
    type: futu
    futu_account_id: "REAL_12345678"
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
    futu_account_id: "REAL_12345678"
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
    futu_account_id: "REAL_12345678"
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


def test_yaml_symbol_set_updates_sell_put_max_strike_as_dry_run(tmp_path: Path) -> None:
    config_path = _write_yaml(tmp_path / "config.yaml", _minimal_yaml())
    before = config_path.read_text(encoding="utf-8")

    out = set_yaml_symbol_config(
        repo_root=REPO_ROOT,
        market="us",
        symbol="FUTU",
        config_path=config_path,
        sell_put_max_strike=90,
        apply=False,
    )

    assert out["dry_run"] is True
    assert out["write_applied"] is False
    assert out["summary"]["canonical_symbol"] == "FUTU"
    assert out["summary"]["changed_paths"] == ["markets.us.overrides.FUTU.sell_put.max_strike"]
    assert out["summary"]["entry"]["sell_put"]["max_strike"] == 90.0
    assert out["validation"]["us"]["ok"] is True
    assert config_path.read_text(encoding="utf-8") == before


def test_yaml_symbol_set_updates_combo_yield_enabled_as_dry_run(tmp_path: Path) -> None:
    doc = yaml.safe_load(_minimal_yaml())
    doc["markets"]["hk"]["symbols"].append("3690.HK")
    doc["markets"]["hk"]["overrides"] = {
        "3690.HK": {
            "sell_put": {"enabled": True},
            "covered_call": {"enabled": True},
            "combo_yield": {"enabled": False},
        }
    }
    config_path = _write_yaml(tmp_path / "config.yaml", yaml.safe_dump(doc, sort_keys=False))
    before = config_path.read_text(encoding="utf-8")

    out = set_yaml_symbol_config(
        repo_root=REPO_ROOT,
        market="hk",
        symbol="3690.HK",
        config_path=config_path,
        combo_yield_enabled=True,
        apply=False,
    )

    assert out["dry_run"] is True
    assert out["write_applied"] is False
    assert out["summary"]["changed_paths"] == ["markets.hk.overrides.3690.HK.combo_yield.enabled"]
    assert out["summary"]["entry"]["combo_yield"]["enabled"] is True
    assert out["summary"]["entry"]["sell_put"]["enabled"] is True
    assert out["summary"]["entry"]["covered_call"]["enabled"] is True
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


def test_yaml_account_add_is_preview_only_by_default(tmp_path: Path) -> None:
    config_path = _write_yaml(tmp_path / "config.yaml", _minimal_yaml())
    before = config_path.read_bytes()

    out = mutate_yaml_account_config(
        repo_root=REPO_ROOT,
        action="add",
        market="us",
        account_label="new",
        account_type="external_holdings",
        config_path=config_path,
    )

    assert out["dry_run"] is True
    assert out["write_applied"] is False
    assert out["summary"]["accounts"] == ["lx", "sy", "new"]
    assert out["summary"]["holdings_account"] == "new"
    assert config_path.read_bytes() == before
    assert not (tmp_path / "config.us.json").exists()


def test_yaml_account_add_apply_publishes_one_generation(tmp_path: Path) -> None:
    config_path = _write_yaml(tmp_path / "config.yaml", _minimal_yaml())
    runtime_root = tmp_path / "runtime"

    out = mutate_yaml_account_config(
        repo_root=REPO_ROOT,
        action="add",
        market="us",
        account_label="new",
        account_type="external_holdings",
        config_path=config_path,
        rebuild_runtime_root=runtime_root,
        apply=True,
    )

    assert out["write_applied"] is True
    source_doc = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert source_doc["accounts"]["new"]["type"] == "external_holdings"
    assert source_doc["markets"]["us"]["accounts"] == ["lx", "sy", "new"]
    assert source_doc["markets"]["hk"]["accounts"] == ["lx"]
    us_runtime = json.loads((runtime_root / "config.us.json").read_text(encoding="utf-8"))
    hk_runtime = json.loads((runtime_root / "config.hk.json").read_text(encoding="utf-8"))
    assert us_runtime["accounts"] == ["lx", "sy", "new"]
    assert hk_runtime["accounts"] == ["lx"]
    assert us_runtime[RESOLVED_KEY]["config_yaml_sha256"] == out["source_revision"]["after_sha256"]
    assert hk_runtime[RESOLVED_KEY]["config_yaml_sha256"] == out["source_revision"]["after_sha256"]


def test_yaml_account_remove_keeps_global_definition_used_by_other_market(tmp_path: Path) -> None:
    config_path = _write_yaml(tmp_path / "config.yaml", _minimal_yaml())

    out = mutate_yaml_account_config(
        repo_root=REPO_ROOT,
        action="remove",
        market="us",
        account_label="lx",
        config_path=config_path,
        apply=True,
    )

    source_doc = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert out["summary"]["removed_global_account"] is False
    assert source_doc["markets"]["us"]["accounts"] == ["sy"]
    assert source_doc["markets"]["hk"]["accounts"] == ["lx"]
    assert "lx" in source_doc["accounts"]


def test_yaml_symbol_set_preserves_existing_legacy_sell_call_key(tmp_path: Path) -> None:
    config_path = _write_yaml(
        tmp_path / "config.yaml",
        """\
accounts:
  lx:
    type: futu
    futu_account_id: "REAL_12345678"
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
    assert cfg["assistant"]["copilot"]["enabled"] is False
    assert cfg["assistant"]["copilot"]["toolsets"]["portfolio"] is False
    assert cfg["assistant"]["llm"]["api_key_env"] == "OM_LLM_API_KEY"
    assert cfg["inbound"]["feishu_ws"]["reply_enabled"] is True
    assert cfg["inbound"]["feishu_ws"]["queue_size"] == 100
    assert cfg["inbound"]["feishu_ws"]["ack_reaction"] == "THUMBSUP"
    assert cfg["inbound"]["wechat_clawbot"]["label"] == "default"
    assert cfg["inbound"]["wechat_clawbot"]["allowed_senders"] == "wechat:user_1"
    assert cfg["inbound"]["wechat_clawbot"]["reply_enabled"] is True
    assert cfg["inbound"]["wechat_clawbot"]["max_reply_chars"] == 3500
    assert cfg["inbound"]["wechat_clawbot"]["poll_interval_sec"] == 0.5
    assert cfg["inbound"]["wechat_clawbot"]["keepalive_interval_sec"] == 1800.0


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
                        "copilot": {"enabled": True},
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
    assert cfg["assistant"]["copilot"]["enabled"] is True
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
  copilot:
    enabled: true
  active_model: deepseek-default
  models:
    deepseek-default:
      provider: deepseek
      model: deepseek-chat
      api_key_env: DEEPSEEK_API_KEY
      context_window_tokens: 24000
    openai-default:
      provider: openai
      model: gpt-5.2
      api_key_env: OM_LLM_API_KEY
      context_window_tokens: 24000
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
    assert assistant["llm"]["context_window_tokens"] == 24000
    assert "max_attempts" not in assistant["llm"]
    resolved = cfg[RESOLVED_KEY]["assistant_models"]
    assert resolved["active_model"] == "deepseek-default"
    assert resolved["profile_count"] == 2
    assert resolved["resolved_profile"]["provider"] == "deepseek"


def test_yaml_assistant_model_profile_requires_declared_context_window(tmp_path: Path) -> None:
    config_path = _write_yaml(
        tmp_path / "config.yaml",
        """\
assistant:
  enabled: true
  copilot:
    enabled: true
  active_model: deepseek-default
  models:
    deepseek-default:
      provider: deepseek
      model: deepseek-chat
      api_key_env: DEEPSEEK_API_KEY
""",
    )

    with pytest.raises(AgentToolError, match="context_window_tokens must be an integer"):
        resolve_yaml_assistant_config(repo_root=REPO_ROOT, config_path=config_path)


def test_yaml_assistant_config_allows_local_ollama_without_api_key(tmp_path: Path) -> None:
    config_path = _write_yaml(
        tmp_path / "config.yaml",
        """\
assistant:
  enabled: true
  copilot:
    enabled: true
  active_model: local
  models:
    local:
      provider: ollama
      model: gpt-oss:20b
      context_window_tokens: 24000
""",
    )

    cfg, _meta = resolve_yaml_assistant_config(repo_root=REPO_ROOT, config_path=config_path)

    assert cfg["assistant"]["llm"] == {
        "provider": "ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "gpt-oss:20b",
        "api_key_env": "",
        "context_window_tokens": 24000,
    }


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
  copilot:
    enabled: true
  active_model: missing
  models:
    deepseek-default:
      provider: deepseek
      model: deepseek-chat
      api_key_env: DEEPSEEK_API_KEY
      context_window_tokens: 24000
""",
    )

    with pytest.raises(AgentToolError, match="unknown model profile"):
        resolve_yaml_assistant_config(repo_root=REPO_ROOT, config_path=config_path)


def test_yaml_assistant_config_rejects_user_configurable_hooks(tmp_path: Path) -> None:
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
  copilot:
    enabled: true
  hooks:
    pre_tool_use: custom
""",
    )

    with pytest.raises(SystemExit, match="assistant has unsupported keys: hooks"):
        resolve_yaml_assistant_config(repo_root=REPO_ROOT, config_path=config_path)


def test_yaml_assistant_config_omits_retired_copilot_keys(tmp_path: Path) -> None:
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
  copilot:
    enabled: true
    channel_scenes: [operations_diagnostics]
    human_review: false
""",
    )

    cfg, _meta = resolve_yaml_assistant_config(repo_root=REPO_ROOT, config_path=config_path)

    assert cfg["assistant"]["copilot"] == {
        "enabled": True,
        "toolsets": {"portfolio": False},
    }
    assert cfg[RESOLVED_KEY]["assistant_models"]["warnings"] == [
        "retired assistant.copilot keys omitted: channel_scenes, human_review"
    ]


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
  copilot:
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
    assert payload["assistant"]["copilot"]["enabled"] is True
    assert payload["assistant"]["copilot"]["toolsets"]["portfolio"] is False
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
    assert assistant_cfg["assistant"]["copilot"]["enabled"] is True
    assert assistant_cfg["assistant"]["copilot"]["toolsets"]["portfolio"] is False
    assert assistant_cfg["assistant"]["context_window_messages"] == 8
    assert "default_market_scope" not in assistant_cfg["assistant"]
    assert "active_model" not in assistant_cfg["assistant"]
    assert "models" not in assistant_cfg["assistant"]
    assert assistant_cfg["assistant"]["llm"]["base_url"] == "https://api.deepseek.com"
    assert assistant_cfg["assistant"]["llm"]["api_key_env"] == "DEEPSEEK_API_KEY"
    assert assistant_cfg["assistant"]["llm"]["timeout_seconds"] == 90
    assert assistant_cfg["assistant"]["llm"]["context_window_tokens"] == 24000
    assert assistant_cfg["assistant"]["llm"]["max_output_tokens"] == 2048
    assert assistant_cfg["inbound"]["feishu_ws"]["ack_reaction"] == "THUMBSUP"


@pytest.mark.parametrize(
    "invalid_scope",
    [
        {"account_label": "../escaped"},
        {"account_label": "lx.sy"},
        {"external_holdings_account": "sy account"},
    ],
)
def test_config_init_invalid_account_scope_has_dry_run_apply_parity_and_preserves_existing(
    tmp_path: Path,
    invalid_scope: dict[str, str],
) -> None:
    output_path = tmp_path / "config.yaml"
    runtime_dir = tmp_path / "runtime"
    preserved = "accounts:\n  lx:\n    type: futu\n"
    output_path.write_text(preserved, encoding="utf-8")

    for dry_run in (True, False):
        with pytest.raises(AgentToolError, match="invalid"):
            init_yaml_config(
                repo_root=REPO_ROOT,
                output_config_yaml_path=output_path,
                runtime_output_dir=runtime_dir,
                dry_run=dry_run,
                force=True,
                **invalid_scope,
            )
        assert output_path.read_text(encoding="utf-8") == preserved
        assert not runtime_dir.exists()


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
    futu_account_id: "REAL_12345678"
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
    futu_account_id: "REAL_12345678"
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
    futu_account_id: "REAL_12345678"
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
    futu_account_id: "REAL_12345678"
trade_intake:
  mode: apply
markets:
  us:
    accounts: [lx]
    symbols: [NVDA]
""",
    )

    with pytest.raises(AgentToolError, match=r"trade_intake\.mode is not supported"):
        resolve_yaml_runtime_config(repo_root=REPO_ROOT, market="us", config_path=config_path)


def test_yaml_config_accepts_trade_intake_holdings_sync(tmp_path: Path) -> None:
    config_path = _write_yaml(
        tmp_path / "config.yaml",
        """\
accounts:
  lx:
    type: futu
    futu_account_id: "REAL_12345678"
trade_intake:
  holdings_sync:
    enabled: true
markets:
  us:
    accounts: [lx]
    symbols: [NVDA]
""",
    )

    cfg, _meta = resolve_yaml_runtime_config(
        repo_root=REPO_ROOT,
        market="us",
        config_path=config_path,
    )

    assert cfg["trade_intake"]["mode"] == "apply"
    assert cfg["trade_intake"]["holdings_sync"] == {"enabled": True}


def test_yaml_config_accepts_settlement_observation_kill_switch(
    tmp_path: Path,
) -> None:
    config_path = _write_yaml(
        tmp_path / "config.yaml",
        """\
accounts:
  lx:
    type: futu
    futu_account_id: "REAL_12345678"
trade_intake:
  settlement_observation:
    enabled: false
markets:
  us:
    accounts: [lx]
    symbols: [NVDA]
""",
    )

    cfg, _meta = resolve_yaml_runtime_config(
        repo_root=REPO_ROOT,
        market="us",
        config_path=config_path,
    )

    assert cfg["trade_intake"]["settlement_observation"] == {
        "enabled": False
    }


@pytest.mark.parametrize(
    "settlement_yaml",
    [
        'enabled: "false"',
        "enabled: true\n    retry_policy: custom",
    ],
)
def test_yaml_config_rejects_invalid_settlement_observation(
    tmp_path: Path,
    settlement_yaml: str,
) -> None:
    config_path = _write_yaml(
        tmp_path / "config.yaml",
        f"""\
accounts:
  lx:
    type: futu
    futu_account_id: "REAL_12345678"
trade_intake:
  settlement_observation:
    {settlement_yaml}
markets:
  us:
    accounts: [lx]
    symbols: [NVDA]
""",
    )

    with pytest.raises(AgentToolError, match="settlement_observation"):
        resolve_yaml_runtime_config(
            repo_root=REPO_ROOT,
            market="us",
            config_path=config_path,
        )


def test_yaml_config_accepts_account_scoped_combo_reconciliation(tmp_path: Path) -> None:
    config_path = _write_yaml(
        tmp_path / "config.yaml",
        """\
accounts:
  lx:
    type: futu
    futu_account_id: "REAL_12345678"
trade_intake:
  combo_reconciliation:
    default_mode: off
    accounts:
      lx: observe
markets:
  us:
    accounts: [lx]
    symbols: [NVDA]
""",
    )

    cfg, _meta = resolve_yaml_runtime_config(
        repo_root=REPO_ROOT,
        market="us",
        config_path=config_path,
    )

    assert cfg["trade_intake"]["combo_reconciliation"] == {
        "default_mode": "off",
        "accounts": {"lx": "observe"},
    }


@pytest.mark.parametrize(
    ("combo_yaml", "expected_error"),
    [
        (
            "default_mode: observe\n    accounts: {}",
            "default_mode must remain off",
        ),
        (
            "default_mode: off\n    accounts:\n      LX: confirm",
            "account labels must be lowercase",
        ),
        (
            "default_mode: off\n    accounts:\n      unknown: confirm",
            "is not a configured account",
        ),
    ],
)
def test_yaml_config_rejects_invalid_combo_reconciliation(
    tmp_path: Path,
    combo_yaml: str,
    expected_error: str,
) -> None:
    config_path = _write_yaml(
        tmp_path / "config.yaml",
        f"""\
accounts:
  lx:
    type: futu
    futu_account_id: "REAL_12345678"
trade_intake:
  combo_reconciliation:
    {combo_yaml}
markets:
  us:
    accounts: [lx]
    symbols: [NVDA]
""",
    )

    with pytest.raises(AgentToolError, match=expected_error):
        resolve_yaml_runtime_config(
            repo_root=REPO_ROOT,
            market="us",
            config_path=config_path,
        )


def test_yaml_config_rejects_invalid_trade_intake_holdings_sync(tmp_path: Path) -> None:
    config_path = _write_yaml(
        tmp_path / "config.yaml",
        """\
accounts:
  lx:
    type: futu
    futu_account_id: "REAL_12345678"
trade_intake:
  holdings_sync:
    enabled: "true"
markets:
  us:
    accounts: [lx]
    symbols: [NVDA]
""",
    )

    with pytest.raises(AgentToolError, match="holdings_sync.enabled must be a boolean"):
        resolve_yaml_runtime_config(
            repo_root=REPO_ROOT,
            market="us",
            config_path=config_path,
        )


def test_yaml_config_rejects_override_for_symbol_not_in_market(tmp_path: Path) -> None:
    config_path = _write_yaml(
        tmp_path / "config.yaml",
        """\
accounts:
  lx:
    type: futu
    futu_account_id: "REAL_12345678"
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
    assert not _contains_mapping_key(cfg, "output_mode")
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
                        "context_window_tokens": 24000,
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
    assert payload["assistant"]["copilot"]["enabled"] is True
    assert AssistantSettings.from_runtime_config(payload).enabled_copilot_toolsets == frozenset()
    assert payload["assistant"]["context_window_messages"] == 6
    assert payload["assistant"]["llm"] == {
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "api_key_env": "DEEPSEEK_API_KEY",
        "context_window_tokens": 24000,
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
    assert assistant_cfg["assistant"]["copilot"]["enabled"] is True
    assert AssistantSettings.from_runtime_config(assistant_cfg).enabled_copilot_toolsets == frozenset()
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
