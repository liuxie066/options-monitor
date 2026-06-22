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
    cfg["notifications"] = {"provider": "wechat_clawbot", "target": ""}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "notifications.target must be a non-empty wechat_clawbot binding string" in str(exc)


def test_validate_config_rejects_non_string_notification_target() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["notifications"] = {"provider": "wechat_clawbot", "target": ["ou_x"]}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "notifications.target must be a string when configured" in str(exc)


def test_validate_config_rejects_openclaw_notification_route() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["notifications"] = {"provider": "openclaw", "channel": "openclaw-weixin", "target": "clawbot:test-room"}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "OpenClaw notification routing has been removed" in str(exc)


def test_validate_config_rejects_retired_agent_config() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["agent"] = {"runtime": {"enabled": "yes"}}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "agent.* config is retired; use assistant.*" in str(exc)


def test_validate_config_rejects_invalid_assistant_context_window() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["assistant"] = {"context_window_messages": "many"}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "assistant.context_window_messages must be an integer" in str(exc)

    cfg = _base_cfg()
    cfg["assistant"] = {"context_window_messages": 21}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "assistant.context_window_messages must be <= 20" in str(exc)


def test_validate_config_rejects_assistant_mode() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["assistant"] = {"mode": "disabled"}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "assistant has unsupported keys: mode" in str(exc)


def test_validate_config_rejects_non_object_assistant() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["assistant"] = False

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "assistant must be an object" in str(exc)


def test_validate_config_rejects_invalid_assistant_llm_config() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["assistant"] = {"llm": {"enabled": True}}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "assistant.llm.enabled is retired; use assistant.agent_loop.enabled" in str(exc)

    cfg = _base_cfg()
    cfg["assistant"] = {"llm": {"provider": ["openai"]}}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "assistant.llm.provider must be a string" in str(exc)

    cfg = _base_cfg()
    cfg["assistant"] = {"llm": {"base_url": ["https://llm.example/v1"]}}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "assistant.llm.base_url must be a string" in str(exc)

    cfg = _base_cfg()
    cfg["assistant"] = {"llm": {"base_url": "llm.example/v1"}}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "assistant.llm.base_url must start with http:// or https:// when set" in str(exc)

    cfg = _base_cfg()
    cfg["assistant"] = {"llm": {"confidence_min": 1.5}}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "assistant.llm.confidence_min must be between 0 and 1" in str(exc)

    cfg = _base_cfg()
    cfg["assistant"] = {"llm": {"timeout_seconds": "slow"}}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "assistant.llm.timeout_seconds must be an integer" in str(exc)

    cfg = _base_cfg()
    cfg["assistant"] = {"llm": {"timeout_seconds": 121}}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "assistant.llm.timeout_seconds must be <= 120" in str(exc)

    cfg = _base_cfg()
    cfg["assistant"] = {"llm": {"max_output_tokens": 63}}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "assistant.llm.max_output_tokens must be >= 64" in str(exc)

    cfg = _base_cfg()
    cfg["assistant"] = {"llm": {"max_output_tokens": 4097}}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "assistant.llm.max_output_tokens must be <= 4096" in str(exc)

    cfg = _base_cfg()
    cfg["assistant"] = {"llm": {"provider": "anthropic"}}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "assistant.llm.provider must be one of: openai, deepseek, kimi" in str(exc)


def test_validate_config_rejects_legacy_assistant_modes_and_accepts_planner_config() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["assistant"] = {"mode": "llm_router", "llm": {"provider": "", "model": "gpt-5.2", "api_key_env": "OM_LLM_API_KEY"}}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "assistant has unsupported keys: mode" in str(exc)

    cfg = _base_cfg()
    cfg["assistant"] = {"mode": "deterministic"}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "assistant has unsupported keys: mode" in str(exc)

    cfg = _base_cfg()
    cfg["assistant"] = {"enabled": "yes"}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "assistant.enabled must be a boolean" in str(exc)

    cfg = _base_cfg()
    cfg["assistant"] = {"planner": "enabled"}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "assistant.planner must be an object" in str(exc)

    cfg = _base_cfg()
    cfg["assistant"] = {"planner": {"enabled": "yes"}}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "assistant.planner.enabled must be a boolean" in str(exc)

    cfg = _base_cfg()
    cfg["assistant"] = {
        "enabled": True,
        "planner": {"enabled": True},
        "llm": {
            "provider": "openai",
            "base_url": "https://llm.example/v1",
            "model": "gpt-5.2",
            "api_key_env": "OM_LLM_API_KEY",
            "timeout_seconds": 20,
            "max_output_tokens": 512,
        }
    }
    mod.validate_config(cfg)

    cfg = _base_cfg()
    cfg["assistant"] = {
        "enabled": True,
        "planner": {"enabled": True},
        "llm": {
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-flash",
            "api_key_env": "DEEPSEEK_API_KEY",
            "timeout_seconds": 20,
            "max_output_tokens": 512,
        }
    }
    mod.validate_config(cfg)


def test_validate_config_rejects_retired_intake_multiplier_metadata() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["intake"] = {"symbol_aliases": {"中海油": "0883.HK"}, "multiplier_by_symbol": {"0883.HK": 1000}}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "intake.multiplier_by_symbol is retired" in str(exc)


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
        assert "notifications.target must be a non-empty wechat_clawbot binding string" in str(exc)


def test_validate_config_rejects_unsupported_notification_channel() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["notifications"] = {"provider": "sms", "target": "user:test"}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "notifications.provider must be one of: wechat_clawbot, feishu_app" in str(exc)


def test_validate_config_rejects_removed_openclaw_channel_with_wechat_provider() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["notifications"] = {"provider": "wechat_clawbot", "channel": "openclaw-weixin", "target": "wechat:ops"}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "OpenClaw notification routing has been removed" in str(exc)
        assert "provider=wechat_clawbot" in str(exc)


def test_validate_config_rejects_removed_openclaw_transport_channel() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["notifications"] = {"transport_channel": "openclaw-weixin", "target": "wechat:ops"}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "OpenClaw notification routing has been removed" in str(exc)


def test_validate_config_rejects_non_boolean_trade_intake_enabled() -> None:
    import src.application.config_validator as mod

    cfg = _base_cfg()
    cfg["trade_intake"] = {"enabled": "false"}

    try:
        mod.validate_config(cfg)
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "trade_intake.enabled must be a boolean" in str(exc)


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
