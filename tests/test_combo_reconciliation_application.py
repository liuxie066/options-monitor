from __future__ import annotations

import json
from dataclasses import replace

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.trade_contract_identity import derive_trade_side
from domain.domain.trade_execution import execution_identity_from_input
from src.application.ledger.combo_reconciliation import (
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
    execution = {"external_id_namespace": "futu.deal", "external_execution_id": event_id,
        "broker_account_ref": {"broker_id": "futu", "external_account_id": "1001", "environment": "REAL", "account_label": "lx"}}
    return TradeEvent(
        multiplier=100,
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
            "execution_input": execution, "execution_id": execution_identity_from_input(execution),
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
    from unittest.mock import patch
    from src.application.trades import attribution
    from src.application.ledger.api import read_trade_attribution_snapshot
    config = {"accounts": ["lx"], "market": "us", "account_settings": {"lx": {"futu": {"account_id": "1001", "trd_env": "REAL"}}}, "trade_intake": {"combo_reconciliation": {"accounts": {"lx": "confirm"}}}}
    context = {"config": config, "market": "us", "combo_evidence": {"complete": True, "exposures": []},
               "capacity_observation": {}, "combo_mode": "confirm"}
    # These tests exercise Combo membership/persistence; broker capacity has its own fixtures.
    with patch("time.time", return_value=effective_now_ms / 1000), patch.object(attribution,
            "trade_attribution_capacity_check", return_value={"status": "available", "reason_codes": []}), patch.object(
            attribution, "read_trade_attribution_context", return_value=context):
        view = attribution.build_trade_attribution_view(read_trade_attribution_snapshot(repo, account="lx", market="us"),
            account="lx", now_ms=effective_now_ms, **context)
        fact = next(row for row in view["rows"] if row["lot_id"] == proposal["put_record_id"])
        proposal.setdefault("attribution_input_hash", fact["input_hash"])
        return attribution.apply_referenced_trade_attribution(repo, account="lx", config=config,
            runtime_root=repo.db_path.parent, inference_id=proposal["inference_id"],
            expected_input_hash=proposal["attribution_input_hash"], request_id="combo-test:" + proposal["inference_id"],
            actor=actor, apply_changes=apply_changes)


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


@pytest.mark.parametrize("runtime_source", ["argument", "data_config"])
def test_confirm_combo_cli_uses_shared_atomic_proofs(tmp_path, monkeypatch, capsys, runtime_source):
    from src.interfaces.cli import option_positions as cli
    from src.application.trades import attribution
    from src.application.ledger.api import read_trade_attribution_snapshot
    runtime_root = tmp_path / "active-runtime"
    repo = SQLiteOptionPositionsRepository(runtime_root / "output_shared/state/option_positions.sqlite3")
    persist_trade_event_object(repo, _call_open())
    persist_trade_event_object(repo, _put_open())
    now = BASE_TIME_MS + 3000
    _reconcile(repo, effective_now_ms=now)
    proposal = repo.list_combo_pair_inferences(account="lx")[0]
    config = {"market": "us", "accounts": ["lx"], "account_settings": {"lx": {"futu": {"account_id": "1001", "trd_env": "REAL"}}},
        "trade_intake": {"combo_reconciliation": {"accounts": {"lx": "confirm"}}}}
    config_path = tmp_path / "configuration/config.us.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps(config))
    data_path = runtime_root / "data.json"
    data_path.write_text(json.dumps({"option_positions": {"sqlite_path": str(repo.db_path)}}))
    context = dict(config=config, market="us", combo_mode="confirm", combo_evidence={"complete": True, "exposures": []}, capacity_observation={})
    monkeypatch.setattr("time.time", lambda: now / 1000)
    observed_roots = []

    def read_context(*a, **kwargs):
        observed_roots.append(kwargs["runtime_root"])
        assert kwargs["runtime_root"] == runtime_root
        return context

    monkeypatch.setattr(attribution, "read_trade_attribution_context", read_context)
    monkeypatch.delenv("OM_RUNTIME_ROOT", raising=False)
    monkeypatch.setattr(attribution, "trade_attribution_capacity_check", lambda **k: {"status": "available", "reason_codes": []})
    view = attribution.build_trade_attribution_view(read_trade_attribution_snapshot(repo, account="lx", market="us"),
        account="lx", now_ms=now, **context)
    fact = next(row for row in view["rows"] if row["lot_id"] == proposal["put_record_id"])
    argv = ["--data-config", str(data_path), "confirm-combo", "--config", str(config_path),
        "--inference-id", proposal["inference_id"],
        "--expected-input-hash", fact["input_hash"], "--actor", "tester"]
    if runtime_source == "argument":
        argv.extend(["--runtime-root", str(runtime_root)])
    before = repo.list_trade_events()
    for mode in ("off", "observe"):
        config["trade_intake"]["combo_reconciliation"]["accounts"]["lx"] = mode
        config_path.write_text(json.dumps(config))
        for flags in ([], ["--apply", "--confirm"]):
            with pytest.raises(ValueError, match="[Cc]ombo.*mode"):
                cli.main(argv + flags)
            assert repo.list_trade_events() == before
    config["trade_intake"]["combo_reconciliation"]["accounts"]["lx"] = "confirm"
    config_path.write_text(json.dumps(config))
    assert cli.main(argv) == 0
    preview = json.loads(capsys.readouterr().out)
    assert repo.list_trade_events() == before
    assert cli.main(argv + ["--apply", "--confirm"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert observed_roots
    assert applied["proof_event_ids"] == preview["proof_event_ids"]
    assert applied["write_applied"] and len(applied["proof_event_ids"]) == 2
    monkeypatch.setattr(attribution, "read_trade_attribution_context", lambda *a, **k: pytest.fail("recovery must read proofs"))
    after = repo.list_trade_events()
    for mode in ("confirm", "observe", "off"):
        config["trade_intake"]["combo_reconciliation"]["accounts"]["lx"] = mode
        config_path.write_text(json.dumps(config))
        assert cli.main(argv + ["--apply", "--confirm"]) == 0
        recovered = json.loads(capsys.readouterr().out)
        assert recovered["status"] == "already_confirmed"
        assert not recovered["write_applied"]
        assert recovered["proof_event_ids"] == applied["proof_event_ids"]
        assert recovered["confirmation_mode"]["mode"] == mode
        for flag, wrong in (("--expected-input-hash", "wrong-hash"), ("--actor", "other-actor")):
            invalid = argv[:]
            invalid[invalid.index(flag) + 1] = wrong
            with pytest.raises(ValueError, match="attribution request identity conflicts"):
                cli.main(invalid + ["--apply", "--confirm"])
        assert repo.list_trade_events() == after
    assert len(after) == len(before) + 2


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
    assert preview["write_applied"] is False

    # The alias must only bridge the key rename: a genuine fact change still
    # has to abort, otherwise the precondition has been blunted into a no-op.
    def to_drifted_strike(snapshot: dict) -> None:
        snapshot["strike"] = "999"

    _rewrite_stored_snapshot(
        repo, inference_id=proposal["inference_id"], mutate=to_drifted_strike
    )
    with pytest.raises(ValueError, match="evidence changed"):
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
    assert preview["write_applied"] is False
    assert len(repo.list_trade_events()) == 2

    adopted = _adopt(repo, proposal, apply_changes=True, effective_now_ms=BASE_TIME_MS + 4_000)
    assert len(adopted["decision"]["members"]) == 2
    assert repo.list_strategy_group_identities(account="lx")
    assert read_current_decision_projection(
        repo,
        account="lx",
        now_ms=BASE_TIME_MS + 4_000,
    )["payload"]["combo"]["current_groups"][0]["status"] == "active_combo"
    assert repo.get_combo_pair_inference(proposal["inference_id"])["status"] == "user_confirmed"
    assert len(repo.list_trade_events()) == 4

    repeated = _adopt(repo, proposal, apply_changes=True, effective_now_ms=BASE_TIME_MS + 5_000)
    assert repeated["write_applied"] is False
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
    with pytest.raises(ValueError, match="explicit complete member decision"):
        _adopt(repo, second, apply_changes=True, effective_now_ms=BASE_TIME_MS + 6_000, actor="tester-b")

    statuses = {
        item["inference_id"]: item["status"]
        for item in repo.list_combo_pair_inferences(account="lx")
    }
    assert list(statuses.values()).count("user_confirmed") == 1
    assert statuses[second["inference_id"]] == "ambiguous"


@pytest.mark.parametrize("original_strategy", ["csp", "wheel", "combo_yield"])
def test_complete_combo_transfer_proves_later_assignment(tmp_path, monkeypatch, original_strategy):
    from copy import deepcopy
    import time
    from domain.domain.wheel import lot_strategy_metadata_from_trade_events
    from src.application.ledger.api import (
        read_trade_attribution_snapshot, record_trade_attribution_conflict, with_sqlite_repo_transaction,
    )
    from src.application.ledger.combo_membership import (
        publish_combo_pair_identity, resolve_combo_group_membership, resolve_combo_assignment_proof,
    )
    from src.application.ledger.wheel_trade_companions import plan_wheel_assignment_companion
    from src.application.ledger.writer import persist_trade_event_objects_atomically
    from src.application.trades import attribution
    from src.application.wheel.read_model import build_wheel_read_model
    from src.application.wheel.workflows import decide_wheel_branch
    from test_wheel_workflows import (
        _wheel_repo, _persist_wheel_call_open, _persist_wheel_call_assignment,
    )

    repo, branch_id = _wheel_repo(tmp_path)
    if original_strategy == "wheel":
        _persist_wheel_call_open(repo, event_id="prior-wheel-call", lot_id="prior-wheel-call-lot",
                                 source_stock_lot_id=branch_id)
        _persist_wheel_call_assignment(repo, event_id="prior-wheel-assignment", lot_id="prior-wheel-call-lot")
        branch = next(row for row in build_wheel_read_model(repo, "lx", 5000)["wheel_branches"]
                      if row["direction"] == "put")
        branch_id = branch["wheel_branch_id"]
        decide_wheel_branch(repo, account="lx", wheel_branch_id=branch_id, decision="start",
            expected_batch_generation_hash=branch["batch_generation_hash"], request_id="start-prior-wheel",
            actor="tester", market="us", account_configured=True, policy_sha256="a" * 64,
            activation_descriptor={"market": "us", "account": "lx", "generation": 1,
                "activated_at_ms": 500, "deactivated_at_ms": None, "policy_hash": "a" * 64},
            apply_changes=True, as_of_ms=6000)
    old_group = "combo_yield:lx:old-pair"
    put = _put_open()
    raw = {**put.raw_payload, "strategy": original_strategy,
           "source_type": "broker_trade_event", "multiplier_source": "payload"}
    if original_strategy == "wheel":
        raw.update(leg_role="wheel_put", source_wheel_branch_id=branch_id)
    elif original_strategy == "combo_yield":
        raw.update(leg_role="funding_put", strategy_group_id=old_group)
    put = replace(put, raw_payload=raw)
    events = [put, _call_open("new-call-open", "new-call-lot", strike=115)]
    if original_strategy == "combo_yield":
        old_call = _call_open()
        events.append(replace(old_call, raw_payload={**old_call.raw_payload, "strategy": "combo_yield",
            "strategy_group_id": old_group, "leg_role": "participation_call"}))
    persist_trade_event_objects_atomically(repo, events)
    if original_strategy == "combo_yield":
        with repo._writer_connection(begin_immediate=True) as conn:
            _, membership = publish_combo_pair_identity(repo, conn=conn, inference={
                "strategy_group_id": old_group, "account": "lx", "symbol": "NVDA",
                "put_record_id": "put-lot", "put_open_event_id": "put-open",
                "call_record_id": "call-lot", "call_open_event_id": "call-open"})
        assert membership.fact["status"] == "exact"
    config = {"market": "us", "accounts": ["lx"],
        "account_settings": {"lx": {"futu": {"account_id": "1001", "trd_env": "REAL"}}},
        "trade_intake": {"combo_reconciliation": {"accounts": {"lx": "confirm"}}}}
    evidence = {"complete": True, "exposures": []}
    now = BASE_TIME_MS + 5000
    monkeypatch.setattr(time, "time", lambda: now / 1000)
    # Isolate the broker snapshot; use the real transfer, membership and proof owners.
    monkeypatch.setattr(attribution, "trade_attribution_capacity_check",
                        lambda **_: {"status": "available", "reason_codes": []})

    def view():
        return attribution.build_trade_attribution_view(
            read_trade_attribution_snapshot(repo, account="lx", market="us"), config=config,
            account="lx", market="us", now_ms=now, combo_evidence=evidence,
            capacity_observation={}, combo_mode="confirm")

    current = view()
    fact = next(row for row in current["rows"] if row["lot_id"] == "put-lot")
    target = next(row["candidate_id"] for row in fact["candidates"]
                  if row["strategy"] == "combo_yield" and "new-call-lot" in row["member_lot_ids"])
    conflict = with_sqlite_repo_transaction(repo, lambda active, conn: record_trade_attribution_conflict(
        active, account="lx", execution_key=fact["execution_key"],
        branch=next(row for row in current["wheel_model"]["wheel_branches"] if row["wheel_branch_id"] == branch_id),
        candidate_ids=[target], input_hash=fact["input_hash"], now_ms=now, conn=conn))
    now += 100
    current = view()
    fact = next(row for row in current["rows"] if row["lot_id"] == "put-lot")
    members = tuple({"execution_key": row["execution_key"],
                     "candidate_id": "ordinary" if row["lot_id"] == "call-lot" else target}
                    for row in current["rows"] if row["lot_id"] in {"put-lot", "call-lot", "new-call-lot"})
    result = attribution.apply_trade_attribution(repo, account="lx", market="us", config=config,
        execution_key=fact["execution_key"], candidate_id=target, expected_input_hash=fact["input_hash"],
        request_id="transfer-to-new-combo", actor="tester", combo_evidence=evidence,
        capacity_observation={}, combo_mode="confirm", manual=True, conflict_event_ids=(conflict,),
        member_decisions=members)
    assert result["write_applied"]
    history = repo.list_trade_events()
    accepted = set()
    lot_strategy_metadata_from_trade_events(history, accepted_proof_event_ids=accepted)
    assert set(result["proof_event_ids"]) <= accepted
    group_id = target.removeprefix("combo:")
    membership = resolve_combo_group_membership(group_id=group_id, account="lx",
        trade_events=history, projected_position_lots=repo.list_position_lots())
    assert membership.fact["status"] == "exact"
    if original_strategy == "combo_yield":
        assert resolve_combo_group_membership(group_id=old_group, account="lx", trade_events=history,
            projected_position_lots=repo.list_position_lots()).fact["status"] == "released"
    assignment = replace(put, event_id="assignment-after-transfer", event_type="assignment",
        event_time_ms=now + 100, lot_id=None, target_lot_id="put-lot", price=0,
        raw_payload={"side": "buy", "stock_settlement": {"side": "buy", "shares": 100,
            "price": 100, "fees": 0, "currency": "USD",
            "fee_provenance": {"basis": "actual", "source": "test"}}})
    identities = repo.list_strategy_group_identities(account="lx")
    assert resolve_combo_assignment_proof(assignment=assignment, group_id=group_id,
        trade_events=history, identities=identities) == ("csp_lc", None)
    rows = repo.read_lifecycle_account_rows(account="lx")
    fields = repo.get_position_lot_fields("put-lot")
    activation = repo.get_current_wheel_activation_window(market="us", account="lx")
    companion, reason = plan_wheel_assignment_companion(assignment, fields, rows, activation,
                                                       recorded_at_ms=now + 100)
    assert reason is None
    assert companion["payload"]["direction"] == "call"
    assert companion["payload"]["initial_lifecycle_status"] == "active"
    assert companion["payload"]["parent_branch_id"] is None
    broken = deepcopy(history)
    for row in broken:
        if row["event_id"] in result["proof_event_ids"]:
            row["raw_payload"]["attribution_decision"]["members"][0]["before"]["strategy_snapshot"] = {"never": "existed"}
    assert resolve_combo_assignment_proof(assignment=assignment, group_id=group_id,
        trade_events=broken, identities=identities)[0] is None
    if original_strategy != "combo_yield":
        return  # Only old-Combo transfer carries a third member outside the new pair.
    inference = next(row for row in repo.list_combo_pair_inferences(account="lx")
                     if row["status"] == "user_confirmed" and row["strategy_group_id"] == group_id)
    before = (repo.list_trade_events(), repo.list_position_lots(), repo.list_combo_pair_inferences(account="lx"))
    for apply_changes in (False, True):
        with pytest.raises(ValueError, match="complete attribution decision"):
            supersede_post_trade_combo_pair(repo=repo, inference_id=inference["inference_id"],
                expected_input_hash=inference["input_snapshot_hash"], reason="wrong pair", actor="tester",
                effective_now_ms=now + 200, apply_changes=apply_changes)
        assert (repo.list_trade_events(), repo.list_position_lots(), repo.list_combo_pair_inferences(account="lx")) == before
