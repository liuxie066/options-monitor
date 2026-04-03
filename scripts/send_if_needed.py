#!/usr/bin/env python3
"""Single-entrypoint for production cron.

Responsibilities (no extra layers beyond existing 2):
- Use scan_scheduler to decide due + notify allowance
- Run pipeline when due
- If should_notify and notification meaningful: send to Feishu via OpenClaw CLI
- After successful send: mark-notified
- Always write last_run.json

This avoids relying on an LLM to correctly execute multi-step tool instructions.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.io_utils import utc_now


def sh(cmd: list[str], cwd: Path, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=capture, text=True)


def _pid_alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def _acquire_lock(lock_path: Path) -> int:
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Best-effort stale lock cleanup
    if lock_path.exists():
        try:
            pid_txt = lock_path.read_text(encoding='utf-8').strip()
            pid = int(pid_txt) if pid_txt else -1
            if pid <= 0 or not _pid_alive(pid):
                lock_path.unlink(missing_ok=True)
        except Exception:
            # If unreadable, prefer removing to avoid permanent deadlock
            try:
                lock_path.unlink(missing_ok=True)
            except Exception:
                pass

    fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    os.write(fd, str(os.getpid()).encode('utf-8'))
    return fd


def _release_lock(fd: int, lock_path: Path):
    try:
        os.close(fd)
    except Exception:
        pass
    try:
        lock_path.unlink(missing_ok=True)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description='Run scheduled tick and send notification if needed')
    ap.add_argument('--config', default='config.json')
    ap.add_argument('--state-dir', default='output/state', help='Directory for scheduler_state/last_run/locks (default: output/state)')
    ap.add_argument('--channel', default=None)
    ap.add_argument('--target', default=None)
    ap.add_argument('--state', default=None, help='[deprecated] scheduler state file path. Prefer --state-dir.')
    ap.add_argument('--report-dir', default='output/reports', help='Directory where pipeline writes reports (default: output/reports)')
    ap.add_argument('--notification', default=None, help='Notification text file path. Default: <report-dir>/symbols_notification.txt')
    args = ap.parse_args()

    base = Path(__file__).resolve().parents[1]
    vpy = base / '.venv' / 'bin' / 'python'

    cfg = Path(args.config)
    if not cfg.is_absolute():
        cfg = (base / cfg).resolve()

    cfg_obj = json.loads(cfg.read_text(encoding='utf-8'))
    notif_cfg = cfg_obj.get('notifications') or {}

    channel = args.channel or notif_cfg.get('channel') or 'feishu'
    target = args.target or notif_cfg.get('target')
    if not target:
        raise SystemExit('[CONFIG_ERROR] notifications.target is required (e.g. user:open_id or chat:chat_id)')

    state_dir = Path(args.state_dir)
    if not state_dir.is_absolute():
        state_dir = (base / state_dir).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)

    # Backward compat: allow explicit --state file override
    if args.state:
        state = Path(args.state)
        if not state.is_absolute():
            state = (base / state).resolve()
    else:
        state = (state_dir / 'scheduler_state.json').resolve()

    report_dir = Path(args.report_dir)
    if not report_dir.is_absolute():
        report_dir = (base / report_dir).resolve()

    if args.notification:
        notif = Path(args.notification)
        if not notif.is_absolute():
            notif = (base / notif).resolve()
    else:
        notif = (report_dir / 'symbols_notification.txt').resolve()

    last_run = (state_dir / 'last_run.json').resolve()
    lock_path = (state_dir / 'send_if_needed.lock').resolve()

    started = utc_now()

    # Prevent concurrent runs (cron overlap)
    lock_fd = None
    try:
        lock_fd = _acquire_lock(lock_path)
    except FileExistsError:
        sh([str(vpy), 'scripts/write_last_run.py', '--path', str(last_run), '--status', 'skip', '--stage', 'lock', '--reason', 'locked (another run in progress)', '--started-at', started], cwd=base)
        return 0

    try:
        # 1) scheduler decision
        sch = sh([str(vpy), 'scripts/scan_scheduler.py', '--config', str(cfg), '--state', str(state), '--jsonl'], cwd=base, capture=True)
        if sch.returncode != 0:
            sh([str(vpy), 'scripts/write_last_run.py', '--path', str(last_run), '--status', 'error', '--stage', 'scheduler', '--details', (sch.stderr or sch.stdout or '').strip(), '--started-at', started], cwd=base)
            sys.stderr.write(sch.stderr)
            raise SystemExit(sch.returncode)

        decision = json.loads((sch.stdout or '').strip())
        should_run = bool(decision.get('should_run_scan'))
        should_notify = bool(decision.get('should_notify'))

        if not should_run:
            sh([str(vpy), 'scripts/write_last_run.py', '--path', str(last_run), '--status', 'skip', '--stage', 'scheduler', '--reason', str(decision.get('reason') or ''), '--started-at', started], cwd=base)
            return 0

        # 2) pipeline
        pipe = subprocess.run([
            str(vpy), 'scripts/run_pipeline.py',
            '--config', str(cfg),
            '--mode', 'scheduled',
            '--report-dir', str(report_dir),
            '--state-dir', str(state_dir),
        ], cwd=str(base))
        if pipe.returncode != 0:
            sh([str(vpy), 'scripts/write_last_run.py', '--path', str(last_run), '--status', 'error', '--stage', 'pipeline', '--reason', 'pipeline failed', '--started-at', started], cwd=base)
            return pipe.returncode

        text = notif.read_text(encoding='utf-8', errors='replace').strip() if notif.exists() else ''

        # Prefix notification with account tag when configured.
        try:
            p = (cfg_obj.get('portfolio') or {}) if isinstance(cfg_obj, dict) else {}
            acct = str(p.get('account') or '').strip()
            if acct and text:
                text = f"[{acct}]\n" + text
        except Exception:
            pass

        meaningful = bool(text) and (text != '今日无需要主动提醒的内容。')

        if should_notify and meaningful:
            # 3) send via OpenClaw CLI
            send = subprocess.run(
                ['openclaw', 'message', 'send', '--channel', channel, '--target', target, '--message', text, '--json'],
                cwd=str(base),
                capture_output=True,
                text=True,
            )
            if send.returncode != 0:
                sh([str(vpy), 'scripts/write_last_run.py', '--path', str(last_run), '--status', 'error', '--stage', 'send', '--details', (send.stderr or send.stdout or '').strip(), '--started-at', started], cwd=base)
                sys.stderr.write(send.stderr)
                return send.returncode

            message_id = None
            try:
                data = json.loads((send.stdout or '').strip())
                # OpenClaw CLI may return {"messageId": ...} or nested {"result": {"messageId": ...}}
                message_id = data.get('messageId') or ((data.get('result') or {}).get('messageId') if isinstance(data.get('result'), dict) else None)
            except Exception:
                message_id = None

            # 4) mark notified (only after successful send)
            mark = subprocess.run([str(vpy), 'scripts/scan_scheduler.py', '--config', str(cfg), '--state', str(state), '--mark-notified'], cwd=str(base))
            if mark.returncode != 0:
                # send succeeded but mark failed: still record it
                sh([str(vpy), 'scripts/write_last_run.py', '--path', str(last_run), '--status', 'error', '--stage', 'mark-notified', '--reason', 'send ok but mark-notified failed', '--started-at', started], cwd=base)
                return mark.returncode

            detail = 'sent+marked'
            if message_id:
                detail += f" message_id={message_id}"
            sh([str(vpy), 'scripts/write_last_run.py', '--path', str(last_run), '--status', 'ok', '--stage', 'send', '--reason', 'sent', '--details', detail, '--started-at', started], cwd=base)
            return 0

        # not sending
        sh([str(vpy), 'scripts/write_last_run.py', '--path', str(last_run), '--status', 'ok', '--stage', 'pipeline', '--reason', str(decision.get('reason') or ''), '--details', f"should_notify={should_notify} meaningful={meaningful}", '--started-at', started], cwd=base)
        return 0
    finally:
        if lock_fd is not None:
            _release_lock(lock_fd, lock_path)


if __name__ == '__main__':
    raise SystemExit(main())
