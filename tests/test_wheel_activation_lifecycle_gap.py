from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

import src.application.config_authoring_transaction as publishing
from src.application.agent_tool_contracts import AgentToolError
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.writer import persist_trade_event_objects_atomically
from src.application.wheel.read_model import build_wheel_read_model
from src.application.wheel.scanning import run_wheel_call_scan
from src.application.wheel.workflows import (
    create_wheel_call_intent,
    end_wheel_lifecycle,
)
from tests.test_wheel_activation_operation import _apply, _call, _deployment
from tests.test_wheel_assignment_companions import (
    _assignment_payload,
    _put_event,
    _trusted_multiplier_payload,
)


def _window_descriptor(window: dict) -> dict:
    return {
        "market": window["market"],
        "account": window["account"],
        "generation": window["generation"],
        "activated_at_ms": window["activated_at_ms"],
        "deactivated_at_ms": window["deactivated_at_ms"],
        "policy_sha256": window["policy_hash"],
    }


def _persist_put_assignment(
    repo: SQLiteOptionPositionsRepository,
    *,
    prefix: str,
    opened_at_ms: int,
    assigned_at_ms: int,
) -> tuple[dict, str, object]:
    lot_id = f"{prefix}-put-lot"
    open_event_id = f"{prefix}-put-open"
    open_event = replace(
        _put_event(
            event_id=open_event_id,
            event_type="open",
            multiplier=100,
            raw_payload=_trusted_multiplier_payload(open_event_id),
        ),
        event_time_ms=opened_at_ms,
        lot_id=lot_id,
    )
    assignment_event_id = f"{prefix}-put-assignment"
    assignment_payload = _assignment_payload(100)
    assignment_payload["target_lot_id"] = lot_id
    assignment_event = replace(
        _put_event(
            event_id=assignment_event_id,
            event_type="assignment",
            multiplier=100,
            raw_payload=assignment_payload,
        ),
        event_time_ms=assigned_at_ms,
        target_lot_id=lot_id,
    )
    persist_trade_event_objects_atomically(repo, [open_event])
    result = persist_trade_event_objects_atomically(repo, [assignment_event])[0]
    return result.to_dict(), f"assigned-stock-{assignment_event_id}", assignment_event


