from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from domain.storage.repositories import run_repo, state_repo
from src.application.multi_tick.misc import (
    ensure_account_output_dir,
)


@dataclass(frozen=True)
class TickRunWorkspace:
    accounts_root: Path
    run_dir: Path
    shared_required: Path


def prepare_tick_run_workspace(
    *,
    base: Path,
    run_id: str,
    default_account: str,
) -> TickRunWorkspace:
    _cleanup_old_run_dirs(base)

    accounts_root = (base / "output_accounts").resolve()
    accounts_root.mkdir(parents=True, exist_ok=True)
    ensure_account_output_dir(accounts_root / default_account)

    run_dir = run_repo.ensure_run_dir(base, run_id)
    required_dir = (run_dir / "required_data").resolve()
    required_raw = (required_dir / "raw").resolve()
    required_parsed = (required_dir / "parsed").resolve()
    required_raw.mkdir(parents=True, exist_ok=True)
    required_parsed.mkdir(parents=True, exist_ok=True)

    run_repo.ensure_run_state_dir(base, run_id)
    try:
        state_repo.write_last_run_dir_pointer(base, run_id)
    except Exception:
        pass

    return TickRunWorkspace(
        accounts_root=accounts_root,
        run_dir=run_dir,
        shared_required=required_dir,
    )


def _cleanup_old_run_dirs(base: Path) -> None:
    try:
        runs_root = (base / "output_runs").resolve()
        runs_root.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - 7 * 86400
        pattern = re.compile(r"^\d{8}T\d{6}$")
        for path in runs_root.iterdir():
            try:
                if not path.is_dir():
                    continue
                if not pattern.match(path.name):
                    continue
                if path.stat().st_mtime < cutoff:
                    shutil.rmtree(path, ignore_errors=True)
            except Exception:
                pass
    except Exception:
        pass
