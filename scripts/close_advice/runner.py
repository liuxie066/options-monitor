from __future__ import annotations

import csv
from collections import OrderedDict
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from domain.domain.fetch_source import is_futu_fetch_source
from domain.domain.close_advice import (
    CloseAdviceConfig,
    CloseAdviceInput,
    evaluate_close_advice,
    safe_float,
    safe_int,
    sort_advice_rows,
)
from scripts.fee_calc import calc_futu_option_fee
from scripts.io_utils import atomic_write_text, read_json, safe_read_csv
from scripts.opend_utils import normalize_underlier, resolve_underlier_alias


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
    "remaining_annualized_return",
    "evaluation_status",
    "quote_status",
    "tier",
    "tier_label",
    "reason",
    "data_quality_flags",
]

QUOTE_ISSUE_FLAGS = {
    "missing_quote",
    "missing_mid",
    "required_data_missing_expiration",
    "required_data_missing_contract",
    "required_data_fetch_error",
    "required_data_fetch_skipped_non_futu_source",
    "opend_fetch_error",
    "opend_fetch_no_usable_quote",
    "spread_too_wide",
    "invalid_spread",
}


def _norm_symbol(value: Any, *, base_dir: Path | None = None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    resolved = resolve_underlier_alias(raw, base_dir=base_dir)
    return str(resolved or raw).strip().upper()


def _norm_option_type(value: Any) -> str:
    return str(value or "").strip().lower()


def _market_for_symbol(symbol: Any) -> str:
    sym = _norm_symbol(symbol)
    if sym.endswith(".HK"):
        return "HK"
    if sym:
        return "US"
    return ""


def _date_from_ms(value: Any) -> str | None:
    try:
        if value in (None, ""):
            return None
        n = int(float(value))
        if n <= 0:
            return None
        if n > 10_000_000_000:
            return datetime.fromtimestamp(n / 1000, tz=timezone.utc).date().isoformat()
        return datetime.fromtimestamp(n, tz=timezone.utc).date().isoformat()
    except Exception:
        return None


def normalize_expiration(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if len(s) >= 10 and s[4:5] == "-" and s[7:8] == "-":
            return s[:10]
        parsed = _date_from_ms(s)
        return parsed or s
    return _date_from_ms(value)


def _strike_key(value: Any) -> str:
    v = safe_float(value)
    if v is None:
        return ""
    return f"{v:.6f}"


def _quote_key(symbol: Any, option_type: Any, expiration: Any, strike: Any, *, base_dir: Path | None = None) -> tuple[str, str, str, str]:
    return (
        _norm_symbol(symbol, base_dir=base_dir),
        _norm_option_type(option_type),
        normalize_expiration(expiration) or "",
        _strike_key(strike),
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
    if _quote_number(quote.get("mid")) is not None or _quote_number(quote.get("last_price")) is not None:
        return True
    bid = _quote_number(quote.get("bid"))
    ask = _quote_number(quote.get("ask"))
    return bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid


def _build_position_fetch_specs(
    positions: list[dict[str, Any]],
    *,
    base_dir: Path,
) -> dict[str, dict[str, Any]]:
    specs: dict[str, dict[str, Any]] = {}
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        key = _quote_key(pos.get("symbol"), pos.get("option_type"), _position_expiration(pos), pos.get("strike"), base_dir=base_dir)
        if not all(key):
            continue
        sym = key[0]
        item = specs.get(sym)
        if item is None:
            item = {
                "symbol": sym,
                "requested_keys": set(),
                "requested_expirations": set(),
                "option_types": set(),
                "strikes": [],
            }
            specs[sym] = item
        item["requested_keys"].add(key)
        item["requested_expirations"].add(key[2])
        item["option_types"].add(key[1])
        strike_num = safe_float(pos.get("strike"))
        if strike_num is not None:
            item["strikes"].append(strike_num)
    return specs


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
) -> tuple[dict[tuple[str, str, str, str], str], dict[tuple[str, str, str, str], dict[str, Any]], dict[str, Any]]:
    symbol_cfgs = _symbol_config_by_symbol(config)
    specs = _build_position_fetch_specs(positions, base_dir=base_dir)
    fetch_reasons: dict[tuple[str, str, str, str], str] = {}
    fetch_details: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    summary = {"attempted_symbols": 0, "fetched_symbols": 0, "errors": 0}
    if not specs:
        return fetch_reasons, fetch_details, summary

    current_covered, current_expirations = load_required_data_coverage(required_data_root, symbols=set(specs), base_dir=base_dir)

    try:
        from scripts.fetch_market_data_opend import fetch_symbol, save_outputs
    except Exception:
        return fetch_reasons, fetch_details, summary

    for symbol, spec in specs.items():
        requested_keys = set(spec.get("requested_keys") or set())
        requested_expirations = sorted(spec.get("requested_expirations") or set())
        missing_keys = [key for key in requested_keys if key not in current_covered]
        if not missing_keys:
            continue
        summary["attempted_symbols"] += 1
        symbol_cfg = symbol_cfgs.get(symbol) or {}
        fetch_cfg = symbol_cfg.get("fetch") if isinstance(symbol_cfg, dict) else {}
        fetch_cfg = fetch_cfg if isinstance(fetch_cfg, dict) else {}
        if not is_futu_fetch_source(fetch_cfg.get("source")):
            for key in missing_keys:
                fetch_reasons[key] = "required_data_fetch_skipped_non_futu_source"
                fetch_details[key] = {
                    "quote_key": "|".join(key),
                    "requested_expirations": requested_expirations,
                    "available_expirations": sorted(current_expirations.get(symbol) or set()),
                }
            continue
        strikes = [safe_float(v) for v in (spec.get("strikes") or [])]
        strikes = [v for v in strikes if v is not None]
        try:
            payload = fetch_symbol(
                symbol,
                limit_expirations=safe_int(fetch_cfg.get("limit_expirations")) or max(len(requested_expirations), 8),
                host=str(fetch_cfg.get("host") or "127.0.0.1"),
                port=safe_int(fetch_cfg.get("port")) or 11111,
                base_dir=base_dir,
                option_types=",".join(sorted(spec.get("option_types") or {"put", "call"})),
                min_strike=min(strikes) if strikes else None,
                max_strike=max(strikes) if strikes else None,
                explicit_expirations=requested_expirations,
                chain_cache=True,
                chain_cache_force_refresh=True,
            )
        except Exception as exc:
            summary["errors"] += 1
            for key in missing_keys:
                fetch_reasons[key] = "required_data_fetch_error"
                fetch_details[key] = {
                    "quote_key": "|".join(key),
                    "requested_expirations": requested_expirations,
                    "available_expirations": sorted(current_expirations.get(symbol) or set()),
                    "message": str(exc),
                }
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
    attempted_reasons: dict[tuple[str, str, str, str], str] = {}
    attempted_details: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        key = _quote_key(pos.get("symbol"), pos.get("option_type"), _position_expiration(pos), pos.get("strike"), base_dir=base_dir)
        if all(key) and key in covered_keys and not _quote_has_usable_price(quotes.get(key)):
            missing_by_symbol.setdefault(key[0], []).append(pos)

    if not missing_by_symbol:
        return {}, {}

    try:
        from scripts.fetch_market_data_opend import fetch_symbol
    except Exception:
        return {}, {}

    for symbol, missing_positions in missing_by_symbol.items():
        symbol_cfg = symbol_cfgs.get(symbol) or {}
        fetch_cfg = symbol_cfg.get("fetch") if isinstance(symbol_cfg, dict) else {}
        fetch_cfg = fetch_cfg if isinstance(fetch_cfg, dict) else {}
        requested_symbol = symbol
        resolved_underlier = None
        try:
            resolved_underlier = normalize_underlier(symbol).code
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
                if all(key):
                    attempted_reasons[key] = "opend_fetch_skipped_non_futu_source"
            continue
        expirations = sorted({key[2] for key in missing_keys if len(key) >= 3 and key[2]})
        if not expirations:
            for key in missing_keys:
                if all(key):
                    attempted_reasons[key] = "opend_fetch_skipped_missing_expiration"
            continue
        strikes = [safe_float(p.get("strike")) for p in missing_positions]
        strikes = [s for s in strikes if s is not None]
        if not strikes:
            for key in missing_keys:
                if all(key):
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
            )
        except Exception as exc:
            err_low = str(exc or "").lower()
            detail = "opend_fetch_error"
            if "rate limit" in err_low or "too frequent" in err_low or "最多10次" in str(exc or "") or "频率太高" in str(exc or ""):
                detail = "opend_fetch_error_rate_limit"
            elif "retry budget" in err_low:
                detail = "opend_fetch_error_retry_budget"
            for key in missing_keys:
                if all(key):
                    attempted_reasons[key] = detail
            continue
        rows = payload.get("rows") if isinstance(payload, dict) else []
        _merge_quote_rows(quotes, rows if isinstance(rows, list) else [])
        for key in missing_keys:
            if not all(key):
                continue
            if _quote_has_usable_price(quotes.get(key)):
                continue
            attempted_reasons[key] = "opend_fetch_no_usable_quote"
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
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        key = _quote_key(pos.get("symbol"), pos.get("option_type"), _position_expiration(pos), pos.get("strike"), base_dir=base_dir)
        if not all(key) or key in covered_keys:
            continue
        available_expirations = sorted(expirations_by_symbol.get(key[0]) or set())
        has_expiration = bool(key[2] and key[2] in (expirations_by_symbol.get(key[0]) or set()))
        reasons[key] = "required_data_missing_contract" if has_expiration else "required_data_missing_expiration"
        details[key] = {
            "quote_key": "|".join(key),
            "available_expirations": available_expirations[:5],
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
        if available_expirations:
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
    q_dte = safe_int((quote or {}).get("dte"))
    if q_dte is not None:
        return q_dte
    if not expiration:
        return None
    try:
        exp_date = datetime.strptime(expiration[:10], "%Y-%m-%d").date()
        return (exp_date - datetime.now(timezone.utc).date()).days
    except Exception:
        return None


def _mid_from_quote(quote: dict[str, Any] | None) -> tuple[float | None, list[str]]:
    if not isinstance(quote, dict):
        return None, ["missing_quote"]
    mid = _quote_number(quote.get("mid"))
    if mid is not None:
        return mid, []
    bid = _quote_number(quote.get("bid"))
    ask = _quote_number(quote.get("ask"))
    if bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid:
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
            account=str(pos.get("account") or "").strip().lower(),
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
            multiplier=safe_float(pos.get("multiplier")) or safe_float((quote or {}).get("multiplier")),
            spot=safe_float((quote or {}).get("spot")),
            currency=str(pos.get("currency") or (quote or {}).get("currency") or "").strip().upper(),
        ),
        quote_flags,
    )


