"""Offline portfolio economics on explicit frozen facts; no clocks or I/O.

summarize_evaluation/compare_evaluations (v1/v2) are frozen compatibility;
new backtest metrics are maintained in OptionBench scripts/csp_metrics.py.
Valuation, market/FX qualification and cash facts remain owned here.
"""

from __future__ import annotations

from calendar import monthrange
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from math import isfinite
from zoneinfo import ZoneInfo

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


def summarize_evaluation(
    *,
    initial: dict,
    daily: list[dict],
    expected_days: list[str],
    option_cashflow: dict,
    reconciliation: dict,
    evaluation_gaps: list[str] = (),
) -> dict:
    if not isinstance(evaluation_gaps, (list, tuple)):
        raise ValueError("evaluation_gaps must be a list or tuple")
    gaps = {_text(gap, "evaluation gap") for gap in evaluation_gaps}
    expected = [date.fromisoformat(day) for day in expected_days]
    if not expected or any(right != left + timedelta(days=1) for left, right in zip(expected, expected[1:])):
        raise ValueError("expected_days must be a nonempty consecutive natural-day sequence")
    days = [row.get("day") for row in daily]
    if days != expected_days:
        gaps.add("daily_coverage")
    points = [initial, *daily]
    times = [row["at_ms"] for row in points]
    if any(type(value) is not int or value <= 0 for value in times) or any(
        right <= left for left, right in zip(times, times[1:])
    ):
        gaps.add("valuation_time_order")
    bases = {row.get("base_currency") for row in points}
    if len(bases) != 1 or None in bases:
        gaps.add("base_currency_mismatch")
    for row in points:
        if row.get("status") != "complete" or row.get("nav") is None:
            gaps.add("valuation:" + str(row.get("point_id")))
        gaps.update(row.get("gaps", ()))
        for name in ("current_stock_ratio", "all_put_stock_ratio"):
            ratio = row.get(name, {})
            if ratio.get("overall") is None or any(value is None for value in ratio.get("by_symbol", {}).values()):
                gaps.add(f"required_metric:{name}")
    if (
        option_cashflow.get("status") not in {"complete", "estimated", "observed"}
        or option_cashflow.get("amount") is None
    ):
        gaps.add("option_cashflow_incomplete")
    if reconciliation.get("status") != "matched":
        gaps.add("cash_reconciliation")
    start = initial.get("nav")
    end = daily[-1].get("nav") if daily else None
    valid = not gaps
    net = quantize_money(end - start) if valid else None
    cumulative = net / start if valid and start > 0 else None
    if cumulative is None:
        gaps.add("return_denominator_or_coverage")
        valid = False
        net = None
    mdd = None
    if valid:
        high, mdd = start, ZERO
        for row in daily:
            high = max(high, row["nav"])
            mdd = max(mdd, 1 - row["nav"] / high)
    structures = {}
    for kind, name in (("current", "current_stock_ratio"), ("all_put", "all_put_stock_ratio")):
        ratios = [row.get(name, {}) for row in points]
        symbols = sorted({key for row in ratios for key in row.get("by_symbol", {})})
        structures[kind] = {
            "ending": ratios[-1].get("overall") if daily else None,
            "peak": max(row["overall"] for row in ratios) if valid else None,
            "by_symbol": {
                symbol: {
                    "ending": ratios[-1].get("by_symbol", {}).get(symbol, ZERO) if daily else None,
                    "peak": max(row.get("by_symbol", {}).get(symbol, ZERO) for row in ratios) if valid else None,
                }
                for symbol in symbols
            },
        }
    return {
        "status": "complete" if valid else "incomplete",
        "gaps": sorted(gaps),
        "base_currency": initial.get("base_currency"),
        "initial_nav": start,
        "ending_nav": end,
        "net_profit": net,
        "cumulative_return": cumulative,
        "max_drawdown": mdd,
        "daily_nav": [
            {
                "day": row.get("day"),
                "at_ms": row["at_ms"],
                "nav": row.get("nav"),
                "status": row.get("status"),
                "gaps": row.get("gaps", []),
            }
            for row in daily
        ],
        "stock_ratios": structures,
        "option_cashflow": deepcopy(option_cashflow),
        "reconciliation": deepcopy(reconciliation),
        "coverage": {
            "expected_days": list(expected_days),
            "actual_days": days,
            "daily_at_ms": times[1:],
            "initial_at_ms": initial["at_ms"],
            "final_at_ms": daily[-1]["at_ms"] if daily else None,
        },
    }


