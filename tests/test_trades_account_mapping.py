from __future__ import annotations

import pytest

from src.application.trades.account_mapping import (
    resolve_futu_account_mapping,
    resolve_futu_lookup_account_ids,
    resolve_internal_account,
    resolve_trade_intake_config,
    resolve_trade_intake_sources,
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

    with pytest.raises(ValueError) as _caught:
        resolve_futu_account_mapping(cfg)
    exc = _caught.value
    assert "not a futu account" in str(exc)


def test_resolve_futu_account_mapping_rejects_external_holdings_account() -> None:
    cfg = {
        "accounts": ["user1", "ext1"],
        "account_settings": {
            "ext1": {"type": "external_holdings", "holdings_account": "feishu-ext1"},
        },
        "trade_intake": {"account_mapping": {"futu": {"REAL_1": "ext1"}}},
    }

    with pytest.raises(ValueError) as _caught:
        resolve_futu_account_mapping(cfg)
    exc = _caught.value
    assert "not a futu account" in str(exc)


def test_resolve_trade_intake_config_uses_defaults() -> None:
    out = resolve_trade_intake_config({"accounts": ["lx"]})

    assert out["enabled"] is True
    assert out["mode"] == "dry-run"
    assert str(out["state_path"]).endswith("output_shared/state/auto_trade_intake_state.json")
    assert str(out["status_path"]).endswith("output_shared/state/auto_trade_intake_status.json")
    assert out["receipt"] == {
        "enabled": True,
        "notify_applied": True,
        "notify_unresolved": True,
        "notify_failed": True,
        "notify_duplicate": False,
        "retry_unconfirmed_duplicate": True,
    }
    assert out["backfill"] == {
        "enabled": True,
        "startup_check": True,
        "interval_sec": 300,
        "lookback_hours": 6.0,
    }
    assert out["holdings_sync"] == {
        "enabled": False,
        "debounce_sec": 2.0,
        "request_timeout_sec": 120.0,
        "max_attempts": 3,
        "retry_backoff_sec": 2.0,
        "queue_capacity": 100,
        "recent_deal_limit": 2000,
        "state_dir": out["holdings_sync"]["state_dir"],
    }
    assert str(out["holdings_sync"]["state_dir"]).endswith(
        "output_shared/state/trade_intake/stock_holdings_sync"
    )
    assert out["combo_reconciliation"] == {
        "default_mode": "off",
        "accounts": {},
    }
    assert out["settlement_observation"] == {"enabled": True}


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


def test_resolve_trade_intake_config_accepts_settlement_kill_switch() -> None:
    cfg = {
        "accounts": ["lx"],
        "trade_intake": {
            "settlement_observation": {"enabled": False}
        },
    }

    out = resolve_trade_intake_config(cfg)

    assert out["settlement_observation"] == {"enabled": False}
    assert out["sources"][0]["settlement_observation"] == {
        "enabled": False
    }


@pytest.mark.parametrize(
    "value",
    ["false", {"enabled": "false"}, {"retry_policy": "custom"}],
)
def test_resolve_trade_intake_config_rejects_invalid_settlement_config(
    value: object,
) -> None:
    with pytest.raises(ValueError, match="settlement_observation"):
        resolve_trade_intake_config(
            {
                "accounts": ["lx"],
                "trade_intake": {"settlement_observation": value},
            }
        )


def test_resolve_trade_intake_config_rejects_non_boolean_enabled() -> None:
    cfg = {"accounts": ["lx"], "trade_intake": {"enabled": "false"}}

    with pytest.raises(ValueError) as _caught:
        resolve_trade_intake_config(cfg)
    exc = _caught.value
    assert "trade_intake.enabled must be a boolean" in str(exc)


def test_resolve_trade_intake_config_accepts_backfill_overrides() -> None:
    cfg = {
        "accounts": ["lx"],
        "trade_intake": {
            "backfill": {
                "enabled": False,
                "startup_check": False,
                "interval_sec": 120,
                "lookback_hours": 12,
            }
        },
    }

    out = resolve_trade_intake_config(cfg)

    assert out["backfill"] == {
        "enabled": False,
        "startup_check": False,
        "interval_sec": 120,
        "lookback_hours": 12.0,
    }


def test_resolve_trade_intake_config_rejects_non_boolean_receipt_flag() -> None:
    cfg = {"accounts": ["lx"], "trade_intake": {"receipt": {"enabled": "yes"}}}

    with pytest.raises(ValueError) as _caught:
        resolve_trade_intake_config(cfg)
    exc = _caught.value
    assert "trade_intake.receipt.enabled must be a boolean" in str(exc)


def test_resolve_trade_intake_config_rejects_invalid_backfill_flag() -> None:
    cfg = {"accounts": ["lx"], "trade_intake": {"backfill": {"enabled": "yes"}}}

    with pytest.raises(ValueError) as _caught:
        resolve_trade_intake_config(cfg)
    exc = _caught.value
    assert "trade_intake.backfill.enabled must be a boolean" in str(exc)


def test_resolve_trade_intake_config_accepts_holdings_sync_overrides() -> None:
    cfg = {
        "accounts": ["lx"],
        "trade_intake": {
            "holdings_sync": {
                "enabled": True,
                "debounce_sec": 0.5,
                "request_timeout_sec": 45,
                "max_attempts": 4,
                "retry_backoff_sec": 1,
                "queue_capacity": 20,
                "recent_deal_limit": 500,
                "state_dir": "state/pm-sync",
            }
        },
    }

    out = resolve_trade_intake_config(cfg)["holdings_sync"]

    assert out["enabled"] is True
    assert out["debounce_sec"] == 0.5
    assert out["request_timeout_sec"] == 45.0
    assert out["max_attempts"] == 4
    assert out["retry_backoff_sec"] == 1.0
    assert out["queue_capacity"] == 20
    assert out["recent_deal_limit"] == 500
    assert str(out["state_dir"]) == "state/pm-sync"


def test_resolve_trade_intake_config_rejects_invalid_holdings_sync() -> None:
    cfg = {
        "accounts": ["lx"],
        "trade_intake": {
            "holdings_sync": {
                "enabled": True,
                "queue_capacity": 0,
            }
        },
    }

    with pytest.raises(ValueError) as _caught:
        resolve_trade_intake_config(cfg)
    exc = _caught.value
    assert "holdings_sync.queue_capacity must be > 0" in str(exc)


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


def test_resolve_futu_account_mapping_derives_from_enabled_account_settings() -> None:
    cfg = {
        "accounts": ["lx", "sy"],
        "account_settings": {
            "lx": {"type": "futu", "futu": {"account_id": "111"}},
            "sy": {"type": "futu", "trade_intake_enabled": False, "futu": {"account_id": "222"}},
        },
    }

    assert resolve_futu_account_mapping(cfg) == {"111": "lx"}
    assert resolve_futu_lookup_account_ids(cfg) == ["111"]


def test_resolve_trade_intake_sources_uses_account_opend_settings_for_multiple_accounts() -> None:
    cfg = {
        "accounts": ["lx", "sy"],
        "account_settings": {
            "lx": {
                "type": "futu",
                "futu": {"account_id": "111", "host": "127.0.0.1", "port": 11111},
            },
            "sy": {
                "type": "futu",
                "futu": {"account_id": "222", "host": "127.0.0.1", "port": 11112},
            },
        },
        "trade_intake": {
            "combo_reconciliation": {
                "default_mode": "off",
                "accounts": {"lx": "observe", "sy": "confirm"},
            }
        },
    }

    out = resolve_trade_intake_sources(
        cfg,
        mode="apply",
        enabled=True,
        receipt={"enabled": False},
        backfill={"enabled": False},
        reconnect_sec=7,
        fallback_state_path="legacy/state.json",
        fallback_audit_path="legacy/audit.jsonl",
        fallback_status_path="legacy/status.json",
    )

    assert [item["id"] for item in out] == ["lx", "sy"]
    assert [item["port"] for item in out] == [11111, 11112]
    assert out[0]["account_mapping"] == {"111": "lx"}
    assert out[1]["account_mapping"] == {"222": "sy"}
    assert str(out[0]["state_path"]) == "output_shared/state/trade_intake/lx/state.json"
    assert str(out[1]["status_path"]) == "output_shared/state/trade_intake/sy/status.json"
    assert [item["combo_reconciliation_mode"] for item in out] == [
        "observe",
        "confirm",
    ]


def test_resolve_trade_intake_sources_keeps_legacy_paths_for_single_source() -> None:
    cfg = {
        "accounts": ["lx"],
        "account_settings": {
            "lx": {
                "type": "futu",
                "futu": {"account_id": "111", "host": "127.0.0.1", "port": 11111},
            },
        },
    }

    out = resolve_trade_intake_sources(
        cfg,
        fallback_state_path="legacy/state.json",
        fallback_audit_path="legacy/audit.jsonl",
        fallback_status_path="legacy/status.json",
    )

    assert len(out) == 1
    assert out[0]["id"] == "lx"
    assert str(out[0]["state_path"]) == "legacy/state.json"
    assert str(out[0]["audit_path"]) == "legacy/audit.jsonl"
    assert str(out[0]["status_path"]) == "legacy/status.json"


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
