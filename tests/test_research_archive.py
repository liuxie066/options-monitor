from __future__ import annotations

import json
import shlex
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tests.candidate_evidence_helpers import seal_opening_candidate_fixture


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
    seal_opening_candidate_fixture(
        root,
        run_id=run_id,
        accepted_rows=[
            {
                "symbol": "NVDA",
                "account": "lx",
                "option_type": "put",
                "contract_symbol": "NVDA260619P00100000",
                "expiration": "2026-06-19",
                "dte": 30,
                "delta": -0.2,
                "strike": 100,
                "spot": 120,
                "annualized_net_return_on_cash_basis": 0.12,
                "spread_ratio": 0.10,
                "open_interest": 500,
                "volume": 20,
            }
        ],
        rejected_rows=[
            {
                "symbol": "AMD",
                "account": "lx",
                "option_type": "put",
                "contract_symbol": "AMD260619P00080000",
                "expiration": "2026-06-19",
                "strike": 80,
                "spot": 95,
                "rule": "risk_spread",
                "spread_ratio": 0.45,
            }
        ],
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
    assert data["runs"][0]["run_id"] == "run-1"
    assert data["runs"][0]["verified"] is True
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


def test_archive_pull_syncs_only_selected_run_blob_refs(tmp_path: Path) -> None:
    from src.application.required_data_blobs import publish_required_data_scan_blob
    from src.application.research.archive import archive_pull

    source = tmp_path / "source"
    run_dir = _write_run(source, "run-1")
    provider = {
        "symbol": "NVDA",
        "rows": [
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-08-21",
                "contract_symbol": "NVDA260821P00100000",
                "strike": 100,
                "multiplier": 100,
            }
        ],
    }
    raw_bytes = (
        json.dumps(provider, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    columns = [
        "symbol",
        "option_type",
        "expiration",
        "contract_symbol",
        "strike",
        "multiplier",
    ]
    csv_bytes = (
        ",".join(columns)
        + "\nNVDA,put,2026-08-21,NVDA260821P00100000,100,100\n"
    ).encode("utf-8")
    ref = publish_required_data_scan_blob(
        runtime_root=source,
        symbol="NVDA",
        market="US",
        raw_json_bytes=raw_bytes,
        required_data_csv_bytes=csv_bytes,
        columns=columns,
    )
    (run_dir / "state" / "required_data_snapshot_manifest.json").write_text(
        json.dumps({"symbols": {"NVDA": {"scan_blob_ref": ref}}}),
        encoding="utf-8",
    )
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

    blob_calls = [
        command for command in calls if ref["blob_relpath"] in command[-2]
    ]
    assert data["scan_blob_refs"] == [ref]
    assert len(blob_calls) == 1
    assert blob_calls[0][-2].endswith(ref["blob_relpath"])
    assert not any(
        command[-2].endswith("output_shared/blobs/") for command in calls
    )


def test_archive_rejects_symlinked_required_data_manifest(tmp_path: Path) -> None:
    from src.application.research.archive import _run_scan_blob_refs

    run_dir = _write_run(tmp_path, "run-1")
    outside = tmp_path / "outside.json"
    outside.write_text("{}\n", encoding="utf-8")
    manifest = run_dir / "state" / "required_data_snapshot_manifest.json"
    manifest.symlink_to(outside)

    refs, status, error = _run_scan_blob_refs(run_dir)

    assert refs == []
    assert status == "invalid"
    assert error == "required-data snapshot manifest is unsafe"


def test_archive_deduplicates_same_blob_with_runtime_local_publish_times(
    tmp_path: Path,
) -> None:
    from src.application.required_data_blobs import publish_required_data_scan_blob
    from src.application.research.archive import _selected_scan_blob_refs

    provider = {
        "symbol": "NVDA",
        "rows": [
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-08-21",
                "contract_symbol": "NVDA260821P00100000",
                "strike": 100,
                "multiplier": 100,
            }
        ],
    }
    columns = [
        "symbol",
        "option_type",
        "expiration",
        "contract_symbol",
        "strike",
        "multiplier",
    ]
    ref = publish_required_data_scan_blob(
        runtime_root=tmp_path,
        symbol="NVDA",
        market="US",
        raw_json_bytes=(json.dumps(provider, indent=2) + "\n").encode(),
        required_data_csv_bytes=(
            ",".join(columns)
            + "\nNVDA,put,2026-08-21,NVDA260821P00100000,100,100\n"
        ).encode(),
        columns=columns,
    )
    newer_at = datetime.fromisoformat(ref["published_at_utc"].replace("Z", "+00:00")) + timedelta(seconds=1)
    newer = {**ref, "published_at_utc": newer_at.isoformat().replace("+00:00", "Z")}

    selected = _selected_scan_blob_refs(
        source={"kind": "ssh"},
        source_run_inventory=[
            {"scan_blob_reference_status": "ready", "scan_blob_refs": [ref]},
            {"scan_blob_reference_status": "ready", "scan_blob_refs": [newer]},
        ],
    )

    assert selected == [newer]


def test_remote_inventory_script_applies_run_filter(tmp_path: Path) -> None:
    from src.application.research.archive import REMOTE_INVENTORY_SCRIPT

    _write_run(tmp_path, "run-selected")
    _write_run(tmp_path, "run-ignored")

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            REMOTE_INVENTORY_SCRIPT,
            str(tmp_path),
            "",
            json.dumps(["run-selected"]),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(proc.stdout)
    assert [item["run_id"] for item in payload["runs"]] == ["run-selected"]


def test_archive_pull_filters_and_batches_explicit_remote_run_inventory(tmp_path: Path) -> None:
    from src.application.research.archive import archive_pull

    run_ids = [f"run-{index}" for index in range(11)]
    inventory_batches: list[list[str]] = []

    def _run_cmd(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[0] != "ssh":
            return subprocess.CompletedProcess(command, 0, stdout="dry\n", stderr="")
        selected = json.loads(shlex.split(command[-1])[-1])
        inventory_batches.append(selected)
        inventory = {
            "runtime_root": "/var/lib/options-monitor",
            "runs_root": "/var/lib/options-monitor/output_runs",
            "source_host": "prod.example",
            "runs": [{"run_id": run_id, "mtime": 1} for run_id in selected],
        }
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(inventory), stderr="")

    data = archive_pull(
        repo_root=tmp_path,
        archive_root=tmp_path / "archive",
        ssh_target="deploy@example",
        run_ids=[*run_ids, run_ids[0]],
        write=False,
        run_cmd=_run_cmd,
    )

    assert data["ok"] is True
    assert data["selected_run_ids"] == run_ids
    assert inventory_batches == [run_ids[:10], run_ids[10:]]
    assert data["operations"][0]["batch_count"] == 2


def test_archive_pull_rejects_malformed_remote_inventory(tmp_path: Path) -> None:
    from src.application.research.archive import archive_pull

    def _run_cmd(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        stdout = "not-json" if command[0] == "ssh" else "dry\n"
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    data = archive_pull(
        repo_root=tmp_path,
        archive_root=tmp_path / "archive",
        ssh_target="deploy@example",
        run_ids=["run-1"],
        write=False,
        run_cmd=_run_cmd,
    )

    assert data["ok"] is False
    assert data["selected_run_ids"] == []


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
    kept_run = _write_run(archive_root, "run-kept")
    _verify_remote_archive(tmp_path, archive_root)
    (kept_run / "state" / "last_run.json").write_text("changed", encoding="utf-8")
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
    assert data["deletion_guard"]["mutated_or_missing_archive_run_ids"] == []
    assert data["deletion_guard"]["changed_or_missing_remote_run_ids"] == []
    assert data["include_logs"] is False
    assert data["include_logs_requested"] is True
    assert "runtime_log_pruning_disabled" in data["limitations"][0]
    assert len(calls) == 3
    assert "python3 -c" not in calls[0][-1]
    assert json.loads(shlex.split(calls[1][-1])[-1]) == ["run-1"]
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


def test_critical_files_include_bidirectional_wheel_artifacts(tmp_path: Path) -> None:
    from src.application.research.archive import _critical_files

    run_dir = tmp_path / "run-wheel"
    account_dir = run_dir / "accounts" / "lx"
    state_dir = account_dir / "state"
    state_dir.mkdir(parents=True)
    for path in (
        state_dir / "candidate_snapshot_manifest.v3.json",
        state_dir / "wheel_candidate_snapshot.v2.json",
        account_dir / "strategy_scan_status_index.v4.json",
        account_dir / "nvda_wheel_put_scan_status.v2.json",
    ):
        path.write_text("{}\n", encoding="utf-8")

    critical = _critical_files(run_dir)

    assert critical["candidate_manifest_files"] == [
        "accounts/lx/state/candidate_snapshot_manifest.v3.json"
    ]
    assert critical["candidate_snapshot_files"] == [
        "accounts/lx/state/wheel_candidate_snapshot.v2.json"
    ]
    assert critical["candidate_status_files"] == [
        "accounts/lx/nvda_wheel_put_scan_status.v2.json",
        "accounts/lx/strategy_scan_status_index.v4.json",
    ]
