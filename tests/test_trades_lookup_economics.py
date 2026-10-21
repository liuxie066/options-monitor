from copy import deepcopy
from functools import partial
import json
import sqlite3

import pytest

from domain.domain.trade_execution import canonical_trade_execution_content
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.trades import auto_intake, futu_detail_lookup
from src.application.trades.deal_identity import broker_deal_key_from_payload
from src.application.trades.inbox import (
    TRADE_INTAKE_ADAPTER_VERSIONS,
    TradePayloadClaimLost,
    claim_trade_payload,
    enqueue_trade_payload,
    read_trade_payload,
    read_trade_source_evidence,
    resume_trade_payload,
    trade_payload_commit_scope,
    trade_payload_evidence_ref,
)
from src.application.trades.inbox_authority import resolve_execution_inbox_path
from src.application.trades.normalizer import normalize_trade_deal


QUANTITY = ('contracts', 'qty', 'quantity')
PRICE = ('price', 'execution_price', 'dealt_price')


def _payload(**economics):
    return {'acc_id': '123', 'broker_account_id': 'futu:REAL:123', 'environment': 'REAL', 'status': 'OK',
            'external_id_namespace': 'futu.deal', 'external_order_namespace': 'futu.order',
            'deal_id': 'synthetic-lookup', 'order_id': 'synthetic-order',
            'code': 'US.NVDA261218P00100000', 'multiplier': '100', 'trd_side': 'SELL_SHORT',
            'create_time': '2026-10-21 10:30:00', **economics}


def _gateway(monkeypatch, row):
    calls = []
    class Gateway:
        def get_deal_list(self, **kwargs):
            assert kwargs == {'acc_id': 123}
            calls.append('deal')
            return [deepcopy(row)]
        def get_order_list(self, **kwargs):
            pytest.fail('complete identity must not fetch an order summary')
        def close(self):
            calls.append('close')
    monkeypatch.setattr(futu_detail_lookup, 'build_ready_futu_broker_gateway', lambda **_: Gateway())
    return calls


def _setup(tmp_path):
    repo = SQLiteOptionPositionsRepository(tmp_path / 'ledger.sqlite3')
    inbox = resolve_execution_inbox_path(repo, tmp_path / 'unused.sqlite3')
    process = partial(auto_intake._process_payload, repo=repo, state_path=tmp_path / 'state.json',
                      audit_path=tmp_path / 'audit.jsonl', account_mapping={'123': 'synthetic'},
                      futu_account_ids=['123'], apply_changes=True, host='example.invalid', port=1,
                      runtime_root=tmp_path, config=None, source='push')
    return repo, inbox, process


def _ledger_snapshot(repo):
    with sqlite3.connect(repo.db_path) as conn:
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {name: sorted(conn.execute(f'SELECT * FROM "{name}"').fetchall(), key=repr) for name in tables}


def _evidence(inbox, key):
    return read_trade_source_evidence(inbox, evidence_ref=trade_payload_evidence_ref(key))


