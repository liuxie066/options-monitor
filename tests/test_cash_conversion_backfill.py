from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from domain.domain.ledger import ContractKey, TradeEvent
from src.application.cash_conversion import build_cash_conversion
from src.application.ledger.cash_conversion_migration import backfill_cash_conversions
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.performance.service import build_option_period_performance
from src.infrastructure.performance_evidence_sqlite import PerformanceEvidenceSQLiteRepository


TZ = ZoneInfo("Asia/Shanghai")
EVENT_MS = int(datetime(2026, 7, 3, 10, 0, tzinfo=TZ).timestamp() * 1000)
RATE_MS = int(datetime(2026, 7, 3, 9, 15, tzinfo=TZ).timestamp() * 1000)
MIGRATION_MS = int(datetime(2026, 7, 24, 15, 0, tzinfo=TZ).timestamp() * 1000)


def _event(
    event_id: str,
    *,
    account: str = "lx",
    event_time_ms: int = EVENT_MS,
    raw_payload: dict | None = None,
) -> TradeEvent:
    return TradeEvent(
        event_id=event_id,
        event_type="open",
        event_time_ms=event_time_ms,
        contract_key=ContractKey.from_values(
            broker="富途",
            account=account,
            underlying_symbol="NVDA",
            option_type="put",
            position_side="short",
            strike=100,
            expiration_ymd="2026-08-21",
        ),
        contracts=1,
        price=2.0,
        currency="USD",
        source="test",
        multiplier=100,
        fees=1.0,
        lot_id=f"lot-{event_id}",
        raw_payload={
            "fee_provenance": {"basis": "actual", "source": "test"},
            **(raw_payload or {}),
        },
    )


def _import_rate(
    evidence_repo: PerformanceEvidenceSQLiteRepository,
    *,
    effective_at_ms: int = RATE_MS,
    quality: dict | None = None,
) -> str:
    payload = {
        "schema_version": "option_performance_evidence.v1",
        "valuation_marks": [],
        "fx_rates": [
            {
                "base_currency": "USD",
                "quote_currency": "CNY",
                "rate": "7.2",
                "rate_kind": "central_parity",
                "effective_at_ms": effective_at_ms,
                "observed_at_ms": MIGRATION_MS,
                "source": "pbc_central_parity",
                "source_id": "pbc:2026-07-03",
                "revision": 1,
                "quality": quality or {"backfill": True},
                "raw": {},
            }
        ],
    }
    result = evidence_repo.import_envelope(payload, apply=True, migrated_at_ms=MIGRATION_MS)
    return str(result.envelope.fx_rates[0].fact_id)


def _has_table(path: Path, name: str) -> bool:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
    return row is not None


