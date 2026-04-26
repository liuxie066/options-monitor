from __future__ import annotations

import json
from dataclasses import dataclass
from time import monotonic, sleep
from typing import Any, Callable

from domain.domain import (
    Decision,
    DeliveryPlan,
    SchemaValidationError,
    SnapshotDTO,
    markets_for_trading_day_guard as domain_markets_for_trading_day_guard,
)
from domain.domain.engine import (
    AccountSchedulerDecisionView,
    decide_notification_delivery,
    decide_notify_window_open,
    resolve_multi_tick_engine_entrypoint,
    resolve_scheduler_decision,
)


@dataclass(frozen=True)
class PreparedAccountMessages:
    account_messages: dict[str, str]
    threshold_met: bool
    used_heartbeat: bool


@dataclass(frozen=True)
class PreparedSingleAccountDelivery:
    account_name: str
    account_messages: dict[str, str]
    delivery_decision: dict[str, Any]
    delivery_plan: DeliveryPlan | None
    effective_target: str | None


@dataclass(frozen=True)
class PipelineExecutionResult:
    payload: dict[str, Any]
    returncode: int


@dataclass(frozen=True)
class SingleAccountSendResult:
    ok: bool
    error_code: str | None
    details: str
    returncode: int
    message_id: str | None


@dataclass(frozen=True)
class MultiAccountSendExecution:
    sent_accounts: list[str]
    notify_failures: list[dict[str, object]]


NOTIFY_SEND_MAX_ATTEMPTS = 1
NOTIFY_SEND_RETRY_DELAYS_SEC: tuple[float, ...] = ()


def _snapshot_payload_dict(
    *,
    snapshot_cls: type[SnapshotDTO],
    snapshot_name: str,
    as_of_utc: str,
    payload: dict[str, Any],
    key: str,
    error_message: str,
) -> dict[str, Any]:
    snapshot = snapshot_cls.from_payload(
        {
            "schema_kind": "snapshot_dto",
            "schema_version": "1.0",
            "snapshot_name": snapshot_name,
            "as_of_utc": as_of_utc,
            "payload": payload,
        }
    )
    value = snapshot.payload.get(key)
    if not isinstance(value, dict):
        raise SchemaValidationError(error_message)
    return value


def infer_trading_day_guard_markets(
    cfg_obj: dict,
    *,
    resolver: Callable[[list[str], dict, str], list[str]] = domain_markets_for_trading_day_guard,
) -> list[str]:
    return resolver([], cfg_obj, "auto")


def build_scheduler_decision(
    *,
    scheduler_stdout: str,
    cfg_obj: dict,
    as_of_utc: str,
    snapshot_cls: type[SnapshotDTO] = SnapshotDTO,
    decision_cls: type[Decision] = Decision,
    scheduler_resolver: Callable[[dict[str, Any]], tuple[dict[str, Any], Any]] = resolve_scheduler_decision,
    notify_window_resolver: Callable[..., bool] = decide_notify_window_open,
) -> Decision:
    scheduler_payload = _snapshot_payload_dict(
        snapshot_cls=snapshot_cls,
        snapshot_name="send_if_needed_scheduler_raw",
        as_of_utc=as_of_utc,
        payload={"scheduler_raw": json.loads((scheduler_stdout or "").strip())},
        key="scheduler_raw",
        error_message="scheduler_raw must be a dict",
    )

    scheduler_decision, scheduler_view = scheduler_resolver(scheduler_payload)
    snapshot_cls.from_payload(
        {
            "schema_kind": "snapshot_dto",
            "schema_version": "1.0",
            "snapshot_name": "send_if_needed_scheduler_decision",
            "as_of_utc": as_of_utc,
            "payload": {"scheduler_decision": scheduler_decision},
        }
    )
    return decision_cls.from_payload(
        {
            "schema_kind": "decision",
            "schema_version": "1.0",
            "account": str(((cfg_obj.get("portfolio") or {}).get("account") or "default")).strip() or "default",
            "should_run": bool(scheduler_view.should_run_scan),
            "should_notify": bool(notify_window_resolver(scheduler_decision=scheduler_view)),
            "reason": str(scheduler_view.reason),
        }
    )


def evaluate_trading_day_guard(
    *,
    cfg_obj: dict,
    trading_day_guard: Callable[[dict, str], tuple[bool | None, str]],
    market_resolver: Callable[[dict], list[str]] | None = None,
) -> list[dict[str, bool | str | None]]:
    results: list[dict[str, bool | str | None]] = []
    resolve_markets = market_resolver or infer_trading_day_guard_markets
    for market in resolve_markets(cfg_obj):
        is_trading_day, market_used = trading_day_guard(cfg_obj, market)
        results.append({"market": market_used, "is_trading_day": is_trading_day})
    return results


