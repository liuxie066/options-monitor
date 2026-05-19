from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo


BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def _to_int_ms(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def format_trade_time_beijing(ms: Any) -> str | None:
    parsed = _to_int_ms(ms)
    if parsed is None:
        return None
    dt = datetime.fromtimestamp(parsed / 1000, tz=timezone.utc).astimezone(BEIJING_TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S 北京时间")


def add_trade_time_beijing(row: dict[str, Any], *, key: str = "trade_time_ms", field: str = "trade_time_beijing") -> dict[str, Any]:
    out = dict(row)
    formatted = format_trade_time_beijing(out.get(key))
    if formatted is not None:
        out[field] = formatted
    return out
