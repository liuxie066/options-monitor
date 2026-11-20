"""Read the private, account-scoped OSS money-fund reconciliation object."""
from decimal import Decimal, InvalidOperation
import json
import os
import re
import subprocess


def mmf_oss_configured() -> bool:
    return bool(os.environ.get("FUTU_MMF_FUNDING_OSS_PREFIX", "").strip())


def mmf_oss_uri(account: str) -> str:
    prefix = os.environ.get("FUTU_MMF_FUNDING_OSS_PREFIX", "").strip()
    if not re.fullmatch(r"oss://[a-z0-9-]+/[a-zA-Z0-9_/.-]+", prefix):
        raise ValueError("A private OSS bucket and object prefix are required")
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", account):
        raise ValueError("Invalid account for MMF funding evidence")
    return f"{prefix.rstrip('/')}/{account}.json"


def read_mmf_oss(account: str) -> dict:
    result = subprocess.run(
        ["ossutil", "cat", mmf_oss_uri(account), "--count", "262145"],
        capture_output=True, check=False, timeout=3,
    )
    if result.returncode or len(result.stdout) > 262144:
        raise ValueError("OSS money-fund reconciliation object is unavailable")
    payload = json.loads(result.stdout)
    if (
        not isinstance(payload, dict) or payload.get("schema") != "futu.mmf-funding.v1"
        or payload.get("account") != account
        or payload.get("status") not in {"none", "verified", "unverified"}
    ):
        raise ValueError("Invalid OSS money-fund reconciliation object")
    if payload["status"] == "verified":
        _verify_transfer_totals(payload)
    return payload


def _verify_transfer_totals(payload: dict) -> None:
    rows = payload.get("transfers")
    amounts = payload.get("adjustments")
    if not isinstance(rows, list) or not rows or not isinstance(amounts, dict):
        raise ValueError("OSS reconciliation is missing its confirmed transfer rows")
    totals: dict[str, Decimal] = {}
    identities: set[str] = set()
    try:
        for row in rows:
            receipt_id = row["receipt_id"]
            currency = row["currency"]
            if (
                not isinstance(receipt_id, str) or not receipt_id or receipt_id in identities
                or row["direction"] not in {"subscription", "redemption"}
                or currency not in {"CNY", "HKD", "USD"}
            ):
                raise ValueError("Invalid OSS reconciliation transfer identity")
            identities.add(receipt_id)
            amount, debit, credit = (Decimal(row[key]) for key in ("amount", "source_debit", "target_credit"))
            if not all(value.is_finite() for value in (amount, debit, credit)) or not 0 <= credit <= debit <= amount or amount <= 0:
                raise ValueError("Invalid OSS reconciliation transfer amounts")
            if debit > credit:
                totals[currency] = totals.get(currency, Decimal(0)) + debit - credit
        if totals != {currency: Decimal(amount) for currency, amount in amounts.items()}:
            raise ValueError("OSS reconciliation totals do not match its confirmed transfer rows")
    except (InvalidOperation, TypeError, KeyError) as exc:
        raise ValueError("Invalid OSS reconciliation transfer row") from exc
