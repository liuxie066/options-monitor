from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _write_run(root: Path, run_id: str = "run-1") -> Path:
    run_dir = root / "output_runs" / run_id
    account_dir = run_dir / "accounts" / "lx"
    state_dir = run_dir / "state"
    account_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    (state_dir / "last_run.json").write_text(json.dumps({"run_id": run_id, "status": "ok"}), encoding="utf-8")
    (account_dir / "nvda_sell_put_candidates_labeled.csv").write_text(
        (
            "symbol,account,option_type,contract_symbol,dte,delta,strike,spot,annualized_net_return_on_cash_basis,"
            "spread_ratio,open_interest,volume\n"
            "NVDA,lx,put,NVDA260619P00100000,30,-0.2,100,120,0.12,0.10,500,20\n"
        ),
        encoding="utf-8",
    )
    (account_dir / "candidate_filter_trace.jsonl").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "account": "lx",
                "symbol": "AMD",
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
    return run_dir


def _write_state_only_run(root: Path, run_id: str = "run-state-only") -> Path:
    run_dir = root / "output_runs" / run_id
    state_dir = run_dir / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "tick_metrics.json").write_text(
        json.dumps(
            {
                "run_dir": str(run_dir),
                "scheduler_decision": {"should_run_scan": False, "reason": "outside window"},
                "accounts": [{"account": "lx", "ran_scan": False}],
                "ran_scan": False,
            }
        ),
        encoding="utf-8",
    )
    return run_dir


def _write_hk_run(root: Path, run_id: str = "run-hk") -> Path:
    run_dir = root / "output_runs" / run_id
    account_dir = run_dir / "accounts" / "lx"
    state_dir = run_dir / "state"
    account_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    (state_dir / "last_run.json").write_text(json.dumps({"run_id": run_id, "status": "ok"}), encoding="utf-8")
    (account_dir / "0700.hk_sell_put_candidates_labeled.csv").write_text(
        (
            "symbol,account,option_type,contract_symbol,dte,delta,strike,spot,annualized_net_return_on_cash_basis,"
            "spread_ratio,open_interest,volume\n"
            "0700.HK,lx,put,HK.TCH260619P400000,30,-0.2,400,450,0.12,0.10,500,20\n"
        ),
        encoding="utf-8",
    )
    (account_dir / "candidate_filter_trace.jsonl").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "account": "lx",
                "symbol": "0700.HK",
                "function": "sell_put",
                "mode": "put",
                "contract_symbol": "HK.TCH260619P400000",
                "status": "rejected",
                "rule": "spread_too_wide",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return run_dir


def _write_trace_only_run(root: Path, run_id: str = "run-trace-only") -> Path:
    run_dir = root / "output_runs" / run_id
    account_dir = run_dir / "accounts" / "lx"
    state_dir = run_dir / "state"
    account_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    (state_dir / "last_run.json").write_text(
        json.dumps({"run_id": run_id, "status": "ok", "ran_scan": True}),
        encoding="utf-8",
    )
    (account_dir / "candidate_filter_trace.jsonl").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "account": "lx",
                "symbol": "NVDA",
                "function": "sell_put",
                "mode": "put",
                "contract_symbol": "NVDA260619P00100000",
                "status": "rejected",
                "rule": "spread_too_wide",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return run_dir


def _fixed_now() -> datetime:
    return datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)


def _verify_remote_archive(repo_root: Path, archive_root: Path) -> None:
    from src.application.research.archive import _run_inventory, archive_verify

    inventory = _run_inventory(archive_root / "output_runs", base=repo_root)
    archive_verify(
        repo_root=repo_root,
        archive_root=archive_root,
        now_fn=_fixed_now,
        source_identity={
            "kind": "ssh",
            "ssh_target": "deploy@example",
            "runtime_root": "/var/lib/options-monitor",
            "source_host": "prod.example",
        },
        source_run_inventory=inventory,
    )


def _remote_inventory_payload(repo_root: Path, archive_root: Path) -> dict[str, Any]:
    from src.application.research.archive import _run_inventory

    return {
        "runtime_root": "/var/lib/options-monitor",
        "runs_root": "/var/lib/options-monitor/output_runs",
        "source_host": "prod.example",
        "runs": _run_inventory(archive_root / "output_runs", base=repo_root),
    }


