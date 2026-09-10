from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src.infrastructure.pi_agent_process import derive_pi_local_session_id, pi_session_locks, run_pi_agent
from test_pi_agent_process import _start_payload

REPO = Path(__file__).resolve().parents[1]
SESSION = derive_pi_local_session_id('key:us', 'lock-test')


def test_offline_bridge_uses_public_resolver_and_excludes_provider_credentials(monkeypatch, tmp_path):
    import src.infrastructure.pi_agent_process as module

    monkeypatch.setenv("OM_PI_MODEL_API_KEY", "must-not-reach-offline-converter")
    monkeypatch.setenv("NODE_OPTIONS", "--import=unapproved-loader")
    calls = []

    def run(argv, **kwargs):
        if argv[1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout="v22.19.0\n")
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout='{"ok":true,"version":"0.85.1"}')

    monkeypatch.setattr(module.subprocess, "run", run)
    result = module.run_pi_migration_bridge("identity", tmp_path / "selected runtime", {})
    assert result["version"] == "0.85.1"
    argv, options = calls[0]
    assert "--experimental-import-meta-resolve" in argv
    assert argv[-3:] == ["identity", "--runtime", str(tmp_path / "selected runtime")]
    assert options["env"] == {"PATH": os.environ["PATH"]}
    monkeypatch.setattr(module.subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 0, stdout="v22.19.0\n" if argv[1:] == ["--version"] else '{"ok":false}'
    ))
    with pytest.raises(ValueError, match="bridge failed"):
        module.run_pi_migration_bridge("probe", tmp_path, {"database": tmp_path / "sessions.sqlite3"})
    assert list(tmp_path.iterdir()) == []


def test_independent_runs_contend_but_distinct_sessions_can_run(tmp_path):
    database = tmp_path / 'sessions.sqlite3'
    with pi_session_locks(database, SESSION):
        with pytest.raises(BlockingIOError):
            with pi_session_locks(database, SESSION):
                pytest.fail('same-process lock became reentrant')
        with pytest.raises(BlockingIOError):
            with pi_session_locks(database):
                pytest.fail('converter entered an active database')
        with pi_session_locks(database, derive_pi_local_session_id('key:hk', 'lock-test')):
            pass
    with pi_session_locks(database):
        with pytest.raises(BlockingIOError):
            with pi_session_locks(database, SESSION):
                pytest.fail('writer entered a conversion')
    assert not database.exists()


def test_aliases_share_lock_identity_and_final_symlinks_are_rejected(tmp_path):
    actual = tmp_path / 'actual'
    actual.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(actual, target_is_directory=True)
    database = actual / 'private' / 'sessions.sqlite3'
    with pi_session_locks(database, SESSION):
        with pytest.raises(BlockingIOError):
            with pi_session_locks(alias / 'private' / database.name, SESSION):
                pytest.fail('alias bypassed lock')
    target = actual / 'private' / 'target.sqlite3'
    target.touch()
    database.symlink_to(target)
    with pytest.raises(OSError):
        with pi_session_locks(database, SESSION):
            pytest.fail('database symlink was accepted')
    database.unlink()
    os.link(target, database)
    with pytest.raises(OSError, match='hard-link'):
        with pi_session_locks(database, SESSION):
            pytest.fail('hard-link alias could bypass database lock identity')


def test_closing_parent_copy_keeps_inherited_child_lock(tmp_path):
    database = tmp_path / 'sessions.sqlite3'
    with pi_session_locks(database, SESSION) as (_, descriptors):
        child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.buffer.read()'],
                                 stdin=subprocess.PIPE, pass_fds=descriptors)
    try:
        with pytest.raises(BlockingIOError):
            with pi_session_locks(database, SESSION):
                pytest.fail('parent close released the surviving child lock')
    finally:
        child.communicate(timeout=5)
    with pi_session_locks(database, SESSION):
        pass


