from __future__ import annotations

import json
from pathlib import Path


def _base_cfg() -> dict:
    return {
        "accounts": ["user1"],
        "account_settings": {"user1": {"type": "futu"}},
        "portfolio": {
            "data_config": "secrets/portfolio.sqlite.json",
            "broker": "富途",
            "account": "user1",
            "source": "futu",
            "base_currency": "CNY",
        },
        "symbols": [
            {
                "symbol": "NVDA",
                "market": "US",
                "fetch": {"source": "futu"},
                "sell_put": {"enabled": False},
                "sell_call": {"enabled": False},
            }
        ],
    }


def test_validate_config_rejects_empty_notification_target(tmp_path: Path, monkeypatch) -> None:
    import scripts.validate_config as mod

    secrets = tmp_path / "notif.json"
    secrets.write_text(json.dumps({"feishu": {"app_id": "cli", "app_secret": "sec"}}), encoding="utf-8")
    monkeypatch.setattr(mod, "repo_base", tmp_path)

    cfg = _base_cfg()
    cfg["notifications"] = {"channel": "feishu", "target": "", "secrets_file": "notif.json"}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "notifications.target must be a non-empty open_id string" in str(exc)


def test_validate_config_rejects_non_string_notification_target(tmp_path: Path, monkeypatch) -> None:
    import scripts.validate_config as mod

    secrets = tmp_path / "notif.json"
    secrets.write_text(json.dumps({"feishu": {"app_id": "cli", "app_secret": "sec"}}), encoding="utf-8")
    monkeypatch.setattr(mod, "repo_base", tmp_path)

    cfg = _base_cfg()
    cfg["notifications"] = {"channel": "feishu", "target": ["ou_x"], "secrets_file": "notif.json"}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "notifications.target must be a non-empty open_id string" in str(exc)


def test_validate_config_accepts_valid_notification_open_id(tmp_path: Path, monkeypatch) -> None:
    import scripts.validate_config as mod

    secrets = tmp_path / "notif.json"
    secrets.write_text(json.dumps({"feishu": {"app_id": "cli", "app_secret": "sec"}}), encoding="utf-8")
    monkeypatch.setattr(mod, "repo_base", tmp_path)

    cfg = _base_cfg()
    cfg["notifications"] = {"channel": "feishu", "target": "ou_valid", "secrets_file": "notif.json"}

    mod.validate_config(cfg)
