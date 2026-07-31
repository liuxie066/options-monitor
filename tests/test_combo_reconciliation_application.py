from __future__ import annotations

from dataclasses import replace

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from src.application.ledger.combo_reconciliation import (
    adopt_post_trade_combo_pair,
    reconcile_combo_pair_inferences,
    reject_post_trade_combo_pair,
    supersede_post_trade_combo_pair,
)
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.writer import persist_trade_event_object


BASE_TIME_MS = 1_785_312_000_000
RUNTIME_ENVIRONMENT = "opend:127.0.0.1:11111"


def _event(
    event_id: str,
    record_id: str,
    *,
    option_type: str,
    position_side: str,
    strike: int,
    event_time_ms: int,
    opend_host: str = "127.0.0.1",
) -> TradeEvent:
    return TradeEvent(
        event_id=event_id,
        event_type="open",
        event_time_ms=event_time_ms,
        contract_key=ContractKey.from_values(
            broker="futu",
            account="lx",
            underlying_symbol="NVDA",
            option_type=option_type,
            position_side=position_side,
            strike=strike,
            expiration_ymd="2026-08-21",
        ),
        contracts=1,
        price=1,
        currency="USD",
        source="test",
        lot_id=record_id,
        raw_payload={
            "_trade_intake_source": {
                "schema_version": "trade_intake_source.v1",
                "transport": "push",
                "source_id": "lx",
                "account": "lx",
                "futu_account_id": "1001",
                "opend_process": "FutuOpenD",
                "opend_host": opend_host,
                "opend_port": 11111,
                "received_at_utc": "2026-07-31T13:00:00+00:00",
            }
        },
    )


