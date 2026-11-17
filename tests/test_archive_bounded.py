from __future__ import annotations
import json
from pathlib import Path

from src.application.research.archive_bounded import bounded_pull


def limits_file(tmp_path):
    p = tmp_path / "limits.json"
    p.write_text(
        json.dumps(
            dict(
                seconds=15,
                grace_seconds=0.1,
                max_bytes=1000000,
                max_entries=1000,
                max_file_bytes=100000,
                max_rss_bytes=1024**3,
                evidence_bytes=100000,
                reserve_bytes=1,
                stderr_bytes=1000,
            )
        )
    )
    return p


def test_local_batch_real_worker_isolated_and_no_delete(tmp_path):
    source = tmp_path / "source"
    run = source / "output_runs" / "run-1"
    (run / "state").mkdir(parents=True)
    import os, time

    for index in range(200):
        (source / "output_runs" / f"new-{index}").mkdir()
    os.utime(run, (time.time() - 40 * 86400, time.time() - 40 * 86400))
    (run / "state" / "last_run.json").write_text('{"ran_scan":false}')
    (source / "output_shared" / "research").mkdir(parents=True)
    (source / "output_shared" / "research" / "shared.json").write_text('{"synthetic":true}')
    before = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    target = tmp_path / "batch"
    result = bounded_pull(
        repo_root=Path.cwd(),
        archive_root=target,
        source_root=source,
        run_ids=["run-1"],
        include_logs=False,
        write=True,
        limits_file=limits_file(tmp_path),
    )
    assert result["ok"], result
    assert result["source_deleted"] is False
    assert (target / "bounded-receipt.json").is_file()
    assert (target / "output_runs" / "run-1" / "state" / "last_run.json").read_bytes() == before[
        "output_runs/run-1/state/last_run.json"
    ]
    assert {str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()} == before


def test_limits_symlink_rejected_before_worker(tmp_path):
    from src.application.research.archive_bounded import read_limits
    from src.application.research.archive_stream import TransferError
    import pytest

    original = limits_file(tmp_path)
    link = tmp_path / "alias"
    link.symlink_to(original)
    with pytest.raises(TransferError):
        read_limits(link)


def test_missing_canonical_blob_rejected_in_snapshot(tmp_path):
    from src.application.research.archive_bounded import SNAPSHOT_SCRIPT
    import subprocess, sys

    source = tmp_path / "source"
    state = source / "output_runs/r/state"
    state.mkdir(parents=True)
    digest = "a" * 64
    (state / "required_data_snapshot_manifest.json").write_text(
        json.dumps(
            {
                "symbols": {
                    "S": {
                        "scan_blob_ref": {
                            "blob_sha256": digest,
                            "blob_relpath": f"output_shared/blobs/sha256/aa/{digest}.json.gz",
                            "uncompressed_size_bytes": 10,
                        }
                    }
                }
            }
        )
    )
    request = dict(roots=["output_runs/r"], seconds=2, max_entries=100, max_bytes=100000, max_file_bytes=10000)
    result = subprocess.run(
        [sys.executable, "-c", SNAPSHOT_SCRIPT, str(source), json.dumps(request)], capture_output=True, timeout=3
    )
    assert result.returncode != 0
    assert json.loads(result.stdout) == {"ok": False, "error_stage": "selected_tree", "error_code": "selected_root_missing"}
    assert result.stderr == b""


def test_snapshot_symlink_never_reads_target(tmp_path):
    from src.application.research.archive_bounded import SNAPSHOT_SCRIPT
    import subprocess, sys

    source = tmp_path / "source"
    run = source / "output_runs/r"
    run.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_bytes(b"private")
    (run / "link").symlink_to(outside)
    request = dict(roots=["output_runs/r"], seconds=2, max_entries=100, max_bytes=100000, max_file_bytes=10000)
    result = subprocess.run(
        [sys.executable, "-c", SNAPSHOT_SCRIPT, str(source), json.dumps(request)], capture_output=True, timeout=3
    )
    assert result.returncode != 0
    assert b"private" not in result.stdout + result.stderr


def test_retention_owner_union_and_pointer():
    from src.application.service_cleanup import retained_output_runs
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    rows = [("old", now - timedelta(days=40)), ("count", now - timedelta(days=35)), ("recent", now - timedelta(days=1))]
    result = retained_output_runs(rows, keep_days=30, keep_count=1, now=now, pointer="old")
    assert result == {"recent": "latest_keep_count", "old": "last_run_dir_pointer"}


