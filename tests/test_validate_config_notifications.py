from __future__ import annotations

import json
from pathlib import Path


def _base_cfg() -> dict[str, object]:
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


def test_validate_config_rejects_empty_notification_target(tmp_path: Path) -> None:
    import src.application.config_validator as mod

    secrets = tmp_path / "notif.json"
    secrets.write_text(json.dumps({"feishu": {"app_id": "cli", "app_secret": "sec"}}), encoding="utf-8")
    cfg = _base_cfg()
    cfg["notifications"] = {"provider": "openclaw", "channel": "openclaw-weixin", "target": ""}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "notifications.target must be a non-empty openclaw target string" in str(exc)


def test_validate_config_rejects_non_string_notification_target(tmp_path: Path) -> None:
    import src.application.config_validator as mod

    secrets = tmp_path / "notif.json"
    secrets.write_text(json.dumps({"feishu": {"app_id": "cli", "app_secret": "sec"}}), encoding="utf-8")
    cfg = _base_cfg()
    cfg["notifications"] = {"provider": "openclaw", "channel": "openclaw-weixin", "target": ["ou_x"]}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "notifications.target must be a non-empty openclaw target string" in str(exc)


def test_validate_config_accepts_valid_openclaw_notification_route(tmp_path: Path) -> None:
    import src.application.config_validator as mod

    secrets = tmp_path / "notif.json"
    secrets.write_text(json.dumps({"feishu": {"app_id": "cli", "app_secret": "sec"}}), encoding="utf-8")
    cfg = _base_cfg()
    cfg["notifications"] = {"provider": "openclaw", "channel": "openclaw-weixin", "target": "clawbot:test-room"}

    mod.validate_config(cfg)


def test_validate_config_accepts_wechat_clawbot_without_feishu_secrets() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["notifications"] = {"channel": "wechat_clawbot", "target": "clawbot:test-room"}

    mod.validate_config(cfg)


def test_validate_config_rejects_empty_wechat_clawbot_target() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["notifications"] = {"channel": "wechat_clawbot", "target": ""}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "notifications.target must be a non-empty openclaw target string" in str(exc)


def test_validate_config_rejects_unsupported_notification_channel() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["notifications"] = {"provider": "sms", "target": "user:test"}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "notifications.provider must be one of: openclaw, feishu_app" in str(exc)


def test_validate_config_accepts_option_positions_sync_to_feishu_enabled_boolean() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["option_positions"] = {"sync_to_feishu": {"enabled": False}}

    mod.validate_config(cfg)


def test_validate_config_rejects_non_boolean_option_positions_sync_to_feishu_enabled() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["option_positions"] = {"sync_to_feishu": {"enabled": "no"}}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "option_positions.sync_to_feishu.enabled must be a boolean" in str(exc)
