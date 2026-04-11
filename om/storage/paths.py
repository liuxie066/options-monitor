from __future__ import annotations

from pathlib import Path


def shared_state_dir(base: Path) -> Path:
    return (base / "output_shared" / "state").resolve()


def shared_state_path(base: Path, name: str) -> Path:
    return (shared_state_dir(base) / str(name)).resolve()


def run_dir(base: Path, run_id: str) -> Path:
    return (base / "output_runs" / str(run_id)).resolve()


def run_state_dir(base: Path, run_id: str) -> Path:
    return (run_dir(base, run_id) / "state").resolve()


def run_account_dir(base: Path, run_id: str, account: str) -> Path:
    return (run_dir(base, run_id) / "accounts" / str(account).strip()).resolve()


def run_account_state_dir(base: Path, run_id: str, account: str) -> Path:
    return (run_account_dir(base, run_id, account) / "state").resolve()


def account_output_dir(base: Path, account: str) -> Path:
    return (base / "output_accounts" / str(account).strip()).resolve()


def account_state_dir(base: Path, account: str) -> Path:
    return (account_output_dir(base, account) / "state").resolve()

