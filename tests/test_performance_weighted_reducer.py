from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from domain.domain.ledger import ContractKey, TradeEvent, project_trade_events
from domain.domain.ledger.events import LedgerDiagnostic
from domain.domain.performance.models import MetricStatus
from domain.domain.performance.period import PeriodRequest, normalize_performance_period
from domain.domain.performance.weighted_reducer import (
    reduce_option_performance,
)


_TZ = ZoneInfo("Asia/Shanghai")


def _ms(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=_TZ).timestamp() * 1000)


def _key(
    *,
    account: str = "lx",
    broker: str = "futu",
    symbol: str = "NVDA",
    option_type: str = "put",
    side: str = "short",
    strike: float = 100,
    expiration: str = "2026-09-30",
) -> ContractKey:
    return ContractKey.from_values(
        broker=broker,
        account=account,
        underlying_symbol=symbol,
        option_type=option_type,
        position_side=side,
        strike=strike,
        expiration_ymd=expiration,
    )


def _event(
    event_id: str,
    event_type: str,
    at: str,
    *,
    key: ContractKey | None = None,
    lot_id: str | None = None,
    target_lot_id: str | None = None,
    contracts: int = 1,
    price: float = 1,
    fee: float = 0,
    fee_basis: str | None = "actual",
    close_type: str | None = None,
    raw: dict | None = None,
) -> TradeEvent:
    payload = dict(raw or {})
    if fee_basis:
        payload["fee_provenance"] = {
            "basis": fee_basis,
            "amount": fee,
            "source": "test",
        }
    if close_type:
        payload["close_type"] = close_type
    return TradeEvent(
        event_id=event_id,
        event_type=event_type,
        event_time_ms=_ms(at),
        contract_key=key or _key(),
        contracts=contracts,
        price=price,
        currency="USD",
        source="test",
        multiplier=100,
        fees=0 if fee_basis == "estimated" else fee,
        lot_id=lot_id,
        target_lot_id=target_lot_id,
        raw_payload=payload,
    )


def _period(
    *,
    kind: str = "mtd",
    as_of: str | None = None,
    now: str = "2026-09-02T12:00:00",
):
    return normalize_performance_period(
        PeriodRequest(period=kind, as_of_date=as_of),
        report_now_ms=_ms(now),
    )


def test_period_normalization_and_cohort_use_effective_opening_date() -> None:
    current = _period()
    assert current.requested_start_date == "2026-09-01"
    assert current.effective_end_exclusive_at_ms == _ms("2026-09-02T12:00:00") + 1
    assert current.statistic_days == Decimal(129600001) / Decimal(86400000)

    historical = _period(kind="ytd", as_of="2026-09-01")
    assert historical.requested_start_date == "2026-01-01"
    assert historical.effective_end_exclusive_at_ms == _ms("2026-09-02T00:00:00")
    natural_month = normalize_performance_period(
        PeriodRequest(period="month", month="2026-08"),
        report_now_ms=_ms("2026-09-02T12:00:00"),
    )
    assert natural_month.requested_start_date == "2026-08-01"
    assert natural_month.effective_end_exclusive_at_ms == _ms("2026-09-01T00:00:00")

    adjusted = project_trade_events(
        [
            _event("open", "open", "2026-08-31T10:00:00", lot_id="lot-1"),
            _event(
                "adjust",
                "adjust",
                "2026-09-02T10:00:00",
                target_lot_id="lot-1",
                contracts=0,
                price=0,
                raw={"patch": {"opened_at": _ms("2026-09-01T10:00:00")}},
            ),
        ]
    )
    reduction = reduce_option_performance(adjusted, period=current)
    assert [(fact.open_event_id, fact.opened_at_ms) for fact in reduction.facts] == [
        ("open", _ms("2026-09-01T10:00:00"))
    ]


