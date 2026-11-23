"""Synthetic FUTU scenarios using the reported historical arrival gaps, not broker exports."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.application.ledger import api
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from domain.domain.ledger import ContractKey, TradeEvent
from src.application.ledger.writer import persist_trade_event_object
from src.application.trades.close_reason_evidence import build_lifecycle_timing_policy
from src.application.trades.lifecycle_reconciliation import lifecycle_case_read_model
from src.application.trades.lifecycle_runtime import reconcile_due_lifecycle_cases_for_source
from src.application.trades.resolver import resolve_trade_deal
from src.application.trades.settlement_observation import build_settlement_observation_collector
from tests.test_trades_resolver_close import _deal
from tests.test_settlement_observation import _Gateway, _IncompleteGateway, _UntypedFailedReceiptGateway


def _ms(iso):
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


EVENT_MS = _ms("2026-11-20T21:00:00+00:00")
RECEIVED_MS = EVENT_MS + 10_000


def _pending(repo, *, account="sy", option_type="call", strike=117.45):
    if option_type == "call":
        api.record_manual_position_open(
            repo, broker="富途", account=account, symbol="FUTU", option_type="put", side="short",
            contracts=1, currency="USD", strike=100, multiplier=100, expiration_ymd="2026-11-13",
            premium_per_share=2, opened_at_ms=EVENT_MS - 14 * 86400_000, request_id=f"stock-origin-{account}",
        )
        lot = next(row for row in repo.list_position_lots()
                   if row["fields"]["contract_key"]["account"] == account
                   and row["fields"]["contract_key"]["option_type"] == "put")
        persist_trade_event_object(repo, TradeEvent(
            event_id=f"stock-origin-assignment-{account}", event_type="assignment",
            event_time_ms=EVENT_MS - 7 * 86400_000, target_lot_id=lot["record_id"],
            contract_key=ContractKey.from_values(broker="富途", account=account, underlying_symbol="FUTU",
                                               option_type="put", strike=100, expiration_ymd="2026-11-13"),
            contracts=1, price=0, currency="USD", source="test", multiplier=100,
            raw_payload={"side": "buy", "target_lot_id": lot["record_id"], "stock_settlement": {
                "side": "buy", "shares": 100, "price": 100, "fees": 0, "currency": "USD",
                "fee_provenance": {"basis": "actual", "source": "test"},
            }},
        ))
    api.record_manual_position_open(
        repo, broker="富途", account=account, symbol="FUTU", option_type=option_type, side="short",
        contracts=1, currency="USD", strike=strike, multiplier=100, expiration_ymd="2026-11-20",
        premium_per_share=2, opened_at_ms=EVENT_MS - 7 * 86400_000, request_id=f"open-{account}-{strike}",
    )
    code = f"US.FUTU261120{option_type[0].upper()}{round(strike * 1000)}"
    deal = _deal(
        internal_account=account, futu_account_id=f"TEST-{account}", deal_id=f"zero-{account}-{strike}",
        order_id=f"order-{account}-{strike}", symbol="FUTU", option_type=option_type, strike=strike,
        contracts=1, price=0, currency="USD", expiration_ymd="2026-11-20", trade_time_ms=EVENT_MS,
        raw_payload={"code": code, "remark": "", "_trade_intake_source": {
            "received_at_utc": datetime.fromtimestamp(RECEIVED_MS / 1000, timezone.utc).isoformat(),
        }},
    )
    result = resolve_trade_deal(deal, repo=repo, state={}, apply_changes=True)
    assert result.status == "applied", result
    case = next(row for row in repo.list_trade_lifecycle_cases(account=account) if row["strike"] == strike)
    policy = build_lifecycle_timing_policy(
        case_id=case["case_id"], market="US", expiration_ymd="2026-11-20",
        contract_metadata={"settlement_style": "physical", "underlying_security_type": "equity",
                           "last_trade_cutoff_ms": EVENT_MS, "last_trade_cutoff_source": "instrument_policy_registry"},
        trading_days=[{"date": "2026-11-20", "type": "WHOLE"}, {"date": "2026-11-23", "type": "WHOLE"}],
        calendar_source="fixture", calendar_observed_at_ms=EVENT_MS,
    )
    repo.insert_trade_lifecycle_timing_policy_once(policy)
    return case, deal, policy


def _gateway(deal, cls=_Gateway):
    gateway = cls(history_deals=[{"deal_id": deal.deal_id, "acc_id": deal.futu_account_id,
                                 "code": deal.raw_payload["code"], "price": "0", "qty": 1}])
    gateway.history_order_rows = [{"order_id": deal.order_id, "is_broker_auto": True}]
    gateway.calendar_rows = [{"date": "2026-11-20", "type": "WHOLE"}, {"date": "2026-11-23", "type": "WHOLE"}]
    return gateway


@pytest.mark.parametrize("gateway_cls", [_Gateway, _IncompleteGateway, _UntypedFailedReceiptGateway])
def test_sy_call_rechecks_within_two_hours_without_releasing_shares(tmp_path, gateway_cls):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    case, deal, policy = _pending(repo)
    _pending(repo, account="lx", strike=113)
    now = RECEIVED_MS + 16 * 60_000
    manifest = api.build_current_decision_projection_migration_inventory(repo.db_path, now_ms=now)
    api.apply_current_decision_projection_migration(repo.db_path, manifest)
    gateway = _gateway(deal, gateway_cls)
    gateway.history_deal_rows.extend([
        {"deal_id": "lx-delivery", "acc_id": "TEST-lx", "code": "US.FUTU", "price": price,
         "qty": 100, "trd_side": "SELL", "trade_time_ms": EVENT_MS + 550, "order_id": "lx-stock"}
        for price in (113, 117.45)
    ])
    gateway.history_order_rows.append({"order_id": "lx-stock", "is_broker_auto": True})
    source = {"id": "sy", "account": "sy", "futu_account_ids": ["TEST-sy"],
              "inbox_path": repo.db_path.with_name(repo.db_path.name + ".trade_intake_inbox.sqlite3")}
    seals = []

    def run(at_ms):
        # New repository and collector emulate process restart; the receipt stays in SQLite.
        restarted = SQLiteOptionPositionsRepository(repo.db_path)
        collector = build_settlement_observation_collector(
            repo=restarted, gateway=gateway, futu_account_id="TEST-sy", now_ms_fn=lambda: at_ms, source_id="sy",
        )
        return reconcile_due_lifecycle_cases_for_source(
            restarted, source=source, now_ms=at_ms, apply_changes=True,
            settlement_collector=collector, settlement_control_now_ms_fn=lambda: at_ms,
            seal_sink=seals.append,
        )

    before = repo.list_trade_events()
    assert run(now)["provider_attempt_count"] == 1
    audits = repo.list_trade_lifecycle_attempt_audits(case_id=case["case_id"])
    assert audits and RECEIVED_MS <= audits[0]["attempted_at_ms"] <= RECEIVED_MS + 2 * 3600_000
    assert run(now)["provider_attempt_count"] == 0
    run(RECEIVED_MS + 2 * 3600_000)
    model = lifecycle_case_read_model(repo, case_id=case["case_id"], now_ms=now)
    assert model["first_option_close_received_at_ms"] == RECEIVED_MS
    assert model["reason_state"] == "cause_pending"
    assert repo.list_trade_events() == before
    assert not repo.list_trade_lifecycle_attempt_audits(case_id=repo.list_trade_lifecycle_cases(account="lx")[0]["case_id"])
    _, context = api.read_wheel_capacity_context(repo, account="sy", broker="富途", as_of_ms=RECEIVED_MS + 2 * 3600_000)
    assert context["locked_shares_unavailable_by_symbol"] == {"FUTU": "option_close_settlement_pending"}, str(context)
    # Re-delivery after restart cannot move the first receipt or duplicate the close.
    replay = replace(deal, raw_payload={**deal.raw_payload, "_trade_intake_source": {
        "received_at_utc": datetime.fromtimestamp((now + 1) / 1000, timezone.utc).isoformat(),
    }})
    assert resolve_trade_deal(replay, repo=repo, state={}, apply_changes=True).status == "skipped"
    assert lifecycle_case_read_model(repo, case_id=case["case_id"], now_ms=now)["first_option_close_received_at_ms"] == RECEIVED_MS
    assert repo.list_trade_events() == before
    assert now < policy["settlement_deadline_ms"]
    # Native Futu stock history lacks a verifiable automatic-delivery origin.
    # Even matching economics must not manufacture proof after failed or empty polls.
    gateway = _gateway(deal)
    gateway.history_deal_rows.append({
        "deal_id": "late-sy-stock", "acc_id": "TEST-sy", "code": "US.FUTU", "price": "117.45",
        "qty": 100, "trd_side": "SELL", "trade_time_ms": EVENT_MS + 550, "order_id": "late-sy-stock-order",
    })
    gateway.history_order_rows.append({
        "order_id": "late-sy-stock-order", "code": "US.FUTU", "trd_side": "SELL",
        "order_type": "NORMAL", "order_status": "FILLED_ALL", "qty": 100,
        "dealt_qty": 100, "price": 117.45, "dealt_avg_price": 117.45, "remark": "",
    })
    run(RECEIVED_MS + 3 * 3600_000)
    assert repo.list_trade_events() == before
    assert lifecycle_case_read_model(repo, case_id=case["case_id"], now_ms=RECEIVED_MS + 3 * 3600_000)["reason_state"] == "cause_pending"
    late_result = run(RECEIVED_MS + 4 * 3600_000)
    assert late_result["provider_attempt_count"] == 1
    assert lifecycle_case_read_model(repo, case_id=case["case_id"], now_ms=RECEIVED_MS + 4 * 3600_000)["reason_state"] == "cause_pending"
    assert repo.list_trade_events() == before
    lx_case = repo.list_trade_lifecycle_cases(account="lx")[0]
    assert lifecycle_case_read_model(repo, case_id=lx_case["case_id"], now_ms=RECEIVED_MS + 4 * 3600_000)["reason_state"] == "cause_pending"
    events = repo.list_trade_events()
    notices = repo.list_trade_lifecycle_notifications(case_id=case["case_id"])
    assert run(RECEIVED_MS + 4 * 3600_000)["provider_attempt_count"] == 0
    assert repo.list_trade_events() == events
    assert repo.list_trade_lifecycle_notifications(case_id=case["case_id"]) == notices


@pytest.mark.parametrize("arrival_gap_ms", [40_000, 53_000, 61 * 60_000, 76 * 60_000])
def test_futu_put_positive_evidence_resolves_before_deadline_once(tmp_path, monkeypatch, arrival_gap_ms):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    case, deal, policy = _pending(repo, option_type="put", strike=97.45)
    observed = RECEIVED_MS + arrival_gap_ms
    monkeypatch.setattr("src.application.ledger.repository_lifecycle_cases.now_ms", lambda: observed)
    monkeypatch.setattr("src.application.ledger.repository_trade_events.now_ms", lambda: observed)
    stock = replace(deal, deal_id="stock-sy", order_id="stock-order", option_type=None, position_effect=None,
                    contracts=100, price=97.45, strike=None, multiplier=None, expiration_ymd=None,
                    trade_time_ms=EVENT_MS + 550, raw_payload={"code": "US.FUTU"})
    result = resolve_trade_deal(stock, repo=repo, state={}, apply_changes=True)
    assert result.status == "applied", result
    assert observed < policy["settlement_deadline_ms"]
    assert lifecycle_case_read_model(repo, case_id=case["case_id"], now_ms=observed)["close_reason"] == "assignment"
    events = repo.list_trade_events()
    assert len([row for row in events if row["event_type"] == "assignment"]) == 1
    notifications = repo.list_trade_lifecycle_notifications(case_id=case["case_id"])
    allocations = repo.list_trade_lifecycle_allocations(case_id=case["case_id"])
    with repo._connect() as conn:
        stock_times = conn.execute("SELECT created_at_ms FROM trade_lifecycle_evidence WHERE evidence_type='stock_settlement_leg'").fetchall()
        assignment = next(row for row in events if row["event_type"] == "assignment")
        finalized_at = conn.execute("SELECT created_at_ms FROM trade_events WHERE event_id=?", (assignment["event_id"],)).fetchone()[0]
    assert len(stock_times) == 1 and stock_times[0][0] == observed
    assert finalized_at == observed
    anchor = next(row for row in repo.list_trade_lifecycle_evidence(case_id=case["case_id"])
                  if row["evidence_type"] == "option_zero_price_close")
    assert (anchor["event_time_ms"], anchor["received_at_ms"]) == (EVENT_MS, RECEIVED_MS)
    restarted = SQLiteOptionPositionsRepository(repo.db_path)
    for repeated in (stock, deal):
        assert resolve_trade_deal(repeated, repo=restarted, state={}, apply_changes=True).status == "skipped"
    assert restarted.list_trade_events() == events
    assert restarted.list_trade_lifecycle_allocations(case_id=case["case_id"]) == allocations
    assert restarted.list_trade_lifecycle_notifications(case_id=case["case_id"]) == notifications


def test_early_assignment_poll_covers_previous_local_date(tmp_path, monkeypatch):
    monkeypatch.setattr(__name__ + ".EVENT_MS", _ms("2026-11-19T00:02:00-05:00"))
    monkeypatch.setattr(__name__ + ".RECEIVED_MS", EVENT_MS + 10_000)
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    case, deal, _ = _pending(repo, option_type="put", strike=97.45)
    gateway = _gateway(deal)
    gateway.history_deal_rows.append({
        "deal_id": "early-stock", "acc_id": "TEST-sy", "code": "US.FUTU", "price": "97.45",
        "qty": 100, "trd_side": "BUY", "trade_time_ms": EVENT_MS - 300_000, "order_id": "early-stock-order",
    })
    gateway.history_order_rows.append({"order_id": "early-stock-order", "is_broker_auto": True})
    now = RECEIVED_MS + 16 * 60_000
    collector = build_settlement_observation_collector(
        repo=repo, gateway=gateway, futu_account_id="TEST-sy", now_ms_fn=lambda: now, source_id="sy",
    )
    seals = []
    result = reconcile_due_lifecycle_cases_for_source(
        repo, source={"id": "sy", "account": "sy", "futu_account_ids": ["TEST-sy"],
                      "inbox_path": tmp_path / "inbox.sqlite3"},
        now_ms=now, apply_changes=True, settlement_collector=collector,
        settlement_control_now_ms_fn=lambda: now, seal_sink=seals.append,
    )
    assert result["provider_attempt_count"] == 1
    assert gateway.history_deal_queries[0]["start"] == "2026-11-18"
    assert lifecycle_case_read_model(repo, case_id=case["case_id"], now_ms=now)["close_reason"] == "assignment"
