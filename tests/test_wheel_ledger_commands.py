"""Public Wheel commands own atomic effects and safe retries on real SQLite."""
from copy import deepcopy

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from src.application.ledger import api, wheel_commands, wheel_policy_binding
from src.application.ledger.writer import persist_trade_event_objects_atomically
from src.application.wheel import workflows
from src.application.wheel.read_model import build_wheel_read_model
from tests.test_wheel_intent_policy_binding import _environment
from tests.test_wheel_workflows import _wheel_repo, _open_unlinked_call, _trusted_multiplier_payload


def _linkage_scope(tmp_path, monkeypatch, direction):
    if direction == "call":
        repo, _ = _wheel_repo(tmp_path)
        _open_unlinked_call(repo)
    else:
        repo, _, _, _, _ = _environment(tmp_path, "put", monkeypatch)
        persist_trade_event_objects_atomically(repo, [TradeEvent(
            event_id="unowned-put-open", event_type="open", event_time_ms=4_500,
            contract_key=ContractKey.from_values(
                broker="富途", account="lx", underlying_symbol="NVDA", option_type="put",
                strike=100, expiration_ymd="2026-09-18"),
            contracts=1, price=1, currency="USD", source="test", multiplier=100,
            lot_id="unowned-put-lot",
            raw_payload=_trusted_multiplier_payload("unowned-put-open", side="sell"),
        )])
    model = build_wheel_read_model(repo, "lx", 5_000, market="us")
    candidate = next(item for item in model["linkage_candidates"] if item["direction"] == direction)
    common = dict(
        account="lx", linkage_candidate_id=candidate["linkage_candidate_id"],
        expected_input_hash=candidate["input_snapshot_hash"],
        expected_batch_generation_hash=candidate["batch_generation_hash"],
        request_id="reject-once", actor="tester", reason="not this cycle", market="us", as_of_ms=5_000,
    )
    neutral = dict(common, direction=direction, wheel_branch_id=candidate["wheel_branch_id"],
                   option_lot_id=candidate["option_record_id"])
    if direction == "call":
        command = api.reject_wheel_call_linkage
        arguments = dict(common, call_lot_id=candidate["call_record_id"], lot_id=candidate["stock_lot_id"])
    else:
        command = api.reject_wheel_linkage
        arguments = neutral.copy()
    return repo, command, arguments, neutral, candidate


@pytest.mark.parametrize("direction", ["call", "put"])
@pytest.mark.parametrize("entry", ["command", "workflow"])
def test_linkage_rejection_preview_apply_and_retry_share_ledger_owner(tmp_path, monkeypatch, direction, entry):
    repo, command, arguments, neutral, candidate = _linkage_scope(tmp_path, monkeypatch, direction)
    assert workflows.reject_wheel_call_linkage is api.reject_wheel_call_linkage is wheel_commands.reject_wheel_call_linkage
    assert workflows.reject_wheel_linkage is api.reject_wheel_linkage is wheel_commands.reject_wheel_linkage
    monkeypatch.setattr(workflows, "with_sqlite_repo_transaction", lambda *a, **k: pytest.fail("application transaction"), raising=False)
    if entry == "workflow":
        command, arguments = workflows.reject_wheel_linkage, neutral
    before = repo.read_lifecycle_account_rows(account="lx")
    preview = command(repo, **arguments)
    assert preview["dry_run"] is True and preview["write_applied"] is False
    assert repo.read_lifecycle_account_rows(account="lx") == before
    applied = command(repo, **arguments, apply_changes=True)
    replay = command(repo, **arguments, apply_changes=True)
    assert applied["write_applied"] is True
    assert replay["status"] == "idempotent" and replay["write_applied"] is False
    assert preview["event_id"] == applied["event_id"] == replay["event_id"]
    assert len(repo.list_wheel_events(account="lx")) == len(before["account_wheel_events"]) + 1
    after = repo.read_lifecycle_account_rows(account="lx")
    assert after["trade_events"] == before["trade_events"]
    assert after["account_position_lots"] == before["account_position_lots"]
    model = build_wheel_read_model(repo, "lx", 5_000, market="us")
    assert all(item["linkage_candidate_id"] != candidate["linkage_candidate_id"] for item in model["linkage_candidates"])


