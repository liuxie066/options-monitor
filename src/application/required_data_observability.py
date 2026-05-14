from __future__ import annotations

from typing import Any


def extract_fetch_payload_metrics(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _empty_metrics()
    raw_meta = payload.get("meta")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    raw_rows = payload.get("rows")
    rows = raw_rows if isinstance(raw_rows, list) else []
    return {
        "rows": len(rows),
        "expiration_count": _int(payload.get("expiration_count")),
        "expiration_opend_calls": _int(meta.get("expiration_opend_calls")),
        "expiration_cache_hits": _int(meta.get("expiration_cache_hits")),
        "option_chain_opend_calls": _int(meta.get("opend_call_count")),
        "option_chain_rate_gate_wait_sec": round(_float(meta.get("rate_gate_wait_sec")), 6),
        "option_chain_cache_hits": len(meta.get("from_cache_expirations") or []),
        "option_chain_stale_cache_hits": len(meta.get("stale_cache_expirations") or []),
        "option_chain_fetched_expirations": len(meta.get("fetched_expirations") or []),
        "snapshot_requested_codes": _int(
            meta.get("snapshot_requested_codes")
            if meta.get("snapshot_requested_codes") is not None
            else meta.get("option_codes")
        ),
        "snapshot_opend_calls": _int(meta.get("snapshot_opend_call_count")),
        "spot_snapshot_opend_calls": _int(meta.get("spot_snapshot_opend_calls")),
        "market_snapshot_opend_calls": _int(meta.get("snapshot_opend_call_count"))
        + _int(meta.get("spot_snapshot_opend_calls")),
        "spot_snapshot_requested_codes": _int(meta.get("spot_snapshot_requested_codes")),
        "snapshot_rows": _int(meta.get("snapshots_rows")),
        "snapshot_fallback_filled": _int(meta.get("snapshot_fallback_filled")),
        "snapshot_fallback_failed": _int(meta.get("snapshot_fallback_failed")),
    }


def summarize_prefetch_fetch_metrics(audit_items: list[dict[str, Any]]) -> dict[str, Any]:
    out = _empty_metrics()
    symbols_with_payload = 0
    for item in audit_items:
        if not isinstance(item, dict):
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else None
        if payload is None:
            continue
        symbols_with_payload += 1
        metrics = extract_fetch_payload_metrics(payload)
        for key, value in metrics.items():
            out[key] = _float(out.get(key)) + _float(value)
    out["symbols_with_payload"] = symbols_with_payload
    out["option_chain_rate_gate_wait_sec"] = round(_float(out.get("option_chain_rate_gate_wait_sec")), 6)
    for key in (
        "rows",
        "expiration_count",
        "expiration_opend_calls",
        "expiration_cache_hits",
        "option_chain_opend_calls",
        "option_chain_cache_hits",
        "option_chain_stale_cache_hits",
        "option_chain_fetched_expirations",
        "snapshot_requested_codes",
        "snapshot_opend_calls",
        "spot_snapshot_opend_calls",
        "market_snapshot_opend_calls",
        "spot_snapshot_requested_codes",
        "snapshot_rows",
        "snapshot_fallback_filled",
        "snapshot_fallback_failed",
    ):
        out[key] = int(out.get(key) or 0)
    return out


def summarize_required_data_prefetch_run(
    *,
    symbols_total: int,
    unique_symbols_total: int,
    to_fetch: int,
    cached_unique_symbols: int,
    submitted_count: int,
    completed_count: int,
    skipped_count: int,
    failed_count: int,
    fetch_metrics: dict[str, Any],
    dedupe: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metrics = fetch_metrics if isinstance(fetch_metrics, dict) else _empty_metrics()
    option_chain_calls = _int(metrics.get("option_chain_opend_calls"))
    snapshot_calls = _int(
        metrics.get("market_snapshot_opend_calls")
        if metrics.get("market_snapshot_opend_calls") is not None
        else metrics.get("snapshot_opend_calls")
    )
    expiration_calls = _int(metrics.get("expiration_opend_calls"))
    total_opend_calls = option_chain_calls + snapshot_calls + expiration_calls
    option_chain_wait = round(_float(metrics.get("option_chain_rate_gate_wait_sec")), 6)
    dedupe_obj = dedupe if isinstance(dedupe, dict) else {}
    return {
        "symbols_total": int(symbols_total),
        "unique_symbols_total": int(unique_symbols_total),
        "deduped_count": _int(dedupe_obj.get("deduped_count")),
        "to_fetch": int(to_fetch),
        "cached_unique_symbols": int(cached_unique_symbols),
        "submitted_count": int(submitted_count),
        "completed_count": int(completed_count),
        "skipped_count": int(skipped_count),
        "failed_count": int(failed_count),
        "opend_calls": {
            "total": total_opend_calls,
            "option_expiration": expiration_calls,
            "option_chain": option_chain_calls,
            "market_snapshot": snapshot_calls,
        },
        "cache": {
            "option_expiration_hits": _int(metrics.get("expiration_cache_hits")),
            "option_chain_hits": _int(metrics.get("option_chain_cache_hits")),
            "option_chain_stale_hits": _int(metrics.get("option_chain_stale_cache_hits")),
            "option_chain_fetched_expirations": _int(metrics.get("option_chain_fetched_expirations")),
        },
        "rate_gate_wait_sec": {
            "option_chain": option_chain_wait,
        },
        "snapshot": {
            "requested_codes": _int(metrics.get("snapshot_requested_codes")),
            "spot_requested_codes": _int(metrics.get("spot_snapshot_requested_codes")),
            "option_snapshot_calls": _int(metrics.get("snapshot_opend_calls")),
            "spot_snapshot_calls": _int(metrics.get("spot_snapshot_opend_calls")),
            "rows": _int(metrics.get("snapshot_rows")),
            "fallback_filled": _int(metrics.get("snapshot_fallback_filled")),
            "fallback_failed": _int(metrics.get("snapshot_fallback_failed")),
        },
        "bottleneck": _infer_fetch_bottleneck(
            option_chain_wait_sec=option_chain_wait,
            option_chain_calls=option_chain_calls,
            snapshot_calls=snapshot_calls,
            expiration_calls=expiration_calls,
            failed_count=int(failed_count),
        ),
    }


def _empty_metrics() -> dict[str, Any]:
    return {
        "rows": 0,
        "expiration_count": 0,
        "expiration_opend_calls": 0,
        "expiration_cache_hits": 0,
        "option_chain_opend_calls": 0,
        "option_chain_rate_gate_wait_sec": 0.0,
        "option_chain_cache_hits": 0,
        "option_chain_stale_cache_hits": 0,
        "option_chain_fetched_expirations": 0,
        "snapshot_requested_codes": 0,
        "snapshot_opend_calls": 0,
        "spot_snapshot_opend_calls": 0,
        "market_snapshot_opend_calls": 0,
        "spot_snapshot_requested_codes": 0,
        "snapshot_rows": 0,
        "snapshot_fallback_filled": 0,
        "snapshot_fallback_failed": 0,
    }


def _infer_fetch_bottleneck(
    *,
    option_chain_wait_sec: float,
    option_chain_calls: int,
    snapshot_calls: int,
    expiration_calls: int,
    failed_count: int,
) -> str:
    if option_chain_wait_sec > 0:
        return "option_chain_rate_gate"
    if failed_count > 0:
        return "fetch_errors"
    if option_chain_calls >= max(snapshot_calls, expiration_calls, 1):
        return "option_chain_calls"
    if snapshot_calls >= max(option_chain_calls, expiration_calls, 1):
        return "market_snapshot_calls"
    if expiration_calls > 0:
        return "option_expiration_calls"
    return "none"


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0
