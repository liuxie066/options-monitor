from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest


MARKET_DATE = "2026-07-21"
TARGET_1000 = "2026-07-21T10:00:00-04:00"
IDENTITY_NVDA = "candidate:v1:lx:US:NVDA:sell_put"
IDENTITY_AMD = "candidate:v1:lx:US:AMD:sell_put"


def _action(*, symbol: str = "NVDA", priority: str = "P1", contracts: int = 1) -> dict:
    return {
        "priority": priority,
        "state": "active",
        "action_type": "open_candidate",
        "strategy_family": "sell_put",
        "account": "lx",
        "symbol": symbol,
        "option_type": "put",
        "side": "short",
        "expiration": "2026-08-21",
        "strike": 100,
        "contract_symbol": f"{symbol}260821P00100000",
        "metrics": {
            "mid": 1.0,
            "capacity": {"contracts_available": contracts},
        },
    }


def _brief(
    *,
    run_id: str,
    actions: list[dict] | None = None,
    status: str = "ready",
    actionability: str = "live_actionable",
    account: str = "lx",
    market: str = "US",
    market_date: str = MARKET_DATE,
) -> dict:
    return {
        "market": market,
        "market_trading_date": market_date,
        "account": account,
        "revision": 999,
        "run_id": run_id,
        "generated_at_utc": "2026-07-21T14:00:00+00:00",
        "data_as_of_utc": "2026-07-21T13:59:00+00:00",
        "valid_until_utc": "2026-07-21T20:00:00+00:00",
        "status": status,
        "actionability": actionability,
        "strategy_summary": "test",
        "actions": list(actions or []),
        "positions": [],
        "capacity": {
            "sell_put": {"contracts_available": 2},
            "covered_call": {"contracts_available": 0},
        },
        "candidates": {"sell_put": [], "covered_call": [], "combo_yield": []},
        "rejections": {},
        "events": [],
        "data_gaps": [],
        "source_artifacts": [],
    }


def _persist(tmp_path: Path, *, run_id: str = "run-1", actions: list[dict] | None = None) -> dict:
    from src.application.daily_decision_brief_repository import persist_daily_decision_brief_success

    return persist_daily_decision_brief_success(
        base=tmp_path,
        brief=_brief(run_id=run_id, actions=actions),
    )


def _prepare_fixed(tmp_path: Path, persisted: dict, *, message: str = "# lx · 美股期权监控") -> dict:
    from src.application.daily_decision_brief_repository import prepare_daily_decision_brief_delivery

    return prepare_daily_decision_brief_delivery(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
        run_id=persisted["brief"]["run_id"],
        delivery_kind="fixed_report",
        source_kind="successful_brief",
        revision=persisted["current_revision"],
        source_digest=persisted["current_brief_digest"],
        scheduled_target_market=TARGET_1000,
        candidate_identities=persisted["current_candidate_identities"],
        rendered_message=message,
        render_context={"projection": "fixed_report"},
        prepared_at_utc="2026-07-21T14:00:02+00:00",
    )


