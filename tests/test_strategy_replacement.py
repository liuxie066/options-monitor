"""Finite test-only templates: real source steps, invariants and sealed consumption."""
from copy import deepcopy
from dataclasses import replace

import pandas as pd
import pytest
from candidate_evidence_helpers import delta_evidence

from domain.domain import strategy_core as core
from domain.domain.engine.candidate_engine import build_candidate_decision, evaluate_opening_candidate_policy
from domain.domain.insurance_underwriting import evaluate_underwriting_candidate
from domain.domain.strategy_identity import implementation_policy_hash, strategy_policy_hash
from src.application.candidate_scanning import attach_strategy_identity
from src.application.opening_candidate_snapshot import load_opening_candidate_snapshot, _snapshot_decisions
from src.application.agent_tools.candidate_rank_impl import candidate_rank_explain_tool
from src.application.sell_put_steps import run_sell_put_scan_and_summarize
from src.application.sell_call_steps import run_sell_call_scan_and_summarize
from test_strategy_core import VALID, CONVERTER, UNKNOWN
from test_strategy_core_integration import frozen_scan, _portfolio, _put_quote, _strategy  # noqa: F401
from test_cc_spot_distance_contract import _quote
from test_opening_candidate_snapshot import _seal_snapshot, _scan_status, _publish_opening_manifest, _snapshot_path


def _accept(row, *, mode, cfg):
    return build_candidate_decision(mode=mode, symbol=row['symbol'], contract_symbol=row['contract_symbol'],
                                    accepted=True, normalized_input=row)


def _constraints(row, *, mode, cfg):
    return {"effective_min_strike": 1.0} if mode == "call" else {}


def _rank(rows, *, mode, ranking_profile):
    # Fact-based, complete-output idempotent, stable within-symbol selection.
    return sorted(rows, key=lambda row: (-row['strike'], row['symbol'], row['contract_symbol']))


def _explain(row, *, mode, cfg):
    return {"primary_drivers": ["strike"], "primary_driver_labels": ["测试行权价"],
            "rank_reason": "test-only: descending strike, then symbol and contract identity",
            "ranking_policy": "test_only_strike"}


@pytest.fixture
def alternatives(monkeypatch):
    templates = {}
    for mode in ('put', 'call'):
        template = core.StrategyTemplate(f'test_{mode}', 'test.1', mode, _constraints, _accept, _rank, _explain)
        monkeypatch.setitem(core._TEMPLATES, (template.template_id, template.version), template)
        templates[mode] = template
    return templates


def _alternate(mode):
    return core.resolve_strategy(mode=mode, config=_strategy(mode).config, account='lx', market='us',
                                 template_id=f'test_{mode}', template_version='test.1')


def _run(root, mode, strategy, *, reverse=False, portfolio=None):
    finals, decisions, summaries = [], [], []
    for symbol in ('NVDA', 'MSFT'):
        strikes = (95, 96, 105) if mode == 'put' else (110, 115, 140)
        quotes = [(_put_quote(symbol, strike, 3.0) if mode == 'put' else _quote(symbol, strike))
                  for strike in strikes]
        if reverse:
            quotes.reverse()
        common = dict(symbol=symbol, symbol_cfg={}, required_data_dir=root,
                      required_data_frame=pd.DataFrame(quotes), resolved_strategy=strategy,
                      exchange_rate_converter=CONVERTER,
                      final_candidates_sink_fn=lambda mode, rows: finals.extend(rows),
                      candidate_decisions_sink_fn=lambda mode, rows: decisions.extend(rows))
        if mode == 'put':
            summaries.extend(run_sell_put_scan_and_summarize(
                **common, sp={'strategy': 'insurance_underwriting', 'max_strike': 100}, portfolio_ctx=portfolio if portfolio is not None else _portfolio()))
        else:
            summaries.append(run_sell_call_scan_and_summarize(
                **common, cc={'strategy': 'insurance_underwriting'},
                stock={'shares': 200, 'can_sell_qty': 200, 'avg_cost': 80},
                portfolio_ctx={'filters': {'account': 'lx'}, 'portfolio_source_name': 'futu',
                               'capacity_authority': {'status': 'available'}},
                locked_shares_status='available', locked_shares_by_symbol={'NVDA': 100},
                locked_shares_unavailable_by_symbol={}))
    return finals, decisions, summaries


