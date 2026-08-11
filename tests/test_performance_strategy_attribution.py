from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from domain.domain.ledger import ContractKey, TradeEvent, project_trade_events
from domain.domain.performance.engine import build_period_performance
from domain.domain.performance.period import normalize_period

TZ = ZoneInfo("Asia/Shanghai")
NOW_MS = int(datetime(2026, 7, 17, 12, 0, tzinfo=TZ).timestamp() * 1000)


def _ms(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=TZ).timestamp() * 1000)


def _event(
    event_id: str,
    event_type: str,
    at: str,
    *,
    role: str,
    option_type: str,
    side: str,
    strike: float,
    price: float,
    target_lot_id: str | None = None,
    structure: str | None = "same_expiry",
    structure_mode: str | None = None,
    put_expiration_ymd: str = "2026-05-31",
    call_expiration_ymd: str = "2026-05-31",
) -> TradeEvent:
    snapshot: dict = {
        "strategy": "combo_yield",
        "leg_role": role,
        "strategy_group_id": "combo_yield:lx:pair-1",
    }
    if structure is not None:
        snapshot["expiry_structure"] = structure
    if structure_mode is not None:
        snapshot["structure_mode"] = structure_mode
    return TradeEvent(
        event_id=event_id,
        event_type=event_type,
        event_time_ms=_ms(at),
        contract_key=ContractKey.from_values(
            broker="futu",
            account="lx",
            underlying_symbol="NVDA",
            option_type=option_type,
            position_side=side,
            strike=strike,
            expiration_ymd=put_expiration_ymd if option_type == "put" else call_expiration_ymd,
        ),
        contracts=1,
        price=price,
        currency="USD",
        source="test",
        multiplier=100,
        fees=0,
        lot_id=f"lot-{event_id}" if event_type == "open" else None,
        target_lot_id=target_lot_id,
        raw_payload={
            "fee_provenance": {"basis": "actual", "amount": 0, "source": "test"},
            "strategy_snapshot": snapshot,
        },
    )


def _report(events: list[TradeEvent]) -> dict:
    projection = project_trade_events(events)
    window = normalize_period({"period": "month", "month": "2026-05"}, now_ms=NOW_MS)
    return build_period_performance(
        events=events,
        allocations=projection.allocations,
        period=window,
        diagnostics=[item.to_dict() for item in projection.diagnostics],
    ).to_dict()


def test_group_attribution_keeps_call_basis_out_of_put_pnl_and_conserves_group_pnl() -> None:
    events = [
        _event("put-open", "open", "2026-05-01T00:00:00", role="funding_put", option_type="put", side="short", strike=100, price=5),
        _event("call-open", "open", "2026-05-01T00:00:00", role="participation_call", option_type="call", side="long", strike=120, price=4),
        _event("put-expire", "expire_close", "2026-05-10T00:00:00", role="funding_put", option_type="put", side="short", strike=100, price=0, target_lot_id="lot-put-open"),
        _event("call-close", "close", "2026-05-20T00:00:00", role="participation_call", option_type="call", side="long", strike=120, price=7, target_lot_id="lot-call-open"),
    ]

    report = _report(events)
    attribution = report["attribution"]
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


def test_legacy_diagonal_group_fails_closed_to_partial_attribution() -> None:
    events = [
        _event("put-open", "open", "2026-05-01T00:00:00", role="funding_put", option_type="put", side="short", strike=100, price=5, structure="diagonal", put_expiration_ymd="2026-05-31"),
        _event("call-open", "open", "2026-05-01T00:00:00", role="participation_call", option_type="call", side="long", strike=120, price=4, structure="diagonal", call_expiration_ymd="2026-07-31"),
        _event("put-expire", "expire_close", "2026-05-10T00:00:00", role="funding_put", option_type="put", side="short", strike=100, price=0, target_lot_id="lot-put-open"),
        _event("call-close", "close", "2026-05-20T00:00:00", role="participation_call", option_type="call", side="long", strike=120, price=7, target_lot_id="lot-call-open"),
    ]

    report = _report(events)
    attribution = report["attribution"]

    assert attribution["coverage"]["status"] == "partial"
    assert attribution["groups"] == []
    assert "strategy_group_invalid:combo_yield:lx:pair-1:unsupported_expiry_structure" in attribution["coverage"]["issues"]


