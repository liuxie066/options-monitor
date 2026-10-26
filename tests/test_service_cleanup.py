from __future__ import annotations

import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.application import service_cleanup as cleanup


def test_path_size_counts_links_without_following_them(tmp_path: Path) -> None:
    root = tmp_path / 'tree'
    nested = root / 'nested'
    nested.mkdir(parents=True)
    payload = nested / '数据.bin'
    payload.write_bytes(b'payload')
    os.link(payload, root / 'hardlink')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'large.bin').write_bytes(b'x' * 4096)
    links = [root / 'directory-link', root / 'file-link', root / 'broken-link', nested / 'cycle']
    for link, target in zip(links, [outside, payload, tmp_path / 'missing', root]):
        link.symlink_to(target)
    expected = 2 * len(b'payload') + sum(link.lstat().st_size for link in links)

    assert cleanup._path_size(root) == expected
    assert cleanup._path_size(payload) == len(b'payload')
    assert cleanup._path_size(root / 'broken-link') == links[2].lstat().st_size
    assert cleanup._path_size(root / 'directory-link') == links[0].lstat().st_size
    assert cleanup._path_size(tmp_path / 'missing') == 0
    assert (outside / 'large.bin').read_bytes() == b'x' * 4096


def test_path_size_skips_unreadable_subtree(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / 'tree'
    blocked = root / 'blocked'
    blocked.mkdir(parents=True)
    (blocked / 'hidden').write_bytes(b'hidden')
    (root / 'visible').write_bytes(b'visible')
    original = os.scandir

    def scandir(path):
        if Path(path) == blocked:
            raise PermissionError('denied')
        return original(path)

    monkeypatch.setattr(cleanup, 'os', SimpleNamespace(scandir=scandir, path=os.path, lstat=os.lstat))
    assert cleanup._path_size(root) == len(b'visible')


def test_path_size_skips_file_that_disappears_during_scan(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / 'remaining').write_bytes(b'visible')
    (tmp_path / 'vanishing').write_bytes(b'deleted')
    original = os.scandir

    class Scan:
        def __enter__(self):
            with original(tmp_path) as entries:
                entries = list(entries)
            (tmp_path / 'vanishing').unlink()
            return iter(entries)

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(cleanup, 'os', SimpleNamespace(scandir=lambda path: Scan(), path=os.path, lstat=os.lstat))
    assert cleanup._path_size(tmp_path) == len(b'visible')


def test_path_size_does_not_follow_replaced_queued_directory(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / 'tree'
    queued = root / 'queued'
    queued.mkdir(parents=True)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'payload').write_bytes(b'x' * 4096)
    original = os.scandir

    class Scan:
        def __enter__(self):
            self.entries = original(root)
            return self.entries

        def __exit__(self, *args):
            self.entries.close()
            queued.rmdir()
            queued.symlink_to(outside)

    monkeypatch.setattr(cleanup, 'os', SimpleNamespace(
        scandir=lambda path: Scan() if Path(path) == root else original(path), path=os.path, lstat=os.lstat,
    ))
    assert cleanup._path_size(root) == queued.lstat().st_size


def test_path_size_uses_directory_entries_for_file_metadata(tmp_path: Path, monkeypatch) -> None:
    for index in range(20):
        (tmp_path / str(index)).write_bytes(b'abc')
    original = Path.lstat
    calls = []

    def lstat(path, *args, **kwargs):
        calls.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'lstat', lstat)
    assert cleanup._path_size(tmp_path) == 60
    assert calls == [tmp_path]


def test_release_versions_read_once_per_inventory_and_refreshed_next_time(tmp_path: Path, monkeypatch) -> None:
    versions = {'a': '1.2.0', 'b': '1.10.0', 'c': '1.1.0', 'd': '1.0.0'}
    for name, version in versions.items():
        release = tmp_path / name
        release.mkdir()
        (release / 'VERSION').write_text(version)
    (tmp_path / 'without-version').mkdir()
    (tmp_path / 'linked').symlink_to(tmp_path / 'a')
    original = cleanup._release_version
    calls = Counter()

    def version(path):
        calls[path.name] += 1
        return original(path)

    monkeypatch.setattr(cleanup, '_release_version', version)
    assert [p.name for p in cleanup._release_dirs(tmp_path)] == ['b', 'a', 'c', 'd']
    assert calls == Counter({name: 1 for name in versions})
    (tmp_path / 'd' / 'VERSION').write_text('2.0.0')
    assert [p.name for p in cleanup._release_dirs(tmp_path)] == ['d', 'b', 'a', 'c']
    assert calls == Counter({name: 2 for name in versions})


