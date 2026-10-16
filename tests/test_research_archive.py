from __future__ import annotations

import json
import shlex
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

from tests.candidate_evidence_helpers import seal_opening_candidate_fixture


def _cleanup_payload(**overrides: Any) -> dict[str, Any]:
    """Build one ``service.cleanup`` payload; call sites only ever replace ``data``."""
    base: dict[str, Any] = {
        "schema_version": "1.0",
        "tool_name": "service.cleanup",
        "ok": True,
        "data": {
            "output_runs_cleanup": {
                "delete_runs": [{"path": "/var/lib/options-monitor/output_runs/run-1"}]
            }
        },
    }
    base.update(overrides)
    return base


def _completed(command: list[str], stdout: str = "dry\n") -> subprocess.CompletedProcess[str]:
    """The successful fake runner result this module returns over and over."""
    return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")


def _nvda_scan_provider() -> dict[str, Any]:
    """The single-row NVDA required-data provider this module publishes twice."""
    return {
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


def _nvda_scan_columns() -> list[str]:
    """The column list matching :func:`_nvda_scan_provider`."""
    return ["symbol", "option_type", "expiration", "contract_symbol", "strike", "multiplier"]


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


def _write_minimal_run(root: Path, run_id: str = "run-1") -> Path:
    run = root / "output_runs" / run_id
    (run / "state").mkdir(parents=True)
    (run / "state" / "last_run.json").write_text(
        json.dumps({"run_id": run_id, "status": "ok"}), encoding="utf-8",
    )
    return run


def _synthetic_archive_transfer(
    source: Path, inventory: dict[str, Any],
) -> Callable[..., subprocess.CompletedProcess[str]]:
    import shutil

    def run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[0] == "ssh":
            assert "python3 -c" in command[-1]
            selected_raw = shlex.split(command[-1])[-1]
            selected = None if not selected_raw else json.loads(selected_raw)
            runs = [row for row in inventory["runs"] if selected is None or row["run_id"] in selected]
            return _completed(command, json.dumps({**inventory, "runs": runs}))
        assert command[0] == "rsync"
        path = Path(command[-2].split(":", 1)[1])
        assert path.is_relative_to(source)
        if path.is_dir():
            shutil.copytree(path, Path(command[-1]), dirs_exist_ok=True)
        return _completed(command)

    return run


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


def _prune_run_cmd(
    calls: list[list[str]],
    preview: dict[str, Any],
    tmp_path: Path,
    archive_root: Path,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Fake remote runner the prune-guard tests share: inventory, then cleanup."""

    def _run_cmd(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if "python3 -c" in command[-1]:
            return _completed(command, json.dumps(_remote_inventory_payload(tmp_path, archive_root)))
        if "--confirm" in command[-1]:
            from src.application.research.archive import _validate_cleanup_preview

            digest = _validate_cleanup_preview(
                preview,
                remote_runtime_root="/var/lib/options-monitor",
            )["plan_sha256"]
            confirmed = _cleanup_payload(
                data={"status": "cleaned", "expected_output_runs_plan_sha256": digest}
            )
            return _completed(command, json.dumps(confirmed))
        return _completed(command, json.dumps(preview))

    return _run_cmd


def test_archive_verify_without_source_is_read_only(tmp_path: Path) -> None:
    from src.application.research.archive import archive_verify

    archive_root = tmp_path / "archive"
    _write_minimal_run(archive_root)

    data = archive_verify(repo_root=tmp_path, archive_root=archive_root, now_fn=_fixed_now)

    latest_path = archive_root / "manifests" / "inventory.latest.json"
    assert data["ok"] is True
    assert data["summary"]["verified_run_count"] == 1
    assert data["runs"][0]["run_id"] == "run-1"
    assert data["runs"][0]["verified"] is True
    assert data["source_identity"] == {}
    assert data["runs"][0]["source_content_verified"] is False
    assert data["runs"][0]["deletion_verified"] is False
    assert data["verified_at_utc"] == "2026-06-04T12:00:00Z"
    assert not latest_path.parent.exists()


def test_archive_verify_missing_archive_does_not_create_evidence(tmp_path: Path) -> None:
    from src.application.research.archive import archive_prune_remote, archive_verify

    root = tmp_path / "absent"
    data = archive_verify(repo_root=tmp_path, archive_root=root)
    assert data["ok"] is False
    assert data["runs"] == []
    assert not root.exists()

    def unexpected_command(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("missing evidence must not start a remote command")

    prune = archive_prune_remote(
        repo_root=tmp_path, archive_root=root, ssh_target="deploy@example",
        run_cmd=unexpected_command,
    )
    assert prune["status"] == "missing_verified_inventory"
    assert "pull --write" in prune["reason"]
    assert not root.exists()


@pytest.mark.parametrize("explicit", [
    {"source_identity": {}},
    {"source_run_inventory": []},
    {"source_identity": {}, "source_run_inventory": []},
])
def test_archive_verify_explicit_empty_evidence_replaces_prior_proof(
    tmp_path: Path, explicit: dict[str, Any],
) -> None:
    from src.application.research.archive import archive_verify

    root = tmp_path / "archive"
    _write_minimal_run(root)
    _verify_remote_archive(tmp_path, root)
    latest = root / "manifests" / "inventory.latest.json"
    before = json.loads(latest.read_text(encoding="utf-8"))
    assert before["runs"][0]["deletion_verified"] is True

    result = archive_verify(repo_root=tmp_path, archive_root=root, **explicit)

    assert json.loads(latest.read_text(encoding="utf-8")) == result
    assert result["source_identity"] == {}
    assert result["runs"][0]["deletion_verified"] is False


def test_archive_cli_verify_preserves_pull_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import argparse
    import shutil

    from src.application.research import archive
    from src.interfaces.cli.research import add_research_commands, handle_research_command

    source = tmp_path / "source"
    root = tmp_path / "archive"
    _write_minimal_run(source)
    retained_cleanup: list[Path] = []
    monkeypatch.setattr(archive.shutil, "rmtree", lambda path: retained_cleanup.append(Path(path)))

    def synthetic_transfer(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[0] == "ssh":
            return _completed(command, json.dumps(_remote_inventory_payload(tmp_path, source)))
        assert command[0] == "rsync"
        relative = command[-2].split("/var/lib/options-monitor/", 1)[1]
        path = source / relative
        if path.is_dir():
            shutil.copytree(path, Path(command[-1]), dirs_exist_ok=True)
        return _completed(command)

    pulled = archive.archive_pull(
        repo_root=tmp_path, archive_root=root, ssh_target="deploy@example",
        include_logs=False, write=True, run_cmd=synthetic_transfer, now_fn=_fixed_now,
    )
    assert pulled["ok"] is True
    latest = root / "manifests" / "inventory.latest.json"
    before_bytes, before_mtime = latest.read_bytes(), latest.stat().st_mtime_ns
    before = json.loads(before_bytes)
    assert before["runs"][0]["deletion_verified"] is True

    parser = argparse.ArgumentParser()
    add_research_commands(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(["research", "archive", "verify", "--archive-root", str(root)])
    result = handle_research_command(args, repo_base_fn=lambda: tmp_path)

    assert result["ok"] is True
    assert result["data"]["runs"][0]["verified"] is True
    assert result["data"]["source_identity"] == {}
    assert result["data"]["runs"][0]["deletion_verified"] is False
    assert latest.read_bytes() == before_bytes
    assert latest.stat().st_mtime_ns == before_mtime
    calls: list[list[str]] = []
    guard = archive.archive_prune_remote(
        repo_root=tmp_path, archive_root=root, ssh_target="deploy@example",
        run_cmd=_prune_run_cmd(calls, _cleanup_payload(), tmp_path, source),
    )["deletion_guard"]
    assert guard["confirmable"] is True
    assert len(calls) == 2
    assert retained_cleanup and all(path.exists() for path in retained_cleanup)


def test_archive_verify_does_not_overwrite_concurrent_source_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.application.research import archive

    root = tmp_path / "archive"
    _write_minimal_run(root)
    _verify_remote_archive(tmp_path, root)
    latest = root / "manifests" / "inventory.latest.json"
    prior = latest.read_bytes()
    original_inventory = archive._run_inventory
    published: list[bytes] = []

    def concurrent_inventory(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        runs = original_inventory(*args, **kwargs)
        with monkeypatch.context() as context:
            context.setattr(archive, "_run_inventory", original_inventory)
            archive.archive_verify(
                repo_root=tmp_path, archive_root=root,
                source_identity={"kind": "local", "runtime_root": str(tmp_path / "new-source")},
                source_run_inventory=runs,
            )
        published.append(latest.read_bytes())
        return runs

    monkeypatch.setattr(archive, "_run_inventory", concurrent_inventory)
    result = archive.archive_verify(repo_root=tmp_path, archive_root=root)

    assert result["source_identity"] == {}
    assert published[0] != prior
    assert latest.read_bytes() == published[0]


@pytest.mark.parametrize("change", ["same_size_and_mtime", "missing", "partial"])
def test_archive_verify_does_not_refresh_changed_local_proof(
    tmp_path: Path, change: str,
) -> None:
    import os

    from src.application.research.archive import archive_prune_remote, archive_verify

    root = tmp_path / "archive"
    run = _write_minimal_run(root)
    _verify_remote_archive(tmp_path, root)
    latest = root / "manifests" / "inventory.latest.json"
    before = latest.read_bytes()
    remote = _remote_inventory_payload(tmp_path, root)
    if change == "same_size_and_mtime":
        path = run / "state" / "last_run.json"
        stat = path.stat()
        path.write_bytes(path.read_bytes().replace(b'"ok"', b'"no"'))
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    elif change == "missing":
        run.rename(root / "retained-run-1")
    else:
        (run / ".partial-fact").write_text("incomplete", encoding="utf-8")

    local = archive_verify(repo_root=tmp_path, archive_root=root)
    assert latest.read_bytes() == before
    assert local["summary"]["deletion_verified_run_count"] == 0
    calls: list[list[str]] = []

    def run_cmd(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert "--confirm" not in command[-1]
        payload = remote if "python3 -c" in command[-1] else _cleanup_payload()
        return _completed(command, json.dumps(payload))

    result = archive_prune_remote(
        repo_root=tmp_path, archive_root=root, ssh_target="deploy@example",
        confirm=True, run_cmd=run_cmd,
    )
    assert result["status"] == "remote_prune_guard_failed"
    assert result["deletion_guard"]["confirmable"] is False
    assert result["deletion_guard"]["unverified_delete_run_ids"] == ["run-1"]
    assert len(calls) == 2


@pytest.mark.parametrize("count", [1, 3, 6])
def test_archive_repeat_pull_hashes_only_current_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int,
) -> None:
    from collections import Counter
    from src.application.research import archive

    source, root = tmp_path / "source", tmp_path / "archive"
    run_ids = [f"run-{index}" for index in range(count)]
    for run_id in run_ids:
        _write_minimal_run(source, run_id)
    remote = _remote_inventory_payload(tmp_path, source)
    transfer = _synthetic_archive_transfer(source, remote)
    retained: list[Path] = []
    monkeypatch.setattr(archive.shutil, "rmtree", lambda path: retained.append(Path(path)))
    original_manifest = archive._file_manifest
    hashed: list[Path] = []

    def record_hash(path: Path) -> list[dict[str, Any]]:
        hashed.append(path)
        return original_manifest(path)

    monkeypatch.setattr(archive, "_file_manifest", record_hash)
    arguments = dict(
        repo_root=tmp_path, archive_root=root, ssh_target="deploy@example",
        remote_runtime_root=source, write=True, run_cmd=transfer, include_logs=False,
    )
    assert archive.archive_pull(**arguments)["ok"] is True
    assert len(hashed) == 2 * count
    hashed.clear()
    assert archive.archive_pull(**arguments)["ok"] is True
    assert len(hashed) == 3 * count
    assert Counter(path.name for path in hashed if path.parent == root / "output_runs") == {
        run_id: 2 for run_id in run_ids
    }

    hashed.clear()
    assert archive.archive_pull(**arguments, run_ids=[run_ids[0]])["ok"] is True
    # Selected staging + selected comparison + one final full verification.
    assert len(hashed) == count + 2
    live_counts = Counter(path.name for path in hashed if path.parent == root / "output_runs")
    assert live_counts == {run_id: 2 if run_id == run_ids[0] else 1 for run_id in run_ids}
    assert retained and all(path.exists() for path in retained)


def test_archive_repeat_pull_detects_change_after_prior_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.application.research import archive

    source, root = tmp_path / "source", tmp_path / "archive"
    for run_id in ("run-1", "run-2"):
        _write_minimal_run(source, run_id)
    remote = _remote_inventory_payload(tmp_path, source)
    remote["runs"].sort(key=lambda row: row["run_id"])
    transfer = _synthetic_archive_transfer(source, remote)
    retained: list[Path] = []
    monkeypatch.setattr(archive.shutil, "rmtree", lambda path: retained.append(Path(path)))
    arguments = dict(
        repo_root=tmp_path, archive_root=root, ssh_target="deploy@example",
        remote_runtime_root=source, write=True, run_cmd=transfer, include_logs=False,
    )
    assert archive.archive_pull(**arguments)["ok"] is True
    original_inventory = archive._run_inventory
    changed: list[Path] = []

    def mutate_after_first_comparison(runs_root: Path, **kwargs: Any) -> list[dict[str, Any]]:
        rows = original_inventory(runs_root, **kwargs)
        if runs_root == root / "output_runs" and not changed:
            path = runs_root / "run-2" / "state" / "last_run.json"
            path.write_text("changed during publication", encoding="utf-8")
            changed.append(path)
        return rows

    monkeypatch.setattr(archive, "_run_inventory", mutate_after_first_comparison)
    result = archive.archive_pull(**arguments)
    assert result["ok"] is False
    assert result["manifest"] is None
    assert changed and changed[0].read_text(encoding="utf-8") == "changed during publication"
    assert any(
        operation.get("reason") == "archive_run_conflict" and operation.get("run_id") == "run-2"
        for operation in result["operations"]
    )


@pytest.mark.parametrize("filename", ["ascii.txt", "证据-é-🙂.txt", 'space \\"quoted\\" \\\\ evidence.txt'])
def test_remote_archive_digest_matches_local_for_valid_filenames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str,
) -> None:
    from contextlib import redirect_stdout
    import hashlib
    import io
    import os
    import socket
    from src.application.research import archive

    source, root = tmp_path / "source", tmp_path / "archive"
    run = _write_minimal_run(source)
    evidence = run / filename
    evidence.write_text("before", encoding="utf-8")

    def inventory_from_actual_script() -> dict[str, Any]:
        output = io.StringIO()
        with monkeypatch.context() as context, redirect_stdout(output):
            context.setattr(sys, "argv", ["inventory", str(source), "", ""])
            context.setattr(socket, "getfqdn", lambda: "prod.example")
            exec(compile(archive.REMOTE_INVENTORY_SCRIPT, "remote_inventory", "exec"), {})
        return json.loads(output.getvalue())

    remote = inventory_from_actual_script()
    local = archive._run_inventory(source / "output_runs", base=tmp_path)[0]
    assert remote["runs"][0]["file_manifest"] == local["file_manifest"]
    assert remote["runs"][0]["content_digest"] == local["content_digest"]
    if filename.isascii():
        assert local["content_digest"] == hashlib.sha256(
            json.dumps(local["file_manifest"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    retained: list[Path] = []
    monkeypatch.setattr(archive.shutil, "rmtree", lambda path: retained.append(Path(path)))
    result = archive.archive_pull(
        repo_root=tmp_path, archive_root=root, ssh_target="deploy@example",
        remote_runtime_root=source, write=True, include_logs=False,
        run_cmd=_synthetic_archive_transfer(source, remote),
    )
    assert result["ok"] is True
    latest = json.loads((root / "manifests" / "inventory.latest.json").read_text(encoding="utf-8"))
    assert latest["runs"][0]["deletion_verified"] is True

    stat = evidence.stat()
    evidence.write_text("after!", encoding="utf-8")
    os.utime(evidence, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    changed_remote = inventory_from_actual_script()["runs"][0]
    changed_local = archive._run_inventory(source / "output_runs", base=tmp_path)[0]
    assert evidence.stat().st_size == stat.st_size
    assert evidence.stat().st_mtime_ns == stat.st_mtime_ns
    assert changed_remote["content_digest"] == changed_local["content_digest"]
    assert changed_remote["content_digest"] != local["content_digest"]


def test_archive_pull_defaults_to_rsync_dry_run_and_filters_local_runs(tmp_path: Path) -> None:
    from src.application.research.archive import archive_pull

    source = tmp_path / "source"
    _write_run(source, "run-1")
    calls: list[list[str]] = []

    def _run_cmd(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return _completed(command)

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
    provider = _nvda_scan_provider()
    raw_bytes = (json.dumps(provider, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    columns = _nvda_scan_columns()
    csv_bytes = (
        ",".join(columns) + "\nNVDA,put,2026-08-21,NVDA260821P00100000,100,100\n"
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
        return _completed(command)

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

    provider = _nvda_scan_provider()
    columns = _nvda_scan_columns()
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
            return _completed(command)
        selected = json.loads(shlex.split(command[-1])[-1])
        inventory_batches.append(selected)
        inventory = {
            "runtime_root": "/var/lib/options-monitor",
            "runs_root": "/var/lib/options-monitor/output_runs",
            "source_host": "prod.example",
            "runs": [{"run_id": run_id, "mtime": 1} for run_id in selected],
        }
        return _completed(command, json.dumps(inventory))

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
        return _completed(command, "not-json" if command[0] == "ssh" else "dry\n")

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
        return _completed(command)

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
    preview = _cleanup_payload(
        data={
            "output_runs_cleanup": {
                "delete_runs": [
                    {"path": "/var/lib/options-monitor/output_runs/run-1"},
                    {"path": "/var/lib/options-monitor/output_runs/run-2"},
                ]
            }
        }
    )

    data = archive_prune_remote(
        repo_root=tmp_path,
        archive_root=archive_root,
        ssh_target="deploy@example",
        confirm=True,
        run_cmd=_prune_run_cmd(calls, preview, tmp_path, archive_root),
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
    preview = _cleanup_payload()

    data = archive_prune_remote(
        repo_root=tmp_path,
        archive_root=archive_root,
        ssh_target="deploy@example",
        confirm=True,
        run_cmd=_prune_run_cmd(calls, preview, tmp_path, archive_root),
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
            return _completed(command, json.dumps(_remote_inventory_payload(tmp_path, archive_root)))
        return _completed(command, "not-json")

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
    preview = _cleanup_payload()
    calls: list[list[str]] = []

    def _run_cmd(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        payload = remote_inventory if "python3 -c" in command[-1] else preview
        return _completed(command, json.dumps(payload))

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
    preview = _cleanup_payload()
    calls: list[list[str]] = []

    def _run_cmd(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        payload = (
            _remote_inventory_payload(tmp_path, archive_root)
            if "python3 -c" in command[-1]
            else preview
        )
        return _completed(command, json.dumps(payload))

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


def test_critical_files_include_current_candidate_contract(tmp_path: Path) -> None:
    from src.application.research.archive import _critical_files

    run_dir = tmp_path / "run-current"
    account_dir = run_dir / "accounts" / "lx"
    state_dir = account_dir / "state"
    state_dir.mkdir(parents=True)
    for path in (
        state_dir / "candidate_snapshot_manifest.v4.json",
        state_dir / "wheel_candidate_snapshot.v3.json",
        account_dir / "strategy_scan_status_index.v5.json",
        account_dir / "nvda_wheel_put_scan_status.v3.json",
    ):
        path.write_text("{}\n", encoding="utf-8")

    critical = _critical_files(run_dir)

    assert critical["candidate_manifest_files"] == [
        "accounts/lx/state/candidate_snapshot_manifest.v4.json"
    ]
    assert critical["candidate_snapshot_files"] == [
        "accounts/lx/state/wheel_candidate_snapshot.v3.json"
    ]
    assert critical["candidate_status_files"] == [
        "accounts/lx/nvda_wheel_put_scan_status.v3.json",
        "accounts/lx/strategy_scan_status_index.v5.json",
    ]
