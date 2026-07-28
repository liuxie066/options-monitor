from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import src.application.position_advice_notification_authority as notification_authority
from domain.domain.position_advice_authority import scope_for
from src.application.position_advice_authority_service import (
    PositionAdviceAuthorityError,
    apply_authority_change,
    build_identity_binding_evidence,
    plan_authority_change,
)
from src.application.position_advice_notification_authority import (
    PositionAdviceNotificationAuthorityError,
    build_notification_authority_token,
    execute_notification_with_authority,
    resolve_notification_unknown,
    unresolved_notification_authority_exists,
)


NOW = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
IDENTITY = "a" * 64


def _binding() -> dict[str, object]:
    return build_identity_binding_evidence(
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
        authoring_config_hash="b" * 64,
        market_bindings=[
            {
                "market": "US",
                "generated_config_hash": "c" * 64,
                "source_receipt_hash": "d" * 64,
                "normalized_account": "lx",
                "normalized_portfolio_source": "futu",
                "portfolio_account_identity_hash": IDENTITY,
                "source_receipt_fresh": True,
            }
        ],
    )


def _apply(
    base: Path,
    *,
    mode: str,
    expected_hash: str = "absent",
) -> dict[str, object]:
    return apply_authority_change(
        base=base,
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
        target_mode=mode,
        expected_policy_hash=expected_hash,
        actor="operator@example",
        requested_at=NOW,
        confirm=True,
        identity_binding_evidence=_binding() if expected_hash == "absent" else None,
    )


def _token(
    policy: dict[str, object],
    *,
    account_run_id: str = "run-1",
) -> dict[str, object]:
    mode = str(policy["mode"])
    return build_notification_authority_token(
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
        selected_advice_contract="v1" if mode != "v2" else "v2",
        resolved_mode=mode,
        authority_generation=int(policy["generation"]),
        authority_policy_hash=str(policy["policy_hash"]),
        account_run_id=account_run_id,
    )


def test_notification_receipt_accepts_once_and_suppresses_duplicate(
    tmp_path: Path,
) -> None:
    applied = _apply(tmp_path, mode="v2_shadow")
    token = _token(dict(applied["policy"]))
    calls: list[str] = []

    def send() -> dict[str, object]:
        calls.append("sent")
        return {
            "ok": True,
            "command_ok": True,
            "delivery_confirmed": True,
            "message_id": "m-1",
            "idempotency_key": "provider-1",
        }

    first = execute_notification_with_authority(
        base=tmp_path,
        token=token,
        channel="feishu",
        send=send,
        now=NOW,
    )
    second = execute_notification_with_authority(
        base=tmp_path,
        token=token,
        channel="feishu",
        send=send,
        now=NOW,
    )

    assert first["authority_receipt_status"] == "accepted"
    assert second["authority_duplicate_suppressed"] is True
    assert calls == ["sent"]
    assert unresolved_notification_authority_exists(
        base=tmp_path,
        portfolio_scope_id=scope_for("lx"),
    ) is False
    duplicate_calls: list[str] = []
    duplicate = execute_notification_with_authority(
        base=tmp_path,
        token=token,
        channel="feishu",
        send=lambda: duplicate_calls.append("sent") or {
            "ok": True,
            "delivery_confirmed": True,
        },
        now=NOW,
    )
    assert duplicate["authority_duplicate_suppressed"] is True
    assert duplicate_calls == []


def test_notification_token_fails_closed_after_authority_generation_change(
    tmp_path: Path,
) -> None:
    first = _apply(tmp_path, mode="v1")
    token = _token(dict(first["policy"]))
    _apply(
        tmp_path,
        mode="v2_shadow",
        expected_hash=str(first["policy"]["policy_hash"]),
    )

    with pytest.raises(
        PositionAdviceNotificationAuthorityError,
        match="generation changed",
    ):
        execute_notification_with_authority(
            base=tmp_path,
            token=token,
            channel="feishu",
            send=lambda: {"ok": True, "delivery_confirmed": True},
            now=NOW,
        )