def compare_evaluations(*, evaluations: dict[str, dict], reference: str) -> dict:
    if not evaluations or reference not in evaluations:
        raise ValueError("reference must name an evaluation")
    gaps, ref = set(), evaluations[reference]
    metrics = {}
    for name, row in evaluations.items():
        if row.get("status") != "complete":
            gaps.add(f"evaluation_incomplete:{name}")
        if row.get("gaps") or row.get("reconciliation", {}).get("status") != "matched":
            gaps.add(f"evaluation_evidence_incomplete:{name}")
        if row.get("option_cashflow", {}).get("status") not in {"complete", "estimated", "observed"}:
            gaps.add(f"cashflow_incomplete:{name}")
        if not row.get("coverage") or row.get("coverage") != ref.get("coverage"):
            gaps.add(f"coverage_mismatch:{name}")
        if row.get("base_currency") != ref.get("base_currency") or row.get("initial_nav") != ref.get("initial_nav"):
            gaps.add(f"initial_resources_mismatch:{name}")
        metrics[name] = {key: row.get(key) for key in ("net_profit", "cumulative_return", "max_drawdown")}
        metrics[name]["option_cashflow"] = row.get("option_cashflow", {}).get("amount")
        metrics[name]["stock_ratios"] = deepcopy(row.get("stock_ratios", {}))
        if any(
            metrics[name][key] is None for key in ("net_profit", "cumulative_return", "max_drawdown", "option_cashflow")
        ):
            gaps.add(f"required_metric:{name}")
        for kind in ("current", "all_put"):
            structure = metrics[name]["stock_ratios"].get(kind, {})
            if (
                structure.get("ending") is None
                or structure.get("peak") is None
                or any(
                    item.get("ending") is None or item.get("peak") is None
                    for item in structure.get("by_symbol", {}).values()
                )
            ):
                gaps.add(f"required_structure:{name}:{kind}")
    valid = not gaps
    delta = {}
    for name, row in metrics.items():
        delta[name] = {
            key: row[key] - metrics[reference][key] if valid else None
            for key in ("net_profit", "cumulative_return", "max_drawdown", "option_cashflow")
        }
        delta[name]["stock_ratios"] = {}
        for kind in ("current", "all_put"):
            current = row["stock_ratios"].get(kind, {})
            baseline = metrics[reference]["stock_ratios"].get(kind, {})
            symbols = sorted(set(current.get("by_symbol", {})) | set(baseline.get("by_symbol", {})))
            delta[name]["stock_ratios"][kind] = {
                **{key: current[key] - baseline[key] if valid else None for key in ("ending", "peak")},
                "by_symbol": {
                    symbol: {
                        key: (
                            current.get("by_symbol", {}).get(symbol, {}).get(key, ZERO)
                            - baseline.get("by_symbol", {}).get(symbol, {}).get(key, ZERO)
                        )
                        if valid
                        else None
                        for key in ("ending", "peak")
                    }
                    for symbol in symbols
                },
            }
    return {
        "comparability": "complete" if valid else "incomplete",
        "status": "complete" if valid else "incomplete",
        "reference": reference,
        "gaps": sorted(gaps),
        "metrics": metrics,
        "delta": delta,
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


_V2_SCHEMA_VERSION = "om.strategy-evaluation.v2"
_V2_METRIC_NAMES = (
    "total_return",
    "max_drawdown",
    "drawdown_duration",
    "tail_loss",
    "option_cashflow",
    "assignment_exposure",
)
_V2_VALUE_KEYS = {
    "total_return": ("net_profit", "cumulative_return", "ending_nav"),
    "max_drawdown": ("amount",),
    "drawdown_duration": ("days",),
    "tail_loss": ("worst_daily_return", "worst_monthly_return"),
    "option_cashflow": (
        "amount",
        "positive_full_months",
        "full_months",
        "positive_month_ratio",
        "worst_full_month",
        "longest_nonpositive_months",
    ),
    "assignment_exposure": (
        "peak_same_day_loss",
        "ending_unreleased_cost_ratio",
        "peak_unreleased_cost_ratio",
    ),
}
_V2_METRIC_STATUSES = frozenset({"complete", "incomplete", "not_applicable"})
_V2_CASHFLOW_STATUSES = frozenset({"complete", "estimated", "observed"})
_V2_COMPONENT_KEYS = ("cash", "stock", "option", "receivable")
_V2_ASSIGNMENT_FIELDS = frozenset(
    {
        "batch_id",
        "event_id",
        "fill_id",
        "at_ms",
        "symbol",
        "currency",
        "strike",
        "multiplier",
        "contracts",
        "stock_fee",
    }
)
_MISSING = object()


def _v2_metric(name, *, status, gaps, values, **details):
    if status not in _V2_METRIC_STATUSES:
        raise ValueError(f"unsupported metric status: {status}")
    result = {
        "status": status,
        "gaps": sorted(gaps),
        "values": {key: values.get(key) for key in _V2_VALUE_KEYS[name]},
    }
    result.update(details)
    return result


def _v2_zone(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timezone must be a nonempty IANA timezone name")
    try:
        return ZoneInfo(value.strip())
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"timezone is not a known IANA timezone: {value}") from exc


def _v2_local_date(at_ms, zone):
    return datetime.fromtimestamp(at_ms / 1000, tz=zone).date()


def _v2_month_key(day):
    return f"{day.year:04d}-{day.month:02d}"


def _v2_month_end(day):
    return date(day.year, day.month, monthrange(day.year, day.month)[1])


def _v2_months(first, last):
    cursor = date(first.year, first.month, 1)
    stop = date(last.year, last.month, 1)
    while cursor <= stop:
        yield cursor
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)


def _v2_expected_days(expected_days):
    if not isinstance(expected_days, (list, tuple)) or not expected_days:
        raise ValueError("expected_days must be a nonempty list")
    parsed = []
    for day in expected_days:
        if not isinstance(day, str):
            raise ValueError("expected_days must be ISO date strings")
        try:
            parsed.append(date.fromisoformat(day))
        except ValueError as exc:
            raise ValueError("expected_days must be ISO date strings") from exc
    if any(right != left + timedelta(days=1) for left, right in zip(parsed, parsed[1:])):
        raise ValueError("expected_days must be a nonempty consecutive natural-day sequence")
    return parsed


