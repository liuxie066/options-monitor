from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _service(tmp_path: Path) -> tuple[Any, Path, list[list[str]]]:
    """ToolExecutionService over a recording runner; returns (service, base, calls)."""
    from domain.services import ToolExecutionService

    class _Proc:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    calls: list[list[str]] = []

    def _runner(cmd, **_kwargs):
        calls.append(cmd)
        return _Proc()

    base = Path(tmp_path)
    return ToolExecutionService(base=base, runner=_runner), base, calls


def _intent(base: Path, **overrides: object) -> Any:
    """Required-data prefetch intent on the idempotency cases' defaults."""
    from domain.services import ToolExecutionIntent

    values: dict[str, object] = {
        "tool_name": "required_data_prefetch",
        "symbol": "AAPL",
        "source": "yahoo",
        "limit_exp": 8,
        "cmd": ["python", "fake.py"],
        "cwd": base,
        "idempotency_scope": "required_data_prefetch",
    }
    return ToolExecutionIntent(**{**values, **overrides})


def test_subprocess_boundary_wrappers() -> None:
    from domain.domain import (
        normalize_notify_subprocess_output,
        normalize_pipeline_subprocess_output,
        normalize_watchdog_subprocess_output,
    )

    wd = normalize_watchdog_subprocess_output(
        returncode=2,
        stdout='noise\n{"ok": false, "error_code": "OPEND_NOT_READY", "message": "OpenD 未就绪"}\n',
        stderr="",
    )
    assert wd["schema_kind"] == "subprocess_adapter"
    assert wd["adapter"] == "watchdog"
    assert wd["ok"] is False
    assert wd["status"] == "error"
    assert wd["watchdog_payload"]["error_code"] == "OPEND_NOT_READY"

    pipe = normalize_pipeline_subprocess_output(returncode=0, stdout="done\n", stderr="")
    assert pipe["adapter"] == "pipeline"
    assert pipe["ok"] is True

    notif = normalize_notify_subprocess_output(
        returncode=0,
        stdout='{"result":{"messageId":"m-1"}}',
        stderr="",
    )
    assert notif["adapter"] == "notify"
    assert notif["ok"] is True
    assert notif["command_ok"] is True
    assert notif["delivery_confirmed"] is True
    assert notif["message_id"] == "m-1"

    stderr_notif = normalize_notify_subprocess_output(
        returncode=0,
        stdout="",
        stderr='log\n{"result":{"messageId":"stderr-1"}}',
    )
    assert stderr_notif["ok"] is True
    assert stderr_notif["delivery_confirmed"] is True
    assert stderr_notif["message_id"] == "stderr-1"

    nested_notif = normalize_notify_subprocess_output(
        returncode=0,
        stdout='{"data":{"messageId":"nested-data-1","deliveryconfirmed":false}}',
        stderr="",
    )
    assert nested_notif["ok"] is True
    assert nested_notif["delivery_confirmed"] is True
    assert nested_notif["message_id"] == "nested-data-1"

    unconfirmed = normalize_notify_subprocess_output(
        returncode=0,
        stdout='{"ok":true}',
        stderr="",
    )
    assert unconfirmed["adapter"] == "notify"
    assert unconfirmed["ok"] is False
    assert unconfirmed["status"] == "error"
    assert unconfirmed["command_ok"] is True
    assert unconfirmed["delivery_confirmed"] is False
    assert unconfirmed["message_id"] is None
    assert "message_id is missing" in unconfirmed["message"]


def test_state_repo_idempotency_and_audit_helpers(tmp_path: Path) -> None:
    from domain.storage.repositories import state_repo

    base = Path(tmp_path)
    started_at = (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0)
    finished_at = started_at + timedelta(seconds=1)
    r1 = state_repo.put_idempotency_success(
        base,
        scope="required_data_prefetch",
        key="k1",
        payload={"tool_name": "required_data_prefetch", "status": "fetched"},
    )
    assert r1["created"] is True
    r2 = state_repo.put_idempotency_success(
        base,
        scope="required_data_prefetch",
        key="k1",
        payload={"tool_name": "required_data_prefetch", "status": "fetched"},
    )
    assert r2["created"] is False

    state_repo.append_tool_execution_audit(
        base,
        {
            "schema_kind": "tool_execution",
            "schema_version": "1.0",
            "tool_name": "required_data_prefetch",
            "symbol": "AAPL",
            "source": "yahoo",
            "limit_exp": 8,
            "idempotency_key": "k1",
            "status": "fetched",
            "ok": True,
            "message": "fetched",
            "returncode": 0,
            "started_at_utc": started_at.isoformat(),
            "finished_at_utc": finished_at.isoformat(),
        },
    )
    audit_path = base / "output_shared" / "state" / "tool_execution_audit.jsonl"
    assert len(audit_path.read_text(encoding="utf-8").splitlines()) == 1


def test_tool_execution_service_idempotency_and_audit(tmp_path: Path) -> None:
    svc, base, calls = _service(tmp_path)
    intent = _intent(base)
    p1 = svc.execute(intent)
    p2 = svc.execute(intent)

    assert p1["status"] == "fetched"
    assert p2["status"] == "skipped"
    assert len(calls) == 1

    audit_path = base / "output_shared" / "state" / "tool_execution_audit.jsonl"
    assert len(audit_path.read_text(encoding="utf-8").splitlines()) >= 2


def test_tool_execution_service_force_refresh_bypasses_persisted_idempotency(tmp_path: Path) -> None:
    svc, base, calls = _service(tmp_path)
    p1 = svc.execute(_intent(base))
    p2 = svc.execute(_intent(base, force_refresh=True))

    assert p1["status"] == "fetched"
    assert p2["status"] == "fetched"
    assert len(calls) == 2
