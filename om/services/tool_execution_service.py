from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Callable

from om.domain import build_tool_idempotency_key, normalize_tool_execution_payload
from om.storage.repositories import state_repo


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ToolExecutionIntent:
    tool_name: str
    symbol: str
    source: str
    limit_exp: int
    cmd: list[str]
    cwd: Path
    timeout_sec: int | None = None
    capture_output: bool = True
    text: bool = True
    idempotency_scope: str = "tool_execution"
    run_id: str | None = None


class ToolExecutionService:
    """Lightweight subprocess executor with audit + idempotency plumbing."""

    def __init__(
        self,
        *,
        base: Path,
        run_id: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
    ) -> None:
        self._base = Path(base)
        self._run_id = run_id
        self._runner = runner or subprocess.run
        self._lock = Lock()
        self._claimed: set[str] = set()

    def _build_key(self, intent: ToolExecutionIntent) -> str:
        return build_tool_idempotency_key(
            tool_name=intent.tool_name,
            symbol=intent.symbol,
            source=intent.source,
            limit_exp=int(intent.limit_exp),
        )

    def _claim(self, key: str) -> bool:
        with self._lock:
            if key in self._claimed:
                return False
            self._claimed.add(key)
            return True

    def execute(self, intent: ToolExecutionIntent) -> dict:
        key = self._build_key(intent)
        existing = state_repo.read_idempotency_record(
            self._base,
            scope=intent.idempotency_scope,
            key=key,
        )
        if isinstance(existing, dict) and bool(existing.get("ok")):
            payload = normalize_tool_execution_payload(
                tool_name=intent.tool_name,
                symbol=intent.symbol,
                source=intent.source,
                limit_exp=int(intent.limit_exp),
                status="skipped",
                ok=True,
                message="idempotent_duplicate_persisted",
                returncode=0,
                idempotency_key=key,
            )
            state_repo.append_tool_execution_audit(self._base, payload, run_id=(intent.run_id or self._run_id))
            return payload

        if not self._claim(key):
            payload = normalize_tool_execution_payload(
                tool_name=intent.tool_name,
                symbol=intent.symbol,
                source=intent.source,
                limit_exp=int(intent.limit_exp),
                status="skipped",
                ok=True,
                message="idempotent_duplicate",
                returncode=0,
                idempotency_key=key,
            )
            state_repo.append_tool_execution_audit(self._base, payload, run_id=(intent.run_id or self._run_id))
            return payload

        started = _utc_now_iso()
        try:
            proc = self._runner(
                intent.cmd,
                cwd=str(intent.cwd),
                timeout=intent.timeout_sec,
                capture_output=bool(intent.capture_output),
                text=bool(intent.text),
            )
        except subprocess.TimeoutExpired:
            finished = _utc_now_iso()
            payload = normalize_tool_execution_payload(
                tool_name=intent.tool_name,
                symbol=intent.symbol,
                source=intent.source,
                limit_exp=int(intent.limit_exp),
                status="error",
                ok=False,
                message=f"timeout after {intent.timeout_sec}s",
                returncode=None,
                idempotency_key=key,
                started_at_utc=started,
                finished_at_utc=finished,
            )
            state_repo.append_tool_execution_audit(self._base, payload, run_id=(intent.run_id or self._run_id))
            return payload
        except Exception as e:
            finished = _utc_now_iso()
            payload = normalize_tool_execution_payload(
                tool_name=intent.tool_name,
                symbol=intent.symbol,
                source=intent.source,
                limit_exp=int(intent.limit_exp),
                status="error",
                ok=False,
                message=f"{type(e).__name__}: {e}",
                returncode=None,
                idempotency_key=key,
                started_at_utc=started,
                finished_at_utc=finished,
            )
            state_repo.append_tool_execution_audit(self._base, payload, run_id=(intent.run_id or self._run_id))
            return payload

        finished = _utc_now_iso()
        rc = int(proc.returncode)
        err_src = ((proc.stderr or proc.stdout) or "").strip()
        msg = err_src.splitlines()[-1] if err_src else ("fetched" if rc == 0 else f"returncode={rc}")
        payload = normalize_tool_execution_payload(
            tool_name=intent.tool_name,
            symbol=intent.symbol,
            source=intent.source,
            limit_exp=int(intent.limit_exp),
            status=("fetched" if rc == 0 else "error"),
            ok=(rc == 0),
            message=msg,
            returncode=rc,
            idempotency_key=key,
            started_at_utc=started,
            finished_at_utc=finished,
        )
        state_repo.append_tool_execution_audit(self._base, payload, run_id=(intent.run_id or self._run_id))

        if rc == 0:
            state_repo.put_idempotency_success(
                self._base,
                scope=intent.idempotency_scope,
                key=key,
                payload={
                    "tool_name": intent.tool_name,
                    "symbol": str(intent.symbol or "").strip().upper(),
                    "source": str(intent.source or "").strip().lower(),
                    "limit_exp": int(intent.limit_exp),
                    "status": "fetched",
                    "ok": True,
                    "finished_at_utc": finished,
                },
            )

        return payload
