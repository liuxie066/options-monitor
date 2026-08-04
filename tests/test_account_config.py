from __future__ import annotations

import pytest


def test_accounts_from_config_normalizes_and_dedupes() -> None:
    from src.application.account_config import accounts_from_config

    assert accounts_from_config({"accounts": [" LX ", "sy", "lx"]}) == ["lx", "sy"]


def test_accounts_from_config_keeps_legacy_fallback() -> None:
    from src.application.account_config import accounts_from_config

    assert accounts_from_config({}) == ["user1"]


@pytest.mark.parametrize("accounts", [[], [""], [0], [False], None])
def test_accounts_from_config_rejects_explicit_empty_or_non_string_scope(accounts) -> None:
    from src.application.account_config import accounts_from_config

    with pytest.raises(ValueError, match="accounts"):
        accounts_from_config({"accounts": accounts})


@pytest.mark.parametrize("account", ["../lx", "/lx", "lx/sy", "lx.sy", "lx sy"])
def test_accounts_from_config_rejects_unsafe_labels(account: str) -> None:
    from src.application.account_config import accounts_from_config

    with pytest.raises(ValueError, match="account label"):
        accounts_from_config({"accounts": [account]})


@pytest.mark.parametrize("accounts", [["../lx"], "../lx"])
def test_config_validator_rejects_unsafe_account_label(accounts) -> None:
    from src.application.config_validator import validate_config

    with pytest.raises(SystemExit, match="accounts contains invalid label"):
        validate_config({"accounts": accounts})


def test_cash_footer_accounts_prefers_notification_override_then_accounts() -> None:
    from src.application.account_config import cash_footer_accounts_from_config

    assert cash_footer_accounts_from_config({"accounts": ["alpha"]}) == ["alpha"]
    assert cash_footer_accounts_from_config(
        {
            "accounts": ["alpha"],
            "notifications": {"cash_footer_accounts": ["beta", "gamma"]},
        }
    ) == ["beta", "gamma"]


def test_resolve_portfolio_source_prefers_account_override_then_global_then_auto() -> None:
    from src.application.account_config import resolve_portfolio_source

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
    from src.application.account_config import resolve_account_type

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
    from src.application.account_config import resolve_holdings_account

    cfg = {
        "accounts": ["user1", "ext1"],
        "account_settings": {
            "user1": {"type": "futu", "holdings_account": "LX"},
            "ext1": {"type": "external_holdings", "holdings_account": "Feishu EXT"},
        },
    }

    assert resolve_holdings_account(cfg, account="ext1") == "Feishu EXT"
    assert resolve_holdings_account(cfg, account="user1") == "LX"


def test_resolve_portfolio_source_keeps_auto_for_futu_account() -> None:
    from src.application.account_config import resolve_portfolio_source

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
    from src.application.account_config import build_account_portfolio_source_plan

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
    assert out.holdings_account == "LX"


def test_build_account_portfolio_source_plan_for_external_holdings_account() -> None:
    from src.application.account_config import build_account_portfolio_source_plan

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
    assert out.primary_source == "external_holdings"
    assert out.holdings_account == "Feishu EXT"


def test_build_account_config_view_exposes_futu_runtime_plan() -> None:
    from src.application.account_config import build_account_config_view

    cfg = {
        "accounts": ["lx"],
        "account_settings": {
            "lx": {
                "type": "futu",
                "trade_intake_enabled": False,
                "futu": {
                    "account_id": "281756479859383816",
                    "host": "127.0.0.1",
                    "port": "11111",
                    "telnet_port": "22222",
                    "opend_root": "/home/liuxie/apps/futu-opend-lx/current",
                    "trd_env": "REAL",
                },
            }
        },
        "trade_intake": {
            "account_mapping": {
                "futu": {
                    "legacy-id": "lx",
                }
            }
        },
    }

    out = build_account_config_view(cfg, account="lx")

    assert out.futu_acc_ids == ["281756479859383816"]
    assert out.runtime_plan.portfolio_source == "futu"
    assert out.runtime_plan.trade_source == "api"
    assert out.runtime_plan.trade_intake_enabled is False
    assert out.runtime_plan.futu_account_id == "281756479859383816"
    assert out.runtime_plan.futu_host == "127.0.0.1"
    assert out.runtime_plan.futu_port == 11111
    assert out.runtime_plan.futu_telnet_port == 22222
    assert out.runtime_plan.futu_opend_root == "/home/liuxie/apps/futu-opend-lx/current"
    assert out.runtime_plan.futu_trd_env == "REAL"


def test_build_account_runtime_plan_does_not_truncate_non_integer_futu_ports() -> None:
    from src.application.account_config import build_account_runtime_plan

    for value in (11111.9, True, "11111.0", " 11111"):
        cfg = {
            "accounts": ["lx"],
            "account_settings": {
                "lx": {
                    "type": "futu",
                    "futu": {
                        "host": "127.0.0.1",
                        "port": value,
                    },
                }
            },
        }

        assert build_account_runtime_plan(cfg, account="lx").futu_port is None


def test_resolve_futu_account_ids_falls_back_to_legacy_trade_mapping() -> None:
    from src.application.account_config import resolve_futu_account_ids

    cfg = {
        "accounts": ["lx"],
        "trade_intake": {
            "account_mapping": {
                "futu": {
                    "legacy-id": "lx",
                }
            }
        },
    }

    assert resolve_futu_account_ids(cfg, account="lx") == ["legacy-id"]


def test_parse_option_message_accepts_configured_account_labels() -> None:
    from src.application.parse_option_message import parse_account

    assert parse_account("成交 accountA账户", accounts=["accountA"]) == "accounta"
    assert parse_account("成交 lx", accounts=["accountA"]) is None
