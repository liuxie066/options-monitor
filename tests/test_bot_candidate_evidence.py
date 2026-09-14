from __future__ import annotations

from pathlib import Path
import pytest

from src.application.agent_tools import candidate
from src.application.bot.tools import compact_observation
from src.application.tool_execution import execute_tool
from tests.candidate_evidence_helpers import seal_opening_candidate_fixture


def _seed(monkeypatch, tmp_path: Path, *, count: int = 1) -> None:
    monkeypatch.setattr(candidate, 'repo_base', lambda: tmp_path)
    monkeypatch.setattr(candidate, 'load_runtime_config', lambda **_: (tmp_path / 'config.us.json', {}))
    seal_opening_candidate_fixture(tmp_path, run_id='candidate-source',
        accepted_rows=[{'symbol': 'PDD', 'contract_symbol': f'PDD-{mode}-{i}', 'mode': mode}
                       for mode in ('put', 'call') for i in range(count)],
        rejected_rows=[{'symbol': 'PDD', 'contract_symbol': f'PDD-rejected-{i}', 'rule': 'risk_spread',
                        'metric_value': (0.4, 0.5)[i], 'threshold': (0.2, 0.3)[i]}
                       for i in range(2)])


def test_oversized_candidate_page_has_supported_narrowing(monkeypatch, tmp_path: Path) -> None:
    _seed(monkeypatch, tmp_path, count=21)
    payload = {'account': 'lx', 'run_id': 'candidate-source', 'mode': 'put', 'top_n': 100}
    obs = compact_observation('candidate_rank_explain', execute_tool('candidate_rank_explain', payload), payload)
    assert obs['ok'] is True
    assert obs['data']['coverage']['status'] == 'partial'
    assert 'top_n=1' in obs['data']['narrowing_hint']


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
    assert obs['ok'] is True
    summary = obs['data']['summary'][0]
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
    for tool, extra in [
        ('candidate_rank_explain', {'mode': 'put'}),
        ('candidate_filter_explain', {'symbol': 'PDD'}),
    ]:
        payload = {'account': 'lx', 'run_id': 'empty-scan', **extra}
        obs = compact_observation(tool, execute_tool(tool, payload), payload)
        assert obs['ok'] is True, obs
        if tool == 'candidate_rank_explain':
            assert obs['data'].get('ranked_summary', []) == []
            assert obs['data']['coverage']['status'] == 'complete'
            assert obs['data']['coverage']['total_count'] == 0
        else:
            assert obs['data']['conclusion_status'] == 'indeterminate'
            assert 'no_matching_snapshot_scope' in obs['warnings']


def test_missing_manifest_time_cannot_be_used_as_evidence(monkeypatch, tmp_path: Path) -> None:
    import json
    _seed(monkeypatch, tmp_path)
    path = tmp_path / 'output_runs/candidate-source/accounts/lx/state/candidate_snapshot_manifest.v1.json'
    manifest = json.loads(path.read_text())
    from domain.domain.decision_state_fingerprint import canonical_sha256
    manifest.pop('sealed_at_utc')
    manifest['content_sha256'] = canonical_sha256({k: v for k, v in manifest.items() if k != 'content_sha256'})
    path.write_text(json.dumps(manifest))
    payload = {'account': 'lx', 'run_id': 'candidate-source', 'mode': 'put', 'top_n': 1}
    obs = compact_observation('candidate_rank_explain', execute_tool('candidate_rank_explain', payload), payload)
    assert obs['ok'] is False
    assert obs['error']['code'] == 'DEPENDENCY_MISSING'
