from __future__ import annotations

import pytest

import subprocess
import sys
from pathlib import Path


BASE = Path(__file__).resolve().parents[1]
VPY = Path(sys.executable)


def _available_portfolio_ctx() -> dict:
    return {
        'portfolio_source_name': 'futu',
        'capacity_authority': {'status': 'available'},
    }


def test_symbol_sell_call_min_overrides_template() -> None:
    from domain.domain.sell_call_config import resolve_min_annualized_net_premium_return

    symbol_cfg = {
        'symbol': 'AAPL',
        'use': ['call_base'],
        'sell_call': {'min_annualized_net_premium_return': 0.12},
    }
    profiles = {'call_base': {'sell_call': {'min_annualized_net_premium_return': 0.08}}}

    assert resolve_min_annualized_net_premium_return(symbol_cfg=symbol_cfg, profiles=profiles) == 0.12


def test_template_sell_call_min_overrides_default() -> None:
    from domain.domain.sell_call_config import resolve_min_annualized_net_premium_return

    symbol_cfg = {
        'symbol': 'AAPL',
        'use': ['call_base'],
        'sell_call': {'min_annualized_net_premium_return': None},
    }
    profiles = {'call_base': {'sell_call': {'min_annualized_net_premium_return': 0.09}}}

    assert resolve_min_annualized_net_premium_return(symbol_cfg=symbol_cfg, profiles=profiles) == 0.09


def test_none_sell_call_min_uses_default() -> None:
    from domain.domain.sell_call_config import (
        DEFAULT_MIN_ANNUALIZED_NET_PREMIUM_RETURN,
        resolve_min_annualized_net_premium_return,
    )

    symbol_cfg = {
        'symbol': 'AAPL',
        'use': ['call_base'],
        'sell_call': {'min_annualized_net_premium_return': None},
    }
    profiles = {'call_base': {'sell_call': {'min_annualized_net_premium_return': None}}}

    assert (
        resolve_min_annualized_net_premium_return(symbol_cfg=symbol_cfg, profiles=profiles)
        == DEFAULT_MIN_ANNUALIZED_NET_PREMIUM_RETURN
    )


def test_legacy_sell_call_field_still_works() -> None:
    from domain.domain.sell_call_config import resolve_min_annualized_net_premium_return

    symbol_cfg = {
        'symbol': 'AAPL',
        'sell_call': {'min_annualized_net_return': 0.11},
    }

    assert resolve_min_annualized_net_premium_return(symbol_cfg=symbol_cfg, profiles={}) == 0.11


def test_invalid_sell_call_min_raises() -> None:
    from domain.domain.sell_call_config import resolve_min_annualized_net_premium_return

    symbol_cfg = {
        'symbol': 'AAPL',
        'sell_call': {'min_annualized_net_premium_return': 1.2},
    }

    with pytest.raises(ValueError) as _caught:
        resolve_min_annualized_net_premium_return(symbol_cfg=symbol_cfg, profiles={})
    e = _caught.value
    assert 'within [0, 1]' in str(e)


def test_scan_sell_call_requires_min_annualized_arg() -> None:
    p = subprocess.run(
        [
            str(VPY),
            '-m',
            'src.application.scan_sell_call',
            '--symbols',
            'AAPL',
            '--avg-cost',
            '100',
            '--shares',
            '100',
            '--shares-can-sell',
            '100',
            '--shares-locked',
            '0',
        ],
        cwd=str(BASE),
        capture_output=True,
        text=True,
        check=False,
    )

    assert p.returncode != 0
    assert '[ARG_ERROR]' in (p.stderr or '')
    assert '--min-annualized-net-return' in (p.stderr or '')


def test_scan_sell_call_rejects_out_of_range_arg() -> None:
    p = subprocess.run(
        [
            str(VPY),
            '-m',
            'src.application.scan_sell_call',
            '--symbols',
            'AAPL',
            '--avg-cost',
            '100',
            '--shares',
            '100',
            '--shares-can-sell',
            '100',
            '--shares-locked',
            '0',
            '--min-annualized-net-return',
            '1.2',
        ],
        cwd=str(BASE),
        capture_output=True,
        text=True,
        check=False,
    )

    assert p.returncode != 0
    assert '[ARG_ERROR]' in (p.stderr or '')
    assert 'within [0, 1]' in (p.stderr or '')


