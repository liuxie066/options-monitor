from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.application.strategy_lab.top1.capability_receipts import (
    ACCOUNT_FEE_PLAN_RECEIPT_SCHEMA,
    CAPABILITY_FACTS,
    MAX_CAPABILITY_RECEIPT_BYTES,
    Top1CapabilityReceiptError,
    capability_facts_from_receipt,
    load_account_fee_plan_receipt,
    read_top1_capability_receipt,
    refresh_top1_capability_receipt,
)


CONTRACT = "HK.00700260828P00400000"
BINDING = {"host": "127.0.0.1", "port": 11111}


class FakeGateway:
    def __init__(self) -> None:
        self.fail_terms = False

    def get_snapshot(self, codes: list[str]) -> list[dict[str, object]]:
        return [{"code": codes[0], "bid_price": 1.2, "ask_price": 1.3}]

    def get_exact_expiration_option_terms(self, **_kwargs: object) -> dict[str, object] | None:
        if self.fail_terms:
            return None
        return {
            "contract_symbol": CONTRACT,
            "stock_owner": "HK.00700",
            "expiration": "2026-08-28",
            "option_type": "PUT",
            "option_standard_type": "STANDARD",
            "strike": 400.0,
            "multiplier": 100,
            "currency": "HKD",
        }

    def get_history_kl_quota(self) -> dict[str, object]:
        return {
            "used_quota": 1,
            "remain_quota": 99,
            "detail_list": [
                {
                    "code": "HK.00700",
                    "request_time": "2026-08-15 09:31:00",
                }
            ],
        }

    def get_exact_expiration_close(self, **_kwargs: object) -> dict[str, object]:
        return {
            "code": "HK.00700",
            "expiration": "2026-08-14",
            "close": 600.0,
        }


def _fee_plan_payload() -> dict[str, object]:
    return {
        "schema_version": ACCOUNT_FEE_PLAN_RECEIPT_SCHEMA,
        "market": "HK",
        "account": "user1",
        "commission_free": True,
        "platform_fee": 15.0,
        "fee_plan_ref": "futu-hk-plan.v1",
        "observed_at_utc": "2026-08-16T01:00:00Z",
        "evidence_ref": "operator://futu/user1/fee-plan/2026-08-16",
        "evidence_sha256": "a" * 64,
    }


def _fee_plan(tmp_path: Path) -> dict[str, object]:
    path = tmp_path / "fee-plan.json"
    path.write_text(json.dumps(_fee_plan_payload()), encoding="utf-8")
    return load_account_fee_plan_receipt(path)


def _refresh(tmp_path: Path, gateway: Any, observed_at_utc: str) -> dict[str, object]:
    return refresh_top1_capability_receipt(
        tmp_path,
        gateway=gateway,
        market="HK",
        account="user1",
        opend_binding=BINDING,
        account_fee_plan_receipt=_fee_plan(tmp_path),
        stock_owner="HK.00700",
        contract_symbol=CONTRACT,
        terms_expiration="2026-08-28",
        close_expiration="2026-08-14",
        observed_at_utc=observed_at_utc,
    )


def test_refresh_publishes_one_compact_receipt_and_readiness_facts(tmp_path: Path) -> None:
    first = _refresh(tmp_path, FakeGateway(), "2026-08-16T02:00:00Z")

    receipt_path = tmp_path.joinpath(*str(first["receipt_ref"]).split("/"))
    assert receipt_path.stat().st_size <= MAX_CAPABILITY_RECEIPT_BYTES
    assert capability_facts_from_receipt(first) == {name: True for name in CAPABILITY_FACTS}
    assert "request_time" not in receipt_path.read_text(encoding="utf-8")

    second = _refresh(tmp_path, FakeGateway(), "2026-08-16T03:00:00Z")
    assert second["receipt_ref"] == first["receipt_ref"]
    assert second["receipt_file_sha256"] != first["receipt_file_sha256"]
    assert list(receipt_path.parent.glob("*.json")) == [receipt_path]
    assert (
        read_top1_capability_receipt(
            tmp_path,
            market="HK",
            account="user1",
            expected_opend_binding=BINDING,
        )
        == second
    )


def test_receipt_fails_closed_for_provider_failure_binding_drift_and_tamper(
    tmp_path: Path,
) -> None:
    gateway = FakeGateway()
    gateway.fail_terms = True
    with pytest.raises(Top1CapabilityReceiptError) as failed:
        _refresh(tmp_path, gateway, "2026-08-16T02:00:00Z")
    assert failed.value.reason_code == "exact_expiration_terms_receipt_unavailable"
    assert not (tmp_path / "strategy_lab").exists()

    receipt = _refresh(tmp_path, FakeGateway(), "2026-08-16T02:00:00Z")
    with pytest.raises(Top1CapabilityReceiptError) as drifted:
        read_top1_capability_receipt(
            tmp_path,
            market="HK",
            account="user1",
            expected_opend_binding={"host": "127.0.0.1", "port": 22222},
        )
    assert drifted.value.reason_code == "top1_capability_receipt_unavailable"

    receipt_path = tmp_path.joinpath(*str(receipt["receipt_ref"]).split("/"))
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload["quote_observation_receipt"]["ask"] = 0
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(Top1CapabilityReceiptError) as tampered:
        read_top1_capability_receipt(
            tmp_path,
            market="HK",
            account="user1",
            expected_opend_binding=BINDING,
        )
    assert tampered.value.reason_code == "top1_capability_receipt_unavailable"


@pytest.mark.parametrize(
    "change",
    [
        {"commission_free": 1},
        {"platform_fee": -1},
        {"fee_plan_ref": ""},
        {"evidence_sha256": "bad"},
    ],
)
def test_fee_plan_receipt_requires_auditable_exact_facts(tmp_path: Path, change: dict[str, object]) -> None:
    payload = {**_fee_plan_payload(), **change}
    path = tmp_path / "fee-plan.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(Top1CapabilityReceiptError):
        load_account_fee_plan_receipt(path)


def test_refresh_rejects_cross_account_fee_plan(tmp_path: Path) -> None:
    fee_plan = _fee_plan_payload()
    fee_plan["account"] = "user2"

    with pytest.raises(Top1CapabilityReceiptError) as exc_info:
        refresh_top1_capability_receipt(
            tmp_path,
            gateway=FakeGateway(),
            market="HK",
            account="user1",
            opend_binding=BINDING,
            account_fee_plan_receipt=fee_plan,
            stock_owner="HK.00700",
            contract_symbol=CONTRACT,
            terms_expiration="2026-08-28",
            close_expiration="2026-08-14",
            observed_at_utc="2026-08-16T02:00:00Z",
        )

    assert exc_info.value.reason_code == "top1_capability_input_invalid"
