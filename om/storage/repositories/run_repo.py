from __future__ import annotations

from pathlib import Path

from om.storage import paths


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