def test_archive_verify_writes_latest_inventory(tmp_path: Path) -> None:
    from src.application.research.archive import archive_verify

    archive_root = tmp_path / "archive"
    _write_run(archive_root)

    data = archive_verify(repo_root=tmp_path, archive_root=archive_root, now_fn=_fixed_now)

    latest_path = archive_root / "manifests" / "inventory.latest.json"
    assert data["ok"] is True
    assert data["summary"]["verified_run_count"] == 1
    assert data["summary"]["replay_evidence_run_count"] == 1
    assert data["runs"][0]["run_id"] == "run-1"
    assert data["runs"][0]["verified"] is True
    assert data["runs"][0]["has_replay_evidence"] is True
    assert latest_path.exists()
    assert json.loads(latest_path.read_text(encoding="utf-8"))["verified_at_utc"] == "2026-06-04T12:00:00Z"


def test_archive_pull_defaults_to_rsync_dry_run_and_filters_local_runs(tmp_path: Path) -> None:
    from src.application.research.archive import archive_pull

    source = tmp_path / "source"
    _write_run(source, "run-1")
    calls: list[list[str]] = []

    def _run_cmd(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="dry\n", stderr="")

    data = archive_pull(
        repo_root=tmp_path,
        archive_root=tmp_path / "archive",
        source_root=source,
        run_ids=["run-1"],
        write=False,
        run_cmd=_run_cmd,
    )

    assert data["ok"] is True
    assert data["dry_run"] is True
    assert data["changed"] is False
    assert data["selected_run_ids"] == ["run-1"]
    assert calls
    assert all("--dry-run" in command for command in calls)
    assert any("output_runs/run-1" in command[-2] for command in calls)
    assert not (tmp_path / "archive" / "manifests" / "inventory.latest.json").exists()


def test_archive_pull_can_auto_select_local_replay_evidence_runs(tmp_path: Path) -> None:
    from src.application.research.archive import archive_pull

    source = tmp_path / "source"
    _write_run(source, "run-1")
    _write_state_only_run(source, "run-state-only")
    calls: list[list[str]] = []

    def _run_cmd(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="dry\n", stderr="")

    data = archive_pull(
        repo_root=tmp_path,
        archive_root=tmp_path / "archive",
        source_root=source,
        require_replay_evidence=True,
        write=False,
        run_cmd=_run_cmd,
    )

    assert data["ok"] is True
    assert data["require_replay_evidence"] is True
    assert data["selected_run_ids"] == ["run-1"]
    assert any("output_runs/run-1" in command[-2] for command in calls)
    assert not any("output_runs/run-state-only" in command[-2] for command in calls)


def test_archive_pull_can_auto_select_remote_replay_evidence_runs_without_stdout_truncation(tmp_path: Path) -> None:
    from src.application.research.archive import archive_pull

    calls: list[list[str]] = []
    inventory = {
        "runtime_root": "/var/lib/options-monitor",
        "runs_root": "/var/lib/options-monitor/output_runs",
        "padding": "x" * 6000,
        "runs": [
            {
                "run_id": "run-scan",
                "mtime": 1,
                "has_replay_evidence": True,
                "critical_files": {"candidate_files": ["accounts/lx/sell_put_candidates.csv"]},
            }
        ],
    }

    def _run_cmd(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[0] == "ssh":
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(inventory), stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="dry\n", stderr="")

    data = archive_pull(
        repo_root=tmp_path,
        archive_root=tmp_path / "archive",
        ssh_target="deploy@example",
        require_replay_evidence=True,
        write=False,
        run_cmd=_run_cmd,
    )

    assert data["ok"] is True
    assert data["selected_run_ids"] == ["run-scan"]
    assert data["operations"][0]["stdout"].startswith("{")
    assert "--dry-run" in calls[1]
    assert "output_runs/run-scan" in calls[1][-2]


