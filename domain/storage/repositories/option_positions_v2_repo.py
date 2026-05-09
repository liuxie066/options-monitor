from __future__ import annotations

import json
from pathlib import Path
import shutil
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


def _snapshots_dir(base: Path) -> Path:
    out = (_state_dir(base) / "snapshots").resolve()
    out.mkdir(parents=True, exist_ok=True)
    return out


def _events_dir(base: Path) -> Path:
    out = (_state_dir(base) / "events").resolve()
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
            _snapshots_dir(base) / f"{snapshot['snapshot_id']}.json",
            snapshot,
        ),
    }
    return out


def append_position_event(base: Path, payload: dict[str, Any]) -> dict[str, Path]:
    event = normalize_position_event(payload)
    return {
        "events": _append_jsonl(_state_dir(base) / "events.jsonl", event),
        "current": _write_json(_current_dir(base) / "event.latest.json", event),
        "event": _write_json(_events_dir(base) / f"{event['event_id']}.json", event),
    }


def write_current_projection(base: Path, payload: dict[str, Any]) -> Path:
    return _write_json(_current_dir(base) / "projection.current.json", payload)


def write_reconciliation_report(base: Path, payload: dict[str, Any]) -> dict[str, Path]:
    report_id = str((payload or {}).get("report_id") or "reconciliation").strip()
    if report_id == "reconciliation":
        generated_at = str((payload or {}).get("generated_at_utc") or "").strip().replace(":", "-")
        report_id = f"reconciliation-{generated_at or 'latest'}"
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


def _replace_json_dir_contents(path: Path, payloads: list[dict[str, Any]], *, id_field: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = (path.parent / f".{path.name}.staging").resolve()
    backup = (path.parent / f".{path.name}.backup").resolve()
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    for item in payloads:
        identifier = str(item.get(id_field) or "").strip()
        if not identifier:
            continue
        _write_json(staging / f"{identifier}.json", item)
    if path.exists():
        path.rename(backup)
    staging.rename(path)
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)


def replace_position_snapshots(base: Path, payloads: list[dict[str, Any]]) -> dict[str, Path | None]:
    snapshots = [normalize_position_snapshot(item) for item in payloads]
    jsonl_path = _state_dir(base) / "snapshots.jsonl"
    jsonl_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in snapshots),
        encoding="utf-8",
    )
    _replace_json_dir_contents(_snapshots_dir(base), snapshots, id_field="snapshot_id")
    current_path = None
    latest_by_type: dict[str, dict[str, Any]] = {}
    for snapshot in snapshots:
        latest_by_type[str(snapshot.get("snapshot_type") or "")] = snapshot
    for snapshot_type, snapshot in latest_by_type.items():
        current_path = _write_json(
            _current_dir(base) / f"snapshot.{snapshot_type}.latest.json",
            snapshot,
        )
    return {"events": jsonl_path, "current": current_path}


def replace_position_events(base: Path, payloads: list[dict[str, Any]]) -> dict[str, Path | None]:
    events = [normalize_position_event(item) for item in payloads]
    jsonl_path = _state_dir(base) / "events.jsonl"
    jsonl_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in events),
        encoding="utf-8",
    )
    _replace_json_dir_contents(_events_dir(base), events, id_field="event_id")
    current_path = None
    if events:
        current_path = _write_json(_current_dir(base) / "event.latest.json", events[-1])
    return {"events": jsonl_path, "current": current_path}