@pytest.mark.parametrize('aliases,original_value,lookup_value,other', [
    (QUANTITY, '1', '9', {'price': '2.50'}),
    (PRICE, '2.50', '99', {'qty': '1'}),
])
@pytest.mark.parametrize('left,right', [(a, b) for a in range(3) for b in range(3)])
def test_lookup_conflict_is_durable_before_any_economic_write(
    tmp_path, monkeypatch, aliases, original_value, lookup_value, other, left, right,
):
    original = _payload(**other, **{aliases[left]: original_value})
    lookup = {'acc_id': '123', 'deal_id': original['deal_id'], 'order_id': original['order_id'],
              **other, aliases[right]: lookup_value}
    saved = deepcopy(original), deepcopy(lookup)
    calls = _gateway(monkeypatch, lookup)
    repo, inbox, process = _setup(tmp_path)
    before = _ledger_snapshot(repo)
    result = process(original, on_result_fn=lambda _: pytest.fail('conflict must not dispatch a receipt'))
    assert result['status'] == 'unresolved', result
    assert result['diagnostics']['retryable'] is False
    key = result['inbox_id']
    stored = read_trade_payload(inbox, inbox_id=key)
    assert (stored['status'], stored['result_reason']) == ('conflict', 'broker_economic_payload_conflict')
    evidence = _evidence(inbox, key)
    assert {r['source'] for r in evidence} == {'push', 'lookup'}
    assert any(r['raw_payload'] == original for r in evidence)
    observed = next(r['raw_payload'] for r in evidence if r['source'] == 'lookup')
    assert observed['_futu_detail_lookup_row'] == lookup
    assert observed[aliases[right]] == lookup_value
    assert all(alias not in observed for alias in aliases if alias != aliases[right])
    assert (original, lookup) == saved
    assert calls == ['deal', 'close']
    assert _ledger_snapshot(repo) == before
    assert claim_trade_payload(inbox, inbox_id=key, repo=repo) is None
    assert resume_trade_payload(inbox, inbox_id=key, operator='synthetic', repo=repo) is False
    assert process(original, allow_external_lookup=False, retry_failed_deal=True)['status'] == 'unresolved'
    assert process(_payload(), allow_external_lookup=False)['status'] == 'unresolved'
    assert _ledger_snapshot(repo) == before
    assert len(_evidence(inbox, key)) >= 2


@pytest.mark.parametrize('original,detail', [
    ({'quantity': '1.00', 'dealt_price': '2.500'}, {'contracts': 1, 'price': 2.5}),
    ({}, {'qty': '1', 'price': '2.50'}),
    ({'quantity': '1'}, {'contracts': '1.0', 'price': '2.50'}),
    ({'execution_price': '2.50'}, {'quantity': '1', 'price': '2.500'}),
    ({'quantity': None, 'execution_price': ''}, {'qty': '1', 'price': '2.50'}),
])
@pytest.mark.parametrize('has_order', [True, False])
def test_compatible_lookup_preserves_claim_and_commits_once(tmp_path, monkeypatch, original, detail, has_order):
    payload = _payload(**original)
    if not has_order:
        payload.pop('order_id')
        payload.pop('external_order_namespace')
    lookup = {'acc_id': '123', 'deal_id': payload['deal_id'], 'order_id': 'synthetic-order',
              'external_order_namespace': 'futu.order', **detail}
    _gateway(monkeypatch, lookup)
    repo, inbox, process = _setup(tmp_path)
    result = process(payload)
    assert result['status'] == 'applied', result
    events = repo.list_trade_events()
    assert len(events) == len(repo.list_position_lots()) == 1
    execution = events[0]['raw_payload']['execution_input']
    assert (execution['quantity'], execution['price']) == ('1', '2.5')
    sources = _evidence(inbox, result['inbox_id'])
    assert {r['source'] for r in sources} == {'push', 'lookup'}
    observation = next(r['raw_payload'] for r in sources if r['source'] == 'lookup')
    assert observation.get('order_id') == payload.get('order_id')
    assert observation['_futu_detail_lookup_row']['order_id'] == 'synthetic-order'
    before = _ledger_snapshot(repo)
    assert process(payload)['reason'] == 'duplicate'
    assert _ledger_snapshot(repo) == before


@pytest.mark.parametrize('bad', [True, 'NaN', 'bad', '1.5', '-1'])
@pytest.mark.parametrize('bad_side', ['original', 'lookup'])
def test_invalid_economics_cannot_be_repaired_by_lookup(tmp_path, monkeypatch, bad, bad_side):
    original = _payload(contracts='1', price='2.5')
    detail = {'acc_id': '123', 'deal_id': original['deal_id'], 'qty': '1', 'price': '2.5'}
    (original if bad_side == 'original' else detail)['quantity'] = bad
    _gateway(monkeypatch, detail)
    repo, inbox, process = _setup(tmp_path)
    before = _ledger_snapshot(repo)
    result = process(original)
    assert result['status'] == 'unresolved', result
    stored = read_trade_payload(inbox, inbox_id=result['inbox_id'])
    assert stored['status'] == 'conflict'
    assert stored['result_reason'] == 'broker_economic_payload_conflict'
    assert process(_payload(contracts='1', price='2.5'), allow_external_lookup=False)['status'] == 'unresolved'
    assert _ledger_snapshot(repo) == before


