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
    ('pending', ['combo_proposal_expired'], False, None, '组合提案已过期，需重新核验确认'),
    ('pending', ['combo_confirmation_required'], False, None, '归属待确认，OM Bot 查看'),
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


def _expired_scope(tmp_path, monkeypatch):
    repo, config, pair, observation, evidence, _ = _scope(tmp_path, monkeypatch)
    old = _read()
    now = pair['proposal_expires_at_ms'] + 1
    monkeypatch.setattr(attribution.time, 'time', lambda: now / 1000)
    observation['portfolio']['position_snapshot_input']['observed_at_utc'] = datetime.fromtimestamp(
        now / 1000, timezone.utc).isoformat()
    repo.expire_combo_pair_inferences(effective_now_ms=now, account='lx')
    return repo, config, pair, observation, evidence, now, old


def test_expired_combo_requires_fresh_manual_preview_and_is_atomic_idempotent(tmp_path, monkeypatch):
    repo, config, pair, observation, evidence, now, old = _expired_scope(tmp_path, monkeypatch)
    before_events = repo.list_trade_events()
    before_pairs = repo.list_combo_pair_inferences(account='lx')
    raw, _, _ = TRADE_ATTRIBUTION_READ_TOOL.call({'account': 'lx'})
    assert all(row['selected_candidate_id'] is None for row in raw['rows'])
    assert all('combo_proposal_expired' in row['reason_codes'] for row in raw['rows'])
    assert all('combo_counterpart_missing_or_asymmetric' not in row['reason_codes'] for row in raw['rows'])
    fresh = _read()
    assert fresh['selected_candidate_id'] is None
    assert fresh['candidates'][0]['inference']['revalidated_expired'] is True
    assert fresh['reason_codes'] == ['combo_confirmation_required']
    assert fresh['input_hash'] != old['input_hash']
    assert repo.list_combo_pair_inferences(account='lx') == before_pairs
    with pytest.raises(ValueError, match='evidence changed'):
        attribution.apply_referenced_trade_attribution(repo, **_args(repo, config, pair, old), apply_changes=True)
    args = _args(repo, config, pair, fresh)
    assert attribution.apply_referenced_trade_attribution(repo, **args, apply_changes=False)['status'] == 'dry_run'
    assert repo.list_trade_events() == before_events
    assert repo.list_combo_pair_inferences(account='lx') == before_pairs
    assert attribution.apply_referenced_trade_attribution(repo, **args, apply_changes=True)['status'] == 'adopted'
    assert len(repo.list_trade_events()) == len(before_events) + 2
    stored = repo.get_combo_pair_inference(pair['inference_id'])
    assert stored['status'] == 'user_confirmed'
    assert stored['proposal_expires_at_ms'] == pair['proposal_expires_at_ms']
    assert attribution.apply_referenced_trade_attribution(repo, **args, apply_changes=True)['status'] == 'already_confirmed'
    assert len(repo.list_trade_events()) == len(before_events) + 2


@pytest.mark.parametrize('change', ['account', 'quantity', 'stale', 'partial', 'scope', 'ambiguous', 'missing_evidence'])
def test_expired_manual_revalidation_preserves_current_guards(tmp_path, monkeypatch, change):
    repo, config, pair, observation, evidence, _, _ = _expired_scope(tmp_path, monkeypatch)
    fresh = _read()
    snapshot = observation['portfolio']['position_snapshot_input']
    if change == 'account':
        observation['portfolio']['capacity_authority']['futu_account_id'] = 'other'
    elif change == 'quantity':
        snapshot['rows'][0]['quantity'] = '2'
    elif change == 'stale':
        snapshot['observed_at_utc'] = '2020-01-01T00:00:00+00:00'
    elif change == 'partial':
        snapshot['completeness'] = 'partial'
    elif change == 'scope':
        snapshot['scope']['markets'] = ['US']
    elif change == 'ambiguous':
        persist_trade_event_object(repo, _call_open('competing-call', 'competing-call-lot'))
    else:
        evidence.update(complete=False, exposures=[])
    before = repo.list_trade_events()
    with pytest.raises(ValueError):
        attribution.apply_referenced_trade_attribution(repo, **_args(repo, config, pair, fresh), apply_changes=True)
    assert repo.list_trade_events() == before
    assert repo.get_combo_pair_inference(pair['inference_id'])['status'] == 'expired_unresolved'
    current = _read()
    with pytest.raises(ValueError):
        attribution.apply_referenced_trade_attribution(repo, **_args(repo, config, pair, current), apply_changes=True)
    assert repo.list_trade_events() == before