@pytest.mark.parametrize("direction", ["call", "put"])
@pytest.mark.parametrize("failure", ["append", "readback"])
def test_linkage_rejection_rolls_back_real_append_then_retries(tmp_path, monkeypatch, direction, failure):
    repo, command, arguments, _, candidate = _linkage_scope(tmp_path, monkeypatch, direction)
    before = repo.read_lifecycle_account_rows(account="lx")
    original_append = repo.append_wheel_event_once
    appended = []

    def append_then_fail(event, *, conn=None):
        assert conn is not None and conn.in_transaction
        created = original_append(event, conn=conn)
        assert created
        assert any(row["event_id"] == event["event_id"] for row in
                   repo.read_lifecycle_account_rows(account="lx", conn=conn)["account_wheel_events"])
        appended.append(event["event_id"])
        if failure == "append":
            raise RuntimeError("injected append failure")
        return created

    with monkeypatch.context() as faults:
        faults.setattr(repo, "append_wheel_event_once", append_then_fail)
        if direction == "call":
            original_model = wheel_commands.build_wheel_read_model_from_rows

            def stale_model(*args, **kwargs):
                model = original_model(*args, **kwargs)
                if appended:
                    model["linkage_candidates"].append(deepcopy(candidate))
                return model

            faults.setattr(wheel_commands, "build_wheel_read_model_from_rows", stale_model)
        else:
            original_candidates = wheel_commands.project_wheel_linkage_candidates

            def stale_candidates(*args, **kwargs):
                candidates = original_candidates(*args, **kwargs)
                return [*candidates, deepcopy(candidate)] if appended else candidates

            faults.setattr(wheel_commands, "project_wheel_linkage_candidates", stale_candidates)
        with pytest.raises((ValueError, RuntimeError), match="injected append failure|verification failed"):
            command(repo, **arguments, apply_changes=True)
    assert len(appended) == 1
    assert repo.read_lifecycle_account_rows(account="lx") == before
    applied = command(repo, **arguments, apply_changes=True)
    assert applied["event_id"] == appended[0] and applied["write_applied"] is True
    assert command(repo, **arguments, apply_changes=True)["write_applied"] is False


@pytest.mark.parametrize("direction", ["call", "put"])
@pytest.mark.parametrize("field,value", [
    ("account", "sy"), ("market", "hk"), ("expected_input_hash", "stale"),
    ("expected_batch_generation_hash", "stale"), ("linkage_candidate_id", "wrong"),
])
def test_linkage_rejection_rejects_mismatched_facts_without_effects(tmp_path, monkeypatch, direction, field, value):
    repo, command, arguments, _, _ = _linkage_scope(tmp_path, monkeypatch, direction)
    before = repo.read_lifecycle_account_rows(account="lx")
    arguments[field] = value
    with pytest.raises(ValueError, match="stale or unavailable|input changed"):
        command(repo, **arguments, apply_changes=True)
    assert repo.read_lifecycle_account_rows(account="lx") == before


@pytest.mark.parametrize("direction", ["call", "put"])
def test_linkage_rejection_refuses_changed_request_identity(tmp_path, monkeypatch, direction):
    repo, command, arguments, _, _ = _linkage_scope(tmp_path, monkeypatch, direction)
    command(repo, **arguments, apply_changes=True)
    before = repo.read_lifecycle_account_rows(account="lx")
    for key in ("actor", "reason", "expected_input_hash", "expected_batch_generation_hash", "linkage_candidate_id"):
        with pytest.raises(ValueError, match="identity conflicts"):
            command(repo, **{**arguments, key: "changed"}, apply_changes=True)
    assert repo.read_lifecycle_account_rows(account="lx") == before


def test_neutral_call_reads_branch_and_candidate_only_inside_ledger_transaction(tmp_path, monkeypatch):
    repo, _, _, arguments, _ = _linkage_scope(tmp_path, monkeypatch, "call")
    read = repo.read_lifecycle_account_rows
    reads = []

    def locked_read(*, account, conn=None):
        assert conn is not None and conn.in_transaction
        reads.append(account)
        return read(account=account, conn=conn)

    monkeypatch.setattr(repo, "read_lifecycle_account_rows", locked_read)
    assert not hasattr(workflows, "_canonical_wheel_branch")
    assert not hasattr(workflows, "build_wheel_read_model")
    result = api.reject_wheel_linkage(repo, **arguments, apply_changes=True)
    assert result["status"] == "rejected" and reads == ["lx", "lx"]
    assert api.reject_wheel_linkage(repo, **arguments, apply_changes=True)["status"] == "idempotent"


@pytest.mark.parametrize("field,value", [
    ("account", "sy"), ("market", "hk"), ("direction", "unknown"),
    ("expected_input_hash", "stale"), ("expected_batch_generation_hash", "stale"),
    ("wheel_branch_id", "wrong-branch"), ("wheel_branch_id", ""),
])
def test_neutral_call_rejection_preserves_first_write_identity_checks(tmp_path, monkeypatch, field, value):
    repo, _, _, arguments, _ = _linkage_scope(tmp_path, monkeypatch, "call")
    before = repo.read_lifecycle_account_rows(account="lx")
    with pytest.raises(ValueError, match="resolve uniquely|input changed|call or put|complete fields"):
        api.reject_wheel_linkage(repo, **{**arguments, field: value}, apply_changes=True)
    assert repo.read_lifecycle_account_rows(account="lx") == before