def _v2_actual_nav(point, label):
    components = point.get("components")
    if not isinstance(components, dict):
        return None, {f"actual_components_missing:{label}"}
    values, gaps = [], set()
    for key in _V2_COMPONENT_KEYS:
        value = components.get(key)
        if value is None:
            gaps.add(f"actual_component:{key}:{label}")
        else:
            values.append(to_decimal(value, field_name=f"{label}.{key}"))
    if gaps:
        return None, gaps
    return quantize_money(sum(values, ZERO)), gaps


def _v2_cashflow_links(option_cashflow):
    if not isinstance(option_cashflow, dict):
        raise ValueError("option_cashflow must be an object")
    raw_links = option_cashflow.get("links")
    if not isinstance(raw_links, list):
        raise ValueError("option_cashflow links must be a list")
    links, seen = [], set()
    for row in raw_links:
        if not isinstance(row, dict):
            raise ValueError("option_cashflow link must be an object")
        fact_id = _text(row.get("fact_id"), "cashflow fact_id")
        if fact_id in seen:
            raise ValueError(f"duplicate cashflow fact_id: {fact_id}")
        seen.add(fact_id)
        at_ms = _integer(row.get("effective_at_ms"), "effective_at_ms", minimum=1)
        raw_gaps = row.get("gaps", ())
        if not isinstance(raw_gaps, (list, tuple)):
            raise ValueError("cashflow link gaps must be a list")
        base_amount = row.get("base_amount")
        converted = None if base_amount is None else to_decimal(base_amount, field_name=f"base_amount:{fact_id}")
        links.append(
            {
                "fact_id": fact_id,
                "effective_at_ms": at_ms,
                "base_amount": converted,
                "gaps": [_text(gap, "cashflow link gap") for gap in raw_gaps],
            }
        )
    return links


def _v2_cash_evidence(option_cashflow, reconciliation, base_currency, links):
    if not isinstance(reconciliation, dict):
        raise ValueError("reconciliation must be an object")
    common = set()
    if option_cashflow.get("status") not in _V2_CASHFLOW_STATUSES:
        common.add("option_cashflow_incomplete")
    raw_gaps = option_cashflow.get("gaps", ())
    if not isinstance(raw_gaps, (list, tuple)):
        raise ValueError("option_cashflow gaps must be a list")
    common.update(f"cashflow:{_text(gap, 'option cashflow gap')}" for gap in raw_gaps)
    if option_cashflow.get("base_currency") != base_currency:
        common.add("option_cashflow_base_currency_mismatch")
    amount_value = None
    if option_cashflow.get("amount") is None:
        common.add("option_cashflow_amount_missing")
    else:
        amount_value = quantize_money(to_decimal(option_cashflow["amount"], field_name="option_cashflow amount"))
    total = ZERO
    amounts_complete = True
    for link in links:
        common.update(f"cashflow_link:{link['fact_id']}:{gap}" for gap in link["gaps"])
        if link["base_amount"] is None:
            amounts_complete = False
            common.add(f"cashflow_link_amount_missing:{link['fact_id']}")
        else:
            total += link["base_amount"]
        if link["gaps"]:
            amounts_complete = False
    if amount_value is not None and amounts_complete and not raw_gaps and amount_value != quantize_money(total):
        common.add("option_cashflow_amount_mismatch")
    if reconciliation.get("status") != "matched":
        common.add("cash_reconciliation")
    return set(common), common, amount_value


def _v2_monthly_nav_returns(*, nav_by_day, initial_day, initial_nav, last_day):
    entries, gaps, full = [], set(), 0
    for start in _v2_months(initial_day, last_day):
        end = _v2_month_end(start)
        if initial_day >= start or last_day < end:
            continue
        full += 1
        key = _v2_month_key(start)
        end_nav = nav_by_day.get(end)
        if end_nav is None:
            gaps.add(f"month_end_nav_missing:{key}")
            continue
        baseline_day = start - timedelta(days=1)
        if baseline_day == initial_day:
            baseline = initial_nav
        elif baseline_day < initial_day:
            gaps.add(f"prior_month_end_unavailable:{key}")
            continue
        else:
            baseline = nav_by_day.get(baseline_day)
            if baseline is None:
                gaps.add(f"prior_month_end_missing:{key}")
                continue
        if baseline <= 0:
            gaps.add(f"monthly_return_denominator:{key}")
            continue
        entries.append((key, end_nav / baseline - 1))
    status = "not_applicable" if full == 0 else ("incomplete" if gaps else "complete")
    return entries, status, gaps


