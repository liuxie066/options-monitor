from __future__ import annotations

import unittest


from domain.domain.cash_secured_utils import (
    cash_secured_unavailable_for_cash_snapshot,
    cash_secured_symbol_by_ccy,
    cash_secured_symbol_cny,
    normalize_cash_secured_by_symbol_by_ccy,
    normalize_cash_secured_total_by_ccy,
    read_cash_secured_total_cny,
)


class TestCashSecuredUtils(unittest.TestCase):
    def test_closed_put_is_not_released_by_newer_total_cash_snapshot(self) -> None:
        option_ctx = {
            "decision_snapshot_status": "trusted",
            "cash_secured_unavailable_by_symbol": {
                "0700.HK": "option_close_settlement_pending",
                "NVDA": "short_put_cash_secured_basis_missing",
            },
            "open_positions_min": [
                {
                    "lot_id": f"lot-{index}",
                    "symbol": "0700.HK",
                    "side": "short",
                    "option_type": "put",
                    "closure_fact": "option_leg_closed",
                    "reserved_contracts_by_lot": {f"lot-{index}": 1},
                    "first_option_close_received_at_ms": 1_790_682_868_000,
                    "last_option_close_received_at_ms": 1_790_682_868_000,
                }
                for index in range(4)
            ],
        }
        cash = {
            "context_source": "futu_direct",
            "source_observed_at": "2026-09-30T03:00:48+00:00",
            "cash_balance_reliable": True,
        }
        self.assertEqual(
            cash_secured_unavailable_for_cash_snapshot(option_ctx, cash),
            option_ctx["cash_secured_unavailable_by_symbol"],
        )
        self.assertEqual(
            cash_secured_unavailable_for_cash_snapshot(
                option_ctx, {**cash, "source_observed_at": "2026-09-29T10:00:00+00:00"}
            ),
            option_ctx["cash_secured_unavailable_by_symbol"],
        )
        option_ctx["open_positions_min"][0]["closure_fact"] = "partial_close_observed"
        self.assertEqual(
            cash_secured_unavailable_for_cash_snapshot(option_ctx, cash),
            option_ctx["cash_secured_unavailable_by_symbol"],
        )
        option_ctx["open_positions_min"][0]["closure_fact"] = "option_leg_closed"
        option_ctx["open_positions_min"][0]["last_option_close_received_at_ms"] = 1_800_000_000_000
        self.assertEqual(
            cash_secured_unavailable_for_cash_snapshot(option_ctx, cash),
            option_ctx["cash_secured_unavailable_by_symbol"],
        )

    def test_missing_decision_snapshot_blocks_cash_even_without_open_puts(self) -> None:
        self.assertEqual(
            cash_secured_unavailable_for_cash_snapshot(
                {"cash_secured_unavailable_by_symbol": {}},
                {"cash_by_currency": {"USD": 1000}},
            ),
            "option_decision_snapshot_unavailable",
        )

    def test_untrusted_decision_snapshot_blocks_cash_even_without_open_puts(self) -> None:
        option_ctx = {
            "decision_snapshot_status": "source_untrusted",
            "cash_secured_unavailable_by_symbol": {},
        }
        self.assertEqual(
            cash_secured_unavailable_for_cash_snapshot(option_ctx, {"cash_by_currency": {"USD": 1000}}),
            "option_decision_snapshot_unavailable",
        )

    def test_cash_secured_utils_only_by_ccy(self) -> None:
        ctx = {
            'cash_secured_by_symbol_by_ccy': {
                ' nvda ': {'usd': '1200', 'hkd': 1000},
            }
        }

        by_ccy = normalize_cash_secured_by_symbol_by_ccy(ctx)
        total_by_ccy = normalize_cash_secured_total_by_ccy(ctx, by_symbol_by_ccy=by_ccy)

        self.assertEqual(by_ccy, {'NVDA': {'USD': 1200.0, 'HKD': 1000.0}})
        self.assertEqual(total_by_ccy, {'USD': 1200.0, 'HKD': 1000.0})
        self.assertEqual(cash_secured_symbol_by_ccy(ctx, 'nvda', by_symbol_by_ccy=by_ccy), {'USD': 1200.0, 'HKD': 1000.0})

        cny = cash_secured_symbol_cny(
            ctx,
            'NVDA',
            by_symbol_by_ccy=by_ccy,
            native_to_cny=lambda amt, ccy: (
                float(amt)
                if ccy == 'CNY'
                else (float(amt) * 7.2 if ccy == 'USD' else (float(amt) * 0.92 if ccy == 'HKD' else None))
            ),
        )
        self.assertEqual(cny, (1200.0 * 7.2 + 1000.0 * 0.92))
        self.assertIsNone(read_cash_secured_total_cny(ctx))

    def test_cash_secured_utils_only_legacy_fields(self) -> None:
        ctx = {
            'cash_secured_by_symbol': {'tsla': '500.5', 'AAPL': 0},
            'cash_secured_by_symbol_cny': {'TSLA': '3600'},
            'cash_secured_total_cny': '9999',
        }

        by_ccy = normalize_cash_secured_by_symbol_by_ccy(ctx)
        total_by_ccy = normalize_cash_secured_total_by_ccy(ctx, by_symbol_by_ccy=by_ccy)

        self.assertEqual(by_ccy, {'TSLA': {'USD': 500.5}})
        self.assertEqual(total_by_ccy, {'USD': 500.5})
        self.assertEqual(cash_secured_symbol_by_ccy(ctx, 'tsla', by_symbol_by_ccy=by_ccy), {'USD': 500.5})
        self.assertEqual(cash_secured_symbol_cny(ctx, 'tsla', by_symbol_by_ccy=by_ccy), 3600.0)
        self.assertEqual(read_cash_secured_total_cny(ctx), 9999.0)

    def test_cash_secured_utils_matches_hk_symbol_aliases(self) -> None:
        ctx = {
            'cash_secured_by_symbol_by_ccy': {'00700.HK': {'hkd': '1000'}},
            'cash_secured_by_symbol_cny': {'00700.HK': '920'},
        }

        by_ccy = normalize_cash_secured_by_symbol_by_ccy(ctx)

        self.assertEqual(by_ccy, {'0700.HK': {'HKD': 1000.0}})
        self.assertEqual(cash_secured_symbol_by_ccy(ctx, '700', by_symbol_by_ccy=by_ccy), {'HKD': 1000.0})
        self.assertEqual(cash_secured_symbol_cny(ctx, '0700.HK', by_symbol_by_ccy=by_ccy), 920.0)

    def test_cash_secured_utils_uses_shared_currency_aliases(self) -> None:
        ctx = {
            'cash_secured_by_symbol_by_ccy': {'NVDA': {'rmb': '100', '港币': '200'}},
        }

        by_ccy = normalize_cash_secured_by_symbol_by_ccy(ctx)
        total_by_ccy = normalize_cash_secured_total_by_ccy(ctx, by_symbol_by_ccy=by_ccy)

        self.assertEqual(by_ccy, {'NVDA': {'CNY': 100.0, 'HKD': 200.0}})
        self.assertEqual(total_by_ccy, {'CNY': 100.0, 'HKD': 200.0})