def test_neutral_call_replay_rejects_changed_identity(tmp_path, monkeypatch):
    repo, _, _, arguments, _ = _linkage_scope(tmp_path, monkeypatch, "call")
    api.reject_wheel_linkage(repo, **arguments, apply_changes=True)
    before = repo.read_lifecycle_account_rows(account="lx")
    for key in ("actor", "reason", "option_lot_id", "expected_input_hash", "expected_batch_generation_hash", "linkage_candidate_id"):
        with pytest.raises(ValueError, match="identity conflicts"):
            api.reject_wheel_linkage(repo, **{**arguments, key: "changed"}, apply_changes=True)
    assert repo.read_lifecycle_account_rows(account="lx") == before


def _branch_scope(tmp_path):
    from tests.test_wheel_workflows import _persist_wheel_call_open, _persist_wheel_call_assignment

    repo, lot_id = _wheel_repo(tmp_path)
    _persist_wheel_call_open(repo, event_id="decision-call-open", lot_id="decision-call",
                             source_stock_lot_id=lot_id)
    _persist_wheel_call_assignment(repo, event_id="decision-call-assignment", lot_id="decision-call")
    branch = next(item for item in build_wheel_read_model(repo, "lx", 5_000)["wheel_branches"]
                  if item["direction"] == "put")
    assert branch["lifecycle_status"] == "pending_decision"
    window = repo.get_current_wheel_activation_window(market="us", account="lx")
    return repo, dict(account="lx", wheel_branch_id=branch["wheel_branch_id"],
        expected_batch_generation_hash=branch["batch_generation_hash"],
        request_id="branch-decision-once", actor="tester", market="us", as_of_ms=6_000,
        activation_descriptor=window, account_configured=True, policy_sha256="a" * 64)


@pytest.mark.parametrize("decision", ["start", "end"])
def test_branch_decision_preview_apply_and_retry_are_ledger_owned(tmp_path, monkeypatch, decision):
    repo, arguments = _branch_scope(tmp_path)
    assert workflows.decide_wheel_branch is api.decide_wheel_branch is wheel_commands.decide_wheel_branch
    assert wheel_commands._wheel_branch is api.resolve_wheel_branch_from_rows
    monkeypatch.setattr(workflows, "with_sqlite_repo_transaction", lambda *a, **k: pytest.fail("application transaction"), raising=False)
    assert not hasattr(workflows, "_wheel_branch")
    before = repo.read_lifecycle_account_rows(account="lx")
    preview = api.decide_wheel_branch(repo, **arguments, decision=decision)
    assert preview["dry_run"] and not preview["write_applied"]
    assert repo.read_lifecycle_account_rows(account="lx") == before
    applied = api.decide_wheel_branch(repo, **arguments, decision=decision, apply_changes=True)
    replay = api.decide_wheel_branch(repo, **arguments, decision=decision, apply_changes=True)
    assert applied["write_applied"] and replay["status"] == "idempotent" and not replay["write_applied"]
    assert preview["event_id"] == applied["event_id"] == replay["event_id"]
    assert len(repo.list_wheel_events(account="lx")) == len(before["account_wheel_events"]) + 1
    after = repo.read_lifecycle_account_rows(account="lx")
    assert after["trade_events"] == before["trade_events"]
    assert after["account_position_lots"] == before["account_position_lots"]


@pytest.mark.parametrize("decision", ["start", "end"])
@pytest.mark.parametrize("failure", ["append", "readback"])
def test_branch_decision_rolls_back_actual_append_and_retries(tmp_path, monkeypatch, decision, failure):
    repo, arguments = _branch_scope(tmp_path)
    before = repo.read_lifecycle_account_rows(account="lx")
    append = repo.append_wheel_event_once
    resolve = wheel_commands.resolve_wheel_branch_from_rows
    appended = []

    def append_then_fail(event, *, conn=None):
        assert conn is not None and conn.in_transaction
        created = append(event, conn=conn)
        assert created
        assert any(row["event_id"] == event["event_id"] for row in
                   repo.read_lifecycle_account_rows(account="lx", conn=conn)["account_wheel_events"])
        appended.append(event["event_id"])
        if failure == "append":
            raise RuntimeError("injected append failure")
        return created

    def bad_projection(*args, **kwargs):
        result = resolve(*args, **kwargs)
        return {**result, "lifecycle_status": "pending_decision"} if appended else result

    with monkeypatch.context() as faults:
        faults.setattr(repo, "append_wheel_event_once", append_then_fail)
        faults.setattr(wheel_commands, "resolve_wheel_branch_from_rows", bad_projection)
        with pytest.raises((RuntimeError, ValueError), match="injected append failure|projection verification failed"):
            api.decide_wheel_branch(repo, **arguments, decision=decision, apply_changes=True)
    assert len(appended) == 1
    assert repo.read_lifecycle_account_rows(account="lx") == before
    result = api.decide_wheel_branch(repo, **arguments, decision=decision, apply_changes=True)
    assert result["event_id"] == appended[0] and result["write_applied"]