def test_success_persistence_advances_only_reliable_current_and_returns_identity_delta(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import (
        persist_daily_decision_brief_success,
        read_latest_daily_decision_brief,
    )

    first = _persist(tmp_path, run_id="run-1", actions=[_action()])
    second = _persist(tmp_path, run_id="run-2", actions=[_action(), _action(symbol="AMD")])

    assert first["current_revision"] == 0
    assert first["newly_detected_candidate_identities"] == [IDENTITY_NVDA]
    assert second["previous_candidate_identities"] == [IDENTITY_NVDA]
    assert second["newly_detected_candidate_identities"] == [IDENTITY_AMD]
    current_before = second["paths"]["current"].read_bytes()

    with pytest.raises(ValueError, match="only ready or degraded"):
        persist_daily_decision_brief_success(
            base=tmp_path,
            brief=_brief(run_id="run-blocked", status="blocked", actionability="blocked"),
        )

    assert second["paths"]["current"].read_bytes() == current_before
    assert read_latest_daily_decision_brief(base=tmp_path, account="lx", market="US")["brief"]["revision"] == 1


def test_success_persistence_uses_empty_candidate_baseline_on_new_market_date(tmp_path: Path) -> None:
    first = _persist(tmp_path, run_id="day-1", actions=[_action()])
    from src.application.daily_decision_brief_repository import persist_daily_decision_brief_success

    second = persist_daily_decision_brief_success(
        base=tmp_path,
        brief=_brief(
            run_id="day-2",
            market_date="2026-07-22",
            actions=[_action()],
        ),
    )

    assert first["newly_detected_candidate_identities"] == [IDENTITY_NVDA]
    assert second["previous_successful_brief"]["market_trading_date"] == MARKET_DATE
    assert second["previous_candidate_identities"] == []
    assert second["newly_detected_candidate_identities"] == [IDENTITY_NVDA]


def test_v2_candidate_state_fixed_envelope_and_retry_read_round_trip(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import (
        read_daily_decision_brief_delivery_state,
        read_retryable_daily_decision_brief_delivery,
        record_daily_decision_brief_candidates,
    )

    persisted = _persist(tmp_path, actions=[_action(), _action(symbol="AMD")])
    recorded = record_daily_decision_brief_candidates(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
        revision=persisted["current_revision"],
        brief_digest=persisted["current_brief_digest"],
        candidate_identities=persisted["current_candidate_identities"],
        observed_at_utc="2026-07-21T14:00:01+00:00",
    )
    assert recorded["pending_candidate_identities"] == [IDENTITY_AMD, IDENTITY_NVDA]

    prepared = _prepare_fixed(tmp_path, persisted)
    envelope = prepared["envelope"]
    assert envelope["delivery_key"] == f"option-report:US:{MARKET_DATE}:lx:{TARGET_1000}"
    assert envelope["message_sha256"] == hashlib.sha256(envelope["rendered_message"].encode()).hexdigest()
    assert prepared["paths"]["run_plan"].name == "daily_decision_brief_delivery_plan.US.json"
    assert prepared["paths"]["run_plan"].parent == (
        tmp_path / "output_runs" / "run-1" / "accounts" / "lx" / "state"
    )

    before = prepared["paths"]["delivery"].read_bytes()
    read = read_daily_decision_brief_delivery_state(base=tmp_path, account="lx", market="US")
    retry = read_retryable_daily_decision_brief_delivery(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
    )
    assert read["available"] is True
    assert retry["reason"] == "pending_fixed"
    assert retry["envelope"] == envelope
    assert prepared["paths"]["delivery"].read_bytes() == before


def test_candidate_delivery_key_is_stable_and_pending_content_can_rotate(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import (
        DailyDecisionBriefStateError,
        prepare_daily_decision_brief_delivery,
        record_daily_decision_brief_candidates,
    )

    persisted = _persist(tmp_path, actions=[_action(), _action(symbol="AMD")])
    record_daily_decision_brief_candidates(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
        revision=persisted["current_revision"],
        brief_digest=persisted["current_brief_digest"],
        candidate_identities=persisted["current_candidate_identities"],
    )
    common = {
        "base": tmp_path,
        "account": "lx",
        "market": "US",
        "market_trading_date": MARKET_DATE,
        "run_id": "run-1",
        "delivery_kind": "candidate_alert",
        "source_kind": "successful_brief",
        "revision": persisted["current_revision"],
        "source_digest": persisted["current_brief_digest"],
        "render_context": {"projection": "candidate_alert"},
        "prepared_at_utc": "2026-07-21T14:30:02+00:00",
    }
    first = prepare_daily_decision_brief_delivery(
        **common,
        candidate_identities=[IDENTITY_NVDA],
        rendered_message="# 新增候选\nNVDA",
    )
    same = prepare_daily_decision_brief_delivery(
        **common,
        candidate_identities=[IDENTITY_NVDA],
        rendered_message="# 新增候选\nNVDA",
    )
    with pytest.raises(DailyDecisionBriefStateError, match="cannot change content"):
        prepare_daily_decision_brief_delivery(
            **common,
            candidate_identities=[IDENTITY_NVDA],
            rendered_message="# 新增候选\nNVDA changed",
        )
    rotated = prepare_daily_decision_brief_delivery(
        **{**common, "prepared_at_utc": "2026-07-21T14:30:12+00:00"},
        candidate_identities=[IDENTITY_AMD, IDENTITY_NVDA],
        rendered_message="# 新增候选\nAMD / NVDA",
    )

    assert first["prepared"] is True
    assert same["prepared"] is False
    assert rotated["prepared"] is True
    assert first["envelope"]["delivery_key"] != rotated["envelope"]["delivery_key"]
    assert rotated["envelope"]["candidate_identities"] == [IDENTITY_AMD, IDENTITY_NVDA]
    assert rotated["envelope"]["first_prepared_at_utc"] == "2026-07-21T14:30:12+00:00"
    assert rotated["envelope"]["last_attempt_at_utc"] is None


def test_candidate_retry_stops_after_later_success_removes_identity_from_pending(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import (
        prepare_daily_decision_brief_delivery,
        read_retryable_daily_decision_brief_delivery,
        record_daily_decision_brief_candidates,
    )

    first = _persist(tmp_path, run_id="run-1", actions=[_action()])
    record_daily_decision_brief_candidates(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
        revision=first["current_revision"],
        brief_digest=first["current_brief_digest"],
        candidate_identities=[IDENTITY_NVDA],
    )
    prepare_daily_decision_brief_delivery(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
        run_id="run-1",
        delivery_kind="candidate_alert",
        source_kind="successful_brief",
        revision=first["current_revision"],
        source_digest=first["current_brief_digest"],
        candidate_identities=[IDENTITY_NVDA],
        rendered_message="# 新增候选\nNVDA",
        render_context={"projection": "candidate_alert"},
    )

    second = _persist(tmp_path, run_id="run-2", actions=[])
    record_daily_decision_brief_candidates(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
        revision=second["current_revision"],
        brief_digest=second["current_brief_digest"],
        candidate_identities=[],
    )

    retry = read_retryable_daily_decision_brief_delivery(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
    )
    assert retry["reason"] == "stale_candidate_envelope"
    assert retry["envelope"] is None


def test_fixed_failure_validates_source_once_then_retries_from_durable_envelope(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import (
        DailyDecisionBriefStateError,
        prepare_daily_decision_brief_delivery,
        read_retryable_daily_decision_brief_delivery,
    )

    artifact = tmp_path / "output_runs" / "run-fail" / "accounts" / "lx" / "state" / "pipeline_failure.US.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"reason":"pipeline_failed"}\n', encoding="utf-8")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    source_reference = artifact.relative_to(tmp_path).as_posix()

    with pytest.raises(DailyDecisionBriefStateError, match="source digest mismatch"):
        prepare_daily_decision_brief_delivery(
            base=tmp_path,
            account="lx",
            market="US",
            market_trading_date=MARKET_DATE,
            run_id="run-fail",
            delivery_kind="fixed_failure",
            source_kind="scan_failure",
            source_digest="0" * 64,
            source_reference=source_reference,
            scheduled_target_market=TARGET_1000,
            rendered_message="# 本轮扫描失败",
            render_context={"projection": "fixed_failure"},
        )

    prepared = prepare_daily_decision_brief_delivery(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
        run_id="run-fail",
        delivery_kind="fixed_failure",
        source_kind="scan_failure",
        source_digest=digest,
        source_reference=source_reference,
        scheduled_target_market=TARGET_1000,
        rendered_message="# 本轮扫描失败",
        render_context={"projection": "fixed_failure"},
    )
    artifact.unlink()

    retry = read_retryable_daily_decision_brief_delivery(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
    )
    assert retry["envelope"] == prepared["envelope"]
    assert retry["envelope"]["source_reference"] == source_reference


def test_pending_fixed_failure_can_upgrade_but_confirmed_failure_cannot(
    tmp_path: Path,
) -> None:
    from src.application.daily_decision_brief_repository import (
        confirm_daily_decision_brief_delivery_v2,
        prepare_daily_decision_brief_delivery,
    )
    from src.application.notification_delivery_adapter import (
        build_notification_transport_key,
    )

    persisted = _persist(tmp_path, actions=[_action()])
    artifact = (
        tmp_path
        / "output_runs"
        / "run-fail"
        / "accounts"
        / "lx"
        / "state"
        / "pipeline_failure.US.json"
    )
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"reason":"pipeline_failed"}\n', encoding="utf-8")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    failure = prepare_daily_decision_brief_delivery(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
        run_id="run-fail",
        delivery_kind="fixed_failure",
        source_kind="scan_failure",
        source_digest=digest,
        source_reference=artifact.relative_to(tmp_path).as_posix(),
        scheduled_target_market=TARGET_1000,
        rendered_message="# 本轮扫描失败",
        render_context={"projection": "fixed_failure"},
    )

    upgraded = _prepare_fixed(tmp_path, persisted)
    assert upgraded["prepared"] is True
    assert upgraded["envelope"]["delivery_kind"] == "fixed_report"
    assert upgraded["envelope"]["status"] == "pending"

    other_root = tmp_path / "confirmed"
    confirmed_persisted = _persist(
        other_root,
        actions=[_action()],
    )
    confirmed_artifact = (
        other_root
        / "output_runs"
        / "run-fail"
        / "accounts"
        / "lx"
        / "state"
        / "pipeline_failure.US.json"
    )
    confirmed_artifact.parent.mkdir(parents=True)
    confirmed_artifact.write_text(
        '{"reason":"pipeline_failed"}\n',
        encoding="utf-8",
    )
    confirmed_digest = hashlib.sha256(
        confirmed_artifact.read_bytes()
    ).hexdigest()
    confirmed_failure = prepare_daily_decision_brief_delivery(
        base=other_root,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
        run_id="run-fail",
        delivery_kind="fixed_failure",
        source_kind="scan_failure",
        source_digest=confirmed_digest,
        source_reference=confirmed_artifact.relative_to(
            other_root
        ).as_posix(),
        scheduled_target_market=TARGET_1000,
        rendered_message="# 本轮扫描失败",
        render_context={"projection": "fixed_failure"},
    )["envelope"]
    confirm_daily_decision_brief_delivery_v2(
        base=other_root,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
        delivery_key=confirmed_failure["delivery_key"],
        source_digest=confirmed_failure["source_digest"],
        message_sha256=confirmed_failure["message_sha256"],
        transport_idempotency_key=build_notification_transport_key(
            confirmed_failure["delivery_key"]
        ),
    )

    retained = _prepare_fixed(other_root, confirmed_persisted)
    assert retained["prepared"] is False
    assert retained["envelope"]["delivery_kind"] == "fixed_failure"
    assert retained["envelope"]["status"] == "confirmed"


def test_ambiguous_fixed_failure_cannot_upgrade_to_normal_report(
    tmp_path: Path,
) -> None:
    from src.application.daily_decision_brief_repository import (
        DailyDecisionBriefStateError,
        prepare_daily_decision_brief_delivery,
        record_daily_decision_brief_delivery_attempt,
    )
    from src.application.notification_delivery_adapter import (
        build_notification_transport_key,
    )

    persisted = _persist(tmp_path, actions=[_action()])
    artifact = (
        tmp_path
        / "output_runs"
        / "run-fail"
        / "accounts"
        / "lx"
        / "state"
        / "pipeline_failure.US.json"
    )
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"reason":"pipeline_failed"}\n', encoding="utf-8")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    failure = prepare_daily_decision_brief_delivery(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
        run_id="run-fail",
        delivery_kind="fixed_failure",
        source_kind="scan_failure",
        source_digest=digest,
        source_reference=artifact.relative_to(tmp_path).as_posix(),
        scheduled_target_market=TARGET_1000,
        rendered_message="# 本轮扫描失败",
        render_context={"projection": "fixed_failure"},
    )["envelope"]
    record_daily_decision_brief_delivery_attempt(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
        delivery_key=failure["delivery_key"],
        source_digest=failure["source_digest"],
        message_sha256=failure["message_sha256"],
        transport_idempotency_key=build_notification_transport_key(
            failure["delivery_key"]
        ),
        ambiguous=True,
    )

    with pytest.raises(DailyDecisionBriefStateError, match="frozen"):
        _prepare_fixed(tmp_path, persisted)


def test_delivery_identity_validator_returns_persisted_kind_and_status(
    tmp_path: Path,
) -> None:
    from src.application.daily_decision_brief_repository import (
        prepare_daily_decision_brief_delivery,
        validate_daily_decision_brief_delivery_identity,
    )
    from src.application.notification_delivery_adapter import (
        build_notification_transport_key,
    )

    artifact = (
        tmp_path
        / "output_runs"
        / "run-fail"
        / "accounts"
        / "lx"
        / "state"
        / "pipeline_failure.US.json"
    )
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"reason":"pipeline_failed"}\n', encoding="utf-8")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    prepared = prepare_daily_decision_brief_delivery(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
        run_id="run-fail",
        delivery_kind="fixed_failure",
        source_kind="scan_failure",
        source_digest=digest,
        source_reference=artifact.relative_to(tmp_path).as_posix(),
        scheduled_target_market=TARGET_1000,
        rendered_message="# 本轮扫描失败",
        render_context={"projection": "fixed_failure"},
    )
    envelope = prepared["envelope"]

    validated = validate_daily_decision_brief_delivery_identity(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
        delivery_key=envelope["delivery_key"],
        source_digest=envelope["source_digest"],
        message_sha256=envelope["message_sha256"],
        transport_idempotency_key=build_notification_transport_key(
            envelope["delivery_key"]
        ),
    )

    assert validated["delivery_kind"] == "fixed_failure"
    assert validated["status"] == "pending"
    assert validated["envelope"] == envelope


