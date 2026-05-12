from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from src.application.scan_scheduler import mark_scheduler_accounts


def write_last_run(
    *,
    sh: Callable[..., Any],
    cwd: Path,
    vpy: Path,
    last_run: Path,
    status: str,
    stage: str,
    started_at: str,
    reason: str | None = None,
    details: str | None = None,
) -> Any:
    cmd = [
        str(vpy),
        "scripts/write_last_run.py",
        "--path",
        str(last_run),
        "--status",
        str(status),
        "--stage",
        str(stage),
        "--started-at",
        str(started_at),
    ]
    if reason:
        cmd.extend(["--reason", str(reason)])
    if details:
        cmd.extend(["--details", str(details)])
    return sh(cmd, cwd=cwd)


def request_scheduler_update(
    *,
    runner: Callable[..., Any],
    vpy: Path,
    base: Path,
    config: Path,
    state: Path,
    state_dir: Path | None = None,
    schedule_key: str | None = None,
    account: str | None = None,
    mark_notified: bool = False,
    mark_scanned: bool = False,
    jsonl: bool = False,
    capture_output: bool = False,
) -> Any:
    kwargs: dict[str, Any] = {
        "vpy": vpy,
        "base": base,
        "config": config,
        "state": state,
        "capture_output": bool(capture_output),
    }
    if jsonl:
        kwargs["jsonl"] = True
    if state_dir is not None:
        kwargs["state_dir"] = state_dir
    if schedule_key:
        kwargs["schedule_key"] = str(schedule_key)
    if account:
        kwargs["account"] = str(account)
    if mark_notified:
        kwargs["mark_notified"] = True
    if mark_scanned:
        kwargs["mark_scanned"] = True
    return runner(**kwargs)


def mark_accounts_notified(
    *,
    runner: Callable[..., Any],
    vpy: Path,
    base: Path,
    config: Path,
    state: Path,
    state_dir: Path | None,
    schedule_key: str | None,
    accounts: list[str],
) -> None:
    del runner, vpy
    mark_scheduler_accounts(
        config=config,
        state=state,
        state_dir=state_dir or "output/state",
        schedule_key=str(schedule_key or "schedule"),
        accounts=[str(a).strip() for a in accounts if str(a).strip()],
        mark_notified=True,
        base_dir=base,
    )


def build_notify_summary(
    *,
    sent_accounts: list[str],
    notify_failures: list[dict[str, object]],
    total_accounts: int,
    send_attempted_count: int | None = None,
    send_confirmed_count: int | None = None,
) -> dict[str, int]:
    attempted_count = (
        int(send_attempted_count)
        if send_attempted_count is not None
        else len(sent_accounts) + len(notify_failures)
    )
    confirmed_count = int(send_confirmed_count) if send_confirmed_count is not None else len(sent_accounts)
    return {
        "success_count": len(sent_accounts),
        "failure_count": len(notify_failures),
        "total_accounts": int(total_accounts),
        "account_messages_count": int(total_accounts),
        "send_attempted_count": attempted_count,
        "send_confirmed_count": confirmed_count,
        "send_failed_count": len(notify_failures),
    }


def apply_notify_results_to_tick_metrics(
    *,
    tick_metrics: dict[str, Any],
    no_send: bool,
    sent_accounts: list[str],
    notify_failures: list[dict[str, object]],
    notify_summary: dict[str, int],
) -> dict[str, Any]:
    tick_metrics["sent"] = (not no_send) and bool(sent_accounts)
    tick_metrics["sent_accounts"] = sent_accounts
    tick_metrics["notify_summary"] = notify_summary
    for key in (
        "account_messages_count",
        "send_attempted_count",
        "send_confirmed_count",
        "send_failed_count",
    ):
        tick_metrics[key] = int(notify_summary.get(key, 0))
    if notify_failures:
        tick_metrics["reason"] = "sent_partial_notify_failure" if sent_accounts else "notify_failed"
        tick_metrics["notify_failures"] = notify_failures
    else:
        tick_metrics["reason"] = "sent" if ((not no_send) and bool(sent_accounts)) else ("no_send" if no_send else "no_account_sent")
    return tick_metrics


def build_shared_last_run_meta(
    *,
    now_utc: str,
    channel: str | None,
    target: str | None,
    results: list[Any],
    sent_accounts: list[str],
    notify_failures: list[dict[str, object]],
    notify_summary: dict[str, int],
    no_send: bool = False,
) -> dict[str, Any]:
    actual_sent_accounts = [] if no_send else list(sent_accounts)
    return {
        "last_run_utc": str(now_utc),
        "sent": (not no_send) and bool(sent_accounts),
        "no_send": bool(no_send),
        "channel": str(channel),
        "target": str(target),
        "accounts": [r.account for r in results],
        "sent_accounts": actual_sent_accounts,
        "would_send_accounts": list(sent_accounts) if no_send else [],
        "notify_failures": notify_failures,
        "notify_summary": notify_summary,
        "results": [r.__dict__ for r in results],
    }


def build_run_end_payload(
    *,
    no_send: bool,
    results: list[Any],
    sent_accounts: list[str],
    notify_summary: dict[str, int] | None = None,
    notify_failures: list[dict[str, object]] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "sent": (not no_send) and bool(sent_accounts),
        "accounts": [r.account for r in results],
    }
    if reason:
        payload["reason"] = str(reason)
    if sent_accounts:
        payload["sent_accounts"] = sent_accounts
    if notify_failures:
        payload["notify_failures"] = notify_failures
    if notify_summary is not None:
        payload["notify_summary"] = notify_summary
    return payload
