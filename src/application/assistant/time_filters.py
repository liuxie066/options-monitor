from __future__ import annotations

import re
from datetime import date


_MONTH_RE = re.compile(r"(?<!\d)(20\d{2})[-/.](0[1-9]|1[0-2])(?!\d)")
_YEAR_MONTH_CN_RE = re.compile(r"(?<!\d)(20\d{2})年(1[0-2]|0?[1-9]|十[一二]?|[一二三四五六七八九])月")
_MONTH_CN_RE = re.compile(r"(?<!\d)(1[0-2]|0?[1-9]|十[一二]?|[一二三四五六七八九])月")
_CN_MONTHS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "十一": 11,
    "十二": 12,
}


def extract_month_filter(text: str, *, today: date) -> str | None:
    compact = re.sub(r"\s+", "", str(text or "").strip().lower())
    match = _MONTH_RE.search(str(text or ""))
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    year_month_cn = _YEAR_MONTH_CN_RE.search(compact)
    if year_month_cn:
        month = _month_number(year_month_cn.group(2))
        if month:
            return f"{int(year_month_cn.group(1)):04d}-{month:02d}"
    if "本月" in compact or "这个月" in compact:
        return today.strftime("%Y-%m")
    if "上月" in compact or "上个月" in compact:
        year = today.year
        month = today.month - 1
        if month == 0:
            year -= 1
            month = 12
        return f"{year:04d}-{month:02d}"
    month_cn = _MONTH_CN_RE.search(compact)
    if month_cn:
        month = _month_number(month_cn.group(1))
        if month:
            return f"{today.year:04d}-{month:02d}"
    return None


def _month_number(raw: str) -> int | None:
    if raw.isdigit():
        value = int(raw)
    else:
        value = _CN_MONTHS.get(raw)
    return value if value is not None and 1 <= value <= 12 else None
