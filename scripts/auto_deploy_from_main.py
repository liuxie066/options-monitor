#!/usr/bin/env python3
"""Auto deploy options-monitor main branch updates to options-monitor-prod.

Run this script from cron every 2 minutes.

Safety model:
- Single-instance lock (re-entrant friendly; stale lock cleanup by PID)
- Abort on dirty dev/prod worktrees
- Abort on non-main current branch (prevents surprising branch switches)
- Pull with --ff-only only when origin/main has new commits
- Deploy only after successful pull
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT_DEV = Path('/home/node/.openclaw/workspace/options-monitor')
ROOT_PROD = Path('/home/node/.openclaw/workspace/options-monitor-prod')
LOCK_PATH = ROOT_DEV / '.tmp_auto_deploy_from_main.lock'
DEPLOY_SCRIPT = ROOT_DEV / 'scripts' / 'deploy_to_prod.py'


def _pid_alive(pid: int) -> bool:
    return Path(f'/proc/{pid}').exists()


def _acquire_lock(lock_path: Path) -> int:
    if lock_path.exists():
        try:
            pid_txt = lock_path.read_text(encoding='utf-8').strip()
            pid = int(pid_txt) if pid_txt else -1
            if pid <= 0 or not _pid_alive(pid):
                lock_path.unlink(missing_ok=True)
        except Exception:
            try:
                lock_path.unlink(missing_ok=True)
            except Exception:
                pass

    fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    os.write(fd, str(os.getpid()).encode('utf-8'))
    return fd


def _release_lock(fd: int, lock_path: Path) -> None:
    try:
        os.close(fd)
    except Exception:
        pass
    try:
        lock_path.unlink(missing_ok=True)
    except Exception:
        pass


def run(cmd: list[str], cwd: Path, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=capture, text=True)


def git(cwd: Path, *args: str, capture: bool = True) -> subprocess.CompletedProcess:
    return run(['git', '-c', f'safe.directory={cwd}', *args], cwd=cwd, capture=capture)


def _must_clean_repo(path: Path, label: str) -> None:
    # Only block on tracked changes. Untracked/ignored runtime artifacts are expected.
    res = git(path, 'status', '--porcelain=v1', '--untracked-files=no')
    if res.returncode != 0:
        raise RuntimeError(f'[{label}] git status failed: {(res.stderr or res.stdout).strip()}')
    if (res.stdout or '').strip():
        raise RuntimeError(f'[{label}] repo has tracked uncommitted changes; abort')


def _must_on_main(path: Path) -> None:
    res = git(path, 'branch', '--show-current')
    if res.returncode != 0:
        raise RuntimeError(f'[DEV] cannot determine current branch: {(res.stderr or res.stdout).strip()}')
    branch = (res.stdout or '').strip()
    if branch != 'main':
        raise RuntimeError(f"[DEV] current branch is '{branch}', not 'main'; abort")


def _rev(path: Path, ref: str) -> str:
    res = git(path, 'rev-parse', ref)
    if res.returncode != 0:
        raise RuntimeError(f'[DEV] cannot resolve {ref}: {(res.stderr or res.stdout).strip()}')
    return (res.stdout or '').strip()


def _choose_python() -> str:
    vpy = ROOT_DEV / '.venv' / 'bin' / 'python'
    if vpy.exists():
        return str(vpy)
    return sys.executable


def main() -> int:
    lock_fd = None
    try:
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            lock_fd = _acquire_lock(LOCK_PATH)
        except FileExistsError:
            print('[skip] another auto-deploy run is in progress')
            return 0

        if not ROOT_DEV.is_dir():
            raise RuntimeError(f'[DEV] missing: {ROOT_DEV}')
        if not ROOT_PROD.is_dir():
            raise RuntimeError(f'[PROD] missing: {ROOT_PROD}')
        if not DEPLOY_SCRIPT.is_file():
            raise RuntimeError(f'[DEV] missing deploy script: {DEPLOY_SCRIPT}')

        _must_on_main(ROOT_DEV)
        _must_clean_repo(ROOT_DEV, 'DEV')
        # PROD worktree is expected to be dirty because deploy sync overwrites tracked files.
        # We rely on deploy_to_prod.py exclusions to protect runtime configs.

        fetched = git(ROOT_DEV, 'fetch', '--prune', 'origin', 'main', capture=False)
        if fetched.returncode != 0:
            raise RuntimeError('[DEV] git fetch failed')

        local_head = _rev(ROOT_DEV, 'main')
        remote_head = _rev(ROOT_DEV, 'origin/main')

        if local_head == remote_head:
            print(f'[skip] main is up to date: {local_head[:12]}')
            return 0

        print(f'[info] new main commit detected: {local_head[:12]} -> {remote_head[:12]}')

        pulled = git(ROOT_DEV, 'pull', '--ff-only', 'origin', 'main', capture=False)
        if pulled.returncode != 0:
            raise RuntimeError('[DEV] git pull --ff-only failed')

        after_pull = _rev(ROOT_DEV, 'HEAD')
        print(f'[info] pulled to {after_pull[:12]}')

        py = _choose_python()
        deploy = run([py, str(DEPLOY_SCRIPT), '--apply', '--prune'], cwd=ROOT_DEV, capture=False)
        if deploy.returncode != 0:
            raise RuntimeError('[DEPLOY] deploy_to_prod.py failed')

        print(f'[ok] deployed commit {after_pull[:12]} to prod')
        return 0
    except Exception as e:
        print(f'[error] {e}', file=sys.stderr)
        return 1
    finally:
        if lock_fd is not None:
            _release_lock(lock_fd, LOCK_PATH)


if __name__ == '__main__':
    raise SystemExit(main())
