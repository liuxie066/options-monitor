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

from src.application.pipeline_reporting import (
    run_pipeline_alert_stage,
    run_pipeline_notification_stage,
)


def run_stage_only_alert_notify(
    *,
    report_dir: Path,
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

    # stage-only: do NOT update snapshot/history
    # stage-only: do NOT update snapshot/history (do not touch symbols_summary_prev.csv)
    if want('alert'):
        run_pipeline_alert_stage(
            summary_input=(report_dir / 'symbols_summary.csv').resolve(),
            output=(report_dir / 'symbols_alerts.txt').resolve(),
            changes_output=(report_dir / 'symbols_changes.txt').resolve(),
        )

    if want('notify'):
        run_pipeline_notification_stage(
            alerts_input=(report_dir / 'symbols_alerts.txt').resolve(),
            output=(report_dir / 'symbols_notification.txt').resolve(),
        )

    log(f"[INFO] stage-only done: {stage_only}")
