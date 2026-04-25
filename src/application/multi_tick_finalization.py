from __future__ import annotations

from typing import Any, Callable

from domain.domain import build_no_account_notification_payloads, build_shared_last_run_payload
from src.application.cron_runtime import build_run_end_payload, build_shared_last_run_meta


def finalize_no_account_notification(
    *,
    base,
    run_id: str,
    runlog,
    results: list[Any],
    tick_metrics: dict[str, Any],
    no_send: bool,
    state_repo,
    utc_now_fn: Callable[[], str],
    audit_fn: Callable[..., Any],
    safe_data_fn: Callable[[dict[str, Any]], dict[str, Any]],
    on_success: Callable[[], Any],
) -> int:
    runlog.safe_event("notify", "skip", message="no account notification content")
    shared_payload, account_payloads = build_no_account_notification_payloads(
        now_utc_fn=utc_now_fn,
        results=results,
        run_dir=str(tick_metrics.get("run_dir") or ""),
    )
    try:
        state_repo.write_shared_last_run(base, shared_payload)
        audit_fn("write", "write_shared_last_run", run_id=run_id, status="skip", message="no_account_notification")
    except Exception:
        pass
    try:
        for result in results:
            payload = account_payloads.get(str(result.account), {})
            state_repo.write_account_last_run(base, result.account, payload)
            state_repo.write_run_account_last_run(base, run_id, result.account, payload)
            audit_fn("write", "write_account_last_run", run_id=run_id, account=str(result.account), status="skip", message="no_account_notification")
    except Exception:
        pass
    try:
        tick_metrics["sent"] = False
        tick_metrics["reason"] = "no_account_notification"
        state_repo.write_tick_metrics(base, run_id, tick_metrics)
        state_repo.append_tick_metrics_history(base, run_id, tick_metrics)
        audit_fn("write", "write_tick_metrics", run_id=run_id, status="skip", message="no_account_notification")
    except Exception:
        pass

    runlog.safe_event(
        "run_end",
        "ok",
        data=safe_data_fn(
            build_run_end_payload(
                no_send=no_send,
                results=results,
                sent_accounts=[],
                reason="no_account_notification",
            )
        ),
    )
    on_success()
    return 0


def finalize_multi_tick_run(
    *,
    base,
    run_id: str,
    runlog,
    results: list[Any],
    tick_metrics: dict[str, Any],
    no_send: bool,
    sent_accounts: list[str],
    notify_failures: list[dict[str, object]],
    notify_summary: dict[str, int],
    channel: str | None,
    target: str | None,
    state_repo,
    read_json_fn: Callable[..., dict[str, Any]],
    shared_state_dir_getter: Callable[[Any], Any],
    utc_now_fn: Callable[[], str],
    audit_fn: Callable[..., Any],
    safe_data_fn: Callable[[dict[str, Any]], dict[str, Any]],
    on_success: Callable[[], Any],
) -> int:
    try:
        last_run_path = (shared_state_dir_getter(base) / "last_run.json").resolve()
        prev = read_json_fn(last_run_path, {})
        run_meta = build_shared_last_run_meta(
            now_utc=utc_now_fn(),
            channel=channel,
            target=target,
            results=results,
            sent_accounts=sent_accounts,
            notify_failures=notify_failures,
            notify_summary=notify_summary,
        )
        state_repo.write_shared_last_run(
            base,
            build_shared_last_run_payload(prev_payload=prev, run_meta=run_meta, history_limit=20),
        )
        audit_fn("write", "write_shared_last_run", run_id=run_id, extra={"sent_accounts": list(sent_accounts)})
    except Exception:
        pass

    if notify_failures:
        runlog.safe_event(
            "run_end",
            "error",
            error_code=("NOTIFY_PARTIAL_FAILED" if sent_accounts else "NOTIFY_FAILED"),
            data=safe_data_fn(
                build_run_end_payload(
                    no_send=no_send,
                    results=results,
                    sent_accounts=sent_accounts,
                    notify_failures=notify_failures,
                    notify_summary=notify_summary,
                )
            ),
        )
        return 1

    runlog.safe_event(
        "run_end",
        "ok",
        data=safe_data_fn(
            build_run_end_payload(
                no_send=no_send,
                results=results,
                sent_accounts=sent_accounts,
                notify_summary=notify_summary,
            )
        ),
    )
    on_success()
    return 0
