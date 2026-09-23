from __future__ import annotations

import json
from dataclasses import replace

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.trade_contract_identity import derive_trade_side
from src.application.ledger.combo_reconciliation import (
    adopt_post_trade_combo_pair,
    reconcile_combo_pair_inferences,
    reject_post_trade_combo_pair,
    supersede_post_trade_combo_pair,
)
from src.application.ledger.current_decision_projection import (
    build_current_decision_projection,
    current_decision_projection_row,
    empty_assigned_stock_fact,
    read_current_decision_projection,
)
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.writer import persist_trade_event_object


BASE_TIME_MS = 1_785_312_000_000
RUNTIME_ENVIRONMENT = "opend:127.0.0.1:11111"


def _event(
    event_id: str,
    lot_id: str,
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
            strike=strike,
            expiration_ymd="2026-08-21",
                ),
        contracts=1,
        price=1,
        currency="USD",
        source="test",
        lot_id=lot_id,
        raw_payload={
            "execution_input": {"broker_account_ref": {"broker_id": "futu", "external_account_id": "1001", "environment": "REAL", "account_label": "lx"}},
            # §9.2 step 3: the contract key no longer carries the position side,
            # so the fixture's side travels as the trade side of this open.
            "side": derive_trade_side("open", position_side) or "",
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


def _call_open(
    event_id: str = "call-open",
    lot_id: str = "call-lot",
    *,
    strike: int = 110,
    event_time_ms: int = BASE_TIME_MS + 1_000,
    opend_host: str = "127.0.0.1",
) -> TradeEvent:
    """A long call open; defaults copied verbatim from the repeated inline fixture."""
    return _event(
        event_id,
        lot_id,
        option_type="call",
        position_side="long",
        strike=strike,
        event_time_ms=event_time_ms,
        opend_host=opend_host,
    )


def _put_open(
    event_id: str = "put-open",
    lot_id: str = "put-lot",
    *,
    strike: int = 100,
    event_time_ms: int = BASE_TIME_MS + 2_000,
    opend_host: str = "127.0.0.1",
) -> TradeEvent:
    """A short put open; defaults copied verbatim from the repeated inline fixture."""
    return _event(
        event_id,
        lot_id,
        option_type="put",
        position_side="short",
        strike=strike,
        event_time_ms=event_time_ms,
        opend_host=opend_host,
    )


def _reconcile(repo, *, effective_now_ms: int, persist: bool = True) -> dict:
    return reconcile_combo_pair_inferences(
        repo=repo,
        account="lx",
        runtime_environment=RUNTIME_ENVIRONMENT,
        persist=persist,
        effective_now_ms=effective_now_ms,
    )


def _adopt(
    repo,
    proposal: dict,
    *,
    effective_now_ms: int,
    apply_changes: bool,
    actor: str = "tester",
) -> dict:
    return adopt_post_trade_combo_pair(
        repo=repo,
        inference_id=proposal["inference_id"],
        expected_input_hash=proposal["input_snapshot_hash"],
        actor=actor,
        apply_changes=apply_changes,
        effective_now_ms=effective_now_ms,
    )


def _supersede(repo, proposal: dict, *, effective_now_ms: int) -> dict:
    return supersede_post_trade_combo_pair(
        repo=repo,
        inference_id=proposal["inference_id"],
        expected_input_hash=proposal["input_snapshot_hash"],
        reason="wrong pair",
        actor="tester",
        apply_changes=True,
        effective_now_ms=effective_now_ms,
    )


def test_application_reconcile_is_post_trade_and_persists_only_inference_state(
    tmp_path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    persist_trade_event_object(repo, _call_open())

    waiting = _reconcile(repo, effective_now_ms=BASE_TIME_MS + 2_000, persist=False)
    assert waiting["inferences"] == []
    assert waiting["waiting_for_counterpart"][0]["record_id"] == "call-lot"
    assert repo.list_combo_pair_inferences(account="lx") == []

    persist_trade_event_object(repo, _put_open())
    persisted = _reconcile(repo, effective_now_ms=BASE_TIME_MS + 3_000)

    assert persisted["proposal_ready_count"] == 1
    assert persisted["inserted_inference_count"] == 1
    stored = repo.list_combo_pair_inferences(account="lx")
    assert len(stored) == 1
    assert stored[0]["put_open_event_id"] == "put-open"
    assert stored[0]["call_open_event_id"] == "call-open"
    assert len(repo.list_trade_events()) == 2
    # The strategy family left the lot payload (``write-side-definition.md``
    # §2/§7); "nothing is grouped yet" is the strategy group identity table's
    # answer now, and the payload must not carry the retired keys at all.
    assert repo.list_strategy_group_identities(account="lx") == []
    assert not any(
        {"strategy", "strategy_group_id", "leg_role"} & set(item["fields"])
        for item in repo.list_position_lots()
    )


def _rewrite_stored_snapshot(
    repo: SQLiteOptionPositionsRepository,
    *,
    inference_id: str,
    mutate,
) -> None:
    """Rewrite a persisted inference's lot snapshots in place, bypassing the writer."""
    with repo._connect() as conn:  # noqa: SLF001 - persisted-input fixture
        row = conn.execute(
            "SELECT raw_json FROM combo_pair_inferences WHERE inference_id = ?",
            (inference_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["raw_json"])
        for prefix in ("put", "call"):
            mutate(payload[f"{prefix}_lot_snapshot"])
        conn.execute(
            "UPDATE combo_pair_inferences SET raw_json = ? WHERE inference_id = ?",
            (
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                inference_id,
            ),
        )


def test_confirm_accepts_legacy_contracts_original_snapshot_key(tmp_path) -> None:
    """§7.3 renamed the snapshot field ``contracts_original`` -> ``contracts_opened``.

    Inferences persisted before the rename still carry the old key. The
    confirmation precondition reads the stored snapshot by the *current* field
    name, so without a read-side alias the unchanged quantity is reported as
    ``contracts_opened`` changed and confirmation hard-fails with a misleading
    "input facts changed" error on rows that are actually intact.
    """
    repo = SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    for event in (_call_open(), _put_open()):
        persist_trade_event_object(repo, event)
    reconciled = _reconcile(repo, effective_now_ms=BASE_TIME_MS + 3_000)
    proposal = reconciled["inferences"][0]

    def to_legacy(snapshot: dict) -> None:
        assert snapshot.pop("contracts_opened") == 1
        snapshot["contracts_original"] = 1

    _rewrite_stored_snapshot(
        repo, inference_id=proposal["inference_id"], mutate=to_legacy
    )

    preview = _adopt(repo, proposal, apply_changes=False, effective_now_ms=BASE_TIME_MS + 4_000)
    assert preview["status"] == "dry_run"

    # The alias must only bridge the key rename: a genuine fact change still
    # has to abort, otherwise the precondition has been blunted into a no-op.
    def to_drifted_strike(snapshot: dict) -> None:
        snapshot["strike"] = "999"

    _rewrite_stored_snapshot(
        repo, inference_id=proposal["inference_id"], mutate=to_drifted_strike
    )
    with pytest.raises(ValueError, match="input facts changed: .*strike"):
        _adopt(repo, proposal, apply_changes=False, effective_now_ms=BASE_TIME_MS + 4_000)


def test_confirm_reject_and_supersede_are_exact_atomic_decisions(tmp_path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    for event in (_call_open(), _put_open()):
        persist_trade_event_object(repo, event)
    reconciled = _reconcile(repo, effective_now_ms=BASE_TIME_MS + 3_000)
    proposal = reconciled["inferences"][0]
    with repo._connect() as conn:  # noqa: SLF001 - migrated generation seed
        conn.execute(
            """
            INSERT INTO current_decision_input_generations (
              account, generation, case_generation, evidence_generation,
              allocation_generation, source_consumption_generation,
              timing_generation, combo_identity_generation,
              assigned_stock_generation, updated_at_ms
            ) VALUES ('lx', 0, 0, 0, 0, 0, 0, 0, 0, 1)
            """
        )
    projection = build_current_decision_projection(
        repo,
        account="lx",
        updated_at_ms=BASE_TIME_MS + 3_500,
        assigned_stock_after=empty_assigned_stock_fact("lx"),
        all_quality_case_facts=[],
    )
    repo.upsert_current_decision_projection(
        current_decision_projection_row(projection)
    )
    preview = _adopt(repo, proposal, apply_changes=False, effective_now_ms=BASE_TIME_MS + 4_000)
    assert preview["status"] == "dry_run"
    assert len(repo.list_trade_events()) == 2

    adopted = _adopt(repo, proposal, apply_changes=True, effective_now_ms=BASE_TIME_MS + 4_000)
    assert adopted["membership"]["status"] == "exact"
    assert adopted["decision_projection"]["statuses"] == {"lx": "published"}
    assert read_current_decision_projection(
        repo,
        account="lx",
        now_ms=BASE_TIME_MS + 4_000,
    )["payload"]["combo"]["current_groups"][0]["status"] == "active_combo"
    assert repo.get_combo_pair_inference(proposal["inference_id"])["status"] == "user_confirmed"
    assert len(repo.list_trade_events()) == 4

    repeated = _adopt(repo, proposal, apply_changes=True, effective_now_ms=BASE_TIME_MS + 5_000)
    assert repeated["status"] == "already_confirmed"
    assert len(repo.list_trade_events()) == 4

    superseded = _supersede(repo, proposal, effective_now_ms=BASE_TIME_MS + 6_000)
    assert superseded["membership"]["status"] != "exact"
    assert superseded["decision_projection"]["statuses"] == {
        "lx": "explicit_rebuild_required"
    }
    assert read_current_decision_projection(
        repo,
        account="lx",
        now_ms=BASE_TIME_MS + 6_000,
    )["status"] == "data_unavailable"
    assert repo.get_combo_pair_inference(proposal["inference_id"])["status"] == "superseded"
    assert len(repo.list_trade_events()) == 6


def test_supersede_reactivates_alternative_that_expired_only_while_leg_was_claimed(
    tmp_path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    for event in (
        _put_open(event_time_ms=BASE_TIME_MS + 1_000),
        _call_open("call-a-open", "call-a-lot", event_time_ms=BASE_TIME_MS + 2_000),
        _call_open("call-b-open", "call-b-lot", strike=120, event_time_ms=BASE_TIME_MS + 2_000),
    ):
        persist_trade_event_object(repo, event)

    initial = _reconcile(repo, effective_now_ms=BASE_TIME_MS + 3_000)
    assert len(initial["inferences"]) == 2
    chosen = initial["inferences"][0]
    alternative_id = next(
        item["inference_id"]
        for item in initial["inferences"]
        if item["inference_id"] != chosen["inference_id"]
    )
    _adopt(repo, chosen, apply_changes=True, effective_now_ms=BASE_TIME_MS + 4_000)
    _reconcile(repo, effective_now_ms=BASE_TIME_MS + 5_000)
    assert repo.get_combo_pair_inference(alternative_id)["decision_reason"] == (
        "facts_drifted_or_leg_claimed"
    )

    _supersede(repo, chosen, effective_now_ms=BASE_TIME_MS + 6_000)
    reconciled = _reconcile(repo, effective_now_ms=BASE_TIME_MS + 7_000)

    assert [item["inference_id"] for item in reconciled["inferences"]] == [
        alternative_id
    ]
    assert repo.get_combo_pair_inference(alternative_id)["status"] == (
        reconciled["inferences"][0]["status"]
    )
    assert repo.get_combo_pair_inference(chosen["inference_id"])["status"] == (
        "superseded"
    )


def test_reconcile_same_physical_account_across_runtime_sources(
    tmp_path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    for event in (
        _put_open(event_time_ms=BASE_TIME_MS + 1_000, opend_host="127.0.0.1"),
        _call_open(event_time_ms=BASE_TIME_MS + 2_000, opend_host="127.0.0.2"),
    ):
        persist_trade_event_object(repo, event)

    result = _reconcile(repo, effective_now_ms=BASE_TIME_MS + 3_000)

    assert len(result["inferences"]) == 1
    assert len(repo.list_combo_pair_inferences(account="lx")) == 1


def test_reconcile_fails_closed_when_open_event_runtime_source_is_missing(
    tmp_path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    persist_trade_event_object(
        repo,
        _put_open(event_time_ms=BASE_TIME_MS + 1_000),
    )
    persist_trade_event_object(
        repo,
        replace(
            _call_open(event_time_ms=BASE_TIME_MS + 2_000),
            # §9.2 step 3: still a long call open, but without an intake source.
            raw_payload={"side": "buy"},
        ),
    )

    result = _reconcile(repo, effective_now_ms=BASE_TIME_MS + 3_000)

    assert result["inferences"] == []
    assert repo.list_combo_pair_inferences(account="lx") == []


def test_reject_is_idempotent_and_does_not_change_trade_events(tmp_path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    for event in (_call_open(), _put_open()):
        persist_trade_event_object(repo, event)
    proposal = _reconcile(repo, effective_now_ms=BASE_TIME_MS + 3_000)["inferences"][0]
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
    for event in (_call_open(), _put_open()):
        persist_trade_event_object(repo, event)
    proposal = _reconcile(repo, effective_now_ms=BASE_TIME_MS + 3_000)["inferences"][0]

    def _fail_identity(*_args, **_kwargs):
        raise RuntimeError("injected identity failure")

    monkeypatch.setattr(repo, "insert_strategy_group_identity", _fail_identity)
    with pytest.raises(RuntimeError, match="injected identity failure"):
        _adopt(repo, proposal, apply_changes=True, effective_now_ms=BASE_TIME_MS + 4_000)

    assert len(repo.list_trade_events()) == 2
    assert repo.list_strategy_group_identities(account="lx") == []
    assert repo.get_combo_pair_inference(proposal["inference_id"])["status"] == "proposal_ready"
    assert not any(
        {"strategy", "strategy_group_id", "leg_role"} & set(item["fields"])
        for item in repo.list_position_lots()
    )


def test_supersede_rolls_back_both_voids_and_projection_on_failure(
    tmp_path,
    monkeypatch,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    for event in (_call_open(), _put_open()):
        persist_trade_event_object(repo, event)
    proposal = _reconcile(repo, effective_now_ms=BASE_TIME_MS + 3_000)["inferences"][0]
    _adopt(repo, proposal, apply_changes=True, effective_now_ms=BASE_TIME_MS + 4_000)
    original_transition = repo.transition_combo_pair_inference

    def _fail_supersede(*args, **kwargs):
        if kwargs.get("new_status") == "superseded":
            raise RuntimeError("injected supersede transition failure")
        return original_transition(*args, **kwargs)

    monkeypatch.setattr(repo, "transition_combo_pair_inference", _fail_supersede)
    with pytest.raises(RuntimeError, match="injected supersede transition failure"):
        _supersede(repo, proposal, effective_now_ms=BASE_TIME_MS + 5_000)

    assert len(repo.list_trade_events()) == 4
    assert repo.get_combo_pair_inference(proposal["inference_id"])["status"] == "user_confirmed"
    # The group binding lives in the strategy group identity table now (the lot
    # payload no longer carries ``strategy_group_id``).
    assert {
        str(item.get("group_id") or "")
        for item in repo.list_strategy_group_identities(account="lx")
    } == {proposal["strategy_group_id"]}
    assert all(
        item["fields"].get("contract_key")
        for item in repo.list_position_lots()
    )


def test_two_confirmations_competing_for_one_leg_allow_only_one_commit(
    tmp_path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    for event in (
        _put_open(event_time_ms=BASE_TIME_MS + 1_000),
        _call_open("call-open-a", "call-lot-a", event_time_ms=BASE_TIME_MS + 2_000),
        _call_open("call-open-b", "call-lot-b", strike=120, event_time_ms=BASE_TIME_MS + 3_000),
    ):
        persist_trade_event_object(repo, event)
    proposals = _reconcile(repo, effective_now_ms=BASE_TIME_MS + 4_000)["inferences"]
    assert len(proposals) == 2

    first, second = proposals
    _adopt(repo, first, apply_changes=True, effective_now_ms=BASE_TIME_MS + 5_000, actor="tester-a")
    with pytest.raises(ValueError, match="already claimed|facts changed|fully ungrouped"):
        _adopt(repo, second, apply_changes=True, effective_now_ms=BASE_TIME_MS + 6_000, actor="tester-b")

    statuses = {
        item["inference_id"]: item["status"]
        for item in repo.list_combo_pair_inferences(account="lx")
    }
    assert list(statuses.values()).count("user_confirmed") == 1
    assert statuses[second["inference_id"]] == "ambiguous"
