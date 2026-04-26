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
from domain.domain import (
    Decision,
    DeliveryPlan,
    SchemaValidationError,
    SnapshotDTO,
    ensure_runtime_canonical_config,
    markets_for_trading_day_guard as domain_markets_for_trading_day_guard,
    normalize_notify_subprocess_output,
    normalize_pipeline_subprocess_output,
    resolve_notification_route_from_config,
    resolve_scheduler_state_path,
)
from domain.domain.engine import (
    decide_notification_delivery,
    decide_notify_window_open,
    resolve_scheduler_decision,
)
from src.application.scheduled_notification import (
    build_scheduler_decision,
    evaluate_trading_day_guard,
    execute_single_account_delivery,
    execute_single_account_pipeline,
    infer_trading_day_guard_markets,
    prepare_single_account_delivery,
)
from src.application.cron_runtime import request_scheduler_update, write_last_run
from scripts.infra.service import (
    normalize_feishu_app_send_output,
    run_command,
    run_pipeline_script,
    run_scan_scheduler_cli,
    send_openclaw_message,
    send_feishu_app_message_process,
    trading_day_via_futu,
)

SCHEMA_VALIDATION_ERROR_CODE = "SCHEMA_VALIDATION_FAILED"
_DEFAULT_NOTIFY_NORMALIZER = normalize_notify_subprocess_output
_DEFAULT_OPENCLAW_SENDER = send_openclaw_message


def _infer_trading_day_guard_markets(cfg_obj: dict) -> list[str]:
    return infer_trading_day_guard_markets(cfg_obj, resolver=domain_markets_for_trading_day_guard)


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
        except (OSError, ValueError):
            # If unreadable/unparseable, prefer removing to avoid permanent deadlock
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass

    fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        os.write(fd, str(os.getpid()).encode('utf-8'))
    except BaseException:
        # Guarantee fd is closed if write fails
        os.close(fd)
        raise
    return fd


