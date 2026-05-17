from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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


def _load_config(tmp_path: Path, cfg: dict[str, Any] | None = None):
    config_path = tmp_path / "config.us.json"
    config = cfg or {"accounts": ["lx"], "symbols": []}
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_runtime_config(**_kwargs):
        return config_path, config

    return _load_runtime_config


def test_doctor_reports_scheduler_failure_without_ai(tmp_path: Path) -> None:
    from src.application.doctor.service import doctor_tool

    def _runtime_status(_payload):
        return _runtime_status_data(), [], {}

    data, warnings, meta = doctor_tool(
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
        load_runtime_config=_load_config(tmp_path),
        repo_base=lambda: tmp_path,
        mask_path=lambda path: f".../{Path(path).name}",
        now_fn=lambda: datetime(2026, 5, 16, 2, 0, tzinfo=timezone.utc),
    )

    assert warnings == []
    assert data["status"] == "fail"
    assert data["category"] == "scheduler_failed"
    assert data["deterministic"]["findings"][0]["code"] == "SCHEDULER_FAILED"
    assert "Online Doctor Conclusion" in data["handoff_markdown"]
    assert meta["outputs"]["written"] is False


def test_doctor_does_not_guess_missing_scheduler_evidence(tmp_path: Path) -> None:
    from src.application.doctor.service import doctor_tool

    runtime_data = _runtime_status_data()
    runtime_data["summary"]["ok"] = False
    runtime_data["summary"]["warning_count"] = 1

    def _runtime_status(_payload):
        return runtime_data, ["runtime warning"], {}

    data, warnings, _meta = doctor_tool(
        {
            "config_path": str(tmp_path / "config.us.json"),
            "write_outputs": False,
        },
        runtime_status_tool_fn=_runtime_status,
        load_runtime_config=_load_config(tmp_path),
        repo_base=lambda: tmp_path,
        mask_path=lambda path: f".../{Path(path).name}",
        now_fn=lambda: datetime(2026, 5, 16, 2, 0, tzinfo=timezone.utc),
    )

    assert "scheduler_evidence_missing: online scheduler status was not provided" in warnings
    assert data["category"] == "scheduler_unknown"
    codes = [item["code"] for item in data["deterministic"]["findings"]]
    assert "SCHEDULER_EVIDENCE_MISSING" in codes
    assert "RUNTIME_STATUS_WARNINGS" in codes


def test_doctor_ai_uses_custom_config_and_redacted_evidence(monkeypatch, tmp_path: Path) -> None:
    from src.application.doctor.service import doctor_tool

    captured: dict[str, Any] = {}

    def _runtime_status(_payload):
        return _runtime_status_data(), [], {}

    def _ai_complete(body: dict[str, Any], ai_config: dict[str, Any]) -> dict[str, Any]:
        captured["body"] = body
        captured["ai_config"] = ai_config
        return {
            "status": "fail",
            "category": "suspected_runtime_bug",
            "confidence": "high",
            "problem": "Runtime completed but produced inconsistent scheduler evidence.",
            "impact": "Production quality is degraded.",
            "evidence": [{"source": "scheduler_evidence.stdout_tail", "observed": "redacted", "expected": "success"}],
            "ai_diagnosis": "The run should be debugged locally.",
            "strategy_observations": ["Candidate evidence is available."],
            "strategy_improvement_directions": ["Review rejected rules before changing thresholds."],
            "suspected_code_area": ["src/application/tick_scheduler_context.py"],
            "local_debug_steps": ["Inspect tick scheduler context."],
            "issue_candidate": {"create_issue": True, "reason": "Likely runtime bug."},
        }

    monkeypatch.setenv("OM_DOCTOR_AI_API_KEY", "secret-key")
    data, warnings, meta = doctor_tool(
        {
            "config_path": str(tmp_path / "config.us.json"),
            "ai": True,
            "write_outputs": False,
            "scheduler_evidence": {
                "provider": "openclaw",
                "job_name": "us-tick",
                "last_triggered_at": "2026-05-16T01:00:00Z",
                "last_status": "success",
                "last_exit_code": 0,
                "stdout_tail": "sent to https://example.com/webhook/token for 281756479859383816",
            },
            "ai_config": {
                "base_url": "https://ai.example.test/v1",
                "model": "doctor-model",
                "api_key_env": "OM_DOCTOR_AI_API_KEY",
            },
        },
        runtime_status_tool_fn=_runtime_status,
        load_runtime_config=_load_config(tmp_path),
        repo_base=lambda: tmp_path,
        mask_path=lambda path: f".../{Path(path).name}",
        ai_complete_fn=_ai_complete,
        now_fn=lambda: datetime(2026, 5, 16, 2, 0, tzinfo=timezone.utc),
    )

    evidence_json = json.dumps(captured["body"]["evidence"], ensure_ascii=False)
    assert warnings == []
    assert captured["ai_config"]["base_url"] == "https://ai.example.test/v1"
    assert captured["ai_config"]["model"] == "doctor-model"
    assert "secret-key" not in evidence_json
    assert "webhook/token" not in evidence_json
    assert "***REDACTED_URL***" in evidence_json
    assert "281756479859383816" not in evidence_json
    assert "...3816" in evidence_json
    assert data["category"] == "suspected_runtime_bug"
    assert data["ai"]["issue_candidate"]["create_issue"] is True
    assert data["ai"]["strategy_improvement_directions"] == ["Review rejected rules before changing thresholds."]
    assert meta["ai"]["api_key_env"] == "OM_DOCTOR_AI_API_KEY"


