from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from src.application.quality.paths import default_quality_artifact_path
from src.infrastructure.quality.artifact_repository import QualityArtifactRepository


class QualityStatusReader(Protocol):
    def read_published(self) -> dict[str, Any] | None: ...


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
        service.read_published()
        if service is not None
        else QualityArtifactRepository(default_quality_artifact_path()).read()
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


__all__ = ["QualityGateBlocked", "assert_quality_allows", "quality_onboarded"]
