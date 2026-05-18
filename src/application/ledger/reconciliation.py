from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from domain.domain.ledger.identity import ContractKey
from domain.domain.ledger.position_fields import (
    effective_contracts_open,
    effective_expiration_ymd,
    effective_multiplier,
    effective_strike,
    normalize_currency,
    normalize_status,
)


SNAPSHOT_TYPE_VERIFICATION = "verification"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_dir(base: Path) -> Path:
    out = Path(base).resolve() / "output_shared" / "state" / "option_positions"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _current_dir(base: Path) -> Path:
    out = _state_dir(base) / "current"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _snapshots_dir(base: Path) -> Path:
    out = _state_dir(base) / "snapshots"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _reports_dir(base: Path) -> Path:
    out = _state_dir(base) / "reconciliation"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _required_text(value: Any, field_name: str) -> str:
    text = _text(value)
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _contracts(value: Any, *, field_name: str) -> int:
    try:
        number = int(float(value))
    except Exception as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if number < 0:
        raise ValueError(f"{field_name} must be >= 0")
    return number


def _contract_key(fields: dict[str, Any]) -> ContractKey:
    return ContractKey.from_values(
        broker=fields.get("broker") or fields.get("market"),
        account=fields.get("account"),
        underlying_symbol=fields.get("symbol") or fields.get("underlying_symbol"),
        option_type=fields.get("option_type"),
        position_side=fields.get("side") or fields.get("position_side"),
        strike=effective_strike(fields),
        expiration_ymd=fields.get("expiration_ymd") or effective_expiration_ymd(fields),
    )


def normalize_verification_snapshot(raw: dict[str, Any] | Any) -> dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    snapshot_id = _required_text(src.get("snapshot_id"), "snapshot_id")
    snapshot_type = _text(src.get("snapshot_type")).lower() or SNAPSHOT_TYPE_VERIFICATION
    if snapshot_type != SNAPSHOT_TYPE_VERIFICATION:
        raise ValueError("verification snapshot is required for reconciliation")
    raw_lots = src.get("lots")
    if not isinstance(raw_lots, list):
        raise ValueError("lots must be a list")

    lots: list[dict[str, Any]] = []
    for index, item in enumerate(raw_lots):
        fields = item if isinstance(item, dict) else {}
        key = _contract_key(fields)
        raw_contracts = fields.get("contracts", fields.get("contracts_open", 0))
        lots.append(
            {
                "snapshot_lot_id": _text(fields.get("snapshot_lot_id")) or f"{snapshot_id}:{index}",
                "snapshot_id": snapshot_id,
                **key.to_dict(),
                "contracts": _contracts(raw_contracts, field_name="contracts"),
                "currency": normalize_currency(fields.get("currency")),
                "multiplier": effective_multiplier(fields),
                "source_record_id": _text(fields.get("source_record_id")) or None,
                "evidence_ref": _text(fields.get("evidence_ref")) or None,
                "note": _text(fields.get("note")) or None,
            }
        )

    return {
        "schema_kind": "option_positions_verification_snapshot",
        "schema_version": "1.0",
        "snapshot_id": snapshot_id,
        "snapshot_type": SNAPSHOT_TYPE_VERIFICATION,
        "acceptance_status": _text(src.get("acceptance_status")).lower() or "accepted",
        "snapshot_at_utc": _text(src.get("snapshot_at_utc")) or utc_now_iso(),
        "source_name": _required_text(src.get("source_name"), "source_name"),
        "source_type": _text(src.get("source_type")) or "manual_verification",
        "note": src.get("note"),
        "lots": lots,
        "lot_count": len(lots),
    }


def _current_open_lots(repo: Any) -> list[dict[str, Any]]:
    list_position_lots = getattr(repo, "list_position_lots", None)
    if not callable(list_position_lots):
        raise TypeError("option_positions repo does not satisfy read repository interface")
    rows = list_position_lots()
    if not isinstance(rows, list):
        return []

    lots: list[dict[str, Any]] = []
    for row in rows:
        item = row if isinstance(row, dict) else {}
        raw_fields = item.get("fields")
        fields: dict[str, Any] = raw_fields if isinstance(raw_fields, dict) else {}
        record_id = _text(item.get("record_id") or fields.get("record_id"))
        if not record_id or normalize_status(fields.get("status")) == "close":
            continue
        contracts_open = effective_contracts_open(fields)
        if contracts_open <= 0:
            continue
        key = _contract_key(fields)
        lots.append(
            {
                "record_id": record_id,
                **key.to_dict(),
                "contracts": int(contracts_open),
                "currency": normalize_currency(fields.get("currency")),
                "multiplier": effective_multiplier(fields),
                "source_event_id": fields.get("source_event_id"),
            }
        )
    return lots


