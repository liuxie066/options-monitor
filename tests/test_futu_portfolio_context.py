from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

FAKE_FUTU_ACC_ID_LX_PRIMARY = "123456789012345678"
FAKE_FUTU_ACC_ID_LX_SECONDARY = "123456789012345679"
FAKE_FUTU_ACC_ID_SY = "123456789012345680"


def test_resolve_trade_intake_futu_account_ids_uses_runtime_mapping() -> None:
    from scripts.futu_portfolio_context import resolve_trade_intake_futu_account_ids

    cfg = {
        "trade_intake": {
            "account_mapping": {
                "futu": {
                    FAKE_FUTU_ACC_ID_LX_PRIMARY: "lx",
                    FAKE_FUTU_ACC_ID_LX_SECONDARY: "lx",
                    FAKE_FUTU_ACC_ID_SY: "sy",
                }
            }
        }
    }

    assert resolve_trade_intake_futu_account_ids(cfg, account="lx") == [
        FAKE_FUTU_ACC_ID_LX_PRIMARY,
        FAKE_FUTU_ACC_ID_LX_SECONDARY,
    ]
    assert resolve_trade_intake_futu_account_ids(cfg, account="sy") == [FAKE_FUTU_ACC_ID_SY]
    assert resolve_trade_intake_futu_account_ids(cfg, account="zz") == []


def test_infer_futu_portfolio_settings_prefers_account_settings() -> None:
    from scripts.futu_portfolio_context import infer_futu_portfolio_settings

    cfg = {
        "portfolio": {"futu": {"host": "global-host", "port": 11111}},
        "account_settings": {
            "lx": {
                "futu": {"host": "lx-host", "port": 22222}
            }
        }
    }

    # 1. With account label, should prefer account_settings
    out = infer_futu_portfolio_settings(cfg, account="lx")
    assert out["host"] == "lx-host"
    assert out["port"] == 22222

    # 2. Without account label, should use global portfolio.futu
    out = infer_futu_portfolio_settings(cfg)
    assert out["host"] == "global-host"
    assert out["port"] == 11111

    # 3. Non-existent account label, should use global portfolio.futu
    out = infer_futu_portfolio_settings(cfg, account="unknown")
    assert out["host"] == "global-host"
    assert out["port"] == 11111


def test_infer_futu_portfolio_settings_falls_back_to_symbol_fetch_config() -> None:
    from scripts.futu_portfolio_context import infer_futu_portfolio_settings

    cfg = {
        "portfolio": {"source": "auto"},
        "symbols": [
            {"symbol": "NVDA", "fetch": {"source": "yahoo"}},
            {
                "symbol": "AAPL",
                "fetch": {
                    "source": "futu",
                    "host": "10.0.0.8",
                    "port": 22222,
                    "trd_env": "REAL",
                },
            },
        ],
    }

    out = infer_futu_portfolio_settings(cfg)
    assert out["host"] == "10.0.0.8"
    assert out["port"] == 22222
    assert out["trd_env"] == "REAL"


def test_build_futu_portfolio_context_merges_cash_and_fund_assets_and_normalizes_symbols() -> None:
    from scripts.futu_portfolio_context import build_futu_portfolio_context

    out = build_futu_portfolio_context(
        balance_rows=[
            {"currency": "rmb", "cash": 100000, "fund_assets": 25000},
            {"currency": "USD", "cash": 1000},
        ],
        position_rows=[
            {"code": "US.NVDA", "qty": 100, "cost_price": 120, "currency": "USD", "stock_name": "NVIDIA"},
            {"code": "HK.00700", "qty": 200, "cost_price": 380, "currency": "港币", "stock_name": "Tencent"},
        ],
        account=" LX ",
        market="富途",
        base_currency="CNY",
    )

    assert out["portfolio_source_name"] == "futu"
    assert out["filters"]["broker"] == "富途"
    assert "market" not in out["filters"]
    assert out["cash_by_currency"]["CNY"] == 125000.0
    assert out["cash_by_currency"]["USD"] == 1000.0
    assert out["stocks_by_symbol"]["NVDA"]["shares"] == 100
    assert out["stocks_by_symbol"]["0700.HK"]["shares"] == 200
    assert out["stocks_by_symbol"]["0700.HK"]["currency"] == "HKD"
    assert out["stocks_by_symbol"]["0700.HK"]["account"] == "lx"


