from copy import deepcopy
from datetime import datetime, timezone

import pytest

from src.application.agent_tools.positions import TRADE_ATTRIBUTION_READ_TOOL
from src.application.futu_portfolio_context import build_futu_position_snapshot
from src.application.ledger.api import read_trade_attribution_snapshot
from src.application.ledger.combo_reconciliation import reconcile_combo_pair_inferences
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.writer import persist_trade_event_object
from src.application.trades import attribution
from src.application.wheel.capacity import trade_attribution_capacity_check
from test_combo_reconciliation_application import BASE_TIME_MS, RUNTIME_ENVIRONMENT, _call_open, _put_open
from test_combo_reconciliation_domain import _exposure


def _scope(tmp_path, monkeypatch):
    repo = SQLiteOptionPositionsRepository(tmp_path / 'ledger.sqlite3')
    persist_trade_event_object(repo, _call_open())
    persist_trade_event_object(repo, _put_open())
    now = BASE_TIME_MS + 3000
    monkeypatch.setattr(attribution.time, 'time', lambda: now / 1000)
    config = {'accounts': ['lx'], 'market': 'us', 'account_settings': {
        'lx': {'futu': {'account_id': '1001', 'trd_env': 'REAL'}}},
        'trade_intake': {'combo_reconciliation': {'accounts': {'lx': 'confirm'}}}}
    evidence = {'complete': True, 'exposures': [_exposure(delivery_confirmed=True)]}
    reconcile_combo_pair_inferences(repo=repo, account='lx', runtime_environment=RUNTIME_ENVIRONMENT,
        effective_now_ms=now, exposures=evidence['exposures'], persist=True)
    pair, = repo.list_combo_pair_inferences(account='lx')
    snapshot = build_futu_position_snapshot(rows=[
        {'code': 'US.NVDA260821P100000', 'stock_owner': 'US.NVDA', 'sec_type': 'OPTION', 'qty': -1,
         'option_type': 'PUT', 'option_strike_price': 100, 'strike_time': '2026-08-21', 'multiplier': 100},
        {'code': 'US.NVDA260821C110000', 'stock_owner': 'US.NVDA', 'sec_type': 'OPTION', 'qty': 1,
         'option_type': 'CALL', 'option_strike_price': 110, 'strike_time': '2026-08-21', 'multiplier': 100},
        {'code': 'US.VOO', 'sec_type': 'STOCK', 'qty': 2.8758},
    ], broker_account_ref={'broker_id': 'futu', 'external_account_id': '1001', 'environment': 'REAL',
        'account_label': 'lx', 'broker_account_id': 'futu:REAL:1001'}, markets=['US', 'HK'],
        asset_types=['stock', 'option'], completeness='complete',
        observed_at_utc=datetime.fromtimestamp(now / 1000, timezone.utc).isoformat())
    observation = {'portfolio': {'capacity_authority': {'status': 'available', 'logical_account': 'lx',
        'futu_account_id': '1001', 'trd_env': 'REAL', 'market': 'us'}, 'position_snapshot_input': snapshot,
        'cash_by_currency': {'USD': 0}, 'exchange_rates': {'rates': {'HKDCNY': 0.8542}},
        'exchange_rate_status': 'unavailable'}}
    monkeypatch.setattr(attribution, 'attribution_runtime', lambda **_: (repo, config,
        {'runtime_root': str(tmp_path), 'config_path': str(tmp_path / 'config.json')}, {}))
    monkeypatch.setattr(attribution, 'read_attribution_combo_evidence', lambda *a, **kw: evidence)
    monkeypatch.setattr('src.application.wheel.capacity.observe_trade_attribution_capacity',
        lambda **_: deepcopy(observation))
    return repo, config, pair, observation, evidence, now


def _read():
    data, _, _ = TRADE_ATTRIBUTION_READ_TOOL.call({'account': 'lx', 'prepare_confirmation': True})
    return next(row for row in data['rows'] if row['contract_key']['option_type'] == 'put')


def _args(repo, config, pair, fact):
    return dict(account='lx', config=config, runtime_root=repo.db_path.parent,
        inference_id=pair['inference_id'], expected_input_hash=fact['input_hash'],
        request_id='combo-boundary:test', actor='fixture:operator')


