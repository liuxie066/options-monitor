from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.application.ledger.publisher import (
    PROJECTION_CONTRACT_VERSION,
    project_stored_trade_events_to_position_lots,
)
from src.infrastructure.io_utils import utc_now as utc_now_iso

# The three face rules below are the probe's, imported rather than restated:
# ``tests/test_lot_parity_probe.py`` binds that derivation to the writer's own
# (message and covered columns, column by column), and a second copy here would
# be a third thing to keep in step. ``_scalar_matches`` is private there on
# purpose -- the tolerance rule for the three contract scalars is the writer's,
# and importing it is how this module inherits it instead of guessing.
from src.application.ledger.lot_parity_probe import (
    DERIVED_COLUMNS,
    EXCLUDED_PAYLOAD_KEYS,
    _scalar_matches,
    compare_column_face,
    compare_payload_face,
    derive_stored_row_columns,
)


SCHEMA_KIND = "option_positions_projection_verify"
CHECKPOINT_SCHEMA_KIND = "option_positions_projection_verify_checkpoint"

#: ``comparator-spec.md`` §5, pre-registered before the first production run.
#: ``verdicts`` in the report counts these; ``both`` is §6's third attribution
#: (a lot whose column face *and* payload face both differ).
V5_VERDICT_TERMS = (
    "matched",
    "payload_key_missing",
    "payload_value_differs",
    "column_differs_known_dirty",
    "column_differs_unexplained",
    "extra_in_store",
    "missing_in_store",
    "rowid_moved",
    "count_mismatch",
)

#: §7's admission list, empty at the start by rule: an entry needs a measured
#: source and a reason it cannot self-heal, and both are only available from the
#: production read-only run. ``(column, reason)``. A difference on an admitted
#: column is ``column_differs_known_dirty``; every other column difference is
#: ``column_differs_unexplained``, which §8 puts back on the payload face.
COLUMN_DIRTY_ALLOWLIST: tuple[tuple[str, str], ...] = ()

#: The two §5 terms §8 keeps out of red/green. They are *not* emitted today:
#: every consumer of ``summary`` filters on ``!= "matched"``, so a new
#: non-blocking status would start blocking real production paths the moment it
#: appeared. ``tests/test_projection_verify.py`` pins the emitted set.
NON_BLOCKING_STATUSES = ("matched", "rowid_moved", "column_differs_known_dirty")


def _state_dir(base: Path) -> Path:
    return Path(base).resolve() / "output_shared" / "state" / "option_positions"


def _current_dir(base: Path) -> Path:
    return _state_dir(base) / "current"


def _reports_dir(base: Path) -> Path:
    return _state_dir(base) / "projection_verify"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _canonical_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical_payload(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical_payload(item) for item in value]
    return value


