from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from cash_evidence_helpers import cash_config, cash_portfolio

from domain.domain.ledger import ContractKey, TradeEvent
from src.application.ledger.api import decision_state_snapshot, list_position_lot_snapshots
from src.application.ledger.position_projection_runtime import run_position_projection_forced_full
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.positions.context_builder import build_context


def _ledger(tmp_path, multiplier=500):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    event = TradeEvent(
        event_id="open", event_type="open", event_time_ms=1000,
        contract_key=ContractKey.from_values(
            broker="futu", account="lx", underlying_symbol="NVDA", option_type="put",
            strike=100, expiration_ymd="2027-06-18",
        ),
        contracts=1, price=1.5, currency="USD", source="test", multiplier=multiplier,
        lot_id="lot-a", raw_payload={"side": "sell", "strategy": "yield_enhancement", "leg_role": "sell_put"},
    )
    run_position_projection_forced_full(repo, [event])
    return repo


def _break_history(repo, *, clear_lots=False):
    with repo._connect() as conn:
        payload = json.loads(conn.execute("SELECT event_json FROM trade_events WHERE event_id='open'").fetchone()[0])
        payload.pop("multiplier")
        conn.execute("UPDATE trade_events SET event_json=? WHERE event_id='open'", (json.dumps(payload),))
        if clear_lots:
            conn.execute("DELETE FROM position_lots")
        conn.commit()


@pytest.mark.parametrize("clear_lots", [False, True])
def test_bad_history_blocks_context_and_keeps_raw_inspection(tmp_path, clear_lots):
    from src.application.positions.inspection import inspect_projection_state

    repo = _ledger(tmp_path)
    _break_history(repo, clear_lots=clear_lots)
    rows = list(list_position_lot_snapshots(repo))
    snapshot = decision_state_snapshot(repo, account="lx", portfolio_scope_id="test-lx")
    assert snapshot["snapshot_status"] != "trusted"
    context = build_context(rows, broker="futu", account="lx", decision_snapshot=snapshot)
    assert context["context_status"] == "unavailable"
    assert context["locked_shares_status"] == "unavailable"
    assert context["cash_secured_total_by_ccy"] == {}
    assert context["open_positions_min"] == []
    inspection = inspect_projection_state(repo, base=tmp_path, account="lx")
    assert inspection["all_projection_diagnostic_count"] > 0
    assert bool(inspection["current_lots"]) is not clear_lots


def test_context_uses_lots_bound_to_trusted_snapshot(tmp_path):
    repo = _ledger(tmp_path)
    snapshot = decision_state_snapshot(repo, account="lx", portfolio_scope_id="test-lx")
    assert snapshot["snapshot_status"] == "trusted"
    context = build_context([], broker="futu", account="lx", decision_snapshot=snapshot)
    assert context["cash_secured_total_by_ccy"] == {"USD": 50000.0}
    assert len(context["open_positions_min"]) == 1
    assert context["open_positions_min"][0]["strategy"] == "yield_enhancement"


@pytest.mark.parametrize("clear_lots", [False, True])
def test_pipeline_checks_requested_account_even_without_lots(tmp_path, monkeypatch, clear_lots):
    from src.application import pipeline_context as mod

    repo = _ledger(tmp_path)
    _break_history(repo, clear_lots=clear_lots)
    monkeypatch.setattr(mod, "open_position_ledger", lambda *_a, **_k: repo)
    monkeypatch.setattr(mod, "_persist_source_snapshot", lambda *_a: None)
    context, refreshed = mod.load_option_positions_context(
        base=tmp_path, data_config="fixture.json", market="futu", account="lx", ttl_sec=0,
        state_dir=tmp_path / "state", shared_state_dir=tmp_path / "shared", log=lambda _m: None,
        exchange_rate_observation={},
    )
    assert refreshed
    assert context["context_status"] == "unavailable"
    assert context["context_source"] == "shared_refresh"
    shared = json.loads((tmp_path / "shared" / "option_positions_context.shared.json").read_text())
    assert shared["by_account"]["lx"]["context_status"] == "unavailable"
    assert shared["all_accounts"]["context_status"] == "unavailable"
    snapshots = mod._decision_snapshots_for_records(repo, [], accounts=("lx", "sy"))
    assert set(snapshots) == {"lx", "sy"}
    assert all(s["snapshot_status"] != "trusted" for s in snapshots.values())


