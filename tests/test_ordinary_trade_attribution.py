from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from threading import Event

import pytest

from cash_evidence_helpers import cash_config, cash_portfolio
from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.trade_execution import normalize_execution_input, execution_identity_from_input
from src.application.futu_portfolio_context import build_futu_position_snapshot
from src.application.ledger.api import (apply_trade_attribution, build_trade_attribution_view,
    read_trade_attribution_snapshot, read_trade_attribution_facts, assert_trade_attribution_unclaimed,
    ordinary_attribution_window)
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.writer import persist_trade_event_object
from src.application.trades import attribution
from test_combo_reconciliation_domain import _exposure


FILL = int(datetime(2026, 7, 31, 15, tzinfo=timezone.utc).timestamp() * 1000)
NOW = FILL + 24 * 3_600_000
REF = {"broker_id": "futu", "external_account_id": "1001", "environment": "REAL"}


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def _execution(*, key="fill", option="put", side="sell", quantity=1, occurred=FILL, strike=100):
    return normalize_execution_input({"schema_version": "trade_execution.v1",
        "broker_account_ref": {**REF, "broker_account_id": "futu:REAL:1001", "account_label": "lx"},
        "instrument_ref": {"asset_type": "option", "symbol": "NVDA", "market": "US", "currency": "USD",
            "option_type": option, "strike": str(strike), "expiration_ymd": "2026-08-21", "multiplier": "100"},
        "external_id_namespace": "futu.deal", "external_execution_id": key,
        "external_order_namespace": "futu.order", "external_order_id": "order-" + key,
        "side": side, "position_effect": "open", "quantity": str(quantity), "price": "2", "currency": "USD",
        "occurred_at_utc": _iso(occurred)})


def _persist(repo, execution):
    instrument = execution["instrument_ref"]
    key = execution["external_execution_id"]
    persist_trade_event_object(repo, TradeEvent(event_id=key, event_type="open",
        event_time_ms=int(datetime.fromisoformat(execution["occurred_at_utc"].replace("Z", "+00:00")).timestamp()*1000),
        contract_key=ContractKey.from_values(broker="futu", account="lx", underlying_symbol=instrument["symbol"],
            option_type=instrument["option_type"], strike=instrument["strike"], expiration_ymd=instrument["expiration_ymd"]),
        contracts=int(execution["quantity"]), price=2, multiplier=100, currency="USD", source="futu",
        lot_id="lot-"+key, raw_payload={"side": execution["side"], "execution_input": execution,
            "execution_id": execution_identity_from_input(execution), "order_id": execution["external_order_id"]}))


def _scope(tmp_path, monkeypatch, option="put", policy_start=FILL-1):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    execution = _execution(option=option)
    _persist(repo, execution)
    if policy_start is not None:
      with repo._writer_connection(begin_immediate=True) as conn:
        conn.execute("""INSERT INTO trade_attribution_policy_enablings
            (broker,physical_account_id,environment,account,market,policy_version,effective_from_ms,
             created_at_ms,actor,request_id,request_hash)
            VALUES ('futu','1001','REAL','lx','us','trade_attribution.v2',?,?, 'test','enable',?)""",
            (policy_start, FILL-1, "a"*64))
    config = {**cash_config(account_id="1001"), "market": "us", "accounts": ["lx"]}
    snapshot = build_futu_position_snapshot(rows=[
        {"code": "US.NVDA260821"+("P" if option=="put" else "C")+"100000", "stock_owner": "US.NVDA",
         "sec_type": "OPTION", "qty": -1, "option_type": option.upper(), "option_strike_price": 100,
         "strike_time": "2026-08-21", "multiplier": 100},
        {"code": "US.NVDA", "sec_type": "STOCK", "qty": 100}],
        broker_account_ref={**REF, "account_label": "lx", "broker_account_id": "futu:REAL:1001"},
        markets=["US", "HK"], asset_types=["stock", "option"], completeness="complete", observed_at_utc=_iso(NOW))
    portfolio = cash_portfolio({"cash_by_currency": {"USD": 100000}, "source_observed_at": _iso(NOW),
        "cash_source_observed_at": _iso(NOW), "position_snapshot_input": snapshot,
        "exchange_rates": {"rates": {"USDCNY": 7, "HKDCNY": .9}}, "exchange_rate_status": "ready"}, account_id="1001")
    scope = {**REF, "account": "lx"}
    start, _ = ordinary_attribution_window({"contract_key": {"underlying_symbol": "NVDA"}, "event_time_ms": FILL})
    orders = {"scope": scope, "retcode": 0, "coverage_complete": True, "pagination_complete": True,
        "rows": [{"order_id": "order-fill", "status": "terminal_with_fill", "dealt_qty": "1", "created_at_ms": FILL}]}
    history = {"dataset": "executions", "request_id": "source-query", "trd_env": "REAL",
        "window_start_utc": _iso(start), "window_end_utc": _iso(NOW), "account_results": [{
            "futu_account_id": "1001", "trd_env": "REAL", "coverage_status": "complete",
            "coverage_complete": True, "pagination_complete": True, "row_count": 1, "accepted_row_count": 1,
            "covered_start_utc": _iso(start), "covered_end_utc": _iso(NOW)}]}
    observation = {"portfolio": portfolio, "ordinary_evidence": {"scope": scope,
        "window_start_ms": start, "window_end_ms": NOW, "observed_at_ms": NOW,
        "history": history, "executions": [execution], "current_orders": deepcopy(orders), "history_orders": deepcopy(orders)}}
    exposures = [_exposure(delivery_confirmed=True, generated_at_ms=FILL-1000, valid_until_ms=FILL+60000,
        exposure_id="exposure-"+str(i), occurrence_id="occ-"+str(i)) for i in range(2)] if option=="put" else []
    evidence = {"complete": True, "exposures": exposures}
    monkeypatch.setattr(attribution.time, "time", lambda: NOW / 1000)
    context = dict(config=config, market="us", account="lx", combo_mode="confirm", combo_evidence=evidence,
                   capacity_observation=observation)
    return repo, context


