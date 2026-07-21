from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from domain.domain.daily_decision_brief import decide_daily_brief_notification, diff_daily_decision_briefs
from domain.domain.intermediate_objects import SchemaValidationError, SnapshotDTO
from domain.domain.multi_tick import cash_footer_for_account, evaluate_dnd_quiet_hours
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
from src.application.daily_decision_brief_renderer import (
    render_candidate_alert,
    render_fixed_failure,
    render_fixed_report,
    resolve_daily_brief_render_limits,
)
from src.application.daily_decision_brief_repository import (
    confirm_daily_decision_brief_delivery_v2,
    persist_daily_decision_brief_success,
    prepare_daily_decision_brief_delivery,
    read_retryable_daily_decision_brief_delivery,
    record_daily_decision_brief_candidates,
    record_daily_decision_brief_delivery_attempt,
)
from src.application.daily_decision_brief_service import assemble_daily_decision_briefs
from src.application.multi_tick.cash_footer import query_cash_footer
from src.application.multi_tick.misc import _safe_runlog_data, parse_hhmm
from src.application.multi_tick.assistant_perception_event import build_notification_perception_event
from src.application.multi_tick.notify_format import build_account_message, build_account_message_compact
from src.application.multi_tick_finalization import (
    finalize_multi_tick_run,
    finalize_no_account_notification,
)
from src.application.notification_delivery_route import resolve_notification_delivery_route
from src.application.notification_delivery_adapter import (
    build_notification_transport_key,
    select_notification_delivery_adapter,
)
from src.application.scheduled_notification import (
    PreparedPerAccountMessages,
    build_notify_failure_summary_message,
    build_per_account_delivery_batch,
    execute_per_account_delivery,
    mark_no_candidate_notification_metrics,
    prepare_multi_account_notification,
    send_account_message_with_retry,
)
from src.infrastructure.external_services import (
    run_scan_scheduler_cli,
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
    markets_to_run: tuple[str, ...] | list[str] = ()
    scheduler_markets: tuple[str, ...] | list[str] = ()
    scheduler_decision: dict[str, Any] | None = None
    ran_pipeline_accounts: tuple[str, ...] | list[str] = ()
    account_ids: tuple[str, ...] | list[str] = ()
    scheduler_decisions_by_account: dict[str, dict[str, Any]] | None = None
    scheduled_scan_targets_by_account: dict[str, str | None] | None = None
    commit_scan_targets_fn: Callable[[dict[str, str | None]], None] | None = None
    delivery_only: bool = False
    trigger_kind: str = "scheduled"


@dataclass(frozen=True)
class DailyBriefNotificationPreparation:
    prepared_messages: Any
    lifecycles_by_account: dict[str, dict[str, Any]]
    delivery_keys_by_account: dict[str, str]
    markets: tuple[str, ...]
    multi_market_delivery_skipped: bool = False


def run_tick_notification_flow(request: TickNotificationRequest) -> int:
    process_root = (request.repo_root or request.base).resolve()
    _validate_scheduled_scan_targets(request)

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
    daily_brief_prep: DailyBriefNotificationPreparation | None = None
    if _daily_brief_enabled(request.base_cfg):
        daily_brief_prep = _prepare_daily_brief_notification(request)
        prepared_messages = daily_brief_prep.prepared_messages
        notify_candidates: list[Any] = []
        results_count = len(request.results)
    else:
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
        prepared_messages = notification_prep.prepared_messages
        notify_candidates = notification_prep.notify_candidates
        results_count = notification_prep.results_count

    _commit_scan_targets_before_delivery(request)

    request.runlog.safe_event(
        "notify",
        "prepare",
        data=_safe_runlog_data(
            {
                "results_count": results_count,
                "notify_candidates": len(notify_candidates),
            }
        ),
    )
    account_messages = prepared_messages.messages_by_account

    route_hint = _notification_perception_route_hint(request.base_cfg)

    if daily_brief_prep is not None and daily_brief_prep.multi_market_delivery_skipped:
        _record_daily_brief_multi_market_skip(request, daily_brief_prep)
        request.audit_helper.guard_mark_success()
        request.complete_tick_idempotency_fn(
            status="skipped",
            message="daily_brief_multi_market_delivery_skipped",
        )
        return 0

    if not bool(prepared_messages.threshold_met):
        _audit_notification_perception(
            request,
            build_notification_perception_event(
                event_kind="notification_prepared",
                run_id=request.run_id,
                results_count=results_count,
                notify_candidates=notify_candidates,
                account_messages=account_messages,
                threshold_met=prepared_messages.threshold_met,
                used_heartbeat=prepared_messages.used_heartbeat,
                heartbeat_accounts=prepared_messages.heartbeat_accounts,
                provider=route_hint.get("provider"),
                channel=route_hint.get("channel"),
                target=route_hint.get("target"),
                no_send=request.no_send,
            ),
        )
        _audit_notification_perception(
            request,
            build_notification_perception_event(
                event_kind="no_account_notification",
                run_id=request.run_id,
                results_count=results_count,
                notify_candidates=notify_candidates,
                account_messages=account_messages,
                threshold_met=prepared_messages.threshold_met,
                used_heartbeat=prepared_messages.used_heartbeat,
                heartbeat_accounts=prepared_messages.heartbeat_accounts,
                provider=route_hint.get("provider"),
                channel=route_hint.get("channel"),
                target=route_hint.get("target"),
                no_send=request.no_send,
            ),
        )
        if daily_brief_prep is not None:
            if request.delivery_only:
                request.runlog.safe_event("run_end", "skip", message="no_retryable_delivery")
            request.audit_helper.guard_mark_success()
            request.complete_tick_idempotency_fn(
                status="skipped",
                message="no_retryable_delivery" if request.delivery_only else "no_daily_brief_delivery",
            )
            return 0
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

    notify_route = resolve_notification_delivery_route(config=request.base_cfg)
    notif_cfg = notify_route.get("notifications") or {}
    provider = notify_route.get("provider")
    channel = notify_route.get("channel")
    target = notify_route.get("target")
    perception_scope = _notification_perception_conversation_scope(
        provider=provider,
        channel=channel,
        target=target,
        base=process_root,
        notifications=notif_cfg,
    )
    _audit_notification_perception(
        request,
        build_notification_perception_event(
            event_kind="notification_prepared",
            run_id=request.run_id,
            results_count=results_count,
            notify_candidates=notify_candidates,
            account_messages=account_messages,
            threshold_met=prepared_messages.threshold_met,
            used_heartbeat=prepared_messages.used_heartbeat,
            heartbeat_accounts=prepared_messages.heartbeat_accounts,
            provider=provider,
            channel=channel,
            target=target,
            no_send=request.no_send,
            conversation_scope=perception_scope,
        ),
    )
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
    _audit_notification_perception(
        request,
        build_notification_perception_event(
            event_kind="notification_delivery_decided",
            run_id=request.run_id,
            results_count=results_count,
            notify_candidates=notify_candidates,
            account_messages=account_messages,
            threshold_met=prepared_messages.threshold_met,
            used_heartbeat=prepared_messages.used_heartbeat,
            heartbeat_accounts=prepared_messages.heartbeat_accounts,
            provider=provider,
            channel=channel,
            target=target,
            no_send=request.no_send,
            quiet_hours=quiet_hours,
            delivery_decision=notify_delivery,
            conversation_scope=perception_scope,
        ),
    )
    if str(notify_delivery.get("action") or "") == "skip_quiet_hours":
        quiet_window = str(notify_delivery.get("quiet_window") or "")
        request.runlog.safe_event("notify", "skip", message=f"in quiet hours ({quiet_window})")
        print(f"[SKIP] Currently in quiet hours (DND). Target was: {target}")
        _audit_notification_perception(
            request,
            build_notification_perception_event(
                event_kind="quiet_hours_skipped",
                run_id=request.run_id,
                results_count=results_count,
                notify_candidates=notify_candidates,
                account_messages=account_messages,
                threshold_met=prepared_messages.threshold_met,
                used_heartbeat=prepared_messages.used_heartbeat,
                heartbeat_accounts=prepared_messages.heartbeat_accounts,
                provider=provider,
                channel=channel,
                target=target,
                no_send=request.no_send,
                quiet_hours=quiet_hours,
                delivery_decision=notify_delivery,
                conversation_scope=perception_scope,
            ),
        )
        request.audit_helper.guard_mark_success()
        request.complete_tick_idempotency_fn(status="skipped", message="quiet_hours")
        return 0

    sent_accounts: list[str] = []
    notify_failures: list[dict[str, object]] = []
    send_attempted_count = 0
    send_confirmed_count = 0
    retry_attempt_count = 0
    ambiguous_send_count = 0
    duplicate_risk_count = 0
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
            idempotency_keys_by_account=(
                daily_brief_prep.delivery_keys_by_account
                if daily_brief_prep is not None
                else None
            ),
        )
        sent_accounts = list(execution.sent_accounts)
        notify_failures = list(execution.notify_failures)
        confirmation_failures: list[dict[str, object]] = []
        if daily_brief_prep is not None:
            sent_accounts, confirmation_failures = _confirm_daily_brief_execution(
                request=request,
                preparation=daily_brief_prep,
                execution=execution,
            )
            notify_failures.extend(confirmation_failures)
        send_attempted_count = execution.send_attempted_count
        send_confirmed_count = (
            len(sent_accounts)
            if daily_brief_prep is not None
            else execution.send_confirmed_count
        )
        retry_attempt_count = execution.retry_attempt_count
        ambiguous_send_count = execution.ambiguous_send_count + sum(
            1 for item in confirmation_failures if bool(item.get("ambiguous_send"))
        )
        duplicate_risk_count = execution.duplicate_risk_count + sum(
            1 for item in confirmation_failures if bool(item.get("duplicate_risk"))
        )
        if notify_failures and daily_brief_prep is None:
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

    _audit_notification_perception(
        request,
        build_notification_perception_event(
            event_kind="notification_delivery_completed",
            run_id=request.run_id,
            results_count=results_count,
            notify_candidates=notify_candidates,
            account_messages=account_messages,
            threshold_met=prepared_messages.threshold_met,
            used_heartbeat=prepared_messages.used_heartbeat,
            heartbeat_accounts=prepared_messages.heartbeat_accounts,
            provider=provider,
            channel=channel,
            target=target,
            no_send=request.no_send,
            quiet_hours=quiet_hours,
            delivery_decision=notify_delivery,
            conversation_scope=perception_scope,
            sent_accounts=sent_accounts,
            notify_failures=notify_failures,
            send_attempted_count=send_attempted_count,
            send_confirmed_count=send_confirmed_count,
        ),
    )

    if request.delivery_only:
        if notify_failures:
            request.runlog.safe_event("run_end", "error", error_code="NOTIFY_FAILED")
            return 1
        request.runlog.safe_event(
            "run_end",
            "ok",
            data=_safe_runlog_data({"sent_accounts": sent_accounts, "delivery_only": True}),
        )
        request.audit_helper.guard_mark_success()
        request.complete_tick_idempotency_fn(
            status="completed" if sent_accounts else "skipped",
            message="delivery_only_sent" if sent_accounts else "delivery_only_no_send",
        )
        return 0

    if not request.no_send and daily_brief_prep is None:
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
        retry_attempt_count=retry_attempt_count,
        ambiguous_send_count=ambiguous_send_count,
        duplicate_risk_count=duplicate_risk_count,
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


