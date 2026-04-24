from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

FAKE_FUTU_ACC_ID_LX = "123456789012345678"


def test_query_sell_put_cash_uses_futu_portfolio_context_when_runtime_config_allows_it() -> None:
    import scripts.query_sell_put_cash as m

    def fake_fetch_futu_portfolio_context(**_kwargs):  # type: ignore[no-untyped-def]
        return {
            "cash_by_currency": {"CNY": 130000.0, "USD": 1000.0},
            "stocks_by_symbol": {},
            "portfolio_source_name": "futu",
        }

    old_fetch = m.fetch_futu_portfolio_context
    old_load_repo = m.load_option_positions_repo
    old_load_option_position_records = m.load_option_position_records
    old_build_context = m.build_option_positions_context
    old_exchange_rates = m.get_exchange_rates_or_fetch_latest
    try:
        m.fetch_futu_portfolio_context = fake_fetch_futu_portfolio_context
        m.load_option_positions_repo = lambda *_a, **_k: object()  # type: ignore[assignment]
        m.load_option_position_records = lambda *_a, **_k: []  # type: ignore[assignment]
        m.build_option_positions_context = lambda *_a, **_k: {  # type: ignore[assignment]
            "cash_secured_by_symbol_by_ccy": {"NVDA": {"CNY": 72000.0}},
            "cash_secured_total_by_ccy": {"CNY": 72000.0},
            "cash_secured_total_cny": 72000.0,
        }
        m.get_exchange_rates_or_fetch_latest = lambda **_kwargs: {}  # type: ignore[assignment]

        out_dir = BASE / "output" / "state" / "test_query_sell_put_cash_futu"
        out_dir.mkdir(parents=True, exist_ok=True)
        result = m.query_sell_put_cash(
            config="config.us.json",
            market="富途",
            account="lx",
            out_dir=str(out_dir),
            base_dir=BASE,
            runtime_config={
                "portfolio": {"source": "auto", "base_currency": "CNY"},
                "trade_intake": {"account_mapping": {"futu": {FAKE_FUTU_ACC_ID_LX: "lx"}}},
            },
            no_exchange_rates=True,
        )
    finally:
        m.fetch_futu_portfolio_context = old_fetch
        m.load_option_positions_repo = old_load_repo  # type: ignore[assignment]
        m.load_option_position_records = old_load_option_position_records  # type: ignore[assignment]
        m.build_option_positions_context = old_build_context  # type: ignore[assignment]
        m.get_exchange_rates_or_fetch_latest = old_exchange_rates  # type: ignore[assignment]

    assert result["portfolio_source_name"] == "futu"
    assert result["cash_available_cny"] == 130000.0
    assert result["cash_free_cny"] == 58000.0


