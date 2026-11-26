import pandas as pd
import pytest

from cash_evidence_helpers import cash_portfolio
from conftest import phase2_opening_row
from domain.domain.ledger import ContractKey, TradeEvent
from src.application.daily_decision_brief_renderer import render_fixed_report, select_daily_brief_notification
from src.application.daily_decision_brief_service import _load_wheel_snapshot_family
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.writer import persist_trade_event_objects_atomically
from src.application.wheel.candidate_snapshot import seal_wheel_candidate_snapshot
from src.application.wheel.capacity import build_shared_coverage_facts, finalize_wheel_capacity, finalize_wheel_put_capacity
from src.application.wheel.read_model import build_wheel_read_model
from src.application.wheel.scanning import run_wheel_call_scan, run_wheel_put_scan
from tests.test_daily_decision_brief_renderer import _brief, _scheduled_context
from tests.test_wheel_assignment_companions import _assignment_payload, _open_activation, _put_event
from tests.test_wheel_candidate_snapshot import _dependencies
from tests.test_wheel_scanning import AS_OF, _call_row, _converter, _policy


@pytest.mark.parametrize('shares,expected', [(200, 0), (100, 0)])
def test_ledger_unresolved_call_blocks_recommendation_without_rewriting_events(tmp_path, shares, expected):
    from tests.test_wheel_workflows import _wheel_repo, _open_unlinked_call
    repo, _ = _wheel_repo(tmp_path)
    _open_unlinked_call(repo, event_time_ms=2500)
    before = (repo.list_trade_events(), repo.list_wheel_events(account='lx'))
    model = build_wheel_read_model(repo, 'lx', 3000, market='us',
        monitoring_readiness={'monitoring_gate': 'enabled'})
    assert model['wheel_branches'][0]['phase'] == 'linkage_unresolved'
    facts = build_shared_coverage_facts(account='lx', wheel_read_model=model,
        portfolio_context=cash_portfolio({'stocks_by_symbol': {'NVDA': {'shares': shares, 'can_sell_qty': shares}}}),
        option_context={'decision_snapshot_status': 'trusted', 'locked_shares_status': 'available',
            'locked_shares_by_symbol': {'NVDA': 100}})
    scan = run_wheel_call_scan(model, _policy(), {'frames': {'NVDA': pd.DataFrame([phase2_opening_row(_call_row())])}},
        {fact['symbol']: fact for fact in facts}, {'exchange_rate_converter': _converter()},
        decision_time_ms=int(AS_OF.timestamp() * 1000))
    captured = finalize_wheel_capacity(account='lx', wheel_read_model=model, wheel_scan=scan,
        opening_call_candidates=[], coverage_facts=facts)
    assert sum(row['granted_contracts'] for row in captured['batches']) == expected, captured
    assert (repo.list_trade_events(), repo.list_wheel_events(account='lx')) == before
    assert not build_wheel_read_model(repo, 'sy', 3000, market='us')['wheel_branches']


