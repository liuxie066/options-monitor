"""Regression: watchlist runner should honor --symbols whitelist."""

from __future__ import annotations

from pathlib import Path


def test_watchlist_whitelist_filters_symbols() -> None:
    from scripts.pipeline_watchlist import run_watchlist_pipeline

    calls: list[str] = []

    def _apply_profiles(item: dict, profiles: dict) -> dict:
        return dict(item)

    def _process_symbol(*args, **kwargs):
        item = args[2]
        calls.append(str(item.get('symbol')))
        return [{'symbol': str(item.get('symbol')), 'strategy': 'sell_put', 'candidate_count': 0}]

    def _build_ctx(**kwargs):
        return ({}, None, None, None)

    def _noop(*args, **kwargs):
        return None

    cfg = {
        'watchlist': [
            {'symbol': '0700.HK', 'sell_put': {'enabled': True}, 'sell_call': {'enabled': True}},
            {'symbol': '3690.HK', 'sell_put': {'enabled': True}, 'sell_call': {'enabled': True}},
        ],
        'profiles': {},
        'runtime': {},
    }

    out = run_watchlist_pipeline(
        py='python',
        base=Path('.'),
        cfg=cfg,
        report_dir=Path('.'),
        is_scheduled=True,
        top_n=3,
        symbol_timeout_sec=1,
        portfolio_timeout_sec=1,
        want_scan=True,
        no_context=True,
        symbols_arg='0700.HK',
        log=lambda _: None,
        want_fn=lambda _: True,
        apply_profiles_fn=_apply_profiles,
        process_symbol_fn=_process_symbol,
        build_pipeline_context_fn=_build_ctx,
        build_symbols_summary_fn=_noop,
        build_symbols_digest_fn=_noop,
    )

    assert calls == ['0700.HK']
    assert len(out) == 1