def _validate_scheduled_scan_targets(request: TickNotificationRequest) -> None:
    if request.delivery_only or str(request.trigger_kind or "scheduled").strip().lower() != "scheduled":
        return
    targets = {
        str(account or "").strip().lower(): str(target or "").strip()
        for account, target in (request.scheduled_scan_targets_by_account or {}).items()
        if str(account or "").strip()
    }
    scanned_accounts = {
        _daily_brief_result_account(result)
        for result in request.results
        if _daily_brief_result_value(result, "ran_scan") is True and _daily_brief_result_account(result)
    }
    missing = sorted(account for account in scanned_accounts if not targets.get(account))
    if not missing:
        return
    request.audit_helper.guard_mark_failure("SCHEDULED_SCAN_TARGET_MISSING", "validate_scan_targets")
    request.audit_helper.audit(
        "scheduler",
        "scheduled_scan_target_missing",
        run_id=request.run_id,
        status="error",
        extra={"accounts": missing},
    )
    raise RuntimeError(f"scheduled scan target missing for accounts: {', '.join(missing)}")


def _commit_scan_targets_before_delivery(request: TickNotificationRequest) -> None:
    if request.delivery_only or not request.scheduled_scan_targets_by_account:
        return
    targets = dict(request.scheduled_scan_targets_by_account)
    if str(request.trigger_kind or "scheduled").strip().lower() != "scheduled":
        targets = {str(account): None for account in targets}
    if request.commit_scan_targets_fn is None:
        raise RuntimeError("scheduled scan target commit callback is required")
    try:
        request.commit_scan_targets_fn(targets)
    except Exception as exc:
        request.audit_helper.guard_mark_failure("SCHEDULER_TARGET_COMMIT_FAILED", "commit_scan_targets")
        request.audit_helper.audit(
            "scheduler",
            "scan_target_commit_failed",
            run_id=request.run_id,
            status="error",
            message=str(exc),
            extra={"targets": targets},
        )
        raise