def test_branch_start_rechecks_durable_activation_after_preview(tmp_path, monkeypatch):
    from unittest.mock import patch

    repo, arguments = _branch_scope(tmp_path)
    assert api.decide_wheel_branch(repo, **arguments, decision="start")["status"] == "planned"
    with patch("src.application.ledger.repository_assigned_stock.now_ms", return_value=5_500):
        with repo._writer_connection(begin_immediate=True) as conn:
            repo.close_wheel_activation_window(market="us", account="lx", expected_current_generation=1,
                policy_hash="a" * 64, request_id="deactivate-after-preview", request_hash="c" * 64, conn=conn)
    before = repo.read_lifecycle_account_rows(account="lx")
    get_window = repo.get_current_wheel_activation_window
    observations = []

    def locked_window(**kwargs):
        assert kwargs["conn"] is not None and kwargs["conn"].in_transaction
        window = get_window(**kwargs)
        observations.append(window)
        return window

    monkeypatch.setattr(repo, "get_current_wheel_activation_window", locked_window)
    with pytest.raises(ValueError, match="wheel_disabled"):
        api.decide_wheel_branch(repo, **arguments, decision="start", apply_changes=True)
    assert len(observations) == 1
    assert observations[0] is None or observations[0]["deactivated_at_ms"] == 5_500
    assert repo.read_lifecycle_account_rows(account="lx") == before
    # Ending an existing branch is still allowed after the activation window closes.
    result = api.decide_wheel_branch(repo, **arguments, decision="end", apply_changes=True)
    assert result["lifecycle_status_after"] == "manual_ended"


@pytest.mark.parametrize("field,value", [
    ("account", "sy"), ("market", "hk"), ("wheel_branch_id", "wrong"),
    ("expected_batch_generation_hash", "stale"),
])
def test_branch_decision_rejects_stale_or_foreign_inputs(tmp_path, field, value):
    repo, arguments = _branch_scope(tmp_path)
    before = repo.read_lifecycle_account_rows(account="lx")
    with pytest.raises(ValueError, match="resolve uniquely|generation changed"):
        api.decide_wheel_branch(repo, **{**arguments, field: value}, decision="start", apply_changes=True)
    assert repo.read_lifecycle_account_rows(account="lx") == before


def _intent_scope(tmp_path, monkeypatch):
    # Direction-specific setup uses qualified Call evidence and a deterministic
    # Put grant; capacity rules have separate real-fact consumer regressions.
    from tests.test_wheel_intent_policy_binding import _request, POLICY_A
    from src.application.opening_candidate_snapshot import strategy_policy_hash

    def prepare(direction):
        repo, branch, snapshot, capacity, resolved = _environment(tmp_path, direction, monkeypatch)
        request = _request(branch, snapshot, capacity, resolved)
        request["current_strategy_policy_sha256"] = strategy_policy_hash(POLICY_A)
        return repo, request
    return prepare


def _cancel_request(repo, created, original):
    branch = build_wheel_read_model(repo, "lx", 6_000, market="us")["wheel_branches"][0]
    return dict(
        account="lx", wheel_branch_id=branch["wheel_branch_id"], direction=branch["direction"],
        intent_id=created["intent_id"], expected_batch_generation_hash=branch["batch_generation_hash"],
        request_id="cancel-once", actor="tester", reason="operator cancelled",
        broker_order_inactive_confirmed=True, capacity_fact=original["capacity_fact"],
        market="us", as_of_ms=6_000,
    )


