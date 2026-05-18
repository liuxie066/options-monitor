from __future__ import annotations

from src.application.secret_resolver import (
    resolve_feishu_holdings_config,
    resolve_feishu_notification_app_config,
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


def test_feishu_notification_resolver_ignores_plain_secret_values() -> None:
    resolved = resolve_feishu_notification_app_config(
        {"app_id": "app_in_json", "app_secret": "secret_in_json"},
        environ={},
    )

    assert resolved.ready is False
    assert resolved.missing_fields == ("OM_NOTIFY_FEISHU_APP_ID", "OM_NOTIFY_FEISHU_APP_SECRET")
