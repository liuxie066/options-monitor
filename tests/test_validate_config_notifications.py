from __future__ import annotations


def _base_cfg() -> dict[str, object]:
    return {
        "accounts": ["user1"],
        "account_settings": {"user1": {"type": "futu"}},
        "portfolio": {
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


def test_validate_config_rejects_empty_notification_target() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["notifications"] = {"provider": "openclaw", "channel": "openclaw-weixin", "target": ""}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "notifications.target must be a non-empty openclaw target string" in str(exc)


def test_validate_config_rejects_non_string_notification_target() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["notifications"] = {"provider": "openclaw", "channel": "openclaw-weixin", "target": ["ou_x"]}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "notifications.target must be a non-empty openclaw target string" in str(exc)


def test_validate_config_accepts_valid_openclaw_notification_route() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["notifications"] = {"provider": "openclaw", "channel": "openclaw-weixin", "target": "clawbot:test-room"}

    mod.validate_config(cfg)


def test_validate_config_accepts_wechat_clawbot_without_feishu_secrets() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["notifications"] = {"channel": "wechat_clawbot", "target": "clawbot:test-room"}

    mod.validate_config(cfg)


def test_validate_config_accepts_feishu_app_without_config_target() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["notifications"] = {"provider": "feishu_app"}

    mod.validate_config(cfg)


def test_validate_config_rejects_feishu_app_config_target() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["notifications"] = {"provider": "feishu_app", "target": "ou_xxx"}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "OM_FEISHU_BOT_USER_OPEN_ID" in str(exc)


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


def test_validate_config_accepts_option_positions_auto_close_enabled_boolean() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["option_positions"] = {
        "auto_close": {
            "enabled": False,
            "receipt": {"enabled": True, "notify_failed": True, "notify_noop": False},
        }
    }

    mod.validate_config(cfg)


def test_validate_config_rejects_non_boolean_option_positions_auto_close_enabled() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["option_positions"] = {"auto_close": {"enabled": "no"}}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "option_positions.auto_close.enabled must be a boolean" in str(exc)


def test_validate_config_rejects_non_boolean_option_positions_auto_close_receipt() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["option_positions"] = {"auto_close": {"receipt": {"enabled": "yes"}}}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "option_positions.auto_close.receipt.enabled must be a boolean" in str(exc)


def test_validate_config_rejects_option_positions_feishu_sync_config() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["option_positions"] = {"sync_to_feishu": {"enabled": True}}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "option_positions.sync_to_feishu has been removed" in str(exc)


def test_validate_config_rejects_inline_secret_material() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["feishu"] = {"app_secret": "secret_in_json"}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "must not contain inline secret material" in str(exc)


def test_validate_config_rejects_retired_feishu_callback_keys() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["inbound"] = {"feishu": {"verification_token_env": "OM_OLD_TOKEN"}}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "Feishu inbound uses long-connection Bot env settings" in str(exc)