@pytest.mark.parametrize("cache_source", ["account", "shared"])
def test_pipeline_rejects_ttl_cache_when_history_invalid(tmp_path, monkeypatch, cache_source):
    from src.application import pipeline_context as mod

    repo = _ledger(tmp_path)
    monkeypatch.setattr(mod, "open_position_ledger", lambda *_a, **_k: repo)
    monkeypatch.setattr(mod, "_persist_source_snapshot", lambda *_a: None)
    args = dict(
        base=tmp_path, data_config="fixture.json", market="futu", account="lx", ttl_sec=3600,
        state_dir=tmp_path / "state", shared_state_dir=tmp_path / "shared", log=lambda _m: None,
        exchange_rate_observation={},
    )
    first, _ = mod.load_option_positions_context(**args)
    assert first["decision_snapshot_status"] == "trusted"
    if cache_source == "shared":
        (tmp_path / "state" / "option_positions_context.json").unlink()
    _break_history(repo)
    context, refreshed = mod.load_option_positions_context(**args)
    assert refreshed
    assert context["context_status"] == "unavailable"
    assert context["locked_shares_status"] == "unavailable"
    assert context["open_positions_min"] == []


@pytest.mark.parametrize("clear_lots", [False, True])
def test_cash_query_refuses_bad_history_with_fresh_broker_cash(tmp_path, monkeypatch, clear_lots):
    from src.application import cash_headroom_query as mod

    repo = _ledger(tmp_path)
    _break_history(repo, clear_lots=clear_lots)
    monkeypatch.setattr(mod, "open_position_ledger", lambda *_a, **_k: repo)
    portfolio = cash_portfolio({
        "portfolio_source_name": "futu", "cash_by_currency": {"USD": 100000},
        "source_observed_at": datetime.now(timezone.utc).isoformat(),
        "source_observation_status": "trusted", "cash_balance_reliable": True,
    })
    assert mod.cash_snapshot_is_usable(portfolio)
    monkeypatch.setattr(mod, "load_account_portfolio_context", lambda **_k: portfolio)
    payload = mod.query_sell_put_cash(
        base_dir=tmp_path, data_config=tmp_path / "fixture.json", account="lx", market="futu",
        no_exchange_rates=True, output_format="json", write_cache=False, runtime_config=cash_config(),
    )
    assert payload["cash_secured_usage_reliable"] is False
    assert payload["cash_secured_unavailable_reason"] == "option_decision_snapshot_unavailable"
    assert payload["cash_free_usd"] is None
    assert payload["cash_secured_used_usd"] is None


@pytest.mark.parametrize("clear_lots", [False, True])
def test_close_advice_barrier_blocks_bad_history_even_without_lots(tmp_path, monkeypatch, clear_lots):
    from src.application import tick_account_execution as mod

    repo = _ledger(tmp_path)
    _break_history(repo, clear_lots=clear_lots)
    monkeypatch.setattr(mod, "resolve_position_data_config_path", lambda **_k: tmp_path / "fixture.json")
    monkeypatch.setattr(mod, "open_position_ledger_from_data_config", lambda **_k: (tmp_path / "fixture.json", repo))
    request = SimpleNamespace(base=tmp_path, cfg_path=tmp_path / "config.json", run_id="test", base_cfg={}, markets_to_run=["US"])
    _, path = mod._build_close_advice_barrier_plan(
        request=request, scanning_configs={"lx": {"close_advice": {"enabled": True}}},
        candidate_config={}, run_state_dir=tmp_path / "state",
        run_started_at_utc=datetime.now(timezone.utc),
    )
    plan = json.loads(path.read_text())
    account = plan["accounts"]["lx"]
    assert account["status"] == "unavailable"
    assert account["requirements"] == []
    assert account["planning_errors"][0]["reason"] == "option_decision_snapshot_unavailable"