def _view(repo, context, now=NOW):
    return build_trade_attribution_view(read_trade_attribution_snapshot(repo, account="lx", market="us"), now_ms=now, **context)


def _args(repo, context, request="auto-ordinary"):
    row = next(row for row in _view(repo, context)["rows"] if row["open_event_id"]=="fill")
    return dict(**context, execution_key=row["execution_key"], candidate_id="ordinary",
                expected_input_hash=row["input_hash"], actor="trade_intake:attribution_rule", request_id=request)


@pytest.mark.parametrize("option", ["put", "call"])
def test_complete_ordinary_preview_apply_retry_and_economics(tmp_path, monkeypatch, option):
    repo, context = _scope(tmp_path, monkeypatch, option)
    row, = _view(repo, context)["rows"]
    assert row["selected_candidate_id"] == "ordinary", row
    assert row["status"] == "pending" and row["ordinary_basis"]
    before = read_trade_attribution_snapshot(repo, account="lx", market="us")
    args = _args(repo, context)
    assert apply_trade_attribution(repo, **args, apply_changes=False)["write_applied"] is False
    assert read_trade_attribution_snapshot(repo, account="lx", market="us") == before
    applied = apply_trade_attribution(repo, **args)
    assert applied["status"] == "ordinary" and applied["origin"] == "rule"
    assert applied["write_applied"] is True
    assert apply_trade_attribution(repo, **args)["write_applied"] is False
    events = repo.list_trade_events()
    assert events[0] == before["trade_events"][0] and len(events) == 2
    proof = next(event for event in events if event["event_type"]=="adjust")
    assert proof["contracts"] == 0 and Decimal(proof["price"]) == Decimal(proof["fees"]) == 0
    assert proof["raw_payload"]["attribution_decision"]["manual"] is False
    assert proof["raw_payload"]["ordinary_basis"] == row["ordinary_basis"]
    assert_trade_attribution_unclaimed(events, ["lot-fill"])
    final, = _view(repo, context)["rows"]
    assert final["status"] == "ordinary" and final["selected_candidate_id"] is None
    assert read_trade_attribution_facts(repo, account="lx")[0]["origin"] == "rule"


@pytest.mark.parametrize("change", ["missing", "partial", "stale", "future", "scope", "history_scope",
    "covered_window", "active_order", "unknown_order", "order_quantity", "missing_order", "duplicate_order",
    "unapplied", "changed_content", "missing_fill", "bad_execution", "old_portfolio", "cash", "position",
    "incomplete_recommendations", "disabled", "policy_after_fill", "exposure_still_valid"])
