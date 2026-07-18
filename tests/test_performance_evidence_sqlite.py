from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from domain.domain.performance.models import (
    ValuationMarkFact,
    parse_evidence_envelope,
    select_fx_rate,
    select_valuation_mark,
    validate_evidence_facts,
)
from src.infrastructure.performance_evidence_sqlite import PerformanceEvidenceSQLiteRepository


NOW_MS = 1_768_000_000_000


def _mark(
    *,
    source: str = "broker_snapshot",
    source_id: str = "mark-1",
    revision: int = 1,
    effective_at_ms: int = NOW_MS,
    supersedes_fact_id: str | None = None,
    fact_id: str | None = None,
    symbol: str = "NVDA",
) -> dict:
    return {
        "fact_id": fact_id,
        "instrument": {
            "type": "option",
            "symbol": symbol,
            "option_type": "put",
            "strike": "100",
            "expiration_ymd": "2026-08-21",
            "currency": "USD",
            "multiplier": "100",
        },
        "price": "2.35",
        "mark_kind": "midpoint",
        "effective_at_ms": effective_at_ms,
        "observed_at_ms": NOW_MS,
        "source": source,
        "source_id": source_id,
        "revision": revision,
        "supersedes_fact_id": supersedes_fact_id,
        "quality": {},
        "raw": {},
    }


def _rate(*, source: str = "official_close", source_id: str = "fx-1", effective_at_ms: int = NOW_MS) -> dict:
    return {
        "base_currency": "USD",
        "quote_currency": "CNY",
        "rate": "7.12",
        "rate_kind": "spot",
        "effective_at_ms": effective_at_ms,
        "observed_at_ms": NOW_MS,
        "source": source,
        "source_id": source_id,
        "revision": 1,
        "quality": {},
        "raw": {},
    }


def _envelope(*, marks: list[dict] | None = None, rates: list[dict] | None = None) -> dict:
    return {
        "schema_version": "option_performance_evidence.v1",
        "valuation_marks": marks or [],
        "fx_rates": rates or [],
    }


