"""Pure performance entry for caller-supplied, content-bound event snapshots."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from typing import Any

from domain.domain.ledger.cash_facts import OptionFeeCashEvent
from domain.domain.ledger.events import TradeEvent
from domain.domain.ledger.projection import project_trade_events
from domain.domain.option_position_identity import normalize_account, normalize_broker
from domain.domain.performance.cash_conversion import cash_conversion_timestamp_ms
from domain.domain.performance.period import PeriodWindow
from domain.domain.performance.weighted_reducer import (
    PERFORMANCE_PARTIAL_PROJECTION_CODES, PerformanceReduction, reduce_option_performance,
)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def cashflow_input_digest(item: TradeEvent | OptionFeeCashEvent) -> str:
    """Bind archive availability evidence to the exact canonical input payload."""
    if not isinstance(item, (TradeEvent, OptionFeeCashEvent)):
        raise ValueError("cashflow input must be a TradeEvent or OptionFeeCashEvent")
    return _digest(item.to_dict())


def _positive_ms(value: Any, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer timestamp")
    return value


def _observation_times(item: TradeEvent | OptionFeeCashEvent) -> list[int]:
    if isinstance(item, TradeEvent):
        raw = item.raw_payload
        candidates = [raw.get("fee_provenance")]
        conversions = raw.get("cash_conversions")
        if isinstance(conversions, Mapping):
            candidates.extend(conversions.values())
    else:
        candidates = [item.cash_conversion]
    times = []
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            for key in ("observed_at_ms", "provider_observed_at_ms", "rate_observed_at_ms"):
                if candidate.get(key) is not None:
                    times.append(_positive_ms(candidate[key], key))
            if candidate.get("rate_timestamp") is not None:
                times.append(_positive_ms(cash_conversion_timestamp_ms(candidate["rate_timestamp"]), "rate_timestamp"))
    return times


def calculate_option_performance(
    events: Iterable[TradeEvent],
    *,
    period: PeriodWindow,
    accounts: Iterable[str],
    broker: str | None = None,
    fee_cash_events: Iterable[OptionFeeCashEvent] = (),
    fee_mode: str = "actual",
    knowledge_cutoff_ms: int | None = None,
    input_evidence: Mapping[str, Mapping[str, Any]] | None = None,
) -> PerformanceReduction:
    """Calculate the same domain metrics as OM, without repository or clock reads.

    Point-in-time use requires archived availability evidence for every input.
    Hashes validate binding, not the archive's authenticity or completeness.
    Supply one version per identity; this function does not reconstruct history.
    """
    if isinstance(accounts, (str, bytes)):
        raise ValueError("accounts must be an explicit nonempty collection")
    account_values = tuple(accounts)
    if not account_values or any(not isinstance(v, str) or not v.strip() for v in account_values):
        raise ValueError("accounts must contain nonblank account names")
    scope = tuple(sorted({normalize_account(v) for v in account_values}))
    if broker is not None and (not isinstance(broker, str) or not broker.strip()):
        raise ValueError("broker must be a nonblank name")
    broker_scope = normalize_broker(broker) if broker is not None else None
    records: dict[str, TradeEvent | OptionFeeCashEvent] = {}
    for prefix, items, expected in (("trade", events, TradeEvent), ("fee", fee_cash_events, OptionFeeCashEvent)):
        for original in items:
            if not isinstance(original, expected):
                raise ValueError(f"{prefix} input has an invalid type")
            item = deepcopy(original)
            identity = item.event_id if isinstance(item, TradeEvent) else item.fee_id
            if not identity:
                raise ValueError(f"{prefix} identity is required")
            key = f"{prefix}:{identity}"
            if key in records and cashflow_input_digest(records[key]) != cashflow_input_digest(item):
                raise ValueError(f"conflicting input versions: {key}")
            records[key] = item
    evidence = deepcopy(input_evidence)
    if knowledge_cutoff_ms is None:
        if evidence is not None:
            raise ValueError("input_evidence requires knowledge_cutoff_ms")
        visibility = "corrected_snapshot"
    else:
        cutoff = _positive_ms(knowledge_cutoff_ms, "knowledge_cutoff_ms")
        if period.effective_end_exclusive_at_ms > cutoff + 1:
            raise ValueError("period extends beyond knowledge cutoff")
        if not isinstance(evidence, Mapping) or set(evidence) != set(records):
            raise ValueError("availability evidence is required for every input")
        for key, item in records.items():
            entry = evidence[key]
            if not isinstance(entry, Mapping) or set(entry) != {"available_at_ms", "source", "payload_sha256"}:
                raise ValueError(f"invalid availability evidence: {key}")
            available = _positive_ms(entry["available_at_ms"], "available_at_ms")
            if not isinstance(entry["source"], str) or not entry["source"].strip():
                raise ValueError(f"archive source is required: {key}")
            if entry["payload_sha256"] != cashflow_input_digest(item):
                raise ValueError(f"availability evidence payload mismatch: {key}")
            effective = item.event_time_ms if isinstance(item, TradeEvent) else item.effective_at_ms
            if not 0 < effective <= available <= cutoff or any(t > available for t in _observation_times(item)):
                raise ValueError(f"input was not available by the declared cutoff: {key}")
        visibility = "point_in_time_archive"
    ordered = {key: records[key].to_dict() for key in sorted(records)}
    projection = project_trade_events([item for key, item in sorted(records.items()) if key.startswith("trade:")])
    if knowledge_cutoff_ms is not None and any(
        not 0 < event.event_time_ms <= knowledge_cutoff_ms
        for event in projection.effective_cash_events
    ):
        raise ValueError("effective cash event was not available by the declared cutoff")
    # Offline ingestion rejects corrupt control graphs just as the live ledger boundary does.
    if any(item.severity == "error" and item.code not in PERFORMANCE_PARTIAL_PROJECTION_CODES and (
        not item.account or item.account in scope
    ) and (not broker_scope or not item.broker or normalize_broker(item.broker) == broker_scope)
           for item in projection.diagnostics):
        raise ValueError("invalid frozen ledger projection")
    result = reduce_option_performance(
        projection, period=period, accounts=scope, broker=broker_scope,
        fee_cash_events=tuple(item for key, item in sorted(records.items()) if key.startswith("fee:")),
        fee_mode=fee_mode,
    )
    return replace(result, bundle={**result.bundle, "cashflow_source": {
        "schema_version": "option_performance.inputs.v1",
        "snapshot_hash": _digest({"records": ordered, "input_evidence": evidence}),
        "visibility": visibility,
        "knowledge_cutoff_ms": knowledge_cutoff_ms,
        "input_count": len(records),
    }})


__all__ = ["calculate_option_performance", "cashflow_input_digest"]