def test_incomplete_or_conflicting_evidence_never_auto_defaults(tmp_path, monkeypatch, change):
    repo, context = _scope(tmp_path, monkeypatch,
        policy_start=None if change=="disabled" else FILL+1 if change=="policy_after_fill" else FILL-1)
    observation = context["capacity_observation"]
    evidence = observation["ordinary_evidence"]
    if change=="missing": observation.pop("ordinary_evidence")
    elif change=="partial": evidence["current_orders"]["coverage_complete"] = False
    elif change=="stale": evidence["observed_at_ms"] -= 60001
    elif change=="future": evidence["observed_at_ms"] += 1
    elif change=="scope": evidence["scope"] = {**evidence["scope"], "external_account_id": "999"}
    elif change=="history_scope": evidence["history"]["account_results"][0]["trd_env"] = "SIMULATE"
    elif change=="covered_window": evidence["history"]["account_results"][0]["covered_start_utc"] = _iso(FILL+1)
    elif change in {"active_order", "unknown_order"}:
        evidence["current_orders"]["rows"][0]["status"] = "retryable" if change=="active_order" else "unknown"
    elif change=="order_quantity":
        evidence["current_orders"]["rows"][0]["dealt_qty"] = "2"
        evidence["history_orders"]["rows"][0]["dealt_qty"] = "2"
    elif change=="missing_order":
        evidence["current_orders"]["rows"] = []; evidence["history_orders"]["rows"] = []
    elif change=="duplicate_order": evidence["current_orders"]["rows"].append({**evidence["current_orders"]["rows"][0], "dealt_qty":"2"})
    elif change=="unapplied": evidence["executions"].append(_execution(key="new", option="call", side="buy", strike=110))
    elif change=="changed_content": evidence["executions"][0]["price"] = "3"
    elif change=="missing_fill": evidence["executions"] = []
    elif change=="bad_execution": evidence["executions"][0]["errors"] = ["unknown_multiplier"]
    elif change=="old_portfolio": observation["portfolio"]["position_snapshot_input"]["observed_at_utc"] = _iso(NOW-1)
    elif change=="cash": observation["portfolio"]["cash_by_currency"] = {"USD": 0}
    elif change=="position": observation["portfolio"]["position_snapshot_input"]["rows"][0]["quantity"] = "2"
    elif change=="incomplete_recommendations": context["combo_evidence"]["complete"] = False
    elif change=="exposure_still_valid": context["combo_evidence"]["exposures"][0]["valid_until_ms"] = NOW+1
    row, = _view(repo, context)["rows"]
    assert row["selected_candidate_id"] is None, row
    before = repo.list_trade_events()
    with pytest.raises(ValueError): apply_trade_attribution(repo, **_args(repo, context))
    assert repo.list_trade_events() == before


def test_waits_for_market_day_end_and_dst_is_local_date(tmp_path, monkeypatch):
    repo, context = _scope(tmp_path, monkeypatch)
    _, end = ordinary_attribution_window({"contract_key": {"underlying_symbol":"NVDA"}, "event_time_ms":FILL})
    assert _view(repo, context, now=end-1)["rows"][0]["selected_candidate_id"] is None
    for symbol, occurred, hours in [("NVDA", "2026-11-01T12:00:00+00:00", 25),
                                     ("NVDA", "2026-03-08T12:00:00+00:00", 23),
                                     ("0700.HK", "2026-10-15T07:19:18+00:00", 24)]:
        start, finish = ordinary_attribution_window({"contract_key":{"underlying_symbol":symbol},
            "event_time_ms":int(datetime.fromisoformat(occurred).timestamp()*1000)})
        assert finish-start == hours*3_600_000


@pytest.mark.parametrize("quantity", [1, 2])
def test_late_actual_counterpart_reconsiders_rule_ordinary_and_asymmetry_blocks(tmp_path, monkeypatch, quantity):
    repo, context = _scope(tmp_path, monkeypatch)
    apply_trade_attribution(repo, **_args(repo, context))
    _persist(repo, _execution(key="late-call", option="call", side="buy", quantity=quantity, strike=110, occurred=FILL+1000))
    row = next(row for row in _view(repo, context)["rows"] if row["open_event_id"]=="fill")
    assert row["ordinary_basis"] is None
    assert row["status"] == "pending" and row["selected_candidate_id"] is None
    assert row["origin"] == "rule"
    assert not any(reason=="manual_ordinary_preserved" for reason in row["reason_codes"])


def test_manual_ordinary_stays_protected_against_late_counterpart(tmp_path, monkeypatch):
    repo, context = _scope(tmp_path, monkeypatch)
    apply_trade_attribution(repo, **_args(repo, context), manual=True)
    _persist(repo, _execution(key="late-call", option="call", side="buy", strike=110, occurred=FILL+1000))
    row = next(row for row in _view(repo, context)["rows"] if row["open_event_id"]=="fill")
    assert row["status"] == "ordinary" and row["origin"] == "manual"
    assert "manual_ordinary_preserved" in row["reason_codes"]
    with pytest.raises(ValueError, match="manually excluded"):
        assert_trade_attribution_unclaimed(repo.list_trade_events(), ["lot-fill"])


