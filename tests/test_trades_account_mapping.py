from __future__ import annotations

from src.application.trades.account_mapping import (
    resolve_futu_account_mapping,
    resolve_futu_lookup_account_ids,
    resolve_internal_account,
    resolve_trade_intake_config,
)
from domain.domain.trade_account_identity import extract_primary_account_id, extract_visible_account_fields


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
    assert str(out["status_path"]).endswith("output/state/auto_trade_intake_status.json")
    assert out["receipt"] == {
        "enabled": True,
        "notify_applied": True,
        "notify_unresolved": True,
        "notify_failed": True,
        "notify_duplicate": False,
        "retry_unconfirmed_duplicate": True,
    }


def test_resolve_trade_intake_config_accepts_receipt_overrides() -> None:
    cfg = {
        "accounts": ["lx"],
        "trade_intake": {
            "status_path": "tmp/status.json",
            "receipt": {
                "enabled": False,
                "notify_unresolved": False,
                "notify_duplicate": True,
            },
        },
    }

    out = resolve_trade_intake_config(cfg)

    assert str(out["status_path"]) == "tmp/status.json"
    assert out["receipt"]["enabled"] is False
    assert out["receipt"]["notify_unresolved"] is False
    assert out["receipt"]["notify_duplicate"] is True
    assert out["receipt"]["notify_applied"] is True


def test_resolve_trade_intake_config_rejects_non_boolean_receipt_flag() -> None:
    cfg = {"accounts": ["lx"], "trade_intake": {"receipt": {"enabled": "yes"}}}

    try:
        resolve_trade_intake_config(cfg)
    except ValueError as exc:
        assert "trade_intake.receipt.enabled must be a boolean" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_resolve_futu_lookup_account_ids_merges_account_settings_account_id() -> None:
    cfg = {
        "accounts": ["lx", "sy"],
        "account_settings": {
            "lx": {"type": "futu", "futu": {"account_id": "222"}},
            "sy": {"type": "external_holdings", "holdings_account": "sy"},
        },
        "trade_intake": {"account_mapping": {"futu": {"111": "lx"}}},
    }

    out = resolve_futu_lookup_account_ids(cfg)

    assert out == ["111", "222"]


def test_extract_primary_account_id_prefers_canonical_priority_order() -> None:
    payload = {
        "trade_acc_id": "TRADE_1",
        "account_id": "ACCOUNT_1",
        "futu_account_id": "FUTU_1",
    }

    out = extract_primary_account_id(payload)

    assert out == "FUTU_1"


def test_extract_visible_account_fields_keeps_all_visible_account_keys() -> None:
    payload = {
        "trade_acc_id": "TRADE_1",
        "account_id": "ACCOUNT_1",
        "futu_account_id": "FUTU_1",
        "accID": "ACCID_1",
    }

    out = extract_visible_account_fields(payload)

    assert out == {
        "futu_account_id": "FUTU_1",
        "account_id": "ACCOUNT_1",
        "trade_acc_id": "TRADE_1",
        "accID": "ACCID_1",
    }
