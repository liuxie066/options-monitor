from copy import deepcopy
from dataclasses import replace
import hashlib

from domain.domain.wheel import build_wheel_event
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.decision_history_results import attach_trade_results
from test_performance_service_v2 import _event, _ms
from test_decision_history_query import SCOPE


def fixture(base, *, closed=True, fee_missing=False, physical="1001", snapshot="a"*64, void=False, intent_contracts=1, opening_contracts=1):
    repo = SQLiteOptionPositionsRepository(base / "ledger.sqlite3")
    opening = _event("open", "open", "2026-09-01T10:00:00")
    opening = replace(opening, contracts=opening_contracts, raw_payload={**opening.raw_payload, "futu_account_id": physical, "trd_env": "REAL", "market": "US"})
    repo.upsert_trade_event(opening)
    if closed:
        close = _event("close", "close", "2026-09-02T10:00:00", target_lot_id="lot-1")
        close = replace(close, raw_payload={**close.raw_payload, "futu_account_id": physical, "trd_env": "REAL", "market": "US"})
        if fee_missing:
            close = replace(close, fees=None, raw_payload={k: v for k, v in close.raw_payload.items() if k != "fee_provenance"})
        repo.upsert_trade_event(close)
    common = dict(account="lx", lot_id=None, wheel_branch_id="branch", intent_id="intent")
    created = build_wheel_event(**common, event_id="created", event_type="wheel_put_intent_created",
        occurred_at_ms=_ms("2026-09-01T09:00:00"), recorded_at_ms=_ms("2026-09-01T09:00:00"),
        payload={"contracts": intent_contracts, "multiplier": 100, "expires_at_ms": _ms("2026-09-01T11:00:00"),
                 "final_candidate_id": "candidate", "snapshot_hash": snapshot})
    consumed = build_wheel_event(**common, event_id="consumed", event_type="wheel_put_intent_consumed",
        occurred_at_ms=opening.event_time_ms, recorded_at_ms=opening.event_time_ms,
        source_trade_event_id="open", payload={"contracts": opening_contracts, "multiplier": 100, "put_lot_id": "lot-1"})
    with repo._writer_connection(begin_immediate=True) as conn:
        repo.append_wheel_event_once(created, conn=conn)
        repo.append_wheel_event_once(consumed, conn=conn)
        if void:
            repo.append_wheel_event_once(build_wheel_event(**common, event_id="void", event_type="wheel_event_voided",
                occurred_at_ms=_ms("2026-09-03T10:00:00"), recorded_at_ms=_ms("2026-09-03T10:00:00"),
                payload={"target_wheel_event_id": "consumed", "reason": "test"}), conn=conn)
    return repo


def result():
    candidate = {"action_id": "advice", "symbol": "NVDA", "strategy_family": "wheel", "option_type": "put",
        "wheel_branch_id": "branch", "source": {"final_candidate_id": "candidate", "candidate_snapshot_hash": "a"*64}}
    return {"status": "ok", "rows": [{"brief": {"market": "US"}, "original_candidate_sources": [candidate]}]}


def outcome(base, **options):
    repo = fixture(base, **options)
    value = result()
    attach_trade_results(value, ledger_path=repo.db_path, scope=SCOPE, account="lx")
    return repo, value["rows"][0]["trade_results"][0]


def test_exact_link_uses_existing_realized_cashflow_and_read_only_ledger(tmp_path):
    repo = fixture(tmp_path)
    before = hashlib.sha256(repo.db_path.read_bytes()).hexdigest()
    value = result()
    original = deepcopy(value["rows"][0]["brief"])
    attach_trade_results(value, ledger_path=repo.db_path, scope=SCOPE, account="lx")
    linked = value["rows"][0]["trade_results"][0]
    assert linked["status"] == "linked", linked
    assert linked["trades"][0]["outcomes"][0]["option_net_cashflow"] == 100
    assert linked["trades"][0]["outcomes"][0]["label"] == "已实现期权净现金流"
    assert value["rows"][0]["brief"] == original
    assert hashlib.sha256(repo.db_path.read_bytes()).hexdigest() == before


def test_open_and_fee_missing_never_fabricate_result(tmp_path):
    _, opened = outcome(tmp_path / "open", closed=False)
    assert opened["status"] == "linked", opened
    assert opened["trades"][0]["outcomes"][0]["label"] == "未结束"
    assert opened["trades"][0]["outcomes"][0]["option_net_cashflow"] is None
    _, missing = outcome(tmp_path / "fee", fee_missing=True)
    assert missing["status"] == "linked", missing
    assert missing["trades"][0]["outcomes"][0]["label"] == "结果不可用"
    assert missing["trades"][0]["outcomes"][0]["missing"]


def test_wrong_snapshot_voided_link_and_scope_do_not_infer_trade(tmp_path):
    _, wrong = outcome(tmp_path / "wrong", snapshot="b"*64)
    assert wrong["status"] == "unlinked"
    _, void = outcome(tmp_path / "void", void=True)
    assert void["status"] == "unlinked", void
    _, scope = outcome(tmp_path / "scope", physical="1002")
    assert scope["status"] == "unavailable" and scope["reason"] == "trade_scope_unproven"