def _apply_buy_to_close_fee(row: dict[str, Any]) -> dict[str, Any]:
    mid = safe_float(row.get("close_mid"))
    contracts = safe_int(row.get("contracts_open")) or 1
    if mid is None:
        return row
    multiplier = safe_int(row.get("multiplier"))
    if multiplier is None or multiplier <= 0:
        return _with_extra_flags(row, ["fee_calc_unavailable"])
    try:
        fee = calc_futu_option_fee(
            row.get("currency"),
            mid,
            contracts=contracts,
            multiplier=multiplier,
            is_sell=False,
        )
    except Exception:
        return _with_extra_flags(row, ["fee_calc_unavailable"])
    realized = safe_float(row.get("realized_if_close"))
    if realized is not None:
        row["realized_if_close"] = realized - float(fee)
    row["buy_to_close_fee"] = float(fee)
    return row


def _apply_fee_profitability_gate(row: dict[str, Any]) -> dict[str, Any]:
    realized = safe_float(row.get("realized_if_close"))
    if realized is None:
        return row
    if str(row.get("tier") or "").strip().lower() == "none":
        return row
    if realized > 0:
        return row
    row = _with_extra_flags(row, ["not_profitable_after_fee"])
    row["tier"] = "none"
    row["tier_label"] = "不提醒"
    row["reason"] = "扣除平仓手续费后已无正收益，不建议作为收益型买回提醒"
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
    ccy = str(currency or "").strip().upper()
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
        if str(row.get("tier") or "").strip().lower() not in notify_levels:
            continue
        acct = str(row.get("account") or "当前账户").strip().lower() or "当前账户"
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
        acct = str(row.get("account") or "当前账户").strip().lower() or "当前账户"
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
        "required_data_fetch_skipped_non_futu_source": "非 Futu 行情源，无法补拉持仓覆盖",
        "opend_fetch_no_usable_quote": "无可用报价",
        "opend_fetch_error_rate_limit": "OpenD 限频",
        "opend_fetch_error_retry_budget": "OpenD 重试预算耗尽",
        "opend_fetch_error": "OpenD 拉取失败",
        "missing_quote": "缺少报价",
        "missing_mid": "缺少可用定价",
        "spread_too_wide": "价差过宽",
        "invalid_spread": "价差无效",
    }
    for flag in flags:
        if flag in mapping:
            return mapping[flag]
    return str(row.get("reason") or "无法评估").strip() or "无法评估"


