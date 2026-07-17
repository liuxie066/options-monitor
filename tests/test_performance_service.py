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


def test_historical_service_uses_persisted_marks_and_never_invokes_live_collector(tmp_path) -> None:
    from domain.domain.performance.models import FXRateFact, OptionInstrumentKey, ValuationMarkFact
    from src.infrastructure.performance_evidence_sqlite import PerformanceEvidenceSQLiteRepository

    repo = SQLiteOptionPositionsRepository(tmp_path / "historical.sqlite3")
    repo.upsert_trade_event(_event("open", account="lx", symbol="NVDA", price=2))
    window = __import__("domain.domain.performance.period", fromlist=["normalize_period"]).normalize_period(
        {"period": "month", "month": "2026-05"}, now_ms=NOW_MS
    )
    instrument = OptionInstrumentKey(
        symbol="NVDA",
        option_type="put",
        strike="100",
        expiration_ymd="2026-08-21",
        currency="USD",
        multiplier="100",
    )
    evidence = PerformanceEvidenceSQLiteRepository(repo.db_path)
    evidence.import_envelope(
        {
            "schema_version": "option_performance_evidence.v1",
            "valuation_marks": [
                ValuationMarkFact(
                    fact_id="end-mark",
                    instrument=instrument,
                    price="1",
                    mark_kind="official_close",
                    effective_at_ms=window.valuation_end_at_ms,
                    observed_at_ms=window.valuation_end_at_ms,
                    source="official_close",
                    source_id="end-mark",
                ).normalized_payload()
            ],
            "fx_rates": [
                FXRateFact(
                    fact_id="end-fx",
                    base_currency="USD",
                    quote_currency="CNY",
                    rate="7.2",
                    rate_kind="official_close",
                    effective_at_ms=window.valuation_end_at_ms,
                    observed_at_ms=window.valuation_end_at_ms,
                    source="official_close",
                    source_id="end-fx",
                ).normalized_payload()
            ],
        },
        apply=True,
        migrated_at_ms=NOW_MS,
    )

    def forbidden_collector(**_kwargs):
        raise AssertionError("historical report must not invoke live collector")

    report = build_option_period_performance(
        repo,
        period=window,
        account="lx",
        now_ms=NOW_MS,
        evidence_repo=evidence,
        refresh_quotes=True,
        evidence_collector=forbidden_collector,
    )

    assert report["pnl"]["ending_unrealized_gross"]["by_currency"] == {"USD": 100.0}
    assert report["pnl"]["period_total_gross"]["by_currency"] == {"USD": 100.0}
    assert report["pnl"]["period_total_gross"]["cny"] == 720.0
    assert report["evidence"]["collection"]["status"] == "skipped_historical"
    assert report["evidence"]["schema_state"] == "initialized_v1"