def test_doctor_ai_unavailable_handoff_keeps_deterministic_status(tmp_path: Path) -> None:
    from src.application.doctor.service import doctor_tool

    runtime_data = _runtime_status_data()
    runtime_data["freshness"] = {"status": "stale", "stale": True, "age_seconds": 7200, "max_age_minutes": 60}

    def _runtime_status(_payload):
        return runtime_data, [], {}

    data, warnings, _meta = doctor_tool(
        {
            "config_path": str(tmp_path / "config.us.json"),
            "ai": True,
            "write_outputs": False,
            "scheduler_evidence": {
                "provider": "openclaw",
                "job_name": "us-tick",
                "last_triggered_at": "2026-05-16T01:00:00Z",
                "last_status": "success",
                "last_exit_code": 0,
            },
            "ai_config": {
                "base_url": "https://ai.example.test/v1",
                "model": "doctor-model",
                "api_key_env": "OM_DOCTOR_AI_API_KEY",
            },
        },
        runtime_status_tool_fn=_runtime_status,
        load_runtime_config=_load_config(tmp_path),
        repo_base=lambda: tmp_path,
        mask_path=lambda path: f".../{Path(path).name}",
        now_fn=lambda: datetime(2026, 5, 16, 2, 0, tzinfo=timezone.utc),
    )

    assert data["status"] == "fail"
    assert data["ai"]["status"] == "unavailable"
    assert "ai_unavailable" in warnings[0]
    assert "Status: fail" in data["handoff_markdown"]
    assert "Category: runtime_failed" in data["handoff_markdown"]


