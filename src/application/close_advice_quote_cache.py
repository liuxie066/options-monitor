from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from domain.domain.symbol_identity import canonical_symbol, symbol_market
from src.infrastructure.io_utils import atomic_write_json


QUOTE_CACHE_METADATA_SCHEMA = "close_advice_quote_cache.v1"
DEFAULT_QUOTE_MAX_AGE_SEC = 900
_MARKET_TIMEZONES = {
    "US": ZoneInfo("America/New_York"),
    "HK": ZoneInfo("Asia/Hong_Kong"),
}


def quote_cache_metadata_path(csv_path: Path) -> Path:
    path = Path(csv_path)
    suffix = "_required_data.csv"
    if path.name.endswith(suffix):
        return path.with_name(
            path.name.removesuffix(suffix) + "_required_data.meta.json"
        )
    return path.with_suffix(path.suffix + ".meta.json")


def publish_quote_cache_metadata(
    *,
    csv_path: Path,
    symbol: str,
    source: str,
    source_run_id: str,
    observed_at: datetime | str | None = None,
) -> dict[str, Any]:
    path = Path(csv_path).resolve()
    if not path.exists() or not path.is_file():
        raise ValueError("required-data CSV is unavailable")
    symbol_norm = canonical_symbol(symbol)
    market = str(symbol_market(symbol_norm) or "").upper()
    source_norm = str(source or "").strip().lower()
    run_id = str(source_run_id or "").strip()
    if not symbol_norm or market not in _MARKET_TIMEZONES:
        raise ValueError("quote cache symbol or market is invalid")
    if source_norm != "opend":
        raise ValueError("quote cache source is not authoritative")
    if not run_id:
        raise ValueError("quote cache source_run_id is required")
    observed = _parse_observed_at(observed_at or datetime.now(timezone.utc))
    market_tz = _MARKET_TIMEZONES[market]
    payload = {
        "schema_version": QUOTE_CACHE_METADATA_SCHEMA,
        "symbol": symbol_norm,
        "market": market,
        "source": source_norm,
        "source_run_id": run_id,
        "source_observed_at": _iso_utc(observed),
        "market_session_date": observed.astimezone(market_tz).date().isoformat(),
        "csv_sha256": _sha256(path),
    }
    atomic_write_json(
        quote_cache_metadata_path(path),
        payload,
        sort_keys=True,
    )
    return payload


def validate_quote_cache_metadata(
    *,
    csv_path: Path,
    symbol: str,
    max_age_sec: int = DEFAULT_QUOTE_MAX_AGE_SEC,
    now: datetime | str | None = None,
) -> dict[str, Any]:
    path = Path(csv_path).resolve()
    metadata_path = quote_cache_metadata_path(path)
    base = {
        "ok": False,
        "csv_path": str(path),
        "metadata_path": str(metadata_path),
    }
    if not path.exists() or not path.is_file():
        return {**base, "reason": "quote_csv_missing"}
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception:
        return {**base, "reason": "quote_provenance_missing"}
    if not isinstance(payload, dict):
        return {**base, "reason": "quote_provenance_malformed"}
    if payload.get("schema_version") != QUOTE_CACHE_METADATA_SCHEMA:
        return {**base, "reason": "quote_provenance_schema_invalid"}
    symbol_norm = canonical_symbol(symbol)
    market = str(symbol_market(symbol_norm) or "").upper()
    if payload.get("symbol") != symbol_norm or payload.get("market") != market:
        return {**base, "reason": "quote_provenance_scope_mismatch"}
    source = str(payload.get("source") or "").strip().lower()
    if source != "opend":
        return {**base, "reason": "quote_provenance_source_invalid"}
    if not str(payload.get("source_run_id") or "").strip():
        return {**base, "reason": "quote_provenance_run_missing"}
    if str(payload.get("csv_sha256") or "") != _sha256(path):
        return {**base, "reason": "quote_provenance_bytes_mismatch"}
    try:
        observed = _parse_observed_at(payload.get("source_observed_at"))
        now_value = _parse_observed_at(now or datetime.now(timezone.utc))
    except ValueError:
        return {**base, "reason": "quote_provenance_time_invalid"}
    age_sec = (now_value - observed).total_seconds()
    if age_sec < -300:
        return {**base, "reason": "quote_observed_in_future"}
    if age_sec > max(0, int(max_age_sec)):
        return {**base, "reason": "quote_cache_stale", "age_sec": age_sec}
    market_tz = _MARKET_TIMEZONES.get(market)
    expected_session = (
        observed.astimezone(market_tz).date().isoformat()
        if market_tz is not None
        else None
    )
    if payload.get("market_session_date") != expected_session:
        return {**base, "reason": "quote_market_session_invalid"}
    if market_tz is not None and expected_session != now_value.astimezone(
        market_tz
    ).date().isoformat():
        return {**base, "reason": "quote_market_session_stale"}
    return {
        **base,
        "ok": True,
        "reason": None,
        "age_sec": max(0.0, age_sec),
        "source": source,
        "source_run_id": payload["source_run_id"],
        "source_observed_at": payload["source_observed_at"],
        "market": market,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_observed_at(value: datetime | str | Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip().replace("Z", "+00:00")
        if not text:
            raise ValueError("timestamp missing")
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp timezone missing")
    return parsed.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "DEFAULT_QUOTE_MAX_AGE_SEC",
    "QUOTE_CACHE_METADATA_SCHEMA",
    "publish_quote_cache_metadata",
    "quote_cache_metadata_path",
    "validate_quote_cache_metadata",
]
