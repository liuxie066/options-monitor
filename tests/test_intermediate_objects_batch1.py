from __future__ import annotations

from pathlib import Path


def test_snapshot_decision_delivery_plan_roundtrip() -> None:
    from om.domain import Decision, DeliveryPlan, SnapshotDTO

    snapshot = SnapshotDTO.from_payload(
        {
            "schema_kind": "snapshot_dto",
            "schema_version": "1.0",
            "snapshot_name": "scheduler_decision",
            "as_of_utc": "2026-04-13T00:00:00+00:00",
            "payload": {"decision": {"schema_kind": "scheduler_decision", "schema_version": "1.0"}},
        }
    )
    assert snapshot.to_payload()["schema_kind"] == "snapshot_dto"

    decision = Decision.from_payload(
        {
            "schema_kind": "decision",
            "schema_version": "1.0",
            "account": "lx",
            "should_run": True,
            "should_notify": False,
            "reason": "ok",
        }
    )
    assert decision.to_payload()["account"] == "lx"

    plan = DeliveryPlan.from_payload(
        {
            "schema_kind": "delivery_plan",
            "schema_version": "1.0",
            "channel": "openclaw",
            "target": "user:abc",
            "account_messages": {"lx": "hello"},
            "should_send": True,
        }
    )
    assert plan.to_payload()["account_messages"]["lx"] == "hello"


def test_delivery_plan_validation_blocks_empty_target() -> None:
    from om.domain import DeliveryPlan, SchemaValidationError

    try:
        DeliveryPlan.from_payload(
            {
                "schema_kind": "delivery_plan",
                "schema_version": "1.0",
                "channel": "openclaw",
                "target": "",
                "account_messages": {"lx": "hello"},
                "should_send": True,
            }
        )
        assert False, "expected SchemaValidationError"
    except SchemaValidationError:
        pass


def test_main_uses_intermediate_objects_in_critical_path() -> None:
    base = Path(__file__).resolve().parents[1]
    src = (base / "scripts" / "multi_tick" / "main.py").read_text(encoding="utf-8")
    assert "SnapshotDTO.from_payload" in src
    assert "Decision.from_payload" in src
    assert "DeliveryPlan.from_payload" in src
    assert "SCHEMA_VALIDATION_FAILED" in src


def test_snapshot_schema_error_has_stable_error_code() -> None:
    from om.domain import SchemaValidationError, SnapshotDTO

    try:
        SnapshotDTO.from_payload(
            {
                "schema_kind": "snapshot_dto",
                "schema_version": "1.0",
                "snapshot_name": "bad",
                "as_of_utc": "2026-04-13T00:00:00+00:00",
                "payload": [],
            }
        )
        assert False, "expected SchemaValidationError"
    except SchemaValidationError as e:
        assert "E_SNAPSHOT_PAYLOAD_INVALID" in str(e)


def test_decision_schema_error_has_stable_error_code_for_bool_field() -> None:
    from om.domain import Decision, SchemaValidationError

    try:
        Decision.from_payload(
            {
                "schema_kind": "decision",
                "schema_version": "1.0",
                "account": "lx",
                "should_run": "yes",
                "should_notify": False,
                "reason": "bad",
            }
        )
        assert False, "expected SchemaValidationError"
    except SchemaValidationError as e:
        assert "E_DECISION_SHOULD_RUN_INVALID" in str(e)


def test_delivery_plan_schema_error_has_stable_error_code_for_message_type() -> None:
    from om.domain import DeliveryPlan, SchemaValidationError

    try:
        DeliveryPlan.from_payload(
            {
                "schema_kind": "delivery_plan",
                "schema_version": "1.0",
                "channel": "openclaw",
                "target": "user:abc",
                "account_messages": {"lx": 1},
                "should_send": True,
            }
        )
        assert False, "expected SchemaValidationError"
    except SchemaValidationError as e:
        assert "E_DELIVERY_ACCOUNT_MESSAGE_INVALID" in str(e)