def test_multi_call_occupancy_uses_real_ledger_before_batch_cost_scan(tmp_path):
    from tests.test_wheel_workflows import _trusted_multiplier_payload
    from src.application.ledger.assigned_stock_projection import build_wheel_read_model_from_rows

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _open_activation(repo)
    for index, price in enumerate([110, 97.45, 97.45, 97.45]):
        opened, assigned = (100, 200) if index == 0 else (1000 + index, 2000 + index)
        lot_id = f"put-{index}"
        key = ContractKey.from_values(broker="富途", account="lx", underlying_symbol="NVDA",
            option_type="put", strike=price, expiration_ymd="2026-03-20")
        persist_trade_event_objects_atomically(repo, [
            TradeEvent(event_id=lot_id, event_type="open", event_time_ms=opened, contract_key=key,
                contracts=1, price=2, multiplier=100, currency="USD", source="test", lot_id=lot_id,
                raw_payload=_trusted_multiplier_payload(lot_id, side="sell")),
            TradeEvent(event_id=f"assign-{index}", event_type="assignment", event_time_ms=assigned,
                contract_key=key, contracts=1, price=0, multiplier=100, currency="USD", source="test",
                target_lot_id=lot_id, raw_payload={"side": "buy", "target_lot_id": lot_id,
                    "stock_settlement": {"side": "buy", "shares": 100, "price": price, "fees": 0,
                        "currency": "USD", "fee_provenance": {"basis": "actual", "source": "test"}}}),
        ])
    persist_trade_event_objects_atomically(repo, [TradeEvent(
        event_id="three-calls", event_type="open", event_time_ms=2500,
        contract_key=ContractKey.from_values(broker="富途", account="lx", underlying_symbol="NVDA",
            option_type="call", strike=105, expiration_ymd="2026-05-06"),
        contracts=3, price=2, multiplier=100, currency="USD", source="test", lot_id="three-calls",
        raw_payload=_trusted_multiplier_payload("three-calls", side="sell", strategy="cc"),
    )])
    before = (repo.list_trade_events(), repo.list_wheel_events(account="lx"))
    model = build_wheel_read_model(repo, "lx", 3000, market="us",
        monitoring_readiness={"monitoring_gate": "enabled"})
    assert len(model["wheel_branches"]) == 3
    assert {b["shares_remaining"] for b in model["wheel_branches"]} == {100}
    assert {b["phase"] for b in model["wheel_branches"]} == {"linkage_unresolved"}
    # A 300-share Call must not become a whole-lot linkage choice for a 100-share batch.
    assert model["linkage_candidates"] == []
    historical = build_wheel_read_model_from_rows(repo.read_lifecycle_account_rows(account="lx"),
        account="lx", as_of_ms=3000, market="us", _historical=True,
        monitoring_readiness={"monitoring_gate": "enabled"})
    assert historical["wheel_branches"] == model["wheel_branches"]
    facts = build_shared_coverage_facts(account="lx", wheel_read_model=model,
        portfolio_context=cash_portfolio({"stocks_by_symbol": {"NVDA": {"shares": 400, "can_sell_qty": 400}}}),
        option_context={"decision_snapshot_status": "trusted", "locked_shares_status": "available",
            "locked_shares_by_symbol": {"NVDA": 300}})
    assert facts[0]["shares_available_for_cover"] == 100
    scan = run_wheel_call_scan(model, _policy(),
        {"frames": {"NVDA": pd.DataFrame([phase2_opening_row(_call_row(strike=100.5, spot=85.6))])}},
        {f["symbol"]: f for f in facts}, {"exchange_rate_converter": _converter()},
        decision_time_ms=int(AS_OF.timestamp() * 1000))
    final = finalize_wheel_capacity(account="lx", wheel_read_model=model, wheel_scan=scan,
        opening_call_candidates=[], coverage_facts=facts)
    assert sum(b["final_candidate"] is not None for b in final["batches"]) == 0
    assert sum(b["granted_contracts"] for b in final["batches"]) == 0
    assert (repo.list_trade_events(), repo.list_wheel_events(account="lx")) == before


@pytest.mark.parametrize('symbol_count', [170, 200])
def test_many_wheel_gaps_keep_all_reasons_without_blocking_notification(symbol_count):
    brief = _brief()
    brief['wheel_batches'] = [{
        'wheel_branch_id': f'gap-{index}',
        'symbol': f'WHEEL{index:03d}',
        'direction': 'call',
        'status': 'data_unavailable',
        'recommended_contracts': 0,
        'reason_code': 'data_unavailable',
        'reason_codes': [
            'multiplier_unproven', 'assignment_cash_facts_unavailable',
            'assignment_currency_unavailable',
        ],
    } for index in range(symbol_count)]
    selection = select_daily_brief_notification(
        brief, delivery_kind='fixed_report', context=_scheduled_context(),
    )
    for message in (selection['text'], '\n\n'.join(selection['markdown_blocks'])):
        assert 0 < len(message) <= 12_000
        assert 'WHEEL000 Wheel Call、WHEEL001 Wheel Call、WHEEL002 Wheel Call' in message
        assert f'另有 {symbol_count - 3} 个标的/方向，共 {symbol_count} 个' in message
        assert '候选数据不足，无法评估' in message
        assert '本次记录未提供具体缺项' not in message
        assert '合约乘数来源未核实，暂停推荐' in message
        assert '指派交割金额或实际费用证据不完整，暂停推荐' in message
        assert '指派交割币种缺失，暂停推荐' in message
        assert 'WHEEL003 Wheel Call' not in message