@pytest.mark.parametrize('missing_flag', ('--shares', '--shares-can-sell', '--shares-locked'))
def test_scan_sell_call_requires_explicit_share_facts(missing_flag: str) -> None:
    from src.application.scan_sell_call import parse_args

    argv = [
        '--symbols', 'AAPL',
        '--avg-cost', '100',
        '--shares', '100',
        '--shares-can-sell', '100',
        '--shares-locked', '0',
    ]
    offset = argv.index(missing_flag)
    del argv[offset:offset + 2]

    with pytest.raises(SystemExit):
        parse_args(argv)


@pytest.mark.parametrize('shares_locked', (-1, 101))
def test_scan_sell_call_rejects_invalid_locked_share_facts(shares_locked: int) -> None:
    p = subprocess.run(
        [
            str(VPY),
            '-m',
            'src.application.scan_sell_call',
            '--symbols',
            'AAPL',
            '--avg-cost',
            '100',
            '--shares',
            '100',
            '--shares-can-sell',
            '100',
            '--shares-locked',
            str(shares_locked),
            '--min-annualized-net-return',
            '0.1',
        ],
        cwd=str(BASE),
        capture_output=True,
        text=True,
        check=False,
    )

    assert p.returncode != 0
    assert '[ARG_ERROR]' in (p.stderr or '')
    assert 'shares_locked' in (p.stderr or '')


def test_sell_call_steps_defers_underwriting_thresholds_to_post_filter() -> None:

    import src.application.sell_call_steps as steps
    import pandas as pd
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    calls: list[dict] = []
    orig_run_sell_call_scan = steps.run_sell_call_scan

    def _fake_run_sell_call_scan(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame()

    steps.run_sell_call_scan = _fake_run_sell_call_scan
    try:
        out = steps.run_sell_call_scan_and_summarize(
            symbol='AAPL',
            symbol_cfg={'symbol': 'AAPL', 'sell_call': {}},
            cc={'enabled': True, 'min_annualized_net_premium_return': 0.123, 'min_strike_cost_multiplier': 1.02},
            required_data_dir=BASE / 'output',
            stock={'shares': 300, 'can_sell_qty': 300, 'avg_cost': 100},
            portfolio_ctx=_available_portfolio_ctx(),
            exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14, cny_per_hkd=0.92)),
            locked_shares_status='available',
            locked_shares_by_symbol={'AAPL': 100},
            locked_shares_unavailable_by_symbol={},
        )
    finally:
        steps.run_sell_call_scan = orig_run_sell_call_scan

    assert out['strategy'] == 'sell_call'
    assert len(calls) >= 1
    kwargs = calls[0]
    assert kwargs['min_annualized_net_return'] == 0.0
    assert kwargs['min_net_income'] == 0.0
    assert kwargs['min_strike'] == 102.0
    assert kwargs['min_strike_cost_multiplier'] == 1.02
    assert kwargs['shares'] == 300
    assert kwargs['shares_can_sell'] == 300
    assert kwargs['shares_locked'] == 100
    assert 'shares_available_for_cover' not in kwargs


def test_sell_call_steps_blocks_when_locked_shares_basis_unavailable(tmp_path: Path) -> None:

    import src.application.sell_call_steps as steps
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    calls: list[dict] = []
    orig_run_sell_call_scan = steps.run_sell_call_scan

    def _fake_run_sell_call_scan(**kwargs):
        calls.append(kwargs)

    steps.run_sell_call_scan = _fake_run_sell_call_scan
    try:
        out = steps.run_sell_call_scan_and_summarize(
            symbol='0700.HK',
            symbol_cfg={'symbol': '0700.HK', 'sell_call': {}},
            cc={'enabled': True, 'min_net_income': 100},
            required_data_dir=tmp_path / 'required_data',
            stock={'shares': 500, 'can_sell_qty': 500, 'avg_cost': 400},
            portfolio_ctx=_available_portfolio_ctx(),
            exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14, cny_per_hkd=0.92)),
            locked_shares_status='available',
            locked_shares_by_symbol={},
            locked_shares_unavailable_by_symbol={'0700.HK': 'short_call_locked_shares_basis_missing'},
        )
    finally:
        steps.run_sell_call_scan = orig_run_sell_call_scan

    assert out['strategy'] == 'sell_call'
    assert out['candidate_count'] == 0
    assert out["_strategy_status"] == "unavailable"
    assert calls == []


