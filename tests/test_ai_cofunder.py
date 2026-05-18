from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypedDict


class _ToolKwargs(TypedDict):
    load_runtime_config: Callable[..., tuple[Path, dict[str, Any]]]
    repo_base: Callable[[], Path]
    mask_path: Callable[[Any], str | None]
    now_fn: Callable[[], datetime]


def _runtime_status_data(*, tick_metrics: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "summary": {
            "ok": True,
            "latest_status": "ok",
            "latest_run_path": "output_runs/run-1",
            "latest_scanned_run_path": "output_runs/run-1",
        },
        "freshness": {"status": "fresh", "stale": False, "age_seconds": 10, "max_age_minutes": 60},
        "latest_run_selection": {"found": True, "source": "last_run_dir_or_mtime"},
        "latest_scanned_run_selection": {"found": True, "source": "runs_root_mtime"},
        "latest_run": {
            "path": "output_runs/run-1",
            "state": {
                "last_run": {"json": {"status": "ok", "run_id": "run-1"}},
                "tick_metrics": {
                    "json": tick_metrics
                    or {
                        "scheduler_decision": {
                            "should_run_scan": True,
                            "is_notify_window_open": True,
                            "reason": "run",
                        },
                        "accounts": [{"account": "lx", "status": "ok", "ran_scan": True}],
                    }
                },
            },
            "accounts": {},
        },
        "latest_scanned_run": {
            "path": "output_runs/run-1",
            "state": {
                "last_run": {"json": {"status": "ok", "run_id": "run-1"}},
                "tick_metrics": {"json": tick_metrics or {}},
            },
            "accounts": {},
        },
        "required_data_prefetch": {"available": True, "total_errors": 0},
        "latest_scanned_run_required_data_prefetch": {"available": True, "total_errors": 0},
        "notification_diagnosis": {"status": "sent"},
        "trade_intake": {"summary": {"failed_count": 0, "unresolved_count": 0}},
        "paths": {
            "shared_state_dir": "output_shared/state",
            "runs_root": "output_runs",
        },
    }


def _load_config(tmp_path: Path, cfg: dict[str, Any] | None = None) -> Callable[..., tuple[Path, dict[str, Any]]]:
    config_path = tmp_path / "config.us.json"
    config = cfg or {"accounts": ["lx"], "symbols": []}
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_runtime_config(**_kwargs):
        return config_path, config

    return _load_runtime_config


def _tool_kwargs(tmp_path: Path) -> _ToolKwargs:
    return {
        "load_runtime_config": _load_config(tmp_path),
        "repo_base": lambda: tmp_path,
        "mask_path": lambda path: f".../{Path(path).name}",
        "now_fn": lambda: datetime(2026, 5, 16, 2, 0, tzinfo=timezone.utc),
    }


def test_ai_cofunder_reports_scheduler_failure(tmp_path: Path) -> None:
    from src.application.ai_cofunder.service import ai_cofunder_tool

    def _runtime_status(_payload):
        return _runtime_status_data(), [], {}

    data, warnings, meta = ai_cofunder_tool(
        {
            "config_path": str(tmp_path / "config.us.json"),
            "write_outputs": False,
            "scheduler_evidence": {
                "provider": "openclaw",
                "job_name": "us-tick",
                "last_triggered_at": "2026-05-16T01:00:00Z",
                "last_status": "failed",
                "last_exit_code": 1,
                "stderr_tail": "Traceback: boom",
            },
        },
        runtime_status_tool_fn=_runtime_status,
        **_tool_kwargs(tmp_path),
    )

    findings = data["bundle"]["runtime_quality"]["findings"]
    assert warnings == []
    assert data["status"] == "fail"
    assert data["category"] == "scheduler_failed"
    assert findings[0]["code"] == "SCHEDULER_FAILED"
    assert "AI Cofunder Handoff" in data["handoff_markdown"]
    assert meta["outputs"]["written"] is False


def test_ai_cofunder_does_not_guess_missing_scheduler_evidence(tmp_path: Path) -> None:
    from src.application.ai_cofunder.service import ai_cofunder_tool

    runtime_data = _runtime_status_data()
    runtime_data["summary"]["ok"] = False
    runtime_data["summary"]["warning_count"] = 1

    def _runtime_status(_payload):
        return runtime_data, ["runtime warning"], {}

    data, warnings, _meta = ai_cofunder_tool(
        {
            "config_path": str(tmp_path / "config.us.json"),
            "write_outputs": False,
        },
        runtime_status_tool_fn=_runtime_status,
        **_tool_kwargs(tmp_path),
    )

    assert "scheduler_evidence_missing: online scheduler status was not provided" in warnings
    assert data["category"] == "scheduler_unknown"
    codes = [item["code"] for item in data["bundle"]["runtime_quality"]["findings"]]
    assert "SCHEDULER_EVIDENCE_MISSING" in codes
    assert "RUNTIME_STATUS_WARNINGS" in codes


