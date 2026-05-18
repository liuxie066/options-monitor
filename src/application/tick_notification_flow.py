from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from domain.domain.intermediate_objects import SchemaValidationError, SnapshotDTO
from domain.domain.multi_tick import (
    cash_footer_for_account,
    evaluate_dnd_quiet_hours,
    resolve_notification_route_from_config,
)
from domain.domain.multi_tick_result import (
    build_account_messages,
    build_no_candidate_account_messages,
)
from domain.domain.engine import (
    build_failure_audit_fields,
    decide_notification_delivery,
    filter_notify_candidates as engine_filter_notify_candidates,
    rank_notify_candidates,
    resolve_multi_tick_engine_entrypoint,
)
from domain.storage.repositories import run_repo, state_repo
from src.application.account_config import cash_footer_accounts_from_config
from src.application.cron_runtime import (
    apply_notify_results_to_tick_metrics,
    build_notify_summary,
    mark_accounts_notified,
)
from src.application.multi_tick.cash_footer import query_cash_footer
from src.application.multi_tick.misc import _safe_runlog_data, parse_hhmm
from src.application.multi_tick.notify_format import build_account_message, build_account_message_compact
from src.application.multi_tick_finalization import (
    finalize_multi_tick_run,
    finalize_no_account_notification,
)
from src.application.scheduled_notification import (
    build_notify_failure_summary_message,
    build_per_account_delivery_batch,
    execute_per_account_delivery,
    mark_no_candidate_notification_metrics,
    prepare_multi_account_notification,
    send_account_message_with_retry,
)
from src.infrastructure.external_services import (
    run_scan_scheduler_cli,
    select_notification_delivery_adapter,
)
from src.infrastructure.io_utils import bj_now, read_json, utc_now


@dataclass(frozen=True)
class TickNotificationRequest:
    base: Path
    cfg_path: Path
    state_path: Path
    scheduler_schedule_key: str
    base_cfg: dict[str, Any]
    run_id: str
    runlog: Any
    results: list[Any]
    tick_metrics: dict[str, Any]
    no_send: bool
    bj_tz: ZoneInfo
    audit_helper: Any
    vpy: Path
    complete_tick_idempotency_fn: Callable[..., None]
    repo_root: Path | None = None


