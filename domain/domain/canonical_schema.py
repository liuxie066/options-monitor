from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


CANONICAL_SCHEMA_VERSION_V1 = "3.0"

SCHEMA_KIND_PROCESSOR_OUTPUT = "processor_output"
SCHEMA_KIND_SOURCE_SNAPSHOT = "source_snapshot"

_ALLOWED_SOURCE_NAMES = {"opend", "holdings", "option_positions"}
_ALLOWED_SOURCE_STATUS = {"ok", "fallback", "error"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_canonical_payload(payload: dict[str, Any], *, kind: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("canonical payload must be a dict")
    if str(payload.get("schema_kind") or "") != str(kind):
        raise ValueError(f"schema_kind must be {kind}")
    if str(payload.get("schema_version") or "") != CANONICAL_SCHEMA_VERSION_V1:
        raise ValueError(f"unsupported canonical schema_version: {payload.get('schema_version')}")
    return payload


def normalize_processor_row(raw: dict[str, Any] | Any) -> dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    symbol = str(src.get("symbol") or "").strip().upper()
    strategy = str(src.get("strategy") or "").strip()
    note = str(src.get("note") or "")
    try:
        candidate_count = int(src.get("candidate_count") or 0)
    except Exception:
        candidate_count = 0

    if not symbol:
        raise ValueError("processor output requires non-empty symbol")
    if not strategy:
        raise ValueError("processor output requires strategy")

    out = {
        "schema_kind": SCHEMA_KIND_PROCESSOR_OUTPUT,
        "schema_version": CANONICAL_SCHEMA_VERSION_V1,
        "symbol": symbol,
        "strategy": strategy,
        "candidate_count": candidate_count,
        # Keep summary/report contract fields stable even when upstream rows are partial.
        "top_contract": src.get("top_contract", ""),
        "annualized_return": src.get("annualized_return", None),
        "net_income": src.get("net_income", None),
        "strike": src.get("strike", None),
        "dte": src.get("dte", None),
        "risk_label": src.get("risk_label", ""),
        "note": note,
        # Keep put/call alert fields stable through canonical processor row normalization.
        "delta": src.get("delta", None),
        "iv": src.get("iv", None),
        "mid": src.get("mid", None),
        "bid": src.get("bid", None),
        "ask": src.get("ask", None),
        "option_ccy": src.get("option_ccy", None),
        "cash_required_cny": src.get("cash_required_cny", None),
        "cash_required_usd": src.get("cash_required_usd", None),
        "cash_free_cny": src.get("cash_free_cny", None),
        "cash_free_total_cny": src.get("cash_free_total_cny", None),
        "cash_free_usd": src.get("cash_free_usd", None),
        "cash_free_usd_est": src.get("cash_free_usd_est", None),
        "cash_available_cny": src.get("cash_available_cny", None),
        "cash_available_total_cny": src.get("cash_available_total_cny", None),
        "cash_available_usd": src.get("cash_available_usd", None),
        "cash_available_usd_est": src.get("cash_available_usd_est", None),
        "cash_secured_used_usd": src.get("cash_secured_used_usd", None),
        "cash_secured_used_usd_symbol": src.get("cash_secured_used_usd_symbol", None),
        "cash_secured_used_cny": src.get("cash_secured_used_cny", None),
        "cash_secured_used_cny_total": src.get("cash_secured_used_cny_total", None),
        "cash_secured_used_cny_symbol": src.get("cash_secured_used_cny_symbol", None),
    }
    for key in ("market", "account", "source", "run_id"):
        if key in src:
            out[key] = src.get(key)
    return validate_canonical_payload(out, kind=SCHEMA_KIND_PROCESSOR_OUTPUT)


def normalize_processor_rows(raw_rows: Any) -> list[dict[str, Any]]:
    if raw_rows is None:
        return []
    if not isinstance(raw_rows, list):
        raise ValueError("processor output rows must be a list")
    return [normalize_processor_row(row) for row in raw_rows]


def normalize_source_snapshot(
    *,
    source_name: str,
    status: str,
    payload: dict[str, Any] | None,
    as_of_utc: str | None = None,
    fallback_used: bool = False,
    error_code: str | None = None,
    error_category: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    source_norm = str(source_name or "").strip().lower()
    if source_norm not in _ALLOWED_SOURCE_NAMES:
        raise ValueError(f"unsupported source_name: {source_name}")

    status_norm = str(status or "").strip().lower() or "error"
    if status_norm not in _ALLOWED_SOURCE_STATUS:
        raise ValueError(f"unsupported source status: {status}")

    out = {
        "schema_kind": SCHEMA_KIND_SOURCE_SNAPSHOT,
        "schema_version": CANONICAL_SCHEMA_VERSION_V1,
        "source_name": source_norm,
        "status": status_norm,
        "as_of_utc": str(as_of_utc or utc_now_iso()),
        "fallback_used": bool(fallback_used),
        "error_code": (str(error_code) if error_code else None),
        "error_category": (str(error_category) if error_category else None),
        "error_message": (str(error_message) if error_message else None),
        "payload": (payload if isinstance(payload, dict) else {}),
    }
    return validate_canonical_payload(out, kind=SCHEMA_KIND_SOURCE_SNAPSHOT)
