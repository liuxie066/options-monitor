"""Preserve existing scan facts before filtering; never request additional market data."""

from __future__ import annotations

from pathlib import Path

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.symbol_identity import symbol_market
from src.application.payload_helpers import canonical_json_bytes_lines
from src.application.research.option_quote_capture import _json_safe
from src.application.source_receipts import publish_source_receipt, sha256_bytes
from src.infrastructure.io_utils import utc_now

SCHEMA = "research.scan-prescreen.v1"
POLICY = canonical_sha256({"schema": SCHEMA, "time": "capture_time_not_quote_source_time"})


def publish_scan_prescreen(*, producer_root: Path, producer_run_id, payload, fetch_plan):
    """Publish failed/empty/partial captures too, without granting quote authority."""
    if not producer_run_id or not isinstance(payload, dict):
        return None
    capture = (payload.get("meta") or {}).get("research_prescreen")
    if capture is None:
        return None
    body = {
        "schema_version": SCHEMA,
        "symbol": payload["symbol"],
        "underlier_code": payload["underlier_code"],
        "fetch_plan": fetch_plan,
        "capture": capture,
        "fetch_status": (payload.get("meta") or {}).get("status"),
    }
    raw = canonical_json_bytes_lines(_json_safe(body))
    digest = sha256_bytes(raw)
    prefix = "source_receipts/research_prescreen/" + canonical_sha256({"run_id": producer_run_id}) + "/" + digest
    completed = capture["completed_at_utc"]
    producer_root.mkdir(parents=True, exist_ok=True)
    return publish_source_receipt(
        producer_root=producer_root,
        receipt_relpath=prefix + "/receipt.json",
        payload_relpath=prefix + "/payload.json",
        payload_bytes=raw,
        source_kind="research_supplemental",
        producer_schema_version=SCHEMA,
        producer_run_id=producer_run_id,
        broker="futu",
        included_markets=[payload["underlier_code"].split(".")[0]],
        source_native_id="scan-prescreen:" + digest,
        source_observed_at=completed,
        completed_at=completed,
        producer_policy_hash=POLICY,
        private_files=True,
    )


def publish_watchlist_plan(*, producer_root: Path, producer_run_id, plan):
    """Freeze the owner's whole planned roster, including failed discovery symbols."""
    if not producer_run_id or not plan["symbols"]:
        return None
    schema = "research.watchlist-plan.v1"
    captured = utc_now()
    raw = canonical_json_bytes_lines(_json_safe({"schema_version": schema, "captured_at_utc": captured, "plan": plan}))
    digest = sha256_bytes(raw)
    prefix = "source_receipts/research_watchlist/" + canonical_sha256({"run_id": producer_run_id}) + "/" + digest
    markets = sorted({symbol_market(item["symbol"]) for item in plan["symbols"]})
    producer_root.mkdir(parents=True, exist_ok=True)
    return publish_source_receipt(
        producer_root=producer_root,
        receipt_relpath=prefix + "/receipt.json",
        payload_relpath=prefix + "/payload.json",
        payload_bytes=raw,
        source_kind="research_supplemental",
        producer_schema_version=schema,
        producer_run_id=producer_run_id,
        broker="futu",
        included_markets=markets,
        source_native_id="research-watchlist:" + digest,
        source_observed_at=captured,
        completed_at=captured,
        producer_policy_hash=POLICY,
        private_files=True,
    )


def start_capture(*, request, underlier_code, trading_date):
    return {
        "schema_version": SCHEMA,
        "underlier_code": underlier_code,
        "trading_date": trading_date,
        "started_at_utc": utc_now(),
        "completed_at_utc": None,
        "request_scope": {
            "option_types": request.option_types,
            "min_strike": request.min_strike,
            "max_strike": request.max_strike,
            "side_strike_windows": request.side_strike_windows,
            "min_dte": request.min_dte,
            "max_dte": request.max_dte,
            "explicit_expirations": request.explicit_expirations,
            "limit_expirations": request.limit_expirations,
            "query_filters": request.query_filters,
        },
        "chain": None,
        "snapshots": None,
        "missing_reasons": ["chain_not_attempted", "snapshots_not_attempted"],
    }
