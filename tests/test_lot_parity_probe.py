"""Slice 1 (`lot-parity-probe`): red/green controls for the read-only comparator.

Every fixture is built through the real write path (``persist_manual_open_event``)
and only then degraded, so the "stored" side of every comparison is a payload the
publisher actually produces rather than one hand-written to match the probe.

The controls are deliberately one-per-face and one-per-C-class: a probe whose
classes can be conflated is worse than no probe, because it turns "the ledger is
missing an event" into "the projection is unfaithful".

Several controls exist for one reason only: the assertions they pin are invisible
on a store that still has its WAL sidecars, or that has never been degraded past
what the write path itself permits. Those fixtures (a settled copy with no
``-shm``/``-wal``, a row the account guard would refuse) are built by hand and
labelled as such at the point of use.

Which of the two a **write path** hands over is the SQLite build's business, not
the store's — measured, the last close leaves ``-shm``/``-wal`` behind on some
builds and deletes them on others — so no fixture below inherits that shape: it
either unlinks the sidecars or holds a connection open.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
import hashlib
import importlib
import json
from pathlib import Path
import sqlite3
import sys
import types

import pytest

import src.application.ledger.manual_trades as ledger_manual_trades
import src.application.ledger.repository as ledger_repository
from src.application.ledger.position_records import PositionLotRecord
from src.application.ledger.repository_common import _position_lot_storage_values
from src.application.ledger.lot_parity_probe import (
    C_ATTRIBUTION_CLASSES,
    DERIVED_COLUMNS,
    LIVE_READ_MODE,
    SETTLED_READ_MODE,
    WRITER_RAISES_COLUMNS_KEY,
    WRITER_RAISES_KEY,
    assert_report_path_outside_runtime_state,
    compare_column_face,
    compare_payload_face,
    derive_stored_row_columns,
    main as probe_main,
    run_lot_parity_probe,
    write_report,
)


def _build_green_store(tmp_path: Path) -> tuple[Path, Path]:
    """A store written by the real path, where the replay reproduces every row."""
    sqlite_path = tmp_path / "option_positions.sqlite3"
    data_config = tmp_path / "data.json"
    data_config.write_text(
        json.dumps({"option_positions": {"sqlite_path": str(sqlite_path)}}),
        encoding="utf-8",
    )
    repo = ledger_repository.SQLiteOptionPositionsRepository(sqlite_path)
    repo.data_config_path = data_config  # type: ignore[attr-defined]
    ledger_manual_trades.persist_manual_open_event(
        repo,
        broker="富途",
        account="lx",
        symbol="TSLA",
        option_type="put",
        side="short",
        contracts=1,
        currency="USD",
        strike=100.0,
        multiplier=100,
        expiration_ymd="2026-06-19",
        premium_per_share=1.23,
        opened_at_ms=1000,
    )
    return sqlite_path, data_config


def _probe_module() -> types.ModuleType:
    """The probe module object, for the controls that patch its globals.

    Fetched dynamically rather than with a local ``import`` statement, because of
    how this repository's generated dependency graph works: it counts **one edge
    per import statement**, and this file already imports names from the probe
    module at the top. A second, function-local spelling of that same import is
    not a new dependency — it would only make ``docs/DEPENDENCY_GRAPH.md`` stale
    (that generator documents that it does not see dynamic imports, which is the
    property being used here).
    """
    return importlib.import_module("src.application.ledger.lot_parity_probe")


def _stored_lot_id(sqlite_path: Path) -> str:
    conn = sqlite3.connect(sqlite_path)
    try:
        row = conn.execute("SELECT lot_id FROM position_lots").fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row[0])


def _stored_fields(sqlite_path: Path) -> dict[str, object]:
    conn = sqlite3.connect(sqlite_path)
    try:
        row = conn.execute("SELECT fields_json FROM position_lots").fetchone()
    finally:
        conn.close()
    assert row is not None
    return json.loads(str(row[0]))


def _replace_with_v351_legacy_payload(sqlite_path: Path) -> None:
    """Persist the flat payload emitted by the v3.5.15 publisher."""
    fields = _stored_fields(sqlite_path)
    contract = fields["contract_key"]
    assert isinstance(contract, dict)
    legacy = {
        "broker": contract["broker"],
        "account": contract["account"],
        "symbol": contract["underlying_symbol"],
        "option_type": contract["option_type"],
        "side": fields["position_side"],
        "contracts": fields["contracts_opened"],
        "contracts_open": fields["contracts_open"],
        "contracts_closed": fields["contracts_closed"],
        "currency": fields["currency"],
        "status": fields["status"],
        "strike": contract["strike"],
        "expiration_ymd": contract["expiration_ymd"],
        "multiplier": fields["multiplier"],
        "premium": float(fields["premium_open"]),
        "opened_at": fields["opened_at_ms"],
        "last_action_at": fields["opened_at_ms"],
        "position_id": "TSLA-20260619-100-PUT-SHORT-1",
        "position_key": fields["position_key"],
        "source_event_id": fields["open_event_id"],
        "cash_secured_amount": 10_000.0,
    }
    _mutate_fields(sqlite_path, lambda current: (current.clear(), current.update(legacy)))


def _settle_store(sqlite_path: Path) -> None:
    """Leave the store as a ``.backup`` copy: contents in the file, no sidecars.

    This is the input slice 1 is defined against (``production-readout.md``:
    ``.backup``, never a bare ``cp``). The unlink is why this is a helper rather
    than "write and walk away": whether a write path's last close leaves
    ``-shm``/``-wal`` behind is a property of the SQLite build, not of the store
    (sqlite.org/wal.html: the last connection checkpoints and deletes the WAL and
    its shared-memory file) — measured, the development build here keeps them and
    the CI runner deletes them — so a settled shape inherited from that close
    would be describing the build.
    """
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    finally:
        conn.close()
    for suffix in ("-wal", "-shm"):
        Path(f"{sqlite_path}{suffix}").unlink(missing_ok=True)


def _sidecar_sizes(sqlite_path: Path) -> tuple[int, int]:
    wal = Path(f"{sqlite_path}-wal")
    shm = Path(f"{sqlite_path}-shm")
    return (
        wal.stat().st_size if wal.exists() else 0,
        shm.stat().st_size if shm.exists() else 0,
    )


def _drop_table_triggers(sqlite_path: Path, table: str) -> None:
    """Drop every trigger that guards a write to ``table``.

    The fixtures below have to describe store shapes the *current* write path
    refuses (a payload without ``account``, a corrupt ledger payload). Those
    shapes are reachable only on a store written before today's guards, or damaged
    on disk — and the guards sit at three separate layers per table, so dropping
    them by name would make each fixture depend on today's guard inventory.
    """
    conn = sqlite3.connect(sqlite_path)
    try:
        names = [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
                (table,),
            ).fetchall()
        ]
        for name in names:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        conn.commit()
    finally:
        conn.close()


def _drop_account_guards(sqlite_path: Path) -> None:
    """Allow a legacy row whose column is NULL and whose payload predates the key.

    The write path raises for that payload (``repository_common.py``:218-221) and
    the DB guards reject it a second time, which is exactly why the probe's copy
    of the derivation has to notice it.
    """
    _drop_table_triggers(sqlite_path, "position_lots")


def _drop_trade_event_guards(sqlite_path: Path) -> None:
    """Allow a ledger row whose payload is not an object."""
    _drop_table_triggers(sqlite_path, "trade_events")


def _degrade_lot_table(sqlite_path: Path, *, with_carrier: bool = False) -> None:
    """Build the historical identity shape only for the read-only probe tests."""

    with sqlite3.connect(sqlite_path) as conn:
        conn.execute("ALTER TABLE position_lots RENAME COLUMN lot_id TO record_id")
        conn.execute("ALTER TABLE position_lots ADD COLUMN expiration INTEGER")
        conn.execute("UPDATE position_lots SET expiration = 1781827200000")
        if with_carrier:
            conn.execute("ALTER TABLE position_lots ADD COLUMN lot_id TEXT")
            conn.execute("UPDATE position_lots SET lot_id = record_id")


def _ledger_event_id(sqlite_path: Path) -> str:
    conn = sqlite3.connect(sqlite_path)
    try:
        row = conn.execute(
            "SELECT event_id FROM trade_events ORDER BY trade_time_ms ASC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row[0])


def _tamper(sqlite_path: Path, statements: list[tuple[str, tuple[object, ...]]]) -> None:
    conn = sqlite3.connect(sqlite_path)
    try:
        for sql, params in statements:
            conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _mutate_fields(
    sqlite_path: Path,
    mutate: Callable[[dict[str, object]], None],
) -> None:
    conn = sqlite3.connect(sqlite_path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(position_lots)")}
        identity = "lot_id" if "lot_id" in columns else "record_id"
        row = conn.execute(
            f"SELECT {identity}, fields_json FROM position_lots"
        ).fetchone()
        assert row is not None
        fields = json.loads(str(row[1]))
        mutate(fields)
        conn.execute(
            f"UPDATE position_lots SET fields_json = ? WHERE {identity} = ?",
            (json.dumps(fields, ensure_ascii=False, sort_keys=True), row[0]),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_stored_row(
    sqlite_path: Path,
    *,
    record_id: str,
    source_event_id: object,
    payload_source_event_id: object = None,
) -> None:
    """Insert a stored row the replay cannot produce, with a chosen source event id."""
    fields: dict[str, object] = {
        "contract_key": {"account": "lx", "underlying_symbol": "TSLA"},
        "status": "open",
    }
    if payload_source_event_id is not None:
        fields["open_event_id"] = payload_source_event_id
    _tamper(
        sqlite_path,
        [
            (
                """
                INSERT INTO position_lots (
                    lot_id, account, fields_json, source_event_id,
                    strike, multiplier, updated_at_ms
                ) VALUES (?, 'lx', ?, ?, NULL, NULL, 1)
                """,
                (
                    record_id,
                    json.dumps(fields, ensure_ascii=False, sort_keys=True),
                    source_event_id,
                ),
            )
        ],
    )


def _stage_a_crashed_writer(sqlite_path: Path, *, strike: object) -> None:
    """Leave the store exactly as a writer that died mid-commit leaves it.

    Everything here is SQLite's own output: the pages in the main file are the
    ones a real transaction wrote, and the ``-journal`` next to them is the
    rollback log SQLite wrote for that same transaction, header magic included.
    Nothing is hand-written — the "crash" is staged by copying the two files out
    of the live transaction and putting them back, rather than racing a SIGKILL,
    which makes the resulting state deterministic instead of timing-dependent.

    Three details are load-bearing:

    * the store is switched to ``journal_mode=DELETE`` and its ``-shm``/``-wal``
      are unlinked first, so the shape under test is the journal and not a WAL
      sidecar (which the probe already refuses to fall back around);
    * ``cache_spill=ON`` and the churn table are what make the cache evict:
      SQLite only finalizes the journal header (magic + record count) once it
      starts writing pages into the main file, and a connection that keeps the
      whole transaction in cache leaves a placeholder header that no reader would
      call hot. The row count is what puts enough dirty pages in
      the transaction to force that eviction (at 5000 rows SQLite still keeps the
      whole update in cache and the header stays a placeholder, so the assertion
      below would fire);
    * the assertion below is that finalization, so a fixture that quietly stopped
      producing a hot journal fails here instead of passing for the wrong reason.
    """
    sqlite_path = Path(sqlite_path)
    journal = Path(f"{sqlite_path}-journal")
    conn = sqlite3.connect(sqlite_path, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        for suffix in ("-wal", "-shm"):
            Path(f"{sqlite_path}{suffix}").unlink(missing_ok=True)
        conn.execute("PRAGMA cache_size=1")
        conn.execute("PRAGMA cache_spill=ON")
        conn.execute("CREATE TABLE IF NOT EXISTS probe_churn (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("DELETE FROM probe_churn")
        conn.executemany(
            "INSERT INTO probe_churn(v) VALUES (?)",
            [(f"r{i:05d}",) for i in range(10000)],
        )
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE position_lots SET strike = ?", (strike,))
        conn.execute("UPDATE probe_churn SET v = 'uncommitted'")
        # Reading the table back is what forces the dirty pages out to the file.
        conn.execute("SELECT count(*) FROM probe_churn WHERE v > 'r00000'")
        assert journal.read_bytes()[:8] == bytes.fromhex("d9d505f920a163d7"), (
            "the staged journal is not hot: SQLite did not reach the point where "
            "it finalizes the header, so this fixture would pass for the wrong reason"
        )
        staged = (sqlite_path.read_bytes(), journal.read_bytes())
    finally:
        conn.close()
    # The rollback that ``close()`` performs puts both files back to the committed
    # state; the copies take their place, so what is on disk is the crash.
    sqlite_path.write_bytes(staged[0])
    journal.write_bytes(staged[1])


def _leave_a_spent_persist_journal(sqlite_path: Path) -> None:
    """A settled store with the journal ``journal_mode=PERSIST`` leaves behind.

    PERSIST does not delete its journal at commit — it zeroes the header, which
    is how it says "this log is spent". The file stays, non-empty, on a store no
    writer is attached to, and it is the shape a blanket "any ``-journal`` blocks
    the fallback" rule would turn into a false refusal.
    """
    sqlite_path = Path(sqlite_path)
    conn = sqlite3.connect(sqlite_path, isolation_level=None)
    try:
        assert conn.execute("PRAGMA journal_mode=PERSIST").fetchone() == ("persist",)
        conn.execute("PRAGMA cache_size=1")
        conn.execute("PRAGMA cache_spill=ON")
        conn.execute("CREATE TABLE IF NOT EXISTS probe_churn (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("DELETE FROM probe_churn")
        conn.executemany(
            "INSERT INTO probe_churn(v) VALUES (?)",
            [(f"r{i:05d}",) for i in range(10000)],
        )
        conn.execute("UPDATE probe_churn SET v = 'settled'")
    finally:
        conn.close()
    for suffix in ("-wal", "-shm"):
        Path(f"{sqlite_path}{suffix}").unlink(missing_ok=True)
    journal = Path(f"{sqlite_path}-journal")
    assert journal.exists() and journal.stat().st_size > 0
    assert journal.read_bytes()[:8] == b"\0" * 8


def _attribution(report: dict[str, object]) -> dict[str, int]:
    attribution = report["c_attribution"]
    assert isinstance(attribution, dict)
    return {name: int(attribution[name]) for name in C_ATTRIBUTION_CLASSES}


def _faces(report: dict[str, object]) -> dict[str, dict[str, object]]:
    faces = report["faces"]
    assert isinstance(faces, dict)
    return faces  # type: ignore[return-value]


# --- green baseline ----------------------------------------------------------


def test_probe_is_green_on_an_untouched_store(tmp_path: Path) -> None:
    sqlite_path, _config = _build_green_store(tmp_path)

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    assert report["green"] is True
    assert report["difference_count"] == 0
    assert report["tier"] == "tier-1"
    assert report["stored_lot_count"] == 1
    assert report["projected_lot_count"] == 1
    assert report["event_count"] == 1
    faces = _faces(report)
    assert faces["a_payload"]["difference_count"] == 0
    assert faces["b_columns"]["difference_count"] == 0
    assert faces["c_rows"]["difference_count"] == 0
    assert report["excluded_payload_keys"] == ["updated_at_ms"]
    assert _attribution(report) == {
        "null_source_event_id": 0,
        "ledger_missing_row": 0,
        "projection_omission": 0,
        "other": 0,
    }


def test_probe_reports_replay_diagnostics_beside_green_without_changing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replay error is a diagnostic, not a face difference.

    ``green`` is the three faces and nothing else — the cadence's question is
    "does the replay reproduce the stored rows", and a replay that emitted an
    error while producing the same rows has not answered that question *no*. The
    diagnostics are reported beside the verdict, so neither reading is lost.
    """
    probe_module = _probe_module()

    sqlite_path, _config = _build_green_store(tmp_path)
    real_projection = probe_module.project_stored_trade_events_to_position_lots

    class _ErrorDiagnostic:
        """The one attribute the probe reads off a diagnostic.

        Deliberately not ``LedgerDiagnostic``: importing that domain type here
        would add a ``tests -> domain`` edge to the generated dependency graph for
        one attribute, and the probe's own read is a ``getattr(item, "severity")``.
        """

        severity = "error"

    class _ProjectionWithAnError:
        def __init__(self, projection: object) -> None:
            self._projection = projection

        @property
        def lots(self) -> object:
            return self._projection.lots  # type: ignore[attr-defined]

        @property
        def diagnostics(self) -> list[object]:
            return [
                *self._projection.diagnostics,  # type: ignore[attr-defined]
                _ErrorDiagnostic(),
            ]

    monkeypatch.setattr(
        probe_module,
        "project_stored_trade_events_to_position_lots",
        lambda events: _ProjectionWithAnError(real_projection(events)),
    )

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    assert report["projection_error_count"] == 1
    assert report["projection_diagnostic_count"] == 1
    assert report["green"] is True
    assert report["difference_count"] == 0