def test_query_sell_put_cash_uses_account_scoped_portfolio_source_override() -> None:
    import scripts.query_sell_put_cash as m

    def fake_fetch_futu_portfolio_context(**_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("futu portfolio context should not run for holdings override")

    def fake_load_account_portfolio_context(**kwargs):  # type: ignore[no-untyped-def]
        assert kwargs.get("account") == "sy"
        return {"cash_by_currency": {"CNY": 90000.0}, "stocks_by_symbol": {}, "portfolio_source_name": "holdings"}

    old_fetch = m.fetch_futu_portfolio_context
    old_load_portfolio = m.load_account_portfolio_context
    old_load_repo = m.load_option_positions_repo
    old_load_option_position_records = m.load_option_position_records
    old_build_context = m.build_option_positions_context
    old_exchange_rates = m.get_exchange_rates_or_fetch_latest
    try:
        m.fetch_futu_portfolio_context = fake_fetch_futu_portfolio_context
        m.load_account_portfolio_context = fake_load_account_portfolio_context
        m.load_option_positions_repo = lambda *_a, **_k: object()  # type: ignore[assignment]
        m.load_option_position_records = lambda *_a, **_k: []  # type: ignore[assignment]
        m.build_option_positions_context = lambda *_a, **_k: {  # type: ignore[assignment]
            "cash_secured_by_symbol_by_ccy": {"NVDA": {"CNY": 12000.0}},
            "cash_secured_total_by_ccy": {"CNY": 12000.0},
            "cash_secured_total_cny": 12000.0,
        }
        m.get_exchange_rates_or_fetch_latest = lambda **_kwargs: {}  # type: ignore[assignment]

        out_dir = BASE / "output" / "state" / "test_query_sell_put_cash_holdings_override"
        out_dir.mkdir(parents=True, exist_ok=True)
        result = m.query_sell_put_cash(
            config="config.us.json",
            market="富途",
            account="sy",
            out_dir=str(out_dir),
            base_dir=BASE,
            runtime_config={
                "portfolio": {
                    "source": "auto",
                    "source_by_account": {"sy": "holdings"},
                    "base_currency": "CNY",
                },
            },
            no_exchange_rates=True,
        )
    finally:
        m.fetch_futu_portfolio_context = old_fetch
        m.load_account_portfolio_context = old_load_portfolio
        m.load_option_positions_repo = old_load_repo  # type: ignore[assignment]
        m.load_option_position_records = old_load_option_position_records  # type: ignore[assignment]
        m.build_option_positions_context = old_build_context  # type: ignore[assignment]
        m.get_exchange_rates_or_fetch_latest = old_exchange_rates  # type: ignore[assignment]

    assert result["portfolio_source_name"] == "holdings"
    assert result["cash_available_cny"] == 90000.0
    assert result["cash_free_cny"] == 78000.0


def test_query_sell_put_cash_uses_holdings_account_mapping_for_external_account() -> None:
    import scripts.query_sell_put_cash as m

    def fake_load_account_portfolio_context(**kwargs):  # type: ignore[no-untyped-def]
        assert kwargs.get("account") == "ext1"
        return {"cash_by_currency": {"CNY": 50000.0}, "stocks_by_symbol": {}, "portfolio_source_name": "holdings"}

    old_load_portfolio = m.load_account_portfolio_context
    old_load_repo = m.load_option_positions_repo
    old_load_option_position_records = m.load_option_position_records
    old_build_context = m.build_option_positions_context
    old_exchange_rates = m.get_exchange_rates_or_fetch_latest
    try:
        m.load_account_portfolio_context = fake_load_account_portfolio_context
        m.load_option_positions_repo = lambda *_a, **_k: object()  # type: ignore[assignment]
        m.load_option_position_records = lambda *_a, **_k: []  # type: ignore[assignment]
        m.build_option_positions_context = lambda *_a, **_k: {  # type: ignore[assignment]
            "cash_secured_by_symbol_by_ccy": {"NVDA": {"CNY": 8000.0}},
            "cash_secured_total_by_ccy": {"CNY": 8000.0},
            "cash_secured_total_cny": 8000.0,
        }
        m.get_exchange_rates_or_fetch_latest = lambda **_kwargs: {}  # type: ignore[assignment]

        out_dir = BASE / "output" / "state" / "test_query_sell_put_cash_external_holdings"
        out_dir.mkdir(parents=True, exist_ok=True)
        result = m.query_sell_put_cash(
            config="config.us.json",
            market="富途",
            account="ext1",
            out_dir=str(out_dir),
            base_dir=BASE,
            runtime_config={
                "accounts": ["user1", "ext1"],
                "account_settings": {
                    "ext1": {"type": "external_holdings", "holdings_account": "Feishu EXT"},
                },
                "portfolio": {
                    "source": "auto",
                    "source_by_account": {"ext1": "holdings"},
                    "base_currency": "CNY",
                },
            },
            no_exchange_rates=True,
        )
    finally:
        m.load_account_portfolio_context = old_load_portfolio
        m.load_option_positions_repo = old_load_repo  # type: ignore[assignment]
        m.load_option_position_records = old_load_option_position_records  # type: ignore[assignment]
        m.build_option_positions_context = old_build_context  # type: ignore[assignment]
        m.get_exchange_rates_or_fetch_latest = old_exchange_rates  # type: ignore[assignment]

    assert result["portfolio_source_name"] == "holdings"
    assert result["cash_available_cny"] == 50000.0
    assert result["cash_free_cny"] == 42000.0
