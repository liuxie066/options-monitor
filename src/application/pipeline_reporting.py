from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from scripts.alert_engine import run_alert_engine
from scripts.notify_symbols import (
    _infer_account_label,
    build_notification,
    read_text,
)


@dataclass(frozen=True)
class AlertStageResult:
    alert_text: str
    changes_text: str
    output_path: Path
    changes_path: Path


def run_pipeline_alert_stage(
    *,
    summary_input: Path,
    output: Path,
    changes_output: Path,
    previous_summary: Path | None = None,
    state_dir: Path | None = None,
    update_snapshot: bool = False,
    policy_json: str | None = None,
) -> AlertStageResult:
    result = run_alert_engine(
        summary_input=str(summary_input),
        output=str(output),
        changes_output=str(changes_output),
        previous_summary=(str(previous_summary) if previous_summary is not None else None),
        state_dir=(str(state_dir) if state_dir is not None else None),
        update_snapshot=bool(update_snapshot),
        policy_json=policy_json,
    )
    return AlertStageResult(
        alert_text=str(result.get("alert_text") or ""),
        changes_text=str(result.get("changes_text") or ""),
        output_path=Path(result["output_path"]),
        changes_path=Path(result["changes_path"]),
    )


def run_pipeline_notification_stage(
    *,
    alerts_input: Path,
    changes_input: Path,
    output: Path,
) -> str:
    alerts_text = read_text(alerts_input)
    account_label = _infer_account_label(output, alerts_input)
    notification = build_notification(
        "",
        alerts_text,
        exchange_rate_info=None,
        account_label=account_label,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(notification, encoding="utf-8")
    return notification