@pytest.mark.parametrize("failure", ["stale", "cancel"])
def test_final_check_rolls_back_after_provisional_write(tmp_path, monkeypatch, failure):
    repo, context = _scope(tmp_path, monkeypatch)
    args = _args(repo, context)
    stop = Event()
    calls = []
    def fail():
        calls.append(1)
        if len(calls)==2:
            if failure=="stale": monkeypatch.setattr(attribution.time, "time", lambda: (NOW+60001)/1000)
            else: stop.set()
    before = read_trade_attribution_snapshot(repo, account="lx", market="us")
    with pytest.raises(ValueError): apply_trade_attribution(repo, **args, stop_event=stop, before_commit=fail)
    assert len(calls)==2
    assert read_trade_attribution_snapshot(repo, account="lx", market="us") == before


def test_periodic_reconciliation_writes_one_ordinary_decision(tmp_path, monkeypatch):
    repo, context = _scope(tmp_path, monkeypatch)
    observations = []
    def observe(**kwargs):
        observations.append(kwargs)
        return deepcopy(context["capacity_observation"])
    monkeypatch.setattr("src.application.wheel.capacity.observe_trade_attribution_capacity", observe)
    monkeypatch.setattr(attribution, "read_attribution_combo_evidence", lambda *a,**kw: context["combo_evidence"])
    monkeypatch.setattr("src.application.trades.inbox.cache_trade_attribution_result", lambda *a,**kw: 0)
    args = dict(config=context["config"], account="lx", market="us", runtime_root=tmp_path,
                inbox_path=tmp_path/"inbox.sqlite3", combo_mode="confirm")
    first = attribution.reconcile_trade_attribution_account(repo, **args)
    assert first["ordinary"] == 1 and first["linked"] == 0 and not first["errors"], first
    second = attribution.reconcile_trade_attribution_account(repo, **args)
    assert second["ordinary"] == second["linked"] == 0 and not second["errors"]
    assert len(repo.list_trade_events())==2
    assert observations[0]["ordinary_history_start_ms"] <= FILL
    assert "ordinary_history_start_ms" not in observations[1]


def _add_window_execution(repo, context, execution, *, snapshot_quantity=None):
    _persist(repo, execution)
    observed = context["capacity_observation"]
    evidence = observed["ordinary_evidence"]
    evidence["executions"].append(execution)
    account = evidence["history"]["account_results"][0]
    account["row_count"] = account["accepted_row_count"] = len(evidence["executions"])
    for name in ("current_orders", "history_orders"):
        evidence[name]["rows"].append({"order_id": execution["external_order_id"], "status":"terminal_with_fill",
            "dealt_qty":execution["quantity"], "created_at_ms":FILL})
    if snapshot_quantity is not None:
        rows = observed["portfolio"]["position_snapshot_input"]["rows"]
        call = deepcopy(rows[0])
        call["instrument_ref"].update(option_type="call", strike=execution["instrument_ref"]["strike"])
        call.update(position_side="long", quantity=str(snapshot_quantity))
        rows.append(call)


def test_late_complete_pair_can_replace_rule_ordinary_automatically(tmp_path, monkeypatch):
    repo, context = _scope(tmp_path, monkeypatch)
    apply_trade_attribution(repo, **_args(repo, context))
    monkeypatch.setattr(attribution.time, "time", lambda: (NOW+1)/1000)
    _add_window_execution(repo, context, _execution(key="late", option="call", side="buy", strike=110, occurred=FILL+1000),
                          snapshot_quantity=1)
    row = next(row for row in _view(repo, context)["rows"] if row["open_event_id"]=="fill")
    assert row["status"] == "pending" and row["selected_candidate_id"].startswith("combo:"), row
    applied = apply_trade_attribution(repo, **{**context, "execution_key":row["execution_key"],
        "candidate_id":row["selected_candidate_id"], "expected_input_hash":row["input_hash"],
        "request_id":"late-pair", "actor":"trade_intake:attribution_rule"})
    assert applied["status"]=="linked" and applied["strategy"]=="combo_yield"
    assert {row["status"] for row in _view(repo, context)["rows"]}=={"linked"}
    assert len([event for event in repo.list_trade_events() if event["event_type"]=="adjust"])==3


