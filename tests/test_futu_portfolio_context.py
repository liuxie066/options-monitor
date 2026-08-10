from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

FAKE_FUTU_ACC_ID_LX_PRIMARY = "123456789012345678"
FAKE_FUTU_ACC_ID_LX_SECONDARY = "123456789012345679"
FAKE_FUTU_ACC_ID_SY = "123456789012345680"


def test_resolve_trade_intake_futu_account_ids_uses_runtime_mapping() -> None:
    from src.application.account_config import resolve_trade_intake_futu_account_ids

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
    from src.application.futu_portfolio_context import infer_futu_portfolio_settings

    cfg = {
        "portfolio": {
            "futu": {
                "host": "global-host",
                "port": 11111,
                "trd_env": "REAL",
            }
        },
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
    assert out["trd_env"] == "REAL"

    # 2. Without account label, should use global portfolio.futu
    out = infer_futu_portfolio_settings(cfg)
    assert out["host"] == "global-host"
    assert out["port"] == 11111
    assert out["trd_env"] == "REAL"

    # 3. Non-existent account label, should use global portfolio.futu
    out = infer_futu_portfolio_settings(cfg, account="unknown")
    assert out["host"] == "global-host"
    assert out["port"] == 11111
    assert out["trd_env"] == "REAL"


def test_infer_futu_settings_merges_partial_account_override_per_key() -> None:
    from src.application.futu_portfolio_context import infer_futu_portfolio_settings

    cfg = {
        "portfolio": {
            "futu": {
                "host": "global-host",
                "port": 11111,
                "trd_env": "REAL",
            }
        },
        "account_settings": {
            "lx": {
                "futu": {
                    "trd_env": "SIMULATE",
                }
            }
        },
    }

    out = infer_futu_portfolio_settings(cfg, account="LX")

    assert out == {
        "host": "global-host",
        "port": 11111,
        "trd_env": "SIMULATE",
    }


def test_infer_futu_settings_rejects_unknown_environment() -> None:
    import pytest

    from src.application.futu_portfolio_context import infer_futu_portfolio_settings

    with pytest.raises(ValueError, match="invalid futu trd_env"):
        infer_futu_portfolio_settings(
            {
                "portfolio": {
                    "futu": {
                        "host": "global-host",
                        "port": 11111,
                        "trd_env": "SIMULATED",
                    }
                }
            }
        )


def test_infer_futu_portfolio_settings_falls_back_to_symbol_fetch_config() -> None:
    from src.application.futu_portfolio_context import infer_futu_portfolio_settings

    cfg = {
        "portfolio": {"source": "auto"},
        "symbols": [
            # Explicit non-Futu source example: this symbol should be ignored when
            # searching for Futu/OpenD connection settings.
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


def test_build_futu_portfolio_context_merges_explicit_cash_and_fund_assets_and_normalizes_symbols() -> None:
    from src.application.futu_portfolio_context import build_futu_portfolio_context

    out = build_futu_portfolio_context(
        balance_rows=[
            {"currency": "rmb", "cn_cash": 100000, "fund_assets": 25000},
            {"currency": "USD", "us_cash": 1000},
        ],
        position_rows=[
            {"code": "US.NVDA", "qty": 100, "can_sell_qty": 80, "average_cost": 120, "currency": "USD", "stock_name": "NVIDIA"},
            {"code": "HK.00700", "qty": 200, "can_sell_qty": 200, "average_cost": 380, "currency": "港币", "stock_name": "Tencent"},
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
    assert out["cash_source"] == "futu_cash_like_assets"
    assert out["cash_components_by_currency"]["CNY"] == {
        "fund_assets": 25000.0,
        "cn_cash": 100000.0,
    }
    assert out["stocks_by_symbol"]["NVDA"]["shares"] == 100
    assert out["stocks_by_symbol"]["NVDA"]["can_sell_qty"] == 80
    assert out["stocks_by_symbol"]["NVDA"]["eligible_underlying_shares"] == 80
    assert out["stocks_by_symbol"]["0700.HK"]["shares"] == 200
    assert out["stocks_by_symbol"]["0700.HK"]["currency"] == "HKD"
    assert out["stocks_by_symbol"]["0700.HK"]["account"] == "lx"


def test_build_futu_portfolio_context_canonicalizes_alias_and_hk_prefixed_codes() -> None:
    from src.application.futu_portfolio_context import build_futu_portfolio_context

    out = build_futu_portfolio_context(
        balance_rows=[],
        position_rows=[
            {"code": "HK.09992", "qty": 100, "can_sell_qty": 80, "average_cost": 120, "currency": "HKD", "stock_name": "Pop Mart"},
            {"symbol": "POP", "qty": 50, "can_sell_qty": 50, "average_cost": 125, "currency": "HKD"},
        ],
        account="lx",
        market="富途",
        base_currency="CNY",
    )

    assert sorted(out["stocks_by_symbol"].keys()) == ["9992.HK"]
    assert out["stocks_by_symbol"]["9992.HK"]["shares"] == 150
    assert out["stocks_by_symbol"]["9992.HK"]["can_sell_qty"] == 130
    assert out["stocks_by_symbol"]["9992.HK"]["currency"] == "HKD"


def test_build_futu_portfolio_context_maps_average_cost_not_diluted_cost() -> None:
    from src.application.futu_portfolio_context import build_futu_portfolio_context

    out = build_futu_portfolio_context(
        balance_rows=[],
        position_rows=[
            {
                "code": "HK.00883",
                "qty": 1000,
                "can_sell_qty": 1000,
                "average_cost": 18.153,
                "cost_price": -6.6,
                "diluted_cost": -6.6,
                "currency": "HKD",
            }
        ],
        account="sy",
        market="富途",
        base_currency="CNY",
    )

    stock = out["stocks_by_symbol"]["0883.HK"]
    assert stock["avg_cost"] == pytest.approx(18.153)
    assert stock["cost_basis_complete"] is True
    assert stock["cost_known_shares"] == 1000
    assert stock["cost_unknown_shares"] == 0


def test_build_futu_portfolio_context_does_not_treat_diluted_cost_as_average_cost() -> None:
    from src.application.futu_portfolio_context import build_futu_portfolio_context

    out = build_futu_portfolio_context(
        balance_rows=[],
        position_rows=[
            {
                "code": "HK.00883",
                "qty": 1000,
                "can_sell_qty": 1000,
                "cost_price": -6.6,
                "diluted_cost": -6.6,
                "currency": "HKD",
            }
        ],
        account="sy",
        market="富途",
        base_currency="CNY",
    )

    stock = out["stocks_by_symbol"]["0883.HK"]
    assert stock["avg_cost"] is None
    assert stock["cost_basis_complete"] is False
    assert stock["cost_known_shares"] == 0
    assert stock["cost_unknown_shares"] == 1000


def test_build_futu_portfolio_context_does_not_apply_partial_cost_basis_to_all_shares() -> None:
    from src.application.futu_portfolio_context import build_futu_portfolio_context

    out = build_futu_portfolio_context(
        balance_rows=[],
        position_rows=[
            {"code": "US.NVDA", "qty": 50, "average_cost": 100, "currency": "USD"},
            {"code": "US.NVDA", "qty": 50, "average_cost": None, "currency": "USD"},
        ],
        account="lx",
        market="富途",
        base_currency="CNY",
    )

    stock = out["stocks_by_symbol"]["NVDA"]
    assert stock["shares"] == 100
    assert stock["avg_cost"] is None
    assert stock["cost_basis_complete"] is False
    assert stock["cost_known_shares"] == 50
    assert stock["cost_unknown_shares"] == 50


def test_build_futu_portfolio_context_fails_sellability_closed_when_one_row_is_unknown() -> None:
    from src.application.futu_portfolio_context import build_futu_portfolio_context

    out = build_futu_portfolio_context(
        balance_rows=[],
        position_rows=[
            {"code": "US.NVDA", "qty": 50, "can_sell_qty": 50, "currency": "USD"},
            {"code": "US.NVDA", "qty": 50, "currency": "USD"},
        ],
        account="lx",
    )

    stock = out["stocks_by_symbol"]["NVDA"]
    assert stock["shares"] == 100
    assert stock["can_sell_qty"] is None
    assert stock["eligible_underlying_shares"] is None


def test_mixed_ordinary_and_assigned_shares_remain_unallocated_for_wheel_return() -> None:
    from src.application.futu_portfolio_context import build_futu_portfolio_context

    out = build_futu_portfolio_context(
        balance_rows=[],
        position_rows=[
            {
                "code": "US.NVDA",
                "qty": 100,
                "can_sell_qty": 100,
                "average_cost": 90,
                "currency": "USD",
                "holding_origin": "ordinary",
            },
            {
                "code": "US.NVDA",
                "qty": 100,
                "can_sell_qty": 100,
                "average_cost": 110,
                "currency": "USD",
                "holding_origin": "sell_put_assignment",
            },
        ],
        account="lx",
    )

    stock = out["stocks_by_symbol"]["NVDA"]
    assert stock["shares"] == 200
    assert stock["eligible_underlying_shares"] == 200
    assert stock["coverage_allocation_status"] == "unallocated"
    assert stock["stock_lot_id"] is None
    assert stock["wheel_batch_return_status"] == "not_calculated_unallocated"


def test_build_futu_portfolio_context_binds_capacity_to_physical_account() -> None:
    from src.application.futu_portfolio_context import build_futu_portfolio_context

    common = {
        "balance_rows": [{"currency": "USD", "us_cash": 10_000}],
        "position_rows": [
            {"code": "US.NVDA", "qty": 100, "can_sell_qty": 100, "currency": "USD"}
        ],
        "account": "lx",
        "source_observed_at": "2026-08-06T01:02:03+00:00",
        "trd_env": "REAL",
        "capacity_market": "us",
    }
    primary = build_futu_portfolio_context(
        **common,
        broker_account_identifiers=[FAKE_FUTU_ACC_ID_LX_PRIMARY],
        futu_account_id=FAKE_FUTU_ACC_ID_LX_PRIMARY,
    )
    secondary = build_futu_portfolio_context(
        **common,
        broker_account_identifiers=[FAKE_FUTU_ACC_ID_LX_SECONDARY],
        futu_account_id=FAKE_FUTU_ACC_ID_LX_SECONDARY,
    )

    assert primary["capacity_authority"]["status"] == "available"
    assert primary["capacity_authority"]["logical_account"] == "lx"
    assert primary["capacity_authority"]["trd_env"] == "REAL"
    assert primary["capacity_authority"]["market"] == "us"
    assert primary["capacity_identity_hash"] != secondary["capacity_identity_hash"]
    assert primary["cash_capacity_by_currency"]["USD"]["pool_additive_across_candidates"] is False


def test_build_opend_exchange_rate_observation_uses_account_funds_conversion() -> None:
    from src.application.futu_portfolio_context import build_opend_exchange_rate_observation
    from src.infrastructure.exchange_rates import exchange_rate_observation_status

    observed_at = datetime.now(timezone.utc).isoformat()
    observation = build_opend_exchange_rate_observation(
        {
            "CNH": [{"currency": "CNH", "total_assets": 7200.0}],
            "USD": [{"currency": "USD", "total_assets": 1000.0}],
            "HKD": [{"currency": "HKD", "total_assets": 7800.0}],
        },
        observed_at_utc=observed_at,
    )

    assert observation is not None
    assert observation["rates"]["USDCNY"] == pytest.approx(7.2)
    assert observation["rates"]["HKDCNY"] == pytest.approx(7200.0 / 7800.0)
    assert observation["source"] == "opend_account_funds_conversion"
    assert observation["timestamp"] == observed_at
    assert observation["value_basis"] == "total_assets"
    assert exchange_rate_observation_status(observation, max_age_hours=24) == "ready"


def test_build_opend_exchange_rate_observation_fails_closed_without_nonzero_basis() -> None:
    from src.application.futu_portfolio_context import build_opend_exchange_rate_observation

    observation = build_opend_exchange_rate_observation(
        {
            "CNH": [{"currency": "CNH", "total_assets": 0.0}],
            "USD": [{"currency": "USD", "total_assets": 1000.0}],
            "HKD": [{"currency": "HKD", "total_assets": 7800.0}],
        }
    )

    assert observation is None


def test_build_opend_exchange_rate_observation_fails_closed_when_currency_is_ignored() -> None:
    from src.application.futu_portfolio_context import build_opend_exchange_rate_observation

    observation = build_opend_exchange_rate_observation(
        {
            "CNH": [{"currency": "CNH", "total_assets": 7200.0}],
            "USD": [{"currency": "CNH", "total_assets": 7200.0}],
            "HKD": [{"currency": "CNH", "total_assets": 7200.0}],
        }
    )

    assert observation is None


def test_fetch_futu_portfolio_context_filters_rows_by_mapped_account_ids() -> None:
    import src.application.futu_portfolio_context as fc

    class _FakeGateway:
        balance_calls: list[int] = []
        position_calls: list[int] = []

        def get_account_balance(self, **kwargs):
            acc_id = kwargs.get("acc_id")
            assert isinstance(acc_id, int)
            self.balance_calls.append(acc_id)
            if acc_id == int(FAKE_FUTU_ACC_ID_LX_PRIMARY):
                return [
                    {"currency": "CNY", "cn_cash": 100000, "fund_assets": 20000},
                ]
            if acc_id == int(FAKE_FUTU_ACC_ID_LX_SECONDARY):
                return [
                    {"currency": "CNY", "cn_cash": 999999},
                ]
            return []

        def get_positions(self, **kwargs):
            acc_id = kwargs.get("acc_id")
            assert isinstance(acc_id, int)
            self.position_calls.append(acc_id)
            if acc_id == int(FAKE_FUTU_ACC_ID_LX_PRIMARY):
                return [
                    {"code": "US.NVDA", "qty": 100, "average_cost": 120, "currency": "USD"},
                ]
            if acc_id == int(FAKE_FUTU_ACC_ID_LX_SECONDARY):
                return [
                    {"code": "US.AAPL", "qty": 100, "average_cost": 180, "currency": "USD"},
                ]
            return []

        def close(self):
            return None

    old_build_gateway = fc.build_ready_futu_broker_gateway
    fake_gateway = _FakeGateway()
    try:
        fc.build_ready_futu_broker_gateway = lambda **_kwargs: fake_gateway  # type: ignore[assignment]
        out = fc.fetch_futu_portfolio_context(
            cfg={
                "portfolio": {
                    "futu": {
                        "host": "127.0.0.1",
                        "port": 11111,
                        "trd_env": "REAL",
                    }
                },
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
        fc.build_ready_futu_broker_gateway = old_build_gateway  # type: ignore[assignment]

    assert out["cash_by_currency"] == {"CNY": 120000.0}
    assert sorted(out["stocks_by_symbol"].keys()) == ["NVDA"]
    assert fake_gateway.balance_calls == [int(FAKE_FUTU_ACC_ID_LX_PRIMARY)] * 3
    assert fake_gateway.position_calls == [int(FAKE_FUTU_ACC_ID_LX_PRIMARY)]


def test_fetch_futu_portfolio_context_rejects_multiple_physical_accounts() -> None:
    import src.application.futu_portfolio_context as fc

    with pytest.raises(ValueError, match="exactly one physical account_id"):
        fc.fetch_futu_portfolio_context(
            cfg={
                "portfolio": {
                    "futu": {
                        "host": "127.0.0.1",
                        "port": 11111,
                        "trd_env": "REAL",
                    }
                },
                "trade_intake": {
                    "account_mapping": {
                        "futu": {
                            FAKE_FUTU_ACC_ID_LX_PRIMARY: "lx",
                            FAKE_FUTU_ACC_ID_LX_SECONDARY: "lx",
                        }
                    }
                },
            },
            account="lx",
        )


def test_fetch_futu_portfolio_context_uses_account_settings_account_id_without_trade_mapping() -> None:
    import src.application.futu_portfolio_context as fc

    captured: dict[str, list] = {"balance": [], "positions": []}

    class _FakeGateway:
        def get_account_balance(self, **kwargs):
            captured["balance"].append(dict(kwargs))
            return [{"currency": "USD", "us_cash": 2500}]

        def get_positions(self, **kwargs):
            captured["positions"].append(dict(kwargs))
            return [{"code": "US.NVDA", "qty": 10, "average_cost": 120, "currency": "USD"}]

        def close(self):
            return None

    old_build_gateway = fc.build_ready_futu_broker_gateway
    try:
        fc.build_ready_futu_broker_gateway = lambda **_kwargs: _FakeGateway()  # type: ignore[assignment]
        out = fc.fetch_futu_portfolio_context(
            cfg={
                "accounts": ["lx"],
                "account_settings": {
                    "lx": {
                        "type": "futu",
                        "futu": {
                            "account_id": FAKE_FUTU_ACC_ID_LX_PRIMARY,
                            "host": "127.0.0.1",
                            "port": 11111,
                            "trd_env": "REAL",
                        },
                    }
                },
            },
            account="lx",
        )
    finally:
        fc.build_ready_futu_broker_gateway = old_build_gateway  # type: ignore[assignment]

    assert captured["balance"] == [
        {
            "currency": currency,
            "acc_id": int(FAKE_FUTU_ACC_ID_LX_PRIMARY),
            "trd_env": "REAL",
        }
        for currency in ("CNH", "USD", "HKD")
    ]
    assert captured["positions"] == [{"acc_id": int(FAKE_FUTU_ACC_ID_LX_PRIMARY), "trd_env": "REAL"}]
    assert out["cash_by_currency"] == {"USD": 2500.0}
    assert sorted(out["stocks_by_symbol"].keys()) == ["NVDA"]


def test_fetch_futu_portfolio_context_rejects_non_numeric_mapped_account_id() -> None:
    import pytest

    import src.application.futu_portfolio_context as fc

    class _FakeGateway:
        def get_account_balance(self, **kwargs):
            return []

        def get_positions(self, **kwargs):
            return []

        def close(self):
            return None

    old_build_gateway = fc.build_ready_futu_broker_gateway
    try:
        fc.build_ready_futu_broker_gateway = lambda **_kwargs: _FakeGateway()  # type: ignore[assignment]
        with pytest.raises(ValueError, match="mapped account_id=not-a-number"):
            fc.fetch_futu_portfolio_context(
                cfg={
                    "portfolio": {
                        "futu": {
                            "host": "127.0.0.1",
                            "port": 11111,
                            "trd_env": "REAL",
                        }
                    },
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
        fc.build_ready_futu_broker_gateway = old_build_gateway  # type: ignore[assignment]


def test_build_futu_portfolio_context_excludes_short_positions_and_options() -> None:
    from src.application.futu_portfolio_context import build_futu_portfolio_context

    out = build_futu_portfolio_context(
        balance_rows=[],
        position_rows=[
            {"code": "US.NVDA", "qty": 100, "average_cost": 120, "currency": "USD", "position_side": "LONG", "sec_type": "STOCK"},
            {"code": "US.AAPL", "qty": 100, "average_cost": 180, "currency": "USD", "position_side": "SHORT", "sec_type": "STOCK"},
            {"code": "US.TSLA", "qty": 50, "average_cost": 200, "currency": "USD", "sec_type": "DRVT"},
            {"code": "US.AAPL250117C00175000", "qty": 1, "cost_price": 5, "currency": "USD"},
            {"code": "US.PDD", "qty": 1, "cost_price": 1.2, "currency": "USD", "stock_name": "PDD 260626 91.00C"},
            {"symbol": "PDD", "qty": 1, "cost_price": 1.2, "currency": "USD", "name": "PDD 260626 91.00C"},
        ],
        account="lx",
        market="富途",
        base_currency="USD",
    )

    assert sorted(out["stocks_by_symbol"].keys()) == ["NVDA"]
    assert out["stocks_by_symbol"]["NVDA"]["shares"] == 100


def test_build_futu_portfolio_context_ignores_legacy_balance_aliases_and_cash() -> None:
    from src.application.futu_portfolio_context import build_futu_portfolio_context

    out = build_futu_portfolio_context(
        balance_rows=[
            {"currency": "USD", "available_funds": 9999, "withdraw_cash": 8888, "power": 7777},
            {"currency": "USD", "cash": 100},
        ],
        position_rows=[],
        account="lx",
    )

    assert out["cash_by_currency"] == {}
    assert out["cash_components_by_currency"] == {}
    assert out["cash_source"] == "empty"


def test_build_futu_portfolio_context_prefers_explicit_futu_cash_fields_over_legacy_cash() -> None:
    from src.application.futu_portfolio_context import build_futu_portfolio_context

    out = build_futu_portfolio_context(
        balance_rows=[
            {
                "currency": "HKD",
                "cash": 999999,
                "fund_assets": 567440.6,
                "hk_cash": 0,
                "us_cash": -0.01,
                "hkd_net_cash_power": 76587.61,
                "usd_net_cash_power": 59021.91,
            },
        ],
        position_rows=[],
        account="lx",
    )

    assert out["cash_by_currency"] == {"HKD": 567440.6, "USD": -0.01}
    assert out["cash_components_by_currency"] == {
        "HKD": {"fund_assets": 567440.6, "hk_cash": 0.0},
        "USD": {"us_cash": -0.01},
    }
    assert out["cash_power_by_currency"] == {"HKD": 76587.61, "USD": 59021.91}
    assert out["cash_source"] == "futu_cash_like_assets"


def test_build_futu_portfolio_context_dedups_balance_rows_by_acc_env_currency() -> None:
    from src.application.futu_portfolio_context import build_futu_portfolio_context

    out = build_futu_portfolio_context(
        balance_rows=[
            {"acc_id": "1", "trd_env": "REAL", "currency": "USD", "us_cash": 1000},
            {"acc_id": "1", "trd_env": "REAL", "currency": "USD", "us_cash": 1000},
            {"acc_id": "1", "trd_env": "REAL", "currency": "HKD", "hk_cash": 500},
        ],
        position_rows=[],
        account="lx",
    )

    assert out["cash_by_currency"] == {"USD": 1000.0, "HKD": 500.0}


def test_filter_rows_for_account_ids_rejects_wrong_env_even_without_acc_id() -> None:
    from src.application.futu_portfolio_context import _filter_rows_for_account_ids

    rows = [
        {"trd_env": "SIMULATE", "currency": "USD", "cash": 100},
        {"acc_id": FAKE_FUTU_ACC_ID_LX_PRIMARY, "trd_env": "SIMULATE", "currency": "USD", "cash": 999},
        {"acc_id": FAKE_FUTU_ACC_ID_LX_PRIMARY, "trd_env": "REAL", "currency": "USD", "cash": 1000},
        {"acc_id": FAKE_FUTU_ACC_ID_SY, "trd_env": "REAL", "currency": "USD", "cash": 2000},
    ]

    out = _filter_rows_for_account_ids(rows, {FAKE_FUTU_ACC_ID_LX_PRIMARY}, trd_env="REAL")

    assert out == [
        {"acc_id": FAKE_FUTU_ACC_ID_LX_PRIMARY, "trd_env": "REAL", "currency": "USD", "cash": 1000},
    ]


def test_fetch_futu_portfolio_context_requires_explicit_environment() -> None:
    import pytest

    import src.application.futu_portfolio_context as fc

    with pytest.raises(ValueError, match="trd_env is required"):
        fc.fetch_futu_portfolio_context(
            cfg={
                "portfolio": {
                    "futu": {
                        "host": "127.0.0.1",
                        "port": 11111,
                    }
                },
                "account_settings": {
                    "lx": {
                        "futu": {
                            "account_id": FAKE_FUTU_ACC_ID_LX_PRIMARY,
                        }
                    }
                },
            },
            account="lx",
        )


def test_fetch_futu_portfolio_context_passes_trd_env_and_filters_simulate_rows() -> None:
    import src.application.futu_portfolio_context as fc

    captured: dict[str, list] = {"balance_kwargs": [], "position_kwargs": []}

    class _FakeGateway:
        def get_account_balance(self, **kwargs):
            captured["balance_kwargs"].append(dict(kwargs))
            return [
                {"acc_id": str(int(FAKE_FUTU_ACC_ID_LX_PRIMARY)), "trd_env": "REAL", "currency": "USD", "us_cash": 1000},
                {"acc_id": str(int(FAKE_FUTU_ACC_ID_LX_PRIMARY)), "trd_env": "SIMULATE", "currency": "USD", "us_cash": 9999},
            ]

        def get_positions(self, **kwargs):
            captured["position_kwargs"].append(dict(kwargs))
            return []

        def close(self):
            return None

    old_build_gateway = fc.build_ready_futu_broker_gateway
    try:
        fc.build_ready_futu_broker_gateway = lambda **_kwargs: _FakeGateway()  # type: ignore[assignment]
        out = fc.fetch_futu_portfolio_context(
            cfg={
                "account_settings": {
                    "lx": {"futu": {"host": "127.0.0.1", "port": 11111, "trd_env": "REAL"}},
                },
                "trade_intake": {
                    "account_mapping": {"futu": {FAKE_FUTU_ACC_ID_LX_PRIMARY: "lx"}}
                },
            },
            account="lx",
        )
    finally:
        fc.build_ready_futu_broker_gateway = old_build_gateway  # type: ignore[assignment]

    assert captured["balance_kwargs"][0].get("trd_env") == "REAL"
    assert captured["position_kwargs"][0].get("trd_env") == "REAL"
    assert out["cash_by_currency"] == {"USD": 1000.0}