def test_same_millisecond_open_precedes_its_terminal_event_regardless_of_ids() -> None:
    events = [
        _event("a-close", "close", "2026-09-01T10:00:00", target_lot_id="lot-1", close_type="buy_to_close"),
        _event("z-open", "open", "2026-09-01T10:00:00", lot_id="lot-1"),
    ]

    first = project_trade_events(events)
    second = project_trade_events(list(reversed(events)))

    assert first.diagnostics == second.diagnostics == []
    assert first.lots == second.lots
    assert first.lots[0].contracts_open == 0
    assert first.allocations[0].close_event_id == "a-close"


def test_weighted_partial_closes_conserve_contracts_cash_and_capital() -> None:
    short_key = _key(side="short", option_type="put")
    long_key = _key(side="long", option_type="call", strike=110)
    events = [
        _event(
            "short-open",
            "open",
            "2026-09-01T10:00:00",
            key=short_key,
            lot_id="short-lot",
            contracts=3,
            price=2,
            fee=0.9,
        ),
        _event(
            "short-close",
            "close",
            "2026-09-02T10:00:00",
            key=short_key,
            target_lot_id="short-lot",
            price=1,
            fee=0.1,
            close_type="buy_to_close",
        ),
        _event(
            "long-open",
            "open",
            "2026-09-01T10:00:00",
            key=long_key,
            lot_id="long-lot",
            contracts=2,
            price=1,
            fee=0.2,
        ),
        _event(
            "long-close",
            "close",
            "2026-09-02T10:00:00",
            key=long_key,
            target_lot_id="long-lot",
            price=2,
            fee=0.1,
            close_type="sell_to_close",
        ),
    ]
    period = _period()
    first = reduce_option_performance(project_trade_events(events), period=period)
    second = reduce_option_performance(project_trade_events(list(reversed(events))), period=period)

    assert first.facts == second.facts
    assert len([fact for fact in first.facts if fact.state == "open"]) == 2
    assert sum(fact.contracts for fact in first.facts if fact.open_lot_id == "short-lot") == 3
    assert sum(fact.contracts for fact in first.facts if fact.open_lot_id == "long-lot") == 2
    cash = first.bundle["option_net_cashflow"]["by_currency"]["USD"]
    assert cash["terminated"]["amount"] == Decimal("199.400000")
    assert cash["open"]["amount"] == Decimal("299.300000")
    assert cash["total"]["amount"] == Decimal("498.700000")
    assert first.bundle["sell_option_win_rate"]["rate"] == Decimal("1.000000000000")
    assert first.bundle["buy_option_win_rate"]["rate"] == Decimal("1.000000000000")
    option_return = first.bundle["option_return"]["by_currency"]["USD"]
    assert option_return["capital_days"] > 0
    assert option_return["average_occupied_capital"] > 0
    assert option_return["status"] == MetricStatus.OBSERVED


