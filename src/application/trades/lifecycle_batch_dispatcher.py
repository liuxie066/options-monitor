from __future__ import annotations

import copy
import threading
import time
from typing import Any, Callable

from src.application.trades.lifecycle_outbox import (
    dispatch_notification_batch_once,
)


DISPATCHER_STATUS_SCHEMA = "trade_lifecycle_batch_dispatcher_status.v1"


def resolve_lifecycle_receipt_dispatch_scope(
    intake_config: dict[str, Any],
) -> dict[str, Any]:
    """Resolve the accounts one process-level receipt owner may deliver."""

    fallback_receipt = (
        dict(intake_config.get("receipt") or {})
        if isinstance(intake_config.get("receipt"), dict)
        else {}
    )
    accounts: set[str] = set()
    enabled_receipt_config: dict[str, Any] | None = None
    for raw_source in intake_config.get("sources") or []:
        if not isinstance(raw_source, dict) or not bool(
            raw_source.get("enabled", True)
        ):
            continue
        source_receipt = (
            dict(raw_source.get("receipt") or {})
            if isinstance(raw_source.get("receipt"), dict)
            else fallback_receipt
        )
        if not bool(source_receipt.get("enabled", True)):
            continue
        if enabled_receipt_config is None:
            enabled_receipt_config = source_receipt
        account = str(raw_source.get("account") or "").strip().lower()
        if account:
            accounts.add(account)
        mapping = raw_source.get("account_mapping")
        if isinstance(mapping, dict):
            accounts.update(
                str(value or "").strip().lower()
                for value in mapping.values()
                if str(value or "").strip()
            )
    return {
        "allowed_accounts": sorted(accounts),
        "receipt_config": dict(
            enabled_receipt_config
            if enabled_receipt_config is not None
            else fallback_receipt
        ),
    }


def lifecycle_receipt_dispatcher_status(
    *,
    status: str,
    reason: str,
    allowed_accounts: list[str] | tuple[str, ...] | set[str] = (),
    route: dict[str, Any] | None = None,
) -> dict[str, Any]:
    route_value = dict(route or {})
    return {
        "schema_version": DISPATCHER_STATUS_SCHEMA,
        "status": str(status),
        "reason": str(reason),
        "allowed_accounts": sorted(
            {
                str(account or "").strip().lower()
                for account in allowed_accounts
                if str(account or "").strip()
            }
        ),
        "provider": str(route_value.get("provider") or "") or None,
        "channel": str(route_value.get("channel") or "") or None,
        "target_fingerprint": (
            str(route_value.get("target_fingerprint") or "") or None
        ),
        "route_fingerprint": (
            str(route_value.get("route_fingerprint") or "") or None
        ),
    }


