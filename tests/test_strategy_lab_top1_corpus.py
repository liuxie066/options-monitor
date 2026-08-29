from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from src.application.strategy_lab.top1.contracts import VALIDATION_REQUIRED_DAYS
from src.application.strategy_lab.top1.corpus import (
    CorpusError,
    read_market_calendar_binding,
    refresh_market_calendar_binding,
)
from src.infrastructure.strategy_lab.experiment_store import ExperimentStore
from tests.candidate_evidence_helpers import (
    seal_market_calendar_fixture,
)


AVAILABLE = {"OM_STRATEGY_LAB_TOP1_AVAILABLE": "1"}
CALENDAR_HASH = "a" * 64
SOURCE_SHA = "c" * 40


class _CalendarGateway:
    def __init__(self, receipt: dict[str, Any]) -> None:
        self.receipt = receipt
        self.calls: list[dict[str, Any]] = []

    def get_trading_days_with_receipt(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.receipt


def _schedule(*, start_plus_min: int = 10, enabled: bool = True) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "timezone": "Asia/Hong_Kong",
        "run_window": {"start": "09:50", "end": "10:10"},
        "run_points": {"start_plus_min": start_plus_min},
    }


def _candidate(symbol: str = "0700.HK") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "contract_symbol": f"{symbol.replace('.', '')}260821P00400000",
        "expiration": "2026-08-21",
        "strike": 400,
        "spot": 450,
        "currency": "HKD",
        "open_interest": 500,
        "period_net_return_on_cash_basis": 0.012,
        "net_assignment_discount_pct": 0.10,
        "symbol_concentration_after": 0.20,
        "sell_limit": 5.10,
        "net_premium": 505.0,
        "net_cash_basis": 39_495.0,
        "net_income": 505.0,
        "net_income_cny": 465.0,
        "spread_ratio": 0.10,
        "stock_owner": "none",
        "fee_schedule_version": "fixture.v1",
        "fee_basis": "fixture",
        "fee_schedule_url": "https://example.test/fees",
    }


def _store(tmp_path: Path) -> ExperimentStore:
    store = ExperimentStore(tmp_path / "strategy-lab.sqlite3")
    store.migrate(migrated_at_utc="2026-07-20T00:00:00Z")
    return store


def _target_for(day: str, *, hour: int = 10, minute: int = 0) -> str:
    return f"{day}T{hour:02d}:{minute:02d}:00+08:00"


def _scheduler(
    day: str,
    *,
    hour: int = 10,
    minute: int = 0,
) -> dict[str, Any]:
    target = datetime.fromisoformat(_target_for(day, hour=hour, minute=minute))
    now_utc = target.astimezone(timezone.utc) + timedelta(seconds=30)
    return {
        "should_run_scan": True,
        "scheduled_scan_target_market": target.isoformat(),
        "now_utc": now_utc.isoformat().replace("+00:00", "Z"),
    }


def _trading_days(start: str, count: int) -> list[str]:
    current = date.fromisoformat(start)
    days: list[str] = []
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def test_calendar_binding_is_content_addressed_and_fails_closed(
    tmp_path: Path,
) -> None:
    with pytest.raises(CorpusError) as missing:
        read_market_calendar_binding(tmp_path / "missing", market="HK")
    assert missing.value.reason_code == "market_calendar_binding_unavailable"

    days = _trading_days("2026-07-21", VALIDATION_REQUIRED_DAYS)
    binding = seal_market_calendar_fixture(
        tmp_path, days, version="hk-calendar.fixture.v1"
    )
    assert binding["trading_dates"] == days
    snapshot_path = tmp_path.joinpath(*str(binding["snapshot_ref"]).split("/"))
    snapshot_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(CorpusError) as tampered:
        read_market_calendar_binding(tmp_path, market="HK")
    assert tampered.value.reason_code == "market_calendar_binding_unavailable"
    recovered = seal_market_calendar_fixture(
        tmp_path, days, version="hk-calendar.fixture.v2"
    )
    assert recovered["market_calendar_version"] == "hk-calendar.fixture.v2"
    assert recovered["snapshot_ref"] != binding["snapshot_ref"]


