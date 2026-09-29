from __future__ import annotations

import json
from hashlib import sha256
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from src.application.agent_tool_contracts import AgentToolError
from src.application.config_defaults import default_config
from src.application.config_validator import validate_config
from src.application.config_yaml import build_yaml_runtime_config_file, resolve_yaml_runtime_config
from src.application.layered_config import build_layered_runtime_config_from_user_config
from src.application.opening_candidate_snapshot import strategy_policy_hash
from src.application.wheel.config import WHEEL_DEFAULTS, build_wheel_policy_hash, resolve_wheel_config


REPO_ROOT = Path(__file__).resolve().parents[1]


def _runtime(**extra):
    return {"symbols": [{"symbol": "NVDA"}], **extra}


def _yaml_config(tmp_path, *, override=None, close_advice=None, features=None, market_close_advice=None, market_features=None):
    doc = {
        "accounts": {"lx": {"type": "futu", "futu_account_id": "REAL_12345678"}},
        "markets": {"us": {"accounts": ["lx"], "symbols": ["NVDA"], "overrides": {"NVDA": override or {}}}},
    }
    if close_advice is not None:
        doc["close_advice"] = close_advice
    if features is not None:
        doc["features"] = features
    if market_close_advice is not None:
        doc["markets"]["us"]["close_advice"] = market_close_advice
    if market_features is not None:
        doc["markets"]["us"]["features"] = market_features
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return resolve_yaml_runtime_config(repo_root=REPO_ROOT, market="us", config_path=path)[0]


@pytest.mark.parametrize("side", ["sell_put", "sell_call"])
def test_null_enabled_dte_is_config_error(side):
    config = _runtime()
    config["symbols"][0][side] = {"enabled": True, "strategy": "insurance_underwriting", "min_dte": None, "max_dte": 30}
    with pytest.raises(SystemExit, match=rf"\[CONFIG_ERROR\] NVDA\.{side} enabled but missing min_dte"):
        validate_config(config)


@pytest.mark.parametrize("side", ["sell_put", "sell_call"])
def test_reversed_enabled_dte_still_config_error(side):
    config = _runtime()
    config["symbols"][0][side] = {"enabled": True, "strategy": "insurance_underwriting", "min_dte": 31, "max_dte": 30}
    with pytest.raises(SystemExit, match=rf"\[CONFIG_ERROR\] NVDA\.{side}\.min_dte > NVDA\.{side}\.max_dte"):
        validate_config(config)


@pytest.mark.parametrize("side", ["sell_put", "sell_call"])
def test_null_enabled_max_dte_is_config_error(side):
    config = _runtime()
    config["symbols"][0][side] = {"enabled": True, "strategy": "insurance_underwriting", "min_dte": 7, "max_dte": None}
    with pytest.raises(SystemExit, match=rf"\[CONFIG_ERROR\] NVDA\.{side} enabled but missing max_dte"):
        validate_config(config)


def test_close_advice_runtime_typo_and_enabled_type():
    with pytest.raises(SystemExit, match="quote_sourse->quote_source"):
        validate_config(_runtime(close_advice={"quote_sourse": "auto"}))
    with pytest.raises(SystemExit, match=r"close_advice.enabled must be a boolean"):
        validate_config(_runtime(close_advice={"enabled": "false"}))


@pytest.mark.parametrize("position", ["root", "feature", "market", "market_feature"])
def test_close_advice_authoring_rejects_typo_before_build(tmp_path, position):
    payload = {"quote_sourse": "auto"}
    kwargs = {
        "root": {"close_advice": payload},
        "feature": {"features": {"close_advice": payload}},
        "market": {"market_close_advice": payload},
        "market_feature": {"market_features": {"close_advice": payload}},
    }[position]
    with pytest.raises(AgentToolError, match="quote_sourse->quote_source"):
        _yaml_config(tmp_path, **kwargs)


def test_close_advice_authoring_rejects_string_enabled(tmp_path):
    with pytest.raises(AgentToolError, match="close_advice.enabled must be a boolean"):
        _yaml_config(tmp_path, close_advice={"enabled": "false"})


def test_close_advice_retired_keys_still_warn(capsys):
    validate_config(_runtime(close_advice={"notify_levels": ["high"]}))
    assert "CLOSE_ADVICE_STRICT_POLICY_KEYS_IGNORED" in capsys.readouterr().err


def test_close_advice_authoring_retired_key_still_warns_after_build(tmp_path, capsys):
    cfg = _yaml_config(tmp_path, market_close_advice={"notify_levels": ["high"]})
    assert "notify_levels" in cfg["close_advice"]
    assert "CLOSE_ADVICE_STRICT_POLICY_KEYS_IGNORED" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("key", "value", "runtime_key"),
    [
        ("broker", "US", "broker"),
        ("accounts", ["lx"], "accounts"),
        ("fetch", {"source": "futu"}, "fetch"),
        ("use", ["put_base"], "use"),
        ("sell_put", {"enabled": False}, "sell_put"),
        ("covered_call", {"enabled": False}, "sell_call"),
        ("sell_call", {"enabled": False}, "sell_call"),
        ("combo_yield", {"enabled": False}, "combo_yield"),
    ],
)
def test_each_legal_symbol_override_survives_build_and_validation(tmp_path, key, value, runtime_key):
    cfg = _yaml_config(tmp_path, override={key: value})
    item = cfg["symbols"][0]
    assert runtime_key in item
    validate_config(cfg)


