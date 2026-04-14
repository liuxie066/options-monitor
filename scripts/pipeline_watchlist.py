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

from scripts.config_profiles import deep_merge
from scripts.sell_call_config import resolve_min_annualized_net_premium_return
from scripts.sell_put_config import resolve_min_annualized_net_return
from domain.domain import normalize_processor_row, normalize_processor_rows

D3_COMMON_FIELDS = (
    'min_open_interest',
    'min_volume',
    'max_spread_ratio',
)


def _extract_d3_event_cfg(side_cfg: dict) -> dict:
    default = {"enabled": True, "mode": "warn"}
    raw = side_cfg.get("d3_event")
    if not isinstance(raw, dict):
        return default
    out = dict(default)
    out.update(raw)
    out["enabled"] = bool(out.get("enabled", True))
    out["mode"] = str(out.get("mode") or "warn").strip().lower() or "warn"
    return out


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


def _resolve_profile_side_cfg(item: dict, profiles: dict, side: str) -> dict:
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
    side_cfg = merged.get(side)
    return dict(side_cfg) if isinstance(side_cfg, dict) else {}


def _extract_d3_fields(side_cfg: dict, *, is_put: bool) -> dict:
    keys = list(D3_COMMON_FIELDS)
    return {k: side_cfg[k] for k in keys if k in side_cfg}


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

            # Resolve min annualized return with a single source-of-truth chain:
            # symbol.sell_put > templates.sell_put > DEFAULT.
            resolved_put_min = resolve_min_annualized_net_return(symbol_cfg=item0, profiles=profiles)
            sell_put_cfg = dict(item.get('sell_put') or {})
            sell_put_cfg['min_annualized_net_return'] = resolved_put_min
            item['sell_put'] = sell_put_cfg

            resolved_call_min = resolve_min_annualized_net_premium_return(symbol_cfg=item0, profiles=profiles)
            sell_call_cfg = dict(item.get('sell_call') or {})
            sell_call_cfg['min_annualized_net_premium_return'] = resolved_call_min
            sell_call_cfg.pop('min_annualized_net_return', None)
            item['sell_call'] = sell_call_cfg
            item['_global_sell_put_d3'] = _extract_d3_fields(
                _resolve_profile_side_cfg(item0, profiles, 'sell_put'),
                is_put=True,
            )
            item['_global_sell_call_d3'] = _extract_d3_fields(
                _resolve_profile_side_cfg(item0, profiles, 'sell_call'),
                is_put=False,
            )
            item['_global_sell_put_d3_event'] = _extract_d3_event_cfg(
                _resolve_profile_side_cfg(item0, profiles, 'sell_put'),
            )
            item['_global_sell_call_d3_event'] = _extract_d3_event_cfg(
                _resolve_profile_side_cfg(item0, profiles, 'sell_call'),
            )

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

            processor_rows = process_symbol_fn(
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
            validated_rows = normalize_processor_rows(processor_rows)
            summary_rows.extend(validated_rows)
        except Exception as e:
            symbol = item0.get('symbol', 'UNKNOWN')
            log(f'[WARN] {symbol} processing failed: {e}')
            summary_rows.append(
                normalize_processor_row(
                    {
                        'symbol': symbol,
                        'strategy': 'sell_put',
                        'candidate_count': 0,
                        'note': f'处理失败: {e}',
                    }
                )
            )
            summary_rows.append(
                normalize_processor_row(
                    {
                        'symbol': symbol,
                        'strategy': 'sell_call',
                        'candidate_count': 0,
                        'note': f'处理失败: {e}',
                    }
                )
            )

    if want_fn('scan'):
        build_symbols_summary_fn(summary_rows)
        build_symbols_digest_fn(summary_rows, int(top_n))

    return summary_rows
