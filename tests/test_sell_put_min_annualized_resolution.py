from __future__ import annotations

import sys
from pathlib import Path


def _add_repo_to_syspath() -> None:
    base = Path(__file__).resolve().parents[1]
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))


def test_symbol_sell_put_min_overrides_template() -> None:
    _add_repo_to_syspath()
    from scripts.sell_put_config import resolve_min_annualized_net_return

    symbol_cfg = {
        'symbol': 'NVDA',
        'use': ['put_base'],
        'sell_put': {'min_annualized_net_return': 0.12},
    }
    profiles = {'put_base': {'sell_put': {'min_annualized_net_return': 0.08}}}

    assert resolve_min_annualized_net_return(symbol_cfg=symbol_cfg, profiles=profiles) == 0.12


def test_template_sell_put_min_overrides_default() -> None:
    _add_repo_to_syspath()
    from scripts.sell_put_config import resolve_min_annualized_net_return

    symbol_cfg = {
        'symbol': 'NVDA',
        'use': ['put_base'],
        'sell_put': {'min_annualized_net_return': None},
    }
    profiles = {'put_base': {'sell_put': {'min_annualized_net_return': 0.09}}}

    assert resolve_min_annualized_net_return(symbol_cfg=symbol_cfg, profiles=profiles) == 0.09


def test_none_sell_put_min_uses_default() -> None:
    _add_repo_to_syspath()
    from scripts.sell_put_config import DEFAULT_MIN_ANNUALIZED_NET_RETURN, resolve_min_annualized_net_return

    symbol_cfg = {
        'symbol': 'NVDA',
        'use': ['put_base'],
        'sell_put': {'min_annualized_net_return': None},
    }
    profiles = {'put_base': {'sell_put': {'min_annualized_net_return': None}}}

    assert resolve_min_annualized_net_return(symbol_cfg=symbol_cfg, profiles=profiles) == DEFAULT_MIN_ANNUALIZED_NET_RETURN


def test_invalid_sell_put_min_raises() -> None:
    _add_repo_to_syspath()
    from scripts.sell_put_config import resolve_min_annualized_net_return

    symbol_cfg = {
        'symbol': 'NVDA',
        'sell_put': {'min_annualized_net_return': 1.2},
    }

    try:
        resolve_min_annualized_net_return(symbol_cfg=symbol_cfg, profiles={})
    except ValueError as e:
        assert 'within [0, 1]' in str(e)
    else:
        raise AssertionError('expected ValueError for invalid min_annualized_net_return')
