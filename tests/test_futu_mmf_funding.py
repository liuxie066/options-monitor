from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256

import pytest

from src.application.futu_mmf_funding import apply_mmf_funding


def test_mmf_funding_applies_same_snapshot_native_balances_once():
    context = {
        "cash_balance_reliable": True,
        "cash_components_by_currency": {
            "CNY": {"cn_cash": 0, "fund_assets": 1032301.98},
            "HKD": {"hk_cash": -84134.88},
            "USD": {"us_cash": 0},
        },
        "cash_capacity_by_currency": {currency: {} for currency in ("CNY", "HKD", "USD")},
    }
    funding = {
        "account": "sy", "status": "verified", "environment": "REAL",
        "account_fingerprint": "sha256:" + sha256(b"123").hexdigest(),
        "snapshot_id": "snapshot-1", "observed_at": datetime.now(timezone.utc).isoformat(),
        "cash": {"CNY": 0, "HKD": -84134.88, "USD": 0}, "fund_assets_cny": "1032301.98",
        "adjustments": {"HKD": "84134.88"},
    }
    context["cash_source_observed_at"] = funding["observed_at"]
    scope = {"account": "sy", "physical_account_id": "123", "environment": "REAL"}
    before = deepcopy(context)
    zero = {**funding, "status": "none", "adjustments": {}}
    with pytest.raises(ValueError, match="scope"):
        apply_mmf_funding(context, {**zero, "environment": "SIMULATE"}, **scope)
    with pytest.raises(ValueError, match="observation"):
        apply_mmf_funding(context, {**zero, "observed_at": "2099-01-01T00:00:00Z"}, **scope)
    with pytest.raises(ValueError, match="diverged"):
        apply_mmf_funding(context, {**funding, "cash": {**funding["cash"], "HKD": 0}}, **scope)
    assert context == before
    apply_mmf_funding(context, funding, **scope)
    apply_mmf_funding(context, funding, **scope)
    assert context["cash_by_currency"] == {"CNY": 1032301.98, "HKD": 0, "USD": 0}
    assert context["cash_capacity_by_currency"]["HKD"]["amount"] == 0
    assert context["cash_components_by_currency"]["HKD"]["hk_cash"] == -84134.88


def test_shared_reader_checks_account_and_recomputes_adjustments(monkeypatch, tmp_path):
    import json

    from src.application.futu_mmf_shared import read_mmf_shared

    payload = {
        "schema": "futu.mmf-funding.v1", "account": "sy", "status": "verified",
        "adjustments": {"HKD": "50"},
        "transfers": [{
            "receipt_id": "receipt-1", "direction": "redemption", "currency": "HKD",
            "amount": "100", "source_debit": "75", "target_credit": "25",
        }],
    }
    monkeypatch.setenv("FUTU_MMF_FUNDING_DIR", str(tmp_path))
    target = tmp_path / "sy.json"
    target.write_text(json.dumps(payload))
    assert read_mmf_shared("sy") == payload
    with pytest.raises(FileNotFoundError):
        read_mmf_shared("lx")
    with pytest.raises(ValueError, match="Invalid account"):
        read_mmf_shared("../sy")
    payload["adjustments"] = {"HKD": "75"}
    target.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="totals"):
        read_mmf_shared("sy")
