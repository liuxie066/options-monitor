"""``projection_verify``'s comparison surface (A3, window-prep).

Binding: ``comparator-spec.md`` §1-§9, pre-registered before the first production
read-only run. The three faces are payload (existing), the five stored columns
against the value re-derived from the stored payload (§4/§6 rule 1), and the row
set (§9.3 counting, §9.5 duplicates). ``rowid`` is carried and archived for §3's
pre/post-rewrite comparison, which is a two-snapshot job and not this read's.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.application.ledger import projection_verify as module
from src.application.ledger.lot_parity_probe import DERIVED_COLUMNS
from src.application.ledger.position_records import PositionLotRecord
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.sqlite_row_codec import DERIVED_COLUMN_NAMES
from src.application.ledger.sqlite_row_codec import position_lot_row_to_record


#: A payload the writer would accept: contract nested, account lowercase.
def _fields(
    *,
    lot_id: str = "lot_a",
    account: str = "us",
    expiration_ymd: str = "2026-06-19",
    strike: str = "100",
    multiplier: int | float = 100,
    open_event_id: str = "evt_1",
    status: str = "open",
) -> dict[str, object]:
    return {
        "lot_id": lot_id,
        "open_event_id": open_event_id,
        "contract_key": {
            "account": account,
            "expiration_ymd": expiration_ymd,
            "strike": strike,
            "option_type": "put",
        },
        "multiplier": multiplier,
        "status": status,
    }


#: Measured, not guessed: ``parse_exp_to_ms("2026-06-19")``.
EXPIRATION_MS = 1781827200000


#: The store-side columns the derivation above produces for ``_fields()``.
def _columns(**overrides: object) -> dict[str, object]:
    columns: dict[str, object] = {
        "account": "us",
        "expiration": EXPIRATION_MS,
        "strike": 100.0,
        "multiplier": 100.0,
        "source_event_id": "evt_1",
    }
    columns.update(overrides)
    return columns


def _store_lot(lot_id: str = "lot_a", *, fields: dict[str, object] | None = None, **column_overrides: object) -> dict:
    return {
        "record_id": lot_id,
        "lot_id": lot_id,
        "fields": _fields(lot_id=lot_id) if fields is None else fields,
        "columns": _columns(**column_overrides),
        "rowid": 7,
    }


def _replay_lot(lot_id: str = "lot_a", *, fields: dict[str, object] | None = None) -> PositionLotRecord:
    return PositionLotRecord(lot_id=lot_id, fields=_fields(lot_id=lot_id) if fields is None else fields)


def _compare(store: list[dict], replay: list, diagnostics: list | None = None) -> dict:
    return module.compare_projection_lots(
        projected_lots=replay,
        current_lots=store,
        diagnostics=diagnostics or [],
    )


def test_the_two_derived_column_tuples_stay_in_step() -> None:
    """The codec restates the probe's tuple to stay import-cycle free."""
    assert tuple(DERIVED_COLUMN_NAMES) == tuple(DERIVED_COLUMNS)