def test_incomplete_assignment_is_visible_in_brief_without_trade_capacity(tmp_path):
    repo = SQLiteOptionPositionsRepository(tmp_path / 'ledger.sqlite3')
    persist_trade_event_objects_atomically(repo, [_put_event(
        event_id='put-open', event_type='open', multiplier=100,
        raw_payload={'multiplier_source': 'operator_seed_post_upgrade'},
    )])
    _open_activation(repo)
    payload = _assignment_payload(100, actual_fee=False)
    del payload['stock_settlement']['currency']
    persist_trade_event_objects_atomically(repo, [_put_event(
        event_id='put-assignment', event_type='assignment', multiplier=100,
        raw_payload=payload,
    )])
    model = build_wheel_read_model(
        repo, 'lx', 3000, monitoring_readiness={'monitoring_gate': 'enabled'}, market='us',
    )
    branch = model['wheel_branches'][0]
    assert branch['shares_remaining'] == 100
    assert 'multiplier_unproven' in branch['reason_codes']
    assert 'assignment_cash_facts_unavailable' in branch['reason_codes']
    scan = run_wheel_call_scan(
        model, _policy(), {'frames': {'NVDA': pd.DataFrame([phase2_opening_row(_call_row())])}},
        {}, {'exchange_rate_converter': _converter()}, decision_time_ms=int(AS_OF.timestamp() * 1000),
    )
    assert scan['capacity_claims'] == []
    assert scan['scope_results'][0]['reason_code'] == sorted(branch['reason_codes'])[0]
    finalized = finalize_wheel_capacity(
        account='lx', wheel_read_model=model, wheel_scan=scan,
        opening_call_candidates=[], coverage_facts=[],
    )
    assert finalized['allocations'] == []
    snapshot = seal_wheel_candidate_snapshot(
        base=tmp_path, run_id='run-1', account='lx', market='us',
        account_config_sha256='a' * 64, strategy_policy_sha256='b' * 64,
        dependencies=_dependencies(), scope_results=finalized['scope_results'],
        run_mode={'scan_mode': 'standard', 'executable': True},
        batches=finalized['batches'],
    )
    gaps = []
    batches, candidates, _ = _load_wheel_snapshot_family(
        run_id='run-1', account='lx', market='US', source_artifacts=[],
        data_gaps=gaps, snapshot=snapshot,
    )
    assert [gap['reason'] for gap in gaps] == [sorted(branch['reason_codes'])[0]]
    assert candidates == []
    assert batches[0]['recommended_contracts'] == 0
    brief = _brief()
    brief['wheel_batches'] = batches
    message = render_fixed_report(brief, context=_scheduled_context())
    # The default five A/B cards can use the account quota; missing-evidence
    # reasons still survive in the gap summary without another status card.
    assert '缺口摘要｜NVDA Wheel Call' in message
    assert '合约乘数来源未核实，暂停推荐' in message
    assert '指派交割金额或实际费用证据不完整，暂停推荐' in message
    expanded = render_fixed_report(brief, context=_scheduled_context(), limits={'max_items_per_account': 0})
    assert 'NVDA｜未覆盖 100 股' in expanded
    assert '合约乘数来源未核实，暂停推荐' in expanded
    assert '指派交割金额或实际费用证据不完整，暂停推荐' in expanded


def _source_event(event_id, event_type, option, at, lot, *, contracts=1, known=True, raw=None):
    proof = {
        'source_type': 'broker_trade_event', 'source_deal_id': event_id,
        'external_event_key': 'futu:' + event_id, 'multiplier_source': 'payload',
    } if known and event_type == 'open' else {}
    return TradeEvent(
        event_id=event_id, event_type=event_type, event_time_ms=at,
        contract_key=ContractKey.from_values(
            broker='富途', account='lx', underlying_symbol='NVDA', option_type=option,
            strike=100 if option == 'put' else 110, expiration_ymd='2026-09-18',
        ),
        contracts=contracts, price=2.5 if event_type == 'open' else 0, currency='USD',
        source='test', multiplier=100,
        lot_id=lot if event_type == 'open' else None,
        target_lot_id=lot if event_type == 'assignment' else None,
        raw_payload={**proof, 'side': 'sell' if event_type == 'open' else 'buy', **(raw or {})},
    )