def run_tick_notification_flow(request: TickNotificationRequest) -> int:
    process_root = (request.repo_root or request.base).resolve()

    def finish_success(
        fn: Callable[[], int],
        *,
        status: str = "completed",
        message: str | None = None,
    ) -> int:
        rc = int(fn())
        if rc == 0:
            request.complete_tick_idempotency_fn(status=status, message=message)
        return rc

    now_bj = bj_now()
    try:
        notifications_cfg = request.base_cfg.get("notifications", {}) or {}
        render_style = str(notifications_cfg.get("render_style") or "compact").strip().lower()
        if render_style == "compact":
            build_account_message_fn = build_account_message_compact
        else:
            build_account_message_fn = build_account_message

        notification_prep = prepare_multi_account_notification(
            results=request.results,
            base=request.base,
            config_path=request.cfg_path,
            config=request.base_cfg,
            now_bj=now_bj,
            as_of_utc=utc_now(),
            filter_notify_candidates_fn=engine_filter_notify_candidates,
            rank_notify_candidates_fn=rank_notify_candidates,
            query_cash_footer_fn=query_cash_footer,
            cash_footer_accounts_from_config_fn=cash_footer_accounts_from_config,
            cash_footer_for_account_fn=cash_footer_for_account,
            build_account_message_fn=build_account_message_fn,
            build_account_messages_fn=build_account_messages,
            build_no_candidate_account_messages_fn=build_no_candidate_account_messages,
            snapshot_cls=SnapshotDTO,
            engine_entrypoint=resolve_multi_tick_engine_entrypoint,
        )
    except SchemaValidationError as exc:
        request.audit_helper.fail_schema_validation(stage="account_messages_snapshot", exc=exc, run_id=request.run_id)
        raise

    request.runlog.safe_event(
        "notify",
        "prepare",
        data=_safe_runlog_data(
            {
                "results_count": notification_prep.results_count,
                "notify_candidates": len(notification_prep.notify_candidates),
            }
        ),
    )
    prepared_messages = notification_prep.prepared_messages
    account_messages = prepared_messages.messages_by_account

    if not bool(prepared_messages.threshold_met):
        return finish_success(
            lambda: finalize_no_account_notification(
                base=request.base,
                run_id=request.run_id,
                runlog=request.runlog,
                results=request.results,
                tick_metrics=request.tick_metrics,
                no_send=request.no_send,
                state_repo=state_repo,
                utc_now_fn=utc_now,
                audit_fn=request.audit_helper.audit,
                safe_data_fn=_safe_runlog_data,
                on_success=request.audit_helper.guard_mark_success,
            ),
            status="completed",
            message="no_account_notification",
        )

    if prepared_messages.used_heartbeat:
        heartbeat_accounts = {
            str(account or "").strip().lower()
            for account in getattr(prepared_messages, "heartbeat_accounts", ())
            if str(account or "").strip()
        }
        heartbeat_account_messages = dict(account_messages)
        if heartbeat_accounts:
            heartbeat_account_messages = {
                account: message
                for account, message in account_messages.items()
                if str(account or "").strip().lower() in heartbeat_accounts
            }
        request.runlog.safe_event(
            "notify",
            "prepare",
            message="sending no-candidate monitor heartbeat",
            data=_safe_runlog_data({"accounts": list(heartbeat_account_messages.keys())}),
        )
        mark_no_candidate_notification_metrics(
            tick_metrics=request.tick_metrics,
            account_messages=heartbeat_account_messages,
        )

    notify_route = resolve_notification_route_from_config(config=request.base_cfg)
    notif_cfg = notify_route.get("notifications") or {}
    provider = notify_route.get("provider")
    channel = notify_route.get("channel")
    target = notify_route.get("target")
    quiet_hours = notif_cfg.get("quiet_hours_beijing")
    dnd_decision = evaluate_dnd_quiet_hours(
        quiet_hours=quiet_hours,
        no_send=request.no_send,
        now_bj_time=datetime.now(timezone.utc).astimezone(request.bj_tz).time(),
        parse_hhmm_fn=parse_hhmm,
    )
    parse_error = dnd_decision.get("parse_error")
    if parse_error:
        request.runlog.safe_event("notify", "error", message=f"failed to parse quiet_hours: {parse_error}")

    try:
        notify_delivery, delivery_batch, target = build_per_account_delivery_batch(
            channel=channel,
            target=target,
            account_messages=account_messages,
            no_send=request.no_send,
            is_quiet=bool(dnd_decision.get("is_quiet")),
            quiet_window=str(dnd_decision.get("quiet_window") or ""),
            decision_builder=decide_notification_delivery,
        )
    except ValueError as err:
        request.runlog.safe_event("notify", "error", error_code="CONFIG_ERROR", message=str(err))
        raise SystemExit(f"[CONFIG_ERROR] {err}") from err

    request.audit_helper.audit(
        "notify",
        "delivery_decision",
        run_id=request.run_id,
        status=("ok" if not notify_delivery.get("config_error") else "error"),
        target=(str(target) if target else None),
        extra={
            "reason": notify_delivery.get("reason"),
            "should_send": bool(notify_delivery.get("should_send")),
            "account_keys": list(account_messages.keys()),
            "account_count": len(account_messages),
            "account_messages_count": len(account_messages),
            "message_len_by_account": {str(acct): len(str(msg)) for acct, msg in account_messages.items()},
            "provider": str(provider) if provider else None,
            "channel": str(channel) if channel else None,
            "target_set": bool(target),
        },
    )
    if str(notify_delivery.get("action") or "") == "skip_quiet_hours":
        quiet_window = str(notify_delivery.get("quiet_window") or "")
        request.runlog.safe_event("notify", "skip", message=f"in quiet hours ({quiet_window})")
        print(f"[SKIP] Currently in quiet hours (DND). Target was: {target}")
        request.audit_helper.guard_mark_success()
        request.complete_tick_idempotency_fn(status="skipped", message="quiet_hours")
        return 0

    sent_accounts: list[str] = []
    notify_failures: list[dict[str, object]] = []
    send_attempted_count = 0
    send_confirmed_count = 0
    failure_summary_delivery: dict[str, object] | None = None
    if bool(notify_delivery.get("should_send")):
        assert delivery_batch is not None
        try:
            delivery_adapter = select_notification_delivery_adapter(provider)
        except ValueError as err:
            request.runlog.safe_event("notify", "error", error_code="CONFIG_ERROR", message=str(err))
            raise SystemExit(f"[CONFIG_ERROR] {err}") from err

        def _send_with_route_notifications(**kwargs: Any) -> Any:
            return delivery_adapter.send_fn(
                **kwargs,
                notifications=notify_route.get("notifications") or {},
            )

        execution = execute_per_account_delivery(
            delivery_batch=delivery_batch,
            run_id=request.run_id,
            runlog=request.runlog,
            audit_fn=request.audit_helper.audit,
            safe_data_fn=_safe_runlog_data,
            send_fn=_send_with_route_notifications,
            normalize_fn=delivery_adapter.normalize_fn,
            failure_fields_builder=build_failure_audit_fields,
            on_failure=lambda error_code: request.audit_helper.guard_mark_failure(
                error_code,
                delivery_adapter.failure_stage,
            ),
            base=process_root,
            failure_stage=delivery_adapter.failure_stage,
        )
        sent_accounts = execution.sent_accounts
        notify_failures = execution.notify_failures
        send_attempted_count = execution.send_attempted_count
        send_confirmed_count = execution.send_confirmed_count
        if notify_failures:
            failure_summary_result = send_account_message_with_retry(
                base=process_root,
                channel=delivery_batch.channel,
                target=delivery_batch.target,
                account="notify_failure_summary",
                message=build_notify_failure_summary_message(
                    run_id=request.run_id,
                    sent_accounts=sent_accounts,
                    notify_failures=notify_failures,
                ),
                run_id=request.run_id,
                runlog=request.runlog,
                audit_fn=request.audit_helper.audit,
                send_fn=_send_with_route_notifications,
                normalize_fn=delivery_adapter.normalize_fn,
                safe_data_fn=_safe_runlog_data,
                failure_fields_builder=build_failure_audit_fields,
                failure_stage=delivery_adapter.failure_stage,
                max_attempts=1,
                retry_delays_sec=(),
            )
            failure_summary_delivery = {
                "ok": bool(failure_summary_result.get("ok")),
                "error_code": failure_summary_result.get("error_code"),
                "attempts": int(failure_summary_result.get("attempts") or 0),  # pyright: ignore[reportArgumentType]
                "message_id": failure_summary_result.get("message_id"),
                "delivery_confirmed": bool(failure_summary_result.get("delivery_confirmed")),
            }
    else:
        sent_accounts = list(account_messages.keys())
        request.runlog.safe_event("notify", "skip", message="no_send mode")

    if not request.no_send:
        try:
            mark_accounts_notified(
                runner=run_scan_scheduler_cli,
                vpy=request.vpy,
                base=process_root,
                config=request.cfg_path,
                state=request.state_path,
                state_dir=run_repo.get_run_state_dir(request.base, request.run_id),
                schedule_key=str(request.scheduler_schedule_key),
                accounts=sent_accounts,
            )
        except Exception:
            pass

    notify_summary = build_notify_summary(
        sent_accounts=sent_accounts,
        notify_failures=notify_failures,
        total_accounts=len(account_messages),
        send_attempted_count=send_attempted_count,
        send_confirmed_count=send_confirmed_count,
    )
    try:
        apply_notify_results_to_tick_metrics(
            tick_metrics=request.tick_metrics,
            no_send=request.no_send,
            sent_accounts=sent_accounts,
            notify_failures=notify_failures,
            notify_summary=notify_summary,
        )
        if failure_summary_delivery is not None:
            request.tick_metrics["notify_failure_summary_delivery"] = failure_summary_delivery
        state_repo.write_tick_metrics(request.base, request.run_id, request.tick_metrics)
        state_repo.append_tick_metrics_history(request.base, request.run_id, request.tick_metrics)
        request.audit_helper.audit(
            "write",
            "write_tick_metrics",
            run_id=request.run_id,
            extra={"sent": bool(request.tick_metrics.get("sent"))},
        )
    except Exception:
        pass

    return finish_success(
        lambda: finalize_multi_tick_run(
            base=request.base,
            run_id=request.run_id,
            runlog=request.runlog,
            results=request.results,
            tick_metrics=request.tick_metrics,
            no_send=request.no_send,
            sent_accounts=sent_accounts,
            notify_failures=notify_failures,
            notify_summary=notify_summary,
            channel=(str(channel) if channel else None),
            target=(str(target) if target else None),
            state_repo=state_repo,
            read_json_fn=read_json,
            shared_state_dir_getter=state_repo.shared_state_dir,
            utc_now_fn=utc_now,
            audit_fn=request.audit_helper.audit,
            safe_data_fn=_safe_runlog_data,
            on_success=request.audit_helper.guard_mark_success,
        )
    )