def test_cli_bounded_parameters_parse_without_running():
    import argparse
    from src.interfaces.cli.research import add_research_commands

    parser = argparse.ArgumentParser()
    add_research_commands(parser.add_subparsers())
    args = parser.parse_args(
        [
            "research",
            "archive",
            "pull",
            "--limits-file",
            "budget.json",
            "--existing-archive-root",
            "old",
            "--expected-source-host",
            "fixture",
            "--no-logs",
            "--run-id",
            "r",
            "--write",
        ]
    )
    assert args.limits_file == "budget.json"
    assert args.existing_archive_root == "old"
    assert args.expected_source_host == "fixture"
    assert args.no_logs and args.write


def test_cumulative_evidence_failure_never_publishes(tmp_path):
    source = tmp_path / "source"
    (source / "output_runs/r/state").mkdir(parents=True)
    (source / "output_runs/r/state/last_run.json").write_text("{}")
    target = tmp_path / "batch"
    budget = limits_file(tmp_path)
    value = json.loads(budget.read_text())
    value["evidence_bytes"] = 4096
    budget.write_text(json.dumps(value))
    result = bounded_pull(
        repo_root=Path.cwd(),
        archive_root=target,
        source_root=source,
        run_ids=["r"],
        include_logs=False,
        write=True,
        limits_file=budget,
    )
    assert not result["ok"]
    assert not target.exists()


def test_old_archive_reverification_is_read_only(tmp_path):
    import os, shutil, time

    source = tmp_path / "source"
    for name in ("new-batch", "prior-batch"):
        state = source / "output_runs" / name / "state"
        state.mkdir(parents=True)
        (state / "last_run.json").write_text('{"ran_scan":false}')
        os.utime(state.parent, (time.time() - 40 * 86400, time.time() - 40 * 86400))
    for index in range(200):
        (source / "output_runs" / f"current-{index}").mkdir()
    old = tmp_path / "old"
    (old / "output_runs").mkdir(parents=True)
    shutil.copytree(source / "output_runs/prior-batch", old / "output_runs/prior-batch")
    (old / "manifests").mkdir()
    (old / "manifests/inventory.latest.json").write_text(json.dumps({"runs": [{"run_id": "prior-batch"}]}))
    before = {str(p.relative_to(old)): p.read_bytes() for p in old.rglob("*") if p.is_file()}
    result = bounded_pull(
        repo_root=Path.cwd(),
        archive_root=tmp_path / "batch",
        source_root=source,
        run_ids=["new-batch"],
        include_logs=False,
        write=True,
        limits_file=limits_file(tmp_path),
        existing_archive_root=old,
    )
    assert result["ok"], result
    assert {str(p.relative_to(old)): p.read_bytes() for p in old.rglob("*") if p.is_file()} == before
    receipt = json.loads((tmp_path / "batch/bounded-receipt.json").read_bytes())
    assert receipt["old_archive_review"]["status"] == "content_verified_read_only"


def test_738_batch_and_independent_restore_real_workers(tmp_path, monkeypatch):
    import os
    import time
    from src.application.research.archive_bounded import restore_batch

    source = tmp_path / "source"
    ids = [f"old-{i:04d}" for i in range(738)]
    for name in ids:
        run = source / "output_runs" / name
        (run / "state").mkdir(parents=True)
        (run / "state/last_run.json").write_text('{"ran_scan":false}')
        os.utime(run, (1, 1))
    for i in range(200):
        (source / "output_runs" / f"new-{i}").mkdir()
    budget = limits_file(tmp_path)
    value = json.loads(budget.read_text())
    value.update(seconds=180, max_entries=10000, evidence_bytes=8 * 1024**2)
    budget.write_text(json.dumps(value))
    batch = tmp_path / "batch"
    result = bounded_pull(
        repo_root=Path.cwd(),
        archive_root=batch,
        source_root=source,
        run_ids=ids,
        include_logs=False,
        write=True,
        limits_file=budget,
    )
    assert result["ok"], result
    restored = tmp_path / "restored"
    monkeypatch.chdir(tmp_path)  # Relative API paths bind to the caller, not worker cwd.
    result = restore_batch(archive_root=Path("batch"), target=Path("restored"), limits_file=budget)
    assert result["complete"] and result["run_count"] == 738
    for name in ids:
        rel = Path("output_runs") / name / "state/last_run.json"
        assert (restored / rel).read_bytes() == (source / rel).read_bytes()
    assert not result["source_deleted"]


