from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from decimal import Decimal
import json

import pytest

from domain.domain.ledger import ContractKey, TradeEvent, project_trade_events
from domain.domain.ledger.cash_facts import OptionFeeCashEvent, cash_facts_for_trade_event
from domain.domain.ledger.events import LedgerDiagnostic
from domain.domain.performance.cash_conversion import cash_conversion_id, cash_conversion_identity
from domain.domain.performance.period import PeriodWindow
from domain.domain.performance.period_cashflow import reduce_period_option_cashflow
from domain.domain.performance.weighted_reducer import reduce_option_performance

T0 = 1788192000000
DAY = 86400000


def _period(start=T0, end=T0 + 30 * DAY):
    return PeriodWindow("month", "UTC", "2026-09-01", "2026-09-30", start, end,
                        start, end - 1, "complete")


def _event(identity="open", kind="open", at=T0, *, price=2, contracts=1, target="lot",
           side="short", currency="CNY", account="lx", broker="futu", fee=0,
           basis="actual", raw=None, option_type="put", **kwargs):
    payload = {"side": "sell" if (side == "short") == (kind == "open") else "buy"}
    if basis is not None:
        payload["fee_provenance"] = {"amount": fee, "basis": basis, "source": "fixture"}
    payload.update(raw or {})
    return TradeEvent(
        event_id=identity, event_type=kind, event_time_ms=at,
        contract_key=ContractKey.from_values(broker=broker, account=account,
            underlying_symbol="NVDA", option_type=option_type, strike=100, expiration_ymd="2026-12-31"),
        contracts=contracts, price=price, currency=currency, multiplier=100,
        source="fixture", fees=0 if basis == "estimated" else fee,
        lot_id=target if kind == "open" else None,
        target_lot_id=target if kind in {"close", "expire_close", "assignment", "exercise", "adjust"} else None,
        raw_payload=payload, **kwargs,
    )


def _fee(identity="fee", source="open", at=T0, amount=-2, **kwargs):
    return OptionFeeCashEvent(identity, source, at, kwargs.pop("currency", "CNY"),
                             amount, kwargs.pop("basis", "actual"), "archived-fee", **kwargs)


def _reduce(events=(), *, period=None, **kwargs):
    return reduce_period_option_cashflow(project_trade_events(events), period=period or _period(),
                                        accounts=kwargs.pop("accounts", ("lx",)), **kwargs)


def _total(metric, currency="CNY"):
    return metric["by_currency"][currency]["total"]["amount"]


@pytest.mark.parametrize("case,opening_at,closing_kind,opening_price,closing_price,side,expected", [
    ("A_prior_open_current_close", T0-DAY, "close", 2, 3, "short", -300),
    ("B_current_open", T0, None, 2, 0, "short", 200),
    ("C_current_round_trip", T0, "close", 2, .8, "short", 120),
    ("D_expiry_no_rebooking", T0-DAY, "expire_close", 2, 0, "short", 0),
    ("E_long_round_trip", T0, "close", .8, 1.5, "long", 70),
    ("F_prior_long_current_sale", T0-DAY, "close", .8, 1.5, "long", 150),
    ("G_physical_assignment", T0, "assignment", 2, 0, "short", 200),
    ("H_physical_exercise", T0, "exercise", .8, 0, "long", -80),
    ("I_current_open_then_expiry", T0, "expire_close", 2, 0, "short", 200),
])
def test_a_i_event_time_cash(case, opening_at, closing_kind, opening_price, closing_price, side, expected):
    events = [_event(at=opening_at, price=opening_price, side=side)]
    if closing_kind:
        events.append(_event("close", closing_kind, T0+DAY, price=closing_price, side=side,
                             raw={"stock_settlement": {"shares": 100, "price": 100,
                                  "side": "buy", "currency": "CNY", "fees": 200}}))
    metric, facts = _reduce(events)
    assert _total(metric) == Decimal(expected), case
    assert metric["status"] == "observed"
    assert metric["cny_total"]["amount"] == Decimal(expected)
    assert all(f["fact_kind"] in {"option_trade_cash_gross", "option_fee_cash"} for f in facts)
    assert metric["fact_count"] == len(facts)
    if case.startswith("A"):
        result = reduce_option_performance(project_trade_events(events), period=_period(), accounts=("lx",))
        assert result.bundle["option_net_cashflow"]["by_currency"] == {}
        assert result.cashflow_facts == facts
        assert result.bundle["period_option_net_cashflow"] == metric


