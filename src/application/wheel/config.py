from __future__ import annotations

from typing import Any, Mapping


WHEEL_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "accounts": [],
    "min_dte": 30,
    "max_dte": 45,
    "min_delta": 0.30,
    "min_annualized_net_premium_return": 0.10,
    "min_net_premium_cny": 50.0,
    "max_spread_ratio": 0.40,
    "min_iv_rv_ratio": 1.10,
    "min_iv_minus_rv": 0.05,
}


def resolve_wheel_config(config: Mapping[str, Any], account: str) -> dict[str, Any]:
    """Return the validated market Wheel policy for one account."""

    account_value = str(account or "").strip().lower()
    if not account_value:
        raise ValueError("Wheel config requires account")
    raw = config.get("wheel")
    if raw is None:
        raw = WHEEL_DEFAULTS
    if not isinstance(raw, Mapping):
        raise ValueError("wheel must be an object")
    resolved = {**WHEEL_DEFAULTS, **dict(raw)}
    accounts = [str(value or "").strip().lower() for value in resolved["accounts"]]
    resolved["accounts"] = accounts
    resolved["enabled_for_new_lifecycle"] = bool(resolved["enabled"] and account_value in accounts)
    resolved["account"] = account_value
    return resolved


__all__ = ["WHEEL_DEFAULTS", "resolve_wheel_config"]