def test_ai_cofunder_preserves_scheduler_run_id_and_downgrades_confirmed_stale_runtime(tmp_path: Path) -> None:
    from src.application.ai_cofunder.service import ai_cofunder_tool

    runtime_data = _runtime_status_data()
    runtime_data["summary"]["ok"] = False
    runtime_data["summary"]["warning_count"] = 1
    runtime_data["freshness"] = {
        "status": "stale",
        "stale": True,
        "age_seconds": 4500,
        "max_age_minutes": 60,
    }

    def _runtime_status(_payload):
        return runtime_data, ["runtime output is stale"], {}

    data, _warnings, _meta = ai_cofunder_tool(
        {
            "config_path": str(tmp_path / "config.us.json"),
            "write_outputs": False,
            "scheduler_evidence": {
                "provider": "cron",
                "job_name": "hk-tick",
                "last_run_id": "run-1",
                "last_status": "success",
                "last_exit_code": 0,
            },
        },
        runtime_status_tool_fn=_runtime_status,
        **_tool_kwargs(tmp_path),
    )

    findings = data["bundle"]["runtime_quality"]["findings"]
    stale = next(item for item in findings if item["code"] == "RUNTIME_OUTPUT_STALE")
    assert data["status"] == "warn"
    assert data["bundle"]["scheduler_evidence"]["last_run_id"] == "run-1"
    assert stale["severity"] == "warn"
    assert stale["category"] == "runtime_stale"
    assert "scheduler evidence points at the latest runtime run" in stale["message"]


def test_ai_cofunder_includes_feishu_sync_problem_details(tmp_path: Path) -> None:
    from src.application.ai_cofunder.service import ai_cofunder_tool

    runtime_data = _runtime_status_data()
    runtime_data["summary"]["option_positions_feishu_sync_status"] = "partial_failed"
    runtime_data["summary"]["option_positions_feishu_sync_receipt_status"] = "sent"
    runtime_data["option_positions_feishu_sync"] = {
        "last_run": {
            "json": {
                "status": "partial_failed",
                "summary": {"create": 0, "update": 2, "delete": 0, "skip": 1, "conflict": 0, "failed": 1},
                "rows": [
                    {"record_id": "lot_1", "symbol": "0700.HK", "option_type": "put", "side": "short", "action": "update"},
                    {
                        "record_id": "lot_2",
                        "symbol": "9992.HK",
                        "option_type": "put",
                        "side": "short",
                        "action": "failed",
                        "reason": "bitable_update_failed: field mismatch",
                    },
                ],
            }
        },
        "receipt": {"status": "sent", "reason": "partial_failed", "delivery_confirmed": True},
    }

    def _runtime_status(_payload):
        return runtime_data, [], {}

    data, _warnings, _meta = ai_cofunder_tool(
        {
            "config_path": str(tmp_path / "config.us.json"),
            "write_outputs": False,
            "scheduler_evidence": {
                "provider": "cron",
                "job_name": "hk-tick",
                "last_triggered_at": "2026-05-16T01:00:00Z",
                "last_status": "success",
                "last_exit_code": 0,
            },
        },
        runtime_status_tool_fn=_runtime_status,
        **_tool_kwargs(tmp_path),
    )

    ledger_sync = data["bundle"]["ledger_quality"]["position_summary"]["option_positions_feishu_sync"]
    findings = data["bundle"]["runtime_quality"]["findings"]
    assert data["status"] == "warn"
    assert data["category"] == "position_maintenance_issue"
    assert ledger_sync["summary"]["failed"] == 1
    assert ledger_sync["receipt"]["status"] == "sent"
    assert ledger_sync["problem_rows"] == [
        {
            "record_id": "lot_2",
            "symbol": "9992.HK",
            "option_type": "put",
            "side": "short",
            "action": "failed",
            "reason": "bitable_update_failed: field mismatch",
        }
    ]
    assert any(item["code"] == "OPTION_POSITION_SYNC_ISSUE" for item in findings)
    assert "- feishu_sync_failed: 1" in data["handoff_markdown"]