@pytest.mark.parametrize("paused_command", ["export", "import"])
def test_conversion_parent_death_retains_actual_sdk_child_lock(tmp_path, paused_command):
    from src.application.bot.pi_migration import migrate_pi, read_pi_migration_receipt
    from tests.bot_pi_test_support import seed_actual_legacy_pi_store

    database = tmp_path / "sessions.sqlite3"
    fixture = seed_actual_legacy_pi_store(database)
    ready = tmp_path / "converter.pid"
    preload = tmp_path / "pause-sdk.mjs"
    preload.write_text(f'''
import {{ writeFileSync }} from "node:fs";
import {{ pathToFileURL }} from "node:url";
if (process.argv[2] === {json.dumps(paused_command)}) {{
  const runtime = process.argv[process.argv.indexOf("--runtime") + 1];
  const sdk = await import(import.meta.resolve("@earendil-works/pi-session-backend-sqlite-node", pathToFileURL(runtime + "/package.json").href));
  const prototype = (sdk.SqliteSessionRepository ?? sdk.SqliteSessionRepo).prototype;
  const method = process.argv[2] === "export" ? "open" : "create";
  const original = prototype[method];
  prototype[method] = async function (...args) {{
    const session = await original.apply(this, args);
    writeFileSync({json.dumps(str(ready))}, String(process.pid));
    process.kill(process.pid, "SIGSTOP");
    return session;
  }};
}}
''')
    script = f'''
from pathlib import Path
from src.infrastructure import pi_agent_process as process
from src.application.bot.pi_migration import migrate_pi
original = process._runtime_command
def instrument(*args, **kwargs):
    argv, entry = original(*args, **kwargs)
    argv[1:1] = ["--import", {str(preload)!r}]
    return argv, entry
process._runtime_command = instrument
migrate_pi(pi_db={str(database)!r}, source_runtime={str(fixture.source_runtime)!r},
           target_runtime={str(fixture.target_runtime)!r}, apply=True, writers_stopped=True)
'''
    child_pid = None
    log = tmp_path / "converter.log"
    with log.open("w") as output:
        parent = subprocess.Popen([sys.executable, "-c", script], cwd=REPO, stdout=output, stderr=output)
    try:
        deadline = time.monotonic() + 30
        while not ready.exists():
            assert parent.poll() is None, log.read_text()
            assert time.monotonic() < deadline, "actual SDK converter did not reach store open"
            time.sleep(0.02)
        child_pid = int(ready.read_text())
        receipt = read_pi_migration_receipt(database)
        assert receipt["phase"] == "prepared"
        backup = Path(receipt["backup"]["path"])
        preserved = {path: path.read_bytes() for path in (database, backup)}
        parent.kill()
        parent.wait(timeout=5)
        with pytest.raises(BlockingIOError):
            migrate_pi(pi_db=database, source_runtime=fixture.source_runtime,
                       target_runtime=fixture.target_runtime, apply=True, writers_stopped=True)
        os.kill(child_pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while True:
            try:
                with pi_session_locks(database):
                    break
            except BlockingIOError:
                assert time.monotonic() < deadline, "dead converter retained maintenance lock"
                time.sleep(0.02)
        assert all(path.read_bytes() == contents for path, contents in preserved.items())
        recovered = migrate_pi(pi_db=database, source_runtime=fixture.source_runtime,
                               target_runtime=fixture.target_runtime, apply=True, writers_stopped=True)
        assert recovered["receipt_phase"] == "published"
        assert backup.read_bytes() == preserved[backup]
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("death_order", ["parent_first", "child_first"])
def test_facade_kill_order_keeps_surviving_holder_excluded(tmp_path, death_order):
    database = tmp_path / 'sessions.sqlite3'
    ready = tmp_path / 'child.pid'
    runtime = tmp_path / 'paused.ts'
    runtime.write_text('import fs from "node:fs";\n'
                       f'fs.writeFileSync({json.dumps(str(ready))}, String(process.pid));\n'
                       'process.kill(process.pid, "SIGSTOP");\nsetInterval(() => {}, 1000);\n')
    script = (
        'import sys, os\n'
        f'sys.path.insert(0, {str(REPO / "tests")!r})\n'
        'from pathlib import Path\nfrom test_pi_agent_process import _start_payload\n'
        'from src.infrastructure.pi_agent_process import run_pi_agent\n'
        f'run_pi_agent(_start_payload(session_id={SESSION!r}), request_id="lock", run_id="lock", '
        f'timeout_seconds=60, runtime_entry=Path({str(runtime)!r}), '
        f'environ={{**os.environ, "OM_PI_SESSION_DB": {str(database)!r}}})\n'
    )
    parent = subprocess.Popen([sys.executable, '-c', script], cwd=REPO)
    child_pid = None
    try:
        import time
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert parent.poll() is None, 'facade exited before Node startup'
            assert time.monotonic() < deadline, 'Node startup timed out'
            time.sleep(0.02)
        child_pid = int(ready.read_text())
        if death_order == 'parent_first':
            parent.kill()
            parent.wait(timeout=3)
        else:
            os.kill(parent.pid, signal.SIGSTOP)
            os.kill(child_pid, signal.SIGKILL)
        result = run_pi_agent(_start_payload(session_id=SESSION), request_id='second', run_id='second',
                              timeout_seconds=60, runtime_entry=runtime,
                              environ={**os.environ, 'OM_PI_SESSION_DB': str(database)})
        assert result['error']['code'] == 'SESSION_ERROR'
        assert result['error']['retryable'] is True
        if death_order == 'parent_first':
            os.kill(child_pid, signal.SIGKILL)
        else:
            os.kill(parent.pid, signal.SIGCONT)
            parent.wait(timeout=5)
        # Orphan exit/reap is asynchronous; lock release is the observable fact.
        deadline = time.monotonic() + 5
        while True:
            try:
                with pi_session_locks(database, SESSION):
                    break
            except BlockingIOError:
                assert time.monotonic() < deadline, 'dead child retained the lock'
                time.sleep(0.02)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=3)
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    assert not database.exists()


@pytest.mark.parametrize('failure', ['spawn', 'pipe_setup'])
def test_failed_spawn_or_pipe_setup_releases_session_lock(tmp_path, monkeypatch, failure):
    from src.infrastructure import pi_agent_process as process_module
    import shutil

    database = tmp_path / 'sessions.sqlite3'
    runtime = tmp_path / 'wait.ts'
    runtime.write_text('setInterval(() => {}, 1000);\n')
    monkeypatch.setattr(process_module, '_runtime_command',
                        lambda *args, **kwargs: ([shutil.which('node'), str(runtime)], runtime))

    def fail(*args, **kwargs):
        raise OSError('injected process setup failure')

    if failure == 'spawn':
        monkeypatch.setattr(subprocess, 'Popen', fail)
    else:
        monkeypatch.setattr(os, 'set_blocking', fail)
    result = run_pi_agent(_start_payload(session_id=SESSION), request_id='setup', run_id='setup',
                          timeout_seconds=60, runtime_entry=runtime,
                          environ={**os.environ, 'OM_PI_SESSION_DB': str(database)})
    assert result['ok'] is False
    assert result['error']['code'] == ('PI_RUNTIME_UNAVAILABLE' if failure == 'spawn' else 'PI_PROCESS_EXITED')
    with pi_session_locks(database, SESSION):
        pass
