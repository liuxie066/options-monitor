from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import src.application.trades.backfill as backfill_module
from src.application.trades.backfill import run_history_backfill
from src.application.trades.inbox import (
    list_retryable_trade_payloads,
    trade_inbox_summary,
)
from src.application.trades.state import load_trade_intake_state, write_trade_intake_state


class _FakeRepo:
    def __init__(self, events: list[dict[str, Any]] | None = None) -> None:
        self.events = list(events or [])

    def list_trade_events(self) -> list[dict[str, Any]]:
        return list(self.events)


@pytest.fixture(autouse=True)
def _healthy_lifecycle_discovery(monkeypatch) -> None:
    def _discover(_repo, *, account, observed_at_ms, apply_changes):
        return {
            "schema_version": "lifecycle_discovery_result.v2",
            "observed_at_ms": observed_at_ms,
            "account": account,
            "apply_changes": apply_changes,
            "created_case_ids": [],
            "would_create_case_ids": [],
            "discovered_case_ids": [],
            "refreshed_case_ids": [],
            "would_refresh_case_ids": [],
            "skipped_targeted_lot_ids": [],
        }

    monkeypatch.setattr(
        backfill_module,
        "discover_lifecycle_cases",
        _discover,
    )


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
    callback = lambda _context: {"status": "queued"}

    def _history_deals_fn(**_kwargs):
        return (
            [{"deal_id": "deal-1", "code": "HK.TCH260605P440000"}],
            {"window_start_utc": "2026-06-03T00:00:00+00:00", "window_end_utc": "2026-06-03T06:00:00+00:00"},
        )

    def _process_payload_fn(payload: dict[str, Any], **kwargs):
        processed.append(
            {
                "payload": payload,
                "source": kwargs.get("source"),
                "callback": kwargs.get("on_stock_holdings_sync_fn"),
            }
        )
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
        on_stock_holdings_sync_fn=callback,
    )

    assert out["ok"] is True
    assert out["deal_count"] == 1
    assert out["applied_count"] == 1
    assert len(processed) == 1
    assert processed[0]["payload"]["deal_id"] == "deal-1"
    assert processed[0]["payload"]["code"] == "HK.TCH260605P440000"
    assert processed[0]["payload"]["futu_account_id"] == "REAL_1"
    assert processed[0]["payload"]["internal_account"] == "lx"
    assert processed[0]["payload"]["_trade_intake_source"]["transport"] == "poll"
    assert processed[0]["payload"]["_trade_intake_source"]["opend_port"] == 11111
    assert processed[0]["source"] == "backfill"
    assert processed[0]["callback"] is callback
    phases = [event["phase"] for event in _audit_events(tmp_path / "audit.jsonl")]
    assert phases == [
        "backfill_check_started",
        "backfill_lifecycle_discovery_before",
        "backfill_received",
        "backfill_applied",
        "backfill_lifecycle_reconciliation_after",
        "backfill_check_finished",
    ]


