from pathlib import Path
import json
import os
import time
import sys
import pytest

from src.application.research.archive_bounded import bounded_pull, restore_batch
from src.application.research.archive_retirement import retire_batch
from src.application.research.archive_stream import TransferError
from tests.test_archive_bounded import limits_file


def ready(tmp_path, canonical=False):
    source = tmp_path / "source"
    for name in ["old-a", "old-b", *[f"new-{i}" for i in range(200)]]:
        d = source / "output_runs" / name / "state"
        d.mkdir(parents=True)
        (d / "last_run.json").write_text('{"ran_scan":false}')
    if canonical:
        from tests.candidate_evidence_helpers import seal_opening_candidate_fixture
        from tests.test_research_archive import _nvda_scan_provider, _nvda_scan_columns
        from src.application.required_data_blobs import publish_required_data_scan_blob

        seal_opening_candidate_fixture(source, run_id="old-a")
        ref = publish_required_data_scan_blob(
            runtime_root=source,
            symbol="NVDA",
            market="US",
            raw_json_bytes=(json.dumps(_nvda_scan_provider(), ensure_ascii=False, indent=2) + "\n").encode(),
            required_data_csv_bytes=(
                ",".join(_nvda_scan_columns()) + "\nNVDA,put,2026-08-21,NVDA260821P00100000,100,100\n"
            ).encode(),
            columns=_nvda_scan_columns(),
        )
        (source / "output_runs/old-a/state/required_data_snapshot_manifest.json").write_text(
            json.dumps({"symbols": {"NVDA": {"scan_blob_ref": ref}}})
        )
    for name in ["old-a", "old-b"]:
        os.utime(source / "output_runs" / name, (time.time() - 40 * 86400,) * 2)
    limit = limits_file(tmp_path)
    if canonical:
        d = json.loads(limit.read_text())
        d.update(max_file_bytes=1000000, max_bytes=10000000, evidence_bytes=1000000)
        limit.write_text(json.dumps(d))
    archive = tmp_path / "archive"
    restored = tmp_path / "restore"
    result = bounded_pull(
        repo_root=Path.cwd(),
        archive_root=archive,
        source_root=source,
        run_ids=["old-a"],
        include_logs=False,
        include_fixed_shared=False,
        write=True,
        limits_file=limit,
    )
    assert result["ok"], result
    result = restore_batch(archive_root=archive, target=restored, limits_file=limit)
    assert result["ok"], result
    return dict(
        archive_root=archive,
        restore_root=restored,
        source_root=source,
        limits_file=limit,
        reference_check_sudo=sys.platform.startswith("linux"),
    )


def test_real_archive_restore_preview_and_exact_retirement(tmp_path):
    args = ready(tmp_path)
    preview = retire_batch(**args)
    assert preview["ok"] and not preview["source_deleted"], preview
    assert (args["source_root"] / "output_runs/old-a").exists()
    result = retire_batch(**args, confirm=True, expected_plan_sha256=preview["plan_sha256"])
    assert result["ok"] and result["source_deleted"]
    assert not (args["source_root"] / "output_runs/old-a").exists()
    assert (args["source_root"] / "output_runs/old-b").exists()
    assert (args["restore_root"] / "output_runs/old-a/state/last_run.json").exists()
    result = retire_batch(**args, confirm=True, expected_plan_sha256=preview["plan_sha256"])
    assert not result["ok"] and result["source_deleted"] is None


@pytest.mark.parametrize("where", ["source_root", "archive_root", "restore_root"])
def test_changed_copy_or_source_never_deletes(tmp_path, where):
    args = ready(tmp_path)
    preview = retire_batch(**args)
    (args[where] / "output_runs/old-a/state/last_run.json").write_text('{"ran_scan":true}')
    result = retire_batch(**args, confirm=True, expected_plan_sha256=preview["plan_sha256"])
    assert not result["ok"] and result["source_deleted"] is None
    assert (args["source_root"] / "output_runs/old-a").exists()


