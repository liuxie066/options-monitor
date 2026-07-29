from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from domain.domain.ledger.position_fields import normalize_account
from domain.domain.symbol_identity import symbol_market
from src.infrastructure.io_utils import atomic_write_json


CLOSE_ADVICE_REPORT_SCHEMA = "close_advice_report.v1"
MANIFEST_NAME = "close_advice.manifest.json"


def publish_close_advice_report_manifest(
    *,
    csv_path: Path,
    text_path: Path,
    context_path: Path,
    context: dict[str, Any],
    rows: list[dict[str, Any]],
    markets_to_run: list[str] | None,
    run_id: str | None = None,
    quote_mode: str | None = None,
    required_data_snapshot_manifest_sha256: str | None = None,
    close_advice_required_data_plan_sha256: str | None = None,
) -> dict[str, Any]:
    csv = Path(csv_path).resolve()
    text = Path(text_path).resolve()
    context_file = Path(context_path).resolve()
    if not csv.is_file() or not text.is_file() or not context_file.is_file():
        raise ValueError("close_advice report inputs are incomplete")
    markets = {
        str(symbol_market(row.get("symbol")) or "").upper()
        for row in rows
        if isinstance(row, dict)
    }
    markets.discard("")
    if markets_to_run:
        markets.update(
            str(item or "").strip().upper()
            for item in markets_to_run
            if str(item or "").strip().upper() in {"US", "HK"}
        )
    filters = context.get("filters") if isinstance(context.get("filters"), dict) else {}
    accounts = {
        normalize_account(row.get("account"))
        for row in rows
        if isinstance(row, dict) and normalize_account(row.get("account"))
    }
    filter_account = normalize_account(filters.get("account"))
    if filter_account:
        accounts.add(filter_account)
    generated_at = datetime.now(timezone.utc)
    identity = "|".join(
        (
            _sha256(csv),
            _sha256(text),
            _sha256(context_file),
            ",".join(sorted(markets)),
            ",".join(sorted(accounts)),
        )
    )
    payload = {
        "schema_version": CLOSE_ADVICE_REPORT_SCHEMA,
        "status": "success",
        "generation_id": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        "generated_at_utc": generated_at.isoformat().replace("+00:00", "Z"),
        "included_markets": sorted(markets),
        "accounts": sorted(accounts),
        "row_count": len(rows),
        "csv_sha256": _sha256(csv),
        "text_sha256": _sha256(text),
        "context_sha256": _sha256(context_file),
    }
    if str(run_id or "").strip():
        payload["run_id"] = str(run_id).strip()
    if str(quote_mode or "").strip():
        payload["quote_mode"] = str(quote_mode).strip()
    if str(required_data_snapshot_manifest_sha256 or "").strip():
        payload["required_data_snapshot_manifest_sha256"] = str(
            required_data_snapshot_manifest_sha256
        ).strip()
    if str(close_advice_required_data_plan_sha256 or "").strip():
        payload["close_advice_required_data_plan_sha256"] = str(
            close_advice_required_data_plan_sha256
        ).strip()
    atomic_write_json(csv.parent / MANIFEST_NAME, payload, sort_keys=True)
    return payload


def publish_close_advice_report_status(
    *,
    output_dir: Path,
    status: str,
    run_id: str | None = None,
    quote_mode: str | None = None,
    reason: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status_norm = str(status or "").strip().lower()
    if status_norm not in {"pending", "failed"}:
        raise ValueError("close-advice report status must be pending or failed")
    generated_at = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "schema_version": CLOSE_ADVICE_REPORT_SCHEMA,
        "status": status_norm,
        "generated_at_utc": generated_at.isoformat().replace("+00:00", "Z"),
    }
    if str(run_id or "").strip():
        payload["run_id"] = str(run_id).strip()
    if str(quote_mode or "").strip():
        payload["quote_mode"] = str(quote_mode).strip()
    if str(reason or "").strip():
        payload["reason"] = str(reason).strip()
    if evidence:
        payload["evidence"] = dict(evidence)
    target_dir = Path(output_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(target_dir / MANIFEST_NAME, payload, sort_keys=True)
    return payload


def validate_close_advice_report_manifest(
    *,
    csv_path: Path,
    desired_market: str | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    csv = Path(csv_path).resolve()
    manifest_path = csv.parent / MANIFEST_NAME
    base = {
        "ok": False,
        "manifest_path": str(manifest_path),
    }
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return {**base, "reason": "close_advice_manifest_missing"}
    if not isinstance(payload, dict):
        return {**base, "reason": "close_advice_manifest_malformed"}
    if payload.get("schema_version") != CLOSE_ADVICE_REPORT_SCHEMA:
        return {**base, "reason": "close_advice_manifest_schema_invalid"}
    status = str(payload.get("status") or "success").strip().lower()
    if status != "success":
        return {
            **base,
            "reason": "close_advice_manifest_not_success",
            "status": status,
        }
    if not csv.is_file() or payload.get("csv_sha256") != _sha256(csv):
        return {**base, "reason": "close_advice_report_bytes_mismatch"}
    markets = {
        str(item or "").strip().upper()
        for item in list(payload.get("included_markets") or [])
        if str(item or "").strip().upper() in {"US", "HK"}
    }
    market = str(desired_market or "").strip().upper()
    if market and market not in markets:
        return {**base, "reason": "close_advice_report_market_mismatch"}
    account_norm = normalize_account(account)
    accounts = {
        normalize_account(item)
        for item in list(payload.get("accounts") or [])
        if normalize_account(item)
    }
    if account_norm and account_norm not in accounts:
        return {**base, "reason": "close_advice_report_account_mismatch"}
    return {
        **base,
        "ok": True,
        "reason": None,
        "generation_id": payload.get("generation_id"),
        "generated_at_utc": payload.get("generated_at_utc"),
        "included_markets": sorted(markets),
        "accounts": sorted(accounts),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "CLOSE_ADVICE_REPORT_SCHEMA",
    "MANIFEST_NAME",
    "publish_close_advice_report_manifest",
    "publish_close_advice_report_status",
    "validate_close_advice_report_manifest",
]
