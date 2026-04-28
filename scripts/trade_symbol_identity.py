from __future__ import annotations

import re
from typing import Any

from scripts.opend_utils import resolve_underlier_alias


OPTION_CODE_RE = re.compile(
    r"^(?P<market>[A-Z]{2})\.(?P<root>[A-Z]+)(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})(?P<cp>[CP])(?P<strike>\d+)$"
)


def normalize_symbol_candidate(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    upper = raw.upper()
    option_code_match = OPTION_CODE_RE.match(upper)
    if option_code_match:
        return resolve_underlier_alias(option_code_match.group("root")) or None
    if upper.startswith("US."):
        return resolve_underlier_alias(upper[3:]) or None
    if upper.startswith("HK."):
        digits = "".join(ch for ch in upper[3:] if ch.isdigit())
        if digits:
            return resolve_underlier_alias(f"{int(digits):04d}.HK") or None
        return None
    return resolve_underlier_alias(raw) or None


def pick_first_normalized_symbol(src: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = normalize_symbol_candidate(src.get(key))
        if value:
            return value
    return None
