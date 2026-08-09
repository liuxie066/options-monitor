from __future__ import annotations

import pytest

from src.application.ai_decision_advice.config import (
    API_KEY_ENV,
    ai_decision_advice_enabled,
    resolve_api_key,
)
from src.application.config_validator import validate_config
from src.application.config_yaml import yaml_to_market_user_config


def _base_cfg() -> dict:
    return {
        "accounts": ["lx"],
        "symbols": [{"symbol": "NVDA", "market": "us"}],
    }


def test_ai_decision_advice_accepts_disabled_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    cfg = _base_cfg()
    cfg["ai_decision_advice"] = {"enabled": False}
    validate_config(cfg)


def test_ai_decision_advice_accepts_enabled_with_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_ENV, "test-key")
    cfg = _base_cfg()
    cfg["ai_decision_advice"] = {"enabled": True}
    validate_config(cfg)


def test_ai_decision_advice_static_validation_does_not_read_runtime_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    cfg = _base_cfg()
    cfg["ai_decision_advice"] = {"enabled": True}
    validate_config(cfg)
    assert resolve_api_key({"OM_SECRET_BACKEND": "env"}) is None


def test_ai_decision_advice_rejects_unknown_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_ENV, "test-key")
    cfg = _base_cfg()
    cfg["ai_decision_advice"] = {"enabled": True, "model": "other"}
    with pytest.raises(SystemExit, match="unsupported keys: model"):
        validate_config(cfg)


def test_ai_decision_advice_rejects_non_bool_enabled() -> None:
    cfg = _base_cfg()
    cfg["ai_decision_advice"] = {"enabled": "yes"}
    with pytest.raises(SystemExit, match="enabled must be a boolean"):
        validate_config(cfg)


def test_ai_decision_advice_rejects_retired_keys() -> None:
    for key in ("ai_interpretation", "ai_strategy_advice"):
        cfg = _base_cfg()
        cfg[key] = {"enabled": True}
        with pytest.raises(SystemExit, match=f"{key} is no longer supported"):
            validate_config(cfg)


def test_ai_decision_advice_enabled_helper() -> None:
    assert ai_decision_advice_enabled({}) is False
    assert ai_decision_advice_enabled({"ai_decision_advice": {"enabled": True}}) is True
    assert ai_decision_advice_enabled({"ai_decision_advice": {"enabled": False}}) is False


def test_resolve_api_key() -> None:
    assert resolve_api_key({}) is None
    assert resolve_api_key({API_KEY_ENV: "  "}) is None
    assert resolve_api_key({API_KEY_ENV: "abc"}) == "abc"


def test_yaml_passthrough_root_and_market_override() -> None:
    raw = {
        "accounts": {"lx": {"type": "external_holdings", "holdings_account": "lx"}},
        "ai_decision_advice": {"enabled": True},
        "markets": {"us": {"accounts": ["lx"], "symbols": ["NVDA"]}},
    }
    out = yaml_to_market_user_config(raw, market="us")
    assert out.get("ai_decision_advice") == {"enabled": True}

    raw_market_off = {
        "accounts": {"lx": {"type": "external_holdings", "holdings_account": "lx"}},
        "ai_decision_advice": {"enabled": True},
        "markets": {
            "us": {
                "accounts": ["lx"],
                "symbols": ["NVDA"],
                "ai_decision_advice": {"enabled": False},
            }
        },
    }
    out_off = yaml_to_market_user_config(raw_market_off, market="us")
    assert out_off.get("ai_decision_advice") == {"enabled": False}


def test_yaml_passthrough_absent_stays_absent() -> None:
    raw = {
        "accounts": {"lx": {"type": "external_holdings", "holdings_account": "lx"}},
        "markets": {"us": {"accounts": ["lx"], "symbols": ["NVDA"]}},
    }
    out = yaml_to_market_user_config(raw, market="us")
    assert "ai_decision_advice" not in out
