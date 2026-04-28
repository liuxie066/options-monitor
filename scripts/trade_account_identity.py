from __future__ import annotations

from typing import Any


ACCOUNT_ID_KEYS = (
    "futu_account_id",
    "trd_acc_id",
    "acc_id",
    "account_id",
    "trade_acc_id",
    "account",
    "accID",
)


def norm_account_identity(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def extract_primary_account_id(src: dict[str, Any] | Any) -> str | None:
    if not isinstance(src, dict):
        return None
    for key in ACCOUNT_ID_KEYS:
        value = norm_account_identity(src.get(key))
        if value:
            return value
    return None


def extract_visible_account_fields(src: dict[str, Any] | Any) -> dict[str, str]:
    if not isinstance(src, dict):
        return {}
    out: dict[str, str] = {}
    for key in ACCOUNT_ID_KEYS:
        value = norm_account_identity(src.get(key))
        if value:
            out[key] = value
    return out
