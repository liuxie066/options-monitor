from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from domain.domain.performance.models import (
    EvidenceEnvelope,
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
    assert repo.read_fx_rates().schema_state == "not_initialized"
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


def test_read_all_validates_persisted_corrections_independent_of_fact_id_order(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    repo = PerformanceEvidenceSQLiteRepository(path)
    parent = {**_rate(), "fact_id": "fx_z_parent"}
    correction = {
        **_rate(source="manual_correction", source_id="corrected"),
        "fact_id": "fx_a_child",
        "rate": "7.11",
        "supersedes_fact_id": "fx_z_parent",
    }

    repo.import_envelope(
        _envelope(rates=[parent, correction]),
        apply=True,
        migrated_at_ms=NOW_MS,
    )

    bundle = repo.read_all()
    assert bundle.schema_state == "initialized_v1"
    assert {fact.fact_id for fact in bundle.fx_rates} == {"fx_z_parent", "fx_a_child"}
    assert repo.read_fx_rates().fx_rates == bundle.fx_rates
    assert repo.freeze_cash_fx_daily_rates(migrated_at_ms=NOW_MS) == bundle.fx_rates


def test_fx_reads_skip_invalid_valuation_records_and_preview_has_no_writes(tmp_path, monkeypatch):
    from src.application.cash_conversion import load_cash_fx_payload
    from src.infrastructure import performance_evidence_sqlite as module

    repo = PerformanceEvidenceSQLiteRepository(tmp_path / "evidence.sqlite3")
    repo.import_envelope(_envelope(marks=[_mark()], rates=[_rate()]), apply=True, migrated_at_ms=NOW_MS)
    expected = repo.read_all().fx_rates
    with sqlite3.connect(repo.db_path) as conn:
        conn.execute("UPDATE performance_valuation_marks SET price_text='999'")
    assert repo.read_all().schema_state == "unsupported_schema"
    with pytest.raises(ValueError, match="valuation normalized payload mismatch"):
        repo.import_envelope(_envelope(), apply=True, migrated_at_ms=NOW_MS)
    before = repo.db_path.read_bytes()
    statements = []
    original = module.PerformanceEvidenceSQLiteRepository._connect_readonly

    def traced_readonly(self):
        conn = original(self)
        conn.set_trace_callback(statements.append)
        return conn

    def unexpected_mark(_row):
        raise AssertionError("FX paths must not decode valuation marks")

    monkeypatch.setattr(module.PerformanceEvidenceSQLiteRepository, "_connect_readonly", traced_readonly)
    monkeypatch.setattr(module, "_mark_from_row", unexpected_mark)
    assert repo.read_fx_rates().fx_rates == expected
    assert load_cash_fx_payload(repo, persist=False)["fx_rate_facts"] == expected
    assert repo.db_path.read_bytes() == before
    with sqlite3.connect(repo.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.set_trace_callback(statements.append)
        assert repo.freeze_cash_fx_daily_rates(migrated_at_ms=NOW_MS, conn=conn) == expected
        assert conn.in_transaction
        conn.rollback()
    assert not any("FROM performance_valuation_marks" in sql for sql in statements)


@pytest.mark.parametrize("damage", ["value", "column"])
def test_fx_read_unavailable_bundle_preserves_full_reader_error_contract(tmp_path, damage):
    repo = PerformanceEvidenceSQLiteRepository(tmp_path / "evidence.sqlite3")
    repo.import_envelope(_envelope(rates=[_rate()]), apply=True, migrated_at_ms=NOW_MS)
    with sqlite3.connect(repo.db_path) as conn:
        conn.execute("UPDATE performance_fx_rate_facts SET rate_text='99'" if damage == "value" else
                     "ALTER TABLE performance_fx_rate_facts RENAME COLUMN raw_json TO missing_raw_json")
    before = repo.db_path.read_bytes()
    fx, full = repo.read_fx_rates(), repo.read_all()
    assert fx.schema_state == full.schema_state == "unsupported_schema"
    assert fx.message == full.message
    assert fx.fx_rates == ()
    assert repo.db_path.read_bytes() == before


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


def _fact(kind: str, **changes):
    envelope = parse_evidence_envelope(_envelope(marks=[_mark()], rates=[_rate()]))
    return replace(getattr(envelope, kind)[0], **changes)


@pytest.mark.parametrize("kind", ["valuation_marks", "fx_rates"])
@pytest.mark.parametrize("invalid", ["self", "missing", "forward", "identity", "fact_id", "source"])
def test_invalid_batches_preserve_first_error_for_dict_and_typed_import(tmp_path, kind, invalid):
    base = _fact(kind, fact_id="base")
    child = replace(base, fact_id="child", source_id="child", supersedes_fact_id="base")
    facts = [base, child]
    if invalid == "self":
        facts[1] = replace(child, supersedes_fact_id="child")
        message = "evidence fact cannot supersede itself"
    elif invalid == "missing":
        facts[1] = replace(child, supersedes_fact_id="absent")
        message = "supersedes_fact_id must reference an existing or earlier fact: absent"
    elif invalid == "forward":
        facts.reverse()
        message = "supersedes_fact_id must reference an existing or earlier fact: base"
    elif invalid == "identity":
        changes = ({"instrument": replace(base.instrument, symbol="AAPL")}
                   if kind == "valuation_marks" else {"base_currency": "HKD"})
        facts[1] = replace(child, **changes)
        message = "superseding evidence must preserve exact identity"
    elif invalid == "fact_id":
        facts[1] = replace(child, fact_id="base")
        message = "fact_id conflict: base"
    else:
        facts[1] = replace(child, source_id=base.source_id)
        message = f"source identity conflict: {base.source_identity}"
    facts.append(replace(child, fact_id="later", source_id="later", supersedes_fact_id="later"))
    envelope = EvidenceEnvelope(**{kind: tuple(facts)})
    payload = {"schema_version": envelope.schema_version, kind: [fact.normalized_payload() for fact in facts]}
    repo = PerformanceEvidenceSQLiteRepository(tmp_path / "evidence.sqlite3")
    for value in (payload, envelope):
        with pytest.raises(ValueError) as caught:
            repo.import_envelope(value, apply=True, migrated_at_ms=NOW_MS)
        assert str(caught.value) == message
        assert not repo.db_path.exists()


@pytest.mark.parametrize("kind", ["valuation_marks", "fx_rates"])
def test_corrections_to_existing_and_earlier_facts_survive_readback_and_retry(tmp_path, kind):
    base = _fact(kind, fact_id="z-parent", source_id="parent")
    child = replace(base, fact_id="m-child", source_id="child", supersedes_fact_id=base.fact_id)
    grandchild = replace(base, fact_id="a-child", source_id="grandchild", supersedes_fact_id=child.fact_id)
    repo = PerformanceEvidenceSQLiteRepository(tmp_path / "evidence.sqlite3")
    repo.import_envelope(EvidenceEnvelope(**{kind: (base,)}), apply=True, migrated_at_ms=NOW_MS)
    envelope = EvidenceEnvelope(**{kind: (child, grandchild, child)})
    result = repo.import_envelope(envelope, apply=True, migrated_at_ms=NOW_MS)
    assert (result.inserted_count, result.idempotent_count) == (2, 1)
    assert getattr(repo.read_all(), kind) == (grandchild, child, base)
    retry = repo.import_envelope(envelope, apply=True, migrated_at_ms=NOW_MS)
    assert (retry.inserted_count, retry.idempotent_count) == (0, 3)


@pytest.mark.parametrize("kind", ["valuation_marks", "fx_rates"])
@pytest.mark.parametrize("damage", ["cycle", "missing", "identity"])
def test_existing_graph_must_be_valid_before_incoming_can_be_considered(kind, damage):
    base = _fact(kind, fact_id="base")
    child = replace(base, fact_id="child", source_id="child", supersedes_fact_id="base")
    incoming = ()
    if damage == "cycle":
        existing = (replace(base, supersedes_fact_id="child"), child)
        message = "evidence correction cycle detected"
    elif damage == "missing":
        existing = (child,)
        incoming = (base,)
        message = "supersedes_fact_id does not exist: base"
    else:
        changes = ({"instrument": replace(base.instrument, symbol="AAPL")}
                   if kind == "valuation_marks" else {"base_currency": "HKD"})
        existing = (base, replace(child, **changes))
        message = "superseding evidence must preserve exact identity"
    envelope = EvidenceEnvelope(**{kind: incoming})
    existing_key = "existing_marks" if kind == "valuation_marks" else "existing_rates"
    with pytest.raises(ValueError) as caught:
        validate_evidence_facts(envelope.valuation_marks, envelope.fx_rates, **{existing_key: existing})
    assert str(caught.value) == message


@pytest.mark.parametrize("kind", ["valuation_marks", "fx_rates"])
def test_correction_reference_cannot_cross_evidence_kinds(tmp_path, kind):
    mark = _fact("valuation_marks", fact_id="mark")
    rate = _fact("fx_rates", fact_id="rate")
    if kind == "valuation_marks":
        mark = replace(mark, supersedes_fact_id="rate")
        target = "rate"
    else:
        rate = replace(rate, supersedes_fact_id="mark")
        target = "mark"
    envelope = EvidenceEnvelope(valuation_marks=(mark,), fx_rates=(rate,))
    payload = _envelope(marks=[mark.normalized_payload()], rates=[rate.normalized_payload()])
    repo = PerformanceEvidenceSQLiteRepository(tmp_path / "evidence.sqlite3")
    for value in (payload, envelope):
        with pytest.raises(ValueError) as caught:
            repo.import_envelope(value, apply=True, migrated_at_ms=NOW_MS)
        assert str(caught.value) == f"supersedes_fact_id must reference an existing or earlier fact: {target}"
        assert not repo.db_path.exists()


@pytest.mark.parametrize("kind", ["valuation_marks", "fx_rates"])
def test_failure_after_first_insert_rolls_back_facts_and_schema(tmp_path, monkeypatch, kind):
    from src.infrastructure import performance_evidence_sqlite as module

    first = _fact(kind, fact_id="first", source_id="first")
    second = replace(first, fact_id="second", source_id="second")
    table = "performance_valuation_marks" if kind == "valuation_marks" else "performance_fx_rate_facts"
    insert_name = "_insert_mark" if kind == "valuation_marks" else "_insert_rate"
    original = getattr(module, insert_name)

    def fail_second(conn, fact):
        if fact.fact_id == "second":
            assert conn.in_transaction
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
            raise RuntimeError("second insert failed")
        return original(conn, fact)

    monkeypatch.setattr(module, insert_name, fail_second)
    path = tmp_path / "evidence.sqlite3"
    repo = PerformanceEvidenceSQLiteRepository(path)
    with pytest.raises(RuntimeError, match="second insert failed"):
        repo.import_envelope(EvidenceEnvelope(**{kind: (first, second)}), apply=True, migrated_at_ms=NOW_MS)
    assert path.exists()
    assert _table_names(path) == set()
    assert PerformanceEvidenceSQLiteRepository(path).read_all().schema_state == "not_initialized"


@pytest.mark.parametrize("kind", ["valuation_marks", "fx_rates"])
def test_bulk_import_graph_pass_count_does_not_grow_with_batch_size(tmp_path, kind):
    from domain.domain.performance import models

    counts = []
    factory = _mark if kind == "valuation_marks" else _rate
    for size in (8, 80):
        payload = {"schema_version": "option_performance_evidence.v1",
                   kind: [{**factory(source_id=f"fact-{i}"), "fact_id": f"fact-{i}"} for i in range(size)]}
        repo = PerformanceEvidenceSQLiteRepository(tmp_path / f"{size}.sqlite3")
        with patch.object(models, "_validate_correction_graph", wraps=models._validate_correction_graph) as graph:
            result = repo.import_envelope(payload, apply=True, migrated_at_ms=NOW_MS)
            counts.append(graph.call_count)
        assert result.inserted_count == size
        assert {fact.fact_id for fact in getattr(repo.read_all(), kind)} == {f"fact-{i}" for i in range(size)}
    assert counts[0] > 0
    assert counts[0] == counts[1]
