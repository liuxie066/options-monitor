"""Effective chronology must not discard raw execution-completion evidence."""

from dataclasses import replace
import json
import sqlite3

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.trade_execution import ledger_execution_event_set_is_complete
from src.application.ledger.publisher import project_stored_trade_events_to_position_lots
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.writer_trade_events import rebuild_position_lots_from_trade_events
from src.application.trades import state as intake_state
from src.application.trades.inbox import enqueue_trade_payload
from src.application.trades.intake import process_trade_payload
from src.application.trades.normalizer import normalize_trade_deal
from src.application.trades.resolver import resolve_trade_deal


ACCOUNT = "invented797"
PHYSICAL = "797000"
EPOCH = 1780300800000
CONTRACT = ContractKey.from_values(
    broker="futu", account=ACCOUNT, underlying_symbol="TIGR",
    option_type="put", strike=6, expiration_ymd="2026-07-17",
)


def _payload(identity="incoming", *, time=200, side="buy", quantity=1):
    return {
        "deal_id": identity, "futu_account_id": PHYSICAL,
        "broker_account_id": "synthetic-797-real", "environment": "REAL",
        "external_id_namespace": "futu.deal", "code": "US.TIGR260717P6000",
        "trd_side": side.upper(), "qty": quantity, "price": 1.25,
        "multiplier": 100, "currency": "USD", "trade_time_ms": EPOCH + time * 1000,
    }


def _event(identity, time, *, target=None, side="buy", contract=CONTRACT, raw=None):
    return TradeEvent(
        event_id=identity, event_type="void" if target else "open",
        event_time_ms=EPOCH + time * 1000, contract_key=contract,
        contracts=0 if target else 1, price=0 if target else 1,
        currency="USD", source="synthetic-history", multiplier=100,
        target_event_id=target, lot_id=None if target else "lot:" + identity,
        raw_payload=raw if raw is not None else {"side": side},
    )


def _seed(repo, *events, publish=False):
    for event in events:
        assert repo.upsert_trade_event(event)
    if publish:
        rebuild_position_lots_from_trade_events(repo)


def _void_history(repo, target_time=300):
    _seed(repo, _event("target", target_time), _event("void", 400, target="target"))
    projected = project_stored_trade_events_to_position_lots(repo.list_trade_events())
    assert not projected.has_errors and not projected.lots


def _tables(repo):
    with repo._connect() as conn:
        names = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        return {
            name[0]: sorted(
                [list(row) for row in conn.execute('SELECT * FROM "' + name[0].replace('"', '""') + '"')],
                key=repr,
            )
            for name in names
        }


def _intake(tmp_path, repo, payload, *, apply=True):
    folder = tmp_path / f"intake-{len(list(tmp_path.glob('intake-*')))}"
    folder.mkdir()

    def normalize(data, **kwargs):
        return normalize_trade_deal(
            data, repo_base=tmp_path, runtime_root=tmp_path,
            allow_opend_refresh=False, **kwargs,
        )

    result = process_trade_payload(
        payload, repo=repo, state_path=folder / "state.json", audit_path=folder / "audit.jsonl",
        account_mapping={PHYSICAL: ACCOUNT}, apply_changes=apply,
        load_trade_intake_state_fn=intake_state.load_trade_intake_state,
        write_trade_intake_state_fn=intake_state.write_trade_intake_state,
        upsert_deal_state_fn=intake_state.upsert_deal_state,
        append_trade_intake_audit_fn=intake_state.append_trade_intake_audit,
        enrich_trade_payload_fn=None, normalize_trade_deal_fn=normalize,
        resolve_trade_deal_fn=resolve_trade_deal, on_result_fn=None,
        portfolio_management_enabled=False, source="file",
    )
    audit = [json.loads(line) for line in (folder / "audit.jsonl").read_text().splitlines()]
    normalized = [row["deal"] for row in audit if row["phase"] == "normalized"]
    assert len(normalized) == 1
    assert normalized[0]["execution_input"]["errors"] == []
    assert normalized[0]["internal_account"] == ACCOUNT
    assert not any(row["phase"] == "receipt_sent" for row in audit)
    assert "portfolio_refresh_intent" not in result
    if not apply:
        assert not (folder / "state.json").exists()
    else:
        state = intake_state.load_trade_intake_state(folder / "state.json")
        if result["status"] == "applied" or result["reason"] == "ledger_recorded":
            assert state["processed_deal_ids"] and not state["unresolved_deal_ids"]
            assert any(row["phase"] == "ledger_persisted" for row in audit)
        elif result["status"] == "unresolved":
            assert state["unresolved_deal_ids"] and not state["processed_deal_ids"]
    return result