@pytest.mark.parametrize(
    ('locked_shares', 'reason'),
    ((-1, 'share_coverage_calc_failed'), (101, 'locked_shares_exceed_eligible_underlying')),
)
def test_sell_call_steps_fail_closed_on_invalid_locked_shares(
    tmp_path: Path,
    locked_shares: int,
    reason: str,
) -> None:
    from src.application.sell_call_steps import run_sell_call_scan_and_summarize
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    out = run_sell_call_scan_and_summarize(
        symbol='AAPL',
        symbol_cfg={'symbol': 'AAPL', 'sell_call': {}},
        cc={'enabled': True},
        required_data_dir=tmp_path / 'required_data',
        stock={'shares': 100, 'can_sell_qty': 100, 'avg_cost': 100},
        portfolio_ctx=_available_portfolio_ctx(),
        exchange_rate_converter=CurrencyConverter(
            ExchangeRates(usd_per_cny=0.14, cny_per_hkd=0.92)
        ),
        locked_shares_status='available',
        locked_shares_by_symbol={'AAPL': locked_shares},
        locked_shares_unavailable_by_symbol={},
    )

    assert out['_strategy_status'] == 'unavailable'
    assert out['_strategy_reason'] == reason


def test_sell_call_steps_blocks_when_account_authority_is_missing(tmp_path: Path) -> None:
    from src.application.sell_call_steps import run_sell_call_scan_and_summarize
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    out = run_sell_call_scan_and_summarize(
        symbol='AAPL',
        symbol_cfg={'symbol': 'AAPL', 'sell_call': {}},
        cc={'enabled': True},
        required_data_dir=tmp_path / 'required_data',
        stock={'shares': 100, 'can_sell_qty': 100, 'avg_cost': 100},
        exchange_rate_converter=CurrencyConverter(
            ExchangeRates(usd_per_cny=0.14, cny_per_hkd=0.92)
        ),
        locked_shares_status='available',
        locked_shares_by_symbol={},
        locked_shares_unavailable_by_symbol={},
    )

    assert out['_strategy_status'] == 'unavailable'
    assert out['_strategy_reason'] == 'physical_account_capacity_authority_unavailable'


def test_sell_call_steps_blocks_when_locked_share_context_is_missing(tmp_path: Path) -> None:
    from src.application.sell_call_steps import run_sell_call_scan_and_summarize
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    out = run_sell_call_scan_and_summarize(
        symbol='AAPL',
        symbol_cfg={'symbol': 'AAPL', 'sell_call': {}},
        cc={'enabled': True},
        required_data_dir=tmp_path / 'required_data',
        stock={'shares': 100, 'can_sell_qty': 100, 'avg_cost': 100},
        portfolio_ctx=_available_portfolio_ctx(),
        exchange_rate_converter=CurrencyConverter(
            ExchangeRates(usd_per_cny=0.14, cny_per_hkd=0.92)
        ),
    )

    assert out['_strategy_status'] == 'unavailable'
    assert out['_strategy_reason'] == 'option_positions_context_unavailable'


def test_sell_call_steps_blocks_when_option_context_is_globally_unavailable(
    tmp_path: Path,
) -> None:

    import src.application.sell_call_steps as steps
    from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates

    stale_path = tmp_path / "reports" / "aapl_sell_call_candidates.csv"
    stale_path.parent.mkdir(parents=True)
    stale_path.write_text("stale\n1\n", encoding="utf-8")

    out = steps.run_sell_call_scan_and_summarize(
        symbol="AAPL",
        symbol_cfg={"symbol": "AAPL", "sell_call": {}},
        cc={"enabled": True},
        required_data_dir=tmp_path / "required_data",
        stock={"shares": 100, "can_sell_qty": 100, "avg_cost": 100},
        portfolio_ctx=_available_portfolio_ctx(),
        exchange_rate_converter=CurrencyConverter(
            ExchangeRates(usd_per_cny=0.14, cny_per_hkd=0.92)
        ),
        locked_shares_status="unavailable",
        locked_shares_unavailable_reason="option_positions_context_unavailable",
        locked_shares_by_symbol={},
        locked_shares_unavailable_by_symbol={},
    )

    assert out["_strategy_status"] == "unavailable"
    assert out["_strategy_reason"] == "option_positions_context_unavailable"
    assert stale_path.read_text(encoding="utf-8") == "stale\n1\n"
