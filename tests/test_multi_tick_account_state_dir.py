"""Regression: multi-account run_pipeline state dir must be account-isolated."""

from __future__ import annotations

from pathlib import Path


def test_account_run_state_dir_isolated_by_account() -> None:
    from scripts.multi_tick.main import account_run_state_dir

    run_dir = Path("/tmp/output_runs/20260407T220000")
    lx = account_run_state_dir(run_dir, "lx")
    sy = account_run_state_dir(run_dir, "sy")

    assert lx != sy
    assert lx == (run_dir / "accounts" / "lx" / "state").resolve()
    assert sy == (run_dir / "accounts" / "sy" / "state").resolve()