def build_multi_account_delivery(
    *,
    channel: str | None,
    target: str | None,
    account_messages: dict[str, str],
    should_notify_window: bool = True,
    no_send: bool = False,
    is_quiet: bool = False,
    quiet_window: str = "",
    decision_builder: Callable[..., dict[str, Any]] = decide_notification_delivery,
    delivery_plan_cls: type[DeliveryPlan] = DeliveryPlan,
) -> tuple[dict[str, Any], DeliveryPlan | None, str | None]:
    delivery_decision = decision_builder(
        should_notify_window=bool(should_notify_window),
        notification_text="\n".join(str(msg) for msg in account_messages.values()),
        target=target,
        no_send=no_send,
        is_quiet=is_quiet,
        quiet_window=quiet_window,
    )
    config_error = delivery_decision.get("config_error")
    if config_error:
        raise ValueError(str(config_error))

    effective_target = delivery_decision.get("effective_target")
    if not bool(delivery_decision.get("should_send")):
        return delivery_decision, None, effective_target

    delivery_plan = delivery_plan_cls.from_payload(
        {
            "schema_kind": "delivery_plan",
            "schema_version": "1.0",
            "channel": str(channel),
            "target": str(effective_target),
            "account_messages": account_messages,
            "should_send": True,
        }
    )
    return delivery_decision, delivery_plan, effective_target


def build_single_account_messages(
    *,
    account: str,
    notification_text: str,
) -> dict[str, str]:
    account_name = str(account or "").strip() or "default"
    text = str(notification_text or "")
    rendered = f"[{account_name}]\n{text}" if (account_name and text) else text
    return {account_name: rendered}


def snapshot_account_messages(
    *,
    account_messages: dict[str, str],
    as_of_utc: str,
    snapshot_cls: type[SnapshotDTO] = SnapshotDTO,
) -> dict[str, str]:
    snapshot = snapshot_cls.from_payload(
        {
            "schema_kind": "snapshot_dto",
            "schema_version": "1.0",
            "snapshot_name": "account_messages",
            "as_of_utc": as_of_utc,
            "payload": {"account_messages": account_messages},
        }
    )
    raw_account_messages = snapshot.payload.get("account_messages")
    if not isinstance(raw_account_messages, dict):
        raise SchemaValidationError("account_messages must be a dict")
    return {str(k): str(v) for k, v in raw_account_messages.items()}


def prepare_multi_account_messages(
    *,
    notify_candidates: list[Any],
    results: list[Any],
    now_bj: str,
    cash_footer_lines: list[str],
    cash_footer_for_account_fn: Callable[[list[str], str], list[str]],
    build_account_message_fn: Callable[..., str],
    build_account_messages_fn: Callable[..., dict[str, str]],
    build_no_candidate_account_messages_fn: Callable[..., dict[str, str]],
    as_of_utc: str,
    snapshot_cls: type[SnapshotDTO] = SnapshotDTO,
    engine_entrypoint: Callable[..., dict[str, Any]] = resolve_multi_tick_engine_entrypoint,
) -> PreparedAccountMessages:
    account_messages = build_account_messages_fn(
        notify_candidates=notify_candidates,
        now_bj=now_bj,
        cash_footer_lines=cash_footer_lines,
        cash_footer_for_account_fn=cash_footer_for_account_fn,
        build_account_message_fn=build_account_message_fn,
    )
    account_messages = snapshot_account_messages(
        account_messages=account_messages,
        as_of_utc=as_of_utc,
        snapshot_cls=snapshot_cls,
    )

    notify_threshold = engine_entrypoint(
        notify_account_messages=account_messages,
        notify_min_accounts=1,
    ).get("notify_threshold") or {}
    if bool(notify_threshold.get("threshold_met")):
        return PreparedAccountMessages(
            account_messages=account_messages,
            threshold_met=True,
            used_heartbeat=False,
        )

    account_messages = build_no_candidate_account_messages_fn(
        results=results,
        now_bj=now_bj,
        cash_footer_lines=cash_footer_lines,
        cash_footer_for_account_fn=cash_footer_for_account_fn,
    )
    notify_threshold = engine_entrypoint(
        notify_account_messages=account_messages,
        notify_min_accounts=1,
    ).get("notify_threshold") or {}
    return PreparedAccountMessages(
        account_messages=account_messages,
        threshold_met=bool(notify_threshold.get("threshold_met")),
        used_heartbeat=bool(notify_threshold.get("threshold_met")),
    )