@pytest.mark.parametrize('conflicting', [True, False])
def test_status_recovery_observes_lookup_before_claim(tmp_path, monkeypatch, conflicting):
    original = _payload(quantity='1', execution_price='2.50')
    original.pop('status')
    detail = {'acc_id': '123', 'deal_id': original['deal_id'], 'qty': '9' if conflicting else '1',
              'price': '99' if conflicting else '2.500', 'status': 'OK'}
    _gateway(monkeypatch, detail)
    repo, inbox, process = _setup(tmp_path)
    before = _ledger_snapshot(repo)
    result = process(original)
    if conflicting:
        assert result['status'] == 'unresolved', result
        assert read_trade_payload(inbox, inbox_id=result['inbox_id'])['result_reason'] == 'broker_economic_payload_conflict'
        assert _ledger_snapshot(repo) == before
    else:
        assert result['status'] == 'applied', result
        assert len(repo.list_trade_events()) == 1
    assert any(r['raw_payload'].get('_futu_detail_lookup_row') == detail for r in _evidence(inbox, result['inbox_id']))


@pytest.mark.parametrize('conflicting', [True, False])
def test_sparse_status_observations_cannot_erase_numeric_history(tmp_path, conflicting):
    repo, inbox, process = _setup(tmp_path)
    before = _ledger_snapshot(repo)
    first = _payload(quantity='1', execution_price='2.50')
    first.pop('status')
    second = _payload(qty='9' if conflicting else '1.0', price='99' if conflicting else '2.500')
    second.pop('status')
    sparse = _payload()
    sparse.pop('status')
    broker_key = broker_deal_key_from_payload(first, account_mapping={'123': 'synthetic'})
    for index, payload in enumerate((first, second, sparse, {**sparse, 'status': 'OK'})):
        key = enqueue_trade_payload(inbox, payload=payload, source='lookup' if index else 'push',
                                    broker_deal_key=broker_key, repo=repo,
                                    adapter_version=TRADE_INTAKE_ADAPTER_VERSIONS['lookup' if index else 'push'])
        stored = read_trade_payload(inbox, inbox_id=key)
        if conflicting and index >= 1:
            assert (stored['status'], stored['result_reason']) == ('conflict', 'broker_economic_payload_conflict')
    assert len(_evidence(inbox, key)) == 4
    if conflicting:
        assert claim_trade_payload(inbox, inbox_id=key, repo=repo) is None
        assert process({**sparse, 'status': 'OK'}, allow_external_lookup=False, retry_failed_deal=True)['status'] == 'unresolved'
    else:
        assert stored['status'] == 'pending'
        assert claim_trade_payload(inbox, inbox_id=key, repo=repo) is not None
    assert _ledger_snapshot(repo) == before


def test_conflict_invalidates_existing_claim_without_economic_changes(tmp_path):
    repo, inbox, _ = _setup(tmp_path)
    original = _payload(qty='1', price='2.5')
    broker_key = broker_deal_key_from_payload(original, account_mapping={'123': 'synthetic'})
    kwargs = dict(source='push', broker_deal_key=broker_key, repo=repo, adapter_version=TRADE_INTAKE_ADAPTER_VERSIONS['push'])
    key = enqueue_trade_payload(inbox, payload=original, **kwargs)
    claim = claim_trade_payload(inbox, inbox_id=key, repo=repo)
    assert claim is not None
    enqueue_trade_payload(inbox, payload={**original, 'qty': '9'}, **kwargs)
    with pytest.raises(TradePayloadClaimLost):
        with trade_payload_commit_scope(inbox, claim=claim, repo=repo):
            pytest.fail('stale claim entered economic scope')


