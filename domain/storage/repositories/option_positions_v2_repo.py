from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from domain.domain.option_positions_v2 import (
    normalize_position_event,
    normalize_position_snapshot,
)


def _state_dir(base: Path) -> Path:
    out = (Path(base).resolve() / "output_shared" / "state" / "option_positions_v2").resolve()
    out.mkdir(parents=True, exist_ok=True)
    return out


def _current_dir(base: Path) -> Path:
    out = (_state_dir(base) / "current").resolve()
    out.mkdir(parents=True, exist_ok=True)
    return out


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _append_jsonl(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return path


def append_position_snapshot(base: Path, payload: dict[str, Any]) -> dict[str, Path]:
    snapshot = normalize_position_snapshot(payload)
    out = {
        "events": _append_jsonl(_state_dir(base) / "snapshots.jsonl", snapshot),
        "current": _write_json(
            _current_dir(base) / f"snapshot.{snapshot['snapshot_type']}.latest.json",
            snapshot,
        ),
        "snapshot": _write_json(
            _state_dir(base) / "snapshots" / f"{snapshot['snapshot_id']}.json",
            snapshot,
        ),
    }
    return out


def append_position_event(base: Path, payload: dict[str, Any]) -> dict[str, Path]:
    event = normalize_position_event(payload)
    return {
        "events": _append_jsonl(_state_dir(base) / "events.jsonl", event),
        "current": _write_json(_current_dir(base) / "event.latest.json", event),
        "event": _write_json(_state_dir(base) / "events" / f"{event['event_id']}.json", event),
    }


def write_current_projection(base: Path, payload: dict[str, Any]) -> Path:
    return _write_json(_current_dir(base) / "projection.current.json", payload)


def write_reconciliation_report(base: Path, payload: dict[str, Any]) -> dict[str, Path]:
    report_id = str((payload or {}).get("report_id") or "reconciliation").strip()
    return {
        "report": _write_json(
            _state_dir(base) / "reconciliation_reports" / f"{report_id}.json",
            payload,
        ),
        "current": _write_json(_current_dir(base) / "reconciliation.latest.json", payload),
    }


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if isinstance(item, dict):
            out.append(item)
    return out


def load_position_snapshots(base: Path) -> list[dict[str, Any]]:
    return _load_jsonl(_state_dir(base) / "snapshots.jsonl")


def load_position_events(base: Path) -> list[dict[str, Any]]:
    return _load_jsonl(_state_dir(base) / "events.jsonl")
