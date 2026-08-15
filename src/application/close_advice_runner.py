from __future__ import annotations

import csv
from collections import OrderedDict
from collections.abc import Mapping
from io import BytesIO
import json
import math
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, TypedDict
from uuid import uuid4

import pandas as pd

from domain.domain.expiration_dates import (
    expiration_business_today,
    expiration_timestamp_to_date,
)
from domain.domain.fetch_source import is_futu_fetch_source
from domain.domain.close_advice import (
    CloseAdviceInput,
    DECISION_EVIDENCE_NOT_EVALUABLE,
    RECOMMENDATION_CLOSE,
    RECOMMENDATION_NOT_EVALUABLE,
    STRICT_CLOSE_POLICY_VERSION,
    evaluate_close_advice,
    safe_float,
    safe_int,
    select_close_advice_notification_rows,
    sort_advice_rows,
)
from domain.domain.fee_calc import (
    FUTU_HK_OPTION_FEE_BASIS,
    FUTU_US_OPTION_FEE_BASIS,
    calc_futu_option_fee,
)
from src.infrastructure.io_utils import atomic_write_text, read_json, safe_read_csv
from domain.domain.ledger.position_fields import (
    effective_expiration_ymd,
    effective_multiplier,
    normalize_account,
)
from domain.domain.option_position_identity import normalize_broker, normalize_currency
from src.application.opend_utils import normalize_underlier
from domain.domain.trade_contract_identity import (
    canonical_contract_symbol,
    contract_key,
    normalize_contract_expiration,
    normalize_contract_option_type,
)
from domain.domain.symbol_identity import symbol_market
from src.application.expiration_normalization import find_unique_near_miss_expiration
from src.application.close_advice_quote_cache import (
    DEFAULT_QUOTE_MAX_AGE_SEC,
    validate_quote_cache_metadata,
)
from src.application.close_advice_report_manifest import (
    publish_close_advice_report_manifest,
    publish_close_advice_report_status,
)
from src.application.close_advice_required_data import (
    CloseAdviceRequiredDataPlanError,
    account_requirement_index,
    resolve_bound_close_advice_required_data_plan_snapshot,
)
from src.application.source_receipts import sha256_bytes
from src.application.required_data_snapshot import (
    FrozenRequiredDataUnavailable,
    RequiredDataSnapshotError,
    load_required_data_snapshot_manifest_snapshot,
    resolve_frozen_required_data_csv_bytes,
)
from src.application.opend_fetch_config import opend_fetch_kwargs
from src.application.symbol_aliases import load_runtime_symbol_aliases
from src.infrastructure.opend_retcodes import classify_opend_error
OUTPUT_COLUMNS = [
    "account",
    "position_lot_id",
    "quote_mode",
    "required_data_snapshot_plan_id",
    "required_data_snapshot_manifest_sha256",
    "close_advice_required_data_plan_sha256",
    "required_data_requirement_id",
    "required_data_binding_id",
    "required_data_snapshot_id",
    "required_data_receipt_hash",
    "required_data_payload_sha256",
    "required_data_source_observed_at",
    "required_data_expires_at",
    "symbol",
    "option_type",
    "expiration",
    "strike",
    "contracts_open",
    "multiplier",
    "spot",
    "currency",
    "premium",
    "bid",
    "ask",
    "close_mid",
    "dte",
    "original_dte",
    "remaining_term_ratio",
    "spread_ratio",
    "position_lifecycle_state",
    "net_capture_ratio",
    "opening_gross_credit",
    "estimated_open_fee",
    "opening_net_credit",
    "all_in_close_cost",
    "close_cost_ratio",
    "is_otm",
    "estimated_close_fee",
    "fee_calc_status",
    "fee_calc_basis",
    "estimated_pnl_if_close_net",
    "evaluation_status",
    "quote_status",
    "reason",
    "policy_version",
    "recommendation_state",
    "decision_basis",
    "decision_evidence_status",
    "broker",
    "position_side",
    "strategy_family",
    "strategy_profile",
    "data_quality_flags",
]

QUOTE_ISSUE_FLAGS = {
    "missing_quote",
    "missing_bid",
    "missing_ask",
    "missing_bid_ask",
    "invalid_bid",
    "invalid_ask",
    "invalid_bid_ask",
    "required_data_missing_expiration",
    "required_data_missing_contract",
    "required_data_fetch_error",
    "required_data_fetch_error_rate_limit",
    "required_data_fetch_skipped_non_futu_source",
    "close_advice_plan_unavailable",
    "required_data_position_not_planned",
    "required_data_symbol_config_missing",
    "required_data_symbol_source_unsupported",
    "required_data_route_conflict",
    "required_data_symbol_not_planned",
    "required_data_snapshot_unavailable",
    "opend_fetch_error",
    "opend_fetch_no_usable_quote",
    "spread_too_wide",
    "invalid_spread",
}


class _PositionFetchSpec(TypedDict):
    symbol: str
    requested_keys: set[tuple[str, str, str, str]]
    requested_expirations: set[str]
    option_types: set[str]
    strikes: list[float]


class _OpenDFetchKwargs(TypedDict):
    max_wait_sec: float
    option_chain_window_sec: float
    option_chain_max_calls: int
    snapshot_max_wait_sec: float
    snapshot_window_sec: float
    snapshot_max_calls: int
    expiration_max_wait_sec: float
    expiration_window_sec: float
    expiration_max_calls: int

def _norm_symbol(value: Any, *, base_dir: Path | None = None) -> str:
    aliases = load_runtime_symbol_aliases(base_dir) if base_dir is not None else None
    return canonical_contract_symbol(value, symbol_aliases=aliases)


def _norm_option_type(value: Any) -> str:
    return normalize_contract_option_type(value, fallback_raw=True)


def _market_for_symbol(symbol: Any) -> str:
    return symbol_market(symbol) or ""


def normalize_expiration(value: Any) -> str | None:
    return normalize_contract_expiration(value, fallback_raw=True)


def _row_account(value: Any, *, default: str = "当前账户") -> str:
    return normalize_account(value) or default


def _quote_key(symbol: Any, option_type: Any, expiration: Any, strike: Any, *, base_dir: Path | None = None) -> tuple[str, str, str, str]:
    aliases = load_runtime_symbol_aliases(base_dir) if base_dir is not None else None
    return contract_key(
        symbol,
        option_type,
        expiration,
        strike,
        symbol_aliases=aliases,
        option_type_fallback_raw=True,
        expiration_fallback_raw=True,
    )


def load_required_data_quotes(
    required_data_root: Path,
    symbols: set[str] | None = None,
    *,
    base_dir: Path | None = None,
) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    root = Path(required_data_root)
    parsed = root / "parsed"
    quotes: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    if not parsed.exists():
        return quotes

    for path in sorted(parsed.glob("*_required_data.csv")):
        sym_from_name = path.name.removesuffix("_required_data.csv").upper()
        if symbols and sym_from_name not in symbols:
            continue
        df = safe_read_csv(path)
        if df.empty:
            continue
        for row in df.to_dict("records"):
            key = _quote_key(
                row.get("symbol") or sym_from_name,
                row.get("option_type"),
                row.get("expiration"),
                row.get("strike"),
                base_dir=base_dir,
            )
            if not all(key):
                continue
            quotes[key] = row
    return quotes


def load_required_data_coverage(
    required_data_root: Path,
    symbols: set[str] | None = None,
    *,
    base_dir: Path | None = None,
) -> tuple[set[tuple[str, str, str, str]], dict[str, set[str]]]:
    root = Path(required_data_root)
    parsed = root / "parsed"
    covered_keys: set[tuple[str, str, str, str]] = set()
    expirations_by_symbol: dict[str, set[str]] = {}
    if not parsed.exists():
        return covered_keys, expirations_by_symbol

    for path in sorted(parsed.glob("*_required_data.csv")):
        sym_from_name = path.name.removesuffix("_required_data.csv").upper()
        if symbols and sym_from_name not in symbols:
            continue
        df = safe_read_csv(path)
        if df.empty:
            continue
        for row in df.to_dict("records"):
            key = _quote_key(
                row.get("symbol") or sym_from_name,
                row.get("option_type"),
                row.get("expiration"),
                row.get("strike"),
                base_dir=base_dir,
            )
            if not all(key):
                continue
            covered_keys.add(key)
            expirations_by_symbol.setdefault(key[0], set()).add(key[2])
    return covered_keys, expirations_by_symbol


def _build_contract_expiration_index(
    covered_keys: set[tuple[str, str, str, str]],
) -> dict[tuple[str, str, str], set[str]]:
    index: dict[tuple[str, str, str], set[str]] = {}
    for symbol, option_type, expiration, strike in covered_keys:
        if not (symbol and option_type and expiration and strike):
            continue
        index.setdefault((symbol, option_type, strike), set()).add(expiration)
    return index


