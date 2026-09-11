from __future__ import annotations

from pathlib import Path
import pytest

from src.application.agent_tools import candidate
from src.application.bot.tools import compact_observation
from src.application.tool_execution import execute_tool
from tests.candidate_evidence_helpers import seal_opening_candidate_fixture
from tests.test_bot_s8_host_admission import _run_answered_host


def _seed(monkeypatch, tmp_path: Path, *, count: int = 1) -> None:
    monkeypatch.setattr(candidate, 'repo_base', lambda: tmp_path)
    monkeypatch.setattr(candidate, 'load_runtime_config', lambda **_: (tmp_path / 'config.us.json', {}))
    seal_opening_candidate_fixture(tmp_path, run_id='candidate-source',
        accepted_rows=[{'symbol': 'PDD', 'contract_symbol': f'PDD-{mode}-{i}', 'mode': mode}
                       for mode in ('put', 'call') for i in range(count)],
        rejected_rows=[{'symbol': 'PDD', 'contract_symbol': f'PDD-rejected-{i}', 'rule': 'risk_spread',
                        'metric_value': (0.4, 0.5)[i], 'threshold': (0.2, 0.3)[i]}
                       for i in range(2)])


@pytest.mark.parametrize('tool,args,scope', [
    ('candidate_filter_explain', {'symbol': 'PDD', 'function': 'sell_put'}, 'point'),
    ('candidate_rank_explain', {'mode': 'put', 'top_n': 1}, 'requested_page'),
    ('candidate_rank_explain', {}, 'requested_page'),
])
def test_real_candidate_tool_through_host_admission(monkeypatch, tmp_path: Path, tool, args, scope) -> None:
    _seed(monkeypatch, tmp_path, count=10)
    def flow(call):
        obs = call({'call_id': 'candidate', 'tool_name': tool,
                    'arguments': {'account': 'lx', 'run_id': 'candidate-source', **args}})
        assert obs['status'] == 'complete', obs
        assert obs['freshness']['status'] == 'historical'
        assert obs['source']['run_id'] == 'candidate-source'
        if tool == 'candidate_filter_explain':
            summary = obs['value']['summary'][0]
            assert summary['accepted_count'] == 10 and summary['rejected_count'] == 2
            assert {(row['contract_symbol'], row['threshold'], row['metric_value']) for row in summary['examples']} == {
                ('PDD-rejected-0', 0.2, 0.4), ('PDD-rejected-1', 0.3, 0.5)}
        else:
            assert len(obs['value']['ranked_summary']) == (1 if args else 20)
            assert all(row['primary_drivers'] for row in obs['value']['ranked_summary'])
        for kind, required in [('current_fact', scope), ('historical_fact', 'full_query')]:
            rejected = call({'call_id': f'reject-{kind}', 'tool_name': 'submit_answer', 'arguments': {
                'mode': 'evidence', 'status': 'complete', 'answer_markdown': '候选记录。',
                'claims': [{'text': '候选记录', 'kind': kind, 'required_scope': required, 'observation_ids': [obs['ref']]}]}})
            assert rejected['observation']['ok'] is False
        admitted = call({'call_id': 'answer', 'tool_name': 'submit_answer', 'arguments': {
            'mode': 'evidence', 'status': 'complete', 'answer_markdown': '历史批次的候选记录可用。',
            'claims': [{'text': '历史批次的候选记录可用', 'kind': 'historical_fact', 'required_scope': scope,
                        'observation_ids': [obs['ref']]}]}})
        assert admitted['observation']['ok'] is True, admitted
        return admitted['approved_answer']['text']
    result = _run_answered_host(monkeypatch, '解释 PDD 的历史筛选和排名', flow)
    assert result.ok is True, result


def test_oversized_candidate_page_has_supported_narrowing(monkeypatch, tmp_path: Path) -> None:
    _seed(monkeypatch, tmp_path, count=21)
    payload = {'account': 'lx', 'run_id': 'candidate-source', 'mode': 'put', 'top_n': 100}
    obs = compact_observation('candidate_rank_explain', execute_tool('candidate_rank_explain', payload), payload)
    assert obs['status'] == 'needs_narrowing'
    assert obs['coverage']['status'] == 'partial'
    assert 'top_n=1' in obs['value']['message']