def test_restore_receipt_binding_required(tmp_path):
    args = ready(tmp_path)
    p = args["restore_root"] / "restore-receipt.json"
    d = json.loads(p.read_text())
    d["archive_receipt_sha256"] = "0" * 64
    p.write_text(json.dumps(d))
    result = retire_batch(**args)
    assert not result["ok"] and "not bound" in result["reason"]
    assert (args["source_root"] / "output_runs/old-a").exists()


def test_changed_pointer_protects_after_preview(tmp_path):
    args = ready(tmp_path)
    preview = retire_batch(**args)
    shared = args["source_root"] / "output_shared/state"
    shared.mkdir(parents=True)
    (shared / "last_run_dir.txt").write_text(str(args["source_root"] / "output_runs/old-a"))
    result = retire_batch(**args, confirm=True, expected_plan_sha256=preview["plan_sha256"])
    assert not result["ok"] and result["source_deleted"] is None
    assert (args["source_root"] / "output_runs/old-a").exists()


def test_wrong_preview_hash_has_no_effect(tmp_path):
    args = ready(tmp_path)
    result = retire_batch(**args, confirm=True, expected_plan_sha256="0" * 64)
    assert not result["ok"] and "digest changed" in result["reason"]
    assert (args["source_root"] / "output_runs/old-a").exists()


def test_canonical_dependency_survives_exact_retirement(tmp_path):
    args = ready(tmp_path, canonical=True)
    receipt = json.loads((args["archive_root"] / "bounded-receipt.json").read_text())
    blobs = [row["path"] for row in receipt["source_files"] if row["path"].startswith("output_shared/blobs/")]
    assert blobs
    preview = retire_batch(**args)
    assert preview["ok"], preview
    result = retire_batch(**args, confirm=True, expected_plan_sha256=preview["plan_sha256"])
    assert result["ok"], result
    for rel in blobs:
        assert (args["source_root"] / rel).read_bytes() == (args["restore_root"] / rel).read_bytes()
    journal = Path(result["outcome"]["receipt_path"])
    events = [json.loads(line) for line in journal.read_text().splitlines()]
    assert [e["state"] for e in events] == ["started", "deleting", "removed", "complete"]


def test_partial_first_delete_is_unknown_and_durable(tmp_path, monkeypatch):
    from src.application import service_cleanup as owner

    root = tmp_path / "source"
    for name in ["old", *[f"new-{i}" for i in range(200)]]:
        folder = root / "output_runs" / name
        folder.mkdir(parents=True)
        (folder / "data").write_bytes(b"x" * 10)
    os.utime(root / "output_runs/old", (1, 1))
    plan = owner.cleanup_output_runs_for_ids(runtime_root=root, run_ids=["old"])

    def partial(path):
        (path / "data").unlink()
        raise OSError("synthetic deletion failure after effect")

    monkeypatch.setattr(owner.shutil, "rmtree", partial)
    result = owner.cleanup_output_runs_for_ids(
        runtime_root=root,
        run_ids=["old"],
        confirm=True,
        expected_plan_sha256=plan["plan_sha256"],
        before_delete=lambda: None,
        archive_binding_sha256="a" * 64,
        evidence_bytes=50000,
    )
    assert not result["ok"] and result["source_deleted"] is None
    assert result["deleted_paths"] == []
    events = [json.loads(line) for line in Path(result["receipt_path"]).read_text().splitlines()]
    assert [e["state"] for e in events] == ["started", "deleting", "failed", "partial_or_unknown"]
    assert not events[2]["operation"]["ok"]


def test_progress_budget_refuses_before_delete(tmp_path, monkeypatch):
    from src.application import service_cleanup as owner

    root = tmp_path / "source"
    for name in ["old", *[f"new-{i}" for i in range(200)]]:
        (root / "output_runs" / name).mkdir(parents=True)
    os.utime(root / "output_runs/old", (1, 1))
    plan = owner.cleanup_output_runs_for_ids(runtime_root=root, run_ids=["old"])

    def forbidden(*a, **k):
        raise AssertionError("delete called before budget accepted")

    monkeypatch.setattr(owner, "_delete_path_result", forbidden)
    with pytest.raises(ValueError, match="progress evidence budget"):
        owner.cleanup_output_runs_for_ids(
            runtime_root=root,
            run_ids=["old"],
            confirm=True,
            expected_plan_sha256=plan["plan_sha256"],
            before_delete=lambda: None,
            archive_binding_sha256="a" * 64,
            evidence_bytes=4096,
        )


