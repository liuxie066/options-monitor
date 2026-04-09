from __future__ import annotations

from pathlib import Path
import sys
import unittest

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from scripts.cash_secured_utils import (
    cash_secured_symbol_by_ccy,
    cash_secured_symbol_cny,
    normalize_cash_secured_by_symbol_by_ccy,
    normalize_cash_secured_total_by_ccy,
    read_cash_secured_total_cny,
)


class TestCashSecuredUtils(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
