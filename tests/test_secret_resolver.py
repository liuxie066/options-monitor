from __future__ import annotations

import pytest

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
    assert resolved.missing_fields == (
        "OM_FEISHU_APP_ID",
        "feishu.holdings.app_secret",
        "OM_FEISHU_HOLDINGS_TABLE",
    )


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
        "feishu.bot.app_secret",
        "OM_FEISHU_BOT_ALLOWED_OPEN_IDS",
    )


def test_feishu_bot_config_metadata_only_with_legacy_env() -> None:
    """metadata_only mode reports credential presence without resolving the value."""
    env = {
        "OM_FEISHU_BOT_APP_ID": "cli_test",
        "OM_FEISHU_BOT_APP_SECRET": "real-secret-here",
        "OM_FEISHU_BOT_USER_OPEN_ID": "ou_test",
    }

    cfg = resolve_feishu_bot_config(environ=env, metadata_only=True)

    assert cfg.app_id == "cli_test"
    assert cfg.app_secret == "configured"
    assert cfg.credentials_ready is True


def test_feishu_bot_config_metadata_only_without_secret() -> None:
    """metadata_only mode returns empty app_secret when no credential is found."""
    env = {
        "OM_FEISHU_BOT_APP_ID": "cli_test",
        "OM_FEISHU_BOT_USER_OPEN_ID": "ou_test",
    }

    cfg = resolve_feishu_bot_config(environ=env, metadata_only=True)

    assert cfg.app_id == "cli_test"
    assert cfg.app_secret == ""
    assert cfg.credentials_ready is False


def test_resolve_secret_status_fallback_on_backend_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_secret_status degrades gracefully when the secret backend is unavailable."""
    from src.application.secret_store.runtime import resolve_secret_status
    from src.application.secret_store.registry import FEISHU_BOT_APP_SECRET
    import src.infrastructure.secret_store.factory as factory

    monkeypatch.delenv("OM_SECRET_BACKEND", raising=False)
    original_platform = factory._platform_name
    factory._platform_name = lambda v=None: "linux"
    try:
        # No CREDENTIALS_DIRECTORY, no legacy env → unavailable but not crashed
        # Use environ={} to isolate from process env (OM_SECRET_BACKEND may be set)
        env: dict[str, str] = {}
        status = resolve_secret_status(
            FEISHU_BOT_APP_SECRET,
            environ=env,
            legacy_env_name="OM_FEISHU_BOT_APP_SECRET",
        )
        assert status.configured is False
        assert status.backend == "unavailable"
        assert status.source == "missing"

        # With legacy env present → detected via fallback
        env2 = dict(env)
        env2["OM_FEISHU_BOT_APP_SECRET"] = "real-secret"
        status2 = resolve_secret_status(
            FEISHU_BOT_APP_SECRET,
            environ=env2,
            legacy_env_name="OM_FEISHU_BOT_APP_SECRET",
        )
        assert status2.configured is True
        assert status2.backend == "unavailable"
        assert status2.source == "legacy_env_fallback"
    finally:
        factory._platform_name = original_platform