# --- face A: payload ---------------------------------------------------------


def test_probe_accepts_v351_flat_payload_and_still_detects_fact_drift(tmp_path: Path) -> None:
    sqlite_path, _config = _build_green_store(tmp_path)
    _replace_with_v351_legacy_payload(sqlite_path)

    assert run_lot_parity_probe(sqlite_path=sqlite_path)["green"] is True

    _mutate_fields(sqlite_path, lambda fields: fields.__setitem__("premium", 9.99))
    report = run_lot_parity_probe(sqlite_path=sqlite_path)
    assert report["green"] is False
    assert _faces(report)["a_payload"]["items"][0]["value_differences"] == [
        {"key": "premium", "stored": "9.99", "projected": "1.23"}
    ]


@pytest.mark.parametrize(
    ("legacy_premium", "canonical_premium"),
    [
        (1.8399999999999999, "1.84"),
        (1.5699999999999998, "1.57"),
        (1.6099999999999999, "1.61"),
    ],
)
def test_cross_shape_comparison_uses_the_money_contract(
    legacy_premium: float, canonical_premium: str,
) -> None:
    stored = {"premium": legacy_premium}
    projected = {"contract_key": {}, "premium_open": canonical_premium}

    assert compare_payload_face(stored_fields=stored, projected_fields=projected)["differs"] is False

    stored["premium"] = float(canonical_premium) + 0.01
    assert compare_payload_face(stored_fields=stored, projected_fields=projected)["differs"] is True


@pytest.mark.parametrize(
    ("legacy_key", "bad_value", "fact"),
    [
        ("account", "sy", "account"),
        ("source_event_id", "wrong-event", "source_event_id"),
        ("side", "long", "side"),
        ("contracts", 2, "contracts"),
    ],
)
def test_cross_shape_comparison_keeps_business_fact_drift_red(
    tmp_path: Path, legacy_key: str, bad_value: object, fact: str,
) -> None:
    sqlite_path, _config = _build_green_store(tmp_path)
    _replace_with_v351_legacy_payload(sqlite_path)
    if legacy_key == "account":
        _drop_account_guards(sqlite_path)
    _mutate_fields(sqlite_path, lambda fields: fields.__setitem__(legacy_key, bad_value))

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    assert report["green"] is False
    assert fact in {
        item["key"]
        for item in _faces(report)["a_payload"]["items"][0]["value_differences"]
    }


@pytest.mark.parametrize(
    "fact",
    ["shares_opened", "shares_open", "shares_closed", "cost_basis_total"],
)
def test_cross_shape_comparison_keeps_stock_fact_drift_red(fact: str) -> None:
    stored = {fact: "10"}
    projected = {"contract_key": {}, fact: "11"}

    detail = compare_payload_face(stored_fields=stored, projected_fields=projected)

    assert detail["value_differences"] == [
        {"key": fact, "stored": "1E+1", "projected": "11"}
    ]


def test_same_shape_comparison_keeps_fee_derived_realized_pnl_strict(tmp_path: Path) -> None:
    sqlite_path, _config = _build_green_store(tmp_path)
    _mutate_fields(sqlite_path, lambda fields: fields.__setitem__("realized_pnl", "1.000000"))

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    assert report["green"] is False
    assert _faces(report)["a_payload"]["items"][0]["value_differences"] == [
        {"key": "realized_pnl", "stored": "1.000000", "projected": "0"}
    ]


def test_probe_face_a_reports_a_key_set_difference(tmp_path: Path) -> None:
    sqlite_path, _config = _build_green_store(tmp_path)
    lot_id = _stored_lot_id(sqlite_path)
    _mutate_fields(sqlite_path, lambda fields: fields.__setitem__("probe_only_key", "x"))

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    faces = _faces(report)
    assert report["green"] is False
    assert faces["a_payload"]["key_set_difference_lot_count"] == 1
    assert faces["a_payload"]["value_difference_count"] == 0
    assert faces["b_columns"]["difference_count"] == 0
    assert faces["c_rows"]["difference_count"] == 0
    item = faces["a_payload"]["items"][0]
    assert item["lot_id"] == lot_id
    assert item["keys_only_in_store"] == ["probe_only_key"]
    assert item["keys_only_in_projection"] == []


def test_probe_face_a_reports_a_value_difference(tmp_path: Path) -> None:
    sqlite_path, _config = _build_green_store(tmp_path)

    def _reprice(fields: dict[str, object]) -> None:
        fields["premium_open"] = "9.99"

    _mutate_fields(sqlite_path, _reprice)

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    faces = _faces(report)
    assert report["green"] is False
    assert faces["a_payload"]["key_set_difference_lot_count"] == 0
    assert faces["a_payload"]["value_difference_count"] == 1
    assert faces["b_columns"]["difference_count"] == 0
    difference = faces["a_payload"]["items"][0]["value_differences"][0]
    assert difference["key"] == "premium_open"
    assert difference["stored"] == "9.99"
    assert difference["projected"] == "1.23"


