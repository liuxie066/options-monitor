from src.application.daily_decision_brief_renderer import render_fixed_report
from src.application.daily_decision_brief_service import _load_wheel_snapshot_family
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.writer import persist_trade_event_objects_atomically
from src.application.wheel.candidate_snapshot import seal_wheel_candidate_snapshot
from src.application.wheel.capacity import finalize_wheel_capacity
from src.application.wheel.read_model import build_wheel_read_model
from src.application.wheel.scanning import run_wheel_call_scan
from tests.test_daily_decision_brief_renderer import _brief, _scheduled_context
from tests.test_wheel_assignment_companions import _assignment_payload, _open_activation, _put_event
from tests.test_wheel_candidate_snapshot import _dependencies


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
    model = build_wheel_read_model(repo, 'lx', 3000)
    branch = model['wheel_branches'][0]
    assert branch['shares_remaining'] == 100
    assert 'multiplier_unproven' in branch['reason_codes']
    assert 'assignment_cash_facts_unavailable' in branch['reason_codes']
    scan = run_wheel_call_scan(model, {}, {}, {}, None, decision_time_ms=3000)
    assert scan['capacity_claims'] == []
    finalized = finalize_wheel_capacity(
        account='lx', wheel_read_model=model, wheel_scan=scan,
        opening_call_candidates=[], coverage_facts=[],
    )
    assert finalized['allocations'] == []
    snapshot = seal_wheel_candidate_snapshot(
        base=tmp_path, run_id='run-1', account='lx', market='us',
        account_config_sha256='a' * 64, strategy_policy_sha256='b' * 64,
        dependencies=_dependencies(), scope_results=finalized['scope_results'],
        batches=finalized['batches'],
    )
    gaps = []
    batches, candidates, _ = _load_wheel_snapshot_family(
        run_id='run-1', account='lx', market='US', source_artifacts=[],
        data_gaps=gaps, snapshot=snapshot,
    )
    assert not gaps
    assert candidates == []
    assert batches[0]['recommended_contracts'] == 0
    brief = _brief()
    brief['wheel_batches'] = batches
    message = render_fixed_report(brief, context=_scheduled_context())
    assert 'NVDA｜Wheel Call' in message
    assert '剩余股份｜100 股' in message
    assert '合约乘数来源未核实，暂停推荐' in message
    assert '指派交割金额或实际费用证据不完整，暂停推荐' in message
