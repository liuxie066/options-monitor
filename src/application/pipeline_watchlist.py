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
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import signal
from threading import Lock, current_thread, main_thread
import time
from typing import Any, Callable, Iterable

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.config_profiles import deep_merge
from src.application.config_sections import (
    resolve_templates_config,
    resolve_watchlist_config,
)
from domain.domain.sell_call_config import resolve_min_annualized_net_premium_return
from domain.domain.sell_put_config import resolve_min_annualized_net_return
from domain.domain.symbol_identity import symbol_market
from domain.domain import normalize_processor_row, normalize_processor_rows
from src.application.yield_enhancement_config import (
    COMBO_YIELD_CONFIG_KEY,
    derive_yield_enhancement_policy,
    resolve_yield_enhancement_cfg,
    wants_yield_enhancement_separate,
)
from src.application.symbol_mutations import normalize_symbol_read
from src.application.config_validator import validate_resolved_watchlist_item_runtime_config
from src.application.prefilters import apply_prefilters
from src.application.strategy_scan_status import publish_strategy_scan_status_index
from src.application.opening_candidate_snapshot import (
    dependency_from_file,
    dependency_from_hash,
    seal_opening_candidate_snapshot,
    strategy_policy_hash,
)

LIQUIDITY_COMMON_FIELDS = (
    'min_open_interest',
    'min_volume',
    'max_spread_ratio',
)
DEFAULT_PIPELINE_SYMBOL_MAX_WORKERS = 4


class SymbolProcessingTimeout(TimeoutError):
    pass


