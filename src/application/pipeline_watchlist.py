"""Symbols pipeline runner.

Why:
- Keep run_pipeline orchestration-only (Stage 3).
- Centralize symbols loop and summary aggregation.

Design:
- External dependencies are injected (process_symbol_fn, apply_profiles_fn, build_pipeline_context_fn)
  to keep this module unit-testable.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Iterable

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.config_profiles import deep_merge
from src.application.config_sections import (
    resolve_templates_config,
    resolve_watchlist_config,
)
from domain.domain.candidate_defaults import resolve_event_risk_config
from domain.domain.sell_call_config import resolve_min_annualized_net_premium_return
from domain.domain.sell_put_config import resolve_min_annualized_net_return
from domain.domain import normalize_processor_row, normalize_processor_rows
from src.application.yield_enhancement_config import (
    COMBO_YIELD_CONFIG_KEY,
    resolve_yield_enhancement_cfg,
    wants_yield_enhancement_separate,
)
from src.application.symbol_mutations import normalize_symbol_read
from src.application.config_validator import validate_resolved_watchlist_item_runtime_config
from src.infrastructure.io_utils import atomic_write_json

LIQUIDITY_COMMON_FIELDS = (
    'min_net_income',
    'min_open_interest',
    'min_volume',
    'max_spread_ratio',
)
DEFAULT_PIPELINE_SYMBOL_MAX_WORKERS = 4
POSITION_ADVICE_CANDIDATE_CAPTURE_SCHEMA = (
    "position_advice_candidate_all_decisions_capture.v1"
)
POSITION_ADVICE_CANDIDATE_RISK_POLICY_VERSION = "candidate_pipeline_policy.v2"


def _to_positive_int(value, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = int(default)
    return max(1, parsed)


def _resolve_pipeline_symbol_max_workers(cfg: dict, symbol_count: int) -> int:
    if symbol_count <= 1:
        return 1
    runtime = cfg.get('runtime') if isinstance(cfg.get('runtime'), dict) else {}
    raw = runtime.get('pipeline_symbol_max_workers')
    if raw is None:
        raw = runtime.get('watchlist_max_workers')
    workers = _to_positive_int(raw, DEFAULT_PIPELINE_SYMBOL_MAX_WORKERS)
    return min(symbol_count, workers)


def _extract_event_risk_cfg(side_cfg: dict) -> dict:
    raw = side_cfg.get("event_risk")
    return resolve_event_risk_config(raw if isinstance(raw, dict) else None)


def _apply_event_snapshot_path(item: dict, snapshot_path: str | None) -> dict:
    if not snapshot_path:
        return item
    out = dict(item)
    for key in ("_global_sell_put_event_risk", "_global_sell_call_event_risk"):
        raw = out.get(key)
        event_cfg = dict(raw) if isinstance(raw, dict) else {"enabled": True, "mode": "warn"}
        event_cfg["snapshot_path"] = snapshot_path
        out[key] = event_cfg
    return out


def _parse_symbols_whitelist(symbols_arg: str | None) -> set[str] | None:
    if not symbols_arg:
        return None
    items = {normalize_symbol_read(s) for s in str(symbols_arg).split(',') if str(s).strip()}
    return items or None


def _iter_watchlist(cfg: dict) -> Iterable[dict]:
    return resolve_watchlist_config(cfg)


def _resolve_profile_cfg(item: dict, profiles: dict) -> dict:
    use = item.get('use')
    if not use:
        return {}

    use_list: list[str] = []
    if isinstance(use, str):
        use_list = [use]
    elif isinstance(use, list):
        use_list = [x for x in use if isinstance(x, str)]

    merged: dict = {}
    for name in use_list:
        p = profiles.get(name)
        if isinstance(p, dict):
            merged = deep_merge(merged, p)
    return merged


def _resolve_profile_side_cfg(item: dict, profiles: dict, side: str) -> dict:
    merged = _resolve_profile_cfg(item, profiles)
    side_cfg = merged.get(side)
    return dict(side_cfg) if isinstance(side_cfg, dict) else {}


def _extract_liquidity_fields(side_cfg: dict, *, is_put: bool, fields: tuple[str, ...] = LIQUIDITY_COMMON_FIELDS) -> dict:
    del is_put
    keys = list(fields)
    return {k: side_cfg[k] for k in keys if k in side_cfg}


def resolve_watchlist_item_runtime_config(
    *,
    item: dict,
    profiles: dict,
    apply_profiles_fn: Callable[[dict, dict], dict],
) -> dict:
    resolved = apply_profiles_fn(item, profiles)

    # Resolve min annualized return with a single source-of-truth chain:
    # symbol.sell_put > templates.sell_put > DEFAULT.
    resolved_put_min = resolve_min_annualized_net_return(symbol_cfg=item, profiles=profiles)
    sell_put_cfg = dict(resolved.get('sell_put') or {})
    sell_put_cfg['min_annualized_net_return'] = resolved_put_min
    resolved['sell_put'] = sell_put_cfg

    resolved_call_min = resolve_min_annualized_net_premium_return(symbol_cfg=item, profiles=profiles)
    sell_call_cfg = dict(resolved.get('sell_call') or {})
    sell_call_cfg['min_annualized_net_premium_return'] = resolved_call_min
    sell_call_cfg.pop('min_annualized_net_return', None)
    resolved['sell_call'] = sell_call_cfg
    resolved_yield_enhancement_cfg = resolve_yield_enhancement_cfg(resolved)
    if resolved_yield_enhancement_cfg:
        resolved.pop('yield_enhancement', None)
        resolved[COMBO_YIELD_CONFIG_KEY] = resolved_yield_enhancement_cfg

    resolved['_global_sell_put_liquidity'] = _extract_liquidity_fields(
        _resolve_profile_side_cfg(item, profiles, 'sell_put'),
        is_put=True,
    )
    resolved['_global_sell_call_liquidity'] = _extract_liquidity_fields(
        _resolve_profile_side_cfg(item, profiles, 'sell_call'),
        is_put=False,
    )
    yield_enhancement_profile = resolve_yield_enhancement_cfg(_resolve_profile_cfg(item, profiles))
    resolved['_global_yield_enhancement_liquidity'] = _extract_liquidity_fields(
        yield_enhancement_profile,
        is_put=False,
        fields=LIQUIDITY_COMMON_FIELDS + ('max_combo_spread_ratio',),
    )
    resolved['_global_sell_put_event_risk'] = _extract_event_risk_cfg(
        _resolve_profile_side_cfg(item, profiles, 'sell_put'),
    )
    resolved['_global_sell_call_event_risk'] = _extract_event_risk_cfg(
        _resolve_profile_side_cfg(item, profiles, 'sell_call'),
    )
    validate_resolved_watchlist_item_runtime_config(resolved)
    return resolved


def run_watchlist_pipeline(
    *,
    py: str,
    base: Path,
    cfg: dict,
    report_dir: Path,
    is_scheduled: bool,
    top_n: int,
    symbol_timeout_sec: int,
    portfolio_timeout_sec: int,
    want_scan: bool,
    no_context: bool,
    symbols_arg: str | None,
    log: Callable[[str], None],
    want_fn: Callable[[str], bool],
    apply_profiles_fn: Callable[[dict, dict], dict],
    process_symbol_fn: Callable[..., list[dict]],
    build_pipeline_context_fn: Callable[..., tuple[dict | None, dict | None, float | None, float | None]],
    build_symbols_summary_fn: Callable[[list[dict]], object],
    build_symbols_digest_fn: Callable[[list[dict], int], object],
    position_advice_quote_snapshot_ids: dict[str, str] | None = None,
    position_advice_all_decisions_sink_fn: (
        Callable[[list[dict[str, Any]]], None] | None
    ) = None,
    position_advice_risk_policy_version: str | None = None,
    position_advice_producer_run_id: str | None = None,
    position_advice_candidate_capture_status_sink_fn: (
        Callable[[dict[str, Any]], None] | None
    ) = None,
) -> list[dict]:
    sym_whitelist = _parse_symbols_whitelist(symbols_arg)

    runtime = cfg.get('runtime', {}) or {}
    event_snapshot_path = str(runtime.get("event_snapshot_path") or "").strip()
    profiles = resolve_templates_config(cfg)

    portfolio_ctx, option_ctx, usd_per_cny_exchange_rate, cny_per_hkd_exchange_rate = build_pipeline_context_fn(
        py=py,
        base=base,
        cfg=cfg,
        report_dir=report_dir,
        portfolio_timeout_sec=portfolio_timeout_sec,
        runtime=runtime,
        is_scheduled=is_scheduled,
        log=log,
        no_context=no_context,
        want_scan=want_fn('scan'),
    )

    watchlist_items = []
    for item0 in _iter_watchlist(cfg):
        if sym_whitelist is not None:
            s0 = normalize_symbol_read(item0.get('symbol'))
            if s0 and s0 not in sym_whitelist:
                continue
        watchlist_items.append(item0)

    if portfolio_ctx is not None and option_ctx is not None:
        portfolio_ctx = dict(portfolio_ctx)
        portfolio_ctx['option_ctx'] = option_ctx

    def _failure_rows(item0: dict, exc: Exception) -> list[dict]:
        symbol = item0.get('symbol', 'UNKNOWN')
        log(f'[WARN] {symbol} processing failed: {exc}')
        rows = [
            normalize_processor_row(
                {
                    'symbol': symbol,
                    'strategy': 'sell_put',
                    'candidate_count': 0,
                    'note': f'处理失败: {exc}',
                }
            ),
            normalize_processor_row(
                {
                    'symbol': symbol,
                    'strategy': 'sell_call',
                    'candidate_count': 0,
                    'note': f'处理失败: {exc}',
                }
            ),
        ]
        if wants_yield_enhancement_separate(resolve_yield_enhancement_cfg(item0)):
            rows.append(
                normalize_processor_row(
                    {
                        'symbol': symbol,
                        'strategy': 'combo_yield',
                        'candidate_count': 0,
                        'note': f'处理失败: {exc}',
                    }
                )
            )
        return rows

    def _process_item(item0: dict) -> list[dict]:
        try:
            item = resolve_watchlist_item_runtime_config(
                item=item0,
                profiles=profiles,
                apply_profiles_fn=apply_profiles_fn,
            )
            item = _apply_event_snapshot_path(item, event_snapshot_path)
            item_portfolio_ctx = dict(portfolio_ctx) if isinstance(portfolio_ctx, dict) else None
            symbol_key = normalize_symbol_read(item.get("symbol"))
            quote_snapshot_id = (
                (position_advice_quote_snapshot_ids or {}).get(symbol_key)
                if symbol_key
                else None
            )
            advice_scan_kwargs: dict[str, Any] = {}
            if (
                position_advice_all_decisions_sink_fn is not None
                and position_advice_risk_policy_version
            ):
                advice_scan_kwargs = {
                    "risk_policy_version": position_advice_risk_policy_version,
                    "all_decisions_sink_fn": position_advice_all_decisions_sink_fn,
                    "position_advice_producer_run_id": (
                        position_advice_producer_run_id
                    ),
                    "candidate_capture_status_sink_fn": (
                        position_advice_candidate_capture_status_sink_fn
                    ),
                }
                if quote_snapshot_id:
                    advice_scan_kwargs["quote_snapshot_id"] = quote_snapshot_id

            if not want_scan:
                process_symbol_fn(
                    py,
                    base,
                    item,
                    top_n,
                    portfolio_ctx=item_portfolio_ctx,
                    usd_per_cny_exchange_rate=usd_per_cny_exchange_rate,
                    cny_per_hkd_exchange_rate=cny_per_hkd_exchange_rate,
                    timeout_sec=symbol_timeout_sec,
                    is_scheduled=is_scheduled,
                    runtime_config=cfg,
                    fetch_only=True,
                )
                return []

            processor_rows = process_symbol_fn(
                py,
                base,
                item,
                top_n,
                portfolio_ctx=item_portfolio_ctx,
                usd_per_cny_exchange_rate=usd_per_cny_exchange_rate,
                cny_per_hkd_exchange_rate=cny_per_hkd_exchange_rate,
                timeout_sec=symbol_timeout_sec,
                is_scheduled=is_scheduled,
                runtime_config=cfg,
                **advice_scan_kwargs,
            )
            validated_rows = normalize_processor_rows(processor_rows)
            return list(validated_rows)
        except Exception as e:
            return _failure_rows(item0, e)

    summary_rows: list[dict] = []
    max_workers = _resolve_pipeline_symbol_max_workers(cfg, len(watchlist_items))
    if max_workers <= 1:
        for item0 in watchlist_items:
            summary_rows.extend(_process_item(item0))
    else:
        rows_by_index: dict[int, list[dict]] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_by_index = {
                executor.submit(_process_item, item0): idx
                for idx, item0 in enumerate(watchlist_items)
            }
            for future in as_completed(future_by_index):
                rows_by_index[future_by_index[future]] = future.result()
        for idx in range(len(watchlist_items)):
            summary_rows.extend(rows_by_index.get(idx, []))

    if want_fn('scan'):
        build_symbols_summary_fn(summary_rows)
        build_symbols_digest_fn(summary_rows, int(top_n))

    return summary_rows


def run_watchlist_pipeline_default(
    *,
    py: str,
    base: Path,
    cfg: dict,
    report_dir: Path,
    state_dir: Path,
    shared_state_dir: Path | None,
    required_data_dir: Path,
    is_scheduled: bool,
    top_n: int,
    symbol_timeout_sec: int,
    portfolio_timeout_sec: int,
    want_scan: bool,
    no_context: bool,
    symbols_arg: str | None,
    log: Callable[[str], None],
    want_fn: Callable[[str], bool],
    position_advice_account_run_id: str | None = None,
) -> list[dict]:
    from src.application.config_profiles import apply_profiles
    from src.application.pipeline_context import build_pipeline_context
    from src.application.pipeline_symbol import process_symbol
    from src.application.report_builders import build_symbols_digest, build_symbols_summary

    whitelist = _parse_symbols_whitelist(symbols_arg)
    account_run_id = str(position_advice_account_run_id or "").strip()
    capture_started_at = datetime.now(timezone.utc)
    captured_decisions: list[dict[str, Any]] = []
    capture_statuses: list[dict[str, Any]] = []
    capture_lock = Lock()

    def _capture_all_decisions(rows: list[dict[str, Any]]) -> None:
        with capture_lock:
            captured_decisions.extend(dict(item) for item in rows)

    def _capture_status(status: dict[str, Any]) -> None:
        with capture_lock:
            capture_statuses.append(dict(status))

    candidate_capture_enabled = bool(account_run_id and want_scan)
    result = run_watchlist_pipeline(
        py=py,
        base=base,
        cfg=cfg,
        report_dir=report_dir,
        is_scheduled=is_scheduled,
        top_n=top_n,
        symbol_timeout_sec=symbol_timeout_sec,
        portfolio_timeout_sec=portfolio_timeout_sec,
        want_scan=want_scan,
        no_context=no_context,
        symbols_arg=symbols_arg,
        log=log,
        want_fn=want_fn,
        apply_profiles_fn=apply_profiles,
        process_symbol_fn=(
            lambda *a, **kw: process_symbol(
                *a,
                **{k: v for k, v in kw.items() if k != 'is_scheduled'},
                required_data_dir=required_data_dir,
                report_dir=report_dir,
                state_dir=state_dir,
                is_scheduled=is_scheduled,
            )
        ),
        build_pipeline_context_fn=(
            lambda **kw: build_pipeline_context(
                **kw,
                state_dir=state_dir,
                shared_state_dir=shared_state_dir,
            )
        ),
        build_symbols_summary_fn=lambda rows: build_symbols_summary(rows, report_dir, is_scheduled=is_scheduled),
        build_symbols_digest_fn=lambda rows, n: (
            None
            if is_scheduled
            else build_symbols_digest([r.get("symbol") for r in rows if r.get("symbol")], report_dir)
        ),
        position_advice_all_decisions_sink_fn=(
            _capture_all_decisions if candidate_capture_enabled else None
        ),
        position_advice_risk_policy_version=(
            POSITION_ADVICE_CANDIDATE_RISK_POLICY_VERSION
            if candidate_capture_enabled
            else None
        ),
        position_advice_producer_run_id=(
            account_run_id if candidate_capture_enabled else None
        ),
        position_advice_candidate_capture_status_sink_fn=(
            _capture_status if candidate_capture_enabled else None
        ),
    )
    if not candidate_capture_enabled:
        return result

    captured_at = datetime.now(timezone.utc)
    profiles = resolve_templates_config(cfg)
    expected_scopes: set[tuple[str, str]] = set()
    configuration_errors: list[str] = []
    for raw_item in _iter_watchlist(cfg):
        symbol = normalize_symbol_read(raw_item.get("symbol"))
        if not symbol or (whitelist is not None and symbol not in whitelist):
            continue
        try:
            resolved_item = resolve_watchlist_item_runtime_config(
                item=raw_item,
                profiles=profiles,
                apply_profiles_fn=apply_profiles,
            )
        except Exception:
            configuration_errors.append(symbol)
            continue
        if bool((resolved_item.get("sell_put") or {}).get("enabled", False)):
            expected_scopes.add((symbol, "put"))
        if bool((resolved_item.get("sell_call") or {}).get("enabled", False)):
            expected_scopes.add((symbol, "call"))

    normalized_statuses = sorted(
        (
            {
                "symbol": normalize_symbol_read(item.get("symbol")),
                "strategy_mode": str(item.get("strategy_mode") or "").strip(),
                "status": str(item.get("status") or "").strip(),
                "reason": str(item.get("reason") or "").strip(),
                "quote_snapshot_id": (
                    str(item.get("quote_snapshot_id") or "").strip() or None
                ),
                "quote_receipt_relpath": (
                    str(item.get("quote_receipt_relpath") or "").strip() or None
                ),
            }
            for item in capture_statuses
        ),
        key=lambda item: (item["symbol"], item["strategy_mode"]),
    )
    statuses_by_scope: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in normalized_statuses:
        key = (str(item["symbol"]), str(item["strategy_mode"]))
        statuses_by_scope.setdefault(key, []).append(item)
    missing_scan_scopes = sorted(
        f"{symbol}:{mode}"
        for symbol, mode in expected_scopes
        if (symbol, mode) not in statuses_by_scope
    )
    duplicate_scan_scopes = sorted(
        f"{symbol}:{mode}"
        for (symbol, mode), items in statuses_by_scope.items()
        if len(items) != 1
    )
    unexpected_scan_scopes = sorted(
        f"{symbol}:{mode}"
        for symbol, mode in statuses_by_scope
        if (symbol, mode) not in expected_scopes
    )
    incomplete_scan_scopes = sorted(
        f"{symbol}:{mode}"
        for (symbol, mode), items in statuses_by_scope.items()
        if (
            len(items) != 1
            or items[0]["status"] not in {"completed", "not_applicable"}
            or (
                items[0]["status"] == "completed"
                and (
                    not items[0]["quote_snapshot_id"]
                    or not items[0]["quote_receipt_relpath"]
                )
            )
        )
    )
    quote_bindings_by_symbol: dict[str, set[tuple[str, str]]] = {}
    for item in normalized_statuses:
        if item["quote_snapshot_id"] and item["quote_receipt_relpath"]:
            quote_bindings_by_symbol.setdefault(
                str(item["symbol"]),
                set(),
            ).add(
                (
                    str(item["quote_snapshot_id"]),
                    str(item["quote_receipt_relpath"]),
                )
            )
    quote_binding_conflict_symbols = sorted(
        symbol
        for symbol, bindings in quote_bindings_by_symbol.items()
        if len(bindings) != 1
    )
    quote_receipt_relpaths = {
        str(item["symbol"]): str(item["quote_receipt_relpath"])
        for item in normalized_statuses
        if item["quote_receipt_relpath"]
    }
    quote_snapshot_ids = {
        str(item["symbol"]): str(item["quote_snapshot_id"])
        for item in normalized_statuses
        if item["quote_snapshot_id"]
    }
    candidate_symbols = sorted({symbol for symbol, _mode in expected_scopes})
    decisions = sorted(
        captured_decisions,
        key=lambda item: (
            str(item.get("strategy_mode") or ""),
            str(item.get("candidate_id") or ""),
        ),
    )
    required_quote_symbols = {
        symbol
        for (symbol, mode), items in statuses_by_scope.items()
        if (
            (symbol, mode) in expected_scopes
            and len(items) == 1
            and items[0]["status"] != "not_applicable"
        )
    }
    missing_quote_symbols = sorted(
        required_quote_symbols - set(quote_snapshot_ids)
    )
    account = str(
        ((cfg.get("portfolio") or {}).get("account"))
        if isinstance(cfg.get("portfolio"), dict)
        else ""
    ).strip().lower()
    capture_payload = {
        "schema_version": POSITION_ADVICE_CANDIDATE_CAPTURE_SCHEMA,
        "account_run_id": account_run_id,
        "account": account or None,
        "risk_policy_version": POSITION_ADVICE_CANDIDATE_RISK_POLICY_VERSION,
        "capture_started_at": capture_started_at.isoformat().replace(
            "+00:00",
            "Z",
        ),
        "captured_at": captured_at.isoformat().replace("+00:00", "Z"),
        "complete": bool(
            account
            and not configuration_errors
            and not missing_quote_symbols
            and not missing_scan_scopes
            and not duplicate_scan_scopes
            and not unexpected_scan_scopes
            and not incomplete_scan_scopes
            and not quote_binding_conflict_symbols
        ),
        "candidate_symbols": candidate_symbols,
        "expected_scan_scopes": sorted(
            f"{symbol}:{mode}" for symbol, mode in expected_scopes
        ),
        "configuration_error_symbols": sorted(set(configuration_errors)),
        "missing_quote_symbols": missing_quote_symbols,
        "missing_scan_scopes": missing_scan_scopes,
        "duplicate_scan_scopes": duplicate_scan_scopes,
        "unexpected_scan_scopes": unexpected_scan_scopes,
        "incomplete_scan_scopes": incomplete_scan_scopes,
        "quote_binding_conflict_symbols": (
            quote_binding_conflict_symbols
        ),
        "scan_statuses": normalized_statuses,
        "quote_receipt_relpaths": dict(sorted(quote_receipt_relpaths.items())),
        "quote_snapshot_ids": dict(sorted(quote_snapshot_ids.items())),
        "candidate_decisions": decisions,
        "candidate_count": len(decisions),
    }
    capture_payload["capture_hash"] = canonical_sha256(capture_payload)
    atomic_write_json(
        state_dir / "position_advice_candidate_all_decisions.raw.json",
        capture_payload,
        sort_keys=True,
    )
    return result