def _release_lock(fd: int, lock_path: Path):
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def _fail_schema_validation(*, base: Path, vpy: Path, last_run: Path, started: str, stage: str, exc: BaseException) -> None:
    msg = f"{stage}: {type(exc).__name__}: {exc}"
    try:
        write_last_run(
            sh=sh,
            cwd=base,
            vpy=vpy,
            last_run=last_run,
            status="error",
            stage="contract",
            reason=SCHEMA_VALIDATION_ERROR_CODE,
            details=msg,
            started_at=started,
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
    market_hint = "hk" if cfg.name == "config.hk.json" else "us" if cfg.name == "config.us.json" else "auto"
    ensure_runtime_canonical_config(
        cfg,
        market_hint,
        repo_base=base,
        require_sibling_external=True,
    )

    cfg_obj = json.loads(cfg.read_text(encoding='utf-8'))
    notify_route = resolve_notification_route_from_config(
        config=cfg_obj,
        cli_channel=args.channel,
        cli_target=args.target,
    )
    channel = notify_route.get('channel')
    target = notify_route.get('target')

    state_dir = Path(args.state_dir)
    if not state_dir.is_absolute():
        state_dir = (base / state_dir).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "config_source.json").write_text(
        json.dumps(
            {
                "config_source_path": str(cfg.resolve()),
                "config_name": cfg.name,
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

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
        write_last_run(
            sh=sh,
            cwd=base,
            vpy=vpy,
            last_run=last_run,
            status="skip",
            stage="lock",
            reason="locked (another run in progress)",
            started_at=started,
        )
        return 0

    try:
        # 1) scheduler decision
        sch = request_scheduler_update(
            runner=run_scan_scheduler_cli,
            vpy=vpy,
            base=base,
            config=cfg,
            state=state,
            jsonl=True,
            capture_output=True,
        )
        if sch.returncode != 0:
            write_last_run(
                sh=sh,
                cwd=base,
                vpy=vpy,
                last_run=last_run,
                status="error",
                stage="scheduler",
                details=(sch.stderr or sch.stdout or "").strip(),
                started_at=started,
            )
            sys.stderr.write(sch.stderr)
            raise SystemExit(sch.returncode)

        try:
            decision = build_scheduler_decision(
                scheduler_stdout=str(sch.stdout or ""),
                cfg_obj=cfg_obj,
                as_of_utc=utc_now(),
                snapshot_cls=SnapshotDTO,
                decision_cls=Decision,
                scheduler_resolver=resolve_scheduler_decision,
                notify_window_resolver=decide_notify_window_open,
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
            write_last_run(
                sh=sh,
                cwd=base,
                vpy=vpy,
                last_run=last_run,
                status="skip",
                stage="scheduler",
                reason=reason,
                started_at=started,
            )
            return 0

        # 1.5) trading day guard (multi-market)
        guard_results = evaluate_trading_day_guard(
            cfg_obj=cfg_obj,
            trading_day_guard=_trading_day_guard_for_market,
            market_resolver=_infer_trading_day_guard_markets,
        )
        guard_markets = [str(item.get("market") or "") for item in guard_results]

        false_markets = [str(r.get('market')) for r in guard_results if r.get('is_trading_day') is False]
        if false_markets and len(false_markets) == len(guard_markets):
            write_last_run(
                sh=sh,
                cwd=base,
                vpy=vpy,
                last_run=last_run,
                status="skip",
                stage="trading_day_guard",
                reason=f"non-trading day: {','.join(false_markets)}",
                started_at=started,
            )
            return 0

        try:
            pipe_result = execute_single_account_pipeline(
                run_pipeline=run_pipeline_script,
                normalize_pipeline_output=normalize_pipeline_subprocess_output,
                vpy=vpy,
                base=base,
                config=cfg,
                report_dir=report_dir,
                state_dir=state_dir,
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
        if not bool(pipe_result.payload.get("ok")):
            write_last_run(
                sh=sh,
                cwd=base,
                vpy=vpy,
                last_run=last_run,
                status="error",
                stage="pipeline",
                reason="pipeline failed",
                started_at=started,
            )
            return pipe_result.returncode

        text = notif.read_text(encoding='utf-8', errors='replace').strip() if notif.exists() else ''
        account_name = str(((cfg_obj.get("portfolio") or {}).get("account") or "default")).strip() or "default"
        try:
            prepared_delivery = prepare_single_account_delivery(
                account=account_name,
                notification_text=text,
                channel=channel,
                target=target,
                should_notify_window=bool(should_notify),
                as_of_utc=utc_now(),
                snapshot_cls=SnapshotDTO,
                decision_builder=decide_notification_delivery,
                delivery_plan_cls=DeliveryPlan,
            )
        except ValueError as err:
            raise SystemExit(f"[CONFIG_ERROR] {err}")
        except SchemaValidationError as e:
            _fail_schema_validation(
                base=base,
                vpy=vpy,
                last_run=last_run,
                started=started,
                stage="delivery_plan",
                exc=e,
            )
        delivery_decision = prepared_delivery.delivery_decision
        delivery_plan = prepared_delivery.delivery_plan
        if prepared_delivery.effective_target is not None:
            target = prepared_delivery.effective_target
        meaningful = bool(delivery_decision.get('meaningful'))

        if (delivery_plan is not None) and bool(delivery_plan.should_send):
            use_legacy_notify_adapter = (
                send_openclaw_message is not _DEFAULT_OPENCLAW_SENDER
                or normalize_notify_subprocess_output is not _DEFAULT_NOTIFY_NORMALIZER
            )
            try:
                send_result = execute_single_account_delivery(
                    delivery_plan=delivery_plan,
                    account_name=prepared_delivery.account_name,
                    send_message=(
                        send_openclaw_message
                        if use_legacy_notify_adapter
                        else lambda **kwargs: send_feishu_app_message_process(
                            **kwargs,
                            notifications=notify_route.get('notifications') or {},
                        )
                    ),
                    normalize_notify_output=(
                        normalize_notify_subprocess_output
                        if use_legacy_notify_adapter
                        else normalize_feishu_app_send_output
                    ),
                    mark_scheduler_notified=lambda: request_scheduler_update(
                        runner=run_scan_scheduler_cli,
                        vpy=vpy,
                        base=base,
                        config=cfg,
                        state=state,
                        capture_output=False,
                        mark_notified=True,
                    ),
                    base=base,
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
            if not send_result.ok:
                write_last_run(
                    sh=sh,
                    cwd=base,
                    vpy=vpy,
                    last_run=last_run,
                    status="error",
                    stage=("mark-notified" if send_result.error_code == "MARK_NOTIFIED_FAILED" else "send"),
                    reason=send_result.error_code,
                    details=send_result.details,
                    started_at=started,
                )
                return send_result.returncode
            write_last_run(
                sh=sh,
                cwd=base,
                vpy=vpy,
                last_run=last_run,
                status="ok",
                stage="send",
                reason="sent",
                details=send_result.details,
                started_at=started,
            )
            return 0

        # not sending
        write_last_run(
            sh=sh,
            cwd=base,
            vpy=vpy,
            last_run=last_run,
            status="ok",
            stage="pipeline",
            reason=reason,
            details=f"should_notify={should_notify} meaningful={meaningful}",
            started_at=started,
        )
        return 0
    finally:
        if lock_fd is not None:
            _release_lock(lock_fd, lock_path)


if __name__ == '__main__':
    raise SystemExit(main())