def test_ledger_void_refresh_changes_result_not_original_decision(tmp_path):
    repo = fixture(tmp_path)
    value = result()
    attach_trade_results(value, ledger_path=repo.db_path, scope=SCOPE, account="lx")
    assert value["rows"][0]["trade_results"][0]["status"] == "linked"
    repo.upsert_trade_event(_event("void-open", "void", "2026-09-03T10:00:00", target_event_id="open"))
    refreshed = result()
    attach_trade_results(refreshed, ledger_path=repo.db_path, scope=SCOPE, account="lx")
    assert refreshed["rows"][0]["trade_results"][0]["status"] != "linked"
    assert refreshed["rows"][0]["brief"] == value["rows"][0]["brief"]


def test_split_origin_conflict_is_not_allocated_by_guess(tmp_path):
    repo = fixture(tmp_path)
    with repo._writer_connection(begin_immediate=True) as conn:
        repo.append_wheel_event_once(build_wheel_event(account="lx", lot_id=None, wheel_branch_id="other-branch", intent_id="other-intent",
            event_id="other-consumption", event_type="wheel_put_intent_consumed", occurred_at_ms=_ms("2026-09-01T10:00:00"),
            recorded_at_ms=_ms("2026-09-01T10:00:00"), source_trade_event_id="open", payload={"contracts": 1, "multiplier": 100}), conn=conn)
    value = result()
    attach_trade_results(value, ledger_path=repo.db_path, scope=SCOPE, account="lx")
    entry = value["rows"][0]["trade_results"][0]
    assert entry["status"] == "conflict" and entry["trades"] == []


def test_missing_terminal_scope_masks_money(tmp_path):
    repo = fixture(tmp_path, closed=False)
    # Append an older incomplete terminal source to the isolated ledger.
    repo.upsert_trade_event(_event("close", "close", "2026-09-02T10:00:00", target_lot_id="lot-1"))
    value = result()
    attach_trade_results(value, ledger_path=repo.db_path, scope=SCOPE, account="lx")
    output = value["rows"][0]["trade_results"][0]["trades"][0]["outcomes"][0]
    assert output["option_net_cashflow"] is None
    assert "terminal_scope_unproven" in output["missing"]
    assert value["status"] == "partial"


def test_multiple_explicit_fills_are_distinct_without_duplicate_cash(tmp_path):
    repo = fixture(tmp_path, closed=False, intent_contracts=2)
    extra = _event("open-2", "open", "2026-09-01T10:01:00")
    extra = replace(extra, lot_id="lot-2", raw_payload={**extra.raw_payload, "futu_account_id": "1001", "trd_env": "REAL", "market": "US"})
    repo.upsert_trade_event(extra)
    with repo._writer_connection(begin_immediate=True) as conn:
        repo.append_wheel_event_once(build_wheel_event(account="lx", lot_id=None, wheel_branch_id="branch", intent_id="intent",
            event_id="consumed-2", event_type="wheel_put_intent_consumed", occurred_at_ms=extra.event_time_ms,
            recorded_at_ms=extra.event_time_ms, source_trade_event_id="open-2", payload={"contracts": 1, "multiplier": 100}), conn=conn)
    value = result()
    attach_trade_results(value, ledger_path=repo.db_path, scope=SCOPE, account="lx")
    entry = value["rows"][0]["trade_results"][0]
    assert entry["status"] == "linked", entry
    assert {trade["open_event_id"] for trade in entry["trades"]} == {"open", "open-2"}
    assert sum(trade["contracts"] for trade in entry["trades"]) == 2


def test_missing_multiplier_never_defaults_to_100(tmp_path):
    repo = fixture(tmp_path, closed=False)
    # An append-only economic adjustment with absent units cannot invent a result.
    from src.application.decision_history_results import _link
    opening = _event("open", "open", "2026-09-01T10:00:00")
    opening = replace(opening, multiplier=None, raw_payload={**opening.raw_payload, "futu_account_id": "1001", "trd_env": "REAL", "market": "US"})
    events = repo.list_wheel_events(account="lx")
    value = result()
    candidate = value["rows"][0]["original_candidate_sources"][0]
    entry = {"status": "unlinked", "trades": []}
    _link(entry, candidate, scope=SCOPE, account="lx", market="US", events=events, invalid={},
          open_events={"open": opening}, facts={}, now_ms=_ms("2026-09-03T10:00:00"), source_events={})
    assert entry["status"] == "conflict" and not entry["trades"]


def test_partial_close_keeps_whole_fill_unfinished(tmp_path):
    repo = fixture(tmp_path, intent_contracts=2, opening_contracts=2)
    value = result()
    attach_trade_results(value, ledger_path=repo.db_path, scope=SCOPE, account="lx")
    linked = value["rows"][0]["trade_results"][0]
    assert linked["status"] == "linked", linked
    outcomes = linked["trades"][0]["outcomes"]
    assert sum(row["contracts"] for row in outcomes) == 2
    assert {row["state"] for row in outcomes} == {"unresolved_after_expiry", "terminated"}
    assert all(row["label"] == "未结束" and row["option_net_cashflow"] is None for row in outcomes)
