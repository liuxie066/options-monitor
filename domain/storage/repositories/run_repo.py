from __future__ import annotations

import shutil
from pathlib import Path

from domain.storage import paths


def get_run_dir(base: Path, run_id: str) -> Path:
    return paths.run_dir(base, run_id)


def ensure_run_dir(base: Path, run_id: str) -> Path:
    p = get_run_dir(base, run_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_run_state_dir(base: Path, run_id: str) -> Path:
    return paths.run_state_dir(base, run_id)


def ensure_run_state_dir(base: Path, run_id: str) -> Path:
    p = get_run_state_dir(base, run_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_run_account_dir(base: Path, run_id: str, account: str) -> Path:
    return paths.run_account_dir(base, run_id, account)


def ensure_run_account_dir(base: Path, run_id: str, account: str) -> Path:
    p = get_run_account_dir(base, run_id, account)
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_run_account_state_dir(base: Path, run_id: str, account: str) -> Path:
    return paths.run_account_state_dir(base, run_id, account)


def ensure_run_account_state_dir(base: Path, run_id: str, account: str) -> Path:
    p = get_run_account_state_dir(base, run_id, account)
    p.mkdir(parents=True, exist_ok=True)
    return p


def write_run_account_text(base: Path, run_id: str, account: str, name: str, text: str) -> Path:
    out = (ensure_run_account_dir(base, run_id, account) / str(name)).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(str(text), encoding="utf-8")
    return out


def copy_to_run_account(base: Path, run_id: str, account: str, src: Path, name: str) -> Path:
    out = (ensure_run_account_dir(base, run_id, account) / str(name)).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(src), str(out))
    return out


def list_retained_run_ids(base: Path, *, limit: int = 500) -> tuple[list[str], bool]:
    """Inventory retained run directories without creating or following external paths."""
    import heapq
    import os
    root = (base / "output_runs").resolve()
    if not root.is_dir():
        raise FileNotFoundError("run_history_missing")
    with os.scandir(root) as entries:
        names = heapq.nlargest(limit + 1, (item.name for item in entries if item.is_dir(follow_symlinks=False)))
    return names[:limit], len(names) > limit