def test_unknown_delivery_is_append_only_and_requires_manual_resolution(
    tmp_path: Path,
) -> None:
    applied = _apply(tmp_path, mode="v2_shadow")
    token = _token(dict(applied["policy"]))
    result = execute_notification_with_authority(
        base=tmp_path,
        token=token,
        channel="feishu",
        send=lambda: {
            "ok": False,
            "command_ok": True,
            "delivery_confirmed": False,
            "error_code": "SEND_UNCONFIRMED",
            "ambiguous_send": True,
        },
        now=NOW,
    )
    receipt_id = str(result["authority_receipt_id"])
    assert result["authority_receipt_status"] == "unknown"
    assert unresolved_notification_authority_exists(
        base=tmp_path,
        portfolio_scope_id=scope_for("lx"),
    ) is True

    plan = resolve_notification_unknown(
        base=tmp_path,
        normalized_account="lx",
        receipt_id=receipt_id,
        resolution="delivered",
        evidence={"provider_audit_id": "audit-1"},
        actor="operator@example",
        resolved_at=NOW,
        confirm=False,
        dry_run=True,
    )
    assert plan["would_change"] is True
    assert not Path(str(plan["resolution_path"])).exists()

    with pytest.raises(
        PositionAdviceNotificationAuthorityError,
        match="explicit confirm",
    ):
        resolve_notification_unknown(
            base=tmp_path,
            normalized_account="lx",
            receipt_id=receipt_id,
            resolution="delivered",
            evidence={"provider_audit_id": "audit-1"},
            actor="operator@example",
            resolved_at=NOW,
            confirm=False,
            dry_run=False,
        )

    resolved = resolve_notification_unknown(
        base=tmp_path,
        normalized_account="lx",
        receipt_id=receipt_id,
        resolution="delivered",
        evidence={"provider_audit_id": "audit-1"},
        actor="operator@example",
        resolved_at=NOW,
        confirm=True,
        dry_run=False,
    )
    assert resolved["status"] == "applied"
    assert unresolved_notification_authority_exists(
        base=tmp_path,
        portfolio_scope_id=scope_for("lx"),
    ) is False

    with pytest.raises(
        PositionAdviceNotificationAuthorityError,
        match="conflicts",
    ):
        resolve_notification_unknown(
            base=tmp_path,
            normalized_account="lx",
            receipt_id=receipt_id,
            resolution="failed",
            evidence={"provider_audit_id": "audit-2"},
            actor="operator@example",
            resolved_at=NOW,
            confirm=True,
            dry_run=False,
        )


