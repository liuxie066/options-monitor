from __future__ import annotations

import csv
from collections import OrderedDict
from dataclasses import dataclass
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypedDict

import pandas as pd

from domain.domain.expiration_dates import expiration_business_today
from domain.domain.fetch_source import is_futu_fetch_source
from domain.domain.close_advice import (
    CloseAdviceConfig,
    CloseAdviceInput,
    CloseOptimizerConfig,
    HOLD_REASON_TYPE_ASSIGNMENT_ACCEPTABLE,
    HOLD_REASON_TYPE_CALLED_AWAY_ACCEPTABLE,
    LongCallConvexityConfig,
    EXIT_STATE_HOLD,
    EXIT_STATE_LET_EXPIRE,
    EXIT_STATE_NOT_EVALUABLE,
    EXIT_STATE_PROFIT_CAPTURE,
    EXIT_STATE_RISK_EXIT,
    EXIT_STATE_SALVAGE,
    EXIT_STATE_TAKE_PROFIT,
    evaluate_close_advice,
    evaluate_long_call_convexity_advice,
    evaluate_close_optimizer,
    evaluate_short_vol_close_advice,
    OPTIMIZER_TIER_LABELS,
    OPTIMIZER_TIER_PRIORITY,
    safe_float,
    safe_int,
    sort_advice_rows,
)
from domain.domain.fee_calc import calc_futu_option_fee
from src.infrastructure.io_utils import atomic_write_text, read_json, safe_read_csv
from domain.domain.ledger.position_fields import (
    effective_expiration_ymd,
    effective_multiplier,
    normalize_account,
    normalize_currency,
)
from src.application.opend_utils import normalize_underlier
from domain.domain.trade_contract_identity import (
    canonical_contract_symbol,
    contract_key,
    contract_strike_key,
    normalize_contract_expiration,
    normalize_contract_option_type,
)
from domain.domain.symbol_identity import symbol_market
from src.application.expiration_normalization import find_unique_near_miss_expiration
from src.application.opend_fetch_config import opend_fetch_kwargs
from src.application.symbol_aliases import load_runtime_symbol_aliases
from src.infrastructure.opend_retcodes import classify_opend_error
from src.application.candidate_filter_trace import (
    append_candidate_filter_trace_rows,
    build_candidate_filter_trace_row,
    candidate_trace_path_for_output,
    infer_trace_scope_from_path,
)
from src.application.events.annotator import annotate_candidates_with_event_snapshot, load_event_snapshot
from src.application.covered_call_strategy_risk import resolve_covered_call_short_vol_config
from src.application.sell_put_strategy_risk import resolve_sell_put_short_vol_config
from src.application.strategy_policy import (
    SELL_CALL_FAMILY,
    SELL_PUT_FAMILY,
    SHORT_VOL_PROFILE,
    resolve_position_strategy,
    resolve_position_strategy_semantics,
    resolve_yield_enhancement_position_role,
    strategy_side_config_for_resolution,
    yield_enhancement_mode_uses_short_vol,
)


OUTPUT_COLUMNS = [
    "account",
    "symbol",
    "option_type",
    "expiration",
    "strike",
    "contracts_open",
    "premium",
    "close_mid",
    "bid",
    "ask",
    "dte",
    "multiplier",
    "capture_ratio",
    "remaining_premium",
    "realized_if_close",
    "buy_to_close_fee",
    "sell_to_close_fee",
    "close_fee",
    "put_leg_realized_if_close",
    "combo_call_cost",
    "combo_call_value_if_close",
    "combo_net_locked_if_close_put_keep_call",
    "combo_net_if_close_both",
    "combo_cost_basis_status",
    "paired_leg_status",
    "long_call_value_ratio",
    "long_call_cost_basis",
    "long_call_current_value",
    "remaining_annualized_return",
    "evaluation_status",
    "quote_status",
    "tier",
    "tier_label",
    "reason",
    "exit_state",
    "exit_reason_type",
    "hold_reason_type",
    "close_action",
    "optional_combo_action",
    "strategy_exit_mode",
    "strategy",
    "leg_role",
    "strategy_group_id",
    "yield_enhancement_mode",
    "position_side",
    "strategy_family",
    "strategy_profile",
    "strategy_source",
    "strategy_config_path",
    "risk_model",
    "close_advice_profile",
    "close_requires_rv",
    "short_vol_thesis_status",
    "short_vol_reason",
    "short_vol_mode",
    "short_gamma_profile",
    "short_vega_profile",
    "implied_volatility",
    "realized_volatility_estimate",
    "iv_rv_ratio",
    "iv_minus_rv",
    "abs_delta",
    "equity_delta_equivalent",
    "delta_target_score",
    "vol_edge_score",
    "event_risk_flag",
    "event_risk_types",
    "event_risk_dates",
    "event_source_status",
    "event_source_error",
    "path_stress_status",
    "path_stress_evaluable",
    "path_stress_unavailable_reason",
    "stress_sigma_move_pct",
    "put_stress_down_loss_nav_pct",
    "put_gap_down_loss_nav_pct",
    "call_gap_up_opportunity_cost_nav_pct",
    "call_gap_up_opportunity_cost_to_premium",
    "data_quality_flags",
    "optimizer_tier",
    "optimizer_reason",
    "effective_annualized_return",
    "tail_risk_score",
    "risk_adjusted_return",
    "switch_value_ratio",
    "alternative_annualized_return",
    "alternative_symbol",
    "alternative_contract_symbol",
    "alternative_option_type",
    "alternative_expiration",
    "alternative_strike",
    "alternative_source_path",
    "delta",
    "otm_pct",
]

QUOTE_ISSUE_FLAGS = {
    "missing_quote",
    "missing_mid",
    "mid_fallback_last_price",
    "required_data_missing_expiration",
    "required_data_missing_contract",
    "required_data_fetch_error",
    "required_data_fetch_error_rate_limit",
    "required_data_fetch_skipped_non_futu_source",
    "opend_fetch_error",
    "opend_fetch_no_usable_quote",
    "spread_too_wide",
    "invalid_spread",
}
ACTIONABLE_CLOSE_TIERS = {"strong", "medium", "weak", "optional", "optimizer_close", "optimizer_switch"}
EVENT_SOURCE_COLUMNS = (
    "event_flag",
    "event_types",
    "event_dates",
    "event_source_status",
    "event_source_error",
)
CLOSE_ACTION_MODE_STANDARD_SHORT_OPTION = "standard_short_option"
CLOSE_ACTION_MODE_YIELD_ENHANCEMENT_PUT_LEG = "yield_enhancement_put_leg"
CLOSE_ACTION_MODE_YIELD_ENHANCEMENT_LONG_CALL_LEG = "yield_enhancement_long_call_leg"


class _PositionFetchSpec(TypedDict):
    symbol: str
    requested_keys: set[tuple[str, str, str, str]]
    requested_expirations: set[str]
    option_types: set[str]
    strikes: list[float]
    short_vol_keys: set[tuple[str, str, str, str]]


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


@dataclass(frozen=True)
class _CloseActionPolicy:
    exit_mode: str
    apply: Callable[[dict[str, Any]], dict[str, Any]]


def _norm_symbol(value: Any, *, base_dir: Path | None = None) -> str:
    aliases = load_runtime_symbol_aliases(base_dir) if base_dir is not None else None
    return canonical_contract_symbol(value, symbol_aliases=aliases)


def _norm_option_type(value: Any) -> str:
    return normalize_contract_option_type(value, fallback_raw=True)


def _market_for_symbol(symbol: Any) -> str:
    return symbol_market(symbol) or ""


def normalize_expiration(value: Any) -> str | None:
    return normalize_contract_expiration(value, fallback_raw=True)


def _strike_key(value: Any) -> str:
    return contract_strike_key(value)


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
        for _, row0 in df.iterrows():
            row = row0.to_dict()
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
        for _, row0 in df.iterrows():
            row = row0.to_dict()
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
    has_usable_bid_ask = bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid
    if has_usable_bid_ask:
        return True
    mid = _quote_number(quote.get("mid"))
    if mid is None:
        return False
    last_price = _quote_number(quote.get("last_price"))
    if last_price is not None and abs(float(mid) - float(last_price)) < 0.000001:
        return False
    return True


_SHORT_VOL_RV_FIELDS = (
    "realized_volatility_estimate",
    "rv_estimate",
    "rv_est",
    "rv_60",
    "realized_volatility_60",
)


def _quote_has_numeric_field(quote: dict[str, Any] | None, *keys: str) -> bool:
    if not isinstance(quote, dict):
        return False
    return any(_quote_number(quote.get(key)) is not None for key in keys)


def _short_vol_source_missing_fields(quote: dict[str, Any] | None) -> list[str]:
    if not isinstance(quote, dict):
        return ["quote"]
    missing: list[str] = []
    if not _quote_has_numeric_field(quote, "implied_volatility"):
        missing.append("iv")
    if not _quote_has_numeric_field(quote, *_SHORT_VOL_RV_FIELDS):
        missing.append("rv")
    if not _quote_has_numeric_field(quote, "delta"):
        missing.append("delta")
    return missing