def test_combo_public_preview_commit_ignores_unrelated_stock_cash_and_fx(tmp_path, monkeypatch):
    repo, config, pair, observation, evidence, now = _scope(tmp_path, monkeypatch)
    first = _read()
    assert first['candidates'][0]['eligible']
    assert first['reason_codes'] == ['awaiting_ledger_commit']
    args = _args(repo, config, pair, first)
    before = repo.list_trade_events()
    assert attribution.apply_referenced_trade_attribution(repo, **args, apply_changes=False)['status'] == 'dry_run'
    assert repo.list_trade_events() == before
    portfolio = observation['portfolio']
    portfolio['cash_by_currency'] = {'USD': -500, 'HKD': 123}
    portfolio['exchange_rates']['rates']['HKDCNY'] = 0.8541168432
    portfolio['exchange_rate_status'] = 'ready'
    stock = next(row for row in portfolio['position_snapshot_input']['rows']
                 if row['instrument_ref']['asset_type'] == 'stock')
    stock['quantity'] = '3.8758'
    assert _read()['input_hash'] == first['input_hash']
    applied = attribution.apply_referenced_trade_attribution(repo, **args, apply_changes=True)
    assert applied['status'] == 'adopted'
    assert len(repo.list_trade_events()) == len(before) + 2
    view = attribution.build_trade_attribution_view(read_trade_attribution_snapshot(repo, account='lx', market='us'),
        config=config, account='lx', market='us', now_ms=now, combo_evidence=evidence,
        capacity_observation=observation)
    assert {row['status'] for row in view['rows']} == {'linked'}
    assert {row['strategy_group_id'] for row in view['rows']} == {pair['strategy_group_id']}
    assert attribution.apply_referenced_trade_attribution(repo, **args, apply_changes=True)['status'] == 'already_confirmed'
    assert len(repo.list_trade_events()) == len(before) + 2


@pytest.mark.parametrize('change', ['account', 'quantity', 'fractional_option', 'stale', 'partial', 'scope', 'pair'])
def test_combo_confirmation_preserves_evidence_guards(tmp_path, monkeypatch, change):
    repo, config, pair, observation, _, _ = _scope(tmp_path, monkeypatch)
    fact = _read()
    args = _args(repo, config, pair, fact)
    snapshot = observation['portfolio']['position_snapshot_input']
    if change == 'account':
        observation['portfolio']['capacity_authority']['futu_account_id'] = 'other'
    elif change in {'quantity', 'fractional_option'}:
        snapshot['rows'][0]['quantity'] = '2' if change == 'quantity' else '1.5'
    elif change == 'stale':
        snapshot['observed_at_utc'] = '2020-01-01T00:00:00+00:00'
    elif change == 'partial':
        snapshot['completeness'] = 'partial'
    elif change == 'scope':
        snapshot['scope']['markets'] = ['US']
    else:
        persist_trade_event_object(repo, _call_open('other-call', 'other-call-lot'))
    before = repo.list_trade_events()
    with pytest.raises(ValueError):
        attribution.apply_referenced_trade_attribution(repo, **args, apply_changes=True)
    assert repo.list_trade_events() == before


def test_combo_final_position_check_rolls_back_both_members(tmp_path, monkeypatch):
    repo, config, _, observation, evidence, now = _scope(tmp_path, monkeypatch)
    fact = _read()
    before = repo.list_trade_events()
    def expire():
        monkeypatch.setattr(attribution.time, 'time', lambda: (now + 61000) / 1000)
    with pytest.raises(ValueError, match='capacity changed before commit'):
        attribution.apply_trade_attribution(repo, account='lx', market='us', config=config,
            execution_key=fact['execution_key'], candidate_id=fact['candidate_ids'][0],
            expected_input_hash=fact['input_hash'], request_id='final-expiry', actor='fixture',
            combo_evidence=evidence, capacity_observation=observation, combo_mode='confirm',
            manual=True, before_commit=expire)
    assert repo.list_trade_events() == before
    assert repo.list_combo_pair_inferences(account='lx')[0]['status'] != 'user_confirmed'


@pytest.mark.parametrize('quantity,valid', [('100.25', True), ('-1', False), ('NaN', False)])
def test_wheel_stock_quantity_allows_fractions_but_not_invalid_values(tmp_path, monkeypatch, quantity, valid):
    from test_trade_attribution_view import _writable_call_scope, _call_capacity_observation
    repo, config = _writable_call_scope(tmp_path, monkeypatch)
    observation = _call_capacity_observation()
    snapshot = observation['portfolio']['position_snapshot_input']
    stock = next(row for row in snapshot['rows'] if row['instrument_ref']['asset_type'] == 'stock')
    stock['quantity'] = quantity
    facts = attribution.trade_attribution_facts_from_events(repo.list_trade_events(), account='lx')
    call = next(row for row in facts if row['contract_key']['option_type'] == 'call')
    result = trade_attribution_capacity_check(fact=call, facts=facts, config=config,
        wheel_read_model={'wheel_branches': []}, observation=observation, now_ms=4000)
    assert (result['status'] == 'available') is valid


