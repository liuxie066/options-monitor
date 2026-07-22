from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.performance.models import FXRateFact, OptionInstrumentKey, StockInstrumentKey, ValuationMarkFact
from domain.domain.performance.period import normalize_period
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.performance.adapters import load_assigned_stock_projection, load_ledger_performance_inputs
from src.application.performance.service import build_option_period_performance
from src.infrastructure.performance_evidence_sqlite import PerformanceEvidenceSQLiteRepository


TZ = ZoneInfo("Asia/Shanghai")
NOW_MS = int(datetime(2026, 7, 17, 12, 0, tzinfo=TZ).timestamp() * 1000)


def _ms(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=TZ).timestamp() * 1000)


def _key() -> ContractKey:
    return ContractKey.from_values(
        broker="futu",
        account="lx",
        underlying_symbol="NVDA",
        option_type="put",
        position_side="short",
        strike=100,
        expiration_ymd="2026-08-21",
    )


def _repo_with_assignment(
    tmp_path,
    *,
    assignment_stock_fee: float = 0,
    assignment_stock_fee_basis: str = "actual",
    include_stock_settlement: bool = True,
) -> SQLiteOptionPositionsRepository:
    repo = SQLiteOptionPositionsRepository(tmp_path / "assignment-performance.sqlite3")
    key = _key()
    assignment_payload = {"fee_provenance": {"basis": "actual", "source": "test"}}
    if include_stock_settlement:
        assignment_payload["stock_settlement"] = {
            "side": "buy",
            "shares": 100,
            "price": 100,
            "fees": assignment_stock_fee,
            "fee_provenance": {"basis": assignment_stock_fee_basis, "source": "test"},
        }
    repo.upsert_trade_event(
        TradeEvent(
            event_id="open-put",
            event_type="open",
            event_time_ms=_ms("2026-04-03T10:00:00"),
            contract_key=key,
            contracts=1,
            price=2.5,
            currency="USD",
            source="test",
            multiplier=100,
            fees=0,
            lot_id="lot-put",
            raw_payload={"fee_provenance": {"basis": "actual", "source": "test"}},
        )
    )
    repo.upsert_trade_event(
        TradeEvent(
            event_id="assign-put",
            event_type="assignment",
            event_time_ms=_ms("2026-05-01T10:00:00"),
            contract_key=key,
            contracts=1,
            price=0,
            currency="USD",
            source="test",
            multiplier=100,
            fees=0,
            target_lot_id="lot-put",
            raw_payload=assignment_payload,
        )
    )
    return repo


def _evidence(
    repo,
    marks: list[tuple[str, int, float]],
    *,
    option_marks: list[tuple[str, int, float]] | None = None,
    fx_at_ms: list[int] | None = None,
) -> PerformanceEvidenceSQLiteRepository:
    stock_instrument = StockInstrumentKey(symbol="NVDA", currency="USD")
    facts = [
        ValuationMarkFact(
            fact_id=fact_id,
            instrument=stock_instrument,
            price=str(price),
            mark_kind="official_close",
            effective_at_ms=at_ms,
            observed_at_ms=at_ms,
            source="official_close",
            source_id=fact_id,
        )
        for fact_id, at_ms, price in marks
    ]
    option_instrument = OptionInstrumentKey.from_contract_key(_key(), currency="USD", multiplier=100)
    facts.extend(
        ValuationMarkFact(
            fact_id=fact_id,
            instrument=option_instrument,
            price=str(price),
            mark_kind="official_close",
            effective_at_ms=at_ms,
            observed_at_ms=at_ms,
            source="official_close",
            source_id=fact_id,
        )
        for fact_id, at_ms, price in (option_marks or [])
    )
    fx_rates = [
        FXRateFact(
            fact_id=f"fx-{at_ms}",
            base_currency="USD",
            quote_currency="CNY",
            rate="7",
            rate_kind="official_close",
            effective_at_ms=at_ms,
            observed_at_ms=at_ms,
            source="official_close",
            source_id=f"fx-{at_ms}",
        )
        for at_ms in (fx_at_ms or [])
    ]
    evidence = PerformanceEvidenceSQLiteRepository(repo.db_path)
    evidence.import_envelope(
        {
            "schema_version": "option_performance_evidence.v1",
            "valuation_marks": [fact.normalized_payload() for fact in facts],
            "fx_rates": [fact.normalized_payload() for fact in fx_rates],
        },
        apply=True,
        migrated_at_ms=NOW_MS,
    )
    return evidence


