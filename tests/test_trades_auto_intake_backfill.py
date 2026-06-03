from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.application.trades.backfill import run_history_backfill
from src.application.trades.state import load_trade_intake_state, write_trade_intake_state


class _FakeRepo:
    def __init__(self, events: list[dict[str, Any]] | None = None) -> None:
        self.events = list(events or [])

    def list_trade_events(self) -> list[dict[str, Any]]:
        return list(self.events)


def _backfill_kwargs(tmp_path: Path) -> dict[str, Any]:
    return {
        "repo": _FakeRepo(),
        "state_path": tmp_path / "state.json",
        "audit_path": tmp_path / "audit.jsonl",
        "account_mapping": {"REAL_1": "lx"},
        "futu_account_ids": ["REAL_1"],
        "apply_changes": True,
        "host": "127.0.0.1",
        "port": 11111,
        "config": {},
        "config_path": tmp_path / "config.json",
        "runtime_root": tmp_path,
        "backfill_config": {"lookback_hours": 6},
        "on_result_fn": None,
        "now_fn": lambda: datetime(2026, 6, 3, 6, 0, tzinfo=timezone.utc),
    }


def _audit_events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_run_history_backfill_processes_missing_deal_through_pipeline(tmp_path: Path) -> None:
    processed: list[dict[str, Any]] = []

    def _history_deals_fn(**_kwargs):
        return (
            [{"deal_id": "deal-1", "code": "HK.TCH260605P440000"}],
            {"window_start_utc": "2026-06-03T00:00:00+00:00", "window_end_utc": "2026-06-03T06:00:00+00:00"},
        )

    def _process_payload_fn(payload: dict[str, Any], **kwargs):
        processed.append({"payload": payload, "source": kwargs.get("source")})
        return {
            "status": "applied",
            "action": "open",
            "reason": "applied_open",
            "deal_id": payload["deal_id"],
            "account": "lx",
        }

    out = run_history_backfill(
        **_backfill_kwargs(tmp_path),
        history_deals_fn=_history_deals_fn,
        process_payload_fn=_process_payload_fn,
    )

    assert out["ok"] is True
    assert out["deal_count"] == 1
    assert out["applied_count"] == 1
    assert processed == [{"payload": {"deal_id": "deal-1", "code": "HK.TCH260605P440000"}, "source": "backfill"}]
    phases = [event["phase"] for event in _audit_events(tmp_path / "audit.jsonl")]
    assert phases == ["backfill_check_started", "backfill_received", "backfill_applied", "backfill_check_finished"]


def test_run_history_backfill_skips_state_duplicate_before_pipeline(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    write_trade_intake_state(
        state_path,
        {
            "processed_deal_ids": {"deal-1": {"status": "applied", "reason": "applied_open"}},
            "failed_deal_ids": {},
            "unresolved_deal_ids": {},
        },
    )

    def _history_deals_fn(**_kwargs):
        return ([{"deal_id": "deal-1"}], {})

    def _process_payload_fn(_payload: dict[str, Any], **_kwargs):
        raise AssertionError("duplicate should not enter process pipeline")

    kwargs = _backfill_kwargs(tmp_path)
    kwargs["state_path"] = state_path
    out = run_history_backfill(
        **kwargs,
        history_deals_fn=_history_deals_fn,
        process_payload_fn=_process_payload_fn,
    )

    assert out["applied_count"] == 0
    assert out["skipped_duplicate_count"] == 1
    events = _audit_events(tmp_path / "audit.jsonl")
    skipped = [event for event in events if event["phase"] == "backfill_skipped_duplicate"]
    assert skipped == [{"phase": "backfill_skipped_duplicate", "source": "backfill", "deal_id": "deal-1", "reason": "state:processed_deal_ids"}]


def test_run_history_backfill_marks_ledger_duplicate_processed_without_pipeline(tmp_path: Path) -> None:
    def _history_deals_fn(**_kwargs):
        return ([{"deal_id": "deal-1"}], {})

    def _process_payload_fn(_payload: dict[str, Any], **_kwargs):
        raise AssertionError("ledger duplicate should not enter process pipeline")

    kwargs = _backfill_kwargs(tmp_path)
    kwargs["repo"] = _FakeRepo([{"event_id": "deal-1", "raw_payload": {"source_deal_id": "deal-1"}}])
    out = run_history_backfill(
        **kwargs,
        history_deals_fn=_history_deals_fn,
        process_payload_fn=_process_payload_fn,
    )

    assert out["applied_count"] == 0
    assert out["skipped_duplicate_count"] == 1
    state = load_trade_intake_state(tmp_path / "state.json")
    assert state["processed_deal_ids"]["deal-1"]["status"] == "reconciled"
    assert state["processed_deal_ids"]["deal-1"]["reason"] == "ledger_event_already_recorded"