def test_preview_lookup_conflict_writes_nothing(tmp_path, monkeypatch):
    original = _payload(quantity='1', execution_price='2.5')
    _gateway(monkeypatch, {'acc_id': '123', 'deal_id': original['deal_id'], 'qty': '9', 'price': '99'})
    result = auto_intake._process_payload(original, repo=object(), state_path=tmp_path / 'state.json',
        audit_path=tmp_path / 'audit.jsonl', account_mapping={'123': 'synthetic'}, futu_account_ids=['123'],
        apply_changes=False, host='example.invalid', port=1, runtime_root=tmp_path)
    assert result['status'] == 'unresolved'
    assert result['diagnostics']['retryable'] is False
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('later', [
    {'status': 'CANCELLED'}, {'status': 'CHANGED'}, {'status': 'UNKNOWN'},
    {'status': 'OK', 'update_timestamp': 1},
    {'status': 'OK', 'external_execution_id': 'different-execution'},
])
def test_other_conflict_reasons_cannot_reopen_numeric_history(tmp_path, later):
    repo, inbox, _ = _setup(tmp_path)
    broker_key = broker_deal_key_from_payload(_payload(), account_mapping={'123': 'synthetic'})
    kwargs = dict(source='push', broker_deal_key=broker_key, repo=repo,
                  adapter_version=TRADE_INTAKE_ADAPTER_VERSIONS['push'])
    for row in (_payload(qty='1', price='2.5', update_timestamp=2000),
                _payload(quantity='9', dealt_price='99', update_timestamp=2000),
                _payload(**later), _payload()):
        key = enqueue_trade_payload(inbox, payload=row, **kwargs)
    stored = read_trade_payload(inbox, inbox_id=key)
    expected = ('broker_source_identity_conflict' if 'external_execution_id' in later
                else 'broker_economic_payload_conflict')
    assert (stored['status'], stored['result_reason']) == ('conflict', expected)
    assert claim_trade_payload(inbox, inbox_id=key, repo=repo) is None
    assert resume_trade_payload(inbox, inbox_id=key, operator='synthetic', repo=repo) is False


def test_duplicate_reclassifies_retained_conflict_then_is_idempotent(tmp_path):
    repo, inbox, _ = _setup(tmp_path)
    original, conflicting = _payload(qty='1', price='2.5'), _payload(qty='9', price='99')
    kwargs = dict(source='push', broker_deal_key=broker_deal_key_from_payload(original, account_mapping={'123': 'synthetic'}),
                  repo=repo, adapter_version=TRADE_INTAKE_ADAPTER_VERSIONS['push'])
    key = enqueue_trade_payload(inbox, payload=original, **kwargs)
    enqueue_trade_payload(inbox, payload=conflicting, **kwargs)
    with sqlite3.connect(inbox) as conn:
        conn.create_function("trade_inbox_writer_version", 0, lambda: 2)
        conn.execute("UPDATE trade_inbox SET result_reason='broker_deal_status_missing' WHERE inbox_id=?", (key,))
    before = read_trade_payload(inbox, inbox_id=key)
    count = len(_evidence(inbox, key))
    enqueue_trade_payload(inbox, payload=conflicting, **kwargs)
    fixed = read_trade_payload(inbox, inbox_id=key)
    assert fixed['result_reason'] == 'broker_economic_payload_conflict'
    assert fixed['payload_version'] == before['payload_version'] + 1
    assert len(_evidence(inbox, key)) == count
    enqueue_trade_payload(inbox, payload=conflicting, **kwargs)
    assert read_trade_payload(inbox, inbox_id=key)['payload_version'] == fixed['payload_version']
    assert claim_trade_payload(inbox, inbox_id=key, repo=repo) is None


def test_order_only_lookup_still_cannot_supply_economics(tmp_path, monkeypatch):
    class Gateway:
        def get_deal_list(self, **kwargs):
            return []
        def get_order_list(self, **kwargs):
            return [{'acc_id': '123', 'order_id': 'synthetic-order', 'qty': '9', 'price': '99'}]
        def close(self):
            pass
    monkeypatch.setattr(futu_detail_lookup, 'build_ready_futu_broker_gateway', lambda **_: Gateway())
    repo, inbox, process = _setup(tmp_path)
    result = process(_payload(qty='1', price='2.5'))
    assert result['status'] == 'applied'
    execution = repo.list_trade_events()[0]['raw_payload']['execution_input']
    assert (execution['quantity'], execution['price']) == ('1', '2.5')
    assert {r['source'] for r in _evidence(inbox, result['inbox_id'])} == {'push'}


