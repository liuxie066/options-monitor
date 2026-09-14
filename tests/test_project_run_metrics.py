import json

import pytest

from src.application.agent_tools.project_reader import ProjectReaderError
from src.application.agent_tools.project_runs import discover_runs, load_run_bundle, load_run_metrics
from src.application.runtime_paths import RuntimeRootResolution


def _metrics(root, **changes):
    path = root / 'output_runs/run-failure/accounts/lx/state/account_metrics.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {'run_id': 'run-failure', 'account': 'lx', 'markets_to_run': ['US'],
             'as_of_utc': '2026-09-12T00:00:00Z', 'ran_scan': True, 'ran_pipeline': False,
             'reason': 'account_config_hash_mismatch', 'run_dir': '/private/runtime/location'}
    value.update(changes)
    path.write_text(json.dumps(value))
    return path


def _scope(root):
    return {'runtime_root': RuntimeRootResolution(root, 'argument'),
            'run_id': 'run-failure', 'account': 'lx', 'market': 'us',
            'authorized_accounts': ['lx']}


def test_failed_run_is_discoverable_without_candidate_bundle(tmp_path):
    _metrics(tmp_path)
    scope = _scope(tmp_path)
    found = discover_runs(**{key: value for key, value in scope.items() if key != 'run_id'})
    assert found['entries'][0]['run_id'] == 'run-failure'
    assert found['entries'][0]['reason'] == 'account_config_hash_mismatch'
    assert found['entries'][0]['resource_categories'] == ['account_diagnostics']
    assert '/private/runtime' not in json.dumps(found)
    with pytest.raises(ProjectReaderError, match='bundle_unavailable'):
        load_run_bundle(**scope)


@pytest.mark.parametrize(('changes', 'code'), [
    ({'markets_to_run': ['HK']}, 'market_unverifiable'),
    ({'markets_to_run': []}, 'market_unverifiable'),
    ({'markets_to_run': ['US', 'HK']}, 'market_unverifiable'),
    ({'account': 'sy'}, 'scope_conflict'),
    ({'run_id': 'other'}, 'scope_conflict'),
    ({'as_of_utc': '2026-09-12'}, 'metrics_invalid'),
    ({'ran_scan': 'true'}, 'metrics_invalid'),
])
def test_metrics_payload_must_prove_its_identity(tmp_path, changes, code):
    _metrics(tmp_path, **changes)
    with pytest.raises(ProjectReaderError, match=code):
        load_run_metrics(**_scope(tmp_path))


def test_unknown_reason_does_not_expose_free_exception_text(tmp_path):
    _metrics(tmp_path, reason='failed token=sk-secret-private /private/path')
    result = load_run_metrics(**_scope(tmp_path))
    assert result['reason'] is None and result['cause_available'] is False
    assert 'sk-secret' not in json.dumps(result)


def test_corrupt_newer_run_is_an_explicit_discovery_gap(tmp_path):
    _metrics(tmp_path)
    path = tmp_path / 'output_runs/z-newer/accounts/lx/state/account_metrics.json'
    path.parent.mkdir(parents=True)
    path.write_text('{broken')
    scope = _scope(tmp_path)
    found = discover_runs(**{key: value for key, value in scope.items() if key != 'run_id'})
    assert found['entries'][0]['unverified_newer_count'] == 1
    assert found['coverage']['status'] == 'partial'
    assert 'metrics_invalid' in found['reasons']
