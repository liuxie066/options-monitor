from __future__ import annotations

import json
from pathlib import Path

import pytest

import src.application.config_validator as mod


def _base_cfg() -> dict[str, object]:
    return {
        "accounts": ["user1"],
        "account_settings": {
            "user1": {
                "type": "futu",
                "futu": {"account_id": "REAL_12345678"},
            }
        },
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


def _config(**sections: object) -> dict[str, object]:
    cfg = _base_cfg()
    cfg.update(sections)
    return cfg


def _reject(cfg: dict[str, object], *expected: str) -> None:
    with pytest.raises(SystemExit) as _caught:
        mod.validate_config(cfg)
    message = str(_caught.value)
    for fragment in expected:
        assert fragment in message


def _reject_config(*expected: str, **sections: object) -> None:
    _reject(_config(**sections), *expected)


def test_validate_config_rejects_empty_notification_target() -> None:
    _reject_config(
        "notifications.target must be a non-empty wechat_clawbot binding string",
        notifications={"provider": "wechat_clawbot", "target": ""},
    )


def test_validate_config_rejects_non_string_notification_target() -> None:
    _reject_config(
        "notifications.target must be a string when configured",
        notifications={"provider": "wechat_clawbot", "target": ["ou_x"]},
    )


def test_validate_config_rejects_openclaw_notification_route() -> None:
    _reject_config(
        "OpenClaw notification routing has been removed",
        notifications={
            "provider": "openclaw",
            "channel": "openclaw-weixin",
            "target": "clawbot:test-room",
        },
    )


def test_validate_config_rejects_retired_agent_config() -> None:
    _reject_config(
        "agent.* config is retired; use assistant.*",
        agent={"runtime": {"enabled": "yes"}},
    )


def test_validate_config_rejects_invalid_assistant_context_window() -> None:
    _reject_config(
        "assistant.context_window_messages must be an integer",
        assistant={"context_window_messages": "many"},
    )
    _reject_config(
        "assistant.context_window_messages must be <= 20",
        assistant={"context_window_messages": 21},
    )


def test_validate_config_rejects_assistant_mode() -> None:
    _reject_config(
        "assistant has unsupported keys: mode",
        assistant={"mode": "disabled"},
    )


def test_validate_config_rejects_non_object_assistant() -> None:
    _reject_config("assistant must be an object", assistant=False)


def test_validate_config_rejects_invalid_assistant_llm_config() -> None:
    _reject_config(
        "assistant.llm.enabled is retired; use assistant.bot.enabled",
        assistant={"llm": {"enabled": True}},
    )
    _reject_config(
        "assistant.llm.provider must be a string",
        assistant={"llm": {"provider": ["openai"]}},
    )


def test_validate_config_accepts_known_boolean_bot_toolsets() -> None:
    for enabled in (True, False):
        mod.validate_config(
            _config(
                assistant={
                    "enabled": True,
                    "bot": {"enabled": True, "toolsets": {"portfolio": enabled}},
                }
            )
        )


def test_validate_config_rejects_invalid_bot_toolsets() -> None:
    cases = (
        ({"portfolio": "yes"}, "assistant.bot.toolsets.portfolio must be a boolean"),
        ({"portfolio": None}, "assistant.bot.toolsets.portfolio must be a boolean"),
        ({"unknown": True}, "assistant.bot.toolsets contains unsupported keys: unknown"),
    )
    for toolsets, expected in cases:
        _reject_config(expected, assistant={"bot": {"enabled": True, "toolsets": toolsets}})

    _reject_config(
        "assistant.bot.toolsets must be an object",
        assistant={"bot": {"enabled": True, "toolsets": ["portfolio"]}},
    )
    _reject_config(
        "assistant.llm.base_url must be a string",
        assistant={"llm": {"base_url": ["https://llm.example/v1"]}},
    )
    _reject_config(
        "assistant.llm.base_url must start with http:// or https:// when set",
        assistant={"llm": {"base_url": "llm.example/v1"}},
    )
    _reject_config(
        "assistant.llm.confidence_min must be between 0 and 1",
        assistant={"llm": {"confidence_min": 1.5}},
    )
    _reject_config(
        "assistant.llm.timeout_seconds must be an integer",
        assistant={"llm": {"timeout_seconds": "slow"}},
    )
    _reject_config(
        "assistant.llm.timeout_seconds must be <= 120",
        assistant={"llm": {"timeout_seconds": 121}},
    )
    _reject_config(
        "assistant.llm.max_output_tokens must be >= 64",
        assistant={"llm": {"max_output_tokens": 63}},
    )
    mod.validate_config(_config(assistant={"llm": {"max_output_tokens": 4097}}))
    _reject_config(
        "assistant.llm.provider must be one of: openai, deepseek, kimi",
        assistant={"llm": {"provider": "anthropic"}},
    )


def test_validate_config_rejects_legacy_assistant_modes_and_accepts_bot_config() -> None:
    _reject_config(
        "assistant has unsupported keys: mode",
        assistant={
            "mode": "llm_router",
            "llm": {"provider": "", "model": "gpt-5.2", "api_key_env": "OM_LLM_API_KEY"},
        },
    )
    _reject_config("assistant has unsupported keys: mode", assistant={"mode": "deterministic"})
    _reject_config("assistant.enabled must be a boolean", assistant={"enabled": "yes"})
    _reject_config("assistant has unsupported keys: planner", assistant={"planner": "enabled"})
    _reject_config("assistant has unsupported keys: planner", assistant={"planner": {"enabled": "yes"}})
    mod.validate_config(
        _config(
            assistant={
                "enabled": True,
                "bot": {"enabled": True},
                "llm": {
                    "provider": "openai",
                    "base_url": "https://llm.example/v1",
                    "model": "gpt-5.2",
                    "api_key_env": "OM_LLM_API_KEY",
                    "timeout_seconds": 20,
                    "context_window_tokens": 24000,
                    "max_output_tokens": 512,
                },
            }
        )
    )
    mod.validate_config(
        _config(
            assistant={
                "enabled": True,
                "bot": {"enabled": True},
                "llm": {
                    "provider": "deepseek",
                    "base_url": "https://api.deepseek.com",
                    "model": "deepseek-v4-flash",
                    "api_key_env": "DEEPSEEK_API_KEY",
                    "timeout_seconds": 20,
                    "context_window_tokens": 24000,
                    "max_output_tokens": 512,
                },
            }
        )
    )


@pytest.mark.parametrize(
    ("context_window_tokens", "message"),
    [
        (None, "context_window_tokens is required"),
        (4095, "context_window_tokens must be >= 4096"),
        (2_000_001, "context_window_tokens must be <= 2000000"),
        (2512, "context_window_tokens must be >= 4096"),
        (4096, "must exceed output reservation 4096 by more than 2000"),
    ],
)
def test_validate_active_bot_context_window(context_window_tokens, message) -> None:
    llm = {
        "provider": "openai",
        "model": "gpt-5.2",
        "max_output_tokens": 4096 if context_window_tokens == 4096 else 2048,
    }
    if context_window_tokens is not None:
        llm["context_window_tokens"] = context_window_tokens
    cfg = _config(
        assistant={
            "enabled": True,
            "bot": {"enabled": True},
            "llm": llm,
        }
    )

    with pytest.raises(SystemExit, match=message):
        mod.validate_config(cfg)


def test_validate_config_rejects_retired_intake_multiplier_metadata() -> None:
    _reject_config(
        "intake.multiplier_by_symbol is retired",
        intake={"symbol_aliases": {"中海油": "0883.HK"}, "multiplier_by_symbol": {"0883.HK": 1000}},
    )


def test_validate_config_accepts_wechat_clawbot_without_feishu_secrets() -> None:
    mod.validate_config(_config(notifications={"channel": "wechat_clawbot", "target": "clawbot:test-room"}))


def test_validate_config_accepts_feishu_app_without_config_target() -> None:
    mod.validate_config(_config(notifications={"provider": "feishu_app"}))


def test_validate_config_rejects_feishu_app_config_target() -> None:
    _reject_config(
        "OM_FEISHU_BOT_USER_OPEN_ID",
        notifications={"provider": "feishu_app", "target": "ou_xxx"},
    )


def test_validate_config_rejects_empty_wechat_clawbot_target() -> None:
    _reject_config(
        "notifications.target must be a non-empty wechat_clawbot binding string",
        notifications={"channel": "wechat_clawbot", "target": ""},
    )


def test_validate_config_rejects_unsupported_notification_channel() -> None:
    _reject_config(
        "notifications.provider must be one of: wechat_clawbot, feishu_app",
        notifications={"provider": "sms", "target": "user:test"},
    )


def test_validate_config_rejects_removed_openclaw_channel_with_wechat_provider() -> None:
    _reject_config(
        "OpenClaw notification routing has been removed",
        "provider=wechat_clawbot",
        notifications={"provider": "wechat_clawbot", "channel": "openclaw-weixin", "target": "wechat:ops"},
    )


def test_validate_config_rejects_removed_openclaw_transport_channel() -> None:
    _reject_config(
        "OpenClaw notification routing has been removed",
        notifications={"transport_channel": "openclaw-weixin", "target": "wechat:ops"},
    )


def test_validate_config_rejects_non_boolean_trade_intake_enabled() -> None:
    _reject_config(
        "trade_intake.enabled must be a boolean",
        trade_intake={"enabled": "false"},
    )


def test_validate_config_rejects_non_boolean_account_trade_intake_enabled() -> None:
    cfg = _base_cfg()
    cfg["account_settings"]["user1"]["trade_intake_enabled"] = "false"

    _reject(cfg, "account_settings.user1.trade_intake_enabled must be a boolean")


def test_validate_config_rejects_futu_account_without_account_id() -> None:
    cfg = _base_cfg()
    cfg["account_settings"]["user1"]["futu"] = {}

    _reject(cfg, "account_settings.user1.futu.account_id must be a non-empty string")


def test_validate_config_requires_host_port_for_multiple_futu_accounts() -> None:
    cfg = _base_cfg()
    cfg["accounts"] = ["user1", "sy"]
    cfg["account_settings"]["sy"] = {
        "type": "futu",
        "futu": {"account_id": "REAL_87654321", "host": "127.0.0.1", "port": 11112},
    }

    _reject(
        cfg,
        "account_settings.user1.futu.host must be set when multiple futu accounts are configured",
    )


def test_validate_config_rejects_lossy_or_out_of_range_futu_ports() -> None:
    for value, expected in (
        (11111.9, "must be an integer"),
        (True, "must be an integer"),
        ("11111.0", "must be an integer"),
        (" 11111", "must be an integer"),
        (0, "must be between 1 and 65535"),
        (70000, "must be between 1 and 65535"),
    ):
        cfg = _base_cfg()
        cfg["account_settings"]["user1"]["futu"]["port"] = value

        _reject(cfg, expected)


def test_validate_config_rejects_unknown_futu_trade_environments() -> None:
    cases = [
        ("portfolio.futu.trd_env", lambda cfg: cfg["portfolio"].update(
            futu={"host": "127.0.0.1", "port": 11111, "trd_env": "SIMULATED"}
        )),
        (
            "account_settings.user1.futu.trd_env",
            lambda cfg: cfg["account_settings"]["user1"]["futu"].update(
                trd_env="PAPER"
            ),
        ),
        (
            "NVDA.fetch.trd_env",
            lambda cfg: cfg["symbols"][0]["fetch"].update(
                trd_env="PRODUCTION"
            ),
        ),
    ]
    for expected, mutate in cases:
        cfg = _base_cfg()
        mutate(cfg)
        _reject(cfg, expected)


def test_validate_config_accepts_option_positions_auto_close_enabled_boolean() -> None:
    mod.validate_config(
        _config(
            option_positions={
                "auto_close": {
                    "enabled": False,
                    "receipt": {"enabled": True, "notify_failed": True, "notify_noop": False},
                }
            }
        )
    )


def test_validate_config_rejects_non_boolean_option_positions_auto_close_enabled() -> None:
    _reject_config(
        "option_positions.auto_close.enabled must be a boolean",
        option_positions={"auto_close": {"enabled": "no"}},
    )


def test_validate_config_rejects_non_boolean_option_positions_auto_close_receipt() -> None:
    _reject_config(
        "option_positions.auto_close.receipt.enabled must be a boolean",
        option_positions={"auto_close": {"receipt": {"enabled": "yes"}}},
    )


def test_validate_config_rejects_option_positions_feishu_sync_config() -> None:
    _reject_config(
        "option_positions.sync_to_feishu has been removed",
        option_positions={"sync_to_feishu": {"enabled": True}},
    )


def test_validate_config_rejects_inline_secret_material() -> None:
    _reject_config(
        "must not contain inline secret material",
        feishu={"app_secret": "secret_in_json"},
    )


def test_validate_config_rejects_retired_feishu_callback_keys() -> None:
    _reject_config(
        "feishu.bot.app_secret logical credential",
        inbound={"feishu": {"verification_token_env": "OM_OLD_TOKEN"}},
    )


def test_validate_config_accepts_default_off_daily_brief() -> None:
    mod.validate_config(
        _config(
            notifications={
                "daily_brief": {
                    "enabled": False,
                    "max_actions_per_priority": 5,
                    "max_candidates_per_strategy": 3,
                    "max_rejection_reasons": 5,
                }
            }
        )
    )


def test_validate_config_rejects_invalid_daily_brief_contract() -> None:
    for daily_brief, expected in (
        (True, "notifications.daily_brief must be an object"),
        ({"enabled": "yes"}, "notifications.daily_brief.enabled must be a boolean"),
        ({"max_actions_per_priority": 0}, "must be between 1 and 20"),
        ({"max_candidates_per_strategy": 21}, "must be between 1 and 20"),
        ({"max_rejection_reasons": 1.5}, "must be an integer"),
    ):
        _reject_config(expected, notifications={"daily_brief": daily_brief})


def test_daily_brief_defaults_and_examples_remove_deprecated_enabled_switch() -> None:
    from src.application.config_defaults import DEFAULT_CONFIG

    root = Path(__file__).resolve().parents[1]
    example = json.loads((root / "configs" / "examples" / "user.common.example.json").read_text())
    system = json.loads((root / "configs" / "system.json").read_text())

    assert "enabled" not in DEFAULT_CONFIG["defaults"]["notifications"]["daily_brief"]
    assert "enabled" not in example["notifications"]["daily_brief"]
    assert "enabled" not in system["defaults"]["notifications"]["daily_brief"]


def test_deprecated_notification_renderer_keys_warn_but_do_not_fail(capsys) -> None:
    for enabled in (True, False):
        mod.validate_config(
            _config(
                notifications={
                    "daily_brief": {"enabled": enabled},
                    "render_style": "legacy",
                }
            )
        )

    stderr = capsys.readouterr().err
    assert stderr.count("NOTIFICATIONS_DAILY_BRIEF_ENABLED_DEPRECATED") == 2
    assert stderr.count("NOTIFICATIONS_RENDER_STYLE_DEPRECATED") == 2
    assert stderr.count("notifications.daily_brief.enabled is deprecated and ignored") == 2
    assert stderr.count("notifications.render_style=legacy is deprecated and ignored") == 2


def test_notification_render_style_rejects_unknown_or_wrong_type() -> None:
    for value in ("typo", 1, None):
        _reject_config(
            "notifications.render_style must be one of: compact, legacy",
            notifications={"render_style": value},
        )
