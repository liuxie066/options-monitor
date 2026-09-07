from copy import deepcopy
from decimal import Decimal
import json

import pytest

from domain.domain.trade_execution import (
    canonical_decimal,
    canonical_utc_instant,
    conflicting_execution_associations,
    epoch_milliseconds_instant,
    normalize_execution_input,
)
from src.application.trades.normalizer import canonical_trade_execution_content, normalize_trade_deal


def execution_input() -> dict:
    return {
        "broker_account_ref": {
            "broker_account_id": "account-1",
            "broker_id": "futu",
            "external_account_id": "900000000000000001",
            "environment": "REAL",
            "account_label": "lx",
        },
        "instrument_ref": {
            "asset_type": "option",
            "symbol": "NVDA",
            "market": "US",
            "currency": "USD",
            "option_type": "put",
            "strike": "100.000",
            "expiration_ymd": "2026-09-18",
            "multiplier": "100",
            "deliverable": {"symbol": "NVDA", "quantity": "100"},
        },
        "external_id_namespace": "futu-us-deals",
        "external_execution_id": "fill-1",
        "external_order_namespace": "futu-orders",
        "external_order_id": "order-1",
        "side": "sell",
        "quantity": "1",
        "price": "0.123456789012345678901234567890123456789",
        "currency": "USD",
        "occurred_at_utc": "2026-09-07T01:02:03.123456789Z",
        "evidence_refs": ["synthetic-test-evidence"],
    }


def test_standard_input_stays_exact_and_offline(monkeypatch) -> None:
    def unexpected_provider_call(**_kwargs):
        pytest.fail("standard input must not query Futu or multiplier storage")

    monkeypatch.setattr("src.application.trades.normalizer.resolve_multiplier_with_source_and_diagnostics", unexpected_provider_call)
    payload = execution_input()
    deal = normalize_trade_deal(payload)
    assert deal.execution_input["errors"] == []
    assert deal.execution_input["price"] == payload["price"]
    assert deal.execution_input["occurred_at_utc"] == payload["occurred_at_utc"]
    assert deal.execution_input["broker_account_ref"]["external_account_id"] == "900000000000000001"
    assert deal.asset_type == "option"
    assert json.loads(json.dumps(deal.execution_input, allow_nan=False))["price"] == payload["price"]


def test_futu_aliases_match_standard_content_without_rounding() -> None:
    standard = execution_input()
    raw = {
        "broker_account_id": "account-1", "trd_acc_id": "900000000000000001", "trd_env": "REAL",
        "external_id_namespace": "futu-us-deals", "dealID": "fill-1",
        "external_order_namespace": "futu-orders", "orderID": "order-1",
        "code": "US.NVDA260918P100000", "trd_side": "SELL", "qty": "1.00",
        "dealt_price": standard["price"], "contract_multiplier": "100.00",
        "create_time": "2026-09-07 09:02:03.123456789", "deliverable": {"symbol": "NVDA", "quantity": "100.00"},
        "fee": "99", "strategy": "derived-csp", "diagnostics": "different-source",
    }
    left = canonical_trade_execution_content(standard)
    right = canonical_trade_execution_content(raw)
    assert left["errors"] == right["errors"] == []
    assert left["economic"] == right["economic"]
    assert conflicting_execution_associations(left, right) == []
    raw["create_time"] = "2026-09-07 09:02:03.123456788"
    assert canonical_trade_execution_content(raw)["economic"] != left["economic"]


