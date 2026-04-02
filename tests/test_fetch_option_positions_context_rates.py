from __future__ import annotations

from pathlib import Path
import sys

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))
if str(BASE / 'scripts') not in sys.path:
    sys.path.insert(0, str(BASE / 'scripts'))

from scripts.fetch_option_positions_context import build_context


def _record(symbol: str, side: str, option_type: str, currency: str, cash_secured: float) -> dict:
    return {
        "fields": {
            "market": "富途",
            "account": "lx",
            "symbol": symbol,
            "status": "open",
            "side": side,
            "option_type": option_type,
            "contracts": 1,
            "cash_secured_amount": cash_secured,
            "currency": currency,
        }
    }


def test_build_context_reads_nested_rates_payload() -> None:
    records = [_record('NVDA', 'short', 'put', 'USD', 120.0)]
    ctx = build_context(records, market="富途", account="lx", rates={"rates": {"USDCNY": "7.2", "HKDCNY": "7.8"}})

    assert ctx["cash_secured_by_symbol_by_ccy"]["NVDA"]["USD"] == 120.0
    assert ctx["cash_secured_total_by_ccy"]["USD"] == 120.0
    assert ctx["cash_secured_total_cny"] == 120.0 * 7.2


def test_build_context_reads_plain_rates_payload() -> None:
    records = [_record('700.HK', 'short', 'put', 'HKD', 100.0)]
    ctx = build_context(records, market="富途", account="lx", rates={"USDCNY": "7.2", "HKDCNY": "7.9"})

    assert ctx["cash_secured_by_symbol_by_ccy"]["700.HK"]["HKD"] == 100.0
    assert ctx["cash_secured_total_by_ccy"]["HKD"] == 100.0
    assert ctx["cash_secured_total_cny"] == 100.0 * 7.9