@pytest.mark.parametrize("direction", ["call", "put"])
@pytest.mark.parametrize("entry", ["api", "workflow"])
def test_intent_create_cancel_preview_replay_and_identity(tmp_path, monkeypatch, direction, entry):
    repo, request = _intent_scope(tmp_path, monkeypatch)(direction)
    facade = api if entry == "api" else workflows
    monkeypatch.setattr(workflows, "with_sqlite_repo_transaction", lambda *a, **k: pytest.fail("application transaction"), raising=False)
    before = repo.read_lifecycle_account_rows(account="lx")
    preview = facade.create_wheel_intent(repo, **request)
    assert preview["dry_run"] is True and preview["write_applied"] is False
    assert repo.read_lifecycle_account_rows(account="lx") == before
    created = facade.create_wheel_intent(repo, **request, apply_changes=True)
    replay = facade.create_wheel_intent(repo, **request, apply_changes=True)
    assert created["write_applied"] is True
    assert created["event_id"] == preview["event_id"] == replay["event_id"]
    assert replay["status"] == "idempotent" and replay["write_applied"] is False
    cancel = _cancel_request(repo, created, request)
    before_cancel = repo.read_lifecycle_account_rows(account="lx")
    planned = facade.cancel_wheel_intent(repo, **cancel)
    assert planned["dry_run"] is True and planned["write_applied"] is False
    assert repo.read_lifecycle_account_rows(account="lx") == before_cancel
    cancelled = facade.cancel_wheel_intent(repo, **cancel, apply_changes=True)
    replay = facade.cancel_wheel_intent(repo, **cancel, apply_changes=True)
    assert cancelled["write_applied"] is True
    assert cancelled["event_id"] == planned["event_id"] == replay["event_id"]
    assert replay["status"] == "idempotent" and replay["write_applied"] is False
    after = repo.read_lifecycle_account_rows(account="lx")
    assert len(after["account_wheel_events"]) == len(before["account_wheel_events"]) + 2
    assert after["trade_events"] == before["trade_events"]
    assert after["account_position_lots"] == before["account_position_lots"]
    for command, arguments in ((facade.create_wheel_intent, request), (facade.cancel_wheel_intent, cancel)):
        for changes in ({"actor": "someone-else"}, {"wheel_branch_id": "foreign-branch"}, {"market": "hk"}):
            with pytest.raises(ValueError):
                command(repo, **{**arguments, **changes}, apply_changes=True)
            assert repo.read_lifecycle_account_rows(account="lx") == after


@pytest.mark.parametrize("direction", ["call", "put"])
@pytest.mark.parametrize("operation", ["create", "cancel"])
@pytest.mark.parametrize("failure", ["append", "projection"])
def test_intent_commands_rollback_real_event_and_retry(tmp_path, monkeypatch, direction, operation, failure):
    repo, request = _intent_scope(tmp_path, monkeypatch)(direction)
    command = api.create_wheel_intent
    if operation == "cancel":
        created = command(repo, **request, apply_changes=True)
        request = _cancel_request(repo, created, request)
        command = api.cancel_wheel_intent
    before = repo.read_lifecycle_account_rows(account="lx")
    append, read = repo.append_wheel_event_once, repo.read_lifecycle_account_rows
    appended = []

    def append_then_fail(event, *, conn=None):
        assert conn is not None and conn.in_transaction
        created = append(event, conn=conn)
        assert created
        assert any(item["event_id"] == event["event_id"] for item in read(account="lx", conn=conn)["account_wheel_events"])
        appended.append(event["event_id"])
        if failure == "append":
            raise RuntimeError("injected append failure")
        return created

    def inconsistent_projection(*args, **kwargs):
        rows = read(*args, **kwargs)
        if appended:
            rows = deepcopy(rows)
            rows["account_wheel_events"] = [item for item in rows["account_wheel_events"] if item["event_id"] not in appended]
        return rows

    with monkeypatch.context() as faults:
        faults.setattr(repo, "append_wheel_event_once", append_then_fail)
        faults.setattr(repo, "read_lifecycle_account_rows", inconsistent_projection)
        with pytest.raises((RuntimeError, ValueError), match="injected append failure|verification failed"):
            command(repo, **request, apply_changes=True)
    assert len(appended) == 1
    assert repo.read_lifecycle_account_rows(account="lx") == before
    result = command(repo, **request, apply_changes=True)
    replay = command(repo, **request, apply_changes=True)
    assert result["write_applied"] is True and result["event_id"] == appended[0]
    assert replay["status"] == "idempotent" and replay["write_applied"] is False
    assert len(repo.list_wheel_events(account="lx")) == len(before["account_wheel_events"]) + 1