@pytest.mark.parametrize("field,value,error", [
    ("quantity", "1.5", "invalid:quantity:integer_required"),
    ("quantity", "0", "invalid:quantity:must_be_positive"),
    ("price", "NaN", "invalid:price:non_finite_decimal"),
    ("price", "Infinity", "invalid:price:non_finite_decimal"),
    ("price", 1.5, "invalid:price:decimal_string_required"),
    ("price", None, "missing:price"),
    ("price", "-1", "invalid:price:must_be_nonnegative"),
    ("occurred_at_utc", "2026-09-07", "invalid:occurred_at_utc:timestamp_with_seconds_and_timezone_required"),
    ("external_id_namespace", None, "missing:external_id_namespace"),
    ("schema_version", "trade_execution.v0", "unsupported:schema_version"),
    ("data_type", "order_summary", "unsupported:data_type:execution_required"),
])
def test_incomplete_or_invalid_execution_keeps_specific_diagnostic(field, value, error) -> None:
    payload = execution_input()
    payload[field] = value
    normalized = normalize_execution_input(payload)
    assert error in normalized["errors"]
    assert payload[field] == value


def test_missing_zero_and_order_summary_are_not_fills() -> None:
    payload = execution_input()
    payload["price"] = "0.000"
    zero = canonical_trade_execution_content(payload)
    assert zero["errors"] == []
    payload.pop("price")
    assert canonical_trade_execution_content(payload)["economic"] != zero["economic"]
    deal = normalize_trade_deal({"deal_id": "summary", "code": "US.NVDA", "dealt_qty": "2", "dealt_avg_price": "100"})
    assert deal.contracts is None and deal.price is None
    assert "unsupported:data_type:execution_required" in deal.execution_input["errors"]


def test_asset_class_requires_source_evidence_and_stock_accepts_share_units() -> None:
    unknown = normalize_trade_deal({"symbol": "NVDA", "side": "buy", "qty": "1", "price": "1"})
    assert unknown.asset_type is None
    unsupported = normalize_trade_deal({"code": "HK.12345", "security_type": "WARRANT"})
    assert unsupported.asset_type is None
    stock = execution_input()
    stock["instrument_ref"] = {"asset_type": "stock", "symbol": "NVDA", "market": "US", "currency": "USD"}
    stock["quantity"] = "0.25"
    normalized = normalize_execution_input(stock)
    assert normalized["errors"] == [] and normalized["quantity_unit"] == "share"
    assert "multiplier" not in normalized["instrument_ref"]


def test_account_environment_instrument_and_known_associations_are_isolated() -> None:
    original = execution_input()
    left = canonical_trade_execution_content(original)
    for section, field, value in (
        ("broker_account_ref", "external_account_id", "another-account"),
        ("broker_account_ref", "environment", "SIMULATE"),
        ("instrument_ref", "multiplier", "10"),
        ("instrument_ref", "deliverable", {"symbol": "NVDA", "quantity": "10"}),
    ):
        changed = deepcopy(original)
        changed[section][field] = value
        assert canonical_trade_execution_content(changed)["economic"] != left["economic"]
    original["broker_account_ref"]["account_label"] = "sy"
    original["external_order_id"] = "order-2"
    original["position_effect"] = "open"
    right = canonical_trade_execution_content(original)
    assert right["economic"] == left["economic"]
    assert conflicting_execution_associations(left, right) == ["external_order_id"]
    left["associations"]["position_effect"] = "close"
    assert conflicting_execution_associations(left, right) == ["external_order_id", "position_effect"]


def test_exact_decimal_and_timestamp_helpers_do_not_use_decimal_context_or_float() -> None:
    number = Decimal("1.234567890123456789012345678901234567890000")
    assert canonical_decimal(number) == "1.23456789012345678901234567890123456789"
    assert canonical_decimal("1e-20") == "0.00000000000000000001"
    assert canonical_decimal("1.00") == canonical_decimal("1")
    assert canonical_utc_instant("2026-09-07T09:02:03.000000000123+08:00") == "2026-09-07T01:02:03.000000000123Z"
    assert epoch_milliseconds_instant("0.123456789012345678901234567890") == "1970-01-01T00:00:00.00012345678901234567890123456789Z"
    assert epoch_milliseconds_instant("-0.1") == "1969-12-31T23:59:59.9999Z"