@pytest.mark.parametrize('mode', ['put', 'call'])
def test_replacement_real_steps_summary_seal_load_explanation_and_restore(frozen_scan, alternatives, mode, monkeypatch):
    portfolio = _portfolio()
    baseline = _run(frozen_scan, mode, _strategy(mode), portfolio=portfolio)
    strategy = _alternate(mode)
    finals, decisions, summaries = _run(frozen_scan, mode, strategy)
    assert len(baseline[0]) == 4 and len(finals) == 6
    assert len(decisions) == 6 and all(row['opening_decision']['accepted'] for row in decisions)
    assert all(item['candidate_count'] == 3 for item in summaries)
    high = 105 if mode == 'put' else 140
    assert all(f'{high}{"P" if mode == "put" else "C"}' in item['top_contract'] for item in summaries)
    assert all(item['strategy_core']['template'] == f'test_{mode}' for item in summaries)
    reversed_finals, _, reversed_summaries = _run(frozen_scan, mode, strategy, reverse=True)
    assert [r['contract_symbol'] for r in finals] == [r['contract_symbol'] for r in reversed_finals]
    assert summaries == reversed_summaries
    ranked = core.rank_candidates(finals, mode=mode)
    assert core.rank_candidates(ranked, mode=mode) == ranked
    assert core.rank_candidates(list(reversed(finals)), mode=mode) == ranked
    for symbol in ('NVDA', 'MSFT'):
        local = [r for r in finals if r['symbol'] == symbol]
        assert core.rank_candidates(local, mode=mode)[0]['contract_symbol'] == next(
            r['contract_symbol'] for r in ranked if r['symbol'] == symbol)
    snapshot = _seal_snapshot(frozen_scan, run_id='run-1',
        scan_statuses=[_scan_status(s, mode, 'completed') for s in ('NVDA', 'MSFT')],
        final_candidates={mode: finals}, candidate_evaluations={mode: decisions})
    _publish_opening_manifest(frozen_scan, snapshot)
    loaded = load_opening_candidate_snapshot(base=frozen_scan, run_id='run-1', account='lx', require_current_contract=True)
    assert loaded == snapshot
    assert [r['facts']['contract_symbol'] for r in loaded['ranked_candidates']] == [r['contract_symbol'] for r in ranked]
    for item in loaded['ranked_candidates']:
        assert item['ranking']['primary_drivers'] == ['strike']
        assert item['ranking']['rank_reason'] == _explain({}, mode=mode, cfg=None)['rank_reason']
    template = alternatives[mode]
    monkeypatch.delitem(core._TEMPLATES, (template.template_id, template.version))
    original = _snapshot_path(frozen_scan).read_bytes()
    assert load_opening_candidate_snapshot(base=frozen_scan, run_id='run-1', account='lx') == loaded
    explained, _, _ = candidate_rank_explain_tool(
        {'runtime_root': str(frozen_scan), 'run_id': 'run-1', 'account': 'lx', 'mode': mode},
        repo_base=lambda: frozen_scan, resolve_output_root=lambda _: frozen_scan, mask_path=str)
    assert len(explained['ranked']) == 6
    assert all(item['primary_drivers'] == ['strike'] and item['rank_reason'] == _explain({}, mode=mode, cfg=None)['rank_reason']
               for item in explained['ranked'])
    assert _snapshot_path(frozen_scan).read_bytes() == original
    for execute in (lambda: core.evaluate_candidates(finals, strategy=strategy),
                    lambda: core.rank_candidates(finals, mode=mode),
                    lambda: _snapshot_decisions({mode: []}, statuses=[_scan_status(s, mode, 'completed') for s in ('NVDA', 'MSFT')],
                        account='lx', market='US', futu_account_id='fixture', risk_policy_hash='a'*64,
                        candidate_evaluations={mode: decisions})):
        with pytest.raises(ValueError, match='unavailable strategy template'):
            execute()
    restored = _run(frozen_scan, mode, _strategy(mode), portfolio=portfolio)
    assert core._stable(restored) == core._stable(baseline)


