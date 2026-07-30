from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.position_advice_authority import (
    normalize_account_label,
    normalize_portfolio_source,
    portfolio_account_identity_hash,
)
from src.application.position_advice_source_producers import (
    PORTFOLIO_SOURCE_SCHEMA,
)
from src.application.position_advice_source_receipts import (
    PositionAdviceSourceError,
    safe_existing_relative_path,
    sha256_bytes,
    validate_source_receipt,
)


class PositionAdviceAccountIdentityError(RuntimeError):
    """Raised when current-run portfolio identity cannot be proven."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def read_current_run_portfolio_identity(
    *,
    account_state_dir: Path,
    account_run_id: str,
    expected_account: str,
    expected_market: str,
    now: datetime,
) -> dict[str, Any]:
    """Read one immutable current-run portfolio receipt without history fallback."""

    root = _existing_real_directory(
        Path(account_state_dir),
        reason_code="current_run_portfolio_receipt_missing",
        label="account state directory",
    )
    run_id = _required_text(account_run_id, "account_run_id")
    account = normalize_account_label(expected_account)
    market = _market(expected_market)
    run_key = canonical_sha256({"producer_run_id": run_id})
    run_dir = (
        root
        / "position_advice_producers"
        / "portfolio"
        / run_key
    )
    receipt_path = _locate_unique_receipt(run_dir)

    try:
        receipt_path = safe_existing_relative_path(
            root,
            receipt_path.relative_to(root).as_posix(),
        )
        receipt_bytes = _stable_read(receipt_path, "portfolio receipt")
        receipt = _json_object(receipt_bytes, "portfolio receipt")
        validated = validate_source_receipt(
            receipt,
            producer_root=root,
            now=now,
            require_fresh=True,
            expected_source_kind="portfolio",
            expected_account=account,
            expected_producer_account_run_id=run_id,
        )
    except PositionAdviceSourceError as exc:
        reason = (
            "current_run_portfolio_receipt_stale"
            if "stale" in str(exc).lower()
            else "current_run_portfolio_receipt_invalid"
        )
        raise PositionAdviceAccountIdentityError(reason, str(exc)) from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PositionAdviceAccountIdentityError(
            "current_run_portfolio_receipt_invalid",
            str(exc),
        ) from exc

    content_dir = receipt_path.parent
    expected_run_dir = run_dir.resolve()
    if (
        content_dir.parent != expected_run_dir
        or receipt_path.name != "receipt.json"
        or receipt.get("producer_run_id") != run_id
        or receipt.get("producer_schema_version") != PORTFOLIO_SOURCE_SCHEMA
    ):
        raise PositionAdviceAccountIdentityError(
            "current_run_portfolio_receipt_invalid",
            "portfolio receipt does not belong to the expected account run",
        )

    payload_path = Path(validated["payload_path"])
    expected_payload_path = content_dir / "payload.json"
    expected_payload_relpath = expected_payload_path.relative_to(root).as_posix()
    if (
        payload_path != expected_payload_path
        or receipt.get("payload_relpath") != expected_payload_relpath
    ):
        raise PositionAdviceAccountIdentityError(
            "current_run_portfolio_receipt_invalid",
            "portfolio payload path does not match its receipt directory",
        )

    try:
        payload_bytes = _stable_read(payload_path, "portfolio payload")
        if sha256_bytes(payload_bytes) != validated["payload_sha256"]:
            raise ValueError("portfolio payload hash changed after validation")
        payload = _json_object(payload_bytes, "portfolio payload")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PositionAdviceAccountIdentityError(
            "current_run_portfolio_receipt_invalid",
            str(exc),
        ) from exc

    if (
        set(payload)
        != {
            "schema_version",
            "normalized_portfolio_source",
            "portfolio_context",
        }
        or payload.get("schema_version") != PORTFOLIO_SOURCE_SCHEMA
        or content_dir.name != canonical_sha256(payload)
    ):
        raise PositionAdviceAccountIdentityError(
            "current_run_portfolio_receipt_invalid",
            "portfolio payload schema or content directory hash is invalid",
        )

    context = payload.get("portfolio_context")
    if not isinstance(context, dict):
        raise PositionAdviceAccountIdentityError(
            "current_run_portfolio_receipt_invalid",
            "portfolio context must be an object",
        )
    identifiers = context.get("source_account_identifiers")
    if (
        not isinstance(identifiers, list)
        or not identifiers
        or any(not isinstance(item, str) or not item.strip() for item in identifiers)
    ):
        raise PositionAdviceAccountIdentityError(
            "current_run_portfolio_receipt_invalid",
            "portfolio source account identifiers are unavailable",
        )
    try:
        portfolio_source = normalize_portfolio_source(
            payload.get("normalized_portfolio_source")
        )
        computed_identity_hash = portfolio_account_identity_hash(
            normalized_portfolio_source=portfolio_source,
            broker_account_identifiers=identifiers,
        )
    except ValueError as exc:
        raise PositionAdviceAccountIdentityError(
            "current_run_portfolio_receipt_invalid",
            str(exc),
        ) from exc

    receipt_identity_hash = validated.get("portfolio_account_identity_hash")
    if computed_identity_hash != receipt_identity_hash:
        raise PositionAdviceAccountIdentityError(
            "current_run_portfolio_identity_mismatch",
            "portfolio payload identity does not match its receipt",
        )
    included_markets = sorted(
        {str(item or "").strip().upper() for item in receipt.get("included_markets", [])}
    )
    if market not in included_markets:
        raise PositionAdviceAccountIdentityError(
            "current_run_portfolio_receipt_invalid",
            "portfolio receipt does not include the expected market",
        )

    return {
        "status": "available",
        "account": account,
        "normalized_portfolio_source": portfolio_source,
        "portfolio_account_identity_hash": computed_identity_hash,
        "producer_account_run_id": validated["producer_account_run_id"],
        "included_markets": included_markets,
        "snapshot_id": validated["snapshot_id"],
        "receipt_hash": sha256_bytes(receipt_bytes),
        "payload_sha256": validated["payload_sha256"],
        "source_observed_at": validated["source_observed_at"],
        "expires_at": validated["expires_at"],
    }


def _locate_unique_receipt(run_dir: Path) -> Path:
    if not run_dir.exists():
        raise PositionAdviceAccountIdentityError(
            "current_run_portfolio_receipt_missing",
            "current-run portfolio receipt is missing",
        )
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise PositionAdviceAccountIdentityError(
            "current_run_portfolio_receipt_invalid",
            "current-run portfolio directory is invalid",
        )
    try:
        children = list(run_dir.iterdir())
    except OSError as exc:
        raise PositionAdviceAccountIdentityError(
            "current_run_portfolio_receipt_invalid",
            str(exc),
        ) from exc
    if any(child.is_symlink() for child in children):
        raise PositionAdviceAccountIdentityError(
            "current_run_portfolio_receipt_invalid",
            "current-run portfolio directory may not contain symlinks",
        )
    candidates = [
        child / "receipt.json"
        for child in children
        if child.is_dir() and (child / "receipt.json").exists()
    ]
    if not candidates:
        raise PositionAdviceAccountIdentityError(
            "current_run_portfolio_receipt_missing",
            "current-run portfolio receipt is missing",
        )
    if len(candidates) != 1:
        raise PositionAdviceAccountIdentityError(
            "current_run_portfolio_receipt_ambiguous",
            "multiple current-run portfolio receipts were found",
        )
    return candidates[0]


def _existing_real_directory(
    path: Path,
    *,
    reason_code: str,
    label: str,
) -> Path:
    if not path.exists():
        raise PositionAdviceAccountIdentityError(reason_code, f"{label} is missing")
    if path.is_symlink() or not path.is_dir():
        raise PositionAdviceAccountIdentityError(
            "current_run_portfolio_receipt_invalid",
            f"{label} is invalid",
        )
    return path.resolve()


def _stable_read(path: Path, label: str) -> bytes:
    first = path.read_bytes()
    second = path.read_bytes()
    if first != second:
        raise ValueError(f"{label} changed while it was being read")
    return first


def _json_object(payload: bytes, label: str) -> dict[str, Any]:
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def _market(value: Any) -> str:
    market = str(value or "").strip().upper()
    if market not in {"US", "HK"}:
        raise ValueError(f"unsupported market: {value}")
    return market


__all__ = [
    "PositionAdviceAccountIdentityError",
    "read_current_run_portfolio_identity",
]