@pytest.mark.parametrize("target_time", [100, 300])
@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("apply", [False, True])
def test_void_target_and_control_time_do_not_block_intake(tmp_path, target_time, side, apply):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _void_history(repo, target_time)
    before, raw = _tables(repo), repo.list_trade_events()
    result = _intake(tmp_path, repo, _payload(side=side), apply=apply)
    assert result["status"] == ("applied" if apply else "dry_run"), result
    assert result["action"] == "open"
    assert result["reason"] == ("applied_open" if apply else "preview_open")
    if apply:
        assert result["projection_status"] == "recorded_and_projected"
        assert len(repo.list_trade_events()) == len(raw) + 1
        assert all(row in repo.list_trade_events() for row in raw)
        lots = repo.list_position_lots()
        assert len(lots) == 1
        assert lots[0]["fields"]["contracts_open"] == 1
        assert lots[0]["fields"]["position_side"] == ("long" if side == "buy" else "short")
    else:
        assert _tables(repo) == before


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("quantity,action", [(1, "close"), (2, "open_close")])
def test_valid_opposing_lot_allocation_preview_apply_and_replay(tmp_path, side, quantity, action):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _void_history(repo)
    _seed(repo, _event("opposing", 50, side="sell" if side == "buy" else "buy"), publish=True)
    raw, before = repo.list_trade_events(), _tables(repo)
    payload = _payload(side=side, quantity=quantity)
    preview = _intake(tmp_path, repo, payload, apply=False)
    assert preview["status"] == "dry_run" and preview["action"] == action, preview
    assert _tables(repo) == before
    applied = _intake(tmp_path, repo, payload)
    assert applied["status"] == "applied" and applied["action"] == action, applied
    assert applied["diagnostics"]["post_write_projection_verification"]["ok"]
    assert repo.get_record_fields("lot:opposing")["contracts_open"] == 0
    events = repo.list_trade_events()
    assert all(row in events for row in raw)
    allocation = [row for row in events if
                  (row["raw_payload"].get("execution_input") or {}).get("external_execution_id") == "incoming"]
    assert len(allocation) == quantity
    assert sum(row["contracts"] for row in allocation) == quantity
    assert ledger_execution_event_set_is_complete(allocation)
    assert all(row["raw_payload"]["execution_input"]["quantity"] == str(quantity) for row in allocation)
    after = _tables(repo)
    replay = _intake(tmp_path, repo, payload)
    assert replay["reason"] == "ledger_recorded" and replay["action"] == action
    assert _tables(repo) == after


@pytest.mark.parametrize("time", [199, 200, 201])
def test_active_ledger_chronology_keeps_inclusive_boundary(tmp_path, time):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _seed(repo, _event("active", time), publish=True)
    before = _tables(repo)
    result = _intake(tmp_path, repo, _payload())
    if time < 200:
        assert result["status"] == "applied", result
    else:
        assert result["status"] == "unresolved"
        assert result["reason"] == "unknown_position_effect:later_or_same_time_ledger_event"
        assert result["diagnostics"]["retryable"] is False
        assert _tables(repo) == before


@pytest.mark.parametrize("scope", ["account", "contract"])
def test_unrelated_active_history_does_not_block(tmp_path, scope):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    key = replace(CONTRACT, account="other797") if scope == "account" else replace(CONTRACT, strike=7)
    _seed(repo, _event("unrelated", 300, contract=key), publish=True)
    raw = repo.list_trade_events()
    result = _intake(tmp_path, repo, _payload())
    assert result["status"] == "applied", result
    assert all(row in repo.list_trade_events() for row in raw)


