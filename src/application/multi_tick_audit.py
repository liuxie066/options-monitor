from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, Callable


SCHEMA_VALIDATION_ERROR_CODE = "SCHEMA_VALIDATION_FAILED"


def record_tick_latency(
    *,
    runlog: Any,
    stage: str,
    started: float,
    data: Mapping[str, Any] | None = None,
) -> int:
    duration_ms = max(0, int((monotonic() - started) * 1000))
    try:
        runlog.safe_event(
            "tick_latency",
            "ok",
            duration_ms=duration_ms,
            data={"stage": stage, **dict(data or {})},
        )
    except Exception:
        pass
    return duration_ms


# Diagnostic context only; reset at each synchronous account preparation boundary.
_daily_brief_timing: ContextVar[tuple[Any, str, str] | None] = ContextVar(
    "daily_brief_timing", default=None,
)


def _emit_daily_brief_phase(
    runlog: Any, status: str, *, data: dict[str, Any], duration_ms: int | None = None,
) -> None:
    try:
        runlog.safe_event("daily_brief_phase", status, data=data, duration_ms=duration_ms)
    except Exception:
        pass


@contextmanager
def daily_brief_phase(phase: str, *, operation: str) -> Iterator[None]:
    """Time an actual owner boundary only inside account preparation.

    Durations are inclusive. A start without a terminal is incomplete, not ok.
    Diagnostics must preserve the operation's result and exception.
    """
    scope = _daily_brief_timing.get()
    if scope is None:
        yield
        return
    runlog, account, market = scope
    data = {"account": account, "market": market, "phase": phase, "operation": operation}
    try:
        started = monotonic()
    except Exception:
        started = None
    _emit_daily_brief_phase(runlog, "start", data=data)
    outcome = "ok"
    error_type = None
    try:
        yield
    except BaseException as exc:
        outcome = "error"
        error_type = type(exc).__name__
        raise
    finally:
        duration_ms = None
        if started is not None:
            try:
                duration_ms = max(0, int((monotonic() - started) * 1000))
            except Exception:
                pass
        terminal = {**data, "outcome": outcome}
        if error_type is not None:
            terminal["error_type"] = error_type
        _emit_daily_brief_phase(runlog, outcome, data=terminal, duration_ms=duration_ms)


@contextmanager
def daily_brief_timing_scope(*, runlog: Any, account: str, market: str) -> Iterator[None]:
    """Bind telemetry to one account; never retain it across preparation calls."""
    token = _daily_brief_timing.set((runlog, account, market))
    try:
        with daily_brief_phase("account_prepare", operation="prepare"):
            yield
    finally:
        _daily_brief_timing.reset(token)


@dataclass
class MultiTickAuditHelper:
    base: Path
    base_cfg: dict[str, Any]
    runlog: Any
    safe_data_fn: Callable[[dict[str, Any]], dict[str, Any]]
    append_audit_event: Callable[..., Any]
    record_project_failure: Callable[..., dict[str, Any]]
    record_project_success: Callable[..., dict[str, Any]]
    build_failure_audit_fields: Callable[..., dict[str, Any]]
    run_id: str
    idempotency_key: str
    write_run_artifacts: bool = True
    guard_failure_recorded: bool = False

    def audit(
        self,
        event_type: str,
        action: str,
        *,
        status: str = "ok",
        run_id: str | None = None,
        account: str | None = None,
        shared_only: bool = False,
        **kwargs,
    ) -> None:
        try:
            payload = {
                "event_type": event_type,
                "action": action,
                "status": status,
                "run_id": run_id or self.run_id,
                "account": account,
                "idempotency_key": self.idempotency_key,
            }
            payload.update(kwargs)
            run_scope = (
                (run_id or self.run_id)
                if self.write_run_artifacts and not shared_only
                else None
            )
            self.append_audit_event(self.base, payload, run_id=run_scope)
        except Exception:
            pass

    def enable_run_artifacts(self) -> None:
        self.write_run_artifacts = True

    def guard_mark_failure(self, error_code: str, stage: str) -> None:
        if self.guard_failure_recorded:
            return
        try:
            result = self.record_project_failure(
                self.base,
                self.base_cfg,
                error_code=str(error_code),
                stage=str(stage),
            )
            self.runlog.safe_event(
                "project_guard",
                ("open" if bool(result.get("opened")) else "record_failure"),
                error_code=str(error_code),
                data=self.safe_data_fn(
                    {
                        "stage": str(stage),
                        "state": result.get("state"),
                        "failure_count": result.get("failure_count"),
                        "open_until_utc": result.get("open_until_utc"),
                    }
                ),
            )
        except Exception:
            pass
        self.guard_failure_recorded = True

    def guard_mark_success(self) -> None:
        if self.guard_failure_recorded:
            return
        try:
            result = self.record_project_success(self.base, self.base_cfg)
            if bool(result.get("closed")):
                self.runlog.safe_event(
                    "project_guard",
                    "closed",
                    data=self.safe_data_fn({"state": result.get("state")}),
                )
        except Exception:
            pass

    def fail_schema_validation(self, *, stage: str, exc: BaseException, run_id: str | None = None) -> None:
        msg = f"{stage}: {type(exc).__name__}: {exc}"
        self.runlog.safe_event("contract", "error", error_code=SCHEMA_VALIDATION_ERROR_CODE, message=msg)
        failure_fields = self.build_failure_audit_fields(
            failure_kind="decision_error",
            failure_stage=str(stage),
        )
        try:
            self.audit(
                "contract",
                f"validate_{stage}",
                run_id=run_id,
                status="error",
                error_code=SCHEMA_VALIDATION_ERROR_CODE,
                message=msg,
                **failure_fields,
            )
        except Exception:
            pass
        raise SystemExit(f"[CONTRACT_ERROR] {msg}")
