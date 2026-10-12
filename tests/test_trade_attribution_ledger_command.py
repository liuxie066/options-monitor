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

    monkeypatch.setattr(attribution, "with_sqlite_repo_transaction", forbidden)
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
