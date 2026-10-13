from __future__ import annotations

from copy import deepcopy

import pytest

from domain.domain.risk_capacity import evaluate_booked_option_capacity


def _inputs(option_type="call"):
    ref = {"broker_id": "futu", "external_account_id": "1001", "environment": "REAL"}
    contract = {"underlying_symbol": "NVDA", "option_type": option_type,
                "strike": "100", "expiration_ymd": "2026-12-18"}
    fact = {"account": "fixture", "broker_account_ref": ref, "contract_key": contract,
            "contracts_open": 1, "position_side": "short", "multiplier": 100, "currency": "USD"}
    return dict(
        fact=fact, facts=[fact], wheel_read_model={"wheel_branches": []},
        snapshot={"rows": [
            {"instrument_ref": {"asset_type": "stock", "market": "us", "symbol": "NVDA"},
             "position_side": "long", "quantity": "100", "can_sell_qty": "0"},
            {"instrument_ref": {**contract, "symbol": "NVDA", "asset_type": "option",
                                "market": "us", "multiplier": 100, "currency": "USD"},
             "position_side": "short", "quantity": "1"},
        ]},
        cash_usable=True, cash_by_currency={"USD": 10000}, fx_status="available",
        convert_currency=lambda amount, source, target: amount if source == target else None,
    )


def _assert(result, reasons=(), *, complete=True):
    assert result == {"status": "unavailable" if reasons else "available",
                      "reason_codes": sorted(reasons), "evidence_complete": complete}


@pytest.mark.parametrize("option_type", ["call", "put"])
def test_booked_obligation_is_counted_once_without_mutating_inputs(option_type):
    args = _inputs(option_type)
    before = deepcopy(args)
    _assert(evaluate_booked_option_capacity(**args))
    assert args == before


@pytest.mark.parametrize("option_type,reason", [
    ("call", "account_stock_capacity_exceeded"), ("put", "account_cash_capacity_exceeded"),
])
def test_verified_zero_is_shortage_but_combo_only_reconciles(option_type, reason):
    args = _inputs(option_type)
    args["snapshot"]["rows"][0]["quantity"] = "0"
    args["cash_by_currency"] = {"USD": 0}
    _assert(evaluate_booked_option_capacity(**args), [reason])
    _assert(evaluate_booked_option_capacity(**args, check_account_capacity=False))


@pytest.mark.parametrize("cash_usable,reason", [
    (False, "cash_capacity_unavailable"), (None, "capacity_basis_unavailable"),
])
def test_unknown_cash_is_not_proven_shortage_and_mismatch_has_precedence(cash_usable, reason):
    args = _inputs("put")
    args.update(cash_usable=cash_usable, cash_by_currency={"USD": 0})
    _assert(evaluate_booked_option_capacity(**args), [reason], complete=False)
    args["snapshot"]["rows"][1]["quantity"] = "2"
    _assert(evaluate_booked_option_capacity(**args), ["broker_ledger_positions_mismatch"], complete=False)


@pytest.mark.parametrize("quantity", ["NaN", "Infinity", "-1", "0.5"])
def test_invalid_broker_quantity_cannot_prove_capacity(quantity):
    args = _inputs()
    args["snapshot"]["rows"][1]["quantity"] = quantity
    _assert(evaluate_booked_option_capacity(**args), ["capacity_basis_unavailable"], complete=False)


def test_same_contract_from_another_physical_account_cannot_fill_ledger_gap():
    args = _inputs()
    wrong = deepcopy(args["fact"])
    wrong["broker_account_ref"]["external_account_id"] = "other"
    args["facts"] = [wrong]
    _assert(evaluate_booked_option_capacity(**args),
            ["ledger_capacity_account_unproven", "broker_ledger_positions_mismatch"], complete=False)


@pytest.mark.parametrize("option_type,reason", [
    ("call", "account_stock_capacity_exceeded"), ("put", "account_cash_capacity_exceeded"),
])
def test_fill_conversion_releases_only_its_remaining_reservation(option_type, reason):
    args = _inputs(option_type)
    branch = {"wheel_branch_id": "branch", "direction": option_type, "symbol": "NVDA",
              "lifecycle_status": "active", "integrity_status": "trusted",
              "active_intent_reserved_shares": 100, "active_intent_ids": ["intent"],
              "active_intent_reservations": [{"intent_id": "intent", "remaining_contracts": 1,
                                               "cash_reservation_amount": "10000", "currency": "USD"}]}
    args["wheel_read_model"]["wheel_branches"] = [branch]
    _assert(evaluate_booked_option_capacity(**args), [reason])
    consume = {"wheel_branch_id": "branch", "intent_id": "intent", "contracts": 1}
    _assert(evaluate_booked_option_capacity(**args, consumed_reservation=consume))
    consume["contracts"] = 2
    _assert(evaluate_booked_option_capacity(**args, consumed_reservation=consume),
            ["capacity_basis_unavailable"], complete=False)
    branch["integrity_status"] = "untrusted"
    _assert(evaluate_booked_option_capacity(**args), ["capacity_basis_unavailable"], complete=False)
    branch["lifecycle_status"] = "ended"
    _assert(evaluate_booked_option_capacity(**args))


def test_foreign_collateral_uses_shared_native_cash_owner(monkeypatch):
    from domain.domain import risk_capacity

    args = _inputs("put")
    hk = deepcopy(args["fact"])
    hk.update(currency="HKD", multiplier=500)
    hk["contract_key"].update(underlying_symbol="0700.HK", strike="10")
    args["facts"].append(hk)
    args["snapshot"]["rows"].append({
        "instrument_ref": {**hk["contract_key"], "symbol": "0700.HK", "asset_type": "option",
                           "market": "hk", "multiplier": 500, "currency": "HKD"},
        "quantity": "1", "position_side": "short",
    })
    args["cash_by_currency"] = {"USD": 10000, "HKD": 5000}
    original = risk_capacity.compute_sell_put_effective_cash
    calls = []

    def record(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(risk_capacity, "compute_sell_put_effective_cash", record)
    _assert(evaluate_booked_option_capacity(**args))
    assert len(calls) == 1
    assert calls[0]["cash_secured_by_currency"] == {"USD": 0.0, "HKD": 5000.0}
    assert calls[0]["cash_required_native"] == 10000