def _v2_option_cashflow_metric(
    *, zone, initial_day, last_day, initial_at_ms, final_at_ms, links, amount_value, common_gaps
):
    buckets, outside_gaps, gaps = {}, {}, set(common_gaps)
    for link in links:
        local = _v2_local_date(link["effective_at_ms"], zone)
        month = (local.year, local.month)
        if not initial_at_ms < link["effective_at_ms"] <= final_at_ms:
            gap = f"cashflow_link_outside_coverage:{link['fact_id']}"
            gaps.add(gap)
            outside_gaps.setdefault(month, set()).add(gap)
            continue
        buckets.setdefault(month, []).append(link)
    starts = {date(day.year, day.month, 1) for day in _v2_months(initial_day, last_day)}
    starts.update(date(year, month, 1) for year, month in buckets.keys() | outside_gaps.keys())
    months = []
    for start in sorted(starts):
        end = _v2_month_end(start)
        key = _v2_month_key(start)
        bucket = buckets.get((start.year, start.month), ())
        month_gaps = {f"cashflow_link:{link['fact_id']}:{gap}" for link in bucket for gap in link["gaps"]}
        month_gaps.update(common_gaps)
        month_gaps.update(outside_gaps.get((start.year, start.month), ()))
        missing = bool(month_gaps) or any(link["base_amount"] is None for link in bucket)
        if missing:
            month_gaps.add("amount_missing")
        amount = None if missing else quantize_money(sum((link["base_amount"] for link in bucket), ZERO))
        full = initial_day < start and last_day >= end
        if full:
            status = "incomplete" if missing else "complete"
        else:
            intersects = not (end < initial_day or start > last_day)
            status = "partial" if intersects else "outside"
        months.append(
            {
                "month": key,
                "full": full,
                "amount": amount,
                "status": status,
                "gaps": sorted(month_gaps),
            }
        )
    full_months = [month for month in months if month["full"]]
    outside = any(month["status"] == "outside" for month in months)
    if not full_months:
        monthly_status = "not_applicable"
    elif outside or any(month["status"] != "complete" for month in full_months):
        monthly_status = "incomplete"
    else:
        monthly_status = "complete"
    values = {}
    if monthly_status == "not_applicable":
        values = {
            "amount": amount_value,
            "positive_full_months": 0,
            "full_months": 0,
            "positive_month_ratio": None,
            "worst_full_month": None,
            "longest_nonpositive_months": 0,
        }
    elif monthly_status == "complete":
        count = len(full_months)
        positive = sum(1 for month in full_months if month["amount"] > 0)
        longest = run = 0
        for month in full_months:
            if month["amount"] <= 0:
                run += 1
                longest = max(longest, run)
            else:
                run = 0
        values = {
            "amount": amount_value,
            "positive_full_months": positive,
            "full_months": count,
            "positive_month_ratio": Decimal(positive) / Decimal(count),
            "worst_full_month": min(month["amount"] for month in full_months),
            "longest_nonpositive_months": longest,
        }
    status = "complete" if not gaps and monthly_status in {"complete", "not_applicable"} else "incomplete"
    return _v2_metric(
        "option_cashflow",
        status=status,
        gaps=gaps,
        values=values if status == "complete" else {},
        months=months,
        monthly_status=monthly_status,
    )


def _v2_assignment_batches(assignments):
    if not isinstance(assignments, (list, tuple)):
        raise ValueError("assignments must be a list")
    batches, batch_ids = [], set()
    for row in assignments:
        if not isinstance(row, dict) or set(row) != _V2_ASSIGNMENT_FIELDS:
            raise ValueError(
                "assignment must contain exactly batch_id/event_id/fill_id/at_ms/symbol/currency/"
                "strike/multiplier/contracts/stock_fee"
            )
        batch_id = _text(row["batch_id"], "batch_id")
        event_id = _text(row["event_id"], "event_id")
        fill_id = _text(row["fill_id"], "fill_id")
        if batch_id in batch_ids:
            raise ValueError(f"duplicate assignment batch_id: {batch_id}")
        batch_ids.add(batch_id)
        at_ms = _integer(row["at_ms"], "at_ms", minimum=1)
        symbol = _text(row["symbol"], "symbol")
        currency = _currency(row["currency"])
        strike = _nonnegative(row["strike"], "strike")
        multiplier = _integer(row["multiplier"], "multiplier", minimum=1)
        contracts = _integer(row["contracts"], "contracts", minimum=1)
        stock_fee = None if row["stock_fee"] is None else _nonnegative(row["stock_fee"], "stock_fee")
        shares = contracts * multiplier
        cost = None if stock_fee is None else quantize_money(strike * shares + stock_fee)
        batches.append(
            {
                "batch_id": batch_id,
                "event_id": event_id,
                "fill_id": fill_id,
                "at_ms": at_ms,
                "symbol": symbol,
                "currency": currency,
                "strike": strike,
                "multiplier": multiplier,
                "contracts": contracts,
                "stock_fee": stock_fee,
                "shares": shares,
                "cost": cost,
            }
        )
    return batches


def _v2_adjustment_status(*, batches, final_at_ms, assignment_adjustments):
    if not batches:
        return "not_required", set()
    if assignment_adjustments is None:
        return "unknown", {"assignment_adjustments_unknown"}
    if not isinstance(assignment_adjustments, dict) or set(assignment_adjustments) != {
        "start_at_ms",
        "end_at_ms",
        "events",
    }:
        raise ValueError("assignment_adjustments must contain exactly start_at_ms/end_at_ms/events")
    start = _integer(assignment_adjustments["start_at_ms"], "assignment_adjustments start_at_ms")
    end = _integer(assignment_adjustments["end_at_ms"], "assignment_adjustments end_at_ms")
    if start > end:
        raise ValueError("assignment_adjustments start_at_ms must not exceed end_at_ms")
    events = assignment_adjustments["events"]
    if not isinstance(events, (list, tuple)):
        raise ValueError("assignment_adjustments events must be a list")
    if events:
        return "unsupported", {"assignment_adjustments_unsupported"}
    gaps = set()
    if start > min(batch["at_ms"] for batch in batches) or end < final_at_ms:
        gaps.add("assignment_adjustments_coverage")
    return ("complete" if not gaps else "coverage_insufficient"), gaps