def test_build_futu_portfolio_context_canonicalizes_alias_and_hk_prefixed_codes() -> None:
    from scripts.futu_portfolio_context import build_futu_portfolio_context

    out = build_futu_portfolio_context(
        balance_rows=[],
        position_rows=[
            {"code": "HK.09992", "qty": 100, "cost_price": 120, "currency": "HKD", "stock_name": "Pop Mart"},
            {"symbol": "POP", "qty": 50, "cost_price": 125, "currency": "HKD"},
        ],
        account="lx",
        market="富途",
        base_currency="CNY",
    )

    assert sorted(out["stocks_by_symbol"].keys()) == ["9992.HK"]
    assert out["stocks_by_symbol"]["9992.HK"]["shares"] == 150
    assert out["stocks_by_symbol"]["9992.HK"]["currency"] == "HKD"


def test_fetch_futu_portfolio_context_filters_rows_by_mapped_account_ids() -> None:
    import scripts.futu_portfolio_context as fc

    class _FakeGateway:
        balance_calls: list[int] = []
        position_calls: list[int] = []

        def get_account_balance(self, **kwargs):
            acc_id = kwargs.get("acc_id")
            assert isinstance(acc_id, int)
            self.balance_calls.append(acc_id)
            if acc_id == int(FAKE_FUTU_ACC_ID_LX_PRIMARY):
                return [
                    {"currency": "CNY", "cash": 100000, "fund_assets": 20000},
                ]
            if acc_id == int(FAKE_FUTU_ACC_ID_LX_SECONDARY):
                return [
                    {"currency": "CNY", "cash": 999999},
                ]
            return []

        def get_positions(self, **kwargs):
            acc_id = kwargs.get("acc_id")
            assert isinstance(acc_id, int)
            self.position_calls.append(acc_id)
            if acc_id == int(FAKE_FUTU_ACC_ID_LX_PRIMARY):
                return [
                    {"code": "US.NVDA", "qty": 100, "cost_price": 120, "currency": "USD"},
                ]
            if acc_id == int(FAKE_FUTU_ACC_ID_LX_SECONDARY):
                return [
                    {"code": "US.AAPL", "qty": 100, "cost_price": 180, "currency": "USD"},
                ]
            return []

        def close(self):
            return None

    old_build_gateway = fc.build_ready_futu_gateway
    fake_gateway = _FakeGateway()
    try:
        fc.build_ready_futu_gateway = lambda **_kwargs: fake_gateway  # type: ignore[assignment]
        out = fc.fetch_futu_portfolio_context(
            cfg={
                "portfolio": {"futu": {"host": "127.0.0.1", "port": 11111}},
                "trade_intake": {
                    "account_mapping": {
                        "futu": {
                            FAKE_FUTU_ACC_ID_LX_PRIMARY: "lx",
                            FAKE_FUTU_ACC_ID_LX_SECONDARY: "sy",
                        }
                    }
                },
            },
            account="lx",
            market="富途",
            base_currency="CNY",
        )
    finally:
        fc.build_ready_futu_gateway = old_build_gateway  # type: ignore[assignment]

    assert out["cash_by_currency"] == {"CNY": 120000.0}
    assert sorted(out["stocks_by_symbol"].keys()) == ["NVDA"]
    assert fake_gateway.balance_calls == [int(FAKE_FUTU_ACC_ID_LX_PRIMARY)]
    assert fake_gateway.position_calls == [int(FAKE_FUTU_ACC_ID_LX_PRIMARY)]


def test_fetch_futu_portfolio_context_rejects_non_numeric_mapped_account_id() -> None:
    import pytest

    import scripts.futu_portfolio_context as fc

    class _FakeGateway:
        def get_account_balance(self, **kwargs):
            return []

        def get_positions(self, **kwargs):
            return []

        def close(self):
            return None

    old_build_gateway = fc.build_ready_futu_gateway
    try:
        fc.build_ready_futu_gateway = lambda **_kwargs: _FakeGateway()  # type: ignore[assignment]
        with pytest.raises(ValueError, match="mapped account_id=not-a-number"):
            fc.fetch_futu_portfolio_context(
                cfg={
                    "portfolio": {"futu": {"host": "127.0.0.1", "port": 11111}},
                    "trade_intake": {
                        "account_mapping": {
                            "futu": {
                                "not-a-number": "lx",
                            }
                        }
                    },
                },
                account="lx",
                market="富途",
                base_currency="CNY",
            )
    finally:
        fc.build_ready_futu_gateway = old_build_gateway  # type: ignore[assignment]
