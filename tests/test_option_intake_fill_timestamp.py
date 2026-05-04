"""Tests for fill-timestamp parsing and propagation in option_intake pipeline.

Covers:
- HK message with explicit timestamp is parsed and returned as fill_time_ms
- Missing timestamp falls back to None (caller supplies now() fallback)
- Timezone conversion: (香港) -> Asia/Hong_Kong (UTC+8)
- Timezone conversion: (美国) -> America/New_York
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scripts.parse_option_message import parse_fill_timestamp, parse_option_message_text


# ---------------------------------------------------------------------------
# Unit tests for parse_fill_timestamp
# ---------------------------------------------------------------------------

def test_hk_timestamp_parsed_to_utc_ms() -> None:
    """(香港) is UTC+8, so 10:52:11 local -> 02:52:11 UTC."""
    s = "2026/05/04 10:52:11 (香港)"
    ms = parse_fill_timestamp(s)
    assert ms is not None
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    assert dt.year == 2026
    assert dt.month == 5
    assert dt.day == 4
    assert dt.hour == 2       # 10:52:11 HKT = 02:52:11 UTC
    assert dt.minute == 52
    assert dt.second == 11


def test_hk_timestamp_returns_correct_ms_value() -> None:
    """Round-trip: check the exact UTC millisecond value for a known HK time."""
    s = "2026/04/09 13:10:25 (香港)"
    ms = parse_fill_timestamp(s)
    assert ms is not None
    # 2026-04-09 13:10:25 HKT (UTC+8) = 2026-04-09 05:10:25 UTC
    expected_utc = datetime(2026, 4, 9, 5, 10, 25, tzinfo=timezone.utc)
    assert ms == int(expected_utc.timestamp() * 1000)


def test_us_timestamp_parsed_to_utc_ms() -> None:
    """(美国) -> America/New_York. In April, NY is EDT = UTC-4.
    15:30:00 NY local = 19:30:00 UTC.
    """
    s = "2026/04/26 15:30:00 (美国)"
    ms = parse_fill_timestamp(s)
    assert ms is not None
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    # America/New_York in April 2026 is EDT (UTC-4)
    assert dt.year == 2026
    assert dt.month == 4
    assert dt.day == 26
    assert dt.hour == 19   # 15:30 EDT = 19:30 UTC
    assert dt.minute == 30
    assert dt.second == 0


def test_missing_timestamp_returns_none() -> None:
    s = "【成交提醒】成功卖出2张$腾讯 260429 480.00 沽$，成交价格：3.93，此笔订单委托已全部成交。"
    ms = parse_fill_timestamp(s)
    assert ms is None


def test_timestamp_embedded_in_full_message() -> None:
    """Timestamp parsed correctly when embedded inside a longer Futu message."""
    s = (
        "【成交提醒】成功卖出2张$腾讯 260528 510.00 购$，成交价格：4.1，"
        "此笔订单委托已全部成交，2026/05/04 10:52:11 (香港)。【富途证券(香港)】"
    )
    ms = parse_fill_timestamp(s)
    assert ms is not None
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    assert dt.hour == 2
    assert dt.minute == 52
    assert dt.second == 11


def test_unknown_tz_hint_treated_as_utc() -> None:
    """Unknown timezone hints fall back to UTC (no crash)."""
    s = "2026/05/04 10:52:11 (未知市场)"
    ms = parse_fill_timestamp(s)
    assert ms is not None
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    # Treated as UTC, so local == UTC
    assert dt.hour == 10
    assert dt.minute == 52


# ---------------------------------------------------------------------------
# Integration tests: parse_option_message_text includes fill_time_ms
# ---------------------------------------------------------------------------

def test_parse_option_message_includes_fill_time_ms_for_hk_message(monkeypatch) -> None:
    monkeypatch.setattr(
        "scripts.parse_option_message.resolve_multiplier_with_source",
        lambda **_kwargs: (100, "cache"),
    )
    msg = (
        "【成交提醒】成功卖出2张$腾讯 260528 510.00 购$，成交价格：4.1，"
        "此笔订单委托已全部成交，2026/05/04 10:52:11 (香港)。【富途证券(香港)】 lx"
    )
    out = parse_option_message_text(msg, accounts=["lx", "sy"])
    fill_time_ms = out["parsed"]["fill_time_ms"]
    assert fill_time_ms is not None
    dt = datetime.fromtimestamp(fill_time_ms / 1000, tz=timezone.utc)
    assert dt.year == 2026
    assert dt.month == 5
    assert dt.day == 4
    assert dt.hour == 2    # 10:52:11 HKT = 02:52:11 UTC
    assert dt.minute == 52


def test_parse_option_message_fill_time_ms_is_none_when_absent(monkeypatch) -> None:
    monkeypatch.setattr(
        "scripts.parse_option_message.resolve_multiplier_with_source",
        lambda **_kwargs: (100, "cache"),
    )
    msg = "期权：腾讯20260330 put，strike500，成本5.425每股，乘数100，short 10张，sy，HKD"
    out = parse_option_message_text(msg, accounts=["lx", "sy"])
    assert out["parsed"]["fill_time_ms"] is None
