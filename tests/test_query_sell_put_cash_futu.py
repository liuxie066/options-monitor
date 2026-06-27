from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

FAKE_FUTU_ACC_ID_LX = "123456789012345678"


def test_query_sell_put_cash_uses_futu_portfolio_context_when_runtime_config_allows_it() -> None:
    import src.application.cash_headroom_query as m

    def fake_fetch_futu_portfolio_context(**_kwargs):  # type: ignore[no-untyped-def]
        return {
            "cash_by_currency": {"CNY": 130000.0, "USD": 1000.0},
            "cash_components_by_currency": {
                "CNY": {"cn_cash": 130000.0},
                "USD": {"us_cash": 1000.0},
            },
            "cash_source": "futu_cash_like_assets",
            "cash_power_by_currency": {"CNY": 150000.0},
            "cash_power_source": "futu_net_cash_power",
            "stocks_by_symbol": {},
            "portfolio_source_name": "futu",
        }

    old_fetch = m.fetch_futu_portfolio_context
    old_open_position_ledger = m.open_position_ledger
    old_load_option_position_records = m._load_option_position_records
    old_build_context = m.build_option_positions_context
    old_exchange_rates = m.get_exchange_rates_or_fetch_latest
    try:
        m.fetch_futu_portfolio_context = fake_fetch_futu_portfolio_context
        m.open_position_ledger = lambda *_a, **_k: object()  # type: ignore[assignment]
        m._load_option_position_records = lambda *_a, **_k: []  # type: ignore[assignment]
        m.build_option_positions_context = lambda *_a, **_k: {  # type: ignore[assignment]
            "cash_secured_by_symbol_by_ccy": {"NVDA": {"CNY": 72000.0}},
            "cash_secured_total_by_ccy": {"CNY": 72000.0},
            "cash_secured_total_cny": 72000.0,
        }
        m.get_exchange_rates_or_fetch_latest = lambda **_kwargs: {}  # type: ignore[assignment]

        out_dir = BASE / "output_shared" / "state" / "test_query_sell_put_cash_futu"
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
        m.open_position_ledger = old_open_position_ledger  # type: ignore[assignment]
        m._load_option_position_records = old_load_option_position_records  # type: ignore[assignment]
        m.build_option_positions_context = old_build_context  # type: ignore[assignment]
        m.get_exchange_rates_or_fetch_latest = old_exchange_rates  # type: ignore[assignment]

    assert result["portfolio_source_name"] == "futu"
    assert result["cash_available_cny"] == 130000.0
    assert result["cash_free_cny"] == 58000.0
    assert result["cash_source"] == "futu_cash_like_assets"
    assert result["cash_components_by_currency"] == {
        "CNY": {"cn_cash": 130000.0},
        "USD": {"us_cash": 1000.0},
    }
    assert result["cash_power_by_currency"] == {"CNY": 150000.0}
    assert result["cash_power_total_cny"] == 150000.0
    assert result["cash_power_source"] == "futu_net_cash_power"


def test_query_sell_put_cash_can_run_without_writing_cache(tmp_path: Path) -> None:
    import src.application.cash_headroom_query as m

    out_dir = tmp_path / "cash_headroom_state"

    def fake_load_account_portfolio_context(**kwargs):  # type: ignore[no-untyped-def]
        assert kwargs.get("write_cache") is False
        assert not out_dir.exists()
        return {"cash_by_currency": {"CNY": 130000.0}, "stocks_by_symbol": {}, "portfolio_source_name": "holdings"}

    old_load_portfolio = m.load_account_portfolio_context
    old_open_position_ledger = m.open_position_ledger
    old_load_option_position_records = m._load_option_position_records
    old_build_context = m.build_option_positions_context
    try:
        m.load_account_portfolio_context = fake_load_account_portfolio_context
        m.open_position_ledger = lambda *_a, **_k: object()  # type: ignore[assignment]
        m._load_option_position_records = lambda *_a, **_k: []  # type: ignore[assignment]
        m.build_option_positions_context = lambda *_a, **_k: {  # type: ignore[assignment]
            "cash_secured_by_symbol_by_ccy": {"NVDA": {"CNY": 72000.0}},
            "cash_secured_total_by_ccy": {"CNY": 72000.0},
            "cash_secured_total_cny": 72000.0,
        }
        result = m.query_sell_put_cash(
            config="config.us.json",
            market="富途",
            account="lx",
            out_dir=str(out_dir),
            base_dir=BASE,
            runtime_config={"portfolio": {"source": "auto", "base_currency": "CNY"}},
            no_exchange_rates=True,
            write_cache=False,
        )
    finally:
        m.load_account_portfolio_context = old_load_portfolio
        m.open_position_ledger = old_open_position_ledger  # type: ignore[assignment]
        m._load_option_position_records = old_load_option_position_records  # type: ignore[assignment]
        m.build_option_positions_context = old_build_context  # type: ignore[assignment]

    assert result["cash_secured_used_cny"] == 72000.0
    assert result["cash_available_total_cny"] == 130000.0
    assert not out_dir.exists()