def test_backfill_lifecycle_discovery_is_scoped_to_single_mapped_account(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[str | None] = []

    def _discover(_repo, *, account, observed_at_ms, apply_changes):
        calls.append(account)
        return {
            "schema_version": "lifecycle_discovery_result.v2",
            "observed_at_ms": observed_at_ms,
            "account": account,
            "apply_changes": apply_changes,
            "created_case_ids": [],
            "would_create_case_ids": [],
            "discovered_case_ids": [],
            "refreshed_case_ids": [],
            "would_refresh_case_ids": [],
            "skipped_targeted_lot_ids": [],
        }

    monkeypatch.setattr(
        backfill_module,
        "discover_lifecycle_cases",
        _discover,
    )
    out = run_history_backfill(
        **_backfill_kwargs(tmp_path),
        history_deals_fn=lambda **_kwargs: ([], {}),
        process_payload_fn=lambda *_args, **_kwargs: {},
    )

    assert calls == ["lx", "lx"]
    lifecycle = out["diagnostics"]["lifecycle_reconciliation"]
    assert lifecycle["before"]["accounts"] == ["lx"]
    assert lifecycle["after"]["accounts"] == ["lx"]
    assert lifecycle["before"]["schema_version"] == (
        "lifecycle_discovery_result.v2"
    )
    assert lifecycle["after"]["schema_version"] == (
        "lifecycle_discovery_result.v2"
    )
    assert lifecycle["before"]["account_results"][0]["account"] == "lx"
    assert lifecycle["after"]["account_results"][0]["account"] == "lx"
    audits = _audit_events(tmp_path / "audit.jsonl")
    lifecycle_audits = [
        event
        for event in audits
        if event["phase"]
        in {
            "backfill_lifecycle_discovery_before",
            "backfill_lifecycle_reconciliation_after",
        }
    ]
    assert all(event["ok"] is True for event in lifecycle_audits)
    assert all(event["result"]["accounts"] == ["lx"] for event in lifecycle_audits)
    assert all(
        event["result"]["schema_version"]
        == "lifecycle_discovery_result.v2"
        for event in lifecycle_audits
    )
    assert all("accounts" not in event for event in lifecycle_audits)


def test_backfill_lifecycle_discovery_scopes_legacy_source_per_account(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[str | None] = []

    def _discover(_repo, *, account, observed_at_ms, apply_changes):
        calls.append(account)
        return {
            "schema_version": "lifecycle_discovery_result.v2",
            "observed_at_ms": observed_at_ms,
            "account": account,
            "apply_changes": apply_changes,
            "created_case_ids": [f"created-{account}"],
            "would_create_case_ids": [],
            "discovered_case_ids": [f"discovered-{account}"],
            "refreshed_case_ids": [],
            "would_refresh_case_ids": [],
            "skipped_targeted_lot_ids": [f"lot-{account}"],
        }

    monkeypatch.setattr(
        backfill_module,
        "discover_lifecycle_cases",
        _discover,
    )
    kwargs = _backfill_kwargs(tmp_path)
    kwargs["account_mapping"] = {
        "REAL_2": "sy",
        "REAL_1": "LX",
    }
    kwargs["futu_account_ids"] = ["REAL_2", "REAL_1"]
    out = run_history_backfill(
        **kwargs,
        history_deals_fn=lambda **_kwargs: ([], {}),
        process_payload_fn=lambda *_args, **_kwargs: {},
    )

    assert calls == ["lx", "sy", "lx", "sy"]
    before = out["diagnostics"]["lifecycle_reconciliation"]["before"]
    assert before["ok"] is True
    assert before["accounts"] == ["lx", "sy"]
    assert [item["account"] for item in before["account_results"]] == [
        "lx",
        "sy",
    ]
    assert before["created_case_ids"] == ["created-lx", "created-sy"]
    assert before["discovered_case_ids"] == [
        "discovered-lx",
        "discovered-sy",
    ]
    assert before["skipped_targeted_lot_ids"] == ["lot-lx", "lot-sy"]


def test_backfill_lifecycle_discovery_rejects_incomplete_account_scope_without_partial_scan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        backfill_module,
        "discover_lifecycle_cases",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("incomplete account scope must not scan any account")
        ),
    )
    kwargs = _backfill_kwargs(tmp_path)
    kwargs["account_mapping"] = {"REAL_1": "lx"}
    kwargs["futu_account_ids"] = ["REAL_1", "REAL_2"]
    out = run_history_backfill(
        **kwargs,
        history_deals_fn=lambda **_kwargs: (
            [
                {
                    "deal_id": "deal-unmapped",
                    "futu_account_id": "REAL_2",
                }
            ],
            {},
        ),
        process_payload_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unmapped payload must remain unresolved")
        ),
    )

    assert out["ok"] is False
    assert out["error"] == "lifecycle_discovery_incomplete"
    assert out["deal_count"] == 1
    assert out["unresolved_count"] == 1
    assert out["diagnostics"]["lifecycle_discovery_complete"] is False
    lifecycle = out["diagnostics"]["lifecycle_reconciliation"]
    for phase in ("before", "after"):
        assert lifecycle[phase]["ok"] is False
        assert lifecycle[phase]["reason"] == (
            "lifecycle_account_scope_incomplete"
        )
        assert lifecycle[phase]["accounts"] == []
        assert lifecycle[phase]["account_results"] == []
        assert "REAL_2" in lifecycle[phase]["error"]
    lifecycle_audits = [
        event
        for event in _audit_events(tmp_path / "audit.jsonl")
        if event["phase"]
        in {
            "backfill_lifecycle_discovery_before",
            "backfill_lifecycle_reconciliation_after",
        }
    ]
    assert all(event["ok"] is False for event in lifecycle_audits)
    assert all("REAL_2" in event["error"] for event in lifecycle_audits)
    assert all(event["result"]["accounts"] == [] for event in lifecycle_audits)
    phases = [
        event["phase"]
        for event in _audit_events(tmp_path / "audit.jsonl")
    ]
    assert "backfill_received" in phases
    assert "backfill_identity_needs_review" in phases