@pytest.mark.parametrize("cache_source", ["account", "shared", "aggregate"])
@pytest.mark.parametrize("conflict_account", ["lx", "sy"])
def test_pipeline_ttl_checks_current_inbox_constraints(tmp_path, monkeypatch, cache_source, conflict_account):
    import sqlite3

    from src.application import pipeline_context as mod
    from src.application.ledger.api import read_current_position_projection

    repo = _ledger(tmp_path)
    monkeypatch.setattr(mod, "open_position_ledger", lambda *_a, **_k: repo)
    monkeypatch.setattr(mod, "_persist_source_snapshot", lambda *_a: None)
    args = dict(
        base=tmp_path, data_config="fixture.json", market="futu",
        account=None if cache_source == "aggregate" else "lx", ttl_sec=3600,
        runtime_config={"accounts": ["lx"]},
        state_dir=tmp_path / "state", shared_state_dir=tmp_path / "shared", log=lambda _m: None,
        exchange_rate_observation={},
    )
    first, _ = mod.load_option_positions_context(**args)
    assert first["context_status"] == "available"
    if cache_source == "shared":
        (tmp_path / "state" / "option_positions_context.json").unlink()
    with sqlite3.connect(tmp_path / "ledger.sqlite3.trade_intake_inbox.sqlite3") as conn:
        conn.executescript(
            "CREATE TABLE trade_inbox(inbox_id TEXT, broker_deal_key TEXT, payload_json TEXT,"
            "status TEXT, result_reason TEXT, payload_version INT);"
            "CREATE TABLE trade_inbox_evidence(inbox_id TEXT, source TEXT, payload_hash TEXT,"
            "evidence_id TEXT, evidence_json TEXT);"
        )
        conn.execute("INSERT INTO trade_inbox VALUES(?,?,?,?,?,?)", (
            "bad", "deal", json.dumps({"account": conflict_account}),
            "conflict", "source_multiplier_conflict", 1,
        ))
        conn.execute("INSERT INTO trade_inbox_evidence VALUES(?,?,?,?,?)", (
            "bad", "test", "hash", "id", "{}",
        ))
    assert read_current_position_projection(repo, account="lx")["status"] == "trusted"
    current = decision_state_snapshot(repo, account="lx", portfolio_scope_id="test-lx")
    assert (current["snapshot_status"] == "trusted") is (conflict_account != "lx")
    context, refreshed = mod.load_option_positions_context(**args)
    if conflict_account == "lx":
        assert refreshed
        assert context["context_status"] == "unavailable"
        assert context["cash_secured_total_by_ccy"] == {}
        assert context["open_positions_min"] == []
    else:
        assert context["context_status"] == "available"
        assert context["cash_secured_total_by_ccy"] == {"USD": 50000.0}
        assert context["context_source"] == {
            "account": "account_cache", "shared": "shared_slice", "aggregate": "shared_refresh",
        }[cache_source]



def test_aggregate_context_does_not_fall_back_to_raw_lots_on_shared_write_failure(tmp_path, monkeypatch):
    from src.application import pipeline_context as mod

    repo = _ledger(tmp_path)
    monkeypatch.setattr(mod, "open_position_ledger", lambda *_a, **_k: repo)
    monkeypatch.setattr(mod, "_persist_source_snapshot", lambda *_a: None)
    original_write = mod.atomic_write_json

    def write(path, payload):
        if path.name == "option_positions_context.shared.json":
            raise OSError("shared cache is not writable")
        return original_write(path, payload)

    monkeypatch.setattr(mod, "atomic_write_json", write)
    context, refreshed = mod.load_option_positions_context(
        base=tmp_path, data_config="fixture.json", market="futu", account=None, ttl_sec=0,
        runtime_config={"accounts": ["lx"]},
        state_dir=tmp_path / "state", shared_state_dir=tmp_path / "shared", log=lambda _m: None,
        exchange_rate_observation={},
    )
    assert context is None
    assert not refreshed


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("bad_history", [False, True])
def test_aggregate_empty_lots_require_complete_configured_account_scope(tmp_path, monkeypatch, configured, bad_history):
    from src.application import pipeline_context as mod

    repo = _ledger(tmp_path)
    if bad_history:
        _break_history(repo, clear_lots=True)
    else:
        repo = SQLiteOptionPositionsRepository(tmp_path / "empty.sqlite3")
        run_position_projection_forced_full(repo, [])
    monkeypatch.setattr(mod, "open_position_ledger", lambda *_a, **_k: repo)
    monkeypatch.setattr(mod, "_persist_source_snapshot", lambda *_a: None)
    context, refreshed = mod.load_option_positions_context(
        base=tmp_path, data_config="fixture.json", market="futu", account=None, ttl_sec=0,
        runtime_config={"accounts": ["lx"]} if configured else None,
        state_dir=tmp_path / "state", shared_state_dir=tmp_path / "shared", log=lambda _m: None,
        exchange_rate_observation={},
    )
    snapshot = decision_state_snapshot(repo, account="lx", portfolio_scope_id="test-lx")
    assert (snapshot["snapshot_status"] == "trusted") is (not bad_history)
    if not configured:
        assert context is None
        assert not refreshed
    else:
        assert refreshed
        assert context["context_status"] == ("unavailable" if bad_history else "available")
        assert context["cash_secured_total_by_ccy"] == {}
        assert context["open_positions_min"] == []