def test_archive_pull_treats_missing_optional_remote_dirs_as_skipped(tmp_path: Path) -> None:
    from src.application.research.archive import archive_pull

    source = tmp_path / "source"
    _write_run(source, "run-1")

    def _run_cmd(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        source_arg = command[-2]
        if "output_shared/required_data" in source_arg:
            return subprocess.CompletedProcess(
                command,
                23,
                stdout="",
                stderr='rsync: [Receiver] change_dir "/var/lib/options-monitor/output_shared/required_data" failed: No such file or directory (2)',
            )
        return subprocess.CompletedProcess(command, 0, stdout="dry\n", stderr="")

    data = archive_pull(
        repo_root=tmp_path,
        archive_root=tmp_path / "archive",
        source_root=source,
        run_ids=["run-1"],
        write=False,
        run_cmd=_run_cmd,
    )

    skipped = [item for item in data["operations"] if item.get("skipped")]
    assert data["ok"] is True
    assert skipped[0]["reason"] == "source_dir_missing"


def test_archive_build_datasets_uses_verified_archive_runs(tmp_path: Path) -> None:
    from src.application.research.archive import archive_build_datasets

    archive_root = tmp_path / "archive"
    _write_run(archive_root, "run-1")
    _verify_remote_archive(tmp_path, archive_root)

    data = archive_build_datasets(
        repo_root=tmp_path,
        archive_root=archive_root,
        remote="prod",
        market="us",
        write=True,
    )

    dataset_dir = tmp_path / "output_shared" / "research" / "shadow_replay" / "datasets" / "prod-us-run-1"
    assert data["ok"] is True
    assert data["changed"] is True
    assert data["selected_run_ids"] == ["run-1"]
    assert (dataset_dir / "manifest.json").exists()
    manifest = json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["dataset_id"] == "prod-us-run-1"
    assert manifest["summary"]["candidate_snapshot_count"] == 2


def test_archive_build_datasets_filters_verified_runs_by_market(tmp_path: Path) -> None:
    from src.application.research.archive import archive_build_datasets

    archive_root = tmp_path / "archive"
    _write_run(archive_root, "run-us")
    _write_hk_run(archive_root, "run-hk")
    _verify_remote_archive(tmp_path, archive_root)

    data = archive_build_datasets(
        repo_root=tmp_path,
        archive_root=archive_root,
        remote="prod",
        market="us",
        write=False,
    )

    assert data["ok"] is True
    assert data["selected_run_ids"] == ["run-us"]
    assert data["market_filter"]["requested_market"] == "us"
    assert data["market_filter"]["skipped_run_count"] == 1
    assert data["market_filter"]["skipped_runs"] == [
        {"run_id": "run-hk", "inferred_market": "hk", "reason": "market_mismatch"}
    ]


def test_archive_build_datasets_infers_market_from_trace_only_run(tmp_path: Path) -> None:
    from src.application.research.archive import archive_build_datasets, archive_verify

    archive_root = tmp_path / "archive"
    _write_trace_only_run(archive_root)
    archive_verify(repo_root=tmp_path, archive_root=archive_root, now_fn=_fixed_now)

    data = archive_build_datasets(
        repo_root=tmp_path,
        archive_root=archive_root,
        remote="prod",
        market="us",
        write=False,
    )

    assert data["ok"] is True
    assert data["selected_run_ids"] == ["run-trace-only"]
    assert data["market_filter"]["skipped_runs"] == []


def test_archive_build_datasets_marks_from_archived_run_required_data(tmp_path: Path) -> None:
    from src.application.research.archive import archive_build_datasets, archive_verify

    archive_root = tmp_path / "archive"
    run_dir = _write_run(archive_root, "run-1")
    parsed = run_dir / "required_data" / "parsed"
    parsed.mkdir(parents=True)
    (parsed / "NVDA_required_data.csv").write_text(
        (
            "symbol,option_type,contract_symbol,expiration,strike,bid,ask,last_price,multiplier\n"
            "NVDA,put,NVDA260619P00100000,2026-06-19,100,1.0,1.4,1.2,100\n"
        ),
        encoding="utf-8",
    )
    (parsed / "AMD_required_data.csv").write_text(
        (
            "symbol,option_type,contract_symbol,expiration,strike,bid,ask,last_price,multiplier\n"
            "AMD,put,AMD260619P00080000,2026-06-19,80,1.4,1.8,1.6,100\n"
        ),
        encoding="utf-8",
    )
    archive_verify(repo_root=tmp_path, archive_root=archive_root, now_fn=_fixed_now)

    data = archive_build_datasets(
        repo_root=tmp_path,
        archive_root=archive_root,
        remote="prod",
        market="us",
        write=True,
    )

    dataset_dir = tmp_path / "output_shared" / "research" / "shadow_replay" / "datasets" / "prod-us-run-1"
    marks = [json.loads(line) for line in (dataset_dir / "mark_path_snapshots.jsonl").read_text(encoding="utf-8").splitlines()]
    manifest = json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8"))

    assert data["ok"] is True
    assert data["built"][0]["post_build_marking"]["status"] == "marked"
    assert data["built"][0]["post_build_marking"]["summary"]["generated_mark_snapshot_count"] == 2
    assert len(marks) == 2
    assert manifest["post_build"]["mark_from_run_required_data"]["status"] == "marked"


def test_archive_prune_remote_requires_verified_delete_runs(tmp_path: Path) -> None:
    from src.application.research.archive import archive_prune_remote

    archive_root = tmp_path / "archive"
    _write_run(archive_root, "run-1")
    _verify_remote_archive(tmp_path, archive_root)
    calls: list[list[str]] = []
    preview = {
        "schema_version": "1.0",
        "tool_name": "service.cleanup",
        "ok": True,
        "data": {
            "output_runs_cleanup": {
                "delete_runs": [
                    {"path": "/var/lib/options-monitor/output_runs/run-1"},
                    {"path": "/var/lib/options-monitor/output_runs/run-2"},
                ]
            }
        },
    }

    def _run_cmd(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if "python3 -c" in command[-1]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(_remote_inventory_payload(tmp_path, archive_root)),
                stderr="",
            )
        if "--confirm" in command[-1]:
            from src.application.research.archive import _validate_cleanup_preview

            digest = _validate_cleanup_preview(
                preview,
                remote_runtime_root="/var/lib/options-monitor",
            )["plan_sha256"]
            confirmed = {
                "schema_version": "1.0",
                "tool_name": "service.cleanup",
                "ok": True,
                "data": {
                    "status": "cleaned",
                    "expected_output_runs_plan_sha256": digest,
                },
            }
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps(confirmed), stderr=""
            )
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(preview), stderr="")

    data = archive_prune_remote(
        repo_root=tmp_path,
        archive_root=archive_root,
        ssh_target="deploy@example",
        confirm=True,
        run_cmd=_run_cmd,
    )

    assert data["ok"] is False
    assert data["status"] == "remote_prune_guard_failed"
    assert data["deletion_guard"]["unverified_delete_run_ids"] == ["run-2"]
    assert len(calls) == 2
    assert all("--confirm" not in " ".join(call) for call in calls)