def test_ai_cofunder_collects_strategy_evidence_for_handoff(tmp_path: Path) -> None:
    from src.application.ai_cofunder.service import ai_cofunder_tool

    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    account_report_dir = report_dir / "accounts" / "lx"
    account_report_dir.mkdir(parents=True)
    (account_report_dir / "nvda_sell_put_candidates_labeled.csv").write_text(
        "symbol,option_type,dte,delta,annualized_net_return,net_income\nNVDA,put,30,-0.2,0.12,120\n",
        encoding="utf-8",
    )
    (account_report_dir / "nvda_sell_put_candidates_reject_log.csv").write_text(
        "symbol,reject_stage,engine_reject_stage,engine_reject_reason\nNVDA,step3_risk_gate,stage3_risk_filter,risk_spread\n",
        encoding="utf-8",
    )
    (account_report_dir / "candidate_filter_trace.jsonl").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "account": "lx",
                "symbol": "NVDA",
                "function": "sell_put",
                "status": "rejected",
                "rule": "risk_volume",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    def _runtime_status(_payload):
        return _runtime_status_data(), [], {}

    data, _warnings, _meta = ai_cofunder_tool(
        {
            "config_path": str(tmp_path / "config.us.json"),
            "strategy_report_dir": str(report_dir),
            "write_outputs": False,
            "scheduler_evidence": {
                "provider": "openclaw",
                "job_name": "us-tick",
                "last_triggered_at": "2026-05-16T01:00:00Z",
                "last_status": "success",
                "last_exit_code": 0,
            },
        },
        runtime_status_tool_fn=_runtime_status,
        **_tool_kwargs(tmp_path),
    )

    summary = data["bundle"]["strategy_evidence"]["summary"]
    reject_logs = data["bundle"]["strategy_evidence"]["reject_logs"]
    account_strategy = data["bundle"]["account_strategy_matrix"]["accounts"]["lx"]["strategy_evidence"]
    assert data["status"] == "ok"
    assert summary["candidate_row_count"] == 1
    assert summary["candidate_file_count"] == 1
    assert summary["reject_log_row_count"] == 1
    assert reject_logs[0]["reason_counts"] == {"risk_spread": 1}
    assert account_strategy["candidate_rows"] == 1
    assert account_strategy["reject_log_rows"] == 1
    assert account_strategy["trace_rows"] == 1
    assert account_strategy["trace_status_counts"] == {"rejected": 1}
    assert "candidate_rows: 1" in data["handoff_markdown"]
    assert "reject_log_rows: 1" in data["handoff_markdown"]


def test_ai_cofunder_builds_redacted_bundle_and_handoff(tmp_path: Path) -> None:
    from src.application.ai_cofunder.service import ai_cofunder_tool

    runtime_data = _runtime_status_data()
    runtime_data["latest_run"]["state"]["tick_metrics"]["json"]["accounts"] = [
        {"account": "lx", "status": "ok", "ran_scan": True, "should_notify": True},
        {"account": "sy", "status": "ok", "ran_scan": True, "should_notify": False, "reason": "no candidates"},
    ]

    def _runtime_status(_payload):
        return runtime_data, [], {}

    data, warnings, meta = ai_cofunder_tool(
        {
            "scope": "full",
            "config_path": str(tmp_path / "config.us.json"),
            "write_outputs": False,
            "scheduler_evidence": {
                "provider": "openclaw",
                "job_name": "us-tick",
                "last_triggered_at": "2026-05-16T01:00:00Z",
                "last_status": "success",
                "last_exit_code": 0,
                "stdout_tail": "https://example.com/webhook/token for 281756479859383816",
            },
        },
        runtime_status_tool_fn=_runtime_status,
        **_tool_kwargs(tmp_path),
    )

    bundle = data["bundle"]
    bundle_json = json.dumps(bundle, ensure_ascii=False)
    assert warnings == []
    assert data["schema_version"] == "ai_cofunder.v1"
    assert bundle["schema_version"] == "ai_cofunder_bundle.v1"
    assert bundle["ledger_quality"]["status"] == "ok"
    assert sorted(bundle["account_strategy_matrix"]["accounts"]) == ["lx", "sy"]
    assert bundle["healthcheck_snapshot"] == {
        "status": "skipped",
        "included": False,
        "reason": "include_healthcheck=false",
    }
    assert "AI Cofunder Handoff" in data["handoff_markdown"]
    assert "webhook/token" not in bundle_json
    assert "281756479859383816" not in bundle_json
    assert meta["outputs"]["written"] is False


