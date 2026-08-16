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
        lifecycle_rows_requested: bool = True,
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
            lifecycle_rows_requested=True,
        )
        if service is not None
        else QualityArtifactRepository(default_quality_artifact_path()).read()
    )
    if service is None:
        record_quality_consumer_read(
            consumer=consumer,
            account=account,
            market=market,
            lifecycle_rows_requested=True,
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
    for dataset in payload.get("datasets") or []:
        if not isinstance(dataset, dict):
            continue
        if dataset.get("dataset_id") == "om.lifecycle_evidence_summary":
            continue
        scope = dataset.get("scope") if isinstance(dataset.get("scope"), dict) else {}
        if account_key and scope.get("account") and str(scope.get("account")).lower() != account_key:
            continue
        if market_key and scope.get("market") and str(scope.get("market")).lower() != market_key:
            continue
        if consumer not in set(str(value) for value in dataset.get("blocked_consumers") or []):
            continue
        blockers.update(str(value) for value in dataset.get("blocked_by") or [] if str(value))
        reasons.update(str(value) for value in dataset.get("reason_codes") or [] if str(value))
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