def test_neutral_call_intent_reads_branch_in_the_write_transaction(tmp_path, monkeypatch):
    repo, request = _intent_scope(tmp_path, monkeypatch)("call")
    original = repo.read_lifecycle_account_rows
    reads = []

    def require_transaction(*args, **kwargs):
        conn = kwargs.get("conn")
        assert conn is not None and conn.in_transaction
        reads.append(conn)
        return original(*args, **kwargs)

    with monkeypatch.context() as check:
        check.setattr(repo, "read_lifecycle_account_rows", require_transaction)
        created = api.create_wheel_intent(repo, **request, apply_changes=True)
        assert api.create_wheel_intent(repo, **request, apply_changes=True)["status"] == "idempotent"
    cancel = _cancel_request(repo, created, request)
    with monkeypatch.context() as check:
        check.setattr(repo, "read_lifecycle_account_rows", require_transaction)
        api.cancel_wheel_intent(repo, **cancel, apply_changes=True)
        assert api.cancel_wheel_intent(repo, **cancel, apply_changes=True)["status"] == "idempotent"
    assert len(reads) >= 6


def test_put_intent_uses_real_capacity_adapter_and_domain_allocation(tmp_path, monkeypatch):
    from domain.domain.portfolio_scope import portfolio_scope_id
    from src.application.ledger import wheel_capacity
    from tests.test_wheel_intent_policy_binding import _cash

    repo, request = _intent_scope(tmp_path, monkeypatch)("put")
    monkeypatch.setattr(wheel_commands, "revalidate_selected_wheel_put_candidate_from_rows",
                        wheel_capacity.revalidate_selected_wheel_put_candidate_from_rows)
    rows = repo.read_lifecycle_account_rows(account="lx")
    decision = api.decision_state_snapshot(repo, account="lx", portfolio_scope_id=portfolio_scope_id("lx"),
                                          current_decision_now_ms=5_000)
    portfolio = _cash()
    request = deepcopy(request)
    candidate = request["candidate_snapshot"]["batches"][0]["final_candidate"]
    candidate.pop("capacity_identity_hash")
    allocation = api.revalidate_selected_wheel_put_candidate_from_rows(
        account="lx", portfolio_context=portfolio, position_lots=repo.list_position_lots(),
        lifecycle_rows=rows, broker="futu", as_of_ms=5_000, fx_snapshot={},
        final_candidate=candidate, decision_snapshot=decision,
    )
    candidate["capacity_identity_hash"] = allocation["capacity_identity_hash"]
    assert allocation["cash_reservation_amount"] == 10_000
    before = repo.read_lifecycle_account_rows(account="lx")
    assert api.create_wheel_intent(repo, **request)["status"] == "planned"
    assert repo.read_lifecycle_account_rows(account="lx") == before
    insufficient = deepcopy(request)
    insufficient["capacity_fact"]["cash_evidence"]["cash_by_currency"] = {"USD": 9_000}
    with pytest.raises(ValueError, match="no longer has cash capacity"):
        api.create_wheel_intent(repo, **insufficient, apply_changes=True)
    assert repo.read_lifecycle_account_rows(account="lx") == before
    created = api.create_wheel_intent(repo, **request, apply_changes=True)
    assert created["write_applied"] is True
    event = next(row for row in repo.list_wheel_events(account="lx") if row["event_id"] == created["event_id"])
    assert event["payload"]["cash_reservation_amount"] == 10_000
    assert event["payload"]["capacity_identity_hash"] == allocation["capacity_identity_hash"]


def test_cash_capacity_application_entries_share_ledger_adapter():
    from src.application.ledger import wheel_capacity
    from src.application.wheel import capacity
    for name in ("broker_capacity_observation_is_fresh", "build_shared_cash_capacity_fact",
                 "revalidate_selected_wheel_put_candidate", "revalidate_selected_wheel_put_candidate_from_rows"):
        assert getattr(capacity, name) is getattr(api, name) is getattr(wheel_capacity, name)


def _activation_command_scope(tmp_path, action):
    repo = api.open_wheel_activation_repository(tmp_path / "activation.sqlite3")
    request = dict(action="enable", market="us", account="lx", expected_current_generation=0,
                   request_id="open-once", actor="tester", policy_sha256="a" * 64)
    if action == "disable":
        api.change_wheel_activation_window(repo, **request)
        request.update(action="disable", expected_current_generation=1, request_id="close-once")
    return repo, request


@pytest.mark.parametrize("action", ["enable", "disable"])
def test_activation_window_command_replay_and_scope(tmp_path, action):
    repo, request = _activation_command_scope(tmp_path, action)
    assert workflows._change_wheel_activation_window is api.change_wheel_activation_window is wheel_commands.change_wheel_activation_window
    applied = api.change_wheel_activation_window(repo, **request)
    replay = api.change_wheel_activation_window(repo, **request)
    assert applied["write_applied"] is True
    assert replay["idempotent"] is True and replay["write_applied"] is False
    assert replay["expected_config_descriptor"] == applied["expected_config_descriptor"]
    before = repo.list_wheel_activation_windows(market="us", account="lx")
    for changes in ({"actor": "other"}, {"policy_sha256": "b" * 64},
                    {"expected_current_generation": 7}, {"action": "unknown"}):
        with pytest.raises(ValueError):
            api.change_wheel_activation_window(repo, **{**request, **changes})
        assert repo.list_wheel_activation_windows(market="us", account="lx") == before
    assert repo.list_wheel_activation_windows(market="hk", account="lx") == []
    assert repo.list_wheel_activation_windows(market="us", account="sy") == []