def test_production_form_without_expiry_structure_is_ready() -> None:
    events = [
        _event("put-open", "open", "2026-05-01T00:00:00", role="funding_put", option_type="put", side="short", strike=100, price=5, structure=None),
        _event("call-open", "open", "2026-05-01T00:00:00", role="participation_call", option_type="call", side="long", strike=120, price=4, structure=None, call_expiration_ymd="2026-05-31"),
        _event("put-expire", "expire_close", "2026-05-10T00:00:00", role="funding_put", option_type="put", side="short", strike=100, price=0, target_lot_id="lot-put-open"),
        _event("call-close", "close", "2026-05-20T00:00:00", role="participation_call", option_type="call", side="long", strike=120, price=7, target_lot_id="lot-call-open"),
    ]

    report = _report(events)
    attribution = report["attribution"]

    assert attribution["coverage"]["status"] == "observed"
    assert len(attribution["groups"]) == 1
    assert attribution["coverage"]["issues"] == []


def test_legacy_staggered_structure_mode_fails_closed_to_partial_attribution() -> None:
    events = [
        _event("put-open", "open", "2026-05-01T00:00:00", role="funding_put", option_type="put", side="short", strike=100, price=5, structure=None, structure_mode="staggered_expiry_pair", put_expiration_ymd="2026-05-31"),
        _event("call-open", "open", "2026-05-01T00:00:00", role="participation_call", option_type="call", side="long", strike=120, price=4, structure=None, structure_mode="staggered_expiry_pair", call_expiration_ymd="2026-07-31"),
        _event("put-expire", "expire_close", "2026-05-10T00:00:00", role="funding_put", option_type="put", side="short", strike=100, price=0, target_lot_id="lot-put-open"),
        _event("call-close", "close", "2026-05-20T00:00:00", role="participation_call", option_type="call", side="long", strike=120, price=7, target_lot_id="lot-call-open"),
    ]

    report = _report(events)
    attribution = report["attribution"]

    assert attribution["coverage"]["status"] == "partial"
    assert attribution["groups"] == []
    assert "strategy_group_invalid:combo_yield:lx:pair-1:unsupported_expiry_structure" in attribution["coverage"]["issues"]


def test_metadata_missing_staggered_expiries_fails_closed_to_partial_attribution() -> None:
    events = [
        _event("put-open", "open", "2026-05-01T00:00:00", role="funding_put", option_type="put", side="short", strike=100, price=5, structure=None, put_expiration_ymd="2026-05-31"),
        _event("call-open", "open", "2026-05-01T00:00:00", role="participation_call", option_type="call", side="long", strike=120, price=4, structure=None, call_expiration_ymd="2026-07-31"),
        _event("put-expire", "expire_close", "2026-05-10T00:00:00", role="funding_put", option_type="put", side="short", strike=100, price=0, target_lot_id="lot-put-open"),
        _event("call-close", "close", "2026-05-20T00:00:00", role="participation_call", option_type="call", side="long", strike=120, price=7, target_lot_id="lot-call-open"),
    ]

    report = _report(events)
    attribution = report["attribution"]

    assert attribution["coverage"]["status"] == "partial"
    assert attribution["groups"] == []
    assert "strategy_group_invalid:combo_yield:lx:pair-1:same_expiry_mismatch" in attribution["coverage"]["issues"]


def test_legacy_same_expiry_structure_mode_is_ready() -> None:
    events = [
        _event("put-open", "open", "2026-05-01T00:00:00", role="funding_put", option_type="put", side="short", strike=100, price=5, structure=None, structure_mode="same_expiry_pair"),
        _event("call-open", "open", "2026-05-01T00:00:00", role="participation_call", option_type="call", side="long", strike=120, price=4, structure=None, structure_mode="same_expiry_pair"),
        _event("put-expire", "expire_close", "2026-05-10T00:00:00", role="funding_put", option_type="put", side="short", strike=100, price=0, target_lot_id="lot-put-open"),
        _event("call-close", "close", "2026-05-20T00:00:00", role="participation_call", option_type="call", side="long", strike=120, price=7, target_lot_id="lot-call-open"),
    ]

    report = _report(events)
    attribution = report["attribution"]

    assert attribution["coverage"]["status"] == "observed"
    assert len(attribution["groups"]) == 1


