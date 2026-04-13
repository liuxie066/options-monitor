from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.io_utils import atomic_write_json, read_json


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _events_path(state_dir: Path) -> Path:
    return (state_dir / "fetch_required_data.events.jsonl").resolve()


def _snapshots_path(state_dir: Path) -> Path:
    return (state_dir / "fetch_required_data.snapshots.json").resolve()


def _current_path(state_dir: Path) -> Path:
    return (state_dir / "current" / "fetch_required_data.current.json").resolve()


def _load_obj(path: Path) -> dict[str, Any]:
    obj = read_json(path, {})
    return obj if isinstance(obj, dict) else {}


def _write_current(state_dir: Path, payload: dict[str, Any]) -> Path:
    p = _current_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(p, payload)
    return p


def record_fetch_snapshot(
    *,
    state_dir: Path,
    symbol: str,
    source: str,
    status: str,
    reason: str = "",
    fallback_used: bool = False,
    meta: dict[str, Any] | None = None,
) -> None:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return
    state_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "schema_kind": "required_data_fetch_snapshot",
        "schema_version": "1.0",
        "symbol": sym,
        "source": str(source or "unknown"),
        "status": str(status or "unknown"),
        "reason": str(reason or ""),
        "fallback_used": bool(fallback_used),
        "as_of_utc": _utc_now(),
        "meta": (meta if isinstance(meta, dict) else {}),
    }

    events = _events_path(state_dir)
    events.parent.mkdir(parents=True, exist_ok=True)
    with events.open("a", encoding="utf-8") as f:
        f.write(json.dumps(snapshot, ensure_ascii=False) + "\n")

    snapshots = _load_obj(_snapshots_path(state_dir))
    symbols = snapshots.get("symbols")
    if not isinstance(symbols, dict):
        symbols = {}
    symbols[sym] = snapshot
    snapshots["symbols"] = symbols
    snapshots["updated_at_utc"] = _utc_now()
    atomic_write_json(_snapshots_path(state_dir), snapshots)

    current = _load_obj(_current_path(state_dir))
    cur_syms = current.get("symbols")
    if not isinstance(cur_syms, dict):
        cur_syms = {}
    cur_syms[sym] = snapshot
    current["symbols"] = cur_syms
    current["updated_at_utc"] = _utc_now()
    _write_current(state_dir, current)


def read_symbol_fetch_current(*, state_dir: Path, symbol: str) -> dict[str, Any] | None:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    cur = _load_obj(_current_path(state_dir))
    symbols = cur.get("symbols")
    if not isinstance(symbols, dict):
        return None
    out = symbols.get(sym)
    return out if isinstance(out, dict) else None


def backfill_symbol_snapshot_from_raw(
    *,
    required_data_dir: Path,
    state_dir: Path,
    symbol: str,
    source: str,
) -> dict[str, Any] | None:
    _ = required_data_dir
    _ = source
    return read_symbol_fetch_current(state_dir=state_dir, symbol=symbol)
