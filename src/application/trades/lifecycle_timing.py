from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from domain.domain.symbol_identity import symbol_market
from src.application.trades.close_reason_evidence import (
    MARKET_TIMEZONES,
    build_lifecycle_timing_policy,
)


INSTRUMENT_POLICY_SCHEMA = "lifecycle_instrument_policy.v1"
INSTRUMENT_POLICY_REGISTRY: dict[str, dict[str, Any]] = {
    "US:standard_equity_option": {
        "schema_version": INSTRUMENT_POLICY_SCHEMA,
        "policy_id": "us_standard_equity_option.v1",
        "market": "US",
        "contract_class": "standard_equity_option",
        "underlying_security_type": "equity",
        "settlement_style": "physical",
        "timezone": "America/New_York",
        "last_trade_session_close": "16:00:00",
    },
    "HK:standard_equity_option": {
        "schema_version": INSTRUMENT_POLICY_SCHEMA,
        "policy_id": "hk_standard_equity_option.v1",
        "market": "HK",
        "contract_class": "standard_equity_option",
        "underlying_security_type": "equity",
        "settlement_style": "physical",
        "timezone": "Asia/Hong_Kong",
        "last_trade_session_close": "16:00:00",
    },
}


def resolve_authoritative_contract_timing(
    *,
    market: str,
    expiration_ymd: str,
    contract_metadata: dict[str, Any],
) -> dict[str, Any]:
    """Resolve cutoff only from broker metadata or a versioned registry row."""

    market_value = str(market or "").strip().upper()
    metadata = dict(contract_metadata or {})
    settlement_style = str(
        metadata.get("settlement_style") or ""
    ).strip().lower()
    security_type = str(
        metadata.get("underlying_security_type") or ""
    ).strip().lower()
    if settlement_style != "physical":
        raise ValueError("physical settlement metadata is required")
    if security_type != "equity":
        raise ValueError("equity underlying metadata is required")

    cutoff_ms = _positive_int(metadata.get("last_trade_cutoff_ms"))
    cutoff_source = str(
        metadata.get("last_trade_cutoff_source") or ""
    ).strip().lower()
    if cutoff_ms is not None:
        if cutoff_source != "broker_contract_metadata":
            raise ValueError(
                "broker cutoff requires broker_contract_metadata source"
            )
        return {
            **metadata,
            "market": market_value,
            "last_trade_cutoff_ms": cutoff_ms,
            "last_trade_cutoff_source": cutoff_source,
        }

    contract_class = str(
        metadata.get("contract_class") or ""
    ).strip().lower()
    registry_key = f"{market_value}:{contract_class}"
    policy = INSTRUMENT_POLICY_REGISTRY.get(registry_key)
    if not isinstance(policy, dict):
        raise ValueError(
            "versioned instrument timing policy is unavailable"
        )
    if (
        str(policy.get("settlement_style") or "").strip().lower()
        != settlement_style
        or str(
            policy.get("underlying_security_type") or ""
        ).strip().lower()
        != security_type
        or str(policy.get("timezone") or "")
        != MARKET_TIMEZONES.get(market_value)
    ):
        raise ValueError(
            "contract metadata conflicts with instrument timing policy"
        )
    expiration = date.fromisoformat(
        str(expiration_ymd or "").strip()
    )
    session_close = time.fromisoformat(
        str(policy["last_trade_session_close"])
    )
    cutoff = datetime.combine(
        expiration,
        session_close,
        tzinfo=ZoneInfo(str(policy["timezone"])),
    )
    return {
        **metadata,
        "market": market_value,
        "last_trade_cutoff_ms": int(cutoff.timestamp() * 1000),
        "last_trade_cutoff_source": "instrument_policy_registry",
        "instrument_policy_id": str(policy["policy_id"]),
    }


def bind_lifecycle_timing_policy(
    repo: Any,
    *,
    lifecycle_case: dict[str, Any],
    contract_metadata: dict[str, Any],
    trading_days: Iterable[dict[str, Any] | str],
    calendar_source: str,
    calendar_observed_at_ms: int,
    apply_changes: bool,
) -> dict[str, Any]:
    case_id = str(lifecycle_case.get("case_id") or "").strip()
    market = str(
        lifecycle_case.get("market")
        or symbol_market(lifecycle_case.get("symbol"))
        or ""
    ).strip().upper()
    expiration_ymd = str(
        lifecycle_case.get("expiration_ymd") or ""
    ).strip()
    resolved_metadata = resolve_authoritative_contract_timing(
        market=market,
        expiration_ymd=expiration_ymd,
        contract_metadata=contract_metadata,
    )
    policy = build_lifecycle_timing_policy(
        case_id=case_id,
        market=market,
        expiration_ymd=expiration_ymd,
        contract_metadata=resolved_metadata,
        trading_days=trading_days,
        calendar_source=calendar_source,
        calendar_observed_at_ms=calendar_observed_at_ms,
    )
    existing = repo.get_trade_lifecycle_timing_policy(case_id)
    if existing is not None and dict(existing) != policy:
        raise ValueError(
            f"lifecycle timing policy immutable conflict for case_id={case_id}"
        )
    created = False
    if apply_changes:
        created = bool(
            repo.insert_trade_lifecycle_timing_policy_once(policy)
        )
    return {
        "schema_version": "lifecycle_timing_binding_result.v1",
        "case_id": case_id,
        "apply_changes": bool(apply_changes),
        "created": created,
        "existing": existing is not None,
        "policy": policy,
    }


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


__all__ = [
    "INSTRUMENT_POLICY_REGISTRY",
    "INSTRUMENT_POLICY_SCHEMA",
    "bind_lifecycle_timing_policy",
    "resolve_authoritative_contract_timing",
]
