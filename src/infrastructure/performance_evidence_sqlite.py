from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from domain.domain.performance.models import (
    EvidenceEnvelope,
    FXRateFact,
    OptionInstrumentKey,
    StockInstrumentKey,
    ValuationMarkFact,
    canonical_decimal_text,
    parse_evidence_envelope,
    validate_evidence_facts,
)
from src.infrastructure.private_storage import connect_private_sqlite, private_path, secure_sqlite_artifacts

_SCHEMA_COMPONENT = "option_performance_evidence"
_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class EvidenceReadBundle:
    schema_state: str
    valuation_marks: tuple[ValuationMarkFact, ...] = ()
    fx_rates: tuple[FXRateFact, ...] = ()
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_state": self.schema_state,
            "valuation_marks": [item.normalized_payload() for item in self.valuation_marks],
            "fx_rates": [item.normalized_payload() for item in self.fx_rates],
            "message": self.message,
        }


@dataclass(frozen=True)
class EvidenceImportResult:
    applied: bool
    schema_state_before: str
    schema_state_after: str
    valuation_mark_count: int
    fx_rate_count: int
    inserted_count: int
    idempotent_count: int
    envelope: EvidenceEnvelope

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "schema_state_before": self.schema_state_before,
            "schema_state_after": self.schema_state_after,
            "valuation_mark_count": self.valuation_mark_count,
            "fx_rate_count": self.fx_rate_count,
            "inserted_count": self.inserted_count,
            "idempotent_count": self.idempotent_count,
            "envelope": self.envelope.to_dict(),
        }


def migrate_evidence_schema(conn: sqlite3.Connection, *, migrated_at_ms: int) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS performance_evidence_schema(
          component TEXT PRIMARY KEY,
          schema_version INTEGER NOT NULL,
          migrated_at_ms INTEGER NOT NULL
        )
        """
    )
    row = conn.execute(
        "SELECT schema_version FROM performance_evidence_schema WHERE component = ?",
        (_SCHEMA_COMPONENT,),
    ).fetchone()
    if row is not None and int(row[0]) != _SCHEMA_VERSION:
        raise ValueError(f"unsupported evidence schema version: {row[0]}")
    conn.execute(
        """
        INSERT INTO performance_evidence_schema(component, schema_version, migrated_at_ms)
        VALUES (?, ?, ?)
        ON CONFLICT(component) DO NOTHING
        """,
        (_SCHEMA_COMPONENT, _SCHEMA_VERSION, int(migrated_at_ms)),
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS performance_valuation_marks(
          fact_id TEXT PRIMARY KEY,
          instrument_key TEXT NOT NULL,
          key_version INTEGER NOT NULL,
          instrument_type TEXT NOT NULL,
          symbol TEXT NOT NULL,
          option_type TEXT,
          strike_text TEXT,
          expiration_ymd TEXT,
          multiplier_text TEXT,
          currency TEXT NOT NULL,
          price_text TEXT NOT NULL,
          mark_kind TEXT NOT NULL,
          effective_at_ms INTEGER NOT NULL,
          observed_at_ms INTEGER NOT NULL,
          source TEXT NOT NULL,
          source_id TEXT NOT NULL,
          revision INTEGER NOT NULL,
          supersedes_fact_id TEXT,
          quality_json TEXT NOT NULL,
          raw_json TEXT NOT NULL,
          UNIQUE(source, source_id, revision)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_performance_marks_identity_time
        ON performance_valuation_marks(instrument_key, effective_at_ms)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS performance_fx_rate_facts(
          fact_id TEXT PRIMARY KEY,
          base_currency TEXT NOT NULL,
          quote_currency TEXT NOT NULL,
          rate_text TEXT NOT NULL,
          rate_kind TEXT NOT NULL,
          effective_at_ms INTEGER NOT NULL,
          observed_at_ms INTEGER NOT NULL,
          source TEXT NOT NULL,
          source_id TEXT NOT NULL,
          revision INTEGER NOT NULL,
          supersedes_fact_id TEXT,
          quality_json TEXT NOT NULL,
          raw_json TEXT NOT NULL,
          UNIQUE(source, source_id, revision)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_performance_fx_pair_time
        ON performance_fx_rate_facts(base_currency, quote_currency, effective_at_ms)
        """
    )


