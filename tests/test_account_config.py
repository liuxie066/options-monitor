from __future__ import annotations


def test_accounts_from_config_normalizes_and_dedupes() -> None:
    from scripts.account_config import accounts_from_config

    assert accounts_from_config({"accounts": [" LX ", "sy", "lx", ""]}) == ["lx", "sy"]


def test_accounts_from_config_keeps_legacy_fallback() -> None:
    from scripts.account_config import accounts_from_config

    assert accounts_from_config({}) == ["lx", "sy"]


def test_cash_footer_accounts_prefers_notification_override_then_accounts() -> None:
    from scripts.account_config import cash_footer_accounts_from_config

    assert cash_footer_accounts_from_config({"accounts": ["alpha"]}) == ["alpha"]
    assert cash_footer_accounts_from_config(
        {
            "accounts": ["alpha"],
            "notifications": {"cash_footer_accounts": ["beta", "gamma"]},
        }
    ) == ["beta", "gamma"]


def test_parse_option_message_accepts_configured_account_labels() -> None:
    from scripts.parse_option_message import parse_account

    assert parse_account("成交 accountA账户", accounts=["accountA"]) == "accounta"
    assert parse_account("成交 lx", accounts=["accountA"]) is None
