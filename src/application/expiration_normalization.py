from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


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
        try:
            return datetime.fromtimestamp(int(digits), tz=timezone.utc).date().isoformat()
        except Exception:
            return None
    if len(digits) == 13:
        try:
            return datetime.fromtimestamp(int(digits) / 1000.0, tz=timezone.utc).date().isoformat()
        except Exception:
            return None
    return None
