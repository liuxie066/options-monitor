from __future__ import annotations

import json
import os
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


def test_research_reports_scheduler_failure(tmp_path: Path) -> None:
    from src.application.research.service import research_tool

    def _runtime_status(_payload):
        return _runtime_status_data(), [], {}

    data, warnings, meta = research_tool(
        {
            "config_path": str(tmp_path / "config.us.json"),
            "write_outputs": False,
            "scheduler_evidence": {
                "provider": "systemd",
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
    assert "Research Handoff" in data["handoff_markdown"]
    assert meta["outputs"]["written"] is False


def test_research_does_not_guess_missing_scheduler_evidence(tmp_path: Path) -> None:
    from src.application.research.service import research_tool

    runtime_data = _runtime_status_data()
    runtime_data["summary"]["ok"] = False
    runtime_data["summary"]["warning_count"] = 1

    def _runtime_status(_payload):
        return runtime_data, ["runtime warning"], {}

    data, warnings, _meta = research_tool(
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


def test_research_preserves_scheduler_run_id_and_downgrades_confirmed_stale_runtime(tmp_path: Path) -> None:
    from src.application.research.service import research_tool

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

    data, _warnings, _meta = research_tool(
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


def test_research_downgrades_remediated_upgrade_failure_to_warning(tmp_path: Path) -> None:
    from src.application.research.service import research_tool

    runtime_data = _runtime_status_data()
    runtime_data["summary"]["ok"] = False
    runtime_data["summary"]["warning_count"] = 1
    runtime_data["summary"]["warning_codes"] = ["SERVICE_UPGRADE_REMEDIATED"]
    runtime_data["summary"]["service_upgrade_status"] = "remediated"
    runtime_data["summary"]["service_upgrade_historical_status"] = "failed"
    runtime_data["service_upgrade"] = {
        "json": {"status": "failed", "target_version": "1.2.82"},
        "evaluation": {
            "status": "remediated",
            "historical_status": "failed",
            "target_version": "1.2.82",
            "current_version": "1.2.82",
            "runtime_failed": False,
            "warning": True,
        },
    }

    def _runtime_status(_payload):
        return runtime_data, ["service upgrade remediated"], {}

    data, _warnings, _meta = research_tool(
        {
            "config_path": str(tmp_path / "config.us.json"),
            "write_outputs": False,
            "scheduler_evidence": {
                "provider": "cron",
                "job_name": "us-tick",
                "last_triggered_at": "2026-05-16T01:55:00Z",
                "last_run_id": "run-1",
                "last_status": "success",
                "last_exit_code": 0,
            },
        },
        runtime_status_tool_fn=_runtime_status,
        **_tool_kwargs(tmp_path),
    )

    findings = data["bundle"]["runtime_quality"]["findings"]
    assert data["status"] == "warn"
    assert data["category"] == "service_upgrade_historical"
    assert any(item["code"] == "SERVICE_UPGRADE_REMEDIATED" for item in findings)
    assert not any(item["code"] == "RUNTIME_STATUS_WARNINGS" for item in findings)


def test_research_keeps_unrecovered_upgrade_failure_as_runtime_failed(tmp_path: Path) -> None:
    from src.application.research.service import research_tool

    runtime_data = _runtime_status_data()
    runtime_data["summary"]["ok"] = False
    runtime_data["summary"]["warning_count"] = 1
    runtime_data["summary"]["warning_codes"] = ["SERVICE_UPGRADE_FAILED"]
    runtime_data["summary"]["service_upgrade_status"] = "failed"
    runtime_data["summary"]["service_upgrade_runtime_failed"] = True
    runtime_data["service_upgrade"] = {
        "json": {"status": "failed", "target_version": "1.2.82"},
        "evaluation": {
            "status": "failed",
            "historical_status": "failed",
            "target_version": "1.2.82",
            "current_version": "1.2.82",
            "runtime_failed": True,
            "warning": False,
        },
    }

    def _runtime_status(_payload):
        return runtime_data, ["service upgrade failed"], {}

    data, _warnings, _meta = research_tool(
        {
            "config_path": str(tmp_path / "config.us.json"),
            "write_outputs": False,
            "scheduler_evidence": {
                "provider": "cron",
                "job_name": "us-tick",
                "last_triggered_at": "2026-05-16T01:55:00Z",
                "last_run_id": "run-1",
                "last_status": "success",
                "last_exit_code": 0,
            },
        },
        runtime_status_tool_fn=_runtime_status,
        **_tool_kwargs(tmp_path),
    )

    assert data["status"] == "fail"
    assert data["category"] == "runtime_failed"
    assert data["bundle"]["runtime_quality"]["findings"][0]["code"] == "SERVICE_UPGRADE_FAILED"


def test_research_collects_candidate_evidence_for_handoff(tmp_path: Path) -> None:
    from src.application.research.service import research_tool

    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    account_report_dir = report_dir / "accounts" / "lx"
    account_report_dir.mkdir(parents=True)
    (account_report_dir / "nvda_sell_put_candidates_labeled.csv").write_text(
        (
            "symbol,account,option_type,dte,delta,strike,spot,annualized_net_return_on_cash_basis,"
            "net_income,otm_pct,iv_rv_ratio,spread_ratio,single_trade_concentration,open_interest,volume,"
            "cash_required_usd,cash_free_usd\n"
            "NVDA,lx,put,30,-0.2,140,150,0.12,120,0.066667,1.25,0.12,0.04,500,20,14000,28000\n"
        ),
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

    data, _warnings, _meta = research_tool(
        {
            "config_path": str(tmp_path / "config.us.json"),
            "candidate_report_dir": str(report_dir),
            "write_outputs": False,
            "scheduler_evidence": {
                "provider": "systemd",
                "job_name": "us-tick",
                "last_triggered_at": "2026-05-16T01:00:00Z",
                "last_status": "success",
                "last_exit_code": 0,
            },
        },
        runtime_status_tool_fn=_runtime_status,
        **_tool_kwargs(tmp_path),
    )

    summary = data["bundle"]["candidate_evidence"]["summary"]
    reject_logs = data["bundle"]["candidate_evidence"]["reject_logs"]
    ranking = data["bundle"]["candidate_evidence"]["ranking_evidence"]
    shadow_replay = data["bundle"]["candidate_evidence"]["shadow_replay"]
    ranking_row = ranking["reports"][0]["top_rows"][0]
    account_candidate = data["bundle"]["account_candidate_matrix"]["accounts"]["lx"]["candidate_evidence"]
    assert data["status"] == "ok"
    assert summary["candidate_row_count"] == 1
    assert summary["candidate_file_count"] == 1
    assert summary["reject_log_row_count"] == 1
    assert summary["ranking_report_count"] == 1
    assert summary["ranking_top_row_count"] == 1
    assert summary["shadow_replay_status"] == "not_ready"
    assert reject_logs[0]["reason_counts"] == {"risk_spread": 1}
    assert reject_logs[0]["sample_rows"][0]["engine_reject_reason"] == "risk_spread"
    assert ranking["summary"]["strategy_counts"] == {"sell_put": 1}
    assert ranking_row["metrics"]["annualized_return"] == 0.12
    assert ranking_row["metrics"]["otm_pct"] == 0.066667
    assert ranking_row["cash_constraint"]["cash_headroom_ratio"] == 2.0
    assert ranking_row["rank_explanation"]["score_inputs"]["spread_ratio"] == 0.12
    assert shadow_replay["schema_version"] == "shadow_replay_readiness.v1"
    assert shadow_replay["summary"]["candidate_snapshot_count"] == 3
    assert shadow_replay["summary"]["counterfactual_candidate_count"] == 2
    assert shadow_replay["summary"]["reason"] == "candidate_snapshot_count_below_min_sample"
    assert shadow_replay["bucket_stats"]["dte"]["30-44"]["count"] == 1
    assert shadow_replay["bucket_stats"]["dte"]["missing"]["count"] == 2
    assert shadow_replay["evidence_checks"]["survivorship_bias_risk"] == "medium"
    assert shadow_replay["safety"]["writes_runtime_config"] is False
    assert account_candidate["candidate_rows"] == 1
    assert account_candidate["reject_log_rows"] == 1
    assert account_candidate["trace_rows"] == 1
    assert account_candidate["trace_status_counts"] == {"rejected": 1}
    assert "candidate_rows: 1" in data["handoff_markdown"]
    assert "reject_log_rows: 1" in data["handoff_markdown"]
    assert "## Ranking Evidence" in data["handoff_markdown"]
    assert "cash_headroom=2" in data["handoff_markdown"]


def test_research_shadow_replay_uses_mark_and_outcome_paths(tmp_path: Path) -> None:
    from src.application.research.service import research_tool

    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    candidate_path = report_dir / "sell_put_candidates.csv"
    trace_path = report_dir / "candidate_filter_trace.jsonl"
    mark_path = report_dir / "mark_path_snapshots.jsonl"
    outcome_path = report_dir / "outcome_facts.jsonl"
    candidate_path.write_text(
        (
            "symbol,account,option_type,contract_symbol,dte,delta,strike,iv_rv_ratio,spread_ratio\n"
            "NVDA,lx,put,NVDA260619P00100000,30,-0.2,100,1.25,0.10\n"
        ),
        encoding="utf-8",
    )
    trace_path.write_text(
        json.dumps(
            {
                "symbol": "AMD",
                "account": "lx",
                "function": "sell_put",
                "mode": "put",
                "contract_symbol": "AMD260619P00080000",
                "status": "rejected",
                "rule": "spread_too_wide",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    mark_path.write_text(
        "\n".join(
            [
                json.dumps({"contract_symbol": "NVDA260619P00100000", "unrealized_pnl": 20}),
                json.dumps({"contract_symbol": "AMD260619P00080000", "unrealized_pnl": -50}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    outcome_path.write_text(
        "\n".join(
            [
                json.dumps({"contract_symbol": "NVDA260619P00100000", "outcome": "expired_worthless", "realized_pnl": 100}),
                json.dumps({"contract_symbol": "AMD260619P00080000", "outcome": "would_close_loss", "realized_pnl": -60}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    def _runtime_status(_payload):
        return _runtime_status_data(), [], {}

    data, _warnings, _meta = research_tool(
        {
            "config_path": str(tmp_path / "config.us.json"),
            "candidate_paths": [str(candidate_path)],
            "trace_paths": [str(trace_path)],
            "mark_paths": [str(mark_path)],
            "outcome_paths": [str(outcome_path)],
            "shadow_replay_min_sample": 2,
            "write_outputs": False,
            "scheduler_evidence": {
                "provider": "systemd",
                "job_name": "us-tick",
                "last_triggered_at": "2026-05-16T01:00:00Z",
                "last_status": "success",
                "last_exit_code": 0,
            },
        },
        runtime_status_tool_fn=_runtime_status,
        **_tool_kwargs(tmp_path),
    )

    shadow_replay = data["bundle"]["candidate_evidence"]["shadow_replay"]
    assert data["status"] == "ok"
    assert data["bundle"]["candidate_evidence"]["summary"]["shadow_replay_status"] == "needs_human_review"
    assert shadow_replay["summary"]["evidence_level"] == "outcome_incomplete"
    assert shadow_replay["outcome_coverage"]["marked_instrument_count"] == 2
    assert shadow_replay["path_risk"]["by_status"]["rejected"]["max_adverse_pnl"] == -50
    assert shadow_replay["outcome_stats"]["by_status"]["accepted"]["realized_pnl_total"] == 100


def test_research_collects_candidate_evidence_from_profile_runtime_root(tmp_path: Path) -> None:
    from src.application.research.service import research_tool

    runtime_root = tmp_path.parent / f"{tmp_path.name}-runtime"
    runs_root = runtime_root / "output_runs"
    run_dir = runs_root / "run-1"
    account_run_dir = run_dir / "accounts" / "lx"
    account_run_dir.mkdir(parents=True)
    (account_run_dir / "nvda_sell_put_candidates_labeled.csv").write_text(
        (
            "symbol,account,option_type,dte,delta,strike,spot,annualized_net_return_on_cash_basis,"
            "net_income,otm_pct,spread_ratio,open_interest,volume,cash_required_usd,cash_free_usd\n"
            "NVDA,lx,put,30,-0.2,140,150,0.12,120,0.066667,0.12,500,20,14000,28000\n"
        ),
        encoding="utf-8",
    )
    (account_run_dir / "nvda_sell_put_candidates_reject_log.csv").write_text(
        "symbol,reject_stage,engine_reject_stage,engine_reject_reason\nNVDA,step3_risk_gate,stage3_risk_filter,risk_spread\n",
        encoding="utf-8",
    )
    (account_run_dir / "candidate_filter_trace.jsonl").write_text(
        json.dumps({"run_id": "run-1", "account": "lx", "status": "rejected", "rule": "risk_volume"}) + "\n",
        encoding="utf-8",
    )
    profile_path = tmp_path / "service.profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "runtime_root": str(runtime_root),
                "paths": {
                    "runs_root": str(runs_root),
                    "report_dir": str(runtime_root / "output_shared" / "reports"),
                    "shared_state_dir": str(runtime_root / "output_shared" / "state"),
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    runtime_data = _runtime_status_data()
    runtime_data["summary"]["latest_run_path"] = ".../run-1"
    runtime_data["summary"]["latest_scanned_run_path"] = ".../run-1"
    runtime_data["latest_run"]["path"] = ".../run-1"
    runtime_data["latest_scanned_run"]["path"] = ".../run-1"
    runtime_data["paths"] = {
        "runs_root": ".../output_runs",
        "shared_state_dir": ".../state",
    }

    def _runtime_status(_payload):
        return runtime_data, [], {}

    data, _warnings, _meta = research_tool(
        {
            "config_path": str(tmp_path / "config.us.json"),
            "profile_path": str(profile_path),
            "write_outputs": False,
            "scheduler_evidence": {
                "provider": "systemd",
                "job_name": "us-tick",
                "last_triggered_at": "2026-05-16T01:00:00Z",
                "last_status": "success",
                "last_exit_code": 0,
            },
        },
        runtime_status_tool_fn=_runtime_status,
        **_tool_kwargs(tmp_path),
    )

    summary = data["bundle"]["candidate_evidence"]["summary"]
    account_candidate = data["bundle"]["account_candidate_matrix"]["accounts"]["lx"]["candidate_evidence"]
    assert summary["candidate_row_count"] == 1
    assert summary["reject_log_row_count"] == 1
    assert summary["filter_trace_file_count"] == 1
    assert summary["ranking_report_count"] == 1
    assert account_candidate["candidate_rows"] == 1
    assert account_candidate["reject_log_rows"] == 1
    assert account_candidate["trace_rows"] == 1


def test_research_builds_redacted_bundle_and_handoff(tmp_path: Path) -> None:
    from src.application.research.service import research_tool

    runtime_data = _runtime_status_data()
    runtime_data["latest_run"]["state"]["tick_metrics"]["json"]["accounts"] = [
        {"account": "lx", "status": "ok", "ran_scan": True, "should_notify": True},
        {"account": "sy", "status": "ok", "ran_scan": True, "should_notify": False, "reason": "no candidates"},
    ]

    def _runtime_status(_payload):
        return runtime_data, [], {}

    data, warnings, meta = research_tool(
        {
            "scope": "full",
            "config_path": str(tmp_path / "config.us.json"),
            "write_outputs": False,
            "scheduler_evidence": {
                "provider": "systemd",
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
    assert data["schema_version"] == "research.v1"
    assert bundle["schema_version"] == "research_bundle.v2"
    assert bundle["ledger_quality"]["status"] == "ok"
    assert sorted(bundle["account_candidate_matrix"]["accounts"]) == ["lx", "sy"]
    assert bundle["healthcheck_snapshot"] == {
        "status": "skipped",
        "included": False,
        "reason": "include_healthcheck=false",
    }
    assert bundle["runtime_runs"]["schema_version"] == "runtime_runs.v1"
    assert bundle["runtime_logs"]["schema_version"] == "runtime_logs.v1"
    assert "Research Handoff" in data["handoff_markdown"]
    assert "Runtime Evidence" in data["handoff_markdown"]
    assert "webhook/token" not in bundle_json
    assert "281756479859383816" not in bundle_json
    assert meta["outputs"]["written"] is False


def test_research_ledger_quality_uses_projection_verify_evidence(tmp_path: Path) -> None:
    from src.application.research.service import research_tool

    runtime_data = _runtime_status_data()
    runtime_data["projection_verify"] = {
        "exists": True,
        "path": "output_shared/state/option_positions/current/projection_verify.latest.json",
        "json": {
            "ok": True,
            "mode_used": "full_replay",
            "event_count": 37,
            "position_lot_count": 33,
            "projected_lot_count": 33,
            "projection_error_count": 0,
            "summary": {"matched": 33},
        },
    }

    def _runtime_status(_payload):
        return runtime_data, [], {}

    data, warnings, _meta = research_tool(
        {
            "scope": "full",
            "config_path": str(tmp_path / "config.us.json"),
            "write_outputs": False,
            "scheduler_evidence": {
                "provider": "systemd",
                "job_name": "us-tick",
                "last_triggered_at": "2026-05-16T01:00:00Z",
                "last_status": "success",
                "last_exit_code": 0,
            },
        },
        runtime_status_tool_fn=_runtime_status,
        **_tool_kwargs(tmp_path),
    )

    ledger = data["bundle"]["ledger_quality"]
    assert warnings == []
    assert ledger["status"] == "ok"
    assert ledger["known_gap"] is None
    assert ledger["projection_verify"]["status"] == "ok"
    assert ledger["projection_verify"]["event_count"] == 37
    assert ledger["projection_verify"]["position_lot_count"] == 33
    assert "projection_verify: status=ok" in data["handoff_markdown"]


def test_research_can_include_redacted_healthcheck_snapshot(tmp_path: Path) -> None:
    from src.application.research.service import research_tool

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
                        "name": "notification_credentials",
                        "status": "error",
                        "message": "missing https://example.com/webhook/token for 281756479859383816",
                    }
                ],
            },
            ["notification target 281756479859383816 is not ready"],
            {"config_path": str(tmp_path / "config.us.json")},
        )

    data, warnings, meta = research_tool(
        {
            "scope": "full",
            "config_path": str(tmp_path / "config.us.json"),
            "include_healthcheck": True,
            "write_outputs": False,
            "scheduler_evidence": {
                "provider": "systemd",
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


def test_research_healthcheck_loads_env_file_from_service_profile(monkeypatch, tmp_path: Path) -> None:
    from src.application.research.service import research_tool

    profile_path = tmp_path / "service.profile.json"
    env_path = tmp_path / "options-monitor.env"
    env_path.write_text("OM_TEST_PROFILE_ENV=loaded\n", encoding="utf-8")
    profile_path.write_text(json.dumps({"env_file": str(env_path)}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.delenv("OM_TEST_PROFILE_ENV", raising=False)

    def _runtime_status(_payload):
        return _runtime_status_data(), [], {}

    def _healthcheck(payload):
        assert payload["profile_path"] == str(profile_path)
        assert os.environ.get("OM_TEST_PROFILE_ENV") == "loaded"
        return (
            {
                "summary": {"ok": True, "critical_count": 0, "warning_count": 0},
                "config": {"config_path": str(tmp_path / "config.us.json"), "accounts": ["lx"]},
                "account_paths": {},
                "checks": [],
            },
            [],
            {"config_path": str(tmp_path / "config.us.json")},
        )

    data, warnings, meta = research_tool(
        {
            "scope": "full",
            "config_path": str(tmp_path / "config.us.json"),
            "profile_path": str(profile_path),
            "include_healthcheck": True,
            "write_outputs": False,
            "scheduler_evidence": {
                "provider": "systemd",
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

    assert warnings == []
    assert data["bundle"]["healthcheck_snapshot"]["status"] == "ok"
    assert meta["healthcheck"]["env_file_loaded"] is True
    assert meta["healthcheck"]["env_file_key_count"] == 1
    assert os.environ.get("OM_TEST_PROFILE_ENV") is None


def test_research_writes_bundle_and_handoff(tmp_path: Path) -> None:
    from src.application.research.service import research_tool

    def _runtime_status(_payload):
        return _runtime_status_data(), [], {}

    data, warnings, _meta = research_tool(
        {
            "config_path": str(tmp_path / "config.us.json"),
            "write_outputs": True,
            "research_output_dir": str(tmp_path / "research"),
            "research_current_dir": str(tmp_path / "current"),
            "scheduler_evidence": {
                "provider": "systemd",
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
    assert handoff_path.read_text(encoding="utf-8").startswith("## Research Handoff")
    assert current_path.exists()
    bundle_text = bundle_path.read_text(encoding="utf-8")
    assert "webhook/token" not in bundle_text
    assert "***REDACTED_URL***" in bundle_text


def test_research_defaults_to_no_output_writes(tmp_path: Path) -> None:
    from src.application.research.service import research_tool

    def _runtime_status(_payload):
        return _runtime_status_data(), [], {}

    data, _warnings, _meta = research_tool(
        {
            "config_path": str(tmp_path / "config.us.json"),
            "scheduler_evidence": {
                "provider": "systemd",
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
    assert not (tmp_path / "output_shared" / "research").exists()


def test_research_rejects_output_paths_outside_repo(tmp_path: Path) -> None:
    from src.application.agent_tool_contracts import AgentToolError
    from src.application.research.service import research_tool

    def _runtime_status(_payload):
        return _runtime_status_data(), [], {}

    try:
        research_tool(
            {
                "config_path": str(tmp_path / "config.us.json"),
                "write_outputs": True,
                "research_output_dir": str(tmp_path.parent / "outside-research"),
                "scheduler_evidence": {
                    "provider": "systemd",
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


def test_research_collect_write_outputs_requires_confirm(tmp_path: Path) -> None:
    from src.application.research.facade import run_research_collect

    out = run_research_collect(
        {
            "config_path": str(tmp_path / "config.us.json"),
            "write_outputs": True,
            "confirm": False,
        },
    )

    assert out["ok"] is False
    assert out["tool_name"] == "research.collect"
    assert out["error"]["code"] == "CONFIRMATION_REQUIRED"


def test_research_collect_runs_with_local_runtime_artifacts(tmp_path: Path) -> None:
    from src.application.research.facade import run_research_collect

    cfg_path = tmp_path / "config.us.json"
    cfg_path.write_text(
        json.dumps(
            {
                "_generated": {"market": "us", "source_format": "yaml"},
                "accounts": ["lx"],
                "symbols": [],
                "notifications": {
                    "provider": "wechat_clawbot",
                    "channel": "wechat_clawbot",
                    "target": "clawbot:test-room",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    shared_state_dir = tmp_path / "output_shared" / "state"
    report_dir = tmp_path / "output_shared" / "reports"
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
    (run_dir / "state" / "tool_execution_audit.jsonl").write_text(
        '{"tool_name":"tick","status":"ok"}\n',
        encoding="utf-8",
    )

    out = run_research_collect(
        {
            "config_path": str(cfg_path),
            "shared_state_dir": str(shared_state_dir),
            "report_dir": str(report_dir),
            "accounts_root": str(accounts_root),
            "runs_root": str(runs_root),
            "write_outputs": False,
            "scheduler_evidence": {
                "provider": "systemd",
                "job_name": "us-tick",
                "last_triggered_at": "2026-05-16T01:00:00Z",
                "last_status": "success",
                "last_exit_code": 0,
            },
        },
    )

    assert out["ok"] is True
    assert out["tool_name"] == "research.collect"
    assert out["data"]["schema_version"] == "research.v1"
    assert out["data"]["status"] in {"ok", "warn"}
    assert out["data"]["outputs"]["written"] is False
    assert "Research Handoff" in out["data"]["handoff_markdown"]
    bundle = out["data"]["bundle"]
    assert bundle["runtime_runs"]["summary"]["total_count"] == 1
    assert bundle["runtime_runs"]["runs"][0]["run_id"] == "run-1"
    assert bundle["runtime_logs"]["summary"]["existing_file_count"] == 1


def test_research_collects_combo_yield_pair_diagnostics(tmp_path: Path) -> None:
    from src.application.research.service import research_tool

    report_dir = tmp_path / "reports"
    header = (
        "run_id,account,diagnostic_scope,diagnostic_stage,accepted,reject_reasons,symbol,expiration,"
        "put_contract_symbol,call_contract_symbol,call_delta,call_open_interest,call_volume,call_spread_ratio,"
        "combo_net_credit,net_debit,net_credit_retention,annualized_net_credit_yield,combo_spread_ratio,"
        "policy_call_min_delta,policy_call_max_delta,policy_call_min_open_interest,policy_call_min_volume,"
        "policy_call_max_spread_ratio,policy_max_debit_native,policy_min_net_credit_retention,"
        "policy_min_net_credit_annualized,policy_max_combo_spread_ratio\n"
    )
    for account in ("lx", "sy"):
        account_dir = report_dir / "accounts" / account
        account_dir.mkdir(parents=True)
        rows = (
            f"run-1,{account},call,call_filter,False,call_delta_below_min,NVDA,2026-08-21,,NVDA-C170,"
            "0.04,100,10,0.10,,,,,,0.05,0.20,20,0,0.30,,,,\n"
            f"run-1,{account},pair,pair_filter,False,annualized_net_credit_yield|min_net_credit_retention|combo_spread_ratio,"
            "NVDA,2026-08-21,NVDA-P140,NVDA-C170,0.10,100,10,0.10,1.0,0,0.78,0.075,0.31,"
            "0.05,0.20,20,0,0.30,,0.80,0.08,0.30\n"
            f"run-1,{account},pair,pair_filter,True,,NVDA,2026-08-21,NVDA-P140,NVDA-C175,"
            "0.08,100,10,0.10,1.2,0,0.85,0.09,0.20,0.05,0.20,20,0,0.30,,0.80,0.08,0.30\n"
        )
        if account == "sy":
            rows += (
                "run-1,sy,call,call_filter,False,call_delta_below_min,NVDA,2026-08-21,,NVDA-C170,"
                "0.03,100,10,0.10,,,,,,0.05,0.20,20,0,0.30,,,,\n"
            )
        (account_dir / "nvda_combo_yield_pair_diagnostics.csv").write_text(header + rows, encoding="utf-8")

    def _runtime_status(_payload):
        return _runtime_status_data(), [], {}

    kwargs = _tool_kwargs(tmp_path)
    kwargs["load_runtime_config"] = _load_config(tmp_path, {"accounts": ["lx", "sy"], "symbols": []})
    data, _warnings, _meta = research_tool(
        {
            "config_path": str(tmp_path / "config.us.json"),
            "candidate_report_dir": str(report_dir),
            "scope": "candidate",
            "write_outputs": False,
            "scheduler_evidence": {
                "provider": "systemd",
                "job_name": "us-tick",
                "last_triggered_at": "2026-07-16T08:00:00Z",
                "last_status": "success",
                "last_exit_code": 0,
            },
        },
        runtime_status_tool_fn=_runtime_status,
        **kwargs,
    )

    candidate = data["bundle"]["candidate_evidence"]
    diagnostics = candidate["combo_yield_pair_diagnostics"]
    summary = diagnostics["summary"]
    assert summary["file_count"] == 2
    assert summary["row_count"] == 7
    assert summary["unique_market_row_count"] == 4
    assert summary["status_counts"] == {"rejected": 5, "accepted": 2}
    assert summary["unique_status_counts"] == {"rejected": 3, "accepted": 1}
    assert summary["unique_reject_reason_counts"] == {
        "call_delta_below_min": 2,
        "annualized_net_credit_yield": 1,
        "min_net_credit_retention": 1,
        "combo_spread_ratio": 1,
    }
    assert summary["unique_rejection_funnel"] == [
        {
            "stage": "call_filter",
            "row_count": 2,
            "accepted_count": 0,
            "rejected_count": 2,
            "unknown_count": 0,
            "reject_reason_counts": {"call_delta_below_min": 2},
        },
        {
            "stage": "pair_filter",
            "row_count": 2,
            "accepted_count": 1,
            "rejected_count": 1,
            "unknown_count": 0,
            "reject_reason_counts": {
                "annualized_net_credit_yield": 1,
                "min_net_credit_retention": 1,
                "combo_spread_ratio": 1,
            },
        },
    ]
    assert candidate["summary"]["evidence_level"] == "pair_diagnostics"
    assert candidate["summary"]["combo_yield_pair_diagnostic_unique_market_row_count"] == 4
    delta_miss = diagnostics["nearest_misses"]["call_delta_below_min"][0]
    assert round(delta_miss["gap"], 6) == 0.01
    assert delta_miss["accounts"] == ["lx", "sy"]
    assert round(diagnostics["nearest_misses"]["annualized_net_credit_yield"][0]["gap"], 6) == 0.005
    assert round(diagnostics["nearest_misses"]["min_net_credit_retention"][0]["gap"], 6) == 0.02
    assert round(diagnostics["nearest_misses"]["combo_spread_ratio"][0]["gap"], 6) == 0.01
    assert "## Combo Yield Pair Diagnostics" in data["handoff_markdown"]
    assert "unique_market_rows: 4" in data["handoff_markdown"]
    assert "call_filter: rows=2 accepted=0 rejected=2" in data["handoff_markdown"]