def _v2_assignment_metric(
    *,
    batches,
    initial_nav,
    markets,
    points,
    nav_valid,
    nav_gaps,
    base_currency,
    zone,
    last_day,
    assignment_adjustments,
):
    if not batches:
        return _v2_metric(
            "assignment_exposure",
            status="not_applicable",
            gaps=(),
            values={
                "peak_same_day_loss": ZERO,
                "ending_unreleased_cost_ratio": ZERO,
                "peak_unreleased_cost_ratio": ZERO,
            },
            batches=[],
            adjustments_status="not_required",
        )
    if not isinstance(markets, dict):
        raise ValueError("markets must be a mapping keyed by point_id")
    final_at_ms = points[-1]["at_ms"]
    adjustments_status, gaps = _v2_adjustment_status(
        batches=batches,
        final_at_ms=final_at_ms,
        assignment_adjustments=assignment_adjustments,
    )
    min_at = min(batch["at_ms"] for batch in batches)
    parsed = {}
    for point in points:
        if point["at_ms"] < min_at:
            continue
        market = markets.get(point["point_id"])
        if market is None:
            parsed[point["point_id"]] = None
            gaps.add(f"assignment_market_missing:{point['point_id']}")
        else:
            parsed[point["point_id"]] = _market(market, point["at_ms"])

    rate_cache = {}

    def rate_at(point, currency):
        key = (point["point_id"], currency)
        if key not in rate_cache:
            market = parsed.get(point["point_id"])
            if market is None:
                rate_cache[key] = None
            else:
                try:
                    rate_cache[key] = _cross_rate(market[1], currency, base_currency, point["at_ms"])
                except IncompleteCashError:
                    rate_cache[key] = None
        if rate_cache[key] is None:
            gaps.add(f"assignment_fx:{currency}:{point['label']}")
        return rate_cache[key]

    base_loss_by_point, cost_by_point = {}, {}
    batches_out = []
    for batch in batches:
        identity = batch["batch_id"]
        held = [point for point in points if point["at_ms"] >= batch["at_ms"]]
        native_complete = bool(held)
        if not held:
            gaps.add(f"assignment_after_coverage:{identity}")
        native_losses = []
        for point in held:
            market = parsed.get(point["point_id"])
            stock = market[0]["stocks"].get((batch["symbol"], batch["currency"])) if market is not None else None
            native = None
            if stock is None:
                if market is not None:
                    gaps.add(f"assignment_stock_mark:{identity}:{point['label']}")
            elif batch["cost"] is None:
                gaps.add(f"assignment_stock_fee_missing:{identity}")
            else:
                native = quantize_money(max(batch["cost"] - batch["shares"] * stock["price"], ZERO))
                native_losses.append(native)
            if native is None:
                native_complete = False
            rate = rate_at(point, batch["currency"]) if native is not None else None
            base_loss_by_point[(identity, point["point_id"])] = (
                None if native is None or rate is None else quantize_money(native * rate)
            )
            cost_rate = rate if native is not None else rate_at(point, batch["currency"])
            cost_by_point[(identity, point["point_id"])] = (
                None if batch["cost"] is None or cost_rate is None else quantize_money(batch["cost"] * cost_rate)
            )
        native_max = (
            max(native_losses)
            if adjustments_status == "complete" and native_complete and len(native_losses) == len(held)
            else None
        )
        cost = batch["cost"]
        ratio = None if native_max is None or cost is None or cost <= 0 else native_max / cost
        assign_day = _v2_local_date(batch["at_ms"], zone)
        held_days = (last_day - assign_day).days if assign_day <= last_day else None
        batches_out.append(
            {
                "batch_id": identity,
                "event_id": batch["event_id"],
                "fill_id": batch["fill_id"],
                "at_ms": batch["at_ms"],
                "symbol": batch["symbol"],
                "currency": batch["currency"],
                "strike": batch["strike"],
                "multiplier": batch["multiplier"],
                "contracts": batch["contracts"],
                "shares": batch["shares"],
                "stock_fee": batch["stock_fee"],
                "cost": cost,
                "native_max_unrealized_loss": native_max,
                "native_max_unrealized_loss_ratio": ratio,
                "held_natural_days": held_days,
                "unended": True,
            }
        )

    peak_same_day = ZERO
    loss_complete = bool(points) and bool([point for point in points if point["at_ms"] >= min_at])
    ratio_values = []
    ratios_complete = loss_complete
    for point in points:
        if point["at_ms"] < min_at:
            continue
        total_loss = total_cost = ZERO
        point_loss_known = point_cost_known = True
        for batch in batches:
            if batch["at_ms"] > point["at_ms"]:
                continue
            loss = base_loss_by_point.get((batch["batch_id"], point["point_id"]), _MISSING)
            if loss is _MISSING or loss is None:
                point_loss_known = False
            else:
                total_loss += loss
            cost = cost_by_point.get((batch["batch_id"], point["point_id"]), _MISSING)
            if cost is _MISSING or cost is None:
                point_cost_known = False
            else:
                total_cost += cost
        if point_loss_known:
            peak_same_day = max(peak_same_day, total_loss)
        else:
            loss_complete = False
            gaps.add(f"assignment_same_day_loss_incomplete:{point['label']}")
        if not point_cost_known:
            ratios_complete = False
            gaps.add(f"assignment_exposure_incomplete:{point['label']}")
        elif nav_valid:
            nav = initial_nav
            if nav is None:
                ratios_complete = False
            elif nav <= 0:
                ratios_complete = False
                gaps.add(f"assignment_nav_nonpositive:{point['label']}")
            else:
                ratio_values.append(total_cost / nav)
        else:
            ratios_complete = False
    if nav_valid and ratios_complete and ratio_values:
        peak_ratio = max(ratio_values)
        ending_ratio = ratio_values[-1]
    else:
        peak_ratio = ending_ratio = None
    metric_gaps = set(gaps) | set(nav_gaps)
    complete = nav_valid and loss_complete and ratios_complete and not metric_gaps
    values = {}
    if complete:
        values = {
            "peak_same_day_loss": peak_same_day,
            "ending_unreleased_cost_ratio": ending_ratio,
            "peak_unreleased_cost_ratio": peak_ratio,
        }
    return _v2_metric(
        "assignment_exposure",
        status="complete" if complete else "incomplete",
        gaps=metric_gaps,
        values=values,
        batches=batches_out,
        adjustments_status=adjustments_status,
    )