def test_archive_prune_remote_runs_confirm_after_guard_passes(tmp_path: Path) -> None:
    from src.application.research.archive import archive_prune_remote

    archive_root = tmp_path / "archive"
    _write_run(archive_root, "run-1")
    _verify_remote_archive(tmp_path, archive_root)
    calls: list[list[str]] = []
    preview = {
        "schema_version": "1.0",
        "tool_name": "service.cleanup",
        "ok": True,
        "data": {
            "output_runs_cleanup": {
                "delete_runs": [{"path": "/var/lib/options-monitor/output_runs/run-1"}]
            }
        },
    }

    def _run_cmd(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if "python3 -c" in command[-1]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(_remote_inventory_payload(tmp_path, archive_root)),
                stderr="",
            )
        if "--confirm" in command[-1]:
            from src.application.research.archive import _validate_cleanup_preview

            digest = _validate_cleanup_preview(
                preview,
                remote_runtime_root="/var/lib/options-monitor",
            )["plan_sha256"]
            confirmed = {
                "schema_version": "1.0",
                "tool_name": "service.cleanup",
                "ok": True,
                "data": {
                    "status": "cleaned",
                    "expected_output_runs_plan_sha256": digest,
                },
            }
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps(confirmed), stderr=""
            )
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(preview), stderr="")

    data = archive_prune_remote(
        repo_root=tmp_path,
        archive_root=archive_root,
        ssh_target="deploy@example",
        confirm=True,
        run_cmd=_run_cmd,
    )

    assert data["ok"] is True
    assert data["changed"] is True
    assert data["deletion_guard"]["confirmable"] is True
    assert data["include_logs"] is False
    assert data["include_logs_requested"] is True
    assert "runtime_log_pruning_disabled" in data["limitations"][0]
    assert len(calls) == 3
    assert "--confirm" in calls[2][-1]
    assert all("--cleanup-runtime-logs" not in call[-1] for call in calls)