def test_fee_evidence_degrades_cash_without_overriding_lifecycle_win_rules() -> None:
    expiry_key = _key(side="short", option_type="call", strike=120)
    assignment_key = _key(side="short", option_type="put", strike=95)
    exercise_key = _key(side="long", option_type="call", strike=110)
    close_key = _key(side="long", option_type="put", strike=90)
    worthless_key = _key(side="long", option_type="call", strike=130)
    projection = project_trade_events(
        [
            _event(
                "expiry-open",
                "open",
                "2026-09-01T09:00:00",
                key=expiry_key,
                lot_id="expiry-lot",
                fee_basis=None,
            ),
            _event(
                "expiry",
                "expire_close",
                "2026-09-01T16:00:00",
                key=expiry_key,
                target_lot_id="expiry-lot",
                price=0,
                fee_basis=None,
                close_type="expire_auto_close",
            ),
            _event(
                "assignment-open",
                "open",
                "2026-09-01T09:00:00",
                key=assignment_key,
                lot_id="assignment-lot",
            ),
            _event(
                "assignment",
                "assignment",
                "2026-09-01T16:00:00",
                key=assignment_key,
                target_lot_id="assignment-lot",
                price=0,
                close_type="assignment",
            ),
            _event(
                "exercise-open",
                "open",
                "2026-09-01T09:00:00",
                key=exercise_key,
                lot_id="exercise-lot",
            ),
            _event(
                "exercise",
                "exercise",
                "2026-09-01T16:00:00",
                key=exercise_key,
                target_lot_id="exercise-lot",
                price=0,
                fee_basis=None,
                close_type="exercise",
            ),
            _event(
                "worthless-open",
                "open",
                "2026-09-01T09:00:00",
                key=worthless_key,
                lot_id="worthless-lot",
            ),
            _event(
                "worthless",
                "expire_close",
                "2026-09-01T16:00:00",
                key=worthless_key,
                target_lot_id="worthless-lot",
                price=0,
                close_type="expire_auto_close",
            ),
            _event(
                "long-open",
                "open",
                "2026-09-01T09:00:00",
                key=close_key,
                lot_id="long-lot",
            ),
            _event(
                "long-close",
                "close",
                "2026-09-01T16:00:00",
                key=close_key,
                target_lot_id="long-lot",
                price=2,
                fee=0.2,
                fee_basis="estimated",
                close_type="sell_to_close",
            ),
        ]
    )
    reduction = reduce_option_performance(projection, period=_period())

    cash = reduction.bundle["option_net_cashflow"]["by_currency"]["USD"]
    assert cash["total"]["status"] == MetricStatus.PARTIAL
    assert set(cash["total"]["missing"]) == {
        "exercise_fee_missing",
        "fee_estimated",
        "fee_missing",
    }
    assert reduction.bundle["sell_option_win_rate"] == {
        "winning_contracts": 1,
        "eligible_contracts": 2,
        "rate": Decimal("0.500000000000"),
        "status": MetricStatus.OBSERVED,
        "missing": (),
    }
    buy_win = reduction.bundle["buy_option_win_rate"]
    assert buy_win["status"] == MetricStatus.PARTIAL
    assert buy_win["rate"] is None
    assert buy_win["eligible_contracts"] == 1
    assert buy_win["winning_contracts"] == 0
    assert buy_win["missing"] == ("fee_estimated",)


def test_unresolved_after_expiry_keeps_total_cash_but_nulls_split_and_return() -> None:
    conflict_key = _key(expiration="2026-09-01", strike=110)
    projection = project_trade_events(
        [
            _event(
                "open",
                "open",
                "2026-09-01T10:00:00",
                key=_key(expiration="2026-09-01"),
                lot_id="lot-1",
                price=2,
            ),
            _event(
                "conflict-open",
                "open",
                "2026-09-01T10:00:00",
                key=conflict_key,
                lot_id="conflict-lot",
                price=2,
            ),
            _event(
                "conflict-close",
                "close",
                "2026-09-02T10:00:00",
                key=conflict_key,
                target_lot_id="conflict-lot",
                price=1,
                close_type="ambiguous_terminal",
            ),
        ]
    )
    reduction = reduce_option_performance(
        projection,
        period=_period(as_of="2026-09-02", now="2026-09-03T10:00:00"),
    )

    assert [(fact.state, fact.contracts) for fact in reduction.facts] == [
        ("unresolved_after_expiry", 1),
        ("unresolved_after_expiry", 1),
    ]
    assert {reason for fact in reduction.facts for reason in fact.missing} >= {
        "terminal_evidence_conflict",
        "terminal_evidence_missing",
    }
    cash = reduction.bundle["option_net_cashflow"]["by_currency"]["USD"]
    assert cash["total"]["amount"] == Decimal("300.000000")
    assert cash["total"]["status"] == MetricStatus.OBSERVED
    assert cash["open"]["amount"] is None
    assert cash["terminated"]["amount"] is None
    assert set(cash["open"]["missing"]) == {
        "terminal_evidence_conflict",
        "terminal_evidence_missing",
    }
    assert reduction.bundle["option_return"]["by_currency"]["USD"]["rate"] is None
    assert reduction.bundle["sell_option_win_rate"]["status"] == MetricStatus.PARTIAL