def test_run_history_backfill_skips_processed_outbox_managed_duplicate_before_pipeline(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    write_trade_intake_state(
        state_path,
        {
            "processed_deal_ids": {
                "deal-1": {
                    "status": "applied",
                    "reason": "applied_open",
                    "receipt": {
                        "status": "outbox_managed",
                        "reason": "transactional_outbox",
                        "delivery_confirmed": False,
                    },
                }
            },
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
    kwargs["on_result_fn"] = lambda _context: (_ for _ in ()).throw(
        AssertionError("duplicate backfill must not invoke receipt callback")
    )
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


def test_run_history_backfill_retries_retryable_unresolved_state(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    write_trade_intake_state(
        state_path,
        {
            "processed_deal_ids": {},
            "failed_deal_ids": {},
            "unresolved_deal_ids": {
                "deal-1": {
                    "status": "unresolved",
                    "reason": "missing_account_mapping",
                    "retryable": True,
                    "attempt_count": 1,
                }
            },
        },
    )
    processed: list[dict[str, Any]] = []

    def _history_deals_fn(**_kwargs):
        return ([{"deal_id": "deal-1"}], {})

    def _process_payload_fn(payload: dict[str, Any], **kwargs):
        processed.append({"payload": payload, "source": kwargs.get("source")})
        return {
            "status": "applied",
            "action": "open",
            "reason": "applied_open",
            "deal_id": payload["deal_id"],
            "account": "lx",
        }

    kwargs = _backfill_kwargs(tmp_path)
    kwargs["state_path"] = state_path
    out = run_history_backfill(
        **kwargs,
        history_deals_fn=_history_deals_fn,
        process_payload_fn=_process_payload_fn,
    )

    assert out["applied_count"] == 1
    assert out["skipped_duplicate_count"] == 0
    assert len(processed) == 1
    assert processed[0]["payload"]["deal_id"] == "deal-1"
    assert processed[0]["payload"]["futu_account_id"] == "REAL_1"
    assert processed[0]["payload"]["internal_account"] == "lx"
    assert processed[0]["source"] == "backfill"


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
    assert state["processed_deal_ids"][
        "futu:lx:REAL_1:deal-1"
    ]["status"] == "reconciled"
    assert state["processed_deal_ids"][
        "futu:lx:REAL_1:deal-1"
    ]["reason"] == "ledger_event_already_recorded"


def test_backfill_does_not_dedupe_same_deal_id_across_accounts(
    tmp_path: Path,
) -> None:
    processed: list[dict[str, Any]] = []

    def _history_deals_fn(**_kwargs):
        return (
            [{"deal_id": "same-id", "futu_account_id": "REAL_2"}],
            {},
        )

    def _process_payload_fn(payload: dict[str, Any], **_kwargs):
        processed.append(dict(payload))
        return {
            "status": "applied",
            "action": "open",
            "reason": "applied_open",
            "deal_id": payload["deal_id"],
            "account": "sy",
        }

    kwargs = _backfill_kwargs(tmp_path)
    kwargs["account_mapping"] = {"REAL_1": "lx", "REAL_2": "sy"}
    kwargs["futu_account_ids"] = ["REAL_1", "REAL_2"]
    kwargs["repo"] = _FakeRepo(
        [
            {
                "event_id": "futu:lx:REAL_1:same-id",
                "event_type": "open",
                "account": "lx",
                "raw_payload": {
                    "external_event_key": "futu:lx:REAL_1:same-id",
                    "source_deal_id": "same-id",
                    "futu_account_id": "REAL_1",
                    "broker_deal_completion": {
                        "split_count": 1,
                        "split_index": 1,
                        "expected_contracts": 1,
                        "allocated_contracts": 1,
                    },
                },
            }
        ]
    )
    out = run_history_backfill(
        **kwargs,
        history_deals_fn=_history_deals_fn,
        process_payload_fn=_process_payload_fn,
    )

    assert out["applied_count"] == 1
    assert out["skipped_duplicate_count"] == 0
    assert len(processed) == 1
    assert processed[0]["deal_id"] == "same-id"
    assert processed[0]["futu_account_id"] == "REAL_2"
    assert processed[0]["internal_account"] == "sy"
    assert processed[0]["_trade_intake_source"]["transport"] == "poll"


def test_run_history_backfill_does_not_treat_numeric_lot_lineage_as_deal_id(
    tmp_path: Path,
) -> None:
    processed: list[str] = []
    opening_deal_id = "9162790356868244299"
    closing_deal_id = "495287541148725639"

    def _history_deals_fn(**_kwargs):
        return ([{"deal_id": opening_deal_id}], {})

    def _process_payload_fn(payload: dict[str, Any], **_kwargs):
        processed.append(str(payload["deal_id"]))
        return {
            "status": "applied",
            "action": "open",
            "reason": "applied_open",
            "deal_id": payload["deal_id"],
            "account": "lx",
        }

    kwargs = _backfill_kwargs(tmp_path)
    kwargs["repo"] = _FakeRepo(
        [
            {
                "event_id": (
                    "futu:lx:281756479859383816:"
                    f"{closing_deal_id}:close:lot_futu:lx:281756479859383816:{opening_deal_id}"
                ),
                "event_type": "close",
                "raw_payload": {"source_deal_id": closing_deal_id},
            }
        ]
    )

    out = run_history_backfill(
        **kwargs,
        history_deals_fn=_history_deals_fn,
        process_payload_fn=_process_payload_fn,
    )

    assert out["applied_count"] == 1
    assert processed == [opening_deal_id]


def test_history_backfill_does_not_skip_incomplete_broker_close_split(
    tmp_path: Path,
) -> None:
    processed: list[str] = []

    kwargs = _backfill_kwargs(tmp_path)
    kwargs["repo"] = _FakeRepo(
        [
            {
                "event_id": "broker-close-deal-split-lot-1",
                "event_type": "close",
                "contracts": 1,
                "target_lot_id": "lot-1",
                "raw_payload": {
                    "source_deal_id": "deal-split",
                    "broker_deal_completion": {
                        "source_deal_id": "deal-split",
                        "expected_contracts": 2,
                        "split_count": 2,
                        "split_index": 1,
                        "allocated_contracts": 1,
                    },
                },
            }
        ]
    )

    out = run_history_backfill(
        **kwargs,
        history_deals_fn=lambda **_kwargs: ([{"deal_id": "deal-split"}], {}),
        process_payload_fn=lambda payload, **_kwargs: (
            processed.append(str(payload["deal_id"]))
            or {
                "status": "applied",
                "action": "close",
                "reason": "applied_close",
                "deal_id": payload["deal_id"],
                "account": "lx",
            }
        ),
    )

    assert out["applied_count"] == 1
    assert processed == ["deal-split"]


def test_history_backfill_extends_window_from_persisted_checkpoint(
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}
    checkpoint_path = tmp_path / "backfill_checkpoint.json"
    checkpoint_path.write_text(
        json.dumps(
            {
                "last_successful_window_end_utc": "2026-06-01T00:00:00+00:00"
            }
        ),
        encoding="utf-8",
    )

    def _history_deals_fn(**kwargs):
        captured.update(kwargs)
        return (
            [],
            {
                "window_start_utc": "2026-05-31T23:00:00+00:00",
                "window_end_utc": "2026-06-03T06:00:00+00:00",
                "account_results": [
                    {
                        "futu_account_id": "REAL_1",
                        "ret": 0,
                        "row_count": 0,
                    }
                ],
            },
        )

    out = run_history_backfill(
        **_backfill_kwargs(tmp_path),
        checkpoint_path=checkpoint_path,
        history_deals_fn=_history_deals_fn,
        process_payload_fn=lambda *_args, **_kwargs: {},
    )

    assert captured["lookback_hours"] == 55.0
    assert out["ok"] is True
    assert out["diagnostics"]["checkpoint_advanced"] is True
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert (
        checkpoint["last_successful_window_end_utc"]
        == "2026-06-03T06:00:00+00:00"
    )


def test_history_backfill_does_not_advance_checkpoint_on_partial_account_query(
    tmp_path: Path,
) -> None:
    checkpoint_path = tmp_path / "backfill_checkpoint.json"
    original = {
        "last_successful_window_end_utc": "2026-06-01T00:00:00+00:00"
    }
    checkpoint_path.write_text(json.dumps(original), encoding="utf-8")

    def _history_deals_fn(**_kwargs):
        return (
            [],
            {
                "window_end_utc": "2026-06-03T06:00:00+00:00",
                "account_results": [
                    {
                        "futu_account_id": "REAL_1",
                        "ret": -1,
                        "row_count": 0,
                        "error": "trade context unavailable",
                    }
                ],
            },
        )

    out = run_history_backfill(
        **_backfill_kwargs(tmp_path),
        checkpoint_path=checkpoint_path,
        history_deals_fn=_history_deals_fn,
        process_payload_fn=lambda *_args, **_kwargs: {},
    )

    assert out["ok"] is False
    assert out["error"] == "history_query_incomplete"
    assert out["diagnostics"]["checkpoint_advanced"] is False
    assert json.loads(checkpoint_path.read_text(encoding="utf-8")) == original


def test_history_backfill_keeps_unexpected_pipeline_exception_in_durable_inbox(
    tmp_path: Path,
) -> None:
    inbox_path = tmp_path / "trade_inbox.sqlite3"

    def _history_deals_fn(**_kwargs):
        return ([{"deal_id": "deal-crash"}], {})

    out = run_history_backfill(
        **_backfill_kwargs(tmp_path),
        inbox_path=inbox_path,
        history_deals_fn=_history_deals_fn,
        process_payload_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("unexpected pipeline crash")
        ),
    )

    assert out["failed_count"] == 1
    assert trade_inbox_summary(inbox_path)["pending_count"] == 1
    retry_rows = list_retryable_trade_payloads(
        inbox_path,
        retry_delay_sec=0,
    )
    assert retry_rows[0]["payload"]["deal_id"] == "deal-crash"
    assert "unexpected pipeline crash" in retry_rows[0]["last_error"]


def test_history_backfill_handles_lifecycle_pending_in_durable_inbox(
    tmp_path: Path,
) -> None:
    inbox_path = tmp_path / "trade_inbox.sqlite3"

    out = run_history_backfill(
        **_backfill_kwargs(tmp_path),
        inbox_path=inbox_path,
        history_deals_fn=lambda **_kwargs: ([{"deal_id": "deal-waiting"}], {}),
        process_payload_fn=lambda *_args, **_kwargs: {
            "status": "unresolved",
            "action": "lifecycle",
            "reason": "waiting_settlement_evidence",
            "deal_id": "deal-waiting",
            "account": "lx",
            "diagnostics": {
                "retryable": True,
                "broker_evidence_accepted": True,
            },
        },
    )

    assert out["unresolved_count"] == 1
    assert trade_inbox_summary(inbox_path)["pending_count"] == 0
    assert trade_inbox_summary(inbox_path)["handled_count"] == 1
    assert list_retryable_trade_payloads(inbox_path, retry_delay_sec=0) == []