def test_ai_cofunder_can_include_redacted_healthcheck_snapshot(tmp_path: Path) -> None:
    from src.application.ai_cofunder.service import ai_cofunder_tool

    def _runtime_status(_payload):
        return _runtime_status_data(), [], {}

    def _healthcheck(payload):
        assert payload["config_path"] == str(tmp_path / "config.us.json")
        return (
            {
                "summary": {"ok": False, "critical_count": 1, "warning_count": 2},
                "config": {"config_path": str(tmp_path / "config.us.json"), "accounts": ["lx"]},
                "account_paths": {"lx": {"primary": {"source": "futu", "ok": False}}},
                "checks": [
                    {
                        "name": "notification_secrets",
                        "status": "error",
                        "message": "missing https://example.com/webhook/token for 281756479859383816",
                    }
                ],
            },
            ["notification target 281756479859383816 is not ready"],
            {"config_path": str(tmp_path / "config.us.json")},
        )

    data, warnings, meta = ai_cofunder_tool(
        {
            "scope": "full",
            "config_path": str(tmp_path / "config.us.json"),
            "include_healthcheck": True,
            "write_outputs": False,
            "scheduler_evidence": {
                "provider": "openclaw",
                "job_name": "us-tick",
                "last_triggered_at": "2026-05-16T01:00:00Z",
                "last_status": "success",
                "last_exit_code": 0,
            },
        },
        runtime_status_tool_fn=_runtime_status,
        healthcheck_tool_fn=_healthcheck,
        **_tool_kwargs(tmp_path),
    )

    snapshot = data["bundle"]["healthcheck_snapshot"]
    snapshot_json = json.dumps(snapshot, ensure_ascii=False)
    assert snapshot["included"] is True
    assert snapshot["status"] == "fail"
    assert data["summary"]["healthcheck_status"] == "fail"
    assert "healthcheck_snapshot: notification target" in warnings[0]
    assert "281756479859383816" not in warnings[0]
    assert meta["healthcheck"]["included"] is True
    assert "webhook/token" not in snapshot_json
    assert "281756479859383816" not in snapshot_json
    assert "***REDACTED_URL***" in snapshot_json


def test_ai_cofunder_writes_bundle_and_handoff(tmp_path: Path) -> None:
    from src.application.ai_cofunder.service import ai_cofunder_tool

    def _runtime_status(_payload):
        return _runtime_status_data(), [], {}

    data, warnings, _meta = ai_cofunder_tool(
        {
            "config_path": str(tmp_path / "config.us.json"),
            "write_outputs": True,
            "ai_cofunder_output_dir": str(tmp_path / "ai_cofunder"),
            "ai_cofunder_current_dir": str(tmp_path / "current"),
            "scheduler_evidence": {
                "provider": "openclaw",
                "job_name": "us-tick",
                "last_triggered_at": "2026-05-16T01:00:00Z",
                "last_status": "success",
                "last_exit_code": 0,
                "stdout_tail": "https://example.com/webhook/token",
            },
        },
        runtime_status_tool_fn=_runtime_status,
        **_tool_kwargs(tmp_path),
    )

    assert warnings == []
    assert data["outputs"]["written"] is True
    bundle_path = tmp_path / data["outputs"]["bundle_path"]
    handoff_path = tmp_path / data["outputs"]["handoff_path"]
    current_path = tmp_path / data["outputs"]["current_path"]
    assert bundle_path.exists()
    assert handoff_path.read_text(encoding="utf-8").startswith("## AI Cofunder Handoff")
    assert current_path.exists()
    bundle_text = bundle_path.read_text(encoding="utf-8")
    assert "webhook/token" not in bundle_text
    assert "***REDACTED_URL***" in bundle_text


def test_ai_cofunder_defaults_to_no_output_writes(tmp_path: Path) -> None:
    from src.application.ai_cofunder.service import ai_cofunder_tool

    def _runtime_status(_payload):
        return _runtime_status_data(), [], {}

    data, _warnings, _meta = ai_cofunder_tool(
        {
            "config_path": str(tmp_path / "config.us.json"),
            "scheduler_evidence": {
                "provider": "openclaw",
                "job_name": "us-tick",
                "last_triggered_at": "2026-05-16T01:00:00Z",
                "last_status": "success",
                "last_exit_code": 0,
            },
        },
        runtime_status_tool_fn=_runtime_status,
        **_tool_kwargs(tmp_path),
    )

    assert data["outputs"] == {"written": False}
    assert not (tmp_path / "output_shared" / "ai_cofunder").exists()


