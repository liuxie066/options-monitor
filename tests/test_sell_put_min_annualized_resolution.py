from __future__ import annotations



import pytest

def test_symbol_sell_put_min_overrides_template() -> None:
    from domain.domain.sell_put_config import resolve_min_annualized_net_return

    symbol_cfg = {
        'symbol': 'NVDA',
        'use': ['put_base'],
        'sell_put': {'min_annualized_net_return': 0.12},
    }
    profiles = {'put_base': {'sell_put': {'min_annualized_net_return': 0.08}}}

    assert resolve_min_annualized_net_return(symbol_cfg=symbol_cfg, profiles=profiles) == 0.12


def test_template_sell_put_min_overrides_default() -> None:
    from domain.domain.sell_put_config import resolve_min_annualized_net_return

    symbol_cfg = {
        'symbol': 'NVDA',
        'use': ['put_base'],
        'sell_put': {'min_annualized_net_return': None},
    }
    profiles = {'put_base': {'sell_put': {'min_annualized_net_return': 0.09}}}

    assert resolve_min_annualized_net_return(symbol_cfg=symbol_cfg, profiles=profiles) == 0.09


def test_none_sell_put_min_uses_default() -> None:
    from domain.domain.sell_put_config import DEFAULT_MIN_ANNUALIZED_NET_RETURN, resolve_min_annualized_net_return

    symbol_cfg = {
        'symbol': 'NVDA',
        'use': ['put_base'],
        'sell_put': {'min_annualized_net_return': None},
    }
    profiles = {'put_base': {'sell_put': {'min_annualized_net_return': None}}}

    assert resolve_min_annualized_net_return(symbol_cfg=symbol_cfg, profiles=profiles) == DEFAULT_MIN_ANNUALIZED_NET_RETURN


def test_invalid_sell_put_min_raises() -> None:
    from domain.domain.sell_put_config import resolve_min_annualized_net_return

    symbol_cfg = {
        'symbol': 'NVDA',
        'sell_put': {'min_annualized_net_return': 1.2},
    }

    with pytest.raises(ValueError) as _caught:
        resolve_min_annualized_net_return(symbol_cfg=symbol_cfg, profiles={})
    e = _caught.value
    assert 'within [0, 1]' in str(e)
