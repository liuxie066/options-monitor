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


def test_oss_reader_checks_account_and_recomputes_adjustments(monkeypatch):
    import json
    from types import SimpleNamespace

    from src.application import futu_mmf_oss

    payload = {
        "schema": "futu.mmf-funding.v1", "account": "sy", "status": "verified",
        "adjustments": {"HKD": "50"},
        "transfers": [{
            "receipt_id": "receipt-1", "direction": "redemption", "currency": "HKD",
            "amount": "100", "source_debit": "75", "target_credit": "25",
        }],
    }
    calls = []

    def cat(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout=json.dumps(payload).encode(), returncode=0)

    monkeypatch.setenv("FUTU_MMF_FUNDING_OSS_PREFIX", "oss://test-bucket/mmf")
    monkeypatch.setattr(futu_mmf_oss.subprocess, "run", cat)
    assert futu_mmf_oss.read_mmf_oss("sy") == payload
    assert calls == [["ossutil", "cat", "oss://test-bucket/mmf/sy.json", "--count", "262145"]]
    with pytest.raises(ValueError, match="Invalid OSS"):
        futu_mmf_oss.read_mmf_oss("lx")
    payload["adjustments"] = {"HKD": "75"}
    with pytest.raises(ValueError, match="totals"):
        futu_mmf_oss.read_mmf_oss("sy")