def test_group_capital_uses_put_notional_days_plus_call_premium_days() -> None:
    events = [
        _event("put-open", "open", "2026-05-01T00:00:00", role="funding_put", option_type="put", side="short", strike=100, price=5),
        _event("call-open", "open", "2026-05-01T00:00:00", role="participation_call", option_type="call", side="long", strike=120, price=4),
        _event("put-expire", "expire_close", "2026-05-10T00:00:00", role="funding_put", option_type="put", side="short", strike=100, price=0, target_lot_id="lot-put-open"),
        _event("call-close", "close", "2026-05-20T00:00:00", role="participation_call", option_type="call", side="long", strike=120, price=7, target_lot_id="lot-call-open"),
    ]

    group = _report(events)["attribution"]["groups"][0]

    assert group["funding_cycles"][0]["capital"]["capital_days_by_currency"] == {"USD": 90000.0}
    assert group["participation_lifecycles"][0]["capital"]["capital_days_by_currency"] == {"USD": 7600.0}
    assert group["capital"]["capital_days_by_currency"] == {"USD": 97600.0}
    assert group["capital"]["average_incremental_capital_by_currency"]["USD"] == pytest.approx(97600 / 31)


def test_invalid_group_topology_is_partial_without_changing_canonical_totals() -> None:
    events = [
        _event("put-open-1", "open", "2026-05-01T00:00:00", role="funding_put", option_type="put", side="short", strike=100, price=5),
        _event("put-open-2", "open", "2026-05-02T00:00:00", role="funding_put", option_type="put", side="short", strike=95, price=4),
        _event("call-open", "open", "2026-05-01T00:00:00", role="participation_call", option_type="call", side="long", strike=120, price=4),
    ]

    report = _report(events)

    assert report["attribution"]["groups"] == []
    assert report["attribution"]["coverage"]["status"] == "partial"
    assert any("funding_put_count:2" in issue for issue in report["attribution"]["coverage"]["issues"])
    assert report["cash"]["option_trade_cash_gross"]["by_currency"] == {"USD": 500.0}


def test_residual_tail_pnl_is_available_when_report_window_starts_after_put_close() -> None:
    from decimal import Decimal

    from domain.domain.performance.attribution import resolve_event_attribution
    from domain.domain.performance.models import OptionInstrumentKey, OptionValuationPosition, ValuationMarkFact

    put_open = _event("put-open", "open", "2026-04-01T00:00:00", role="funding_put", option_type="put", side="short", strike=100, price=5)
    call_open = _event("call-open", "open", "2026-04-01T00:00:00", role="participation_call", option_type="call", side="long", strike=120, price=4)
    put_close = _event("put-expire", "expire_close", "2026-05-20T00:00:00", role="funding_put", option_type="put", side="short", strike=100, price=0, target_lot_id="lot-put-open")
    events = [put_open, call_open, put_close]
    projection = project_trade_events(events)
    window = normalize_period({"period": "month", "month": "2026-06"}, now_ms=NOW_MS)
    instrument = OptionInstrumentKey(
        symbol="NVDA",
        option_type="call",
        strike=Decimal("120"),
        expiration_ymd="2026-07-31",
        currency="USD",
        multiplier=Decimal("100"),
    )
    resolved = resolve_event_attribution(call_open, lifecycle_source_id="lot-call-open")
    position = OptionValuationPosition(
        lot_id="lot-call-open",
        account="lx",
        broker="futu",
        instrument=instrument,
        position_side="long",
        contracts_open=1,
        open_price=Decimal("4"),
        open_fee_remaining=Decimal("0"),
        open_fee_quality="actual",
        opened_at_ms=call_open.event_time_ms,
        attribution=resolved.attribution,
    )
    marks = [
        ValuationMarkFact(
            fact_id="call-open-boundary",
            instrument=instrument,
            price=Decimal("5"),
            mark_kind="official_close",
            effective_at_ms=window.valuation_open_at_ms,
            observed_at_ms=window.valuation_open_at_ms,
            source="test",
            source_id="call-open-boundary",
        ),
        ValuationMarkFact(
            fact_id="call-end-boundary",
            instrument=instrument,
            price=Decimal("6"),
            mark_kind="official_close",
            effective_at_ms=window.valuation_end_at_ms,
            observed_at_ms=window.valuation_end_at_ms,
            source="test",
            source_id="call-end-boundary",
        ),
    ]

    report = build_period_performance(
        events=events,
        allocations=projection.allocations,
        period=window,
        opening_positions=[position],
        ending_positions=[position],
        valuation_marks=marks,
    ).to_dict()
    tail = report["attribution"]["groups"][0]["residual_tails"][0]

    assert tail["quality"] == {"status": "observed", "issues": []}
    assert tail["pnl"]["period_total_gross"]["by_currency"] == {"USD": 100.0}
    assert tail["capital"]["capital_days_by_currency"] == {"USD": 12000.0}