def prepare_single_account_delivery(
    *,
    account: str,
    notification_text: str,
    channel: str | None,
    target: str | None,
    should_notify_window: bool,
    no_send: bool = False,
    is_quiet: bool = False,
    quiet_window: str = "",
    as_of_utc: str,
    snapshot_cls: type[SnapshotDTO] = SnapshotDTO,
    decision_builder: Callable[..., dict[str, Any]] = decide_notification_delivery,
    delivery_plan_cls: type[DeliveryPlan] = DeliveryPlan,
) -> PreparedSingleAccountDelivery:
    account_name = str(account or "").strip() or "default"
    account_messages = snapshot_account_messages(
        account_messages=build_single_account_messages(
            account=account_name,
            notification_text=notification_text,
        ),
        as_of_utc=as_of_utc,
        snapshot_cls=snapshot_cls,
    )
    delivery_decision, delivery_plan, effective_target = build_multi_account_delivery(
        channel=channel,
        target=target,
        account_messages=account_messages,
        should_notify_window=bool(should_notify_window),
        no_send=no_send,
        is_quiet=is_quiet,
        quiet_window=quiet_window,
        decision_builder=decision_builder,
        delivery_plan_cls=delivery_plan_cls,
    )
    return PreparedSingleAccountDelivery(
        account_name=account_name,
        account_messages=account_messages,
        delivery_decision=delivery_decision,
        delivery_plan=delivery_plan,
        effective_target=effective_target,
    )


def execute_single_account_pipeline(
    *,
    run_pipeline: Callable[..., Any],
    normalize_pipeline_output: Callable[..., dict[str, Any]],
    vpy,
    base,
    config,
    report_dir,
    state_dir,
) -> PipelineExecutionResult:
    pipe = run_pipeline(
        vpy=vpy,
        base=base,
        config=config,
        report_dir=report_dir,
        state_dir=state_dir,
    )
    payload = normalize_pipeline_output(
        returncode=int(pipe.returncode),
        stdout=str(pipe.stdout or ""),
        stderr=str(pipe.stderr or ""),
    )
    return PipelineExecutionResult(
        payload=payload,
        returncode=int(payload.get("returncode") or pipe.returncode or 0),
    )


def execute_single_account_delivery(
    *,
    delivery_plan: DeliveryPlan,
    account_name: str,
    send_message: Callable[..., Any],
    normalize_notify_output: Callable[..., dict[str, Any]],
    mark_scheduler_notified: Callable[[], Any],
    base,
) -> SingleAccountSendResult:
    send = send_message(
        base=base,
        channel=str(delivery_plan.channel),
        target=str(delivery_plan.target),
        message=str(delivery_plan.account_messages.get(account_name) or ""),
    )
    send_payload = _normalize_delivery_output(normalize_notify_output=normalize_notify_output, send=send)
    normalized_returncode = send_payload.get("returncode")
    resolved_returncode = int(send.returncode if normalized_returncode is None else normalized_returncode)
    message_id = send_payload.get("message_id")
    send_ok = bool(send_payload.get("ok") and message_id)
    if not send_ok:
        error_code = "SEND_UNCONFIRMED" if bool(send_payload.get("command_ok")) else "SEND_FAILED"
        failure_returncode = resolved_returncode
        if error_code == "SEND_UNCONFIRMED" and failure_returncode == 0:
            failure_returncode = 1
        return SingleAccountSendResult(
            ok=False,
            error_code=error_code,
            details=str(send_payload.get("message") or (send.stderr or send.stdout or "").strip()),
            returncode=failure_returncode,
            message_id=(None if message_id is None else str(message_id)),
        )

    mark = mark_scheduler_notified()
    if mark.returncode != 0:
        return SingleAccountSendResult(
            ok=False,
            error_code="MARK_NOTIFIED_FAILED",
            details="send ok but mark-notified failed",
            returncode=int(mark.returncode),
            message_id=(None if message_id is None else str(message_id)),
        )

    detail = "sent+marked"
    if message_id:
        detail += f" message_id={message_id}"
    return SingleAccountSendResult(
        ok=True,
        error_code=None,
        details=detail,
        returncode=0,
        message_id=(None if message_id is None else str(message_id)),
    )


def _notify_error_code(send_tool_dto: dict[str, Any]) -> str:
    return "SEND_UNCONFIRMED" if bool(send_tool_dto.get("command_ok")) else "SEND_FAILED"


