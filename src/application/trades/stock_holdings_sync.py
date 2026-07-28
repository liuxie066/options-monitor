from __future__ import annotations

import queue
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from src.application.trades.state import append_trade_intake_audit
from src.infrastructure.io_utils import atomic_write_json, read_json, utc_now
from src.infrastructure.portfolio_holdings_sync_client import (
    PortfolioHoldingsSyncUnknownError,
    sync_portfolio_holdings,
)
from src.infrastructure.portfolio_management_client import (
    validate_holdings_sync_response,
)


@dataclass(frozen=True)
class StockHoldingsSyncIntent:
    account: str
    deal_id: str
    received_at: str
    source: str


class StockHoldingsSyncDispatcher:
    """Coalesces stock fills and refreshes PM holdings outside the Futu callback."""

    def __init__(
        self,
        *,
        accounts: Sequence[str],
        state_dir: str | Path,
        sync_fn: Callable[[str], Mapping[str, Any]] = sync_portfolio_holdings,
        debounce_sec: float = 2.0,
        max_attempts: int = 3,
        retry_backoff_sec: float = 2.0,
        queue_capacity: int = 100,
        recent_deal_limit: int = 2000,
    ) -> None:
        normalized_accounts = list(
            dict.fromkeys(
                str(account or "").strip().lower()
                for account in accounts
                if str(account or "").strip()
            )
        )
        if not normalized_accounts:
            raise ValueError("stock holdings sync requires at least one account")
        if debounce_sec < 0:
            raise ValueError("debounce_sec must be >= 0")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be > 0")
        if retry_backoff_sec < 0:
            raise ValueError("retry_backoff_sec must be >= 0")
        if queue_capacity <= 0:
            raise ValueError("queue_capacity must be > 0")
        if recent_deal_limit <= 0:
            raise ValueError("recent_deal_limit must be > 0")

        self._accounts = tuple(normalized_accounts)
        self._state_dir = Path(state_dir)
        self._sync_fn = sync_fn
        self._debounce_sec = float(debounce_sec)
        self._max_attempts = int(max_attempts)
        self._retry_backoff_sec = float(retry_backoff_sec)
        self._recent_deal_limit = int(recent_deal_limit)
        self._lock = threading.RLock()
        self._accepting = True
        self._stop_event = threading.Event()
        self._queues = {
            account: queue.Queue[StockHoldingsSyncIntent](maxsize=int(queue_capacity))
            for account in self._accounts
        }
        self._pending = {account: set() for account in self._accounts}
        self._states = {
            account: self._load_account_state(account)
            for account in self._accounts
        }
        self._recent_succeeded = {
            account: set(self._states[account]["recent_succeeded_deal_ids"])
            for account in self._accounts
        }
        self._recent_unknown = {
            account: set(self._states[account]["recent_unknown_deal_ids"])
            for account in self._accounts
        }
        self._threads = [
            threading.Thread(
                target=self._worker,
                args=(account,),
                name=f"pm-holdings-sync-{account}",
                daemon=True,
            )
            for account in self._accounts
        ]
        for thread in self._threads:
            thread.start()

    def handle_normalized_deal(self, context: Mapping[str, Any]) -> dict[str, Any]:
        deal = context.get("deal")
        if deal is None:
            return {"status": "skipped", "reason": "missing_normalized_deal"}
        if str(getattr(deal, "option_type", "") or "").strip():
            return {
                "status": "skipped",
                "reason": "option_deal",
                "deal_id": str(getattr(deal, "deal_id", "") or "").strip() or None,
            }
        if not bool(context.get("apply_changes")):
            return {
                "status": "skipped",
                "reason": "dry_run",
                "deal_id": str(getattr(deal, "deal_id", "") or "").strip() or None,
            }

        account = str(getattr(deal, "internal_account", "") or "").strip().lower()
        deal_id = str(getattr(deal, "deal_id", "") or "").strip()
        source = str(context.get("source") or "push").strip() or "push"
        if not account:
            return {
                "status": "rejected",
                "reason": "missing_account_mapping",
                "deal_id": deal_id or None,
            }
        if account not in self._queues:
            return {
                "status": "rejected",
                "reason": "unknown_account",
                "account": account,
                "deal_id": deal_id or None,
            }
        if not deal_id:
            return {
                "status": "rejected",
                "reason": "missing_deal_id",
                "account": account,
            }

        intent = StockHoldingsSyncIntent(
            account=account,
            deal_id=deal_id,
            received_at=utc_now(),
            source=source,
        )
        with self._lock:
            if not self._accepting:
                return {
                    "status": "rejected",
                    "reason": "dispatcher_stopping",
                    "account": account,
                    "deal_id": deal_id,
                }
            if deal_id in self._recent_succeeded[account]:
                return {
                    "status": "skipped",
                    "reason": "already_synchronized",
                    "account": account,
                    "deal_id": deal_id,
                }
            if deal_id in self._recent_unknown[account]:
                return {
                    "status": "rejected",
                    "reason": "sync_result_unknown_requires_reconciliation",
                    "account": account,
                    "deal_id": deal_id,
                }
            if deal_id in self._pending[account]:
                return {
                    "status": "coalesced",
                    "reason": "already_pending",
                    "account": account,
                    "deal_id": deal_id,
                }
            self._pending[account].add(deal_id)
            try:
                self._queues[account].put_nowait(intent)
            except queue.Full:
                self._pending[account].discard(deal_id)
                return {
                    "status": "rejected",
                    "reason": "queue_full",
                    "account": account,
                    "deal_id": deal_id,
                }
        return {
            "status": "queued",
            "reason": "stock_or_etf_deal",
            "account": account,
            "deal_id": deal_id,
            "source": source,
        }

    def reconcile_unknown(
        self,
        *,
        account: str,
        deal_ids: Sequence[str],
        outcome: str,
        evidence: Mapping[str, Any],
    ) -> dict[str, Any]:
        normalized_account = str(account or "").strip().lower()
        normalized_ids = list(
            dict.fromkeys(
                str(deal_id or "").strip()
                for deal_id in deal_ids
                if str(deal_id or "").strip()
            )
        )
        normalized_outcome = str(outcome or "").strip().lower()
        if normalized_account not in self._states:
            raise ValueError("unknown stock holdings sync account")
        if not normalized_ids:
            raise ValueError("deal_ids are required")
        if normalized_outcome not in {"succeeded", "failed"}:
            raise ValueError("outcome must be succeeded or failed")
        if not isinstance(evidence, Mapping) or not str(
            evidence.get("reference") or ""
        ).strip():
            raise ValueError("reconciliation evidence reference is required")

        reconciled_at = utc_now()
        with self._lock:
            unknown = self._recent_unknown[normalized_account]
            if any(deal_id not in unknown for deal_id in normalized_ids):
                raise ValueError("deal_id is not awaiting reconciliation")
            state = self._states[normalized_account]
            recent_unknown = dict(state["recent_unknown_deal_ids"])
            recent_succeeded = dict(state["recent_succeeded_deal_ids"])
            for deal_id in normalized_ids:
                unknown.discard(deal_id)
                recent_unknown.pop(deal_id, None)
                if normalized_outcome == "succeeded":
                    self._recent_succeeded[normalized_account].add(deal_id)
                    recent_succeeded[deal_id] = {
                        "source": "reconciliation",
                        "succeeded_at_utc": reconciled_at,
                        "evidence": dict(evidence),
                    }
            state["recent_unknown_deal_ids"] = recent_unknown
            state["recent_succeeded_deal_ids"] = recent_succeeded
            state_snapshot = _state_snapshot(state)
        self._write_account_state(normalized_account, state_snapshot)
        self._append_audit(
            normalized_account,
            {
                "phase": "stock_holdings_sync_reconciled",
                "account": normalized_account,
                "deal_ids": normalized_ids,
                "outcome": normalized_outcome,
                "evidence": dict(evidence),
                "observed_at_utc": reconciled_at,
            },
        )
        return {
            "status": "reconciled",
            "account": normalized_account,
            "deal_ids": normalized_ids,
            "outcome": normalized_outcome,
        }

    def wait_until_idle(self, timeout_sec: float = 30.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout_sec))
        while time.monotonic() <= deadline:
            with self._lock:
                pending = any(self._pending[account] for account in self._accounts)
            unfinished = any(
                self._queues[account].unfinished_tasks
                for account in self._accounts
            )
            if not pending and not unfinished:
                return True
            time.sleep(0.01)
        return False

    def close(self, timeout_sec: float = 30.0) -> bool:
        with self._lock:
            self._accepting = False
        drained = self.wait_until_idle(timeout_sec=timeout_sec)
        self._stop_event.set()
        deadline = time.monotonic() + max(0.0, float(timeout_sec))
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        return drained and all(not thread.is_alive() for thread in self._threads)

    def _worker(self, account: str) -> None:
        work_queue = self._queues[account]
        while not self._stop_event.is_set() or work_queue.unfinished_tasks:
            try:
                first = work_queue.get(timeout=0.05)
            except queue.Empty:
                continue
            batch = [first]
            if self._debounce_sec:
                deadline = time.monotonic() + self._debounce_sec
                while time.monotonic() < deadline:
                    time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
            while True:
                try:
                    batch.append(work_queue.get_nowait())
                except queue.Empty:
                    break
            try:
                try:
                    self._run_batch(account, batch)
                except Exception as exc:
                    self._record_internal_error(account, batch, exc)
            finally:
                with self._lock:
                    for intent in batch:
                        self._pending[account].discard(intent.deal_id)
                for _intent in batch:
                    work_queue.task_done()

    def _record_internal_error(
        self,
        account: str,
        intents: Sequence[StockHoldingsSyncIntent],
        exc: Exception,
    ) -> None:
        error = f"{type(exc).__name__}: {exc}"
        deal_ids = list(dict.fromkeys(intent.deal_id for intent in intents))
        sources = sorted({intent.source for intent in intents})
        observed_at = utc_now()
        print(
            "[ERROR] stock holdings sync dispatcher "
            f"account={account} deal_ids={','.join(deal_ids)} error={error}",
            file=sys.stderr,
            flush=True,
        )
        try:
            self._update_last_batch(
                account,
                status="internal_error",
                deal_ids=deal_ids,
                sources=sources,
                attempts=0,
                started_at=observed_at,
                finished_at=observed_at,
                error=error,
            )
        except Exception as state_exc:
            print(
                "[ERROR] stock holdings sync state write failed "
                f"account={account} error={type(state_exc).__name__}: {state_exc}",
                file=sys.stderr,
                flush=True,
            )
        try:
            self._append_audit(
                account,
                {
                    "phase": "stock_holdings_sync_internal_error",
                    "account": account,
                    "deal_ids": deal_ids,
                    "sources": sources,
                    "observed_at_utc": observed_at,
                    "error": error,
                },
            )
        except Exception as audit_exc:
            print(
                "[ERROR] stock holdings sync audit write failed "
                f"account={account} error={type(audit_exc).__name__}: {audit_exc}",
                file=sys.stderr,
                flush=True,
            )

    def _run_batch(
        self,
        account: str,
        intents: Sequence[StockHoldingsSyncIntent],
    ) -> None:
        deal_ids = list(dict.fromkeys(intent.deal_id for intent in intents))
        sources = sorted({intent.source for intent in intents})
        started_at = utc_now()
        self._update_last_batch(
            account,
            status="running",
            deal_ids=deal_ids,
            sources=sources,
            attempts=0,
            started_at=started_at,
        )
        self._append_audit(
            account,
            {
                "phase": "stock_holdings_sync_started",
                "account": account,
                "deal_ids": deal_ids,
                "sources": sources,
                "started_at_utc": started_at,
            },
        )

        last_error = ""
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = dict(self._sync_fn(account))
                response = validate_holdings_sync_response(
                    response,
                    requested_account=account,
                )
            except PortfolioHoldingsSyncUnknownError as exc:
                self._record_unknown_result(
                    account,
                    intents,
                    attempt=attempt,
                    started_at=started_at,
                    error=f"{type(exc).__name__}: {exc}",
                )
                return
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                self._append_audit(
                    account,
                    {
                        "phase": "stock_holdings_sync_attempt_failed",
                        "account": account,
                        "deal_ids": deal_ids,
                        "attempt": attempt,
                        "max_attempts": self._max_attempts,
                        "error": last_error,
                        "observed_at_utc": utc_now(),
                    },
                )
                if attempt < self._max_attempts and self._retry_backoff_sec:
                    time.sleep(self._retry_backoff_sec * attempt)
                continue

            succeeded_at = utc_now()
            with self._lock:
                state = self._states[account]
                recent = dict(state["recent_succeeded_deal_ids"])
                for intent in intents:
                    recent[intent.deal_id] = {
                        "source": intent.source,
                        "received_at_utc": intent.received_at,
                        "succeeded_at_utc": succeeded_at,
                    }
                    self._recent_succeeded[account].add(intent.deal_id)
                while len(recent) > self._recent_deal_limit:
                    oldest = next(iter(recent))
                    recent.pop(oldest, None)
                    self._recent_succeeded[account].discard(oldest)
                state["recent_succeeded_deal_ids"] = recent
                state["last_batch"] = {
                    "status": "succeeded",
                    "deal_ids": deal_ids,
                    "sources": sources,
                    "attempts": attempt,
                    "started_at_utc": started_at,
                    "finished_at_utc": succeeded_at,
                    "portfolio_result": _portfolio_result_summary(response),
                }
                state_snapshot = _state_snapshot(state)
            self._write_account_state(account, state_snapshot)
            self._append_audit(
                account,
                {
                    "phase": "stock_holdings_sync_succeeded",
                    "account": account,
                    "deal_ids": deal_ids,
                    "sources": sources,
                    "attempts": attempt,
                    "started_at_utc": started_at,
                    "finished_at_utc": succeeded_at,
                    "portfolio_result": _portfolio_result_summary(response),
                },
            )
            return

        failed_at = utc_now()
        self._update_last_batch(
            account,
            status="failed",
            deal_ids=deal_ids,
            sources=sources,
            attempts=self._max_attempts,
            started_at=started_at,
            finished_at=failed_at,
            error=last_error,
        )
        self._append_audit(
            account,
            {
                "phase": "stock_holdings_sync_failed",
                "account": account,
                "deal_ids": deal_ids,
                "sources": sources,
                "attempts": self._max_attempts,
                "started_at_utc": started_at,
                "finished_at_utc": failed_at,
                "error": last_error,
            },
        )

    def _record_unknown_result(
        self,
        account: str,
        intents: Sequence[StockHoldingsSyncIntent],
        *,
        attempt: int,
        started_at: str,
        error: str,
    ) -> None:
        deal_ids = list(dict.fromkeys(intent.deal_id for intent in intents))
        sources = sorted({intent.source for intent in intents})
        observed_at = utc_now()
        with self._lock:
            state = self._states[account]
            recent_unknown = dict(state["recent_unknown_deal_ids"])
            for intent in intents:
                recent_unknown[intent.deal_id] = {
                    "source": intent.source,
                    "received_at_utc": intent.received_at,
                    "unknown_at_utc": observed_at,
                    "error": error,
                }
                self._recent_unknown[account].add(intent.deal_id)
            state["recent_unknown_deal_ids"] = recent_unknown
            state["last_batch"] = {
                "status": "unknown",
                "deal_ids": deal_ids,
                "sources": sources,
                "attempts": attempt,
                "started_at_utc": started_at,
                "finished_at_utc": observed_at,
                "error": error,
            }
            state_snapshot = _state_snapshot(state)
        self._write_account_state(account, state_snapshot)
        self._append_audit(
            account,
            {
                "phase": "stock_holdings_sync_result_unknown",
                "account": account,
                "deal_ids": deal_ids,
                "sources": sources,
                "attempts": attempt,
                "started_at_utc": started_at,
                "finished_at_utc": observed_at,
                "error": error,
            },
        )

    def _load_account_state(self, account: str) -> dict[str, Any]:
        raw = read_json(self._state_path(account), default={})
        data = raw if isinstance(raw, dict) else {}
        recent = data.get("recent_succeeded_deal_ids")
        recent_unknown = data.get("recent_unknown_deal_ids")
        return {
            "version": 2,
            "account": account,
            "recent_succeeded_deal_ids": (
                dict(recent)
                if isinstance(recent, dict)
                else {}
            ),
            "recent_unknown_deal_ids": (
                dict(recent_unknown)
                if isinstance(recent_unknown, dict)
                else {}
            ),
            "last_batch": (
                dict(data.get("last_batch"))
                if isinstance(data.get("last_batch"), dict)
                else {}
            ),
        }

    def _update_last_batch(
        self,
        account: str,
        *,
        status: str,
        deal_ids: Sequence[str],
        sources: Sequence[str],
        attempts: int,
        started_at: str,
        finished_at: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            payload: dict[str, Any] = {
                "status": status,
                "deal_ids": list(deal_ids),
                "sources": list(sources),
                "attempts": int(attempts),
                "started_at_utc": started_at,
            }
            if finished_at:
                payload["finished_at_utc"] = finished_at
            if error:
                payload["error"] = error
            self._states[account]["last_batch"] = payload
            state_snapshot = _state_snapshot(self._states[account])
        self._write_account_state(account, state_snapshot)

    def _write_account_state(
        self,
        account: str,
        state_snapshot: Mapping[str, Any],
    ) -> None:
        atomic_write_json(self._state_path(account), dict(state_snapshot))

    def _append_audit(self, account: str, payload: dict[str, Any]) -> None:
        append_trade_intake_audit(self._audit_path(account), payload)

    def _state_path(self, account: str) -> Path:
        return self._state_dir / account / "state.json"

    def _audit_path(self, account: str) -> Path:
        return self._state_dir / account / "audit.jsonl"


def _portfolio_result_summary(response: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: response.get(key)
        for key in (
            "success",
            "status",
            "account",
            "dry_run",
            "write_applied",
            "changed",
            "stock_position_count",
            "cash_position_count",
        )
        if key in response
    }


def _state_snapshot(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "version": int(state.get("version") or 2),
        "account": str(state.get("account") or ""),
        "recent_succeeded_deal_ids": dict(
            state.get("recent_succeeded_deal_ids") or {}
        ),
        "recent_unknown_deal_ids": dict(
            state.get("recent_unknown_deal_ids") or {}
        ),
        "last_batch": dict(state.get("last_batch") or {}),
    }
