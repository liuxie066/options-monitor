from __future__ import annotations

from src.application.secret_resolver import (
    resolve_feishu_bot_config,
    resolve_feishu_holdings_config,
)


def test_feishu_holdings_resolver_uses_environment_values() -> None:
    cfg = {
        "feishu": {
            "app_id_env": "CUSTOM_FEISHU_APP_ID",
            "app_secret_env": "CUSTOM_FEISHU_APP_SECRET",
            "tables": {"holdings_env": "CUSTOM_FEISHU_HOLDINGS_TABLE"},
        }
    }

    resolved = resolve_feishu_holdings_config(
        cfg,
        environ={
            "CUSTOM_FEISHU_APP_ID": "app_1",
            "CUSTOM_FEISHU_APP_SECRET": "secret_1",
            "CUSTOM_FEISHU_HOLDINGS_TABLE": "app_token/table_id",
        },
    )

    assert resolved.ready is True
    assert resolved.app_id == "app_1"
    assert resolved.app_secret == "secret_1"
    assert resolved.holdings_ref == "app_token/table_id"


def test_feishu_holdings_resolver_ignores_plain_secret_values() -> None:
    cfg = {
        "feishu": {
            "app_id": "app_in_json",
            "app_secret": "secret_in_json",
            "tables": {"holdings": "app_token/table_id"},
        }
    }

    resolved = resolve_feishu_holdings_config(cfg, environ={})

    assert resolved.ready is False
    assert resolved.missing_fields == ("OM_FEISHU_APP_ID", "OM_FEISHU_APP_SECRET", "OM_FEISHU_HOLDINGS_TABLE")


def test_feishu_bot_resolver_defaults_allowed_open_ids_to_user_open_id() -> None:
    resolved = resolve_feishu_bot_config(
        environ={
            "OM_FEISHU_BOT_APP_ID": "cli_1",
            "OM_FEISHU_BOT_APP_SECRET": "secret_1",
            "OM_FEISHU_BOT_USER_OPEN_ID": "ou_1",
        }
    )

    assert resolved.send_ready is True
    assert resolved.inbound_ready is True
    assert resolved.allowed_open_ids == ("ou_1",)
    assert resolved.default_allowed_senders() == "feishu:ou_1"


def test_feishu_bot_inbound_requires_allowed_sender() -> None:
    resolved = resolve_feishu_bot_config(
        environ={
            "OM_FEISHU_BOT_APP_ID": "cli_1",
            "OM_FEISHU_BOT_APP_SECRET": "secret_1",
        }
    )

    assert resolved.inbound_ready is False
    assert resolved.inbound_missing_fields == ("OM_FEISHU_BOT_ALLOWED_OPEN_IDS",)


def test_feishu_bot_resolver_ignores_custom_env_name_config() -> None:
    resolved = resolve_feishu_bot_config(
        {
            "app_id_env": "CUSTOM_APP_ID",
            "app_secret_env": "CUSTOM_APP_SECRET",
            "target_env": "CUSTOM_OPEN_ID",
            "feishu": {
                "bot": {
                    "allowed_open_ids_env": "CUSTOM_ALLOWED",
                    "encrypt_key_env": "CUSTOM_ENCRYPT",
                    "verification_token_env": "CUSTOM_TOKEN",
                }
            },
        },
        environ={
            "CUSTOM_APP_ID": "cli_custom",
            "CUSTOM_APP_SECRET": "secret_custom",
            "CUSTOM_OPEN_ID": "ou_custom",
            "CUSTOM_ALLOWED": "ou_custom",
        },
    )

    assert resolved.app_id == ""
    assert resolved.app_secret == ""
    assert resolved.user_open_id == ""
    assert resolved.allowed_open_ids == ()
    assert resolved.inbound_missing_fields == (
        "OM_FEISHU_BOT_APP_ID",
        "OM_FEISHU_BOT_APP_SECRET",
        "OM_FEISHU_BOT_ALLOWED_OPEN_IDS",
    )