def render_markdown(rows: list[dict[str, Any]], *, notify_levels: set[str], max_items: int) -> str:
    selected = _selected_notify_rows(rows, notify_levels=notify_levels, max_items=max_items)
    gap_rows = _selected_evaluation_gap_rows(rows, max_items=max_items)
    if not selected and not gap_rows:
        return ""

    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for row in selected:
        acct = str(row.get("account") or "当前账户").strip().lower() or "当前账户"
        grouped.setdefault(acct, []).append(row)
    gap_grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for row in gap_rows:
        acct = str(row.get("account") or "当前账户").strip().lower() or "当前账户"
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
                        f"- {row.get('symbol')} {opt} {exp} {strike}{suffix} · {row.get('tier_label')}",
                        (
                            f"- 已锁定: {_pct(row.get('capture_ratio'))} | "
                            f"剩余DTE={row.get('dte') if row.get('dte') is not None else '-'} | "
                            f"剩余收益年化={_pct(row.get('remaining_annualized_return'))}"
                        ),
                        f"- 价格: 开仓权利金={_num(row.get('premium'))} | 平仓 mid={_num(row.get('close_mid'))}",
                        (
                            f"- 估算: 平仓后锁定收益 {_money(row.get('realized_if_close'), currency)} | "
                            f"剩余权利金 {_money(row.get('remaining_premium'), currency)}"
                        ),
                        f"- 理由: {row.get('reason') or '-'}",
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
    obj = read_json(context_path, default={})
    return obj if isinstance(obj, dict) else {}


def run_close_advice(
    *,
    config: dict[str, Any],
    context_path: Path,
    required_data_root: Path,
    output_dir: Path,
    base_dir: Path,
    markets_to_run: list[str] | None = None,
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
        row = evaluate_close_advice(inp, cfg)
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
        else:
            row["evaluation_status"] = "priced"
            row["quote_status"] = "priced"
            row = _apply_buy_to_close_fee(row)
            row = _apply_fee_profitability_gate(row)
        status = str(row.get("evaluation_status") or "unknown").strip().lower() or "unknown"
        evaluation_status_counts[status] = evaluation_status_counts.get(status, 0) + 1
        rows.append(row)

    rows = sort_advice_rows(rows)
    notify_levels = advice_cfg.get("notify_levels") or ["strong", "medium"]
    notify_level_set = {str(x).strip().lower() for x in notify_levels if str(x).strip()}
    max_items = safe_int(advice_cfg.get("max_items_per_account")) or 5
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
