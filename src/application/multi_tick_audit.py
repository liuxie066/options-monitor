from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


SCHEMA_VALIDATION_ERROR_CODE = "SCHEMA_VALIDATION_FAILED"


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
    guard_failure_recorded: bool = False

    def audit(
        self,
        event_type: str,
        action: str,
        *,
        status: str = "ok",
        run_id: str | None = None,
        account: str | None = None,
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
            self.append_audit_event(self.base, payload, run_id=(run_id or self.run_id))
        except Exception:
            pass

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