@pytest.mark.parametrize('mode', ['put', 'call'])
@pytest.mark.parametrize('changes,reason', [
    ({'max_new_contracts': 0, 'covered_contracts_available': 0}, 'hard_capacity'),
    ({'max_new_contracts': None, 'covered_contracts_available': None}, 'hard_capacity'),
    ({'net_income_cny': None}, 'input_missing'),
    ({'earnings_evidence_status': 'stale'}, 'risk_earnings_unavailable'),
    ({'earnings_hard_coverage_status': 'partial'}, 'risk_earnings_unavailable'),
    ({'earnings_policy_version': 'unsupported'}, 'risk_earnings_unavailable'),
])
def test_permissive_policy_cannot_bypass_qualification(alternatives, mode, changes, reason):
    row = {**deepcopy(next(c['policy_row'] for c in VALID if c['mode'] == mode)), **delta_evidence(mode=mode)}
    row.update(changes)
    decision = core.evaluate_candidates([row], strategy=_alternate(mode))['decisions'][0]
    assert not decision['accepted']
    assert any(reject['reason'].startswith(reason) for reject in decision['opening_decision']['rejects'])


@pytest.mark.parametrize('mode', ['put', 'call'])
def test_baseline_multi_failure_exact_order_and_no_evidence_opt_out_regression(mode):
    row = {**deepcopy(next(c['policy_row'] for c in VALID if c['mode'] == mode)), **delta_evidence(mode=mode)}
    row.update(dte=1, strike=1, max_new_contracts=0, covered_contracts_available=0, net_income_cny=None,
               annualized_net_return_on_cash_basis=0, annualized_net_premium_return=0, spread_ratio=1,
               iv_rv_ratio=0, iv_minus_rv=0, earnings_evidence_status='unavailable')
    cfg = core.InsuranceUnderwritingConfig(min_dte=7, max_dte=60)
    expected = evaluate_underwriting_candidate(row, mode=mode, cfg=cfg)
    actual = core.evaluate_candidates([row], strategy=core.resolve_strategy(mode=mode, config=cfg))['decisions'][0]
    assert actual == expected
    assert [r['reason'] for r in actual['opening_decision']['rejects']] == [
        'hard_dte', 'hard_strike', f'hard_capacity_{mode}', 'return_annualized', 'input_missing',
        'risk_spread', 'risk_iv_rv_ratio', 'risk_iv_minus_rv', 'risk_earnings_unavailable']
    no_evidence = evaluate_opening_candidate_policy(row, mode=mode, require_earnings_evidence=False)
    assert 'risk_earnings_unavailable' not in [r['reason'] for r in no_evidence['rejects']]


@pytest.mark.parametrize('mode', ['put', 'call'])
@pytest.mark.parametrize('operation', ['constraints', 'evaluate', 'rank', 'explain'])
def test_callback_mutation_rejected_without_mutating_original(alternatives, monkeypatch, mode, operation):
    descriptor = alternatives[mode]
    def mutate(facts, **kwargs):
        target = facts[0] if isinstance(facts, list) else facts
        target['net_income_cny'] = 999999
        return getattr(descriptor, operation)(facts, **kwargs)
    monkeypatch.setitem(core._TEMPLATES, (descriptor.template_id, descriptor.version), replace(descriptor, **{operation: mutate}))
    strategy = _alternate(mode)
    row = attach_strategy_identity({**deepcopy(next(c['policy_row'] for c in VALID if c['mode'] == mode)), **delta_evidence(mode=mode)}, strategy)
    before = deepcopy(row)
    with pytest.raises(ValueError, match='mutated frozen'):
        if operation == 'constraints':
            core.calculate_constraints(row, mode=mode, risk_ctx=UNKNOWN, exchange_rate_converter=CONVERTER, strategy=strategy)
        elif operation == 'explain':
            core.explain_rank(row, mode=mode)
        else:
            core.evaluate_candidates([row], strategy=strategy)
    assert row == before


