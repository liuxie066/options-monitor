"""Opening admission must agree with the full oracle, including closed history."""
from dataclasses import replace
from datetime import datetime
import sqlite3

import pytest

from domain.domain.ledger import TradeEvent, project_resumable_trade_events
from src.application.ledger import api
from src.application.ledger.errors import LedgerPreflightError
from src.application.ledger.event_codec import import_stored_trade_events
from src.application.ledger.position_projection_publication import publish_full_position_projection
from src.application.ledger.position_projection_runtime import (
    _RUNTIME_TELEMETRY,
    run_position_projection_fast_if_safe,
    run_position_projection_forced_full,
)
from src.application.ledger.preflight import _manual_open_ledger_inputs, preflight_manual_open
from src.application.ledger.publisher import project_stored_trade_events_to_position_lots
from src.application.ledger.repository import SQLiteOptionPositionsRepository as Repo
from src.application.ledger.writer_trade_events import persist_trade_event_object


def _ms(text):
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)


def _input(request="candidate", **changes):
    values = dict(
        broker="futu", account="synthetic648", symbol="NVDA", option_type="put",
        side="short", contracts=2, currency="USD", strike=100, multiplier=100,
        expiration_ymd="2026-06-19", premium_per_share=1.5,
        opened_at_ms=_ms("2026-05-01T00:00:00Z"), request_id=request,
        underlying_share_locked=None, note=None, strategy_snapshot=None,
    )
    return {**values, **changes}


def _event(values):
    return _manual_open_ledger_inputs(**values)[1]


def _snapshot(repo):
    conn = sqlite3.connect(f"file:{repo.db_path}?mode=ro", uri=True)
    try:
        conn.execute("BEGIN")
        schema = conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        tables = {
            name: sorted(conn.execute('SELECT * FROM "' + name.replace('"', '""') + '"').fetchall(), key=repr)
            for kind, name, _table, _sql in schema if kind == "table"
        }
        return {"schema": schema, "tables": tables,
                "schema_version": conn.execute("PRAGMA schema_version").fetchone()}
    finally:
        conn.rollback()
        conn.close()


def _repo(tmp_path, mode="seeded_on"):
    repo = Repo(tmp_path / "ledger.sqlite3")
    assert repo.data_config_path is None
    if mode.startswith("seeded"):
        assert api.record_manual_position_open(repo, **_input("seed", symbol="MSFT", opened_at_ms=_ms("2026-04-01T00:00:00Z"))).result.created
    if mode in {"seeded_on", "empty_checkpoint"}:
        _checkpoint(repo)
    elif mode == "empty_on":
        repo.set_position_projection_checkpoint_mode("enabled")
    return repo


def _checkpoint(repo):
    repo.set_position_projection_checkpoint_mode("enabled")
    result = run_position_projection_forced_full(repo, seed_checkpoint=True)
    assert result.checkpoint_written and result.publication.heads_trusted
    assert not result.pruned_checkpoint_ids


DATES = [
    ("2026-05-01T00:00:00Z", "2026-05-02", True),
    ("2026-05-01T00:00:00Z", "2026-05-01", True),
    ("2026-04-30T15:59:59.999Z", "2026-04-30", True),
    ("2026-04-30T16:00:00Z", "2026-04-30", False),
    ("2026-05-01T00:00:00Z", "2026-04-30", False),
]


@pytest.mark.parametrize("opened,expiration,valid", DATES)
def test_domain_open_date_matches_full(opened, expiration, valid):
    seed = _event(_input("seed", symbol="MSFT"))
    candidate = _event(_input(opened_at_ms=_ms(opened), expiration_ymd=expiration))
    full = project_resumable_trade_events([seed, candidate])
    tail = project_resumable_trade_events([candidate], initial_state=project_resumable_trade_events([seed]).state, entry_mode="tail")
    assert full.eligible is valid
    assert tail.requires_full_replay is (not valid)
    assert tail.eligible is valid