def test_zero_price_equivalence_keeps_existing_open_contract(tmp_path, monkeypatch):
    _gateway(monkeypatch, {'acc_id': '123', 'deal_id': 'synthetic-lookup', 'qty': '1.0', 'price': 0.0})
    payload = _payload(quantity='1', dealt_price='0.00')
    outcomes = []
    for allow_lookup in (False, True):
        fixture = tmp_path / str(allow_lookup)
        fixture.mkdir()
        repo, inbox, process = _setup(fixture)
        result = process(payload, allow_external_lookup=allow_lookup)
        assert result['status'] == 'failed', result
        assert result['reason'] == 'exception:ValueError'
        assert result['diagnostics']['exception_message'] == 'premium_per_share must be > 0'
        outcomes.append((result['reason'], result['diagnostics']['exception_message']))
        assert repo.list_trade_events() == []
        key = broker_deal_key_from_payload(payload, account_mapping={'123': 'synthetic'})
        with sqlite3.connect(inbox) as conn:
            stored_id = conn.execute('SELECT inbox_id FROM trade_inbox WHERE broker_deal_key=?', (key,)).fetchone()[0]
        assert read_trade_payload(inbox, inbox_id=stored_id)['status'] != 'conflict'
        evidence = _evidence(inbox, stored_id)
        assert len(evidence) == (2 if allow_lookup else 1)
    assert outcomes[0] == outcomes[1]


@pytest.mark.parametrize('failure', ['write', 'readback'])
def test_lookup_evidence_io_failure_never_reaches_writer(tmp_path, monkeypatch, failure):
    _gateway(monkeypatch, {'acc_id': '123', 'deal_id': 'synthetic-lookup', 'qty': '9', 'price': '99'})
    repo, inbox, process = _setup(tmp_path)
    before = _ledger_snapshot(repo)
    real_enqueue, real_read = auto_intake.enqueue_trade_payload, auto_intake.read_trade_payload
    if failure == 'write':
        def enqueue(*args, **kwargs):
            if kwargs['source'] == 'lookup':
                raise OSError('synthetic lookup evidence write failure')
            return real_enqueue(*args, **kwargs)
        monkeypatch.setattr(auto_intake, 'enqueue_trade_payload', enqueue)
        expected = OSError
    else:
        def read(*args, **kwargs):
            row = real_read(*args, **kwargs)
            if row is not None and row.get('result_reason') == 'broker_economic_payload_conflict':
                return None
            return row
        monkeypatch.setattr(auto_intake, 'read_trade_payload', read)
        expected = RuntimeError
    with pytest.raises(expected):
        process(_payload(quantity='1', execution_price='2.5'))
    assert _ledger_snapshot(repo) == before


def test_invalid_state_precedes_lookup_provider_work(tmp_path, monkeypatch):
    repo, _, process = _setup(tmp_path)
    (tmp_path / 'state.json').write_text('{')
    monkeypatch.setattr(futu_detail_lookup, 'build_ready_futu_broker_gateway',
                        lambda **_: pytest.fail('invalid state reached a provider'))
    with pytest.raises(ValueError, match='invalid trade intake state'):
        process(_payload(qty='1', price='2.5'))
    assert repo.list_trade_events() == []