@pytest.mark.parametrize('mode', ['put', 'call'])
@pytest.mark.parametrize('fault', ['constraint_fact', 'evaluation_copy', 'malformed_decision', 'unknown_stage', 'drop', 'duplicate', 'new', 'changed_copy'])
def test_callback_output_contracts_fail_closed(alternatives, monkeypatch, mode, fault):
    descriptor = alternatives[mode]
    def bad_constraints(row, **kwargs):
        return {'cash_free_effective_native': 999999}
    def bad_evaluate(row, **kwargs):
        result = _accept(row, **kwargs)
        if fault == 'evaluation_copy':
            result['normalized_input']['strike'] = 1234
        elif fault == 'unknown_stage':
            result['rejects'] = [{'stage': 'invented', 'reason': 'policy_rejected'}]
        else:
            result['accepted'] = False
        return result
    def bad_rank(rows, **kwargs):
        if fault == 'drop':
            return []
        if fault == 'duplicate':
            return rows + rows
        copied = deepcopy(rows)
        copied[0]['contract_symbol' if fault == 'new' else 'net_income_cny'] = 'forged' if fault == 'new' else 9
        return copied
    operation = ('constraints' if fault == 'constraint_fact' else 'evaluate'
                 if fault in ('evaluation_copy', 'malformed_decision', 'unknown_stage') else 'rank')
    callback = {'constraints': bad_constraints, 'evaluate': bad_evaluate, 'rank': bad_rank}[operation]
    monkeypatch.setitem(core._TEMPLATES, (descriptor.template_id, descriptor.version), replace(descriptor, **{operation: callback}))
    strategy = _alternate(mode)
    row = {**deepcopy(next(c['policy_row'] for c in VALID if c['mode'] == mode)), **delta_evidence(mode=mode)}
    with pytest.raises(ValueError):
        if operation == 'constraints':
            core.calculate_constraints(row, mode=mode, risk_ctx=UNKNOWN, exchange_rate_converter=CONVERTER, strategy=strategy)
        else:
            core.evaluate_candidates([row], strategy=strategy)


@pytest.mark.parametrize('mode', ['put', 'call'])
def test_version_scope_hash_legacy_and_mixed_universes(alternatives, mode):
    strategy = _alternate(mode)
    raw = deepcopy(next(c['policy_row'] for c in VALID if c['mode'] == mode))
    row = attach_strategy_identity(raw, strategy)
    assert core.validate_strategy_identity(row, mode=mode) == strategy
    for changes in ({'template_id': 'missing'}, {'template_version': 'missing'}, {'mode': 'call' if mode == 'put' else 'put'}):
        kwargs = dict(mode=mode, config=strategy.config, template_id=f'test_{mode}', template_version='test.1')
        with pytest.raises(ValueError):
            core.resolve_strategy(**{**kwargs, **changes})
    for key, value in [('template', 'cc_baseline' if mode == 'call' else 'csp_baseline'),
                       ('template_version', '0.1.0'), ('ranking_profile', 'concentration_first')]:
        corrupt = deepcopy(row)
        corrupt['strategy_core'][key] = value
        with pytest.raises(ValueError):
            core.validate_strategy_identity(corrupt, mode=mode)
    for scope in ({'account': 'sy'}, {'market': 'hk'}):
        with pytest.raises(ValueError):
            core.validate_strategy_identity(row, mode=mode, **scope)
    different_strike = replace(strategy, config=replace(strategy.config, max_strike=200))
    assert different_strike.identity['effective_policy_hash'] != strategy.identity['effective_policy_hash']
    other = attach_strategy_identity({**raw, 'contract_symbol': 'another'}, different_strike)
    assert len(core.rank_candidates([row, other], mode=mode)) == 2
    baseline = _strategy(mode)
    legacy = baseline.identity
    legacy.pop('identity_format')
    legacy['effective_policy_hash'] = strategy_policy_hash(legacy['effective_parameters'], legacy['ranking_profile'])
    legacy_row = {**raw, 'strategy_core': legacy}
    assert core.validate_strategy_identity(legacy_row, mode=mode) == baseline
    for other in (raw, legacy_row, attach_strategy_identity(raw, baseline)):
        with pytest.raises(ValueError):
            core.rank_candidates([row, other], mode=mode)
    legacy['core_version'] = legacy['template_version'] = '99.0'
    with pytest.raises(ValueError):
        core.validate_strategy_identity(legacy_row, mode=mode)
    corrupted = deepcopy(row)
    corrupted['strategy_core']['policy_kind'] = 'baseline'
    corrupted['strategy_core']['effective_policy_hash'] = implementation_policy_hash(corrupted['strategy_core'])
    with pytest.raises(ValueError):
        core.validate_strategy_identity(corrupted, mode=mode)