def test_release_order_retains_name_fallback_for_invalid_version(tmp_path: Path) -> None:
    for name in ('z', 'a', 'm'):
        release = tmp_path / name
        release.mkdir()
        (release / 'VERSION').write_text('not-a-version')
    assert [p.name for p in cleanup._release_dirs(tmp_path)] == ['z', 'm', 'a']


@pytest.mark.parametrize(('confirm', 'change_after_preview'), [(False, False), (True, False), (True, True)])
def test_cleanup_preserves_plan_bytes_and_safe_targets(
    tmp_path: Path, confirm: bool, change_after_preview: bool,
) -> None:
    releases = tmp_path / 'releases'
    for version in ('1.0.0', '1.1.0', '1.2.0'):
        release = releases / version
        release.mkdir(parents=True)
        (release / 'VERSION').write_text(version)
        (release / 'payload').write_bytes(b'x' * 20)
    current = tmp_path / 'current'
    current.symlink_to(releases / '1.2.0')
    runtime = tmp_path / 'runtime'
    runs = runtime / 'output_runs'
    for name, timestamp in [('old', 1_600_000_000), ('latest', 1_700_000_000)]:
        run = runs / name
        run.mkdir(parents=True)
        (run / 'payload').write_bytes(b'x' * 30)
        os.utime(run, (timestamp, timestamp))
    logs = runtime / 'logs'
    logs.mkdir()
    (logs / 'old.log').write_bytes(b'x' * 40)
    os.utime(logs / 'old.log', (1_600_000_000, 1_600_000_000))
    (logs / 'audit.jsonl').write_bytes(b'protected')
    args = dict(repo_root=current, releases_root=releases, runtime_root=runtime,
                cleanup_output_runs=True, output_runs_keep_count=1,
                cleanup_runtime_logs=True, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    preview = cleanup.service_cleanup(**args)
    if change_after_preview:
        (runs / 'old' / 'payload').write_bytes(b'changed after preview')
    out = cleanup.service_cleanup(**args, confirm=confirm,
                                  expected_output_runs_plan_sha256=preview['output_runs_plan_sha256'])

    if change_after_preview:
        assert out['ok'] is False
        assert out['status'] == 'output_runs_plan_changed'
        assert out['changed'] is False
        assert out['operations'] == []
        assert out['deleted_paths'] == []
        assert (releases / '1.0.0').is_dir()
        assert (runs / 'old' / 'payload').read_bytes() == b'changed after preview'
        assert (logs / 'old.log').is_file()
        return

    assert out['ok'] is True
    assert out['estimated_freed_bytes'] == 25 + 30 + 40
    assert out['freed_bytes'] == (95 if confirm else 0)
    assert out['output_runs_plan_sha256'] == preview['output_runs_plan_sha256']
    assert (releases / '1.0.0').exists() is not confirm
    assert (runs / 'old').exists() is not confirm
    assert (logs / 'old.log').exists() is not confirm
    assert (releases / '1.1.0').is_dir()
    assert current.resolve() == releases / '1.2.0'
    assert (runs / 'latest').is_dir()
    assert (logs / 'audit.jsonl').read_bytes() == b'protected'


@pytest.mark.parametrize('confirm', [False, True])
def test_cleanup_single_release_does_not_require_decodable_version(tmp_path: Path, confirm: bool) -> None:
    releases = tmp_path / 'releases'
    active = releases / '1.0.0'
    active.mkdir(parents=True)
    version = active / 'VERSION'
    version.write_bytes(b'\xff')
    current = tmp_path / 'current'
    current.symlink_to(active)

    result = cleanup.service_cleanup(
        repo_root=current, releases_root=releases, runtime_root=tmp_path / 'runtime', confirm=confirm,
    )

    assert result['ok'] is True
    assert result['status'] == ('cleaned' if confirm else 'dry_run')
    assert result['changed'] is False
    assert result['delete_releases'] == []
    assert result['kept_releases'] == [{'path': str(active), 'version': '1.0.0'}]
    assert result['operations'] == []
    assert current.resolve() == active
    assert version.read_bytes() == b'\xff'


def _transition_layout(root):
    releases = root / 'releases'
    for version in ('1.0.0', '1.1.0', '1.2.0', '1.3.0'):
        release = releases / version
        release.mkdir(parents=True)
        (release / 'VERSION').write_text(version)
    current = root / 'current'
    current.symlink_to(releases / '1.3.0')
    return current, releases, root / 'runtime'


def _hold_transition(current, releases, runtime, connection):
    from src.application.service_upgrade import _UpgradeLock

    try:
        with _UpgradeLock(Path(runtime) / 'locks/upgrade.lock'):
            link = Path(current)
            link.unlink()
            link.symlink_to(Path(releases) / '1.0.0')
            connection.send('switched')
            assert connection.poll(20)
            assert connection.recv() == 'release'
    finally:
        connection.close()


def _pause_cleanup(current, releases, runtime, connection):
    original = cleanup._release_dirs

    def inventory(path):
        connection.send('inventory')
        assert connection.poll(20)
        assert connection.recv() == 'release'
        return original(path)

    cleanup._release_dirs = inventory
    try:
        result = cleanup.service_cleanup(
            repo_root=current, releases_root=releases, runtime_root=runtime, confirm=True,
        )
        connection.send(result)
    finally:
        cleanup._release_dirs = original
        connection.close()


def _stop_fixture_process(process, connection):
    if process.is_alive():
        process.terminate()
        process.join(5)
    if process.is_alive():
        process.kill()
        process.join(5)
    connection.close()
    assert not process.is_alive()
    process.close()


def test_confirmed_cleanup_contends_with_real_transition_and_rereads_current(tmp_path):
    import multiprocessing

    current, releases, runtime = _transition_layout(tmp_path)
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe()
    process = context.Process(target=_hold_transition, args=(current, releases, runtime, child))
    process.start()
    child.close()
    try:
        assert parent.poll(20) and parent.recv() == 'switched'
        receipt = runtime / 'upgrade_status.json'
        receipt.write_bytes(b'{"status":"holder"}')
        pid = runtime / 'locks/upgrade.lock'
        pid_before = pid.read_bytes()
        guard = runtime / 'locks/upgrade.lock.guard'
        inode = guard.stat().st_ino
        result = cleanup.service_cleanup(
            repo_root=current, releases_root=releases, runtime_root=runtime, confirm=True,
        )
        assert result['ok'] is False and result['status'] == 'upgrade_locked'
        assert result['schema_version'] == 1 and result['confirmed'] is True
        assert result['changed'] is False and result['deleted_paths'] == result['operations'] == []
        assert len(list(releases.iterdir())) == 4
        assert current.resolve() == releases / '1.0.0'
        assert receipt.read_bytes() == b'{"status":"holder"}' and pid.read_bytes() == pid_before
        preview = cleanup.service_cleanup(repo_root=current, releases_root=releases, runtime_root=runtime)
        assert preview['ok'] and preview['status'] == 'dry_run'
        assert len(list(releases.iterdir())) == 4
        assert guard.stat().st_ino == inode
        parent.send('release')
        process.join(10)
        assert process.exitcode == 0
        retry = cleanup.service_cleanup(
            repo_root=current, releases_root=releases, runtime_root=runtime, confirm=True,
        )
        assert retry['ok'] and retry['status'] == 'cleaned'
        assert current.resolve().is_dir() and (releases / '1.0.0').is_dir()
        assert {p.name for p in releases.iterdir()} == {'1.0.0', '1.3.0'}
        assert receipt.read_bytes() == b'{"status":"holder"}'
        assert guard.stat().st_ino == inode and not pid.exists()
    finally:
        _stop_fixture_process(process, parent)


def test_cleanup_holds_transition_guard_before_inventory_through_deletion(tmp_path):
    import multiprocessing

    from src.application.service_upgrade import _UpgradeLock

    current, releases, runtime = _transition_layout(tmp_path)
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe()
    process = context.Process(target=_pause_cleanup, args=(current, releases, runtime, child))
    process.start()
    child.close()
    try:
        assert parent.poll(20) and parent.recv() == 'inventory'
        with pytest.raises(RuntimeError, match='upgrade lock already exists'):
            with _UpgradeLock(runtime / 'locks/upgrade.lock'):
                pytest.fail('transition must not enter while cleanup observes/deletes')
        assert len(list(releases.iterdir())) == 4
        parent.send('release')
        assert parent.poll(20)
        assert parent.recv()['ok']
        process.join(10)
        assert process.exitcode == 0
        assert current.resolve() == releases / '1.3.0' and current.resolve().is_dir()
        assert len(list(releases.iterdir())) == 2
        with _UpgradeLock(runtime / 'locks/upgrade.lock'):
            pass
    finally:
        _stop_fixture_process(process, parent)


def test_cleanup_preview_and_invalid_root_do_not_acquire_guard(tmp_path, monkeypatch):
    current, releases, runtime = _transition_layout(tmp_path)
    assert cleanup.service_cleanup(repo_root=current, releases_root=releases, runtime_root=runtime)['ok']
    assert not runtime.exists()
    def forbidden(*args, **kwargs):
        pytest.fail('invalid repo root must be rejected before guard acquisition')
    monkeypatch.setattr(cleanup, 'single_instance_lock', forbidden)
    result = cleanup.service_cleanup(repo_root=releases, runtime_root=runtime, confirm=True)
    assert result['status'] == 'repo_root_not_symlink' and not runtime.exists()


def test_cleanup_lock_io_failure_is_zero_effect(tmp_path, monkeypatch):
    from contextlib import contextmanager

    current, releases, runtime = _transition_layout(tmp_path)
    @contextmanager
    def denied(*args, **kwargs):
        raise PermissionError('fixture denied')
        yield
    monkeypatch.setattr(cleanup, 'single_instance_lock', denied)
    result = cleanup.service_cleanup(
        repo_root=current, releases_root=releases, runtime_root=runtime, confirm=True,
    )
    assert result['status'] == 'cleanup_failed' and result['ok'] is False
    assert result['changed'] is False and result['deleted_paths'] == result['operations'] == []
    assert len(list(releases.iterdir())) == 4 and not runtime.exists()


@pytest.mark.parametrize('error', [OSError('inventory failed'), KeyboardInterrupt('cancelled')])
def test_cleanup_body_error_propagates_and_releases_guard(tmp_path, monkeypatch, error):
    from src.application.service_upgrade import _UpgradeLock

    current, releases, runtime = _transition_layout(tmp_path)
    inventory = cleanup._release_dirs
    def fail(path):
        raise error
    monkeypatch.setattr(cleanup, '_release_dirs', fail)
    with pytest.raises(type(error)) as raised:
        cleanup.service_cleanup(repo_root=current, releases_root=releases, runtime_root=runtime, confirm=True)
    assert raised.value is error
    assert len(list(releases.iterdir())) == 4
    with _UpgradeLock(runtime / 'locks/upgrade.lock'):
        pass
    monkeypatch.setattr(cleanup, '_release_dirs', inventory)
    assert cleanup.service_cleanup(
        repo_root=current, releases_root=releases, runtime_root=runtime, confirm=True,
    )['ok']


def test_cleanup_default_guard_identity_preserves_omitted_runtime_output_root(tmp_path, monkeypatch):
    from contextlib import contextmanager

    current, releases, runtime = _transition_layout(tmp_path)
    captured = []
    from src.application.file_locks import single_instance_lock
    @contextmanager
    def isolated(path, **kwargs):
        captured.append(path)
        with single_instance_lock(runtime / 'locks/upgrade.lock.guard', **kwargs):
            yield
    monkeypatch.setattr(cleanup, 'single_instance_lock', isolated)
    result = cleanup.service_cleanup(repo_root=current, releases_root=releases, confirm=True, cleanup_output_runs=True)
    assert result['ok'] and result['runtime_root'] is None
    assert captured == [Path('/var/lib/options-monitor/locks/upgrade.lock.guard')]
    assert result['output_runs_cleanup']['root'] == str(tmp_path / 'output_runs')
