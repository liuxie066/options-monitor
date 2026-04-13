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
import sys
from pathlib import Path

# Ensure repo root is on sys.path for `scripts.*` imports when run as a script
from pathlib import Path as _PathLib

_repo_root = _PathLib(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from scripts.io_utils import utc_now
from om.domain import (
    Decision,
    DeliveryPlan,
    SchemaValidationError,
    SnapshotDTO,
    markets_for_trading_day_guard as domain_markets_for_trading_day_guard,
    normalize_notify_subprocess_output,
    normalize_pipeline_subprocess_output,
    resolve_notification_route_from_config,
    resolve_scheduler_state_path,
)
from om.domain.engine import decide_notify_window_open, resolve_scheduler_decision
from scripts.infra.service import (
    run_command,
    run_pipeline_script,
    run_scan_scheduler_cli,
    send_openclaw_message,
    trading_day_via_futu,
)

SCHEMA_VALIDATION_ERROR_CODE = "SCHEMA_VALIDATION_FAILED"


def _infer_trading_day_guard_markets(cfg_obj: dict) -> list[str]:
    # Keep legacy helper name for callers/tests, but centralize compat reads in domain.
    return domain_markets_for_trading_day_guard([], cfg_obj, 'auto')


def _trading_day_guard_for_market(cfg_obj: dict, market: str) -> tuple[bool | None, str]:
    """Return (is_trading_day, market_used).

    None means guard check failed and caller should continue without blocking.
    """
    return trading_day_via_futu(cfg_obj, market)


def sh(cmd: list[str], cwd: Path, capture: bool = True):
    return run_command(cmd, cwd=cwd, capture_output=capture, text=True)


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


def _fail_schema_validation(*, base: Path, vpy: Path, last_run: Path, started: str, stage: str, exc: BaseException) -> None:
    msg = f"{stage}: {type(exc).__name__}: {exc}"
    try:
        sh(
            [
                str(vpy),
                "scripts/write_last_run.py",
                "--path",
                str(last_run),
                "--status",
                "error",
                "--stage",
                "contract",
                "--reason",
                SCHEMA_VALIDATION_ERROR_CODE,
                "--details",
                msg,
                "--started-at",
                started,
            ],
            cwd=base,
        )
    except Exception:
        pass
    raise SystemExit(f"[CONTRACT_ERROR][{SCHEMA_VALIDATION_ERROR_CODE}] {msg}")


def main():
    ap = argparse.ArgumentParser(description='Run scheduled tick and send notification if needed')
    ap.add_argument('--config', default='config.us.json')
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
    notify_route = resolve_notification_route_from_config(
        config=cfg_obj,
        cli_channel=args.channel,
        cli_target=args.target,
    )
    channel = notify_route.get('channel')
    target = notify_route.get('target')
    if not target:
        raise SystemExit('[CONFIG_ERROR] notifications.target is required (e.g. user:open_id or chat:chat_id)')

    state_dir = Path(args.state_dir)
    if not state_dir.is_absolute():
        state_dir = (base / state_dir).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)

    state = resolve_scheduler_state_path(
        base_dir=base,
        state_dir=state_dir,
        state_override=args.state,
        filename='scheduler_state.json',
    )

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
        sch = run_scan_scheduler_cli(vpy=vpy, base=base, config=cfg, state=state, jsonl=True, capture_output=True)
        if sch.returncode != 0:
            sh([str(vpy), 'scripts/write_last_run.py', '--path', str(last_run), '--status', 'error', '--stage', 'scheduler', '--details', (sch.stderr or sch.stdout or '').strip(), '--started-at', started], cwd=base)
            sys.stderr.write(sch.stderr)
            raise SystemExit(sch.returncode)

        try:
            scheduler_raw = json.loads((sch.stdout or "").strip())
            scheduler_input_snapshot = SnapshotDTO.from_payload(
                {
                    "schema_kind": "snapshot_dto",
                    "schema_version": "1.0",
                    "snapshot_name": "send_if_needed_scheduler_raw",
                    "as_of_utc": utc_now(),
                    "payload": {"scheduler_raw": scheduler_raw},
                }
            )
            scheduler_payload = scheduler_input_snapshot.payload.get("scheduler_raw")
            if not isinstance(scheduler_payload, dict):
                raise SchemaValidationError("scheduler_raw must be a dict")
            scheduler_decision, scheduler_view = resolve_scheduler_decision(scheduler_payload)
            SnapshotDTO.from_payload(
                {
                    "schema_kind": "snapshot_dto",
                    "schema_version": "1.0",
                    "snapshot_name": "send_if_needed_scheduler_decision",
                    "as_of_utc": utc_now(),
                    "payload": {"scheduler_decision": scheduler_decision},
                }
            )
            account_name = str(((cfg_obj.get("portfolio") or {}).get("account") or "default")).strip() or "default"
            decision = Decision.from_payload(
                {
                    "schema_kind": "decision",
                    "schema_version": "1.0",
                    "account": account_name,
                    "should_run": bool(scheduler_view.should_run_scan),
                    "should_notify": bool(decide_notify_window_open(scheduler_decision=scheduler_view)),
                    "reason": str(scheduler_view.reason),
                }
            )
        except SchemaValidationError as e:
            _fail_schema_validation(
                base=base,
                vpy=vpy,
                last_run=last_run,
                started=started,
                stage="scheduler_decision",
                exc=e,
            )
        except Exception as e:
            _fail_schema_validation(
                base=base,
                vpy=vpy,
                last_run=last_run,
                started=started,
                stage="scheduler_parse",
                exc=e,
            )
        should_run = bool(decision.should_run)
        should_notify = bool(decision.should_notify)
        reason = str(decision.reason)

        if not should_run:
            sh([str(vpy), 'scripts/write_last_run.py', '--path', str(last_run), '--status', 'skip', '--stage', 'scheduler', '--reason', reason, '--started-at', started], cwd=base)
            return 0

        # 1.5) trading day guard (multi-market)
        guard_markets = _infer_trading_day_guard_markets(cfg_obj)
        guard_results: list[dict] = []
        for gm in guard_markets:
            is_td, gm_used = _trading_day_guard_for_market(cfg_obj, gm)
            guard_results.append({'market': gm_used, 'is_trading_day': is_td})

        false_markets = [str(r.get('market')) for r in guard_results if r.get('is_trading_day') is False]
        if false_markets and len(false_markets) == len(guard_markets):
            sh(
                [
                    str(vpy),
                    'scripts/write_last_run.py',
                    '--path',
                    str(last_run),
                    '--status',
                    'skip',
                    '--stage',
                    'trading_day_guard',
                    '--reason',
                    f"non-trading day: {','.join(false_markets)}",
                    '--started-at',
                    started,
                ],
                cwd=base,
            )
            return 0

        # 2) pipeline
        pipe = run_pipeline_script(
            vpy=vpy,
            base=base,
            config=cfg,
            report_dir=report_dir,
            state_dir=state_dir,
        )
        try:
            pipe_payload = normalize_pipeline_subprocess_output(
                returncode=int(pipe.returncode),
                stdout=str(pipe.stdout or ""),
                stderr=str(pipe.stderr or ""),
            )
        except ValueError as e:
            _fail_schema_validation(
                base=base,
                vpy=vpy,
                last_run=last_run,
                started=started,
                stage="pipeline_subprocess_adapter",
                exc=e,
            )
        if not bool(pipe_payload.get("ok")):
            sh([str(vpy), 'scripts/write_last_run.py', '--path', str(last_run), '--status', 'error', '--stage', 'pipeline', '--reason', 'pipeline failed', '--started-at', started], cwd=base)
            return int(pipe_payload.get("returncode") or pipe.returncode)

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
        account_name = str(((cfg_obj.get("portfolio") or {}).get("account") or "default")).strip() or "default"
        try:
            delivery_plan = DeliveryPlan.from_payload(
                {
                    "schema_kind": "delivery_plan",
                    "schema_version": "1.0",
                    "channel": str(channel),
                    "target": str(target),
                    "account_messages": {account_name: text},
                    "should_send": bool(should_notify and meaningful),
                }
            )
        except SchemaValidationError as e:
            _fail_schema_validation(
                base=base,
                vpy=vpy,
                last_run=last_run,
                started=started,
                stage="delivery_plan",
                exc=e,
            )

        if bool(delivery_plan.should_send):
            # 3) send via OpenClaw CLI
            send = send_openclaw_message(
                base=base,
                channel=str(delivery_plan.channel),
                target=str(delivery_plan.target),
                message=str(delivery_plan.account_messages.get(account_name) or ""),
            )
            try:
                send_payload = normalize_notify_subprocess_output(
                    returncode=int(send.returncode),
                    stdout=str(send.stdout or ""),
                    stderr=str(send.stderr or ""),
                )
            except ValueError as e:
                _fail_schema_validation(
                    base=base,
                    vpy=vpy,
                    last_run=last_run,
                    started=started,
                    stage="notify_subprocess_adapter",
                    exc=e,
                )
            if not bool(send_payload.get("ok")):
                sh([str(vpy), 'scripts/write_last_run.py', '--path', str(last_run), '--status', 'error', '--stage', 'send', '--details', (send.stderr or send.stdout or '').strip(), '--started-at', started], cwd=base)
                sys.stderr.write(send.stderr)
                return int(send_payload.get("returncode") or send.returncode)

            message_id = send_payload.get("message_id")

            # 4) mark notified (only after successful send)
            mark = run_scan_scheduler_cli(vpy=vpy, base=base, config=cfg, state=state, mark_notified=True, capture_output=False)
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
        sh([str(vpy), 'scripts/write_last_run.py', '--path', str(last_run), '--status', 'ok', '--stage', 'pipeline', '--reason', reason, '--details', f"should_notify={should_notify} meaningful={meaningful}", '--started-at', started], cwd=base)
        return 0
    finally:
        if lock_fd is not None:
            _release_lock(lock_fd, lock_path)


if __name__ == '__main__':
    raise SystemExit(main())
