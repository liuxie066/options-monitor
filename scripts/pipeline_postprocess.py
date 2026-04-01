"""Pipeline postprocess (summary/digest/alerts/notify).

Why:
- Keep run_pipeline orchestration-only (Stage 3).
- Centralize side effects after scan.

Design:
- External dependencies are injected to keep this module unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class PostprocessResult:
    meaningful: bool
    notification_text: str


def postprocess_scan_results(
    *,
    summary_rows: list[dict],
    report_dir: Path,
    is_scheduled: bool,
    top_n: int,
    symbols: list[str],
    runtime: dict,
    want_fn: Callable[[str], bool],
    build_symbols_summary_fn: Callable[[list[dict]], object],
    build_symbols_digest_fn: Callable[[list[dict], int], object],
    render_sell_put_alerts_fn: Callable[[Path, int], str],
    render_sell_call_alerts_fn: Callable[[Path, int], str],
    should_notify_symbols_fn: Callable[[dict, str], bool],
    notify_symbols_fn: Callable[[dict, str], None],
    log: Callable[[str], None],
) -> PostprocessResult:
    if want_fn('scan'):
        build_symbols_summary_fn(summary_rows)
        if not is_scheduled:
            build_symbols_digest_fn(summary_rows, int(top_n))

    if not want_fn('notify'):
        return PostprocessResult(meaningful=False, notification_text='')

    alert_text = ''
    if want_fn('alert'):
        try:
            sp = render_sell_put_alerts_fn(report_dir, int(top_n))
            sc = render_sell_call_alerts_fn(report_dir, int(top_n))
            alert_text = (sp + '\n\n' + sc).strip()
        except Exception as e:
            log(f"[WARN] render alerts failed: {e}")
            alert_text = ''

    notification_text = alert_text
    meaningful = bool(notification_text.strip())

    if meaningful and should_notify_symbols_fn(runtime, notification_text):
        notify_symbols_fn(runtime, notification_text)

    return PostprocessResult(meaningful=meaningful, notification_text=notification_text)