def test_symbol_override_typo_rejected_in_authoring_and_runtime(tmp_path):
    with pytest.raises(AgentToolError, match="covred_call->covered_call"):
        _yaml_config(tmp_path, override={"covred_call": {"enabled": False}})
    with pytest.raises(SystemExit, match="covred_call"):
        validate_config(_runtime(symbols=[{"symbol": "NVDA", "covred_call": {"enabled": False}}]))


def test_runtime_symbol_accounts_remain_accepted():
    validate_config(_runtime(accounts=["lx"], symbols=[{"symbol": "NVDA", "accounts": ["lx"]}]))


@pytest.mark.parametrize("key", ["yield_enhancement", "rebound_combo"])
def test_retired_symbol_keys_keep_targeted_runtime_error(key):
    with pytest.raises(SystemExit, match=rf"NVDA\.{key} has been removed; use NVDA\.combo_yield"):
        validate_config(_runtime(symbols=[{"symbol": "NVDA", key: {"enabled": True}}]))


def test_retired_symbol_authoring_error_keeps_combo_yield_hint(tmp_path):
    with pytest.raises(AgentToolError, match="yield_enhancement has been removed; use combo_yield"):
        _yaml_config(tmp_path, override={"yield_enhancement": {"enabled": True}})


def test_wheel_flat_min_delta_retired_defaults_warning_and_fingerprint(capsys):
    system = json.loads((REPO_ROOT / "configs/system.json").read_text(encoding="utf-8"))["defaults"]["wheel"]
    assert all("min_delta" not in source for source in (default_config()["defaults"]["wheel"], system, WHEEL_DEFAULTS))
    assert WHEEL_DEFAULTS["call"]["min_abs_delta"] == 0.25
    with_flat = _runtime(wheel={"min_delta": 0.3})
    without_flat = deepcopy(with_flat)
    del without_flat["wheel"]["min_delta"]
    validate_config(with_flat)
    assert "wheel.call.min_abs_delta" in capsys.readouterr().err
    assert build_wheel_policy_hash(with_flat, market="us", account="lx") == build_wheel_policy_hash(without_flat, market="us", account="lx")


@pytest.mark.parametrize(("market", "symbol"), [("us", "NVDA"), ("hk", "0700.HK")])
def test_layered_wheel_policy_stays_same_after_flat_default_removal(market, symbol):
    new_system = default_config()
    old_system = deepcopy(new_system)
    old_system["defaults"]["wheel"]["min_delta"] = 0.3
    user = {"accounts": ["lx"], "account_settings": {"lx": {"type": "futu", "futu": {"account_id": "REAL_12345678"}}}, "symbols": [{"symbol": symbol}]}
    configs = [
        build_layered_runtime_config_from_user_config(
            repo_root=REPO_ROOT,
            market=market,
            user_config=user,
            system_config=system,
        )[0]
        for system in (old_system, new_system)
    ]
    old_cfg, new_cfg = configs
    assert old_cfg["wheel"]["min_delta"] == 0.3
    assert "min_delta" not in new_cfg["wheel"]
    assert all(resolve_wheel_config(cfg, "lx", market=market)["call"]["min_abs_delta"] == 0.25 for cfg in configs)
    assert build_wheel_policy_hash(old_cfg, market=market, account="lx") == build_wheel_policy_hash(new_cfg, market=market, account="lx")
    assert strategy_policy_hash(old_cfg) != strategy_policy_hash(new_cfg)


def test_isolated_config_build_changes_bytes_but_not_wheel_policy(tmp_path):
    source = tmp_path / "config.yaml"
    source.write_text(yaml.safe_dump({
        "accounts": {"lx": {"type": "futu", "futu_account_id": "REAL_12345678"}},
        "markets": {"us": {"accounts": ["lx"], "symbols": ["NVDA"]}},
    }), encoding="utf-8")
    system_path = tmp_path / "system.json"
    output_path = tmp_path / "config.us.json"
    system = default_config()
    system["defaults"]["wheel"]["min_delta"] = 0.3
    system_path.write_text(json.dumps(system), encoding="utf-8")
    kwargs = {"repo_root": REPO_ROOT, "market": "us", "config_path": source, "system_config_path": system_path, "output_config_path": output_path}
    build_yaml_runtime_config_file(**kwargs)
    old_bytes = output_path.read_bytes()
    old_cfg = json.loads(old_bytes)

    del system["defaults"]["wheel"]["min_delta"]
    system_path.write_text(json.dumps(system), encoding="utf-8")
    result = build_yaml_runtime_config_file(**kwargs)
    new_bytes = output_path.read_bytes()
    new_cfg = json.loads(new_bytes)

    assert sha256(old_bytes).hexdigest() != sha256(new_bytes).hexdigest()
    assert old_cfg["wheel"]["min_delta"] == 0.3
    assert "min_delta" not in new_cfg["wheel"]
    assert resolve_wheel_config(old_cfg, "lx", market="us")["call"]["min_abs_delta"] == 0.25
    assert resolve_wheel_config(new_cfg, "lx", market="us")["call"]["min_abs_delta"] == 0.25
    assert build_wheel_policy_hash(old_cfg, market="us", account="lx") == build_wheel_policy_hash(new_cfg, market="us", account="lx")
    assert "wheel_policy_drift" not in result