@contextmanager
def _enforce_symbol_timeout(
    *,
    symbol: str,
    timeout_sec: int | float | None,
):
    timeout = float(timeout_sec or 0)
    if timeout <= 0:
        yield
        return

    message = f"{symbol} symbol processing exceeded {timeout:g}s deadline"
    can_interrupt = (
        current_thread() is main_thread()
        and hasattr(signal, "SIGALRM")
        and hasattr(signal, "setitimer")
        and hasattr(signal, "ITIMER_REAL")
    )
    started = time.monotonic()
    if not can_interrupt:
        yield
        if time.monotonic() - started > timeout:
            raise SymbolProcessingTimeout(message)
        return

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)

    def _raise_timeout(_signum, _frame) -> None:
        raise SymbolProcessingTimeout(message)

    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        previous_remaining, previous_interval = previous_timer
        if previous_remaining > 0:
            elapsed = time.monotonic() - started
            signal.setitimer(
                signal.ITIMER_REAL,
                max(0.001, previous_remaining - elapsed),
                previous_interval,
            )


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
    position_advice_producer_run_id: str | None = None,
    position_advice_candidate_capture_status_sink_fn: (
        Callable[[dict[str, Any]], None] | None
    ) = None,
    opening_final_candidates_sink_fn: (
        Callable[[str, list[dict[str, Any]]], None] | None
    ) = None,
    opening_candidate_decisions_sink_fn: (
        Callable[[str, list[dict[str, Any]]], None] | None
    ) = None,
    opening_runtime_context_sink_fn: (
        Callable[[dict[str, Any] | None, dict[str, Any] | None], None] | None
    ) = None,
    required_data_snapshot_manifest: Path | None = None,
    prepared_portfolio_context_manifest: Path | None = None,
    prepared_portfolio_context_manifest_sha256: str | None = None,
    prepared_option_positions_context_manifest: Path | None = None,
    prepared_option_positions_context_manifest_sha256: str | None = None,
    account_config_sha256: str | None = None,
) -> list[dict]:
    sym_whitelist = _parse_symbols_whitelist(symbols_arg)

    runtime = cfg.get('runtime', {}) or {}
    profiles = resolve_templates_config(cfg)

    context_kwargs: dict[str, Any] = {
        "py": py,
        "base": base,
        "cfg": cfg,
        "report_dir": report_dir,
        "portfolio_timeout_sec": portfolio_timeout_sec,
        "runtime": runtime,
        "is_scheduled": is_scheduled,
        "log": log,
        "no_context": no_context,
        "want_scan": want_fn('scan'),
    }
    if prepared_portfolio_context_manifest is not None:
        context_kwargs["prepared_portfolio_context_manifest"] = (
            prepared_portfolio_context_manifest
        )
        context_kwargs["prepared_portfolio_context_run_id"] = (
            position_advice_producer_run_id
        )
        context_kwargs[
            "prepared_portfolio_context_account_config_sha256"
        ] = account_config_sha256
        context_kwargs[
            "prepared_portfolio_context_manifest_sha256"
        ] = prepared_portfolio_context_manifest_sha256
    if prepared_option_positions_context_manifest is not None:
        context_kwargs[
            "prepared_option_positions_context_manifest"
        ] = prepared_option_positions_context_manifest
        context_kwargs["prepared_option_positions_context_run_id"] = (
            position_advice_producer_run_id
        )
        context_kwargs[
            "prepared_option_positions_context_account_config_sha256"
        ] = account_config_sha256
        context_kwargs[
            "prepared_option_positions_context_manifest_sha256"
        ] = prepared_option_positions_context_manifest_sha256
    portfolio_ctx, option_ctx, usd_per_cny_exchange_rate, cny_per_hkd_exchange_rate = build_pipeline_context_fn(
        **context_kwargs,
    )
    if opening_runtime_context_sink_fn is not None:
        opening_runtime_context_sink_fn(portfolio_ctx, option_ctx)

    watchlist_items = []
    for item0 in _iter_watchlist(cfg):
        if sym_whitelist is not None:
            s0 = normalize_symbol_read(item0.get('symbol'))
            if s0 and s0 not in sym_whitelist:
                continue
        watchlist_items.append(item0)

    expected_strategy_statuses: list[dict[str, str]] = []
    if (
        required_data_snapshot_manifest is not None
        and str(position_advice_producer_run_id or "").strip()
    ):
        for item0 in watchlist_items:
            resolved = resolve_watchlist_item_runtime_config(
                item=item0,
                profiles=profiles,
                apply_profiles_fn=apply_profiles_fn,
            )
            symbol = normalize_symbol_read(resolved.get("symbol"))
            sp = dict(resolved.get("sell_put") or {})
            cc = dict(resolved.get("sell_call") or {})
            filtered = apply_prefilters(
                symbol=symbol,
                sp=sp,
                cc=cc,
                want_put=bool(sp.get("enabled", False)),
                want_call=bool(cc.get("enabled", False)),
                portfolio_ctx=portfolio_ctx,
            )
            market = str(
                symbol_market(symbol) or resolved.get("broker") or ""
            ).strip().upper()
            if filtered.want_put:
                expected_strategy_statuses.append(
                    {
                        "market": market,
                        "symbol": symbol,
                        "strategy_family": "sell_put",
                    }
                )
            if derive_yield_enhancement_policy(
                resolve_yield_enhancement_cfg(resolved)
            ).enabled:
                expected_strategy_statuses.append(
                    {
                        "market": market,
                        "symbol": symbol,
                        "strategy_family": "combo_yield",
                    }
                )
            if filtered.want_call:
                expected_strategy_statuses.append(
                    {
                        "market": market,
                        "symbol": symbol,
                        "strategy_family": "covered_call",
                    }
                )

    if portfolio_ctx is not None:
        portfolio_ctx = dict(portfolio_ctx)
        portfolio_ctx['option_ctx'] = (
            option_ctx
            if option_ctx is not None
            else {
                "context_status": "unavailable",
                "locked_shares_status": "unavailable",
                "locked_shares_unavailable_reason": "option_positions_context_unavailable",
                "locked_shares_by_symbol": {},
                "locked_shares_unavailable_by_symbol": {},
                "cash_secured_by_symbol_by_ccy": {},
                "cash_secured_total_by_ccy": {},
                "cash_secured_unavailable_by_symbol": {},
            }
        )

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
            item_portfolio_ctx = dict(portfolio_ctx) if isinstance(portfolio_ctx, dict) else None
            symbol_key = normalize_symbol_read(item.get("symbol"))
            quote_snapshot_id = (
                (position_advice_quote_snapshot_ids or {}).get(symbol_key)
                if symbol_key
                else None
            )
            advice_scan_kwargs: dict[str, Any] = {}
            if (
                position_advice_candidate_capture_status_sink_fn is not None
            ):
                advice_scan_kwargs = {
                    "position_advice_producer_run_id": (
                        position_advice_producer_run_id
                    ),
                    "candidate_capture_status_sink_fn": (
                        position_advice_candidate_capture_status_sink_fn
                    ),
                    "final_candidates_sink_fn": (
                        opening_final_candidates_sink_fn
                    ),
                    "candidate_decisions_sink_fn": (
                        opening_candidate_decisions_sink_fn
                    ),
                }
                if quote_snapshot_id:
                    advice_scan_kwargs["quote_snapshot_id"] = quote_snapshot_id
            if required_data_snapshot_manifest is not None:
                advice_scan_kwargs.update(
                    {
                        "required_data_snapshot_manifest": (
                            required_data_snapshot_manifest
                        ),
                        "required_data_snapshot_run_id": (
                            position_advice_producer_run_id
                        ),
                    }
                )

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

    def _process_item_with_timeout(item0: dict) -> list[dict]:
        symbol = str(item0.get("symbol") or "UNKNOWN")
        try:
            with _enforce_symbol_timeout(
                symbol=symbol,
                timeout_sec=symbol_timeout_sec,
            ):
                return _process_item(item0)
        except Exception as exc:
            return _failure_rows(item0, exc)

    summary_rows: list[dict] = []
    max_workers = _resolve_pipeline_symbol_max_workers(cfg, len(watchlist_items))
    if symbol_timeout_sec > 0:
        # A hard POSIX timer is process-main-thread scoped. Keep symbol work
        # sequential so the configured deadline covers fetch, scan, and writes.
        max_workers = 1
    if max_workers <= 1:
        for item0 in watchlist_items:
            summary_rows.extend(_process_item_with_timeout(item0))
    else:
        rows_by_index: dict[int, list[dict]] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_by_index = {
                executor.submit(_process_item_with_timeout, item0): idx
                for idx, item0 in enumerate(watchlist_items)
            }
            for future in as_completed(future_by_index):
                rows_by_index[future_by_index[future]] = future.result()
        for idx in range(len(watchlist_items)):
            summary_rows.extend(rows_by_index.get(idx, []))

    if want_fn('scan'):
        build_symbols_summary_fn(summary_rows)
        build_symbols_digest_fn(summary_rows, int(top_n))
        if (
            required_data_snapshot_manifest is not None
            and str(position_advice_producer_run_id or "").strip()
        ):
            portfolio_cfg = (
                cfg.get("portfolio")
                if isinstance(cfg.get("portfolio"), dict)
                else {}
            )
            publish_strategy_scan_status_index(
                report_dir=report_dir,
                run_id=str(position_advice_producer_run_id),
                account=str(portfolio_cfg.get("account") or ""),
                expected=expected_strategy_statuses,
            )

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
    required_data_snapshot_manifest: Path | None = None,
    prepared_portfolio_context_manifest: Path | None = None,
    prepared_portfolio_context_manifest_sha256: str | None = None,
    prepared_option_positions_context_manifest: Path | None = None,
    prepared_option_positions_context_manifest_sha256: str | None = None,
    account_config_sha256: str | None = None,
) -> list[dict]:
    from src.application.config_profiles import apply_profiles
    from src.application.pipeline_context import build_pipeline_context
    from src.application.pipeline_symbol import process_symbol
    from src.application.report_builders import build_symbols_digest, build_symbols_summary

    whitelist = _parse_symbols_whitelist(symbols_arg)
    account_run_id = str(position_advice_account_run_id or "").strip()
    capture_statuses: list[dict[str, Any]] = []
    captured_final_candidates: dict[str, list[dict[str, Any]]] = {
        "put": [],
        "call": [],
    }
    captured_candidate_decisions: dict[str, list[dict[str, Any]]] = {
        "put": [],
        "call": [],
    }
    captured_runtime_context: dict[str, Any] = {}
    capture_lock = Lock()

    def _capture_status(status: dict[str, Any]) -> None:
        with capture_lock:
            capture_statuses.append(dict(status))

    def _capture_final_candidates(
        mode: str,
        rows: list[dict[str, Any]],
    ) -> None:
        with capture_lock:
            captured_final_candidates.setdefault(str(mode), []).extend(
                dict(item) for item in rows
            )

    def _capture_candidate_decisions(
        mode: str,
        rows: list[dict[str, Any]],
    ) -> None:
        with capture_lock:
            captured_candidate_decisions.setdefault(str(mode), []).extend(
                dict(item) for item in rows
            )

    def _capture_runtime_context(
        portfolio_ctx: dict[str, Any] | None,
        option_ctx: dict[str, Any] | None,
    ) -> None:
        with capture_lock:
            captured_runtime_context["portfolio"] = (
                dict(portfolio_ctx) if isinstance(portfolio_ctx, dict) else None
            )
            captured_runtime_context["ledger"] = (
                dict(option_ctx) if isinstance(option_ctx, dict) else None
            )

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
        position_advice_producer_run_id=(
            account_run_id if candidate_capture_enabled else None
        ),
        position_advice_candidate_capture_status_sink_fn=(
            _capture_status if candidate_capture_enabled else None
        ),
        opening_final_candidates_sink_fn=(
            _capture_final_candidates if candidate_capture_enabled else None
        ),
        opening_candidate_decisions_sink_fn=(
            _capture_candidate_decisions if candidate_capture_enabled else None
        ),
        opening_runtime_context_sink_fn=(
            _capture_runtime_context if candidate_capture_enabled else None
        ),
        required_data_snapshot_manifest=required_data_snapshot_manifest,
        prepared_portfolio_context_manifest=prepared_portfolio_context_manifest,
        prepared_portfolio_context_manifest_sha256=(
            prepared_portfolio_context_manifest_sha256
        ),
        prepared_option_positions_context_manifest=(
            prepared_option_positions_context_manifest
        ),
        prepared_option_positions_context_manifest_sha256=(
            prepared_option_positions_context_manifest_sha256
        ),
        account_config_sha256=account_config_sha256,
    )
    if not candidate_capture_enabled:
        return result

    captured_at = datetime.now(timezone.utc)
    profiles = resolve_templates_config(cfg)
    expected_scopes: set[tuple[str, str]] = set()
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
            continue
        if bool((resolved_item.get("sell_put") or {}).get("enabled", False)):
            expected_scopes.add((symbol, "put"))
        if bool((resolved_item.get("sell_call") or {}).get("enabled", False)):
            expected_scopes.add((symbol, "call"))

    normalized_statuses = [
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
    ]
    statuses_by_scope: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in normalized_statuses:
        key = (str(item["symbol"]), str(item["strategy_mode"]))
        statuses_by_scope.setdefault(key, []).append(item)
    unexpected_scopes = set(statuses_by_scope) - expected_scopes
    if unexpected_scopes:
        unexpected = ", ".join(
            f"{symbol}:{mode}" for symbol, mode in sorted(unexpected_scopes)
        )
        raise ValueError(f"unexpected opening scan scopes: {unexpected}")
    for symbol, mode in sorted(expected_scopes):
        items = statuses_by_scope.get((symbol, mode), [])
        if not items:
            normalized_statuses.append(
                {
                    "symbol": symbol,
                    "strategy_mode": mode,
                    "status": "incomplete",
                    "reason": "opening_scan_status_missing",
                    "quote_snapshot_id": None,
                    "quote_receipt_relpath": None,
                }
            )
            continue
        if len(items) != 1:
            raise ValueError(f"duplicate opening scan scope: {symbol}:{mode}")
        item = items[0]
        if item["status"] == "completed" and (
            not item["quote_snapshot_id"] or not item["quote_receipt_relpath"]
        ):
            item["status"] = "incomplete"
            item["reason"] = "opening_quote_binding_missing"
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
    quote_binding_conflict_symbols = {
        symbol
        for symbol, bindings in quote_bindings_by_symbol.items()
        if len(bindings) != 1
    }
    for item in normalized_statuses:
        if (
            item["symbol"] in quote_binding_conflict_symbols
            and item["status"] == "completed"
        ):
            item["status"] = "incomplete"
            item["reason"] = "opening_quote_binding_conflict"
    normalized_statuses.sort(
        key=lambda item: (str(item["symbol"]), str(item["strategy_mode"]))
    )
    account = str(
        ((cfg.get("portfolio") or {}).get("account"))
        if isinstance(cfg.get("portfolio"), dict)
        else ""
    ).strip().lower()
    if required_data_snapshot_manifest is None or not str(
        account_config_sha256 or ""
    ).strip():
        return result
    portfolio_snapshot = captured_runtime_context.get("portfolio")
    option_snapshot = captured_runtime_context.get("ledger")
    if not isinstance(portfolio_snapshot, dict):
        portfolio_snapshot = {}
    if not isinstance(option_snapshot, dict):
        option_snapshot = {}
    authority = portfolio_snapshot.get("capacity_authority")
    if not isinstance(authority, dict):
        authority = {}
    dependencies = [
        dependency_from_file(
            kind="required_data",
            path=Path(required_data_snapshot_manifest),
            base=base,
        ),
        dependency_from_file(
            kind="portfolio",
            path=(
                Path(prepared_portfolio_context_manifest)
                if prepared_portfolio_context_manifest is not None
                else state_dir / "portfolio_context.json"
            ),
            base=base,
        ),
        dependency_from_file(
            kind="ledger",
            path=(
                Path(prepared_option_positions_context_manifest)
                if prepared_option_positions_context_manifest is not None
                else state_dir / "option_positions_context.json"
            ),
            base=base,
        ),
    ]
    required_hash = str(dependencies[0]["sha256"])
    fx_payload = (
        portfolio_snapshot.get("exchange_rates")
        if isinstance(portfolio_snapshot.get("exchange_rates"), dict)
        else option_snapshot.get("exchange_rates")
    )
    if not isinstance(fx_payload, dict):
        fx_payload = {}
    dependencies.extend(
        (
            dependency_from_hash(
                kind="fx",
                sha256=canonical_sha256(fx_payload),
            ),
            dependency_from_hash(
                kind="earnings_rv",
                sha256=required_hash,
            ),
        )
    )
    seal_opening_candidate_snapshot(
        base=base,
        run_id=account_run_id,
        account=account,
        market=str(authority.get("market") or ""),
        physical_account=authority,
        account_config_sha256=str(account_config_sha256 or ""),
        strategy_policy_sha256=strategy_policy_hash(cfg),
        dependencies=dependencies,
        scan_statuses=normalized_statuses,
        final_candidates=captured_final_candidates,
        candidate_evaluations=captured_candidate_decisions,
        sealed_at=captured_at,
    )
    return result
