from __future__ import annotations

import ast
from pathlib import Path
import subprocess
import sys
from threading import Event

import pytest

from src.application.ledger import api
from src.application.trades import attribution
from test_trade_attribution_view import _writable_call_scope


ROOT = Path(__file__).resolve().parents[1]


def _command_scope(tmp_path, monkeypatch):
    repo, config = _writable_call_scope(tmp_path, monkeypatch)
    evidence = {"complete": True, "exposures": []}
    before = api.read_trade_attribution_snapshot(repo, account="lx", market="us")
    view = api.build_trade_attribution_view(before, config=config, account="lx", market="us",
                                           now_ms=4000, combo_evidence=evidence)
    fact = next(row for row in view["rows"] if row["selected_candidate_id"])
    args = dict(account="lx", market="us", config=config, execution_key=fact["execution_key"],
                candidate_id=fact["selected_candidate_id"], expected_input_hash=fact["input_hash"],
                request_id="ledger-command:1", actor="test:ledger-command", combo_evidence=evidence,
                capacity_observation={}, combo_mode="confirm")
    return repo, args, before


def test_public_command_owns_preview_apply_and_idempotent_readback(tmp_path, monkeypatch):
    repo, args, before = _command_scope(tmp_path, monkeypatch)

    def forbidden(*args, **kwargs):
        pytest.fail("ledger command called application transaction or external observation")

    monkeypatch.setattr(attribution, "with_sqlite_repo_transaction", forbidden, raising=False)
    monkeypatch.setattr(attribution, "read_attribution_combo_evidence", forbidden)
    monkeypatch.setattr("src.application.wheel.capacity.observe_trade_attribution_capacity", forbidden)
    preview = api.apply_trade_attribution(repo, **args, apply_changes=False)
    assert preview["write_applied"] is False and preview["members"]
    assert api.read_trade_attribution_snapshot(repo, account="lx", market="us") == before
    applied = api.apply_trade_attribution(repo, **args)
    assert applied["write_applied"] is True and applied["status"] == "linked"
    committed = api.read_trade_attribution_snapshot(repo, account="lx", market="us")
    assert len(committed["trade_events"]) == len(before["trade_events"]) + 1
    # A replay needs only its durable proof, even when provider evidence is unavailable.
    replay = api.apply_trade_attribution(repo, **{**args, "capacity_observation": {"error": "timeout"}})
    assert replay["write_applied"] is False
    assert replay["proof_event_ids"] == applied["proof_event_ids"]
    assert api.read_trade_attribution_snapshot(repo, account="lx", market="us") == committed


@pytest.mark.parametrize("failure", ["cancel", "error"])
def test_public_command_rolls_back_failure_after_projection(tmp_path, monkeypatch, failure):
    repo, args, before = _command_scope(tmp_path, monkeypatch)
    stop = Event()
    calls = []

    def before_commit():
        calls.append(1)
        if len(calls) == 2:
            assert len(repo.list_trade_events()) == len(before["trade_events"])
            raise ValueError("application authority expired")
        if failure == "cancel":
            stop.set()

    with pytest.raises(ValueError, match="cancelled|authority expired"):
        api.apply_trade_attribution(repo, **args, stop_event=stop, before_commit=before_commit)
    assert len(calls) == (1 if failure == "cancel" else 2)
    assert api.read_trade_attribution_snapshot(repo, account="lx", market="us") == before


def test_attribution_entrypoints_share_the_ledger_owner():
    assert attribution.apply_trade_attribution is api.apply_trade_attribution
    assert attribution.build_trade_attribution_view is api.build_trade_attribution_view
    assert api.apply_trade_attribution.__module__ == "src.application.ledger.trade_attribution"
    assert api.build_trade_attribution_view.__module__ == "src.application.ledger.trade_attribution"
    from src.application.wheel import capacity, read_model
    from src.application import account_config
    from src.application.trades import account_mapping
    assert capacity.trade_attribution_capacity_check is api.trade_attribution_capacity_check
    assert read_model.build_wheel_read_model_from_rows is api.build_wheel_read_model_from_rows
    assert read_model.build_wheel_read_model_with_capacity_from_rows is api.build_wheel_read_model_with_capacity_from_rows
    assert account_mapping.combo_reconciliation_mode_for_account is account_config.combo_reconciliation_mode_for_account
    tree = ast.parse((ROOT / "src/application/trades/attribution.py").read_text())
    assert not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name in {"apply_trade_attribution", "build_trade_attribution_view"}
                   for node in ast.walk(tree))


