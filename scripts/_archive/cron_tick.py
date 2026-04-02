#!/usr/bin/env python3
"""Cron tick entrypoint for options-monitor.

Design goals:
- Keep exactly two decision layers:
  1) scan_scheduler: should_run_scan + should_notify + cooldown
  2) notify_symbols: content selection (changes vs high vs medium)
- This script is orchestration only (run, read, and optionally send).

Behavior:
- If not due: exit 0 quietly.
- If due: run pipeline.
- If should_notify and notification is meaningful: send Feishu DM (OpenClaw message tool is handled by the cron agent turn).

NOTE: This script itself does NOT call OpenClaw tools; it prints key info and writes last_run.json.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)


def main():
    base = Path(__file__).resolve().parents[1]
    vpy = str(base / '.venv' / 'bin' / 'python')
    # Config selection: default to US config; override via OPTIONS_MONITOR_CONFIG env
    import os
    cfg = str(base / os.getenv('OPTIONS_MONITOR_CONFIG', 'config.us.json'))
    state = str(base / 'output' / 'state' / 'scheduler_state.json')
    last_run = str(base / 'output' / 'state' / 'last_run.json')

    started = utc_now()

    # 1) scheduler decision
    sch = run([vpy, 'scripts/scan_scheduler.py', '--config', cfg, '--state', state], cwd=base)
    if sch.returncode != 0:
        run([vpy, 'scripts/write_last_run.py', '--path', last_run, '--status', 'error', '--stage', 'scheduler', '--details', (sch.stderr or sch.stdout or '').strip(), '--started-at', started], cwd=base)
        sys.stderr.write(sch.stderr)
        raise SystemExit(sch.returncode)

    # scan_scheduler prints pretty JSON across multiple lines; parse the full stdout
    decision = json.loads((sch.stdout or '').strip())
    should_run = bool(decision.get('should_run_scan'))
    should_notify = bool(decision.get('should_notify'))

    if not should_run:
        run([vpy, 'scripts/write_last_run.py', '--path', last_run, '--status', 'skip', '--stage', 'scheduler', '--reason', str(decision.get('reason') or ''), '--started-at', started], cwd=base)
        return

    # 2) pipeline
    pipe = subprocess.run([vpy, 'scripts/run_pipeline.py', '--config', cfg], cwd=str(base))
    if pipe.returncode != 0:
        run([vpy, 'scripts/write_last_run.py', '--path', last_run, '--status', 'error', '--stage', 'pipeline', '--reason', 'pipeline failed', '--started-at', started], cwd=base)
        raise SystemExit(pipe.returncode)

    notif_path = base / 'output' / 'reports' / 'symbols_notification.txt'
    notif_text = notif_path.read_text(encoding='utf-8', errors='replace').strip() if notif_path.exists() else ''

    meaningful = bool(notif_text) and (notif_text != '今日无需要主动提醒的内容。')

    # We do NOT send here; cron agentTurn will decide whether to send.
    # Write last_run for observability.
    run([vpy, 'scripts/write_last_run.py', '--path', last_run, '--status', 'ok', '--stage', 'pipeline', '--reason', str(decision.get('reason') or ''), '--details', f"should_notify={should_notify} meaningful={meaningful}", '--started-at', started], cwd=base)

    # Print compact JSON for the cron agent to parse if desired
    print(json.dumps({
        'should_notify': should_notify,
        'meaningful': meaningful,
        'notification_path': str(notif_path),
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
