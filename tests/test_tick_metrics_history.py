from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from domain.storage.json_io import private_json_file_lock
from domain.storage.repositories import state_repo


def _path(base, scope="shared", run="run"):
    parent = base / "output_shared" if scope == "shared" else base / "output_runs" / run
    return parent / "state" / "tick_metrics_history.json"


def _seed(path, content=b'[{"seed": true}]'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _worker(base, run, ready):
    original = state_repo.write_json
    def write(path, payload):
        original(path, payload)
        if "output_runs" in path.parts:
            ready.set()
    state_repo.write_json = write
    try:
        state_repo.append_tick_metrics_history(Path(base), run, {"run": run})
    finally:
        state_repo.write_json = original


def test_missing_duplicate_and_private_modes(tmp_path):
    payload = {"run": "run", "sent": False}
    result = state_repo.append_tick_metrics_history(tmp_path, "run", payload)
    assert list(result) == ["shared", "run"]
    state_repo.append_tick_metrics_history(tmp_path, "run", payload)
    for scope, path in result.items():
        assert path == _path(tmp_path, scope)
        assert json.loads(path.read_text()) == [payload, payload]
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert Path(f"{path}.lock").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("processes", [False, True], ids=["threads", "spawned-processes"])
def test_overlapping_writers_wait_for_stable_lock_and_preserve_all_rows(tmp_path, processes):
    shared = _path(tmp_path)
    _seed(shared)
    context = multiprocessing.get_context("spawn")
    ready = [context.Event() if processes else threading.Event() for _ in range(2)]
    workers = []
    pool = None
    try:
        with private_json_file_lock(shared):
            if processes:
                workers = [context.Process(target=_worker, args=(str(tmp_path), run, event))
                           for run, event in zip(("us", "hk"), ready)]
                for worker in workers:
                    worker.start()
            else:
                pool = ThreadPoolExecutor(2)
                # Keep the real writer unchanged across both threads.
                original = state_repo.write_json
                def write(path, payload):
                    original(path, payload)
                    if "output_runs" in path.parts:
                        ready[("us", "hk").index(payload[-1]["run"])].set()
                state_repo.write_json = write
                workers = [pool.submit(state_repo.append_tick_metrics_history, tmp_path, run, {"run": run})
                           for run in ("us", "hk")]
            assert all(event.wait(15) for event in ready)
            assert json.loads(shared.read_text()) == [{"seed": True}]
        for worker in workers:
            if processes:
                worker.join(15)
                assert worker.exitcode == 0
            else:
                worker.result(timeout=15)
        rows = json.loads(shared.read_text())
        assert rows[0] == {"seed": True}
        assert sorted(row["run"] for row in rows[1:]) == ["hk", "us"]
        for run in ("us", "hk"):
            assert json.loads(_path(tmp_path, "run", run).read_text()) == [{"run": run}]
    finally:
        if pool is not None:
            pool.shutdown(wait=True)
            state_repo.write_json = original
        for worker in workers if processes else []:
            if worker.is_alive():
                worker.terminate()
                worker.join(5)


@pytest.mark.parametrize("scope", ["run", "shared"])
@pytest.mark.parametrize("content", [b"", b"\xff", b"{secret-broken", b"{}", b"null"])
def test_corruption_is_unchanged_and_other_destination_succeeds(tmp_path, scope, content):
    path = _path(tmp_path, scope)
    _seed(path, content)
    with pytest.raises(OSError, match=scope) as error:
        state_repo.append_tick_metrics_history(tmp_path, "run", {"new": True})
    assert "secret-broken" not in str(error.value)
    assert path.read_bytes() == content
    healthy = _path(tmp_path, "shared" if scope == "run" else "run")
    assert json.loads(healthy.read_text()) == [{"new": True}]


@pytest.mark.parametrize("kind", ["symlink", "dangling", "hardlink", "directory", "fifo", "lock-symlink", "lock-hardlink", "lock-fifo"])
def test_unsafe_history_or_lock_is_not_overwritten(tmp_path, kind):
    path = _path(tmp_path)
    path.parent.mkdir(parents=True)
    marker = tmp_path / "marker"
    marker.write_bytes(b"[]")
    unsafe = Path(f"{path}.lock") if kind.startswith("lock-") else path
    if "symlink" in kind or kind == "dangling":
        unsafe.symlink_to(marker if kind != "dangling" else tmp_path / "missing")
    elif "hardlink" in kind:
        os.link(marker, unsafe)
    elif "fifo" in kind:
        os.mkfifo(unsafe)
    else:
        unsafe.mkdir()
    before = unsafe.lstat()
    with pytest.raises(OSError, match="shared"):
        state_repo.append_tick_metrics_history(tmp_path, "run", {"new": True})
    assert unsafe.lstat().st_ino == before.st_ino
    assert marker.read_bytes() == b"[]"
    assert json.loads(_path(tmp_path, "run").read_text()) == [{"new": True}]


@pytest.mark.parametrize("scope", ["run", "shared"])
def test_directory_creation_failure_does_not_skip_other_scope(tmp_path, scope):
    parent = _path(tmp_path, scope).parent.parent
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text("blocked")
    with pytest.raises(OSError, match=scope):
        state_repo.append_tick_metrics_history(tmp_path, "run", {})
    assert json.loads(_path(tmp_path, "shared" if scope == "run" else "run").read_text()) == [{}]


@pytest.mark.parametrize("failure", ["read", "write", "serialize", "cancel"])
def test_failures_preserve_original_and_release_lock(tmp_path, monkeypatch, failure):
    shared = _path(tmp_path)
    _seed(shared)
    original = shared.read_bytes()
    _seed(_path(tmp_path, "run"))
    with monkeypatch.context() as patch:
        if failure == "read":
            patch.setattr(state_repo.json, "load", lambda *_: (_ for _ in ()).throw(PermissionError("secret")))
        elif failure == "write":
            patch.setattr(state_repo, "write_json", lambda *_: (_ for _ in ()).throw(OSError("secret")))
        elif failure == "cancel":
            patch.setattr(state_repo, "write_json", lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt if failure == "cancel" else OSError) as error:
            state_repo.append_tick_metrics_history(tmp_path, "run", {"bad": object()} if failure == "serialize" else {})
        assert "secret" not in str(error.value)
        if failure != "cancel":
            assert "run:" in str(error.value) and "shared:" in str(error.value)
    assert shared.read_bytes() == original
    with ThreadPoolExecutor(1) as pool:
        pool.submit(state_repo.append_tick_metrics_history, tmp_path, "run", {"retry": True}).result(timeout=5)
    assert json.loads(shared.read_text()) == [{"seed": True}, {"retry": True}]


@pytest.mark.parametrize("scope", ["run", "shared"])
def test_atomic_replace_failure_preserves_history_and_other_scope(tmp_path, monkeypatch, scope):
    from domain.storage import json_io
    path = _path(tmp_path, scope)
    _seed(path)
    original = path.read_bytes()
    replace = json_io.os.replace
    def fail_one(source, target):
        if Path(target) == path:
            raise OSError("replace unavailable")
        return replace(source, target)
    with monkeypatch.context() as patch:
        patch.setattr(json_io.os, "replace", fail_one)
        with pytest.raises(OSError, match=scope):
            state_repo.append_tick_metrics_history(tmp_path, "run", {"new": True})
    assert path.read_bytes() == original
    assert not list(path.parent.glob(".*.tmp"))
    assert json.loads(_path(tmp_path, "shared" if scope == "run" else "run").read_text()) == [{"new": True}]
    state_repo.append_tick_metrics_history(tmp_path, "run", {"retry": True})
    assert json.loads(path.read_text()) == [{"seed": True}, {"retry": True}]