def test_unknown_resolution_reconciles_bound_daily_brief_delivery(
    tmp_path: Path,
) -> None:
    from src.application.daily_decision_brief_repository import (
        persist_daily_decision_brief_success,
        prepare_daily_decision_brief_delivery,
        read_daily_decision_brief_delivery_state,
        record_daily_decision_brief_delivery_attempt,
    )
    from src.application.notification_delivery_adapter import (
        build_notification_transport_key,
    )

    applied = _apply(tmp_path, mode="v2_shadow")
    token = _token(dict(applied["policy"]), account_run_id="brief-run")
    brief = {
        "market": "US",
        "market_trading_date": "2026-07-27",
        "account": "lx",
        "revision": 1,
        "run_id": "brief-run",
        "generated_at_utc": NOW.isoformat(),
        "data_as_of_utc": NOW.isoformat(),
        "valid_until_utc": (NOW + timedelta(hours=6)).isoformat(),
        "status": "ready",
        "actionability": "live_actionable",
        "strategy_summary": "test",
        "actions": [],
        "positions": [],
        "capacity": {},
        "candidates": {
            "sell_put": [],
            "covered_call": [],
            "combo_yield": [],
        },
        "rejections": {},
        "events": [],
        "data_gaps": [],
        "source_artifacts": [],
    }
    persisted = persist_daily_decision_brief_success(
        base=tmp_path,
        brief=brief,
    )
    envelope = prepare_daily_decision_brief_delivery(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date="2026-07-27",
        run_id="brief-run",
        delivery_kind="fixed_report",
        source_kind="successful_brief",
        revision=persisted["current_revision"],
        source_digest=persisted["current_brief_digest"],
        scheduled_target_market="2026-07-27T10:00:00-04:00",
        candidate_identities=persisted["current_candidate_identities"],
        rendered_message="# pending delivery",
        render_context={"projection": "fixed_report"},
        prepared_at_utc=NOW.isoformat(),
    )["envelope"]
    transport_key = build_notification_transport_key(
        str(envelope["delivery_key"])
    )
    delivery_identity = {
        "account": "lx",
        "market": "US",
        "market_trading_date": "2026-07-27",
        "delivery_key": envelope["delivery_key"],
        "source_digest": envelope["source_digest"],
        "message_sha256": envelope["message_sha256"],
        "transport_idempotency_key": transport_key,
    }
    result = execute_notification_with_authority(
        base=tmp_path,
        token=token,
        channel="feishu",
        delivery_identity=delivery_identity,
        send=lambda: {
            "ok": False,
            "command_ok": False,
            "delivery_confirmed": False,
            "error_code": "SEND_UNCONFIRMED",
            "ambiguous_send": True,
        },
        now=NOW,
    )
    record_daily_decision_brief_delivery_attempt(
        base=tmp_path,
        **delivery_identity,
        ambiguous=True,
    )

    with pytest.raises(
        PositionAdviceNotificationAuthorityError,
        match="explicit confirm",
    ):
        resolve_notification_unknown(
            base=tmp_path,
            normalized_account="lx",
            receipt_id=str(result["authority_receipt_id"]),
            resolution="delivered",
            evidence={"provider_audit_id": "audit-brief"},
            actor="operator@example",
            resolved_at=NOW + timedelta(minutes=1),
            confirm=False,
            dry_run=False,
        )
    unresolved_state = read_daily_decision_brief_delivery_state(
        base=tmp_path,
        account="lx",
        market="US",
    )["state"]
    assert (
        unresolved_state["days"]["2026-07-27"]["fixed_reports"][
            "2026-07-27T10:00:00-04:00"
        ]["status"]
        == "ambiguous"
    )

    resolved = resolve_notification_unknown(
        base=tmp_path,
        normalized_account="lx",
        receipt_id=str(result["authority_receipt_id"]),
        resolution="delivered",
        evidence={"provider_audit_id": "audit-brief"},
        actor="operator@example",
        resolved_at=NOW + timedelta(minutes=1),
        confirm=True,
        dry_run=False,
    )

    assert resolved["delivery_reconciliation"]["available"] is True
    assert (
        resolved["delivery_reconciliation"]["envelope"]["status"]
        == "confirmed"
    )
    state = read_daily_decision_brief_delivery_state(
        base=tmp_path,
        account="lx",
        market="US",
    )["state"]
    assert (
        state["days"]["2026-07-27"]["fixed_reports"][
            "2026-07-27T10:00:00-04:00"
        ]["status"]
        == "confirmed"
    )


def test_unknown_resolved_failed_allows_next_attempt(
    tmp_path: Path,
) -> None:
    applied = _apply(tmp_path, mode="v2_shadow")
    token = _token(dict(applied["policy"]))
    first = execute_notification_with_authority(
        base=tmp_path,
        token=token,
        channel="feishu",
        send=lambda: {
            "ok": False,
            "command_ok": True,
            "delivery_confirmed": False,
            "error_code": "SEND_UNCONFIRMED",
            "ambiguous_send": True,
        },
        now=NOW,
    )
    resolve_notification_unknown(
        base=tmp_path,
        normalized_account="lx",
        receipt_id=str(first["authority_receipt_id"]),
        resolution="failed",
        evidence={"provider_audit_id": "audit-failed"},
        actor="operator@example",
        resolved_at=NOW,
        confirm=True,
        dry_run=False,
    )

    retried = execute_notification_with_authority(
        base=tmp_path,
        token=token,
        channel="feishu",
        send=lambda: {
            "ok": True,
            "command_ok": True,
            "delivery_confirmed": True,
            "message_id": "m-retry",
        },
        now=NOW + timedelta(minutes=1),
    )

    assert retried["authority_receipt_status"] == "accepted"
    assert retried["authority_receipt_id"] != first["authority_receipt_id"]
    assert unresolved_notification_authority_exists(
        base=tmp_path,
        portfolio_scope_id=scope_for("lx"),
    ) is False