@pytest.mark.parametrize("multiple", [False, True])
def test_asymmetric_or_multiple_actual_pairs_remain_pending_with_complete_evidence(tmp_path, monkeypatch, multiple):
    repo, context = _scope(tmp_path, monkeypatch)
    _add_window_execution(repo, context, _execution(key="asymmetric", option="call", side="buy", strike=110, quantity=1 if multiple else 2),
                          snapshot_quantity=1 if multiple else 2)
    if multiple:
        _add_window_execution(repo, context, _execution(key="second-call", option="call", side="buy", strike=115),
                              snapshot_quantity=1)
    row = next(row for row in _view(repo, context)["rows"] if row["open_event_id"]=="fill")
    assert row["selected_candidate_id"] is None and row["ordinary_basis"] is None
    before = repo.list_trade_events()
    with pytest.raises(ValueError): apply_trade_attribution(repo, **_args(repo, context))
    assert repo.list_trade_events()==before


def test_closed_counterpart_still_prevents_fallback(tmp_path, monkeypatch):
    repo, context = _scope(tmp_path, monkeypatch)
    opening = _execution(key="closed-call", option="call", side="buy", strike=110)
    _add_window_execution(repo, context, opening)
    closing = deepcopy(opening)
    closing.update(external_execution_id="close-call", external_order_id="order-close-call", side="sell", position_effect="close")
    evidence = context["capacity_observation"]["ordinary_evidence"]
    evidence["executions"].append(closing)
    account = evidence["history"]["account_results"][0]
    account["row_count"] = account["accepted_row_count"] = len(evidence["executions"])
    for name in ("current_orders", "history_orders"):
        evidence[name]["rows"].append({"order_id":"order-close-call", "status":"terminal_with_fill", "dealt_qty":"1", "created_at_ms":FILL})
    persist_trade_event_object(repo, TradeEvent(event_id="close-call", event_type="close", event_time_ms=FILL,
        contract_key=ContractKey.from_values(broker="futu", account="lx", underlying_symbol="NVDA",
            option_type="call", strike=110, expiration_ymd="2026-08-21"), contracts=1, price=2, currency="USD", multiplier=100,
        source="futu", target_lot_id="lot-closed-call", raw_payload={"side":"sell", "execution_input":closing,
            "execution_id":execution_identity_from_input(closing)}))
    row = next(row for row in _view(repo, context)["rows"] if row["open_event_id"]=="fill")
    assert row["status"]=="pending" and row["ordinary_basis"] is None


def test_uncovered_short_call_and_long_call_cannot_auto_become_cc(tmp_path, monkeypatch):
    repo, context = _scope(tmp_path, monkeypatch, option="call")
    context["capacity_observation"]["portfolio"]["position_snapshot_input"]["rows"].pop()
    row, = _view(repo, context)["rows"]
    assert row["selected_candidate_id"] is None
    with pytest.raises(ValueError): apply_trade_attribution(repo, **_args(repo, context))
    _persist(repo, _execution(key="long-call", option="call", side="buy"))
    long = next(row for row in _view(repo, context)["rows"] if row["open_event_id"]=="long-call")
    assert long["selected_candidate_id"] is None and long["ordinary_basis"] is None


@pytest.mark.parametrize("history_complete", [False, True])
def test_durable_rule_ordinary_stays_ordinary_in_read_only_views(tmp_path, monkeypatch, history_complete):
    repo, context = _scope(tmp_path, monkeypatch)
    apply_trade_attribution(repo, **_args(repo, context))
    read_context = {**context, "capacity_observation": None,
        "combo_evidence":{**context["combo_evidence"], "complete":history_complete}}
    before = repo.list_trade_events()
    row, = _view(repo, read_context)["rows"]
    assert row["status"]=="ordinary" and row["origin"]=="rule"
    assert row["selected_candidate_id"] is None
    assert "existing_ordinary_preserved" in row["reason_codes"]
    from src.application import daily_decision_brief_service as brief
    monkeypatch.setattr(brief, "resolve_position_ledger_sqlite_path", lambda **kwargs: repo.db_path)
    monkeypatch.setattr(attribution, "read_attribution_combo_evidence", lambda *args, **kwargs: read_context["combo_evidence"])
    pending, error = brief._pending_attribution_for_brief(base=tmp_path, config=context["config"],
        account="lx", market="us", now_ms=NOW)
    assert error is None and pending==[]
    assert repo.list_trade_events()==before
    _persist(repo, _execution(key="late-unmatched", option="call", side="buy", quantity=2, strike=110))
    read_context["combo_evidence"]={"complete":False, "exposures":[]}
    row = next(row for row in _view(repo, read_context)["rows"] if row["open_event_id"]=="fill")
    assert row["status"]=="pending" and row["selected_candidate_id"] is None