def _persist_source_assignment(repo, prefix, option, *, contracts=1, known=True, fee=True, stock_id=None):
    lot = prefix + '-lot'
    opening = {'strategy': 'cc', 'leg_role': 'covered_call', 'source_stock_lot_id': stock_id} if stock_id else {}
    persist_trade_event_objects_atomically(repo, [_source_event(
        prefix + '-open', 'open', option, 1000 if option == 'put' else 3000, lot,
        contracts=contracts, known=known, raw=opening,
    )])
    settlement = {
        'side': 'buy' if option == 'put' else 'sell', 'shares': 100 * contracts,
        'price': 100 if option == 'put' else 110, 'fees': 0, 'currency': 'USD',
        **({'fee_provenance': {'basis': 'actual', 'source': 'test'}} if fee else {}),
    }
    persist_trade_event_objects_atomically(repo, [_source_event(
        prefix + '-assignment', 'assignment', option, 2000 if option == 'put' else 4000, lot,
        contracts=contracts, raw={'target_lot_id': lot, 'stock_settlement': settlement},
    )])


@pytest.mark.parametrize('direction', ['call', 'put'])
@pytest.mark.parametrize('kind', [
    'known', 'unknown', 'missing_fee', 'mixed_siblings', 'option_open_known', 'option_open_unknown',
])
def test_assignment_evidence_gates_real_scan_finalizer_and_brief(tmp_path, direction, kind):
    repo = SQLiteOptionPositionsRepository(tmp_path / 'ledger.sqlite3')
    stock_id = None
    if direction == 'put':
        _persist_source_assignment(repo, 'stock-source', 'put', contracts=4)
        stock_id = 'assigned-stock-stock-source-assignment'
    _open_activation(repo)
    option_open = kind.startswith('option_open_')
    source_option = 'put' if direction == 'call' else 'call'
    _persist_source_assignment(
        repo, 'challenge', source_option, contracts=2 if option_open else 1,
        known=kind in {'known', 'missing_fee', 'option_open_known'},
        fee=kind != 'missing_fee', stock_id=stock_id,
    )
    readiness = {'monitoring_gate': 'enabled'}
    if option_open:
        branch = build_wheel_read_model(repo, 'lx', 4500, monitoring_readiness=readiness, market='us')['wheel_branches'][0]
        linkage = {'strategy': 'wheel', 'leg_role': 'wheel_' + direction, 'source_wheel_branch_id': branch['wheel_branch_id']}
        if direction == 'call':
            linkage['source_stock_lot_id'] = branch['stock_lot_id']
        persist_trade_event_objects_atomically(repo, [_source_event(
            'linked-option-open', 'open', direction, 4500, 'linked-option-lot', raw=linkage,
        )])
    if kind == 'mixed_siblings':
        _persist_source_assignment(repo, 'healthy', source_option, stock_id=stock_id)
    model = build_wheel_read_model(repo, 'lx', 5000, monitoring_readiness=readiness, market='us')
    assert not build_wheel_read_model(repo, 'other', 5000, monitoring_readiness=readiness, market='us')['wheel_branches']
    branches = model['wheel_branches']
    assert len(branches) == (2 if kind == 'mixed_siblings' else 1)
    assert all(b['integrity_status'] == 'trusted' and b['coverage']['available_shares'] == 100 for b in branches)
    if option_open:
        assert branches[0]['phase'] == 'option_open'
        assert branches[0]['coverage']['target_shares'] == 200
        assert branches[0]['coverage']['committed_shares'] == 100
    blocked = [b for b in branches if b['reason_codes']]
    if kind in {'unknown', 'mixed_siblings', 'option_open_unknown'}:
        assert len(blocked) == 1 and blocked[0]['reason_codes'] == ['multiplier_unproven']
    elif kind == 'missing_fee':
        assert len(blocked) == 1 and 'assignment_cash_facts_unavailable' in blocked[0]['reason_codes']
    else:
        assert not blocked
    events_before = (repo.list_trade_events(), repo.list_wheel_events(account='lx'))
    row = phase2_opening_row({**_call_row(), 'option_type': direction,
        'strike': 99 if direction == 'put' else 110, 'delta': -.3 if direction == 'put' else .3})
    frames = {'frames': {'NVDA': pd.DataFrame([row])}}
    fees = {'exchange_rate_converter': _converter()}
    decision_time_ms = int(AS_OF.timestamp() * 1000)
    if direction == 'call':
        scan = run_wheel_call_scan(model, _policy(), frames, {}, fees, decision_time_ms=decision_time_ms)
        finalized = finalize_wheel_capacity(
            account='lx', wheel_read_model=model, wheel_scan=scan, opening_call_candidates=[],
            coverage_facts=[{'account': 'lx', 'symbol': 'NVDA', 'status': 'available',
                'shares_eligible': 1000, 'shares_locked': 0, 'shares_reserved': 0, 'capacity_identity_hash': 'c' * 64}],
        )
    else:
        scan = run_wheel_put_scan(model, _policy(), frames, fees, decision_time_ms=decision_time_ms)
        finalized = finalize_wheel_put_capacity(
            account='lx', wheel_read_model=model, wheel_scan=scan, opening_put_candidates=[],
            cash_capacity_fact=cash_portfolio({'account': 'lx', 'status': 'available',
                'cash_authority': {'status': 'available', 'logical_account': 'lx'},
                'cash_authority_hash': 'authority-test-wheel-assignment',
                'cash_by_currency': {'USD': 100_000}, 'cash_secured_by_currency': {},
                'wheel_intent_reservations': [], 'fx_snapshot': {'rates': {}}}),
            exchange_rate_converter=_converter(),
        )
    expected = 1 if kind in {'known', 'option_open_known', 'mixed_siblings'} else 0
    assert len(scan['capacity_claims']) == expected
    if direction == 'call' and kind == 'option_open_known':
        assert finalized['batches'][0]['raw_candidates']
        assert finalized['batches'][0]['reason_code'] == 'wheel_call_open'
        expected = 0  # Remaining stock is observable; an active Call blocks another intent.
    assert sum(b['final_candidate'] is not None for b in finalized['batches']) == expected, finalized
    for branch in blocked:
        branch_id = branch['wheel_branch_id']
        scope = next(s for s in scan['scope_results'] if s['wheel_branch_id'] == branch_id)
        assert (scope['status'], scope['reason_code'], scope['candidate_count']) == ('unavailable', sorted(branch['reason_codes'])[0], 0)
        batch = next(b for b in finalized['batches'] if b['wheel_branch_id'] == branch_id)
        assert batch['reason_codes'] == branch['reason_codes']
        assert batch['reason_code'] == scope['reason_code']
        assert batch['raw_candidates'] == [] and batch['granted_contracts'] == 0 and batch['final_candidate'] is None
        assert batch['batch_generation_hash'] == branch['batch_generation_hash']
        assert batch['projection_hash'] == branch['projection_hash']
        assert all(c['wheel_branch_id'] != branch_id for c in scan['capacity_claims'])
    snapshot = seal_wheel_candidate_snapshot(
        base=tmp_path, run_id='run-1', account='lx', market='us',
        account_config_sha256='a' * 64, strategy_policy_sha256='b' * 64, dependencies=_dependencies(),
        scope_results=finalized['scope_results'], batches=finalized['batches'],
        capacity_allocations=finalized['allocations'], sealed_at=AS_OF,
        run_mode={'scan_mode': 'standard', 'executable': True},
    )
    gaps = []
    views, candidates, available = _load_wheel_snapshot_family(
        run_id='run-1', account='lx', market='US', source_artifacts=[], data_gaps=gaps, snapshot=snapshot,
    )
    assert available and len(candidates) == expected
    assert all(v['account'] == 'lx' and v['direction'] == direction for v in views)
    for branch in blocked:
        view = next(v for v in views if v['wheel_branch_id'] == branch['wheel_branch_id'])
        assert view['reason_codes'] == branch['reason_codes']
        assert view['recommended_contracts'] == 0 and not view['has_final_candidate']
        assert any(g['wheel_branch_id'] == branch['wheel_branch_id'] and g['reason'] == sorted(branch['reason_codes'])[0] for g in gaps)
    brief = _brief()
    brief['wheel_batches'] = views
    message = render_fixed_report(brief, context=_scheduled_context())
    if blocked:
        reason = 'assignment_cash_facts_unavailable' if kind == 'missing_fee' else 'multiplier_unproven'
        assert {'multiplier_unproven': '合约乘数来源未核实，暂停推荐',
                'assignment_cash_facts_unavailable': '指派交割金额或实际费用证据不完整，暂停推荐'}[reason] in message
    assert (repo.list_trade_events(), repo.list_wheel_events(account='lx')) == events_before
