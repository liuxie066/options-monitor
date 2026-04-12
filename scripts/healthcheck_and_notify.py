#!/usr/bin/env python3
"""Run healthcheck and notify via OpenClaw message send on WARN/CRITICAL.

This is intended to be run by cron.

Behavior:
- Runs scripts/healthcheck.py
- If output contains '## CRITICAL' or '## WARN': send the full report to notifications target
- If output is OK: stay silent (default)

Notes:
- Exits 0 on OK and on successful notify (to avoid cron exponential backoff loops).
- Exits non-zero only on execution/send failures.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from om.domain import normalize_notify_subprocess_output, normalize_pipeline_subprocess_output


def load_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding='utf-8'))


def main() -> int:
    ap = argparse.ArgumentParser(description='options-monitor healthcheck + notify')
    ap.add_argument('--config', default='config.us.json', help='options-monitor config.us.json/config.hk.json')
    ap.add_argument('--accounts', nargs='*', default=['lx', 'sy'])
    ap.add_argument('--notify-on', choices=['warn', 'critical', 'both'], default='both')
    ap.add_argument('--silent-ok', action='store_true', default=True)
    ap.add_argument('--dry-run', action='store_true', help='print what would be sent')
    args = ap.parse_args()

    base = Path(__file__).resolve().parents[1]
    vpy = base / '.venv' / 'bin' / 'python'

    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = (base / cfg_path).resolve()
    cfg = load_json(cfg_path)

    # Run healthcheck
    hc = subprocess.run(
        [str(vpy), 'scripts/healthcheck.py', '--config', str(cfg_path), '--accounts', *args.accounts],
        cwd=str(base),
        capture_output=True,
        text=True,
    )
    hc_payload = normalize_pipeline_subprocess_output(
        returncode=int(hc.returncode),
        stdout=str(hc.stdout or ""),
        stderr=str(hc.stderr or ""),
    )
    out = (hc.stdout or '')
    err = (hc.stderr or '')

    if int(hc_payload.get("returncode") or 0) not in (0, 2):
        # healthcheck itself failed unexpectedly
        sys.stderr.write(err or out)
        return 2

    is_critical = ('## CRITICAL' in out)
    is_warn = ('## WARN' in out)

    should_notify = False
    if args.notify_on == 'both':
        should_notify = is_critical or is_warn
    elif args.notify_on == 'warn':
        should_notify = is_warn
    elif args.notify_on == 'critical':
        should_notify = is_critical

    if not should_notify:
        # OK or not configured to notify
        if not args.silent_ok:
            print(out.strip())
        return 0

    notif = cfg.get('notifications') or {}
    channel = notif.get('channel') or 'feishu'
    target = notif.get('target')
    if not target:
        sys.stderr.write('[CONFIG_ERROR] notifications.target missing\n')
        return 2

    title = 'CRITICAL' if is_critical else 'WARN'
    msg = f"options-monitor healthcheck: {title}\n\n" + out.strip() + "\n"

    if args.dry_run:
        print('[DRY_RUN] would send to', channel, target)
        print(msg)
        return 0

    send = subprocess.run(
        ['openclaw', 'message', 'send', '--channel', str(channel), '--target', str(target), '--message', msg],
        cwd=str(base),
        capture_output=True,
        text=True,
    )
    send_payload = normalize_notify_subprocess_output(
        returncode=int(send.returncode),
        stdout=str(send.stdout or ""),
        stderr=str(send.stderr or ""),
    )
    if not bool(send_payload.get("ok")):
        sys.stderr.write(send.stderr or send.stdout or '')
        return int(send_payload.get("returncode") or send.returncode)

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
