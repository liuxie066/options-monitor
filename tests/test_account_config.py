from __future__ import annotations


def test_accounts_from_config_normalizes_and_dedupes() -> None:
    from scripts.account_config import accounts_from_config

    assert accounts_from_config({"accounts": [" LX ", "sy", "lx", ""]}) == ["lx", "sy"]


def test_accounts_from_config_keeps_legacy_fallback() -> None:
    from scripts.account_config import accounts_from_config

    assert accounts_from_config({}) == ["user1"]


def test_cash_footer_accounts_prefers_notification_override_then_accounts() -> None:
    from scripts.account_config import cash_footer_accounts_from_config

    assert cash_footer_accounts_from_config({"accounts": ["alpha"]}) == ["alpha"]
    assert cash_footer_accounts_from_config(
        {
            "accounts": ["alpha"],
            "notifications": {"cash_footer_accounts": ["beta", "gamma"]},
        }
    ) == ["beta", "gamma"]


def test_resolve_portfolio_source_prefers_account_override_then_global_then_auto() -> None:
    from scripts.account_config import resolve_portfolio_source

    cfg = {
        "portfolio": {
            "source": "holdings",
            "source_by_account": {
                "lx": "futu",
                "sy": "auto",
            },
        }
    }

    assert resolve_portfolio_source(cfg, account="LX") == "futu"
    assert resolve_portfolio_source(cfg, account="sy") == "auto"
    assert resolve_portfolio_source(cfg, account="unknown") == "holdings"
    assert resolve_portfolio_source({}, account="lx") == "auto"


def test_resolve_account_type_uses_account_settings_then_legacy_holdings_override() -> None:
    from scripts.account_config import resolve_account_type

    cfg = {
        "accounts": ["user1", "ext1", "ext2"],
        "account_settings": {
            "user1": {"type": "futu"},
            "ext1": {"type": "external_holdings", "holdings_account": "feishu-ext1"},
        },
        "portfolio": {
            "source_by_account": {
                "ext2": "holdings",
            }
        },
    }

    assert resolve_account_type(cfg, account="user1") == "futu"
    assert resolve_account_type(cfg, account="ext1") == "external_holdings"
    assert resolve_account_type(cfg, account="ext2") == "external_holdings"


def test_resolve_holdings_account_uses_explicit_mapping_then_account_label() -> None:
    from scripts.account_config import resolve_holdings_account

    cfg = {
        "accounts": ["user1", "ext1"],
        "account_settings": {
            "user1": {"type": "futu", "holdings_account": "LX"},
            "ext1": {"type": "external_holdings", "holdings_account": "Feishu EXT"},
        },
    }

    assert resolve_holdings_account(cfg, account="ext1") == "Feishu EXT"
    assert resolve_holdings_account(cfg, account="user1") == "LX"


def test_resolve_portfolio_source_keeps_futu_auto_with_holdings_fallback() -> None:
    from scripts.account_config import resolve_portfolio_source

    cfg = {
        "accounts": ["lx"],
        "account_settings": {
            "lx": {"type": "futu", "holdings_account": "lx"},
        },
        "portfolio": {
            "source": "auto",
            "source_by_account": {"lx": "auto"},
        },
    }

    assert resolve_portfolio_source(cfg, account="lx") == "auto"


def test_build_account_portfolio_source_plan_for_auto_futu_account() -> None:
    from scripts.account_config import build_account_portfolio_source_plan

    cfg = {
        "accounts": ["lx"],
        "account_settings": {
            "lx": {"type": "futu", "holdings_account": "LX"},
        },
        "portfolio": {
            "source": "auto",
        },
    }

    out = build_account_portfolio_source_plan(cfg, account="lx")
    assert out.account_type == "futu"
    assert out.requested_source == "auto"
    assert out.primary_source == "futu"
    assert out.fallback_source == "holdings"
    assert out.holdings_account == "LX"


def test_build_account_portfolio_source_plan_for_external_holdings_account() -> None:
    from scripts.account_config import build_account_portfolio_source_plan

    cfg = {
        "accounts": ["ext1"],
        "account_settings": {
            "ext1": {"type": "external_holdings", "holdings_account": "Feishu EXT"},
        },
        "portfolio": {
            "source": "futu",
        },
    }

    out = build_account_portfolio_source_plan(cfg, account="ext1")
    assert out.account_type == "external_holdings"
    assert out.requested_source == "holdings"
    assert out.primary_source == "holdings"
    assert out.fallback_source is None
    assert out.holdings_account == "Feishu EXT"


def test_parse_option_message_accepts_configured_account_labels() -> None:
    from scripts.parse_option_message import parse_account

    assert parse_account("成交 accountA账户", accounts=["accountA"]) == "accounta"
    assert parse_account("成交 lx", accounts=["accountA"]) is None
