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


def _fixed_now() -> datetime:
    return datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)


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


def test_archive_build_datasets_uses_verified_archive_runs(tmp_path: Path) -> None:
    from src.application.research.archive import archive_build_datasets, archive_verify

    archive_root = tmp_path / "archive"
    _write_run(archive_root, "run-1")
    archive_verify(repo_root=tmp_path, archive_root=archive_root, now_fn=_fixed_now)

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


def test_archive_prune_remote_requires_verified_delete_runs(tmp_path: Path) -> None:
    from src.application.research.archive import archive_prune_remote, archive_verify

    archive_root = tmp_path / "archive"
    _write_run(archive_root, "run-1")
    archive_verify(repo_root=tmp_path, archive_root=archive_root, now_fn=_fixed_now)
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
    assert len(calls) == 1
    assert "--confirm" not in " ".join(calls[0])


def test_archive_prune_remote_runs_confirm_after_guard_passes(tmp_path: Path) -> None:
    from src.application.research.archive import archive_prune_remote, archive_verify

    archive_root = tmp_path / "archive"
    _write_run(archive_root, "run-1")
    archive_verify(repo_root=tmp_path, archive_root=archive_root, now_fn=_fixed_now)
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
    assert len(calls) == 2
    assert "--confirm" in calls[1][-1]