def test_half_open_adjacent_windows_add_same_snapshot_including_boundary_fee():
    events = [_event(at=T0), _event("close", "close", T0+DAY, price=.8)]
    fee = _fee("new-charge", at=T0+DAY, amount=-1)
    left, _ = _reduce(events, period=_period(T0, T0+DAY), fee_cash_events=(fee,))
    right, _ = _reduce(events, period=_period(T0+DAY, T0+2*DAY), fee_cash_events=(fee,))
    combined, _ = _reduce(events, period=_period(T0, T0+2*DAY), fee_cash_events=(fee,))
    assert _total(left) == 200
    assert _total(right) == -81
    assert _total(left) + _total(right) == _total(combined)


def test_partial_closes_across_lots_count_event_cash_once():
    events = [_event("one", at=T0-DAY, contracts=3, target="one"),
              _event("two", at=T0-DAY, contracts=2, target="two", price=1),
              _event("one-close", "close", T0, target="one", contracts=2, price=.8, fee=2),
              _event("two-close", "close", T0+DAY, target="two", contracts=1, price=1.5, fee=1)]
    metric, facts = _reduce(events)
    assert _total(metric) == -313
    assert metric["by_currency"]["CNY"]["gross"]["amount"] == -310
    assert metric["by_currency"]["CNY"]["fees"]["amount"] == -3
    assert len(facts) == 4
    assert {f["attribution"][0]["open_lot_id"] for f in facts} == {"one", "two"}


def test_replacement_full_value_restates_old_period_supplemental_later_charge_and_refund():
    event = _event(at=T0-DAY, fee=2)
    corrected = _fee("correction", at=T0-DAY, amount=-3, replaces_attached_fee=True)
    charge = _fee("later-charge", at=T0, amount=-1)
    refund = _fee("later-refund", at=T0+DAY, amount=1)
    old, _ = _reduce([event], period=_period(T0-2*DAY, T0))
    past, facts = _reduce([event], period=_period(T0-2*DAY, T0),
                         fee_cash_events=(corrected, charge, refund, corrected))
    now, now_facts = _reduce([event], fee_cash_events=(refund, corrected, charge))
    assert _total(old) == 198
    assert _total(past) == 197
    assert _total(now) == 0
    assert len(facts) == len(now_facts) == 2
    assert {f["fact_id"] for f in facts} == {"option_trade_cash_gross:open", corrected.fact_id}
    assert {f["fact_id"] for f in now_facts} == {charge.fact_id, refund.fact_id}
    assert now["by_currency"]["CNY"]["gross"]["amount"] == 0
    assert _reduce([event], period=_period(T0-2*DAY, T0))[0] == old


@pytest.mark.parametrize("fees,match", [
    ((_fee(at=T0+DAY, replaces_attached_fee=True),), "effective time"),
    ((_fee(replaces_attached_fee=True), _fee("other", replaces_attached_fee=True)), "at most one"),
    ((_fee(), _fee(amount=-3)), "conflicting fee identity"),
    ((_fee(source="missing"),), "effective option trade"),
])
def test_rejects_ambiguous_fee_identity_and_timing(fees, match):
    with pytest.raises(ValueError, match=match):
        _reduce([_event()], fee_cash_events=fees)


@pytest.mark.parametrize("control", ["void", "repair"])
def test_canonical_void_or_repair_excludes_old_cash_and_rejects_orphan_fee(control):
    old = _event()
    cancel = _event("cancel", control, T0+DAY, price=0, contracts=0, target_event_id="open")
    events = [old, cancel]
    if control == "repair":
        events.append(_event("void", "void", T0+DAY, price=0, contracts=0, target_event_id="open"))
        events.append(_event("corrected", at=T0, price=3, target="new"))
    metric, facts = _reduce(events)
    assert metric["cny_total"]["amount"] == (300 if control == "repair" else 0)
    assert all(f["source_event_id"] != "open" for f in facts)
    with pytest.raises(ValueError, match="effective option trade"):
        _reduce(events, fee_cash_events=(_fee(),))


def test_canonical_adjustment_changes_effective_cash_not_adjustment_time():
    event = _event(at=T0-DAY)
    adjust = _event("adjust", "adjust", T0+DAY, contracts=0, price=0,
                    raw={"patch": {"premium": "3"}})
    projection = project_trade_events([event, adjust])
    assert not projection.has_errors
    now, _ = _reduce([event, adjust])
    past, _ = _reduce([event, adjust], period=_period(T0-2*DAY, T0))
    assert now["fact_count"] == 0
    assert _total(past) == 300