def test_the_store_read_carries_the_face_b_columns_and_the_rowid(tmp_path: Path) -> None:
    """The real SELECT and the real codec, not a hand-built row.

    ``list_position_lots`` is the store side of every ``compare_projection_lots``
    call; the codec attaches ``columns`` only when the read fetched all five, so
    this pins the pair together.
    """
    repo = SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    with sqlite3.connect(tmp_path / "option_positions.sqlite3") as conn:
        conn.execute(
            """
            INSERT INTO position_lots
                (lot_id, fields_json, account, source_event_id,
                 strike, multiplier, updated_at_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "lot_a",
                '{"contract_key": {"account": "us"}}',
                "us",
                "evt_1",
                100.0,
                100.0,
                1,
            ),
        )
        conn.commit()

    (record,) = repo.list_position_lots()

    assert record["columns"]["account"] == "us"
    assert record["columns"]["source_event_id"] == "evt_1"
    assert record["columns"]["retired_columns"] == ["expiration"]
    assert record["rowid"] == 1


def test_a_narrow_read_reports_no_columns_instead_of_agreeing() -> None:
    """A row that never fetched the columns must not look like a match."""
    narrow = {"record_id": "lot_a", "lot_id": "lot_a", "fields": _fields()}
    comparison = _compare([narrow], [_replay_lot()])

    assert comparison["store_face"]["columns_read"] is False
    assert comparison["store_face"]["reason"]
    assert comparison["summary"] == {"matched": 1}
    # ``ok`` keeps its old meaning; ``green`` is the §8 verdict and needs the face.
    assert comparison["green"] is False


def test_a_payload_difference_reports_its_keys_and_its_verdicts() -> None:
    """§9.4: which keys, not just "the dicts differ"."""
    store = _store_lot(fields=_fields())
    replay_fields = _fields()
    replay_fields["contracts_open"] = 2  # only on the replay side
    store["fields"]["premium_open"] = 1.23  # only on the store side
    store["fields"]["status"] = "closed"  # both sides, different value
    comparison = _compare([store], [_replay_lot(fields=replay_fields)])

    (item,) = [entry for entry in comparison["items"] if entry["status"] == "field_mismatch"]
    assert item["keys_only_in_projection"] == ["contracts_open"]
    assert item["keys_only_in_store"] == ["premium_open"]
    assert [entry["key"] for entry in item["value_differences"]] == ["status"]
    assert item["payload_verdicts"] == ["payload_key_missing", "payload_value_differs"]
    assert comparison["verdicts"]["payload_key_missing"] == 1
    assert comparison["verdicts"]["payload_value_differs"] == 1
    assert comparison["green"] is False


def test_updated_at_ms_alone_is_not_a_payload_difference() -> None:
    """§3: a wall clock is not a fact, so a difference on it is not a difference."""
    store = _store_lot(fields=_fields())
    store["fields"]["updated_at_ms"] = 42
    comparison = _compare([store], [_replay_lot()])

    assert comparison["summary"] == {"matched": 1}
    assert comparison["store_rowids"] == {"lot_a": 7}
    assert comparison["excluded_payload_keys"] == ["updated_at_ms"]


def test_a_column_disagreeing_with_its_own_payload_is_unexplained_and_red() -> None:
    """§6 rule 1: the stored column against the value derived from the payload."""
    comparison = _compare([_store_lot(multiplier=250.0)], [_replay_lot()])

    (item,) = [entry for entry in comparison["items"] if entry["status"] == "column_differs_unexplained"]
    assert item["column"] == "multiplier"
    assert item["stored"] == 250.0
    assert item["derived_from_stored_payload"] == 100.0
    assert comparison["summary"] == {"column_differs_unexplained": 1}
    assert comparison["verdicts"]["column_differs_unexplained"] == 1
    assert comparison["green"] is False


def test_the_replay_deriving_a_different_column_is_a_payload_verdict() -> None:
    """§6 rule 2: the same recompute on both payloads, no stored column involved."""
    replay_fields = _fields(expiration_ymd="2026-07-17")
    store = _store_lot(fields=_fields())  # every stored column agrees with its payload
    comparison = _compare([store], [_replay_lot(fields=replay_fields)])

    (item,) = [entry for entry in comparison["items"] if entry["status"] == "field_mismatch"]
    assert [entry["column"] for entry in item["derived_column_differences"]] == ["expiration"]
    # Same key on both sides, different value: a value verdict, not a key one.
    assert item["payload_verdicts"] == ["payload_value_differs"]
    assert comparison["verdicts"]["payload_value_differs"] == 1
    # The stored column itself agreed with its payload, so face B stayed quiet.
    assert comparison["summary"] == {"field_mismatch": 1}


def test_a_lot_that_fails_both_recomputes_is_counted_as_both() -> None:
    """§6 rule 3: report the double, not one of the two."""
    store = _store_lot(fields=_fields(), multiplier=250.0)
    replay_fields = _fields(multiplier=300)
    comparison = _compare([store], [_replay_lot(fields=replay_fields)])

    assert comparison["verdicts"]["both"] == 1
    assert comparison["summary"]["column_differs_unexplained"] == 1
    assert comparison["summary"]["field_mismatch"] == 1


def test_unequal_counts_are_reported_as_the_count_mismatch() -> None:
    """§9.3: both counts were always produced and never compared."""
    comparison = _compare([_store_lot("lot_a"), _store_lot("lot_b")], [_replay_lot("lot_a")])

    (item,) = [entry for entry in comparison["items"] if entry["status"] == "count_mismatch"]
    assert item["position_lot_count"] == 2
    assert item["projected_lot_count"] == 1
    assert comparison["summary"]["extra_in_position_lots"] == 1
    assert comparison["green"] is False


def test_a_repeated_identity_is_reported_instead_of_folded() -> None:
    """§9.5: the old index silently kept one of the two."""
    comparison = _compare([_store_lot("lot_a"), _store_lot("lot_a")], [_replay_lot("lot_a")])

    (item,) = [entry for entry in comparison["items"] if entry["status"] == "duplicate_lot_id"]
    assert item["side"] == "position_lots"
    assert item["occurrences"] == 2
    assert comparison["green"] is False


def test_the_allowlist_admits_a_column_and_takes_it_out_of_red_green(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§7/§8: an admitted entry is counted and archived, and does not decide green."""
    monkeypatch.setattr(module, "COLUMN_DIRTY_ALLOWLIST", (("multiplier", "measured on the copy"),))
    comparison = _compare([_store_lot(multiplier=250.0)], [_replay_lot()])

    (admitted,) = comparison["known_dirty"]
    assert admitted["column"] == "multiplier"
    assert admitted["record_id"] == "lot_a"
    # Not an ``items`` status: the consumers' filter is ``!= "matched"``.
    assert comparison["summary"] == {"matched": 1}
    assert comparison["verdicts"]["column_differs_known_dirty"] == 1
    assert comparison["green"] is True


def test_every_status_the_instrument_emits_blocks_red_green(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§8 keeps two §5 terms out of red/green; emitting one needs the callers changed.

    Every consumer of ``summary`` filters on ``!= "matched"`` (combo confirmation,
    post-write parity), so a non-blocking ``items`` status would start blocking
    real production paths the moment it appeared. The property is asserted over
    what a battery of scenarios actually produced, not over a list of literals.
    """
    monkeypatch.setattr(module, "COLUMN_DIRTY_ALLOWLIST", (("multiplier", "measured on the copy"),))
    comparisons = [
        _compare([_store_lot()], [_replay_lot()]),
        _compare([_store_lot()], [_replay_lot(fields=_fields(status="closed"))]),
        _compare([_store_lot(multiplier=250.0)], [_replay_lot()]),
        _compare([_store_lot(strike=250.0)], [_replay_lot()]),  # not admitted -> blocking
        _compare([_store_lot(multiplier=250.0)], [_replay_lot(fields=_fields(multiplier=300))]),
        _compare([_store_lot("lot_a")], [_replay_lot("lot_b")]),
        _compare([_store_lot("lot_a")], [_replay_lot("lot_a"), _replay_lot("lot_b")]),
        _compare([_store_lot("lot_a"), _store_lot("lot_a")], [_replay_lot("lot_a")]),
        _compare([{"record_id": "lot_a", "lot_id": "lot_a", "fields": _fields()}], [_replay_lot()]),
        _compare([_store_lot()], [_replay_lot()], diagnostics=[{"severity": "error", "code": "boom"}]),
    ]
    emitted = {str(item.get("status") or "") for comparison in comparisons for item in comparison["items"]}

    assert {
        "matched",
        "field_mismatch",
        "column_differs_unexplained",
        "duplicate_lot_id",
        "count_mismatch",
        "missing_in_position_lots",
        "projection_error",
    } <= emitted
    assert "column_differs_known_dirty" not in emitted  # it lands in ``known_dirty``
    for status in sorted(emitted):
        expected = 0 if status == "matched" else 1
        assert module._blocking_count({status: 1}) == expected, status


def test_the_fingerprint_covers_columns_and_ignores_rowid() -> None:
    """A column drift must invalidate the checkpoint; a rowid move must not (§3/§8)."""
    base = [_store_lot()]
    moved_rowid = [_store_lot()]
    moved_rowid[0]["rowid"] = 99
    drifted_column = [_store_lot(multiplier=250.0)]

    fingerprint = module._fingerprint(module._fingerprint_input(base))
    assert module._fingerprint(module._fingerprint_input(moved_rowid)) == fingerprint
    assert module._fingerprint(module._fingerprint_input(drifted_column)) != fingerprint


class _Repo:
    """The two reads ``verify_position_projection`` makes, and nothing else."""

    def __init__(self, *, events: list, lots: list) -> None:
        self._events = events
        self._lots = lots

    def list_trade_events(self) -> list:
        return self._events

    def list_position_lots(self) -> list:
        return self._lots


def test_the_report_carries_the_new_blocks_end_to_end(tmp_path: Path) -> None:
    """``ok`` stays the old contract; the §8 words travel beside it.

    The repo replays nothing, so the store's lot is an extra rather than a pair --
    this test is about the report's plumbing, and the face rules above are
    exercised against ``compare_projection_lots`` directly.
    """
    repo = _Repo(events=[], lots=[_store_lot(multiplier=250.0)])
    report = module.verify_position_projection(base=tmp_path, repo=repo)

    assert report["ok"] is False
    assert report["green"] is False
    assert report["summary"] == {"extra_in_position_lots": 1, "count_mismatch": 1}
    assert report["verdicts"]["extra_in_store"] == 1
    assert report["store_face"]["columns_read"] is True
    assert report["store_rowids"] == {"lot_a": 7}
    assert report["position_lot_count"] == 1
    assert report["projected_lot_count"] == 0
    assert report["known_dirty"] == []


def test_the_codec_needs_all_five_columns_before_it_attaches_them() -> None:
    """Four columns are not five: the face says so instead of comparing a subset."""
    row = {
        "record_id": "lot_a",
        "lot_id": "lot_a",
        "fields_json": '{"contract_key": {"account": "us"}}',
        "account": "us",
        "expiration": None,
        "strike": 100.0,
        "multiplier": 100.0,
        "rowid": 3,
    }
    record = position_lot_row_to_record(row)

    assert "columns" not in record
    assert record["rowid"] == 3


@pytest.mark.parametrize("identity", ["", "   ", None])
def test_empty_identity_blocks_even_when_both_faces_agree(identity):
    comparison = _compare([_store_lot(identity)], [{"lot_id": identity, "fields": _fields(lot_id=identity)}])
    assert comparison["green"] is False
    assert comparison["summary"]["empty_lot_id"] == 2
    assert comparison["store_rowids"] is None


def test_rowid_movement_requires_two_snapshots():
    comparison = _compare([_store_lot()], [_replay_lot()])
    assert comparison["green"] is True
    assert comparison["verdicts"]["rowid_moved"] is None
    assert comparison["store_rowids"] == {"lot_a": 7}
