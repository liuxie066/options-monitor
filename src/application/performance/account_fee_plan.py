from __future__ import annotations

import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import NoReturn

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.account_config import normalize_account_label
from src.application.candidate_snapshot_contract import (
    CandidateSnapshotContractError,
    utc_timestamp,
)
from src.infrastructure.private_storage import open_private_text, private_path


ACCOUNT_FEE_PLAN_RECEIPT_SCHEMA = "strategy_lab_account_fee_plan_receipt"
MAX_ACCOUNT_FEE_PLAN_RECEIPT_BYTES = 8 * 1024

_FIELDS = frozenset(
    {
        "schema_version",
        "market",
        "account",
        "commission_free",
        "platform_fee",
        "fee_plan_ref",
        "observed_at_utc",
        "evidence_ref",
        "evidence_sha256",
    }
)
_RECORDED_FIELDS = _FIELDS | {"source_receipt_sha256"}
_HASH = frozenset("0123456789abcdef")


class AccountFeePlanReceiptError(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _fail(message: str) -> NoReturn:
    raise AccountFeePlanReceiptError("account_fee_plan_receipt_invalid", message)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _fail(f"{label} must be canonical text")
    return value


def _sha256(value: object, label: str) -> str:
    text = _text(value, label)
    if len(text) != 64 or set(text) - _HASH:
        _fail(f"{label} must be a lowercase SHA-256")
    return text


def normalize_account_fee_plan_receipt(
    value: object,
    *,
    recorded: bool = False,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _fail("account fee-plan receipt must be an object")
    item = dict(value)
    expected = _RECORDED_FIELDS if recorded else _FIELDS
    if set(item) != expected or item.get("schema_version") != ACCOUNT_FEE_PLAN_RECEIPT_SCHEMA:
        _fail("account fee-plan receipt schema is invalid")
    try:
        account = normalize_account_label(item.get("account"))
    except ValueError as exc:
        raise AccountFeePlanReceiptError(
            "account_fee_plan_receipt_invalid",
            "account fee-plan identity must use HK and a canonical account",
        ) from exc
    if item.get("market") != "HK" or item.get("account") != account:
        _fail("account fee-plan identity must use HK and a canonical account")
    if type(item.get("commission_free")) is not bool:
        _fail("commission_free must be boolean")
    platform_fee = item.get("platform_fee")
    if (
        isinstance(platform_fee, bool)
        or not isinstance(platform_fee, (int, float))
        or not math.isfinite(float(platform_fee))
        or float(platform_fee) < 0
    ):
        _fail("platform_fee must be non-negative")
    try:
        observed_at_utc = utc_timestamp(item.get("observed_at_utc"), "observed_at_utc")
    except CandidateSnapshotContractError as exc:
        raise AccountFeePlanReceiptError("account_fee_plan_receipt_invalid", str(exc)) from exc
    if item.get("observed_at_utc") != observed_at_utc:
        _fail("observed_at_utc must be canonical UTC")
    normalized: dict[str, object] = {
        "schema_version": ACCOUNT_FEE_PLAN_RECEIPT_SCHEMA,
        "market": "HK",
        "account": account,
        "commission_free": item["commission_free"],
        "platform_fee": float(platform_fee),
        "fee_plan_ref": _text(item.get("fee_plan_ref"), "fee_plan_ref"),
        "observed_at_utc": observed_at_utc,
        "evidence_ref": _text(item.get("evidence_ref"), "evidence_ref"),
        "evidence_sha256": _sha256(item.get("evidence_sha256"), "evidence_sha256"),
    }
    source_hash = canonical_sha256(normalized)
    if recorded and _sha256(item.get("source_receipt_sha256"), "source_receipt_sha256") != source_hash:
        _fail("account fee-plan receipt hash changed")
    return {**normalized, "source_receipt_sha256": source_hash}


def load_account_fee_plan_receipt(path: str | Path) -> dict[str, object]:
    try:
        with open_private_text(private_path(path)) as handle:
            content = handle.read(MAX_ACCOUNT_FEE_PLAN_RECEIPT_BYTES + 1)
        if len(content.encode("utf-8")) > MAX_ACCOUNT_FEE_PLAN_RECEIPT_BYTES:
            raise ValueError("account fee-plan receipt is too large")
        payload = json.loads(content)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise AccountFeePlanReceiptError(
            "account_fee_plan_receipt_unavailable",
            "account fee-plan receipt cannot be read",
        ) from exc
    return normalize_account_fee_plan_receipt(payload)


__all__ = [
    "ACCOUNT_FEE_PLAN_RECEIPT_SCHEMA",
    "MAX_ACCOUNT_FEE_PLAN_RECEIPT_BYTES",
    "AccountFeePlanReceiptError",
    "load_account_fee_plan_receipt",
    "normalize_account_fee_plan_receipt",
]