def test_assignment_period_pnl_does_not_double_count_put_premium_or_stock_fee(tmp_path) -> None:
    repo = _repo_with_assignment(tmp_path, assignment_stock_fee=1)
    window = normalize_period({"period": "month", "month": "2026-05"}, now_ms=NOW_MS)
    evidence = _evidence(
        repo,
        [("may-end-stock", window.valuation_end_at_ms, 102)],
        option_marks=[("may-open-put", window.valuation_open_at_ms, 2.5)],
    )

    report = build_option_period_performance(
        repo,
        period=window,
        account="lx",
        now_ms=NOW_MS,
        evidence_repo=evidence,
        refresh_quotes=True,
        evidence_collector=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("historical path fetched live")),
    )

    assert report["pnl"]["realized_gross"]["by_currency"] == {"USD": 250.0}
    assert report["pnl"]["ending_unrealized_gross"]["by_currency"] == {"USD": 200.0}
    assert report["pnl"]["period_total_gross"]["by_currency"] == {"USD": 450.0}
    assert report["pnl"]["period_total_net"]["by_currency"] == {"USD": 449.0}
    assert report["cash"]["stock_settlement_fee_cash"]["by_currency"] == {"USD": -1.0}
    assert report["activity"]["assigned_stock_shares_opened"] == 100
    assert report["evidence"]["collection"]["status"] == "skipped_historical"


def test_stock_settlement_fee_cash_requires_actual_provenance_and_preserves_actual_zero(tmp_path) -> None:
    estimated_repo = _repo_with_assignment(
        tmp_path / "estimated",
        assignment_stock_fee=1,
        assignment_stock_fee_basis="estimated",
    )
    zero_repo = _repo_with_assignment(
        tmp_path / "zero",
        assignment_stock_fee=0,
        assignment_stock_fee_basis="actual",
    )

    estimated = build_option_period_performance(
        estimated_repo,
        period={"period": "month", "month": "2026-05"},
        account="lx",
        now_ms=NOW_MS,
    )
    zero = build_option_period_performance(
        zero_repo,
        period={"period": "month", "month": "2026-05"},
        account="lx",
        now_ms=NOW_MS,
    )

    assert estimated["cash"]["stock_settlement_cash_gross"]["by_currency"] == {"USD": -10000.0}
    assert estimated["cash"]["stock_settlement_fee_cash"]["by_currency"] == {}
    assert estimated["cash"]["stock_settlement_fee_cash"]["status"] == "partial"
    assert estimated["cash"]["total_cash_change_net"]["status"] == "partial"
    assert zero["cash"]["stock_settlement_fee_cash"]["by_currency"] == {"USD": 0.0}
    assert zero["cash"]["stock_settlement_fee_cash"]["missing"] == [
        "cash_conversion:stock_settlement_fee_cash:assign-put"
    ]


def test_partial_assigned_stock_sale_conserves_period_pnl_and_actual_fee(tmp_path) -> None:
    repo = _repo_with_assignment(tmp_path)
    june = normalize_period({"period": "month", "month": "2026-06"}, now_ms=NOW_MS)
    repo.upsert_assigned_stock_event(
        {
            "event_type": "sale",
            "stock_event_id": "sale-half",
            "target_stock_lot_id": "assigned-stock-assign-put",
            "account": "lx",
            "broker": "futu",
            "symbol": "NVDA",
            "side": "sell",
            "shares": 50,
            "price": 110,
            "currency": "USD",
            "fees": 2,
            "fee_provenance": {"basis": "actual", "source": "test"},
            "trade_time_ms": _ms("2026-06-15T10:00:00"),
        }
    )
    evidence = _evidence(
        repo,
        [
            ("june-open-stock", june.valuation_open_at_ms, 100),
            ("june-end-stock", june.valuation_end_at_ms, 105),
        ],
    )

    report = build_option_period_performance(
        repo,
        period=june,
        account="lx",
        now_ms=NOW_MS,
        evidence_repo=evidence,
    )

    assert report["activity"]["assigned_stock_shares_sold"] == 50
    assert report["cash"]["assigned_stock_sale_cash_gross"]["by_currency"] == {"USD": 5500.0}
    assert report["cash"]["assigned_stock_sale_fee_cash"]["by_currency"] == {"USD": -2.0}
    assert report["pnl"]["realized_gross"]["by_currency"] == {"USD": 500.0}
    assert report["pnl"]["realized_net"]["by_currency"] == {"USD": 498.0}
    assert report["pnl"]["opening_unrealized_gross"]["by_currency"] == {"USD": 0.0}
    assert report["pnl"]["ending_unrealized_gross"]["by_currency"] == {"USD": 250.0}
    assert report["pnl"]["period_total_gross"]["by_currency"] == {"USD": 750.0}
    assert report["pnl"]["period_total_net"]["by_currency"] == {"USD": 748.0}
    assert report["assigned_stock"]["ending_lots"][0]["shares_remaining"] == 50