def _position_requires_short_vol_source_data(pos: dict[str, Any], config: dict[str, Any] | None) -> bool:
    if not isinstance(pos, dict) or _is_yield_enhancement_long_call_position(pos):
        return False
    try:
        _resolution, semantics = resolve_position_strategy_semantics(position=pos, config=config)
        return bool(semantics.close_requires_rv)
    except Exception:
        return False


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
                "short_vol_keys": set[tuple[str, str, str, str]](),
            }
            specs[sym] = new_item
            item = new_item
        item["requested_keys"].add(key)
        item["requested_expirations"].add(key[2])
        item["option_types"].add(key[1])
        if _position_requires_short_vol_source_data(pos, config):
            item["short_vol_keys"].add(key)
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
    current_quotes = load_required_data_quotes(required_data_root, symbols=set(specs), base_dir=base_dir)
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
            short_vol_keys = set(spec["short_vol_keys"])
            missing_keys = [key for key in requested_keys if key not in current_covered]
            incomplete_short_vol_keys = [
                key
                for key in requested_keys
                if key in current_covered
                and key in short_vol_keys
                and _short_vol_source_missing_fields(current_quotes.get(key))
            ]
            refresh_keys = list(dict.fromkeys([*missing_keys, *incomplete_short_vol_keys]))
            if not refresh_keys:
                continue
            summary["attempted_symbols"] += 1
            symbol_cfg = symbol_cfgs.get(symbol) or {}
            fetch_cfg = symbol_cfg.get("fetch") if isinstance(symbol_cfg, dict) else {}
            fetch_cfg = fetch_cfg if isinstance(fetch_cfg, dict) else {}
            if not is_futu_fetch_source(fetch_cfg.get("source")):
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
            endpoint = (host, int(port))
            try:
                if external_gateway:
                    shared_gw = gateway
                else:
                    shared_gw = shared_gateways.get(endpoint)
                if shared_gw is None:
                    try:
                        from src.infrastructure.futu_gateway import build_ready_futu_gateway

                        shared_gw = build_ready_futu_gateway(
                            host=host,
                            port=port,
                            is_option_chain_cache_enabled=True,
                        )
                        if not external_gateway:
                            shared_gateways[endpoint] = shared_gw
                    except Exception:
                        # Graceful degradation: fall back to per-call gateway built by fetch_symbol.
                        shared_gw = None
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
                    gateway=shared_gw,
                    include_realized_volatility=bool(short_vol_keys.intersection(refresh_keys)),
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
            current_quotes = load_required_data_quotes(required_data_root, symbols=set(specs), base_dir=base_dir)
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
    short_vol_refresh_keys: set[tuple[str, str, str, str]] = set()
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
        needs_short_vol_refresh = (
            _position_requires_short_vol_source_data(pos, config)
            and bool(_short_vol_source_missing_fields(quote))
        )
        if needs_price_refresh or needs_short_vol_refresh:
            missing_by_symbol.setdefault(key[0], []).append(pos)
            if needs_price_refresh:
                price_refresh_keys.add(key)
            if needs_short_vol_refresh:
                short_vol_refresh_keys.add(key)

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
        if not is_futu_fetch_source(fetch_cfg.get("source")):
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
                include_realized_volatility=bool(short_vol_refresh_keys.intersection(set(missing_keys))),
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
    row["tier"] = "not_evaluable"
    row["tier_label"] = "无法评估"
    row["reason"] = reason
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


def _calc_dte(expiration: str | None, quote: dict[str, Any] | None) -> int | None:
    try:
        if not expiration:
            raise ValueError("missing expiration")
        exp_date = datetime.strptime(expiration[:10], "%Y-%m-%d").date()
        return (exp_date - expiration_business_today()).days
    except Exception:
        return safe_int((quote or {}).get("dte"))


def _mid_from_quote(quote: dict[str, Any] | None) -> tuple[float | None, list[str]]:
    if not isinstance(quote, dict):
        return None, ["missing_quote"]
    bid = _quote_number(quote.get("bid"))
    ask = _quote_number(quote.get("ask"))
    has_usable_bid_ask = bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid
    mid = _quote_number(quote.get("mid"))
    if mid is not None:
        last_price = _quote_number(quote.get("last_price"))
        if not has_usable_bid_ask and last_price is not None and abs(float(mid) - float(last_price)) < 0.000001:
            return mid, ["mid_fallback_last_price"]
        return mid, []
    if has_usable_bid_ask:
        assert bid is not None and ask is not None
        return round((bid + ask) / 2, 6), ["mid_from_bid_ask"]
    last_price = _quote_number(quote.get("last_price"))
    if last_price is not None:
        return last_price, ["mid_fallback_last_price"]
    return None, ["missing_mid"]


def _position_to_input(pos: dict[str, Any], quote: dict[str, Any] | None) -> tuple[CloseAdviceInput, list[str]]:
    expiration = _position_expiration(pos)
    mid, quote_flags = _mid_from_quote(quote)
    return (
        CloseAdviceInput(
            account=normalize_account(pos.get("account")),
            symbol=_norm_symbol(pos.get("symbol")),
            option_type=_norm_option_type(pos.get("option_type")),
            side=str(pos.get("side") or "").strip().lower(),
            expiration=expiration,
            strike=safe_float(pos.get("strike")),
            contracts_open=safe_int(pos.get("contracts_open")),
            premium=_position_premium(pos),
            close_mid=mid,
            bid=safe_float((quote or {}).get("bid")),
            ask=safe_float((quote or {}).get("ask")),
            dte=_calc_dte(expiration, quote),
            multiplier=effective_multiplier(pos) or safe_float((quote or {}).get("multiplier")),
            spot=safe_float((quote or {}).get("spot")),
            currency=normalize_currency(pos.get("currency") or (quote or {}).get("currency")),
            delta=safe_float((quote or {}).get("delta")),
            otm_pct=safe_float((quote or {}).get("otm_pct")),
        ),
        quote_flags,
    )


def _evaluate_position_close_advice(
    *,
    inp: CloseAdviceInput,
    pos: dict[str, Any],
    quote: dict[str, Any] | None,
    config: dict[str, Any],
    close_cfg: CloseAdviceConfig,
) -> dict[str, Any]:
    if _is_yield_enhancement_long_call_position(pos):
        strategy_snapshot = pos.get("strategy_snapshot") if isinstance(pos.get("strategy_snapshot"), dict) else {}
        long_call_cfg_raw = (
            ((config.get("close_advice") or {}).get("long_call") if isinstance(config.get("close_advice"), dict) else None)
            if isinstance(config, dict)
            else None
        )
        row = evaluate_long_call_convexity_advice(
            inp,
            LongCallConvexityConfig.from_mapping(
                long_call_cfg_raw if isinstance(long_call_cfg_raw, dict) else None
            ),
        )
        row.update(
            {
                "strategy_family": "yield_enhancement",
                "strategy_profile": str(
                    pos.get("yield_enhancement_mode")
                    or strategy_snapshot.get("yield_enhancement_mode")
                    or ""
                ).strip(),
                "strategy_source": "position_snapshot",
                "strategy_config_path": None,
                "risk_model": "long_call_convexity",
                "close_advice_profile": "yield_enhancement_long_call",
                "close_requires_rv": False,
            }
        )
        row.update(_position_strategy_metadata(pos))
        return row

    resolution, semantics = resolve_position_strategy_semantics(position=pos, config=config)
    if semantics.close_uses_short_vol_thesis:
        side_cfg = strategy_side_config_for_resolution(
            resolution=resolution,
            position=pos,
            config=config,
        )
        if resolution.strategy_family == SELL_PUT_FAMILY:
            short_vol_cfg = resolve_sell_put_short_vol_config(side_cfg)
            row = evaluate_short_vol_close_advice(
                inp,
                short_vol_config=short_vol_cfg,
                close_config=close_cfg,
                quote_row=quote,
                mode="put",
            )
        elif resolution.strategy_family == SELL_CALL_FAMILY:
            short_vol_cfg = resolve_covered_call_short_vol_config(side_cfg)
            row = evaluate_short_vol_close_advice(
                inp,
                short_vol_config=short_vol_cfg,
                close_config=close_cfg,
                quote_row=quote,
                mode="call",
            )
        else:
            row = evaluate_close_advice(inp, close_cfg)
    else:
        row = evaluate_close_advice(inp, close_cfg)
    row.update(resolution.to_fields())
    row.update(
        {
            "close_advice_profile": semantics.close_advice_profile,
            "close_requires_rv": bool(semantics.close_requires_rv),
        }
    )
    row.update(_position_strategy_metadata(pos))
    return row


