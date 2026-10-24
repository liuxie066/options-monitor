"""Ordinary default Delta gate: policy, scope, snapshots and config boundaries."""
from copy import deepcopy
import math

import pytest

from candidate_evidence_helpers import delta_evidence, earnings_evidence
from domain.domain.engine.candidate_engine import evaluate_opening_candidate_policy, rank_candidate_rows
from domain.domain.insurance_underwriting import InsuranceUnderwritingConfig
from domain.domain.strategy_core import resolve_strategy, evaluate_candidates
from domain.domain import strategy_core
from domain.domain.strategy_identity import validate_strategy_identity, implementation_policy_hash
from src.application.covered_call_strategy_risk import resolve_covered_call_underwriting_config
from src.application.sell_put_strategy_risk import resolve_sell_put_underwriting_config
from src.application.config_validator import _validate_opening_strategy_config
from src.application.candidate_scanning import evidence_summary_from_decisions, project_evidence_scan_status


def row(mode='put', **changes):
    data=dict(symbol='NVDA', contract_symbol='NVDA260918P00095000' if mode=='put' else 'NVDA260918C00105000',
              expiration='2026-09-18', dte=43, strike=95 if mode=='put' else 105, spot=100,
              net_income_cny=1000, max_new_contracts=2, covered_contracts_available=2,
              annualized_net_return_on_cash_basis=.25, annualized_net_premium_return=.25,
              period_net_return_on_cash_basis=.03, period_net_premium_return=.03,
              spread_ratio=.1, iv_rv_ratio=1.5, iv_minus_rv=.1, open_interest=500,
              **earnings_evidence(expiration='2026-09-18', market_date='2026-08-06'), **delta_evidence(mode=mode))
    data.update(changes)
    return data


@pytest.mark.parametrize('mode,sign',[('put',-1),('call',1)])
@pytest.mark.parametrize('value,accepted',[(0,True),(.35,True),(.4,True),(math.nextafter(.4,math.inf),False),(.4001,False)])
def test_default_exact_boundary(mode,sign,value,accepted):
    original=row(mode,delta=sign*value); frozen=deepcopy(original)
    result=evaluate_opening_candidate_policy(original,mode=mode)
    assert result['accepted'] is accepted
    assert original==frozen
    assert [r['reason'] for r in result['rejects']]==([] if accepted else ['risk_delta'])
    if not accepted: assert result['rejects'][0]['metric_value']['delta']==sign*value


@pytest.mark.parametrize('changes',[
    {'delta':None},{'delta':True},{'delta':'0.3'},{'delta':float('nan')},{'delta':float('inf')},
    {'delta':-float('inf')},{'delta':10**400},{'delta':35},{'delta':.2},{'delta_source':None},{'delta_unit':'percent'},
    {'delta_reason_code':'delta_identity_unverified'},{'snapshot_received_at_utc':None},
    {'snapshot_received_at_utc':'2026-08-06T14:54:59.999999+00:00'},
    {'snapshot_received_at_utc':'2026-08-06T15:00:00.000001+00:00'},
    {'decision_now_utc':None},{'decision_now_utc':'2026-08-06T15:00:00'}, {'option_type':'call'},
])
def test_unverifiable_is_not_above_cap_or_zero(changes):
    result=evaluate_opening_candidate_policy(row(**changes),mode='put')
    assert [r['reason'] for r in result['rejects']]==['risk_delta_unavailable']
    assert result['rejects'][0]['metric_value']['reason_code']
    assert evaluate_opening_candidate_policy(row(**changes),mode='put',max_abs_delta=None)['accepted']


def test_config_override_null_zero_and_precedence():
    r=row(delta=-.4)
    assert not evaluate_opening_candidate_policy(r,mode='put',max_abs_delta=.35)['accepted']
    assert evaluate_opening_candidate_policy({**r,'policy_max_abs_delta':None},mode='put')['accepted']
    assert not evaluate_opening_candidate_policy({**r,'policy_max_abs_delta':None},mode='put',max_abs_delta=.35)['accepted']
    assert not evaluate_opening_candidate_policy(row(delta=-.0001),mode='put',max_abs_delta=0)['accepted']
    assert evaluate_opening_candidate_policy(row(delta=-0.0),mode='put',max_abs_delta=0)['accepted']
    assert evaluate_opening_candidate_policy(row(snapshot_received_at_utc='2026-08-06T14:55:00+00:00'),mode='put')['accepted']
    for bad in [row(delta=0,spread_ratio=.5),row(delta=0,max_new_contracts=0),row(delta=0,annualized_net_return_on_cash_basis=0)]:
        assert not evaluate_opening_candidate_policy(bad,mode='put')['accepted']


@pytest.mark.parametrize('invalid',[True,False,'0.4',float('nan'),float('inf'),-.01,1.01,10**400])
def test_invalid_policy_never_disables(invalid):
    with pytest.raises(ValueError): InsuranceUnderwritingConfig(max_abs_delta=invalid)
    with pytest.raises(ValueError): evaluate_opening_candidate_policy(row(),mode='put',max_abs_delta=invalid)
    with pytest.raises(ValueError): evaluate_opening_candidate_policy(row(policy_max_abs_delta=invalid),mode='put')
    for resolver in [resolve_covered_call_underwriting_config,resolve_sell_put_underwriting_config]:
        with pytest.raises(ValueError): resolver({'max_abs_delta':invalid})
    with pytest.raises(SystemExit): _validate_opening_strategy_config({'max_abs_delta':invalid},'sell_put')