def test_current_live_evidence_is_read_through_only_and_capture_replays_historically(tmp_path) -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from domain.domain.ledger import ContractKey, TradeEvent
    from domain.domain.performance.models import FXRateFact, OptionInstrumentKey, ValuationMarkFact
    from src.application.performance.evidence_collection import CurrentEvidenceCollection
    from src.infrastructure.performance_evidence_sqlite import PerformanceEvidenceSQLiteRepository

    tz = ZoneInfo("Asia/Shanghai")
    current_now = int(datetime(2026, 7, 17, 12, 0, tzinfo=tz).timestamp() * 1000)
    later_now = int(datetime(2026, 7, 20, 12, 0, tzinfo=tz).timestamp() * 1000)
    repo = SQLiteOptionPositionsRepository(tmp_path / "capture.sqlite3")
    key = ContractKey.from_values(
        broker="futu",
        account="lx",
        underlying_symbol="NVDA",
        option_type="put",
        position_side="short",
        strike=100,
        expiration_ymd="2026-08-21",
    )
    repo.upsert_trade_event(
        TradeEvent(
            event_id="jul-open",
            event_type="open",
            event_time_ms=int(datetime(2026, 7, 5, 10, 0, tzinfo=tz).timestamp() * 1000),
            contract_key=key,
            contracts=1,
            price=2,
            currency="USD",
            source="test",
            multiplier=100,
            fees=0,
            lot_id="lot-jul-open",
            raw_payload={"fee_provenance": {"basis": "actual", "source": "test"}},
        )
    )
    instrument = OptionInstrumentKey.from_contract_key(key, currency="USD", multiplier=100)
    mark = ValuationMarkFact(
        fact_id="captured-mark",
        instrument=instrument,
        price="1",
        mark_kind="midpoint",
        effective_at_ms=current_now,
        observed_at_ms=current_now,
        source="realtime_snapshot",
        source_id="captured-mark",
        quality={"persistence": "live_unpersisted"},
    )
    rate = FXRateFact(
        fact_id="captured-fx",
        base_currency="USD",
        quote_currency="CNY",
        rate="7.2",
        rate_kind="spot",
        effective_at_ms=current_now,
        observed_at_ms=current_now,
        source="realtime_snapshot",
        source_id="captured-fx",
        quality={"persistence": "live_unpersisted"},
    )
    calls = 0

    def collector(**_kwargs):
        nonlocal calls
        calls += 1
        return CurrentEvidenceCollection(status="collected", valuation_marks=(mark,), fx_rates=(rate,))

    evidence = PerformanceEvidenceSQLiteRepository(repo.db_path)
    current_report = build_option_period_performance(
        repo,
        period={"period": "mtd", "as_of_date": "2026-07-17"},
        account="lx",
        now_ms=current_now,
        evidence_repo=evidence,
        refresh_quotes=True,
        evidence_collector=collector,
    )

    assert calls == 1
    assert current_report["pnl"]["ending_unrealized_gross"]["by_currency"] == {"USD": 100.0}
    assert current_report["evidence"]["schema_state"] == "not_initialized"
    assert evidence.schema_state() == "not_initialized"

    evidence.import_envelope(
        CurrentEvidenceCollection(status="collected", valuation_marks=(mark,), fx_rates=(rate,)).envelope,
        apply=True,
        migrated_at_ms=current_now,
    )
    first = build_option_period_performance(
        repo,
        period={"period": "range", "start_date": "2026-07-01", "end_date": "2026-07-17"},
        account="lx",
        now_ms=later_now,
        evidence_repo=evidence,
        refresh_quotes=True,
        evidence_collector=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not call live")),
    )
    second = build_option_period_performance(
        repo,
        period={"period": "range", "start_date": "2026-07-01", "end_date": "2026-07-17"},
        account="lx",
        now_ms=later_now,
        evidence_repo=evidence,
        refresh_quotes=False,
    )

    assert first == second
    assert {"captured-mark", "captured-fx"}.issubset(set(first["quality"]["evidence_fact_ids"]))
    assert first["evidence"]["collection"]["status"] == "skipped_historical"


def test_boundary_valuation_restates_later_valid_voids(tmp_path) -> None:
    from src.application.performance.adapters import load_ledger_performance_inputs, load_option_valuation_inputs

    repo = SQLiteOptionPositionsRepository(tmp_path / "restated-boundary.sqlite3")
    open_event = _event("open-restated", account="lx", symbol="NVDA", price=2)
    repo.upsert_trade_event(open_event)
    repo.upsert_trade_event(
        TradeEvent(
            event_id="close-restated",
            event_type="close",
            event_time_ms=_ms("2026-05-20T10:00:00"),
            contract_key=open_event.contract_key,
            contracts=1,
            price=1,
            currency="USD",
            source="test",
            multiplier=100,
            fees=0,
            target_lot_id=open_event.lot_id,
            raw_payload={"fee_provenance": {"basis": "actual", "source": "test"}},
        )
    )
    repo.upsert_trade_event(
        TradeEvent(
            event_id="void-close-restated",
            event_type="void",
            event_time_ms=_ms("2026-07-10T10:00:00"),
            contract_key=open_event.contract_key,
            contracts=0,
            price=0,
            currency="USD",
            source="test",
            multiplier=100,
            fees=0,
            target_event_id="close-restated",
        )
    )

    inputs = load_ledger_performance_inputs(repo)
    boundary = load_option_valuation_inputs(inputs, as_of_ms=_ms("2026-06-01T00:00:00"), account="lx")

    assert [position.lot_id for position in boundary.positions] == [open_event.lot_id]
    assert boundary.positions[0].contracts_open == 1


