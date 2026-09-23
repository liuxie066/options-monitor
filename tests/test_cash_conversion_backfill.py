from __future__ import annotations

from tests.ledger_sqlite_test_support import connect_ledger_fixture

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from src.application.cash_conversion import build_cash_conversion
from src.application.ledger.cash_conversion_migration import (
    CashConversionBackfillResult,
    backfill_cash_conversions,
    correct_superseded_cash_conversions,
)
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.infrastructure.performance_evidence_sqlite import PerformanceEvidenceSQLiteRepository


TZ = ZoneInfo("Asia/Shanghai")
EVENT_MS = int(datetime(2026, 7, 3, 10, 0, tzinfo=TZ).timestamp() * 1000)
RATE_MS = int(datetime(2026, 7, 3, 9, 15, tzinfo=TZ).timestamp() * 1000)
MIGRATION_MS = int(datetime(2026, 7, 24, 15, 0, tzinfo=TZ).timestamp() * 1000)



def _positions_db(tmp_path: Path) -> tuple[Path, SQLiteOptionPositionsRepository]:
    db_path = tmp_path / "option_positions.sqlite3"
    return db_path, SQLiteOptionPositionsRepository(db_path)


def _sale_event(stock_event_id: str, trade_time_ms: int) -> dict[str, object]:
    return {
        "stock_event_id": stock_event_id,
        "event_type": "sale",
        "trade_time_ms": trade_time_ms,
        "account": "lx",
        "broker": "富途",
        "symbol": "NVDA",
        "currency": "USD",
        "shares": 100,
        "price": 105,
        "fees": 1,
        "fee_provenance": {"basis": "actual", "source": "test"},
    }


def _backfill(
    repo: SQLiteOptionPositionsRepository,
    evidence_repo: PerformanceEvidenceSQLiteRepository,
    *,
    account: str = "lx",
    apply: bool = False,
    migrated_at_ms: int = MIGRATION_MS,
) -> CashConversionBackfillResult:
    return backfill_cash_conversions(
        repo,
        evidence_repo,
        account=account,
        apply=apply,
        migrated_at_ms=migrated_at_ms,
    )


def _correct(
    repo: SQLiteOptionPositionsRepository,
    evidence_repo: PerformanceEvidenceSQLiteRepository,
    *,
    account: str = "lx",
    apply: bool = False,
    migrated_at_ms: int = MIGRATION_MS,
) -> CashConversionBackfillResult:
    return correct_superseded_cash_conversions(
        repo,
        evidence_repo,
        account=account,
        apply=apply,
        migrated_at_ms=migrated_at_ms,
    )


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
            # §9.2 step 3: the short put side travels as the trade side.
            "side": "sell",
            "fee_provenance": {"basis": "actual", "source": "test"},
            **(raw_payload or {}),
        },
    )


def _import_rate(
    evidence_repo: PerformanceEvidenceSQLiteRepository,
    *,
    effective_at_ms: int = RATE_MS,
    quality: dict | None = None,
    rate: str = "7.2",
    source: str = "pbc_central_parity",
    source_id: str = "pbc:2026-07-03",
    supersedes_fact_id: str | None = None,
) -> str:
    rates = []
    if supersedes_fact_id:
        rates.extend(
            item.normalized_payload()
            for item in evidence_repo.read_all().fx_rates
            if str(item.fact_id) == supersedes_fact_id
        )
    payload = {
        "schema_version": "option_performance_evidence.v1",
        "valuation_marks": [],
        "fx_rates": [
            *rates,
            {
                "base_currency": "USD",
                "quote_currency": "CNY",
                "rate": rate,
                "rate_kind": "central_parity",
                "effective_at_ms": effective_at_ms,
                "observed_at_ms": MIGRATION_MS,
                "source": source,
                "source_id": source_id,
                "revision": 1,
                "supersedes_fact_id": supersedes_fact_id,
                "quality": quality or {"backfill": True},
                "raw": {},
            }
        ],
    }
    result = evidence_repo.import_envelope(payload, apply=True, migrated_at_ms=MIGRATION_MS)
    return str(result.envelope.fx_rates[-1].fact_id)


