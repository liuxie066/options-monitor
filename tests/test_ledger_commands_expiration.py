from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from src.application.ledger.commands import _broker_close_event_type


def test_zero_price_close_waits_for_new_york_expiration_date() -> None:
    def deal_at(day: int, hour: int) -> SimpleNamespace:
        trade_time = datetime(2026, 6, day, hour, tzinfo=ZoneInfo("America/New_York"))
        return SimpleNamespace(
            position_effect="close",
            price=0.0,
            expiration_ymd="2026-06-05",
            trade_time_ms=int(trade_time.timestamp() * 1000),
        )

    assert _broker_close_event_type(deal_at(4, 16)) == "close"
    assert _broker_close_event_type(deal_at(5, 0)) == "expire_close"