@pytest.mark.parametrize("multiplier", [500, 1000])
@pytest.mark.parametrize("change", ["open", "close"])
def test_aggregate_uses_snapshot_bound_lots_after_raw_read(tmp_path, monkeypatch, multiplier, change):
    from src.application import pipeline_context as mod

    if change == "open":
        repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
        run_position_projection_forced_full(repo, [])
    else:
        repo = _ledger(tmp_path, multiplier=multiplier)

    def read_then_publish(_repo):
        rows = list(list_position_lot_snapshots(repo))
        if change == "open":
            _ledger(tmp_path, multiplier=multiplier)
        else:
            close = TradeEvent(
                event_id="close", event_type="close", event_time_ms=2000,
                contract_key=ContractKey.from_values(
                    broker="futu", account="lx", underlying_symbol="NVDA", option_type="put",
                    strike=100, expiration_ymd="2027-06-18",
                ),
                contracts=1, price=.5, currency="USD", source="test", multiplier=multiplier,
                target_lot_id="lot-a", raw_payload={"side": "buy"},
            )
            run_position_projection_forced_full(repo, [close])
        return rows

    monkeypatch.setattr(mod, "open_position_ledger", lambda *_a, **_k: repo)
    monkeypatch.setattr(mod, "list_position_lot_snapshots", read_then_publish)
    monkeypatch.setattr(mod, "_persist_source_snapshot", lambda *_a: None)
    context, refreshed = mod.load_option_positions_context(
        base=tmp_path, data_config="fixture.json", market="futu", account=None, ttl_sec=0,
        runtime_config={"accounts": ["lx"]}, state_dir=tmp_path / "state",
        shared_state_dir=tmp_path / "shared", log=lambda _m: None, exchange_rate_observation={},
    )
    shared = json.loads((tmp_path / "shared" / "option_positions_context.shared.json").read_text())
    assert refreshed
    assert context["context_status"] == "available"
    assert shared["by_account"]["lx"]["decision_snapshot_status"] == "trusted"
    assert context["cash_secured_total_by_ccy"] == shared["by_account"]["lx"]["cash_secured_total_by_ccy"]
    assert context["cash_secured_total_by_ccy"] == ({"USD": 100 * multiplier} if change == "open" else {})
    assert len(context["open_positions_min"]) == (1 if change == "open" else 0)


@pytest.mark.parametrize("missing", [True, False])
def test_shared_context_missing_or_bad_snapshot_blocks_aggregate_but_keeps_other_account(tmp_path, missing):
    from src.application.positions.context_builder import build_shared_context

    repo = _ledger(tmp_path)
    snapshot = decision_state_snapshot(repo, account="lx", portfolio_scope_id="test-lx")
    snapshots = {"lx": snapshot}
    if not missing:
        snapshots["sy"] = {"snapshot_status": "source_untrusted", "normalized_account": "sy"}
    shared = build_shared_context(
        list(list_position_lot_snapshots(repo)), broker="futu",
        accounts=["lx", "sy"], decision_snapshots_by_account=snapshots,
    )
    assert shared["all_accounts"]["context_status"] == "unavailable"
    assert shared["all_accounts"]["cash_secured_total_by_ccy"] == {}
    assert shared["by_account"]["sy"]["context_status"] == "unavailable"
    assert shared["by_account"]["lx"]["context_status"] == "available"
    assert shared["by_account"]["lx"]["cash_secured_total_by_ccy"] == {"USD": 50000}


@pytest.mark.parametrize("account", [None, "lx"])
@pytest.mark.parametrize("bad_history", [False, True])
def test_context_builder_cli_requires_trusted_explicit_account(tmp_path, monkeypatch, account, bad_history):
    from src.application.positions import context_builder as mod

    repo = _ledger(tmp_path)
    if bad_history:
        _break_history(repo)
    monkeypatch.setattr(mod, "resolve_position_lot_snapshots", lambda **_k: (
        tmp_path / "fixture.json", repo, list(list_position_lot_snapshots(repo)),
    ))
    monkeypatch.setattr(mod, "get_exchange_rates_or_fetch_latest", lambda **_k: {})
    argv = ["context_builder", "--quiet", "--out", str(tmp_path / "context.json"),
            "--shared-out", str(tmp_path / "shared.json")]
    if account:
        argv.extend(["--account", account])
    monkeypatch.setattr("sys.argv", argv)
    mod.main()
    context = json.loads((tmp_path / "context.json").read_text())
    shared = json.loads((tmp_path / "shared.json").read_text())
    expected = "available" if account and not bad_history else "unavailable"
    assert context["context_status"] == expected
    assert shared["all_accounts"]["context_status"] == expected
    assert context["cash_secured_total_by_ccy"] == ({"USD": 50000} if expected == "available" else {})