def test_doctor_collects_strategy_evidence_for_ai(monkeypatch, tmp_path: Path) -> None:
    from src.application.doctor.service import doctor_tool

    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    (report_dir / "nvda_sell_put_candidates_labeled.csv").write_text(
        "symbol,option_type,dte,delta,annualized_net_return,net_income\nNVDA,put,30,-0.2,0.12,120\n",
        encoding="utf-8",
    )
    (report_dir / "candidate_filter_trace.jsonl").write_text(
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
    captured: dict[str, Any] = {}

    def _runtime_status(_payload):
        return _runtime_status_data(), [], {}

    def _ai_complete(body: dict[str, Any], _ai_config: dict[str, Any]) -> dict[str, Any]:
        captured["body"] = body
        return {
            "status": "ok",
            "category": "strategy_observation",
            "confidence": "medium",
            "problem": "No production bug detected.",
            "impact": "Strategy evidence is available for review.",
            "strategy_observations": ["One candidate row was collected."],
            "strategy_improvement_directions": ["Inspect risk_volume rejects."],
            "issue_candidate": {"create_issue": False, "reason": "No bug."},
        }

    monkeypatch.setenv("OM_DOCTOR_AI_API_KEY", "secret-key")
    data, _warnings, _meta = doctor_tool(
        {
            "config_path": str(tmp_path / "config.us.json"),
            "strategy_report_dir": str(report_dir),
            "ai": True,
            "write_outputs": False,
            "scheduler_evidence": {
                "provider": "openclaw",
                "job_name": "us-tick",
                "last_triggered_at": "2026-05-16T01:00:00Z",
                "last_status": "success",
                "last_exit_code": 0,
            },
            "ai_config": {
                "base_url": "https://ai.example.test/v1",
                "model": "doctor-model",
                "api_key_env": "OM_DOCTOR_AI_API_KEY",
            },
        },
        runtime_status_tool_fn=_runtime_status,
        load_runtime_config=_load_config(tmp_path),
        repo_base=lambda: tmp_path,
        mask_path=lambda path: f".../{Path(path).name}",
        ai_complete_fn=_ai_complete,
        now_fn=lambda: datetime(2026, 5, 16, 2, 0, tzinfo=timezone.utc),
    )

    strategy = captured["body"]["evidence"]["strategy_evidence"]
    assert strategy["summary"]["candidate_file_count"] == 1
    assert strategy["summary"]["candidate_row_count"] == 1
    assert strategy["summary"]["filter_trace_file_count"] == 1
    assert strategy["filter_traces"][0]["rule_counts"] == {"risk_volume": 1}
    assert "candidate_row_count: 1" in data["handoff_markdown"]


def test_doctor_writes_handoff_and_redacted_evidence(tmp_path: Path) -> None:
    from src.application.doctor.service import doctor_tool

    def _runtime_status(_payload):
        return _runtime_status_data(), [], {}

    data, warnings, _meta = doctor_tool(
        {
            "config_path": str(tmp_path / "config.us.json"),
            "write_outputs": True,
            "doctor_output_dir": str(tmp_path / "doctor"),
            "doctor_current_dir": str(tmp_path / "current"),
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
        load_runtime_config=_load_config(tmp_path),
        repo_base=lambda: tmp_path,
        mask_path=lambda path: f".../{Path(path).name}",
        now_fn=lambda: datetime(2026, 5, 16, 2, 0, tzinfo=timezone.utc),
    )

    assert warnings == []
    assert data["outputs"]["written"] is True
    doctor_path = tmp_path / data["outputs"]["doctor_path"]
    evidence_path = tmp_path / data["outputs"]["evidence_path"]
    handoff_path = tmp_path / data["outputs"]["handoff_path"]
    current_path = tmp_path / data["outputs"]["current_path"]
    assert doctor_path.exists()
    assert evidence_path.exists()
    assert handoff_path.read_text(encoding="utf-8").startswith("## Online Doctor Conclusion")
    assert current_path.exists()
    evidence_text = evidence_path.read_text(encoding="utf-8")
    assert "webhook/token" not in evidence_text
    assert "***REDACTED_URL***" in evidence_text


def test_doctor_defaults_to_no_output_writes(tmp_path: Path) -> None:
    from src.application.doctor.service import doctor_tool

    def _runtime_status(_payload):
        return _runtime_status_data(), [], {}

    data, _warnings, _meta = doctor_tool(
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
        load_runtime_config=_load_config(tmp_path),
        repo_base=lambda: tmp_path,
        mask_path=lambda path: f".../{Path(path).name}",
        now_fn=lambda: datetime(2026, 5, 16, 2, 0, tzinfo=timezone.utc),
    )

    assert data["outputs"] == {"written": False}
    assert not (tmp_path / "output_shared" / "doctor").exists()


def test_doctor_rejects_output_paths_outside_repo(tmp_path: Path) -> None:
    from src.application.agent_tool_contracts import AgentToolError
    from src.application.doctor.service import doctor_tool

    def _runtime_status(_payload):
        return _runtime_status_data(), [], {}

    try:
        doctor_tool(
            {
                "config_path": str(tmp_path / "config.us.json"),
                "write_outputs": True,
                "doctor_output_dir": str(tmp_path.parent / "outside-doctor"),
                "scheduler_evidence": {
                    "provider": "openclaw",
                    "job_name": "us-tick",
                    "last_triggered_at": "2026-05-16T01:00:00Z",
                    "last_status": "success",
                    "last_exit_code": 0,
                },
            },
            runtime_status_tool_fn=_runtime_status,
            load_runtime_config=_load_config(tmp_path),
            repo_base=lambda: tmp_path,
            mask_path=lambda path: f".../{Path(path).name}",
            now_fn=lambda: datetime(2026, 5, 16, 2, 0, tzinfo=timezone.utc),
        )
    except AgentToolError as exc:
        assert exc.code == "INPUT_ERROR"
    else:
        raise AssertionError("expected AgentToolError")


def test_doctor_agent_tool_write_outputs_requires_gate(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    monkeypatch.delenv("OM_AGENT_ENABLE_WRITE_TOOLS", raising=False)
    out = run_tool(
        "doctor",
        {
            "config_path": str(tmp_path / "config.us.json"),
            "write_outputs": True,
            "confirm": True,
        },
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "PERMISSION_DENIED"


def test_doctor_agent_tool_runs_with_local_runtime_artifacts(tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool as run_tool

    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(
        json.dumps(
            {
                "accounts": ["user1"],
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
        accounts_root / "user1" / "state",
        accounts_root / "user1" / "reports",
        run_dir / "state",
        run_dir / "accounts" / "user1" / "state",
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
                "accounts": [{"account": "user1", "status": "ok", "ran_scan": True}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (run_dir / "accounts" / "user1" / "state" / "last_run.json").write_text(
        json.dumps({"status": "ok", "run_id": "run-1", "ran_scan": True}),
        encoding="utf-8",
    )

    out = run_tool(
        "doctor",
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
    assert out["data"]["schema_version"] == "doctor.v1"
    assert out["data"]["status"] in {"ok", "warn"}
    assert out["data"]["outputs"]["written"] is False
    assert "Online Doctor Conclusion" in out["data"]["handoff_markdown"]
