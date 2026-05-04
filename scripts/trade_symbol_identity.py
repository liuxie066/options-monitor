from __future__ import annotations

import re
from typing import Any

from scripts.opend_utils import resolve_underlier_alias


OPTION_CODE_RE = re.compile(
    r"^(?P<market>[A-Z]{2})\.(?P<root>[A-Z]+)(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})(?P<cp>[CP])(?P<strike>\d+)$"
)
_US_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9\.-]{0,10}$")


def _canonical_underlier(value: Any) -> str | None:
    resolved = resolve_underlier_alias(str(value or ""))
    upper = str(resolved or "").strip().upper()
    if not upper:
        return None
    if upper.endswith(".HK"):
        code = upper[:-3]
        if code.isdigit():
            return f"{int(code):04d}.HK"
        return None
    if _US_SYMBOL_RE.fullmatch(upper):
        return upper
    return None


def _display_name_candidates(raw: str) -> list[str]:
    out: list[str] = []
    text = str(raw or "").strip()
    if not text:
        return out

    token = re.split(r"[\s,，]+", text, maxsplit=1)[0].strip()
    if token:
        out.append(token)

    date_match = re.search(r"(?:20)?\d{6}", text)
    if date_match:
        prefix = text[: date_match.start()].strip(" -_/，,")
        if prefix:
            out.append(prefix)

    return list(dict.fromkeys(out))


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
            return _canonical_underlier(f"{int(digits):04d}.HK")
        return None
    exact = _canonical_underlier(raw)
    if exact:
        return exact
    for candidate in _display_name_candidates(raw):
        normalized = _canonical_underlier(candidate)
        if normalized:
            return normalized
    return None


def pick_first_normalized_symbol(src: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = normalize_symbol_candidate(src.get(key))
        if value:
            return value
    return None