def test_ambiguous_envelope_is_frozen_and_tampered_hash_fails_closed(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import (
        DailyDecisionBriefStateError,
        prepare_daily_decision_brief_delivery,
        read_daily_decision_brief_delivery_state,
    )

    persisted = _persist(tmp_path, actions=[_action()])
    prepared = _prepare_fixed(tmp_path, persisted)
    delivery_path = prepared["paths"]["delivery"]
    raw = json.loads(delivery_path.read_text(encoding="utf-8"))
    raw["days"][MARKET_DATE]["fixed_reports"][TARGET_1000]["status"] = "ambiguous"
    delivery_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(DailyDecisionBriefStateError, match="frozen"):
        _prepare_fixed(tmp_path, persisted, message="# changed")

    raw = json.loads(delivery_path.read_text(encoding="utf-8"))
    raw["days"][MARKET_DATE]["fixed_reports"][TARGET_1000]["rendered_message"] = "tampered"
    delivery_path.write_text(json.dumps(raw), encoding="utf-8")
    read = read_daily_decision_brief_delivery_state(base=tmp_path, account="lx", market="US")
    assert read["available"] is False
    assert read["reason"] == "state_invalid"
    assert "message digest mismatch" in read["error"]


def test_v1_full_migration_is_dry_run_by_default_and_recomputes_digest(tmp_path: Path) -> None:
    from domain.domain.daily_decision_brief import daily_brief_digest
    from src.application.daily_decision_brief_repository import (
        confirm_daily_decision_brief_delivery,
        migrate_daily_decision_brief_delivery,
        prepare_daily_decision_brief,
    )

    lifecycle = prepare_daily_decision_brief(
        base=tmp_path,
        brief=_brief(run_id="legacy-run", actions=[_action()]),
    )
    brief = lifecycle["brief"]
    confirm_daily_decision_brief_delivery(
        base=tmp_path,
        market="US",
        market_trading_date=MARKET_DATE,
        account="lx",
        revision=brief["revision"],
        delivery_kind=lifecycle["delivery_kind"],
        delivery_key=lifecycle["delivery_key"],
        brief_digest=lifecycle["current_brief_digest"],
        confirmed_at_utc=datetime(2026, 7, 21, 14, 1, tzinfo=timezone.utc),
    )
    pointer_path = lifecycle["paths"]["delivery"]
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer["brief_digest"] = "legacy-wrong-digest"
    pointer["delivery_key"] = f"daily-brief:US:{MARKET_DATE}:lx:full:{'1' * 64}"
    pointer_path.write_text(json.dumps(pointer), encoding="utf-8")
    current_before = lifecycle["paths"]["current"].read_bytes()
    revision_before = lifecycle["paths"]["revision"].read_bytes()
    pointer_before = pointer_path.read_bytes()

    dry_run = migrate_daily_decision_brief_delivery(
        base=tmp_path,
        account="lx",
        market="US",
    )
    assert dry_run["dry_run"] is True
    assert dry_run["write_applied"] is False
    assert dry_run["migration"]["legacy_brief_digest_matches_revision"] is False
    assert pointer_path.read_bytes() == pointer_before
    assert list(pointer_path.parent.glob(f"{pointer_path.name}.v1.backup.*")) == []

    applied = migrate_daily_decision_brief_delivery(
        base=tmp_path,
        account="lx",
        market="US",
        confirm=True,
    )
    assert applied["write_applied"] is True
    assert applied["backup_path"].read_bytes() == pointer_before
    assert applied["state"]["legacy_last_confirmation"]["brief_digest"] == daily_brief_digest(brief)
    assert set(applied["state"]["days"][MARKET_DATE]["alerted_candidates"]) == {IDENTITY_NVDA}
    assert lifecycle["paths"]["current"].read_bytes() == current_before
    assert lifecycle["paths"]["revision"].read_bytes() == revision_before


def test_v1_overlay_digest_remains_valid_for_next_prepare_and_migration(
    tmp_path: Path,
) -> None:
    from domain.domain.daily_decision_brief import daily_brief_compatible_digests
    from src.application.daily_decision_brief_repository import (
        confirm_daily_decision_brief_delivery,
        migrate_daily_decision_brief_delivery,
        prepare_daily_decision_brief,
    )

    lifecycle = prepare_daily_decision_brief(
        base=tmp_path,
        brief=_brief(run_id="legacy-overlay", actions=[_action()]),
    )
    confirm_daily_decision_brief_delivery(
        base=tmp_path,
        market="US",
        market_trading_date=MARKET_DATE,
        account="lx",
        revision=lifecycle["brief"]["revision"],
        delivery_kind=lifecycle["delivery_kind"],
        delivery_key=lifecycle["delivery_key"],
        brief_digest=lifecycle["current_brief_digest"],
        confirmed_at_utc="2026-07-21T14:01:00+00:00",
    )
    for path in (lifecycle["paths"]["revision"], lifecycle["paths"]["current"]):
        historical = json.loads(path.read_text(encoding="utf-8"))
        historical["ai_decision_advice"] = {
            "status": "completed",
            "summary": "historical-overlay",
        }
        historical["ai_decision_advice_evidence_index"] = {
            "source": "historical-evidence",
        }
        path.write_text(json.dumps(historical), encoding="utf-8")
    legacy_digest = daily_brief_compatible_digests(historical)[-1]
    assert legacy_digest != lifecycle["current_brief_digest"]

    pointer_path = lifecycle["paths"]["delivery"]
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer["brief_digest"] = legacy_digest
    pointer_path.write_text(json.dumps(pointer), encoding="utf-8")

    preview = migrate_daily_decision_brief_delivery(
        base=tmp_path,
        account="lx",
        market="US",
    )
    assert preview["migration"]["legacy_brief_digest_matches_revision"] is True

    next_run = prepare_daily_decision_brief(
        base=tmp_path,
        brief=_brief(run_id="after-overlay", actions=[_action(symbol="AMD")]),
    )
    assert next_run["current_revision"] == 1
    assert next_run["last_delivered_brief_digest"] == legacy_digest
    assert next_run["delivery_key"].startswith(
        f"daily-brief:US:{MARKET_DATE}:lx:from:{legacy_digest}:"
    )
    confirmed = confirm_daily_decision_brief_delivery(
        base=tmp_path,
        market="US",
        market_trading_date=MARKET_DATE,
        account="lx",
        revision=next_run["brief"]["revision"],
        delivery_kind=next_run["delivery_kind"],
        delivery_key=next_run["delivery_key"],
        brief_digest=next_run["current_brief_digest"],
        confirmed_at_utc="2026-07-21T14:02:00+00:00",
    )
    assert confirmed["advanced"] is True


def test_v1_overlay_revision_can_be_confirmed_with_its_exact_historical_digest(
    tmp_path: Path,
) -> None:
    from domain.domain.daily_decision_brief import daily_brief_compatible_digests
    from src.application.daily_decision_brief_repository import (
        confirm_daily_decision_brief_delivery,
        prepare_daily_decision_brief,
    )

    lifecycle = prepare_daily_decision_brief(
        base=tmp_path,
        brief=_brief(run_id="legacy-unconfirmed", actions=[_action()]),
    )
    revision_path = lifecycle["paths"]["revision"]
    historical = json.loads(revision_path.read_text(encoding="utf-8"))
    historical["ai_decision_advice"] = {"status": "completed"}
    historical["ai_decision_advice_evidence_index"] = {"source": "legacy"}
    revision_path.write_text(json.dumps(historical), encoding="utf-8")
    legacy_digest = daily_brief_compatible_digests(historical)[-1]

    confirmed = confirm_daily_decision_brief_delivery(
        base=tmp_path,
        market="US",
        market_trading_date=MARKET_DATE,
        account="lx",
        revision=lifecycle["brief"]["revision"],
        delivery_kind=lifecycle["delivery_kind"],
        delivery_key=lifecycle["delivery_key"],
        brief_digest=legacy_digest,
        confirmed_at_utc="2026-07-21T14:01:00+00:00",
    )
    assert confirmed["pointer"]["brief_digest"] == legacy_digest


def test_v1_delta_without_persisted_diff_migrates_no_alerted_candidates(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import (
        confirm_daily_decision_brief_delivery,
        migrate_daily_decision_brief_delivery,
        prepare_daily_decision_brief,
    )

    first = prepare_daily_decision_brief(base=tmp_path, brief=_brief(run_id="legacy-0"))
    confirm_daily_decision_brief_delivery(
        base=tmp_path,
        market="US",
        market_trading_date=MARKET_DATE,
        account="lx",
        revision=first["brief"]["revision"],
        delivery_kind=first["delivery_kind"],
        delivery_key=first["delivery_key"],
        brief_digest=first["current_brief_digest"],
    )
    second = prepare_daily_decision_brief(
        base=tmp_path,
        brief=_brief(run_id="legacy-1", actions=[_action()]),
    )
    assert second["delivery_kind"] == "delta"
    confirm_daily_decision_brief_delivery(
        base=tmp_path,
        market="US",
        market_trading_date=MARKET_DATE,
        account="lx",
        revision=second["brief"]["revision"],
        delivery_kind=second["delivery_kind"],
        delivery_key=second["delivery_key"],
        brief_digest=second["current_brief_digest"],
    )
    second["paths"]["run_diff"].unlink()

    migrated = migrate_daily_decision_brief_delivery(
        base=tmp_path,
        account="lx",
        market="US",
        confirm=True,
    )
    assert migrated["state"]["days"][MARKET_DATE]["alerted_candidates"] == {}
    assert migrated["state"]["legacy_last_confirmation"]["delivery_kind"] == "delta"


def test_migration_missing_revision_fails_closed_without_backup_or_overwrite(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import (
        DailyDecisionBriefStateError,
        confirm_daily_decision_brief_delivery,
        migrate_daily_decision_brief_delivery,
        prepare_daily_decision_brief,
    )

    lifecycle = prepare_daily_decision_brief(base=tmp_path, brief=_brief(run_id="legacy-run"))
    confirm_daily_decision_brief_delivery(
        base=tmp_path,
        market="US",
        market_trading_date=MARKET_DATE,
        account="lx",
        revision=lifecycle["brief"]["revision"],
        delivery_kind=lifecycle["delivery_kind"],
        delivery_key=lifecycle["delivery_key"],
        brief_digest=lifecycle["current_brief_digest"],
    )
    pointer_path = lifecycle["paths"]["delivery"]
    pointer_before = pointer_path.read_bytes()
    lifecycle["paths"]["revision"].unlink()

    with pytest.raises(DailyDecisionBriefStateError, match="missing revision"):
        migrate_daily_decision_brief_delivery(
            base=tmp_path,
            account="lx",
            market="US",
            confirm=True,
        )
    assert pointer_path.read_bytes() == pointer_before
    assert list(pointer_path.parent.glob(f"{pointer_path.name}.v1.backup.*")) == []


def test_account_and_market_delivery_states_are_isolated(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import read_daily_decision_brief_delivery_state

    us_lx = _persist(tmp_path, run_id="us-lx", actions=[_action()])
    _prepare_fixed(tmp_path, us_lx)
    assert read_daily_decision_brief_delivery_state(base=tmp_path, account="lx", market="US")["available"] is True
    assert read_daily_decision_brief_delivery_state(base=tmp_path, account="sy", market="US")["available"] is False
    assert read_daily_decision_brief_delivery_state(base=tmp_path, account="lx", market="HK")["available"] is False


def test_malformed_mixed_delivery_schema_fails_closed(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import read_daily_decision_brief_delivery_state

    state_dir = tmp_path / "output_accounts" / "lx" / "state"
    state_dir.mkdir(parents=True)
    path = state_dir / "daily_decision_brief.US.delivery.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "daily_decision_brief_delivery.v1",
                "account": "lx",
                "market": "US",
                "days": {},
            }
        ),
        encoding="utf-8",
    )

    out = read_daily_decision_brief_delivery_state(base=tmp_path, account="lx", market="US")
    assert out["available"] is False
    assert out["reason"] == "state_invalid"


def test_v2_attempt_and_confirmation_advance_exact_envelope_and_candidates(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import (
        confirm_daily_decision_brief_delivery_v2,
        read_daily_decision_brief_delivery_state,
        record_daily_decision_brief_candidates,
        record_daily_decision_brief_delivery_attempt,
    )
    from src.application.notification_delivery_adapter import build_notification_transport_key

    persisted = _persist(tmp_path, actions=[_action(), _action(symbol="AMD")])
    record_daily_decision_brief_candidates(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
        revision=persisted["current_revision"],
        brief_digest=persisted["current_brief_digest"],
        candidate_identities=persisted["current_candidate_identities"],
    )
    prepared = _prepare_fixed(tmp_path, persisted)
    envelope = prepared["envelope"]
    transport_key = build_notification_transport_key(envelope["delivery_key"])

    attempt = record_daily_decision_brief_delivery_attempt(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
        delivery_key=envelope["delivery_key"],
        source_digest=envelope["source_digest"],
        message_sha256=envelope["message_sha256"],
        transport_idempotency_key=transport_key,
        ambiguous=False,
        attempted_at_utc="2026-07-21T14:00:03+00:00",
    )
    assert attempt["envelope"]["status"] == "pending"
    assert attempt["envelope"]["last_attempt_at_utc"] == "2026-07-21T14:00:03+00:00"

    confirmed = confirm_daily_decision_brief_delivery_v2(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
        delivery_key=envelope["delivery_key"],
        source_digest=envelope["source_digest"],
        message_sha256=envelope["message_sha256"],
        transport_idempotency_key=transport_key,
        confirmed_at_utc="2026-07-21T14:00:04+00:00",
    )
    assert confirmed["advanced"] is True
    day = read_daily_decision_brief_delivery_state(
        base=tmp_path,
        account="lx",
        market="US",
    )["state"]["days"][MARKET_DATE]
    assert day["fixed_reports"][TARGET_1000]["status"] == "confirmed"
    assert day["fixed_reports"][TARGET_1000]["last_attempt_at_utc"] == "2026-07-21T14:00:04+00:00"
    assert day["pending_candidates"] == {}
    assert set(day["alerted_candidates"]) == {IDENTITY_AMD, IDENTITY_NVDA}
    assert {item["via"] for item in day["alerted_candidates"].values()} == {"fixed_report"}


def test_v2_delivery_accepts_exact_digest_from_retired_ai_overlay_revision(
    tmp_path: Path,
) -> None:
    from domain.domain.daily_decision_brief import daily_brief_compatible_digests
    from src.application.daily_decision_brief_repository import (
        confirm_daily_decision_brief_delivery_v2,
        inspect_daily_decision_brief_delivery,
        persist_daily_decision_brief_success,
        read_daily_decision_brief_delivery_state,
    )
    from src.application.notification_delivery_adapter import build_notification_transport_key

    persisted = _persist(tmp_path, actions=[_action()])
    prepared = _prepare_fixed(tmp_path, persisted)
    envelope = prepared["envelope"]
    confirm_daily_decision_brief_delivery_v2(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
        delivery_key=envelope["delivery_key"],
        source_digest=envelope["source_digest"],
        message_sha256=envelope["message_sha256"],
        transport_idempotency_key=build_notification_transport_key(envelope["delivery_key"]),
        confirmed_at_utc="2026-07-21T14:00:04+00:00",
    )

    for path in (persisted["paths"]["revision"], persisted["paths"]["current"]):
        historical = json.loads(path.read_text(encoding="utf-8"))
        historical["ai_decision_advice"] = {
            "status": "completed",
            "summary": "historical-overlay",
        }
        historical["ai_decision_advice_evidence_index"] = {
            "source": "historical-evidence",
        }
        path.write_text(json.dumps(historical), encoding="utf-8")
    legacy_digest = daily_brief_compatible_digests(historical)[-1]
    assert legacy_digest != persisted["current_brief_digest"]

    delivery_path = prepared["paths"]["delivery"]
    delivery = json.loads(delivery_path.read_text(encoding="utf-8"))
    day = delivery["days"][MARKET_DATE]
    day["fixed_reports"][TARGET_1000]["source_digest"] = legacy_digest
    for alerted in day["alerted_candidates"].values():
        alerted["brief_digest"] = legacy_digest
    delivery_path.write_text(json.dumps(delivery), encoding="utf-8")

    inspected = inspect_daily_decision_brief_delivery(
        base=tmp_path,
        account="lx",
        market="US",
    )
    assert inspected["reason"] == "already_v2"
    assert inspected["state"]["days"][MARKET_DATE]["fixed_reports"][TARGET_1000][
        "source_digest"
    ] == legacy_digest

    next_run = persist_daily_decision_brief_success(
        base=tmp_path,
        brief=_brief(run_id="run-2", actions=[_action()]),
    )
    assert next_run["current_revision"] == 1
    assert "ai_decision_advice" not in next_run["previous_successful_brief"]
    assert "ai_decision_advice_evidence_index" not in next_run[
        "previous_successful_brief"
    ]
    assert read_daily_decision_brief_delivery_state(
        base=tmp_path,
        account="lx",
        market="US",
    )["available"] is True


def test_retry_payload_classifier_blocks_retired_source_text_and_card(
    tmp_path: Path,
) -> None:
    from domain.domain.daily_decision_brief import daily_brief_compatible_digests
    from src.application.channels.feishu_notification_renderer import (
        feishu_notification_envelope_sha256,
        render_feishu_notification_card,
    )
    from src.application.daily_decision_brief_repository import (
        classify_retryable_daily_decision_brief_payload,
    )

    persisted = _persist(tmp_path, actions=[_action()])
    prepared = _prepare_fixed(tmp_path, persisted)
    clean = prepared["envelope"]
    common = {
        "base": tmp_path,
        "account": "lx",
        "market": "US",
        "market_trading_date": MARKET_DATE,
    }
    assert classify_retryable_daily_decision_brief_payload(
        **common,
        envelope=clean,
    ) == "clean"

    message_only = {
        **clean,
        "rendered_message": "# AI建议\n旧建议不得重发",
    }
    message_only["message_sha256"] = hashlib.sha256(
        message_only["rendered_message"].encode()
    ).hexdigest()
    assert classify_retryable_daily_decision_brief_payload(
        **common,
        envelope=message_only,
    ) == "legacy_ai_payload_retired"

    card = render_feishu_notification_card(
        markdown="# AI Decision Advice\nretired",
        fallback_text=clean["rendered_message"],
    )
    card_only = {
        **clean,
        "rendered_transport": card,
        "rendered_transport_sha256": feishu_notification_envelope_sha256(card),
    }
    assert classify_retryable_daily_decision_brief_payload(
        **common,
        envelope=card_only,
    ) == "legacy_ai_payload_retired"

    revision_path = persisted["paths"]["revision"]
    historical = json.loads(revision_path.read_text(encoding="utf-8"))
    historical["ai_decision_advice"] = {"status": "completed"}
    historical["ai_decision_advice_evidence_index"] = {"symbols": []}
    revision_path.write_text(json.dumps(historical), encoding="utf-8")
    source_only = {
        **clean,
        "source_digest": daily_brief_compatible_digests(historical)[-1],
    }
    assert classify_retryable_daily_decision_brief_payload(
        **common,
        envelope=source_only,
    ) == "legacy_ai_payload_retired"


def test_retry_payload_classifier_inspects_the_same_raw_revision_it_validates(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.daily_decision_brief_repository as repository

    persisted = _persist(tmp_path, actions=[_action()])
    prepared = _prepare_fixed(tmp_path, persisted)
    revision_path = persisted["paths"]["revision"].resolve()
    revision_reads = 0
    original_read = repository._read_json_strict

    def counted_read(path: Path):
        nonlocal revision_reads
        if Path(path).resolve() == revision_path:
            revision_reads += 1
        return original_read(path)

    monkeypatch.setattr(repository, "_read_json_strict", counted_read)

    assert repository.classify_retryable_daily_decision_brief_payload(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
        envelope=prepared["envelope"],
    ) == "clean"
    assert revision_reads == 1


def test_v2_ambiguous_attempt_freezes_envelope_and_exact_retry_can_confirm(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import (
        DailyDecisionBriefStateError,
        confirm_daily_decision_brief_delivery_v2,
        prepare_daily_decision_brief_delivery,
        record_daily_decision_brief_candidates,
        record_daily_decision_brief_delivery_attempt,
    )
    from src.application.notification_delivery_adapter import build_notification_transport_key

    persisted = _persist(tmp_path, actions=[_action()])
    record_daily_decision_brief_candidates(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
        revision=persisted["current_revision"],
        brief_digest=persisted["current_brief_digest"],
        candidate_identities=persisted["current_candidate_identities"],
    )
    prepared = _prepare_fixed(tmp_path, persisted)
    envelope = prepared["envelope"]
    transport_key = build_notification_transport_key(envelope["delivery_key"])
    ambiguous = record_daily_decision_brief_delivery_attempt(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
        delivery_key=envelope["delivery_key"],
        source_digest=envelope["source_digest"],
        message_sha256=envelope["message_sha256"],
        transport_idempotency_key=transport_key,
        ambiguous=True,
    )
    assert ambiguous["envelope"]["status"] == "ambiguous"

    with pytest.raises(DailyDecisionBriefStateError, match="frozen"):
        prepare_daily_decision_brief_delivery(
            base=tmp_path,
            account="lx",
            market="US",
            market_trading_date=MARKET_DATE,
            run_id="run-2",
            delivery_kind="fixed_report",
            source_kind="successful_brief",
            revision=persisted["current_revision"],
            source_digest=persisted["current_brief_digest"],
            scheduled_target_market=TARGET_1000,
            candidate_identities=persisted["current_candidate_identities"],
            rendered_message="# changed",
        )

    confirmed = confirm_daily_decision_brief_delivery_v2(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
        delivery_key=envelope["delivery_key"],
        source_digest=envelope["source_digest"],
        message_sha256=envelope["message_sha256"],
        transport_idempotency_key=transport_key,
    )
    assert confirmed["envelope"]["status"] == "confirmed"


def test_v2_exact_transition_rejects_mismatched_transport_or_payload(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import (
        DailyDecisionBriefStateError,
        record_daily_decision_brief_delivery_attempt,
    )

    persisted = _persist(tmp_path)
    envelope = _prepare_fixed(tmp_path, persisted)["envelope"]
    common = {
        "base": tmp_path,
        "account": "lx",
        "market": "US",
        "market_trading_date": MARKET_DATE,
        "delivery_key": envelope["delivery_key"],
        "source_digest": envelope["source_digest"],
        "message_sha256": envelope["message_sha256"],
        "transport_idempotency_key": "wrong",
        "ambiguous": False,
    }
    with pytest.raises(DailyDecisionBriefStateError, match="idempotency"):
        record_daily_decision_brief_delivery_attempt(**common)
    with pytest.raises(ValueError, match="message_sha256"):
        record_daily_decision_brief_delivery_attempt(
            **{**common, "transport_idempotency_key": "om-" + "0" * 32, "message_sha256": "bad"}
        )


def test_operator_resolution_reconciles_exact_daily_brief_envelope(
    tmp_path: Path,
) -> None:
    from src.application.daily_decision_brief_repository import (
        read_daily_decision_brief_delivery_state,
        read_retryable_daily_decision_brief_delivery,
        reconcile_daily_decision_brief_delivery_resolution,
        record_daily_decision_brief_candidates,
        record_daily_decision_brief_delivery_attempt,
    )
    from src.application.notification_delivery_adapter import (
        build_notification_transport_key,
    )

    persisted = _persist(tmp_path, actions=[_action()])
    record_daily_decision_brief_candidates(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
        revision=persisted["current_revision"],
        brief_digest=persisted["current_brief_digest"],
        candidate_identities=persisted["current_candidate_identities"],
    )
    envelope = _prepare_fixed(tmp_path, persisted)["envelope"]
    transport_key = build_notification_transport_key(
        envelope["delivery_key"]
    )
    identity = {
        "base": tmp_path,
        "account": "lx",
        "market": "US",
        "market_trading_date": MARKET_DATE,
        "delivery_key": envelope["delivery_key"],
        "source_digest": envelope["source_digest"],
        "message_sha256": envelope["message_sha256"],
        "transport_idempotency_key": transport_key,
    }
    record_daily_decision_brief_delivery_attempt(
        **identity,
        ambiguous=True,
    )

    failed = reconcile_daily_decision_brief_delivery_resolution(
        **identity,
        resolution="failed",
        resolved_at_utc="2026-07-21T14:10:00+00:00",
    )
    assert failed["envelope"]["status"] == "pending"
    retry = read_retryable_daily_decision_brief_delivery(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
    )
    assert retry["envelope"]["delivery_key"] == envelope["delivery_key"]

    record_daily_decision_brief_delivery_attempt(
        **identity,
        ambiguous=True,
    )
    delivered = reconcile_daily_decision_brief_delivery_resolution(
        **identity,
        resolution="delivered",
        resolved_at_utc="2026-07-21T14:20:00+00:00",
    )
    assert delivered["envelope"]["status"] == "confirmed"
    state = read_daily_decision_brief_delivery_state(
        base=tmp_path,
        account="lx",
        market="US",
    )["state"]
    day = state["days"][MARKET_DATE]
    assert day["pending_candidates"] == {}
    assert IDENTITY_NVDA in day["alerted_candidates"]


def test_confirmed_candidate_delivery_rolls_to_later_candidate_batch(tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import (
        confirm_daily_decision_brief_delivery_v2,
        prepare_daily_decision_brief_delivery,
        read_daily_decision_brief_delivery_state,
        read_retryable_daily_decision_brief_delivery,
        record_daily_decision_brief_candidates,
    )
    from src.application.notification_delivery_adapter import build_notification_transport_key

    first = _persist(tmp_path, run_id="run-1", actions=[_action()])
    record_daily_decision_brief_candidates(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
        revision=first["current_revision"],
        brief_digest=first["current_brief_digest"],
        candidate_identities=first["current_candidate_identities"],
    )
    first_envelope = prepare_daily_decision_brief_delivery(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
        run_id="run-1",
        delivery_kind="candidate_alert",
        source_kind="successful_brief",
        revision=first["current_revision"],
        source_digest=first["current_brief_digest"],
        candidate_identities=[IDENTITY_NVDA],
        rendered_message="# 新增候选\nNVDA",
        render_context={"projection": "candidate_alert"},
    )["envelope"]
    confirm_daily_decision_brief_delivery_v2(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
        delivery_key=first_envelope["delivery_key"],
        source_digest=first_envelope["source_digest"],
        message_sha256=first_envelope["message_sha256"],
        transport_idempotency_key=build_notification_transport_key(first_envelope["delivery_key"]),
    )

    second = _persist(
        tmp_path,
        run_id="run-2",
        actions=[_action(), _action(symbol="AMD")],
    )
    recorded = record_daily_decision_brief_candidates(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
        revision=second["current_revision"],
        brief_digest=second["current_brief_digest"],
        candidate_identities=second["current_candidate_identities"],
    )
    assert recorded["pending_candidate_identities"] == [IDENTITY_AMD]

    prepared = prepare_daily_decision_brief_delivery(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
        run_id="run-2",
        delivery_kind="candidate_alert",
        source_kind="successful_brief",
        revision=second["current_revision"],
        source_digest=second["current_brief_digest"],
        candidate_identities=[IDENTITY_AMD],
        rendered_message="# 新增候选\nAMD",
        render_context={"projection": "candidate_alert"},
    )
    second_envelope = prepared["envelope"]
    assert prepared["prepared"] is True
    assert second_envelope["delivery_key"] != first_envelope["delivery_key"]
    retry = read_retryable_daily_decision_brief_delivery(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
    )
    assert retry["envelope"]["delivery_key"] == second_envelope["delivery_key"]
    assert retry["envelope"]["candidate_identities"] == [IDENTITY_AMD]

    confirm_daily_decision_brief_delivery_v2(
        base=tmp_path,
        account="lx",
        market="US",
        market_trading_date=MARKET_DATE,
        delivery_key=second_envelope["delivery_key"],
        source_digest=second_envelope["source_digest"],
        message_sha256=second_envelope["message_sha256"],
        transport_idempotency_key=build_notification_transport_key(second_envelope["delivery_key"]),
    )
    day = read_daily_decision_brief_delivery_state(
        base=tmp_path,
        account="lx",
        market="US",
    )["state"]["days"][MARKET_DATE]
    assert set(day["alerted_candidates"]) == {IDENTITY_NVDA, IDENTITY_AMD}
    assert day["pending_candidates"] == {}