class PerformanceEvidenceSQLiteRepository:
    def __init__(self, db_path: str | Path):
        self.db_path = private_path(db_path)

    def schema_state(self) -> str:
        if not self.db_path.exists():
            return "not_initialized"
        try:
            with self._connect_readonly() as conn:
                tables = {
                    str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                }
                if "performance_evidence_schema" not in tables:
                    return "not_initialized"
                row = conn.execute(
                    "SELECT schema_version FROM performance_evidence_schema WHERE component = ?",
                    (_SCHEMA_COMPONENT,),
                ).fetchone()
                if row is None:
                    return "not_initialized"
                if int(row[0]) != _SCHEMA_VERSION:
                    return "unsupported_schema"
                required = {"performance_valuation_marks", "performance_fx_rate_facts"}
                return "initialized_v1" if required.issubset(tables) else "unsupported_schema"
        except sqlite3.DatabaseError:
            return "unsupported_schema"

    def read_all(self) -> EvidenceReadBundle:
        state = self.schema_state()
        if state != "initialized_v1":
            return EvidenceReadBundle(schema_state=state)
        try:
            with self._connect_readonly() as conn:
                return self._read_all_conn(conn)
        except (sqlite3.DatabaseError, ValueError, json.JSONDecodeError) as exc:
            return EvidenceReadBundle(schema_state="unsupported_schema", message=str(exc))

    def import_envelope(
        self,
        value: EvidenceEnvelope | dict[str, Any],
        *,
        apply: bool = False,
        migrated_at_ms: int,
    ) -> EvidenceImportResult:
        envelope = value if isinstance(value, EvidenceEnvelope) else parse_evidence_envelope(value)
        before = self.read_all()
        if before.schema_state == "unsupported_schema":
            raise ValueError(before.message or "unsupported evidence schema")
        validate_evidence_facts(
            envelope.valuation_marks,
            envelope.fx_rates,
            existing_marks=before.valuation_marks,
            existing_rates=before.fx_rates,
        )
        idempotent = _idempotent_count(before, envelope)
        if not apply:
            return EvidenceImportResult(
                applied=False,
                schema_state_before=before.schema_state,
                schema_state_after=before.schema_state,
                valuation_mark_count=len(envelope.valuation_marks),
                fx_rate_count=len(envelope.fx_rates),
                inserted_count=0,
                idempotent_count=idempotent,
                envelope=envelope,
            )

        conn = connect_private_sqlite(self.db_path, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            migrate_evidence_schema(conn, migrated_at_ms=int(migrated_at_ms))
            existing = self._read_all_conn(conn)
            validate_evidence_facts(
                envelope.valuation_marks,
                envelope.fx_rates,
                existing_marks=existing.valuation_marks,
                existing_rates=existing.fx_rates,
            )
            inserted = 0
            idempotent = 0
            for mark in envelope.valuation_marks:
                changed = _insert_mark(conn, mark)
                inserted += int(changed)
                idempotent += int(not changed)
            for rate in envelope.fx_rates:
                changed = _insert_rate(conn, rate)
                inserted += int(changed)
                idempotent += int(not changed)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
            secure_sqlite_artifacts(self.db_path)
        return EvidenceImportResult(
            applied=True,
            schema_state_before=before.schema_state,
            schema_state_after="initialized_v1",
            valuation_mark_count=len(envelope.valuation_marks),
            fx_rate_count=len(envelope.fx_rates),
            inserted_count=inserted,
            idempotent_count=idempotent,
            envelope=envelope,
        )

    def _connect_readonly(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)

    def _read_all_conn(self, conn: sqlite3.Connection) -> EvidenceReadBundle:
        marks = tuple(_mark_from_row(row) for row in conn.execute(_MARK_SELECT).fetchall())
        rates = tuple(_rate_from_row(row) for row in conn.execute(_FX_SELECT).fetchall())
        validate_evidence_facts(marks, rates)
        return EvidenceReadBundle("initialized_v1", valuation_marks=marks, fx_rates=rates)


_MARK_SELECT = """
SELECT fact_id, instrument_key, key_version, instrument_type, symbol, option_type,
       strike_text, expiration_ymd, multiplier_text, currency, price_text, mark_kind,
       effective_at_ms, observed_at_ms, source, source_id, revision,
       supersedes_fact_id, quality_json, raw_json
FROM performance_valuation_marks
ORDER BY effective_at_ms ASC, fact_id ASC
"""
_FX_SELECT = """
SELECT fact_id, base_currency, quote_currency, rate_text, rate_kind,
       effective_at_ms, observed_at_ms, source, source_id, revision,
       supersedes_fact_id, quality_json, raw_json
FROM performance_fx_rate_facts
ORDER BY effective_at_ms ASC, fact_id ASC
"""


def _mark_from_row(row: Iterable[Any]) -> ValuationMarkFact:
    values = list(row)
    instrument_key = str(values[1])
    instrument = (
        OptionInstrumentKey.decode(instrument_key)
        if instrument_key.startswith("option:v1|")
        else StockInstrumentKey.decode(instrument_key)
    )
    if int(values[2]) != 1:
        raise ValueError("unsupported valuation instrument key version")
    if str(values[3]) != ("option" if isinstance(instrument, OptionInstrumentKey) else "stock"):
        raise ValueError("valuation structured instrument_type mismatch")
    if str(values[4]) != instrument.symbol or str(values[9]) != instrument.currency:
        raise ValueError("valuation structured identity mismatch")
    if isinstance(instrument, OptionInstrumentKey):
        if (
            str(values[5]) != instrument.option_type
            or str(values[6]) != canonical_decimal_text(instrument.strike, field_name="strike")
            or str(values[7]) != instrument.expiration_ymd
            or str(values[8]) != canonical_decimal_text(instrument.multiplier, field_name="multiplier")
        ):
            raise ValueError("valuation structured option identity mismatch")
    payload = json.loads(str(values[19]))
    if not isinstance(payload, dict):
        raise ValueError("valuation raw_json must be an object")
    fact = ValuationMarkFact(
        fact_id=values[0],
        instrument=instrument,
        price=values[10],
        mark_kind=values[11],
        effective_at_ms=values[12],
        observed_at_ms=values[13],
        source=values[14],
        source_id=values[15],
        revision=values[16],
        supersedes_fact_id=values[17],
        quality=json.loads(str(values[18])),
        raw=payload.get("raw") or {},
    )
    if fact.normalized_payload() != payload:
        raise ValueError(f"valuation normalized payload mismatch: {fact.fact_id}")
    return fact


def _rate_from_row(row: Iterable[Any]) -> FXRateFact:
    values = list(row)
    payload = json.loads(str(values[12]))
    if not isinstance(payload, dict):
        raise ValueError("FX raw_json must be an object")
    fact = FXRateFact(
        fact_id=values[0],
        base_currency=values[1],
        quote_currency=values[2],
        rate=values[3],
        rate_kind=values[4],
        effective_at_ms=values[5],
        observed_at_ms=values[6],
        source=values[7],
        source_id=values[8],
        revision=values[9],
        supersedes_fact_id=values[10],
        quality=json.loads(str(values[11])),
        raw=payload.get("raw") or {},
    )
    if fact.normalized_payload() != payload:
        raise ValueError(f"FX normalized payload mismatch: {fact.fact_id}")
    return fact


def _insert_mark(conn: sqlite3.Connection, mark: ValuationMarkFact) -> bool:
    existing = conn.execute(
        "SELECT raw_json FROM performance_valuation_marks WHERE fact_id = ? OR (source = ? AND source_id = ? AND revision = ?)",
        (mark.fact_id, mark.source, mark.source_id, mark.revision),
    ).fetchone()
    payload_json = _json(mark.normalized_payload())
    if existing is not None:
        if str(existing[0]) != payload_json:
            raise ValueError(f"valuation evidence conflict: {mark.fact_id}")
        return False
    instrument = mark.instrument
    conn.execute(
        """
        INSERT INTO performance_valuation_marks(
          fact_id, instrument_key, key_version, instrument_type, symbol, option_type,
          strike_text, expiration_ymd, multiplier_text, currency, price_text, mark_kind,
          effective_at_ms, observed_at_ms, source, source_id, revision,
          supersedes_fact_id, quality_json, raw_json
        ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            mark.fact_id,
            mark.instrument_key,
            mark.instrument_type,
            instrument.symbol,
            instrument.option_type if isinstance(instrument, OptionInstrumentKey) else None,
            canonical_decimal_text(instrument.strike, field_name="strike")
            if isinstance(instrument, OptionInstrumentKey)
            else None,
            instrument.expiration_ymd if isinstance(instrument, OptionInstrumentKey) else None,
            canonical_decimal_text(instrument.multiplier, field_name="multiplier")
            if isinstance(instrument, OptionInstrumentKey)
            else None,
            instrument.currency,
            canonical_decimal_text(mark.price, field_name="price"),
            mark.mark_kind,
            mark.effective_at_ms,
            mark.observed_at_ms,
            mark.source,
            mark.source_id,
            mark.revision,
            mark.supersedes_fact_id,
            _json(dict(mark.quality)),
            payload_json,
        ),
    )
    return True


def _insert_rate(conn: sqlite3.Connection, rate: FXRateFact) -> bool:
    existing = conn.execute(
        "SELECT raw_json FROM performance_fx_rate_facts WHERE fact_id = ? OR (source = ? AND source_id = ? AND revision = ?)",
        (rate.fact_id, rate.source, rate.source_id, rate.revision),
    ).fetchone()
    payload_json = _json(rate.normalized_payload())
    if existing is not None:
        if str(existing[0]) != payload_json:
            raise ValueError(f"FX evidence conflict: {rate.fact_id}")
        return False
    conn.execute(
        """
        INSERT INTO performance_fx_rate_facts(
          fact_id, base_currency, quote_currency, rate_text, rate_kind,
          effective_at_ms, observed_at_ms, source, source_id, revision,
          supersedes_fact_id, quality_json, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rate.fact_id,
            rate.base_currency,
            rate.quote_currency,
            canonical_decimal_text(rate.rate, field_name="rate"),
            rate.rate_kind,
            rate.effective_at_ms,
            rate.observed_at_ms,
            rate.source,
            rate.source_id,
            rate.revision,
            rate.supersedes_fact_id,
            _json(dict(rate.quality)),
            payload_json,
        ),
    )
    return True


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _idempotent_count(existing: EvidenceReadBundle, envelope: EvidenceEnvelope) -> int:
    marks = {item.fact_id: item.normalized_payload() for item in existing.valuation_marks}
    rates = {item.fact_id: item.normalized_payload() for item in existing.fx_rates}
    return sum(marks.get(item.fact_id) == item.normalized_payload() for item in envelope.valuation_marks) + sum(
        rates.get(item.fact_id) == item.normalized_payload() for item in envelope.fx_rates
    )


__all__ = [
    "EvidenceImportResult",
    "EvidenceReadBundle",
    "PerformanceEvidenceSQLiteRepository",
    "migrate_evidence_schema",
]