def test_exact_resolved_duplicates_deduplicate_conflicts_fail():
    event = _event()
    projection = project_trade_events([event])
    expected = reduce_period_option_cashflow(projection, period=_period(), accounts=("lx",))
    duplicate = replace(projection, effective_cash_events=(event, event))
    assert reduce_period_option_cashflow(duplicate, period=_period(), accounts=("lx",)) == expected
    with pytest.raises(ValueError, match="conflicting cash event identity"):
        reduce_period_option_cashflow(replace(projection, effective_cash_events=(event, replace(event, price=3))),
                                      period=_period(), accounts=("lx",))


@pytest.mark.parametrize("kind,side", [("assignment", "short"), ("exercise", "long"), ("expire_close", "short")])
def test_nonzero_physical_terminal_price_is_partial(kind, side):
    metric, facts = _reduce([_event(at=T0-DAY, side=side),
                            _event("close", kind, T0, price=1, side=side)])
    assert _total(metric) is None
    assert "physical_settlement_option_cash_invalid" in metric["missing"]
    assert metric["by_currency"]["CNY"]["fees"]["amount"] == 0
    assert next(f for f in facts if f["fact_kind"] == "option_trade_cash_gross")["amount"] is None


def test_missing_actual_fee_and_explicit_simulation():
    missing, _ = _reduce([_event(basis=None)])
    assert missing["by_currency"]["CNY"]["gross"]["amount"] == 200
    assert _total(missing) is None
    actual, _ = _reduce([_event(basis="estimated", fee=2)])
    simulated, facts = _reduce([_event(basis="estimated", fee=2)], fee_mode="simulated")
    assert _total(actual) is None
    assert _total(simulated) == 198
    assert simulated["status"] == simulated["cny_total"]["status"] == "estimated"
    assert any(f["basis"] == "estimated" for f in facts)
    assert simulated["by_currency"]["CNY"]["gross"]["status"] == "observed"
    assert _total(_reduce([_event(basis=None)], fee_mode="simulated")[0]) is None


@pytest.mark.parametrize("basis,amount", [("actual", 1), ("estimated", -1), ("missing", None)])
def test_supplemental_fee_basis_and_signed_cash(basis, amount):
    fee = _fee(basis=basis, amount=amount)
    metric, _ = _reduce([_event()], fee_cash_events=(fee,), fee_mode="simulated")
    assert _total(metric) == (None if amount is None else 200+amount)
    assert metric["status"] == {"actual": "observed", "estimated": "estimated", "missing": "partial"}[basis]


def _conversion(fact_id, amount, currency="USD", at=T0):
    identity = cash_conversion_identity(cash_fact_id=fact_id, native_amount=amount, native_currency=currency,
        fx_rate=7, amount_cny=Decimal(amount)*7, rate_source_id="archived-fx", effective_at_ms=at)
    return {**identity, "schema_version": "cash_conversion.v1", "status": "observed",
            "quote_currency": "CNY", "method": "booking_fx_snapshot", "rate_source": "fixture",
            "observed_at_ms": at+1, "rate_timestamp": datetime.fromtimestamp(at/1000, timezone.utc).isoformat(),
            "conversion_id": cash_conversion_id(identity)}


def test_historical_fx_uses_exact_fact_binding_and_separates_native_quality():
    event = _event(currency="USD")
    conversion = _conversion("option_trade_cash_gross:open", 200)
    event = replace(event, raw_payload={**event.raw_payload, "cash_conversions": {
        "option_trade_cash_gross": conversion}})
    fee = _fee(currency="USD", amount=-2)
    fee = replace(fee, cash_conversion=_conversion(fee.fact_id, -2))
    metric, _ = _reduce([event], fee_cash_events=(fee,))
    assert _total(metric, "USD") == 198
    assert metric["cny_total"]["amount"] == 1386
    for field, value in [("cash_fact_id", "wrong"), ("native_amount", 300), ("native_currency", "HKD"),
                         ("effective_at_ms", T0+1), ("conversion_id", "bad")]:
        invalid = replace(fee, cash_conversion={**fee.cash_conversion, field: value})
        broken, _ = _reduce([event], fee_cash_events=(invalid,))
        assert _total(broken, "USD") == 198
        assert broken["cny_total"]["amount"] is None
        assert any(reason.startswith("cash_conversion_corrupt:") for reason in broken["missing"])
    unavailable, _ = _reduce([_event(currency="USD")])
    assert _total(unavailable, "USD") == 200
    assert unavailable["cny_total"]["amount"] is None
    assert unavailable["missing"] == ("cash_conversion_missing",)


