from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Protocol
from uuid import uuid4

from src.application.quality.paths import default_quality_artifact_path
from src.infrastructure.quality.artifact_repository import QualityArtifactRepository


class QualityStatusReader(Protocol):
    def read_published(
        self,
        *,
        consumer: str | None = None,
        account: str | None = None,
        market: str | None = None,
        lifecycle_rows_requested: bool = False,
    ) -> dict[str, Any] | None: ...


_TELEMETRY_LIMIT = 128
_TELEMETRY_MAX_COUNT = 2_147_483_647
_PROCESS_START_ID = f"{os.getpid()}:{uuid4().hex}"
_TELEMETRY_LOCK = Lock()
_TELEMETRY_COUNTS: Counter[
    tuple[str, str, str, str, bool, bool]
] = Counter()
_TELEMETRY_OVERFLOW_COUNT = 0


def record_quality_consumer_read(
    *,
    consumer: str | None,
    account: str | None,
    market: str | None,
    lifecycle_rows_requested: bool,
    lifecycle_rows_returned: bool,
    observed_at: datetime | None = None,
) -> None:
    global _TELEMETRY_OVERFLOW_COUNT

    instant = (observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    key = (
        str(consumer or "").strip() or "unexplained",
        str(account or "").strip().lower(),
        str(market or "").strip().lower(),
        instant.date().isoformat(),
        bool(lifecycle_rows_requested),
        bool(lifecycle_rows_returned),
    )
    with _TELEMETRY_LOCK:
        if key not in _TELEMETRY_COUNTS and len(_TELEMETRY_COUNTS) >= _TELEMETRY_LIMIT:
            _TELEMETRY_OVERFLOW_COUNT = min(
                _TELEMETRY_MAX_COUNT,
                _TELEMETRY_OVERFLOW_COUNT + 1,
            )
            return
        _TELEMETRY_COUNTS[key] = min(
            _TELEMETRY_MAX_COUNT,
            _TELEMETRY_COUNTS[key] + 1,
        )


def quality_consumer_telemetry_snapshot() -> dict[str, Any]:
    with _TELEMETRY_LOCK:
        overflow_count = _TELEMETRY_OVERFLOW_COUNT
        rows = [
            {
                "consumer": key[0],
                "account": key[1] or None,
                "market": key[2] or None,
                "market_date_utc": key[3],
                "legacy_rows_requested": key[4],
                "legacy_rows_returned": key[5],
                "count": count,
            }
            for key, count in sorted(_TELEMETRY_COUNTS.items())
        ]
    total = min(
        _TELEMETRY_MAX_COUNT,
        sum(item["count"] for item in rows) + overflow_count,
    )
    unexplained = min(
        _TELEMETRY_MAX_COUNT,
        overflow_count
        + sum(item["count"] for item in rows if item["consumer"] == "unexplained"),
    )
    return {
        "schema_version": "quality_consumer_telemetry.v1",
        "process_start_id": _PROCESS_START_ID,
        "coverage_status": (
            "unobserved" if total == 0 else "unexplained" if unexplained else "observed"
        ),
        "total_count": total,
        "unexplained_count": unexplained,
        "overflow_count": overflow_count,
        "entries": rows,
    }


@dataclass(frozen=True)
class QualityGateBlocked(RuntimeError):
    consumer: str
    reason_code: str
    blocked_by: tuple[str, ...]

    def __str__(self) -> str:
        return (
            f"{self.consumer} blocked by OM quality gate: {self.reason_code}"
            + (f" ({', '.join(self.blocked_by)})" if self.blocked_by else "")
        )


def quality_onboarded() -> bool:
    return str(os.environ.get("OM_QUALITY_ONBOARDED") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def assert_quality_allows(
    consumer: str,
    *,
    account: str | None = None,
    market: str | None = None,
    service: QualityStatusReader | None = None,
    max_age_seconds: int = 1800,
    now: datetime | None = None,
) -> None:
    if not quality_onboarded():
        return
    payload = (
        service.read_published(
            consumer=consumer,
            account=account,
            market=market,
            lifecycle_rows_requested=False,
        )
        if service is not None
        else QualityArtifactRepository(default_quality_artifact_path()).read()
    )
    if service is None:
        record_quality_consumer_read(
            consumer=consumer,
            account=account,
            market=market,
            lifecycle_rows_requested=False,
            lifecycle_rows_returned=quality_payload_has_lifecycle_rows(
                payload,
                account=account,
                market=market,
            ),
        )
    if not isinstance(payload, dict):
        raise QualityGateBlocked(consumer, "QUALITY_STATUS_UNAVAILABLE", ())
    observed_raw = str(payload.get("observed_at_utc") or "").strip()
    try:
        observed = datetime.fromisoformat(observed_raw.replace("Z", "+00:00"))
    except ValueError:
        raise QualityGateBlocked(consumer, "QUALITY_STATUS_TIME_INVALID", ()) from None
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if (current - observed.astimezone(timezone.utc)).total_seconds() > max_age_seconds:
        raise QualityGateBlocked(consumer, "QUALITY_STATUS_STALE", ())

    blockers: set[str] = set()
    reasons: set[str] = set()
    account_key = str(account or "").strip().lower()
    market_key = str(market or "").strip().lower()
    require_scoped_option_positions = bool(
        consumer == "close_advice" and account_key and market_key
    )
    scoped_option_position_count = 0
    scoped_option_position_declaration_count = 0
    cutover_active = (
        (((payload.get("extensions") or {}).get("quality_hot_path_cutover") or {}).get("status"))
        == "active"
    )
    for dataset in payload.get("datasets") or []:
        if not isinstance(dataset, dict):
            continue
        if (
            dataset.get("dataset_id") == "om.lifecycle_evidence_summary"
            and not cutover_active
        ):
            continue
        scope = dataset.get("scope") if isinstance(dataset.get("scope"), dict) else {}
        if (
            dataset.get("dataset_id") == "om.option_positions"
            and account_key
            and market_key
        ):
            if (
                str(scope.get("account") or "").strip().lower() != account_key
                or str(scope.get("market") or "").strip().lower() != market_key
            ):
                continue
            scoped_option_position_count += 1
        if account_key and scope.get("account") and str(scope.get("account")).lower() != account_key:
            continue
        if market_key and scope.get("market") and str(scope.get("market")).lower() != market_key:
            continue
        blocked_consumers = set(
            str(value) for value in dataset.get("blocked_consumers") or []
        )
        declared_consumers = blocked_consumers | set(
            str(value) for value in dataset.get("usable_for") or []
        )
        if (
            dataset.get("dataset_id") == "om.option_positions"
            and consumer in declared_consumers
            and account_key
            and market_key
        ):
            scoped_option_position_declaration_count += 1
        if consumer not in declared_consumers:
            continue
        if dataset.get("dataset_id") == "om.option_positions":
            freshness_value = (
                dataset.get("freshness")
                if isinstance(dataset.get("freshness"), dict)
                else {}
            )
            source_observed_raw = str(
                freshness_value.get("observed_at_utc") or ""
            ).strip()
            try:
                source_observed = datetime.fromisoformat(
                    source_observed_raw.replace("Z", "+00:00")
                )
            except ValueError:
                blockers.add("OM-POS-001")
                reasons.add("QUALITY_DATASET_TIME_INVALID")
            else:
                if (
                    current - source_observed.astimezone(timezone.utc)
                ).total_seconds() > max_age_seconds:
                    blockers.add("OM-POS-001")
                    reasons.add("QUALITY_DATASET_STALE")
        if consumer not in blocked_consumers:
            continue
        blockers.update(str(value) for value in dataset.get("blocked_by") or [] if str(value))
        reasons.update(str(value) for value in dataset.get("reason_codes") or [] if str(value))
    if require_scoped_option_positions:
        if scoped_option_position_count > 1:
            blockers.add("OM-POS-001")
            reasons.add("QUALITY_DATASET_AMBIGUOUS")
        elif (
            scoped_option_position_count != 1
            or scoped_option_position_declaration_count != 1
        ):
            blockers.add("OM-POS-001")
            reasons.add("QUALITY_DATASET_UNAVAILABLE")
    if blockers or reasons:
        raise QualityGateBlocked(
            consumer,
            sorted(reasons)[0] if reasons else "QUALITY_DEPENDENCY_BLOCKED",
            tuple(sorted(blockers)),
        )


def quality_payload_has_lifecycle_rows(
    payload: dict[str, Any] | None,
    *,
    account: str | None,
    market: str | None,
) -> bool:
    account_key = str(account or "").strip().lower()
    market_key = str(market or "").strip().lower()
    return any(
        isinstance(item, dict)
        and item.get("dataset_id")
        in {"om.lifecycle_evidence", "om.lifecycle_history"}
        and (
            not account_key
            or str((item.get("scope") or {}).get("account") or "").lower()
            == account_key
        )
        and (
            not market_key
            or str((item.get("scope") or {}).get("market") or "").lower()
            == market_key
        )
        for item in (payload or {}).get("datasets") or []
    )


__all__ = [
    "QualityGateBlocked",
    "assert_quality_allows",
    "quality_consumer_telemetry_snapshot",
    "quality_onboarded",
    "quality_payload_has_lifecycle_rows",
    "record_quality_consumer_read",
]