def test_canonical_and_candidate_owner_restore(tmp_path):
    import os
    from src.application.required_data_blobs import publish_required_data_scan_blob
    from src.application.research.archive_bounded import restore_batch
    from tests.test_research_archive import _nvda_scan_provider, _nvda_scan_columns
    from tests.candidate_evidence_helpers import seal_opening_candidate_fixture

    source = tmp_path / "source"
    seal_opening_candidate_fixture(source, run_id="old")
    run = source / "output_runs/old"
    raw = (json.dumps(_nvda_scan_provider(), ensure_ascii=False, indent=2) + "\n").encode()
    ref = publish_required_data_scan_blob(
        runtime_root=source,
        symbol="NVDA",
        market="US",
        raw_json_bytes=raw,
        required_data_csv_bytes=(
            ",".join(_nvda_scan_columns()) + "\nNVDA,put,2026-08-21,NVDA260821P00100000,100,100\n"
        ).encode(),
        columns=_nvda_scan_columns(),
    )
    (run / "state").mkdir(exist_ok=True)
    (run / "state/required_data_snapshot_manifest.json").write_text(
        json.dumps({"symbols": {"NVDA": {"scan_blob_ref": ref}}})
    )
    os.utime(run, (1, 1))
    for i in range(200):
        (source / "output_runs" / f"new-{i}").mkdir()
    budget = limits_file(tmp_path)
    value = json.loads(budget.read_text())
    value.update(max_file_bytes=1000000, max_bytes=10000000, evidence_bytes=1000000)
    budget.write_text(json.dumps(value))
    for rel in ("research", "required_data"):
        folder = source / "output_shared" / rel
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "unrelated.json").write_text("unrelated")
    batch = tmp_path / "batch"
    result = bounded_pull(
        repo_root=Path.cwd(),
        archive_root=batch,
        source_root=source,
        run_ids=["old"],
        include_logs=False,
        write=True,
        limits_file=budget,
        include_fixed_shared=False,
    )
    assert result["ok"], result
    assert not (batch / "output_shared/research").exists()
    assert not (batch / "output_shared/required_data").exists()
    restored = tmp_path / "restored"
    result = restore_batch(archive_root=batch, target=restored, limits_file=budget)
    assert result["ok"], result
    assert (restored / ref["blob_relpath"]).read_bytes() == (source / ref["blob_relpath"]).read_bytes()
    # Corrupt the local archive only: restoration must stop and never publish.
    (batch / ref["blob_relpath"]).write_bytes(b"corrupt")
    result = restore_batch(archive_root=batch, target=tmp_path / "corrupt-restore", limits_file=budget)
    assert not result["ok"]
    assert not (tmp_path / "corrupt-restore").exists()


def test_snapshot_comparison_allows_only_unrelated_churn():
    import copy
    import pytest
    from src.application.research.archive_bounded import _compare_selected_snapshots, TransferError

    now = 2000000000
    before = dict(
        host="fixture",
        runtime_root="/fixture",
        directories=["output_runs/old"],
        files=[{"path": "output_runs/old/a", "sha256": "a", "identity": [1, 2]}],
        run_metadata=[["old", now - 40 * 86400]] + [[f"new-{i}", now] for i in range(200)],
        pointer=None,
    )
    after = copy.deepcopy(before)
    after["run_metadata"].append(["unrelated", now])
    after["pointer"] = "unrelated"
    _compare_selected_snapshots(before, after, ["old"], now)
    changed = copy.deepcopy(after)
    changed["run_metadata"][0][1] -= 86400
    with pytest.raises(TransferError):
        _compare_selected_snapshots(before, changed, ["old"], now)
    for field, value in (
        ("host", "other"),
        ("runtime_root", "/other"),
        ("files", []),
        ("directories", []),
        ("pointer", "old"),
        ("run_metadata", [["old", now]]),
        ("run_metadata", [["other", now]]),
    ):
        changed = copy.deepcopy(after)
        changed[field] = value
        with pytest.raises(TransferError):
            _compare_selected_snapshots(before, changed, ["old"], now)


def test_real_batch_survives_unrelated_run_created_during_copy(tmp_path, monkeypatch):
    import os
    import time
    from src.application.research import archive_bounded as owner

    source = tmp_path / "source"
    state = source / "output_runs/old/state"
    state.mkdir(parents=True)
    (state / "last_run.json").write_text('{"ran_scan":false}')
    os.utime(state.parent, (1, 1))
    for i in range(200):
        (source / "output_runs" / f"new-{i}").mkdir()
    original = owner._snapshot
    calls = 0

    def snapshot(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            (source / "output_runs/unrelated-new").mkdir()
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, "_snapshot", snapshot)
    limits = owner.read_limits(limits_file(tmp_path))
    target = tmp_path / "batch"
    result = owner._execute(
        dict(
            source={"kind": "local", "runtime_root": str(source)},
            target=str(target),
            staging=str(tmp_path / "staging"),
            run_ids=["old"],
            include_fixed_shared=False,
            limits=limits,
            evidence_remaining=90000,
            deadline=time.monotonic() + 15,
            frozen_at=time.time(),
            expected_host=__import__("socket").getfqdn(),
            existing_archive_root=None,
        )
    )
    assert result["ok"]
    receipt = json.loads((target / "bounded-receipt.json").read_bytes())
    checks = receipt["retention_checks"]
    assert checks["before"]["metadata_sha256"] != checks["after"]["metadata_sha256"]
    assert "old" not in checks["after"]["protected"]


