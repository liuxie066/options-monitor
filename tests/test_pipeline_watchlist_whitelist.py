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
        'symbols': [
            {'symbol': '0700.HK', 'sell_put': {'enabled': True}, 'sell_call': {'enabled': True}},
            {'symbol': '3690.HK', 'sell_put': {'enabled': True}, 'sell_call': {'enabled': True}},
        ],
        'templates': {},
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


def test_watchlist_whitelist_is_case_insensitive_and_trimmed() -> None:
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
        'symbols': [
            {'symbol': '0700.HK', 'sell_put': {'enabled': True}, 'sell_call': {'enabled': True}},
            {'symbol': '3690.HK', 'sell_put': {'enabled': True}, 'sell_call': {'enabled': True}},
        ],
        'templates': {},
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
        symbols_arg=' 0700.hk ',
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


def test_watchlist_extracts_global_min_net_income_from_profiles() -> None:
    from scripts.pipeline_watchlist import run_watchlist_pipeline

    seen: dict[str, dict] = {}

    def _apply_profiles(item: dict, profiles: dict) -> dict:
        return dict(item)

    def _process_symbol(*args, **kwargs):
        item = args[2]
        seen['put'] = dict(item.get('_global_sell_put_liquidity') or {})
        seen['call'] = dict(item.get('_global_sell_call_liquidity') or {})
        return [{'symbol': str(item.get('symbol')), 'strategy': 'sell_put', 'candidate_count': 0}]

    def _build_ctx(**kwargs):
        return ({}, None, None, None)

    def _noop(*args, **kwargs):
        return None

    cfg = {
        'symbols': [
            {'symbol': '0700.HK', 'use': 'base_profile', 'sell_put': {'enabled': True}, 'sell_call': {'enabled': True}},
        ],
        'templates': {
            'base_profile': {
                'sell_put': {'min_net_income': 100, 'min_open_interest': 50},
                'sell_call': {'min_net_income': 200, 'min_volume': 12},
            }
        },
        'runtime': {},
    }

    run_watchlist_pipeline(
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
        symbols_arg=None,
        log=lambda _: None,
        want_fn=lambda _: True,
        apply_profiles_fn=_apply_profiles,
        process_symbol_fn=_process_symbol,
        build_pipeline_context_fn=_build_ctx,
        build_symbols_summary_fn=_noop,
        build_symbols_digest_fn=_noop,
    )

    assert seen['put']['min_net_income'] == 100
    assert seen['call']['min_net_income'] == 200


def test_watchlist_passes_runtime_config_to_symbol_processor() -> None:
    from scripts.pipeline_watchlist import run_watchlist_pipeline

    seen: list[dict] = []

    def _apply_profiles(item: dict, profiles: dict) -> dict:
        return dict(item)

    def _process_symbol(*args, **kwargs):
        seen.append(dict(kwargs.get("runtime_config") or {}))
        item = args[2]
        return [{"symbol": str(item.get("symbol")), "strategy": "sell_put", "candidate_count": 0}]

    def _build_ctx(**kwargs):
        return ({}, None, None, None)

    def _noop(*args, **kwargs):
        return None

    cfg = {
        "symbols": [
            {"symbol": "0700.HK", "sell_put": {"enabled": True}, "sell_call": {"enabled": False}},
        ],
        "templates": {},
        "runtime": {"option_chain_fetch": {"max_calls": 7}},
    }

    run_watchlist_pipeline(
        py="python",
        base=Path("."),
        cfg=cfg,
        report_dir=Path("."),
        is_scheduled=True,
        top_n=3,
        symbol_timeout_sec=1,
        portfolio_timeout_sec=1,
        want_scan=True,
        no_context=True,
        symbols_arg=None,
        log=lambda _: None,
        want_fn=lambda _: True,
        apply_profiles_fn=_apply_profiles,
        process_symbol_fn=_process_symbol,
        build_pipeline_context_fn=_build_ctx,
        build_symbols_summary_fn=_noop,
        build_symbols_digest_fn=_noop,
    )

    assert seen == [cfg]


def test_resolve_watchlist_item_runtime_config_centralizes_template_expansion() -> None:
    from scripts.pipeline_watchlist import resolve_watchlist_item_runtime_config

    def _apply_profiles(item: dict, profiles: dict) -> dict:
        out = dict(item)
        for name in ([item.get('use')] if isinstance(item.get('use'), str) else item.get('use') or []):
            prof = profiles.get(name) or {}
            for key, value in prof.items():
                if isinstance(value, dict) and isinstance(out.get(key), dict):
                    merged = dict(value)
                    merged.update(out.get(key) or {})
                    out[key] = merged
                else:
                    out.setdefault(key, value)
        return out

    profiles = {
        'put_base': {
            'sell_put': {
                'min_annualized_net_return': 0.12,
                'min_net_income': 100,
                'min_open_interest': 50,
            }
        },
        'call_base': {
            'sell_call': {
                'min_annualized_net_return': 0.11,
                'min_volume': 12,
            }
        },
    }
    item = {
        'symbol': '0700.HK',
        'use': ['put_base', 'call_base'],
        'sell_put': {'enabled': True, 'min_dte': 20},
        'sell_call': {'enabled': True},
    }

    resolved = resolve_watchlist_item_runtime_config(
        item=item,
        profiles=profiles,
        apply_profiles_fn=_apply_profiles,
    )

    assert resolved['sell_put']['enabled'] is True
    assert resolved['sell_put']['min_dte'] == 20
    assert resolved['sell_put']['min_annualized_net_return'] == 0.12
    assert resolved['sell_call']['enabled'] is True
    assert resolved['sell_call']['min_annualized_net_premium_return'] == 0.11
    assert 'min_annualized_net_return' not in resolved['sell_call']
    assert resolved['_global_sell_put_liquidity'] == {'min_net_income': 100, 'min_open_interest': 50}
    assert resolved['_global_sell_call_liquidity'] == {'min_volume': 12}
    assert resolved['_global_sell_put_event_risk'] == {'enabled': True, 'mode': 'warn'}