def test_rss_limit_is_enforced_before_retirement(tmp_path):
    args = ready(tmp_path)
    p = args["limits_file"]
    d = json.loads(p.read_text())
    d["max_rss_bytes"] = 1
    p.write_text(json.dumps(d))
    result = retire_batch(**args)
    assert not result["ok"] and result["source_deleted"] is None
    assert result["reason"] == "rss_limit", result
    assert (args["source_root"] / "output_runs/old-a").exists()


def test_open_run_reference_refuses_retirement(tmp_path):
    args = ready(tmp_path)
    with (args["source_root"] / "output_runs/old-a/state/last_run.json").open():
        result = retire_batch(**args)
    assert not result["ok"]
    assert (args["source_root"] / "output_runs/old-a").exists()


@pytest.mark.parametrize("sudo", [False, True])
def test_retirement_cli_uses_owner_and_confirmation(tmp_path, monkeypatch, sudo):
    import argparse
    from src.interfaces.cli.research import add_research_commands, handle_research_command
    from src.application.research import archive_retirement

    parser = argparse.ArgumentParser()
    add_research_commands(parser.add_subparsers(dest="command"))
    calls = []

    def owner(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "source_deleted": False}

    monkeypatch.setattr(archive_retirement, "retire_batch", owner)
    args = parser.parse_args(
        [
            "research",
            "archive",
            "retire-batch",
            "--archive-root",
            "A",
            "--restore-root",
            "C",
            "--limits-file",
            "L",
            "--source-root",
            "S",
            "--confirm",
            "--expected-plan-sha256",
            "a" * 64,
        ]
    )
    if sudo:
        args = parser.parse_args(
            [
                "research",
                "archive",
                "retire-batch",
                "--archive-root",
                "A",
                "--restore-root",
                "C",
                "--limits-file",
                "L",
                "--source-root",
                "S",
                "--confirm",
                "--expected-plan-sha256",
                "a" * 64,
                "--reference-check-sudo",
            ]
        )
    handle_research_command(args, repo_base_fn=lambda: tmp_path)
    assert calls[0]["confirm"] is True and calls[0]["expected_plan_sha256"] == "a" * 64
    assert calls[0]["source_root"] == "S"
    assert calls[0]["reference_check_sudo"] is sudo


def test_new_receipt_parent_fsync_failure_precedes_delete(tmp_path, monkeypatch):
    from src.application import service_cleanup as owner

    root = tmp_path / "source"
    for name in ["old", *[f"new-{i}" for i in range(200)]]:
        (root / "output_runs" / name).mkdir(parents=True)
    os.utime(root / "output_runs/old", (1, 1))
    plan = owner.cleanup_output_runs_for_ids(runtime_root=root, run_ids=["old"])
    real_fsync = owner.os.fsync
    root_inode = root.stat().st_ino

    def fsync(fd):
        if os.fstat(fd).st_ino == root_inode:
            raise OSError("synthetic parent directory sync failure")
        return real_fsync(fd)

    monkeypatch.setattr(owner.os, "fsync", fsync)

    def forbidden(*args, **kwargs):
        raise AssertionError("delete before receipt directories durable")

    monkeypatch.setattr(owner, "_delete_path_result", forbidden)
    for _ in range(2):  # The second call sees the directory left by failed fsync.
        with pytest.raises(OSError, match="parent directory sync failure"):
            owner.cleanup_output_runs_for_ids(
                runtime_root=root,
                run_ids=["old"],
                confirm=True,
                expected_plan_sha256=plan["plan_sha256"],
                before_delete=lambda: None,
                archive_binding_sha256="a" * 64,
                evidence_bytes=50000,
            )
    assert (root / "output_runs/old").exists()