def test_supervisor_ps_timeout_returns_unknown_with_cleanup(monkeypatch, tmp_path):
    import subprocess
    import time
    from types import SimpleNamespace
    from src.application.research import archive_bounded, archive_stream

    calls, stopped = [], []

    def observe(timeout=1.0):
        calls.append(timeout)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired("ps", timeout)
        return {}

    monkeypatch.setattr(archive_stream, "process_table", observe)
    monkeypatch.setattr(archive_stream, "stop_group", lambda p, grace: stopped.append(p.pid))
    result = archive_bounded._supervise(
        SimpleNamespace(pid=999999),
        {"deadline": time.monotonic() + 10},
        {"max_rss_bytes": 1000000, "grace_seconds": 0.1},
        tmp_path / "batch",
        tmp_path / "partial",
        tmp_path / "result",
    )
    assert result["state"] == "unknown"
    assert result["reason"] == "process_observation_unknown"
    assert result["source_deleted"] is False
    assert stopped == [999999]
    assert len(calls) == 2


def test_supervisor_repeated_observer_timeout_preserves_unknown_and_attempts_stop(monkeypatch, tmp_path):
    import subprocess
    import time
    from types import SimpleNamespace
    from src.application.research import archive_bounded, archive_stream

    def observe(*args):
        raise subprocess.TimeoutExpired("ps", 1)

    stopped = []
    monkeypatch.setattr(archive_stream, "process_table", observe)
    monkeypatch.setattr(archive_stream, "stop_group", lambda p, grace: stopped.append(p.pid))
    result = archive_bounded._supervise(
        SimpleNamespace(pid=999999),
        {"deadline": time.monotonic() + 10},
        {"max_rss_bytes": 1000000, "grace_seconds": 0.1},
        tmp_path / "batch",
        tmp_path / "partial",
        tmp_path / "result",
    )
    assert result["state"] == "unknown"
    assert result["reason"] == "supervision_or_cleanup_unknown"
    assert stopped == [999999]


def test_ssh_compression_is_explicit_and_keeps_strict_connection_contract():
    from src.application.research.archive_bounded import _source_command

    source = {"kind": "ssh", "ssh_target": "test-alias", "runtime_root": "/synthetic"}
    plain = _source_command(source, "pass", [], 5)
    compressed = _source_command({**source, "ssh_compression": True}, "pass", [], 5)
    assert "Compression=yes" not in plain
    assert compressed == plain[:1] + ["-o", "Compression=yes"] + plain[1:]
    assert "StrictHostKeyChecking=yes" in compressed
    assert compressed[-2] == "test-alias"


def test_bounded_cli_threads_transport_and_shared_selection_without_external_calls(tmp_path, monkeypatch):
    import argparse
    from src.interfaces.cli.research import add_research_commands, handle_research_command
    from src.application.research import archive_bounded

    parser = argparse.ArgumentParser()
    add_research_commands(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(
        [
            "research",
            "archive",
            "pull",
            "--ssh-target",
            "test-alias",
            "--expected-source-host",
            "test-host",
            "--archive-root",
            str(tmp_path / "new"),
            "--run-id",
            "old-run",
            "--no-logs",
            "--write",
            "--limits-file",
            str(tmp_path / "limits"),
            "--ssh-compression",
            "--no-fixed-shared",
        ]
    )
    calls = []

    def bounded(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "source_deleted": False}

    monkeypatch.setattr(archive_bounded, "bounded_pull", bounded)
    result = handle_research_command(args, repo_base_fn=lambda: tmp_path)
    assert result["ok"]
    assert len(calls) == 1
    assert calls[0]["ssh_compression"] is True
    assert calls[0]["include_fixed_shared"] is False
    assert calls[0]["run_ids"] == ["old-run"]


def test_bounded_transport_flags_cannot_silently_apply_to_legacy(tmp_path):
    import argparse
    import pytest
    from src.application.agent_tool_contracts import AgentToolError
    from src.interfaces.cli.research import add_research_commands, handle_research_command

    parser = argparse.ArgumentParser()
    add_research_commands(parser.add_subparsers(dest="command", required=True))
    for option in ["--ssh-compression", "--no-fixed-shared"]:
        args = parser.parse_args(["research", "archive", "pull", option])
        with pytest.raises(AgentToolError, match="require --limits-file"):
            handle_research_command(args, repo_base_fn=lambda: tmp_path)