def test_ai_cofunder_rejects_output_paths_outside_repo(tmp_path: Path) -> None:
    from src.application.agent_tool_contracts import AgentToolError
    from src.application.ai_cofunder.service import ai_cofunder_tool

    def _runtime_status(_payload):
        return _runtime_status_data(), [], {}

    try:
        ai_cofunder_tool(
            {
                "config_path": str(tmp_path / "config.us.json"),
                "write_outputs": True,
                "ai_cofunder_output_dir": str(tmp_path.parent / "outside-ai-cofunder"),
                "scheduler_evidence": {
                    "provider": "openclaw",
                    "job_name": "us-tick",
                    "last_triggered_at": "2026-05-16T01:00:00Z",
                    "last_status": "success",
                    "last_exit_code": 0,
                },
            },
            runtime_status_tool_fn=_runtime_status,
            **_tool_kwargs(tmp_path),
        )
    except AgentToolError as exc:
        assert exc.code == "INPUT_ERROR"
    else:
        raise AssertionError("expected AgentToolError")


def test_ai_cofunder_agent_tool_write_outputs_requires_gate(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    monkeypatch.delenv("OM_AGENT_ENABLE_WRITE_TOOLS", raising=False)
    out = run_tool(
        "ai_cofunder",
        {
            "config_path": str(tmp_path / "config.us.json"),
            "write_outputs": True,
            "confirm": True,
        },
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "PERMISSION_DENIED"


def test_ai_cofunder_agent_tool_runs_with_local_runtime_artifacts(tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(
        json.dumps(
            {
                "accounts": ["lx"],
                "symbols": [],
                "notifications": {
                    "provider": "openclaw",
                    "channel": "wechat_clawbot",
                    "target": "clawbot:test-room",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    shared_state_dir = tmp_path / "output_shared" / "state"
    report_dir = tmp_path / "output" / "reports"
    accounts_root = tmp_path / "output_accounts"
    runs_root = tmp_path / "output_runs"
    run_dir = runs_root / "run-1"
    for path in (
        shared_state_dir,
        report_dir,
        accounts_root / "lx" / "state",
        accounts_root / "lx" / "reports",
        run_dir / "state",
        run_dir / "accounts" / "lx" / "state",
    ):
        path.mkdir(parents=True, exist_ok=True)
    (shared_state_dir / "last_run.json").write_text(json.dumps({"status": "ok", "run_id": "run-1"}), encoding="utf-8")
    (shared_state_dir / "last_run_dir.txt").write_text(str(run_dir), encoding="utf-8")
    (report_dir / "symbols_notification.txt").write_text("notification\n", encoding="utf-8")
    (run_dir / "state" / "last_run.json").write_text(json.dumps({"status": "ok", "run_id": "run-1", "ran_scan": True}), encoding="utf-8")
    (run_dir / "state" / "tick_metrics.json").write_text(
        json.dumps(
            {
                "ran_scan": True,
                "scheduler_decision": {"should_run_scan": True, "is_notify_window_open": True, "reason": "run"},
                "notify_summary": {
                    "account_messages_count": 1,
                    "send_attempted_count": 1,
                    "send_confirmed_count": 1,
                    "send_failed_count": 0,
                },
                "accounts": [{"account": "lx", "status": "ok", "ran_scan": True}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (run_dir / "accounts" / "lx" / "state" / "last_run.json").write_text(
        json.dumps({"status": "ok", "run_id": "run-1", "ran_scan": True}),
        encoding="utf-8",
    )

    out = run_tool(
        "ai_cofunder",
        {
            "config_path": str(cfg_path),
            "shared_state_dir": str(shared_state_dir),
            "report_dir": str(report_dir),
            "accounts_root": str(accounts_root),
            "runs_root": str(runs_root),
            "write_outputs": False,
            "scheduler_evidence": {
                "provider": "openclaw",
                "job_name": "us-tick",
                "last_triggered_at": "2026-05-16T01:00:00Z",
                "last_status": "success",
                "last_exit_code": 0,
            },
        },
    )

    assert out["ok"] is True
    assert out["data"]["schema_version"] == "ai_cofunder.v1"
    assert out["data"]["status"] in {"ok", "warn"}
    assert out["data"]["outputs"]["written"] is False
    assert "AI Cofunder Handoff" in out["data"]["handoff_markdown"]
