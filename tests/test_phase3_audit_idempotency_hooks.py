from __future__ import annotations

from pathlib import Path
from datetime import datetime, timedelta, timezone
import json

import pytest

BASE = Path(__file__).resolve().parents[1]


def test_state_repo_normalize_audit_event_requires_minimal_fields() -> None:
    from domain.storage.repositories.state_repo import normalize_audit_event

    out = normalize_audit_event(
        {
            "event_type": "tool_call",
            "action": "scan_scheduler",
            "status": "ok",
            "run_id": "r-1",
            "idempotency_key": "k-1",
            "extra": {"returncode": 0},
        }
    )
    assert out["schema_kind"] == "audit_event"
    assert out["schema_version"] == "1.0"
    assert out["event_type"] == "tool_call"
    assert out["action"] == "scan_scheduler"


def test_multi_tick_main_has_phase3_idempotency_and_audit_hooks() -> None:
    src = (BASE / "src" / "application" / "multi_account_tick.py").read_text(encoding="utf-8")
    assert "scope='tick_execution'" in src
    assert "append_audit_event" in src
    assert "execution_idempotency_key" in src
    assert "claim_idempotency_record" in src
    assert "status': 'started'" not in src


def test_state_repo_has_current_read_model_writers() -> None:
    src = (BASE / "domain" / "storage" / "repositories" / "state_repo.py").read_text(encoding="utf-8")
    assert "shared_current_read_model_dir" in src
    assert "write_shared_current_read_model" in src
    assert "tick_metrics.current.json" in src


def _claim(tmp_path: Path, run_id: str, *, stale_after_sec=60, **payload_extra):
    from domain.storage.repositories import state_repo

    return state_repo.claim_idempotency_record(
        tmp_path,
        scope="tick_execution",
        key="tick-key",
        payload={"run_id": run_id, **payload_extra},
        stale_after_sec=stale_after_sec,
    )