def _assert_admission(repo, candidate, valid, *, ordinary_tail=False):
    before = _snapshot(repo)
    if not valid:
        for preview in (preflight_manual_open, api.preview_manual_position_open, api.record_manual_position_open):
            with pytest.raises(LedgerPreflightError) as error:
                preview(repo, **candidate)
            assert error.value.code == "open_projection_invalid"
            assert _snapshot(repo) == before
        # The writer must reject independently of the public preflight.
        for _ in range(2):
            with pytest.raises(RuntimeError, match="publishable full projection lacks resumable state"):
                persist_trade_event_object(repo, _event(candidate))
            assert _snapshot(repo) == before
        return
    preview = api.preview_manual_position_open(repo, **candidate)
    assert preview.ledger_preflight.status == "ok"
    assert _snapshot(repo) == before
    previous_telemetry = _RUNTIME_TELEMETRY[-1] if _RUNTIME_TELEMETRY else None
    result = api.record_manual_position_open(repo, **candidate)
    assert result.result.created
    if ordinary_tail:
        assert preview.ledger_preflight.details["projection_preview_mode"] == "resumed_tail"
        assert _RUNTIME_TELEMETRY[-1] is not previous_telemetry
        assert _RUNTIME_TELEMETRY[-1]["mode_used"] == "fast_tail"
    fresh = Repo(repo.db_path, initialize=False)
    assert api.read_current_position_projection(fresh, account="synthetic648")["status"] == "trusted"
    after = _snapshot(fresh)
    assert len(fresh.list_trade_events()) == len(before["tables"]["trade_events"]) + 1
    assert not api.record_manual_position_open(fresh, **candidate).result.created
    assert _snapshot(fresh) == after
    api.refresh_position_lot_projection(fresh)
    events, diagnostics = import_stored_trade_events(fresh.list_trade_events())
    assert not diagnostics and project_resumable_trade_events(events).eligible
    assert api.read_current_position_projection(Repo(repo.db_path, initialize=False), account="synthetic648")["status"] == "trusted"


@pytest.mark.parametrize("mode", ["seeded_on", "seeded_off", "empty_on", "empty_checkpoint", "empty_off"])
@pytest.mark.parametrize("opened,expiration,valid", DATES)
def test_public_open_date_admission(tmp_path, mode, opened, expiration, valid):
    _assert_admission(_repo(tmp_path, mode), _input(opened_at_ms=_ms(opened), expiration_ymd=expiration), valid, ordinary_tail=valid and mode == "seeded_on")


def _combo(role, explicit):
    return {"strategy_group_id": "combo_yield:synthetic648", "leg_role": role,
            **({"strategy": "combo_yield"} if explicit else {})}