def summarize_evaluation_v2(
    *,
    initial: dict,
    daily: list[dict],
    expected_days: list[str],
    option_cashflow: dict,
    reconciliation: dict,
    evaluation_gaps: list[str] = (),
    timezone: str,
    assignments: list[dict],
    markets: dict,
    assignment_adjustments: dict | None,
) -> dict:
    zone = _v2_zone(timezone)
    zone_name = timezone.strip()
    expected = _v2_expected_days(expected_days)
    if not isinstance(initial, dict):
        raise ValueError("initial must be a valuation point object")
    if not isinstance(daily, (list, tuple)):
        raise ValueError("daily must be a list of valuation points")
    if not isinstance(evaluation_gaps, (list, tuple)):
        raise ValueError("evaluation_gaps must be a list or tuple")
    gaps = {_text(gap, "evaluation gap") for gap in evaluation_gaps}
    rows = []
    for row in daily:
        if not isinstance(row, dict):
            raise ValueError("daily must contain valuation point objects")
        rows.append(row)
    days, seen_days = [], set()
    for row in rows:
        day = row.get("day")
        if not isinstance(day, str) or not day.strip():
            raise ValueError("daily point day must be a nonempty ISO date string")
        day = day.strip()
        if day in seen_days:
            raise ValueError(f"duplicate daily day: {day}")
        seen_days.add(day)
        days.append(day)
    raw_points = [initial, *rows]
    point_ids = [_text(point.get("point_id"), "point_id") for point in raw_points]
    if len(set(point_ids)) != len(point_ids):
        raise ValueError("duplicate valuation point_id")
    times = [_integer(point.get("at_ms"), "at_ms", minimum=1) for point in raw_points]
    if any(right <= left for left, right in zip(times, times[1:])):
        gaps.add("valuation_time_order")
    base_currency = _currency(initial.get("base_currency"))
    bases = {base_currency} | {_currency(row.get("base_currency")) for row in rows}
    if len(bases) != 1:
        gaps.add("base_currency_mismatch")
    expected_labels = [day.isoformat() for day in expected]
    if days != expected_labels:
        gaps.add("daily_coverage")
    initial_day = expected[0] - timedelta(days=1)
    last_day = expected[-1]

    cash_path_gaps = {gap for gap in gaps if gap == "cash_path_stopped" or gap.startswith("cash_path_stopped:")}
    navs = []
    for point, label in zip(raw_points, [initial_day.isoformat(), *days]):
        nav, point_gaps = _v2_actual_nav(point, label)
        navs.append(nav)
        gaps.update(point_gaps)
        raw_gaps = point.get("gaps", ())
        if not isinstance(raw_gaps, (list, tuple)):
            raise ValueError("valuation point gaps must be a list")
        scenario = point.get("scenario")
        scenario_gaps = set()
        if isinstance(scenario, dict):
            raw_scenario_gaps = scenario.get("gaps", ())
            if not isinstance(raw_scenario_gaps, (list, tuple)):
                raise ValueError("valuation scenario gaps must be a list")
            scenario_gaps = {_text(gap, "valuation gap") for gap in raw_scenario_gaps}
        actual_gaps = {_text(gap, "valuation gap") for gap in raw_gaps} - scenario_gaps
        cash_path_gaps.update(
            gap for gap in actual_gaps if gap == "cash_path_stopped" or gap.startswith("cash_path_stopped:")
        )
        gaps.update(f"valuation:{label}:{gap}" for gap in actual_gaps)
        if point.get("status") != "complete" and not raw_gaps and not point_gaps:
            gaps.add(f"valuation_status_unverified:{label}")
        if _v2_local_date(point["at_ms"], zone).isoformat() != label:
            gaps.add(f"valuation_date_mismatch:{label}")
    initial_nav = navs[0]

    links = _v2_cashflow_links(option_cashflow)
    cash_gaps, metric_cash_gaps, amount_value = _v2_cash_evidence(option_cashflow, reconciliation, base_currency, links)
    nav_gaps = set(gaps) | cash_gaps
    nav_valid = not nav_gaps
    nav_by_day = (
        {day: nav for day, nav in zip(expected, navs[1:]) if nav is not None} if days == expected_labels else {}
    )

    if not nav_valid:
        total_return = _v2_metric("total_return", status="incomplete", gaps=nav_gaps, values={})
    else:
        start, end = navs[0], navs[-1]
        if start <= 0:
            total_return = _v2_metric(
                "total_return",
                status="incomplete",
                gaps=nav_gaps | {"return_denominator_or_coverage"},
                values={},
            )
        else:
            net = quantize_money(end - start)
            total_return = _v2_metric(
                "total_return",
                status="complete",
                gaps=(),
                values={"net_profit": net, "cumulative_return": net / start, "ending_nav": end},
            )

    if not nav_valid:
        max_drawdown = _v2_metric("max_drawdown", status="incomplete", gaps=nav_gaps, values={})
    else:
        high, high_label = navs[0], initial_day.isoformat()
        amount, peak_day, trough_day = ZERO, None, None
        drawdown_gaps = set()
        for label, nav in zip([*days], navs[1:]):
            if high <= 0:
                drawdown_gaps.add("drawdown_nonpositive_peak")
                break
            if nav >= high:
                high, high_label = nav, label
                continue
            value = 1 - nav / high
            if value > amount:
                amount, peak_day, trough_day = value, high_label, label
        if high <= 0:
            drawdown_gaps.add("drawdown_nonpositive_peak")
        if drawdown_gaps:
            max_drawdown = _v2_metric("max_drawdown", status="incomplete", gaps=nav_gaps | drawdown_gaps, values={})
        else:
            max_drawdown = _v2_metric(
                "max_drawdown",
                status="complete",
                gaps=(),
                values={"amount": amount},
                peak_day=peak_day,
                trough_day=trough_day,
            )

    if not nav_valid:
        drawdown_duration = _v2_metric("drawdown_duration", status="incomplete", gaps=nav_gaps, values={})
    else:
        point_days = [initial_day, *expected]
        high, high_day = navs[0], point_days[0]
        episodes, in_episode, episode_peak = [], False, None
        for day, nav in zip(point_days[1:], navs[1:]):
            if nav >= high:
                if in_episode:
                    episodes.append(((day - episode_peak).days, episode_peak, day, False))
                    in_episode = False
                high, high_day = nav, day
            elif not in_episode:
                in_episode, episode_peak = True, high_day
        if in_episode:
            episodes.append(((last_day - episode_peak).days, episode_peak, None, True))
        if episodes:
            days_count, peak, recovery, unrecovered = max(episodes, key=lambda row: row[0])
        else:
            days_count, peak, recovery, unrecovered = 0, None, None, False
        drawdown_duration = _v2_metric(
            "drawdown_duration",
            status="complete",
            gaps=(),
            values={"days": days_count},
            peak_day=peak.isoformat() if peak else None,
            recovery_day=recovery.isoformat() if recovery else None,
            unrecovered=unrecovered,
        )

    if not nav_valid:
        tail_loss = _v2_metric(
            "tail_loss",
            status="incomplete",
            gaps=nav_gaps,
            values={},
            daily_day=None,
            monthly_month=None,
            monthly_status="incomplete",
        )
    else:
        daily_returns, daily_gaps = [], set()
        for index in range(1, len(navs)):
            previous = navs[index - 1]
            if previous <= 0:
                daily_gaps.add(f"daily_return_denominator:{days[index - 1]}")
                continue
            daily_returns.append((days[index - 1], navs[index] / previous - 1))
        monthly_entries, monthly_status, monthly_gaps = _v2_monthly_nav_returns(
            nav_by_day=nav_by_day,
            initial_day=initial_day,
            initial_nav=navs[0],
            last_day=last_day,
        )
        tail_gaps = set(nav_gaps) | daily_gaps | monthly_gaps
        if daily_returns:
            daily_day, worst_daily = min(daily_returns, key=lambda row: row[1])
        else:
            daily_day, worst_daily = None, None
        if monthly_entries:
            monthly_month, worst_monthly = min(monthly_entries, key=lambda row: row[1])
        else:
            monthly_month, worst_monthly = None, None
        tail_values = {}
        if not tail_gaps:
            tail_values = {"worst_daily_return": worst_daily, "worst_monthly_return": worst_monthly}
        tail_loss = _v2_metric(
            "tail_loss",
            status="complete" if not tail_gaps else "incomplete",
            gaps=tail_gaps,
            values=tail_values,
            daily_day=daily_day,
            monthly_month=monthly_month,
            monthly_status=monthly_status,
        )

    option_cashflow_metric = _v2_option_cashflow_metric(
        zone=zone,
        initial_day=initial_day,
        last_day=last_day,
        initial_at_ms=times[0],
        final_at_ms=times[-1],
        links=links,
        amount_value=amount_value,
        common_gaps=metric_cash_gaps | cash_path_gaps,
    )

    assignment_metric = _v2_assignment_metric(
        batches=_v2_assignment_batches(assignments),
        initial_nav=initial_nav,
        markets=markets,
        points=[
            {"point_id": point_ids[index], "label": label, "at_ms": times[index], "nav": navs[index]}
            for index, label in enumerate([initial_day.isoformat(), *days])
        ],
        nav_valid=nav_valid,
        nav_gaps=nav_gaps,
        base_currency=base_currency,
        zone=zone,
        last_day=last_day,
        assignment_adjustments=assignment_adjustments,
    )

    six_metrics = {
        "total_return": total_return,
        "max_drawdown": max_drawdown,
        "drawdown_duration": drawdown_duration,
        "tail_loss": tail_loss,
        "option_cashflow": option_cashflow_metric,
        "assignment_exposure": assignment_metric,
    }
    all_gaps = set(nav_gaps)
    for metric in six_metrics.values():
        all_gaps.update(metric["gaps"])
    return {
        "schema_version": _V2_SCHEMA_VERSION,
        "status": (
            "complete"
            if all(metric["status"] in {"complete", "not_applicable"} for metric in six_metrics.values())
            else "incomplete"
        ),
        "gaps": sorted(all_gaps),
        "base_currency": base_currency,
        "initial_nav": initial_nav,
        "coverage": {
            "expected_days": list(expected_days),
            "actual_days": days,
            "daily_at_ms": times[1:],
            "initial_at_ms": times[0],
            "final_at_ms": times[-1],
            "initial_day": initial_day.isoformat(),
            "last_day": last_day.isoformat(),
            "timezone": zone_name,
        },
        "six_metrics": six_metrics,
    }