def _fingerprint(value: Any) -> str:
    raw = json.dumps(_canonical_payload(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _lot_dict(row: Any) -> dict[str, Any]:
    if hasattr(row, "to_dict"):
        payload = row.to_dict()
    else:
        payload = row
    if not isinstance(payload, dict):
        return {"record_id": "", "fields": {}}
    fields = payload.get("fields")
    record = {
        "record_id": str(payload.get("lot_id") or payload.get("record_id") or "").strip(),
        "fields": dict(fields) if isinstance(fields, dict) else {},
    }
    # Face B rides along when the store read carried it (``sqlite_row_codec``
    # attaches ``columns`` only for a read that fetched all five). The replay side
    # has neither key: ``PositionLotRecord.to_dict()`` is ``lot_id`` + ``fields``.
    columns = payload.get("columns")
    if isinstance(columns, dict):
        record["columns"] = dict(columns)
    rowid = payload.get("rowid")
    if rowid is not None:
        record["rowid"] = rowid
    return record


def _canonical_lots(rows: list[Any]) -> list[dict[str, Any]]:
    return sorted((_lot_dict(row) for row in rows), key=lambda item: str(item.get("record_id") or ""))


def _fingerprint_input(lots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The instrument's own fingerprint input: the compared surface, minus rowid.

    ``columns`` is **in**: a column drift has to invalidate the checkpoint, or the
    reuse path would report every lot ``matched`` while face B disagrees (§8 keeps
    a column difference red). ``rowid`` is **out**: §3 compares it across the
    rewrite, and §8 keeps it out of red/green, so a pure delete-and-reinsert must
    not invalidate a content-green checkpoint. This is the instrument's local
    fingerprint -- ``position_lots_fingerprint`` is a different function and reads
    only ``record_id``/``fields`` either way.
    """
    return [{key: value for key, value in lot.items() if key != "rowid"} for lot in lots]


def _index_by_identity(lots: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[tuple[str, int]]]:
    """Index lots by identity, keeping the first of a repeated one (§9.5).

    The repeats come back as counts instead of collapsing into the index: a
    duplicated identity makes face C's "set difference" ill-defined, which is not
    the same fact as a payload difference and must not be reported as one.
    """
    index: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    for lot in lots:
        identity = str(lot.get("record_id") or "")
        counts[identity] = counts.get(identity, 0) + 1
        index.setdefault(identity, lot)
    duplicates = sorted((identity, count) for identity, count in counts.items() if count > 1)
    return index, duplicates


def _blocking_count(summary: dict[str, int]) -> int:
    """§8's blocking count, fail-closed: anything not listed as non-blocking blocks."""
    return sum(int(count) for status, count in summary.items() if status not in NON_BLOCKING_STATUSES)


def _derived_column_differences(
    *,
    stored_fields: dict[str, Any],
    projected_fields: dict[str, Any],
) -> list[dict[str, Any]]:
    """§6's second recompute: the replay's derived columns against the store's.

    A difference here is a payload verdict (§6 rule 2 -- the projection is not
    faithful), not a column-face one: the stored columns are not consulted. This
    is why a payload difference that reaches a stored column can be reported on
    both faces at once, and §6.3 then counts the lot under ``both``.
    """
    stored_derived = derive_stored_row_columns(stored_fields)
    projected_derived = derive_stored_row_columns(projected_fields)
    differences: list[dict[str, Any]] = []
    for column in DERIVED_COLUMNS:
        stored_value = stored_derived.get(column)
        projected_value = projected_derived.get(column)
        if not _scalar_matches(stored_value, projected_value, column=column):
            differences.append(
                {
                    "column": column,
                    "stored_derived": stored_value,
                    "projected_derived": projected_value,
                }
            )
    return differences


def _load_checkpoint(base: Path) -> dict[str, Any] | None:
    return _read_json(_current_dir(base) / "projection_verify.checkpoint.json")


def load_projection_verify_state(*, base: Path) -> dict[str, Any]:
    current = _current_dir(base)
    return {
        "latest_projection_verify_report": _read_json(current / "projection_verify.latest.json"),
        "latest_projection_verify_checkpoint": _read_json(current / "projection_verify.checkpoint.json"),
    }


def _repo_events(repo: Any) -> list[dict[str, Any]]:
    list_trade_events = getattr(repo, "list_trade_events", None)
    if not callable(list_trade_events):
        raise TypeError("option_positions repo does not expose list_trade_events")
    rows = list_trade_events()
    return rows if isinstance(rows, list) else []


def _repo_lots(repo: Any) -> list[dict[str, Any]]:
    list_position_lots = getattr(repo, "list_position_lots", None)
    if not callable(list_position_lots):
        raise TypeError("option_positions repo does not expose list_position_lots")
    rows = list_position_lots()
    return _canonical_lots(rows if isinstance(rows, list) else [])


def _latest_event_info(events: list[dict[str, Any]]) -> dict[str, Any]:
    if not events:
        return {"last_event_id": None, "last_event_time_ms": None}
    item = events[-1] if isinstance(events[-1], dict) else {}
    return {
        "last_event_id": str(item.get("event_id") or "").strip() or None,
        "last_event_time_ms": item.get("event_time_ms") or item.get("trade_time_ms"),
    }


def _projection_error_items(diagnostics: list[Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in diagnostics:
        payload = item.to_dict() if hasattr(item, "to_dict") else item
        if not isinstance(payload, dict):
            continue
        if str(payload.get("severity") or "").lower() != "error":
            continue
        items.append(
            {
                "status": "projection_error",
                "event_id": payload.get("event_id"),
                "code": payload.get("code"),
                "message": payload.get("message"),
                "details": payload.get("details") if isinstance(payload.get("details"), dict) else {},
            }
        )
    return items


def compare_projection_lots(*, projected_lots: list[Any], current_lots: list[Any], diagnostics: list[Any]) -> dict[str, Any]:
    """Three faces, ``comparator-spec.md`` §1: payload, columns, row set.

    ``ok``/``summary`` keep their old meaning (no blocking item), so the callers
    that read them are unaffected; the section that decides red/green per §8 is
    ``green``, which additionally requires that the column face actually ran.
    A face that could not run says so in ``store_face`` instead of passing
    silently -- an unfetched column is not an agreeing column.
    """
    projected = _canonical_lots(projected_lots)
    current = _canonical_lots(current_lots)
    projected_by_id, projected_duplicates = _index_by_identity(projected)
    current_by_id, current_duplicates = _index_by_identity(current)
    columns_read = all("columns" in lot for lot in current)
    rowids_read = all("rowid" in lot for lot in current)
    allowlisted_columns = {column for column, _reason in COLUMN_DIRTY_ALLOWLIST}

    items: list[dict[str, Any]] = _projection_error_items(diagnostics)
    known_dirty: list[dict[str, Any]] = []
    both_faces = 0
    for lot_id in sorted(set(projected_by_id) | set(current_by_id)):
        projected_item = projected_by_id.get(lot_id)
        current_item = current_by_id.get(lot_id)
        if projected_item is None:
            items.append({"status": "extra_in_position_lots", "record_id": lot_id, "current": current_item})
            continue
        if current_item is None:
            items.append({"status": "missing_in_position_lots", "record_id": lot_id, "projected": projected_item})
            continue
        stored_fields = current_item.get("fields") or {}
        projected_fields = projected_item.get("fields") or {}

        payload_detail = compare_payload_face(stored_fields=stored_fields, projected_fields=projected_fields)
        payload_verdicts: list[str] = []
        if payload_detail["keys_only_in_store"] or payload_detail["keys_only_in_projection"]:
            payload_verdicts.append("payload_key_missing")
        if payload_detail["value_differences"]:
            payload_verdicts.append("payload_value_differs")
        derived_differences = _derived_column_differences(
            stored_fields=stored_fields,
            projected_fields=projected_fields,
        )
        if derived_differences and "payload_value_differs" not in payload_verdicts:
            # §6 rule 2 without a §6-visible payload difference on the same keys:
            # the derivation reads note/contract_key fallbacks, so the two sides
            # can disagree on a *derived* value while the compared flags match.
            payload_verdicts.append("payload_value_differs")

        column_differences = (
            compare_column_face(
                stored_columns=current_item["columns"],
                derived_columns=derive_stored_row_columns(stored_fields),
            )
            if columns_read
            else []
        )
        blocking_columns = False
        for difference in column_differences:
            column = str(difference.get("column") or "")
            if column in allowlisted_columns:
                # §8 keeps an admitted difference out of red/green, and ``items``
                # is what every consumer counts. So it is archived in its own
                # block rather than as a status: a non-blocking ``items`` entry
                # would start blocking combo confirmation and post-write parity,
                # whose filter is ``!= "matched"``.
                known_dirty.append({"record_id": lot_id, **difference})
                continue
            items.append({"status": "column_differs_unexplained", "record_id": lot_id, **difference})
            blocking_columns = True
        if column_differences and payload_verdicts:
            both_faces += 1
        if payload_verdicts:
            items.append(
                {
                    "status": "field_mismatch",
                    "record_id": lot_id,
                    "payload_verdicts": payload_verdicts,
                    "keys_only_in_store": payload_detail["keys_only_in_store"],
                    "keys_only_in_projection": payload_detail["keys_only_in_projection"],
                    "value_differences": payload_detail["value_differences"],
                    "derived_column_differences": derived_differences,
                    "projected_fields": projected_item.get("fields"),
                    "current_fields": current_item.get("fields"),
                }
            )
            continue
        if blocking_columns:
            continue
        # Reached with an admitted column difference too: ``matched`` means "no
        # blocking difference on any face", and the admitted one is archived in
        # ``known_dirty`` rather than dropped from the report.
        items.append({"status": "matched", "record_id": lot_id})

    for lot_id, occurrences in current_duplicates:
        items.append(
            {"status": "duplicate_lot_id", "record_id": lot_id, "side": "position_lots", "occurrences": occurrences}
        )
    for lot_id, occurrences in projected_duplicates:
        items.append(
            {"status": "duplicate_lot_id", "record_id": lot_id, "side": "projection", "occurrences": occurrences}
        )
    for side, lots in (("position_lots", current), ("projection", projected)):
        empty_count = sum(not lot["record_id"] for lot in lots)
        if empty_count:
            items.append({"status": "empty_lot_id", "side": side, "occurrences": empty_count})
    if len(projected) != len(current):
        items.append(
            {
                "status": "count_mismatch",
                "position_lot_count": len(current),
                "projected_lot_count": len(projected),
            }
        )

    summary: dict[str, int] = {}
    for item in items:
        status = str(item.get("status") or "")
        summary[status] = int(summary.get(status) or 0) + 1

    verdicts: dict[str, Any] = {term: 0 for term in V5_VERDICT_TERMS}
    verdicts["rowid_moved"] = None
    verdicts["both"] = both_faces
    verdicts["column_differs_known_dirty"] = len(known_dirty)
    for item in items:
        status = str(item.get("status") or "")
        if status == "matched":
            verdicts["matched"] += 1
        elif status == "field_mismatch":
            for term in item.get("payload_verdicts") or ["payload_value_differs"]:
                verdicts[str(term)] += 1
        elif status == "column_differs_unexplained":
            verdicts["column_differs_unexplained"] += 1
        elif status == "extra_in_position_lots":
            verdicts["extra_in_store"] += 1
        elif status == "missing_in_position_lots":
            verdicts["missing_in_store"] += 1
        elif status == "count_mismatch":
            verdicts["count_mismatch"] += 1

    return {
        "summary": summary,
        "items": items,
        "verdicts": verdicts,
        "green": _blocking_count(summary) == 0 and columns_read,
        "store_face": {
            "columns_read": columns_read,
            "retired_columns": sorted({name for lot in current for name in lot.get("columns", {}).get("retired_columns", [])}),
            "rowids_read": rowids_read,
            "reason": None
            if columns_read
            else "the store read carried no face-B columns (a narrower SELECT or a hand-built lot dict)",
        },
        "store_rowids": {str(lot.get("record_id") or ""): lot.get("rowid") for lot in current}
        if rowids_read and not current_duplicates and all(lot["record_id"] for lot in current) else None,
        "known_dirty": known_dirty,
        "excluded_payload_keys": list(EXCLUDED_PAYLOAD_KEYS),
        "notes": [
            "face A compares raw stored fields_json against the replay payload with neither side healed",
            "face B compares each stored derived column against the value re-derived from the stored payload (§6 rule 1)",
            "derived_column_differences is §6 rule 2: the replay's derived columns against the store's",
            "rowid_moved is null (not compared): A4 compares pre/post-rewrite store_rowids snapshots",
            "column_differs_known_dirty counts known_dirty and is 0 until COLUMN_DIRTY_ALLOWLIST admits a measured entry (§7)",
        ],
    }


def _build_checkpoint(*, report: dict[str, Any], events: list[dict[str, Any]], current_lots: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_kind": CHECKPOINT_SCHEMA_KIND,
        "schema_version": "1.0",
        "projection_contract_version": PROJECTION_CONTRACT_VERSION,
        "checkpoint_id": report["report_id"],
        "created_at_utc": utc_now_iso(),
        "event_count": len(events),
        "position_lot_count": len(current_lots),
        "event_fingerprint": report["event_fingerprint"],
        "position_lots_fingerprint": report["position_lots_fingerprint"],
        "projection_fingerprint": report["projection_fingerprint"],
        **_latest_event_info(events),
    }


def _persist_report(*, base: Path, report: dict[str, Any], checkpoint: dict[str, Any] | None) -> None:
    current = _current_dir(base)
    _write_json(current / "projection_verify.latest.json", report)
    _write_json(_reports_dir(base) / f"{report['report_id'].replace('/', '_')}.json", report)
    if checkpoint is not None:
        _write_json(current / "projection_verify.checkpoint.json", checkpoint)


def verify_position_projection(
    *,
    base: Path,
    repo: Any,
    mode: str = "auto",
    publish_evidence: bool = False,
) -> dict[str, Any]:
    mode_key = str(mode or "auto").strip().lower()
    if mode_key not in {"auto", "full"}:
        raise ValueError("mode must be auto or full")

    events = _repo_events(repo)
    current_lots = _repo_lots(repo)
    event_fingerprint = _fingerprint(events)
    current_fingerprint = _fingerprint(_fingerprint_input(current_lots))
    checkpoint = _load_checkpoint(base)
    now = utc_now_iso()

    if (
        mode_key == "auto"
        and isinstance(checkpoint, dict)
        and checkpoint.get("projection_contract_version") == PROJECTION_CONTRACT_VERSION
        and checkpoint.get("event_fingerprint") == event_fingerprint
        and checkpoint.get("position_lots_fingerprint") == current_fingerprint
    ):
        items = [{"status": "matched", "record_id": item.get("record_id")} for item in current_lots]
        report = {
            "schema_kind": SCHEMA_KIND,
            "schema_version": "1.0",
            "report_id": f"projection-verify-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
            "generated_at_utc": now,
            "ok": True,
            "mode_requested": mode_key,
            "mode_used": "checkpoint_reuse",
            "checkpoint_reused": True,
            "checkpoint_id": checkpoint.get("checkpoint_id"),
            "evidence_published": bool(publish_evidence),
            "projection_contract_version": PROJECTION_CONTRACT_VERSION,
            "source_of_truth": "trade_events",
            "projection": "position_lots",
            "event_count": len(events),
            "position_lot_count": len(current_lots),
            "event_fingerprint": event_fingerprint,
            "position_lots_fingerprint": current_fingerprint,
            "projection_fingerprint": checkpoint.get("projection_fingerprint"),
            "summary": {"matched": len(items)},
            "items": items,
            # The rowid baseline rides on every report, this one included: A4's
            # pre/post comparison (§3) must be able to diff two archived reports,
            # and a reused checkpoint is still a valid "pre" read.
            "store_rowids": (
                {str(lot.get("record_id") or ""): lot.get("rowid") for lot in current_lots}
                if all("rowid" in lot for lot in current_lots)
                else None
            ),
        }
        if publish_evidence:
            _persist_report(base=base, report=report, checkpoint=None)
        return report

    projection = project_stored_trade_events_to_position_lots(events)
    projected_lots = _canonical_lots(projection.lots)
    comparison = compare_projection_lots(
        projected_lots=projected_lots,
        current_lots=current_lots,
        diagnostics=projection.diagnostics,
    )
    summary = comparison["summary"]
    # §8's predicate, not "anything that is not matched": the two non-blocking
    # verdict terms would otherwise start failing production paths that only read
    # ``ok``. Today the two are equal by construction.
    error_count = _blocking_count(summary)
    report = {
        "schema_kind": SCHEMA_KIND,
        "schema_version": "1.0",
        "report_id": f"projection-verify-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
        "generated_at_utc": now,
        "ok": error_count == 0,
        "mode_requested": mode_key,
        "mode_used": "full_replay",
        "checkpoint_reused": False,
        "evidence_published": bool(publish_evidence),
        "projection_contract_version": PROJECTION_CONTRACT_VERSION,
        "source_of_truth": "trade_events",
        "projection": "position_lots",
        "event_count": len(events),
        "position_lot_count": len(current_lots),
        "projected_lot_count": len(projected_lots),
        "event_fingerprint": event_fingerprint,
        "position_lots_fingerprint": current_fingerprint,
        "projection_fingerprint": _fingerprint(projected_lots),
        "projection_diagnostic_count": len(projection.diagnostics),
        "projection_error_count": sum(1 for item in projection.diagnostics if getattr(item, "severity", "") == "error"),
        **comparison,
    }
    next_checkpoint = _build_checkpoint(report=report, events=events, current_lots=current_lots) if report["ok"] else None
    if publish_evidence:
        _persist_report(base=base, report=report, checkpoint=next_checkpoint)
    return report | (
        {"checkpoint_id": next_checkpoint["checkpoint_id"]}
        if publish_evidence and next_checkpoint
        else {}
    )


__all__ = [
    "compare_projection_lots",
    "load_projection_verify_state",
    "verify_position_projection",
]
