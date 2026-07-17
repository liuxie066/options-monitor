from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from domain.domain.ledger import ContractKey, TradeEvent
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.performance.service import build_option_period_performance


TZ = ZoneInfo("Asia/Shanghai")
NOW_MS = int(datetime(2026, 7, 17, 12, 0, tzinfo=TZ).timestamp() * 1000)


def _ms(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=TZ).timestamp() * 1000)


def _event(event_id: str, *, account: str, symbol: str, price: float) -> TradeEvent:
    key = ContractKey.from_values(
        broker="futu",
        account=account,
        underlying_symbol=symbol,
        option_type="put",
        position_side="short",
        strike=100,
        expiration_ymd="2026-08-21",
    )
    return TradeEvent(
        event_id=event_id,
        event_type="open",
        event_time_ms=_ms("2026-05-03T10:00:00"),
        contract_key=key,
        contracts=1,
        price=price,
        currency="USD",
        source="test",
        multiplier=100,
        fees=0,
        lot_id=f"lot-{event_id}",
        raw_payload={"fee_provenance": {"basis": "actual", "source": "test"}},
    )


def test_service_loads_only_through_ledger_api_filters_scope_and_does_not_write(tmp_path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "performance.sqlite3")
    repo.upsert_trade_event(_event("lx-open", account="lx", symbol="NVDA", price=2))
    repo.upsert_trade_event(_event("sy-open", account="sy", symbol="AAPL", price=1))
    before = repo.list_trade_events()

    report = build_option_period_performance(
        repo,
        period={"period": "month", "month": "2026-05"},
        account="LX",
        broker="futu",
        now_ms=NOW_MS,
        include_rows=False,
    )

    assert report["schema_version"] == "option_period_performance.core.v1"
    assert report["scope"]["account"] == "lx"
    assert report["scope"]["broker"] == "富途"
    assert report["scope"]["accounts"] == ["lx"]
    assert report["scope"]["symbols"] == ["NVDA"]
    assert report["activity"]["premium_collected_gross"]["by_currency"] == {"USD": 200.0}
    assert "rows" not in report
    assert repo.list_trade_events() == before
