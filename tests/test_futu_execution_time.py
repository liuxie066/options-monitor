from __future__ import annotations

import pytest

from domain.domain.trade_execution import (
    execution_instant_milliseconds,
    futu_execution_time,
)
from src.application.trades.normalizer import normalize_trade_deal


@pytest.mark.parametrize(('source', 'expected', 'zone'), [
    ({'code': 'US.NVDA', 'create_time': '2026-09-08 10:43:37.674'}, '2026-09-08T14:43:37.674Z', 'America/New_York'),
    ({'code': 'US.NVDA', 'create_time': '2026-01-08 10:43:37.674'}, '2026-01-08T15:43:37.674Z', 'America/New_York'),
    ({'code': 'US.NVDA', 'create_time': '2026-09-08 23:43:37.123456789'}, '2026-09-09T03:43:37.123456789Z', 'America/New_York'),
    ({'code': 'HK.00700', 'stock_name': 'TENCENT', 'create_time': '2026-09-08 10:43:37.674'}, '2026-09-08T02:43:37.674Z', 'Asia/Hong_Kong'),
    ({'code': 'US.NVDA', 'create_time': '2026-09-08T10:43:37+08:00'}, '2026-09-08T02:43:37Z', '+08:00'),
    ({'market': 'SH', 'code': 'SH.600000', 'create_time': '2026-09-08 10:43:37'}, '2026-09-08T02:43:37Z', 'Asia/Shanghai'),
    ({'code': 'US.NVDA', 'source_timezone': 'Asia/Shanghai', 'create_time': '2026-09-08 10:43:37'}, '2026-09-08T02:43:37Z', 'Asia/Shanghai'),
    ({'createTimestamp': '1788878617.123456789'}, '2026-09-08T14:43:37.123456789Z', 'UTC'),
    ({'trade_time_ms': '1788878617123.456789', 'createTimestamp': 'bad'}, '2026-09-08T14:43:37.123456789Z', 'UTC'),
    ({'occurred_at_utc': '2026-09-08T14:43:37Z', 'trade_time_ms': 'bad'}, '2026-09-08T14:43:37Z', 'UTC'),
    ({'occurred_at_utc': '', 'trade_time_ms': 0}, '1970-01-01T00:00:00Z', 'UTC'),
])
def test_futu_time_authority_and_market(source, expected, zone):
    result = futu_execution_time(source)
    assert result['errors'] == []
    assert result['occurred_at_utc'] == expected
    assert result['source_timezone'] == zone


@pytest.mark.parametrize('source', [
    {}, {'create_time': '2026-09-08 10:43:37'},
    {'market': 'HK', 'code': 'US.NVDA', 'create_time': '2026-09-08 10:43:37'},
    {'code': 'US.NVDA', 'source_timezone': 'invalid', 'create_time': '2026-09-08 10:43:37'},
    {'code': 'US.NVDA', 'create_time': '2026-11-01 01:30:00'},
    {'code': 'US.NVDA', 'create_time': '2026-03-08 02:30:00'},
    {'code': 'US.NVDA', 'create_time': '2026-02-30 10:30:00'},
    {'trade_time_ms': 'bad', 'create_time': '2026-09-08T10:43:37Z'},
    {'createTimestamp': False, 'create_time': '2026-09-08T10:43:37Z'},
    {'createTimestamp': 'NaN'}, {'createTimestamp': '1e999'},
])
def test_futu_time_unavailable_never_falls_back(source):
    result = futu_execution_time(source)
    assert result['occurred_at_utc'] is None
    assert result['errors']


def test_ingress_uses_same_instant_for_execution_and_ledger():
    deal = normalize_trade_deal({
        'code': 'US.NVDA', 'create_time': '2026-09-08 10:43:37.674999999',
        'qty': 1, 'price': 10, 'side': 'BUY',
    }, allow_opend_refresh=False)
    assert deal.trade_time_ms == 1788878617674
    assert deal.execution_input['occurred_at_utc'] == '2026-09-08T14:43:37.674999999Z'
    assert execution_instant_milliseconds('1969-12-31T23:59:59.999999999Z') == -1


def test_raw_manual_broker_evidence_uses_same_owner():
    from src.application.trades.manual_lifecycle_resolution import _trade_time_ms
    assert _trade_time_ms({'code': 'US.FUTU', 'create_time': '2026-09-08 10:43:37.674'}) == 1788878617674
    assert _trade_time_ms({'event_time_ms': 1788878617674, 'create_time': 'bad'}) == 1788878617674
    assert _trade_time_ms({'create_time': '2026-09-08 10:43:37'}) == 0


def test_time_correction_changes_content_but_not_execution_identity():
    from domain.domain.trade_execution import _futu_execution_input, execution_identity_from_input, execution_economic_content
    raw = {'code': 'US.NVDA', 'create_time': '2026-09-08 10:43:37',
           'futu_account_id': 'test-account', 'trd_env': 'REAL', 'external_id_namespace': 'futu.deal', 'deal_id': 'test-deal'}
    old = _futu_execution_input({**raw, 'source_timezone': 'Asia/Shanghai'})
    new = _futu_execution_input(raw)
    assert execution_identity_from_input(old) == execution_identity_from_input(new) != ''
    assert execution_economic_content(old) != execution_economic_content(new)
