from __future__ import annotations

from pathlib import Path


def _safe_component(value: object, field: str) -> str:
    text = str(value or "").strip()
    if (
        not text
        or text in {".", ".."}
        or Path(text).name != text
        or "/" in text
        or "\\" in text
    ):
        raise ValueError(f"{field} must be a safe path component")
    return text


def shared_state_dir(base: Path) -> Path:
    return (base / "output_shared" / "state").resolve()


def shared_state_path(base: Path, name: str) -> Path:
    return (shared_state_dir(base) / str(name)).resolve()


def run_dir(base: Path, run_id: str) -> Path:
    return (base / "output_runs" / _safe_component(run_id, "run_id")).resolve()


def run_state_dir(base: Path, run_id: str) -> Path:
    return (run_dir(base, run_id) / "state").resolve()


def run_account_dir(base: Path, run_id: str, account: str) -> Path:
    return (
        run_dir(base, run_id)
        / "accounts"
        / _safe_component(account, "account")
    ).resolve()


def run_account_state_dir(base: Path, run_id: str, account: str) -> Path:
    return (run_account_dir(base, run_id, account) / "state").resolve()


def account_output_dir(base: Path, account: str) -> Path:
    return (
        base / "output_accounts" / _safe_component(account, "account")
    ).resolve()


def account_state_dir(base: Path, account: str) -> Path:
    return (account_output_dir(base, account) / "state").resolve()