@pytest.mark.parametrize("kind", ["csp_lc", "cc_lp"])
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("history", ["active", "closed_first", "closed_pair"])
@pytest.mark.parametrize("equal", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_combo_historical_membership(tmp_path, kind, explicit, history, equal, enabled):
    repo = _repo(tmp_path, "seeded_off")
    collar = kind == "cc_lp"
    first = _input("first", side="long" if collar else "short", strategy_snapshot=_combo("long_put" if collar else "funding_put", explicit))
    first_result = api.record_manual_position_open(repo, **first)
    call = _input("second", option_type="call", side="short" if collar else "long", strike=110,
                  opened_at_ms=_ms("2026-05-02T00:00:00Z"), strategy_snapshot=_combo("short_call" if collar else "participation_call", explicit))
    if history == "closed_pair":
        second = api.record_manual_position_open(repo, **call)
        api.record_manual_position_close(repo, lot_id=second.result.lot_id, contracts_to_close=2, close_price=1, close_reason="manual", as_of_ms=_ms("2026-05-03T00:00:00Z"))
    if history != "active":
        api.record_manual_position_close(repo, lot_id=first_result.result.lot_id, contracts_to_close=2, close_price=1, close_reason="manual", as_of_ms=_ms("2026-05-03T01:00:00Z"))
    if enabled:
        _checkpoint(repo)
    candidate = {**call, "request_id": "candidate", "contracts": 2 if equal else 1, "opened_at_ms": _ms("2026-05-04T00:00:00Z")}
    events, diagnostics = import_stored_trade_events(repo.list_trade_events())
    assert not diagnostics
    state = project_resumable_trade_events(events).state
    tail = project_resumable_trade_events([_event(candidate)], initial_state=state, entry_mode="tail")
    assert tail.requires_full_replay
    full = project_resumable_trade_events([*events, _event(candidate)])
    valid = equal and history != "closed_pair"
    assert full.eligible is valid
    _assert_admission(repo, candidate, valid)


@pytest.mark.parametrize("metadata", [None, {"strategy_group_id": "ordinary", "leg_role": "funding_put"}, {"strategy_group_id": "combo_yield:synthetic648", "leg_role": "other"}])
def test_nonmember_open_keeps_tail(tmp_path, metadata):
    _assert_admission(_repo(tmp_path), _input(strategy_snapshot=metadata), True, ordinary_tail=True)


def test_full_replay_can_correct_original_bad_open():
    opened = _event(_input(expiration_ymd="2026-04-30"))
    adjust = TradeEvent(event_id="correction", event_type="adjust", event_time_ms=_ms("2026-05-02T00:00:00Z"),
                        contract_key=opened.contract_key, contracts=0, price=0, currency="USD", multiplier=100,
                        source="synthetic", target_lot_id=opened.lot_id, raw_payload={"patch": {"expiration_ymd": "2026-06-19"}})
    assert not project_resumable_trade_events([opened]).eligible
    corrected = project_resumable_trade_events([opened, adjust])
    assert corrected.eligible and not corrected.diagnostics


def _historical_bad_open(repo, request):
    # Synthetic historical fixture: bypass admission, never forge a head/checkpoint.
    event = _event(_input(request, expiration_ymd="2026-04-30"))
    assert repo.upsert_trade_event(event)
    projection = project_stored_trade_events_to_position_lots(repo.list_trade_events())
    assert projection.diagnostics and not projection.has_errors
    assert projection.resumable_state is None
    publish_full_position_projection(repo, projection.lots)
    return event.lot_id


@pytest.mark.parametrize("batch", [False, True])
def test_warning_history_remains_correctable(tmp_path, batch):
    repo = _repo(tmp_path, "empty_off")
    ids = [_historical_bad_open(repo, "bad-1")]
    if batch:
        ids.append(_historical_bad_open(repo, "bad-2"))
    before = _snapshot(repo)
    with pytest.raises(LedgerPreflightError) as error:
        api.record_manual_position_open(repo, **_input("new-ordinary"))
    assert error.value.code == "open_projection_invalid"
    assert _snapshot(repo) == before
    for lot_id in ids:
        preview = api.preview_manual_position_adjust(repo, lot_id=lot_id, contracts=None, strike=None,
            expiration_ymd="2026-06-19", premium_per_share=None, opened_at_ms=None)
        assert preview.ledger_preflight.status == "ok"
        assert _snapshot(repo) == before
    # Existing warning-only preview policy stays; writer still rejects and rolls back.
    with pytest.raises(RuntimeError, match="publishable full projection lacks resumable state"):
        if batch:
            api.record_manual_position_adjustments(repo, [{"record_id": lot_id, "expiration_ymd": "2026-04-29"} for lot_id in ids])
        else:
            api.record_manual_position_adjust(repo, lot_id=ids[0], expiration_ymd="2026-04-29")
    assert _snapshot(repo) == before
    if batch:
        results = api.record_manual_position_adjustments(repo, [{"record_id": lot_id, "expiration_ymd": "2026-06-19"} for lot_id in ids])
    else:
        results = [api.record_manual_position_adjust(repo, lot_id=ids[0], expiration_ymd="2026-06-19")]
    assert all(result.result.created for result in results)
    assert len(repo.list_trade_events()) == len(ids) * 2
    api.refresh_position_lot_projection(Repo(repo.db_path, initialize=False))
    assert project_stored_trade_events_to_position_lots(repo.list_trade_events()).resumable_state is not None


@pytest.mark.parametrize("stage", ["after_event_write", "after_tail_projection", "after_full_projection", "after_head_publication", "before_checkpoint_insert", "after_checkpoint_insert", "before_commit"])
@pytest.mark.parametrize("exception", [RuntimeError, KeyboardInterrupt])
def test_combo_fallback_failure_rollback_retry(tmp_path, stage, exception):
    repo = _repo(tmp_path)
    # The tail hook fires only for eligible ordinary tails, before any full fallback.
    candidate = _input(strategy_snapshot=None if stage == "after_tail_projection" else _combo("funding_put", False))
    before = _snapshot(repo)
    fired = []

    def fail(current):
        if current == stage:
            fired.append(current)
            raise exception("synthetic648 hook")

    with pytest.raises(exception, match="synthetic648 hook"):
        run_position_projection_fast_if_safe(repo, [_event(candidate)], failure_hook=fail)
    assert fired == [stage]
    assert _snapshot(repo) == before
    _assert_admission(repo, candidate, True)


@pytest.mark.parametrize("missing", ["resumable_state", "resumable_publication_state"])
def test_open_full_requires_both_states(tmp_path, monkeypatch, missing):
    from src.application.ledger import preflight

    repo = _repo(tmp_path, "empty_off")
    event = _event(_input())
    preview = preflight.preview_position_projection_append(repo, [event])
    assert preview.projection.resumable_state is not None
    assert preview.projection.resumable_publication_state is not None
    candidate = replace(preview.projection, **{missing: None})
    monkeypatch.setattr(preflight, "preview_position_projection_append", lambda *_: replace(preview, projection=candidate))
    with pytest.raises(LedgerPreflightError) as error:
        preflight_manual_open(repo, **_input())
    assert error.value.code == "open_projection_invalid"


def test_broker_open_uses_shared_admission(tmp_path):
    from src.application.trades.normalizer import NormalizedTradeDeal

    repo = _repo(tmp_path)
    deal = NormalizedTradeDeal(broker="futu", futu_account_id="synthetic", internal_account="synthetic648",
        deal_id="synthetic-deal", order_id="synthetic-order", symbol="NVDA", option_type="put", side="sell",
        position_effect="open", contracts=2, price=1.5, strike=100, multiplier=100, multiplier_source="payload",
        expiration_ymd="2026-04-30", currency="USD", trade_time_ms=_ms("2026-05-01T00:00:00Z"), raw_payload={})
    before = _snapshot(repo)
    calls = []

    def persist(*args):
        calls.append(args)
        raise AssertionError("invalid broker open reached persistence")

    with pytest.raises(LedgerPreflightError) as error:
        api.record_broker_trade_open(repo, deal, persist_trade_event_fn=persist)
    assert error.value.code == "open_projection_invalid"
    assert not calls and _snapshot(repo) == before
    valid = replace(deal, expiration_ymd="2026-06-19")
    result = api.record_broker_trade_open(repo, valid, persist_trade_event_fn=api.record_normalized_trade_event)
    assert result.result.created
    fresh = Repo(repo.db_path, initialize=False)
    assert api.read_current_position_projection(fresh, account="synthetic648")["status"] == "trusted"
    after = _snapshot(fresh)
    assert len(fresh.list_trade_events()) == len(before["tables"]["trade_events"]) + 1
    assert not api.record_broker_trade_open(fresh, valid, persist_trade_event_fn=api.record_normalized_trade_event).result.created
    assert _snapshot(fresh) == after
    api.refresh_position_lot_projection(fresh)
    assert project_stored_trade_events_to_position_lots(fresh.list_trade_events()).resumable_state is not None


def test_combo_classification_preserves_metadata_boundaries():
    from domain.domain.ledger.projection import _canonical_combo_member

    seed = _event(_input("seed", symbol="MSFT"))
    state = project_resumable_trade_events([seed]).state
    candidates = [
        ({"strategy": "combo_yield", "strategy_group_id": "explicit-group", "leg_role": "funding_put"}, ("explicit-group", "funding_put")),
        ({"strategy_snapshot": _combo("long_put", False)}, ("combo_yield:synthetic648", "long_put")),
        ({"strategy_group_id": "ordinary", "leg_role": "funding_put"}, None),
        ({"strategy": "combo_yield", "leg_role": "funding_put"}, None),
        ({"strategy_group_id": "combo_yield:synthetic648", "leg_role": "unknown"}, None),
        ({"strategy_group_id": "conflict", "strategy_snapshot": _combo("funding_put", True)}, None),
    ]
    for metadata, expected in candidates:
        candidate = replace(_event(_input()), raw_payload={"side": "sell", **metadata})
        assert _canonical_combo_member(candidate) == expected
        full = project_resumable_trade_events([seed, candidate])
        tail = project_resumable_trade_events([candidate], initial_state=state, entry_mode="tail")
        if expected is not None:
            assert full.eligible
            assert tail.requires_full_replay and tail.full_replay_reason == "tail_combo_attribution"
        else:
            assert tail.full_replay_reason != "tail_combo_attribution"
            assert tail.eligible == full.eligible


def test_stock_new_open_does_not_use_option_expiration_check():
    from domain.domain.ledger import ContractKey

    seed = _event(_input("seed", symbol="MSFT"))
    key = ContractKey.from_values(broker="futu", account="synthetic648", underlying_symbol="NVDA",
        option_type="", strike=0, expiration_ymd="", asset_type="stock")
    stock = TradeEvent(event_id="stock", event_type="open", event_time_ms=_ms("2026-05-02T00:00:00Z"),
        contract_key=key, contracts=100, price=100, currency="USD", source="synthetic", lot_id="stock",
        asset_type="stock", raw_payload={"side": "buy", "fee_provenance": {"basis": "actual", "amount": "0", "source": "synthetic"}})
    full = project_resumable_trade_events([seed, stock])
    tail = project_resumable_trade_events([stock], initial_state=project_resumable_trade_events([seed]).state, entry_mode="tail")
    assert full.eligible and tail.eligible and not tail.requires_full_replay
    assert full.state == tail.state
