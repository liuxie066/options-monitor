from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from domain.domain.ledger import ContractKey, TradeEvent
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.writer import persist_trade_event_object
from src.application.performance.service import build_option_period_performance
from src.application.trades.normalizer import NormalizedTradeDeal
from src.application.trades.resolver import resolve_trade_deal


TZ = ZoneInfo("Asia/Shanghai")
NOW_MS = int(datetime(2026, 7, 17, 12, 0, tzinfo=TZ).timestamp() * 1000)


def _ms(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=TZ).timestamp() * 1000)


def _snapshot(role: str) -> dict:
    return {
        "strategy": "combo_yield",
        "leg_role": role,
        "strategy_group_id": "combo_yield:lx:pair-1",
        "expiry_structure": "same_expiry",
    }


def _open_deal(
    *,
    role: str,
    option_type: str,
    side: str,
    strike: float,
    price: float,
    deal_id: str,
    order_id: str,
    event_time: str,
) -> NormalizedTradeDeal:
    return NormalizedTradeDeal(
        broker="富途",
        futu_account_id="REAL_1",
        internal_account="lx",
        deal_id=deal_id,
        order_id=order_id,
        symbol="NVDA",
        option_type=option_type,
        side=side,
        position_effect="open",
        contracts=1,
        price=price,
        strike=strike,
        multiplier=100,
        multiplier_source="cache",
        expiration_ymd="2026-05-31",
        currency="USD",
        trade_time_ms=_ms(event_time),
        raw_payload={
            "fee_provenance": {"basis": "actual", "amount": 0, "source": "test"},
            "strategy_snapshot": _snapshot(role),
        },
    )


def _close_event(
    *,
    event_id: str,
    event_type: str,
    event_time: str,
    role: str,
    option_type: str,
    position_side: str,
    strike: float,
    price: float,
    target_lot_id: str,
) -> TradeEvent:
    return TradeEvent(
        event_id=event_id,
        event_type=event_type,
        event_time_ms=_ms(event_time),
        contract_key=ContractKey.from_values(
            broker="futu",
            account="lx",
            underlying_symbol="NVDA",
            option_type=option_type,
            position_side=position_side,
            strike=strike,
            expiration_ymd="2026-05-31",
        ),
        contracts=1,
        price=price,
        currency="USD",
        source="test",
        multiplier=100,
        fees=0,
        target_lot_id=target_lot_id,
        raw_payload={
            "fee_provenance": {"basis": "actual", "amount": 0, "source": "test"},
            "strategy_snapshot": _snapshot(role),
        },
    )


def _lot_record_id(lots: list[dict], *, option_type: str, side: str, strike: float) -> str:
    for lot in lots:
        fields = lot["fields"]
        if (
            str(fields.get("option_type") or "") == option_type
            and str(fields.get("side") or "") == side
            and float(fields.get("strike") or 0.0) == strike
        ):
            return str(lot["record_id"])
    raise AssertionError(f"lot not found: option_type={option_type} side={side} strike={strike}")


def test_resolver_open_to_performance_attribution_round_trip(tmp_path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "perf-e2e.sqlite3")

    put_open = _open_deal(
        role="funding_put",
        option_type="put",
        side="sell",
        strike=100.0,
        price=5.0,
        deal_id="put-open",
        order_id="order-put-open",
        event_time="2026-05-01T00:00:00",
    )
    call_open = _open_deal(
        role="participation_call",
        option_type="call",
        side="buy",
        strike=120.0,
        price=4.0,
        deal_id="call-open",
        order_id="order-call-open",
        event_time="2026-05-01T00:00:00",
    )

    put_result = resolve_trade_deal(put_open, repo=repo, state={}, apply_changes=True)
    assert put_result.status == "applied"
    assert put_result.action == "open"
    assert put_result.reason == "applied_open"

    call_result = resolve_trade_deal(call_open, repo=repo, state={}, apply_changes=True)
    assert call_result.status == "applied"
    assert call_result.action == "open"
    assert call_result.reason == "applied_open"

    lots = repo.list_position_lots()
    assert len(lots) == 2
    put_lot_id = _lot_record_id(lots, option_type="put", side="short", strike=100.0)
    call_lot_id = _lot_record_id(lots, option_type="call", side="long", strike=120.0)

    put_expire = _close_event(
        event_id="put-expire",
        event_type="expire_close",
        event_time="2026-05-10T00:00:00",
        role="funding_put",
        option_type="put",
        position_side="short",
        strike=100.0,
        price=0.0,
        target_lot_id=put_lot_id,
    )
    call_close = _close_event(
        event_id="call-close",
        event_type="close",
        event_time="2026-05-20T00:00:00",
        role="participation_call",
        option_type="call",
        position_side="long",
        strike=120.0,
        price=7.0,
        target_lot_id=call_lot_id,
    )

    put_write = persist_trade_event_object(repo, put_expire)
    assert put_write.created is True
    call_write = persist_trade_event_object(repo, call_close)
    assert call_write.created is True

    stored_events = repo.list_trade_events()
    assert len(stored_events) == 4
    snapshots = {
        str(item["event_id"]): (item.get("raw_payload") or {}).get("strategy_snapshot")
        for item in stored_events
    }
    assert snapshots["futu:lx:REAL_1:put-open"] == _snapshot("funding_put")
    assert snapshots["futu:lx:REAL_1:call-open"] == _snapshot("participation_call")
    assert snapshots["put-expire"] == _snapshot("funding_put")
    assert snapshots["call-close"] == _snapshot("participation_call")

    report = build_option_period_performance(
        repo,
        period={"period": "month", "month": "2026-05"},
        account="lx",
        broker="futu",
        now_ms=NOW_MS,
        include_rows=False,
    )

    attribution = report["attribution"]
    assert len(attribution["groups"]) == 1
    group = attribution["groups"][0]

    assert group["funding"]["put_open_credit_gross"] == 500.0
    assert group["funding"]["call_open_debit_gross"] == 400.0
    assert group["funding"]["call_cost_funded_by_put"] == 400.0
    assert group["funding"]["funding_surplus"] == 100.0
    assert group["funding_cycles"][0]["pnl"]["realized_gross"]["by_currency"] == {"USD": 500.0}
    assert group["participation_lifecycles"][0]["pnl"]["realized_gross"]["by_currency"] == {"USD": 300.0}
    assert group["pnl"]["realized_gross"]["by_currency"] == {"USD": 800.0}
    assert report["pnl"]["realized_gross"]["by_currency"] == {"USD": 800.0}
    assert attribution["conservation"]["realized_gross"]["residual_by_currency"] == {"USD": 0.0}