@pytest.mark.parametrize("action", ["enable", "disable"])
@pytest.mark.parametrize("failure", ["write", "readback"])
def test_activation_window_actual_write_rolls_back_and_retries(tmp_path, monkeypatch, action, failure):
    repo, request = _activation_command_scope(tmp_path, action)
    before = repo.list_wheel_activation_windows(market="us", account="lx")
    method_name = "open_wheel_activation_window" if action == "enable" else "close_wheel_activation_window"
    method = getattr(repo, method_name)
    writes = []

    def write_then_fail(**kwargs):
        conn = kwargs["conn"]
        assert conn.in_transaction
        result = method(**kwargs)
        assert result["write_applied"] is True
        assert repo.list_wheel_activation_windows(market="us", account="lx", conn=conn) != before
        writes.append(result)
        if failure == "write":
            raise RuntimeError("injected activation write failure")
        return {**result, "window": {**result["window"], "generation": 999}}

    with monkeypatch.context() as fault:
        fault.setattr(repo, method_name, write_then_fail)
        with pytest.raises((ValueError, RuntimeError), match="readback verification failed|injected activation write failure"):
            api.change_wheel_activation_window(repo, **request)
    assert len(writes) == 1
    assert repo.list_wheel_activation_windows(market="us", account="lx") == before
    applied = api.change_wheel_activation_window(repo, **request)
    replay = api.change_wheel_activation_window(repo, **request)
    assert applied["write_applied"] is True
    assert replay["idempotent"] is True and replay["write_applied"] is False
    assert len(repo.list_wheel_activation_windows(market="us", account="lx")) == 1


def test_activation_request_superseded_by_policy_binding_is_rejected_in_ledger(tmp_path):
    from tests.test_wheel_policy_bindings import _append, _request
    repo, request = _activation_command_scope(tmp_path, "enable")
    api.change_wheel_activation_window(repo, **request)
    _append(repo, _request(repo))
    before = repo.list_wheel_policy_bindings(market="us", account="lx")
    with pytest.raises(ValueError, match="superseded by policy binding"):
        api.change_wheel_activation_window(repo, **request)
    assert repo.list_wheel_policy_bindings(market="us", account="lx") == before
    assert repo.get_current_wheel_activation_window(market="us", account="lx")["effective_policy_hash"] == "b" * 64
    closed = api.change_wheel_activation_window(repo, **{**request, "action": "disable",
        "request_id": "close-after-binding", "expected_current_generation": 1})
    assert closed["write_applied"] is True
    assert repo.get_current_wheel_activation_window(market="us", account="lx") is None


def _policy_binding_scope(tmp_path):
    from src.infrastructure.io_utils import file_identity
    from tests.test_wheel_policy_bindings import _request
    repo, activation = _activation_command_scope(tmp_path, "enable")
    api.change_wheel_activation_window(repo, **activation)
    request = _request(repo)
    for key, name in (("source", "config.yaml"), ("runtime", "config.us.json")):
        path = tmp_path / name
        path.write_text("{}")
        request[key] = file_identity(path, content=True)
    return repo, request


def _apply_binding(repo, *, request, expected_request_hash):
    result = api.record_wheel_policy_binding(
        repo, request=request, expected_preview_hash=expected_request_hash,
        read_configuration=lambda: {
            "source": request["source"], "runtime": request["runtime"],
            "target_policy_hash": request["target_policy_hash"], "descriptor": request["window"],
        },
    )
    return {**result, "write_applied": not result["idempotent"]}


def test_binding_command_concurrent_retry_has_one_durable_effect(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from domain.domain.decision_state_fingerprint import canonical_sha256
    repo, request = _policy_binding_scope(tmp_path)
    before = repo.list_wheel_activation_windows(market="us", account="lx")
    def apply(_):
        return _apply_binding(repo, request=request, expected_request_hash=canonical_sha256(request))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, range(2)))
    assert sum(result["write_applied"] for result in results) == 1
    assert sum(result["idempotent"] for result in results) == 1
    assert results[0]["receipt"] == results[1]["receipt"]
    assert len(repo.list_wheel_policy_bindings(market="us", account="lx")) == 1
    assert repo.list_wheel_activation_windows(market="us", account="lx") == before
    assert repo.list_wheel_policy_bindings(market="hk", account="lx") == []
    assert repo.list_wheel_policy_bindings(market="us", account="sy") == []


