from __future__ import annotations

import pytest

from src.application.ledger.api import decision_state_snapshot
from src.application.ledger.position_projection_runtime import run_position_projection_forced_full
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.positions.context_builder import build_context


@pytest.fixture
def context_inputs(tmp_path, monkeypatch):
    import src.application.pipeline_context as pc

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    run_position_projection_forced_full(repo, [])
    snapshot = decision_state_snapshot(repo, account="lx", portfolio_scope_id="test-lx")
    assert snapshot["snapshot_status"] == "trusted"
    option_context = build_context([], broker="富途", account="lx", decision_snapshot=snapshot)
    monkeypatch.setattr(pc, "open_position_ledger", lambda *_a, **_k: repo)
    monkeypatch.setattr(pc, "is_fresh", lambda *_a, **_k: True)
    logs = []
    return dict(
        base=tmp_path, data_config=str(tmp_path / "fixture.json"), market="富途", account="lx",
        ttl_sec=3600, state_dir=tmp_path / "state", shared_state_dir=tmp_path / "shared",
        log=logs.append,
    ), option_context, logs


def test_load_portfolio_context_rejects_invalid_cached_contract(context_inputs, monkeypatch):
    import src.application.pipeline_context as pc

    args, _, logs = context_inputs
    monkeypatch.setattr(pc, "load_cached_json", lambda *_a, **_k: {
        "as_of_utc": "2026-04-12T00:00:00+00:00",
        "stocks_by_symbol": [], "cash_by_currency": {},
    })
    out = pc.load_portfolio_context(**args)
    assert out is None
    assert any("portfolio context not available" in x for x in logs)


def test_load_option_positions_context_rejects_invalid_cached_contract(context_inputs, monkeypatch):
    import src.application.pipeline_context as pc

    args, context, logs = context_inputs
    monkeypatch.setattr(pc, "load_cached_json", lambda *_a, **_k: {
        **context, "locked_shares_by_symbol": [],
    })
    out, refreshed = pc.load_option_positions_context(**args)
    assert out is None
    assert refreshed is False
    assert any("option positions context not available" in x for x in logs)


def test_load_context_persists_source_snapshots_for_valid_cached_contracts(context_inputs, monkeypatch):
    import src.application.pipeline_context as pc

    args, option_context, _ = context_inputs
    captured = []
    monkeypatch.setattr(pc.state_repo, "append_source_snapshot_event", lambda _base, payload, **_k: captured.append(payload))

    def load_cached(path):
        if path.name == "portfolio_context.json":
            return {
                "as_of_utc": "2026-04-12T00:00:00+00:00",
                "stocks_by_symbol": {"AAPL": {"shares": 100}},
                "cash_by_currency": {"USD": 100.0},
                "portfolio_source_name": "futu", "filters": {"account": "lx"},
            }
        return option_context

    monkeypatch.setattr(pc, "load_cached_json", load_cached)
    pctx = pc.load_portfolio_context(**args)
    octx, refreshed = pc.load_option_positions_context(**args)
    assert pctx is not None
    assert octx is not None
    assert refreshed is False
    assert {str(x.get("source_name")) for x in captured} == {"holdings", "option_positions"}
