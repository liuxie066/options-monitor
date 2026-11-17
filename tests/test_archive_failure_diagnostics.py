"""Bounded archive diagnostics preserve safe causes, never exception payloads."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from src.application.research import archive_bounded as owner


def snapshot(source, roots, **limits):
    request = dict(roots=roots, seconds=2, max_entries=100, max_bytes=10000, max_file_bytes=1000)
    request.update(limits)
    return subprocess.run([sys.executable, '-c', owner.SNAPSHOT_SCRIPT, str(source), json.dumps(request)],
                          capture_output=True, timeout=3)


@pytest.mark.parametrize('case,stage,code', [
    ('missing_run', 'selected_tree', 'selected_root_missing'),
    ('entry_limit', 'selected_tree', 'entry_budget'),
    ('byte_limit', 'selected_tree', 'byte_budget'),
    ('manifest', 'manifest', 'invalid_json'),
    ('pointer', 'pointer', 'pointer_outside_runs'),
])
def test_snapshot_failure_returns_only_safe_stage_and_code(tmp_path, case, stage, code):
    run = tmp_path / 'output_runs/secret-run/state'
    run.mkdir(parents=True)
    (run / 'secret.json').write_text('PRIVATE_ACCOUNT_BODY')
    roots = ['output_runs/secret-run']
    limits = {}
    if case == 'missing_run': roots = ['output_runs/missing-secret']
    if case == 'entry_limit': limits['max_entries'] = 1
    if case == 'byte_limit': limits['max_bytes'] = 1
    if case == 'manifest': (run / 'required_data_snapshot_manifest.json').write_text('PRIVATE_ACCOUNT_BODY')
    if case == 'pointer':
        pointer = tmp_path / 'output_shared/state/last_run_dir.txt'
        pointer.parent.mkdir(parents=True)
        pointer.write_text('/private/account-secret')
    result = snapshot(tmp_path, roots, **limits)
    assert result.returncode != 0
    assert json.loads(result.stdout) == {'ok': False, 'error_stage': stage, 'error_code': code}
    assert result.stderr == b''
    assert len(result.stdout) <= 512
    assert b'PRIVATE' not in result.stdout and b'secret' not in result.stdout


@pytest.mark.parametrize('raw,expected', [
    (b'{"ok":false,"error_stage":"hash","error_code":"source_changed"}', 'source_changed'),
    (b'', 'source_failure_unknown'),
    (b'{"ok":false', 'source_failure_unknown'),
    (b'{"ok":false,"error_stage":"hash","error_code":"SECRET"}', 'source_failure_unknown'),
    (b'{"ok":false,"error_stage":[],"error_code":"source_changed"}', 'source_failure_unknown'),
    (b'{"runtime_root":"/private/secret","files":[]}', 'source_failure_unknown'),
    (b'{"ok":false,"error_stage":"hash","error_code":"source_changed","source_exit_code":0}', 'source_failure_unknown'),
    (b' ' * 513, 'source_failure_unknown'),
])
def test_source_error_protocol_rejects_untrusted_or_missing_detail(raw, expected):
    failure = owner._source_failure(raw, 255)
    assert owner._failure_details(failure, 'unused') == {
        'error_stage': 'hash' if expected == 'source_changed' else 'source_snapshot',
        'error_code': expected, 'source_exit_code': 255,
    }


def test_worker_long_path_fallback_keeps_safe_diagnostic(tmp_path, monkeypatch):
    def fail(request):
        raise owner._ArchiveFailure('source_changed', stage='hash', source_exit_code=1)
    monkeypatch.setattr(owner, '_execute', fail)
    request = {'target': str(tmp_path / 'batch'), 'staging': 'x' * 2000, 'result': str(tmp_path / 'receipt')}
    owner._worker(request)
    raw = Path(request['result']).read_bytes()
    value = json.loads(raw)
    assert len(raw) <= 1024
    assert value['error_code'] == 'source_changed'
    assert value['error_stage'] == 'hash' and value['source_exit_code'] == 1
    assert value['source_deleted'] is False and value['state'] == 'failed'
    assert 'staging' not in value


def test_worker_never_returns_exception_message(tmp_path, monkeypatch):
    def fail(request):
        raise PermissionError('PRIVATE_ACCOUNT_BODY /home/secret')
    monkeypatch.setattr(owner, '_execute', fail)
    request = {'target': str(tmp_path / 'batch'), 'staging': str(tmp_path / 'partial'),
               'result': str(tmp_path / 'receipt'), '_stage': 'target_prepare'}
    owner._worker(request)
    raw = Path(request['result']).read_bytes()
    assert b'PRIVATE_ACCOUNT_BODY' not in raw and b'/home/secret' not in raw
    assert json.loads(raw)['error_code'] == 'permission_denied'


@pytest.mark.parametrize('case,stage,code', [
    ('missing', 'selected_tree', 'selected_root_missing'),
    ('protected', 'retention_check', 'selected_run_is_retention_protected'),
    ('host', 'source_identity', 'source_host_mismatch'),
])
def test_actual_worker_reports_distinct_pre_copy_blockers(tmp_path, case, stage, code):
    import os
    from tests.test_archive_bounded import limits_file

    source = tmp_path / 'source'
    run = source / 'output_runs/old/state'
    run.mkdir(parents=True)
    (run / 'last_run.json').write_text('{"ran_scan":false}')
    os.utime(run.parent, (1, 1))
    if case != 'protected':
        for i in range(200): (source / 'output_runs' / f'new-{i}').mkdir()
    before = {str(p.relative_to(source)):p.read_bytes() for p in source.rglob('*') if p.is_file()}
    target = tmp_path / 'batch'
    result = owner.bounded_pull(repo_root=Path.cwd(), archive_root=target, source_root=source,
                                run_ids=['missing' if case == 'missing' else 'old'], include_logs=False,
                                write=True, limits_file=limits_file(tmp_path), include_fixed_shared=False,
                                expected_source_host='deliberately-wrong-host' if case == 'host' else None)
    assert not result['ok'], result
    assert result['error_stage'] == stage and result['error_code'] == code
    if case == 'missing': assert result['source_exit_code'] != 0
    else: assert 'source_exit_code' not in result
    assert not target.exists() and not Path(result['staging']).exists()
    assert result['source_deleted'] is False
    assert {str(p.relative_to(source)):p.read_bytes() for p in source.rglob('*') if p.is_file()} == before


def test_restore_missing_receipt_reports_safe_cause(tmp_path):
    from tests.test_archive_bounded import limits_file
    archive = tmp_path / 'archive'
    archive.mkdir()
    result = owner.restore_batch(archive_root=archive, target=tmp_path / 'restored', limits_file=limits_file(tmp_path))
    assert not result['ok'], result
    assert result['error_stage'] == 'restore' and result['error_code'] == 'input_missing'
    assert result['source_deleted'] is False
    assert 'source_exit_code' not in result


@pytest.mark.parametrize('exit_code,raw,code', [
    (0, '{"ok":false,"error_stage":"hash","error_code":"source_changed"}', 'source_protocol_invalid'),
    (255, '', 'source_failure_unknown'),
    (1, '{"runtime_root":"/synthetic","files":[]}', 'source_failure_unknown'),
    (0, 'not JSON private text', 'source_protocol_invalid'),
])
def test_snapshot_process_exit_must_match_terminal_protocol(tmp_path, monkeypatch, exit_code, raw, code):
    import time
    command = [sys.executable, '-c', 'import sys;sys.stdout.write(sys.argv[1]);sys.exit(int(sys.argv[2]))', raw, str(exit_code)]
    monkeypatch.setattr(owner, '_source_command', lambda *args: command)
    limits = dict(max_bytes=1000, max_entries=10, max_file_bytes=1000, _evidence_remaining=4096, grace_seconds=.1)
    with pytest.raises(owner._ArchiveFailure) as caught:
        owner._snapshot({'kind':'local','runtime_root':'/synthetic'}, [], limits, time.monotonic()+3, tmp_path)
    assert caught.value.code == code and caught.value.source_exit_code == exit_code


def test_cleanup_unknown_does_not_erase_source_failure(tmp_path, monkeypatch):
    import time
    from src.application.research import archive_stream
    raw = '{"ok":false,"error_stage":"hash","error_code":"source_changed"}'
    command = [sys.executable, '-c', 'import sys;sys.stdout.write(sys.argv[1]);sys.exit(1)', raw]
    monkeypatch.setattr(owner, '_source_command', lambda *args: command)
    def failed_cleanup(*args): raise PermissionError('PRIVATE process metadata')
    monkeypatch.setattr(archive_stream, 'stop_group', failed_cleanup)
    limits = dict(max_bytes=1000, max_entries=10, max_file_bytes=1000, _evidence_remaining=4096, grace_seconds=.1)
    with pytest.raises(owner._ArchiveFailure) as caught:
        owner._snapshot({'kind':'local','runtime_root':'/synthetic'}, [], limits, time.monotonic()+3, tmp_path)
    assert owner._failure_details(caught.value,'unused') == {
        'error_stage':'hash','error_code':'source_changed','source_exit_code':1,'source_cleanup':'unknown'}


def test_snapshot_stage_resets_after_manifest(tmp_path):
    run = tmp_path / 'output_runs/r/state'
    run.mkdir(parents=True)
    manifest = run / 'required_data_snapshot_manifest.json'
    manifest.write_text('{"symbols":{}}')
    result = snapshot(tmp_path, ['output_runs/r/state/required_data_snapshot_manifest.json', 'output_runs/missing'])
    assert result.returncode != 0
    assert json.loads(result.stdout)['error_stage'] == 'selected_tree'
    assert json.loads(result.stdout)['error_code'] == 'selected_root_missing'


@pytest.mark.parametrize('has_primary', [True, False])
def test_worker_never_claims_known_stop_when_cleanup_is_unknown(tmp_path, monkeypatch, has_primary):
    def fail(request):
        if has_primary:
            error = owner._ArchiveFailure('source_changed', stage='hash', source_exit_code=1)
            error.source_cleanup_unknown = True
        else:
            error = owner._ArchiveFailure('process_cleanup_unknown', stage='source_snapshot_cleanup')
        raise error
    monkeypatch.setattr(owner, '_execute', fail)
    request = {'target': str(tmp_path / 'batch'), 'staging': str(tmp_path / 'partial'),
               'result': str(tmp_path / 'receipt')}
    owner._worker(request)
    result = json.loads(Path(request['result']).read_bytes())
    assert result['state'] == 'unknown' and result['ok'] is False
    if has_primary:
        assert result['error_code'] == 'source_changed' and result['source_exit_code'] == 1
        assert result['source_cleanup'] == 'unknown'
    else: assert result['error_code'] == 'process_cleanup_unknown'