def test_calendar_refresh_publishes_compact_evidence_without_duplicate_growth(
    tmp_path: Path,
) -> None:
    gateway = _CalendarGateway(
        {
            "retcode": 0,
            "rows": [
                {"time": "2026-08-04", "trade_date_type": "MORNING"},
                {"time": "2026-08-03", "trade_date_type": "WHOLE"},
            ],
            "coverage_complete": True,
            "pagination_complete": True,
            "page_count": 1,
        }
    )
    kwargs = {
        "gateway": gateway,
        "market": "HK",
        "market_calendar_version": "hk-calendar.opend.v1",
        "coverage_start": "2026-08-03",
        "coverage_end": "2026-08-31",
    }

    first = refresh_market_calendar_binding(
        tmp_path,
        **kwargs,
        observed_at_utc="2026-08-16T01:00:00Z",
    )
    assert first["status"] == "published"
    assert first["binding"]["trading_dates"] == ["2026-08-03", "2026-08-04"]
    assert first["binding"]["trading_sessions"] == [
        {"trading_date": "2026-08-03", "trade_date_type": "WHOLE"},
        {"trading_date": "2026-08-04", "trade_date_type": "MORNING"},
    ]
    assert gateway.calls == [
        {"market": "HK", "start": "2026-08-03", "end": "2026-08-31"}
    ]
    capability_root = (
        tmp_path / "strategy_lab/top1/capabilities/market-calendar/hk"
    )
    files_before = sorted(
        path.relative_to(tmp_path) for path in capability_root.rglob("*.json")
    )
    assert len(files_before) == 2
    assert all("receipt" not in path.name for path in files_before)
    snapshot = json.loads(
        tmp_path.joinpath(*str(first["binding"]["snapshot_ref"]).split("/")).read_text(
            encoding="utf-8"
        )
    )
    assert snapshot["trading_sessions"] == first["binding"]["trading_sessions"]
    assert "trading_dates" not in snapshot

    second = refresh_market_calendar_binding(
        tmp_path,
        **kwargs,
        observed_at_utc="2026-08-16T02:00:00Z",
    )
    assert second["status"] == "unchanged"
    assert second["binding"] == first["binding"]
    assert sorted(
        path.relative_to(tmp_path) for path in capability_root.rglob("*.json")
    ) == files_before


@pytest.mark.parametrize(
    "receipt",
    [
        {
            "retcode": 0,
            "rows": [{"time": "2026-08-03", "trade_date_type": "WHOLE"}],
            "coverage_complete": False,
            "pagination_complete": True,
            "page_count": 1,
        },
        {
            "retcode": 0,
            "rows": [
                {"time": "2026-08-03", "trade_date_type": "WHOLE"},
                {"time": "2026-08-03", "trade_date_type": "MORNING"},
            ],
            "coverage_complete": True,
            "pagination_complete": True,
            "page_count": 1,
        },
        {
            "retcode": 0,
            "rows": [{"time": "2026-09-01", "trade_date_type": "WHOLE"}],
            "coverage_complete": True,
            "pagination_complete": True,
            "page_count": 1,
        },
        {
            "retcode": 0,
            "rows": [{"time": "2026-08-03", "trade_date_type": "UNKNOWN"}],
            "coverage_complete": True,
            "pagination_complete": True,
            "page_count": 1,
        },
    ],
)
def test_calendar_refresh_rejects_untrustworthy_source_without_writing(
    tmp_path: Path,
    receipt: dict[str, Any],
) -> None:
    with pytest.raises(CorpusError) as raised:
        refresh_market_calendar_binding(
            tmp_path,
            gateway=_CalendarGateway(receipt),
            market="HK",
            market_calendar_version="hk-calendar.opend.v1",
            coverage_start="2026-08-03",
            coverage_end="2026-08-31",
            observed_at_utc="2026-08-16T01:00:00Z",
        )
    assert raised.value.reason_code == "market_calendar_source_invalid"
    assert not (tmp_path / "strategy_lab").exists()