def send_account_message_with_retry(
    *,
    base,
    channel: str,
    target: str,
    account: str,
    message: str,
    run_id: str,
    runlog,
    audit_fn,
    send_fn: Callable[..., Any],
    normalize_fn: Callable[..., dict[str, Any]],
    safe_data_fn: Callable[[dict[str, Any]], dict[str, Any]],
    failure_fields_builder: Callable[..., dict[str, Any]],
    sleep_fn: Callable[[float], Any] = sleep,
    max_attempts: int = NOTIFY_SEND_MAX_ATTEMPTS,
    retry_delays_sec: tuple[float, ...] = NOTIFY_SEND_RETRY_DELAYS_SEC,
) -> dict[str, object]:
    attempts = max(1, int(max_attempts or 1))
    final_record: dict[str, object] | None = None
    attempt_records: list[dict[str, object]] = []

    for attempt in range(1, attempts + 1):
        t_notify0 = monotonic()
        send = send_fn(
            base=base,
            channel=str(channel),
            target=str(target),
            message=message,
        )
        send_tool_dto = _normalize_delivery_output(normalize_notify_output=normalize_fn, send=send)
        message_id = send_tool_dto.get("message_id")
        ok = bool(send_tool_dto.get("ok") or (bool(send_tool_dto.get("command_ok")) and message_id))
        error_code = None if ok else _notify_error_code(send_tool_dto)
        record = {
            "account": account,
            "attempt": attempt,
            "max_attempts": attempts,
            "returncode": int(send.returncode),
            "message_id": message_id,
            "command_ok": bool(send_tool_dto.get("command_ok")),
            "delivery_confirmed": bool(ok),
            "stdout_tail": send_tool_dto.get("stdout_tail"),
            "stderr_tail": send_tool_dto.get("stderr_tail"),
        }
        attempt_records.append(record)
        final_record = record

        audit_extra = dict(record)
        if not ok:
            audit_extra.update(
                failure_fields_builder(
                    failure_kind="io_error",
                    failure_stage="send_openclaw_message",
                    failure_adapter=str(send_tool_dto.get("adapter") or "notify"),
                )
            )
        audit_fn(
            "notify",
            "send_openclaw_message",
            run_id=run_id,
            account=account,
            status=("ok" if ok else ("unconfirmed" if error_code == "SEND_UNCONFIRMED" else "error")),
            target=str(target),
            error_code=error_code,
            extra=audit_extra,
        )

        if ok:
            runlog.safe_event(
                "notify",
                "ok",
                duration_ms=int((monotonic() - t_notify0) * 1000),
                data=safe_data_fn({"channel": channel, **record}),
            )
            return {
                "ok": True,
                "account": account,
                "attempts": attempt,
                "attempt_records": attempt_records,
                "final": final_record,
            }

        runlog.safe_event(
            "notify",
            "error",
            duration_ms=int((monotonic() - t_notify0) * 1000),
            error_code=error_code,
            message=(f"message send unconfirmed ({account})" if error_code == "SEND_UNCONFIRMED" else f"message send failed ({account})"),
            data=safe_data_fn(record),
        )

        if attempt < attempts:
            delay = float(retry_delays_sec[min(attempt - 1, len(retry_delays_sec) - 1)] or 0.0) if retry_delays_sec else 0.0
            if delay > 0:
                sleep_fn(delay)

    final = final_record or {
        "account": account,
        "attempt": 0,
        "max_attempts": attempts,
        "returncode": 1,
        "message_id": None,
        "command_ok": False,
        "delivery_confirmed": False,
        "stdout_tail": "",
        "stderr_tail": "",
    }
    command_ok = bool(final.get("command_ok"))
    return {
        "ok": False,
        "account": account,
        "error_code": "SEND_UNCONFIRMED" if command_ok else "SEND_FAILED",
        "attempts": attempts,
        "attempt_records": attempt_records,
        "final": final,
        "final_returncode": int(final.get("returncode") or 0),
        "message_id": final.get("message_id"),
        "command_ok": command_ok,
        "delivery_confirmed": bool(final.get("delivery_confirmed")),
    }


def _normalize_delivery_output(*, normalize_notify_output: Callable[..., dict[str, Any]], send: Any) -> dict[str, Any]:
    try:
        return normalize_notify_output(send_result=(getattr(send, 'raw', send)))
    except TypeError:
        return normalize_notify_output(
            returncode=int(getattr(send, 'returncode', 0) or 0),
            stdout=str(getattr(send, 'stdout', '') or ''),
            stderr=str(getattr(send, 'stderr', '') or ''),
        )