def test_expired_stranded_inflight_can_be_resolved_and_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    applied = _apply(tmp_path, mode="v2_shadow")
    token = _token(dict(applied["policy"]))
    original_write = notification_authority._write_once_or_verify

    def crash_before_terminal(path: Path, payload: dict) -> None:
        if payload.get("status") != "inflight":
            raise RuntimeError("simulated process crash")
        original_write(path, payload)

    monkeypatch.setattr(
        notification_authority,
        "_write_once_or_verify",
        crash_before_terminal,
    )
    with pytest.raises(RuntimeError, match="simulated process crash"):
        execute_notification_with_authority(
            base=tmp_path,
            token=token,
            channel="feishu",
            send=lambda: {
                "ok": True,
                "command_ok": True,
                "delivery_confirmed": True,
            },
            now=NOW,
        )
    monkeypatch.setattr(
        notification_authority,
        "_write_once_or_verify",
        original_write,
    )
    inflight_paths = list(
        (
            tmp_path
            / "output_shared"
            / "state"
            / "position_advice"
            / scope_for("lx")
            / "notification_authority"
            / "inflight"
        ).glob("*.json")
    )
    assert len(inflight_paths) == 1
    intent = json.loads(inflight_paths[0].read_text(encoding="utf-8"))

    with pytest.raises(
        PositionAdviceNotificationAuthorityError,
        match="lease has not expired",
    ):
        resolve_notification_unknown(
            base=tmp_path,
            normalized_account="lx",
            receipt_id=str(intent["receipt_id"]),
            resolution="failed",
            evidence={"provider_audit_id": "audit-inflight"},
            actor="operator@example",
            resolved_at=NOW + timedelta(minutes=4),
            confirm=True,
            dry_run=False,
        )

    resolve_notification_unknown(
        base=tmp_path,
        normalized_account="lx",
        receipt_id=str(intent["receipt_id"]),
        resolution="failed",
        evidence={"provider_audit_id": "audit-inflight"},
        actor="operator@example",
        resolved_at=NOW + timedelta(minutes=6),
        confirm=True,
        dry_run=False,
    )
    retried = execute_notification_with_authority(
        base=tmp_path,
        token=token,
        channel="feishu",
        send=lambda: {
            "ok": True,
            "command_ok": True,
            "delivery_confirmed": True,
            "message_id": "m-after-crash",
        },
        now=NOW + timedelta(minutes=7),
    )
    assert retried["authority_receipt_status"] == "accepted"
    assert retried["authority_receipt_id"] != intent["receipt_id"]


def test_stranded_inflight_blocks_promotion_plan(tmp_path: Path) -> None:
    first = _apply(tmp_path, mode="v2_shadow")
    state_dir = (
        tmp_path
        / "output_shared"
        / "state"
        / "position_advice"
        / scope_for("lx")
        / "notification_authority"
        / "inflight"
    )
    state_dir.mkdir(parents=True)
    (state_dir / f"{'e' * 64}.json").write_text("{}", encoding="utf-8")

    plan = plan_authority_change(
        base=tmp_path,
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
        target_mode="v2",
        expected_policy_hash=str(first["policy"]["policy_hash"]),
        actor="operator@example",
        requested_at=NOW,
        promotion_evidence={"schema_version": "wrong"},
    )

    assert "notification_authority_unknown_unresolved" in plan["reason_codes"]
    with pytest.raises(PositionAdviceAuthorityError):
        apply_authority_change(
            base=tmp_path,
            normalized_account="lx",
            normalized_portfolio_source="futu",
            portfolio_account_identity_hash=IDENTITY,
            target_mode="v2",
            expected_policy_hash=str(first["policy"]["policy_hash"]),
            actor="operator@example",
            requested_at=NOW,
            confirm=True,
            promotion_evidence={"schema_version": "wrong"},
        )