def _daily_brief_enabled(config: dict[str, Any]) -> bool:
    notifications = config.get("notifications") if isinstance(config, dict) else {}
    notification_cfg = notifications if isinstance(notifications, dict) else {}
    daily_cfg = notification_cfg.get("daily_brief")
    return isinstance(daily_cfg, dict) and daily_cfg.get("enabled") is True


def _daily_brief_limits(config: dict[str, Any]) -> dict[str, Any]:
    notifications = config.get("notifications") if isinstance(config, dict) else {}
    notification_cfg = notifications if isinstance(notifications, dict) else {}
    daily_cfg = notification_cfg.get("daily_brief")
    return dict(daily_cfg) if isinstance(daily_cfg, dict) else {}


def _prepare_daily_brief_notification(
    request: TickNotificationRequest,
) -> DailyBriefNotificationPreparation:
    markets = tuple(
        dict.fromkeys(
            str(value or "").strip().upper()
            for value in (request.markets_to_run or request.scheduler_markets or ())
            if str(value or "").strip()
        )
    )
    if not markets:
        raise ValueError("daily brief requires at least one scheduler market")

    multi_market = len(markets) != 1
    lifecycles_by_account: dict[str, dict[str, Any]] = {}
    delivery_keys_by_account: dict[str, str] = {}
    messages_by_account: dict[str, str] = {}
    lifecycle_audit: list[dict[str, Any]] = []
    daily_limits = resolve_daily_brief_render_limits(_daily_brief_limits(request.base_cfg))

    if request.delivery_only:
        if not multi_market:
            market = markets[0]
            for account in _daily_brief_request_accounts(request):
                scheduler = _daily_brief_scheduler_decision(request, account)
                if scheduler.get("in_run_window") is not True:
                    continue
                market_date = _daily_brief_market_date(scheduler)
                if not market_date:
                    continue
                retry = read_retryable_daily_decision_brief_delivery(
                    base=request.base,
                    account=account,
                    market=market,
                    market_trading_date=market_date,
                )
                envelope = retry.get("envelope")
                if not isinstance(envelope, dict):
                    continue
                messages_by_account[account] = str(envelope["rendered_message"])
                delivery_keys_by_account[account] = str(envelope["delivery_key"])
                lifecycles_by_account[account] = {
                    "envelope": envelope,
                    "market": market,
                    "market_trading_date": market_date,
                    "retry_reason": retry.get("reason"),
                }
                lifecycle_audit.append(_daily_brief_envelope_audit(account, market, envelope, retry=True))
    else:
        ran_pipeline_accounts = {
            str(value or "").strip().lower()
            for value in request.ran_pipeline_accounts
            if str(value or "").strip()
        }
        scan_targets = {
            str(account).strip().lower(): str(target).strip()
            for account, target in (request.scheduled_scan_targets_by_account or {}).items()
            if str(account).strip() and target and str(target).strip()
        }
        scheduled_trigger = str(request.trigger_kind or "scheduled").strip().lower() == "scheduled"
        for result in request.results:
            account = _daily_brief_result_account(result)
            if not account:
                raise ValueError("daily brief account result is missing account")
            ran_scan = bool(
                account in scan_targets
                or account in ran_pipeline_accounts
                or _daily_brief_result_value(result, "ran_scan") is True
            )
            if not ran_scan:
                continue
            scheduler = _daily_brief_scheduler_decision(request, account)
            briefs = assemble_daily_decision_briefs(
                base=request.base,
                run_id=request.run_id,
                account=account,
                markets_to_run=list(markets),
                scheduler_decision=scheduler,
                account_result=result,
                pipeline_succeeded=account in ran_pipeline_accounts,
                config=request.base_cfg,
            )
            for market in markets:
                brief = briefs.get(market)
                if brief is None:
                    raise ValueError(f"daily brief assembler did not return market {market} for {account}")
                fixed_target = (
                    str(scheduler.get("scheduled_target_market") or "").strip()
                    if request.trigger_kind == "scheduled"
                    else ""
                )
                reliable = bool(
                    account in ran_pipeline_accounts
                    and brief.get("status") in {"ready", "degraded"}
                    and brief.get("actionability") != "blocked"
                )
                pending: list[str] = []
                persisted: dict[str, Any] | None = None
                diff: dict[str, Any] = {}
                failure_source: tuple[str, str] | None = None
                if reliable:
                    persisted = persist_daily_decision_brief_success(base=request.base, brief=brief)
                    previous = persisted.get("previous_successful_brief")
                    if isinstance(previous, dict) and previous.get("market_trading_date") == persisted["brief"]["market_trading_date"]:
                        diff = diff_daily_decision_briefs(previous, persisted["brief"])
                    if scheduled_trigger:
                        recorded = record_daily_decision_brief_candidates(
                            base=request.base,
                            account=account,
                            market=market,
                            market_trading_date=str(persisted["brief"]["market_trading_date"]),
                            revision=int(persisted["current_revision"]),
                            brief_digest=str(persisted["current_brief_digest"]),
                            candidate_identities=persisted["current_candidate_identities"],
                        )
                        pending = list(recorded["pending_candidate_identities"])
                else:
                    failure_source = _write_daily_brief_failure_artifact(
                        request=request,
                        account=account,
                        market=market,
                        brief=brief,
                        result=result,
                    )

                decision = (
                    decide_daily_brief_notification(
                        ran_scan=True,
                        pipeline_reliable=reliable,
                        fixed_due=bool(fixed_target),
                        pending_candidate_identities=pending,
                    )
                    if scheduled_trigger
                    else {"action": "none", "reason": "non_scheduled_snapshot_only"}
                )
                action = str(decision["action"])
                existing_retry = None
                if scheduled_trigger and not multi_market:
                    existing_retry = read_retryable_daily_decision_brief_delivery(
                        base=request.base,
                        account=account,
                        market=market,
                        market_trading_date=str(brief["market_trading_date"]),
                    )
                should_prepare = not (
                    action == "candidate_alert"
                    and isinstance(existing_retry, dict)
                    and isinstance(existing_retry.get("envelope"), dict)
                )
                if not multi_market and not request.no_send and should_prepare and action in {"fixed_report", "candidate_alert", "fixed_failure"}:
                    render_context = _daily_brief_render_context(request, scheduler_decision=scheduler)
                    if action == "fixed_failure":
                        message = render_fixed_failure(
                            brief,
                            context=render_context,
                        )
                        assert failure_source is not None
                        prepared = prepare_daily_decision_brief_delivery(
                            base=request.base,
                            account=account,
                            market=market,
                            market_trading_date=str(brief["market_trading_date"]),
                            run_id=request.run_id,
                            delivery_kind="fixed_failure",
                            source_kind="scan_failure",
                            source_digest=failure_source[1],
                            source_reference=failure_source[0],
                            scheduled_target_market=fixed_target,
                            rendered_message=message,
                            render_context=render_context,
                        )
                    else:
                        assert persisted is not None
                        identities = (
                            persisted["current_candidate_identities"]
                            if action == "fixed_report"
                            else pending
                        )
                        if action == "fixed_report":
                            message = render_fixed_report(
                                persisted["brief"],
                                limits=daily_limits,
                                context=render_context,
                            )
                        else:
                            message = render_candidate_alert(
                                persisted["brief"],
                                identities,
                                limits=daily_limits,
                                context=render_context,
                            )
                        prepared = prepare_daily_decision_brief_delivery(
                            base=request.base,
                            account=account,
                            market=market,
                            market_trading_date=str(persisted["brief"]["market_trading_date"]),
                            run_id=request.run_id,
                            delivery_kind=action,
                            source_kind="successful_brief",
                            revision=int(persisted["current_revision"]),
                            source_digest=str(persisted["current_brief_digest"]),
                            scheduled_target_market=(fixed_target or None),
                            candidate_identities=identities,
                            rendered_message=message,
                            render_context=render_context,
                        )

                retry = None
                if scheduled_trigger and not multi_market:
                    retry = read_retryable_daily_decision_brief_delivery(
                        base=request.base,
                        account=account,
                        market=market,
                        market_trading_date=str(brief["market_trading_date"]),
                    )
                    envelope = retry.get("envelope")
                    if isinstance(envelope, dict):
                        messages_by_account[account] = str(envelope["rendered_message"])
                        delivery_keys_by_account[account] = str(envelope["delivery_key"])
                        lifecycles_by_account[account] = {
                            "brief": persisted["brief"] if persisted is not None else brief,
                            "diff": diff,
                            "delivery_kind": "full" if envelope["delivery_kind"].startswith("fixed_") else "delta",
                            "delivery_key": envelope["delivery_key"],
                            "envelope": envelope,
                            "market": market,
                            "market_trading_date": str(brief["market_trading_date"]),
                        }
                selected_envelope = retry.get("envelope") if isinstance(retry, dict) else None
                lifecycle_audit.append(
                    {
                        "account": account,
                        "market": market,
                        "market_trading_date": str(brief["market_trading_date"]),
                        "brief_id": (persisted or {}).get("brief", brief).get("brief_id"),
                        "pipeline_reliable": reliable,
                        "decision": action,
                        "decision_reason": decision["reason"],
                        "fixed_target": fixed_target or None,
                        "pending_candidate_count": len(pending),
                        "retry_reason": retry.get("reason") if isinstance(retry, dict) else None,
                        "delivery_key": selected_envelope.get("delivery_key") if isinstance(selected_envelope, dict) else None,
                        "message_sha256": selected_envelope.get("message_sha256") if isinstance(selected_envelope, dict) else None,
                        "message_chars": len(str(selected_envelope.get("rendered_message") or "")) if isinstance(selected_envelope, dict) else 0,
                        "render_limits": dict(daily_limits),
                    }
                )

    request.tick_metrics["daily_brief"] = {
        "enabled": True,
        "delivery_only": bool(request.delivery_only),
        "markets": list(markets),
        "prepared": lifecycle_audit,
        "multi_market_delivery_skipped": multi_market,
    }
    for item in lifecycle_audit:
        request.audit_helper.audit(
            "daily_brief",
            "prepared",
            run_id=request.run_id,
            account=str(item["account"]),
            status="ok",
            extra={key: value for key, value in item.items() if key != "account"},
        )
    return DailyBriefNotificationPreparation(
        prepared_messages=PreparedPerAccountMessages(
            messages_by_account=messages_by_account,
            threshold_met=bool(messages_by_account),
            used_heartbeat=False,
            heartbeat_accounts=(),
        ),
        lifecycles_by_account=lifecycles_by_account,
        delivery_keys_by_account=delivery_keys_by_account,
        markets=markets,
        multi_market_delivery_skipped=multi_market,
    )


