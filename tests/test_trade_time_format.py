from __future__ import annotations

from datetime import datetime, timezone

from src.application.parse_option_message import parse_fill_timestamp
from src.application.trade_time_format import format_trade_time_beijing


def _ms_utc(year: int, month: int, day: int, hour: int, minute: int, second: int) -> int:
    return int(datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc).timestamp() * 1000)


def test_format_trade_time_beijing_keeps_hk_clock_time_after_parse() -> None:
    ms = parse_fill_timestamp("2026/05/19 13:08:31 (香港)")

    assert ms == _ms_utc(2026, 5, 19, 5, 8, 31)
    assert format_trade_time_beijing(ms) == "2026-05-19 13:08:31 北京时间"


def test_format_trade_time_beijing_converts_us_source_epoch() -> None:
    ms = parse_fill_timestamp("2026/04/26 15:30:00 (美国)")

    assert ms == _ms_utc(2026, 4, 26, 19, 30, 0)
    assert format_trade_time_beijing(ms) == "2026-04-27 03:30:00 北京时间"