def test_generic_intake_rechecks_state_after_lookup(tmp_path, monkeypatch):
    repo, _, process = _setup(tmp_path)
    state_path = tmp_path / 'state.json'
    state_path.write_text('{}')

    class Gateway:
        def get_deal_list(self, **kwargs):
            state_path.write_text('{')
            return [{'acc_id': '123', 'deal_id': 'synthetic-lookup', 'qty': '1', 'price': '2.5'}]

        def get_order_list(self, **kwargs):
            pytest.fail('complete identity must not fetch an order summary')

        def close(self):
            pass

    monkeypatch.setattr(futu_detail_lookup, 'build_ready_futu_broker_gateway', lambda **_: Gateway())
    before = _ledger_snapshot(repo)
    with pytest.raises(ValueError, match='invalid trade intake state'):
        process(_payload(quantity='1', execution_price='2.50'))
    assert _ledger_snapshot(repo) == before
    assert not (tmp_path / 'audit.jsonl').exists()
    assert state_path.read_text() == '{'


def _standard_payload(shape):
    raw = _payload(qty='1', price='2.5')
    normalized = normalize_trade_deal(raw, futu_account_mapping={'123': 'synthetic'}, allow_opend_refresh=False)
    if shape == 'nested':
        return {**raw, 'execution_input': normalized.execution_input}
    return {**normalized.execution_input, 'deal_id': raw['deal_id'], 'order_id': raw['order_id'],
            'futu_account_id': '123', 'status': 'OK'}


@pytest.mark.parametrize('shape', ['nested', 'standard'])
@pytest.mark.parametrize('detail,field,value', [
    *[({alias: '9', 'price': '2.5'}, 'quantity', '9') for alias in QUANTITY],
    *[({'qty': '1', alias: '99'}, 'price', '99') for alias in PRICE],
    ({'qty': '9', 'price': '99'}, 'quantity', '9'),
])
def test_supported_standard_lookup_conflicts_are_durable(tmp_path, monkeypatch, shape, detail, field, value):
    original = _standard_payload(shape)
    saved = deepcopy(original)
    lookup = {'acc_id': '123', 'deal_id': original['deal_id'], 'symbol': 'NVDA', **detail}
    _gateway(monkeypatch, lookup)
    repo, inbox, process = _setup(tmp_path)
    before = _ledger_snapshot(repo)
    result = process(original, source='manual', on_result_fn=lambda _: pytest.fail('conflict dispatched receipt'))
    assert result['status'] == 'unresolved', result
    assert result['reason'] == 'broker_economic_payload_conflict'
    entries = _evidence(inbox, result['inbox_id'])
    assert any(item['raw_payload'] == saved for item in entries)
    observation = next(item['raw_payload'] for item in entries if item['source'] == 'lookup')
    assert observation['_futu_detail_lookup_row'] == lookup
    assert canonical_trade_execution_content(observation)['economic'][field] == value
    assert original == saved
    assert _ledger_snapshot(repo) == before
    assert claim_trade_payload(inbox, inbox_id=result['inbox_id'], repo=repo) is None
    assert process(_payload(), source='manual', allow_external_lookup=False, retry_failed_deal=True)['status'] == 'unresolved'
    assert _ledger_snapshot(repo) == before


@pytest.mark.parametrize('shape', ['nested', 'standard'])
@pytest.mark.parametrize('detail', [
    {'qty': '1', 'quantity': 'bad', 'price': '2.5'},
    {'qty': '1', 'quantity': '9', 'price': '2.5'},
    {'qty': '1', 'price': '2.5', 'dealt_price': True},
    {'qty': '1', 'price': '2.5', 'dealt_price': '99'},
])
def test_standard_observation_reparses_raw_alias_errors(tmp_path, monkeypatch, shape, detail):
    original = _standard_payload(shape)
    lookup = {'acc_id': '123', 'deal_id': original['deal_id'], 'symbol': 'NVDA', **detail}
    _gateway(monkeypatch, lookup)
    repo, inbox, process = _setup(tmp_path)
    before = _ledger_snapshot(repo)
    result = process(original, source='manual')
    assert result['status'] == 'unresolved', result
    assert read_trade_payload(inbox, inbox_id=result['inbox_id'])['status'] == 'conflict'
    observation = next(item['raw_payload'] for item in _evidence(inbox, result['inbox_id']) if item['source'] == 'lookup')
    assert observation['_futu_detail_lookup_row'] == lookup
    if shape == 'nested':
        observation['execution_input']['errors'] = []
    else:
        observation['errors'] = []
    assert any(error.startswith(('invalid:quantity:', 'invalid:price:'))
               for error in canonical_trade_execution_content(observation)['errors'])
    assert process(_payload(), source='manual', allow_external_lookup=False)['status'] == 'unresolved'
    assert _ledger_snapshot(repo) == before


