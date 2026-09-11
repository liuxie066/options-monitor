from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from src.application.ledger.api import read_wheel_activation_windows_read_only
from src.application.wheel.config import (
    evaluate_wheel_activation_readiness,
    resolve_wheel_activation_descriptor,
)


WHEEL_ACTIVATION_READINESS_SCHEMA = "wheel_activation_readiness.v1"
_WHEEL_WINDOW_FIELDS = (
    "market",
    "account",
    "generation",
    "activated_at_ms",
    "deactivated_at_ms",
    "policy_hash",
)


def _wheel_window_identity(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    out = {key: value.get(key) for key in _WHEEL_WINDOW_FIELDS}
    if not out.get("policy_hash"):
        out["policy_hash"] = value.get("policy_sha256")
    return out


def _read_latest_wheel_activation_windows(
    sqlite_path: str | Path | None,
    *,
    market: str,
    accounts: Sequence[str],
) -> tuple[dict[str, dict[str, Any]], str]:
    normalized_accounts = tuple(
        dict.fromkeys(
            str(item).strip().lower()
            for item in accounts
            if str(item).strip()
        )
    )
    if not normalized_accounts:
        return {}, "available"
    latest: dict[str, dict[str, Any]] = {}
    source_status = "available"
    for account in normalized_accounts:
        result = read_wheel_activation_windows_read_only(sqlite_path, market, account)
        status = str(result["source_status"])
        if status != "available":
            source_status = status
            continue
        windows = result["windows"]
        if windows:
            latest[account] = dict(windows[-1])
    return latest, source_status


def build_wheel_activation_readiness(
    *,
    config: Mapping[str, Any],
    market: str | None,
    accounts: Sequence[str],
    sqlite_path: str | Path | None,
) -> dict[str, Any]:
    """Compare static Wheel descriptors with durable state using read-only SQLite."""

    normalized_market = str(market or "").strip().lower()
    if normalized_market not in {"us", "hk"}:
        normalized_market = ""
    requested_accounts = tuple(
        dict.fromkeys(
            str(item).strip().lower()
            for item in accounts
            if str(item).strip()
        )
    )
    raw_wheel = config.get("wheel")
    raw_wheel_accounts = (
        raw_wheel.get("accounts") if isinstance(raw_wheel, Mapping) else []
    )
    if isinstance(raw_wheel_accounts, list):
        wheel_accounts = {
            str(item).strip().lower()
            for item in raw_wheel_accounts
            if str(item).strip()
        }
        normalized_accounts = tuple(
            account for account in requested_accounts if account in wheel_accounts
        )
    else:
        normalized_accounts = requested_accounts
    windows, storage_status = (
        ({}, "not_required")
        if not normalized_accounts
        else _read_latest_wheel_activation_windows(
            sqlite_path,
            market=normalized_market,
            accounts=normalized_accounts,
        )
        if normalized_market
        else ({}, "market_unavailable")
    )
    account_results: dict[str, dict[str, Any]] = {}
    for account in normalized_accounts:
        descriptor: dict[str, Any] | None = None
        durable_window = windows.get(account)
        descriptor_error = False
        if normalized_market:
            try:
                descriptor = resolve_wheel_activation_descriptor(
                    config,
                    market=normalized_market,
                    account=account,
                )
            except (TypeError, ValueError):
                descriptor_error = True
        if not normalized_market:
            readiness = {
                "ready": False,
                "enabled_for_new_lifecycle": False,
                "monitoring_gate": "disabled",
                "reason_code": "market_unavailable",
            }
        elif descriptor_error:
            readiness = {
                "ready": False,
                "enabled_for_new_lifecycle": False,
                "monitoring_gate": "config_mismatch",
                "reason_code": "descriptor_mismatch",
            }
        elif storage_status not in {"available", "not_required"}:
            readiness = {
                "ready": False,
                "enabled_for_new_lifecycle": False,
                "monitoring_gate": "disabled",
                "reason_code": storage_status,
            }
        else:
            readiness = evaluate_wheel_activation_readiness(
                descriptor,
                durable_window,
            )
        descriptor_identity = _wheel_window_identity(descriptor)
        durable_identity = _wheel_window_identity(durable_window)
        effective_identity = durable_identity or descriptor_identity or {
            "market": normalized_market or None,
            "account": account,
        }
        account_results[account] = {
            **effective_identity,
            **readiness,
            "identity_source": (
                "durable_window"
                if durable_identity is not None
                else "descriptor"
                if descriptor_identity is not None
                else "none"
            ),
            "descriptor": descriptor_identity,
            "durable_window": durable_identity,
        }

    gates = {str(item["monitoring_gate"]) for item in account_results.values()}
    if "config_mismatch" in gates:
        monitoring_gate = "config_mismatch"
    elif account_results and gates == {"enabled"}:
        monitoring_gate = "enabled"
    else:
        monitoring_gate = "disabled"
    reason_codes = sorted(
        {
            str(item["reason_code"])
            for item in account_results.values()
            if item.get("reason_code")
        }
    )
    if not account_results:
        reason_codes = ["not_configured"]
    enabled_account_count = sum(bool(item["ready"]) for item in account_results.values())
    return {
        "schema_version": WHEEL_ACTIVATION_READINESS_SCHEMA,
        "market": normalized_market or None,
        "monitoring_gate": monitoring_gate,
        "ready": bool(account_results) and enabled_account_count == len(account_results),
        "reason_code": (
            None
            if monitoring_gate == "enabled"
            else reason_codes[0]
            if len(reason_codes) == 1
            else "account_not_ready"
        ),
        "reason_codes": reason_codes,
        "storage_status": storage_status,
        "account_count": len(account_results),
        "enabled_account_count": enabled_account_count,
        "accounts": account_results,
    }
