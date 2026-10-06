from __future__ import annotations

import json
from pathlib import Path
import stat

import pytest

from domain.storage.no_follow import UnsafePathError
from domain.storage.repositories import run_repo, state_repo


def test_account_run_state_writer_keeps_private_json_contract(tmp_path: Path) -> None:
    path = state_repo.write_account_run_state(
        tmp_path, "run-1", "lx", "account_metrics.json", {"account": "lx", "value": 1}
    )

    assert path == tmp_path / "output_runs/run-1/accounts/lx/state/account_metrics.json"
    assert path.read_text(encoding="utf-8") == (
        '{\n  "account": "lx",\n  "value": 1\n}\n'
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    last_run = state_repo.write_run_account_last_run(
        tmp_path, "run-1", "lx", {"status": "failed"}
    )
    assert last_run.parent == path.parent
    assert json.loads(last_run.read_text(encoding="utf-8")) == {"status": "failed"}
    assert stat.S_IMODE(last_run.stat().st_mode) == 0o600


def test_account_run_state_writer_refuses_directory_symlinks(
    tmp_path: Path,
) -> None:
    for component in ("output_runs", "state"):
        base = tmp_path / component / "base"
        base.mkdir(parents=True)
        outside = tmp_path / component / "outside"
        outside.mkdir()
        if component == "output_runs":
            target = base / "output_runs"
        else:
            target = base / "output_runs/run-1/accounts/lx/state"
            target.parent.mkdir(parents=True)
        target.symlink_to(outside, target_is_directory=True)

        with pytest.raises(UnsafePathError):
            state_repo.write_account_run_state(
                base, "run-1", "lx", "account_metrics.json", {"account": "lx"}
            )
        with pytest.raises(UnsafePathError):
            run_repo.ensure_run_account_state_dir(base, "run-1", "lx")
        assert list(outside.iterdir()) == []


def test_account_run_state_writer_replaces_file_symlink_without_following(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text("untouched", encoding="utf-8")
    target = tmp_path / "output_runs/run-1/accounts/lx/state/account_metrics.json"
    target.parent.mkdir(parents=True)
    target.symlink_to(outside)

    result = state_repo.write_account_run_state(
        tmp_path, "run-1", "lx", "account_metrics.json", {"account": "lx"}
    )

    assert result == target
    assert not target.is_symlink()
    assert json.loads(target.read_text(encoding="utf-8")) == {"account": "lx"}
    assert outside.read_text(encoding="utf-8") == "untouched"
