from __future__ import annotations

import json


def _runtime(market: str, account_id: str) -> dict:
    return {
        "_generated": {"market": market},
        "accounts": ["lx"],
        "account_settings": {
            "lx": {
                "type": "futu",
                "futu": {
                    "host": "broker",
                    "port": 11112,
                    "account_id": account_id,
                    "trd_env": "REAL",
                },
            }
        },
        "trade_intake": {"enabled": True, "mode": "dry-run"},
        "symbols": [
            {"symbol": "S", "fetch": {"source": "futu", "host": "quote", "port": 11111}}
        ],
    }


def test_routing_audit_accepts_market_specific_id_members_and_masks_ids() -> None:
    from src.application.futu_routing_audit import build_futu_routing_audit

    audit = build_futu_routing_audit(
        [
            ("us", "/runtime/config.us.json", _runtime("us", "12345678")),
            ("hk", "/runtime/config.hk.json", _runtime("hk", "87654321")),
        ]
    )

    assert audit["ok"] is True
    assert audit["quote"] == {"status": "ok", "host": "quote", "port": 11111, "member_count": 2}
    assert audit["broker_accounts"][0]["required_account_id_count"] == 2
    rendered = json.dumps(audit)
    assert "12345678" not in rendered
    assert "87654321" not in rendered
    assert ".../config.us.json" in rendered


def test_routing_audit_rejects_multi_account_shared_broker_endpoint() -> None:
    from src.application.futu_routing_audit import build_futu_routing_audit

    cfg = _runtime("us", "12345678")
    cfg["accounts"] = ["lx", "sy"]
    cfg["account_settings"]["sy"] = {
        "type": "futu",
        "futu": {
            "host": "broker",
            "port": 11112,
            "account_id": "11223344",
            "trd_env": "REAL",
        },
    }

    audit = build_futu_routing_audit([("us", "/runtime/config.us.json", cfg)])

    assert audit["ok"] is False
    assert any(item["code"] == "broker_binding_conflict" for item in audit["errors"])