@pytest.mark.parametrize('value',[None,0,.35,.4,1])
def test_resolvers_and_config_validate_same_contract(value):
    for resolver in [resolve_covered_call_underwriting_config,resolve_sell_put_underwriting_config]:
        assert resolver({}).max_abs_delta==.4
        assert resolver({'max_abs_delta':value}).max_abs_delta==value
    _validate_opening_strategy_config({'max_abs_delta':value},'sell_call')


def test_custom_template_cannot_bypass_delta(monkeypatch):
    from domain.domain.engine.candidate_engine import build_candidate_decision
    def accept(r,*,mode,cfg):
        return build_candidate_decision(mode=mode,symbol=r['symbol'],contract_symbol=r['contract_symbol'],accepted=True,normalized_input=r)
    template=strategy_core.StrategyTemplate('test_delta','1','put',lambda *a,**k:{},accept,
        lambda rows,**kw:rows,lambda *a,**kw:{})
    monkeypatch.setitem(strategy_core._TEMPLATES,('test_delta','1'),template)
    config=InsuranceUnderwritingConfig()
    strategy=resolve_strategy(mode='put',config=config,template_id='test_delta',template_version='1')
    result=evaluate_candidates([row(delta=-.5,policy_max_abs_delta=None)],strategy=strategy)
    assert result['accepted']==[]
    assert result['decisions'][0]['opening_decision']['rejects'][0]['reason']=='risk_delta'
    result=evaluate_candidates([row(delta_source=None)],strategy=strategy)
    assert result['decisions'][0]['opening_decision']['rejects'][0]['reason']=='risk_delta_unavailable'


def test_identity_versions_hash_and_old_read_only():
    identities=[resolve_strategy(mode='put',config=InsuranceUnderwritingConfig(max_abs_delta=cap)).identity for cap in [.4,.35,None]]
    assert len({i['effective_policy_hash'] for i in identities})==3
    assert all(i['template_version']=='0.2.0' for i in identities)
    old=deepcopy(identities[0]);old['core_version']=old['template_version']='0.1.0'
    del old['effective_parameters']['max_abs_delta']
    old['effective_policy_hash']=implementation_policy_hash(old)
    assert validate_strategy_identity({'strategy_core':old},mode='put')==old
    with pytest.raises(ValueError,match='unavailable strategy template'):
        strategy_core.validate_strategy_identity({'strategy_core':old},mode='put')


def test_unknown_and_valid_empty_status_are_distinct():
    for delta,source,expected in [(-.5,'opend_market_snapshot.option_delta',('completed','no_candidate')),
                                 (-.3,None,('unavailable','data_unavailable'))]:
        decision=evaluate_opening_candidate_policy(row(delta=delta,delta_source=source),mode='put')
        evidence=evidence_summary_from_decisions(decisions=[{'opening_decision':decision}],accepted_count=0)
        assert project_evidence_scan_status(evidence=evidence,candidate_count=0)==expected


def test_survivor_rank_is_unchanged_and_cross_expiry():
    rows=[row(delta=-.4),row(delta=-.3,contract_symbol='NVDA261016P00090000',expiration='2026-10-16',dte=60,strike=90),row(delta=-.5,contract_symbol='HIGH')]
    rows[1].update(earnings_evidence(expiration='2026-10-16',market_date='2026-08-06'))
    expected=rank_candidate_rows(rows[:2],mode='put')
    accepted=[r for r in rows if evaluate_opening_candidate_policy(r,mode='put')['accepted']]
    assert rank_candidate_rows(accepted,mode='put')==expected


def test_yaml_build_covered_call_override_and_null_template(tmp_path):
    from test_config_yaml import _FUTU_ACCOUNTS_YAML, REPO_ROOT, _write_yaml
    from src.application.config_yaml import resolve_yaml_runtime_config, explain_yaml_config_key
    from src.application.config_profiles import apply_profiles
    text=_FUTU_ACCOUNTS_YAML+'''templates:
  put_base:
    sell_put:
      max_abs_delta: 0.35
  call_base:
    covered_call:
      max_abs_delta: 0.4
markets:
  us:
    accounts: [lx]
    symbols: [NVDA]
    overrides:
      NVDA:
        use: [put_base, call_base]
        covered_call:
          max_abs_delta: null
'''
    path=_write_yaml(tmp_path/'config.yaml',text)
    cfg,_=resolve_yaml_runtime_config(repo_root=REPO_ROOT,market='us',config_path=path)
    effective=apply_profiles(cfg['symbols'][0],cfg['templates'])
    assert resolve_sell_put_underwriting_config(effective['sell_put']).max_abs_delta==.35
    assert resolve_covered_call_underwriting_config(effective['sell_call']).max_abs_delta is None
    explained=explain_yaml_config_key(repo_root=REPO_ROOT,market='us',key='symbols.0.covered_call.max_abs_delta',config_path=path)
    assert explained['exists'] and explained['value'] is None
    assert explained['runtime_path']=='symbols.0.sell_call.max_abs_delta'