def test_backfill_dry_run_apply_and_second_apply_are_auditable_and_idempotent(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "option_positions.sqlite3"
    repo = SQLiteOptionPositionsRepository(db_path)
    repo.upsert_trade_event(_event("open-1"))
    evidence_repo = PerformanceEvidenceSQLiteRepository(db_path)
    fx_fact_id = _import_rate(evidence_repo)

    preview = backfill_cash_conversions(
        repo,
        evidence_repo,
        account="lx",
        apply=False,
        migrated_at_ms=MIGRATION_MS,
    )

    assert preview.applied is False
    assert preview.preview_conversion_count == 2
    assert preview.migrated_conversion_count == 0
    assert not _has_table(db_path, "cash_conversion_backfill_audit")
    assert "cash_conversions" not in repo.list_trade_events()[0]["raw_payload"]

    applied = backfill_cash_conversions(
        repo,
        evidence_repo,
        account="lx",
        apply=True,
        migrated_at_ms=MIGRATION_MS,
    )
    repeated = backfill_cash_conversions(
        repo,
        evidence_repo,
        account="lx",
        apply=True,
        migrated_at_ms=MIGRATION_MS + 1,
    )

    assert applied.migrated_conversion_count == 2
    assert applied.changed_event_count == 1
    assert repeated.migrated_conversion_count == 0
    assert repeated.existing_observed_count == 2
    conversions = repo.list_trade_events()[0]["raw_payload"]["cash_conversions"]
    assert conversions["option_trade_cash_gross"]["amount_cny"] == "1440"
    assert conversions["option_fee_cash"]["amount_cny"] == "-7.2"
    assert conversions["option_trade_cash_gross"]["rate_source"] == "pbc_central_parity"
    assert conversions["option_trade_cash_gross"]["rate_evidence_fact_id"] == fx_fact_id
    with sqlite3.connect(db_path) as conn:
        audit_count = conn.execute(
            "SELECT COUNT(*) FROM cash_conversion_backfill_audit"
        ).fetchone()[0]
    assert audit_count == 2
    report = build_option_period_performance(
        repo,
        period={"period": "month", "month": "2026-07"},
        account="lx",
        now_ms=MIGRATION_MS,
    )
    assert report["cash"]["option_trade_cash_gross"]["cny"] == 1440.0
    assert report["cash"]["option_fee_cash"]["cny"] == -7.2


def test_backfill_preserves_observed_conversion_and_does_not_use_stale_fx(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "option_positions.sqlite3"
    repo = SQLiteOptionPositionsRepository(db_path)
    observed = build_cash_conversion(
        cash_fact_id="option_trade_cash_gross:observed",
        amount=200,
        currency="USD",
        fx_payload={
            "rates": {"USDCNY": "7.3"},
            "timestamp": datetime.fromtimestamp(RATE_MS / 1000, tz=TZ).isoformat(),
        },
        effective_at_ms=EVENT_MS,
        observed_at_ms=MIGRATION_MS,
    )
    repo.upsert_trade_event(
        _event(
            "observed",
            raw_payload={"cash_conversions": {"option_trade_cash_gross": observed}},
        )
    )
    repo.upsert_trade_event(_event("stale", account="sy"))
    evidence_repo = PerformanceEvidenceSQLiteRepository(db_path)
    _import_rate(evidence_repo, effective_at_ms=EVENT_MS - 2 * 24 * 60 * 60 * 1000)

    lx = backfill_cash_conversions(
        repo,
        evidence_repo,
        account="lx",
        apply=False,
        migrated_at_ms=MIGRATION_MS,
    )
    sy = backfill_cash_conversions(
        repo,
        evidence_repo,
        account="sy",
        apply=False,
        migrated_at_ms=MIGRATION_MS,
    )

    assert lx.existing_observed_count == 1
    assert lx.preview_conversion_count == 0
    assert any(item["cash_fact_id"] == "option_fee_cash:observed" for item in lx.unresolved)
    assert sy.preview_conversion_count == 0
    assert len(sy.unresolved) == 2


def test_backfill_carries_explicit_official_rate_across_non_business_day(tmp_path: Path) -> None:
    db_path = tmp_path / "option_positions.sqlite3"
    repo = SQLiteOptionPositionsRepository(db_path)
    holiday_event_ms = int(datetime(2026, 7, 5, 10, 0, tzinfo=TZ).timestamp() * 1000)
    weekday_event_ms = int(datetime(2026, 7, 6, 10, 0, tzinfo=TZ).timestamp() * 1000)
    repo.upsert_trade_event(
        _event("holiday", account="sy", event_time_ms=holiday_event_ms)
    )
    repo.upsert_trade_event(
        _event("unlisted", account="sy", event_time_ms=weekday_event_ms)
    )
    evidence_repo = PerformanceEvidenceSQLiteRepository(db_path)
    _import_rate(
        evidence_repo,
        quality={
            "backfill": True,
            "official": True,
            "carry_forward_dates": ["2026-07-05"],
        },
    )

    result = backfill_cash_conversions(
        repo,
        evidence_repo,
        account="sy",
        apply=True,
        migrated_at_ms=MIGRATION_MS,
    )

    assert result.migrated_conversion_count == 2
    assert any(
        item["cash_fact_id"] == "option_trade_cash_gross:unlisted"
        for item in result.unresolved
    )
    conversion = repo.list_trade_events()[0]["raw_payload"]["cash_conversions"][
        "option_trade_cash_gross"
    ]
    assert conversion["amount_cny"] == "1440"
    assert conversion["method"] == "historical_business_day_fx_carry_forward"
    assert conversion["rate_timestamp"] == datetime.fromtimestamp(
        RATE_MS / 1000,
        tz=timezone.utc,
    ).isoformat()
    replay = backfill_cash_conversions(
        repo,
        evidence_repo,
        account="sy",
        apply=False,
        migrated_at_ms=MIGRATION_MS + 1,
    )
    assert replay.changed_event_count == 0
    assert replay.preview_conversion_count == 0
    assert replay.existing_observed_count == 2


def test_backfill_replaces_corrupt_observed_conversion(tmp_path: Path) -> None:
    db_path = tmp_path / "option_positions.sqlite3"
    repo = SQLiteOptionPositionsRepository(db_path)
    conversion = build_cash_conversion(
        cash_fact_id="option_trade_cash_gross:corrupt",
        amount=200,
        currency="USD",
        fx_payload={
            "rates": {"USDCNY": "7.2"},
            "timestamp": datetime.fromtimestamp(
                RATE_MS / 1000,
                tz=TZ,
            ).isoformat(),
        },
        effective_at_ms=EVENT_MS,
        observed_at_ms=MIGRATION_MS,
    )
    conversion["amount_cny"] = "999999"
    repo.upsert_trade_event(
        _event(
            "corrupt",
            raw_payload={
                "cash_conversions": {
                    "option_trade_cash_gross": conversion,
                }
            },
        )
    )
    evidence_repo = PerformanceEvidenceSQLiteRepository(db_path)
    _import_rate(evidence_repo)

    result = backfill_cash_conversions(
        repo,
        evidence_repo,
        account="lx",
        apply=True,
        migrated_at_ms=MIGRATION_MS,
    )

    assert result.existing_observed_count == 0
    assert result.migrated_conversion_count == 2
    repaired = repo.list_trade_events()[0]["raw_payload"]["cash_conversions"]
    assert repaired["option_trade_cash_gross"]["amount_cny"] == "1440"


def test_backfill_enriches_assigned_stock_sale_cash(tmp_path: Path) -> None:
    db_path = tmp_path / "option_positions.sqlite3"
    repo = SQLiteOptionPositionsRepository(db_path)
    holiday_event_ms = int(datetime(2026, 7, 5, 10, 0, tzinfo=TZ).timestamp() * 1000)
    repo.upsert_assigned_stock_event(
        {
            "stock_event_id": "sale-1",
            "event_type": "sale",
            "trade_time_ms": holiday_event_ms,
            "account": "lx",
            "broker": "富途",
            "symbol": "NVDA",
            "currency": "USD",
            "shares": 100,
            "price": 105,
            "fees": 1,
            "fee_provenance": {"basis": "actual", "source": "test"},
        }
    )
    evidence_repo = PerformanceEvidenceSQLiteRepository(db_path)
    _import_rate(
        evidence_repo,
        quality={
            "backfill": True,
            "official": True,
            "carry_forward_dates": ["2026-07-05"],
        },
    )

    result = backfill_cash_conversions(
        repo,
        evidence_repo,
        account="lx",
        apply=True,
        migrated_at_ms=MIGRATION_MS,
    )

    assert result.migrated_conversion_count == 2
    conversions = repo.list_assigned_stock_events()[0]["cash_conversions"]
    assert conversions["assigned_stock_sale_cash_gross"]["amount_cny"] == "75600"
    assert conversions["assigned_stock_sale_fee_cash"]["amount_cny"] == "-7.2"
    assert (
        conversions["assigned_stock_sale_cash_gross"]["method"]
        == "historical_business_day_fx_carry_forward"
    )
    replay = backfill_cash_conversions(
        repo,
        evidence_repo,
        account="lx",
        apply=False,
        migrated_at_ms=MIGRATION_MS + 1,
    )
    assert replay.changed_event_count == 0
    assert replay.preview_conversion_count == 0
    assert replay.existing_observed_count == 2