def _is_yield_enhancement_long_call_position(pos: dict[str, Any]) -> bool:
    return resolve_yield_enhancement_position_role(pos).is_yield_enhancement_long_call


def _position_strategy_metadata(pos: dict[str, Any]) -> dict[str, Any]:
    strategy_snapshot = pos.get("strategy_snapshot") if isinstance(pos.get("strategy_snapshot"), dict) else {}
    return {
        "strategy": str(pos.get("strategy") or strategy_snapshot.get("strategy") or "").strip(),
        "leg_role": str(pos.get("leg_role") or strategy_snapshot.get("leg_role") or "").strip(),
        "strategy_group_id": str(pos.get("strategy_group_id") or strategy_snapshot.get("strategy_group_id") or "").strip(),
        "yield_enhancement_mode": str(
            pos.get("yield_enhancement_mode")
            or strategy_snapshot.get("yield_enhancement_mode")
            or ""
        ).strip(),
        "position_side": str(pos.get("side") or "").strip().lower(),
    }


def _resolve_event_snapshot_path(
    *,
    config: dict[str, Any],
    base_dir: Path,
    output_dir: Path,
) -> Path | None:
    runtime = config.get("runtime") if isinstance(config, dict) and isinstance(config.get("runtime"), dict) else {}
    raw = str(runtime.get("event_snapshot_path") or "").strip()
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (Path(base_dir) / path).resolve()
        return path

    search_roots = [Path(output_dir).resolve(), *Path(output_dir).resolve().parents]
    for root in search_roots:
        for candidate in (root / "state" / "event_snapshot.json", root / "event_snapshot.json"):
            if candidate.exists():
                return candidate.resolve()

    shared_candidate = (Path(base_dir) / "output_shared" / "state" / "event_snapshot.json").resolve()
    if shared_candidate.exists():
        return shared_candidate
    return None