def test_estimated_sale_fee_keeps_gross_and_marks_net_partial(tmp_path) -> None:
    repo = _repo_with_assignment(tmp_path)
    repo.upsert_assigned_stock_event(
        {
            "event_type": "sale",
            "stock_event_id": "sale-estimated",
            "target_stock_lot_id": "assigned-stock-assign-put",
            "account": "lx",
            "broker": "futu",
            "symbol": "NVDA",
            "side": "sell",
            "shares": 100,
            "price": 105,
            "currency": "USD",
            "fees": 2,
            "fee_provenance": {"basis": "estimated", "source": "test"},
            "trade_time_ms": _ms("2026-06-15T10:00:00"),
        }
    )

    report = build_option_period_performance(
        repo,
        period={"period": "month", "month": "2026-06"},
        account="lx",
        now_ms=NOW_MS,
    )

    assert report["pnl"]["realized_gross"]["by_currency"] == {"USD": 500.0}
    assert report["pnl"]["realized_net"]["by_currency"] == {}
    assert report["pnl"]["realized_net"]["status"] == "partial"
    assert report["cash"]["assigned_stock_sale_fee_cash"]["status"] == "partial"


def test_historical_missing_stock_settlement_blocks_proven_zero(tmp_path) -> None:
    repo = _repo_with_assignment(tmp_path, include_stock_settlement=False)

    report = build_option_period_performance(
        repo,
        period={"period": "month", "month": "2026-06"},
        account="lx",
        now_ms=NOW_MS,
        scope_proven=True,
    )

    assert report["quality"]["status"] == "partial"
    assert report["quality"]["warnings"] == ["missing_stock_settlement:assign-put"]
    assert report["pnl"]["period_total_net"]["status"] == "not_observed"
    assert report["pnl"]["period_total_net"]["cny"] is None


def test_invalid_assigned_stock_sale_degrades_observed_period_quality(tmp_path) -> None:
    repo = _repo_with_assignment(tmp_path)
    june = normalize_period({"period": "month", "month": "2026-06"}, now_ms=NOW_MS)
    repo.upsert_assigned_stock_event(
        {
            "event_type": "sale",
            "stock_event_id": "sale-too-many",
            "target_stock_lot_id": "assigned-stock-assign-put",
            "account": "lx",
            "broker": "futu",
            "symbol": "NVDA",
            "side": "sell",
            "shares": 200,
            "price": 110,
            "currency": "USD",
            "fees": 0,
            "fee_provenance": {"basis": "actual", "source": "test"},
            "trade_time_ms": _ms("2026-06-15T10:00:00"),
        }
    )
    evidence = _evidence(
        repo,
        [
            ("june-open-stock", june.valuation_open_at_ms, 100),
            ("june-end-stock", june.valuation_end_at_ms, 105),
        ],
        fx_at_ms=[june.valuation_open_at_ms, june.valuation_end_at_ms],
    )

    report = build_option_period_performance(
        repo,
        period=june,
        account="lx",
        now_ms=NOW_MS,
        evidence_repo=evidence,
        scope_proven=True,
    )

    assert report["pnl"]["period_total_net"]["by_currency"] == {"USD": 500.0}
    assert report["pnl"]["period_total_net"]["status"] == "observed"
    assert report["quality"]["status"] == "partial"
    assert report["quality"]["warnings"] == ["source_conflict:sale-too-many"]
    assert report["assigned_stock"]["sales"] == []


def test_assigned_stock_boundary_projection_restates_later_valid_void(tmp_path) -> None:
    repo = _repo_with_assignment(tmp_path)
    key = _key()
    repo.upsert_trade_event(
        TradeEvent(
            event_id="void-assignment",
            event_type="void",
            event_time_ms=_ms("2026-07-10T10:00:00"),
            contract_key=key,
            contracts=0,
            price=0,
            currency="USD",
            source="test",
            multiplier=100,
            fees=0,
            target_event_id="assign-put",
        )
    )

    inputs = load_ledger_performance_inputs(repo)
    boundary = load_assigned_stock_projection(inputs, as_of_ms=_ms("2026-06-01T00:00:00"), account="lx")

    assert boundary["assigned_stock_lots"] == []
    assert boundary["assignment_lifecycle_rows"] == []