def _v2_number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return Decimal(str(value)) if isfinite(value) else None
    return None


def compare_evaluations_v2(*, evaluations: dict[str, dict], reference: str) -> dict:
    if not isinstance(evaluations, dict) or not evaluations:
        raise ValueError("evaluations must be a nonempty mapping")
    if reference not in evaluations:
        raise ValueError("reference must name an evaluation")
    for name, row in evaluations.items():
        if not isinstance(row, dict):
            raise ValueError(f"evaluation {name} must be an object")
        if row.get("schema_version") != _V2_SCHEMA_VERSION:
            raise ValueError(f"evaluation {name} must use {_V2_SCHEMA_VERSION}")
        six = row.get("six_metrics")
        if not isinstance(six, dict):
            raise ValueError(f"evaluation {name} must contain six_metrics")
        for metric in _V2_METRIC_NAMES:
            if not isinstance(six.get(metric), dict):
                raise ValueError(f"evaluation {name} is missing metric {metric}")
    ref = evaluations[reference]
    gaps = set()
    ref_coverage = ref.get("coverage")
    ref_base = ref.get("base_currency")
    ref_initial = ref.get("initial_nav")
    for name, row in evaluations.items():
        if row.get("coverage") is None:
            gaps.add(f"coverage_unavailable:{name}")
        if row.get("coverage") != ref_coverage:
            gaps.add(f"coverage_mismatch:{name}")
        if row.get("base_currency") is None:
            gaps.add(f"base_currency_unavailable:{name}")
        if row.get("base_currency") != ref_base:
            gaps.add(f"base_currency_mismatch:{name}")
        if row.get("initial_nav") is None:
            gaps.add(f"initial_nav_unavailable:{name}")
        if row.get("initial_nav") != ref_initial:
            gaps.add(f"initial_nav_mismatch:{name}")
    comparable = not gaps
    metrics, delta, metric_gaps = {}, {}, set()
    for name, row in evaluations.items():
        metrics[name], delta[name] = {}, {}
        for metric in _V2_METRIC_NAMES:
            item = row["six_metrics"][metric]
            ref_item = ref["six_metrics"][metric]
            status, ref_status = item.get("status"), ref_item.get("status")
            values = item.get("values")
            if not isinstance(values, dict):
                values = {}
                metric_gaps.add(f"metric_values_missing:{name}:{metric}")
            metrics[name][metric] = {"status": status, "values": deepcopy(values)}
            same = status in {"complete", "not_applicable"} and ref_status in {"complete", "not_applicable"}
            if status not in _V2_METRIC_STATUSES or ref_status not in _V2_METRIC_STATUSES:
                metric_gaps.add(f"metric_status_invalid:{name}:{metric}")
                same = False
            if not same:
                metric_gaps.add(f"metric_incomparable:{name}:{metric}")
            ref_values = ref_item.get("values")
            if not isinstance(ref_values, dict):
                ref_values = {}
            fields = {}
            for key in _V2_VALUE_KEYS[metric]:
                fields[key] = None
                if comparable and same:
                    left, right = _v2_number(values.get(key)), _v2_number(ref_values.get(key))
                    if left is not None and right is not None:
                        fields[key] = left - right
            delta[name][metric] = fields
    all_gaps = gaps | metric_gaps
    return {
        "status": "complete" if comparable and not metric_gaps else "incomplete",
        "comparability": "complete" if comparable and not metric_gaps else "incomplete",
        "reference": reference,
        "gaps": sorted(all_gaps),
        "metrics": metrics,
        "delta": delta,
    }


__all__ = [
    "EvaluationBook",
    "IncompleteCashError",
    "value_portfolio",
    "summarize_evaluation",
    "summarize_evaluation_v2",
    "compare_evaluations",
    "compare_evaluations_v2",
    "convert_option_cashflow",
    "bind_cash_conversions",
    "legacy_market",
    "eligible_stock_marks",
    "resize_execution",
    "close_execution",
]
