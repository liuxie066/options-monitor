from __future__ import annotations

from pathlib import Path

from scripts.trade_intake_state import (
    append_trade_intake_audit,
    load_trade_intake_state,
    lookup_deal_state,
    upsert_deal_state,
    write_trade_intake_state,
)


def test_trade_intake_state_round_trip(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state = upsert_deal_state(
        {},
        bucket="processed_deal_ids",
        deal_id="deal-1",
        payload={"status": "applied", "action": "open", "account": "lx"},
    )
    write_trade_intake_state(state_path, state)

    loaded = load_trade_intake_state(state_path)

    assert lookup_deal_state(loaded, "deal-1")["status"] == "applied"


def test_trade_intake_audit_appends_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    append_trade_intake_audit(path, {"phase": "received", "deal_id": "deal-1"})
    append_trade_intake_audit(path, {"phase": "resolved", "deal_id": "deal-1"})

    lines = path.read_text(encoding="utf-8").strip().splitlines()

    assert len(lines) == 2
    assert '"phase": "received"' in lines[0]
