from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.application.ledger.publisher import project_stored_trade_events_to_position_lots


SCHEMA_KIND = "option_positions_projection_verify"
CHECKPOINT_SCHEMA_KIND = "option_positions_projection_verify_checkpoint"


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


def _reports_dir(base: Path) -> Path:
    out = _state_dir(base) / "projection_verify"
    out.mkdir(parents=True, exist_ok=True)
    return out


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
    return {
        "record_id": str(payload.get("record_id") or "").strip(),
        "fields": dict(fields) if isinstance(fields, dict) else {},
    }


def _canonical_lots(rows: list[Any]) -> list[dict[str, Any]]:
    return sorted((_lot_dict(row) for row in rows), key=lambda item: str(item.get("record_id") or ""))


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
    projected = _canonical_lots(projected_lots)
    current = _canonical_lots(current_lots)
    projected_by_id = {str(item.get("record_id") or ""): item for item in projected}
    current_by_id = {str(item.get("record_id") or ""): item for item in current}

    items: list[dict[str, Any]] = _projection_error_items(diagnostics)
    for record_id in sorted(set(projected_by_id) | set(current_by_id)):
        projected_item = projected_by_id.get(record_id)
        current_item = current_by_id.get(record_id)
        if projected_item is None:
            items.append({"status": "extra_in_position_lots", "record_id": record_id, "current": current_item})
            continue
        if current_item is None:
            items.append({"status": "missing_in_position_lots", "record_id": record_id, "projected": projected_item})
            continue
        if _canonical_payload(projected_item.get("fields")) != _canonical_payload(current_item.get("fields")):
            items.append(
                {
                    "status": "field_mismatch",
                    "record_id": record_id,
                    "projected_fields": projected_item.get("fields"),
                    "current_fields": current_item.get("fields"),
                }
            )
            continue
        items.append({"status": "matched", "record_id": record_id})

    summary: dict[str, int] = {}
    for item in items:
        status = str(item.get("status") or "")
        summary[status] = int(summary.get(status) or 0) + 1
    return {"summary": summary, "items": items}


def _build_checkpoint(*, report: dict[str, Any], events: list[dict[str, Any]], current_lots: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_kind": CHECKPOINT_SCHEMA_KIND,
        "schema_version": "1.0",
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


def verify_position_projection(*, base: Path, repo: Any, mode: str = "auto") -> dict[str, Any]:
    mode_key = str(mode or "auto").strip().lower()
    if mode_key not in {"auto", "full"}:
        raise ValueError("mode must be auto or full")

    events = _repo_events(repo)
    current_lots = _repo_lots(repo)
    event_fingerprint = _fingerprint(events)
    current_fingerprint = _fingerprint(current_lots)
    checkpoint = _load_checkpoint(base)
    now = utc_now_iso()

    if (
        mode_key == "auto"
        and isinstance(checkpoint, dict)
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
            "source_of_truth": "trade_events",
            "projection": "position_lots",
            "event_count": len(events),
            "position_lot_count": len(current_lots),
            "event_fingerprint": event_fingerprint,
            "position_lots_fingerprint": current_fingerprint,
            "projection_fingerprint": checkpoint.get("projection_fingerprint"),
            "summary": {"matched": len(items)},
            "items": items,
        }
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
    error_count = sum(count for key, count in summary.items() if key != "matched")
    report = {
        "schema_kind": SCHEMA_KIND,
        "schema_version": "1.0",
        "report_id": f"projection-verify-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
        "generated_at_utc": now,
        "ok": error_count == 0,
        "mode_requested": mode_key,
        "mode_used": "full_replay",
        "checkpoint_reused": False,
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
    _persist_report(base=base, report=report, checkpoint=next_checkpoint)
    return report | ({"checkpoint_id": next_checkpoint["checkpoint_id"]} if next_checkpoint else {})


__all__ = [
    "compare_projection_lots",
    "load_projection_verify_state",
    "verify_position_projection",
]