def test_application_reconcile_is_post_trade_and_persists_only_inference_state(
    tmp_path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    persist_trade_event_object(
        repo,
        _event(
            "call-open",
            "call-lot",
            option_type="call",
            position_side="long",
            strike=110,
            event_time_ms=BASE_TIME_MS + 1_000,
        ),
    )

    waiting = reconcile_combo_pair_inferences(
        repo=repo,
        account="lx",
        runtime_environment=RUNTIME_ENVIRONMENT,
        persist=False,
        effective_now_ms=BASE_TIME_MS + 2_000,
    )
    assert waiting["inferences"] == []
    assert waiting["waiting_for_counterpart"][0]["record_id"] == "call-lot"
    assert repo.list_combo_pair_inferences(account="lx") == []

    persist_trade_event_object(
        repo,
        _event(
            "put-open",
            "put-lot",
            option_type="put",
            position_side="short",
            strike=100,
            event_time_ms=BASE_TIME_MS + 2_000,
        ),
    )
    persisted = reconcile_combo_pair_inferences(
        repo=repo,
        account="lx",
        runtime_environment=RUNTIME_ENVIRONMENT,
        persist=True,
        effective_now_ms=BASE_TIME_MS + 3_000,
    )

    assert persisted["proposal_ready_count"] == 1
    assert persisted["inserted_inference_count"] == 1
    stored = repo.list_combo_pair_inferences(account="lx")
    assert len(stored) == 1
    assert stored[0]["put_open_event_id"] == "put-open"
    assert stored[0]["call_open_event_id"] == "call-open"
    assert len(repo.list_trade_events()) == 2
    assert all(
        not item["fields"].get("strategy_group_id")
        for item in repo.list_position_lots()
    )


def test_confirm_reject_and_supersede_are_exact_atomic_decisions(tmp_path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    for event in (
        _event(
            "call-open",
            "call-lot",
            option_type="call",
            position_side="long",
            strike=110,
            event_time_ms=BASE_TIME_MS + 1_000,
        ),
        _event(
            "put-open",
            "put-lot",
            option_type="put",
            position_side="short",
            strike=100,
            event_time_ms=BASE_TIME_MS + 2_000,
        ),
    ):
        persist_trade_event_object(repo, event)
    reconciled = reconcile_combo_pair_inferences(
        repo=repo,
        account="lx",
        runtime_environment=RUNTIME_ENVIRONMENT,
        persist=True,
        effective_now_ms=BASE_TIME_MS + 3_000,
    )
    proposal = reconciled["inferences"][0]
    preview = adopt_post_trade_combo_pair(
        repo=repo,
        inference_id=proposal["inference_id"],
        expected_input_hash=proposal["input_snapshot_hash"],
        actor="tester",
        apply_changes=False,
        effective_now_ms=BASE_TIME_MS + 4_000,
    )
    assert preview["status"] == "dry_run"
    assert len(repo.list_trade_events()) == 2

    adopted = adopt_post_trade_combo_pair(
        repo=repo,
        inference_id=proposal["inference_id"],
        expected_input_hash=proposal["input_snapshot_hash"],
        actor="tester",
        apply_changes=True,
        effective_now_ms=BASE_TIME_MS + 4_000,
    )
    assert adopted["membership"]["status"] == "exact"
    assert repo.get_combo_pair_inference(proposal["inference_id"])["status"] == "user_confirmed"
    assert len(repo.list_trade_events()) == 4

    repeated = adopt_post_trade_combo_pair(
        repo=repo,
        inference_id=proposal["inference_id"],
        expected_input_hash=proposal["input_snapshot_hash"],
        actor="tester",
        apply_changes=True,
        effective_now_ms=BASE_TIME_MS + 5_000,
    )
    assert repeated["status"] == "already_confirmed"
    assert len(repo.list_trade_events()) == 4

    superseded = supersede_post_trade_combo_pair(
        repo=repo,
        inference_id=proposal["inference_id"],
        expected_input_hash=proposal["input_snapshot_hash"],
        reason="wrong pair",
        actor="tester",
        apply_changes=True,
        effective_now_ms=BASE_TIME_MS + 6_000,
    )
    assert superseded["membership"]["status"] != "exact"
    assert repo.get_combo_pair_inference(proposal["inference_id"])["status"] == "superseded"
    assert len(repo.list_trade_events()) == 6


def test_supersede_reactivates_alternative_that_expired_only_while_leg_was_claimed(
    tmp_path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    for event in (
        _event(
            "put-open",
            "put-lot",
            option_type="put",
            position_side="short",
            strike=100,
            event_time_ms=BASE_TIME_MS + 1_000,
        ),
        _event(
            "call-a-open",
            "call-a-lot",
            option_type="call",
            position_side="long",
            strike=110,
            event_time_ms=BASE_TIME_MS + 2_000,
        ),
        _event(
            "call-b-open",
            "call-b-lot",
            option_type="call",
            position_side="long",
            strike=120,
            event_time_ms=BASE_TIME_MS + 2_000,
        ),
    ):
        persist_trade_event_object(repo, event)

    initial = reconcile_combo_pair_inferences(
        repo=repo,
        account="lx",
        runtime_environment=RUNTIME_ENVIRONMENT,
        persist=True,
        effective_now_ms=BASE_TIME_MS + 3_000,
    )
    assert len(initial["inferences"]) == 2
    chosen = initial["inferences"][0]
    alternative_id = next(
        item["inference_id"]
        for item in initial["inferences"]
        if item["inference_id"] != chosen["inference_id"]
    )
    adopt_post_trade_combo_pair(
        repo=repo,
        inference_id=chosen["inference_id"],
        expected_input_hash=chosen["input_snapshot_hash"],
        actor="tester",
        apply_changes=True,
        effective_now_ms=BASE_TIME_MS + 4_000,
    )
    reconcile_combo_pair_inferences(
        repo=repo,
        account="lx",
        runtime_environment=RUNTIME_ENVIRONMENT,
        persist=True,
        effective_now_ms=BASE_TIME_MS + 5_000,
    )
    assert repo.get_combo_pair_inference(alternative_id)["decision_reason"] == (
        "facts_drifted_or_leg_claimed"
    )

    supersede_post_trade_combo_pair(
        repo=repo,
        inference_id=chosen["inference_id"],
        expected_input_hash=chosen["input_snapshot_hash"],
        reason="wrong pair",
        actor="tester",
        apply_changes=True,
        effective_now_ms=BASE_TIME_MS + 6_000,
    )
    reconciled = reconcile_combo_pair_inferences(
        repo=repo,
        account="lx",
        runtime_environment=RUNTIME_ENVIRONMENT,
        persist=True,
        effective_now_ms=BASE_TIME_MS + 7_000,
    )

    assert [item["inference_id"] for item in reconciled["inferences"]] == [
        alternative_id
    ]
    assert repo.get_combo_pair_inference(alternative_id)["status"] == (
        reconciled["inferences"][0]["status"]
    )
    assert repo.get_combo_pair_inference(chosen["inference_id"])["status"] == (
        "superseded"
    )


def test_reconcile_fails_closed_when_open_events_have_different_runtime_sources(
    tmp_path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    for event in (
        _event(
            "put-open",
            "put-lot",
            option_type="put",
            position_side="short",
            strike=100,
            event_time_ms=BASE_TIME_MS + 1_000,
            opend_host="127.0.0.1",
        ),
        _event(
            "call-open",
            "call-lot",
            option_type="call",
            position_side="long",
            strike=110,
            event_time_ms=BASE_TIME_MS + 2_000,
            opend_host="127.0.0.2",
        ),
    ):
        persist_trade_event_object(repo, event)

    result = reconcile_combo_pair_inferences(
        repo=repo,
        account="lx",
        runtime_environment=RUNTIME_ENVIRONMENT,
        persist=True,
        effective_now_ms=BASE_TIME_MS + 3_000,
    )

    assert result["inferences"] == []
    assert repo.list_combo_pair_inferences(account="lx") == []


def test_reconcile_fails_closed_when_open_event_runtime_source_is_missing(
    tmp_path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    persist_trade_event_object(
        repo,
        _event(
            "put-open",
            "put-lot",
            option_type="put",
            position_side="short",
            strike=100,
            event_time_ms=BASE_TIME_MS + 1_000,
        ),
    )
    persist_trade_event_object(
        repo,
        replace(
            _event(
                "call-open",
                "call-lot",
                option_type="call",
                position_side="long",
                strike=110,
                event_time_ms=BASE_TIME_MS + 2_000,
            ),
            raw_payload={},
        ),
    )

    result = reconcile_combo_pair_inferences(
        repo=repo,
        account="lx",
        runtime_environment=RUNTIME_ENVIRONMENT,
        persist=True,
        effective_now_ms=BASE_TIME_MS + 3_000,
    )

    assert result["inferences"] == []
    assert repo.list_combo_pair_inferences(account="lx") == []


def test_reject_is_idempotent_and_does_not_change_trade_events(tmp_path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    for event in (
        _event(
            "call-open",
            "call-lot",
            option_type="call",
            position_side="long",
            strike=110,
            event_time_ms=BASE_TIME_MS + 1_000,
        ),
        _event(
            "put-open",
            "put-lot",
            option_type="put",
            position_side="short",
            strike=100,
            event_time_ms=BASE_TIME_MS + 2_000,
        ),
    ):
        persist_trade_event_object(repo, event)
    proposal = reconcile_combo_pair_inferences(
        repo=repo,
        account="lx",
        runtime_environment=RUNTIME_ENVIRONMENT,
        persist=True,
        effective_now_ms=BASE_TIME_MS + 3_000,
    )["inferences"][0]
    for _ in range(2):
        rejected = reject_post_trade_combo_pair(
            repo=repo,
            inference_id=proposal["inference_id"],
            expected_input_hash=proposal["input_snapshot_hash"],
            reason="not a combo",
            actor="tester",
            effective_now_ms=BASE_TIME_MS + 4_000,
        )
        assert rejected["status"] == "user_rejected"
    assert len(repo.list_trade_events()) == 2


def test_confirm_rolls_back_events_projection_identity_and_inference_on_failure(
    tmp_path,
    monkeypatch,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    for event in (
        _event(
            "call-open",
            "call-lot",
            option_type="call",
            position_side="long",
            strike=110,
            event_time_ms=BASE_TIME_MS + 1_000,
        ),
        _event(
            "put-open",
            "put-lot",
            option_type="put",
            position_side="short",
            strike=100,
            event_time_ms=BASE_TIME_MS + 2_000,
        ),
    ):
        persist_trade_event_object(repo, event)
    proposal = reconcile_combo_pair_inferences(
        repo=repo,
        account="lx",
        runtime_environment=RUNTIME_ENVIRONMENT,
        persist=True,
        effective_now_ms=BASE_TIME_MS + 3_000,
    )["inferences"][0]

    def _fail_identity(*_args, **_kwargs):
        raise RuntimeError("injected identity failure")

    monkeypatch.setattr(repo, "insert_strategy_group_identity", _fail_identity)
    with pytest.raises(RuntimeError, match="injected identity failure"):
        adopt_post_trade_combo_pair(
            repo=repo,
            inference_id=proposal["inference_id"],
            expected_input_hash=proposal["input_snapshot_hash"],
            actor="tester",
            apply_changes=True,
            effective_now_ms=BASE_TIME_MS + 4_000,
        )

    assert len(repo.list_trade_events()) == 2
    assert repo.list_strategy_group_identities(account="lx") == []
    assert repo.get_combo_pair_inference(proposal["inference_id"])["status"] == "proposal_ready"
    assert all(
        not item["fields"].get("strategy_group_id")
        for item in repo.list_position_lots()
    )


def test_supersede_rolls_back_both_voids_and_projection_on_failure(
    tmp_path,
    monkeypatch,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    for event in (
        _event(
            "call-open",
            "call-lot",
            option_type="call",
            position_side="long",
            strike=110,
            event_time_ms=BASE_TIME_MS + 1_000,
        ),
        _event(
            "put-open",
            "put-lot",
            option_type="put",
            position_side="short",
            strike=100,
            event_time_ms=BASE_TIME_MS + 2_000,
        ),
    ):
        persist_trade_event_object(repo, event)
    proposal = reconcile_combo_pair_inferences(
        repo=repo,
        account="lx",
        runtime_environment=RUNTIME_ENVIRONMENT,
        persist=True,
        effective_now_ms=BASE_TIME_MS + 3_000,
    )["inferences"][0]
    adopt_post_trade_combo_pair(
        repo=repo,
        inference_id=proposal["inference_id"],
        expected_input_hash=proposal["input_snapshot_hash"],
        actor="tester",
        apply_changes=True,
        effective_now_ms=BASE_TIME_MS + 4_000,
    )
    original_transition = repo.transition_combo_pair_inference

    def _fail_supersede(*args, **kwargs):
        if kwargs.get("new_status") == "superseded":
            raise RuntimeError("injected supersede transition failure")
        return original_transition(*args, **kwargs)

    monkeypatch.setattr(repo, "transition_combo_pair_inference", _fail_supersede)
    with pytest.raises(RuntimeError, match="injected supersede transition failure"):
        supersede_post_trade_combo_pair(
            repo=repo,
            inference_id=proposal["inference_id"],
            expected_input_hash=proposal["input_snapshot_hash"],
            reason="wrong pair",
            actor="tester",
            apply_changes=True,
            effective_now_ms=BASE_TIME_MS + 5_000,
        )

    assert len(repo.list_trade_events()) == 4
    assert repo.get_combo_pair_inference(proposal["inference_id"])["status"] == "user_confirmed"
    assert {
        item["fields"].get("strategy_group_id")
        for item in repo.list_position_lots()
    } == {proposal["strategy_group_id"]}


def test_two_confirmations_competing_for_one_leg_allow_only_one_commit(
    tmp_path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    for event in (
        _event(
            "put-open",
            "put-lot",
            option_type="put",
            position_side="short",
            strike=100,
            event_time_ms=BASE_TIME_MS + 1_000,
        ),
        _event(
            "call-open-a",
            "call-lot-a",
            option_type="call",
            position_side="long",
            strike=110,
            event_time_ms=BASE_TIME_MS + 2_000,
        ),
        _event(
            "call-open-b",
            "call-lot-b",
            option_type="call",
            position_side="long",
            strike=120,
            event_time_ms=BASE_TIME_MS + 3_000,
        ),
    ):
        persist_trade_event_object(repo, event)
    proposals = reconcile_combo_pair_inferences(
        repo=repo,
        account="lx",
        runtime_environment=RUNTIME_ENVIRONMENT,
        persist=True,
        effective_now_ms=BASE_TIME_MS + 4_000,
    )["inferences"]
    assert len(proposals) == 2

    first, second = proposals
    adopt_post_trade_combo_pair(
        repo=repo,
        inference_id=first["inference_id"],
        expected_input_hash=first["input_snapshot_hash"],
        actor="tester-a",
        apply_changes=True,
        effective_now_ms=BASE_TIME_MS + 5_000,
    )
    with pytest.raises(ValueError, match="already claimed|facts changed|fully ungrouped"):
        adopt_post_trade_combo_pair(
            repo=repo,
            inference_id=second["inference_id"],
            expected_input_hash=second["input_snapshot_hash"],
            actor="tester-b",
            apply_changes=True,
            effective_now_ms=BASE_TIME_MS + 6_000,
        )

    statuses = {
        item["inference_id"]: item["status"]
        for item in repo.list_combo_pair_inferences(account="lx")
    }
    assert list(statuses.values()).count("user_confirmed") == 1
    assert statuses[second["inference_id"]] == "ambiguous"