@pytest.mark.parametrize(
    "error,expected",
    [
        (TransferError("source reference unknown"), "source reference unknown"),
        (OSError("private-path-or-message"), "source operation failed"),
    ],
)
def test_source_worker_reports_safe_failure_reason(monkeypatch, capsys, error, expected):
    import io
    from src.application.research import archive_retirement as owner

    class Input:
        buffer = io.BytesIO(json.dumps({"limits": {"evidence_bytes": 4096}}).encode())

    monkeypatch.setattr(owner.sys, "stdin", Input())

    def fail(request):
        raise error

    monkeypatch.setattr(owner, "_source_retire", fail)
    with pytest.raises(SystemExit):
        owner._worker()
    result = json.loads(capsys.readouterr().out)
    assert result["reason"] == expected


def test_sudo_reference_option_is_explicit_and_observer_only(tmp_path, monkeypatch):
    from src.application.research import archive_retirement as owner
    import subprocess

    class Proc:
        def is_dir(self):
            return True

    real_path = owner.Path
    monkeypatch.setattr(owner, "Path", lambda p: Proc() if p == "/proc" else real_path(p))
    calls = []

    def run(command, **kwargs):
        calls.append((command, json.loads(kwargs["input"])))
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps({"hit": False, "unknown": False, "stop_reason": None, "processes": 1, "links": 2}).encode(),
            b"",
        )

    monkeypatch.setattr(owner.subprocess, "run", run)
    owner._active_references(tmp_path, ["run-a"], time.monotonic() + 5)
    assert calls[-1][0][0] == sys.executable
    owner._active_references(tmp_path, ["run-a"], time.monotonic() + 5, reference_check_sudo=True)
    command, request = calls[-1]
    assert command[:3] == ["sudo", "-n", "/usr/bin/python3"]
    assert command[3:6] == ["-I", "-S", "-B"]
    assert request["uid"] == os.getuid()
    assert request["targets"] == [str(tmp_path / "output_runs/run-a")]
    assert command[-1] == owner.LINUX_REFERENCE_PROBE


@pytest.mark.parametrize("mode", ["permission", "hit", "unavailable"])
def test_sudo_observer_still_refuses_unknown_hits_and_failure(tmp_path, monkeypatch, mode):
    from src.application.research import archive_retirement as owner
    import subprocess

    class Proc:
        def is_dir(self):
            return True

    real_path = owner.Path
    monkeypatch.setattr(owner, "Path", lambda p: Proc() if p == "/proc" else real_path(p))

    def run(command, **kwargs):
        out = {
            "hit": mode == "hit",
            "unknown": mode == "permission",
            "stop_reason": "permission" if mode == "permission" else None,
            "processes": 1,
            "links": 0,
        }
        return subprocess.CompletedProcess(command, 1 if mode == "unavailable" else 0, json.dumps(out).encode(), b"")

    monkeypatch.setattr(owner.subprocess, "run", run)
    with pytest.raises(TransferError):
        owner._active_references(tmp_path, ["a"], time.monotonic() + 5, reference_check_sudo=True)


def test_linux_observer_probe_real_local_closed_and_open_file(tmp_path):
    if not sys.platform.startswith("linux"):
        pytest.skip("real /proc observer is Linux-only; CI executes this, no macOS substitute")
    from src.application.research.archive_retirement import _active_references

    source = tmp_path / "source"
    run = source / "output_runs/old"
    run.mkdir(parents=True)
    file = run / "x"
    file.write_text("synthetic")
    _active_references(source, ["old"], time.monotonic() + 5, reference_check_sudo=True)
    with file.open():
        with pytest.raises(TransferError, match="actively referenced"):
            _active_references(source, ["old"], time.monotonic() + 5, reference_check_sudo=True)


def test_reference_privilege_rejects_truthy_non_boolean(tmp_path):
    from src.application.research.archive_retirement import _active_references

    with pytest.raises(TransferError, match="explicit boolean"):
        _active_references(tmp_path, ["a"], time.monotonic() + 5, reference_check_sudo="false")