def test_archive_prune_remote_rejects_malformed_cleanup_preview(tmp_path: Path) -> None:
    from src.application.research.archive import archive_prune_remote

    archive_root = tmp_path / "archive"
    _write_run(archive_root, "run-1")
    _verify_remote_archive(tmp_path, archive_root)
    calls: list[list[str]] = []

    def _run_cmd(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if "python3 -c" in command[-1]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(_remote_inventory_payload(tmp_path, archive_root)),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout="not-json", stderr="")

    data = archive_prune_remote(
        repo_root=tmp_path,
        archive_root=archive_root,
        ssh_target="deploy@example",
        confirm=True,
        run_cmd=_run_cmd,
    )

    assert data["ok"] is False
    assert data["status"] == "remote_prune_guard_failed"
    assert data["deletion_guard"]["confirmable"] is False
    assert "schema_version_mismatch" in data["deletion_guard"]["preview_validation"]["errors"]
    assert len(calls) == 2
    assert all("--confirm" not in call[-1] for call in calls)


def test_archive_prune_remote_rechecks_current_remote_content(tmp_path: Path) -> None:
    from src.application.research.archive import archive_prune_remote

    archive_root = tmp_path / "archive"
    _write_run(archive_root, "run-1")
    _verify_remote_archive(tmp_path, archive_root)
    remote_inventory = _remote_inventory_payload(tmp_path, archive_root)
    remote_inventory["runs"][0]["content_digest"] = "changed"
    preview = {
        "schema_version": "1.0",
        "tool_name": "service.cleanup",
        "ok": True,
        "data": {
            "output_runs_cleanup": {
                "delete_runs": [
                    {"path": "/var/lib/options-monitor/output_runs/run-1"}
                ]
            }
        },
    }
    calls: list[list[str]] = []

    def _run_cmd(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        payload = remote_inventory if "python3 -c" in command[-1] else preview
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(payload),
            stderr="",
        )

    data = archive_prune_remote(
        repo_root=tmp_path,
        archive_root=archive_root,
        ssh_target="deploy@example",
        confirm=True,
        run_cmd=_run_cmd,
    )

    assert data["ok"] is False
    assert data["deletion_guard"]["confirmable"] is False
    assert data["deletion_guard"]["changed_or_missing_remote_run_ids"] == ["run-1"]
    assert data["deletion_guard"]["unverified_delete_run_ids"] == ["run-1"]
    assert len(calls) == 2


def test_archive_prune_remote_rechecks_current_local_copy(tmp_path: Path) -> None:
    from src.application.research.archive import archive_prune_remote

    archive_root = tmp_path / "archive"
    _write_run(archive_root, "run-1")
    _verify_remote_archive(tmp_path, archive_root)
    (archive_root / "output_runs" / "run-1" / "accounts" / "lx" / "sell_put_candidates.csv").write_text(
        "changed-after-verify\n",
        encoding="utf-8",
    )
    preview = {
        "schema_version": "1.0",
        "tool_name": "service.cleanup",
        "ok": True,
        "data": {
            "output_runs_cleanup": {
                "delete_runs": [
                    {"path": "/var/lib/options-monitor/output_runs/run-1"}
                ]
            }
        },
    }
    calls: list[list[str]] = []

    def _run_cmd(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        payload = (
            _remote_inventory_payload(tmp_path, archive_root)
            if "python3 -c" in command[-1]
            else preview
        )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(payload),
            stderr="",
        )

    data = archive_prune_remote(
        repo_root=tmp_path,
        archive_root=archive_root,
        ssh_target="deploy@example",
        confirm=True,
        run_cmd=_run_cmd,
    )

    assert data["ok"] is False
    assert data["deletion_guard"]["confirmable"] is False
    assert data["deletion_guard"]["mutated_or_missing_archive_run_ids"] == ["run-1"]
    assert data["deletion_guard"]["unverified_delete_run_ids"] == ["run-1"]
    assert len(calls) == 2