def _symbol_config_by_symbol(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    items = config.get("symbols") if isinstance(config, dict) else []
    out: dict[str, dict[str, Any]] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        sym = _norm_symbol(item.get("symbol"))
        if sym:
            out[sym] = item
    return out


def _merge_quote_rows(quotes: dict[tuple[str, str, str, str], dict[str, Any]], rows: list[dict[str, Any]]) -> None:
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        key = _quote_key(row.get("symbol"), row.get("option_type"), row.get("expiration"), row.get("strike"))
        if all(key):
            quotes[key] = row


def _quote_number(value: Any) -> float | None:
    num = safe_float(value)
    if num is None:
        return None
    if isinstance(num, float) and math.isnan(num):
        return None
    return num


def _quote_has_usable_price(quote: dict[str, Any] | None) -> bool:
    if not isinstance(quote, dict):
        return False
    bid = _quote_number(quote.get("bid"))
    ask = _quote_number(quote.get("ask"))
    return (
        bid is not None
        and ask is not None
        and bid >= 0
        and ask > 0
        and ask >= bid
    )


def _fetch_payload_error_reason(payload: dict[str, Any] | None, *, prefix: str) -> str | None:
    if not isinstance(payload, dict):
        return None
    meta = payload.get("meta")
    meta = meta if isinstance(meta, dict) else {}
    status = str(meta.get("status") or "").strip().lower()
    error_code = str(meta.get("error_code") or "").strip().upper()
    error_text = " ".join(
        str(x)
        for x in (
            meta.get("error"),
            meta.get("message"),
            json.dumps(meta.get("errors"), ensure_ascii=False, default=str) if meta.get("errors") else "",
        )
        if str(x).strip()
    )
    is_rate_limited = classify_opend_error({"error_code": error_code, "message": error_text}).is_rate_limit
    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    if status == "error" or (status == "partial" and error_code) or (error_code and not rows):
        if is_rate_limited:
            return f"{prefix}_rate_limit"
        return prefix
    return None


def _build_position_fetch_specs(
    positions: list[dict[str, Any]],
    *,
    base_dir: Path,
    config: dict[str, Any] | None = None,
) -> dict[str, _PositionFetchSpec]:
    specs: dict[str, _PositionFetchSpec] = {}
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        key = _quote_key(pos.get("symbol"), pos.get("option_type"), _position_expiration(pos), pos.get("strike"), base_dir=base_dir)
        if not all(key):
            continue
        sym = key[0]
        item = specs.get(sym)
        if item is None:
            new_item: _PositionFetchSpec = {
                "symbol": sym,
                "requested_keys": set[tuple[str, str, str, str]](),
                "requested_expirations": set[str](),
                "option_types": set[str](),
                "strikes": list[float](),
            }
            specs[sym] = new_item
            item = new_item
        item["requested_keys"].add(key)
        item["requested_expirations"].add(key[2])
        item["option_types"].add(key[1])
        strike_num = safe_float(pos.get("strike"))
        if strike_num is not None:
            item["strikes"].append(strike_num)
    return specs


def _typed_opend_fetch_kwargs(config: dict[str, Any]) -> _OpenDFetchKwargs:
    raw = opend_fetch_kwargs(config)
    return {
        "max_wait_sec": float(raw["max_wait_sec"]),
        "option_chain_window_sec": float(raw["option_chain_window_sec"]),
        "option_chain_max_calls": int(raw["option_chain_max_calls"]),
        "snapshot_max_wait_sec": float(raw["snapshot_max_wait_sec"]),
        "snapshot_window_sec": float(raw["snapshot_window_sec"]),
        "snapshot_max_calls": int(raw["snapshot_max_calls"]),
        "expiration_max_wait_sec": float(raw["expiration_max_wait_sec"]),
        "expiration_window_sec": float(raw["expiration_window_sec"]),
        "expiration_max_calls": int(raw["expiration_max_calls"]),
    }


def _load_required_data_rows(required_data_root: Path, symbol: str) -> list[dict[str, Any]]:
    path = Path(required_data_root) / "parsed" / f"{symbol}_required_data.csv"
    df = safe_read_csv(path)
    return df.to_dict(orient="records") if not df.empty else []


def _merge_required_data_rows(existing_rows: list[dict[str, Any]], new_rows: list[dict[str, Any]], *, base_dir: Path) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    order: list[tuple[str, str, str, str]] = []
    for source_rows in (existing_rows or [], new_rows or []):
        for row in source_rows:
            if not isinstance(row, dict):
                continue
            key = _quote_key(row.get("symbol"), row.get("option_type"), row.get("expiration"), row.get("strike"), base_dir=base_dir)
            if not all(key):
                continue
            if key not in merged:
                order.append(key)
            merged[key] = row
    return [merged[key] for key in order]


def _ensure_required_data_coverage_for_positions(
    *,
    config: dict[str, Any],
    positions: list[dict[str, Any]],
    required_data_root: Path,
    base_dir: Path,
    gateway: Any = None,
) -> tuple[dict[tuple[str, str, str, str], str], dict[tuple[str, str, str, str], dict[str, Any]], dict[str, Any]]:
    symbol_cfgs = _symbol_config_by_symbol(config)
    specs = _build_position_fetch_specs(positions, base_dir=base_dir, config=config)
    fetch_reasons: dict[tuple[str, str, str, str], str] = {}
    fetch_details: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    summary = {"attempted_symbols": 0, "fetched_symbols": 0, "errors": 0}
    advice_cfg = config.get("close_advice") if isinstance(config, dict) else {}
    if isinstance(advice_cfg, dict) and str(advice_cfg.get("quote_source") or "auto").strip().lower() == "required_data":
        return fetch_reasons, fetch_details, summary
    if not specs:
        return fetch_reasons, fetch_details, summary

    current_covered, current_expirations = load_required_data_coverage(required_data_root, symbols=set(specs), base_dir=base_dir)
    current_contract_expiration_index = _build_contract_expiration_index(current_covered)

    try:
        from src.application.opend_symbol_fetching import fetch_symbol
        from src.application.opend_symbol_outputs import save_outputs
    except Exception:
        return fetch_reasons, fetch_details, summary

    external_gateway = gateway is not None
    shared_gateways: dict[tuple[str, int], Any] = {}

    try:
        for symbol, spec in specs.items():
            requested_keys = set(spec["requested_keys"])
            requested_expirations = sorted(spec["requested_expirations"])
            missing_keys = [key for key in requested_keys if key not in current_covered]
            if not missing_keys:
                continue
            summary["attempted_symbols"] += 1
            symbol_cfg = symbol_cfgs.get(symbol) or {}
            fetch_cfg = symbol_cfg.get("fetch") if isinstance(symbol_cfg, dict) else {}
            fetch_cfg = fetch_cfg if isinstance(fetch_cfg, dict) else {}
            can_refresh = is_futu_fetch_source(fetch_cfg.get("source"))
            if not can_refresh:
                for key in missing_keys:
                    near_miss = find_unique_near_miss_expiration(
                        key[2],
                        current_contract_expiration_index.get((key[0], key[1], key[3])) or set(),
                    )
                    fetch_reasons[key] = "required_data_fetch_skipped_non_futu_source"
                    fetch_details[key] = {
                        "quote_key": "|".join(key),
                        "requested_expirations": requested_expirations,
                        "available_expirations": sorted(current_expirations.get(symbol) or set()),
                    }
                    if near_miss:
                        fetch_details[key]["expiration_near_miss"] = {
                            "requested_expiration": key[2],
                            "matched_expiration": near_miss,
                        }
                continue
            strikes = [safe_float(v) for v in spec["strikes"]]
            strikes = [v for v in strikes if v is not None]
            host = str(fetch_cfg.get("host") or "127.0.0.1")
            port = safe_int(fetch_cfg.get("port")) or 11111
            try:
                payload = fetch_symbol(
                    symbol,
                    limit_expirations=safe_int(fetch_cfg.get("limit_expirations")) or max(len(requested_expirations), 8),
                    host=host,
                    port=port,
                    base_dir=base_dir,
                    option_types=",".join(sorted(spec["option_types"] or {"put", "call"})),
                    min_strike=min(strikes) if strikes else None,
                    max_strike=max(strikes) if strikes else None,
                    explicit_expirations=requested_expirations,
                    chain_cache=True,
                    chain_cache_force_refresh=False,
                    freshness_policy="refresh_missing",
                    gateway=gateway,
                    include_realized_volatility=False,
                    **_typed_opend_fetch_kwargs(config),
                )
            except Exception as exc:
                summary["errors"] += 1
                err_text = str(exc or "")
                reason = "required_data_fetch_error_rate_limit" if classify_opend_error({"error_code": err_text.lower(), "message": err_text}).is_rate_limit else "required_data_fetch_error"
                for key in missing_keys:
                    near_miss = find_unique_near_miss_expiration(
                        key[2],
                        current_contract_expiration_index.get((key[0], key[1], key[3])) or set(),
                    )
                    fetch_reasons[key] = reason
                    fetch_details[key] = {
                        "quote_key": "|".join(key),
                        "requested_expirations": requested_expirations,
                        "available_expirations": sorted(current_expirations.get(symbol) or set()),
                        "message": str(exc),
                    }
                    if near_miss:
                        fetch_details[key]["expiration_near_miss"] = {
                            "requested_expiration": key[2],
                            "matched_expiration": near_miss,
                        }
                continue
            payload_reason = _fetch_payload_error_reason(payload, prefix="required_data_fetch_error")
            if payload_reason and not list(payload.get("rows") or []):
                summary["errors"] += 1
                for key in missing_keys:
                    near_miss = find_unique_near_miss_expiration(
                        key[2],
                        current_contract_expiration_index.get((key[0], key[1], key[3])) or set(),
                    )
                    fetch_reasons[key] = payload_reason
                    fetch_details[key] = {
                        "quote_key": "|".join(key),
                        "requested_expirations": requested_expirations,
                        "available_expirations": sorted(current_expirations.get(symbol) or set()),
                        "message": str(((payload.get("meta") or {}) if isinstance(payload.get("meta"), dict) else {}).get("error") or payload_reason),
                    }
                    if near_miss:
                        fetch_details[key]["expiration_near_miss"] = {
                            "requested_expiration": key[2],
                            "matched_expiration": near_miss,
                        }
                try:
                    save_outputs(base_dir, symbol, payload, output_root=required_data_root)
                except Exception:
                    pass
                continue
            merged_rows = _merge_required_data_rows(
                _load_required_data_rows(required_data_root, symbol),
                list(payload.get("rows") or []),
                base_dir=base_dir,
            )
            payload = dict(payload)
            payload["rows"] = merged_rows
            save_outputs(base_dir, symbol, payload, output_root=required_data_root)
            summary["fetched_symbols"] += 1
            current_covered, current_expirations = load_required_data_coverage(required_data_root, symbols=set(specs), base_dir=base_dir)
            if payload_reason:
                still_missing = [key for key in requested_keys if key not in current_covered]
                if still_missing:
                    summary["errors"] += 1
                    for key in still_missing:
                        fetch_reasons[key] = payload_reason
                        fetch_details[key] = {
                            "quote_key": "|".join(key),
                            "requested_expirations": requested_expirations,
                            "available_expirations": sorted(current_expirations.get(symbol) or set()),
                        }
    finally:
        if not external_gateway:
            seen: set[int] = set()
            for shared_gw in shared_gateways.values():
                if id(shared_gw) in seen:
                    continue
                seen.add(id(shared_gw))
                try:
                    shared_gw.close()
                except Exception:
                    pass
    return fetch_reasons, fetch_details, summary


def _fetch_missing_quotes_via_opend(
    *,
    config: dict[str, Any],
    positions: list[dict[str, Any]],
    quotes: dict[tuple[str, str, str, str], dict[str, Any]],
    covered_keys: set[tuple[str, str, str, str]],
    base_dir: Path,
) -> tuple[dict[tuple[str, str, str, str], str], dict[tuple[str, str, str, str], dict[str, Any]]]:
    advice_cfg = config.get("close_advice") if isinstance(config, dict) else {}
    if isinstance(advice_cfg, dict) and str(advice_cfg.get("quote_source") or "auto").strip().lower() == "required_data":
        return {}, {}

    symbol_cfgs = _symbol_config_by_symbol(config)
    missing_by_symbol: dict[str, list[dict[str, Any]]] = {}
    price_refresh_keys: set[tuple[str, str, str, str]] = set()
    attempted_reasons: dict[tuple[str, str, str, str], str] = {}
    attempted_details: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        key = _quote_key(pos.get("symbol"), pos.get("option_type"), _position_expiration(pos), pos.get("strike"), base_dir=base_dir)
        if not all(key) or key not in covered_keys:
            continue
        quote = quotes.get(key)
        needs_price_refresh = not _quote_has_usable_price(quote)
        if needs_price_refresh:
            missing_by_symbol.setdefault(key[0], []).append(pos)
            price_refresh_keys.add(key)

    if not missing_by_symbol:
        return {}, {}

    try:
        from src.application.opend_symbol_fetching import fetch_symbol
    except Exception:
        return {}, {}

    for symbol, missing_positions in missing_by_symbol.items():
        symbol_cfg = symbol_cfgs.get(symbol) or {}
        fetch_cfg = symbol_cfg.get("fetch") if isinstance(symbol_cfg, dict) else {}
        fetch_cfg = fetch_cfg if isinstance(fetch_cfg, dict) else {}
        requested_symbol = symbol
        resolved_underlier = None
        try:
            resolved_underlier = normalize_underlier(symbol, base_dir=base_dir).code
        except Exception:
            resolved_underlier = None
        missing_keys = [
            _quote_key(pos.get("symbol"), pos.get("option_type"), _position_expiration(pos), pos.get("strike"), base_dir=base_dir)
            for pos in missing_positions
            if isinstance(pos, dict)
        ]
        for key in missing_keys:
            if all(key):
                attempted_details.setdefault(
                    key,
                    {
                        "requested_symbol": requested_symbol,
                        "resolved_underlier": resolved_underlier,
                        "quote_key": "|".join(key),
                    },
                )
        can_fetch = is_futu_fetch_source(fetch_cfg.get("source"))
        if not can_fetch:
            for key in missing_keys:
                if all(key) and key in price_refresh_keys:
                    attempted_reasons[key] = "opend_fetch_skipped_non_futu_source"
            continue
        expirations = sorted({key[2] for key in missing_keys if len(key) >= 3 and key[2]})
        if not expirations:
            for key in missing_keys:
                if all(key) and key in price_refresh_keys:
                    attempted_reasons[key] = "opend_fetch_skipped_missing_expiration"
            continue
        strikes = [safe_float(p.get("strike")) for p in missing_positions]
        strikes = [s for s in strikes if s is not None]
        if not strikes:
            for key in missing_keys:
                if all(key) and key in price_refresh_keys:
                    attempted_reasons[key] = "opend_fetch_skipped_invalid_strike"
            continue
        option_types = sorted({_norm_option_type(p.get("option_type")) for p in missing_positions if p.get("option_type")})
        try:
            payload = fetch_symbol(
                symbol,
                limit_expirations=safe_int(fetch_cfg.get("limit_expirations")) or 8,
                host=str(fetch_cfg.get("host") or "127.0.0.1"),
                port=safe_int(fetch_cfg.get("port")) or 11111,
                base_dir=base_dir,
                option_types=",".join(option_types or ["put", "call"]),
                min_strike=min(strikes),
                max_strike=max(strikes),
                explicit_expirations=expirations,
                chain_cache=True,
                freshness_policy="refresh_missing",
                include_realized_volatility=False,
                **_typed_opend_fetch_kwargs(config),
            )
        except Exception as exc:
            detail = "opend_fetch_error"
            if classify_opend_error(exc).is_rate_limit:
                detail = "opend_fetch_error_rate_limit"
            elif "retry budget" in str(exc or "").lower():
                detail = "opend_fetch_error_retry_budget"
            for key in missing_keys:
                if all(key) and key in price_refresh_keys:
                    attempted_reasons[key] = detail
            continue
        payload_reason = _fetch_payload_error_reason(payload, prefix="opend_fetch_error")
        rows = payload.get("rows") if isinstance(payload, dict) else []
        _merge_quote_rows(quotes, rows if isinstance(rows, list) else [])
        for key in missing_keys:
            if not all(key):
                continue
            if _quote_has_usable_price(quotes.get(key)):
                continue
            if key in price_refresh_keys:
                attempted_reasons[key] = payload_reason or "opend_fetch_no_usable_quote"
    return attempted_reasons, attempted_details


def _classify_required_data_coverage(
    positions: list[dict[str, Any]],
    covered_keys: set[tuple[str, str, str, str]],
    expirations_by_symbol: dict[str, set[str]],
    *,
    base_dir: Path,
) -> tuple[dict[tuple[str, str, str, str], str], dict[tuple[str, str, str, str], dict[str, Any]]]:
    reasons: dict[tuple[str, str, str, str], str] = {}
    details: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    contract_expiration_index = _build_contract_expiration_index(covered_keys)
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        key = _quote_key(pos.get("symbol"), pos.get("option_type"), _position_expiration(pos), pos.get("strike"), base_dir=base_dir)
        if not all(key) or key in covered_keys:
            continue
        available_expirations = sorted(expirations_by_symbol.get(key[0]) or set())
        has_expiration = bool(key[2] and key[2] in (expirations_by_symbol.get(key[0]) or set()))
        near_miss = find_unique_near_miss_expiration(
            key[2],
            contract_expiration_index.get((key[0], key[1], key[3])) or set(),
        )
        reasons[key] = "required_data_missing_contract" if has_expiration else "required_data_missing_expiration"
        details[key] = {
            "quote_key": "|".join(key),
            "available_expirations": available_expirations[:5],
        }
        if near_miss:
            details[key]["expiration_near_miss"] = {
                "requested_expiration": key[2],
                "matched_expiration": near_miss,
            }
    return reasons, details


def _quote_observability_flags(
    key: tuple[str, str, str, str],
    quote: dict[str, Any] | None,
    attempted_fetch_reasons: dict[tuple[str, str, str, str], str],
) -> list[str]:
    reason = attempted_fetch_reasons.get(key)
    if not reason:
        return []
    if reason in {
        "close_advice_plan_unavailable",
        "required_data_position_not_planned",
        "required_data_symbol_config_missing",
        "required_data_symbol_source_unsupported",
        "required_data_route_conflict",
        "required_data_symbol_not_planned",
        "required_data_snapshot_unavailable",
    }:
        return [reason]
    if _quote_has_usable_price(quote):
        return []
    if reason == "required_data_fetch_error_rate_limit":
        return ["required_data_fetch_error", reason]
    if reason in {"required_data_fetch_error", "required_data_fetch_skipped_non_futu_source"}:
        return [reason]
    if reason.startswith("opend_fetch_error_"):
        return ["opend_fetch_error", reason]
    return [reason]


def _filter_positions_by_markets(positions: list[dict[str, Any]], markets_to_run: list[str] | None) -> list[dict[str, Any]]:
    allow = {str(x).strip().upper() for x in (markets_to_run or []) if str(x).strip()}
    if not allow:
        return positions
    out: list[dict[str, Any]] = []
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        market = _market_for_symbol(pos.get("symbol"))
        if market and market in allow:
            out.append(pos)
    return out


def _build_quote_issue_samples(
    positions: list[dict[str, Any]],
    issue_reasons: dict[tuple[str, str, str, str], str],
    issue_details: dict[tuple[str, str, str, str], dict[str, Any]],
    *,
    base_dir: Path | None = None,
    limit: int = 3,
) -> list[str]:
    samples: list[str] = []
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        key = _quote_key(pos.get("symbol"), pos.get("option_type"), _position_expiration(pos), pos.get("strike"), base_dir=base_dir)
        reason = issue_reasons.get(key)
        if not reason:
            continue
        opt = _norm_option_type(pos.get("option_type")) or "option"
        exp = _position_expiration(pos) or "-"
        strike = _num(pos.get("strike"))
        suffix = "P" if opt == "put" else ("C" if opt == "call" else "")
        reason_label = {
            "required_data_missing_expiration": "缺少到期日覆盖",
            "required_data_missing_contract": "缺少合约覆盖",
            "required_data_fetch_error": "补拉持仓覆盖失败",
            "required_data_fetch_error_rate_limit": "OpenD 限频",
            "required_data_fetch_skipped_non_futu_source": "非 Futu 行情源，无法补拉持仓覆盖",
            "opend_fetch_no_usable_quote": "无可用报价",
            "opend_fetch_error_rate_limit": "OpenD 限频",
            "opend_fetch_error_retry_budget": "OpenD 重试预算耗尽",
            "opend_fetch_error": "OpenD 拉取失败",
            "opend_fetch_skipped_non_futu_source": "非 Futu 行情源，跳过补拉",
            "opend_fetch_skipped_missing_expiration": "缺少到期日，跳过补拉",
            "opend_fetch_skipped_invalid_strike": "缺少有效行权价，跳过补拉",
        }.get(reason, reason)
        detail = issue_details.get(key) or {}
        diag = ""
        resolved_underlier = str(detail.get("resolved_underlier") or "").strip()
        requested_symbol = str(detail.get("requested_symbol") or "").strip()
        available_expirations = [str(x).strip() for x in (detail.get("available_expirations") or []) if str(x).strip()]
        near_miss_raw = detail.get("expiration_near_miss")
        near_miss: dict[str, Any] = near_miss_raw if isinstance(near_miss_raw, dict) else {}
        matched_expiration = str(near_miss.get("matched_expiration") or "").strip()
        requested_expiration = str(near_miss.get("requested_expiration") or "").strip()
        if "rate_limit" in reason and str(detail.get("message") or "").strip():
            diag = f" | detail={str(detail.get('message')).strip()[:80]}"
        elif matched_expiration:
            diag = f" | near_miss={requested_expiration or exp}->{matched_expiration}"
        elif available_expirations:
            diag = f" | have={','.join(available_expirations[:3])}"
        elif str(detail.get("message") or "").strip():
            diag = f" | detail={str(detail.get('message')).strip()[:80]}"
        elif resolved_underlier:
            diag = f" | opend={resolved_underlier}"
        elif requested_symbol:
            diag = f" | requested={requested_symbol}"
        sample = f"{_norm_symbol(pos.get('symbol'), base_dir=base_dir)} {opt} {exp} {strike}{suffix}: {reason_label}{diag}"
        if sample not in samples:
            samples.append(sample)
        if len(samples) >= max(int(limit), 0):
            break
    return samples


def _mark_not_evaluable(
    row: dict[str, Any],
    *,
    evaluation_status: str,
    quote_status: str,
    reason: str,
) -> dict[str, Any]:
    row["evaluation_status"] = evaluation_status
    row["quote_status"] = quote_status
    row["reason"] = reason
    row["recommendation_state"] = RECOMMENDATION_NOT_EVALUABLE
    row["decision_evidence_status"] = DECISION_EVIDENCE_NOT_EVALUABLE
    row["decision_basis"] = evaluation_status
    return row


def _position_expiration(pos: dict[str, Any]) -> str | None:
    exp = normalize_expiration(pos.get("expiration_ymd"))
    if exp:
        return exp
    exp = normalize_expiration(effective_expiration_ymd(pos))
    if exp:
        return exp
    exp = normalize_expiration(pos.get("expiration"))
    if exp:
        return exp
    note = str(pos.get("note") or "")
    for token in note.replace(";", " ").split():
        if token.startswith("exp="):
            return normalize_expiration(token.split("=", 1)[1])
    return None


def _position_premium(pos: dict[str, Any]) -> float | None:
    premium = safe_float(pos.get("premium"))
    if premium is not None:
        return premium
    note = str(pos.get("note") or "")
    for token in note.replace(";", " ").split():
        if token.startswith("premium_per_share="):
            return safe_float(token.split("=", 1)[1])
    return None


def _is_supported_short_option(pos: dict[str, Any]) -> bool:
    return (
        str(pos.get("side") or "").strip().lower() == "short"
        and _norm_option_type(pos.get("option_type")) in {"put", "call"}
    )


def _position_lifecycle(
    pos: dict[str, Any],
    *,
    business_date: date,
) -> tuple[str, int | None]:
    expiration = _position_expiration(pos)
    if not expiration:
        return "unknown", None
    try:
        exp_date = datetime.strptime(expiration[:10], "%Y-%m-%d").date()
    except ValueError:
        return "unknown", None
    dte = (exp_date - business_date).days
    if dte < 0:
        return "expired_open", dte
    if dte == 0:
        return "expiry_day", dte
    return "active", dte


def _calc_dte(expiration: str | None, *, business_date: date) -> int | None:
    if not expiration:
        return None
    try:
        exp_date = datetime.strptime(expiration[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return (exp_date - business_date).days


def _mid_from_quote(quote: dict[str, Any] | None) -> tuple[float | None, list[str]]:
    if not isinstance(quote, dict):
        return None, ["missing_quote"]
    bid = _quote_number(quote.get("bid"))
    ask = _quote_number(quote.get("ask"))
    if bid is None or ask is None:
        return None, ["missing_bid_ask"]
    if bid < 0 or ask <= 0 or ask < bid:
        return None, ["invalid_bid_ask"]
    return round((bid + ask) / 2, 6), ["mid_from_bid_ask"]


def _original_dte(pos: dict[str, Any], expiration: str | None) -> int | None:
    if not expiration:
        return None
    opened_at = pos.get("opened_at")
    if isinstance(opened_at, bool):
        return None
    opened_date = expiration_timestamp_to_date(opened_at)
    if opened_date is None:
        return None
    try:
        expiration_date = datetime.strptime(expiration[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return (expiration_date - opened_date).days


def _position_multiplier(pos: dict[str, Any]) -> float | None:
    if isinstance(pos.get("multiplier"), bool):
        return None
    return safe_float(effective_multiplier(pos))


def _strict_fee_estimates(
    pos: dict[str, Any],
    *,
    ask: float | None,
) -> tuple[float | None, float | None, str, str | None]:
    broker = normalize_broker(pos.get("broker"))
    currency = normalize_currency(pos.get("currency"))
    premium = _position_premium(pos)
    contracts = safe_int(pos.get("contracts_open"))
    multiplier = _position_multiplier(pos)
    multiplier_int = safe_int(multiplier)
    if broker != "富途":
        return None, None, "unsupported_broker", None
    if currency not in {"USD", "HKD"}:
        return None, None, "unsupported_currency", None
    if (
        premium is None
        or premium <= 0
        or ask is None
        or ask <= 0
        or contracts is None
        or contracts <= 0
        or multiplier_int is None
        or multiplier_int <= 0
    ):
        return None, None, "unavailable", None
    try:
        open_fee = calc_futu_option_fee(
            currency,
            premium,
            contracts=contracts,
            multiplier=multiplier_int,
            is_sell=True,
        )
        close_fee = calc_futu_option_fee(
            currency,
            ask,
            contracts=contracts,
            multiplier=multiplier_int,
            is_sell=False,
        )
    except (TypeError, ValueError):
        return None, None, "unavailable", None
    if currency == "HKD":
        return (
            float(open_fee),
            float(close_fee),
            "conservative_estimate",
            FUTU_HK_OPTION_FEE_BASIS,
        )
    return (
        float(open_fee),
        float(close_fee),
        "schedule_estimate",
        FUTU_US_OPTION_FEE_BASIS,
    )


def _position_to_input(
    pos: dict[str, Any],
    quote: dict[str, Any] | None,
    *,
    business_date: date,
) -> tuple[CloseAdviceInput, list[str]]:
    expiration = _position_expiration(pos)
    mid, quote_flags = _mid_from_quote(quote)
    bid = safe_float((quote or {}).get("bid"))
    ask = safe_float((quote or {}).get("ask"))
    open_fee, close_fee, fee_status, fee_basis = _strict_fee_estimates(
        pos,
        ask=ask,
    )
    return (
        CloseAdviceInput(
            account=normalize_account(pos.get("account")),
            position_lot_id=str(pos.get("record_id") or "").strip() or None,
            symbol=_norm_symbol(pos.get("symbol")),
            option_type=_norm_option_type(pos.get("option_type")),
            side=str(pos.get("side") or "").strip().lower(),
            expiration=expiration,
            strike=safe_float(pos.get("strike")),
            contracts_open=safe_int(pos.get("contracts_open")),
            premium=_position_premium(pos),
            bid=bid,
            ask=ask,
            dte=_calc_dte(expiration, business_date=business_date),
            multiplier=_position_multiplier(pos),
            spot=safe_float((quote or {}).get("spot")),
            currency=normalize_currency(pos.get("currency") or (quote or {}).get("currency")),
            original_dte=_original_dte(pos, expiration),
            estimated_open_fee=open_fee,
            estimated_close_fee=close_fee,
            fee_calc_status=fee_status,
            fee_calc_basis=fee_basis,
        ),
        quote_flags,
    )


def _evaluate_position_close_advice(
    *,
    inp: CloseAdviceInput,
    pos: dict[str, Any],
    quote: dict[str, Any] | None,
) -> dict[str, Any]:
    del quote
    row = evaluate_close_advice(inp)
    row.update(
        {
            "broker": normalize_broker(pos.get("broker")),
            "position_side": str(pos.get("side") or "").strip().lower(),
            "strategy_family": (
                "sell_put" if inp.option_type == "put" else "covered_call"
            ),
            "strategy_profile": "strict_profit_capture.v1",
        }
    )
    return row


def _lifecycle_not_evaluable_row(
    *,
    inp: CloseAdviceInput,
    pos: dict[str, Any],
    config: dict[str, Any],
    lifecycle_state: str,
) -> dict[str, Any]:
    reasons = {
        "expiry_day": "持仓已到到期日，已离开严格提前止盈窗口，当前不请求常规平仓报价",
        "expired_open": "持仓到期日已过但仍标记为 open，需要先核对持仓生命周期；当前不请求行情",
        "unknown": "持仓缺少可解析到期日，当前无法确定生命周期或评估平仓建议",
    }
    flags = {
        "expiry_day": "expiry_day_lifecycle",
        "expired_open": "expired_position_marked_open",
        "unknown": "missing_expiration",
    }
    row: dict[str, Any] = {
        "account": str(inp.account or "").strip().lower(),
        "position_lot_id": inp.position_lot_id,
        "symbol": str(inp.symbol or "").strip().upper(),
        "option_type": str(inp.option_type or "").strip().lower(),
        "expiration": inp.expiration,
        "strike": safe_float(inp.strike),
        "contracts_open": safe_int(inp.contracts_open),
        "premium": safe_float(inp.premium),
        "close_mid": None,
        "bid": None,
        "ask": None,
        "dte": safe_int(inp.dte),
        "position_lifecycle_state": lifecycle_state,
        "multiplier": safe_float(inp.multiplier),
        "estimated_close_fee": None,
        "fee_calc_status": "not_required",
        "fee_calc_basis": None,
        "estimated_pnl_if_close_net": None,
        "spread_ratio": None,
        "reason": reasons[lifecycle_state],
        "evaluation_status": "not_evaluable",
        "quote_status": "not_required" if lifecycle_state != "unknown" else "not_evaluable",
        "data_quality_flags": flags[lifecycle_state],
        "currency": str(inp.currency or "").strip().upper() or None,
        "spot": safe_float(inp.spot),
        "policy_version": STRICT_CLOSE_POLICY_VERSION,
        "recommendation_state": RECOMMENDATION_NOT_EVALUABLE,
        "decision_basis": flags[lifecycle_state],
        "decision_evidence_status": DECISION_EVIDENCE_NOT_EVALUABLE,
    }
    del config
    row.update(
        {
            "broker": normalize_broker(pos.get("broker")),
            "position_side": str(pos.get("side") or "").strip().lower(),
            "strategy_family": (
                "sell_put" if inp.option_type == "put" else "covered_call"
            ),
            "strategy_profile": "strict_profit_capture.v1",
        }
    )
    return row


def _with_extra_flags(row: dict[str, Any], flags: list[str]) -> dict[str, Any]:
    cur = [x for x in str(row.get("data_quality_flags") or "").split(";") if x]
    for flag in flags:
        if flag and flag not in cur:
            cur.append(flag)
    row["data_quality_flags"] = ";".join(cur)
    return row


def _money(value: Any, currency: Any) -> str:
    v = safe_float(value)
    if v is None:
        return "-"
    ccy = normalize_currency(currency)
    prefix = "$" if ccy == "USD" else ("HK$" if ccy == "HKD" else "")
    abs_v = abs(v)
    fmt = f"{v:,.2f}" if abs_v < 100 else f"{v:,.0f}"
    if prefix:
        return f"{prefix}{fmt}"
    return f"{fmt} {ccy}".strip()


def _pct(value: Any) -> str:
    v = safe_float(value)
    if v is None:
        return "-"
    return f"{v * 100:.1f}%"


def _num(value: Any) -> str:
    v = safe_float(value)
    if v is None:
        return "-"
    return f"{v:.2f}"


def render_markdown(rows: list[dict[str, Any]], *, max_items: int) -> str:
    selected = select_close_advice_notification_rows(
        rows,
        max_items_per_account=max_items,
    )
    if not selected:
        return ""

    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for row in selected:
        acct = _row_account(row.get("account"))
        grouped.setdefault(acct, []).append(row)

    lines: list[str] = []
    for acct, acct_rows in grouped.items():
        if lines:
            lines.append("")
        lines.append(f"### [{acct}] 严格平仓提醒")
        for row in acct_rows:
            opt = "Put" if str(row.get("option_type")) == "put" else "Call"
            exp = row.get("expiration") or "-"
            strike = _num(row.get("strike"))
            currency = row.get("currency")
            lines.extend(
                [
                    f"- {row.get('symbol')} {opt} {exp} @{strike} · 建议买回平仓",
                    (
                        f"- 条件: 净兑现 {_pct(row.get('net_capture_ratio'))} | "
                        f"平仓全成本/名义本金 {_pct(row.get('close_cost_ratio'))} | "
                        f"剩余期限 {_pct(row.get('remaining_term_ratio'))}"
                    ),
                    (
                        f"- 价格: 当前 ask={_money(row.get('ask'), currency)} | "
                        f"全成本={_money(row.get('all_in_close_cost'), currency)} | "
                        f"预计净锁定={_money(row.get('estimated_pnl_if_close_net'), currency)}"
                    ),
                    f"- 理由: {row.get('reason') or '-'}",
                ]
            )
    return "\n".join(lines).strip() + "\n"


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    from io import StringIO

    buf = StringIO()
    writer = csv.DictWriter(buf, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    atomic_write_text(path, buf.getvalue(), encoding="utf-8")


def _load_context(context_path: Path) -> dict[str, Any]:
    obj = read_json(context_path, default=None)
    return _validate_context(obj)


def _validate_context(obj: Any) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise ValueError("close_advice position context is missing or malformed")
    status = str(obj.get("context_status") or "").strip().lower()
    ledger = obj.get("ledger") if isinstance(obj.get("ledger"), dict) else {}
    if status == "unavailable" or bool(ledger.get("fail_closed")):
        raise ValueError("close_advice position context is unavailable")
    if not isinstance(obj.get("open_positions_min"), list):
        raise ValueError("close_advice position context has no valid open_positions_min list")
    return obj


def _snapshot_integrity_failure_result(
    *,
    output_dir: Path,
    run_id: str | None,
    reason: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = publish_close_advice_report_status(
        output_dir=output_dir,
        status="failed",
        run_id=run_id,
        quote_mode="frozen_snapshot",
        reason=reason,
        evidence=evidence,
    )
    return {
        "enabled": True,
        "status": "snapshot_integrity_failed",
        "snapshot_authority": "invalid",
        "rows": 0,
        "evaluable_rows": 0,
        "evaluation_gap_rows": 0,
        "notify_rows": 0,
        "recommendation_counts": {},
        "evaluation_status_counts": {},
        "flag_counts": {"required_data_snapshot_integrity_failed": 1},
        "quote_issue_rows": 0,
        "quote_issue_samples": [],
        "coverage_summary": {},
        "quote_fetch_diagnostics": {
            "attempted": 0,
            "coverage_missing": 0,
            "coverage_fetch_attempted_symbols": 0,
            "network_fetch_attempts": 0,
            "required_data_write_attempts": 0,
            "position_requirements_total": 0,
            "position_requirements_planned": 0,
            "position_requirements_validated": 0,
            "position_requirements_missing": 0,
            "binding_ids": [],
        },
        "quote_freshness": {
            "enforced": True,
            "authority": "required_data_snapshot_manifest",
            "symbols": {},
        },
        "report_manifest": manifest,
        "integrity_failure": {
            "reason": reason,
            "evidence": dict(evidence or {}),
        },
        "csv": str(Path(output_dir).resolve() / "close_advice.csv"),
        "text": str(Path(output_dir).resolve() / "close_advice.txt"),
        "notification_text": "",
    }


def _frozen_position_plan_reasons(
    *,
    positions: list[dict[str, Any]],
    plan: dict[str, Any] | None,
    account: str,
    base_dir: Path,
) -> tuple[
    dict[tuple[str, str, str, str], str],
    set[str],
]:
    reasons: dict[tuple[str, str, str, str], str] = {}
    symbols_to_validate: set[str] = set()
    requirements: dict[str, dict[str, Any]] = {}
    requirement_reasons: dict[str, str] = {}
    account_status = "unavailable"
    if plan is not None:
        (
            requirements,
            requirement_reasons,
            account_status,
        ) = account_requirement_index(
            payload=plan,
            account=account,
        )
    for position in positions:
        if not isinstance(position, dict):
            continue
        key = _quote_key(
            position.get("symbol"),
            position.get("option_type"),
            _position_expiration(position),
            position.get("strike"),
            base_dir=base_dir,
        )
        if not all(key):
            continue
        lot_id = str(position.get("record_id") or "").strip()
        if plan is None or account_status == "unavailable":
            reasons[key] = "close_advice_plan_unavailable"
            continue
        requirement = requirements.get(lot_id)
        if requirement is None:
            reasons[key] = "required_data_position_not_planned"
            continue
        if str(requirement.get("quote_key") or "") != "|".join(key):
            reasons[key] = "required_data_position_not_planned"
            continue
        planning_reason = (
            requirement_reasons.get(lot_id)
            or str(requirement.get("planning_reason") or "").strip()
        )
        if (
            str(requirement.get("planning_status") or "ready") != "ready"
            or planning_reason
        ):
            reasons[key] = (
                planning_reason or "required_data_position_not_planned"
            )
            continue
        symbols_to_validate.add(key[0])
    return reasons, symbols_to_validate


def _validate_frozen_symbols(
    *,
    manifest_path: Path,
    expected_run_id: str,
    required_data_root: Path,
    symbols: set[str],
    expected_manifest_sha256: str,
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, str],
    dict[str, bytes],
]:
    provenance: dict[str, dict[str, Any]] = {}
    unavailable: dict[str, str] = {}
    csv_bytes_by_symbol: dict[str, bytes] = {}
    for symbol in sorted(symbols):
        try:
            (
                symbol_provenance,
                symbol_csv_bytes,
            ) = resolve_frozen_required_data_csv_bytes(
                manifest_path=manifest_path,
                expected_run_id=expected_run_id,
                symbol=symbol,
                required_data_root=required_data_root,
            )
            if (
                str(symbol_provenance.get("manifest_sha256") or "")
                != expected_manifest_sha256
            ):
                raise RequiredDataSnapshotError(
                    "required-data snapshot manifest changed during Close Advice"
                )
            provenance[symbol] = symbol_provenance
            csv_bytes_by_symbol[symbol] = symbol_csv_bytes
        except FrozenRequiredDataUnavailable as exc:
            if exc.reason in {
                "manifest_invalid",
                "receipt_or_payload_mismatch",
            }:
                raise RequiredDataSnapshotError(str(exc)) from exc
            unavailable[symbol] = (
                "required_data_symbol_not_planned"
                if exc.reason == "symbol_entry_missing"
                else "required_data_snapshot_unavailable"
            )
    return provenance, unavailable, csv_bytes_by_symbol


def _load_frozen_required_data_quotes(
    *,
    csv_bytes_by_symbol: dict[str, bytes],
    base_dir: Path,
) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    quotes: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for symbol in sorted(csv_bytes_by_symbol):
        try:
            frame = pd.read_csv(BytesIO(csv_bytes_by_symbol[symbol]))
        except (
            UnicodeDecodeError,
            ValueError,
            pd.errors.EmptyDataError,
            pd.errors.ParserError,
        ) as exc:
            raise RequiredDataSnapshotError(
                f"{symbol} sealed required-data CSV is unreadable"
            ) from exc
        for row in frame.to_dict("records"):
            key = _quote_key(
                row.get("symbol") or symbol,
                row.get("option_type"),
                row.get("expiration"),
                row.get("strike"),
                base_dir=base_dir,
            )
            if all(key):
                quotes[key] = row
    return quotes


def _apply_required_data_row_provenance(
    row: dict[str, Any],
    *,
    position: dict[str, Any],
    quote_key: tuple[str, str, str, str] | None,
    frozen_mode: bool,
    frozen_manifest: dict[str, Any] | None,
    frozen_manifest_sha256: str | None,
    frozen_plan_sha256: str | None,
    requirements_by_lot: dict[str, dict[str, Any]],
    provenance_by_symbol: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    row["quote_mode"] = (
        "frozen_snapshot" if frozen_mode else "legacy_mutable"
    )
    if not frozen_mode:
        return row
    row["required_data_snapshot_plan_id"] = str(
        (frozen_manifest or {}).get("plan_id") or ""
    ) or None
    row["required_data_snapshot_manifest_sha256"] = (
        frozen_manifest_sha256
    )
    row["close_advice_required_data_plan_sha256"] = frozen_plan_sha256
    lot_id = str(position.get("record_id") or "").strip()
    requirement = requirements_by_lot.get(lot_id) or {}
    binding = (
        requirement.get("fetch_binding")
        if isinstance(requirement.get("fetch_binding"), dict)
        else {}
    )
    row["required_data_requirement_id"] = str(
        requirement.get("requirement_id") or ""
    ) or None
    row["required_data_binding_id"] = str(
        binding.get("binding_id") or ""
    ) or None
    symbol = quote_key[0] if quote_key and all(quote_key) else ""
    provenance = provenance_by_symbol.get(symbol) or {}
    row["required_data_snapshot_id"] = str(
        provenance.get("snapshot_id") or ""
    ) or None
    row["required_data_receipt_hash"] = str(
        provenance.get("receipt_hash") or ""
    ) or None
    row["required_data_payload_sha256"] = str(
        provenance.get("payload_sha256") or ""
    ) or None
    row["required_data_source_observed_at"] = str(
        provenance.get("source_observed_at") or ""
    ) or None
    row["required_data_expires_at"] = str(
        provenance.get("expires_at") or ""
    ) or None
    return row


def _unlink_if_present(path: Path | None) -> None:
    if path is None:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass


def run_close_advice(
    *,
    config: dict[str, Any],
    context_path: Path,
    required_data_root: Path,
    output_dir: Path,
    base_dir: Path,
    markets_to_run: list[str] | None = None,
    gateway: Any = None,
    required_data_snapshot_manifest: Path | None = None,
    required_data_snapshot_run_id: str | None = None,
    close_advice_required_data_plan: Path | None = None,
    account: str | None = None,
    context_override: Mapping[str, Any] | None = None,
    required_data_snapshot_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    advice_cfg_raw = config.get("close_advice") if isinstance(config, dict) else {}
    advice_cfg = advice_cfg_raw if isinstance(advice_cfg_raw, dict) else {}
    output_dir = Path(output_dir).resolve()
    csv_path = output_dir / "close_advice.csv"
    text_path = output_dir / "close_advice.txt"
    frozen_mode = required_data_snapshot_manifest is not None
    quote_mode = "frozen_snapshot" if frozen_mode else "legacy_mutable"

    if not bool(advice_cfg.get("enabled", False)):
        report_manifest = publish_close_advice_report_status(
            output_dir=output_dir,
            status="failed",
            run_id=required_data_snapshot_run_id,
            quote_mode=quote_mode,
            reason="close_advice_disabled",
        )
        _write_csv(csv_path, [])
        atomic_write_text(text_path, "", encoding="utf-8")
        return {
            "enabled": False,
            "status": "disabled",
            "rows": 0,
            "notify_rows": 0,
            "report_manifest": report_manifest,
            "csv": str(csv_path),
            "text": str(text_path),
            "notification_text": "",
        }

    publish_close_advice_report_status(
        output_dir=output_dir,
        status="pending",
        run_id=required_data_snapshot_run_id,
        quote_mode=quote_mode,
    )
    frozen_manifest_path = (
        Path(required_data_snapshot_manifest).resolve()
        if required_data_snapshot_manifest is not None
        else None
    )
    frozen_plan: dict[str, Any] | None = None
    frozen_plan_path: Path | None = None
    frozen_manifest_payload: dict[str, Any] | None = None
    frozen_manifest_sha256: str | None = None
    frozen_plan_sha256: str | None = None
    if frozen_mode:
        try:
            run_id = str(required_data_snapshot_run_id or "").strip()
            if not run_id or frozen_manifest_path is None:
                raise RequiredDataSnapshotError(
                    "frozen Close Advice run identity is unavailable"
                )
            (
                frozen_manifest_payload,
                _frozen_root,
                frozen_manifest_bytes,
            ) = load_required_data_snapshot_manifest_snapshot(
                manifest_path=frozen_manifest_path,
                expected_run_id=run_id,
                expected_required_data_root=Path(required_data_root),
            )
            frozen_manifest_sha256 = sha256_bytes(frozen_manifest_bytes)
            expected_manifest_sha256 = str(
                required_data_snapshot_manifest_sha256 or ""
            ).strip().lower()
            if (
                expected_manifest_sha256
                and expected_manifest_sha256 != frozen_manifest_sha256
            ):
                raise RequiredDataSnapshotError(
                    "required-data snapshot manifest generation mismatch"
                )
            bound_plan = resolve_bound_close_advice_required_data_plan_snapshot(
                manifest_path=frozen_manifest_path,
                manifest=frozen_manifest_payload,
                expected_run_id=run_id,
                expected_plan_path=close_advice_required_data_plan,
            )
            if bound_plan is not None:
                frozen_plan, frozen_plan_path, frozen_plan_bytes = bound_plan
                frozen_plan_sha256 = sha256_bytes(frozen_plan_bytes)
                business_date = datetime.strptime(
                    str(frozen_plan["business_date"]),
                    "%Y-%m-%d",
                ).date()
            else:
                business_date = expiration_business_today()
        except (
            OSError,
            ValueError,
            RequiredDataSnapshotError,
            CloseAdviceRequiredDataPlanError,
        ) as exc:
            return _snapshot_integrity_failure_result(
                output_dir=output_dir,
                run_id=required_data_snapshot_run_id,
                reason="required_data_snapshot_integrity_failed",
                evidence={
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
    else:
        business_date = expiration_business_today()
    ctx = (
        _validate_context(dict(context_override))
        if context_override is not None
        else _load_context(context_path)
    )
    account_norm = (
        normalize_account(account)
        or normalize_account(
            ((ctx.get("filters") or {}) if isinstance(ctx, dict) else {}).get(
                "account"
            )
        )
    )
    positions = ctx.get("open_positions_min") if isinstance(ctx, dict) else []
    positions = positions if isinstance(positions, list) else []
    positions = _filter_positions_by_markets(positions, markets_to_run)
    positions = [
        pos
        for pos in positions
        if isinstance(pos, dict) and _is_supported_short_option(pos)
    ]
    position_entries = [
        (pos, *_position_lifecycle(pos, business_date=business_date))
        for pos in positions
        if isinstance(pos, dict)
    ]
    coverage_positions = [
        pos
        for pos, lifecycle_state, _dte in position_entries
        if lifecycle_state in {"active", "unknown"}
    ]
    quote_positions = [
        pos
        for pos, lifecycle_state, _dte in position_entries
        if lifecycle_state == "active"
    ]
    symbols = {
        _norm_symbol(p.get("symbol"), base_dir=Path(base_dir))
        for p in quote_positions
        if p.get("symbol")
    }
    frozen_plan_reasons: dict[
        tuple[str, str, str, str],
        str,
    ] = {}
    frozen_provenance: dict[str, dict[str, Any]] = {}
    frozen_requirements_by_lot: dict[str, dict[str, Any]] = {}
    if frozen_mode:
        if frozen_plan is not None:
            frozen_requirements_by_lot, _requirement_reasons, _account_status = (
                account_requirement_index(
                    payload=frozen_plan,
                    account=account_norm or "",
                )
            )
        frozen_plan_reasons, symbols_to_validate = (
            _frozen_position_plan_reasons(
                positions=quote_positions,
                plan=frozen_plan,
                account=account_norm or "",
                base_dir=Path(base_dir),
            )
        )
        try:
            assert frozen_manifest_path is not None
            (
                frozen_provenance,
                frozen_symbol_unavailable,
                frozen_csv_bytes_by_symbol,
            ) = _validate_frozen_symbols(
                manifest_path=frozen_manifest_path,
                expected_run_id=str(
                    required_data_snapshot_run_id or ""
                ),
                required_data_root=Path(required_data_root),
                symbols=symbols_to_validate,
                expected_manifest_sha256=str(frozen_manifest_sha256),
            )
            quotes = _load_frozen_required_data_quotes(
                csv_bytes_by_symbol=frozen_csv_bytes_by_symbol,
                base_dir=Path(base_dir),
            )
        except RequiredDataSnapshotError as exc:
            return _snapshot_integrity_failure_result(
                output_dir=output_dir,
                run_id=required_data_snapshot_run_id,
                reason="required_data_snapshot_integrity_failed",
                evidence={
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
        for position in quote_positions:
            key = _quote_key(
                position.get("symbol"),
                position.get("option_type"),
                _position_expiration(position),
                position.get("strike"),
                base_dir=Path(base_dir),
            )
            if (
                all(key)
                and key not in frozen_plan_reasons
                and key[0] in frozen_symbol_unavailable
            ):
                frozen_plan_reasons[key] = frozen_symbol_unavailable[key[0]]
        coverage_fetch_reasons: dict[
            tuple[str, str, str, str],
            str,
        ] = {}
        coverage_fetch_details: dict[
            tuple[str, str, str, str],
            dict[str, Any],
        ] = {}
        coverage_fetch_summary = {
            "attempted_symbols": 0,
            "fetched_symbols": 0,
            "errors": 0,
        }
    else:
        (
            coverage_fetch_reasons,
            coverage_fetch_details,
            coverage_fetch_summary,
        ) = _ensure_required_data_coverage_for_positions(
            config=config,
            positions=quote_positions,
            required_data_root=Path(required_data_root),
            base_dir=Path(base_dir),
            gateway=gateway,
        )
    if frozen_mode:
        covered_keys = set(quotes)
        expirations_by_symbol: dict[str, set[str]] = {}
        for symbol, _option_type, expiration, _strike in covered_keys:
            expirations_by_symbol.setdefault(symbol, set()).add(
                expiration
            )
    else:
        quotes = load_required_data_quotes(
            Path(required_data_root),
            symbols=symbols,
            base_dir=Path(base_dir),
        )
        covered_keys, expirations_by_symbol = load_required_data_coverage(
            Path(required_data_root),
            symbols=symbols,
            base_dir=Path(base_dir),
        )
    provenance_enforced = (
        str(ctx.get("context_status") or "").strip().lower() == "available"
    )
    quote_max_age_sec = DEFAULT_QUOTE_MAX_AGE_SEC
    quote_freshness_by_symbol: dict[str, dict[str, Any]] = {}
    freshness_reasons: dict[tuple[str, str, str, str], str] = {}
    if frozen_mode:
        quote_freshness_by_symbol = {
            symbol: {
                "ok": True,
                "authority": "required_data_snapshot_manifest",
                **dict(provenance),
            }
            for symbol, provenance in frozen_provenance.items()
        }
    elif provenance_enforced:
        for symbol in sorted(symbols):
            quote_csv_path = (
                Path(required_data_root)
                / "parsed"
                / f"{symbol}_required_data.csv"
            )
            freshness = validate_quote_cache_metadata(
                csv_path=quote_csv_path,
                symbol=symbol,
                max_age_sec=quote_max_age_sec,
            )
            quote_freshness_by_symbol[symbol] = freshness
            if freshness.get("ok"):
                continue
            reason = str(freshness.get("reason") or "quote_provenance_invalid")
            for pos in quote_positions:
                if _norm_symbol(pos.get("symbol"), base_dir=Path(base_dir)) != symbol:
                    continue
                key = _quote_key(
                    pos.get("symbol"),
                    pos.get("option_type"),
                    _position_expiration(pos),
                    pos.get("strike"),
                    base_dir=Path(base_dir),
                )
                freshness_reasons[key] = reason
    coverage_reasons, coverage_details = _classify_required_data_coverage(
        coverage_positions,
        covered_keys,
        expirations_by_symbol,
        base_dir=Path(base_dir),
    )
    if frozen_mode:
        attempted_fetch_reasons: dict[
            tuple[str, str, str, str],
            str,
        ] = {}
        attempted_fetch_details: dict[
            tuple[str, str, str, str],
            dict[str, Any],
        ] = {}
    else:
        (
            attempted_fetch_reasons,
            attempted_fetch_details,
        ) = _fetch_missing_quotes_via_opend(
            config=config,
            positions=quote_positions,
            quotes=quotes,
            covered_keys=covered_keys,
            base_dir=Path(base_dir),
        )
    issue_reasons = {
        **freshness_reasons,
        **coverage_reasons,
        **coverage_fetch_reasons,
        **attempted_fetch_reasons,
        **frozen_plan_reasons,
    }
    issue_details = {**coverage_details, **coverage_fetch_details, **attempted_fetch_details}

    rows: list[dict[str, Any]] = []
    evaluation_status_counts: dict[str, int] = {}
    for pos0, lifecycle_state, _lifecycle_dte in position_entries:
        exp = _position_expiration(pos0)
        if lifecycle_state != "active":
            inp, _quote_flags = _position_to_input(
                pos0,
                None,
                business_date=business_date,
            )
            row = _lifecycle_not_evaluable_row(
                inp=inp,
                pos=pos0,
                config=config,
                lifecycle_state=lifecycle_state,
            )
            row = _apply_required_data_row_provenance(
                row,
                position=pos0,
                quote_key=_quote_key(
                    pos0.get("symbol"),
                    pos0.get("option_type"),
                    exp,
                    pos0.get("strike"),
                    base_dir=Path(base_dir),
                ),
                frozen_mode=frozen_mode,
                frozen_manifest=frozen_manifest_payload,
                frozen_manifest_sha256=frozen_manifest_sha256,
                frozen_plan_sha256=frozen_plan_sha256,
                requirements_by_lot=frozen_requirements_by_lot,
                provenance_by_symbol=frozen_provenance,
            )
            status = str(row.get("evaluation_status") or "unknown").strip().lower() or "unknown"
            evaluation_status_counts[status] = evaluation_status_counts.get(status, 0) + 1
            rows.append(row)
            continue

        key = _quote_key(pos0.get("symbol"), pos0.get("option_type"), exp, pos0.get("strike"), base_dir=Path(base_dir))
        quote = quotes.get(key)
        inp, quote_flags = _position_to_input(
            pos0,
            quote,
            business_date=business_date,
        )
        row = _evaluate_position_close_advice(
            inp=inp,
            pos=pos0,
            quote=quote,
        )
        row["position_lifecycle_state"] = lifecycle_state
        row = _apply_required_data_row_provenance(
            row,
            position=pos0,
            quote_key=key,
            frozen_mode=frozen_mode,
            frozen_manifest=frozen_manifest_payload,
            frozen_manifest_sha256=frozen_manifest_sha256,
            frozen_plan_sha256=frozen_plan_sha256,
            requirements_by_lot=frozen_requirements_by_lot,
            provenance_by_symbol=frozen_provenance,
        )
        row = _with_extra_flags(row, quote_flags)
        row = _with_extra_flags(row, _quote_observability_flags(key, quote, issue_reasons))
        issue_reason = str(issue_reasons.get(key) or "").strip()
        if (
            issue_reason.startswith("required_data_")
            or issue_reason == "close_advice_plan_unavailable"
        ):
            row = _mark_not_evaluable(
                row,
                evaluation_status="coverage_missing",
                quote_status="coverage_missing",
                reason="持仓对应合约未完成行情覆盖，当前无法评估平仓建议",
            )
        elif issue_reason:
            row = _mark_not_evaluable(
                row,
                evaluation_status="quote_unusable",
                quote_status="quote_unusable",
                reason="持仓对应合约已定位，但当前未取得可用价格，暂无法评估平仓建议",
            )
        elif (
            str(row.get("recommendation_state") or "").strip().lower()
            == RECOMMENDATION_NOT_EVALUABLE
        ):
            row["evaluation_status"] = "not_evaluable"
            row["quote_status"] = "not_evaluable"
        else:
            row["evaluation_status"] = "priced"
            row["quote_status"] = "priced"
        status = str(row.get("evaluation_status") or "unknown").strip().lower() or "unknown"
        evaluation_status_counts[status] = evaluation_status_counts.get(status, 0) + 1
        rows.append(row)

    rows = sort_advice_rows(rows)
    max_items_raw = safe_int(advice_cfg.get("max_items_per_account"))
    max_items = 5 if max_items_raw is None else max_items_raw
    text = render_markdown(
        rows,
        max_items=max_items,
    )
    selected_notify_rows = select_close_advice_notification_rows(
        rows,
        max_items_per_account=max_items,
    )
    flag_counts: dict[str, int] = {}
    recommendation_counts: dict[str, int] = {}
    quote_issue_rows = 0
    evaluation_gap_rows = 0
    for row in rows:
        if str(row.get("evaluation_status") or "").strip().lower() == "priced":
            recommendation = (
                str(row.get("recommendation_state") or "")
                .strip()
                .lower()
                or "unknown"
            )
            recommendation_counts[recommendation] = (
                recommendation_counts.get(recommendation, 0) + 1
            )
        else:
            evaluation_gap_rows += 1
        flags = [x for x in str(row.get("data_quality_flags") or "").split(";") if x]
        if any(flag in QUOTE_ISSUE_FLAGS for flag in flags):
            quote_issue_rows += 1
        for flag in flags:
            flag_counts[flag] = flag_counts.get(flag, 0) + 1

    attempt_csv_path: Path | None = None
    attempt_text_path: Path | None = None
    write_csv_path = csv_path
    write_text_path = text_path
    if frozen_mode:
        attempt_id = uuid4().hex
        attempt_csv_path = output_dir / f".close_advice.{attempt_id}.csv.tmp"
        attempt_text_path = output_dir / f".close_advice.{attempt_id}.txt.tmp"
        write_csv_path = attempt_csv_path
        write_text_path = attempt_text_path

    _write_csv(write_csv_path, rows)
    atomic_write_text(write_text_path, text, encoding="utf-8")
    if frozen_mode:
        try:
            assert frozen_manifest_path is not None
            (
                manifest_now,
                _root_now,
                manifest_bytes_now,
            ) = load_required_data_snapshot_manifest_snapshot(
                manifest_path=frozen_manifest_path,
                expected_run_id=str(required_data_snapshot_run_id or ""),
                expected_required_data_root=Path(required_data_root),
            )
            manifest_hash_now = sha256_bytes(manifest_bytes_now)
            if manifest_hash_now != frozen_manifest_sha256:
                raise RequiredDataSnapshotError(
                    "required-data snapshot manifest changed during Close Advice"
                )
            plan_now = resolve_bound_close_advice_required_data_plan_snapshot(
                manifest_path=frozen_manifest_path,
                manifest=manifest_now,
                expected_run_id=str(required_data_snapshot_run_id or ""),
                expected_plan_path=frozen_plan_path,
            )
            if (plan_now is None) != (frozen_plan is None):
                raise CloseAdviceRequiredDataPlanError(
                    "close-advice required-data plan binding changed"
                )
            if plan_now is not None:
                plan_payload_now, _plan_path_now, plan_bytes_now = plan_now
                if (
                    str(plan_payload_now.get("content_sha256") or "")
                    != str(
                        (frozen_plan or {}).get("content_sha256") or ""
                    )
                    or sha256_bytes(plan_bytes_now) != frozen_plan_sha256
                ):
                    raise CloseAdviceRequiredDataPlanError(
                        "close-advice required-data plan changed during evaluation"
                    )
            (
                _revalidated,
                unavailable_now,
                _revalidated_csv_bytes,
            ) = _validate_frozen_symbols(
                manifest_path=frozen_manifest_path,
                expected_run_id=str(required_data_snapshot_run_id or ""),
                required_data_root=Path(required_data_root),
                symbols=symbols_to_validate,
                expected_manifest_sha256=str(frozen_manifest_sha256),
            )
            if unavailable_now:
                raise RequiredDataSnapshotError(
                    "required-data symbol authority changed during Close Advice"
                )
            assert attempt_csv_path is not None
            assert attempt_text_path is not None
            os.replace(attempt_csv_path, csv_path)
            os.replace(attempt_text_path, text_path)
        except (
            OSError,
            RequiredDataSnapshotError,
            CloseAdviceRequiredDataPlanError,
        ) as exc:
            _unlink_if_present(attempt_csv_path)
            _unlink_if_present(attempt_text_path)
            return _snapshot_integrity_failure_result(
                output_dir=output_dir,
                run_id=required_data_snapshot_run_id,
                reason="required_data_snapshot_integrity_failed",
                evidence={
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
    report_manifest = publish_close_advice_report_manifest(
        csv_path=csv_path,
        text_path=text_path,
        context_path=context_path,
        context=ctx,
        rows=rows,
        markets_to_run=markets_to_run,
        run_id=required_data_snapshot_run_id,
        quote_mode=(
            "frozen_snapshot" if frozen_mode else "legacy_mutable"
        ),
        required_data_snapshot_manifest_sha256=(
            frozen_manifest_sha256
        ),
        close_advice_required_data_plan_sha256=(
            frozen_plan_sha256
        ),
    )
    quote_issue_samples = _build_quote_issue_samples(
        coverage_positions,
        issue_reasons,
        issue_details,
        base_dir=Path(base_dir),
    )
    coverage_summary = {
        "covered_contracts": len(covered_keys),
        "positions_missing_expiration": sum(1 for reason in coverage_reasons.values() if reason == "required_data_missing_expiration"),
        "positions_missing_contract": sum(1 for reason in coverage_reasons.values() if reason == "required_data_missing_contract"),
        "expiration_near_miss_count": sum(
            1
            for detail in coverage_details.values()
            if isinstance(detail, dict) and isinstance(detail.get("expiration_near_miss"), dict)
        ),
        "coverage_fetch_attempted_symbols": int(coverage_fetch_summary.get("attempted_symbols") or 0),
        "coverage_fetch_errors": int(coverage_fetch_summary.get("errors") or 0),
    }
    frozen_requirements_validated = 0
    frozen_binding_ids: set[str] = set()
    if frozen_mode:
        for requirement in frozen_requirements_by_lot.values():
            binding = (
                requirement.get("fetch_binding")
                if isinstance(requirement.get("fetch_binding"), dict)
                else {}
            )
            binding_id = str(binding.get("binding_id") or "").strip()
            if binding_id:
                frozen_binding_ids.add(binding_id)
        for position in quote_positions:
            lot_id = str(position.get("record_id") or "").strip()
            requirement = frozen_requirements_by_lot.get(lot_id)
            key = _quote_key(
                position.get("symbol"),
                position.get("option_type"),
                _position_expiration(position),
                position.get("strike"),
                base_dir=Path(base_dir),
            )
            if (
                requirement is not None
                and str(requirement.get("planning_status") or "") == "ready"
                and all(key)
                and key in covered_keys
                and key[0] in frozen_provenance
            ):
                frozen_requirements_validated += 1

    return {
        "enabled": True,
        "status": (
            "degraded" if evaluation_gap_rows > 0 else "ok"
        ),
        "snapshot_authority": "valid",
        "quote_mode": (
            "frozen_snapshot" if frozen_mode else "legacy_mutable"
        ),
        "rows": len(rows),
        "evaluable_rows": sum(1 for row in rows if str(row.get("evaluation_status") or "").strip().lower() == "priced"),
        "evaluation_gap_rows": evaluation_gap_rows,
        "notify_rows": len(selected_notify_rows),
        "recommendation_counts": recommendation_counts,
        "evaluation_status_counts": evaluation_status_counts,
        "flag_counts": flag_counts,
        "quote_issue_rows": quote_issue_rows,
        "quote_issue_samples": quote_issue_samples,
        "coverage_summary": coverage_summary,
        "quote_fetch_diagnostics": {
            "attempted": len(attempted_fetch_details),
            "coverage_missing": len(coverage_reasons),
            "coverage_fetch_attempted_symbols": int(coverage_fetch_summary.get("attempted_symbols") or 0),
            "network_fetch_attempts": (
                0
                if frozen_mode
                else int(coverage_fetch_summary.get("attempted_symbols") or 0)
                + len(attempted_fetch_details)
            ),
            "required_data_write_attempts": (
                0
                if frozen_mode
                else int(coverage_fetch_summary.get("fetched_symbols") or 0)
            ),
            "position_requirements_total": (
                len(quote_positions) if frozen_mode else 0
            ),
            "position_requirements_planned": (
                len(frozen_requirements_by_lot) if frozen_mode else 0
            ),
            "position_requirements_validated": (
                frozen_requirements_validated if frozen_mode else 0
            ),
            "position_requirements_missing": (
                max(
                    0,
                    len(quote_positions)
                    - frozen_requirements_validated,
                )
                if frozen_mode
                else 0
            ),
            "binding_ids": (
                sorted(frozen_binding_ids) if frozen_mode else []
            ),
        },
        "quote_freshness": {
            "enforced": bool(frozen_mode or provenance_enforced),
            "authority": (
                "required_data_snapshot_manifest"
                if frozen_mode
                else "quote_cache_metadata"
            ),
            "max_age_sec": quote_max_age_sec,
            "symbols": quote_freshness_by_symbol,
        },
        "required_data_snapshot_manifest_sha256": (
            frozen_manifest_sha256
        ),
        "close_advice_required_data_plan_sha256": frozen_plan_sha256,
        "business_date": business_date.isoformat(),
        "report_manifest": report_manifest,
        "csv": str(csv_path),
        "text": str(text_path),
        "notification_text": text,
    }


def load_config(path: Path) -> dict[str, Any]:
    obj = json.loads(Path(path).read_text(encoding="utf-8"))
    return obj if isinstance(obj, dict) else {}


def run_from_paths(
    *,
    config_path: Path,
    context_path: Path,
    required_data_root: Path,
    output_dir: Path,
    base_dir: Path,
    markets_to_run: list[str] | None = None,
) -> dict[str, Any]:
    return run_close_advice(
        config=load_config(config_path),
        context_path=context_path,
        required_data_root=required_data_root,
        output_dir=output_dir,
        base_dir=base_dir,
        markets_to_run=markets_to_run,
    )
