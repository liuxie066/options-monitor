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