def test_zero_fx_and_cny_identity_need_no_conversion_but_native_currencies_never_merge():
    zero, _ = _reduce([_event(currency="USD", price=0)])
    assert zero["cny_total"]["amount"] == 0
    assert zero["status"] == "observed"
    mixed, _ = _reduce([_event(currency="USD"), _event("hk", currency="HKD", target="hk"),
                        _event("cn", currency="CNY", target="cn")])
    assert set(mixed["by_currency"]) == {"USD", "HKD", "CNY"}
    assert all(_total(mixed, currency) == 200 for currency in mixed["by_currency"])
    assert mixed["cny_total"]["amount"] is None


def test_scope_aliases_account_subsets_and_fee_linked_scope():
    events = [_event(), _event("sy", account="sy", target="sy"),
              _event("ib", broker="ibkr", target="ib")]
    fee = _fee(source="sy")
    futu, facts = _reduce(events, accounts=(" LX ",), broker="FUTU", fee_cash_events=(fee,))
    chinese, _ = _reduce(events, broker="富途", fee_cash_events=(fee,))
    assert futu == chinese
    assert _total(futu) == 200
    assert {f["account"] for f in facts} == {"lx"}
    assert {f["broker"] for f in facts} == {"富途"}
    sy, _ = _reduce(events, accounts=("sy",), fee_cash_events=(fee,))
    assert _total(sy) == 198
    empty, facts = _reduce(events, broker="other")
    assert empty["by_currency"] == {} and facts == ()
    assert empty["status"] == "observed" and empty["cny_total"]["amount"] == 0
    first = reduce_option_performance(project_trade_events(events), period=_period(), accounts=("lx",), broker="FUTU")
    second = reduce_option_performance(project_trade_events(events), period=_period(), accounts=("lx",), broker="富途")
    assert first == second


@pytest.mark.parametrize("kwargs", [{"accounts": "lx"}, {"accounts": ()}, {"accounts": ("",)},
                                    {"accounts": (1,)}, {"broker": " "}, {"broker": 5}, {"fee_mode": "bad"}])
def test_invalid_scope_or_policy_rejected(kwargs):
    with pytest.raises(ValueError):
        _reduce([_event()], **kwargs)


@pytest.mark.parametrize("start,end", [(-1, T0), (T0,T0), (T0+1,T0), (True,T0), (1.5,T0)])
def test_invalid_window_rejected(start, end):
    with pytest.raises(ValueError, match="half-open"):
        _reduce(period=_period(start,end))


@pytest.mark.parametrize("details", [{}, {"target_lot_id": "missing"},
                                    {"target_lot_id": "lot", "cohort_time_unreliable": True}])
def test_unlocatable_economic_damage_fails_closed_even_without_cash(details):
    projection = project_trade_events([_event(at=T0-DAY)])
    diagnostic = LedgerDiagnostic("bad", "warning", "economic_adjust_invalid", "fixture", details=details)
    metric, _ = reduce_period_option_cashflow(replace(projection, diagnostics=[diagnostic]),
                                              period=_period(), accounts=("lx",))
    assert metric["status"] == "partial"
    assert metric["cny_total"]["amount"] is None
    assert "economic_adjust_invalid" in metric["missing"]


def test_foreign_diagnostics_isolate_and_attribution_only_does_not_damage_cash():
    events = [_event(), _event("sy", account="sy", target="sy")]
    projection = project_trade_events(events)
    foreign = LedgerDiagnostic("bad", "warning", "economic_adjust_invalid", "fixture", account="sy")
    attribution = LedgerDiagnostic("open", "warning", "combo_membership_incomplete", "fixture",
                                   details={"target_lot_id": "lot"}, account="lx")
    metric, facts = reduce_period_option_cashflow(replace(projection, diagnostics=[foreign, attribution]),
                                                  period=_period(), accounts=("lx",))
    assert metric["status"] == "observed" and _total(metric) == 200
    assert all("strategy_attribution_conflict" in f["attribution_missing"] for f in facts)
    foreign_by_lot = replace(foreign, account=None, details={"target_lot_id": "sy"})
    assert reduce_period_option_cashflow(replace(projection, diagnostics=[foreign_by_lot]),
                                        period=_period(), accounts=("lx",))[0]["status"] == "observed"


def test_missing_currency_or_native_amount_is_not_zero():
    event = _event()
    projection = project_trade_events([event])
    for broken in (replace(event, currency=""), replace(event, price="bad")):
        metric, _ = reduce_period_option_cashflow(replace(projection, effective_cash_events=(broken,)),
                                                  period=_period(), accounts=("lx",))
        assert metric["status"] == "partial"
        assert metric["cny_total"]["amount"] is None


