from datetime import date, datetime, timezone
from types import SimpleNamespace
import pytest

from domain.domain.expiration_dates import expiration_market_date
from src.application.ledger.read_model import build_position_lot_view, list_position_rows
from src.application.positions.context_builder import build_context
from src.application.agent_tools import close_advice_read_impl as close_read
from src.application.bot.control.position_query import PositionQuery, PositionExpirationQuery

ANCHOR = datetime(2026, 10, 9, 17, tzinfo=timezone.utc)


def lot(symbol):
    return {"lot_id": symbol, "fields": {"broker": "富途", "account": "lx", "symbol": symbol,
        "option_type": "put", "side": "short", "status": "open", "contracts": 1,
        "contracts_open": 1, "multiplier": 100, "strike": 100, "premium": 1,
        "opened_at": 1, "currency": "HKD" if symbol.endswith(".HK") else "USD",
        "expiration_ymd": "2026-10-09"}}


def test_market_date_requires_aware_time_and_preserves_unknown():
    assert expiration_market_date(ANCHOR, "US") == date(2026, 10, 9)
    assert expiration_market_date(ANCHOR, "HK") == date(2026, 10, 10)
    assert expiration_market_date(ANCHOR, "unknown") is None
    with pytest.raises(ValueError, match="timezone-aware"):
        expiration_market_date(ANCHOR.replace(tzinfo=None), "US")


@pytest.mark.parametrize("symbol,dte", [("NVDA", 0), ("0700.HK", -1), ("???", None)])
def test_position_dte_market_anchor(symbol, dte):
    assert build_position_lot_view(lot(symbol), as_of_utc=ANCHOR)["days_to_expiration"] == dte
    assert build_position_lot_view(lot(symbol), as_of_date=date(2026, 10, 8))["days_to_expiration"] == 1


def test_position_list_and_context_share_market_dates(monkeypatch):
    from src.application.ledger import read_model
    records = [lot("NVDA"), lot("0700.HK")]
    monkeypatch.setattr(read_model, "load_canonical_position_lot_records", lambda repo: records)
    rows = list_position_rows(None, broker="富途", account="lx", as_of_ms=int(ANCHOR.timestamp()*1000))
    assert {r["symbol"]: r["days_to_expiration"] for r in rows} == {"NVDA": 0, "0700.HK": -1}
    eligible = list_position_rows(None, broker="富途", account="lx", as_of_ms=int(ANCHOR.timestamp()*1000), expiration_within_days=0)
    assert [r["symbol"] for r in eligible] == ["NVDA"]
    ctx = build_context(records, broker="富途", account="lx", rates={"USDCNY": 7, "HKDCNY": 1}, observed_at=ANCHOR)
    assert {r["symbol"]: r["days_to_expiration"] for r in ctx["open_positions_min"]} == {"NVDA": 0, "0700.HK": -1}


def test_close_advice_relative_expiration_uses_each_market():
    query = PositionQuery(expiration=PositionExpirationQuery(within_days=0))
    assert close_read._matches({"symbol": "NVDA", "expiration": "2026-10-09"}, query, now_utc=ANCHOR)
    assert not close_read._matches({"symbol": "0700.HK", "expiration": "2026-10-09"}, query, now_utc=ANCHOR)
    assert not close_read._matches({"symbol": "???", "expiration": "2026-10-09"}, query, now_utc=ANCHOR)
    assert not close_read._matches({"symbol": "NVDA", "expiration": "bad"}, query, now_utc=ANCHOR)


def test_close_read_entry_uses_one_anchor_across_markets_and_host_zones(monkeypatch, tmp_path):
    import os
    import time
    class Clock(datetime):
        calls = 0
        @classmethod
        def now(cls, tz=None):
            cls.calls += 1
            return ANCHOR
    monkeypatch.setattr(close_read, "datetime", Clock)
    monkeypatch.setattr(close_read, "assert_quality_allows", lambda *args, **kwargs: None)
    monkeypatch.setattr(close_read, "_resolve_sources", lambda *args, **kwargs: [SimpleNamespace(generated_at_utc=ANCHOR.isoformat(), run_id=None, path=tmp_path / "synthetic.csv")])
    monkeypatch.setattr(close_read, "_read_rows", lambda source: [
        {"symbol": "NVDA", "expiration": "2026-10-09", "account": "lx"},
        {"symbol": "0700.HK", "expiration": "2026-10-09", "account": "lx"},
    ])
    monkeypatch.setattr(close_read, "_source_payload", lambda *args, **kwargs: {})
    previous = os.environ.get("TZ")
    try:
        for zone in ("Asia/Shanghai", "America/Los_Angeles", "UTC"):
            monkeypatch.setenv("TZ", zone)
            time.tzset()
            Clock.calls = 0
            data, _, _ = close_read.close_advice_read_tool(
                {"account": "lx", "expiration": {"within_days": 0}, "market": "all"},
                load_runtime_config=lambda **kwargs: pytest.fail("no config needed"),
                resolve_output_root=lambda value: tmp_path,
                repo_base=lambda: tmp_path, mask_path=lambda value: "synthetic",
            )
            assert [r["symbol"] for r in data["rows"]] == ["NVDA"]
            assert Clock.calls == 1
    finally:
        if previous is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", previous)
        time.tzset()


def test_market_date_handles_new_york_dst_winter_boundary():
    # In winter 04:30 UTC is still the prior day in New York.
    instant = datetime(2026, 12, 5, 4, 30, tzinfo=timezone.utc)
    assert expiration_market_date(instant, "US") == date(2026, 12, 4)
    assert expiration_market_date(instant, "HK") == date(2026, 12, 5)
