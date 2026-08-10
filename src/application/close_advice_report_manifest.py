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
    if not csv.is_file() or not text.is_file():
        raise ValueError("close_advice report inputs are incomplete")
    csv_bytes = csv.read_bytes()
    text_bytes = text.read_bytes()
    context_bytes = _canonical_context_bytes(context)
    del context_path
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
    csv_sha256 = _sha256_bytes(csv_bytes)
    text_sha256 = _sha256_bytes(text_bytes)
    context_sha256 = _sha256_bytes(context_bytes)
    identity = "|".join(
        (
            csv_sha256,
            text_sha256,
            context_sha256,
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
        "csv_sha256": csv_sha256,
        "text_sha256": text_sha256,
        "context_sha256": context_sha256,
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
    expected_run_id: str | None = None,
    expected_quote_mode: str | None = None,
) -> dict[str, Any]:
    snapshot = read_close_advice_report_snapshot(
        csv_path=csv_path,
        desired_market=desired_market,
        account=account,
        expected_run_id=expected_run_id,
        expected_quote_mode=expected_quote_mode,
    )
    return dict(snapshot["validation"])


def read_close_advice_report_snapshot(
    *,
    csv_path: Path,
    desired_market: str | None = None,
    account: str | None = None,
    expected_run_id: str | None = None,
    expected_quote_mode: str | None = None,
) -> dict[str, Any]:
    """Read and validate the exact report bytes a caller may consume.

    Data files are read before the commit marker.  A concurrent publisher
    therefore either leaves this snapshot bound to one successful manifest or
    makes validation fail closed; callers never need to reopen validated files.
    """

    csv = Path(csv_path).resolve()
    text = csv.parent / "close_advice.txt"
    manifest_path = csv.parent / MANIFEST_NAME
    base = {
        "ok": False,
        "manifest_path": str(manifest_path),
    }
    csv_bytes = _read_bytes(csv)
    text_bytes = _read_bytes(text)
    try:
        manifest_bytes = manifest_path.read_bytes()
        payload = json.loads(manifest_bytes)
    except Exception:
        return _snapshot_result(
            {**base, "reason": "close_advice_manifest_missing"}
        )
    if not isinstance(payload, dict):
        return _snapshot_result(
            {**base, "reason": "close_advice_manifest_malformed"}
        )
    if payload.get("schema_version") != CLOSE_ADVICE_REPORT_SCHEMA:
        return _snapshot_result(
            {**base, "reason": "close_advice_manifest_schema_invalid"}
        )
    status = str(payload.get("status") or "").strip().lower()
    if status != "success":
        return _snapshot_result(
            {
                **base,
                "reason": "close_advice_manifest_not_success",
                "status": status,
            }
        )
    if csv_bytes is None or payload.get("csv_sha256") != _sha256_bytes(
        csv_bytes
    ):
        return _snapshot_result(
            {**base, "reason": "close_advice_report_bytes_mismatch"}
        )
    if text_bytes is None or payload.get("text_sha256") != _sha256_bytes(
        text_bytes
    ):
        return _snapshot_result(
            {**base, "reason": "close_advice_text_bytes_mismatch"}
        )
    run_id = str(payload.get("run_id") or "").strip()
    expected_run = str(expected_run_id or "").strip()
    if expected_run and run_id != expected_run:
        return _snapshot_result(
            {**base, "reason": "close_advice_report_run_mismatch"}
        )
    quote_mode = str(payload.get("quote_mode") or "").strip().lower()
    expected_mode = str(expected_quote_mode or "").strip().lower()
    if expected_mode and quote_mode != expected_mode:
        return _snapshot_result(
            {**base, "reason": "close_advice_report_quote_mode_mismatch"}
        )
    markets = {
        str(item or "").strip().upper()
        for item in list(payload.get("included_markets") or [])
        if str(item or "").strip().upper() in {"US", "HK"}
    }
    market = str(desired_market or "").strip().upper()
    if market and market not in markets:
        return _snapshot_result(
            {**base, "reason": "close_advice_report_market_mismatch"}
        )
    account_norm = normalize_account(account)
    accounts = {
        normalize_account(item)
        for item in list(payload.get("accounts") or [])
        if normalize_account(item)
    }
    if account_norm and account_norm not in accounts:
        return _snapshot_result(
            {**base, "reason": "close_advice_report_account_mismatch"}
        )
    return _snapshot_result(
        {
            **base,
            "ok": True,
            "reason": None,
            "generation_id": payload.get("generation_id"),
            "generated_at_utc": payload.get("generated_at_utc"),
            "run_id": run_id or None,
            "quote_mode": quote_mode or None,
            "included_markets": sorted(markets),
            "accounts": sorted(accounts),
            "row_count": payload.get("row_count"),
            "context_sha256": str(
                payload.get("context_sha256") or ""
            ).strip().lower()
            or None,
            "required_data_snapshot_manifest_sha256": str(
                payload.get("required_data_snapshot_manifest_sha256") or ""
            ).strip().lower()
            or None,
            "close_advice_required_data_plan_sha256": str(
                payload.get("close_advice_required_data_plan_sha256") or ""
            ).strip().lower()
            or None,
        },
        csv_bytes=csv_bytes,
        text_bytes=text_bytes,
    )


def _snapshot_result(
    validation: dict[str, Any],
    *,
    csv_bytes: bytes | None = None,
    text_bytes: bytes | None = None,
) -> dict[str, Any]:
    return {
        "validation": validation,
        "csv_bytes": csv_bytes if validation.get("ok") else None,
        "text_bytes": text_bytes if validation.get("ok") else None,
    }


def _read_bytes(path: Path) -> bytes | None:
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_context_bytes(context: dict[str, Any]) -> bytes:
    return json.dumps(
        context,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


__all__ = [
    "CLOSE_ADVICE_REPORT_SCHEMA",
    "MANIFEST_NAME",
    "publish_close_advice_report_manifest",
    "publish_close_advice_report_status",
    "read_close_advice_report_snapshot",
    "validate_close_advice_report_manifest",
]
