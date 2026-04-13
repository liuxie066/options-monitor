from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def test_state_repo_source_snapshot_events_and_current_models() -> None:
    from domain.storage.repositories import state_repo

    root = (BASE / "tests" / ".tmp_phase3_source_snapshot_read_model").resolve()
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)

    snapshots = [
        {
            "schema_kind": "source_snapshot",
            "schema_version": "3.0",
            "source_name": "opend",
            "status": "ok",
            "as_of_utc": "2026-04-12T00:00:00+00:00",
            "fallback_used": False,
            "payload": {"symbol": "AAPL"},
        },
        {
            "schema_kind": "source_snapshot",
            "schema_version": "3.0",
            "source_name": "holdings",
            "status": "ok",
            "as_of_utc": "2026-04-12T00:01:00+00:00",
            "fallback_used": False,
            "payload": {"stocks_count": 2},
        },
        {
            "schema_kind": "source_snapshot",
            "schema_version": "3.0",
            "source_name": "option_positions",
            "status": "ok",
            "as_of_utc": "2026-04-12T00:02:00+00:00",
            "fallback_used": False,
            "payload": {"locked_symbols": 1},
        },
    ]

    for item in snapshots:
        state_repo.append_source_snapshot_event(root, item)

    events_path = (root / "output_shared" / "state" / "source_snapshots.events.jsonl").resolve()
    lines = [ln for ln in events_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 3

    cur_dir = (root / "output_shared" / "state" / "current").resolve()
    agg = json.loads((cur_dir / "source_snapshots.current.json").read_text(encoding="utf-8"))
    assert "updated_at_utc" in agg
    assert agg["opend"]["source_name"] == "opend"
    assert agg["holdings"]["source_name"] == "holdings"
    assert agg["option_positions"]["source_name"] == "option_positions"

    for src in ("opend", "holdings", "option_positions"):
        p = (cur_dir / f"source_snapshot.{src}.current.json").resolve()
        assert p.exists()
        d = json.loads(p.read_text(encoding="utf-8"))
        assert d["source_name"] == src


def main() -> None:
    test_state_repo_source_snapshot_events_and_current_models()
    print("OK (phase3-source-snapshot-read-model)")


if __name__ == "__main__":
    main()
