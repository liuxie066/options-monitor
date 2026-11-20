"""Context assembly reuses domain calculations without acquiring new facts."""
from copy import deepcopy
from datetime import datetime, timezone
import sqlite3

import pytest

from domain.domain import lifecycle_timing, option_lifecycle
from domain.domain.portfolio_scope import portfolio_scope_id
from src.application.ledger import api, lifecycle_settlement_semantics, position_context
from src.application.positions import context_builder
from src.application.trades import close_reason_evidence, lifecycle_reconciliation
from tests.test_wheel_workflows import _wheel_repo, _open_unlinked_call


def test_context_and_lifecycle_compatibility_entries_share_owners():
    for old, new in (
        ("build_context", "build_position_risk_context"),
        ("build_shared_context", "build_shared_position_risk_context"),
        ("slice_shared_context_for_account", "slice_position_risk_context_for_account"),
        ("validate_option_positions_context_account", "validate_option_positions_context_account"),
        ("build_lifecycle_read_models_from_decision_snapshot", "build_lifecycle_read_models_from_decision_snapshot"),
    ):
        assert getattr(context_builder, old) is getattr(api, new) is getattr(position_context, new)
    for name in (
        "LifecycleCaseDataError",
        "build_lifecycle_read_models_from_resolved_account",
        "build_lifecycle_case_read_model_from_resolved_facts",
    ):
        assert getattr(lifecycle_reconciliation, name) is getattr(option_lifecycle, name)
    assert close_reason_evidence.canonical_hash is lifecycle_timing.canonical_hash
    assert lifecycle_settlement_semantics.canonical_hash is lifecycle_timing.canonical_hash
    assert close_reason_evidence.derive_effective_lifecycle_timing is lifecycle_timing.derive_effective_lifecycle_timing