@pytest.mark.parametrize("failure", ["append", "readback", "config_after_write"])
def test_binding_command_rolls_back_written_receipt_and_retries(tmp_path, monkeypatch, failure):
    from domain.domain.decision_state_fingerprint import canonical_sha256
    repo, request = _policy_binding_scope(tmp_path)
    request_hash = canonical_sha256(request)
    before = repo.get_current_wheel_activation_window(market="us", account="lx")
    original = repo.append_wheel_policy_binding
    appended = []
    source = tmp_path / "config.yaml"
    original_bytes = source.read_bytes()

    def append_then_fail(**kwargs):
        assert kwargs["conn"].in_transaction
        receipt = original(**kwargs)
        assert len(repo.list_wheel_policy_bindings(market="us", account="lx", conn=kwargs["conn"])) == 1
        appended.append(receipt)
        if failure == "append":
            raise RuntimeError("injected binding append failure")
        if failure == "readback":
            return {**receipt, "revision": 999}
        source.write_text("changed after SQL write")
        return receipt

    with monkeypatch.context() as fault:
        fault.setattr(repo, "append_wheel_policy_binding", append_then_fail)
        with pytest.raises((RuntimeError, ValueError), match="injected binding append failure|readback verification failed|source identity changed"):
            _apply_binding(repo, request=request, expected_request_hash=request_hash)
    assert len(appended) == 1
    assert repo.list_wheel_policy_bindings(market="us", account="lx") == []
    assert repo.get_current_wheel_activation_window(market="us", account="lx") == before
    source.write_bytes(original_bytes)
    applied = _apply_binding(repo, request=request, expected_request_hash=request_hash)
    replay = _apply_binding(repo, request=request, expected_request_hash=request_hash)
    assert applied["write_applied"] is True
    assert replay["write_applied"] is False and replay["idempotent"] is True
    assert applied["receipt"] == replay["receipt"]


@pytest.mark.parametrize("key", ["source", "runtime"])
def test_binding_rechecks_identical_file_replacement_at_transaction_entry(tmp_path, monkeypatch, key):
    from pathlib import Path
    from domain.domain.decision_state_fingerprint import canonical_sha256
    repo, request = _policy_binding_scope(tmp_path)
    original = wheel_policy_binding.with_sqlite_repo_transaction
    path = Path(request[key]["path"])
    before = path.read_bytes()

    def replace_before_lock(active_repo, callback):
        replacement = tmp_path / "replacement"
        replacement.write_bytes(before)
        replacement.replace(path)
        return original(active_repo, callback)

    monkeypatch.setattr(wheel_policy_binding, "with_sqlite_repo_transaction", replace_before_lock)
    with pytest.raises(ValueError, match=f"{key} identity changed"):
        _apply_binding(repo, request=request, expected_request_hash=canonical_sha256(request))
    assert path.read_bytes() == before
    assert repo.list_wheel_policy_bindings(market="us", account="lx") == []


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_manual_end_rolls_back_an_appended_event_on_failure(tmp_path, monkeypatch, failure):
    repo, lot_id = _wheel_repo(tmp_path)
    batch = build_wheel_read_model(repo, "lx", 3000, market="us")["batches"][0]
    before = repo.list_wheel_events(account="lx")
    original = wheel_commands.resolve_wheel_batch_from_rows
    calls = 0

    def fail_readback(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise failure("projection interrupted after append")
        return original(*args, **kwargs)

    args = dict(account="lx", lot_id=lot_id, market="us", actor="tester",
                request_id="rollback-manual-end", as_of_ms=4000,
                expected_batch_generation_hash=batch["batch_generation_hash"])
    with monkeypatch.context() as patch:
        patch.setattr(wheel_commands, "resolve_wheel_batch_from_rows", fail_readback)
        with pytest.raises(failure, match="projection interrupted"):
            api.end_wheel_lifecycle(repo, **args, apply_changes=True)
    assert calls == 2
    assert repo.list_wheel_events(account="lx") == before
    assert build_wheel_read_model(repo, "lx", 4000, market="us")["batches"][0]["lifecycle_status"] == "active"
    applied = api.end_wheel_lifecycle(repo, **args, apply_changes=True)
    repeated = api.end_wheel_lifecycle(repo, **args, apply_changes=True)
    assert applied["write_applied"] is True
    assert repeated["idempotent"] is True and repeated["write_applied"] is False
    assert len(repo.list_wheel_events(account="lx")) == len(before) + 1
