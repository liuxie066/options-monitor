from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.application.ledger import api as ledger_api
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.positions.assigned_stock_view import build_assigned_stock_view
from tests.test_performance_assignment import _assign_put, _trade


@pytest.mark.parametrize("account", ["lx", "sy"])
@pytest.mark.parametrize("as_of_ms", [4000, 8000])
@pytest.mark.parametrize("surface", ["view", "rows", "oracle", "wheel"])
def test_future_open_and_public_void_preserve_historical_assignment(tmp_path, account, as_of_ms, surface):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    repo.upsert_trade_event(_trade())
    repo.upsert_trade_event(_assign_put())
    future = _trade(event_id="future-open", lot_id="future-lot", event_time_ms=6000)
    future = replace(future, contract_key=replace(future.contract_key, account=account))
    repo.upsert_trade_event(future)
    result = ledger_api.record_trade_event_void(repo, event_id="future-open", reason="fixture correction")
    assert result["mode"] == "applied"
    stored = ledger_api.trade_event_log(repo)

    rows = {"trade_events": stored}
    if surface == "view":
        report = build_assigned_stock_view(repo, account="lx", as_of_ms=as_of_ms)
    elif surface == "oracle":
        from src.application.ledger.current_decision_oracle import _oracle_assigned_stock_report

        report = _oracle_assigned_stock_report(rows, account="lx", now_ms=as_of_ms)
    elif surface == "wheel":
        report = ledger_api.build_wheel_read_model_from_rows(rows, account="lx", as_of_ms=as_of_ms)["assigned_stock_projection"]
    else:
        report = ledger_api.project_assigned_stock_lifecycle_from_rows(rows, account="lx", as_of_ms=as_of_ms)

    assert [lot["shares_remaining"] for lot in report["assigned_stock_lots"]] == [100]
    assert ledger_api.trade_event_log(repo) == stored


def test_late_unknown_void_still_rejects_historical_projection(tmp_path):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    repo.upsert_trade_event(_trade())
    repo.upsert_trade_event(_assign_put())
    repo.upsert_trade_event(_trade(
        event_id="unknown-void", event_type="void", event_time_ms=7000,
        contracts=0, price=0, lot_id=None, target_event_id="unknown", raw_payload={},
    ))
    with pytest.raises(ValueError, match="target_event_not_found"):
        build_assigned_stock_view(repo, account="lx", as_of_ms=4000)


def _report(rows, surface, as_of_ms=4000):
    if surface == "view":
        repo = SimpleNamespace(
            list_trade_events=lambda: rows,
            list_position_lots=lambda: [],
            list_assigned_stock_events=lambda: [],
        )
        return build_assigned_stock_view(repo, account="lx", as_of_ms=as_of_ms)
    if surface == "oracle":
        from src.application.ledger.current_decision_oracle import _oracle_assigned_stock_report

        return _oracle_assigned_stock_report({"trade_events": rows}, account="lx", now_ms=as_of_ms)
    if surface == "wheel":
        return ledger_api.build_wheel_read_model_from_rows(
            {"trade_events": rows}, account="lx", as_of_ms=as_of_ms,
        )["assigned_stock_projection"]
    return ledger_api.project_assigned_stock_lifecycle_from_rows(
        {"trade_events": rows}, account="lx", as_of_ms=as_of_ms,
    )


@pytest.mark.parametrize("surface", ["view", "rows", "oracle", "wheel"])
@pytest.mark.parametrize("as_of_ms", [4000, 8000])
@pytest.mark.parametrize(("invalid", "error"), [
    ("void-target", "target_event_type_invalid"),
    ("ambiguous", "target_event_ambiguous"),
    ("account", "target_event_contract_mismatch"),
    ("broker", "target_event_contract_mismatch"),
    ("contract", "target_event_contract_mismatch"),
    ("time", "target_event_time_invalid"),
    ("malformed-target", "target_event_not_found"),
    ("malformed-void", "canonical_event_decode_failed"),
    ("invalid-price", "event_price_invalid"),
    ("invalid-void", "event_multiplier_invalid"),
    ("self", "target_event_self_reference"),
    ("duplicate-void", "duplicate_event_id"),
    ("unknown", "target_event_not_found"),
])
def test_future_target_filter_cannot_hide_invalid_controls(surface, as_of_ms, invalid, error):
    future = _trade(event_id="future-open", lot_id="future-lot", event_time_ms=6000).to_dict()
    void = _trade(
        event_id="late-void", event_type="void", event_time_ms=7000,
        contracts=0, price=0, lot_id=None, target_event_id="future-open", raw_payload={},
    ).to_dict()
    extra = []
    if invalid == "void-target":
        future = {**void, "event_id": "future-open", "event_time_ms": 6000, "target_event_id": "assign-put"}
    elif invalid == "ambiguous":
        extra.append({**future, "lot_id": "another-future-lot"})
    elif invalid in {"account", "broker", "contract"}:
        field, value = {"account": ("account", "sy"), "broker": ("broker", "ibkr"),
                        "contract": ("underlying_symbol", "AAPL")}[invalid]
        void["contract_key"] = {**void["contract_key"], field: value}
    elif invalid == "time":
        void["event_time_ms"] = 5000
    elif invalid == "malformed-target":
        future = {"event_id": "future-open", "event_time_ms": 6000}
    elif invalid == "malformed-void":
        void["raw_payload"] = 1
    elif invalid == "invalid-price":
        void["price"] = "invalid"
    elif invalid == "invalid-void":
        void["multiplier"] = None
    elif invalid == "self":
        void["target_event_id"] = "late-void"
    elif invalid == "duplicate-void":
        extra.append(dict(void))
    else:
        void["target_event_id"] = "unknown"
    rows = [_trade().to_dict(), _assign_put().to_dict(), future, void, *extra]
    original = deepcopy(rows)
    with pytest.raises(ValueError, match=error):
        _report(rows, surface, as_of_ms)
    assert rows == original


@pytest.mark.parametrize("surface", ["view", "rows", "oracle", "wheel"])
@pytest.mark.parametrize("void_future", [False, True])
def test_future_economic_errors_stay_out_of_historical_projection(surface, void_future):
    rows = [_trade().to_dict(), _assign_put().to_dict(), _trade(
        event_id="future-open", lot_id="future-lot", event_time_ms=6000, multiplier=None,
    ).to_dict()]
    if void_future:
        rows.append(_trade(
            event_id="late-void", event_type="void", event_time_ms=7000,
            contracts=0, price=0, lot_id=None, target_event_id="future-open", raw_payload={},
        ).to_dict())
    assert [lot["shares_remaining"] for lot in _report(rows, surface)["assigned_stock_lots"]] == [100]


@pytest.mark.parametrize("surface", ["view", "rows", "oracle", "wheel"])
def test_unrelated_account_control_errors_remain_isolated(surface):
    key = replace(_trade().contract_key, account="sy")
    future = _trade(event_id="sy-future", lot_id="sy-lot", event_time_ms=6000, contract_key=key)
    invalid_void = _trade(
        event_id="sy-void", event_type="void", event_time_ms=5000, contract_key=key,
        contracts=0, price=0, lot_id=None, target_event_id=future.event_id, raw_payload={},
    )
    rows = [_trade().to_dict(), _assign_put().to_dict(), future.to_dict(), invalid_void.to_dict()]
    assert [lot["shares_remaining"] for lot in _report(rows, surface)["assigned_stock_lots"]] == [100]