def test_strategy_is_exclusive_grouping_and_parent_universes_only_include_sellers() -> None:
    common = {
        "strategy": "combo_yield",
        "strategy_group_id": "combo_yield:csp-lc",
    }
    cc_common = {
        "strategy": "combo_yield",
        "strategy_group_id": "combo_yield:cc-lp",
    }
    specs = [
        ("csp-put", _key(side="short", option_type="put", strike=100), {**common, "leg_role": "funding_put"}),
        ("csp-call", _key(side="long", option_type="call", strike=110), {**common, "leg_role": "participation_call"}),
        ("cc-call", _key(side="short", option_type="call", strike=120), {**cc_common, "leg_role": "short_call"}),
        ("cc-put", _key(side="long", option_type="put", strike=100), {**cc_common, "leg_role": "long_put"}),
        (
            "wheel-call",
            _key(side="short", option_type="call", strike=130),
            {"strategy": "wheel", "leg_role": "wheel_call", "source_stock_lot_id": "stock-1"},
        ),
    ]
    projection = project_trade_events(
        [
            _event(
                event_id,
                "open",
                "2026-09-01T10:00:00",
                key=key,
                lot_id=f"lot-{event_id}",
                raw=metadata,
            )
            for event_id, key, metadata in specs
        ]
    )
    reduction = reduce_option_performance(projection, period=_period())

    assert sorted({fact.attribution_strategy for fact in reduction.facts}) == [
        "cc_lp",
        "csp_lc",
        "wheel",
    ]
    assert {fact.leg_type: fact.opening_option_cash > 0 for fact in reduction.facts} == {
        "buy_call": False,
        "buy_put": False,
        "sell_call": True,
        "sell_put": True,
    }
    parents = {
        row["key"]: row["option_net_cashflow"]["by_currency"]["USD"]["total"]["amount"]
        for row in reduction.breakdowns["parent_universes"]
    }
    assert parents == {"cc": Decimal("200.000000"), "csp": Decimal("100.000000")}
    assert all(
        fact.leg_type.startswith("sell_")
        for fact in reduction.facts
        if fact.attribution_strategy in {"csp", "cc", "wheel"}
    )


def test_combo_attribution_rejects_cross_broker_membership() -> None:
    metadata = {
        "strategy": "combo_yield",
        "strategy_group_id": "combo_yield:cross-broker",
    }
    projection = project_trade_events(
        [
            _event(
                "put",
                "open",
                "2026-09-01T10:00:00",
                key=_key(broker="futu", side="short", option_type="put", strike=100),
                lot_id="put-lot",
                raw={**metadata, "leg_role": "funding_put"},
            ),
            _event(
                "call",
                "open",
                "2026-09-01T10:00:00",
                key=_key(broker="ibkr", side="long", option_type="call", strike=110),
                lot_id="call-lot",
                raw={**metadata, "leg_role": "participation_call"},
            ),
        ]
    )

    reduction = reduce_option_performance(projection, period=_period())

    assert "csp_lc_membership_incomplete" in {item.code for item in projection.diagnostics}
    assert "csp_lc" not in {fact.attribution_strategy for fact in reduction.facts}
    assert reduction.bundle["status"] == MetricStatus.PARTIAL


def test_incomplete_combo_buyer_remains_unassigned() -> None:
    projection = project_trade_events(
        [
            _event(
                "call",
                "open",
                "2026-09-01T10:00:00",
                key=_key(side="long", option_type="call", strike=110),
                lot_id="call-lot",
                raw={
                    "strategy": "combo_yield",
                    "strategy_group_id": "combo_yield:pending",
                    "leg_role": "participation_call",
                },
            )
        ]
    )

    reduction = reduce_option_performance(projection, period=_period())

    assert projection.diagnostics == []
    assert reduction.facts[0].attribution_strategy == "unassigned"
    assert reduction.facts[0].strategy_group_id is None
    assert "strategy_attribution_conflict" in reduction.facts[0].missing