def test_residual_tail_quality_is_partial_when_isolated_call_marks_are_missing() -> None:
    from decimal import Decimal

    from domain.domain.performance.attribution import resolve_event_attribution
    from domain.domain.performance.models import OptionInstrumentKey, OptionValuationPosition

    put_open = _event("put-open", "open", "2026-04-01T00:00:00", role="funding_put", option_type="put", side="short", strike=100, price=5)
    call_open = _event("call-open", "open", "2026-04-01T00:00:00", role="participation_call", option_type="call", side="long", strike=120, price=4)
    put_close = _event("put-expire", "expire_close", "2026-05-20T00:00:00", role="funding_put", option_type="put", side="short", strike=100, price=0, target_lot_id="lot-put-open")
    events = [put_open, call_open, put_close]
    projection = project_trade_events(events)
    window = normalize_period({"period": "month", "month": "2026-06"}, now_ms=NOW_MS)
    instrument = OptionInstrumentKey(
        symbol="NVDA",
        option_type="call",
        strike=Decimal("120"),
        expiration_ymd="2026-07-31",
        currency="USD",
        multiplier=Decimal("100"),
    )
    resolved = resolve_event_attribution(call_open, lifecycle_source_id="lot-call-open")
    position = OptionValuationPosition(
        lot_id="lot-call-open",
        account="lx",
        broker="futu",
        instrument=instrument,
        position_side="long",
        contracts_open=1,
        open_price=Decimal("4"),
        open_fee_remaining=Decimal("0"),
        open_fee_quality="actual",
        opened_at_ms=call_open.event_time_ms,
        attribution=resolved.attribution,
    )

    report = build_period_performance(
        events=events,
        allocations=projection.allocations,
        period=window,
        opening_positions=[position],
        ending_positions=[position],
        valuation_marks=[],
    ).to_dict()
    tail = report["attribution"]["groups"][0]["residual_tails"][0]

    assert tail["pnl"]["period_total_gross"]["status"] == "partial"
    assert tail["pnl"]["period_total_net"]["status"] == "partial"
    assert tail["quality"] == {
        "status": "partial",
        "issues": [
            "residual_tail_period_total_gross_partial",
            "residual_tail_period_total_net_partial",
        ],
    }


def test_non_combo_strategy_produces_observed_empty_attribution() -> None:
    event = _event("put-open", "open", "2026-05-01T00:00:00", role="funding_put", option_type="put", side="short", strike=100, price=5)
    event.raw_payload["strategy_snapshot"] = {"strategy": "sell_put", "leg_role": "funding_put"}

    report = _report([event])

    assert report["attribution"]["groups"] == []
    assert report["attribution"]["coverage"] == {"status": "observed", "group_count": 0, "issues": []}