def test_live_evidence_conflict_fails_closed_to_persisted_facts(tmp_path) -> None:
    from domain.domain.performance.models import FXRateFact, OptionInstrumentKey, ValuationMarkFact
    from src.application.performance.evidence_collection import CurrentEvidenceCollection
    from src.infrastructure.performance_evidence_sqlite import PerformanceEvidenceSQLiteRepository

    repo = SQLiteOptionPositionsRepository(tmp_path / "merge-conflict.sqlite3")
    repo.upsert_trade_event(_event("open-conflict", account="lx", symbol="NVDA", price=2))
    instrument = OptionInstrumentKey(
        symbol="NVDA",
        option_type="put",
        strike="100",
        expiration_ymd="2026-08-21",
        currency="USD",
        multiplier="100",
    )
    persisted_mark = ValuationMarkFact(
        fact_id="persisted-mark",
        instrument=instrument,
        price="1",
        mark_kind="midpoint",
        effective_at_ms=NOW_MS,
        observed_at_ms=NOW_MS,
        source="realtime_snapshot",
        source_id="same-source",
    )
    persisted_fx = FXRateFact(
        fact_id="persisted-fx",
        base_currency="USD",
        quote_currency="CNY",
        rate="7.2",
        rate_kind="spot",
        effective_at_ms=NOW_MS,
        observed_at_ms=NOW_MS,
        source="realtime_snapshot",
        source_id="fx-source",
    )
    evidence = PerformanceEvidenceSQLiteRepository(repo.db_path)
    evidence.import_envelope(
        CurrentEvidenceCollection(
            status="collected",
            valuation_marks=(persisted_mark,),
            fx_rates=(persisted_fx,),
        ).envelope,
        apply=True,
        migrated_at_ms=NOW_MS,
    )
    conflicting_live = ValuationMarkFact(
        fact_id="live-mark",
        instrument=instrument,
        price="0.5",
        mark_kind="midpoint",
        effective_at_ms=NOW_MS,
        observed_at_ms=NOW_MS,
        source="realtime_snapshot",
        source_id="same-source",
    )

    report = build_option_period_performance(
        repo,
        period={"period": "mtd", "as_of_date": "2026-07-17"},
        account="lx",
        now_ms=NOW_MS,
        evidence_repo=evidence,
        refresh_quotes=True,
        evidence_collector=lambda **_kwargs: CurrentEvidenceCollection(
            status="collected",
            valuation_marks=(conflicting_live,),
        ),
    )

    assert report["pnl"]["ending_unrealized_gross"]["by_currency"] == {"USD": 100.0}
    assert report["evidence"]["collection"]["status"] == "evidence_conflict"
    assert report["evidence"]["live_unpersisted_valuation_mark_count"] == 0
    assert "performance_evidence_merge_conflict" in report["quality"]["warnings"]


def test_unsupported_evidence_schema_degrades_top_level_quality(tmp_path) -> None:
    from types import SimpleNamespace

    repo = SQLiteOptionPositionsRepository(tmp_path / "unsupported.sqlite3")

    class UnsupportedEvidence:
        def read_all(self):
            return SimpleNamespace(
                schema_state="unsupported_schema",
                valuation_marks=(),
                fx_rates=(),
                message="broken evidence schema",
            )

    report = build_option_period_performance(
        repo,
        period={"period": "month", "month": "2026-05"},
        account="lx",
        now_ms=NOW_MS,
        evidence_repo=UnsupportedEvidence(),
    )

    assert report["quality"]["status"] == "partial"
    assert report["quality"]["warnings"] == ["performance_evidence_schema_unsupported"]
    assert report["evidence"]["message"] == "broken evidence schema"