def test_large_filter_keeps_bounded_summary_and_all_counts(monkeypatch, tmp_path: Path) -> None:
    _seed(monkeypatch, tmp_path)
    from domain.domain.engine.candidate_engine import CANDIDATE_REJECT_REASONS
    reasons = CANDIDATE_REJECT_REASONS[:9]
    seal_opening_candidate_fixture(tmp_path, run_id='large-filter', rejected_rows=[
        {'symbol': 'PDD', 'contract_symbol': f'PDD-{i:03d}', 'rule': reasons[i % 9],
         'threshold': i, 'metric_value': i + 1}
        for i in range(74)
    ])
    payload = {'account': 'lx', 'run_id': 'large-filter', 'symbol': 'PDD', 'function': 'sell_put'}
    obs = compact_observation('candidate_filter_explain', execute_tool('candidate_filter_explain', payload), payload)
    assert obs['status'] == 'complete'
    summary = obs['value']['summary'][0]
    assert summary['rejected_count'] == 74
    # Includes the symbol-level no-candidate reason in addition to 74 contracts.
    assert summary['rules_total_count'] == 10 and len(summary['rules']) == 8
    assert summary['examples_total_count'] == 75 and len(summary['examples']) == 3
    for item in summary['examples']:
        index = int(item['contract_symbol'].split('-')[1])
        assert (item['threshold'], item['metric_value']) == (index, index + 1)


def test_empty_rank_is_valid_but_unknown_symbol_is_partial(monkeypatch, tmp_path: Path) -> None:
    _seed(monkeypatch, tmp_path)
    seal_opening_candidate_fixture(tmp_path, run_id='empty-scan')
    for tool, extra, status in [
        ('candidate_rank_explain', {'mode': 'put'}, 'not_found'),
        ('candidate_filter_explain', {'symbol': 'PDD'}, 'partial'),
    ]:
        payload = {'account': 'lx', 'run_id': 'empty-scan', **extra}
        obs = compact_observation(tool, execute_tool(tool, payload), payload)
        assert obs['status'] == status, obs
        if tool == 'candidate_rank_explain':
            assert obs['value'].get('ranked_summary', []) == []
            assert obs['coverage']['status'] == 'complete'
            assert obs['coverage']['total_count'] == 0


def test_missing_manifest_time_cannot_be_used_as_evidence(monkeypatch, tmp_path: Path) -> None:
    import json
    _seed(monkeypatch, tmp_path)
    path = tmp_path / 'output_runs/candidate-source/accounts/lx/state/candidate_snapshot_manifest.v1.json'
    manifest = json.loads(path.read_text())
    from domain.domain.decision_state_fingerprint import canonical_sha256
    manifest.pop('sealed_at_utc')
    manifest['content_sha256'] = canonical_sha256({k: v for k, v in manifest.items() if k != 'content_sha256'})
    path.write_text(json.dumps(manifest))
    def flow(call):
        obs = call({'call_id': 'read', 'tool_name': 'candidate_rank_explain', 'arguments': {
            'account': 'lx', 'run_id': 'candidate-source', 'mode': 'put', 'top_n': 1}})
        assert obs['status'] == 'failed'
        rejected = call({'call_id': 'answer', 'tool_name': 'submit_answer', 'arguments': {
            'mode': 'evidence', 'status': 'complete', 'answer_markdown': '候选排名',
            'claims': [{'text': '候选排名', 'kind': 'historical_fact', 'required_scope': 'requested_page',
                        'observation_ids': [obs['ref']]}]}})
        assert rejected['observation']['ok'] is False
        return '没有可核实证据'
    result = _run_answered_host(monkeypatch, '历史候选排名', flow)
    assert result.status == 'failed', result
    assert result.error['code'] == 'RESULT_REJECTED'


def test_candidate_observation_cannot_be_reused_by_next_request(monkeypatch, tmp_path: Path) -> None:
    _seed(monkeypatch, tmp_path)
    references = []
    def flow(call):
        if not references:
            obs = call({'call_id': 'read', 'tool_name': 'candidate_rank_explain', 'arguments': {
                'account': 'lx', 'run_id': 'candidate-source', 'mode': 'put', 'top_n': 1}})
            references.append(obs['ref'])
            expected = True
        else:
            expected = False
        reply = call({'call_id': 'answer', 'tool_name': 'submit_answer', 'arguments': {
            'mode': 'evidence', 'status': 'complete', 'answer_markdown': '历史排名记录。',
            'claims': [{'text': '历史排名记录', 'kind': 'historical_fact', 'required_scope': 'requested_page',
                        'observation_ids': references}]}})
        assert reply['observation']['ok'] is expected
        if not expected:
            assert reply['observation']['reason'] == 'observation_outside_request'
        return reply.get('approved_answer', {}).get('text', '不能沿用上次证据')
    assert _run_answered_host(monkeypatch, '查看历史排名', flow).ok is True
    result = _run_answered_host(monkeypatch, '另一个请求的排名', flow)
    assert result.error['code'] == 'RESULT_REJECTED'