def test_group_closed_before_period_is_not_emitted() -> None:
    events = [
        _event("put-open", "open", "2026-04-01T00:00:00", role="funding_put", option_type="put", side="short", strike=100, price=5),
        _event("call-open", "open", "2026-04-01T00:00:00", role="participation_call", option_type="call", side="long", strike=120, price=4),
        _event("put-expire", "expire_close", "2026-04-10T00:00:00", role="funding_put", option_type="put", side="short", strike=100, price=0, target_lot_id="lot-put-open"),
        _event("call-close", "close", "2026-04-20T00:00:00", role="participation_call", option_type="call", side="long", strike=120, price=7, target_lot_id="lot-call-open"),
    ]

    report = _report(events)

    assert report["attribution"]["groups"] == []
    assert report["attribution"]["coverage"]["status"] == "observed"


def test_period_total_conservation_is_reported_separately() -> None:
    events = [
        _event("put-open", "open", "2026-05-01T00:00:00", role="funding_put", option_type="put", side="short", strike=100, price=5),
        _event("call-open", "open", "2026-05-01T00:00:00", role="participation_call", option_type="call", side="long", strike=120, price=4),
        _event("put-expire", "expire_close", "2026-05-10T00:00:00", role="funding_put", option_type="put", side="short", strike=100, price=0, target_lot_id="lot-put-open"),
        _event("call-close", "close", "2026-05-20T00:00:00", role="participation_call", option_type="call", side="long", strike=120, price=7, target_lot_id="lot-call-open"),
    ]

    conservation = _report(events)["attribution"]["conservation"]

    assert conservation["period_total_gross"]["status"] == "observed"
    assert conservation["period_total_gross"]["residual_by_currency"] == {"USD": 0.0}
    assert conservation["period_total_net"]["status"] == "observed"


def test_assigned_stock_requires_explicit_group_provenance() -> None:
    from domain.domain.performance.engine import _assigned_stock_fact_kwargs

    attributed = _assigned_stock_fact_kwargs(
        {
            "account": "lx",
            "broker": "futu",
            "symbol": "NVDA",
            "currency": "USD",
            "stock_lot_id": "stock-lot-1",
            "strategy": "combo_yield",
            "strategy_group_id": "combo_yield:lx:pair-1",
        },
        source_event_id="stock-lot-1",
    )
    unproven = _assigned_stock_fact_kwargs(
        {"account": "lx", "broker": "futu", "symbol": "NVDA", "currency": "USD"},
        source_event_id="stock-lot-2",
    )

    assert attributed["attribution"].leg_role == "assigned_stock"
    assert attributed["attribution"].lifecycle_id == "assigned_stock:stock-lot-1"
    assert attributed["attribution_issues"] == ()
    assert unproven["attribution"] is None


@pytest.mark.parametrize("conflict_key", ["strategy", "leg_role", "expiry_structure"])
def test_assigned_stock_metadata_conflicts_fail_closed(conflict_key: str) -> None:
    from domain.domain.performance.engine import _assigned_stock_fact_kwargs

    row = {
        "account": "lx",
        "broker": "futu",
        "symbol": "NVDA",
        "currency": "USD",
        "stock_lot_id": "stock-lot-1",
        "strategy": "combo_yield",
        "leg_role": "assigned_stock",
        "strategy_group_id": "combo_yield:lx:pair-1",
        "expiry_structure": "same_expiry",
        "strategy_snapshot": {
            "strategy": "combo_yield",
            "leg_role": "assigned_stock",
            "strategy_group_id": "combo_yield:lx:pair-1",
            "expiry_structure": "same_expiry",
        },
    }
    row[conflict_key] = {
        "strategy": "sell_put",
        "leg_role": "funding_put",
        "expiry_structure": "unsupported",
    }[conflict_key]

    result = _assigned_stock_fact_kwargs(row, source_event_id="stock-lot-1")

    assert result["attribution"] is None
    assert result["attribution_issues"] == (
        f"assigned_stock_strategy_metadata_conflict:stock-lot-1:{conflict_key}",
    )