def test_publish_failure_preserves_historical_assignment_rules_and_same_request_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _deployment(tmp_path, monkeypatch)
    preview = _call(root)
    publisher = publishing.publish_yaml_config_generation_locked
    monkeypatch.setattr(
        publishing,
        "publish_yaml_config_generation_locked",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("publish fault")),
    )

    with patch(
        "src.application.ledger.repository_assigned_stock.now_ms",
        return_value=500,
    ), pytest.raises(AgentToolError) as failure:
        _call(
            root,
            apply=True,
            sha=preview["expected_source_sha256"],
        )

    failed = failure.value.details
    durable_window = failed["latest_window"]
    assert failed["failure_phase"] == "config_publish"
    assert failed["window_receipt"]["write_applied"] is True
    assert durable_window["generation"] == 1
    assert durable_window["activated_at_ms"] == 500
    assert failed["membership"] is False
    assert failed["ready"] is False

    repo = SQLiteOptionPositionsRepository(failed["paths"]["sqlite_path"])
    assignment, stock_lot_id, _assignment_event = _persist_put_assignment(
        repo,
        prefix="publish-gap",
        opened_at_ms=1_000,
        assigned_at_ms=2_000,
    )
    wheel_events = repo.list_wheel_events(account="lx")
    assert assignment["wheel_event_id"] == wheel_events[0]["event_id"]
    assert (
        _window_descriptor(wheel_events[0]["payload"]["activation_window"])
        == durable_window
    )

    unavailable_status = _call(root, action="status")
    assert unavailable_status["reason_code"] == "missing_descriptor"
    assert unavailable_status["monitoring_gate"] == "disabled"
    unavailable = build_wheel_read_model(
        repo,
        "lx",
        2_500,
        monitoring_readiness=unavailable_status["readiness"],
        market="us",
    )
    branch = unavailable["wheel_branches"][0]
    assert _window_descriptor(branch["activation_window"]) == durable_window

    scan = run_wheel_call_scan(
        unavailable,
        {},
        {"frames": {}},
        {},
        {},
        decision_time_ms=2_500,
    )
    assert scan["scope_results"][0]["reason_code"] == "wheel_disabled"
    assert scan["capacity_claims"] == []

    before_intent = repo.list_wheel_events(account="lx")
    with pytest.raises(ValueError, match="wheel_disabled: descriptor_mismatch"):
        create_wheel_call_intent(
            repo,
            candidate_snapshot={},
            account="lx",
            stock_lot_id=stock_lot_id,
            final_candidate_id="publish-gap-candidate",
            expected_snapshot_hash="publish-gap-snapshot",
            expected_batch_generation_hash=branch["branch_generation_hash"],
            expires_at_ms=10_000,
            request_id="publish-gap-intent",
            actor="tester",
            coverage_fact={},
            new_intent_enabled=True,
            market="us",
            activation_descriptor=None,
            policy_sha256="",
            apply_changes=True,
            as_of_ms=2_500,
        )
    assert repo.list_wheel_events(account="lx") == before_intent

    ended = end_wheel_lifecycle(
        repo,
        account="lx",
        stock_lot_id=stock_lot_id,
        expected_batch_generation_hash=branch["branch_generation_hash"],
        request_id="publish-gap-end",
        actor="tester",
        market="us",
        apply_changes=True,
        as_of_ms=2_600,
    )
    assert ended["lifecycle_status_after"] == "manual_ended"

    monkeypatch.setattr(
        publishing,
        "publish_yaml_config_generation_locked",
        publisher,
    )
    recovered = _call(
        root,
        apply=True,
        sha=preview["expected_source_sha256"],
    )
    assert recovered["ready"] is True
    assert recovered["window_receipt"]["write_applied"] is False
    assert recovered["expected_config_descriptor"] == durable_window
    assert len(repo.list_wheel_activation_windows(market="us", account="lx")) == 1

    with patch(
        "src.application.ledger.repository_assigned_stock.now_ms",
        return_value=3_000,
    ):
        disabled = _apply(
            root,
            action="disable",
            generation=1,
            request="publish-gap-disable",
        )
    assert disabled["latest_window"]["deactivated_at_ms"] == 3_000

    with patch(
        "src.application.ledger.repository_assigned_stock.now_ms",
        return_value=5_000,
    ):
        reenabled = _apply(
            root,
            generation=1,
            request="publish-gap-reenable",
        )
    assert reenabled["latest_window"]["generation"] == 2
    assert reenabled["latest_window"]["activated_at_ms"] == 5_000

    late, _late_stock_lot_id, _late_event = _persist_put_assignment(
        repo,
        prefix="late-active-window",
        opened_at_ms=2_100,
        assigned_at_ms=2_500,
    )
    late_branch_event = next(
        event
        for event in repo.list_wheel_events(account="lx")
        if event["event_id"] == late["wheel_event_id"]
    )
    assert late_branch_event["payload"]["activation_window"]["generation"] == 1
    assert (
        late_branch_event["payload"]["activation_window"]["deactivated_at_ms"]
        == 3_000
    )

    before_gap = repo.list_wheel_events(account="lx")
    gap, _gap_stock_lot_id, gap_event = _persist_put_assignment(
        repo,
        prefix="closed-window-gap",
        opened_at_ms=3_500,
        assigned_at_ms=4_000,
    )
    assert gap.get("wheel_event_id") is None
    assert repo.list_wheel_events(account="lx") == before_gap

    replay = persist_trade_event_objects_atomically(repo, [gap_event])[0]
    assert replay.created is False
    assert repo.list_wheel_events(account="lx") == before_gap