def test_probe_face_a_does_not_heal_the_stored_payload(tmp_path: Path) -> None:
    """The deleted column-heal must not be applied to either side.

    ``multiplier`` leaves the payload while its column keeps the true value. A
    healed stored side would put ``multiplier=100`` back from the column and call
    face A matched; the probe must instead report the key and leave the column to
    face B. ``multiplier`` is the one derived scalar that stays a top-level key in
    the converged shape (``strike``/``expiration`` moved under ``contract_key``),
    so it is also the one whose absence face A can still see.
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    _mutate_fields(sqlite_path, lambda fields: fields.pop("multiplier"))

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    faces = _faces(report)
    assert faces["a_payload"]["key_set_difference_lot_count"] == 1
    assert faces["a_payload"]["items"][0]["keys_only_in_projection"] == ["multiplier"]
    # And the same fact is *also* a face B difference, because the column was
    # never re-derived: this is comparator-spec §6.3's "both", counted twice.
    assert faces["b_columns"]["by_column"]["multiplier"] == 1
    assert faces["b_columns"]["by_column"]["strike"] == 0
    assert faces["b_columns"]["items"][0]["stored"] == 100.0
    assert faces["b_columns"]["items"][0]["derived_from_stored_payload"] is None


def test_probe_face_a_ignores_updated_at_ms(tmp_path: Path) -> None:
    """``updated_at_ms`` is a wall clock, so a difference on it is not a difference."""
    sqlite_path, _config = _build_green_store(tmp_path)
    _mutate_fields(sqlite_path, lambda fields: fields.__setitem__("updated_at_ms", 42))

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    assert report["green"] is True
    assert _faces(report)["a_payload"]["difference_count"] == 0


def test_probe_refuses_a_stored_payload_that_is_not_an_object(tmp_path: Path) -> None:
    """An empty payload reads as empty; a non-object payload is loud.

    The read side (``sqlite_row_codec.position_lot_row_to_record``) reads a NULL /
    absent payload as ``{}``, so face A must not call the same shape "malformed" —
    and, symmetrically, the projected side must not answer a non-object payload
    with ``{}``. Both sides of one comparison, one vocabulary per shape.
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    _drop_account_guards(sqlite_path)
    _tamper(sqlite_path, [("UPDATE position_lots SET fields_json = '[1,2]'", ())])

    with pytest.raises(ValueError, match="stored position lot payload must be an object"):
        run_lot_parity_probe(sqlite_path=sqlite_path)


def test_probe_does_not_shrug_at_a_projected_payload_that_is_not_an_object() -> None:
    """The projected side is the other half of the same asymmetry.

    ``_projected_lot_row`` used to coerce a non-dict ``fields`` to ``{}`` — the
    one silent answer in a comparison whose stored side raises for the same shape.
    """
    import src.application.ledger.lot_parity_probe as probe_module

    assert probe_module._projected_lot_row({"lot_id": "lot_x", "fields": None})["fields"] == {}
    with pytest.raises(ValueError, match="projected position lot fields must be an object"):
        probe_module._projected_lot_row({"lot_id": "lot_x", "fields": [1, 2]})
    assert probe_module._decode_fields_json(None, lot_id="lot_x") == {}
    assert probe_module._decode_fields_json("", lot_id="lot_x") == {}


# --- face B: derived columns -------------------------------------------------


def test_probe_column_comparison_uses_the_published_tolerance() -> None:
    """``_TOLERANCE = 1e-9`` is a number the production readout leans on.

    The readout's residual money cases differ in the last float bit (≤ 4.44e-16)
    and are *not* face-B differences because of this constant; the plan states
    that reading as the reason they are not a third bucket. Nothing else in this
    file, or in the repository, mentions the constant: tightening it to exact
    equality (or to 1e-18) leaves every other control green.
    """
    nothing_derived = {column: None for column in DERIVED_COLUMNS}
    nothing_derived[WRITER_RAISES_KEY] = None
    nothing_derived[WRITER_RAISES_COLUMNS_KEY] = ()
    stored = dict(nothing_derived, strike=1.0)

    below = dict(nothing_derived, strike=1.0 + 4.44e-16)
    assert compare_column_face(stored_columns=stored, derived_columns=below) == []

    above = dict(nothing_derived, strike=1.0 + 1e-6)
    assert [
        item["column"]
        for item in compare_column_face(stored_columns=stored, derived_columns=above)
    ] == ["strike"]


def test_probe_face_b_reports_a_column_difference(tmp_path: Path) -> None:
    sqlite_path, _config = _build_green_store(tmp_path)
    _tamper(
        sqlite_path,
        [("UPDATE position_lots SET multiplier = 7.0", ())],
    )

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    faces = _faces(report)
    assert report["green"] is False
    assert faces["a_payload"]["difference_count"] == 0
    assert faces["b_columns"]["difference_count"] == 1
    assert faces["b_columns"]["by_column"]["multiplier"] == 1
    assert faces["b_columns"]["by_column"]["account"] == 0
    assert faces["b_columns"]["by_column"]["expiration"] == 0
    assert faces["b_columns"]["by_column"]["strike"] == 0
    assert faces["b_columns"]["by_column"]["source_event_id"] == 0
    assert faces["c_rows"]["difference_count"] == 0
    item = faces["b_columns"]["items"][0]
    assert item["column"] == "multiplier"
    assert item["stored"] == 7.0
    assert item["derived_from_stored_payload"] == 100.0


def _writer_columns(fields: dict[str, object], *, lot_id: str) -> dict[str, object]:
    """The five derived columns as **the writer** computes them, for the same payload."""
    values = _position_lot_storage_values(PositionLotRecord(lot_id=lot_id, fields=dict(fields)))
    # (lot_id, account, fields_json, source_event_id, expiration, strike, multiplier)
    return {
        "account": values[1],
        "source_event_id": values[3],
        "expiration": values[4],
        "strike": values[5],
        "multiplier": values[6],
    }


def _probe_side_of(message: str, *, lot_id: str) -> str:
    """The writer's message as the probe states it — without the lot id it embeds.

    The probe's derivation is a function of a payload, so it has no lot id to
    name; both of its copies drop the id for the same reason, and the face-B item
    carries the lot id anyway.
    """
    return message.replace(f": record_id={lot_id}", "").replace(f" {lot_id}", "")


def _writer_named_columns(message: str, *, lot_id: str) -> tuple[str, ...]:
    """The derived columns the writer's own refusal message names.

    ``WRITER_RAISES_COLUMNS_KEY`` is the probe's restatement of the writer's
    wording, and the writer exposes no column tuple to compare against: the only
    thing it publishes is the message. So the binding is "the probe covers exactly
    the columns the writer's message names" — the writer says "missing
    expiration, strike" or "account is required", and both are read off the same
    text the probe's message is already compared against.
    """
    text = _probe_side_of(message, lot_id=lot_id)
    return tuple(column for column in DERIVED_COLUMNS if column in text)


def _nested(payload: dict[str, object]) -> dict[str, object]:
    """A deep-enough copy for editing the nested ``contract_key`` in place.

    The ``dict(stored)`` copies the rest of this file uses are shallow: enough for
    a top-level key, but editing ``copied["contract_key"]["strike"]`` would edit
    the object the green store's payload is still sharing.
    """
    copied = deepcopy(payload)
    contract = copied.get("contract_key")
    if not isinstance(contract, dict):
        copied["contract_key"] = {}
    return copied


def test_probe_derivation_is_bound_to_the_writers_own_derivation(tmp_path: Path) -> None:
    """Face B must use the writer's derivation, not a second opinion that can drift.

    The probe restates ``account``/``source_event_id`` and the three casts instead
    of importing all of them. This test *is* the binding: for the same payload, the
    probe's derived columns must equal the values ``_position_lot_storage_values``
    hands the INSERT, and where the writer **refuses** the payload the probe's
    refusal must be the writer's own — message, precedence *and* the columns the
    message names — instead of ``None``, which would compare equal to a NULL
    column.

    Every payload here is in the converged shape (``PositionLot.to_dict()``): the
    contract lives under ``contract_key`` (``account``/``option_type``/``strike``/
    ``expiration_ymd``), the source event id is ``open_event_id``, and
    ``multiplier`` is the only derived scalar still at the top level.

    The set is built to cover the writer's whole guard surface, not just the
    ``account`` pair: the option-contract validation (``_validate_position_lot_fields``)
    runs first, it applies only to ``put``/``call`` payloads, its ``strike`` read
    has no note fallback, and a payload that is both incomplete and account-less
    is refused for the contract — all of which the probe has to reproduce. The
    boundary payloads at the end are the ones where the writer's guard is a
    truthiness test rather than a type test: ``expiration_ymd``/``strike`` of ``0``
    and a boolean ``strike`` are *present* to ``in (None, "")``/``safe_float`` and
    must be derived the same way on both sides. And the note is read by neither
    side any more: a note carrying ``multiplier=100`` no longer becomes a column.
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    stored = _stored_fields(sqlite_path)
    lot_id = _stored_lot_id(sqlite_path)

    payloads: dict[str, dict[str, object]] = {"stored payload": stored}

    note_only_scalars = dict(stored)
    note_only_scalars["note"] = "exp=2026-06-19; multiplier=100; strike=100"
    note_only_scalars.pop("multiplier", None)
    payloads["multiplier only in the note (fallback retired)"] = note_only_scalars

    no_source_event = dict(stored)
    no_source_event.pop("open_event_id", None)
    payloads["no open_event_id"] = no_source_event

    missing_contract = _nested(stored)
    missing_contract["contract_key"].pop("strike", None)
    missing_contract["contract_key"].pop("expiration_ymd", None)
    payloads["call/put missing strike and expiration"] = missing_contract

    missing_strike = _nested(stored)
    missing_strike["contract_key"].pop("strike", None)
    payloads["call/put missing strike"] = missing_strike

    missing_expiration = _nested(stored)
    missing_expiration["contract_key"].pop("expiration_ymd", None)
    payloads["call/put missing expiration"] = missing_expiration

    empty_expiration = _nested(stored)
    empty_expiration["contract_key"]["expiration_ymd"] = ""
    payloads["call/put with an empty expiration"] = empty_expiration

    shouted = _nested(missing_strike)
    shouted["contract_key"]["option_type"] = "  CALL  "
    payloads["option type needing normalisation"] = shouted

    note_strike = _nested(missing_strike)
    note_strike["note"] = "strike=100; multiplier=100"
    payloads["missing strike with a strike in the note"] = note_strike

    no_option_type = _nested(missing_strike)
    no_option_type.pop("contract_key", None)
    payloads["payload with no contract key"] = no_option_type

    stock = _nested(missing_strike)
    stock["contract_key"]["option_type"] = "stock"
    payloads["stock payload with no option contract"] = stock

    contract_before_account = _nested(missing_strike)
    contract_before_account["contract_key"]["account"] = ""
    payloads["incomplete contract and no account"] = contract_before_account

    account_missing = _nested(stored)
    account_missing["contract_key"].pop("account", None)
    payloads["no account"] = account_missing

    account_uppercase = _nested(stored)
    account_uppercase["contract_key"]["account"] = "LX"
    payloads["uppercase account"] = account_uppercase

    zero_expiration = _nested(stored)
    zero_expiration["contract_key"]["expiration_ymd"] = 0
    payloads["expiration_ymd 0"] = zero_expiration

    zero_strike = _nested(stored)
    zero_strike["contract_key"]["strike"] = 0
    payloads["strike 0"] = zero_strike

    boolean_strike = _nested(stored)
    boolean_strike["contract_key"]["strike"] = True
    payloads["strike True"] = boolean_strike

    # Keeping the ledger/derivation split honest: the payloads that pass the
    # writer's guards must also match column by column, and the rest must match
    # refusal by refusal — message and covered columns both.
    for label, payload in payloads.items():
        derived = derive_stored_row_columns(payload)
        try:
            writer_values = _writer_columns(payload, lot_id=lot_id)
        except ValueError as exc:
            refusal = derived[WRITER_RAISES_KEY]
            assert isinstance(refusal, str), label
            assert refusal == _probe_side_of(str(exc), lot_id=lot_id), label
            assert derived[WRITER_RAISES_COLUMNS_KEY] == _writer_named_columns(
                str(exc), lot_id=lot_id
            ), label
            continue
        assert derived[WRITER_RAISES_KEY] is None, label
        assert {column: derived[column] for column in DERIVED_COLUMNS} == writer_values, label


def test_probe_derivation_reports_the_writers_fail_fast_instead_of_equality(
    tmp_path: Path,
) -> None:
    """``account`` is the one derived column the writer refuses to guess at.

    Bound to the writer's own rule in both directions: whenever the writer raises
    for ``account``, the probe's derivation must carry the refusal instead of
    returning ``None`` (which would compare equal to a NULL column).
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    stored = _stored_fields(sqlite_path)
    lot_id = _stored_lot_id(sqlite_path)

    missing = _nested(stored)
    missing["contract_key"].pop("account", None)
    uppercase = _nested(stored)
    uppercase["contract_key"]["account"] = "LX"

    for payload in (missing, uppercase):
        with pytest.raises(ValueError):
            _position_lot_storage_values(PositionLotRecord(lot_id=lot_id, fields=payload))
        assert derive_stored_row_columns(payload)[WRITER_RAISES_KEY] is not None
    assert derive_stored_row_columns(stored)[WRITER_RAISES_KEY] is None