def test_qualified_context_assembly_needs_no_database_or_provider(tmp_path, monkeypatch):
    repo, _ = _wheel_repo(tmp_path)
    _open_unlinked_call(repo)
    snapshot = api.decision_state_snapshot(repo, account="lx", portfolio_scope_id=portfolio_scope_id("lx"))
    assert snapshot["snapshot_status"] == "trusted"
    before = deepcopy(snapshot)
    ledger_before = repo.read_lifecycle_account_rows(account="lx")

    def unexpected_io(*args, **kwargs):
        pytest.fail("context assembly acquired new facts")

    with monkeypatch.context() as blocked:
        blocked.setattr(sqlite3, "connect", unexpected_io)
        blocked.setattr(context_builder, "resolve_position_lot_snapshots", unexpected_io)
        blocked.setattr(context_builder, "get_exchange_rates_or_fetch_latest", unexpected_io)
        result = api.build_position_risk_context(
            [], broker="富途", account="lx", rates={"USDCNY": 7.2},
            decision_snapshot=snapshot, lifecycle_now_ms=5_000,
            observed_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
    assert result["locked_shares_by_symbol"]["NVDA"] == 100
    assert result["decision_state_fingerprint"] == snapshot["decision_state_fingerprint"]
    assert snapshot == before
    assert repo.read_lifecycle_account_rows(account="lx") == ledger_before


def test_lifecycle_receipt_hash_keeps_persisted_json_identity():
    # Receipt identities preserve raw JSON distinctions (including 1 versus 1.0).
    assert lifecycle_timing.canonical_hash({"b": "中文", "a": 1}) == "db1e1d174330db0f00974178407b16d090326f28edd3798338fe91c275dc5466"
    assert lifecycle_timing.canonical_hash({"a": 1}) != lifecycle_timing.canonical_hash({"a": 1.0})


def test_wheel_capacity_query_reuses_one_read_only_snapshot(tmp_path, monkeypatch):
    import fcntl
    from src.application.ledger import decision_snapshot, read_only_evidence, wheel_capacity
    from src.application.ledger.assigned_stock_projection import build_wheel_read_model_from_rows
    from src.application.wheel import capacity

    repo, _ = _wheel_repo(tmp_path)
    _open_unlinked_call(repo)
    before = repo.read_lifecycle_account_rows(account="lx")
    rows_seen = []
    original_read = read_only_evidence._ReadOnlyPositionQueryRepository.read_lifecycle_account_rows
    original_constraints = decision_snapshot._read_account_trade_source_constraints

    def read_once(reader, **kwargs):
        # A write is rejected even if a future read codec accidentally issues one.
        conn = reader._connect()
        try:
            assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                conn.execute("CREATE TABLE accidental_write (id INTEGER)")
        finally:
            conn.close()
        rows = original_read(reader, **kwargs)
        rows_seen.append(deepcopy(rows))
        return rows

    def constraints_under_intake_lock(*args, **kwargs):
        # The ledger snapshot and sidecar evidence must remain under the same
        # real cross-process lock, not merely a mocked context manager.
        with open(str(repo.db_path) + ".writer.lock", "rb") as handle:
            with pytest.raises(BlockingIOError):
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return original_constraints(*args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("capacity query acquired a writable repository or provider")

    monkeypatch.setattr(read_only_evidence._ReadOnlyPositionQueryRepository,
                        "read_lifecycle_account_rows", read_once)
    monkeypatch.setattr(decision_snapshot, "_read_account_trade_source_constraints", constraints_under_intake_lock)
    with monkeypatch.context() as blocked:
        blocked.setattr(repo, "_connect", forbidden)
        blocked.setattr(repo, "read_lifecycle_account_rows", forbidden)
        blocked.setattr(capacity, "fetch_futu_portfolio_context", forbidden)
        model, context = api.read_wheel_capacity_context(repo, account="lx", broker="富途", as_of_ms=5_000)
    assert api.read_wheel_capacity_context is wheel_capacity.read_wheel_capacity_context
    assert len(rows_seen) == 1
    assert rows_seen[0] == before
    assert model == build_wheel_read_model_from_rows(before, account="lx", as_of_ms=5_000)
    assert context["decision_snapshot_status"] == "trusted"
    assert context["locked_shares_by_symbol"]["NVDA"] == 100
    assert repo.read_lifecycle_account_rows(account="lx") == before


def test_wheel_capacity_query_distinguishes_missing_ledger_from_empty_account(tmp_path):
    from types import SimpleNamespace
    from src.application.ledger.repository import SQLiteOptionPositionsRepository

    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        api.read_wheel_capacity_context(SimpleNamespace(db_path=missing), account="lx", broker="富途", as_of_ms=5_000)
    assert not missing.exists()
    repo = SQLiteOptionPositionsRepository(tmp_path / "empty.sqlite3")
    model, context = api.read_wheel_capacity_context(repo, account="lx", broker="富途", as_of_ms=5_000)
    assert model["wheel_branches"] == []
    assert context["decision_snapshot_status"] == "trusted"
    assert context["locked_shares_by_symbol"] == {}


def test_wheel_capacity_query_keeps_accounts_isolated(tmp_path):
    repo, _ = _wheel_repo(tmp_path)
    _open_unlinked_call(repo)
    model, context = api.read_wheel_capacity_context(repo, account="sy", broker="富途", as_of_ms=5_000)
    assert model["wheel_branches"] == []
    assert context["decision_snapshot_status"] == "trusted"
    assert context["locked_shares_by_symbol"] == {}


def test_wheel_capacity_missing_source_evidence_is_unavailable_not_zero(tmp_path, monkeypatch):
    from src.application.wheel import capacity

    repo, _ = _wheel_repo(tmp_path)
    inbox = repo.db_path.with_name(repo.db_path.name + ".trade_intake_inbox.sqlite3")
    with sqlite3.connect(inbox) as conn:
        conn.execute("CREATE TABLE incomplete_schema (id INTEGER)")
    monkeypatch.setattr(capacity, "fetch_futu_portfolio_context", lambda **kwargs: pytest.fail("invalid ledger triggered provider"))
    fact = capacity.load_shared_cash_capacity_fact(
        repo, config={}, runtime_root=tmp_path, account="lx", broker="富途", as_of_ms=5_000, fx_snapshot={},
    )
    assert fact["status"] == "unavailable"
    assert "trade source inbox schema unavailable" in fact["reason"]
    assert fact["cash_by_currency"] is None
    assert fact["cash_secured_by_currency"] is None


def test_wheel_provider_observation_reuses_already_frozen_context(tmp_path, monkeypatch):
    from src.application.wheel import capacity

    repo, _ = _wheel_repo(tmp_path)
    original_query = api.read_wheel_capacity_context
    captured = []

    def query(*args, **kwargs):
        result = original_query(*args, **kwargs)
        captured.append(result)
        return result

    def provider(**kwargs):
        assert len(captured) == 1
        assert kwargs["write_cache"] is False
        # A ledger change during network I/O cannot splice into the frozen pair.
        _open_unlinked_call(repo)
        return {"observation": "provider"}

    monkeypatch.setattr(capacity, "read_wheel_capacity_context", query)
    monkeypatch.setattr(capacity, "load_account_portfolio_context", provider)
    model, portfolio, context = capacity._load_frozen_wheel_capacity_context(
        repo, config={}, account="lx", broker="富途", as_of_ms=5_000, runtime_root=tmp_path,
    )
    assert model is captured[0][0] and context is captured[0][1]
    assert portfolio == {"observation": "provider"}
    assert context["locked_shares_by_symbol"] == {}
    _, refreshed = original_query(repo, account="lx", broker="富途", as_of_ms=5_000)
    assert refreshed["locked_shares_by_symbol"]["NVDA"] == 100
    assert refreshed["decision_state_fingerprint"] != context["decision_state_fingerprint"]
