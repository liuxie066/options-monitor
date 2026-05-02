from __future__ import annotations

import json
from dataclasses import dataclass
from time import monotonic, sleep
from typing import Any, Callable

from domain.domain import (
    DeliveryPlan,
    SchemaValidationError,
    SnapshotDTO,
)
from domain.domain.engine import (
    AccountSchedulerDecisionView,
    decide_notification_delivery,
    resolve_multi_tick_engine_entrypoint,
)


@dataclass(frozen=True)
class PreparedPerAccountMessages:
    messages_by_account: dict[str, str]
    threshold_met: bool
    used_heartbeat: bool

    @property
    def account_messages(self) -> dict[str, str]:
        """Compatibility alias for the persisted account_messages contract."""
        return self.messages_by_account

@dataclass(frozen=True)
class PreparedMultiAccountNotification:
    prepared_messages: PreparedPerAccountMessages
    notify_candidates: list[Any]
    cash_footer_lines: list[str]
    results_count: int

    @property
    def messages_by_account(self) -> dict[str, str]:
        return self.prepared_messages.messages_by_account

    @property
    def threshold_met(self) -> bool:
        return self.prepared_messages.threshold_met

    @property
    def used_heartbeat(self) -> bool:
        return self.prepared_messages.used_heartbeat


@dataclass(frozen=True)
class AccountDeliveryBatch:
    messages_by_account: dict[str, str]
    target: str
    channel: str
    mode: str = "per_account"
    should_send: bool = True

    @property
    def account_messages(self) -> dict[str, str]:
        """Compatibility alias for DeliveryPlan.account_messages callers."""
        return self.messages_by_account

    @classmethod
    def from_delivery_contract(cls, delivery_contract: Any) -> "AccountDeliveryBatch":
        if isinstance(delivery_contract, dict):
            raw_messages = delivery_contract.get("account_messages") or {}
            target = delivery_contract.get("target")
            channel = delivery_contract.get("channel")
            should_send = delivery_contract.get("should_send")
        else:
            raw_messages = getattr(delivery_contract, "account_messages", {}) or {}
            target = getattr(delivery_contract, "target", None)
            channel = getattr(delivery_contract, "channel", None)
            should_send = getattr(delivery_contract, "should_send", True)

        return cls(
            messages_by_account={str(k): str(v) for k, v in dict(raw_messages).items()},
            target=str(target or ""),
            channel=str(channel or ""),
            should_send=bool(should_send),
        )

    def to_delivery_payload(self) -> dict[str, Any]:
        return {
            "schema_kind": "delivery_plan",
            "schema_version": "1.0",
            "channel": str(self.channel),
            "target": str(self.target),
            "account_messages": dict(self.messages_by_account),
            "should_send": bool(self.should_send),
        }


@dataclass(frozen=True)
class PerAccountSendExecution:
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


def build_per_account_delivery_batch(
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
) -> tuple[dict[str, Any], AccountDeliveryBatch | None, str | None]:
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

    delivery_contract = delivery_plan_cls.from_payload(
        {
            "schema_kind": "delivery_plan",
            "schema_version": "1.0",
            "channel": str(channel),
            "target": str(effective_target),
            "account_messages": account_messages,
            "should_send": True,
        }
    )
    return delivery_decision, AccountDeliveryBatch.from_delivery_contract(delivery_contract), effective_target


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