def test_probe_face_b_reports_a_payload_the_writer_would_refuse(tmp_path: Path) -> None:
    """A legacy row the write path would reject must not pass as "both are NULL".

    The column is NULL and the payload has no ``account``: the writer's
    derivation raises (``repository_common.py``:218-221), and the probe's copy of
    it used to answer ``None`` on both sides — a silent equality on a row the
    writer cannot reproduce. The guards are dropped because the *current* write
    path refuses to create this row; it is exactly the pre-guard shape the probe
    has to notice.
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    _drop_account_guards(sqlite_path)
    fields = _nested(_stored_fields(sqlite_path))
    fields["contract_key"].pop("account", None)
    _tamper(
        sqlite_path,
        [
            (
                "UPDATE position_lots SET account = NULL, fields_json = ?",
                (json.dumps(fields, ensure_ascii=False, sort_keys=True),),
            )
        ],
    )

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    faces = _faces(report)
    assert report["green"] is False
    assert faces["b_columns"]["difference_count"] == 1
    assert faces["b_columns"]["writer_raises_count"] == 1
    item = faces["b_columns"]["items"][0]
    assert item["column"] == "account"
    assert item["stored"] is None
    assert item["derived_from_stored_payload"] is None
    assert item["writer_raises"] == "position lot account is required"


def test_probe_face_b_reports_an_incomplete_option_contract_as_a_refusal(
    tmp_path: Path,
) -> None:
    """The writer's *first* refusal is the contract, and it is not an account fact.

    A ``put``/``call`` payload that lost its ``strike`` is refused by
    ``_validate_position_lot_fields`` before the account rules are reached. Face B
    used to model only the account pair, so this row came out *equal* — the column
    is NULL and the derivation answered ``None`` for the missing field — and the
    report called green a row the writer could never have written.
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    fields = _nested(_stored_fields(sqlite_path))
    fields["contract_key"].pop("strike", None)
    # Only the payload loses the key: the column keeps the value the writer
    # derived from it, which is the shape the old face B read as "nothing to say".
    _tamper(
        sqlite_path,
        [
            (
                "UPDATE position_lots SET fields_json = ?",
                (json.dumps(fields, ensure_ascii=False, sort_keys=True),),
            )
        ],
    )

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    faces = _faces(report)
    assert report["green"] is False
    assert faces["b_columns"]["writer_raises_count"] == 1
    # One refusal, one difference — and the columns it names are not compared a
    # second time against a derivation that does not exist.
    assert faces["b_columns"]["difference_count"] == 1
    item = faces["b_columns"]["items"][0]
    assert item["column"] == "strike"
    assert item["stored"] == 100.0
    assert item["derived_from_stored_payload"] is None
    assert item["writer_raises"] == "incomplete option position lot: missing strike"


def test_probe_derivation_names_the_columns_each_refusal_covers() -> None:
    """``WRITER_RAISES_COLUMNS_KEY`` is read by the histogram: bind its value.

    The key has one consumer (``compare_column_face`` skips the columns it names)
    and one effect (``b_columns.by_column`` counts them), and neither is the
    key's *value* — a tuple that answered ``()`` or ``("account",)`` for every
    refusal would leave every other control in this file green.
    """
    missing_expiration = {
        "contract_key": {"account": "lx", "option_type": "put", "strike": 100.0}
    }
    assert derive_stored_row_columns(missing_expiration)[WRITER_RAISES_COLUMNS_KEY] == (
        "expiration",
    )

    missing_both = {"contract_key": {"account": "lx", "option_type": "put"}}
    assert derive_stored_row_columns(missing_both)[WRITER_RAISES_COLUMNS_KEY] == (
        "expiration",
        "strike",
    )

    no_account = {
        "contract_key": {"option_type": "put", "expiration_ymd": "2026-06-19", "strike": 1.0}
    }
    assert derive_stored_row_columns(no_account)[WRITER_RAISES_COLUMNS_KEY] == ("account",)

    # Nothing refused: the tuple is empty, so the histogram falls back to the
    # item's own column.
    assert derive_stored_row_columns(
        {"contract_key": {**no_account["contract_key"], "account": "lx"}}
    )[WRITER_RAISES_COLUMNS_KEY] == ()


def test_probe_face_b_counts_a_two_column_refusal_per_column(tmp_path: Path) -> None:
    """A refusal naming two columns is one item and two column entries.

    The writer's contract guard names every missing field in one message
    (``missing expiration, strike``). Face B reports that as **one** difference —
    one root cause, one item, and the detail list is one item per root cause on
    purpose — but ``by_column`` is a histogram of columns: with the count read off
    the item's own ``column`` alone, ``strike`` reported ``0`` for this shape
    while the refusal plainly named it, so a reader of the histogram saw a column
    the writer refused as clean.
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    _degrade_lot_table(sqlite_path, with_carrier=True)
    fields = _nested(_stored_fields(sqlite_path))
    fields["contract_key"].pop("strike", None)
    fields["contract_key"].pop("expiration_ymd", None)
    _tamper(
        sqlite_path,
        [
            (
                "UPDATE position_lots SET fields_json = ?",
                (json.dumps(fields, ensure_ascii=False, sort_keys=True),),
            )
        ],
    )

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    columns = _faces(report)["b_columns"]
    assert columns["writer_raises_count"] == 1
    assert columns["difference_count"] == 1
    assert columns["by_column"] == {
        "account": 0,
        "expiration": 1,
        "strike": 1,
        "multiplier": 0,
        "source_event_id": 0,
    }
    assert len(columns["items"]) == 1
    item = columns["items"][0]
    assert item["column"] == "expiration"
    assert item["writer_raises"] == "incomplete option position lot: missing expiration, strike"
    assert item[WRITER_RAISES_COLUMNS_KEY] == ["expiration", "strike"]


def test_probe_face_b_does_not_refuse_a_payload_the_writer_leaves_alone(
    tmp_path: Path,
) -> None:
    """Same missing ``strike``, no option contract: the writer does not validate it.

    The contract guard returns early for anything that is not ``put``/``call``, so
    a stock payload missing ``strike`` is the writer's business only through the
    ``account`` guard. A probe that validated every payload would refuse rows the
    writer happily writes.
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    fields = _nested(_stored_fields(sqlite_path))
    fields["contract_key"].pop("strike", None)
    fields["contract_key"]["option_type"] = "stock"
    _tamper(
        sqlite_path,
        [
            (
                "UPDATE position_lots SET fields_json = ?",
                (json.dumps(fields, ensure_ascii=False, sort_keys=True),),
            )
        ],
    )

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    faces = _faces(report)
    assert faces["b_columns"]["writer_raises_count"] == 0
    # The same missing key is still a face-B difference — the column was not
    # re-derived — but it is a column difference, and nothing more.
    assert faces["b_columns"]["by_column"]["strike"] == 1
    assert "writer_raises" not in faces["b_columns"]["items"][0]


# --- face C: row set ---------------------------------------------------------


def test_probe_face_c_reports_an_extra_stored_row(tmp_path: Path) -> None:
    sqlite_path, _config = _build_green_store(tmp_path)
    ledger_event_id = _ledger_event_id(sqlite_path)
    _insert_stored_row(
        sqlite_path,
        record_id="lot_probe_extra",
        source_event_id=ledger_event_id,
    )

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    faces = _faces(report)
    assert report["green"] is False
    assert faces["a_payload"]["difference_count"] == 0
    assert faces["b_columns"]["difference_count"] == 0
    assert faces["c_rows"]["extra_in_store_count"] == 1
    assert faces["c_rows"]["missing_in_store_count"] == 0
    assert faces["c_rows"]["lot_id_set_difference_count"] == 1
    assert faces["c_rows"]["count_mismatch"] is True
    # ``count_mismatch`` is a restatement of this count, not a third difference:
    # one differing row reads ``c_rows=1``. Counting the boolean adds one, and no
    # other control in this file notices.
    assert faces["c_rows"]["difference_count"] == 1
    assert report["stored_lot_count"] == 2
    assert report["projected_lot_count"] == 1


def test_probe_face_c_reports_a_missing_stored_row(tmp_path: Path) -> None:
    sqlite_path, _config = _build_green_store(tmp_path)
    _tamper(sqlite_path, [("DELETE FROM position_lots", ())])

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    faces = _faces(report)
    assert faces["c_rows"]["missing_in_store_count"] == 1
    assert faces["c_rows"]["extra_in_store_count"] == 0
    assert faces["c_rows"]["count_mismatch"] is True
    assert faces["c_rows"]["items"][0]["status"] == "missing_in_store"
    # Not a ledger gap: the projection produced the row, the store lost it.
    assert _attribution(report) == {
        "null_source_event_id": 0,
        "ledger_missing_row": 0,
        "projection_omission": 0,
        "other": 1,
    }


