"""Subprocess helpers.

Stage 3 refactor target: keep run_pipeline orchestration-only.

Policy:
- In scheduled/cron mode: capture stdout/stderr to reduce log I/O; only print tail on failure.
- In interactive mode: inherit stdout/stderr for immediate feedback.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def run_cmd(cmd: list[str], *, cwd: Path, timeout_sec: int | None = None, is_scheduled: bool = False) -> None:
    if not is_scheduled:
        print(f"[RUN] {' '.join(cmd)}" + (f" (timeout={timeout_sec}s)" if timeout_sec else ""))

    try:
        if is_scheduled:
            result = subprocess.run(cmd, cwd=str(cwd), timeout=timeout_sec, capture_output=True, text=True)
        else:
            result = subprocess.run(cmd, cwd=str(cwd), timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"timeout after {timeout_sec}s: {' '.join(cmd)}")

    if result.returncode != 0:
        if is_scheduled:
            out = ((result.stdout or '') + '\n' + (result.stderr or '')).strip()
            if out:
                tail = '\n'.join(out.splitlines()[-60:])
                print(f"[ERR] {' '.join(cmd)}\n{tail}")
        raise SystemExit(result.returncode)
