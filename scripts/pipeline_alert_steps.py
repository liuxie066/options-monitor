#!/usr/bin/env python3
from __future__ import annotations

"""Alert/notify orchestration (Stage 3 refactor).

Extract the stage-only (alert/notify) logic from run_pipeline.py to keep
run_pipeline as a thin top-level orchestrator.

Constraints:
- minimal/no behavior change
- preserve scheduled quiet behavior
"""

from pathlib import Path

from scripts.subprocess_utils import run_cmd


def stage_only_changes_out(*, is_scheduled: bool) -> str:
    # In dev iteration, stage-only should not mutate snapshot/change history.
    # (Otherwise, formatting tests would pollute symbols_summary_prev.csv / changes.)
    return '/dev/null'


def run_stage_only_alert_notify(
    *,
    py: str,
    base: Path,
    report_dir: Path,
    is_scheduled: bool,
    stage_only: str,
    want,
    log,
) -> None:
    """Run ONLY alert or notify stage using existing output files."""

    summary_path = (report_dir / 'symbols_summary.csv').resolve()
    alerts_path = (report_dir / 'symbols_alerts.txt').resolve()

    if stage_only == 'alert':
        if not (summary_path.exists() and summary_path.stat().st_size > 0):
            raise SystemExit(f"[STAGE_ONLY_ERROR] missing required file: {summary_path}")
    if stage_only == 'notify':
        if not (alerts_path.exists() and alerts_path.stat().st_size > 0):
            raise SystemExit(f"[STAGE_ONLY_ERROR] missing required file: {alerts_path}")

    changes_out = stage_only_changes_out(is_scheduled=is_scheduled)

    alert_cmd = [
        py, 'scripts/alert_engine.py',
        '--summary-input', str((report_dir / 'symbols_summary.csv').as_posix()),
        '--output', str((report_dir / 'symbols_alerts.txt').as_posix()),
        '--changes-output', changes_out,
    ]
    # stage-only: do NOT update snapshot/history
    if (not is_scheduled) and (not stage_only):
        alert_cmd.extend([
            '--previous-summary', 'output/state/symbols_summary_prev.csv',
            '--update-snapshot',
        ])
    if want('alert'):
        run_cmd(alert_cmd, cwd=base, is_scheduled=is_scheduled)

    if want('notify'):
        run_cmd([
            py, 'scripts/notify_symbols.py',
            '--alerts-input', str((report_dir / 'symbols_alerts.txt').as_posix()),
            '--changes-input', changes_out,
            '--output', str((report_dir / 'symbols_notification.txt').as_posix()),
        ], cwd=base, is_scheduled=is_scheduled)

    log(f"[INFO] stage-only done: {stage_only}")