def _batch_summary(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    keys = (
        "batch_id",
        "status",
        "provider",
        "channel",
        "target_fingerprint",
        "route_fingerprint",
        "member_count",
        "attempt_count",
        "next_attempt_at_ms",
        "send_started_at_ms",
        "confirmed_at_ms",
        "provider_message_id",
    )
    return {key: value.get(key) for key in keys}


def _planning_summary(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    keys = (
        "status",
        "reason",
        "candidate_count",
        "quiet_ready_at_ms",
        "max_wait_at_ms",
        "next_attempt_at_ms",
    )
    return {key: value.get(key) for key in keys if key in value}


def _dispatch_result_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": str(result.get("status") or "unknown"),
        "reason": result.get("reason"),
        "planning": _planning_summary(result.get("planning")),
        "recovery": (
            dict(result["recovery"])
            if isinstance(result.get("recovery"), dict)
            else None
        ),
        "batch": _batch_summary(result.get("batch")),
    }


class LifecycleReceiptBatchDispatcher:
    """One process-level owner for durable lifecycle receipt batches."""

    def __init__(
        self,
        *,
        repo: Any,
        route: dict[str, Any],
        allowed_accounts: list[str] | tuple[str, ...] | set[str],
        send_fn: Callable[[dict[str, Any]], dict[str, Any]],
        poll_interval_sec: float = 1.0,
        now_ms_fn: Callable[[], int] | None = None,
        log_fn: Callable[[str], None] | None = None,
    ) -> None:
        accounts = sorted(
            {
                str(account or "").strip().lower()
                for account in allowed_accounts
                if str(account or "").strip()
            }
        )
        if not accounts:
            raise ValueError(
                "lifecycle receipt batch dispatcher requires allowed accounts"
            )
        if float(poll_interval_sec) <= 0:
            raise ValueError("dispatcher poll_interval_sec must be > 0")
        self._repo = repo
        self._route = dict(route)
        self._allowed_accounts = tuple(accounts)
        self._send_fn = send_fn
        self._poll_interval_sec = float(poll_interval_sec)
        self._now_ms_fn = now_ms_fn or (
            lambda: int(time.time() * 1000)
        )
        self._log_fn = log_fn
        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state = {
            **lifecycle_receipt_dispatcher_status(
                status="initialized",
                reason="ready",
                allowed_accounts=accounts,
                route=self._route,
            ),
            "poll_interval_sec": self._poll_interval_sec,
            "poll_count": 0,
            "provider_attempt_count": 0,
            "last_result": None,
            "last_error": None,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            return copy.deepcopy(self._state)

    def start(self) -> None:
        with self._state_lock:
            if self._thread is not None:
                raise RuntimeError(
                    "lifecycle receipt batch dispatcher already started"
                )
            if self._stop_event.is_set():
                raise RuntimeError(
                    "lifecycle receipt batch dispatcher already closed"
                )
            started_at_ms = int(self._now_ms_fn())
            self._state.update(
                {
                    "status": "running",
                    "reason": "started",
                    "started_at_ms": started_at_ms,
                }
            )
            thread = threading.Thread(
                target=self._run,
                name="lifecycle-receipt-batch-dispatcher",
                daemon=True,
            )
            self._thread = thread
        try:
            thread.start()
        except Exception:
            with self._state_lock:
                self._thread = None
                self._state.update(
                    {
                        "status": "start_failed",
                        "reason": "thread_start_failed",
                    }
                )
            raise

    def run_once(self) -> dict[str, Any]:
        with self._poll_lock:
            return self._run_once_locked()

    def _run_once_locked(self) -> dict[str, Any]:
        attempted_at_ms = int(self._now_ms_fn())
        try:
            result = dispatch_notification_batch_once(
                self._repo,
                route=self._route,
                send_fn=self._send_fn,
                now_ms=attempted_at_ms,
                allowed_accounts=self._allowed_accounts,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            with self._state_lock:
                previous_error = self._state.get("last_error")
                self._state.update(
                    {
                        "poll_count": int(
                            self._state.get("poll_count") or 0
                        )
                        + 1,
                        "last_poll_at_ms": attempted_at_ms,
                        "last_error": error,
                    }
                )
            if (
                self._log_fn is not None
                and previous_error != error
            ):
                self._log_fn(
                    "[WARN] lifecycle receipt batch dispatcher error: "
                    + error
                )
            return {
                "status": "error",
                "error": error,
                "batch": None,
            }

        summary = _dispatch_result_summary(result)
        provider_attempted = summary["status"] in {
            "confirmed",
            "accepted",
            "unknown",
            "explicit_failed",
        }
        with self._state_lock:
            self._state.update(
                {
                    "poll_count": int(
                        self._state.get("poll_count") or 0
                    )
                    + 1,
                    "provider_attempt_count": int(
                        self._state.get("provider_attempt_count") or 0
                    )
                    + int(provider_attempted),
                    "last_poll_at_ms": attempted_at_ms,
                    "last_result": summary,
                    "last_error": None,
                }
            )
        return result

    def close(self, *, timeout_sec: float | None = None) -> None:
        self._stop_event.set()
        with self._state_lock:
            thread = self._thread
            if thread is None:
                self._state.update(
                    {
                        "status": "stopped",
                        "reason": "closed_before_start",
                        "stopped_at_ms": int(self._now_ms_fn()),
                    }
                )
                return
            if not thread.is_alive():
                self._state.update(
                    {
                        "status": "stopped",
                        "reason": "already_stopped",
                    }
                )
                return
            self._state.update(
                {
                    "status": "stopping",
                    "reason": "close_requested",
                }
            )
        if thread is threading.current_thread():
            return
        thread.join(timeout=timeout_sec)
        if thread.is_alive():
            with self._state_lock:
                self._state.update(
                    {
                        "status": "stop_timeout",
                        "reason": "dispatcher_thread_still_running",
                    }
                )
            raise TimeoutError(
                "lifecycle receipt batch dispatcher did not stop"
            )

    def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                self.run_once()
                if self._stop_event.wait(self._poll_interval_sec):
                    break
        finally:
            with self._state_lock:
                self._state.update(
                    {
                        "status": "stopped",
                        "reason": "stop_requested",
                        "stopped_at_ms": int(self._now_ms_fn()),
                    }
                )
