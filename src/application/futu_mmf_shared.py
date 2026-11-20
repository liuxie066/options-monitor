"""Read the account-scoped money-fund reconciliation file."""
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re


def mmf_shared_configured() -> bool:
    return bool(os.environ.get("FUTU_MMF_FUNDING_DIR", "").strip())


def mmf_shared_path(account: str) -> Path:
    directory = Path(os.environ.get("FUTU_MMF_FUNDING_DIR", "").strip())
    if not directory.is_absolute():
        raise ValueError("An absolute shared MMF funding directory is required")
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", account):
        raise ValueError("Invalid account for MMF funding evidence")
    return directory / f"{account}.json"


def read_mmf_shared(account: str) -> dict:
    with mmf_shared_path(account).open("rb") as handle:
        raw = handle.read(262145)
    if len(raw) > 262144:
        raise ValueError("Shared money-fund reconciliation file is too large")
    payload = json.loads(raw)
    if (
        not isinstance(payload, dict) or payload.get("schema") != "futu.mmf-funding.v1"
        or payload.get("account") != account
        or payload.get("status") not in {"none", "verified", "unverified"}
    ):
        raise ValueError("Invalid shared money-fund reconciliation object")
    if payload["status"] == "none" and (payload.get("transfers") != [] or payload.get("adjustments") != {}):
        raise ValueError("No-adjustment statement must have empty transfer rows and amounts")
    if payload["status"] == "verified":
        _verify_transfer_totals(payload)
    return payload


def _verify_transfer_totals(payload: dict) -> None:
    rows = payload.get("transfers")
    amounts = payload.get("adjustments")
    if not isinstance(rows, list) or not rows or not isinstance(amounts, dict):
        raise ValueError("shared reconciliation is missing its confirmed transfer rows")
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
                raise ValueError("Invalid shared reconciliation transfer identity")
            identities.add(receipt_id)
            amount, debit, credit = (Decimal(row[key]) for key in ("amount", "source_debit", "target_credit"))
            if not all(value.is_finite() for value in (amount, debit, credit)) or not 0 <= credit <= debit <= amount or amount <= 0:
                raise ValueError("Invalid shared reconciliation transfer amounts")
            if debit > credit:
                totals[currency] = totals.get(currency, Decimal(0)) + debit - credit
        if totals != {currency: Decimal(amount) for currency, amount in amounts.items()}:
            raise ValueError("shared reconciliation totals do not match its confirmed transfer rows")
    except (InvalidOperation, TypeError, KeyError) as exc:
        raise ValueError("Invalid shared reconciliation transfer row") from exc
