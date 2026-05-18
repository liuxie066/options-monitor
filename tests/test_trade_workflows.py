from __future__ import annotations

from src.application.trades.normalizer import NormalizedTradeDeal
from src.application.trades import workflows


def test_preview_trade_open_keeps_optional_note_values_out_of_string_none() -> None:
    preview = workflows.preview_trade_open(
        NormalizedTradeDeal(
            broker="富途",
            futu_account_id="REAL_1",
            internal_account="lx",
            deal_id="deal-preview-1",
            order_id="order-1",
            symbol="NVDA",
            option_type="put",
            side="sell",
            position_effect="open",
            contracts=1,
            price=1.23,
            strike=100.0,
            multiplier=100,
            multiplier_source=None,
            expiration_ymd="2026-06-19",
            currency="USD",
            trade_time_ms=1000,
            raw_payload={},
        )
    )
    command = preview["command"]

    assert command.symbol == "NVDA"
    assert command.currency == "USD"
    assert command.strike == 100.0
    assert command.multiplier == 100
    assert command.expiration_ymd == "2026-06-19"
    assert "multiplier_source=" in str(command.note)
    assert "None" not in str(command.note)