@pytest.mark.parametrize('status,reasons,enabled,selected,text', [
    ('pending', ['awaiting_ledger_commit'], True, 'combo:pair', '系统归属处理中'),
    ('pending', ['awaiting_ledger_commit'], False, None, '归属待确认，OM Bot 查看'),
    ('pending', ['multiple_strategy_candidates'], True, None, '归属待确认，OM Bot 查看'),
    ('pending', ['capacity_basis_unavailable'], True, None, '归属暂受阻，需核对'),
    ('pending', ['attribution_evidence_incomplete'], False, None, '归属证据不足，需核对'),
    ('conflict', ['late_competing_evidence'], True, None, '归属冲突，需核对'),
    ('pending', [], False, None, '归属待核对'),
])
def test_brief_and_receipt_preserve_pending_cause(tmp_path, monkeypatch, status, reasons, enabled, selected, text):
    from src.application import daily_decision_brief_service as service
    from src.application.daily_decision_brief_renderer import render_fixed_report
    from src.application.trades.receipt import build_trade_intake_receipt_message
    from test_daily_decision_brief_renderer import _brief, _scheduled_context
    repo, config, _, observation, _, now = _scope(tmp_path, monkeypatch)
    fact = _read()
    fact.update(status=status, reason_codes=reasons, rules_enabled=enabled, selected_candidate_id=selected)
    monkeypatch.setattr(service, 'resolve_position_ledger_sqlite_path', lambda **_: repo.db_path)
    monkeypatch.setattr(attribution, 'build_trade_attribution_view', lambda *a, **kw: {'rows': [fact]})
    rows, error = service._pending_attribution_for_brief(base=tmp_path, config=config, account='lx',
        market='us', now_ms=now, capacity_observation=observation)
    assert error is None
    assert rows[0]['reason_codes'] == reasons
    brief = deepcopy(_brief())
    brief['attribution_pending'] = rows
    rendered = render_fixed_report(brief, context=_scheduled_context())
    assert text in rendered and 'NVDA 2026-08-21 100 PUT' in rendered
    assert 'execution:v1:' not in rendered
    receipt = build_trade_intake_receipt_message(deal=None, result={'status': 'applied', 'reason': 'applied_open',
        'account': 'lx', 'action': 'open', 'deal_id': 'fill', 'attribution_result': fact}, payload={})
    assert text in receipt and '查询入口' in receipt
    assert '归属待人工确认' not in receipt
    assert '确认入口' not in receipt


def test_exact_combo_is_automatically_attributable_with_fractional_stock(tmp_path, monkeypatch):
    repo, config, _, observation, evidence, now = _scope(tmp_path, monkeypatch)
    with repo._writer_connection(begin_immediate=True) as conn:
        conn.execute("INSERT INTO trade_attribution_policy_enablings "
            "(broker, physical_account_id, environment, account, market, policy_version, effective_from_ms, "
            "created_at_ms, actor, request_id, request_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ('futu', '1001', 'REAL', 'lx', 'us', 'trade_attribution.v2', BASE_TIME_MS - 1000, BASE_TIME_MS - 1000, 'fixture', 'fixture', 'a' * 64))
    fact = _read()
    assert fact['selected_candidate_id'] == fact['candidate_ids'][0]
    result = attribution.apply_trade_attribution(repo, account='lx', market='us', config=config,
        execution_key=fact['execution_key'], candidate_id=fact['selected_candidate_id'],
        expected_input_hash=fact['input_hash'], request_id='auto-pair', actor='fixture:rule',
        combo_evidence=evidence, capacity_observation=observation, combo_mode='confirm')
    assert result['status'] == 'linked' and result['write_applied']


def test_competing_wheel_candidate_keeps_fx_in_confirmation_hash(tmp_path, monkeypatch):
    repo, config, _, observation, evidence, now = _scope(tmp_path, monkeypatch)
    # Isolate candidate fingerprinting from the independently tested Wheel projector.
    branch = {'wheel_branch_id': 'competing-put', 'symbol': 'NVDA', 'direction': 'put',
        'lifecycle_status': 'active', 'integrity_status': 'trusted', 'remaining_contracts': 1,
        'batch_generation_hash': 'generation', 'source_trade_event_id': None}
    model = {'wheel_branches': [branch]}
    monkeypatch.setattr(attribution, 'build_wheel_read_model_with_capacity_from_rows', lambda *a, **kw: (model, model))
    monkeypatch.setattr(attribution, 'build_wheel_read_model_from_rows', lambda *a, **kw: model)
    monkeypatch.setattr(attribution, 'resolve_wheel_fill_intent', lambda *a, **kw: {
        'reason_codes': [], 'intent': None, 'reserved_contracts_to_consume': 0})
    rows = read_trade_attribution_snapshot(repo, account='lx', market='us')
    def read_put():
        view = attribution.build_trade_attribution_view(rows, config=config, account='lx', market='us',
            now_ms=now, combo_evidence=evidence, capacity_observation=observation)
        return next(row for row in view['rows'] if row['contract_key']['option_type'] == 'put')
    first = read_put()
    assert {row['strategy'] for row in first['candidates']} == {'wheel', 'combo_yield'}
    observation['portfolio']['exchange_rates']['rates']['HKDCNY'] = 0.8541168432
    assert read_put()['input_hash'] != first['input_hash']
