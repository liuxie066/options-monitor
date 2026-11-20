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
    scope = {"account": "sy", "physical_account_id": "123", "environment": "REAL"}
    before = deepcopy(context)
    with pytest.raises(ValueError, match="diverged"):
        apply_mmf_funding(context, {**funding, "cash": {**funding["cash"], "HKD": 0}}, **scope)
    assert context == before
    apply_mmf_funding(context, funding, **scope)
    apply_mmf_funding(context, funding, **scope)
    assert context["cash_by_currency"] == {"CNY": 1032301.98, "HKD": 0, "USD": 0}
    assert context["cash_capacity_by_currency"]["HKD"]["amount"] == 0
    assert context["cash_components_by_currency"]["HKD"]["hk_cash"] == -84134.88