@pytest.mark.parametrize("first", [
    "src.application.ledger.api", "src.application.ledger.trade_attribution",
    "src.application.ledger.assigned_stock_projection", "src.application.wheel.capacity", "src.application.wheel.read_model",
    "src.application.trades.attribution", "src.application.bot.control.attribution_operations",
    "src.interfaces.cli.option_positions", "src.interfaces.cli.wheel",
])
def test_attribution_public_import_order(first):
    script = f"""
import importlib
importlib.import_module({first!r})
from src.application.ledger import api
from src.application.trades import attribution
assert attribution.apply_trade_attribution is api.apply_trade_attribution
assert attribution.build_trade_attribution_view is api.build_trade_attribution_view
"""
    result = subprocess.run([sys.executable, "-B", "-c", script], cwd=ROOT,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_all_capacity_entrypoints_delegate_qualified_facts_to_domain(monkeypatch):
    from domain.domain import risk_capacity
    from src.application.wheel import capacity
    from test_trade_attribution_view import _call_capacity_observation

    observation = _call_capacity_observation()
    fact = {"account": "lx", "broker_account_ref": {"broker_id": "futu",
            "external_account_id": "1001", "environment": "REAL"},
            "contract_key": {"underlying_symbol": "NVDA", "option_type": "call"}}
    verdict = {"status": "unavailable", "reason_codes": ["fixture_domain_verdict"], "evidence_complete": False}
    calls = []

    def evaluate(**kwargs):
        calls.append(kwargs)
        return verdict

    monkeypatch.setattr(risk_capacity, "evaluate_booked_option_capacity", evaluate)
    args = dict(fact=fact, facts=[fact], config={}, observation=observation,
                wheel_read_model={}, now_ms=4000)
    for entry in (api.trade_attribution_capacity_check, capacity.trade_attribution_capacity_check):
        assert entry(**args) is verdict
    assert len(calls) == 2
    assert all(call["facts"] is args["facts"] for call in calls)
    # Qualification still happens before the pure calculation, on every use.
    for entry in (api.trade_attribution_capacity_check, capacity.trade_attribution_capacity_check):
        result = entry(**{**args, "now_ms": 64001})
        assert result["evidence_complete"] is False
        assert "snapshot_observed_at_utc_stale_or_future" in result["reason_codes"]
    assert len(calls) == 2


def _conflict_scope(tmp_path, monkeypatch, *, all_rules_enabled=True):
    from test_attribution_capacity_evidence import _fully_linked, _observation, _view, EVIDENCE

    repo, config, _, _ = _fully_linked(tmp_path, monkeypatch, all_rules_enabled=all_rules_enabled)
    observation = _observation(repo)
    observation["portfolio"]["position_snapshot_input"]["rows"][0]["quantity"] = "0"
    view = _view(repo, config, observation=observation)
    current = next(row for row in view["rows"] if len(row.get("wheel_call_allocations") or []) > 1)
    assert current["status"] == "conflict"
    return repo, dict(account="lx", market="hk", config=config, execution_key=current["execution_key"],
                     expected_input_hash=current["input_hash"], combo_evidence=EVIDENCE,
                     capacity_observation=observation, combo_mode="confirm")


def test_conflict_command_owns_writes_and_never_calls_application_transaction(tmp_path, monkeypatch):
    from domain.domain.strategy_membership import trade_attribution_conflict_execution_keys

    repo, args = _conflict_scope(tmp_path, monkeypatch)
    before = api.read_trade_attribution_snapshot(repo, account="lx", market="hk")

    def forbidden(*args, **kwargs):
        pytest.fail("ledger conflict command escaped into the application")

    for name in ("with_sqlite_repo_transaction", "record_trade_attribution_conflict",
                 "build_trade_attribution_view", "read_attribution_combo_evidence"):
        monkeypatch.setattr(attribution, name, forbidden, raising=False)
    monkeypatch.setattr("src.application.wheel.capacity.observe_trade_attribution_capacity", forbidden)
    assert attribution.record_trade_attribution_conflicts is api.record_trade_attribution_conflicts
    assert attribution._conflict_execution_keys is trade_attribution_conflict_execution_keys
    events = api.record_trade_attribution_conflicts(repo, **args)
    after = api.read_trade_attribution_snapshot(repo, account="lx", market="hk")
    assert len(events) > 1 and len(events) == len(set(events))
    appended = [row for row in after["account_wheel_events"] if row["event_id"] in events]
    assert len(appended) == len(events)
    assert all(row["event_type"] == "wheel_attribution_conflict" for row in appended)
    assert after["trade_events"] == before["trade_events"]
    # A stale retry cannot append a second set of conflict receipts.
    with pytest.raises(ValueError, match="conflict evidence changed"):
        api.record_trade_attribution_conflicts(repo, **args)
    assert api.read_trade_attribution_snapshot(repo, account="lx", market="hk") == after


@pytest.mark.parametrize("failure", ["stale", "cancel", "second_append"])
def test_conflict_command_rolls_back_all_members(tmp_path, monkeypatch, failure):
    from src.application.ledger import trade_attribution as owner

    repo, args = _conflict_scope(tmp_path, monkeypatch)
    before = api.read_trade_attribution_snapshot(repo, account="lx", market="hk")
    writes = []
    if failure == "stale":
        args["expected_input_hash"] = "0" * 64
    elif failure == "cancel":
        stop = Event()
        stop.set()
        args["stop_event"] = stop
    else:
        original = owner.record_trade_attribution_conflict

        def fail_second(*a, **kw):
            writes.append(original(*a, **kw))
            if len(writes) == 2:
                raise RuntimeError("second append failed")
            return writes[-1]

        monkeypatch.setattr(owner, "record_trade_attribution_conflict", fail_second)
    with pytest.raises((ValueError, InterruptedError, RuntimeError)):
        api.record_trade_attribution_conflicts(repo, **args)
    if failure == "second_append":
        assert len(writes) == 2  # Provisional events really reached SQLite.
    assert api.read_trade_attribution_snapshot(repo, account="lx", market="hk") == before


def test_conflict_command_rechecks_policy_before_writing(tmp_path, monkeypatch):
    repo, args = _conflict_scope(tmp_path, monkeypatch, all_rules_enabled=False)
    before = api.read_trade_attribution_snapshot(repo, account="lx", market="hk")
    with pytest.raises(ValueError, match="conflict evidence changed"):
        api.record_trade_attribution_conflicts(repo, **args)
    assert api.read_trade_attribution_snapshot(repo, account="lx", market="hk") == before