def reconcile_snapshot_against_current_lots(
    verification_snapshot: dict[str, Any] | Any,
    current_lots: list[dict[str, Any]],
) -> dict[str, Any]:
    snapshot = normalize_verification_snapshot(verification_snapshot)
    current_by_key = {str(item.get("position_key") or ""): item for item in current_lots}
    snapshot_by_key = {str(item.get("position_key") or ""): item for item in snapshot["lots"]}

    items: list[dict[str, Any]] = []
    for key, lot in snapshot_by_key.items():
        current = current_by_key.get(key)
        if current is None:
            items.append(
                {
                    "status": "missing_in_projection",
                    "position_key": key,
                    "snapshot_contracts": lot["contracts"],
                    "projected_contracts": 0,
                    "snapshot_lot_id": lot["snapshot_lot_id"],
                }
            )
            continue

        mismatched_fields: list[str] = []
        if current.get("currency") != lot.get("currency"):
            mismatched_fields.append("currency")
        if current.get("multiplier") != lot.get("multiplier"):
            mismatched_fields.append("multiplier")
        if mismatched_fields:
            items.append(
                {
                    "status": "field_mismatch",
                    "position_key": key,
                    "record_id": current.get("record_id"),
                    "snapshot_contracts": lot["contracts"],
                    "projected_contracts": current["contracts"],
                    "mismatched_fields": mismatched_fields,
                }
            )
            continue

        if int(current["contracts"]) != int(lot["contracts"]):
            items.append(
                {
                    "status": "quantity_mismatch",
                    "position_key": key,
                    "record_id": current.get("record_id"),
                    "snapshot_contracts": lot["contracts"],
                    "projected_contracts": current["contracts"],
                    "source_event_id": current.get("source_event_id"),
                }
            )
            continue

        items.append(
            {
                "status": "matched",
                "position_key": key,
                "record_id": current.get("record_id"),
                "snapshot_contracts": lot["contracts"],
                "projected_contracts": current["contracts"],
            }
        )

    for key, current in current_by_key.items():
        if key in snapshot_by_key:
            continue
        items.append(
            {
                "status": "missing_in_snapshot",
                "position_key": key,
                "record_id": current.get("record_id"),
                "snapshot_contracts": 0,
                "projected_contracts": current["contracts"],
                "source_event_id": current.get("source_event_id"),
            }
        )

    items.sort(key=lambda item: (str(item.get("status") or ""), str(item.get("position_key") or "")))
    summary: dict[str, int] = {}
    for item in items:
        status = str(item.get("status") or "")
        summary[status] = int(summary.get(status, 0)) + 1

    return {
        "schema_kind": "option_positions_reconciliation",
        "schema_version": "1.0",
        "source_of_truth": "trade_events",
        "projection": "position_lots",
        "report_id": f"{snapshot['snapshot_id']}@position_lots",
        "snapshot_id": snapshot["snapshot_id"],
        "generated_at_utc": utc_now_iso(),
        "summary": summary,
        "items": items,
    }


def _verification_snapshot_count(base: Path) -> tuple[int, int]:
    path = _state_dir(base) / "verification_snapshots.jsonl"
    if not path.exists():
        return 0, 0
    count = 0
    accepted = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        count += 1
        if str(payload.get("acceptance_status") or "").strip().lower() == "accepted":
            accepted += 1
    return count, accepted


def persist_reconciliation_state(*, base: Path, snapshot: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    state_dir = _state_dir(base)
    current_dir = _current_dir(base)
    _append_jsonl(state_dir / "verification_snapshots.jsonl", snapshot)
    _write_json(current_dir / "snapshot.verification.latest.json", snapshot)
    _write_json(_snapshots_dir(base) / f"{snapshot['snapshot_id']}.json", snapshot)
    _write_json(current_dir / "reconciliation.latest.json", report)
    _write_json(_reports_dir(base) / f"{report['report_id'].replace('/', '_')}.json", report)
    verification_count, accepted_count = _verification_snapshot_count(base)
    return {
        "latest_verification_snapshot_id": snapshot["snapshot_id"],
        "verification_snapshot_count": verification_count,
        "accepted_verification_snapshot_count": accepted_count,
    }


def load_latest_reconciliation_report(*, base: Path) -> dict[str, Any] | None:
    return _read_json(_current_dir(base) / "reconciliation.latest.json")


def load_reconciliation_state(*, base: Path) -> dict[str, Any]:
    latest_snapshot = _read_json(_current_dir(base) / "snapshot.verification.latest.json")
    latest_report = load_latest_reconciliation_report(base=base)
    verification_count, accepted_count = _verification_snapshot_count(base)
    return {
        "latest_verification_snapshot": latest_snapshot,
        "latest_reconciliation_report": latest_report,
        "verification_snapshot_count": verification_count,
        "accepted_verification_snapshot_count": accepted_count,
    }


def reconcile_option_positions_snapshot(
    *,
    base: Path,
    repo: Any,
    verification_snapshot: dict[str, Any],
) -> dict[str, Any]:
    snapshot = normalize_verification_snapshot(verification_snapshot)
    current_lots = _current_open_lots(repo)
    report = reconcile_snapshot_against_current_lots(snapshot, current_lots)
    state = persist_reconciliation_state(base=base, snapshot=snapshot, report=report)
    return report | state | {"post_reconcile_open_position_count": len(current_lots)}