def test_strategy_clear_changes_wheel_call_back_to_cc() -> None:
    key = _key(side="short", option_type="call")
    projection = project_trade_events(
        [
            _event(
                "open",
                "open",
                "2026-09-01T10:00:00",
                key=key,
                lot_id="call-lot",
                raw={
                    "strategy": "wheel",
                    "leg_role": "wheel_call",
                    "source_stock_lot_id": "stock-1",
                },
            ),
            _event(
                "clear",
                "adjust",
                "2026-09-01T11:00:00",
                key=key,
                contracts=0,
                target_lot_id="call-lot",
                fee_basis=None,
                raw={
                    "patch": {
                        "strategy": None,
                        "leg_role": None,
                        "source_stock_lot_id": None,
                    }
                },
            ),
        ]
    )

    reduction = reduce_option_performance(projection, period=_period())

    assert [fact.attribution_strategy for fact in reduction.facts] == ["cc"]
    assert reduction.bundle["status"] == MetricStatus.OBSERVED


def test_strategy_conflict_only_degrades_the_attribution_view() -> None:
    projection = project_trade_events(
        [
            _event(
                "open",
                "open",
                "2026-09-01T10:00:00",
                lot_id="lot-1",
                raw={
                    "strategy": "combo_yield",
                    "strategy_group_id": "combo_yield:incomplete",
                    "leg_role": "funding_put",
                },
            )
        ]
    )
    reduction = reduce_option_performance(projection, period=_period())

    assert reduction.bundle["status"] == MetricStatus.PARTIAL
    assert reduction.bundle["missing"] == ("strategy_attribution_conflict",)
    assert reduction.bundle["option_net_cashflow"]["by_currency"]["USD"]["total"]["status"] == MetricStatus.OBSERVED
    assert reduction.breakdowns["leg_types"][0]["status"] == MetricStatus.OBSERVED
    strategy = reduction.breakdowns["attribution_strategies"][0]
    assert strategy["key"] == "csp"
    assert strategy["status"] == MetricStatus.PARTIAL
    assert strategy["missing"] == ("strategy_attribution_conflict",)


def test_diagnostics_are_filtered_after_projection_and_missing_dimensions_fail_safe() -> None:
    projection = project_trade_events([_event("open", "open", "2026-09-01T10:00:00", lot_id="lot-1")])
    other_account = LedgerDiagnostic(
        event_id="sy-warning",
        severity="warning",
        code="economic_adjust_invalid",
        message="test",
        account="sy",
        broker="futu",
    )
    scoped = reduce_option_performance(
        replace(projection, diagnostics=[other_account]),
        period=_period(),
        account="lx",
    )
    assert scoped.bundle["status"] == MetricStatus.OBSERVED

    unscoped = replace(other_account, event_id="unknown", account=None, broker=None)
    fail_safe = reduce_option_performance(
        replace(projection, diagnostics=[unscoped]),
        period=_period(),
        account="lx",
    )
    assert fail_safe.bundle["status"] == MetricStatus.PARTIAL
    assert fail_safe.bundle["missing"] == ("economic_adjust_invalid",)

    error = replace(unscoped, severity="error")
    isolated = reduce_option_performance(
        replace(projection, diagnostics=[error]),
        period=_period(),
        account="lx",
    )
    assert isolated.bundle["status"] == MetricStatus.PARTIAL
    assert isolated.bundle["missing"] == ("economic_adjust_invalid",)