def test_fee_serialization_validation_frozen_input_and_order_invariance():
    fee = _fee(currency="rmb", cash_conversion={"nested": {"value": 1}})
    serialized = fee.to_dict()
    assert serialized["currency"] == "CNY"
    assert serialized["amount"] == "-2.000000"
    assert json.loads(json.dumps(serialized)) == serialized
    serialized["cash_conversion"]["nested"]["value"] = 2
    assert fee.cash_conversion["nested"]["value"] == 1
    with pytest.raises(FrozenInstanceError):
        fee.amount = Decimal(9)
    events, fees = [_event()], (_fee(), _fee("refund", amount=1))
    before = deepcopy((events, fees))
    assert _reduce(events, fee_cash_events=fees) == _reduce(events, fee_cash_events=reversed(fees))
    assert (events, fees) == before


@pytest.mark.parametrize("patch", [{"fee_id": ""}, {"source_event_id": " "}, {"source": None},
    {"effective_at_ms": True}, {"effective_at_ms": 0}, {"effective_at_ms": "123"}, {"effective_at_ms": 1.5},
    {"basis": "other"}, {"amount": "NaN"}, {"amount": None}, {"basis": "missing", "amount": 0},
    {"currency": "BAD"}, {"replaces_attached_fee": 1}, {"cash_conversion": []}])
def test_fee_event_rejects_invalid_contract(patch):
    with pytest.raises((ValueError, TypeError)):
        replace(_fee(), **patch)


@pytest.mark.parametrize("carry_in", [False, True])
@pytest.mark.parametrize("mismatch", ["multiplier", "currency"])
def test_known_economic_damage_isolates_native_currency_but_degrades_cny_aggregate(carry_in, mismatch):
    opening = _event("usd", at=T0-DAY if carry_in else T0, currency="USD", target="usd")
    closing = _event("usd-close", "close", T0+DAY, currency="USD", target="usd", price=1)
    closing = replace(closing, **({"multiplier": 200} if mismatch == "multiplier" else {"currency": "HKD"}))
    healthy = _event("cny", target="cny", fee=2)
    projection = project_trade_events([opening, closing, healthy])
    assert any(d.code == "target_economic_units_mismatch" for d in projection.diagnostics)
    metric, _ = reduce_period_option_cashflow(projection, period=_period(), accounts=("lx",))
    baseline, _ = _reduce([healthy])
    assert metric["by_currency"]["CNY"] == baseline["by_currency"]["CNY"]
    affected = {"USD"} if mismatch == "multiplier" else {"USD", "HKD"}
    for currency in affected:
        for component in metric["by_currency"][currency].values():
            assert component["amount"] is None
            assert component["status"] == "partial"
            assert "currency_conflict" in component["missing"]
    assert metric["cny_total"]["amount"] is None
    assert metric["cny_total"]["status"] == metric["status"] == "partial"
    assert "currency_conflict" in metric["cny_total"]["missing"]


@pytest.mark.parametrize("damage", ["unknown_target", "unknown_currency", "invalid_event_currency"])
def test_unresolved_diagnostic_currency_keeps_native_totals_conservatively_partial(damage):
    projection = project_trade_events([_event("usd", currency="USD", target="usd"), _event("cny", target="cny")])
    details = {"target_lot_id": "missing" if damage == "unknown_target" else "usd"}
    if damage == "unknown_currency":
        projection = replace(projection, lots=[replace(lot, currency="") if lot.lot_id == "usd" else lot
                                               for lot in projection.lots])
    if damage == "invalid_event_currency":
        details.update(lot_currency="USD", event_currency="BAD")
    diagnostic = LedgerDiagnostic("damaged-close", "error", "target_economic_units_mismatch", "fixture",
                                  details=details, account="lx")
    metric, _ = reduce_period_option_cashflow(replace(projection, diagnostics=[diagnostic]),
                                              period=_period(), accounts=("lx",))
    assert metric["cny_total"]["amount"] is None
    for components in metric["by_currency"].values():
        for component in components.values():
            assert component["amount"] is None
            assert "currency_conflict" in component["missing"]


def test_epoch_zero_window_preserves_cash_selection_and_weighted_entry():
    events = [_event(at=1), _event("close", "close", 3, price=1)]
    period = _period(0, 3)
    metric, facts = _reduce(events, period=period)
    assert metric["status"] == "observed"
    assert _total(metric) == 200
    assert {fact["source_event_id"] for fact in facts} == {"open"}
    result = reduce_option_performance(project_trade_events(events), period=period, accounts=("lx",))
    assert result.bundle["period_option_net_cashflow"] == metric
    assert result.cashflow_facts == facts