def _table_names(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with sqlite3.connect(path) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_missing_schema_read_and_dry_run_do_not_mutate_database(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    repo = PerformanceEvidenceSQLiteRepository(path)

    assert repo.read_all().schema_state == "not_initialized"
    assert not path.exists()

    result = repo.import_envelope(_envelope(marks=[_mark()], rates=[_rate()]), apply=False, migrated_at_ms=NOW_MS)

    assert result.applied is False
    assert result.schema_state_before == "not_initialized"
    assert not path.exists()


def test_apply_migrates_once_imports_atomically_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    repo = PerformanceEvidenceSQLiteRepository(path)
    payload = _envelope(marks=[_mark()], rates=[_rate()])

    first = repo.import_envelope(payload, apply=True, migrated_at_ms=NOW_MS)
    second = repo.import_envelope(payload, apply=True, migrated_at_ms=NOW_MS + 1)
    bundle = repo.read_all()

    assert first.inserted_count == 2
    assert second.inserted_count == 0
    assert second.idempotent_count == 2
    assert bundle.schema_state == "initialized_v1"
    assert len(bundle.valuation_marks) == 1
    assert len(bundle.fx_rates) == 1
    assert {
        "performance_evidence_schema",
        "performance_valuation_marks",
        "performance_fx_rate_facts",
    }.issubset(_table_names(path))


def test_batch_conflict_rolls_back_migration_and_all_facts(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    repo = PerformanceEvidenceSQLiteRepository(path)
    conflict = _mark(source_id="same")
    conflict2 = {**_mark(source_id="same"), "price": "9.99", "fact_id": "different-id"}

    with pytest.raises(ValueError, match="source identity conflict"):
        repo.import_envelope(_envelope(marks=[conflict, conflict2]), apply=True, migrated_at_ms=NOW_MS)

    assert not path.exists() or "performance_evidence_schema" not in _table_names(path)


def test_correction_requires_same_identity_and_selector_uses_active_priority_then_staleness() -> None:
    base = parse_evidence_envelope(_envelope(marks=[_mark(source="realtime_snapshot", fact_id="base")])).valuation_marks[0]
    correction_payload = _mark(
        source="manual_correction",
        source_id="corrected",
        revision=2,
        effective_at_ms=NOW_MS,
        supersedes_fact_id="base",
        fact_id="correction",
    )
    envelope = parse_evidence_envelope(_envelope(marks=[base.normalized_payload(), correction_payload]))

    selected = select_valuation_mark(
        envelope.valuation_marks,
        instrument_key=base.instrument_key,
        at_ms=NOW_MS + 2 * 86_400_000,
    )
    stale = select_valuation_mark(
        envelope.valuation_marks,
        instrument_key=base.instrument_key,
        at_ms=NOW_MS + 8 * 86_400_000,
    )

    assert selected.fact is not None and selected.fact.fact_id == "correction"
    assert stale.status == "stale"
    assert stale.fact is None

    mismatch = _mark(
        source="manual_correction",
        source_id="bad",
        revision=2,
        supersedes_fact_id="base",
        fact_id="bad",
        symbol="AAPL",
    )
    with pytest.raises(ValueError, match="preserve exact identity"):
        parse_evidence_envelope(_envelope(marks=[base.normalized_payload(), mismatch]))


def test_correction_cycle_and_equal_time_source_priority_are_deterministic() -> None:
    base = parse_evidence_envelope(
        _envelope(marks=[_mark(source="broker_snapshot", source_id="broker", fact_id="broker")])
    ).valuation_marks[0]
    official = ValuationMarkFact(
        fact_id="official",
        instrument=base.instrument,
        price="2.4",
        mark_kind="official_close",
        effective_at_ms=base.effective_at_ms,
        observed_at_ms=base.observed_at_ms,
        source="official_close",
        source_id="official",
    )
    selected = select_valuation_mark(
        [base, official],
        instrument_key=base.instrument_key,
        at_ms=base.effective_at_ms,
    )

    assert selected.fact is not None and selected.fact.fact_id == "official"

    cycle_a = ValuationMarkFact(
        fact_id="cycle-a",
        instrument=base.instrument,
        price="2.1",
        mark_kind="manual",
        effective_at_ms=base.effective_at_ms,
        observed_at_ms=base.observed_at_ms,
        source="manual_correction",
        source_id="cycle-a",
        supersedes_fact_id="cycle-b",
    )
    cycle_b = ValuationMarkFact(
        fact_id="cycle-b",
        instrument=base.instrument,
        price="2.2",
        mark_kind="manual",
        effective_at_ms=base.effective_at_ms,
        observed_at_ms=base.observed_at_ms,
        source="manual_correction",
        source_id="cycle-b",
        supersedes_fact_id="cycle-a",
    )
    with pytest.raises(ValueError, match="cycle"):
        validate_evidence_facts([], [], existing_marks=[cycle_a, cycle_b])

    future_correction = ValuationMarkFact(
        fact_id="future-correction",
        instrument=base.instrument,
        price="1.9",
        mark_kind="manual",
        effective_at_ms=base.effective_at_ms + 86_400_000,
        observed_at_ms=base.observed_at_ms + 86_400_000,
        source="manual_correction",
        source_id="future-correction",
        supersedes_fact_id="broker",
    )
    before_effective = select_valuation_mark(
        [base, future_correction],
        instrument_key=base.instrument_key,
        at_ms=base.effective_at_ms,
    )
    after_effective = select_valuation_mark(
        [base, future_correction],
        instrument_key=base.instrument_key,
        at_ms=future_correction.effective_at_ms,
    )
    assert before_effective.fact is base
    assert after_effective.fact is future_correction


def test_structured_instrument_columns_must_match_canonical_key(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    repo = PerformanceEvidenceSQLiteRepository(path)
    repo.import_envelope(_envelope(marks=[_mark()]), apply=True, migrated_at_ms=NOW_MS)

    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE performance_valuation_marks SET symbol = 'AMD'")

    bundle = repo.read_all()
    assert bundle.schema_state == "unsupported_schema"
    assert "structured identity mismatch" in str(bundle.message)


def test_non_finite_raw_payload_is_rejected_from_canonical_evidence() -> None:
    payload = _mark()
    payload["raw"] = {"bad": float("nan")}

    with pytest.raises(ValueError, match="Out of range float values"):
        parse_evidence_envelope(_envelope(marks=[payload]))


def test_fx_selector_supports_weekend_previous_close_and_rejects_over_seven_days() -> None:
    rate = parse_evidence_envelope(_envelope(rates=[_rate(effective_at_ms=NOW_MS)])).fx_rates[0]

    weekend = select_fx_rate([rate], base_currency="USD", at_ms=NOW_MS + 2 * 86_400_000)
    stale = select_fx_rate([rate], base_currency="USD", at_ms=NOW_MS + 7 * 86_400_000 + 1)

    assert weekend.status == "selected"
    assert weekend.fact is rate
    assert stale.status == "stale"