def test_probe_face_c_detects_a_duplicate_identity(tmp_path: Path) -> None:
    """Two rows collapsing onto one identity is what the old instrument silently lost.

    A unique index pins the ``lot_id`` column, so the collision has to come from
    the carrier-or-fallback rule the two existing read surfaces share: one row
    carries ``lot_probe_shared`` while a second, carrier-less row falls back to
    the same string through ``record_id``.
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    _degrade_lot_table(sqlite_path, with_carrier=True)
    _tamper(
        sqlite_path,
        [
            (
                """
                INSERT INTO position_lots (
                    record_id, account, fields_json, source_event_id,
                    expiration, strike, multiplier, updated_at_ms, lot_id
                ) VALUES ('probe_carrier_row', 'lx', '{"account":"lx"}', NULL,
                          NULL, NULL, NULL, 1, 'lot_probe_shared')
                """,
                (),
            ),
            (
                """
                INSERT INTO position_lots (
                    record_id, account, fields_json, source_event_id,
                    expiration, strike, multiplier, updated_at_ms, lot_id
                ) VALUES ('lot_probe_shared', 'lx', '{"account":"lx"}', NULL,
                          NULL, NULL, NULL, 1, NULL)
                """,
                (),
            ),
        ],
    )

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    faces = _faces(report)
    assert faces["c_rows"]["duplicate_identity_count"] == 1
    duplicate = next(
        item
        for item in faces["c_rows"]["items"]
        if item["status"] == "duplicate_identity"
    )
    assert duplicate == {
        "status": "duplicate_identity",
        "side": "store",
        "lot_id": "lot_probe_shared",
        "occurrences": 2,
    }
    assert faces["c_rows"]["count_mismatch"] is True
    # Two C differences — one extra identity and one collapse — and not three:
    # the row count differs *because* of those two, and the boolean that says so
    # is not counted again. Nothing else in this file reads this number.
    assert faces["c_rows"]["difference_count"] == 2


def test_probe_index_keeps_the_first_row_of_a_repeated_identity() -> None:
    """Faces A and B compare the first row of a collapsed identity, not the last.

    "The second row of a collapsed pair is the same identity read twice, not a
    second opinion about the payload" is a *selection* rule, and a selection rule
    nothing pins is a coin flip: every fixture in this file builds its collapsed
    rows with the same payload, so swapping ``setdefault`` for an assignment
    changes which payload is compared and no store-shaped control notices.
    """
    probe_module = _probe_module()

    first = {"lot_id": "lot_x", "fields": {"premium": "1.23"}, "columns": {}}
    second = {"lot_id": "lot_x", "fields": {"premium": "9.99"}, "columns": {}}

    index, duplicates = probe_module._index_by_identity([first, second])

    assert index == {"lot_x": first}
    assert duplicates == [("lot_x", 2)]


# --- C-face attribution: four classes, never conflated -----------------------


def test_probe_attributes_an_empty_source_event_id_without_calling_it_a_ledger_gap(
    tmp_path: Path,
) -> None:
    sqlite_path, _config = _build_green_store(tmp_path)
    _insert_stored_row(
        sqlite_path,
        record_id="lot_probe_no_source",
        source_event_id=None,
        payload_source_event_id=None,
    )

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    assert _attribution(report) == {
        "null_source_event_id": 1,
        "ledger_missing_row": 0,
        "projection_omission": 0,
        "other": 0,
    }
    item = report["c_attribution"]["items"][0]
    assert item["attribution"] == "null_source_event_id"
    assert item["source_event_id"] is None
    assert item["source_event_id_source"] == "absent"


def test_probe_attributes_a_missing_ledger_event_as_ledger_missing_row(tmp_path: Path) -> None:
    sqlite_path, _config = _build_green_store(tmp_path)
    _insert_stored_row(
        sqlite_path,
        record_id="lot_probe_no_event",
        source_event_id="manual-open-does-not-exist",
    )

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    attribution = _attribution(report)
    assert attribution["ledger_missing_row"] == 1
    # Not a NULL id, and not projection infidelity: that distinction is the fix.
    assert attribution["null_source_event_id"] == 0
    assert attribution["projection_omission"] == 0
    assert attribution["other"] == 0
    assert report["c_attribution"]["items"][0]["attribution"] == "ledger_missing_row"


def test_probe_attributes_an_existing_ledger_event_as_projection_omission(tmp_path: Path) -> None:
    sqlite_path, _config = _build_green_store(tmp_path)
    ledger_event_id = _ledger_event_id(sqlite_path)
    _insert_stored_row(
        sqlite_path,
        record_id="lot_probe_omitted",
        source_event_id=ledger_event_id,
    )

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    attribution = _attribution(report)
    assert attribution["projection_omission"] == 1
    assert attribution["null_source_event_id"] == 0
    assert attribution["ledger_missing_row"] == 0
    assert attribution["other"] == 0
    item = report["c_attribution"]["items"][0]
    assert item["attribution"] == "projection_omission"
    assert item["source_event_id"] == ledger_event_id


def test_probe_attributes_a_payload_difference_as_other(tmp_path: Path) -> None:
    """The remainder class: a difference that is not a row-set gap at all."""
    sqlite_path, _config = _build_green_store(tmp_path)

    def _reprice(fields: dict[str, object]) -> None:
        fields["premium_open"] = "9.99"

    _mutate_fields(sqlite_path, _reprice)

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    attribution = _attribution(report)
    assert attribution["other"] == 1
    assert attribution["null_source_event_id"] == 0
    assert attribution["ledger_missing_row"] == 0
    assert attribution["projection_omission"] == 0
    assert report["c_attribution"]["items"][0]["attribution"] == "other"


def test_probe_reads_the_source_event_id_from_the_payload_when_the_column_is_empty(
    tmp_path: Path,
) -> None:
    """A never-backfilled column must not masquerade as "no source event at all".

    ``source_event_id``'s payload spelling converged onto ``open_event_id``, so the
    fallback reads that key.
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    ledger_event_id = _ledger_event_id(sqlite_path)
    _insert_stored_row(
        sqlite_path,
        record_id="lot_probe_column_null",
        source_event_id=None,
        payload_source_event_id=ledger_event_id,
    )

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    attribution = _attribution(report)
    assert attribution["projection_omission"] == 1
    assert attribution["null_source_event_id"] == 0
    assert report["c_attribution"]["items"][0]["source_event_id_source"] == "payload"