def test_expired_combo_rolls_back_reactivation_when_final_capacity_fails(tmp_path, monkeypatch):
    repo, config, pair, observation, evidence, now, _ = _expired_scope(tmp_path, monkeypatch)
    fresh = _read()
    before = repo.list_trade_events()
    pairs = repo.list_combo_pair_inferences(account='lx')
    def stale():
        monkeypatch.setattr(attribution.time, 'time', lambda: (now + 61000) / 1000)
    with pytest.raises(ValueError, match='capacity changed before commit'):
        attribution.apply_trade_attribution(repo, account='lx', market='us', config=config,
            execution_key=fresh['execution_key'], candidate_id=fresh['candidate_ids'][0],
            expected_input_hash=fresh['input_hash'], request_id='expired-rollback', actor='fixture',
            combo_evidence=evidence, capacity_observation=observation, combo_mode='confirm',
            manual=True, before_commit=stale)
    assert repo.list_trade_events() == before
    assert repo.list_combo_pair_inferences(account='lx') == pairs


@pytest.mark.parametrize('status', ['user_rejected', 'superseded'])
def test_manual_preview_does_not_reactivate_other_terminal_proposals(tmp_path, monkeypatch, status):
    repo, _, pair, _, _, _ = _scope(tmp_path, monkeypatch)
    repo.transition_combo_pair_inference(inference_id=pair['inference_id'], expected_statuses=['proposal_ready'],
        new_status=status, expected_input_hash=pair['input_snapshot_hash'], decision_fields={'decision_reason': status})
    assert not any(candidate.get('inference', {}).get('revalidated_expired')
                   for row in TRADE_ATTRIBUTION_READ_TOOL.call({'account': 'lx', 'prepare_confirmation': True})[0]['rows']
                   for candidate in row['candidates'])


def test_control_expired_pair_preview_confirm_and_retry_share_revalidation(tmp_path, monkeypatch):
    from src.application.bot.control import attribution_operations as operations
    from src.application.bot.control.contracts import BotInboundRequest, ControlCommand
    from src.application.bot.control.operation_store import InboundOperationStore
    from src.application.ledger.api import ledger_resource_identity
    repo, config, pair, observation, evidence, _, _ = _expired_scope(tmp_path, monkeypatch)
    for key, value in {'OM_INBOUND_OPERATIONS_ENABLED': '1', 'OM_INBOUND_TRADE_WRITE_ENABLED': '1',
                       'OM_INBOUND_ADMIN_OPEN_IDS': 'wechat:user',
                       'OM_INBOUND_OPERATION_HMAC_KEY': 'isolated-test-key'}.items():
        monkeypatch.setenv(key, value)
    authority = {'config_path': str(tmp_path / 'config.us.json'), 'runtime_root': str(tmp_path),
                 'ledger': ledger_resource_identity(repo), 'account_mapping_hash': 'fixture'}
    monkeypatch.setattr(operations, 'attribution_runtime', lambda **_: (repo, config, authority,
        {'physical_account_ids': ['1001'], 'environment': 'REAL'}))
    monkeypatch.setattr(operations, 'observe_trade_attribution_capacity', lambda **_: deepcopy(observation))
    monkeypatch.setattr(operations, 'read_attribution_combo_evidence', lambda *a, **kw: evidence)
    store = InboundOperationStore(tmp_path / 'audit.sqlite3')
    request = BotInboundRequest(text='归属', sender_id='user', channel='wechat', conversation_id='room', config_key='us')
    fresh = _read()
    before = repo.list_trade_events()
    preview = ControlCommand('attribution_preview', {'account': 'lx', 'execution_key': fresh['execution_key'],
        'action': 'combo', 'target_id': pair['strategy_group_id']})
    operations.handle_attribution_operation(preview, request, command_id='expired-control', store=store)
    assert repo.list_trade_events() == before
    assert repo.get_combo_pair_inference(pair['inference_id'])['status'] == 'expired_unresolved'
    confirm = ControlCommand('attribution_confirm', {'operation_id': 'expired-control'})
    operations.handle_attribution_operation(confirm, request, command_id='confirm', store=store)
    assert store.get('expired-control')['status'] == 'applied'
    assert len(repo.list_trade_events()) == len(before) + 2
    operations.handle_attribution_operation(confirm, request, command_id='retry', store=store)
    assert len(repo.list_trade_events()) == len(before) + 2


