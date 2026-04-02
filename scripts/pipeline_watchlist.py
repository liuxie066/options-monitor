"""Watchlist pipeline runner.

Why:
- Keep run_pipeline orchestration-only (Stage 3).
- Centralize watchlist loop and summary aggregation.

Design:
- External dependencies are injected (process_symbol_fn, apply_profiles_fn, build_pipeline_context_fn)
  to keep this module unit-testable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable


def _parse_symbols_whitelist(symbols_arg: str | None) -> set[str] | None:
    if not symbols_arg:
        return None
    items = {s.strip() for s in str(symbols_arg).split(',') if s.strip()}
    return items or None


def _iter_watchlist(cfg: dict) -> Iterable[dict]:
    wl = cfg.get('watchlist')
    if not isinstance(wl, list):
        return []
    return [it for it in wl if isinstance(it, dict)]


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
) -> list[dict]:
    sym_whitelist = _parse_symbols_whitelist(symbols_arg)

    runtime = cfg.get('runtime', {}) or {}
    profiles = cfg.get('profiles') or {}

    portfolio_ctx, option_ctx, fx_usd_per_cny, hkdcny = build_pipeline_context_fn(
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

    summary_rows: list[dict] = []

    for item0 in _iter_watchlist(cfg):
        try:
            if sym_whitelist is not None:
                s0 = str(item0.get('symbol') or '').strip()
                if s0 and s0 not in sym_whitelist:
                    continue

            item = apply_profiles_fn(item0, profiles)

            # inject option_ctx into portfolio_ctx for now (minimal change)
            if portfolio_ctx is not None and option_ctx is not None:
                portfolio_ctx['option_ctx'] = option_ctx

            if not want_scan:
                item_fetch = dict(item)
                item_fetch['sell_put'] = {'enabled': False}
                item_fetch['sell_call'] = {'enabled': False}
                process_symbol_fn(
                    py,
                    base,
                    item_fetch,
                    top_n,
                    portfolio_ctx=None,
                    fx_usd_per_cny=None,
                    hkdcny=None,
                    timeout_sec=symbol_timeout_sec,
                    is_scheduled=is_scheduled,
                )
                continue

            summary_rows.extend(
                process_symbol_fn(
                    py,
                    base,
                    item,
                    top_n,
                    portfolio_ctx=portfolio_ctx,
                    fx_usd_per_cny=fx_usd_per_cny,
                    hkdcny=hkdcny,
                    timeout_sec=symbol_timeout_sec,
                    is_scheduled=is_scheduled,
                )
            )
        except Exception as e:
            symbol = item0.get('symbol', 'UNKNOWN')
            log(f'[WARN] {symbol} processing failed: {e}')
            summary_rows.append({'symbol': symbol, 'strategy': 'sell_put', 'candidate_count': 0, 'note': f'处理失败: {e}'})
            summary_rows.append({'symbol': symbol, 'strategy': 'sell_call', 'candidate_count': 0, 'note': f'处理失败: {e}'})

    if want_fn('scan'):
        build_symbols_summary_fn(summary_rows)
        build_symbols_digest_fn(summary_rows, int(top_n))

    return summary_rows