def test_assigned_stock_combo_without_group_reports_unavailable_attribution() -> None:
    from domain.domain.performance.engine import _assigned_stock_fact_kwargs

    result = _assigned_stock_fact_kwargs(
        {
            "account": "lx",
            "broker": "futu",
            "symbol": "NVDA",
            "currency": "USD",
            "stock_lot_id": "stock-lot-1",
            "strategy": "combo_yield",
            "leg_role": "assigned_stock",
        },
        source_event_id="stock-lot-1",
    )

    assert result["attribution"] is None
    assert result["attribution_issues"] == ("assigned_stock_attribution_unavailable:stock-lot-1",)


def test_assigned_stock_non_combo_metadata_remains_observed_empty() -> None:
    from domain.domain.performance.engine import _assigned_stock_fact_kwargs

    result = _assigned_stock_fact_kwargs(
        {
            "account": "lx",
            "broker": "futu",
            "symbol": "NVDA",
            "currency": "USD",
            "stock_lot_id": "stock-lot-1",
            "strategy": "sell_put",
            "leg_role": "assigned_stock",
            "strategy_group_id": "sell_put:lx:lot-1",
        },
        source_event_id="stock-lot-1",
    )

    assert result["attribution"] is None
    assert result["attribution_issues"] == ()


def test_assigned_stock_partial_sales_keep_stock_lot_lifecycle_identity() -> None:
    from domain.domain.performance.engine import _assigned_stock_fact_kwargs

    row = {
        "account": "lx",
        "broker": "futu",
        "symbol": "NVDA",
        "currency": "USD",
        "stock_lot_id": "stock-lot-1",
        "strategy": "combo_yield",
        "strategy_group_id": "combo_yield:lx:pair-1",
    }

    first = _assigned_stock_fact_kwargs(row, source_event_id="sale-1")
    second = _assigned_stock_fact_kwargs(row, source_event_id="sale-2")

    assert first["attribution"].lifecycle_id == "assigned_stock:stock-lot-1"
    assert second["attribution"].lifecycle_id == "assigned_stock:stock-lot-1"


def test_attribution_summary_does_not_depend_on_rows_serialization() -> None:
    events = [
        _event("put-open", "open", "2026-05-01T00:00:00", role="funding_put", option_type="put", side="short", strike=100, price=5),
        _event("call-open", "open", "2026-05-01T00:00:00", role="participation_call", option_type="call", side="long", strike=120, price=4),
        _event("put-expire", "expire_close", "2026-05-10T00:00:00", role="funding_put", option_type="put", side="short", strike=100, price=0, target_lot_id="lot-put-open"),
        _event("call-close", "close", "2026-05-20T00:00:00", role="participation_call", option_type="call", side="long", strike=120, price=7, target_lot_id="lot-call-open"),
    ]
    projection = project_trade_events(events)
    window = normalize_period({"period": "month", "month": "2026-05"}, now_ms=NOW_MS)
    result = build_period_performance(events=events, allocations=projection.allocations, period=window)

    with_rows = result.to_dict(include_rows=True)
    without_rows = result.to_dict(include_rows=False)

    assert "rows" in with_rows
    assert "rows" not in without_rows
    assert without_rows["attribution"] == with_rows["attribution"]


def test_mislabeled_leg_contract_fails_closed_for_attribution() -> None:
    events = [
        _event("bad-put", "open", "2026-05-01T00:00:00", role="funding_put", option_type="call", side="long", strike=100, price=5),
        _event("call-open", "open", "2026-05-01T00:00:00", role="participation_call", option_type="call", side="long", strike=120, price=4),
    ]

    report = _report(events)

    assert report["attribution"]["groups"] == []
    assert any("funding_put_contract_invalid" in issue for issue in report["attribution"]["coverage"]["issues"])


def test_group_quality_is_partial_when_period_pnl_is_unobserved() -> None:
    events = [
        _event("put-open", "open", "2026-05-01T00:00:00", role="funding_put", option_type="put", side="short", strike=100, price=5),
        _event("call-open", "open", "2026-05-01T00:00:00", role="participation_call", option_type="call", side="long", strike=120, price=4),
    ]

    group = _report(events)["attribution"]["groups"][0]

    assert group["pnl"]["period_total_net"]["status"] == "not_observed"
    assert group["quality"]["status"] == "partial"
    assert "group_period_total_net_not_observed" in group["quality"]["issues"]