def test_expired_pair_with_a_manually_claimed_leg_cannot_confirm(tmp_path, monkeypatch):
    repo, config, pair, observation, evidence, _, _ = _expired_scope(tmp_path, monkeypatch)
    rows = TRADE_ATTRIBUTION_READ_TOOL.call({'account': 'lx', 'prepare_confirmation': True})[0]['rows']
    call = next(row for row in rows if row['contract_key']['option_type'] == 'call')
    attribution.apply_trade_attribution(repo, account='lx', market='us', config=config,
        execution_key=call['execution_key'], candidate_id='ordinary', expected_input_hash=call['input_hash'],
        request_id='claim-call', actor='fixture', manual=True, combo_evidence=evidence,
        capacity_observation=observation, combo_mode='confirm')
    current = _read()
    before = repo.list_trade_events()
    pairs = repo.list_combo_pair_inferences(account='lx')
    with pytest.raises(ValueError):
        attribution.apply_referenced_trade_attribution(repo, **_args(repo, config, pair, current), apply_changes=True)
    assert repo.list_trade_events() == before
    assert repo.list_combo_pair_inferences(account='lx') == pairs


def test_automatic_reconciliation_does_not_reactivate_expired_combo(tmp_path, monkeypatch):
    repo, config, pair, _, evidence, now, _ = _expired_scope(tmp_path, monkeypatch)
    with repo._writer_connection(begin_immediate=True) as conn:
        conn.execute('INSERT INTO trade_attribution_policy_enablings '
            '(broker, physical_account_id, environment, account, market, policy_version, effective_from_ms, '
            'created_at_ms, actor, request_id, request_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            ('futu', '1001', 'REAL', 'lx', 'us', 'trade_attribution.v2', BASE_TIME_MS - 1000,
             BASE_TIME_MS - 1000, 'fixture', 'fixture', 'a' * 64))
    before = repo.list_trade_events()
    pairs = repo.list_combo_pair_inferences(account='lx')
    reconcile_combo_pair_inferences(repo=repo, account='lx', runtime_environment=RUNTIME_ENVIRONMENT,
        effective_now_ms=now, exposures=evidence['exposures'], persist=True)
    monkeypatch.setattr('src.application.trades.inbox.cache_trade_attribution_result', lambda *a, **kw: 0)
    result = attribution.reconcile_trade_attribution_account(repo, account='lx', market='us', config=config,
        runtime_root=tmp_path, inbox_path=tmp_path / 'inbox.sqlite3', combo_mode='auto')
    assert result['linked'] == 0 and result['errors'] == []
    assert repo.list_trade_events() == before
    assert repo.list_combo_pair_inferences(account='lx') == pairs