@pytest.mark.parametrize('mode', ['put', 'call'])
def test_constraint_extra_fields_survive_real_projection(frozen_scan, alternatives, monkeypatch, mode):
    descriptor = alternatives[mode]
    def constraints(row, *, mode, cfg):
        assert row['policy_min_net_premium_cny'] == cfg.min_net_income
        return {**_constraints(row, mode=mode, cfg=cfg), 'policy_test_threshold': 42.0, 'policy_max_dte': 123}
    def evaluate(row, **kwargs):
        assert row['policy_test_threshold'] == 42.0
        assert row['policy_max_dte'] == 123
        return _accept(row, **kwargs)
    monkeypatch.setitem(core._TEMPLATES, (descriptor.template_id, descriptor.version),
                        replace(descriptor, constraints=constraints, evaluate=evaluate))
    finals, decisions, _ = _run(frozen_scan, mode, _alternate(mode))
    assert all(row['policy_test_threshold'] == 42 and row['policy_max_dte'] == 123 for row in finals)
    assert all(row['normalized_input']['policy_test_threshold'] == 42 for row in decisions)


def test_exact_legacy_identity_readable_but_new_execution_rejected():
    from domain.domain import strategy_identity
    raw = deepcopy(next(case['policy_row'] for case in VALID if case['mode'] == 'put'))
    legacy = _strategy('put').identity
    legacy.pop('identity_format')
    legacy['core_version'] = legacy['template_version'] = '0.1.0'
    legacy['effective_parameters'].pop('max_abs_delta')
    legacy['effective_policy_hash'] = strategy_policy_hash(legacy['effective_parameters'], legacy['ranking_profile'])
    legacy['runtime'] = {'mode': 'source', 'core_version': '0.1.0', 'build_digest': None}
    row = {**raw, 'strategy_core': legacy}
    before = deepcopy(row)
    assert strategy_identity.validate_strategy_identity(row, mode='put') == legacy
    with pytest.raises(ValueError, match='unavailable strategy template'):
        core.validate_strategy_identity(row, mode='put')
    with pytest.raises(ValueError, match='unavailable strategy template'):
        core.rank_candidates([row], mode='put')
    assert row == before


@pytest.mark.parametrize('mode', ['put', 'call'])
def test_known_event_treatment_is_policy_over_qualified_evidence(alternatives, mode):
    from candidate_evidence_helpers import earnings_evidence
    row = {**deepcopy(next(case['policy_row'] for case in VALID if case['mode'] == mode)), **delta_evidence(mode=mode)}
    row.update(earnings_evidence(expiration=row['expiration'], market_date=row['earnings_market_date'],
                                 event_date=row['expiration']))
    baseline = core.evaluate_candidates([row], strategy=_strategy(mode))['decisions'][0]
    assert any(item['reason'] == 'risk_earnings_event' for item in baseline['opening_decision']['rejects'])
    assert core.evaluate_candidates([row], strategy=_alternate(mode))['decisions'][0]['accepted']
