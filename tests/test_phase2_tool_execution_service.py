from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory


BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def test_subprocess_boundary_wrappers() -> None:
    from om.domain import (
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
    assert notif["message_id"] == "m-1"


def test_state_repo_idempotency_and_audit_helpers() -> None:
    from om.storage.repositories import state_repo

    with TemporaryDirectory() as td:
        base = Path(td)
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
                "started_at_utc": "2026-04-12T00:00:00+00:00",
                "finished_at_utc": "2026-04-12T00:00:01+00:00",
            },
        )
        rows = state_repo.query_tool_execution_audit(base, tool_name="required_data_prefetch", limit=10)
        assert len(rows) == 1
        ret = state_repo.apply_tool_execution_audit_retention(base, max_lines=1, max_age_days=30)
        assert ret["kept"] == 1


def test_tool_execution_service_idempotency_and_audit() -> None:
    from om.services import ToolExecutionIntent, ToolExecutionService
    from om.storage.repositories import state_repo

    class _Proc:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    calls: list[list[str]] = []

    def _runner(cmd, **kwargs):
        calls.append(cmd)
        return _Proc()

    with TemporaryDirectory() as td:
        base = Path(td)
        svc = ToolExecutionService(base=base, runner=_runner)
        intent = ToolExecutionIntent(
            tool_name="required_data_prefetch",
            symbol="AAPL",
            source="yahoo",
            limit_exp=8,
            cmd=["python", "fake.py"],
            cwd=base,
            idempotency_scope="required_data_prefetch",
        )
        p1 = svc.execute(intent)
        p2 = svc.execute(intent)

        assert p1["status"] == "fetched"
        assert p2["status"] == "skipped"
        assert len(calls) == 1

        rows = state_repo.query_tool_execution_audit(base, limit=20)
        assert len(rows) >= 2


def main() -> None:
    test_subprocess_boundary_wrappers()
    test_state_repo_idempotency_and_audit_helpers()
    test_tool_execution_service_idempotency_and_audit()
    print("OK (phase2 tool execution service)")


if __name__ == "__main__":
    main()
