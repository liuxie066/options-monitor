"""Early formal phases must use the core owner, including isolated demos."""
from dataclasses import replace
import pytest
import pandas as pd

from domain.domain import strategy_core as core
from test_strategy_core import CONVERTER
from test_strategy_core_integration import frozen_scan, _portfolio, _put_quote, _strategy  # noqa: F401
from test_cc_spot_distance_contract import _quote
from src.application.sell_put_steps import run_sell_put_scan_and_summarize
from src.application.sell_call_steps import run_sell_call_scan_and_summarize


@pytest.mark.parametrize('mode', ['put', 'call'])
@pytest.mark.parametrize('demo', [False, True])
def test_real_early_phase_calls_core_before_underwriting(frozen_scan, monkeypatch, mode, demo):
    calls = []
    owner = core.calculate_capacity
    def spy(row, **kwargs):
        assert 'portfolio_nav_cny' not in row
        assert 'policy_min_dte' not in row
        result = owner(row, **kwargs)
        calls.append((kwargs['capacity_inputs'], result))
        return result
    monkeypatch.setattr(core, 'calculate_capacity', spy)
    common = dict(symbol='NVDA', symbol_cfg={}, required_data_dir=frozen_scan,
                  exchange_rate_converter=CONVERTER, demo_capacity=demo,
                  resolved_strategy=_strategy(mode))
    if mode == 'put':
        result = run_sell_put_scan_and_summarize(
            **common, sp={'strategy': 'insurance_underwriting', 'min_dte': 'ignored', 'max_strike': 'ignored'},
            portfolio_ctx=_portfolio(), required_data_frame=pd.DataFrame([_put_quote('NVDA', 95, 3)]))[0]
        assert all(isinstance(facts, core.PutCapacityInputs) for facts, _ in calls)
        assert len(calls) == (1 if demo else 2)
        assert calls[-1][1]['max_new_contracts'] == (1 if demo else 10)
    else:
        result = run_sell_call_scan_and_summarize(
            **common, cc={'strategy': 'insurance_underwriting', 'min_dte': 'ignored', 'max_strike': 'ignored'},
            stock={'shares': 250, 'can_sell_qty': 200, 'avg_cost': 80},
            portfolio_ctx={'filters': {'account': 'lx'}, 'portfolio_source_name': 'futu',
                           'capacity_authority': {'status': 'available'}},
            locked_shares_status='available', locked_shares_by_symbol={'NVDA': 100},
            locked_shares_unavailable_by_symbol={}, required_data_frame=pd.DataFrame([_quote()]))
        assert len(calls) == 1 and isinstance(calls[0][0], core.CallCapacityInputs)
        assert calls[0][1]['covered_contracts_available'] == 1
        assert calls[0][1]['shares_available_for_cover'] == 100
    assert result['candidate_count'] == 1


@pytest.mark.parametrize('cash,secured,fx,rates,expected,available', [
    ({'USD': 0}, {}, None, CONVERTER, 0, True),
    ({'USD': 9000}, {}, None, CONVERTER, 0, True),
    ({'USD': 20000}, {'USD': 10000}, None, CONVERTER, 1, True),
    ({'USD': 1.0}, {}, None, CONVERTER, 0, True),
    ({'CNY': 100000}, {}, 'fresh', CONVERTER, 1, True),
    ({'CNY': 100000}, {}, 'stale', core.CurrencyConverter(core.ExchangeRates()), 0, False),
    ({}, {}, None, CONVERTER, 0, False),
])
def test_pure_capacity_retains_unknown_zero_occupied_and_fx(cash, secured, fx, rates, expected, available):
    inputs = core.PutCapacityInputs(tuple(cash.items()), tuple(secured.items()), fx)
    out = core.calculate_capacity({'strike': 100, 'multiplier': 100, 'currency': 'USD'}, mode='put',
                                  capacity_inputs=inputs, exchange_rate_converter=rates)
    assert out['max_new_contracts'] == expected
    assert out['put_cash_capacity_available'] is available
    if not available:
        assert out['put_cash_free'] is None
    blocked = core.calculate_capacity({'strike': 100, 'multiplier': 100, 'currency': 'USD'}, mode='put',
        capacity_inputs=replace(inputs, unavailable_reason='qualified_context_missing'), exchange_rate_converter=rates)
    assert blocked['put_cash_free'] is None and blocked['max_new_contracts'] == 0


def test_baseline_tail_order_through_core_remains_input_dependent():
    for mode, field in [('put', 'period_net_return_on_cash_basis'), ('call', 'period_net_premium_return')]:
        strategy = _strategy(mode)
        rows = [{'symbol': symbol, 'contract_symbol': f'{symbol}-{n}', field: value,
                 'strategy_core': strategy.identity}
                for symbol, n, value in [('NVDA', 1, .04), ('NVDA', 2, .01), ('AMD', 1, .08), ('AMD', 2, .02)]]
        once = core.rank_candidates(rows, mode=mode)
        twice = core.rank_candidates(once, mode=mode)
        assert [r['contract_symbol'] for r in once] == ['AMD-1', 'NVDA-1', 'NVDA-2', 'AMD-2']
        assert [r['contract_symbol'] for r in twice] == ['AMD-1', 'NVDA-1', 'AMD-2', 'NVDA-2']
        for symbol in ('NVDA', 'AMD'):
            local = [r for r in rows if r['symbol'] == symbol]
            assert core.rank_candidates(local, mode=mode)[0] == next(r for r in twice if r['symbol'] == symbol)