def test_tick_idempotency_claim_blocks_active_and_reclaims_stale(tmp_path: Path) -> None:
    from domain.storage.repositories import state_repo

    first = _claim(tmp_path, "run-1")
    assert first["claimed"] is True
    assert first["record"]["ok"] is False
    assert first["record"]["status"] == "in_progress"

    active_duplicate = _claim(tmp_path, "run-2")
    assert active_duplicate["claimed"] is False
    assert active_duplicate["stale"] is False
    assert active_duplicate["record"]["run_id"] == "run-1"

    path = first["path"]
    stale_record = dict(first["record"])
    stale_record["updated_at_utc"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    path.write_text(json.dumps(stale_record), encoding="utf-8")

    reclaimed = _claim(tmp_path, "run-3")
    assert reclaimed["claimed"] is True
    assert reclaimed["stale"] is True
    assert reclaimed["record"]["run_id"] == "run-3"

    state_repo.write_idempotency_record(
        tmp_path,
        scope="tick_execution",
        key="tick-key",
        payload={"ok": True, "status": "completed", "run_id": "run-3"},
    )
    completed_duplicate = _claim(tmp_path, "run-4", stale_after_sec=1)
    assert completed_duplicate["claimed"] is False
    assert completed_duplicate["record"]["status"] == "completed"


def test_tick_idempotency_claim_reclaims_dead_owner_pid(tmp_path: Path) -> None:
    first = _claim(tmp_path, "run-1", stale_after_sec=3600, pid=99999999)
    assert first["claimed"] is True

    reclaimed = _claim(tmp_path, "run-2", stale_after_sec=3600)

    assert reclaimed["claimed"] is True
    assert reclaimed["stale"] is True
    assert reclaimed["record"]["run_id"] == "run-2"


def _competing_tick_claim(base, run_id, start, release, connection):
    import os
    from domain.storage.repositories import state_repo

    try:
        connection.send('ready')
        assert start.wait(20)
        result = state_repo.claim_idempotency_record(
            Path(base), scope='tick_execution', key='tick-key',
            payload={'run_id': run_id, 'pid': os.getpid()}, stale_after_sec=3600,
        )
        connection.send(result)
        assert release.wait(20)  # Keep the winning PID alive until both results are observed.
    finally:
        connection.close()




@pytest.mark.parametrize('stale', [False, True], ids=['initial-claim', 'dead-owner-reclaim'])
def test_tick_claims_serialize_on_same_sibling_lock_across_processes(tmp_path, stale):
    import multiprocessing
    from domain.storage.json_io import private_json_file_lock
    from domain.storage.repositories import state_repo

    path = state_repo._idempotency_path(tmp_path, scope='tick_execution', key='tick-key')
    if stale:
        _claim(tmp_path, 'dead', pid=99999999)
    context = multiprocessing.get_context('spawn')
    start, release = context.Event(), context.Event()
    children = []
    try:
        with private_json_file_lock(path):
            for index in range(2):
                parent, child = context.Pipe()
                process = context.Process(target=_competing_tick_claim,
                                          args=(tmp_path, f'worker-{index}', start, release, child))
                process.start()
                child.close()
                children.append((process, parent))
            for _, connection in children:
                assert connection.poll(20) and connection.recv() == 'ready'
            start.set()
            # Admission cannot proceed while another cooperating owner holds this inode.
            assert not any(connection.poll(0.2) for _, connection in children)
        results = []
        for _, connection in children:
            assert connection.poll(20)
            results.append(connection.recv())
        assert sum(result['claimed'] for result in results) == 1
        winner = next(result for result in results if result['claimed'])
        loser = next(result for result in results if not result['claimed'])
        assert winner['created'] is not stale and winner['stale'] is stale
        assert loser['created'] is False and loser['stale'] is False
        assert loser['record'] == winner['record']
        assert state_repo.read_idempotency_record(tmp_path, scope='tick_execution', key='tick-key') == winner['record']
        assert winner['record']['ok'] is False and winner['record']['status'] == 'in_progress'
        assert path.stat().st_mode & 0o777 == 0o600
        release.set()
        for process, _ in children:
            process.join(10)
            assert process.exitcode == 0
    finally:
        release.set()
        for process, connection in children:
            if process.is_alive():
                process.terminate()
                process.join(5)
            if process.is_alive():
                process.kill()
                process.join(5)
            connection.close()
            assert not process.is_alive()
            process.close()


def test_old_tick_terminal_cannot_overwrite_successor_claim(tmp_path):
    from domain.storage.repositories import state_repo

    _claim(tmp_path, 'old', pid=99999999)
    successor = _claim(tmp_path, 'successor')
    assert successor['claimed'] and successor['stale']
    before = successor['path'].read_bytes()
    with pytest.raises(RuntimeError, match='ownership'):
        state_repo.write_idempotency_record(tmp_path, scope='tick_execution', key='tick-key',
                                           payload={'run_id': 'old', 'ok': True, 'status': 'completed'})
    assert successor['path'].read_bytes() == before


@pytest.mark.parametrize('status,ok', [('completed', True), ('failed', False), ('skipped', True)])
def test_matching_tick_owner_keeps_terminal_and_path_contract(tmp_path, status, ok):
    from domain.storage.repositories import state_repo

    claimed = _claim(tmp_path, 'owner')
    path = state_repo.write_idempotency_record(tmp_path, scope='tick_execution', key='tick-key',
                                             payload={'run_id': 'owner', 'status': status, 'ok': ok})
    assert isinstance(path, Path) and path == claimed['path']
    record = json.loads(path.read_text())
    assert record['run_id'] == 'owner' and record['status'] == status and record['ok'] is ok
    assert record['idempotency_key'] == 'tick-key' and record['scope'] == 'tick_execution'
    assert record['updated_at_utc']


@pytest.mark.parametrize('current', [None, '{broken', '[]', '{}', '{"run_id":""}', '{"run_id":"successor"}'])
@pytest.mark.parametrize('incoming', [None, '', '   ', 'owner'])
def test_tick_terminal_requires_provable_nonempty_matching_owner(tmp_path, current, incoming):
    from domain.storage.repositories import state_repo

    path = state_repo._idempotency_path(tmp_path, scope='tick_execution', key='tick-key')
    if current is not None:
        path.write_text(current)
    before = path.read_bytes() if path.exists() else None
    with pytest.raises(RuntimeError, match='ownership') as raised:
        state_repo.write_idempotency_record(tmp_path, scope='tick_execution', key='tick-key',
                                           payload={'run_id': incoming, 'ok': True})
    assert len(str(raised.value)) < 150 and (current is None or current not in str(raised.value))
    assert (path.read_bytes() if path.exists() else None) == before


def test_claim_recovers_corrupt_json_but_does_not_hide_read_io_errors(tmp_path, monkeypatch):
    from domain.storage.repositories import state_repo

    path = state_repo._idempotency_path(tmp_path, scope='tick_execution', key='tick-key')
    path.write_text('{broken')
    assert _claim(tmp_path, 'owner')['stale'] is True
    before = path.read_bytes()
    read = Path.read_text
    def denied(target, *args, **kwargs):
        if target == path:
            raise PermissionError('fixture private contents must not be copied')
        return read(target, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'read_text', denied)
        with pytest.raises(PermissionError):
            _claim(tmp_path, 'retry')
        with pytest.raises(RuntimeError, match='ownership') as raised:
            state_repo.write_idempotency_record(tmp_path, scope='tick_execution', key='tick-key',
                                               payload={'run_id': 'owner'})
        assert 'private contents' not in str(raised.value)
    assert path.read_bytes() == before
    assert _claim(tmp_path, 'retry')['claimed'] is False


@pytest.mark.parametrize('error', [OSError('disk failure'), KeyboardInterrupt('cancelled')])
@pytest.mark.parametrize('existing', [False, True])
def test_claim_write_error_or_cancellation_releases_lock_for_retry(tmp_path, monkeypatch, error, existing):
    from domain.storage.repositories import state_repo

    path = state_repo._idempotency_path(tmp_path, scope='tick_execution', key='tick-key')
    if existing:
        _claim(tmp_path, 'dead', pid=99999999)
    before = path.read_bytes() if path.exists() else None
    def failed(*args, **kwargs):
        raise error
    with monkeypatch.context() as patch:
        patch.setattr(state_repo, 'write_json', failed)
        with pytest.raises(type(error)) as raised:
            _claim(tmp_path, 'owner')
        assert raised.value is error
    assert (path.read_bytes() if path.exists() else None) == before
    assert _claim(tmp_path, 'retry')['claimed'] is True


def test_tick_keys_are_independent_and_generic_tool_writer_is_unchanged(tmp_path):
    from domain.storage.json_io import private_json_file_lock
    from domain.storage.repositories import state_repo

    path = state_repo._idempotency_path(tmp_path, scope='tick_execution', key='held')
    with private_json_file_lock(path):
        claim = state_repo.claim_idempotency_record(tmp_path, scope='tick_execution', key='independent',
                                                   payload={'run_id': 'owner'})
        assert claim['created'] and claim['claimed']
    tool = state_repo.write_idempotency_record(tmp_path, scope='tool_execution', key='tool', payload={'ok': False})
    assert isinstance(tool, Path)
    assert json.loads(tool.read_text())['ok'] is False
    result = state_repo.put_idempotency_success(tmp_path, scope='tool_execution', key='success')
    assert result['created'] and result['record']['ok']
    repeated = state_repo.put_idempotency_success(tmp_path, scope='tool_execution', key='success')
    assert not repeated['created'] and repeated['record'] == result['record']


@pytest.mark.parametrize('error', [OSError('terminal write failed'), KeyboardInterrupt('cancelled')])
def test_terminal_write_error_releases_same_owner_lock_for_retry(tmp_path, monkeypatch, error):
    from domain.storage.repositories import state_repo

    claimed = _claim(tmp_path, 'owner')
    before = claimed['path'].read_bytes()
    def fail(*args, **kwargs):
        raise error
    with monkeypatch.context() as patch:
        patch.setattr(state_repo, 'write_json', fail)
        with pytest.raises(type(error)) as raised:
            state_repo.write_idempotency_record(tmp_path, scope='tick_execution', key='tick-key',
                                               payload={'run_id': 'owner', 'ok': True})
        assert raised.value is error
    assert claimed['path'].read_bytes() == before
    assert state_repo.write_idempotency_record(tmp_path, scope='tick_execution', key='tick-key',
                                              payload={'run_id': 'owner', 'ok': True}) == claimed['path']


def _finish_tick_in_child(base, start, connection):
    from domain.storage.repositories import state_repo

    try:
        connection.send('ready')
        assert start.wait(20)
        try:
            state_repo.write_idempotency_record(Path(base), scope='tick_execution', key='tick-key',
                                               payload={'run_id': 'old', 'ok': True, 'status': 'completed'})
        except RuntimeError as exc:
            connection.send(str(exc))
        else:
            connection.send('overwritten')
    finally:
        connection.close()


def test_terminal_rereads_owner_after_waiting_for_claim_lock(tmp_path):
    import multiprocessing
    from domain.storage.json_io import atomic_write_private_json, private_json_file_lock

    path = _claim(tmp_path, 'old')['path']
    context = multiprocessing.get_context('spawn')
    start = context.Event()
    parent, child = context.Pipe()
    process = context.Process(target=_finish_tick_in_child, args=(tmp_path, start, child))
    try:
        with private_json_file_lock(path):
            process.start()
            child.close()
            assert parent.poll(20) and parent.recv() == 'ready'
            start.set()
            assert not parent.poll(0.2)
            # A cooperating claim replacement finishes before this waiter can inspect ownership.
            atomic_write_private_json(path, {'run_id': 'successor', 'ok': False, 'status': 'in_progress'})
            before = path.read_bytes()
        assert parent.poll(20) and 'ownership' in parent.recv()
        process.join(10)
        assert process.exitcode == 0
        assert path.read_bytes() == before
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)
        parent.close()
        child.close()
        assert not process.is_alive()
        process.close()
