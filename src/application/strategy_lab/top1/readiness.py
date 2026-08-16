from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any


READINESS_SCHEMA = "sell_put_top1_readiness.v1"
ADVANCE_SERVICE = "options-monitor-strategy-lab-top1-advance.service"
ADVANCE_TIMER = "options-monitor-strategy-lab-top1-advance.timer"
CAPABILITY_FACTS = (
    "account_fee_plan_receipt",
    "quote_observation_receipt",
    "exact_expiration_terms_receipt",
    "history_kline_quota_receipt",
    "exact_expiration_close_receipt",
)
_PROFILE_FIELDS = {
    "enabled",
    "market",
    "account",
    "opend_binding",
    "advance_interval",
    "timeout_start_sec",
}


def _object(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and set(value) <= set("0123456789abcdef")
    )


def _date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _calendar_ready(value: Mapping[str, Any] | None) -> bool:
    if value is None or value.get("market") != "HK":
        return False
    texts = (
        "market_calendar_version",
        "snapshot_ref",
    )
    if any(
        not isinstance(value.get(key), str) or not str(value[key]).strip()
        for key in texts
    ):
        return False
    if any(
        not _sha256(value.get(key))
        for key in (
            "snapshot_content_sha256",
            "snapshot_file_sha256",
            "source_receipt_sha256",
        )
    ):
        return False
    coverage_start = _date(value.get("coverage_start"))
    coverage_end = _date(value.get("coverage_end"))
    raw_dates = value.get("trading_dates")
    if not isinstance(raw_dates, list):
        return False
    trading_dates = [_date(item) for item in raw_dates]
    if coverage_start is None or coverage_end is None or any(
        item is None for item in trading_dates
    ):
        return False
    dates = [item for item in trading_dates if item is not None]
    return bool(
        coverage_start <= coverage_end
        and dates
        and dates == sorted(set(dates))
        and coverage_start <= dates[0] <= dates[-1] <= coverage_end
    )


def _service_check_ok(status: Mapping[str, Any], name: str, check: str) -> bool:
    services = status.get("services")
    if not isinstance(services, list):
        return False
    for raw in services:
        item = _object(raw)
        if item.get("name") != name:
            continue
        result = _object(item.get(check))
        return result.get("status") == "ok"
    return False


def build_top1_readiness(
    *,
    profile: Mapping[str, Any],
    drift: Mapping[str, Any],
    service_status: Mapping[str, Any],
    schema_state: Mapping[str, Any],
    feature_status: Mapping[str, Any] | None,
    corpus_status: Mapping[str, Any] | None,
    calendar_binding: Mapping[str, Any] | None,
    capability_facts: Mapping[str, bool] | None = None,
) -> dict[str, Any]:
    """Combine existing read-only facts; never probe providers or mutate services."""

    top1 = _object(profile.get("strategy_lab_top1"))
    binding = _object(top1.get("opend_binding"))
    profile_valid = (
        set(top1) == _PROFILE_FIELDS
        and top1.get("enabled") is True
        and top1.get("market") == "hk"
        and top1.get("account") == "lx"
        and isinstance(binding.get("host"), str)
        and bool(str(binding.get("host") or "").strip())
        and _positive_int(binding.get("port"))
        and int(binding["port"]) <= 65535
        and _positive_int(top1.get("advance_interval"))
        and _positive_int(top1.get("timeout_start_sec"))
    )
    service_items = (
        service_status.get("services")
        if isinstance(service_status.get("services"), list)
        else []
    )
    expected = set(drift.get("expected_services") or [])
    installed = set(drift.get("installed_units") or [])
    profile_services = set(drift.get("profile_services") or [])
    units_present = {ADVANCE_SERVICE, ADVANCE_TIMER}.issubset(
        expected & installed & profile_services
    )

    source_blockers: list[str] = []
    if not profile_valid:
        source_blockers.append("strategy_lab_top1_profile_invalid")
    if profile.get("service_provider") != "systemd":
        source_blockers.append("strategy_lab_top1_provider_unsupported")
    env_file = str(profile.get("env_file") or "").strip()
    if not env_file or not Path(env_file).expanduser().is_absolute():
        source_blockers.append("strategy_lab_top1_env_file_missing")
    if _object(drift.get("summary")).get("status") != "ok":
        source_blockers.append("strategy_lab_top1_service_drift")
    if not units_present:
        source_blockers.append("strategy_lab_top1_units_missing")
    if not _service_check_ok(service_status, ADVANCE_TIMER, "enabled"):
        source_blockers.append("strategy_lab_top1_timer_not_enabled")
    if not _service_check_ok(service_status, ADVANCE_TIMER, "active"):
        source_blockers.append("strategy_lab_top1_timer_not_active")

    source_ready = not source_blockers
    capabilities = {
        key: bool((capability_facts or {}).get(key) is True)
        for key in CAPABILITY_FACTS
    }
    runtime_blockers = list(source_blockers)
    if schema_state.get("status") != "ready":
        runtime_blockers.append("strategy_lab_top1_store_not_ready")
    if not feature_status or feature_status.get("effective") is not True:
        runtime_blockers.append("strategy_lab_top1_feature_disabled")
    if not _calendar_ready(calendar_binding):
        runtime_blockers.append("market_calendar_binding_unavailable")
    runtime_blockers.extend(
        f"{key}_missing" for key, ready in capabilities.items() if not ready
    )

    return {
        "schema_version": READINESS_SCHEMA,
        "market": "HK",
        "account": "lx",
        "source_delivery_ready": source_ready,
        "validation_runtime_ready": not runtime_blockers,
        "source_delivery_blockers": source_blockers,
        "validation_runtime_blockers": runtime_blockers,
        "facts": {
            "profile": top1,
            "service_drift_summary": _object(drift.get("summary")),
            "timer_status": next(
                (
                    _object(item)
                    for item in service_items
                    if _object(item).get("name") == ADVANCE_TIMER
                ),
                None,
            ),
            "store_schema": dict(schema_state),
            "feature": dict(feature_status) if feature_status is not None else None,
            "corpus": dict(corpus_status) if corpus_status is not None else None,
            "market_calendar": (
                {
                    key: calendar_binding.get(key)
                    for key in (
                        "market_calendar_version",
                        "coverage_start",
                        "coverage_end",
                        "snapshot_ref",
                        "snapshot_content_sha256",
                        "source_receipt_sha256",
                    )
                }
                if calendar_binding is not None
                else None
            ),
            "capabilities": capabilities,
        },
    }


__all__ = [
    "ADVANCE_SERVICE",
    "ADVANCE_TIMER",
    "CAPABILITY_FACTS",
    "READINESS_SCHEMA",
    "build_top1_readiness",
]
