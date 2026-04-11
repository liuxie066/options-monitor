from __future__ import annotations


def test_cash_footer_for_account_keeps_header_and_asof_when_matched() -> None:
    from om.domain.multi_tick import cash_footer_for_account

    lines = [
        '**💰 现金 CNY**',
        '- **LX**: CNY 100',
        '- **SY**: CNY 200',
        '> 截至 2026-04-11 20:00:00',
    ]

    out = cash_footer_for_account(lines, 'lx')
    assert out == [
        '**💰 现金 CNY**',
        '- **LX**: CNY 100',
        '',
        '> 截至 2026-04-11 20:00:00',
    ]


def test_cash_footer_for_account_returns_empty_when_not_matched() -> None:
    from om.domain.multi_tick import cash_footer_for_account

    lines = [
        '**💰 现金 CNY**',
        '- **SY**: CNY 200',
        '> 截至 2026-04-11 20:00:00',
    ]
    assert cash_footer_for_account(lines, 'lx') == []


def test_reduce_trading_day_guard_narrows_markets_when_false_present() -> None:
    from om.domain.multi_tick import reduce_trading_day_guard

    out = reduce_trading_day_guard(
        markets_to_run=['HK', 'US'],
        guard_results=[
            {'market': 'HK', 'is_trading_day': False},
            {'market': 'US', 'is_trading_day': True},
        ],
    )

    assert out['should_skip'] is False
    assert out['markets_to_run'] == ['US']
    assert out['skip_message'] == ''


def test_reduce_trading_day_guard_offhours_uses_true_markets_when_false_exists() -> None:
    from om.domain.multi_tick import reduce_trading_day_guard

    out = reduce_trading_day_guard(
        markets_to_run=[],
        guard_results=[
            {'market': 'HK', 'is_trading_day': True},
            {'market': 'US', 'is_trading_day': False},
        ],
    )

    assert out['should_skip'] is False
    assert out['markets_to_run'] == ['HK']


def test_reduce_trading_day_guard_skip_when_all_false() -> None:
    from om.domain.multi_tick import reduce_trading_day_guard

    out = reduce_trading_day_guard(
        markets_to_run=['US'],
        guard_results=[{'market': 'US', 'is_trading_day': False}],
    )

    assert out['should_skip'] is True
    assert out['markets_to_run'] == []
    assert out['skip_message'] == 'non-trading day: US'


def test_select_scheduler_state_filename_keeps_existing_mapping() -> None:
    from om.domain.multi_tick import select_scheduler_state_filename

    assert select_scheduler_state_filename(['HK']) == 'scheduler_state_hk.json'
    assert select_scheduler_state_filename(['US']) == 'scheduler_state_us.json'
    assert select_scheduler_state_filename(['HK', 'US']) == 'scheduler_state.json'
    assert select_scheduler_state_filename([]) == 'scheduler_state.json'
