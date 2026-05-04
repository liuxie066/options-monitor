from __future__ import annotations

from pathlib import Path

from scripts.trade_intake_state import (
    append_trade_intake_audit,
    is_retryable_unresolved_deal,
    load_trade_intake_state,
    lookup_deal_state_entry,
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
    assert lookup_deal_state_entry(loaded, "deal-1")[0] == "processed_deal_ids"


def test_trade_intake_audit_appends_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    append_trade_intake_audit(path, {"phase": "received", "deal_id": "deal-1"})
    append_trade_intake_audit(path, {"phase": "resolved", "deal_id": "deal-1"})

    lines = path.read_text(encoding="utf-8").strip().splitlines()

    assert len(lines) == 2
    assert '"phase": "received"' in lines[0]


def test_retryable_unresolved_state_is_distinguishable_from_terminal_state() -> None:
    state = upsert_deal_state(
        {},
        bucket="unresolved_deal_ids",
        deal_id="deal-retry-1",
        payload={"status": "unresolved", "retryable": True, "attempt_count": 1},
    )
    terminal = upsert_deal_state(
        state,
        bucket="processed_deal_ids",
        deal_id="deal-done-1",
        payload={"status": "applied", "action": "open", "account": "lx"},
    )

    assert is_retryable_unresolved_deal(terminal, "deal-retry-1") is True
    assert lookup_deal_state_entry(terminal, "deal-retry-1")[0] == "unresolved_deal_ids"
    assert is_retryable_unresolved_deal(terminal, "deal-done-1") is False