def test_probe_c_attribution_counts_are_a_partition_of_the_differing_lots(
    tmp_path: Path,
) -> None:
    """One differing lot, one class — even when it is also a duplicated identity.

    Two stored rows collapse onto one ``lot_id`` whose ``source_event_id`` is in
    the ledger: one differing lot id that is ③. Counting it again as ④ made the
    four classes add up to more than the number of differing lots and read as two
    independent failures — and ③ is a stop condition for slice 2, so it must not
    be inflated by a repeated row.
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    _degrade_lot_table(sqlite_path, with_carrier=True)
    ledger_event_id = _ledger_event_id(sqlite_path)
    _tamper(
        sqlite_path,
        [
            (
                """
                INSERT INTO position_lots (
                    record_id, account, fields_json, source_event_id,
                    expiration, strike, multiplier, updated_at_ms, lot_id
                ) VALUES ('probe_dup_carrier', 'lx', '{"account":"lx"}', ?,
                          NULL, NULL, NULL, 1, 'lot_probe_dup')
                """,
                (ledger_event_id,),
            ),
            (
                """
                INSERT INTO position_lots (
                    record_id, account, fields_json, source_event_id,
                    expiration, strike, multiplier, updated_at_ms, lot_id
                ) VALUES ('lot_probe_dup', 'lx', '{"account":"lx"}', ?,
                          NULL, NULL, NULL, 1, NULL)
                """,
                (ledger_event_id,),
            ),
        ],
    )

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    faces = _faces(report)
    attribution = _attribution(report)
    assert faces["c_rows"]["duplicate_identity_count"] == 1
    assert faces["c_rows"]["extra_in_store_count"] == 1
    assert attribution["projection_omission"] == 1
    # Not ③ + ④: the same lot id, counted once.
    assert attribution["other"] == 0
    assert report["c_attribution"]["differing_lot_count"] == 1
    # Read off the items, not off the counts: the counts are incremented as the
    # items are appended, so "the four counts add up to the total" is a
    # restatement of how they were built and no store shape can falsify it.
    assert {
        item["lot_id"] for item in report["c_attribution"]["items"]
    } == {"lot_probe_dup"}


def test_probe_attributes_a_duplicate_identity_that_is_on_both_sides(
    tmp_path: Path,
) -> None:
    """A collapsed identity the replay *does* produce, so nothing else owns it.

    Both other duplicate fixtures collapse rows the replay never produced, which
    is why they reach ①②③: the extra row is the difference, and the duplicate is
    a second fact about the same lot id. Here the identity is on both sides and
    its representative row matches the replay, so the collapse is the **only**
    reason the lot id differs — the ``duplicate_ids`` input to the partition, and
    nothing else.

    The rows are inserted with the carrier's unique index dropped because the
    collision they describe is between two rows that *both* carry the identity
    (the fallback half of the carrier-or-fallback rule is what makes them one
    identity), which is the shape the read side has to describe whether or not
    the index is still there to stop it.
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    _degrade_lot_table(sqlite_path, with_carrier=True)
    lot_id = _stored_lot_id(sqlite_path)
    fields = _stored_fields(sqlite_path)
    conn = sqlite3.connect(sqlite_path)
    try:
        stored_row = conn.execute(
            "SELECT account, source_event_id, expiration, strike, multiplier "
            "FROM position_lots WHERE record_id = ?",
            (lot_id,),
        ).fetchone()
    finally:
        conn.close()
    assert stored_row is not None
    payload = json.dumps(fields, ensure_ascii=False, sort_keys=True)
    _tamper(sqlite_path, [("DROP INDEX IF EXISTS idx_position_lots_lot_id", ())])
    for record_id in ("probe_dup_both_a", "probe_dup_both_b"):
        _tamper(
            sqlite_path,
            [
                (
                    """
                    INSERT INTO position_lots (
                        record_id, account, fields_json, source_event_id,
                        expiration, strike, multiplier, updated_at_ms, lot_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        record_id,
                        stored_row[0],  # account
                        payload,
                        stored_row[1],  # source_event_id
                        stored_row[2],  # expiration
                        stored_row[3],  # strike
                        stored_row[4],  # multiplier
                        lot_id,
                    ),
                )
            ],
        )

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    faces = _faces(report)
    attribution = _attribution(report)
    # Nothing else is wrong with this lot: same payload, same columns, present on
    # both sides.
    assert faces["a_payload"]["difference_count"] == 0
    assert faces["b_columns"]["difference_count"] == 0
    assert faces["c_rows"]["duplicate_identity_count"] == 1
    assert faces["c_rows"]["extra_in_store_count"] == 0
    assert faces["c_rows"]["missing_in_store_count"] == 0
    assert faces["c_rows"]["count_mismatch"] is True
    assert faces["c_rows"]["difference_count"] == 1
    # ...so the collapse is the differing lot, and it is attributed exactly once.
    assert report["c_attribution"]["differing_lot_count"] == 1
    assert attribution["other"] == 1
    assert attribution["projection_omission"] == 0
    assert [item["lot_id"] for item in report["c_attribution"]["items"]] == [lot_id]


def test_probe_refuses_a_ledger_row_that_is_not_an_object(tmp_path: Path) -> None:
    """A corrupt ledger row is the ledger's fault, not the projection's.

    Keeping the id while dropping the payload (what the probe used to do) put the
    id in the ledger id set with nothing behind it, so every stored row
    referencing it was attributed to ``projection_omission`` — the class slice 2
    treats as a stop condition. The payload side already refuses this shape; the
    event side now does the same.
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    _drop_trade_event_guards(sqlite_path)
    _tamper(sqlite_path, [("UPDATE trade_events SET event_json = ?", ("[1,2]",))])

    with pytest.raises(ValueError, match="stored trade event payload must be an object"):
        run_lot_parity_probe(sqlite_path=sqlite_path)


@pytest.mark.parametrize("raw", ["null", "0", '"a string"'])
def test_probe_refuses_other_non_object_ledger_payloads(tmp_path: Path, raw: str) -> None:
    sqlite_path, _config = _build_green_store(tmp_path)
    _drop_trade_event_guards(sqlite_path)
    _tamper(sqlite_path, [("UPDATE trade_events SET event_json = ?", (raw,))])

    with pytest.raises(ValueError, match="stored trade event payload must be an object"):
        run_lot_parity_probe(sqlite_path=sqlite_path)


def test_probe_survives_the_record_id_rename(tmp_path: Path) -> None:
    """Slice 3 renames ``record_id`` to ``lot_id`` and re-runs this comparison.

    A hard-coded ``record_id`` in the SELECT or the ORDER BY would make the
    comparison die on the rename instead of describing the store on the other side
    of it — at the one step that has to prove "replay == stored" still holds.
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    with sqlite3.connect(sqlite_path) as conn:
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(position_lots)")
        }
    assert "lot_id" in columns and "record_id" not in columns

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    assert report["green"] is True
    assert report["stored_lot_count"] == 1
    assert _faces(report)["c_rows"]["difference_count"] == 0


def test_probe_reads_a_store_that_only_has_record_id(tmp_path: Path) -> None:
    """The production store today: ``record_id`` and no carrier column at all.

    This is the shape of the acceptance input (``production-readout.md``'s
    ``.backup`` copy, whose ``position_lots`` predates the carrier), and the shape
    no fixture in this file produced: the write path creates both columns, so the
    carrier-or-fallback branch that production actually takes was only ever
    exercised by hand.
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    _degrade_lot_table(sqlite_path)
    conn = sqlite3.connect(sqlite_path)
    try:
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(position_lots)").fetchall()
        }
    finally:
        conn.close()
    assert "lot_id" not in columns and "record_id" in columns

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    assert report["green"] is True
    assert report["stored_lot_count"] == 1
    assert report["projected_lot_count"] == 1


# --- zero writes -------------------------------------------------------------


def _store_snapshot(sqlite_path: Path) -> dict[str, object]:
    conn = sqlite3.connect(sqlite_path)
    try:
        schema = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        columns = {row[1] for row in conn.execute("PRAGMA table_info(position_lots)")}
        identity = "lot_id" if "lot_id" in columns else "record_id"
        lots = conn.execute(f"SELECT * FROM position_lots ORDER BY {identity}").fetchall()
        events = conn.execute(
            "SELECT event_id, event_json, trade_time_ms FROM trade_events ORDER BY event_id"
        ).fetchall()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    # Hashed after the connection closes: closing the last writer checkpoints the
    # WAL, exactly as it does for the "after" snapshot. The WAL payload is part of
    # the comparison because it is part of the state: the repository's own
    # read-only criterion hashes db **and** wal bytes
    # (``position_projection_migration._assert_read_only_persistent_sizes``), and
    # a main-file-only hash would call "the probe wrote nothing" while a write sat
    # in the log. ``-shm`` is reported but not compared: SQLite may resize that
    # ephemeral coordination file on any connection, which is why the repository's
    # criterion leaves it out.
    wal_bytes, shm_bytes = _sidecar_sizes(sqlite_path)
    return {
        "sha256": hashlib.sha256(sqlite_path.read_bytes()).hexdigest(),
        "wal_bytes": wal_bytes,
        "shm_bytes": shm_bytes,
        "schema": schema,
        "lots": lots,
        "events": events,
        "integrity_check": integrity,
    }


#: The durable state of the store — everything except ``shm_bytes``.
PERSISTENT_SNAPSHOT_KEYS = (
    "sha256",
    "wal_bytes",
    "schema",
    "lots",
    "events",
    "integrity_check",
)


@pytest.mark.parametrize("tampered", [False, True])
@pytest.mark.parametrize("settled", [False, True])
def test_probe_performs_no_writes(tmp_path: Path, tampered: bool, settled: bool) -> None:
    """Zero writes on a live store **and** on a settled copy (the immutable mode).

    The snapshot compares the main file *and* the WAL payload: the repository's
    own read-only criterion is db + wal bytes
    (``position_projection_migration._assert_read_only_persistent_sizes``), and a
    main-file-only hash would call "the probe wrote nothing" while a write sat in
    the log. The connection mode itself is pinned by the two tests below.
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    if tampered:
        _tamper(sqlite_path, [("UPDATE position_lots SET strike = 999.0", ())])
    if settled:
        # The immutable fallback is the mode with the least right to write; run
        # the zero-write control against it too, not just against ``mode=ro``.
        _settle_store(sqlite_path)
    # Warm-up: settle any pending WAL checkpoint before the "before" snapshot.
    _store_snapshot(sqlite_path)
    before = _store_snapshot(sqlite_path)
    assert before["integrity_check"] == "ok"

    run_lot_parity_probe(sqlite_path=sqlite_path)

    after = _store_snapshot(sqlite_path)
    assert {key: after[key] for key in PERSISTENT_SNAPSHOT_KEYS} == {
        key: before[key] for key in PERSISTENT_SNAPSHOT_KEYS
    }
    # The WAL is part of the compared state, not an implementation detail.
    assert "wal_bytes" in after


def test_probe_reads_both_tables_from_one_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A commit landing between the two reads must not fabricate a C difference.

    The cadence runs against the live store (plan ⑤), so the window between the
    ``position_lots`` read and the ``trade_events`` read is real. A writer that
    commits inside it leaves the probe holding a stored row the replay — read a
    moment later — no longer produces: an ``extra_in_store`` row whose event *is*
    in the ledger, which is exactly ③ ``projection_omission``, the class slice 2
    reads as a stop condition. One transaction (``BEGIN``, the idiom of
    ``read_only_evidence``) makes the two reads one snapshot.

    The writer is the real write path, not a hand-written INSERT: this is the
    window a nightly run actually has.
    """
    import src.application.ledger.lot_parity_probe as probe_module

    sqlite_path, data_config = _build_green_store(tmp_path)
    real_read = probe_module.read_stored_position_lots

    def _read_then_commit(conn: sqlite3.Connection) -> list[dict[str, object]]:
        rows = real_read(conn)
        repo = ledger_repository.SQLiteOptionPositionsRepository(sqlite_path)
        repo.data_config_path = data_config  # type: ignore[attr-defined]
        ledger_manual_trades.persist_manual_open_event(
            repo,
            broker="富途",
            account="lx",
            symbol="AMD",
            option_type="call",
            side="short",
            contracts=1,
            currency="USD",
            strike=50.0,
            multiplier=100,
            expiration_ymd="2026-07-17",
            premium_per_share=0.55,
            opened_at_ms=2000,
        )
        return rows

    monkeypatch.setattr(probe_module, "read_stored_position_lots", _read_then_commit)

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    # The concurrent commit is invisible to both reads, so the comparison is
    # green instead of reporting a projection omission that never happened.
    assert report["event_count"] == 1
    assert report["stored_lot_count"] == 1
    assert report["projected_lot_count"] == 1
    assert report["green"] is True
    assert _attribution(report) == {
        "null_source_event_id": 0,
        "ledger_missing_row": 0,
        "projection_omission": 0,
        "other": 0,
    }


def test_probe_opens_a_settled_copy_and_still_produces_its_verdict(tmp_path: Path) -> None:
    """The acceptance input: a settled ``.backup`` copy with no ``-shm``/``-wal``.

    That is the shape the immutable fallback was added for — the probe could not
    answer it at all on a build that refuses the read-only open. With no ``-shm``
    to reuse, the open is whatever the build can do: SQLite refuses it when it may
    not create the shared-memory file and allows it when the directory permits
    that creation (sqlite.org/wal.html, "Read-Only Databases"), so **which**
    read-only mode a settled copy gets is the build's and the directory's
    decision, not this module's. Both are read-only and the report carries the one
    that was taken; measured, the development build here lands on ``ro+immutable``
    and the CI runner on ``ro``.

    The fallback itself is pinned deterministically — the cannot-open failure is
    forced whatever the build would do — by
    ``test_probe_falls_back_only_when_the_sidecars_are_gone``.
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    _settle_store(sqlite_path)
    assert not Path(f"{sqlite_path}-wal").exists()
    assert not Path(f"{sqlite_path}-shm").exists()

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    # Which of the two read-only modes the copy gets is the build's call (see the
    # docstring); the fallback's own side of that decision is the sibling control.
    assert report["connection_mode"] in (LIVE_READ_MODE, SETTLED_READ_MODE)
    assert report["green"] is True
    assert report["stored_lot_count"] == 1
    assert report["projected_lot_count"] == 1


def test_probe_refuses_a_store_a_writer_is_holding_locked(tmp_path: Path) -> None:
    """A lock is not a settled store: it must not be answered with a verdict.

    ``mode=ro`` fails with "database is locked" — not with the
    "unable to open database file" the fallback exists for. Falling back there
    asked SQLite to read the store with no locks at all and handed back a full
    green judgement, on a store a writer was in the middle of changing.
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    sqlite_path = Path(sqlite_path)
    writer = sqlite3.connect(sqlite_path, isolation_level=None)
    try:
        writer.execute("PRAGMA journal_mode=DELETE")
        for suffix in ("-wal", "-shm"):
            Path(f"{sqlite_path}{suffix}").unlink(missing_ok=True)
        writer.execute("BEGIN EXCLUSIVE")
        writer.execute("UPDATE position_lots SET strike = 999.0")

        with pytest.raises(sqlite3.OperationalError, match="locked"):
            run_lot_parity_probe(sqlite_path=sqlite_path)
    finally:
        writer.execute("ROLLBACK")
        writer.close()


def test_probe_refuses_a_store_with_a_hot_journal(tmp_path: Path) -> None:
    """A rollback the read-only connection may not perform is not a green light.

    The store was left by a writer that died mid-commit: the main file holds its
    uncommitted page and the journal holds the image that page has to be rolled
    back to. SQLite refuses the read-only open ("attempt to write a readonly
    database"); reading anyway with ``immutable=1`` answers with a verdict built
    on page contents that are, by definition, not the store's content.

    The ``match`` is what makes this test say **which** branch refuses: it is
    SQLite's READONLY, not the fallback's cannot-open marker, so the probe's own
    fallback guard is never consulted for a hot journal (see
    ``_read_only_connection``). Without the match this control passes whichever
    of the two error wordings SQLite emits.
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    sqlite_path = Path(sqlite_path)
    _stage_a_crashed_writer(sqlite_path, strike=555.0)
    journal = Path(f"{sqlite_path}-journal")
    assert journal.read_bytes()[:8] == bytes.fromhex("d9d505f920a163d7")

    with pytest.raises(sqlite3.OperationalError, match="readonly database"):
        run_lot_parity_probe(sqlite_path=sqlite_path)

    # ...and the refused run left the crash for a reader that may roll back: the
    # probe must not be the thing that consumes a pending recovery.
    assert sqlite3.connect(sqlite_path).execute(
        "SELECT strike FROM position_lots"
    ).fetchone() == (100.0,)


def test_probe_opens_a_store_with_a_spent_persist_journal(tmp_path: Path) -> None:
    """The other side of the hot-journal judgement: a leftover that is not pending.

    ``journal_mode=PERSIST`` leaves a non-empty journal with a zeroed header on
    every settled store. Treating "a ``-journal`` exists" as "recovery is
    pending" would refuse a normal store; the probe must open it, ``mode=ro``,
    and produce its verdict.
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    sqlite_path = Path(sqlite_path)
    _leave_a_spent_persist_journal(sqlite_path)

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    assert report["connection_mode"] == LIVE_READ_MODE
    assert report["green"] is True


@pytest.mark.parametrize("settled", [True, False])
def test_probe_falls_back_only_when_the_sidecars_are_gone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settled: bool,
) -> None:
    """The fallback's guard, tested on both sides of its one judgement.

    ``mode=ro`` is made to fail the one way the fallback exists for — the
    "unable to open database file" of a settled WAL copy — so that the guard, and
    not the store, decides the outcome:

    * with a ``-wal``/``-shm`` sidecar present the failure propagates: those two
      files carry the lock a WAL writer holds, and ``immutable=1`` reads straight
      past whatever the writer is in the middle of;
    * with the sidecars gone (the ``.backup`` copy shape) the store opens
      ``ro+immutable`` normally.

    This is the whole guard, and it is the ``-wal``/``-shm`` **filename** test.
    It is deliberately not parametrized on a ``-journal`` any more: a hot journal
    fails ``mode=ro`` with SQLite's READONLY wording, which reaches the caller
    before the guard is consulted (see the hot-journal control above), so no store
    shape can put the guard in front of that decision.

    Each arm **places** the filenames the guard reads instead of inheriting them
    from the write path's exit, because that exit belongs to the build: here the
    last close leaves ``-shm``/``-wal`` behind, on the CI runner it deletes them,
    which is what the assertion below caught. So the un-settled arm holds a
    connection open — a live connection carries the shared-memory file on either
    build, and "sidecars present" is what the guard answers for — while the
    settled arm unlinks both files by hand.
    """
    import src.application.ledger.lot_parity_probe as probe_module

    sqlite_path, _config = _build_green_store(tmp_path)
    sqlite_path = Path(sqlite_path)
    holder: sqlite3.Connection | None = None
    if settled:
        _settle_store(sqlite_path)
    else:
        holder = sqlite3.connect(sqlite_path)
        holder.execute("SELECT count(*) FROM position_lots").fetchone()
    try:
        # The fixture's own claim, so neither branch can pass for the wrong
        # reason. It is asserted with the holder still attached, which is the
        # state the probe is handed.
        assert (
            any(Path(f"{sqlite_path}{suffix}").exists() for suffix in ("-wal", "-shm"))
            is not settled
        )

        real_connect = probe_module._connect_read_only

        def _cannot_open(resolved: Path, *, immutable: bool) -> sqlite3.Connection:
            if not immutable:
                raise sqlite3.OperationalError("unable to open database file")
            return real_connect(resolved, immutable=immutable)

        monkeypatch.setattr(probe_module, "_connect_read_only", _cannot_open)

        if not settled:
            with pytest.raises(
                sqlite3.OperationalError, match="unable to open database file"
            ):
                run_lot_parity_probe(sqlite_path=sqlite_path)
            return
        report = run_lot_parity_probe(sqlite_path=sqlite_path)
    finally:
        if holder is not None:
            holder.close()
    assert report["connection_mode"] == SETTLED_READ_MODE
    assert report["green"] is True


def test_probe_keeps_mode_ro_when_a_writer_may_still_be_attached(tmp_path: Path) -> None:
    """``immutable=1`` is for a copy with nothing pending — never for a live store.

    A writer attached to a WAL store holds its ``-shm``, so the store keeps
    ``mode=ro`` and the writer's committed state stays visible. That is what the
    sidecar half of the guard buys: a *file* test, not a test for writers — which
    is why the lock case has its own control above rather than being inferred
    from this one.
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    writer = sqlite3.connect(sqlite_path)
    try:
        assert writer.execute("SELECT count(*) FROM position_lots").fetchone() is not None
        assert Path(f"{sqlite_path}-shm").exists()

        report = run_lot_parity_probe(sqlite_path=sqlite_path)
    finally:
        writer.close()

    assert report["connection_mode"] == LIVE_READ_MODE
    assert report["green"] is True


# --- report landing (plan ⑥: outside output_shared/state/) -------------------


def test_probe_refuses_to_land_a_report_inside_the_runtime_state_tree(tmp_path: Path) -> None:
    forbidden = tmp_path / "output_shared" / "state" / "option_positions" / "probe.json"
    with pytest.raises(ValueError, match="outside output_shared/state/"):
        assert_report_path_outside_runtime_state(forbidden)

    allowed = tmp_path / "artifacts" / "lot-parity.json"
    assert assert_report_path_outside_runtime_state(allowed) == allowed.resolve()
    assert write_report({"green": True}, out_path=allowed) == allowed.resolve()
    assert json.loads(allowed.read_text(encoding="utf-8")) == {"green": True}


def test_probe_sample_limit_truncates_items_but_never_counts(tmp_path: Path) -> None:
    """``--sample-limit`` is a display budget: the counts stay totals.

    Three extra rows and a limit of one: the items are cut, the counts are not,
    and the C-side faces say which of the two happened (``items_truncated``). A
    sample limit that quietly truncated the counts would make a red run look
    small. Faces A and B carry the same flag, but this test does not assert it —
    only the two collections it exercises are pinned here.
    """
    sqlite_path, _config = _build_green_store(tmp_path)
    ledger_event_id = _ledger_event_id(sqlite_path)
    for index in range(3):
        _insert_stored_row(
            sqlite_path,
            record_id=f"lot_probe_extra_{index}",
            source_event_id=ledger_event_id,
        )

    report = run_lot_parity_probe(sqlite_path=sqlite_path, sample_limit=1)

    faces = _faces(report)
    assert report["sample_limit"] == 1
    assert faces["c_rows"]["extra_in_store_count"] == 3
    assert faces["c_rows"]["lot_id_set_difference_count"] == 3
    assert len(faces["c_rows"]["items"]) == 1
    assert faces["c_rows"]["items_truncated"] is True
    assert report["c_attribution"]["projection_omission"] == 3
    assert len(report["c_attribution"]["items"]) == 1
    assert report["c_attribution"]["items_truncated"] is True


def test_probe_module_cli_writes_a_report_and_exits_non_zero_when_red(tmp_path: Path) -> None:
    sqlite_path, _config = _build_green_store(tmp_path)
    green_out = tmp_path / "artifacts" / "green.json"
    assert probe_main(["--db", str(sqlite_path), "--out", str(green_out)]) == 0
    assert json.loads(green_out.read_text(encoding="utf-8"))["green"] is True

    _tamper(sqlite_path, [("UPDATE position_lots SET strike = 999.0", ())])
    red_out = tmp_path / "artifacts" / "red.json"
    assert probe_main(["--db", str(sqlite_path), "--out", str(red_out)]) == 1
    red = json.loads(red_out.read_text(encoding="utf-8"))
    assert red["green"] is False
    assert red["faces"]["b_columns"]["by_column"]["strike"] == 1


def test_probe_module_cli_keeps_its_report_out_of_the_runtime_state_tree(tmp_path: Path) -> None:
    sqlite_path, _config = _build_green_store(tmp_path)
    forbidden = tmp_path / "output_shared" / "state" / "probe.json"
    with pytest.raises(ValueError, match="outside output_shared/state/"):
        probe_main(["--db", str(sqlite_path), "--out", str(forbidden)])


# --- the enforcement channel mounted on verify-projection --------------------


def _mount_cli(
    monkeypatch: pytest.MonkeyPatch,
    sqlite_path: Path,
    data_config: Path,
    *,
    fmt: str | None = "json",
    extra_args: tuple[str, ...] = (),
) -> None:
    """Mount ``verify-projection`` with the argv the cadence would use.

    ``fmt=None`` is the production shape (``service_deploy.py``'s ``verify_args``
    carries no ``--format``, so the handler takes its text branch) — the one
    enforcement path that runs unattended, and the one no test reached.
    """
    import src.interfaces.cli.option_positions as cli_mod

    repo = ledger_repository.SQLiteOptionPositionsRepository(sqlite_path)
    repo.data_config_path = data_config  # type: ignore[attr-defined]
    monkeypatch.setattr(
        cli_mod,
        "resolve_option_positions_repo",
        lambda **_kwargs: (data_config, repo),
    )
    argv = [
        "om option-positions",
        "--data-config",
        str(data_config),
        "verify-projection",
        *extra_args,
    ]
    if fmt is not None:
        argv += ["--format", fmt]
    monkeypatch.setattr(sys, "argv", argv)


def test_verify_projection_adds_the_probe_under_a_new_key_without_touching_ok(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import src.interfaces.cli.option_positions as cli_mod

    sqlite_path, data_config = _build_green_store(tmp_path)
    _mount_cli(monkeypatch, sqlite_path, data_config)

    assert cli_mod.main() == 0

    payload = json.loads(capsys.readouterr().out)
    # Existing semantics untouched.
    assert payload["ok"] is True
    assert payload["mode_used"] == "full_replay"
    assert payload["checkpoint_reused"] is False
    assert payload["summary"]["matched"] == 1
    assert payload["report_id"].startswith("projection-verify-")
    assert payload["source_of_truth"] == "trade_events"
    assert payload["projection"] == "position_lots"
    # ... and the probe rides beside them, never inside them. One key, one
    # verdict: the summary carries the probe's own ``green`` and the handler does
    # not restate it.
    assert payload["lot_parity_probe"]["green"] is True
    assert payload["lot_parity_probe"]["tier"] == "tier-1"
    # Pinned to the ONE mode this fixture can produce: it is a live store with
    # its sidecars present, so a fallback to ``immutable=1`` here would be the
    # regression the read-mode tests exist to catch. ``in {...}`` accepted either
    # spelling and so could not fail. The settled-copy spelling is pinned by
    # ``test_probe_opens_a_settled_copy_and_still_produces_its_verdict``.
    assert payload["lot_parity_probe"]["connection_mode"] == "ro"
    assert "lot_parity_probe_green" not in payload
    assert "lot_parity_probe_error" not in payload


def test_verify_projection_exits_non_zero_when_the_probe_is_red(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import src.interfaces.cli.option_positions as cli_mod

    sqlite_path, data_config = _build_green_store(tmp_path)
    _tamper(sqlite_path, [("UPDATE position_lots SET strike = 999.0", ())])
    _mount_cli(monkeypatch, sqlite_path, data_config)

    assert cli_mod.main() == 1

    # The JSON still reaches stdout on a red run: the report is the evidence,
    # the exit code is the enforcement.
    payload = json.loads(capsys.readouterr().out)
    assert payload["lot_parity_probe"]["green"] is False
    assert payload["lot_parity_probe"]["b_columns_difference_count"] == 1
    # A3 taught the verifier the face the probe has always read, so a column-only
    # drift now lands in ``ok`` too: one fact, two verdicts, both red. That is the
    # intended reading of §8 -- a store whose columns disagree with its payloads is
    # not certified green -- and the checkpoint write and ``--mode auto`` fast path
    # follow ``ok`` deliberately.
    assert payload["ok"] is False
    assert payload["summary"]["column_differs_unexplained"] == 1
    # A red run still carries the detail: the counts say how much, the samples
    # say what.
    samples = payload["lot_parity_probe"]["samples"]
    assert samples["b_columns"][0]["column"] == "strike"
    assert samples["b_columns"][0]["stored"] == 999.0


def test_verify_projection_text_run_enforces_a_red_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The production enforcement channel: no ``--format``, so the text branch.

    The cadence's ``verify_args`` carries no ``--format``, which makes this the
    only path that actually exits non-zero in production. Every other control
    here runs ``--format json``.
    """
    import src.interfaces.cli.option_positions as cli_mod

    sqlite_path, data_config = _build_green_store(tmp_path)
    _tamper(sqlite_path, [("UPDATE position_lots SET strike = 999.0", ())])
    _mount_cli(monkeypatch, sqlite_path, data_config, fmt=None)

    assert cli_mod.main() == 1

    captured = capsys.readouterr()
    assert "[DONE] verified trade_events projection against position_lots" in captured.out
    # §5's ``matched`` is all three faces equal, so a column-only drift leaves it
    # at zero while the payloads agree -- and ``summary`` says which face moved.
    assert "matched=0" in captured.out
    assert "column_differs_unexplained=1" in captured.out
    probe_line = next(
        line for line in captured.out.splitlines() if "lot parity probe (tier-1)" in line
    )
    assert "green=False" in probe_line
    assert "b_columns=1" in probe_line
    assert "lot parity probe is red" in captured.err


def test_verify_projection_text_run_is_green_when_the_probe_is_green(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import src.interfaces.cli.option_positions as cli_mod

    sqlite_path, data_config = _build_green_store(tmp_path)
    _mount_cli(monkeypatch, sqlite_path, data_config, fmt=None)

    assert cli_mod.main() == 0

    captured = capsys.readouterr()
    probe_line = next(
        line for line in captured.out.splitlines() if "lot parity probe (tier-1)" in line
    )
    assert "green=True" in probe_line
    assert captured.err == ""


def test_verify_projection_red_run_leaves_the_existing_fields_alone(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The report's fields keep their meaning across a red column face.

    Read off the same store before and after a face-B diff. Before A3 that diff
    was visible only to the probe; the verifier reads the same face now, so ``ok``
    is the field that moves -- for the reason §8 gives it. What must not move is
    the rest of the shape: the run reports its own id and mode, the checkpoint is
    not reused across a changed store, and the summary still counts the lots.
    """
    import src.interfaces.cli.option_positions as cli_mod

    sqlite_path, data_config = _build_green_store(tmp_path)
    _mount_cli(monkeypatch, sqlite_path, data_config)
    assert cli_mod.main() == 0
    green = json.loads(capsys.readouterr().out)

    _tamper(sqlite_path, [("UPDATE position_lots SET strike = 999.0", ())])
    assert cli_mod.main() == 1
    red = json.loads(capsys.readouterr().out)

    assert green["ok"] is True
    assert green["summary"] == {"matched": 1}
    assert red["ok"] is False
    assert red["summary"] == {"column_differs_unexplained": 1}
    assert red["checkpoint_reused"] == green["checkpoint_reused"] is False
    assert red["mode_used"] == green["mode_used"]
    assert red["report_id"].startswith("projection-verify-")
    # A new run, a new report: the red run's evidence is its own, not a reused one.
    assert red["report_id"] != green["report_id"]


def test_verify_projection_reuse_path_still_runs_the_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A reused checkpoint says the *inputs* are unchanged, not that the store is.

    The probe runs unconditionally, on the reuse path too: "the replay reproduces
    the store" is the one thing the cadence is there to answer.
    """
    import src.interfaces.cli.option_positions as cli_mod

    sqlite_path, data_config = _build_green_store(tmp_path)
    _mount_cli(
        monkeypatch,
        sqlite_path,
        data_config,
        extra_args=("--mode", "auto", "--publish-evidence"),
    )
    calls: list[dict[str, object]] = []
    real_probe = cli_mod.run_lot_parity_probe

    def _counted(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return real_probe(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cli_mod, "run_lot_parity_probe", _counted)

    assert cli_mod.main() == 0
    first = json.loads(capsys.readouterr().out)
    assert first["checkpoint_reused"] is False

    assert cli_mod.main() == 0
    reused = json.loads(capsys.readouterr().out)

    assert reused["checkpoint_reused"] is True
    assert reused["mode_used"] == "checkpoint_reuse"
    assert len(calls) == 2
    assert reused["lot_parity_probe"]["green"] is True
    assert reused["lot_parity_probe"]["c_attribution"] == {
        "null_source_event_id": 0,
        "ledger_missing_row": 0,
        "projection_omission": 0,
        "other": 0,
    }


def test_verify_projection_isolates_a_probe_that_cannot_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A probe that cannot run must not take the verification report with it.

    A store shape the probe refuses (here: a never-migrated ``position_lots``
    without ``source_event_id``, the pre-migration shape) is not the same fact as
    a probe that ran and came back red, and neither may cost the caller the report
    the existing consumers read. Hence a separate key and a separate exit code —
    and the error is written down, never swallowed.
    """
    import src.interfaces.cli.option_positions as cli_mod

    sqlite_path, data_config = _build_green_store(tmp_path)
    _mount_cli(monkeypatch, sqlite_path, data_config)
    monkeypatch.setattr(
        cli_mod,
        "run_lot_parity_probe",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("missing source_event_id")),
    )

    assert cli_mod.main() == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload["lot_parity_probe_error"].startswith("ValueError: ")
    assert "source_event_id" in payload["lot_parity_probe_error"]
    assert "lot_parity_probe" not in payload
    # The report the existing consumers read is intact.
    assert payload["ok"] is True
    assert payload["summary"]["matched"] == 1
    assert payload["report_id"].startswith("projection-verify-")


def test_verify_projection_text_run_reports_a_probe_that_cannot_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Same isolation on the channel production actually uses, with exit code 2."""
    import src.interfaces.cli.option_positions as cli_mod

    sqlite_path, data_config = _build_green_store(tmp_path)
    _mount_cli(monkeypatch, sqlite_path, data_config, fmt=None)
    monkeypatch.setattr(
        cli_mod,
        "run_lot_parity_probe",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("missing source_event_id")),
    )

    assert cli_mod.main() == 2

    captured = capsys.readouterr()
    assert "[DONE] verified trade_events projection against position_lots" in captured.out
    # "cannot run" (2) is not "red" (1): the message says which, and it is loud.
    assert "lot parity probe could not run: ValueError" in captured.err
    assert "lot parity probe is red" not in captured.err


def test_verify_projection_hands_the_probe_only_a_store_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Never the shared write-capable repo object — a path, so the probe owns its handle."""
    import src.interfaces.cli.option_positions as cli_mod

    sqlite_path, data_config = _build_green_store(tmp_path)
    _mount_cli(monkeypatch, sqlite_path, data_config)
    seen: list[dict[str, object]] = []

    def _capture(**kwargs: object) -> dict[str, object]:
        seen.append(kwargs)
        return run_lot_parity_probe(sqlite_path=kwargs["sqlite_path"])  # type: ignore[arg-type]

    monkeypatch.setattr(cli_mod, "run_lot_parity_probe", _capture)

    assert cli_mod.main() == 0
    capsys.readouterr()
    assert seen == [{"sqlite_path": str(sqlite_path.resolve())}]


def test_probe_opens_its_own_connection_read_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.ledger.lot_parity_probe as probe_module

    sqlite_path, _config = _build_green_store(tmp_path)
    real_connect = sqlite3.connect
    uris: list[str] = []

    def _spy(database: object, *args: object, **kwargs: object) -> sqlite3.Connection:
        uris.append(str(database))
        return real_connect(database, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(probe_module.sqlite3, "connect", _spy)

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    assert report["green"] is True
    assert uris == [f"{sqlite_path.resolve().as_uri()}?mode=ro"]


def test_probe_sets_query_only_on_its_own_connection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``mode=ro`` and ``PRAGMA query_only=ON`` are two mechanisms, not one.

    The URI test above pins the open; this one pins the pragma, which is the
    second half of "zero writes is a property of the connection, not an
    intention" and had no control at all: deleting the statement left the whole
    file green. The connection is wrapped, not faked — every statement reaches
    the real SQLite.
    """
    probe_module = _probe_module()

    sqlite_path, _config = _build_green_store(tmp_path)
    statements: list[str] = []

    class _RecordingConnection:
        def __init__(self, real: sqlite3.Connection) -> None:
            self._real = real

        @property
        def row_factory(self) -> object:
            return self._real.row_factory

        @row_factory.setter
        def row_factory(self, value: object) -> None:
            self._real.row_factory = value  # type: ignore[assignment]

        def execute(self, sql: str, *args: object) -> sqlite3.Cursor:
            statements.append(sql)
            return self._real.execute(sql, *args)

        def close(self) -> None:
            self._real.close()

    real_connect = sqlite3.connect
    shim = types.SimpleNamespace(
        connect=lambda *args, **kwargs: _RecordingConnection(real_connect(*args, **kwargs)),
        Row=sqlite3.Row,
        OperationalError=sqlite3.OperationalError,
    )
    monkeypatch.setattr(probe_module, "sqlite3", shim)

    report = run_lot_parity_probe(sqlite_path=sqlite_path)

    assert report["green"] is True
    assert "PRAGMA query_only=ON" in statements


def test_probe_blocks_empty_identity_on_both_sides(tmp_path, monkeypatch):
    module = _probe_module()
    sqlite_path, _ = _build_green_store(tmp_path)
    read = module.read_stored_position_lots
    project = module._projected_lot_row

    def empty_stored(conn):
        return [dict(row, lot_id="") for row in read(conn)]

    monkeypatch.setattr(module, "read_stored_position_lots", empty_stored)
    monkeypatch.setattr(module, "_projected_lot_row", lambda lot: dict(project(lot), lot_id=""))
    report = run_lot_parity_probe(sqlite_path=sqlite_path)
    assert report["green"] is False
    assert report["faces"]["c_rows"]["empty_identity_count"] == 2
    assert {item["status"] for item in report["faces"]["c_rows"]["items"]} == {"empty_lot_id"}
    assert report["c_attribution"]["other"] == 1