def _event_risk_cfg_for_position(
    *,
    pos: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    default = {"enabled": True, "mode": "warn"}
    try:
        resolution = resolve_position_strategy(position=pos, config=config)
        side_cfg = strategy_side_config_for_resolution(
            resolution=resolution,
            position=pos,
            config=config,
        )
    except Exception:
        side_cfg = {}
    raw = side_cfg.get("event_risk") if isinstance(side_cfg, dict) else None
    if not isinstance(raw, dict):
        return default
    out = dict(default)
    out.update(raw)
    out["enabled"] = bool(out.get("enabled", True))
    out["mode"] = str(out.get("mode") or "warn").strip().lower() or "warn"
    return out


def _merge_event_snapshot_for_short_vol_positions(
    *,
    config: dict[str, Any],
    positions: list[dict[str, Any]],
    quotes: dict[tuple[str, str, str, str], dict[str, Any]],
    base_dir: Path,
    output_dir: Path,
) -> None:
    snapshot_path = _resolve_event_snapshot_path(config=config, base_dir=base_dir, output_dir=output_dir)
    snapshot = load_event_snapshot(snapshot_path)
    for pos in positions:
        if not isinstance(pos, dict) or not _position_requires_short_vol_source_data(pos, config):
            continue
        key = _quote_key(pos.get("symbol"), pos.get("option_type"), _position_expiration(pos), pos.get("strike"), base_dir=base_dir)
        if not all(key):
            continue
        if key not in quotes:
            continue
        quote = dict(quotes.get(key) or {})
        if snapshot_path is None and str(quote.get("event_source_status") or "").strip():
            quotes[key] = quote
            continue
        quote.setdefault("symbol", key[0])
        quote.setdefault("option_type", key[1])
        quote.setdefault("expiration", key[2])
        quote.setdefault("strike", key[3])
        annotated = annotate_candidates_with_event_snapshot(
            pd.DataFrame([quote]),
            snapshot=snapshot,
            event_risk_cfg=_event_risk_cfg_for_position(pos=pos, config=config),
        )
        if annotated.empty:
            continue
        event_row = annotated.iloc[0].to_dict()
        for col in EVENT_SOURCE_COLUMNS:
            quote[col] = event_row.get(col)
        quotes[key] = quote


def _is_yield_enhancement_short_put(row: dict[str, Any]) -> bool:
    return resolve_yield_enhancement_position_role(row).is_yield_enhancement_short_put


def _is_yield_enhancement_long_call(row: dict[str, Any]) -> bool:
    return resolve_yield_enhancement_position_role(row).is_yield_enhancement_long_call


def _is_actionable_close(row: dict[str, Any]) -> bool:
    exit_state = str(row.get("exit_state") or "").strip().lower()
    if exit_state in {EXIT_STATE_PROFIT_CAPTURE, EXIT_STATE_RISK_EXIT}:
        return True
    return str(row.get("tier") or "").strip().lower() in ACTIONABLE_CLOSE_TIERS


def _has_complete_yield_enhancement_combo_close(row: dict[str, Any]) -> bool:
    status = str(row.get("combo_cost_basis_status") or "").strip().lower()
    paired = str(row.get("paired_leg_status") or "").strip().lower()
    return (
        paired == "paired"
        and status == "ok"
        and safe_float(row.get("combo_net_if_close_both")) is not None
    )


def _apply_yield_enhancement_put_action(row: dict[str, Any]) -> dict[str, Any]:
    tier = str(row.get("tier") or "").strip().lower()
    exit_state = str(row.get("exit_state") or "").strip().lower()
    if exit_state == EXIT_STATE_NOT_EVALUABLE or tier == "not_evaluable":
        row["close_action"] = "not_evaluable"
        return row
    if _is_actionable_close(row):
        row["close_action"] = "close_put_keep_call"
        if _has_complete_yield_enhancement_combo_close(row):
            row["optional_combo_action"] = "close_both_optional"
    else:
        row["close_action"] = "hold_put_keep_call"
    return row


def _apply_yield_enhancement_long_call_action(row: dict[str, Any]) -> dict[str, Any]:
    tier = str(row.get("tier") or "").strip().lower()
    exit_state = str(row.get("exit_state") or "").strip().lower()
    if exit_state == EXIT_STATE_NOT_EVALUABLE or tier == "not_evaluable":
        row["close_action"] = "not_evaluable"
    elif exit_state == EXIT_STATE_TAKE_PROFIT:
        row["close_action"] = "sell_call_take_profit"
    elif exit_state == EXIT_STATE_SALVAGE:
        row["close_action"] = "sell_call_salvage"
    elif exit_state == EXIT_STATE_LET_EXPIRE:
        row["close_action"] = "hold_to_expiry_or_expire"
    elif yield_enhancement_mode_uses_short_vol(row.get("yield_enhancement_mode")):
        row["close_action"] = "hold_call_as_convexity"
    else:
        row["close_action"] = "hold_call"
    return row


def _apply_standard_short_option_action(row: dict[str, Any]) -> dict[str, Any]:
    tier = str(row.get("tier") or "").strip().lower()
    exit_state = str(row.get("exit_state") or "").strip().lower()
    if exit_state == EXIT_STATE_NOT_EVALUABLE or tier == "not_evaluable":
        row["close_action"] = "not_evaluable"
        return row
    row["close_action"] = "close" if _is_actionable_close(row) else "hold"
    return row


_CLOSE_ACTION_POLICY_REGISTRY: dict[str, _CloseActionPolicy] = {
    CLOSE_ACTION_MODE_YIELD_ENHANCEMENT_PUT_LEG: _CloseActionPolicy(
        exit_mode=CLOSE_ACTION_MODE_YIELD_ENHANCEMENT_PUT_LEG,
        apply=_apply_yield_enhancement_put_action,
    ),
    CLOSE_ACTION_MODE_YIELD_ENHANCEMENT_LONG_CALL_LEG: _CloseActionPolicy(
        exit_mode=CLOSE_ACTION_MODE_YIELD_ENHANCEMENT_LONG_CALL_LEG,
        apply=_apply_yield_enhancement_long_call_action,
    ),
    CLOSE_ACTION_MODE_STANDARD_SHORT_OPTION: _CloseActionPolicy(
        exit_mode=CLOSE_ACTION_MODE_STANDARD_SHORT_OPTION,
        apply=_apply_standard_short_option_action,
    ),
}


def _resolve_close_action_policy(row: dict[str, Any]) -> _CloseActionPolicy:
    if _is_yield_enhancement_short_put(row):
        return _CLOSE_ACTION_POLICY_REGISTRY[CLOSE_ACTION_MODE_YIELD_ENHANCEMENT_PUT_LEG]
    if _is_yield_enhancement_long_call(row):
        return _CLOSE_ACTION_POLICY_REGISTRY[CLOSE_ACTION_MODE_YIELD_ENHANCEMENT_LONG_CALL_LEG]
    return _CLOSE_ACTION_POLICY_REGISTRY[CLOSE_ACTION_MODE_STANDARD_SHORT_OPTION]


def _apply_close_action_semantics(row: dict[str, Any]) -> dict[str, Any]:
    policy = _resolve_close_action_policy(row)
    row["strategy_exit_mode"] = policy.exit_mode
    return policy.apply(row)


def _apply_buy_to_close_fee(row: dict[str, Any]) -> dict[str, Any]:
    mid = safe_float(row.get("close_mid"))
    contracts = safe_int(row.get("contracts_open")) or 1
    if mid is None:
        return row
    multiplier = safe_int(row.get("multiplier"))
    if multiplier is None or multiplier <= 0:
        return _with_extra_flags(row, ["fee_calc_unavailable"])
    is_long_close = str(row.get("position_side") or "").strip().lower() == "long"
    try:
        fee = calc_futu_option_fee(
            row.get("currency"),
            mid,
            contracts=contracts,
            multiplier=multiplier,
            is_sell=is_long_close,
        )
    except Exception:
        return _with_extra_flags(row, ["fee_calc_unavailable"])
    realized = safe_float(row.get("realized_if_close"))
    if realized is not None:
        row["realized_if_close"] = realized - float(fee)
    row["close_fee"] = float(fee)
    if is_long_close:
        row["sell_to_close_fee"] = float(fee)
    else:
        row["buy_to_close_fee"] = float(fee)
    return row


def _apply_fee_profitability_gate(row: dict[str, Any]) -> dict[str, Any]:
    realized = safe_float(row.get("realized_if_close"))
    if realized is None:
        return row
    if str(row.get("exit_reason_type") or "").strip().lower() == EXIT_STATE_RISK_EXIT:
        status = str(row.get("short_vol_thesis_status") or "").strip().lower()
        option_type = str(row.get("option_type") or "").strip().lower()
        if status == "event_risk" and option_type in {"put", "call"} and realized <= 0:
            if option_type == "call":
                reason = (
                    "到期前存在事件风险；Covered Call 默认可被行权卖出正股，"
                    "扣除平仓手续费后买回为亏损，作为风险观察，不作为平仓提醒"
                )
                hold_reason_type = HOLD_REASON_TYPE_CALLED_AWAY_ACCEPTABLE
            else:
                reason = (
                    "到期前存在事件风险；Sell Put 默认可接货，扣除平仓手续费后买回为亏损，"
                    "作为风险观察，不作为平仓提醒"
                )
                hold_reason_type = HOLD_REASON_TYPE_ASSIGNMENT_ACCEPTABLE
            row = _with_extra_flags(row, ["risk_exit_loss_not_actionable"])
            row["tier"] = "none"
            row["tier_label"] = "不提醒"
            row["reason"] = reason
            row["short_vol_reason"] = row["reason"]
            row["hold_reason_type"] = hold_reason_type
            row["exit_state"] = EXIT_STATE_HOLD
            row["exit_reason_type"] = EXIT_STATE_HOLD
            return row
        if status == "event_risk" or realized > 0:
            return row
        row = _with_extra_flags(row, ["risk_exit_loss_not_actionable"])
        row["tier"] = "none"
        row["tier_label"] = "不提醒"
        row["reason"] = (
            f"{row.get('reason') or 'short-vol 风险退出信号'}，但扣除平仓手续费后买回为亏损，"
            "未达到风险止损条件，不作为平仓提醒"
        )
        row["exit_state"] = EXIT_STATE_HOLD
        row["exit_reason_type"] = EXIT_STATE_HOLD
        return row
    if str(row.get("position_side") or "").strip().lower() == "long":
        return row
    if str(row.get("tier") or "").strip().lower() == "none":
        return row
    if realized > 0:
        return row
    row = _with_extra_flags(row, ["not_profitable_after_fee"])
    row["tier"] = "none"
    row["tier_label"] = "不提醒"
    row["reason"] = "扣除平仓手续费后已无正收益，不建议作为收益型买回提醒"
    row["exit_state"] = EXIT_STATE_HOLD
    row["exit_reason_type"] = EXIT_STATE_HOLD
    return row


def _strategy_group_id(row: dict[str, Any]) -> str:
    return str(row.get("strategy_group_id") or "").strip()


def _gross_leg_value(row: dict[str, Any], price_key: str) -> float | None:
    price = safe_float(row.get(price_key))
    multiplier = safe_float(row.get("multiplier"))
    contracts = safe_int(row.get("contracts_open"))
    if price is None or multiplier is None or contracts is None:
        return None
    if multiplier <= 0 or contracts <= 0:
        return None
    return price * multiplier * contracts


def _apply_yield_enhancement_combo_economics(rows: list[dict[str, Any]]) -> None:
    calls_by_group: dict[str, dict[str, Any]] = {}
    for row in rows:
        group_id = _strategy_group_id(row)
        if not group_id or not _is_yield_enhancement_long_call(row):
            continue
        calls_by_group.setdefault(group_id, row)

    for row in rows:
        if not _is_yield_enhancement_short_put(row):
            continue
        row["put_leg_realized_if_close"] = safe_float(row.get("realized_if_close"))
        group_id = _strategy_group_id(row)
        if not group_id:
            row["combo_cost_basis_status"] = "missing_strategy_group_id"
            row["paired_leg_status"] = "missing"
            continue
        call = calls_by_group.get(group_id)
        if call is None:
            row["combo_cost_basis_status"] = "missing_paired_call"
            row["paired_leg_status"] = "missing"
            continue

        row["paired_leg_status"] = "paired"
        call_cost = _gross_leg_value(call, "premium")
        call_value = _gross_leg_value(call, "close_mid")
        call_fee = safe_float(call.get("sell_to_close_fee")) or safe_float(call.get("close_fee")) or 0.0
        put_realized = safe_float(row.get("put_leg_realized_if_close"))
        row["combo_call_cost"] = call_cost
        row["combo_call_value_if_close"] = (
            call_value - call_fee if call_value is not None else None
        )

        if call_cost is None:
            row["combo_cost_basis_status"] = "missing_call_cost"
            continue
        if put_realized is None:
            row["combo_cost_basis_status"] = "missing_put_realized"
            continue

        row["combo_net_locked_if_close_put_keep_call"] = put_realized - call_cost
        if call_value is None:
            row["combo_cost_basis_status"] = "missing_call_quote"
            continue
        row["combo_net_if_close_both"] = put_realized - call_cost + call_value - call_fee
        row["combo_cost_basis_status"] = "ok"


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


def _selected_notify_rows(rows: list[dict[str, Any]], *, notify_levels: set[str], max_items: int) -> list[dict[str, Any]]:
    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for row in sort_advice_rows(rows):
        if str(row.get("evaluation_status") or "priced").strip().lower() != "priced":
            continue
        tier = str(row.get("tier") or "").strip().lower()
        if tier not in notify_levels and tier not in ("optimizer_switch", "optimizer_close"):
            continue
        acct = _row_account(row.get("account"))
        grouped.setdefault(acct, []).append(row)
    selected: list[dict[str, Any]] = []
    for acct_rows in grouped.values():
        if max_items > 0:
            selected.extend(acct_rows[:max_items])
        else:
            selected.extend(acct_rows)
    return selected


def _selected_evaluation_gap_rows(rows: list[dict[str, Any]], *, max_items: int) -> list[dict[str, Any]]:
    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for row in sort_advice_rows(rows):
        if str(row.get("evaluation_status") or "").strip().lower() == "priced":
            continue
        acct = _row_account(row.get("account"))
        grouped.setdefault(acct, []).append(row)
    selected: list[dict[str, Any]] = []
    for acct_rows in grouped.values():
        if max_items > 0:
            selected.extend(acct_rows[:max_items])
        else:
            selected.extend(acct_rows)
    return selected


def _gap_reason_label(row: dict[str, Any]) -> str:
    flags = [x for x in str(row.get("data_quality_flags") or "").split(";") if x]
    mapping = {
        "required_data_missing_expiration": "缺少到期日覆盖",
        "required_data_missing_contract": "缺少合约覆盖",
        "required_data_fetch_error": "补拉持仓覆盖失败",
        "required_data_fetch_error_rate_limit": "OpenD 限频",
        "required_data_fetch_skipped_non_futu_source": "非 Futu 行情源，无法补拉持仓覆盖",
        "opend_fetch_no_usable_quote": "无可用报价",
        "opend_fetch_error_rate_limit": "OpenD 限频",
        "opend_fetch_error_retry_budget": "OpenD 重试预算耗尽",
        "opend_fetch_error": "OpenD 拉取失败",
        "missing_quote": "缺少报价",
        "missing_mid": "缺少可用定价",
        "mid_fallback_last_price": "只有最近成交价，缺少可用 bid/ask",
        "spread_too_wide": "价差过宽",
        "invalid_spread": "价差无效",
        "short_vol_risk_data_missing": "缺少 short-vol 风险数据",
        "event_source_unavailable": "事件风险数据源不可用",
    }
    if "short_vol_risk_data_missing" in flags:
        return str(row.get("reason") or mapping["short_vol_risk_data_missing"]).strip()
    if "event_source_unavailable" in flags:
        err = str(row.get("event_source_error") or "").strip()
        base = str(row.get("reason") or mapping["event_source_unavailable"]).strip()
        return f"{base}: {err}" if err else base
    for flag in ("required_data_fetch_error_rate_limit", "opend_fetch_error_rate_limit"):
        if flag in flags:
            return mapping[flag]
    for flag in flags:
        if flag in mapping:
            return mapping[flag]
    return str(row.get("reason") or "无法评估").strip() or "无法评估"


def _optimizer_detail_lines(row: dict[str, Any]) -> list[str]:
    opt_tier = str(row.get("optimizer_tier") or "").strip()
    if opt_tier in ("defer", ""):
        return []
    lines: list[str] = []
    eff_ann = _pct(row.get("effective_annualized_return"))
    tail = row.get("tail_risk_score")
    tail_str = f"{tail:.3f}" if isinstance(tail, (int, float)) else "-"
    if opt_tier == "optimizer_switch":
        alt_ann = _pct(row.get("alternative_annualized_return"))
        alt_label = _alternative_candidate_label(row)
        alt_text = f"替代候选={alt_label} 年化={alt_ann}" if alt_label else f"替代候选年化={alt_ann}"
        lines.append(
            f"- 优化器: 持有年化={eff_ann} → {alt_text} | 尾部风险={tail_str}"
        )
    elif opt_tier == "optimizer_close":
        lines.append(f"- 优化器: 持有年化={eff_ann} | 尾部风险={tail_str} | 无可替换候选")
    elif opt_tier == "optimizer_hold":
        risk_adj = _pct(row.get("risk_adjusted_return"))
        delta_val = row.get("delta")
        delta_str = f"{delta_val:.2f}" if isinstance(delta_val, (int, float)) else "-"
        lines.append(f"- 优化器: 风险调整收益={risk_adj} | delta={delta_str} | 继续持有")
    return lines


def _close_action_label(row: dict[str, Any]) -> str:
    if _is_risk_exit_display_row(row):
        realized = safe_float(row.get("realized_if_close"))
        return "风险止损" if realized is not None and realized < 0 else "风险平仓"
    action = str(row.get("close_action") or "").strip().lower()
    mapping = {
        "close_put_keep_call": "买回 Put，保留收益增强 Call",
        "hold_put_keep_call": "继续持有 Put，保留收益增强 Call",
        "sell_call_take_profit": "卖出收益增强 Call 止盈",
        "hold_call": "继续持有收益增强 Call",
        "hold_call_as_convexity": "继续持有收益增强 Call 凸性腿",
        "sell_call_salvage": "卖出收益增强 Call 回收残值",
        "hold_to_expiry_or_expire": "保留至到期或允许归零",
        "close_both_optional": "可选组合止盈",
        "close": "平仓",
        "hold": "持有观察",
        "not_evaluable": "无法评估",
    }
    label = mapping.get(action)
    if not label:
        return str(row.get("tier_label") or "-")
    optional = str(row.get("optional_combo_action") or "").strip().lower()
    if action == "close_put_keep_call" and optional == "close_both_optional":
        return f"{label}；组合止盈可选"
    return label


def _close_tier_label_display(row: dict[str, Any]) -> str:
    if _is_risk_exit_display_row(row):
        tier = str(row.get("tier") or "").strip().lower()
        if tier == "strong":
            return "高优先级风险退出"
        if tier == "medium":
            return "中优先级风险退出"
        return "风险退出"
    return str(row.get("tier_label") or "-")


def render_markdown(rows: list[dict[str, Any]], *, notify_levels: set[str], max_items: int) -> str:
    selected = _selected_notify_rows(rows, notify_levels=notify_levels, max_items=max_items)
    gap_rows = _selected_evaluation_gap_rows(rows, max_items=max_items)
    if not selected and not gap_rows:
        return ""

    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for row in selected:
        acct = _row_account(row.get("account"))
        grouped.setdefault(acct, []).append(row)
    gap_grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for row in gap_rows:
        acct = _row_account(row.get("account"))
        gap_grouped.setdefault(acct, []).append(row)

    lines: list[str] = []
    for acct in list(grouped.keys()) + [x for x in gap_grouped.keys() if x not in grouped]:
        acct_rows = grouped.get(acct) or []
        acct_gap_rows = gap_grouped.get(acct) or []
        if lines:
            lines.append("")
        lines.append(f"### [{acct}] 平仓建议")
        if acct_rows:
            for row in acct_rows:
                opt = "Put" if str(row.get("option_type")) == "put" else "Call"
                exp = row.get("expiration") or "-"
                strike = _num(row.get("strike"))
                suffix = "P" if opt == "Put" else "C"
                currency = row.get("currency")
                lines.extend(
                    [
                        f"- {row.get('symbol')} {opt} {exp} {strike}{suffix} · {_close_action_label(row)} · {_close_tier_label_display(row)}",
                        (
                            _long_call_metric_line(row)
                            if _is_long_call_convexity_display_row(row)
                            else (
                                f"- 风险: {_short_vol_status_label(row)} | "
                                f"剩余DTE={row.get('dte') if row.get('dte') is not None else '-'} | "
                                f"剩余收益年化={_pct(row.get('remaining_annualized_return'))}"
                            )
                            if _is_risk_exit_display_row(row)
                            else (
                                f"- 已锁定: {_pct(row.get('capture_ratio'))} | "
                                f"剩余DTE={row.get('dte') if row.get('dte') is not None else '-'} | "
                                f"剩余收益年化={_pct(row.get('remaining_annualized_return'))}"
                            )
                        ),
                        (
                            _long_call_price_line(row, currency)
                            if _is_long_call_convexity_display_row(row)
                            else f"- 价格: 开仓权利金={_num(row.get('premium'))} | 平仓 mid={_num(row.get('close_mid'))}"
                        ),
                        (
                            f"- 估算: 平仓后锁定收益 {_money(row.get('realized_if_close'), currency)} | "
                            f"剩余权利金 {_money(row.get('remaining_premium'), currency)}"
                            if not _is_risk_exit_display_row(row)
                            else f"- 估算: 平仓损益 {_money(row.get('realized_if_close'), currency)} | "
                            f"剩余权利金 {_money(row.get('remaining_premium'), currency)}"
                        ),
                        f"- 理由: {row.get('reason') or '-'}",
                        *_optimizer_detail_lines(row),
                        "---",
                    ]
                )
        else:
            lines.append("- 本次无 strong/medium 平仓建议")
        if acct_gap_rows:
            lines.append("- 待补数据:")
            for row in acct_gap_rows:
                opt = "Put" if str(row.get("option_type")) == "put" else "Call"
                exp = row.get("expiration") or "-"
                strike = _num(row.get("strike"))
                suffix = "P" if opt == "Put" else "C"
                lines.append(
                    f"- {row.get('symbol')} {opt} {exp} {strike}{suffix} · 无法评估 | {_gap_reason_label(row)}"
                )
    return "\n".join(lines).strip() + "\n"


def _tier_emoji_compact(tier: str) -> str:
    tier_map = {
        "optimizer_switch": "🔴",
        "optimizer_close": "🟠",
        "optimizer_hold": "🟢",
        "strong": "🔴",
        "medium": "🟠",
        "weak": "🟡",
        "optional": "⚪",
        "defer": "⚪",
        "none": "⚪",
    }
    return tier_map.get(str(tier).strip().lower(), "⚪")


def _tier_verb_compact(tier: str) -> str:
    verb_map = {
        "optimizer_switch": "换仓",
        "optimizer_close": "平仓",
        "optimizer_hold": "持有",
        "strong": "强烈平仓",
        "medium": "建议平仓",
        "weak": "考虑平仓",
        "optional": "可选平仓",
        "defer": "观察",
        "none": "观察",
    }
    return verb_map.get(str(tier).strip().lower(), "评估")


def _is_risk_exit_display_row(row: dict[str, Any]) -> bool:
    return str(row.get("exit_state") or "").strip().lower() == EXIT_STATE_RISK_EXIT


def _short_vol_status_label(row: dict[str, Any]) -> str:
    status = str(row.get("short_vol_thesis_status") or "").strip().lower()
    mapping = {
        "event_risk": "事件风险",
        "vol_edge_lost": "IV/RV edge丢失",
        "vol_edge_weakened": "IV/RV edge转弱",
        "delta_risk_high": "delta风险升高",
    }
    return mapping.get(status, "风险退出")


def _risk_exit_verb_compact(row: dict[str, Any], fallback: str) -> str:
    realized = safe_float(row.get("realized_if_close"))
    if realized is not None and realized < 0:
        return "风险止损"
    return "风险平仓" if fallback in {"强烈平仓", "建议平仓", "考虑平仓", "可选平仓"} else fallback


def _close_action_verb_compact(row: dict[str, Any], fallback: str) -> str:
    if _is_risk_exit_display_row(row):
        return _risk_exit_verb_compact(row, fallback)
    action = str(row.get("close_action") or "").strip().lower()
    mapping = {
        "close_put_keep_call": "买回Put留Call",
        "hold_put_keep_call": "持有Put留Call",
        "sell_call_take_profit": "卖Call止盈",
        "hold_call": "持有Call",
        "hold_call_as_convexity": "持有凸性Call",
        "sell_call_salvage": "卖Call残值",
        "hold_to_expiry_or_expire": "允许Call归零",
        "close": fallback,
        "hold": fallback,
        "not_evaluable": fallback,
    }
    return mapping.get(action, fallback)


def _fmt_date_compact_ca(exp: str) -> str:
    if not exp or exp == "-":
        return ""
    try:
        from datetime import datetime
        dt = datetime.strptime(str(exp), "%Y-%m-%d")
        now = datetime.now()
        if dt.year == now.year:
            return f"@ {dt.strftime('%m-%d')}"
        return f"@ {exp}"
    except Exception:
        return f"@ {exp}"


def _pct_compact(val) -> str:
    try:
        n = float(val)
        if n >= 0.1:
            return f"{int(round(n * 100))}%"
        return f"{n * 100:.1f}%"
    except Exception:
        return str(val) if val is not None else "-"


def _signed_pct_compact(val: Any) -> str:
    n = safe_float(val)
    if n is None:
        return "-"
    sign = "+" if n > 0 else ""
    if abs(n) >= 0.1:
        return f"{sign}{int(round(n * 100))}%"
    return f"{sign}{n * 100:.1f}%"


def _ratio_x_compact(val: Any) -> str:
    n = safe_float(val)
    if n is None:
        return "-"
    if abs(n) >= 10:
        return f"{n:.0f}x"
    return f"{n:.1f}x"


def _is_long_call_convexity_display_row(row: dict[str, Any]) -> bool:
    if _is_yield_enhancement_long_call(row):
        return True
    if str(row.get("risk_model") or "").strip().lower() == "long_call_convexity":
        return True
    option_type = str(row.get("option_type") or "").strip().lower()
    side = str(row.get("position_side") or row.get("side") or "").strip().lower()
    return (
        option_type == "call"
        and side == "long"
        and safe_float(row.get("long_call_value_ratio")) is not None
    )


def _long_call_gain_ratio(row: dict[str, Any]) -> float | None:
    value_ratio = safe_float(row.get("long_call_value_ratio"))
    if value_ratio is not None:
        return value_ratio - 1.0
    realized = safe_float(row.get("realized_if_close"))
    cost = safe_float(row.get("long_call_cost_basis"))
    if realized is None or cost is None or cost <= 0:
        return None
    return realized / cost


def _long_call_metric_line(row: dict[str, Any]) -> str:
    ratio = _ratio_x_compact(row.get("long_call_value_ratio"))
    dte = row.get("dte")
    dte_text = dte if dte is not None else "-"
    gain = _signed_pct_compact(_long_call_gain_ratio(row))
    return f"- Call价值: 现值/成本={ratio} | 剩余DTE={dte_text} | 浮盈={gain}"


def _long_call_metric_line_compact(row: dict[str, Any], *, dte_str: str) -> str:
    ratio = _ratio_x_compact(row.get("long_call_value_ratio"))
    gain = _signed_pct_compact(_long_call_gain_ratio(row))
    return f"- 现值/成本 {ratio} · {dte_str} · 浮盈 {gain}"


def _risk_exit_metric_line_compact(row: dict[str, Any], *, dte_str: str) -> str:
    remaining_ann = _pct_compact(row.get("remaining_annualized_return"))
    return f"- 风险退出 {_short_vol_status_label(row)} · {dte_str} · 余年化 {remaining_ann}"


def _price_compact(val: Any, currency: str | None) -> str:
    n = safe_float(val)
    if n is None:
        return "-"
    prefix = "$" if currency == "USD" else "¥"
    if abs(n) >= 1000:
        body = f"{n:,.2f}"
    else:
        body = f"{n:.2f}"
    return f"{prefix}{body.rstrip('0').rstrip('.')}"


def _quote_range_compact(row: dict[str, Any], currency: str | None) -> str:
    bid = safe_float(row.get("bid"))
    ask = safe_float(row.get("ask"))
    if bid is not None and ask is not None:
        return f" · 买一/卖一 {_price_compact(bid, currency)}/{_price_compact(ask, currency)}"
    if bid is not None:
        return f" · 买一 {_price_compact(bid, currency)}"
    if ask is not None:
        return f" · 卖一 {_price_compact(ask, currency)}"
    return ""


def _long_call_price_line_compact(row: dict[str, Any], currency: str | None) -> str:
    suggested = _price_compact(row.get("close_mid"), currency)
    realized = _money_compact(row.get("realized_if_close"), currency)
    remaining = _money_compact(row.get("remaining_premium"), currency)
    quote_range = _quote_range_compact(row, currency)
    return f"- 建议出价 {suggested}{quote_range} · 收益 {realized}（余 {remaining}）"


def _short_option_price_line_compact(row: dict[str, Any], currency: str | None) -> str:
    close_mid = _money_compact(row.get("close_mid"), currency)
    realized = _money_compact(row.get("realized_if_close"), currency)
    remaining = _money_compact(row.get("remaining_premium"), currency)
    label = "平仓损益" if _is_risk_exit_display_row(row) else "收益"
    return f"- 建议价 {close_mid} · {label} {realized}（余 {remaining}）"


def _money_compact(val, currency: str | None) -> str:
    try:
        n = float(val)
        prefix = "$" if currency == "USD" else "¥"
        if abs(n) >= 1000:
            return f"{prefix}{n:,.0f}"
        if abs(n) >= 10:
            return f"{prefix}{n:.0f}"
        return f"{prefix}{n:.2f}"
    except Exception:
        return str(val) if val is not None else "-"


def _strike_compact(val: Any) -> str:
    try:
        n = float(val)
        if n.is_integer():
            return str(int(n))
        return f"{n:.2f}".rstrip("0").rstrip(".")
    except Exception:
        return str(val) if val is not None else "-"


def _quote_range_text(row: dict[str, Any], currency: Any) -> str:
    bid = safe_float(row.get("bid"))
    ask = safe_float(row.get("ask"))
    if bid is not None and ask is not None:
        return f"买一/卖一={_money(bid, currency)}/{_money(ask, currency)}"
    if bid is not None:
        return f"买一={_money(bid, currency)}"
    if ask is not None:
        return f"卖一={_money(ask, currency)}"
    return ""


def _long_call_price_line(row: dict[str, Any], currency: Any) -> str:
    parts = [
        f"开仓权利金={_num(row.get('premium'))}",
        f"建议出价={_money(row.get('close_mid'), currency)}",
    ]
    quote_range = _quote_range_text(row, currency)
    if quote_range:
        parts.append(quote_range)
    return "- 价格: " + " | ".join(parts)


def _optimizer_detail_compact(row: dict[str, Any]) -> str:
    opt_tier = str(row.get("optimizer_tier") or "").strip()
    if opt_tier in ("defer", ""):
        return ""
    parts = []
    eff_ann = _pct_compact(row.get("effective_annualized_return"))
    tail = row.get("tail_risk_score")
    tail_str = f"{tail:.3f}" if isinstance(tail, (int, float)) else "-"
    if opt_tier == "optimizer_switch":
        alt_ann = _pct_compact(row.get("alternative_annualized_return"))
        alt_label = _alternative_candidate_label(row)
        alt_text = f"替代 {alt_label} {alt_ann}" if alt_label else f"替代 {alt_ann}"
        parts.append(f"持有 {eff_ann} → {alt_text}")
        parts.append(f"风险 {tail_str}")
    elif opt_tier == "optimizer_close":
        parts.append(f"持有 {eff_ann}")
        parts.append(f"风险 {tail_str}")
        parts.append("无替代")
    elif opt_tier == "optimizer_hold":
        risk_adj = _pct_compact(row.get("risk_adjusted_return"))
        delta_val = row.get("delta")
        delta_str = f"{delta_val:.2f}" if isinstance(delta_val, (int, float)) else "-"
        parts.append(f"风险调整 {risk_adj}")
        parts.append(f"Δ={delta_str}")
    if parts:
        return "- 💡 " + " · ".join(parts)
    return ""


def _alternative_candidate_label(row: dict[str, Any]) -> str:
    contract = str(row.get("alternative_contract_symbol") or "").strip()
    if contract:
        return contract
    symbol = str(row.get("alternative_symbol") or "").strip().upper()
    option_type = str(row.get("alternative_option_type") or "").strip().lower()
    expiration = str(row.get("alternative_expiration") or "").strip()
    strike = row.get("alternative_strike")
    strike_text = _strike_compact(strike) if strike not in (None, "") else ""
    parts = [part for part in (symbol, option_type, expiration, strike_text) if part]
    return " ".join(parts) if parts else ""


def render_markdown_compact(
    rows: list[dict[str, Any]], *, notify_levels: set[str], max_items: int
) -> str:
    selected = _selected_notify_rows(rows, notify_levels=notify_levels, max_items=max_items)
    gap_rows = _selected_evaluation_gap_rows(rows, max_items=max_items)
    if not selected and not gap_rows:
        return ""

    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for row in selected:
        acct = _row_account(row.get("account"))
        grouped.setdefault(acct, []).append(row)
    gap_grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for row in gap_rows:
        acct = _row_account(row.get("account"))
        gap_grouped.setdefault(acct, []).append(row)

    lines: list[str] = []
    for acct in list(grouped.keys()) + [x for x in gap_grouped.keys() if x not in grouped]:
        acct_rows = grouped.get(acct) or []
        acct_gap_rows = gap_grouped.get(acct) or []
        if lines:
            lines.append("")
        lines.append(f"### [{acct}] 平仓建议 ({len(acct_rows)})")
        if acct_rows:
            for row in acct_rows:
                opt = "Put" if str(row.get("option_type")) == "put" else "Call"
                exp = row.get("expiration") or "-"
                strike = _strike_compact(row.get("strike"))
                suffix = "P" if opt == "Put" else "C"
                currency = row.get("currency")
                tier = str(row.get("tier") or "").strip().lower()
                emoji = _tier_emoji_compact(tier)
                verb = _close_action_verb_compact(row, _tier_verb_compact(tier))
                l1 = f"{emoji} {verb} {row.get('symbol')} {opt} {strike}{suffix} {_fmt_date_compact_ca(exp)}"
                is_long_call_row = _is_long_call_convexity_display_row(row)
                capture = _pct_compact(row.get("capture_ratio"))
                dte = row.get("dte")
                dte_str = f"{int(dte)}天" if dte is not None else "-"
                remaining_ann = _pct_compact(row.get("remaining_annualized_return"))
                if is_long_call_row:
                    l2 = _long_call_metric_line_compact(row, dte_str=dte_str)
                elif _is_risk_exit_display_row(row):
                    l2 = _risk_exit_metric_line_compact(row, dte_str=dte_str)
                else:
                    l2 = f"- 已锁定 {capture} · {dte_str} · 余年化 {remaining_ann}"
                if is_long_call_row:
                    l3 = _long_call_price_line_compact(row, currency)
                else:
                    l3 = _short_option_price_line_compact(row, currency)
                opt_detail = _optimizer_detail_compact(row)
                lines.append(l1)
                lines.append(l2)
                lines.append(l3)
                if opt_detail:
                    lines.append(opt_detail)
        else:
            lines.append("- 本次无 strong/medium 平仓建议")
        if acct_gap_rows:
            lines.append("- 待补数据:")
            for row in acct_gap_rows:
                opt = "Put" if str(row.get("option_type")) == "put" else "Call"
                exp = row.get("expiration") or "-"
                strike = _num(row.get("strike"))
                suffix = "P" if opt == "Put" else "C"
                lines.append(
                    f"- {row.get('symbol')} {opt} {exp} {strike}{suffix} · 无法评估 | {_gap_reason_label(row)}"
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


def _close_trace_key(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(row.get("account") or "").strip().lower(),
        str(row.get("symbol") or "").strip().upper(),
        str(row.get("option_type") or "").strip().lower(),
        str(row.get("expiration") or "").strip(),
        str(row.get("strike") or "").strip(),
    )


def _append_close_advice_filter_trace(
    *,
    csv_path: Path,
    rows: list[dict[str, Any]],
    selected_notify_rows: list[dict[str, Any]],
    notify_levels: set[str],
) -> None:
    scope = infer_trace_scope_from_path(csv_path)
    selected_keys = {_close_trace_key(row) for row in selected_notify_rows}
    trace_rows: list[dict[str, Any]] = []
    for row in rows:
        symbol = str(row.get("symbol") or "").strip()
        if not symbol:
            continue
        flags = [flag for flag in str(row.get("data_quality_flags") or "").split(";") if flag]
        evaluation_status = str(row.get("evaluation_status") or "").strip().lower()
        tier = str(row.get("tier") or "").strip().lower()
        key = _close_trace_key(row)
        if evaluation_status != "priced":
            status = "rejected"
            rule = flags[0] if flags else (evaluation_status or "close_advice_not_priced")
            message = str(row.get("reason") or "close advice row could not be priced")
        elif key in selected_keys:
            status = "notified"
            rule = "close_advice_notified"
            message = str(row.get("reason") or "close advice selected for notification")
        elif tier in notify_levels or tier in ("optimizer_switch", "optimizer_close"):
            status = "ranked_below"
            rule = "close_advice_over_max_items"
            message = str(row.get("reason") or "close advice matched notify tier but was not selected")
        else:
            status = "accepted"
            rule = f"close_advice_{tier or 'hold'}"
            message = str(row.get("reason") or "close advice evaluated")
        trace_rows.append(
            build_candidate_filter_trace_row(
                run_id=scope.get("run_id"),
                account=scope.get("account") or row.get("account"),
                symbol=symbol,
                function="close_advice",
                mode=str(row.get("option_type") or "close"),
                status=status,
                stage="close_advice",
                rule=rule,
                metric_value=row.get("effective_annualized_return") or row.get("capture_ratio"),
                threshold=None,
                expiration=row.get("expiration"),
                strike=row.get("strike"),
                message=message,
                evidence_path=csv_path.name,
                config_values={"notify_levels": sorted(notify_levels)},
            )
        )
    append_candidate_filter_trace_rows(candidate_trace_path_for_output(csv_path), trace_rows)


def _load_context(context_path: Path) -> dict[str, Any]:
    obj = read_json(context_path, default={})
    return obj if isinstance(obj, dict) else {}


def _load_alternative_redeploy_candidate_from_scan(output_dir: Path) -> dict[str, Any] | None:
    """Read the best recent Sell Put candidate as explicit redeploy evidence."""

    try:
        candidates_dir = output_dir
        if not candidates_dir.exists():
            return None
        csv_paths = sorted(
            candidates_dir.glob("*_sell_put_candidates.csv"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not csv_paths:
            return None
        df = safe_read_csv(csv_paths[0])
        if df.empty:
            return None
        best_row: dict[str, Any] | None = None
        best_return: float | None = None
        for raw_row in df.to_dict("records"):
            annualized = _candidate_annualized_return(raw_row)
            if annualized is None:
                continue
            if best_return is None or annualized > best_return:
                best_return = annualized
                best_row = raw_row if isinstance(raw_row, dict) else {}
        if best_row is None or best_return is None:
            return None
        symbol = str(best_row.get("symbol") or best_row.get("underlying_symbol") or "").strip().upper()
        contract_symbol = str(best_row.get("contract_symbol") or best_row.get("option_symbol") or "").strip()
        if not symbol and not contract_symbol:
            return None
        return {
            "alternative_annualized_return": best_return,
            "alternative_symbol": symbol,
            "alternative_contract_symbol": contract_symbol,
            "alternative_option_type": str(best_row.get("option_type") or best_row.get("mode") or "").strip().lower(),
            "alternative_expiration": str(best_row.get("expiration") or best_row.get("exp") or "").strip(),
            "alternative_strike": safe_float(best_row.get("strike")),
            "alternative_source_path": str(csv_paths[0]),
        }
    except Exception:
        return None


def _candidate_annualized_return(row: dict[str, Any]) -> float | None:
    for col in (
        "annualized_net_return_on_cash_basis",
        "annualized_net_return_on_strike",
        "annualized_return",
    ):
        if col not in row:
            continue
        parsed = safe_float(row.get(col))
        if parsed is not None:
            return float(parsed)
    return None


def run_close_advice(
    *,
    config: dict[str, Any],
    context_path: Path,
    required_data_root: Path,
    output_dir: Path,
    base_dir: Path,
    markets_to_run: list[str] | None = None,
    gateway: Any = None,
) -> dict[str, Any]:
    advice_cfg_raw = config.get("close_advice") if isinstance(config, dict) else {}
    advice_cfg = advice_cfg_raw if isinstance(advice_cfg_raw, dict) else {}
    output_dir = Path(output_dir).resolve()
    csv_path = output_dir / "close_advice.csv"
    text_path = output_dir / "close_advice.txt"

    if not bool(advice_cfg.get("enabled", False)):
        _write_csv(csv_path, [])
        atomic_write_text(text_path, "", encoding="utf-8")
        return {"enabled": False, "rows": 0, "notify_rows": 0, "csv": str(csv_path), "text": str(text_path)}

    ctx = _load_context(context_path)
    positions = ctx.get("open_positions_min") if isinstance(ctx, dict) else []
    positions = positions if isinstance(positions, list) else []
    positions = _filter_positions_by_markets(positions, markets_to_run)
    coverage_fetch_reasons, coverage_fetch_details, coverage_fetch_summary = _ensure_required_data_coverage_for_positions(
        config=config,
        positions=positions,
        required_data_root=Path(required_data_root),
        base_dir=Path(base_dir),
        gateway=gateway,
    )
    symbols = {_norm_symbol(p.get("symbol"), base_dir=Path(base_dir)) for p in positions if isinstance(p, dict) and p.get("symbol")}
    quotes = load_required_data_quotes(Path(required_data_root), symbols=symbols, base_dir=Path(base_dir))
    covered_keys, expirations_by_symbol = load_required_data_coverage(Path(required_data_root), symbols=symbols, base_dir=Path(base_dir))
    coverage_reasons, coverage_details = _classify_required_data_coverage(
        positions,
        covered_keys,
        expirations_by_symbol,
        base_dir=Path(base_dir),
    )
    attempted_fetch_reasons, attempted_fetch_details = _fetch_missing_quotes_via_opend(
        config=config,
        positions=positions,
        quotes=quotes,
        covered_keys=covered_keys,
        base_dir=Path(base_dir),
    )
    _merge_event_snapshot_for_short_vol_positions(
        config=config,
        positions=positions,
        quotes=quotes,
        base_dir=Path(base_dir),
        output_dir=output_dir,
    )
    issue_reasons = {**coverage_reasons, **coverage_fetch_reasons, **attempted_fetch_reasons}
    issue_details = {**coverage_details, **coverage_fetch_details, **attempted_fetch_details}

    cfg = CloseAdviceConfig.from_mapping(advice_cfg)
    rows: list[dict[str, Any]] = []
    evaluation_status_counts: dict[str, int] = {}
    for pos0 in positions:
        if not isinstance(pos0, dict):
            continue
        exp = _position_expiration(pos0)
        key = _quote_key(pos0.get("symbol"), pos0.get("option_type"), exp, pos0.get("strike"), base_dir=Path(base_dir))
        quote = quotes.get(key)
        inp, quote_flags = _position_to_input(pos0, quote)
        row = _evaluate_position_close_advice(
            inp=inp,
            pos=pos0,
            quote=quote,
            config=config,
            close_cfg=cfg,
        )
        row = _with_extra_flags(row, quote_flags)
        row = _with_extra_flags(row, _quote_observability_flags(key, quote, issue_reasons))
        issue_reason = str(issue_reasons.get(key) or "").strip()
        if issue_reason.startswith("required_data_"):
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
        elif "mid_fallback_last_price" in quote_flags and not _quote_has_usable_price(quote):
            row = _mark_not_evaluable(
                row,
                evaluation_status="quote_unusable",
                quote_status="quote_unusable",
                reason="持仓对应合约只有最近成交价，缺少可用 bid/ask，暂无法评估平仓建议",
            )
        elif (
            str(row.get("exit_state") or "").strip().lower() == EXIT_STATE_NOT_EVALUABLE
            or str(row.get("tier") or "").strip().lower() == "not_evaluable"
        ):
            row["evaluation_status"] = "not_evaluable"
            row["quote_status"] = "not_evaluable"
        else:
            row["evaluation_status"] = "priced"
            row["quote_status"] = "priced"
            row = _apply_buy_to_close_fee(row)
            row = _apply_fee_profitability_gate(row)
        status = str(row.get("evaluation_status") or "unknown").strip().lower() or "unknown"
        evaluation_status_counts[status] = evaluation_status_counts.get(status, 0) + 1
        rows.append(row)

    optimizer_cfg_raw = advice_cfg.get("optimizer") if isinstance(advice_cfg, dict) else {}
    optimizer_enabled = bool(
        optimizer_cfg_raw.get("enabled", True) if isinstance(optimizer_cfg_raw, dict) else True
    )
    if optimizer_enabled:
        optimizer_cfg = CloseOptimizerConfig.from_mapping(
            optimizer_cfg_raw if isinstance(optimizer_cfg_raw, dict) else None
        )
        alternative_candidate = _load_alternative_redeploy_candidate_from_scan(Path(output_dir))
        alt_annualized = (
            safe_float(alternative_candidate.get("alternative_annualized_return"))
            if isinstance(alternative_candidate, dict)
            else None
        )
        for row in rows:
            if str(row.get("evaluation_status") or "").strip().lower() != "priced":
                continue
            if str(row.get("risk_model") or "").strip().lower() == SHORT_VOL_PROFILE:
                continue
            inp = CloseAdviceInput(
                account=str(row.get("account") or ""),
                symbol=str(row.get("symbol") or ""),
                option_type=str(row.get("option_type") or ""),
                side="short",
                expiration=str(row.get("expiration") or ""),
                strike=safe_float(row.get("strike")),
                contracts_open=safe_int(row.get("contracts_open")),
                premium=safe_float(row.get("premium")),
                close_mid=safe_float(row.get("close_mid")),
                bid=safe_float(row.get("bid")),
                ask=safe_float(row.get("ask")),
                dte=safe_int(row.get("dte")),
                multiplier=safe_float(row.get("multiplier")),
                spot=safe_float(row.get("spot")),
                currency=str(row.get("currency") or ""),
                delta=safe_float(row.get("delta")),
                otm_pct=safe_float(row.get("otm_pct")),
            )
            opt_result = evaluate_close_optimizer(
                inp, optimizer_cfg,
                alternative_annualized_return=alt_annualized,
            )
            for key, val in opt_result.items():
                row[key] = val
            if alternative_candidate is not None:
                for key, val in alternative_candidate.items():
                    row[key] = val
            opt_tier = str(opt_result.get("optimizer_tier") or "")
            if opt_tier in ("optimizer_switch", "optimizer_close"):
                row["tier"] = opt_tier
                row["tier_label"] = OPTIMIZER_TIER_LABELS.get(opt_tier, opt_tier)
                row["reason"] = str(
                    opt_result.get("optimizer_reason") or row.get("reason")
                )

    _apply_yield_enhancement_combo_economics(rows)

    for row in rows:
        _apply_close_action_semantics(row)

    rows = sort_advice_rows(rows)
    notify_levels = advice_cfg.get("notify_levels") or ["strong", "medium"]
    notify_level_set = {str(x).strip().lower() for x in notify_levels if str(x).strip()}
    max_items_raw = safe_int(advice_cfg.get("max_items_per_account"))
    max_items = 5 if max_items_raw is None else max_items_raw
    render_style = str(advice_cfg.get("render_style") or "legacy").strip().lower()
    if render_style == "compact":
        text = render_markdown_compact(rows, notify_levels=notify_level_set, max_items=max_items)
    else:
        text = render_markdown(rows, notify_levels=notify_level_set, max_items=max_items)
    selected_notify_rows = _selected_notify_rows(rows, notify_levels=notify_level_set, max_items=max_items)
    flag_counts: dict[str, int] = {}
    tier_counts: dict[str, int] = {}
    quote_issue_rows = 0
    evaluation_gap_rows = 0
    for row in rows:
        if str(row.get("evaluation_status") or "").strip().lower() == "priced":
            tier = str(row.get("tier") or "").strip().lower() or "unknown"
            tier_counts[tier] = tier_counts.get(tier, 0) + 1
        else:
            evaluation_gap_rows += 1
        flags = [x for x in str(row.get("data_quality_flags") or "").split(";") if x]
        if any(flag in QUOTE_ISSUE_FLAGS for flag in flags):
            quote_issue_rows += 1
        for flag in flags:
            flag_counts[flag] = flag_counts.get(flag, 0) + 1

    _write_csv(csv_path, rows)
    try:
        _append_close_advice_filter_trace(
            csv_path=csv_path,
            rows=rows,
            selected_notify_rows=selected_notify_rows,
            notify_levels=notify_level_set,
        )
    except Exception:
        pass
    atomic_write_text(text_path, text, encoding="utf-8")
    quote_issue_samples = _build_quote_issue_samples(
        positions,
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

    return {
        "enabled": True,
        "rows": len(rows),
        "evaluable_rows": sum(1 for row in rows if str(row.get("evaluation_status") or "").strip().lower() == "priced"),
        "evaluation_gap_rows": evaluation_gap_rows,
        "notify_rows": len(selected_notify_rows),
        "tier_counts": tier_counts,
        "evaluation_status_counts": evaluation_status_counts,
        "flag_counts": flag_counts,
        "quote_issue_rows": quote_issue_rows,
        "quote_issue_samples": quote_issue_samples,
        "coverage_summary": coverage_summary,
        "quote_fetch_diagnostics": {
            "attempted": len(attempted_fetch_details),
            "coverage_missing": len(coverage_reasons),
            "coverage_fetch_attempted_symbols": int(coverage_fetch_summary.get("attempted_symbols") or 0),
        },
        "csv": str(csv_path),
        "text": str(text_path),
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
