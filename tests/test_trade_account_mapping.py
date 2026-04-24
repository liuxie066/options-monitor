from __future__ import annotations

from scripts.trade_account_mapping import (
    resolve_futu_account_mapping,
    resolve_internal_account,
    resolve_trade_intake_config,
)


def test_resolve_futu_account_mapping_accepts_known_accounts() -> None:
    cfg = {
        "accounts": ["lx", "sy"],
        "trade_intake": {
            "account_mapping": {
                "futu": {
                    "REAL_1": "lx",
                    "REAL_2": "sy",
                }
            }
        },
    }

    out = resolve_futu_account_mapping(cfg)

    assert out == {"REAL_1": "lx", "REAL_2": "sy"}
    assert resolve_internal_account("REAL_2", out) == "sy"


def test_resolve_futu_account_mapping_rejects_unknown_internal_account() -> None:
    cfg = {
        "accounts": ["lx"],
        "trade_intake": {"account_mapping": {"futu": {"REAL_1": "sy"}}},
    }

    try:
        resolve_futu_account_mapping(cfg)
    except ValueError as exc:
        assert "not a futu account" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_resolve_futu_account_mapping_rejects_external_holdings_account() -> None:
    cfg = {
        "accounts": ["user1", "ext1"],
        "account_settings": {
            "ext1": {"type": "external_holdings", "holdings_account": "feishu-ext1"},
        },
        "trade_intake": {"account_mapping": {"futu": {"REAL_1": "ext1"}}},
    }

    try:
        resolve_futu_account_mapping(cfg)
    except ValueError as exc:
        assert "not a futu account" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_resolve_trade_intake_config_uses_defaults() -> None:
    out = resolve_trade_intake_config({"accounts": ["lx"]})

    assert out["enabled"] is True
    assert out["mode"] == "dry-run"
    assert str(out["state_path"]).endswith("output/state/auto_trade_intake_state.json")