@pytest.mark.parametrize("shape,diagnostic", [
    ("missing", "target_event_not_found"),
    ("self", "target_event_self_reference"),
    ("contract", "target_event_contract_mismatch"),
])
def test_invalid_void_relation_still_fails_canonical_preflight(tmp_path, shape, diagnostic):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    if shape == "missing":
        events = [_event("void", 100, target="absent")]
    elif shape == "self":
        events = [_event("void", 100, target="void")]
    else:
        events = [_event("target", 50), _event("void", 100, target="target", contract=replace(CONTRACT, strike=7))]
    _seed(repo, *events)
    projection = project_stored_trade_events_to_position_lots(repo.list_trade_events())
    assert diagnostic in {item.code for item in projection.diagnostics}
    before = _tables(repo)
    result = _intake(tmp_path, repo, _payload())
    assert result["status"] == "unresolved", result
    assert "ledger preflight found invalid current trade-event projection" in result["reason"]
    assert _tables(repo) == before


@pytest.mark.parametrize("kind", [
    "unbooked", "equal", "partial", "voided", "complete", "later", "physical", "environment", "contract",
])
def test_pending_completion_keeps_raw_evidence_and_source_scope(tmp_path, kind):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    pending = _payload("predecessor", time=200 if kind == "equal" else 50)
    if kind == "later":
        pending["trade_time_ms"] = EPOCH + 201000
    elif kind == "physical":
        pending["futu_account_id"] = "797001"
    elif kind == "environment":
        pending["environment"] = "SIMULATE"
    elif kind == "contract":
        pending["code"] = "US.TIGR260717P7000"
    if kind == "partial":
        pending["qty"] = 2
    # Legacy scoped identity remains valid evidence; use the real Inbox writer/schema.
    source_key = f"futu:{ACCOUNT}:{pending['futu_account_id']}:predecessor"
    inbox = repo.db_path.with_name(repo.db_path.name + ".trade_intake_inbox.sqlite3")
    enqueue_trade_payload(inbox, payload=pending, source="file", broker_deal_key=source_key, repo=repo)
    if kind in {"partial", "voided", "complete"}:
        raw = {
            "side": "buy", "source_deal_id": "predecessor", "futu_account_id": PHYSICAL,
            "environment": "REAL", "external_id_namespace": "futu.deal",
        }
        if kind == "partial":
            raw["broker_deal_completion"] = {
                "source_deal_id": "predecessor", "expected_contracts": 2,
                "split_count": 2, "split_index": 1, "allocated_contracts": 1,
            }
        _seed(repo, _event("predecessor", 50, raw=raw))
        assert ledger_execution_event_set_is_complete(repo.list_trade_events()) is (kind != "partial")
        if kind == "voided":
            _seed(repo, _event("void", 100, target="predecessor"))
        else:
            rebuild_position_lots_from_trade_events(repo)
    with sqlite3.connect(inbox.as_uri() + "?mode=ro", uri=True) as conn:
        inbox_before = conn.execute("SELECT * FROM trade_inbox").fetchall()
    before = _tables(repo)
    result = _intake(tmp_path, repo, _payload())
    if kind in {"unbooked", "equal", "partial", "voided"}:
        assert result["status"] == "unresolved"
        assert result["reason"] == "unknown_position_effect:earlier_or_same_time_pending_execution", result
        assert result["diagnostics"]["retryable"] is False
        assert _tables(repo) == before
    else:
        assert result["status"] == "applied", result
    with sqlite3.connect(inbox.as_uri() + "?mode=ro", uri=True) as conn:
        assert conn.execute("SELECT * FROM trade_inbox").fetchall() == inbox_before


def test_inference_cannot_repair_stored_lot_projection_mismatch(tmp_path):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _void_history(repo)
    _seed(repo, _event("not-published", 50))
    before = _tables(repo)
    result = _intake(tmp_path, repo, _payload())
    assert result["status"] == "unresolved"
    assert result["reason"] == "ledger_position_projection_mismatch"
    assert _tables(repo) == before