def _daily_brief_request_accounts(request: TickNotificationRequest) -> list[str]:
    values = request.account_ids or [_daily_brief_result_account(item) for item in request.results]
    return list(dict.fromkeys(str(value or "").strip().lower() for value in values if str(value or "").strip()))


def _daily_brief_result_account(result: Any) -> str:
    return str(_daily_brief_result_value(result, "account") or "").strip().lower()


def _daily_brief_result_value(result: Any, field: str) -> Any:
    return result.get(field) if isinstance(result, dict) else getattr(result, field, None)


def _daily_brief_scheduler_decision(request: TickNotificationRequest, account: str) -> dict[str, Any]:
    raw = (request.scheduler_decisions_by_account or {}).get(account)
    if isinstance(raw, dict):
        return raw
    return dict(request.scheduler_decision or {})


def _daily_brief_market_date(scheduler: dict[str, Any]) -> str | None:
    value = str(scheduler.get("now_market") or scheduler.get("scheduled_scan_target_market") or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def _write_daily_brief_failure_artifact(
    *, request: TickNotificationRequest, account: str, market: str, brief: dict[str, Any], result: Any
) -> tuple[str, str]:
    payload = {
        "schema_version": "daily_decision_brief_scan_failure.v1",
        "run_id": request.run_id,
        "account": account,
        "market": market,
        "market_trading_date": brief.get("market_trading_date"),
        "recorded_at_utc": utc_now(),
        "decision_reason": _daily_brief_result_value(result, "decision_reason"),
        "status": brief.get("status"),
        "actionability": brief.get("actionability"),
        "data_gaps": brief.get("data_gaps") or [],
    }
    path = state_repo.write_account_run_state(
        request.base, request.run_id, account, f"daily_decision_brief_failure.{market}.json", payload
    )
    relative = path.resolve().relative_to(request.base.resolve()).as_posix()
    return relative, hashlib.sha256(path.read_bytes()).hexdigest()


def _daily_brief_envelope_audit(account: str, market: str, envelope: dict[str, Any], *, retry: bool) -> dict[str, Any]:
    return {
        "account": account,
        "market": market,
        "market_trading_date": str(envelope.get("scheduled_target_market") or "")[:10],
        "delivery_kind": envelope.get("delivery_kind"),
        "delivery_key": envelope.get("delivery_key"),
        "message_sha256": envelope.get("message_sha256"),
        "retry": retry,
    }

def _daily_brief_render_context(
    request: TickNotificationRequest,
    *,
    scheduler_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    schedule = request.base_cfg.get(request.scheduler_schedule_key) if isinstance(request.base_cfg, dict) else {}
    schedule_map = schedule if isinstance(schedule, dict) else {}
    scheduler = scheduler_decision or (request.scheduler_decision if isinstance(request.scheduler_decision, dict) else {})
    user_timezone = getattr(request.bj_tz, "key", None) or str(request.bj_tz)
    trigger_kind = str(request.trigger_kind or "scheduled").strip().lower()
    return {
        "trigger_kind": trigger_kind,
        "scheduled_target_market": (
            (scheduler.get("scheduled_target_market") or scheduler.get("scheduled_scan_target_market"))
            if trigger_kind == "scheduled"
            else None
        ),
        "market_timezone": str(schedule_map.get("timezone") or "").strip(),
        "user_timezone": str(user_timezone or "Asia/Shanghai"),
        "user_timezone_label": "北京",
    }


def _record_daily_brief_multi_market_skip(
    request: TickNotificationRequest,
    preparation: DailyBriefNotificationPreparation,
) -> None:
    markets = list(preparation.markets)
    request.runlog.safe_event(
        "daily_brief",
        "skip",
        message="daily_brief_multi_market_delivery_skipped",
        data=_safe_runlog_data({"markets": markets}),
    )
    request.audit_helper.audit(
        "daily_brief",
        "daily_brief_multi_market_delivery_skipped",
        run_id=request.run_id,
        status="skip",
        extra={"markets": markets, "delivery_pointer_advanced": False},
    )


def _confirm_daily_brief_execution(
    *,
    request: TickNotificationRequest,
    preparation: DailyBriefNotificationPreparation,
    execution: Any,
) -> tuple[list[str], list[dict[str, object]]]:
    confirmed_accounts: list[str] = []
    failures: list[dict[str, object]] = []
    for send_result in execution.send_results:
        if not isinstance(send_result, dict):
            continue
        account = str(send_result.get("account") or "").strip().lower()
        lifecycle = preparation.lifecycles_by_account.get(account)
        if lifecycle is None:
            continue
        envelope = lifecycle.get("envelope")
        if not isinstance(envelope, dict):
            continue
        common = {
            "base": request.base,
            "account": account,
            "market": str(lifecycle["market"]),
            "market_trading_date": str(lifecycle["market_trading_date"]),
            "delivery_key": str(envelope["delivery_key"]),
            "source_digest": str(envelope["source_digest"]),
            "message_sha256": str(envelope["message_sha256"]),
            "transport_idempotency_key": str(send_result.get("idempotency_key") or ""),
        }
        try:
            if bool(send_result.get("ok")) and bool(send_result.get("delivery_confirmed")):
                confirmation = confirm_daily_decision_brief_delivery_v2(
                    **common,
                    confirmed_at_utc=utc_now(),
                )
                confirmed_accounts.append(account)
                request.audit_helper.audit(
                    "daily_brief",
                    "delivery_confirmed",
                    run_id=request.run_id,
                    account=account,
                    status="ok",
                    extra={
                        "delivery_kind": envelope["delivery_kind"],
                        "delivery_key": envelope["delivery_key"],
                        "confirmation_reason": confirmation.get("reason"),
                    },
                )
                continue
            record_daily_decision_brief_delivery_attempt(
                **common,
                ambiguous=bool(
                    send_result.get("ambiguous_send")
                    or send_result.get("duplicate_risk")
                    or send_result.get("command_ok")
                ),
                attempted_at_utc=utc_now(),
            )
        except Exception as exc:
            if bool(send_result.get("delivery_confirmed")):
                try:
                    record_daily_decision_brief_delivery_attempt(
                        **common,
                        ambiguous=True,
                        attempted_at_utc=utc_now(),
                    )
                except Exception:
                    pass
            request.audit_helper.guard_mark_failure(
                "DAILY_BRIEF_CONFIRM_FAILED",
                "confirm_daily_decision_brief_delivery_v2",
            )
            failures.append(
                {
                    "account": account,
                    "error_code": "DAILY_BRIEF_CONFIRM_FAILED",
                    "attempts": int(send_result.get("attempts") or 1),
                    "final_returncode": int(send_result.get("final_returncode") or 0),
                    "message_id": send_result.get("message_id"),
                    "upstream_message_id": send_result.get("upstream_message_id"),
                    "command_ok": bool(send_result.get("command_ok")),
                    "delivery_confirmed": bool(send_result.get("delivery_confirmed")),
                    "provider_response_code": send_result.get("provider_response_code"),
                    "idempotency_key": send_result.get("idempotency_key"),
                    "local_receipt_id": send_result.get("local_receipt_id"),
                    "retry_attempt_count": int(send_result.get("retry_attempt_count") or 0),
                    "ambiguous_send": True,
                    "duplicate_risk": True,
                    "exception_type": type(exc).__name__,
                    "error_message": str(exc),
                }
            )
    return confirmed_accounts, failures

def _audit_notification_perception(request: TickNotificationRequest, event: dict[str, Any]) -> None:
    try:
        request.audit_helper.audit(
            "assistant_perception",
            str(event.get("event_kind") or "notification_event"),
            run_id=request.run_id,
            status="ok",
            extra=event,
        )
    except Exception:
        pass


def _notification_perception_route_hint(config: dict[str, Any]) -> dict[str, Any]:
    notifications = config.get("notifications") if isinstance(config, dict) else {}
    notif_cfg = notifications if isinstance(notifications, dict) else {}
    provider = notif_cfg.get("provider") or notif_cfg.get("channel") or "wechat_clawbot"
    channel = notif_cfg.get("channel") or provider
    return {
        "provider": provider,
        "channel": channel,
        "target": notif_cfg.get("target"),
    }


def _notification_perception_conversation_scope(
    *,
    provider: Any,
    channel: Any,
    target: Any,
    base: Path,
    notifications: dict[str, Any],
) -> dict[str, str | None]:
    if str(provider or "").strip() == "wechat_clawbot" or str(channel or "").strip().lower() == "wechat":
        try:
            from src.application.channels.wechat_clawbot.state import load_wechat_clawbot_binding
            from src.application.conversation_scope import wechat_window_conversation_id

            binding = load_wechat_clawbot_binding(base=base, target=str(target or ""), notifications=notifications)
            conversation_id = wechat_window_conversation_id(
                chat_key=getattr(binding, "chat_key", None),
                group_id=getattr(binding, "group_id", None),
                sender_id=getattr(binding, "to_user_id", None),
            )
            if conversation_id:
                return {"channel": "wechat", "conversation_id": conversation_id}
        except Exception:
            pass
    from src.application.conversation_scope import conversation_scope_from_notification_route

    return conversation_scope_from_notification_route(provider=provider, channel=channel, target=target)