@pytest.mark.parametrize('shape', ['nested', 'standard'])
@pytest.mark.parametrize('detail', [
    {'contracts': 1, 'execution_price': 2.5},
    {'qty': 1.0, 'dealt_price': '2.500'},
    {'quantity': '1.00', 'price': '2.50'},
    {'qty': '1'}, {'price': '2.5'}, {},
])
def test_standard_equivalent_or_missing_lookup_still_applies_once(tmp_path, monkeypatch, shape, detail):
    original = _standard_payload(shape)
    _gateway(monkeypatch, {'acc_id': '123', 'deal_id': original['deal_id'], 'symbol': 'NVDA', **detail})
    repo, inbox, process = _setup(tmp_path)
    result = process(original, source='manual')
    assert result['status'] == 'applied', result
    assert len(repo.list_trade_events()) == len(repo.list_position_lots()) == 1
    execution = repo.list_trade_events()[0]['raw_payload']['execution_input']
    assert (execution['quantity'], execution['price']) == ('1', '2.5')
    observation = next(item['raw_payload'] for item in _evidence(inbox, result['inbox_id']) if item['source'] == 'lookup')
    numeric = canonical_trade_execution_content(observation)
    assert not any(error.startswith(('invalid:quantity:', 'invalid:price:')) for error in numeric['errors'])
    assert numeric['economic']['quantity'] == ('1' if any(alias in detail for alias in QUANTITY) else None)
    assert numeric['economic']['price'] == ('2.5' if any(alias in detail for alias in PRICE) else None)
    before = _ledger_snapshot(repo)
    assert process(original, source='manual')['reason'] == 'duplicate'
    assert _ledger_snapshot(repo) == before


@pytest.mark.parametrize('shape', ['nested', 'standard'])
def test_deal_json_main_blocks_standard_lookup_conflict(tmp_path, monkeypatch, capsys, shape):
    original = _standard_payload(shape)
    lookup = {'acc_id': '123', 'deal_id': original['deal_id'], 'symbol': 'NVDA', 'qty': '9', 'price': '99'}
    _gateway(monkeypatch, lookup)
    repo, inbox, _ = _setup(tmp_path)
    source = {'id': 'synthetic', 'account': 'synthetic', 'enabled': True, 'mode': 'apply',
              'host': 'example.invalid', 'port': 1, 'account_mapping': {'123': 'synthetic'},
              'futu_account_ids': ['123'], 'combo_reconciliation_mode': 'off',
              'state_path': tmp_path / 'state.json', 'audit_path': tmp_path / 'audit.jsonl',
              'status_path': tmp_path / 'status.json', 'inbox_path': inbox,
              'backfill_checkpoint_path': tmp_path / 'checkpoint.json'}
    cfg = {**source, 'sources': [source], 'receipt': {'enabled': False}, 'backfill': {'enabled': False}}
    monkeypatch.setattr(auto_intake, 'load_config', lambda **_: {})
    monkeypatch.setattr(auto_intake, 'resolve_trade_intake_config', lambda *_, **__: cfg)
    monkeypatch.setattr(auto_intake, 'open_position_ledger_from_runtime_config', lambda **_: (None, repo))
    path = tmp_path / 'deal.json'
    path.write_text(json.dumps(original))
    before = _ledger_snapshot(repo)
    assert auto_intake.main(['--config', str(tmp_path / 'unused-config.json'), '--runtime-root', str(tmp_path),
                             '--deal-json', str(path), '--mode', 'apply', '--confirm']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'unresolved', result
    assert result['reason'] == 'broker_economic_payload_conflict'
    assert read_trade_payload(inbox, inbox_id=result['inbox_id'])['status'] == 'conflict'
    assert _ledger_snapshot(repo) == before