def test_diagnostics_for_lots_outside_the_opening_cohort_do_not_degrade_period() -> None:
    projection = project_trade_events(
        [
            _event("aug-open", "open", "2026-08-01T10:00:00", lot_id="aug-lot"),
            _event("sep-open", "open", "2026-09-01T10:00:00", lot_id="sep-lot"),
        ]
    )
    old_lot_diagnostic = LedgerDiagnostic(
        event_id="aug-bad-adjust",
        severity="warning",
        code="economic_adjust_invalid",
        message="test",
        details={"target_lot_id": "aug-lot"},
        account="lx",
        broker="futu",
    )

    reduction = reduce_option_performance(
        replace(projection, diagnostics=[old_lot_diagnostic]),
        period=_period(),
    )

    assert [fact.open_lot_id for fact in reduction.facts] == ["sep-lot"]
    assert reduction.diagnostics == ()
    assert reduction.bundle["status"] == MetricStatus.OBSERVED

    unprovable_opening_time = replace(
        old_lot_diagnostic,
        details={
            "target_lot_id": "aug-lot",
            "cohort_time_unreliable": True,
        },
    )
    fail_safe = reduce_option_performance(
        replace(projection, diagnostics=[unprovable_opening_time]),
        period=_period(),
    )
    assert fail_safe.bundle["status"] == MetricStatus.PARTIAL
    assert fail_safe.bundle["missing"] == ("economic_adjust_invalid",)

    expiration_only = project_trade_events(
        [
            _event("aug-open", "open", "2026-08-01T10:00:00", lot_id="aug-lot"),
            _event(
                "aug-expiration-adjust",
                "adjust",
                "2026-08-02T10:00:00",
                target_lot_id="aug-lot",
                contracts=0,
                price=0,
                raw={"patch": {"expiration_ymd": "2026-07-31"}},
            ),
            _event("sep-open", "open", "2026-09-01T10:00:00", lot_id="sep-lot"),
        ]
    )
    expiration_reduction = reduce_option_performance(
        expiration_only,
        period=_period(),
    )
    assert [fact.open_lot_id for fact in expiration_reduction.facts] == ["sep-lot"]
    assert expiration_reduction.bundle["status"] == MetricStatus.OBSERVED


def test_currency_conflict_fails_only_the_affected_scoped_allocation() -> None:
    lx_key = _key(account="lx")
    sy_key = _key(account="sy", broker="ibkr")
    lx_close = replace(
        _event(
            "lx-close",
            "close",
            "2026-09-02T10:00:00",
            key=lx_key,
            target_lot_id="lx-lot",
            close_type="buy_to_close",
            price=2,
        ),
        currency="HKD",
    )
    projection = project_trade_events(
        [
            _event(
                "lx-open",
                "open",
                "2026-09-01T10:00:00",
                key=lx_key,
                lot_id="lx-lot",
            ),
            lx_close,
            _event(
                "sy-open",
                "open",
                "2026-09-01T10:00:00",
                key=sy_key,
                lot_id="sy-lot",
            ),
        ]
    )

    sy = reduce_option_performance(
        projection,
        period=_period(),
        account="sy",
        broker="ibkr",
    )
    assert [fact.account for fact in sy.facts] == ["sy"]
    assert [fact.broker for fact in sy.facts] == ["ibkr"]
    assert sy.bundle["status"] == MetricStatus.OBSERVED

    aggregate = reduce_option_performance(projection, period=_period())
    assert [fact.account for fact in aggregate.facts] == ["lx", "sy"]
    assert aggregate.bundle["status"] == MetricStatus.PARTIAL
    assert aggregate.bundle["missing"] == ("currency_conflict",)
    usd_cash = aggregate.bundle["option_net_cashflow"]["by_currency"]["USD"]["total"]
    assert usd_cash["amount"] is None
    assert usd_cash["status"] == MetricStatus.PARTIAL
    assert aggregate.bundle["sell_option_win_rate"]["rate"] is None
    assert aggregate.bundle["sell_option_win_rate"]["status"] == MetricStatus.PARTIAL
    assert aggregate.bundle["option_return"]["by_currency"]["USD"]["rate"] is None
    account_rows = {row["key"]: row for row in aggregate.breakdowns["accounts"]}
    assert account_rows["lx"]["status"] == MetricStatus.PARTIAL
    assert account_rows["sy"]["status"] == MetricStatus.OBSERVED