def execute_multi_account_delivery(
    *,
    delivery_plan: DeliveryPlan,
    run_id: str,
    runlog,
    audit_fn,
    safe_data_fn: Callable[[dict[str, Any]], dict[str, Any]],
    send_fn: Callable[..., Any],
    normalize_fn: Callable[..., dict[str, Any]],
    failure_fields_builder: Callable[..., dict[str, Any]],
    on_failure: Callable[[str], Any] | None = None,
    base,
) -> MultiAccountSendExecution:
    sent_accounts: list[str] = []
    notify_failures: list[dict[str, object]] = []
    target = str(delivery_plan.target)
    channel = str(delivery_plan.channel)

    for acct, msg in delivery_plan.account_messages.items():
        runlog.safe_event(
            "notify",
            "start",
            data=safe_data_fn(
                {
                    "channel": channel,
                    "target_set": bool(target),
                    "account": acct,
                    "message_len": len(msg),
                }
            ),
        )
        send_result = send_account_message_with_retry(
            base=base,
            channel=channel,
            target=target,
            account=str(acct),
            message=msg,
            run_id=run_id,
            runlog=runlog,
            audit_fn=audit_fn,
            send_fn=send_fn,
            normalize_fn=normalize_fn,
            safe_data_fn=safe_data_fn,
            failure_fields_builder=failure_fields_builder,
        )
        if not bool(send_result.get("ok")):
            error_code = str(send_result.get("error_code") or "SEND_FAILED")
            if on_failure is not None:
                on_failure(error_code)
            notify_failures.append(
                {
                    "account": acct,
                    "error_code": error_code,
                    "attempts": int(send_result.get("attempts") or 1),
                    "final_returncode": int(send_result.get("final_returncode") or 0),
                    "message_id": send_result.get("message_id"),
                    "command_ok": bool(send_result.get("command_ok")),
                    "delivery_confirmed": bool(send_result.get("delivery_confirmed")),
                }
            )
            continue
        sent_accounts.append(acct)

    return MultiAccountSendExecution(
        sent_accounts=sent_accounts,
        notify_failures=notify_failures,
    )


def build_multi_tick_scheduler_decision(
    *,
    scheduler_stdout: str,
    as_of_utc: str,
    snapshot_cls: type[SnapshotDTO] = SnapshotDTO,
    engine_entrypoint: Callable[..., dict[str, Any]] = resolve_multi_tick_engine_entrypoint,
) -> tuple[dict[str, Any], Any]:
    scheduler_payload = _snapshot_payload_dict(
        snapshot_cls=snapshot_cls,
        snapshot_name="scheduler_raw",
        as_of_utc=as_of_utc,
        payload={"scheduler_raw": json.loads((scheduler_stdout or "").strip())},
        key="scheduler_raw",
        error_message="scheduler_raw must be a dict",
    )
    scheduler_bundle = engine_entrypoint(scheduler_raw=scheduler_payload).get("scheduler") or {}
    scheduler_decision = scheduler_bundle.get("scheduler_decision")
    scheduler_view = scheduler_bundle.get("scheduler_view")
    if not isinstance(scheduler_decision, dict) or scheduler_view is None:
        raise SchemaValidationError("scheduler decision engine entrypoint returned invalid payload")
    return scheduler_decision, scheduler_view


def build_multi_tick_account_scheduler_view(
    *,
    account: str,
    scheduler_stdout: str,
    scheduler_decision: dict[str, Any],
    as_of_utc: str,
    snapshot_cls: type[SnapshotDTO] = SnapshotDTO,
    engine_entrypoint: Callable[..., dict[str, Any]] = resolve_multi_tick_engine_entrypoint,
    account_view_cls: type[AccountSchedulerDecisionView] = AccountSchedulerDecisionView,
) -> AccountSchedulerDecisionView:
    account_scheduler_bundle = engine_entrypoint(
        scheduler_raw=scheduler_decision,
        account_scheduler_raw_by_account={str(account): json.loads((scheduler_stdout or "").strip())},
    ).get("scheduler") or {}
    _snapshot_payload_dict(
        snapshot_cls=snapshot_cls,
        snapshot_name=f"account_scheduler_decision:{account}",
        as_of_utc=as_of_utc,
        payload={
            "account": str(account),
            "decision": (account_scheduler_bundle.get("account_scheduler_decisions") or {}).get(str(account)),
        },
        key="decision",
        error_message="account scheduler decision must be a dict",
    )
    account_scheduler_decision_view = (account_scheduler_bundle.get("account_scheduler_views") or {}).get(str(account))
    if not isinstance(account_scheduler_decision_view, account_view_cls):
        raise SchemaValidationError("account scheduler decision view must be valid")
    return account_scheduler_decision_view
