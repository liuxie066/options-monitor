"""Pure projection for the all-open-short-options-assigned stress scenario."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable, Mapping, Sequence

from domain.domain.fee_calc import (
    FUTU_HK_FEE_SCHEDULE_URL,
    FUTU_US_FEE_SCHEDULE_URL,
    calc_futu_hk_terminal_fee,
    calc_futu_stock_fee,
)
from domain.domain.ledger.position_fields import normalize_broker
from domain.domain.option_position_identity import normalize_currency
from domain.domain.symbol_identity import canonical_symbol


SCHEMA_VERSION = "portfolio.assignment_scenario.v1"
PORTFOLIO_EVIDENCE_VERSION = "portfolio.valuation_evidence.v1"
_MONEY = Decimal("0.01")
_NATIVE_MONEY = Decimal("0.000001")
_RATE = Decimal("0.000001")
_ZERO = Decimal("0")


def _decimal(value: Any) -> Decimal | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _positive(value: Any) -> Decimal | None:
    result = _decimal(value)
    return result if result is not None and result > 0 else None


def _integer(value: Any) -> int | None:
    number = _positive(value)
    if number is None or number != number.to_integral_value():
        return None
    return int(number)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _money(value: Decimal | int | float | None) -> str | None:
    resolved = _decimal(value)
    if resolved is None:
        return None
    return format(resolved.quantize(_MONEY, rounding=ROUND_HALF_UP), "f")


def _native_money(value: Decimal | int | float | None) -> str | None:
    resolved = _decimal(value)
    if resolved is None:
        return None
    return format(resolved.quantize(_NATIVE_MONEY, rounding=ROUND_HALF_UP), "f")


def _rate(value: Decimal | int | float | None) -> str | None:
    resolved = _decimal(value)
    if resolved is None:
        return None
    return format(resolved.quantize(_RATE, rounding=ROUND_HALF_UP), "f")


def _quantity(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = value.normalize()
    return format(normalized, "f")


def _dedupe_warnings(items: Iterable[Any]) -> list[str]:
    return list(dict.fromkeys(_text(item) for item in items if _text(item)))


def _asset_category(row: Mapping[str, Any]) -> str:
    raw = _text(row.get("asset_type") or row.get("type")).lower()
    normalized = _text(row.get("normalized_type")).lower()
    if raw in {"cash", "mmf"} or normalized == "cash":
        return "cash"
    if raw == "crypto":
        return "crypto"
    if raw == "bond":
        return "bond"
    if raw in {
        "fund",
        "exchange_fund",
        "otc_fund",
        "cn_fund",
        "us_fund",
        "hk_fund",
        "etf",
    } or normalized == "fund":
        return "fund"
    if raw in {"a_stock", "hk_stock", "us_stock", "stock"} or normalized == "stock":
        return "stock"
    return "other"


def _quote_map(rows: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        code = canonical_symbol(row.get("code") or row.get("symbol"))
        if code and code not in result:
            result[code] = row
    return result


def _quote_values(
    quote: Mapping[str, Any] | None,
    *,
    expected_currency: str | None = None,
) -> tuple[Decimal | None, Decimal | None, Decimal | None, str | None]:
    if not isinstance(quote, Mapping):
        return None, None, None, "quote_missing"
    currency = normalize_currency(quote.get("currency"))
    if expected_currency and currency != normalize_currency(expected_currency):
        return None, None, None, "quote_currency_mismatch"
    native_price = _positive(quote.get("price_native") if "price_native" in quote else quote.get("price"))
    cny_price = _positive(quote.get("price_cny") if "price_cny" in quote else quote.get("cny_price"))
    if currency == "CNY":
        exchange_rate = Decimal("1")
    else:
        exchange_rate = _positive(
            quote.get("exchange_rate_to_cny")
            if "exchange_rate_to_cny" in quote
            else quote.get("exchange_rate")
        )
    if native_price is None:
        return None, None, exchange_rate, "native_price_missing"
    if cny_price is None:
        return native_price, None, exchange_rate, "cny_price_missing"
    if exchange_rate is None:
        return native_price, cny_price, None, "fx_evidence_missing"
    return native_price, cny_price, exchange_rate, None


def _fee_fact(
    option: Mapping[str, Any],
    *,
    shares: int,
    strike: Decimal,
    currency: str,
    exchange_rate: Decimal,
    option_type: str,
) -> tuple[dict[str, Any], Decimal | None, bool]:
    broker = normalize_broker(option.get("broker"))
    source = FUTU_HK_FEE_SCHEDULE_URL if currency == "HKD" else FUTU_US_FEE_SCHEDULE_URL
    base = {
        "basis": "assignment_at_strike",
        "calculator": (
            "domain.domain.fee_calc.calc_futu_hk_terminal_fee"
            if currency == "HKD"
            else "domain.domain.fee_calc.calc_futu_stock_fee"
        ),
        "currency": currency,
        "is_sell": option_type == "call",
        "shares": shares,
        "strike": _native_money(strike),
        "source": source if currency in {"USD", "HKD"} else None,
    }
    if broker != "富途":
        return (
            {
                **base,
                "status": "missing",
                "estimated_stock_fee_native": None,
                "fee_native": None,
                "fee_cny": None,
                "reason": "unsupported_broker_fee_schedule",
            },
            None,
            False,
        )
    if currency not in {"USD", "HKD"}:
        return (
            {
                **base,
                "status": "missing",
                "estimated_stock_fee_native": None,
                "fee_native": None,
                "fee_cny": None,
                "reason": "unsupported_stock_fee_currency",
            },
            None,
            False,
        )
    if currency == "USD":
        try:
            estimated_native = Decimal(
                str(
                    calc_futu_stock_fee(
                        currency,
                        float(strike),
                        shares=shares,
                        is_sell=option_type == "call",
                    )
                )
            )
        except (TypeError, ValueError, ArithmeticError):
            return (
                {
                    **base,
                    "status": "missing",
                    "estimated_stock_fee_native": None,
                    "fee_native": None,
                    "fee_cny": None,
                    "reason": "stock_fee_estimate_failed",
                },
                None,
                False,
            )
        estimated_cny = estimated_native * exchange_rate
        return (
            {
                **base,
                "status": "missing",
                "estimated_stock_fee_native": _native_money(estimated_native),
                "estimated_stock_fee_cny": _money(estimated_cny),
                "fee_native": None,
                "fee_cny": None,
                "reason": "us_assignment_fee_rule_not_explicit",
            },
            None,
            False,
        )
    terminal = calc_futu_hk_terminal_fee(
        "assignment",
        order_price=float(strike),
        shares=shares,
        contracts=_integer(option.get("contracts_open")) or 0,
    )
    estimated_amount = terminal.get("estimated_amount")
    estimated_native = Decimal(str(estimated_amount)) if estimated_amount is not None else None
    estimated_cny = estimated_native * exchange_rate if estimated_native is not None else None
    return (
        {
            **base,
            "status": "missing",
            "estimated_stock_fee_native": _native_money(estimated_native),
            "estimated_stock_fee_cny": _money(estimated_cny),
            "fee_native": None,
            "fee_cny": None,
            "reason": terminal["reason"],
            "schedule_version": terminal["schedule_version"],
            "fee_plan_ref": terminal["fee_plan_ref"],
            "estimated_basis": terminal["estimated_basis"],
        },
        None,
        False,
    )


def _status_from(*, unavailable: bool, partial: bool) -> str:
    if unavailable:
        return "unavailable"
    return "partial" if partial else "complete"


def _portfolio_evidence_quality(
    *,
    accounts: Sequence[str],
    portfolio_evidence: Mapping[str, Any],
) -> tuple[bool, bool, list[str], dict[str, Any]]:
    warnings = [
        str(item)
        for item in (portfolio_evidence.get("warnings") or [])
        if str(item).strip()
    ]
    evidence_status = _text(portfolio_evidence.get("status")).lower()
    freshness = (
        dict(portfolio_evidence.get("freshness"))
        if isinstance(portfolio_evidence.get("freshness"), Mapping)
        else {}
    )
    freshness_status = _text(freshness.get("status")).lower()
    trust_status = _text(freshness.get("trust_status")).lower()
    unavailable = False
    partial = False

    if portfolio_evidence.get("success") is not True:
        unavailable = True
        warnings.append("portfolio evidence success is unconfirmed")
    if _text(portfolio_evidence.get("schema_version")) != PORTFOLIO_EVIDENCE_VERSION:
        unavailable = True
        warnings.append("portfolio evidence schema is missing or incompatible")
    if evidence_status == "unavailable":
        unavailable = True
    elif evidence_status == "partial":
        partial = True
    elif evidence_status != "complete":
        unavailable = True
        warnings.append("portfolio evidence status is missing or unsupported")

    if freshness_status in {"unavailable", "unknown", ""}:
        unavailable = True
        warnings.append("portfolio evidence freshness is unavailable")
    elif freshness_status == "stale":
        partial = True
        warnings.append("portfolio evidence is stale")
    elif freshness_status != "fresh":
        unavailable = True
        warnings.append("portfolio evidence freshness status is unsupported")

    if trust_status in {"unavailable", "untrusted", ""}:
        unavailable = True
        warnings.append("portfolio evidence is not trusted")
    elif trust_status == "partial":
        partial = True
        warnings.append("portfolio evidence trust is partial")
    elif trust_status != "trusted":
        unavailable = True
        warnings.append("portfolio evidence trust status is unsupported")

    normalized_accounts = [
        _text(item).lower()
        for item in accounts
        if _text(item)
    ]
    scope = (
        portfolio_evidence.get("scope")
        if isinstance(portfolio_evidence.get("scope"), Mapping)
        else {}
    )
    scope_accounts = [
        _text(item).lower()
        for item in (scope.get("accounts") or [])
        if _text(item)
    ]
    if scope_accounts != normalized_accounts:
        unavailable = True
        warnings.append("portfolio evidence account scope mismatch")

    source_snapshot = (
        portfolio_evidence.get("snapshot")
        if isinstance(portfolio_evidence.get("snapshot"), Mapping)
        else {}
    )
    if not _text(source_snapshot.get("snapshot_id")) or not _text(
        source_snapshot.get("observed_at")
        or source_snapshot.get("observed_at_utc")
    ):
        unavailable = True
        warnings.append("portfolio evidence snapshot identity is incomplete")

    return unavailable, partial, warnings, {
        "status": evidence_status or "unavailable",
        "freshness_status": freshness_status or "unavailable",
        "trust_status": trust_status or "unavailable",
        "observed_at_utc": freshness.get("observed_at_utc"),
        "dataset_ids": list(freshness.get("dataset_ids") or []),
        "reason_codes": list(freshness.get("reason_codes") or []),
        "retrieved_at_utc": portfolio_evidence.get("retrieved_at_utc"),
        "source_snapshot_id": source_snapshot.get("snapshot_id"),
        "source_observed_at": (
            source_snapshot.get("observed_at")
            or source_snapshot.get("observed_at_utc")
        ),
    }


def project_assignment_scenario(
    *,
    accounts: Sequence[str],
    portfolio_evidence: Mapping[str, Any],
    option_positions: Sequence[Mapping[str, Any]],
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Project all open short puts/calls as physically assigned.

    The function has no I/O and never reads or mutates ledgers. Long options are
    intentionally outside both its input contract and its output.
    """

    normalized_accounts = list(dict.fromkeys(_text(item).lower() for item in accounts if _text(item)))
    unavailable, partial, warnings, evidence_quality = _portfolio_evidence_quality(
        accounts=normalized_accounts,
        portfolio_evidence=portfolio_evidence,
    )

    holdings = [
        dict(row)
        for row in (portfolio_evidence.get("holdings") or [])
        if isinstance(row, Mapping) and _text(row.get("account")).lower() in normalized_accounts
    ]
    quotes = _quote_map(
        [row for row in (portfolio_evidence.get("quotes") or []) if isinstance(row, Mapping)]
    )

    cash_rows: list[dict[str, Any]] = []
    security_groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    distribution_incomplete = unavailable
    for index, holding in enumerate(holdings):
        raw_type = _text(holding.get("asset_type") or holding.get("type")).lower()
        if "option" in raw_type:
            continue
        account = _text(holding.get("account")).lower()
        broker = normalize_broker(holding.get("broker")) or _text(holding.get("broker"))
        code = canonical_symbol(holding.get("code")) or _text(holding.get("code")).upper()
        quantity = _decimal(holding.get("quantity"))
        category = _asset_category(holding)
        market_value = _decimal(
            holding.get("market_value_cny")
            if "market_value_cny" in holding
            else holding.get("market_value")
        )
        if not code or quantity is None:
            warnings.append(f"holding[{index}]: invalid code or quantity; row skipped")
            partial = True
            distribution_incomplete = True
            continue
        if quantity != 0 and market_value is None:
            warnings.append(f"{account}/{code}: CNY market value missing")
            partial = True
            distribution_incomplete = True
        row = {
            **holding,
            "account": account,
            "broker": broker,
            "code": code,
            "quantity_decimal": quantity,
            "market_value_decimal": market_value,
            "category": category,
        }
        if category == "cash":
            cash_rows.append(row)
            continue
        key = (account, broker, code)
        grouped = security_groups.setdefault(
            key,
            {
                "account": account,
                "broker": broker,
                "code": code,
                "name": _text(holding.get("name")) or code,
                "category": category,
                "opening_shares": _ZERO,
                "opening_market_value_cny": _ZERO,
                "opening_value_complete": True,
            },
        )
        grouped["opening_shares"] += quantity
        if market_value is None:
            grouped["opening_value_complete"] = False
        else:
            grouped["opening_market_value_cny"] += market_value

    starting_cash = _ZERO
    cash_complete = not unavailable
    cash_components: list[dict[str, Any]] = []
    for row in cash_rows:
        value = row["market_value_decimal"]
        if value is None:
            cash_complete = False
        else:
            starting_cash += value
        cash_components.append(
            {
                "account": row["account"],
                "broker": row["broker"],
                "code": row["code"],
                "asset_type": _text(row.get("asset_type") or row.get("type")).lower(),
                "currency": normalize_currency(row.get("currency")) or _text(row.get("currency")).upper(),
                "quantity_native": _quantity(row["quantity_decimal"]),
                "value_cny": _money(value),
            }
        )

    assignment_rows: list[dict[str, Any]] = []
    fee_items: list[dict[str, Any]] = []
    changes: dict[tuple[str, str, str], dict[str, Any]] = {}
    put_outflow = _ZERO
    call_inflow = _ZERO
    known_fees = _ZERO
    fees_complete = True
    assignment_cash_complete = True
    expiry_accumulator: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "put_outflow_cny": _ZERO,
            "call_inflow_cny": _ZERO,
            "known_fees_cny": _ZERO,
            "fees_complete": True,
            "assignments": 0,
        }
    )

    selected_positions: list[Mapping[str, Any]] = []
    for row in option_positions:
        if not isinstance(row, Mapping):
            continue
        if _text(row.get("account")).lower() not in normalized_accounts:
            continue
        if _text(row.get("status")).lower() != "open":
            continue
        if _text(row.get("side")).lower() != "short":
            continue
        if _text(row.get("option_type")).lower() not in {"put", "call"}:
            continue
        selected_positions.append(row)

    for index, option in enumerate(selected_positions):
        account = _text(option.get("account")).lower()
        broker = normalize_broker(option.get("broker")) or _text(option.get("broker"))
        symbol = canonical_symbol(option.get("symbol"))
        option_type = _text(option.get("option_type")).lower()
        state_warning = _text(option.get("state_warning"))
        if state_warning:
            warnings.append(
                f"{account}/{symbol or option.get('record_id') or index}: {state_warning}"
            )
            partial = True
        contracts = _integer(option.get("contracts_open"))
        multiplier = _integer(option.get("multiplier"))
        strike = _positive(option.get("strike"))
        currency = normalize_currency(option.get("currency"))
        expiration = _text(option.get("expiration_ymd") or option.get("expiration"))[:10]
        if (
            not symbol
            or contracts is None
            or multiplier is None
            or strike is None
            or not currency
            or not expiration
        ):
            warnings.append(f"option[{index}]: required assignment inputs missing; row skipped")
            partial = True
            assignment_cash_complete = False
            fees_complete = False
            continue

        quote = quotes.get(symbol)
        spot_native, spot_cny, exchange_rate, quote_error = _quote_values(
            quote,
            expected_currency=currency,
        )
        if quote_error:
            warnings.append(f"{account}/{symbol}: {quote_error}")
            partial = True
        if exchange_rate is None:
            assignment_cash_complete = False
        if spot_cny is None:
            distribution_incomplete = True
        shares = contracts * multiplier
        principal_native = strike * shares
        principal_cny = principal_native * exchange_rate if exchange_rate is not None else None
        stock_delta = Decimal(shares if option_type == "put" else -shares)
        cash_delta_native = -principal_native if option_type == "put" else principal_native
        cash_delta_cny = (
            cash_delta_native * exchange_rate if exchange_rate is not None else None
        )
        stock_value_delta_cny = stock_delta * spot_cny if spot_cny is not None else None

        fee_fact: dict[str, Any]
        fee_cny: Decimal | None
        fee_complete: bool
        if exchange_rate is None:
            fee_fact = {
                "status": "missing",
                "basis": "assignment_at_strike",
                "calculator": "domain.domain.fee_calc.calc_futu_stock_fee",
                "currency": currency,
                "is_sell": option_type == "call",
                "shares": shares,
                "strike": _native_money(strike),
                "estimated_stock_fee_native": None,
                "fee_native": None,
                "fee_cny": None,
                "reason": "fx_evidence_missing",
            }
            fee_cny = None
            fee_complete = False
        else:
            fee_fact, fee_cny, fee_complete = _fee_fact(
                option,
                shares=shares,
                strike=strike,
                currency=currency,
                exchange_rate=exchange_rate,
                option_type=option_type,
            )
        fee_item = {
            "record_id": option.get("record_id"),
            "account": account,
            "broker": broker,
            "symbol": symbol,
            "option_type": option_type,
            **fee_fact,
        }
        fee_items.append(fee_item)
        if fee_cny is not None:
            known_fees += fee_cny
        if not fee_complete:
            fees_complete = False
            partial = True

        if principal_cny is not None:
            if option_type == "put":
                put_outflow += principal_cny
            else:
                call_inflow += principal_cny
        else:
            assignment_cash_complete = False
        expiry = expiry_accumulator[expiration]
        expiry["assignments"] += 1
        if principal_cny is None:
            expiry["cash_complete"] = False
        elif option_type == "put":
            expiry["put_outflow_cny"] += principal_cny
        else:
            expiry["call_inflow_cny"] += principal_cny
        if fee_cny is not None:
            expiry["known_fees_cny"] += fee_cny
        if not fee_complete:
            expiry["fees_complete"] = False

        change_key = (account, broker, symbol)
        change = changes.setdefault(
            change_key,
            {
                "account": account,
                "broker": broker,
                "code": symbol,
                "put_assigned_shares": _ZERO,
                "call_assigned_shares": _ZERO,
                "spot_native": spot_native,
                "spot_cny": spot_cny,
                "currency": currency,
                "quote": quote,
            },
        )
        if option_type == "put":
            change["put_assigned_shares"] += Decimal(shares)
        else:
            change["call_assigned_shares"] += Decimal(shares)
        if change.get("spot_cny") is None and spot_cny is not None:
            change["spot_native"] = spot_native
            change["spot_cny"] = spot_cny
            change["quote"] = quote

        assignment_rows.append(
            {
                "record_id": option.get("record_id"),
                "account": account,
                "broker": broker,
                "symbol": symbol,
                "option_type": option_type,
                "expiration": expiration,
                "contracts_open": contracts,
                "multiplier": multiplier,
                "assigned_shares": shares,
                "strike_native": _native_money(strike),
                "spot_native": _native_money(spot_native),
                "currency": currency,
                "fx_to_cny": _rate(exchange_rate),
                "principal_native": _native_money(principal_native),
                "principal_cny": _money(principal_cny),
                "cash_delta_native": _native_money(cash_delta_native),
                "cash_delta_cny": _money(cash_delta_cny),
                "stock_delta_shares": _quantity(stock_delta),
                "stock_value_delta_cny": _money(stock_value_delta_cny),
                "fee": fee_fact,
                "quote": {
                    "source": quote.get("source") if isinstance(quote, Mapping) else None,
                    "source_chain": list(quote.get("source_chain") or [])
                    if isinstance(quote, Mapping)
                    else [],
                    "observed_at": quote.get("observed_at") if isinstance(quote, Mapping) else None,
                    "is_stale": bool(quote.get("is_stale")) if isinstance(quote, Mapping) else None,
                },
            }
        )

    position_changes: list[dict[str, Any]] = []
    for key in sorted(changes):
        change = changes[key]
        existing = security_groups.get(key)
        opening_shares = existing["opening_shares"] if existing else _ZERO
        ending_shares = (
            opening_shares
            + change["put_assigned_shares"]
            - change["call_assigned_shares"]
        )
        spot_cny = change.get("spot_cny")
        if spot_cny is None:
            ending_value = None
            distribution_incomplete = True
        else:
            ending_value = ending_shares * spot_cny
        opening_value = (
            existing["opening_market_value_cny"]
            if existing and existing["opening_value_complete"]
            else (opening_shares * spot_cny if spot_cny is not None else None)
        )
        category = existing["category"] if existing else "stock"
        security_groups[key] = {
            **(existing or {}),
            "account": key[0],
            "broker": key[1],
            "code": key[2],
            "name": (existing or {}).get("name") or key[2],
            "category": category,
            "opening_shares": opening_shares,
            "ending_shares": ending_shares,
            "ending_market_value_cny": ending_value,
            "impacted": True,
        }
        position_changes.append(
            {
                "account": key[0],
                "broker": key[1],
                "code": key[2],
                "category": category,
                "currency": change.get("currency"),
                "opening_shares": _quantity(opening_shares),
                "put_assigned_shares": _quantity(change["put_assigned_shares"]),
                "call_assigned_shares": _quantity(change["call_assigned_shares"]),
                "ending_shares": _quantity(ending_shares),
                "spot_native": _native_money(change.get("spot_native")),
                "spot_cny": _money(spot_cny),
                "opening_market_value_cny": _money(opening_value),
                "ending_market_value_cny": _money(ending_value),
                "liability_kind": "short_stock" if ending_shares < 0 else None,
            }
        )

    cash_base_complete = cash_complete and assignment_cash_complete
    ending_cash_gross = (
        starting_cash - put_outflow + call_inflow if cash_base_complete else None
    )
    ending_cash_net = (
        ending_cash_gross - known_fees
        if ending_cash_gross is not None and fees_complete
        else None
    )
    conservative_cash_net = (
        starting_cash - put_outflow - known_fees
        if cash_base_complete and fees_complete
        else None
    )

    account_cash: dict[str, dict[str, Any]] = {
        account: {
            "opening_cash_mmf_cny": _ZERO,
            "opening_complete": True,
            "put_outflow_cny": _ZERO,
            "call_inflow_cny": _ZERO,
            "known_fees_cny": _ZERO,
            "fees_complete": True,
            "cash_complete": True,
            "cash_components": [],
        }
        for account in normalized_accounts
    }
    for component, row in zip(cash_components, cash_rows):
        bucket = account_cash[row["account"]]
        bucket["cash_components"].append(component)
        value = row["market_value_decimal"]
        if value is None:
            bucket["opening_complete"] = False
        else:
            bucket["opening_cash_mmf_cny"] += value
    for assignment, fee_item in zip(assignment_rows, fee_items):
        bucket = account_cash[assignment["account"]]
        principal_cny = _decimal(assignment.get("principal_cny"))
        if principal_cny is None:
            bucket["cash_complete"] = False
        elif assignment["option_type"] == "put":
            bucket["put_outflow_cny"] += principal_cny
        else:
            bucket["call_inflow_cny"] += principal_cny
        fee_cny = _decimal(fee_item.get("fee_cny"))
        if fee_cny is not None:
            bucket["known_fees_cny"] += fee_cny
        if fee_item.get("status") != "estimated":
            bucket["fees_complete"] = False

    account_breakdown: list[dict[str, Any]] = []
    for account in normalized_accounts:
        bucket = account_cash[account]
        account_gross = (
            bucket["opening_cash_mmf_cny"]
            - bucket["put_outflow_cny"]
            + bucket["call_inflow_cny"]
            if bucket["opening_complete"] and bucket["cash_complete"]
            else None
        )
        account_net = (
            account_gross - bucket["known_fees_cny"]
            if account_gross is not None and bucket["fees_complete"]
            else None
        )
        short_stock_liability = sum(
            abs(_decimal(row.get("ending_market_value_cny")) or _ZERO)
            for row in position_changes
            if row["account"] == account
            and _decimal(row.get("ending_market_value_cny")) is not None
            and (_decimal(row.get("ending_market_value_cny")) or _ZERO) < 0
        )
        account_breakdown.append(
            {
                "account": account,
                "opening_cash_mmf_cny": _money(
                    bucket["opening_cash_mmf_cny"] if bucket["opening_complete"] else None
                ),
                "put_assignment_outflow_cny": _money(
                    bucket["put_outflow_cny"] if bucket["cash_complete"] else None
                ),
                "call_assignment_inflow_cny": _money(
                    bucket["call_inflow_cny"] if bucket["cash_complete"] else None
                ),
                "known_estimated_fees_cny": _money(bucket["known_fees_cny"]),
                "fees_complete": bucket["fees_complete"],
                "ending_cash_gross_cny": _money(account_gross),
                "ending_cash_net_estimated_cny": _money(account_net),
                "funding_gap_cny": _money(max(_ZERO, -account_net)) if account_net is not None else None,
                "short_stock_liability_cny": _money(short_stock_liability),
                "cash_components": bucket["cash_components"],
            }
        )

    expiry_ladder: list[dict[str, Any]] = []
    cumulative_put = _ZERO
    cumulative_call = _ZERO
    cumulative_fees = _ZERO
    cumulative_complete = cash_complete
    cumulative_fee_complete = True
    for expiration in sorted(expiry_accumulator):
        values = expiry_accumulator[expiration]
        cumulative_put += values["put_outflow_cny"]
        cumulative_call += values["call_inflow_cny"]
        cumulative_fees += values["known_fees_cny"]
        cumulative_complete = cumulative_complete and values.get("cash_complete", True)
        cumulative_fee_complete = cumulative_fee_complete and values["fees_complete"]
        ladder_gross = (
            starting_cash - cumulative_put + cumulative_call
            if cumulative_complete
            else None
        )
        ladder_net = (
            ladder_gross - cumulative_fees
            if ladder_gross is not None and cumulative_fee_complete
            else None
        )
        expiry_ladder.append(
            {
                "expiration": expiration,
                "assignments": values["assignments"],
                "put_outflow_cny": _money(values["put_outflow_cny"]),
                "call_inflow_cny": _money(values["call_inflow_cny"]),
                "known_estimated_fees_cny": _money(values["known_fees_cny"]),
                "fees_complete": values["fees_complete"],
                "cumulative_put_outflow_cny": _money(cumulative_put),
                "cumulative_call_inflow_cny": _money(cumulative_call),
                "projected_ending_cash_net_cny": _money(ladder_net),
                "funding_gap_cny": _money(max(_ZERO, -ladder_net))
                if ladder_net is not None
                else None,
                "basis": "current_snapshot_fx_and_spot; early_assignment_not_modeled",
            }
        )

    distribution_rows: list[dict[str, Any]] = []
    for key in sorted(security_groups):
        row = security_groups[key]
        if row.get("impacted"):
            value = row.get("ending_market_value_cny")
            shares = row.get("ending_shares")
        else:
            value = (
                row["opening_market_value_cny"]
                if row["opening_value_complete"]
                else None
            )
            shares = row["opening_shares"]
        if value is None:
            distribution_incomplete = True
        distribution_rows.append(
            {
                "account": row["account"],
                "broker": row["broker"],
                "code": row["code"],
                "name": row["name"],
                "category": row["category"],
                "quantity": _quantity(shares),
                "value_cny": _money(value),
                "_value": value,
            }
        )
    distribution_rows.append(
        {
            "account": "combined",
            "broker": "combined",
            "code": "CASH+MMF",
            "name": "现金及货币基金（指派后）",
            "category": "cash",
            "quantity": None,
            "value_cny": _money(ending_cash_net),
            "_value": ending_cash_net,
        }
    )
    if ending_cash_net is None:
        distribution_incomplete = True

    by_code_map: dict[tuple[str, str], Decimal | None] = {}
    by_code_meta: dict[tuple[str, str], dict[str, Any]] = {}
    for row in distribution_rows:
        key = (row["category"], row["code"])
        by_code_meta.setdefault(
            key,
            {
                "category": row["category"],
                "code": row["code"],
                "name": row["name"],
            },
        )
        value = row["_value"]
        if key not in by_code_map:
            by_code_map[key] = value
        elif by_code_map[key] is None or value is None:
            by_code_map[key] = None
        else:
            by_code_map[key] = by_code_map[key] + value

    complete_values = not distribution_incomplete and all(
        value is not None for value in by_code_map.values()
    )
    gross_assets = (
        sum((value for value in by_code_map.values() if value is not None and value > 0), _ZERO)
        if complete_values
        else None
    )
    liabilities = (
        sum((-value for value in by_code_map.values() if value is not None and value < 0), _ZERO)
        if complete_values
        else None
    )
    net_assets = gross_assets - liabilities if gross_assets is not None and liabilities is not None else None

    by_code: list[dict[str, Any]] = []
    liability_rows: list[dict[str, Any]] = []
    for key in sorted(by_code_map):
        value = by_code_map[key]
        meta = by_code_meta[key]
        item = {
            **meta,
            "value_cny": _money(value),
            "weight_of_gross_assets": (
                _rate(value / gross_assets)
                if value is not None and value > 0 and gross_assets is not None and gross_assets > 0
                else None
            ),
        }
        by_code.append(item)
        if value is not None and value < 0:
            liability_rows.append(
                {
                    **meta,
                    "liability_cny": _money(-value),
                    "kind": "funding" if meta["category"] == "cash" else "short_position",
                }
            )

    by_category_map: dict[str, Decimal | None] = {}
    for (category, _code), value in by_code_map.items():
        if category not in by_category_map:
            by_category_map[category] = value
        elif by_category_map[category] is None or value is None:
            by_category_map[category] = None
        else:
            by_category_map[category] = by_category_map[category] + value
    by_category = [
        {
            "category": category,
            "value_cny": _money(value),
            "weight_of_gross_assets": (
                _rate(value / gross_assets)
                if value is not None and value > 0 and gross_assets is not None and gross_assets > 0
                else None
            ),
        }
        for category, value in sorted(by_category_map.items())
    ]

    if distribution_incomplete:
        partial = True
    fee_status = "complete" if fees_complete else "partial"
    missing_fee_count = sum(1 for item in fee_items if item.get("status") == "missing")
    status = _status_from(unavailable=unavailable, partial=partial)
    deduped_warnings = _dedupe_warnings(warnings)

    fx_facts: list[dict[str, Any]] = []
    seen_fx: set[tuple[str, str | None, str | None]] = set()
    for quote in quotes.values():
        currency = normalize_currency(quote.get("currency"))
        rate_value = (
            Decimal("1")
            if currency == "CNY"
            else _positive(
                quote.get("exchange_rate_to_cny")
                if "exchange_rate_to_cny" in quote
                else quote.get("exchange_rate")
            )
        )
        key = (currency, _text(quote.get("source")) or None, _text(quote.get("observed_at")) or None)
        if not currency or rate_value is None or key in seen_fx:
            continue
        seen_fx.add(key)
        fx_facts.append(
            {
                "currency": currency,
                "rate_to_cny": _rate(rate_value),
                "source": quote.get("source"),
                "observed_at": quote.get("observed_at"),
                "quality": "stale" if quote.get("is_stale") else "current",
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "scope": {
            "accounts": normalized_accounts,
            "scenario": "all_open_short_put_and_call_assigned",
            "report_name": "指派后资产分布（不含 Long Option）",
            "reporting_currency": "CNY",
            "include_mmf_as_cash": True,
            "include_long_options": False,
        },
        "snapshot": dict(snapshot),
        "portfolio_evidence": evidence_quality,
        "summary": {
            "assignment_count": len(assignment_rows),
            "short_put_count": sum(1 for row in assignment_rows if row["option_type"] == "put"),
            "short_call_count": sum(1 for row in assignment_rows if row["option_type"] == "call"),
            "position_change_count": len(position_changes),
            "warning_count": len(deduped_warnings),
        },
        "cash_coverage": {
            "currency": "CNY",
            "available_cash_and_mmf_cny": _money(starting_cash if cash_complete else None),
            "gross_put_requirement_cny": _money(put_outflow if assignment_cash_complete else None),
            "call_assignment_inflow_cny": _money(call_inflow if assignment_cash_complete else None),
            "known_estimated_fees_cny": _money(known_fees),
            "total_fees_cny": _money(known_fees) if fees_complete else None,
            "ending_cash_gross_cny": _money(ending_cash_gross),
            "ending_cash_net_estimated_cny": _money(ending_cash_net),
            "conservative_put_only_funding_gap_cny": (
                _money(max(_ZERO, -conservative_cash_net))
                if conservative_cash_net is not None
                else None
            ),
            "terminal_funding_gap_cny": (
                _money(max(_ZERO, -ending_cash_net))
                if ending_cash_net is not None
                else None
            ),
            "basis": "cross_account_cny_economic_coverage",
            "operational_note": (
                "账户、券商和币种拆分仅用于操作约束；主覆盖口径假设组合内资金可自由等值调拨。"
            ),
        },
        "fee_summary": {
            "status": fee_status,
            "known_estimated_fees_cny": _money(known_fees),
            "total_fees_cny": _money(known_fees) if fees_complete else None,
            "missing_fee_count": missing_fee_count,
            "items": fee_items,
        },
        "assignments": assignment_rows,
        "position_changes": position_changes,
        "expiration_ladder": expiry_ladder,
        "distribution": {
            "status": "complete" if complete_values else "partial",
            "gross_assets_cny": _money(gross_assets),
            "liabilities_cny": _money(liabilities),
            "net_assets_cny": _money(net_assets),
            "by_category": by_category,
            "by_code": by_code,
            "liabilities": liability_rows,
        },
        "account_breakdown": account_breakdown,
        "fx_facts": sorted(
            fx_facts,
            key=lambda item: (
                _text(item.get("currency")),
                _text(item.get("observed_at")),
                _text(item.get("source")),
            ),
        ),
        "warnings": deduped_warnings,
    }


__all__ = [
    "PORTFOLIO_EVIDENCE_VERSION",
    "SCHEMA_VERSION",
    "project_assignment_scenario",
]