def test_query_sell_put_cash_uses_account_scoped_portfolio_source_override() -> None:
    import src.application.cash_headroom_query as m

    def fake_fetch_futu_portfolio_context(**_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("futu portfolio context should not run for holdings override")

    def fake_load_account_portfolio_context(**kwargs):  # type: ignore[no-untyped-def]
        assert kwargs.get("account") == "sy"
        return {"cash_by_currency": {"CNY": 90000.0}, "stocks_by_symbol": {}, "portfolio_source_name": "holdings"}

    old_fetch = m.fetch_futu_portfolio_context
    old_load_portfolio = m.load_account_portfolio_context
    old_open_position_ledger = m.open_position_ledger
    old_load_option_position_records = m._load_option_position_records
    old_build_context = m.build_option_positions_context
    old_exchange_rates = m.get_exchange_rates_or_fetch_latest
    try:
        m.fetch_futu_portfolio_context = fake_fetch_futu_portfolio_context
        m.load_account_portfolio_context = fake_load_account_portfolio_context
        m.open_position_ledger = lambda *_a, **_k: object()  # type: ignore[assignment]
        m._load_option_position_records = lambda *_a, **_k: []  # type: ignore[assignment]
        m.build_option_positions_context = lambda *_a, **_k: {  # type: ignore[assignment]
            "cash_secured_by_symbol_by_ccy": {"NVDA": {"CNY": 12000.0}},
            "cash_secured_total_by_ccy": {"CNY": 12000.0},
            "cash_secured_total_cny": 12000.0,
        }
        m.get_exchange_rates_or_fetch_latest = lambda **_kwargs: {}  # type: ignore[assignment]

        out_dir = BASE / "output_shared" / "state" / "test_query_sell_put_cash_holdings_override"
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
        m.open_position_ledger = old_open_position_ledger  # type: ignore[assignment]
        m._load_option_position_records = old_load_option_position_records  # type: ignore[assignment]
        m.build_option_positions_context = old_build_context  # type: ignore[assignment]
        m.get_exchange_rates_or_fetch_latest = old_exchange_rates  # type: ignore[assignment]

    assert result["portfolio_source_name"] == "holdings"
    assert result["cash_available_cny"] == 90000.0
    assert result["cash_free_cny"] == 78000.0


def test_query_sell_put_cash_uses_holdings_account_mapping_for_external_account() -> None:
    import src.application.cash_headroom_query as m

    def fake_load_account_portfolio_context(**kwargs):  # type: ignore[no-untyped-def]
        assert kwargs.get("account") == "ext1"
        return {"cash_by_currency": {"CNY": 50000.0}, "stocks_by_symbol": {}, "portfolio_source_name": "holdings"}

    old_load_portfolio = m.load_account_portfolio_context
    old_open_position_ledger = m.open_position_ledger
    old_load_option_position_records = m._load_option_position_records
    old_build_context = m.build_option_positions_context
    old_exchange_rates = m.get_exchange_rates_or_fetch_latest
    try:
        m.load_account_portfolio_context = fake_load_account_portfolio_context
        m.open_position_ledger = lambda *_a, **_k: object()  # type: ignore[assignment]
        m._load_option_position_records = lambda *_a, **_k: []  # type: ignore[assignment]
        m.build_option_positions_context = lambda *_a, **_k: {  # type: ignore[assignment]
            "cash_secured_by_symbol_by_ccy": {"NVDA": {"CNY": 8000.0}},
            "cash_secured_total_by_ccy": {"CNY": 8000.0},
            "cash_secured_total_cny": 8000.0,
        }
        m.get_exchange_rates_or_fetch_latest = lambda **_kwargs: {}  # type: ignore[assignment]

        out_dir = BASE / "output_shared" / "state" / "test_query_sell_put_cash_external_holdings"
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
        m.open_position_ledger = old_open_position_ledger  # type: ignore[assignment]
        m._load_option_position_records = old_load_option_position_records  # type: ignore[assignment]
        m.build_option_positions_context = old_build_context  # type: ignore[assignment]
        m.get_exchange_rates_or_fetch_latest = old_exchange_rates  # type: ignore[assignment]

    assert result["portfolio_source_name"] == "holdings"
    assert result["cash_available_cny"] == 50000.0
    assert result["cash_free_cny"] == 42000.0


def test_query_sell_put_cash_marks_free_cash_unknown_when_cash_secured_unavailable() -> None:
    import src.application.cash_headroom_query as m

    def fake_load_account_portfolio_context(**kwargs):  # type: ignore[no-untyped-def]
        assert kwargs.get("account") == "lx"
        return {"cash_by_currency": {"CNY": 130000.0}, "stocks_by_symbol": {}, "portfolio_source_name": "holdings"}

    old_load_portfolio = m.load_account_portfolio_context
    old_open_position_ledger = m.open_position_ledger
    old_load_option_position_records = m._load_option_position_records
    old_build_context = m.build_option_positions_context
    old_exchange_rates = m.get_exchange_rates_or_fetch_latest
    try:
        m.load_account_portfolio_context = fake_load_account_portfolio_context
        m.open_position_ledger = lambda *_a, **_k: object()  # type: ignore[assignment]
        m._load_option_position_records = lambda *_a, **_k: []  # type: ignore[assignment]
        m.build_option_positions_context = lambda *_a, **_k: {  # type: ignore[assignment]
            "cash_secured_by_symbol_by_ccy": {"NVDA": {"CNY": 12000.0}},
            "cash_secured_total_by_ccy": {"CNY": 12000.0},
            "cash_secured_total_cny": None,
            "cash_secured_unavailable_by_symbol": {
                "0700.HK": "short_put_cash_secured_basis_missing",
            },
        }
        m.get_exchange_rates_or_fetch_latest = lambda **_kwargs: {}  # type: ignore[assignment]

        out_dir = BASE / "output_shared" / "state" / "test_query_sell_put_cash_unavailable"
        out_dir.mkdir(parents=True, exist_ok=True)
        result = m.query_sell_put_cash(
            config="config.us.json",
            market="富途",
            account="lx",
            out_dir=str(out_dir),
            base_dir=BASE,
            runtime_config={
                "portfolio": {"source": "auto", "base_currency": "CNY"},
            },
            no_exchange_rates=True,
        )
    finally:
        m.load_account_portfolio_context = old_load_portfolio
        m.open_position_ledger = old_open_position_ledger  # type: ignore[assignment]
        m._load_option_position_records = old_load_option_position_records  # type: ignore[assignment]
        m.build_option_positions_context = old_build_context  # type: ignore[assignment]
        m.get_exchange_rates_or_fetch_latest = old_exchange_rates  # type: ignore[assignment]

    assert result["cash_secured_usage_reliable"] is False
    assert result["cash_secured_used_cny"] is None
    assert result["cash_free_cny"] is None
    assert result["cash_free_total_cny"] is None
    assert result["cash_secured_total_by_ccy"] == {}
    assert result["cash_secured_known_total_by_ccy"] == {"CNY": 12000.0}
    assert result["cash_secured_unavailable_reason"] == "0700.HK:short_put_cash_secured_basis_missing"