def prepare_per_account_messages(
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
) -> PreparedPerAccountMessages:
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
        return PreparedPerAccountMessages(
            messages_by_account=account_messages,
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
    return PreparedPerAccountMessages(
        messages_by_account=account_messages,
        threshold_met=bool(notify_threshold.get("threshold_met")),
        used_heartbeat=bool(notify_threshold.get("threshold_met")),
    )

def query_multi_account_cash_footer_lines(
    *,
    base,
    config_path,
    config: dict[str, Any],
    query_cash_footer_fn: Callable[..., list[str]],
    cash_footer_accounts_from_config_fn: Callable[[dict[str, Any]], list[str]],
) -> list[str]:
    try:
        cfg = config or {}
        portfolio = cfg.get("portfolio") if isinstance(cfg, dict) else {}
        if not isinstance(portfolio, dict):
            portfolio = {}
        notifications = cfg.get("notifications") if isinstance(cfg, dict) else {}
        if not isinstance(notifications, dict):
            notifications = {}

        market = str(portfolio.get("broker") or "富途")
        accounts = cash_footer_accounts_from_config_fn(cfg)
        timeout_sec = int(notifications.get("cash_footer_timeout_sec") or 180)
        max_age_sec = int(notifications.get("cash_snapshot_max_age_sec") or 900)
        return list(
            query_cash_footer_fn(
                base,
                config_path=str(config_path),
                market=market,
                accounts=accounts,
                timeout_sec=timeout_sec,
                snapshot_max_age_sec=max_age_sec,
            )
            or []
        )
    except Exception:
        return []


def prepare_multi_account_notification(
    *,
    results: list[Any],
    base,
    config_path,
    config: dict[str, Any],
    now_bj: str,
    as_of_utc: str,
    filter_notify_candidates_fn: Callable[[list[Any]], list[Any]],
    rank_notify_candidates_fn: Callable[[list[Any]], list[Any]],
    query_cash_footer_fn: Callable[..., list[str]],
    cash_footer_accounts_from_config_fn: Callable[[dict[str, Any]], list[str]],
    cash_footer_for_account_fn: Callable[[list[str], str], list[str]],
    build_account_message_fn: Callable[..., str],
    build_account_messages_fn: Callable[..., dict[str, str]],
    build_no_candidate_account_messages_fn: Callable[..., dict[str, str]],
    snapshot_cls: type[SnapshotDTO] = SnapshotDTO,
    engine_entrypoint: Callable[..., dict[str, Any]] = resolve_multi_tick_engine_entrypoint,
) -> PreparedMultiAccountNotification:
    notify_candidates = rank_notify_candidates_fn(filter_notify_candidates_fn(results))
    cash_footer_lines = query_multi_account_cash_footer_lines(
        base=base,
        config_path=config_path,
        config=config,
        query_cash_footer_fn=query_cash_footer_fn,
        cash_footer_accounts_from_config_fn=cash_footer_accounts_from_config_fn,
    )
    prepared_messages = prepare_per_account_messages(
        notify_candidates=notify_candidates,
        results=results,
        now_bj=now_bj,
        cash_footer_lines=cash_footer_lines,
        cash_footer_for_account_fn=cash_footer_for_account_fn,
        build_account_message_fn=build_account_message_fn,
        build_account_messages_fn=build_account_messages_fn,
        build_no_candidate_account_messages_fn=build_no_candidate_account_messages_fn,
        as_of_utc=as_of_utc,
        snapshot_cls=snapshot_cls,
        engine_entrypoint=engine_entrypoint,
    )
    return PreparedMultiAccountNotification(
        prepared_messages=prepared_messages,
        notify_candidates=notify_candidates,
        cash_footer_lines=cash_footer_lines,
        results_count=len(results),
    )


def mark_no_candidate_notification_metrics(
    *,
    tick_metrics: dict[str, Any],
    account_messages: dict[str, str],
) -> None:
    accounts = {
        str(account).strip().lower()
        for account in account_messages
        if str(account).strip()
    }
    if not accounts:
        return
    for acct_metrics in tick_metrics.get("accounts", []):
        if not isinstance(acct_metrics, dict):
            continue
        account = str(acct_metrics.get("account") or "").strip().lower()
        if account in accounts:
            acct_metrics["meaningful"] = True
            acct_metrics["notification_type"] = "no_candidate"


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


def execute_per_account_delivery(
    *,
    delivery_batch: AccountDeliveryBatch | DeliveryPlan,
    run_id: str,
    runlog,
    audit_fn,
    safe_data_fn: Callable[[dict[str, Any]], dict[str, Any]],
    send_fn: Callable[..., Any],
    normalize_fn: Callable[..., dict[str, Any]],
    failure_fields_builder: Callable[..., dict[str, Any]],
    on_failure: Callable[[str], Any] | None = None,
    base,
) -> PerAccountSendExecution:
    sent_accounts: list[str] = []
    notify_failures: list[dict[str, object]] = []
    target = str(delivery_batch.target)
    channel = str(delivery_batch.channel)

    for acct, msg in delivery_batch.account_messages.items():
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

    return PerAccountSendExecution(
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
