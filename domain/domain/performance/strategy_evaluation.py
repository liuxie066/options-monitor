"""Offline valuation and cash facts on frozen inputs; backtest metrics live in OB."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from domain.domain.fx_quote_policy import CALENDAR_EVIDENCE, quote_quality
from domain.domain.ledger.cash_facts import cash_facts_for_trade_event
from domain.domain.money import quantize_money, to_decimal
from domain.domain.option_position_identity import normalize_currency
from domain.domain.performance.cash_conversion import (
    HISTORICAL_BUSINESS_DAY_FX_CARRY_FORWARD_METHOD,
    MARKET_CASH_FX_METHOD,
    cash_conversion_id,
    cash_conversion_identity,
    select_cash_fx_rate,
    validate_observed_cash_conversion,
)
from domain.domain.performance.models import FXRateFact
from domain.domain.performance.period import PeriodWindow

ZERO = Decimal(0)
MODEL = "synthetic_linear_quantity_cost.v1"


class IncompleteCashError(ValueError):
    """Unknown spendable cash: the caller must stop dependent execution."""


def _integer(value, name, *, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty")
    return value.strip()


def _currency(value):
    return normalize_currency(value, strict=True)


def _nonnegative(value, name):
    result = to_decimal(value, field_name=name)
    if result < 0:
        raise ValueError(f"{name} must be nonnegative")
    return result


def _fx_facts(rows):
    facts = [row if isinstance(row, FXRateFact) else FXRateFact(**row) for row in rows]
    seen = {}
    for fact in facts:
        if fact.fact_id in seen and seen[fact.fact_id] != fact.normalized_payload():
            raise ValueError(f"conflicting FX fact: {fact.fact_id}")
        seen[fact.fact_id] = fact.normalized_payload()
    return facts


def _rate(facts, currency, at_ms):
    if currency == "CNY":
        return Decimal(1), None
    selected = select_cash_fx_rate(
        [f for f in facts if f.effective_at_ms <= at_ms and f.observed_at_ms <= at_ms],
        base_currency=currency,
        at_ms=at_ms,
    )
    if selected.fact is None:
        raise IncompleteCashError(f"fx:{currency}:{at_ms}")
    return selected.fact.rate, selected.fact


def _cross_rate(facts, currency, base_currency, at_ms):
    if currency == base_currency:
        return Decimal(1)
    return _rate(facts, currency, at_ms)[0] / _rate(facts, base_currency, at_ms)[0]


def _market(market, at_ms):
    _integer(at_ms, "at_ms", minimum=1)
    if not isinstance(market, dict) or set(market) != {"stocks", "options", "fx"}:
        raise ValueError("market must contain exactly stocks/options/fx")
    if any(not isinstance(market[key], list) for key in market):
        raise ValueError("market values must be lists")
    chosen = {}
    for kind, identity in (("stocks", "symbol"), ("options", "contract_id")):
        rows, seen = {}, {}
        fields = {identity, "currency", "source", "effective_at_ms", "available_at_ms", "valid_until_ms"}
        fields |= {"price"} if kind == "stocks" else {"bid", "ask"}
        for raw in market[kind]:
            approximate = isinstance(raw, dict) and raw.get("time_basis") == "received_snapshot"
            if not isinstance(raw, dict) or set(raw) != fields | ({"time_basis"} if approximate else set()):
                raise ValueError(f"invalid {kind} mark schema")
            row = dict(raw)
            row[identity] = _text(row[identity], identity)
            row["currency"] = _currency(row["currency"])
            _text(row["source"], "source")
            available, until = (_integer(row[key], key, minimum=1) for key in ("available_at_ms", "valid_until_ms"))
            if approximate:
                if row["effective_at_ms"] is not None or not available <= until <= available + 300_000:
                    raise ValueError("received snapshot requires unknown effective time and at most 300s validity")
                stamp = available
            else:
                stamp = _integer(row["effective_at_ms"], "effective_at_ms", minimum=1)
                if not stamp <= available <= until:
                    raise ValueError("mark times must satisfy effective<=available<=valid_until")
            if kind == "stocks":
                row["price"] = _nonnegative(row["price"], "price")
                prices = (row["price"],)
            else:
                row["bid"], row["ask"] = _quote(row)
                prices = (row["bid"], row["ask"])
                row["price"] = (row["bid"] + row["ask"]) / 2
            key = (row[identity], row["currency"])
            conflict = (*key, stamp)
            if conflict in seen and seen[conflict] != prices:
                raise ValueError(f"conflicting {kind} mark: {key}")
            seen[conflict] = prices
            if available <= at_ms <= until:
                previous = rows.get(key)
                if previous is None or (stamp, available, row["source"]) > (
                    previous["available_at_ms"]
                    if previous.get("time_basis") == "received_snapshot"
                    else previous["effective_at_ms"],
                    previous["available_at_ms"],
                    previous["source"],
                ):
                    rows[key] = row
        chosen[kind] = rows
    return chosen, _fx_facts(market["fx"])


def eligible_stock_marks(*, market: dict, at_ms: int) -> list[dict]:
    chosen, _ = _market(market, at_ms)
    fields = ("symbol", "currency", "price", "effective_at_ms", "available_at_ms", "valid_until_ms")
    return [
        {key: row[key] for key in (*fields, *(("time_basis",) if "time_basis" in row else ()))}
        for _, row in sorted(chosen["stocks"].items())
    ]


def legacy_market(*, market: dict, base_currency: str, at_ms: int) -> dict:
    base_currency = _currency(base_currency)
    chosen, facts = _market(market, at_ms)
    currencies = (
        {base_currency}
        | {f.base_currency for f in facts}
        | {row["currency"] for kind in chosen.values() for row in kind.values()}
    )
    rates, gaps = {}, set()
    for currency in sorted(currencies):
        try:
            rates[currency] = _cross_rate(facts, currency, base_currency, at_ms)
        except IncompleteCashError as exc:
            gaps.add(str(exc))
    return {
        "status": "incomplete" if gaps else "complete",
        "gaps": sorted(gaps),
        "fx_rates": rates,
        "stock_marks": [
            {key: row[key] for key in ("symbol", "currency", "price")} for _, row in sorted(chosen["stocks"].items())
        ],
        "option_marks": [
            {key: row[key] for key in ("contract_id", "currency", "price")}
            for _, row in sorted(chosen["options"].items())
        ],
    }


def decision_exchange_rates(*, fx: list[dict], at_ms: int) -> dict:
    """Project qualified facts in the legacy input directions without inverse round trips."""
    _integer(at_ms, "at_ms", minimum=1)
    facts = _fx_facts(fx)
    rates, gaps = {}, set()
    for field, source, target in (
        ("usd_per_cny", "CNY", "USD"),
        ("cny_per_hkd", "HKD", "CNY"),
    ):
        try:
            rates[field] = float(_cross_rate(facts, source, target, at_ms))
        except IncompleteCashError as exc:
            gaps.add(str(exc))
    return {
        "status": "incomplete" if gaps else "complete",
        "gaps": sorted(gaps),
        "exchange_rates": rates,
    }


class EvaluationBook:
    def __init__(self, annual_rates: dict[str, str | Decimal | None]):
        self.annual_rates = {
            _currency(key): None if value is None else to_decimal(value) for key, value in annual_rates.items()
        }
        self.cash_events = []
        self._last_day = None
        self._last_interest_at = None
        self._dividends = {}
        self._paid = set()

    def accrue_interest(self, *, day: str, at_ms: int, cash: dict[str, Decimal]) -> dict[str, Decimal]:
        current = date.fromisoformat(day)
        _integer(at_ms, "at_ms", minimum=1)
        if self._last_day is not None and current != self._last_day + timedelta(days=1):
            raise ValueError("interest requires consecutive natural days without duplicates")
        if self._last_interest_at is not None and at_ms <= self._last_interest_at:
            raise ValueError("interest times must increase")
        if cash is None or any(value is None for value in cash.values()):
            raise IncompleteCashError("cash_balance_missing")
        balances = {_currency(key): to_decimal(value) for key, value in cash.items()}
        missing = sorted(key for key in balances if self.annual_rates.get(key) is None)
        if missing:
            raise IncompleteCashError("annual_rate:" + ",".join(missing))
        deltas = {key: quantize_money(value * self.annual_rates[key] / 365) for key, value in balances.items()}
        self.cash_events.extend(
            {"id": f"interest:{day}:{key}", "kind": "interest", "at_ms": at_ms, "currency": key, "amount": value}
            for key, value in sorted(deltas.items())
        )
        self._last_day, self._last_interest_at = current, at_ms
        return deltas

    def ex_dividend(self, *, dividend: dict, stocks: dict[tuple[str, str], int], at_ms: int) -> None:
        fields = {
            "dividend_id",
            "symbol",
            "currency",
            "net_per_share",
            "ex_at_ms",
            "pay_at_ms",
            "source",
            "available_at_ms",
        }
        if not isinstance(dividend, dict) or set(dividend) != fields:
            raise ValueError("dividend requires explicit identity, source and times")
        identity = _text(dividend["dividend_id"], "dividend_id")
        symbol, currency = _text(dividend["symbol"], "symbol"), _currency(dividend["currency"])
        _text(dividend["source"], "dividend source")
        ex, pay, available = (
            _integer(dividend[key], key, minimum=1) for key in ("ex_at_ms", "pay_at_ms", "available_at_ms")
        )
        _integer(at_ms, "at_ms", minimum=1)
        if at_ms != ex or pay < ex or available > ex:
            raise ValueError("dividend must be known at ex, recognized exactly at ex and paid no earlier")
        if identity in self._dividends:
            raise ValueError("duplicate dividend_id")
        if dividend["net_per_share"] is None:
            raise IncompleteCashError(f"dividend_net_per_share:{identity}")
        per_share = _nonnegative(dividend["net_per_share"], "net_per_share")
        quantity = _integer(stocks.get((symbol, currency), 0), "dividend quantity")
        self._dividends[identity] = {
            **deepcopy(dividend),
            "currency": currency,
            "amount": quantize_money(per_share * quantity),
        }

    def pay_dividend(self, *, dividend_id: str, at_ms: int) -> dict[str, Decimal]:
        _integer(at_ms, "at_ms", minimum=1)
        if dividend_id not in self._dividends:
            raise ValueError("unknown dividend_id")
        row = self._dividends[dividend_id]
        if at_ms < row["pay_at_ms"]:
            raise ValueError("dividend cannot pay before pay_at_ms")
        if dividend_id in self._paid:
            return {}
        self._paid.add(dividend_id)
        self.cash_events.append(
            {
                "id": f"dividend:{dividend_id}",
                "kind": "dividend",
                "at_ms": at_ms,
                "currency": row["currency"],
                "amount": row["amount"],
            }
        )
        return {row["currency"]: row["amount"]}

    def receivables(self) -> list[dict]:
        return [deepcopy(row) for key, row in sorted(self._dividends.items()) if key not in self._paid]


def _ratios(stock_values, nav):
    valid = nav is not None and nav > 0 and all(value is not None for value in stock_values.values())
    return {
        "overall": sum(stock_values.values(), ZERO) / nav if valid else None,
        "by_symbol": {key: value / nav if valid else None for key, value in sorted(stock_values.items())},
    }


def value_portfolio(
    *,
    point_id: str,
    at_ms: int,
    base_currency: str,
    positions: dict,
    receivables: list[dict],
    market: dict,
    assignment_fees: dict,
) -> dict:
    _text(point_id, "point_id")
    base_currency = _currency(base_currency)
    marks, facts = _market(market, at_ms)
    required = {"cash_by_currency", "stock_positions", "short_option_positions"}
    if not isinstance(positions, dict) or not required <= positions.keys():
        raise ValueError("positions must contain cash, stock and short option snapshots")
    cash = {_currency(key): to_decimal(value) for key, value in positions["cash_by_currency"].items()}
    stock, options = {}, {}
    for row in positions["stock_positions"]:
        key = (_text(row["symbol"], "symbol"), _currency(row["currency"]))
        if key in stock:
            raise ValueError("duplicate stock position")
        stock[key] = _integer(row["quantity"], "stock quantity")
    for row in positions["short_option_positions"]:
        identity = _text(row["contract_id"], "contract_id")
        if identity in options:
            raise ValueError("duplicate option position")
        item = {
            **row,
            "currency": _currency(row["currency"]),
            "strike": _nonnegative(row["strike"], "strike"),
            "contracts": _integer(row["contracts"], "contracts", minimum=1),
            "multiplier": _integer(row["multiplier"], "multiplier", minimum=1),
            "underlying_symbol": _text(row["underlying_symbol"], "underlying_symbol"),
            "option_type": row.get("option_type", "put"),
        }
        if item["option_type"] not in {"put", "call"}:
            raise ValueError("unsupported option_type")
        options[identity] = item
    recv, recv_ids = {}, set()
    for row in receivables:
        identity = _text(row["dividend_id"], "dividend_id")
        if identity in recv_ids:
            raise ValueError("duplicate dividend receivable")
        recv_ids.add(identity)
        currency = _currency(row["currency"])
        recv[currency] = recv.get(currency, ZERO) + _nonnegative(row["amount"], "receivable amount")

    def valuation(balances, shares, contracts):
        gaps, native = set(), {"cash": dict(balances), "stock": {}, "option": {}, "receivable": dict(recv)}
        symbol_native = {}
        for (symbol, currency), quantity in sorted(shares.items()):
            if not quantity:
                continue
            mark = marks["stocks"].get((symbol, currency))
            value = None if mark is None else quantize_money(mark["price"] * quantity)
            if mark is None:
                gaps.add(f"stock_mark:{symbol}:{currency}")
            bucket = native["stock"]
            bucket[currency] = (
                None
                if value is None or (currency in bucket and bucket[currency] is None)
                else (bucket.get(currency, ZERO) + value)
            )
            symbol_native[(symbol, currency)] = value
        for identity, row in sorted(contracts.items()):
            currency = row["currency"]
            mark = marks["options"].get((identity, currency))
            value = None if mark is None else quantize_money(-mark["price"] * row["multiplier"] * row["contracts"])
            if mark is None:
                gaps.add(f"option_mark:{identity}")
            bucket = native["option"]
            bucket[currency] = (
                None
                if value is None or (currency in bucket and bucket[currency] is None)
                else (bucket.get(currency, ZERO) + value)
            )
        rates = {}
        for currency in sorted({key for values in native.values() for key in values}):
            needs_rate = any(values.get(currency) != 0 for values in native.values() if currency in values)
            try:
                rates[currency] = _cross_rate(facts, currency, base_currency, at_ms) if needs_rate else Decimal(1)
            except IncompleteCashError as exc:
                rates[currency] = None
                gaps.add(str(exc))
        components = {}
        for kind, values in native.items():
            components[kind] = (
                None
                if any(value is None or rates[key] is None for key, value in values.items())
                else (quantize_money(sum((value * rates[key] for key, value in values.items()), ZERO)))
            )
        native_assets = {}
        for currency in rates:
            values = [values[currency] for values in native.values() if currency in values]
            native_assets[currency] = None if None in values else quantize_money(sum(values, ZERO))
        components.update(native_assets=native_assets, native_components=native)
        nav = None if gaps else quantize_money(sum((components[key] for key in native), ZERO))
        by_symbol = {}
        for (symbol, currency), value in symbol_native.items():
            converted = None if value is None or rates[currency] is None else quantize_money(value * rates[currency])
            if symbol not in by_symbol:
                by_symbol[symbol] = converted
            else:
                by_symbol[symbol] = (
                    None if converted is None or by_symbol[symbol] is None else by_symbol[symbol] + converted
                )
        return components, nav, by_symbol, gaps

    components, current_nav, current_stocks, gaps = valuation(cash, stock, options)
    scenario_cash, scenario_stock, scenario_options = dict(cash), dict(stock), dict(options)
    cash_delta, stock_delta, removed, scenario_gaps = {}, {}, {}, set()
    for identity, row in sorted(options.items()):
        if row["option_type"] != "put":
            continue
        currency = row["currency"]
        fee = assignment_fees.get(identity)
        if fee is None:
            scenario_gaps.add(f"assignment_fee:{identity}")
            continue
        fee = _nonnegative(fee, "assignment fee")
        quantity = row["contracts"] * row["multiplier"]
        delta = quantize_money(-row["strike"] * quantity - fee)
        cash_delta[currency] = cash_delta.get(currency, ZERO) + delta
        key = (row["underlying_symbol"], currency)
        stock_delta[key] = stock_delta.get(key, 0) + quantity
        scenario_cash[currency] = scenario_cash.get(currency, ZERO) + delta
        scenario_stock[key] = scenario_stock.get(key, 0) + quantity
        del scenario_options[identity]
        removed[identity] = row["contracts"]
    scenario_components, scenario_nav, scenario_stocks, valuation_gaps = valuation(
        scenario_cash, scenario_stock, scenario_options
    )
    scenario_gaps.update(valuation_gaps)
    constraints = [
        {"kind": "insufficient_assignment_cash", "currency": currency, "cash": quantize_money(amount)}
        for currency, amount in sorted(scenario_cash.items())
        if amount < 0
    ]
    if constraints:
        scenario_gaps.add("constraint:insufficient_assignment_cash")
    if scenario_gaps:
        scenario_nav = None
    gaps.update(scenario_gaps)
    nav = None if gaps else current_nav
    return {
        "point_id": point_id,
        "at_ms": at_ms,
        "base_currency": base_currency,
        "status": "incomplete" if gaps else "complete",
        "gaps": sorted(gaps),
        "nav": nav,
        "components": components,
        "current_stock_ratio": _ratios(current_stocks, nav),
        "all_put_stock_ratio": _ratios(scenario_stocks, scenario_nav),
        "scenario": {
            "status": "incomplete" if scenario_gaps else "complete",
            "gaps": sorted(scenario_gaps),
            "nav": scenario_nav,
            "components": scenario_components,
            "cash_delta_by_currency": cash_delta,
            "stock_delta": [
                {"symbol": key[0], "currency": key[1], "quantity": value} for key, value in sorted(stock_delta.items())
            ],
            "removed_option_contracts": removed,
            "constraints": constraints,
        },
    }


def _quote(quote):
    if not isinstance(quote, dict) or not {"bid", "ask"} <= quote.keys():
        raise ValueError("quote requires explicit bid and ask")
    bid, ask = _nonnegative(quote["bid"], "bid"), _nonnegative(quote["ask"], "ask")
    if ask < bid:
        raise ValueError("ask must be >= bid")
    return bid, ask


def close_execution(*, quote: dict, contracts: int, multiplier: int, fee: Decimal, slippage: Decimal) -> dict:
    _integer(contracts, "contracts", minimum=1)
    _integer(multiplier, "multiplier", minimum=1)
    _, ask = _quote(quote)
    fee = quantize_money(_nonnegative(fee, "fee"))
    price = quantize_money(ask + _nonnegative(slippage, "slippage"))
    gross = quantize_money(price * multiplier * contracts)
    return {"price": price, "gross": gross, "cash_delta": quantize_money(-gross - fee)}


def resize_execution(event: dict, targets: list[dict], quantities: int | list[int]) -> tuple[dict | None, list[dict]]:
    original = _integer(event["contracts"], "source contracts", minimum=1)
    fee = None if event["fee"] is None else _nonnegative(event["fee"], "source fee")
    _text(event["event_id"], "event_id")
    kind = event["event_type"]
    actual, rows = deepcopy(event), []
    if kind == "fill":
        if targets:
            raise ValueError("fill targets must be empty")
        quantity = _integer(quantities, "fill quantity")
        if quantity > original:
            raise ValueError("fill quantity exceeds source")
        if not quantity:
            return None, []
        if fee is None:
            raise IncompleteCashError("execution_fee_missing")
        actual["fee"] = quantize_money(fee * quantity / original)
    elif kind in {"expire_worthless", "assignment", "close"}:
        if (
            not isinstance(targets, list)
            or not targets
            or not isinstance(quantities, list)
            or len(targets) != len(quantities)
        ):
            raise ValueError("lifecycle quantities must align with source targets")
        seen, count, total_fee = set(), 0, ZERO
        fees_complete = fee is not None
        for row in targets:
            if set(row) != {"fill_id", "contracts", "option_fee", "stock_fee"}:
                raise ValueError("invalid lifecycle target schema")
            identity = _text(row["fill_id"], "fill_id")
            if identity in seen:
                raise ValueError("duplicate target fill_id")
            seen.add(identity)
            count += _integer(row["contracts"], "target contracts", minimum=1)
            option_fee = None if row["option_fee"] is None else _nonnegative(row["option_fee"], "option_fee")
            stock_fee = None if row["stock_fee"] is None else _nonnegative(row["stock_fee"], "stock_fee")
            if kind != "assignment" and stock_fee not in (None, ZERO):
                raise ValueError("close/expiry stock_fee must be zero")
            fees_complete = fees_complete and option_fee is not None and stock_fee is not None
            total_fee += sum((value for value in (option_fee, stock_fee) if value is not None), ZERO)
        if count != original or (fees_complete and total_fee != fee) or (fee is not None and total_fee > fee):
            raise ValueError("source targets must conserve contracts/fees before resizing")
        for row, quantity in zip(targets, quantities):
            _integer(quantity, "target quantity")
            if quantity > row["contracts"]:
                raise ValueError("target quantity exceeds source")
        quantity = sum(quantities)
        if kind == "assignment":
            shares = _integer(event["stock_quantity"], "stock_quantity", minimum=1)
            principal = to_decimal(event["cash_delta"], field_name="cash_delta")
            if shares % original or principal >= 0:
                raise ValueError("source assignment units/principal invalid")
            if "multiplier" in event and _integer(event["multiplier"], "multiplier", minimum=1) * original != shares:
                raise ValueError("source assignment shares do not conserve contract units")
            if "strike" in event and quantize_money(-_nonnegative(event["strike"], "strike") * shares) != principal:
                raise ValueError("source assignment principal does not conserve contract units")
            actual["stock_quantity"] = shares * quantity // original
            actual["cash_delta"] = quantize_money(principal * quantity / original)
        if not quantity:
            return None, []
        if not fees_complete:
            raise IncompleteCashError("execution_fee_missing")
        for row, actual_quantity in zip(targets, quantities):
            if actual_quantity:
                rows.append(
                    {
                        **deepcopy(row),
                        "contracts": actual_quantity,
                        **{
                            key: quantize_money(to_decimal(row[key]) * actual_quantity / row["contracts"])
                            for key in ("option_fee", "stock_fee")
                        },
                    }
                )
        actual["fee"] = sum((row["option_fee"] + row["stock_fee"] for row in rows), ZERO)
    else:
        raise ValueError("unsupported execution kind")
    actual["contracts"] = quantity
    return actual, rows


def _conversion(cash, facts):
    amount, currency, at_ms = cash.amount, cash.currency, cash.effective_at_ms
    if amount is None or currency is None:
        return None
    if amount == 0:
        rate, fact = None, None
        method, converted, source, source_id = "zero_identity", ZERO, "identity", "zero_identity"
    elif currency == "CNY":
        rate, fact = Decimal(1), None
        method, converted, source, source_id = "cny_identity", amount, "identity", "CNY"
    else:
        try:
            rate, fact = _rate(facts, currency, at_ms)
        except IncompleteCashError:
            return None
        source = str(fact.quality.get("provider_source", fact.source))
        source_id = fact.source_id
        method = (
            MARKET_CASH_FX_METHOD
            if source in {"tencent_quote", "sina_quote"}
            else (HISTORICAL_BUSINESS_DAY_FX_CARRY_FORWARD_METHOD)
        )
        converted = quantize_money(amount * rate)
    identity = cash_conversion_identity(
        cash_fact_id=cash.fact_id,
        native_amount=amount,
        native_currency=currency,
        fx_rate=rate,
        amount_cny=converted,
        rate_source_id=source_id,
        effective_at_ms=at_ms,
    )
    result = {
        "schema_version": "cash_conversion.v1",
        "status": "observed",
        **identity,
        "conversion_id": cash_conversion_id(identity),
        "quote_currency": "CNY",
        "method": method,
        "observed_at_ms": at_ms,
        "rate_source": source,
        "evidence_basis": "estimated",
        "model": MODEL,
    }
    if fact is not None:
        result.update(
            rate_timestamp=datetime.fromtimestamp(fact.effective_at_ms / 1000, timezone.utc).isoformat(),
            rate_evidence_fact_id=fact.fact_id,
            rate_observed_at_ms=fact.observed_at_ms,
            source_timestamp_verified=fact.quality.get("source_timestamp_verified"),
            fx_calendar=deepcopy(CALENDAR_EVIDENCE),
            rate_quote_quality=quote_quality(
                datetime.fromtimestamp(fact.effective_at_ms / 1000, timezone.utc),
                at=datetime.fromtimestamp(at_ms / 1000, timezone.utc),
            )[0],
        )
    _, issue = validate_observed_cash_conversion(
        result, cash_fact_id=cash.fact_id, native_amount=amount, native_currency=currency, effective_at_ms=at_ms
    )
    if issue:
        raise ValueError(f"generated conversion invalid: {issue}")
    return result


def bind_cash_conversions(events: list, fx_facts: list[dict]) -> list:
    facts = _fx_facts(fx_facts)
    result = []
    for event in events:
        raw = deepcopy(event.raw_payload)
        conversions = raw.setdefault("cash_conversions", {})
        if not isinstance(conversions, dict):
            raise ValueError("cash_conversions must be a mapping")
        for cash in cash_facts_for_trade_event(event, fee_mode="simulated"):
            if cash.fact_kind not in conversions:
                conversion = _conversion(cash, facts)
                if conversion is not None:
                    conversions[cash.fact_kind] = conversion
        result.append(replace(event, raw_payload=raw))
    return result


def _sealed_cash_fx(row, evidence):
    if evidence is None or not isinstance(evidence.get("cash_conversion"), dict):
        return None, "cash_conversion_provenance_missing"
    try:
        if (
            evidence.get("fact_id") != row["fact_id"]
            or evidence.get("fact_kind") != row["fact_kind"]
            or evidence.get("source_event_id") != row["source_event_id"]
            or type(evidence.get("effective_at_ms")) is not int
            or evidence["effective_at_ms"] != row["effective_at_ms"]
            or _currency(evidence.get("currency")) != _currency(row["currency"])
            or to_decimal(evidence.get("amount")) != to_decimal(row["amount"])
        ):
            return None, "cash_conversion_fact_mismatch"
    except (TypeError, ValueError):
        return None, "cash_conversion_fact_mismatch"
    conversion = evidence["cash_conversion"]
    amount, issue = validate_observed_cash_conversion(
        conversion,
        cash_fact_id=row["fact_id"],
        native_amount=row["amount"],
        native_currency=row["currency"],
        effective_at_ms=row["effective_at_ms"],
    )
    if issue:
        return None, f"cash_conversion_corrupt:{issue}"
    if row.get("cny_amount") is None or amount != to_decimal(row["cny_amount"]):
        return None, "cash_conversion_amount_mismatch"
    fact_id = conversion.get("rate_evidence_fact_id")
    if not isinstance(fact_id, str) or not fact_id.strip():
        return None, "cash_conversion_fx_identity_missing"
    return {
        "rate": to_decimal(conversion["fx_rate"]),
        "fact_id": fact_id,
        "conversion_id": conversion["conversion_id"],
        "envelope": deepcopy(conversion),
    }, None


def convert_option_cashflow(
    *,
    cash_facts: list,
    fx_facts: list[dict],
    base_currency: str,
    period: PeriodWindow,
    native_cashflow: dict | None = None,
    conversion_facts: list[dict] | None = None,
) -> dict:
    base_currency = _currency(base_currency)
    if not isinstance(period, PeriodWindow) or not (
        type(period.effective_start_at_ms) is int
        and type(period.effective_end_exclusive_at_ms) is int
        and 0 <= period.effective_start_at_ms < period.effective_end_exclusive_at_ms
    ):
        raise ValueError("period must be an explicit valid PeriodWindow")
    fx, gaps, links, seen = _fx_facts(fx_facts), set(), [], {}
    evidence = {}
    for fact in conversion_facts if conversion_facts is not None else ():
        identity = _text(fact["fact_id"], "conversion fact_id")
        if identity in evidence and evidence[identity] != fact:
            raise ValueError("conflicting conversion cash fact identity")
        evidence[identity] = fact
    total = ZERO
    selected = [
        row
        for row in cash_facts
        if row["fact_kind"] in {"option_trade_cash_gross", "option_fee_cash"}
        and period.contains(_integer(row["effective_at_ms"], "effective_at_ms", minimum=1))
    ]
    for row in selected:
        identity = _text(row["fact_id"], "fact_id")
        if identity in seen:
            if seen[identity] != row:
                raise ValueError("conflicting cash fact identity")
            continue
        seen[identity] = row
        at_ms = row["effective_at_ms"]
        missing = set(row.get("missing", ())) | set(row.get("cny_missing", ()))
        amount, converted, native_fx, base_fx = row.get("amount"), None, None, None
        currency = row.get("currency")
        if currency is None or amount is None:
            missing.add("native_cash_missing")
        else:
            currency, amount = _currency(currency), to_decimal(amount)
            if not missing:
                if amount == 0:
                    converted = ZERO
                    native_fx = {"method": "zero_identity", "rate": None, "fact_id": None}
                elif currency == base_currency:
                    converted = quantize_money(amount)
                    native_fx = {"method": "same_currency_identity", "rate": Decimal(1), "fact_id": None}
                else:
                    if currency == "CNY":
                        native_fx = {"method": "cny_identity", "rate": Decimal(1), "fact_id": None}
                    else:
                        native_fx, issue = _sealed_cash_fx(row, evidence.get(identity))
                        if issue:
                            missing.add(issue)
                    try:
                        cny = amount if currency == "CNY" else row.get("cny_amount")
                        if cny is None:
                            missing.add("cash_conversion_missing")
                        elif not missing:
                            base_rate, base_fact = _rate(fx, base_currency, at_ms)
                            base_fx = {"rate": base_rate, "fact_id": base_fact.fact_id if base_fact else None}
                            converted = quantize_money(to_decimal(cny) / base_rate)
                    except IncompleteCashError as exc:
                        missing.add(str(exc))
        gaps.update(f"{identity}:{reason}" for reason in missing)
        link = {
            "fact_id": identity,
            "effective_at_ms": at_ms,
            "currency": currency,
            "amount": amount,
            "base_amount": converted,
            "gaps": sorted(missing),
            "native_fx": native_fx,
            "base_fx": base_fx,
        }
        links.append(link)
        if converted is not None:
            total += converted
    native = native_cashflow
    if native is None:
        gaps.add("native_reduction_missing")
    else:
        if native.get("fact_count") != len(seen):
            gaps.add("native_reduction_window_mismatch")
        if native.get("status") not in {"observed", "estimated"}:
            gaps.add("native_reduction_incomplete")
        gaps.update(native.get("missing", ()))
    return {
        "status": "incomplete" if gaps else "estimated",
        "amount": None if gaps else quantize_money(total),
        "base_currency": base_currency,
        "by_currency": deepcopy(native["by_currency"]) if native else {},
        "gaps": sorted(gaps),
        "links": links,
        "model": MODEL,
    }


__all__ = [
    "EvaluationBook",
    "IncompleteCashError",
    "value_portfolio",
    "convert_option_cashflow",
    "bind_cash_conversions",
    "legacy_market",
    "eligible_stock_marks",
    "resize_execution",
    "close_execution",
]