# Produced verbatim by the pre-§7 encoder at revision 4fc161b8
# (``encode_trade_event_for_storage`` on the same logical ``open-1`` event).
# It is the shape a database written before the order-domain-model change holds
# on disk: ``contract_key.strike``/``multiplier`` as JSON numbers, and the
# retired ``position_side``/``position_key`` identity keys still present.
_LEGACY_EVENT_JSON = (
    '{"contract_key": {"account": "lx", "broker": "富途", "expiration_ymd":'
    ' "2026-08-21", "option_type": "put", "position_key":'
    ' "富途|lx|NVDA|2026-08-21|100P|short", "position_side": "short",'
    ' "strike": 100.0, "underlying_symbol": "NVDA"}, "contracts": 1, "currency":'
    ' "USD", "event_id": "open-1", "event_time_ms": 1783044000000, "event_type":'
    ' "open", "fees": 1.0, "lot_id": "lot-open-1", "multiplier": 100.0, "price":'
    ' 2.0, "raw_payload": {"fee_provenance": {"basis": "actual", "source":'
    ' "test"}, "side": "sell"}, "source": "test", "target_event_id": null,'
    ' "target_lot_id": null}'
)


def _seed_stored_trade_event(repo: SQLiteOptionPositionsRepository, event_json: str) -> None:
    """Insert a trade event at the storage layer, exactly as a writer would."""
    payload = json.loads(event_json)
    contract_key = payload["contract_key"]
    with repo._connect() as conn:  # noqa: SLF001 - pre-migration storage fixture
        conn.execute(
            "UPDATE trade_event_ingest_sequence SET last_value = last_value + 1 "
            "WHERE singleton_id = 1"
        )
        ingest_seq = conn.execute(
            "SELECT last_value FROM trade_event_ingest_sequence WHERE singleton_id = 1"
        ).fetchone()["last_value"]
        conn.execute(
            """
            INSERT INTO trade_events (
              event_id, account, event_json, trade_time_ms,
              created_at_ms, updated_at_ms, ingest_seq, market, position_effect
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["event_id"],
                str(contract_key["account"]),
                event_json,
                int(payload["event_time_ms"]),
                1,
                1,
                int(ingest_seq),
                "US",
                str(payload["event_type"]).lower(),
            ),
        )


def test_backfill_applies_to_a_pre_section7_stored_event(tmp_path: Path) -> None:
    """A cash-conversion backfill must not rewrite unrelated stored fields.

    The plan compares its replacement against the bytes already in
    ``trade_events.event_json``. Re-encoding the decoded event would turn the
    stored number ``strike`` into the §7.4 decimal *text* and drop the retired
    identity keys, so on any pre-§7 database the compare-and-swap matches
    nothing and the trade-event immutability trigger rejects the write as a
    contract-key transition.
    """
    db_path, repo = _positions_db(tmp_path)
    _seed_stored_trade_event(repo, _LEGACY_EVENT_JSON)
    evidence_repo = PerformanceEvidenceSQLiteRepository(db_path)
    _import_rate(evidence_repo)

    preview = _backfill(repo, evidence_repo)
    assert preview.preview_conversion_count == 2
    assert preview.changed_event_count == 1

    applied = _backfill(repo, evidence_repo, apply=True)
    assert applied.migrated_conversion_count == 2
    assert applied.changed_event_count == 1

    with connect_ledger_fixture(db_path) as conn:
        stored = json.loads(
            conn.execute(
                "SELECT event_json FROM trade_events WHERE event_id = 'open-1'"
            ).fetchone()[0]
        )
    assert stored["raw_payload"]["cash_conversions"]["option_trade_cash_gross"][
        "amount_cny"
    ] == "1440"
    # Only the migrated field moved: the stored representation of every other
    # field survives the write untouched.
    assert stored["contract_key"]["strike"] == 100.0
    assert stored["contract_key"]["position_key"] == "富途|lx|NVDA|2026-08-21|100P|short"
    assert stored["contract_key"]["position_side"] == "short"
    assert stored["multiplier"] == 100.0
    assert stored["lot_id"] == "lot-open-1"


def test_stored_trade_event_rejects_a_reencoded_contract_key(tmp_path: Path) -> None:
    """Why the backfill patches stored JSON rather than re-encoding the event.

    §7.4 renders ``contract_key.strike`` as canonical decimal text. Writing that
    text over a pre-§7 number is a contract-key transition, and the trade-event
    immutability trigger aborts it -- so "re-encode the decoded event and write
    it back" is not a strategy the storage layer will accept at all, whatever
    the plan's compare-and-swap happens to be comparing.
    """
    db_path, repo = _positions_db(tmp_path)
    _seed_stored_trade_event(repo, _LEGACY_EVENT_JSON)

    reencoded = json.loads(_LEGACY_EVENT_JSON)
    reencoded["contract_key"] = {
        "account": "lx",
        "broker": "富途",
        "expiration_ymd": "2026-08-21",
        "option_type": "put",
        "strike": "1E+2",
        "underlying_symbol": "NVDA",
    }

    with connect_ledger_fixture(db_path) as conn:
        assert conn.execute(
            "SELECT json_type(event_json, '$.contract_key.strike') FROM trade_events"
        ).fetchone()[0] == "real"
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE trade_events SET event_json = ?, updated_at_ms = 1 "
                "WHERE event_id = 'open-1'",
                (json.dumps(reencoded, ensure_ascii=False, sort_keys=True),),
            )


def _has_table(path: Path, name: str) -> bool:
    with connect_ledger_fixture(path) as conn:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
    return row is not None


def test_backfill_dry_run_apply_and_second_apply_are_auditable_and_idempotent(
    tmp_path: Path,
) -> None:
    db_path, repo = _positions_db(tmp_path)
    repo.upsert_trade_event(_event("open-1"))
    evidence_repo = PerformanceEvidenceSQLiteRepository(db_path)
    fx_fact_id = _import_rate(evidence_repo)

    preview = _backfill(repo, evidence_repo)

    assert preview.applied is False
    assert preview.preview_conversion_count == 2
    assert preview.migrated_conversion_count == 0
    assert not _has_table(db_path, "cash_conversion_backfill_audit")
    assert "cash_conversions" not in repo.list_trade_events()[0]["raw_payload"]

    applied = _backfill(repo, evidence_repo, apply=True)
    repeated = _backfill(repo, evidence_repo, apply=True, migrated_at_ms=MIGRATION_MS + 1)

    assert applied.migrated_conversion_count == 2
    assert applied.changed_event_count == 1
    assert repeated.migrated_conversion_count == 0
    assert repeated.existing_observed_count == 2
    conversions = repo.list_trade_events()[0]["raw_payload"]["cash_conversions"]
    assert conversions["option_trade_cash_gross"]["amount_cny"] == "1440"
    assert conversions["option_fee_cash"]["amount_cny"] == "-7.2"
    assert conversions["option_trade_cash_gross"]["rate_source"] == "pbc_central_parity"
    assert conversions["option_trade_cash_gross"]["rate_evidence_fact_id"] == fx_fact_id
    with connect_ledger_fixture(db_path) as conn:
        audit_count = conn.execute(
            "SELECT COUNT(*) FROM cash_conversion_backfill_audit"
        ).fetchone()[0]
    assert audit_count == 2


def test_backfill_after_opend_time_correction_preserves_prior_audit(
    tmp_path: Path,
) -> None:
    db_path, repo = _positions_db(tmp_path)
    repo.upsert_trade_event(_event("open-1"))
    evidence_repo = PerformanceEvidenceSQLiteRepository(db_path)
    _import_rate(evidence_repo)
    _backfill(repo, evidence_repo, apply=True)

    with repo._connect() as conn:  # noqa: SLF001 - exact audit recovery fixture
        row = conn.execute(
            "SELECT event_json FROM trade_events WHERE event_id='open-1'"
        ).fetchone()
        payload = json.loads(str(row["event_json"]))
        payload["raw_payload"].pop("cash_conversions")
        payload["raw_payload"]["trade_time_correction_provenance"] = {
            "schema_version": "opend_trade_time_correction.v1",
            "correction_id": "opend_trade_time_correction_test",
            "provider": "opend",
            "source": "manual_trade_event_repair",
            "before_trade_time_ms": EVENT_MS - 1,
            "after_trade_time_ms": EVENT_MS,
            "invalidated_cash_conversion_keys": [
                "option_fee_cash",
                "option_trade_cash_gross",
            ],
        }
        conn.execute(
            "UPDATE trade_events SET event_json=? WHERE event_id='open-1'",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True),),
        )

    reapplied = _backfill(repo, evidence_repo, apply=True, migrated_at_ms=MIGRATION_MS + 1)

    assert reapplied.changed_event_count == 1
    assert reapplied.migrated_conversion_count == 2
    with connect_ledger_fixture(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM cash_conversion_backfill_audit"
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM cash_conversion_trade_time_repair_audit"
        ).fetchone()[0] == 2


def test_backfill_preserves_observed_conversion_and_does_not_use_stale_fx(
    tmp_path: Path,
) -> None:
    db_path, repo = _positions_db(tmp_path)
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

    lx = _backfill(repo, evidence_repo)
    sy = _backfill(repo, evidence_repo, account="sy")

    assert lx.existing_observed_count == 1
    assert lx.preview_conversion_count == 0
    assert any(item["cash_fact_id"] == "option_fee_cash:observed" for item in lx.unresolved)
    assert sy.preview_conversion_count == 0
    assert len(sy.unresolved) == 2


def test_backfill_carries_explicit_official_rate_across_non_business_day(tmp_path: Path) -> None:
    db_path, repo = _positions_db(tmp_path)
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

    result = _backfill(repo, evidence_repo, account="sy", apply=True)

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
    replay = _backfill(repo, evidence_repo, account="sy", migrated_at_ms=MIGRATION_MS + 1)
    assert replay.changed_event_count == 0
    assert replay.preview_conversion_count == 0
    assert replay.existing_observed_count == 2


def test_backfill_replaces_corrupt_observed_conversion(tmp_path: Path) -> None:
    db_path, repo = _positions_db(tmp_path)
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

    result = _backfill(repo, evidence_repo, apply=True)

    assert result.existing_observed_count == 0
    assert result.migrated_conversion_count == 2
    repaired = repo.list_trade_events()[0]["raw_payload"]["cash_conversions"]
    assert repaired["option_trade_cash_gross"]["amount_cny"] == "1440"


def test_backfill_enriches_assigned_stock_sale_cash(tmp_path: Path) -> None:
    db_path, repo = _positions_db(tmp_path)
    holiday_event_ms = int(datetime(2026, 7, 5, 10, 0, tzinfo=TZ).timestamp() * 1000)
    repo.upsert_assigned_stock_event(_sale_event("sale-1", holiday_event_ms))
    evidence_repo = PerformanceEvidenceSQLiteRepository(db_path)
    _import_rate(
        evidence_repo,
        quality={
            "backfill": True,
            "official": True,
            "carry_forward_dates": ["2026-07-05"],
        },
    )

    result = _backfill(repo, evidence_repo, apply=True)

    assert result.migrated_conversion_count == 2
    conversions = repo.list_assigned_stock_events()[0]["cash_conversions"]
    assert conversions["assigned_stock_sale_cash_gross"]["amount_cny"] == "75600"
    assert conversions["assigned_stock_sale_fee_cash"]["amount_cny"] == "-7.2"
    assert (
        conversions["assigned_stock_sale_cash_gross"]["method"]
        == "historical_business_day_fx_carry_forward"
    )
    replay = _backfill(repo, evidence_repo, migrated_at_ms=MIGRATION_MS + 1)
    assert replay.changed_event_count == 0
    assert replay.preview_conversion_count == 0
    assert replay.existing_observed_count == 2


def test_correction_requires_explicit_superseding_evidence_and_is_auditable(
    tmp_path: Path,
) -> None:
    db_path, repo = _positions_db(tmp_path)
    repo.upsert_trade_event(_event("correct-trade"))
    repo.upsert_assigned_stock_event(_sale_event("correct-sale", EVENT_MS))
    evidence_repo = PerformanceEvidenceSQLiteRepository(db_path)
    old_fact_id = _import_rate(evidence_repo)
    _backfill(repo, evidence_repo, apply=True)
    _import_rate(
        evidence_repo,
        rate="7.3",
        source="official_close",
        source_id="unrelated:2026-07-03",
    )
    preserved = _backfill(repo, evidence_repo, apply=True, migrated_at_ms=MIGRATION_MS + 1)

    unrelated = _correct(repo, evidence_repo, migrated_at_ms=MIGRATION_MS + 1)

    assert preserved.migrated_conversion_count == 0
    assert unrelated.preview_conversion_count == 0
    corrected_fact_id = _import_rate(
        evidence_repo,
        rate="7.0",
        source="manual_correction",
        source_id="pbc-correction:2026-07-03",
        supersedes_fact_id=old_fact_id,
    )
    preview = _correct(repo, evidence_repo, migrated_at_ms=MIGRATION_MS + 2)
    assert preview.preview_conversion_count == 4
    assert not _has_table(db_path, "cash_conversion_correction_audit")

    applied = _correct(repo, evidence_repo, apply=True, migrated_at_ms=MIGRATION_MS + 2)
    repeated = _correct(repo, evidence_repo, migrated_at_ms=MIGRATION_MS + 3)

    assert applied.migrated_conversion_count == 4
    assert repeated.preview_conversion_count == 0
    trade_conversions = repo.list_trade_events()[0]["raw_payload"]["cash_conversions"]
    stock_conversions = repo.list_assigned_stock_events()[0]["cash_conversions"]
    assert trade_conversions["option_trade_cash_gross"]["amount_cny"] == "1400"
    assert stock_conversions["assigned_stock_sale_cash_gross"]["amount_cny"] == "73500"
    assert all(
        conversion["rate_evidence_fact_id"] == corrected_fact_id
        for conversions in (trade_conversions, stock_conversions)
        for conversion in conversions.values()
    )
    with connect_ledger_fixture(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM cash_conversion_correction_audit"
        ).fetchone()[0] == 4
