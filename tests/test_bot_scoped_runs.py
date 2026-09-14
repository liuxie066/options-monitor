import json

from src.application.bot import tools
from src.application.agent_tools import project_reader, runtime


def test_bot_runtime_runs_uses_trusted_scope_and_safe_failure_record(tmp_path, monkeypatch, example_config_path):
    monkeypatch.setenv('OM_RUNTIME_ROOT', str(example_config_path.parent))
    monkeypatch.setattr(project_reader, '_key', lambda: 'test-cursor-key')
    base = example_config_path.parent
    metrics = base / 'output_runs/20260912T000000Z/accounts/lx/state/account_metrics.json'
    metrics.parent.mkdir(parents=True)
    metrics.write_text(json.dumps({'run_id': '20260912T000000Z', 'account': 'lx',
        'markets_to_run': ['US'], 'as_of_utc': '2026-09-12T00:00:00Z',
        'ran_scan': True, 'ran_pipeline': False, 'reason': 'account_config_hash_mismatch'}))
    payload, error = tools.build_tool_payload('runtime_runs', {'account': 'lx'},
        fixed_input={'config_path': str(example_config_path)})
    assert error is None
    assert payload['action'] == 'scoped'
    assert payload['config_path'] == str(example_config_path)
    response = tools.call_read_tool('runtime_runs', payload, allowed_tools=('runtime_runs',))
    assert response['ok'], response
    observation = tools.model_observation('runtime_runs', response)
    row = observation['data']['runs'][0]
    assert row['reason'] == 'account_config_hash_mismatch'
    assert row['outcomes'] == {'usable_scan_result': True, 'pipeline_completed_successfully': False}
    assert 'scanned' not in row and 'ran_pipeline' not in row and 'terminal' not in row
    assert observation['data']['pagination']['returned_count'] == 1
    assert str(base) not in json.dumps(observation)
    schema = tools.tool_descriptions(('runtime_runs',))[0]
    assert schema['default_input']['action'] == 'scoped'
    assert schema['output_contract']['schema_version'] == 'runtime_runs.scoped.v1'


def test_public_legacy_runs_interface_remains_available(monkeypatch):
    calls = []
    monkeypatch.setattr(runtime, 'collect_runtime_runs', lambda **kwargs: calls.append(kwargs) or {'runs': [], 'runs_root': None})
    data, warnings, _ = runtime.RUNTIME_RUNS_TOOL.call({'limit': 3})
    assert data['runs'] == [] and warnings == []
    assert calls[0]['limit'] == 3
