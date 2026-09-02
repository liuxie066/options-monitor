from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Mapping
from zoneinfo import ZoneInfo

REPORTING_TIMEZONE = "Asia/Shanghai"
_PERIOD_KINDS = frozenset({"mtd", "ytd"})


def _parse_date(value: Any, *, field_name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{field_name} is required")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be YYYY-MM-DD") from exc


def _local_midnight_ms(value: date, tz: ZoneInfo) -> int:
    return int(datetime.combine(value, time.min, tzinfo=tz).timestamp() * 1000)


@dataclass(frozen=True)
class PeriodRequest:
    period: str = "mtd"
    as_of_date: date | str | None = None
    month: str | None = None
    year: int | str | None = None
    start_date: date | str | None = None
    end_date: date | str | None = None

    def validate(self) -> "PeriodRequest":
        period = str(self.period or "mtd").strip().lower()
        if period not in _PERIOD_KINDS:
            raise ValueError("period must be mtd or ytd")
        extras = sorted(
            field
            for field in ("month", "year", "start_date", "end_date")
            if getattr(self, field) not in (None, "")
        )
        if extras:
            raise ValueError(f"period={period} does not accept: {', '.join(extras)}")
        return self

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "PeriodRequest":
        request = cls(
            period=str(payload.get("period") or "mtd").strip().lower(),
            as_of_date=payload.get("as_of_date"),
            month=payload.get("month"),
            year=payload.get("year"),
            start_date=payload.get("start_date"),
            end_date=payload.get("end_date"),
        )
        return request.validate()


@dataclass(frozen=True)
class PeriodWindow:
    kind: str
    reporting_timezone: str
    requested_start_date: str
    requested_end_date: str
    effective_start_at_ms: int
    effective_end_exclusive_at_ms: int
    valuation_open_at_ms: int
    valuation_end_at_ms: int
    status: str

    @property
    def is_current(self) -> bool:
        return self.status == "partial_current"

    def contains(self, timestamp_ms: int) -> bool:
        value = int(timestamp_ms)
        return self.effective_start_at_ms <= value < self.effective_end_exclusive_at_ms

    @property
    def statistic_days(self) -> Decimal:
        return Decimal(self.effective_end_exclusive_at_ms - self.effective_start_at_ms) / Decimal(86_400_000)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reporting_timezone": self.reporting_timezone,
            "requested_start_date": self.requested_start_date,
            "requested_end_date": self.requested_end_date,
            "effective_start_at_ms": self.effective_start_at_ms,
            "effective_end_exclusive_at_ms": self.effective_end_exclusive_at_ms,
            "valuation_open_at_ms": self.valuation_open_at_ms,
            "valuation_end_at_ms": self.valuation_end_at_ms,
            "status": self.status,
        }


def normalize_performance_period(
    request: PeriodRequest | Mapping[str, Any],
    *,
    report_now_ms: int | None = None,
    cutoff_ms_override: int | None = None,
) -> PeriodWindow:
    if not isinstance(request, PeriodRequest):
        request = PeriodRequest.from_mapping(request)
    request.validate()

    kind = str(request.period or "mtd").strip().lower()
    if kind not in _PERIOD_KINDS:
        raise ValueError("period must be mtd or ytd")

    tz = ZoneInfo(REPORTING_TIMEZONE)
    effective_now_ms = int(
        report_now_ms
        if report_now_ms is not None
        else datetime.now(tz).timestamp() * 1000
    )
    now_local = datetime.fromtimestamp(effective_now_ms / 1000, tz=tz)
    today = now_local.date()

    end_date = (
        _parse_date(request.as_of_date, field_name="as_of_date")
        if request.as_of_date
        else today
    )
    if end_date > today:
        raise ValueError("as_of_date cannot be in the future")
    start_date = date(end_date.year, end_date.month if kind == "mtd" else 1, 1)

    start_at_ms = _local_midnight_ms(start_date, tz)
    current_ending = end_date == today
    natural_end_exclusive_ms = (
        effective_now_ms + 1 if current_ending else _local_midnight_ms(end_date + timedelta(days=1), tz)
    )
    status = "partial_current" if current_ending else "complete_past"

    if cutoff_ms_override is not None:
        cutoff_ms = int(cutoff_ms_override)
        if cutoff_ms > effective_now_ms:
            raise ValueError("cutoff_ms_override cannot be in the future")
        if cutoff_ms < start_at_ms or cutoff_ms >= natural_end_exclusive_ms:
            raise ValueError("cutoff_ms_override must fall inside the normalized period")
        end_exclusive_ms = cutoff_ms + 1
        status = "partial_cutoff"
    else:
        end_exclusive_ms = natural_end_exclusive_ms

    if end_exclusive_ms <= start_at_ms:
        raise ValueError("normalized period must have positive duration")

    return PeriodWindow(
        kind=kind,
        reporting_timezone=REPORTING_TIMEZONE,
        requested_start_date=start_date.isoformat(),
        requested_end_date=end_date.isoformat(),
        effective_start_at_ms=start_at_ms,
        effective_end_exclusive_at_ms=end_exclusive_ms,
        valuation_open_at_ms=start_at_ms - 1,
        valuation_end_at_ms=end_exclusive_ms - 1,
        status=status,
    )


__all__ = [
    "PeriodRequest",
    "PeriodWindow",
    "REPORTING_TIMEZONE",
    "normalize_performance_period",
]
