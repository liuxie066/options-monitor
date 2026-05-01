from __future__ import annotations

from datetime import date, datetime
from typing import Any

from domain.domain.expiration_dates import expiration_timestamp_to_ymd


def normalize_expiration_ymd(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if len(raw) >= 10 and raw[4:5] == "-" and raw[7:8] == "-":
        return raw[:10]

    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 8:
        try:
            return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
        except Exception:
            return None
    if len(digits) == 10:
        return expiration_timestamp_to_ymd(digits)
    if len(digits) == 13:
        return expiration_timestamp_to_ymd(digits)
    return None


def parse_expiration_ymd(value: Any) -> date | None:
    normalized = normalize_expiration_ymd(value)
    if not normalized:
        return None
    try:
        return datetime.strptime(normalized, "%Y-%m-%d").date()
    except Exception:
        return None


def find_unique_near_miss_expiration(
    requested: Any,
    available_expirations: list[Any] | set[Any] | tuple[Any, ...],
    *,
    max_delta_days: int = 1,
) -> str | None:
    requested_date = parse_expiration_ymd(requested)
    if requested_date is None:
        return None
    matches = sorted(
        {
            normalized
            for raw in (available_expirations or [])
            for normalized in [normalize_expiration_ymd(raw)]
            if normalized and (
                (candidate_date := parse_expiration_ymd(normalized)) is not None
                and abs((candidate_date - requested_date).days) <= int(max_delta_days)
            )
        }
    )
    if len(matches) != 1:
        return None
    return matches[0]
