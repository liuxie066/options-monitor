from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.research import storage_baseline as module


NOW = datetime(2026, 8, 13, 5, 0, tzinfo=timezone.utc)


def _make_runtime(tmp_path: Path, *, with_ledger: bool = True) -> Path:
    root = tmp_path / "runtime"
    for relpath in (
        "output_runs",
        "output_accounts",
        "output_shared/state",
        "output_shared/research",
        "output_shared/required_data",
        "logs",
    ):
        (root / relpath).mkdir(parents=True, exist_ok=True)
    if with_ledger:
        _write_ledger(root / "output_shared/state/option_positions.sqlite3")
    return root


def _write_ledger(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE trade_events ("
            "event_id TEXT PRIMARY KEY, event_json TEXT NOT NULL, "
            "trade_time_ms INTEGER NOT NULL, created_at_ms INTEGER NOT NULL, "
            "updated_at_ms INTEGER NOT NULL)"
        )
        conn.execute("CREATE TABLE position_lots (record_id TEXT PRIMARY KEY, fields_json TEXT NOT NULL)")
        conn.execute("CREATE TABLE future_table (payload TEXT)")
        conn.execute(
            "INSERT INTO trade_events VALUES (?, ?, ?, ?, ?)",
            ("event-1", '{"account":"lx","contracts":1}', 1, 1, 1),
        )
        conn.execute(
            "INSERT INTO position_lots VALUES (?, ?)",
            ("lot-1", '{"account":"lx","status":"open"}'),
        )
        conn.commit()
    finally:
        conn.close()


def _stub_source_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        module,
        "_collect_source_inventory",
        lambda **_kwargs: {
            "schema_version": "data_storage_runtime_source_inventory.v1",
            "status": "complete",
            "classified_match_count": 1,
            "stale_locators": [],
            "unclassified_matches": [],
        },
    )


def _collect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    root: Path | None = None,
    history_reports: list[Path] | None = None,
    output: Path | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    _stub_source_inventory(monkeypatch)
    runtime = root or _make_runtime(tmp_path)
    return module.collect_storage_runtime_baseline(
        repo_root=Path.cwd(),
        runtime_root=runtime,
        history_reports=history_reports,
        output=output,
        overwrite=overwrite,
        now_fn=lambda: NOW,
    )


def _tree_identity(root: Path) -> list[tuple[str, str, int, int, bytes | None]]:
    rows: list[tuple[str, str, int, int, bytes | None]] = []
    for path in sorted(root.rglob("*")):
        stat = path.lstat()
        kind = "symlink" if path.is_symlink() else "dir" if path.is_dir() else "file"
        content = path.read_bytes() if kind == "file" else None
        rows.append((path.relative_to(root).as_posix(), kind, stat.st_size, stat.st_mtime_ns, content))
    return rows


def _write_research_manifest(
    root: Path,
    *,
    entries: list[dict[str, Any]],
    name: str = "manifest.json",
) -> Path:
    dataset = root / "output_shared/research/dataset"
    dataset.mkdir(parents=True, exist_ok=True)
    path = dataset / name
    path.write_text(
        json.dumps(
            {
                "schema_version": "research_fixture.v1",
                "generation_id": "generation-1",
                "market": "us",
                "files": entries,
            }
        ),
        encoding="utf-8",
    )
    return path


def _history_report(path: Path, *, root: Path, observed_at: str, unique_bytes: int) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": module.SCHEMA_VERSION,
                "identity": {
                    "runtime_root": str(root.resolve()),
                    "observed_at_utc": observed_at,
                },
                "research_storage": {"unique_declared_bytes": unique_bytes},
            }
        ),
        encoding="utf-8",
    )


def _publish_scan_blob(root: Path, symbol: str) -> dict[str, Any]:
    from src.application.required_data_blobs import publish_required_data_scan_blob

    contract = f"{symbol}300101P00100000"
    provider = {
        "symbol": symbol,
        "rows": [
            {
                "symbol": symbol,
                "option_type": "put",
                "expiration": "2030-01-01",
                "contract_symbol": contract,
                "strike": 100,
                "multiplier": 100,
            }
        ],
    }
    raw_bytes = (json.dumps(provider, ensure_ascii=False, indent=2) + "\n").encode()
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
        + f"\n{symbol},put,2030-01-01,{contract},100,100\n"
    ).encode()
    return publish_required_data_scan_blob(
        runtime_root=root,
        symbol=symbol,
        market="US",
        raw_json_bytes=raw_bytes,
        required_data_csv_bytes=csv_bytes,
        columns=columns,
    )


def _write_scan_blob_manifest(path: Path, *refs: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"scan_blob_refs": list(refs)}), encoding="utf-8")


@pytest.mark.parametrize("fault", ["present", "missing", "symlink"])
def test_baseline_and_gc_agree_on_protected_scan_blob_presence(
    storage_disk_capacity, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fault: str,
) -> None:
    root = _make_runtime(tmp_path)
    ref = _publish_scan_blob(root, "NVDA")
    manifest = root / "output_runs/current/state/required_data_snapshot_manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({
        "schema_version": module.REQUIRED_DATA_SNAPSHOT_MANIFEST_SCHEMA,
        "run_id": "current", "symbols": {"NVDA": {"status": "ready", "scan_blob_ref": ref}},
    }))
    blob = root / ref["blob_relpath"]
    if fault != "present":
        preserved = tmp_path / "preserved-blob.gz"
        blob.rename(preserved)
        if fault == "symlink":
            blob.symlink_to(preserved)
    before = _tree_identity(root)
    baseline = _collect(monkeypatch, tmp_path, root=root)
    gc = module.preview_scan_blob_gc(runtime_root=root, now_fn=lambda: NOW)
    assert _tree_identity(root) == before
    assert gc["deletion_allowed"] is (fault == "present")
    research = baseline["research_storage"]
    if fault == "present":
        assert research["status"] == "complete"
        assert research["protected_reference_failures"] == []
        assert baseline["thresholds"]["critical_reasons"] == []
        assert research["physical_bytes"] == blob.stat().st_size + manifest.stat().st_size
        classes = {row["storage_class"]: row for row in baseline["runtime_storage"]["by_class"]}
        assert classes["immutable_shared_partition"]["size_bytes"] == blob.stat().st_size
        assert research["content_verification"] == "not_performed"
    else:
        assert research["status"] == "data_unavailable"
        assert research["protected_reference_failures"]
        assert baseline["thresholds"]["status"] == "critical"


@pytest.mark.parametrize(("fault", "ancestor"), [
    (fault, ancestor)
    for fault in ("symlink", "dangling_symlink")
    for ancestor in ("blob_file", "shard", "sha256", "blobs")
] + [("not_directory", ancestor) for ancestor in ("shard", "sha256", "blobs")])
def test_gc_absent_expired_ref_keeps_blob_ancestor_guards(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys, ancestor: str, fault: str,
) -> None:
    from src.interfaces.cli.main import main

    root = _make_runtime(tmp_path)
    ref = _publish_scan_blob(root, "NVDA")
    _publish_scan_blob(root, "AMD")
    observed_at = datetime(2030, 2, 1, tzinfo=timezone.utc)
    timestamp = observed_at.timestamp() - 30 * 86400
    for index in range(201):
        run = root / "output_runs" / f"run-{index:03d}"
        run.mkdir()
        if index == 0:
            _write_scan_blob_manifest(run / "state/manifest.json", ref)
        os.utime(run, (timestamp + index, timestamp + index))
    blob = root / ref["blob_relpath"]
    path = {
        "blob_file": blob, "shard": blob.parent,
        "sha256": root / "output_shared/blobs/sha256", "blobs": root / "output_shared/blobs",
    }[ancestor]
    original_bytes = blob.read_bytes()
    preserved = tmp_path / "preserved-target"
    path.rename(preserved)
    if fault == "not_directory":
        path.write_bytes(b"synthetic non-directory")
    else:
        path.symlink_to(preserved if fault == "symlink" else tmp_path / "absent-target")
    monkeypatch.setattr(module, "_utc_now", lambda _fn: observed_at)
    before = _tree_identity(root)
    baseline = _collect(monkeypatch, tmp_path, root=root)
    preview = module.preview_scan_blob_gc(runtime_root=root)
    assert main(["research", "storage-gc-preview", "--runtime-root", str(root)]) == 0
    cli = json.loads(capsys.readouterr().out)["data"]
    assert _tree_identity(root) == before
    assert preview == cli
    assert preview["status"] == "data_unavailable"
    assert preview["deletion_allowed"] is False
    assert preview["candidates"] == []
    assert baseline["research_storage"]["protected_reference_failures"]
    assert baseline["research_storage"]["unprotected_reference_failures"] == []
    preserved_blob = preserved if ancestor == "blob_file" else preserved / blob.relative_to(path)
    assert preserved_blob.read_bytes() == original_bytes


def test_gc_inventory_read_failure_never_proves_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = _make_runtime(tmp_path, with_ledger=False)
    ref = _publish_scan_blob(root, "NVDA")
    scandir = os.scandir
    parent = root / "output_shared/blobs/sha256"

    def unreadable(path):
        if not isinstance(path, int) and Path(os.fsdecode(path)) == parent:
            raise PermissionError("synthetic scan access failure")
        return scandir(path)

    monkeypatch.setattr(os, "scandir", unreadable)
    if scandir in os.supports_fd:
        descriptor = os.open(parent, os.O_RDONLY)
        try:
            with os.scandir(descriptor) as entries:
                assert {entry.name for entry in entries} == {Path(ref["blob_relpath"]).parent.name}
        finally:
            os.close(descriptor)
    with pytest.raises(PermissionError, match="synthetic scan access failure"):
        os.scandir(os.fsencode(parent))
    with pytest.raises(PermissionError, match="synthetic scan access failure"):
        module.preview_scan_blob_gc(runtime_root=root)
    assert (root / ref["blob_relpath"]).is_file()


def test_expired_scan_blob_relocation_keeps_repeated_previews_usable(
    storage_disk_capacity, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    root = _make_runtime(tmp_path)
    ref = _publish_scan_blob(root, "NVDA")
    orphan_ref = _publish_scan_blob(root, "AMD")
    retained_ref = _publish_scan_blob(root, "AAPL")
    observed_at = datetime(2030, 2, 1, tzinfo=timezone.utc)
    timestamp = observed_at.timestamp() - 30 * 86400
    for index in range(201):
        run = root / "output_runs" / f"run-{index:03d}"
        run.mkdir()
        if index == 0:
            _write_scan_blob_manifest(run / "state/manifest.json", ref)
        elif index == 1:
            _write_scan_blob_manifest(run / "state/manifest.json", retained_ref)
        os.utime(run, (timestamp + index, timestamp + index))
    before = _tree_identity(root)
    first = module.preview_scan_blob_gc(runtime_root=root, now_fn=lambda: observed_at)
    assert _tree_identity(root) == before
    assert first["deletion_allowed"] is True
    assert {row["blob_sha256"] for row in first["candidates"]} == {
        ref["blob_sha256"], orphan_ref["blob_sha256"],
    }
    blob = root / ref["blob_relpath"]
    data = blob.read_bytes()
    preserved = tmp_path / "preserved-candidate.gz"
    blob.rename(preserved)
    after = _tree_identity(root)
    second = module.preview_scan_blob_gc(runtime_root=root, now_fn=lambda: observed_at)
    third = module.preview_scan_blob_gc(runtime_root=root, now_fn=lambda: observed_at)
    monkeypatch.setattr(module, "_utc_now", lambda _fn: observed_at)
    baseline = _collect(monkeypatch, tmp_path, root=root)
    assert _tree_identity(root) == after
    assert preserved.read_bytes() == data
    assert second == third
    assert second["deletion_allowed"] is True
    assert second["blockers"] == []
    assert [row["blob_sha256"] for row in second["candidates"]] == [orphan_ref["blob_sha256"]]
    assert retained_ref["blob_sha256"] in second["reachable_blob_sha256"]
    assert first["plan_sha256"] != second["plan_sha256"]
    research = baseline["research_storage"]
    assert research["status"] == "complete"
    assert research["protected_reference_failures"] == []
    assert research["unprotected_reference_failures"] == [{
        "manifest": "output_runs/run-000/state/manifest.json",
        "reference": ref["blob_relpath"], "reason": "missing",
    }]
    assert baseline["thresholds"]["critical_reasons"] == []



@pytest.mark.parametrize("protection", [
    "expired", "count", "recent", "shared", "operational_state", "unsafe_runs", "unsafe_blob",
])
def test_missing_expired_scan_blob_exemption_requires_safe_unprotected_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, protection: str,
) -> None:
    root = _make_runtime(tmp_path)
    ref = _publish_scan_blob(root, "NVDA")
    observed_at = datetime(2030, 2, 1, tzinfo=timezone.utc)
    timestamp = observed_at.timestamp() - 30 * 86400
    for index in range(201):
        run = root / "output_runs" / f"run-{index:03d}"
        run.mkdir()
        if index == 0:
            _write_scan_blob_manifest(run / "state/manifest.json", ref)
        os.utime(run, (timestamp + index, timestamp + index))
    if protection == "count":
        os.utime(root / "output_runs/run-000", (timestamp + 500, timestamp + 500))
    elif protection == "recent":
        recent = observed_at.timestamp() - 13 * 86400
        os.utime(root / "output_runs/run-000", (recent, recent))
    elif protection == "shared":
        _write_scan_blob_manifest(root / "output_accounts/lx/state/manifest.json", ref)
    elif protection == "operational_state":
        _write_scan_blob_manifest(root / "output_shared/state/manifest.json", ref)
    elif protection == "unsafe_runs":
        (root / "output_runs/unknown").symlink_to(tmp_path, target_is_directory=True)
    blob = root / ref["blob_relpath"]
    if protection == "unsafe_blob":
        preserved = tmp_path / "preserved-shard"
        blob.parent.rename(preserved)
        blob.parent.symlink_to(preserved, target_is_directory=True)
    else:
        blob.rename(tmp_path / "preserved-blob.gz")
    monkeypatch.setattr(module, "_utc_now", lambda _fn: observed_at)
    before = _tree_identity(root)
    baseline = _collect(monkeypatch, tmp_path, root=root)
    gc = module.preview_scan_blob_gc(runtime_root=root, now_fn=lambda: observed_at)
    assert _tree_identity(root) == before
    assert gc["deletion_allowed"] is (protection == "expired")
    failures = baseline["research_storage"]["protected_reference_failures"]
    assert bool(failures) is (protection != "expired")
    assert bool(baseline["research_storage"]["unprotected_reference_failures"]) is (protection == "expired")


@pytest.mark.parametrize("fault", ["invalid_ref", "conflicting_ref", "generic_missing", "corrupt_blob"])
def test_expired_manifest_exemption_preserves_reference_and_content_guards(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fault: str,
) -> None:
    root = _make_runtime(tmp_path)
    ref = _publish_scan_blob(root, "NVDA")
    observed_at = datetime(2030, 2, 1, tzinfo=timezone.utc)
    timestamp = observed_at.timestamp() - 30 * 86400
    for index in range(201):
        run = root / "output_runs" / f"run-{index:03d}"
        run.mkdir()
        os.utime(run, (timestamp + index, timestamp + index))
    manifest = root / "output_runs/run-000/state/manifest.json"
    if fault == "invalid_ref":
        _write_scan_blob_manifest(manifest, {**ref, "codec": "invalid"})
    elif fault == "conflicting_ref":
        _write_scan_blob_manifest(manifest, ref, {**ref, "compressed_size_bytes": ref["compressed_size_bytes"] + 1})
    else:
        _write_scan_blob_manifest(manifest, ref)
        if fault == "generic_missing":
            payload = json.loads(manifest.read_text())
            payload["files"] = [{"relpath": "missing.csv", "size_bytes": 5, "sha256": "a" * 64}]
            manifest.write_text(json.dumps(payload))
        else:
            (root / ref["blob_relpath"]).write_bytes(b"corrupt")
    os.utime(root / "output_runs/run-000", (timestamp, timestamp))
    monkeypatch.setattr(module, "_utc_now", lambda _fn: observed_at)
    baseline = _collect(monkeypatch, tmp_path, root=root)
    gc = module.preview_scan_blob_gc(runtime_root=root, now_fn=lambda: observed_at)
    if fault in {"invalid_ref", "conflicting_ref"}:
        assert baseline["research_storage"]["manifest_parse_failures"]
        assert gc["deletion_allowed"] is False
    elif fault == "generic_missing":
        assert baseline["research_storage"]["protected_reference_failures"][0]["reason"] == "missing"
    else:
        assert gc["deletion_allowed"] is False
        assert "referenced_blob_missing_or_corrupt" in {row["reason"] for row in gc["blockers"]}
    assert baseline["research_storage"]["unprotected_reference_failures"] == []


def test_baseline_account_manifest_uses_complete_inventory_without_counting_mutable_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    root = _make_runtime(tmp_path)
    payload = root / "output_accounts/lx/state/synthetic.json"
    payload.parent.mkdir(parents=True)
    payload.write_text("{}")
    manifest = payload.parent / "manifest.json"
    manifest.write_text(json.dumps({"files": [{"relpath": "synthetic.json", "size_bytes": 2}]}))
    before = _tree_identity(root)
    baseline = _collect(monkeypatch, tmp_path, root=root)
    assert _tree_identity(root) == before
    assert baseline["research_storage"]["protected_reference_failures"] == []
    assert baseline["research_storage"]["physical_bytes"] == 0
    assert baseline["research_storage"]["content_verification"] == "not_performed"


def test_baseline_does_not_read_canonical_blob_payload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = _make_runtime(tmp_path)
    ref = _publish_scan_blob(root, "NVDA")
    _write_scan_blob_manifest(root / "output_runs/current/state/manifest.json", ref)
    read_bytes = Path.read_bytes

    def read_without_blob(path: Path) -> bytes:
        assert not str(path).endswith(".json.gz"), "metadata baseline must not read blob payload"
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read_without_blob)
    baseline = _collect(monkeypatch, tmp_path, root=root)
    assert baseline["research_storage"]["protected_reference_failures"] == []
    assert baseline["safety"]["payload_content_reads"] == 0


def test_gc_shared_digest_with_conflicting_ref_still_blocks(tmp_path: Path) -> None:
    root = _make_runtime(tmp_path, with_ledger=False)
    ref = _publish_scan_blob(root, "NVDA")
    _write_scan_blob_manifest(root / "output_runs/current/state/manifest.json", ref)
    _write_scan_blob_manifest(root / "output_shared/research/manifest.json", {
        **ref, "compressed_size_bytes": ref["compressed_size_bytes"] + 1,
    })
    preview = module.preview_scan_blob_gc(runtime_root=root)
    assert preview["deletion_allowed"] is False
    assert "blob_reference_conflict" in {row["reason"] for row in preview["blockers"]}


def test_collect_returns_deterministic_schema_and_aggregate_sqlite_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _make_runtime(tmp_path)
    result = _collect(monkeypatch, tmp_path, root=root)

    assert result["schema_version"] == module.SCHEMA_VERSION
    assert result["status"] == "complete"
    assert result["identity"]["ledger_sqlite"] == "output_shared/state/option_positions.sqlite3"
    assert result["sqlite"]["status"] == "complete"
    assert result["sqlite"]["query_mode"] == "stable_copy_mode_ro_query_only"
    tables = {row["table"]: row for row in result["sqlite"]["tables"]}
    assert tables["trade_events"]["row_count"] == 1
    assert tables["trade_events"]["json_bytes"] == len('{"account":"lx","contracts":1}')
    assert tables["position_lots"]["row_count"] == 1
    assert "future_table" in result["sqlite"]["unknown_tables"]
    assert result["safety"] == {
        "query_only_sqlite": True,
        "source_sqlite_connections": 0,
        "no_follow_traversal": True,
        "payload_content_reads": 0,
        "content_verification": "not_performed",
        "mutation_operations": 0,
        "automatic_actions": [],
    }


def test_collection_leaves_runtime_tree_and_sqlite_sidecars_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _make_runtime(tmp_path, with_ledger=False)
    db = root / "output_shared/state/option_positions.sqlite3"
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE trade_events (event_id TEXT PRIMARY KEY, event_json TEXT, "
            "trade_time_ms INTEGER, created_at_ms INTEGER, updated_at_ms INTEGER)"
        )
        conn.execute("INSERT INTO trade_events VALUES ('e', '{}', 1, 1, 1)")
        conn.commit()
        assert db.with_name(db.name + "-wal").exists()
        assert db.with_name(db.name + "-shm").exists()
        before = _tree_identity(root)

        result = _collect(monkeypatch, tmp_path, root=root)

        after = _tree_identity(root)
        assert result["sqlite"]["status"] == "complete"
        assert before == after
    finally:
        conn.close()


def test_runtime_scan_does_not_follow_root_or_nested_symlinks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _make_runtime(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.bin").write_bytes(b"do-not-count")
    (root / "output_runs/link").symlink_to(outside, target_is_directory=True)
    (root / "output").symlink_to(outside, target_is_directory=True)

    result = _collect(monkeypatch, tmp_path, root=root)

    assert all(row["path"] != "outside/secret.bin" for row in result["runtime_storage"]["largest_files"])
    assert {row["path"] for row in result["runtime_storage"]["symlinks_not_followed"]} == {
        "output",
        "output_runs/link",
    }
    roots = {row["root"]: row for row in result["runtime_storage"]["roots"]}
    assert roots["output"]["status"] == "symlink_not_followed"


def test_manifest_reference_cannot_escape_through_unscanned_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _make_runtime(tmp_path)
    outside = tmp_path / "outside-research"
    outside.mkdir()
    (outside / "payload.bin").write_bytes(b"data")
    dataset = root / "output_shared/research/dataset"
    dataset.mkdir(parents=True)
    (dataset / "linked").symlink_to(outside, target_is_directory=True)
    (dataset / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "research.dataset.v1",
                "entries": [
                    {
                        "relpath": "linked/payload.bin",
                        "size_bytes": 4,
                        "sha256": "a" * 64,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = _collect(monkeypatch, tmp_path, root=root)

    assert result["research_storage"]["protected_reference_failures"] == [
        {
            "manifest": "output_shared/research/dataset/manifest.json",
            "reference": "linked/payload.bin",
            "reason": "missing",
        }
    ]
    assert "output_shared/research/dataset/linked" in {
        row["path"] for row in result["runtime_storage"]["symlinks_not_followed"]
    }


def test_runtime_scan_counts_immediate_non_symlink_account_directories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _make_runtime(tmp_path)
    for account in ("lx", "sy", "qa"):
        account_root = root / "output_accounts" / account
        account_root.mkdir()
        (account_root / "report.json").write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside-account"
    outside.mkdir()
    (root / "output_accounts" / "linked").symlink_to(outside, target_is_directory=True)

    result = _collect(monkeypatch, tmp_path, root=root)

    runtime_storage = result["runtime_storage"]
    assert runtime_storage["account_count"] == 3
    assert runtime_storage["account_count_status"] == "complete"
    assert runtime_storage["account_count_basis"] == "immediate_non_symlink_output_accounts_directories"
    assert "output_accounts/linked" in {row["path"] for row in runtime_storage["symlinks_not_followed"]}


def test_runtime_account_count_distinguishes_empty_missing_and_symlink_roots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    empty_root = _make_runtime(tmp_path / "empty")
    empty = _collect(monkeypatch, tmp_path, root=empty_root)["runtime_storage"]

    missing_root = _make_runtime(tmp_path / "missing")
    (missing_root / "output_accounts").rmdir()
    missing = _collect(monkeypatch, tmp_path, root=missing_root)["runtime_storage"]

    symlink_root = _make_runtime(tmp_path / "symlink")
    (symlink_root / "output_accounts").rmdir()
    target = tmp_path / "linked-accounts"
    target.mkdir()
    (symlink_root / "output_accounts").symlink_to(target, target_is_directory=True)
    linked = _collect(monkeypatch, tmp_path, root=symlink_root)["runtime_storage"]

    assert (empty["account_count"], empty["account_count_status"]) == (0, "complete")
    assert (missing["account_count"], missing["account_count_status"]) == (None, "missing")
    assert (linked["account_count"], linked["account_count_status"]) == (
        None,
        "symlink_not_followed",
    )


def test_missing_default_ledger_is_partial_data_not_fabricated_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = _collect(monkeypatch, tmp_path, root=_make_runtime(tmp_path, with_ledger=False))

    assert result["status"] == "partial_data"
    assert result["sqlite"]["status"] == "missing"
    assert result["sqlite"]["tables"] == []


def test_external_ledger_requires_explicit_allow_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub_source_inventory(monkeypatch)
    root = _make_runtime(tmp_path)
    external = tmp_path / "external.sqlite3"
    _write_ledger(external)

    with pytest.raises(AgentToolError, match="inside runtime root"):
        module.collect_storage_runtime_baseline(
            repo_root=Path.cwd(),
            runtime_root=root,
            ledger_sqlite=external,
        )

    result = module.collect_storage_runtime_baseline(
        repo_root=Path.cwd(),
        runtime_root=root,
        ledger_sqlite=external,
        allow_external_ledger=True,
        now_fn=lambda: NOW,
    )
    assert result["identity"]["ledger_sqlite"] == "external:external.sqlite3"


def test_ledger_path_rejects_symlink_leaf_and_parent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub_source_inventory(monkeypatch)
    root = _make_runtime(tmp_path)
    external = tmp_path / "external.sqlite3"
    _write_ledger(external)
    leaf = root / "output_shared/state/linked.sqlite3"
    leaf.symlink_to(external)

    with pytest.raises(AgentToolError, match="symlink"):
        module.collect_storage_runtime_baseline(
            repo_root=Path.cwd(),
            runtime_root=root,
            ledger_sqlite=leaf,
            allow_external_ledger=True,
        )

    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(root / "output_shared/state", target_is_directory=True)
    with pytest.raises(AgentToolError, match="symlink"):
        module.collect_storage_runtime_baseline(
            repo_root=Path.cwd(),
            runtime_root=root,
            ledger_sqlite=linked_parent / "option_positions.sqlite3",
        )


def test_stable_copy_retries_when_source_tuple_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _make_runtime(tmp_path)
    db = root / "output_shared/state/option_positions.sqlite3"
    real_state = module._sqlite_state
    calls = 0

    def changing_once(path: Path):
        nonlocal calls
        calls += 1
        state = real_state(path)
        if calls == 2:
            db_state = state[db.name]
            state[db.name] = (db_state[0], db_state[1], int(db_state[2] or 0) + 1, db_state[3])
        return state

    monkeypatch.setattr(module, "_sqlite_state", changing_once)

    result = _collect(monkeypatch, tmp_path, root=root)

    assert result["sqlite"]["status"] == "complete"
    assert result["sqlite"]["snapshot_attempts"] == 2


def test_stable_copy_exhaustion_is_partial_data(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _make_runtime(tmp_path)
    real_state = module._sqlite_state
    calls = 0

    def always_changing(path: Path):
        nonlocal calls
        calls += 1
        state = real_state(path)
        key = path.name
        db_state = state[key]
        state[key] = (db_state[0], db_state[1], int(db_state[2] or 0) + calls, db_state[3])
        return state

    monkeypatch.setattr(module, "_sqlite_state", always_changing)

    result = _collect(monkeypatch, tmp_path, root=root)

    assert result["status"] == "partial_data"
    assert result["sqlite"]["status"] == "data_unavailable"
    assert result["sqlite"]["reason"] == "stable_copy_or_query_failed"


def test_manifest_declared_hash_dedup_does_not_read_or_verify_payload_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _make_runtime(tmp_path)
    dataset = root / "output_shared/research/dataset"
    dataset.mkdir(parents=True)
    (dataset / "payload-a.bin").write_bytes(b"AAAA")
    (dataset / "payload-b.bin").write_bytes(b"BBBB")
    digest = "f" * 64
    _write_research_manifest(
        root,
        entries=[
            {"relpath": "payload-a.bin", "size_bytes": 4, "sha256": digest},
            {"relpath": "payload-b.bin", "size_bytes": 4, "sha256": digest},
        ],
    )

    result = _collect(monkeypatch, tmp_path, root=root)
    research = result["research_storage"]

    assert research["status"] == "complete"
    assert research["logical_referenced_bytes"] == 8
    assert research["unique_declared_bytes"] == 4
    assert research["dedup_ratio"] == 2.0
    assert research["content_verification"] == "not_performed"
    assert research["same_size_content_status"] == "not_verified"
    assert research["protected_reference_failures"] == []


def test_required_data_root_relpath_resolves_manifest_references(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _make_runtime(tmp_path)
    payload = root / "output_shared/required_data/NVDA.csv"
    payload.write_bytes(b"data")
    manifest_dir = root / "output_runs/run-1/state"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "required_data_snapshot_manifest.v1.json").write_text(
        json.dumps(
            {
                "schema_version": "required_data_snapshot_manifest.v1",
                "required_data_root_relpath": "../../../output_shared/required_data",
                "symbols": {
                    "NVDA": {
                        "payload_relpath": "NVDA.csv",
                        "payload_sha256": "d" * 64,
                        "payload_size_bytes": 4,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = _collect(monkeypatch, tmp_path, root=root)

    assert result["research_storage"]["protected_reference_failures"] == []
    assert result["research_storage"]["logical_referenced_bytes"] == 4


def test_integrity_file_map_counts_keyed_references(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _make_runtime(tmp_path)
    dataset = root / "output_shared/research/dataset-1"
    dataset.mkdir(parents=True)
    (dataset / "payload.jsonl").write_bytes(b"{}\n")
    (dataset / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "research_dataset.v1",
                "dataset_id": "dataset-1",
                "integrity": {
                    "generation_id": "generation-1",
                    "files": {
                        "payload.jsonl": {
                            "sha256": "e" * 64,
                            "bytes": 3,
                            "row_count": 1,
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    result = _collect(monkeypatch, tmp_path, root=root)

    research = result["research_storage"]
    assert research["protected_reference_failures"] == []
    assert research["logical_referenced_bytes"] == 3
    assert research["unique_declared_bytes"] == 3


def test_research_archive_inventory_binds_file_manifest_to_run_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _make_runtime(tmp_path)
    archive = root / "output_shared/research/remote_archive/prod"
    run_file = archive / "output_runs/run-1/state/tick_metrics.json"
    run_file.parent.mkdir(parents=True)
    run_file.write_bytes(b"{}\n")
    manifests = archive / "manifests"
    manifests.mkdir()
    (manifests / "inventory.latest.json").write_text(
        json.dumps(
            {
                "schema_version": "research_archive.v2",
                "action": "verify",
                "archive_root": str(archive),
                "runs": [
                    {
                        "run_id": "run-1",
                        "file_manifest": [
                            {
                                "path": "state/tick_metrics.json",
                                "size_bytes": 3,
                                "sha256": "1" * 64,
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = _collect(monkeypatch, tmp_path, root=root)
    research = result["research_storage"]

    assert research["protected_reference_failures"] == []
    assert research["logical_referenced_bytes"] == 3
    assert research["unique_declared_bytes"] == 3
    assert research["unmanifested_file_count"] == 0


def test_research_archive_inventory_missing_run_file_is_critical(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _make_runtime(tmp_path)
    archive = root / "output_shared/research/remote_archive/prod"
    manifests = archive / "manifests"
    manifests.mkdir(parents=True)
    (manifests / "inventory.latest.json").write_text(
        json.dumps(
            {
                "schema_version": "research_archive.v2",
                "action": "verify",
                "archive_root": str(archive),
                "runs": [
                    {
                        "run_id": "run-1",
                        "file_manifest": [
                            {
                                "path": "state/missing.json",
                                "size_bytes": 3,
                                "sha256": "2" * 64,
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = _collect(monkeypatch, tmp_path, root=root)

    failures = result["research_storage"]["protected_reference_failures"]
    assert failures == [
        {
            "manifest": "output_shared/research/remote_archive/prod/manifests/inventory.latest.json",
            "reference": "state/missing.json",
            "reason": "missing",
        }
    ]
    assert result["thresholds"]["status"] == "critical"


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        ({"relpath": "missing.bin", "size_bytes": 4, "sha256": "a" * 64}, "missing"),
        ({"relpath": "payload.bin", "size_bytes": 99, "sha256": "b" * 64}, "declared_size_mismatch"),
    ],
)
def test_manifest_missing_or_declared_size_mismatch_is_critical(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    entry: dict[str, Any],
    reason: str,
) -> None:
    root = _make_runtime(tmp_path)
    dataset = root / "output_shared/research/dataset"
    dataset.mkdir(parents=True)
    (dataset / "payload.bin").write_bytes(b"data")
    _write_research_manifest(root, entries=[entry])

    result = _collect(monkeypatch, tmp_path, root=root)

    assert result["status"] == "partial_data"
    assert result["research_storage"]["status"] == "data_unavailable"
    assert result["research_storage"]["protected_reference_failures"][0]["reason"] == reason
    assert result["thresholds"]["status"] == "critical"
    assert result["thresholds"]["automatic_actions"] == []
    assert result["thresholds"]["preview"]["actions"] == []


@pytest.mark.parametrize("envelope", [False, True])
@pytest.mark.parametrize("research,accepted", [
    ({"status": "complete"}, True),
    ({}, True),
    ({"status": "data_unavailable"}, False),
    ({"status": "partial_data"}, False),
    ({"status": "unknown"}, False),
    ({"status": None}, False),
    ({"status": ""}, False),
    ({"status": True}, False),
    ({"status": ["complete"]}, False),
    ({"status": {"value": "complete"}}, False),
])
def test_history_research_status_qualifies_growth(tmp_path, research, accepted, envelope):
    payload = {
        "schema_version": module.SCHEMA_VERSION,
        "status": "partial_data",
        "sqlite": {"status": "data_unavailable"},
        "identity": {"runtime_root": str(tmp_path), "observed_at_utc": "2026-07-14T05:00:00+00:00"},
        "research_storage": {"unique_declared_bytes": 200, **research},
    }
    path = tmp_path / "history.json"
    with path.open("x", encoding="utf-8") as handle:
        json.dump({"tool_name": "research.storage-baseline", "data": payload} if envelope else payload, handle)
    before = path.read_bytes()
    result = module._growth_and_forecast(root=tmp_path, observed_at=NOW, current_unique_bytes=300, history_reports=[path])
    assert path.read_bytes() == before
    if accepted:
        assert result["status"] == "complete"
        assert result["monthly_unique_growth_bytes"] == 100
        assert result["forecast_90d_additional_bytes"] == 300
        assert result["rejected_reports"] == []
    else:
        assert result["status"] == "insufficient_history"
        assert result["observations"] == [{"observed_at_utc": NOW.isoformat(), "unique_declared_bytes": 300, "source": "current"}]
        assert result["intervals"] == []
        assert result["monthly_unique_growth_bytes"] is None
        assert result["forecast_90d_additional_bytes"] is None
        assert result["rejected_reports"] == [{"report": path.name, "reason": "research_storage_incomplete"}]


@pytest.mark.parametrize("order", ["bad-first", "bad-last", "only-bad"])
def test_history_research_status_does_not_advance_ordering(tmp_path, order):
    paths = {}
    for name, status, date, size in [
        ("valid", "complete", "2026-06-14T05:00:00+00:00", 100),
        ("bad", "data_unavailable", "2026-07-14T05:00:00+00:00", 280),
        ("bad2", "data_unavailable", "2026-07-15T05:00:00+00:00", 290),
    ]:
        path = tmp_path / f"{name}.json"
        with path.open("x", encoding="utf-8") as handle:
            json.dump({"schema_version": module.SCHEMA_VERSION,
                "identity": {"runtime_root": str(tmp_path), "observed_at_utc": date},
                "research_storage": {"status": status, "unique_declared_bytes": size}}, handle)
        paths[name] = path
    names = {"bad-first": ["bad", "valid", "bad2"], "bad-last": ["valid", "bad", "bad2"], "only-bad": ["bad", "bad2"]}[order]
    result = module._growth_and_forecast(root=tmp_path, observed_at=NOW, current_unique_bytes=300,
        history_reports=[paths[name] for name in names])
    assert result["rejected_reports"] == [{"report": f"{name}.json", "reason": "research_storage_incomplete"} for name in names if name.startswith("bad")]
    assert [row["source"] for row in result["observations"]] == (["current"] if order == "only-bad" else ["valid.json", "current"])
    assert result["status"] == ("insufficient_history" if order == "only-bad" else "complete")
    assert result["monthly_unique_growth_bytes"] == (None if order == "only-bad" else 100)
    assert result["forecast_90d_additional_bytes"] == (None if order == "only-bad" else 300)


@pytest.mark.parametrize("research", [{}, {"status": "complete"}])
@pytest.mark.parametrize("previous,current,monthly,additional", [(0, 0, 0, 0), (400, 300, -100, 0), (200, 300, 100, 300)])
def test_history_research_status_preserves_valid_deltas(tmp_path, research, previous, current, monthly, additional):
    path = tmp_path / "valid.json"
    with path.open("x", encoding="utf-8") as handle:
        json.dump({"schema_version": module.SCHEMA_VERSION,
            "identity": {"runtime_root": str(tmp_path), "observed_at_utc": "2026-07-14T05:00:00+00:00"},
            "research_storage": {"unique_declared_bytes": previous, **research}}, handle)
    result = module._growth_and_forecast(root=tmp_path, observed_at=NOW, current_unique_bytes=current, history_reports=[path])
    assert result["status"] == "complete"
    assert result["monthly_unique_growth_bytes"] == monthly
    assert result["forecast_90d_additional_bytes"] == additional
    assert result["rejected_reports"] == []


@pytest.mark.parametrize("research", [{}, {"status": "complete"}])
@pytest.mark.parametrize("fault,reason", [
    ("json", "JSONDecodeError"), ("schema", "schema_mismatch"),
    ("identity", "shape_invalid"), ("research", "shape_invalid"),
    ("root", "runtime_root_mismatch"), ("time", "identity_or_measure_invalid"),
    ("measure", "identity_or_measure_invalid"), ("future", "out_of_order"),
])
def test_history_research_status_preserves_other_rejections(tmp_path, research, fault, reason):
    payload = {"schema_version": module.SCHEMA_VERSION,
        "identity": {"runtime_root": str(tmp_path), "observed_at_utc": "2026-07-14T05:00:00+00:00"},
        "research_storage": {"unique_declared_bytes": 200, **research}}
    if fault == "schema":
        payload["schema_version"] = "wrong"
    elif fault in {"identity", "research"}:
        payload["identity" if fault == "identity" else "research_storage"] = None
    elif fault == "root":
        payload["identity"]["runtime_root"] = str(tmp_path / "different")
    elif fault in {"time", "future"}:
        payload["identity"]["observed_at_utc"] = "bad-time" if fault == "time" else NOW.isoformat()
    elif fault == "measure":
        payload["research_storage"]["unique_declared_bytes"] = None
    path = tmp_path / "history.json"
    with path.open("x", encoding="utf-8") as handle:
        handle.write("{broken" if fault == "json" else json.dumps(payload))
    result = module._growth_and_forecast(root=tmp_path, observed_at=NOW, current_unique_bytes=300, history_reports=[path])
    assert result["status"] == "insufficient_history"
    assert result["rejected_reports"] == [{"report": path.name, "reason": reason}]


def test_forecast_requires_compatible_ordered_history_and_uses_observed_deltas(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _make_runtime(tmp_path)
    dataset = root / "output_shared/research/dataset"
    dataset.mkdir(parents=True)
    (dataset / "payload.bin").write_bytes(b"x" * 300)
    _write_research_manifest(
        root,
        entries=[{"relpath": "payload.bin", "size_bytes": 300, "sha256": "c" * 64}],
    )
    no_history = _collect(monkeypatch, tmp_path, root=root)
    assert no_history["research_storage"]["growth"]["status"] == "insufficient_history"
    assert no_history["research_storage"]["growth"]["forecast_90d_additional_bytes"] is None

    prior = tmp_path / "prior.json"
    _history_report(
        prior,
        root=root,
        observed_at="2026-07-14T05:00:00+00:00",
        unique_bytes=200,
    )
    with_history = _collect(monkeypatch, tmp_path, root=root, history_reports=[prior])
    growth = with_history["research_storage"]["growth"]
    assert growth["status"] == "complete"
    assert growth["monthly_unique_growth_bytes"] == 100
    assert growth["forecast_90d_additional_bytes"] == 300

    wrong_root = tmp_path / "wrong-root.json"
    _history_report(
        wrong_root,
        root=tmp_path / "other-runtime",
        observed_at="2026-06-14T05:00:00+00:00",
        unique_bytes=100,
    )
    rejected = _collect(monkeypatch, tmp_path, root=root, history_reports=[wrong_root])
    assert rejected["research_storage"]["growth"]["status"] == "insufficient_history"
    assert rejected["research_storage"]["growth"]["rejected_reports"][0]["reason"] == "runtime_root_mismatch"


def test_out_of_order_prior_reports_are_rejected_without_fabricating_growth(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _make_runtime(tmp_path)
    earlier = tmp_path / "earlier.json"
    later = tmp_path / "later.json"
    _history_report(later, root=root, observed_at="2026-08-01T05:00:00+00:00", unique_bytes=20)
    _history_report(earlier, root=root, observed_at="2026-07-01T05:00:00+00:00", unique_bytes=10)

    result = _collect(monkeypatch, tmp_path, root=root, history_reports=[later, earlier])
    growth = result["research_storage"]["growth"]

    assert any(row["reason"] == "out_of_order" for row in growth["rejected_reports"])
    assert growth["status"] == "complete"
    assert len(growth["observations"]) == 2


def test_warning_and_critical_thresholds_never_create_actions() -> None:
    warning = module._capacity_thresholds(
        disk_total_bytes=100 * module.GIB,
        disk_free_bytes=25 * module.GIB,
        research_storage={
            "growth": {
                "forecast_90d_additional_bytes": 10 * module.GIB,
                "rapid_growth_two_consecutive_months": False,
            },
            "protected_reference_failures": [],
            "manifest_parse_failures": [],
            "cold_candidates": [{"path": "p"}],
            "unmanifested_file_count": 3,
        },
    )
    critical = module._capacity_thresholds(
        disk_total_bytes=100 * module.GIB,
        disk_free_bytes=4 * module.GIB,
        research_storage={
            "growth": {
                "forecast_90d_additional_bytes": 0,
                "rapid_growth_two_consecutive_months": False,
            },
            "protected_reference_failures": [],
            "manifest_parse_failures": [],
            "cold_candidates": [],
            "unmanifested_file_count": 0,
        },
    )

    assert warning["status"] == "warning"
    assert warning["automatic_actions"] == []
    assert warning["preview"]["actions"] == []
    assert critical["status"] == "critical"
    assert critical["automatic_actions"] == []


def test_output_is_atomic_external_and_collision_guarded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _make_runtime(tmp_path)
    output = tmp_path / "baseline.json"

    result = _collect(monkeypatch, tmp_path, root=root, output=output)

    assert json.loads(output.read_text(encoding="utf-8")) == result
    assert list(tmp_path.glob(".baseline.json.*.tmp")) == []
    with pytest.raises(AgentToolError, match="already exists"):
        _collect(monkeypatch, tmp_path, root=root, output=output)
    overwritten = _collect(monkeypatch, tmp_path, root=root, output=output, overwrite=True)
    assert json.loads(output.read_text(encoding="utf-8")) == overwritten

    inside = root / "output_shared/research/baseline.json"
    with pytest.raises(AgentToolError, match="outside"):
        _collect(monkeypatch, tmp_path, root=root, output=inside)

    real_output = tmp_path / "real-baseline.json"
    linked_output = tmp_path / "linked-baseline.json"
    linked_output.symlink_to(real_output)
    with pytest.raises(AgentToolError, match="symlink"):
        _collect(monkeypatch, tmp_path, root=root, output=linked_output, overwrite=True)


def test_checked_in_source_inventory_is_complete() -> None:
    result = module._collect_source_inventory(
        repo_root=Path.cwd(),
        manifest_path=Path("docs/architecture/data-storage-runtime-source-inventory.v1.json").resolve(),
    )

    assert result["status"] == "complete"
    assert result["classified_match_count"] > 0
    assert result["stale_locators"] == []
    assert result["unclassified_matches"] == []


def test_source_inventory_rejects_unclassified_discovery(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/owned.py").write_text("list_trade_events()\n", encoding="utf-8")
    (repo / "src/unowned.py").write_text("list_trade_events()\n", encoding="utf-8")
    manifest = repo / "inventory.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "data_storage_runtime_source_inventory.v1",
                "scan_roots": ["src"],
                "discovery_rules": [
                    {
                        "id": "events",
                        "kind": "call",
                        "symbols": ["list_trade_events"],
                        "classifiers": [
                            {
                                "path_glob": "src/owned.py",
                                "owner": "ledger",
                                "operation": "read",
                                "history_dimension": "events",
                                "path_class": "hot",
                                "later_phase": "phase_3",
                            }
                        ],
                    }
                ],
                "declared_locators": [{"path": "src/owned.py", "locator": "list_trade_events"}],
                "ignores": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AgentToolError) as raised:
        module._collect_source_inventory(repo_root=repo, manifest_path=manifest)

    assert raised.value.details is not None
    assert raised.value.details["unclassified"] == [
        {"rule_id": "events", "path": "src/unowned.py", "locator": "list_trade_events"}
    ]


def test_source_inventory_rejects_stale_locator(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/owned.py").write_text("list_trade_events()\n", encoding="utf-8")
    manifest = repo / "inventory.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "data_storage_runtime_source_inventory.v1",
                "scan_roots": ["src"],
                "discovery_rules": [
                    {
                        "id": "events",
                        "kind": "call",
                        "symbols": ["list_trade_events"],
                        "classifiers": [
                            {
                                "path_glob": "src/*.py",
                                "owner": "ledger",
                                "operation": "read",
                                "history_dimension": "events",
                                "path_class": "hot",
                                "later_phase": "phase_3",
                            }
                        ],
                    }
                ],
                "declared_locators": [{"path": "src/owned.py", "locator": "removed_symbol"}],
                "ignores": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AgentToolError) as raised:
        module._collect_source_inventory(repo_root=repo, manifest_path=manifest)

    assert raised.value.details is not None
    assert raised.value.details["stale_locators"] == [{"path": "src/owned.py", "locator": "removed_symbol"}]


def test_tier_classification_is_preview_only() -> None:
    assert module._storage_tier(storage_class="sealed_run_artifact", age_days=999) == "hot"
    assert module._storage_tier(storage_class="research_artifact", age_days=30) == "hot"
    assert module._storage_tier(storage_class="research_artifact", age_days=31) == "warm"


def test_scan_blob_gc_preview_marks_retained_and_research_roots_and_old_orphans(
    tmp_path: Path,
) -> None:
    root = _make_runtime(tmp_path, with_ledger=False)
    observed_at = datetime(2030, 2, 1, tzinfo=timezone.utc)
    expired_ref = _publish_scan_blob(root, "AAA")
    latest_ref = _publish_scan_blob(root, "BBB")
    research_ref = _publish_scan_blob(root, "CCC")
    orphan_ref = _publish_scan_blob(root, "DDD")
    newer_expired_ref = {
        **expired_ref,
        "published_at_utc": "2029-12-31T00:00:00Z",
    }
    base_timestamp = (observed_at.timestamp() - 30 * 86400)
    for index in range(202):
        run_dir = root / "output_runs" / f"run-{index:03d}"
        run_dir.mkdir()
        if index == 0:
            _write_scan_blob_manifest(run_dir / "state/manifest.json", expired_ref)
        elif index == 1:
            _write_scan_blob_manifest(run_dir / "state/manifest.json", newer_expired_ref)
        elif index == 2:
            _write_scan_blob_manifest(run_dir / "state/manifest.json", latest_ref)
        os.utime(run_dir, (base_timestamp + index, base_timestamp + index))
    _write_scan_blob_manifest(
        root / "output_shared/research/daily/manifest.json",
        research_ref,
    )
    before = _tree_identity(root)

    first = module.preview_scan_blob_gc(
        runtime_root=root,
        now_fn=lambda: observed_at,
    )
    second = module.preview_scan_blob_gc(
        runtime_root=root,
        now_fn=lambda: datetime(2030, 2, 1, 1, tzinfo=timezone.utc),
    )

    assert _tree_identity(root) == before
    assert first["deletion_allowed"] is True
    assert first["summary"]["run_count"] == 202
    assert first["summary"]["retained_run_count"] == 200
    assert first["reachable_blob_sha256"] == sorted(
        [latest_ref["blob_sha256"], research_ref["blob_sha256"]]
    )
    assert {item["blob_sha256"] for item in first["candidates"]} == {
        expired_ref["blob_sha256"],
        orphan_ref["blob_sha256"],
    }
    expired_candidate = next(
        item
        for item in first["candidates"]
        if item["blob_sha256"] == expired_ref["blob_sha256"]
    )
    assert expired_candidate["published_at_utc"] == "2029-12-31T00:00:00Z"
    assert first["plan_sha256"] == second["plan_sha256"]
    assert first["observed_at_utc"] != second["observed_at_utc"]
    assert first["candidates"][0]["age_hours"] != second["candidates"][0]["age_hours"]

    first_empty_root = _make_runtime(tmp_path / "first-empty", with_ledger=False)
    second_empty_root = _make_runtime(tmp_path / "second-empty", with_ledger=False)
    first_empty = module.preview_scan_blob_gc(
        runtime_root=first_empty_root,
        now_fn=lambda: observed_at,
    )
    second_empty = module.preview_scan_blob_gc(
        runtime_root=second_empty_root,
        now_fn=lambda: observed_at,
    )
    assert first_empty["summary"] == second_empty["summary"]
    assert first_empty["plan_sha256"] != second_empty["plan_sha256"]


def test_scan_blob_gc_preview_deduplicates_concurrent_same_hash_publish(
    tmp_path: Path,
) -> None:
    root = _make_runtime(tmp_path, with_ledger=False)
    with ThreadPoolExecutor(max_workers=8) as pool:
        refs = list(pool.map(lambda _index: _publish_scan_blob(root, "AAA"), range(8)))

    digest = refs[0]["blob_sha256"]
    assert {ref["blob_sha256"] for ref in refs} == {digest}
    assert len(list((root / "output_shared/blobs/sha256").glob("*/*.json.gz"))) == 1
    assert list((root / "output_shared/blobs/sha256").glob("**/*.tmp")) == []

    before = _tree_identity(root)
    result = module.preview_scan_blob_gc(
        runtime_root=root,
        now_fn=lambda: datetime(2030, 2, 1, tzinfo=timezone.utc),
    )

    assert _tree_identity(root) == before
    assert [item["blob_sha256"] for item in result["candidates"]] == [digest]
    assert result["deletion_allowed"] is True


def test_scan_blob_gc_preview_keeps_recent_run_outside_latest_200(tmp_path: Path) -> None:
    root = _make_runtime(tmp_path, with_ledger=False)
    observed_at = datetime(2030, 2, 1, tzinfo=timezone.utc)
    timestamp = observed_at.timestamp() - 13 * 86400
    for index in range(201):
        run_dir = root / "output_runs" / f"run-{index:03d}"
        run_dir.mkdir()
        os.utime(run_dir, (timestamp, timestamp))

    result = module.preview_scan_blob_gc(
        runtime_root=root,
        now_fn=lambda: observed_at,
    )

    assert result["summary"]["run_count"] == 201
    assert result["summary"]["retained_run_count"] == 201
    assert result["deletion_allowed"] is True


def test_scan_blob_gc_preview_rejects_runtime_root_symlink(tmp_path: Path) -> None:
    root = _make_runtime(tmp_path, with_ledger=False)
    link = tmp_path / "runtime-link"
    link.symlink_to(root, target_is_directory=True)

    with pytest.raises(AgentToolError, match="must not be a symlink"):
        module.preview_scan_blob_gc(runtime_root=link)


@pytest.mark.parametrize("account", ["alpha", "beta"])
def test_scan_blob_gc_preview_accepts_exact_output_account_compatibility_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, account: str,
) -> None:
    root = _make_runtime(tmp_path, with_ledger=False)
    account_root = root / "output_accounts" / account
    protected_ref = _publish_scan_blob(root, "AAA")
    orphan_ref = _publish_scan_blob(root, "BBB")
    _write_scan_blob_manifest(account_root / "state/manifest.json", protected_ref)
    output = root / "output"
    output.symlink_to(account_root, target_is_directory=True)
    before = _tree_identity(root)
    scandir = os.scandir

    def no_alias_traversal(path):
        assert Path(path) != output
        return scandir(path)

    with monkeypatch.context() as guard:
        guard.setattr(os, "scandir", no_alias_traversal)
        result = module.preview_scan_blob_gc(
            runtime_root=root,
            now_fn=lambda: datetime(2030, 2, 1, tzinfo=timezone.utc),
        )

    assert _tree_identity(root) == before
    assert result["deletion_allowed"] is True
    assert result["reachable_blob_sha256"] == [protected_ref["blob_sha256"]]
    assert [row["blob_sha256"] for row in result["candidates"]] == [orphan_ref["blob_sha256"]]
    assert result["compatibility_aliases_not_followed"] == [
        {
            "path": "output",
            "target": f"output_accounts/{account}",
            "classification": "compatibility_alias",
        }
    ]


@pytest.mark.parametrize("target_kind", ["outside", "descendant", "account-link-outside", "dangling"])
def test_scan_blob_gc_preview_rejects_unsafe_output_alias(
    tmp_path: Path, target_kind: str,
) -> None:
    root = _make_runtime(tmp_path, with_ledger=False)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside
    if target_kind == "descendant":
        target = root / "output_accounts/alpha/state"
        target.mkdir(parents=True)
    elif target_kind == "account-link-outside":
        target = root / "output_accounts/alpha"
        target.symlink_to(outside, target_is_directory=True)
    elif target_kind == "dangling":
        target = tmp_path / "absent-account"
    orphan_ref = _publish_scan_blob(root, "AAA")
    output = root / "output"
    output.symlink_to(target, target_is_directory=True)
    before, outside_before = _tree_identity(root), _tree_identity(outside)

    result = module.preview_scan_blob_gc(
        runtime_root=root, now_fn=lambda: datetime(2030, 2, 1, tzinfo=timezone.utc),
    )

    assert _tree_identity(root) == before
    assert _tree_identity(outside) == outside_before
    assert result["deletion_allowed"] is False
    assert result["compatibility_aliases_not_followed"] == []
    assert {"reason": "runtime_subroot_unsafe", "path": "output"} in result["blockers"]
    assert result["candidates"] == []
    assert result["summary"]["stored_blob_count"] == 1
    assert orphan_ref["blob_sha256"] not in result["reachable_blob_sha256"]


def _assert_gc_link_blocked(result: dict[str, Any], relpath: str) -> None:
    assert result["status"] == "data_unavailable"
    assert result["deletion_allowed"] is False
    assert result["candidates"] == []
    assert result["summary"]["candidate_blob_count"] == 0
    assert result["summary"]["candidate_bytes"] == 0
    assert result["blockers"].count({
        "reason": "protected_manifest_symlink_not_followed", "path": relpath,
    }) == 1
    assert result["safety"]["mutation_operations"] == 0
    assert result["safety"]["automatic_actions"] == []


@pytest.mark.parametrize(("relpath", "target_kind"), [
    pytest.param("output_runs/current/state", "external", id="retained-state"),
    ("output_runs/current/state", "internal"),
    ("output_runs/current/state", "dangling"),
    ("output_runs/current/state", "self-loop"),
    ("output_shared/state", "external"),
    ("output_shared/research/dataset", "external"),
    ("output_accounts/alpha", "external"),
    ("output_accounts/alpha/state", "external"),
    ("output_shared/arbitrary/nested", "external"),
    ("manifests/nested", "external"),
    ("output/nested", "external"),
    ("output_shared/research/dataset/manifest.json", "external"),
])
def test_gc_protected_ancestor_link_blocks_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, relpath: str, target_kind: str,
) -> None:
    from src.interfaces.cli.main import main

    observed_at = datetime(2030, 2, 1, tzinfo=timezone.utc)
    root = _make_runtime(tmp_path, with_ledger=False)
    protected_ref = _publish_scan_blob(root, "AAA")
    orphan_ref = _publish_scan_blob(root, "BBB")
    ancestor = root / relpath
    manifest = ancestor if ancestor.suffix == ".json" else ancestor / "manifest.json"
    _write_scan_blob_manifest(manifest, protected_ref)
    if relpath == "output_accounts/alpha/state":
        (root / "output").symlink_to(ancestor.parent, target_is_directory=True)
    visible = module.preview_scan_blob_gc(runtime_root=root, now_fn=lambda: observed_at)
    assert visible["deletion_allowed"] is True
    assert visible["reachable_blob_sha256"] == [protected_ref["blob_sha256"]]
    assert [row["blob_sha256"] for row in visible["candidates"]] == [orphan_ref["blob_sha256"]]

    preserved = (root if target_kind == "internal" else tmp_path) / "preserved-target"
    ancestor.rename(preserved)
    target = {"dangling": tmp_path / "absent-target", "self-loop": ancestor}.get(target_kind, preserved)
    ancestor.symlink_to(target)
    before = _tree_identity(root)
    preserved_before = preserved.read_bytes() if preserved.is_file() else _tree_identity(preserved)
    link_before = os.readlink(ancestor)
    scandir, read_bytes = os.scandir, Path.read_bytes

    def no_target_scan(path):
        if not isinstance(path, int):
            assert Path(os.fsdecode(path)) not in {ancestor, preserved}
        return scandir(path)

    def no_target_read(path):
        assert not any(path == base or base in path.parents for base in (ancestor, preserved))
        return read_bytes(path)

    with monkeypatch.context() as guard:
        guard.setattr(os, "scandir", no_target_scan)
        guard.setattr(Path, "read_bytes", no_target_read)
        result = module.preview_scan_blob_gc(runtime_root=root, now_fn=lambda: observed_at)
        _assert_gc_link_blocked(result, relpath)
        assert module.preview_scan_blob_gc(runtime_root=root, now_fn=lambda: observed_at) == result
        later = module.preview_scan_blob_gc(
            runtime_root=root, now_fn=lambda: datetime(2030, 2, 1, 1, tzinfo=timezone.utc),
        )
        assert later["plan_sha256"] == result["plan_sha256"]
        if relpath == "output_runs/current/state" and target_kind == "external":
            guard.setattr(module, "_utc_now", lambda _fn: observed_at)
            assert main(["research", "storage-gc-preview", "--runtime-root", str(root)]) == 0
            envelope = json.loads(capsys.readouterr().out)
            assert envelope["ok"] is True
            assert envelope["tool_name"] == "research.storage-gc-preview"
            assert envelope["data"] == result
    assert _tree_identity(root) == before
    assert (preserved.read_bytes() if preserved.is_file() else _tree_identity(preserved)) == preserved_before
    assert os.readlink(ancestor) == link_before
    assert result["summary"]["stored_blob_count"] == 2
    assert result["compatibility_aliases_not_followed"] == visible["compatibility_aliases_not_followed"]


@pytest.mark.parametrize("retention", ["latest", "recent", "expired"])
def test_gc_state_link_keeps_run_retention_policy(tmp_path: Path, retention: str) -> None:
    root = _make_runtime(tmp_path, with_ledger=False)
    observed_at = datetime(2030, 2, 1, tzinfo=timezone.utc)
    protected_ref = _publish_scan_blob(root, "AAA")
    orphan_ref = _publish_scan_blob(root, "BBB")
    timestamp = observed_at.timestamp() - (12 if retention == "recent" else 30) * 86400
    for index in range(201):
        run = root / "output_runs" / f"run-{index:03d}"
        run.mkdir()
        os.utime(run, (timestamp + index, timestamp + index))
    current = root / "output_runs/run-000"
    state = current / "state"
    _write_scan_blob_manifest(state / "manifest.json", protected_ref)
    preserved = tmp_path / "preserved-state"
    state.rename(preserved)
    state.symlink_to(preserved, target_is_directory=True)
    current_time = timestamp + 500 if retention == "latest" else timestamp - 86400
    os.utime(current, (current_time, current_time))
    before, preserved_before = _tree_identity(root), _tree_identity(preserved)

    result = module.preview_scan_blob_gc(runtime_root=root, now_fn=lambda: observed_at)

    assert _tree_identity(root) == before
    assert _tree_identity(preserved) == preserved_before
    assert ("run-000" in result["retained_run_ids"]) is (retention != "expired")
    if retention == "expired":
        assert result["deletion_allowed"] is True
        assert result["blockers"] == []
        assert {row["blob_sha256"] for row in result["candidates"]} == {
            protected_ref["blob_sha256"], orphan_ref["blob_sha256"],
        }
    else:
        _assert_gc_link_blocked(result, "output_runs/run-000/state")
        assert result["summary"]["retained_run_count"] == (201 if retention == "recent" else 200)


def test_gc_internal_account_alias_chain_preserves_alias_and_blocks_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _make_runtime(tmp_path, with_ledger=False)
    protected_ref = _publish_scan_blob(root, "AAA")
    orphan_ref = _publish_scan_blob(root, "BBB")
    real = root / "output_accounts/real"
    _write_scan_blob_manifest(real / "state/manifest.json", protected_ref)
    observed_at = datetime(2030, 2, 1, tzinfo=timezone.utc)
    visible = module.preview_scan_blob_gc(runtime_root=root, now_fn=lambda: observed_at)
    assert [row["blob_sha256"] for row in visible["candidates"]] == [orphan_ref["blob_sha256"]]
    alias = root / "output_accounts/alias"
    alias.symlink_to(real, target_is_directory=True)
    output = root / "output"
    output.symlink_to(alias, target_is_directory=True)
    before = _tree_identity(root)
    scandir = os.scandir

    def no_alias_traversal(path):
        assert Path(path) not in {alias, output}
        return scandir(path)

    with monkeypatch.context() as guard:
        guard.setattr(os, "scandir", no_alias_traversal)
        result = module.preview_scan_blob_gc(runtime_root=root, now_fn=lambda: observed_at)
    _assert_gc_link_blocked(result, "output_accounts/alias")
    assert result["compatibility_aliases_not_followed"] == [{
        "path": "output", "target": "output_accounts/real", "classification": "compatibility_alias",
    }]
    assert result["reachable_blob_sha256"] == [protected_ref["blob_sha256"]]
    assert _tree_identity(root) == before


@pytest.mark.parametrize(
    "fault",
    ["manifest_invalid", "manifest_shape_invalid", "blob_missing", "blob_corrupt"],
)
def test_scan_blob_gc_preview_validates_expired_run_references(
    tmp_path: Path,
    fault: str,
) -> None:
    root = _make_runtime(tmp_path, with_ledger=False)
    observed_at = datetime(2030, 2, 1, tzinfo=timezone.utc)
    referenced_ref = _publish_scan_blob(root, "AAA")
    _publish_scan_blob(root, "BBB")
    base_timestamp = observed_at.timestamp() - 30 * 86400
    for index in range(201):
        run_dir = root / "output_runs" / f"run-{index:03d}"
        run_dir.mkdir()
        os.utime(run_dir, (base_timestamp + index, base_timestamp + index))
    manifest = root / "output_runs/run-000/state/required_data_snapshot_manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "schema_version": module.REQUIRED_DATA_SNAPSHOT_MANIFEST_SCHEMA,
                "run_id": "run-000",
                "symbols": {
                    "AAA": {
                        "status": "ready",
                        "scan_blob_ref": referenced_ref,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    os.utime(root / "output_runs/run-000", (base_timestamp, base_timestamp))
    if fault == "manifest_invalid":
        manifest.write_text("{", encoding="utf-8")
    elif fault == "manifest_shape_invalid":
        manifest.write_text("{}\n", encoding="utf-8")
    elif fault == "blob_missing":
        (root / referenced_ref["blob_relpath"]).unlink()
    else:
        (root / referenced_ref["blob_relpath"]).write_bytes(b"corrupt")

    result = module.preview_scan_blob_gc(
        runtime_root=root,
        now_fn=lambda: observed_at,
    )

    if fault == "blob_missing":
        assert result["deletion_allowed"] is True
        assert result["blockers"] == []
        assert referenced_ref["blob_sha256"] not in {item["blob_sha256"] for item in result["candidates"]}
        return
    assert result["deletion_allowed"] is False
    assert result["candidates"] == []
    assert {item["reason"] for item in result["blockers"]} & {
        "referencing_manifest_invalid",
        "referenced_blob_missing_or_corrupt",
    }


@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        ("manifest_invalid", "referencing_manifest_invalid"),
        ("blob_missing", "referenced_blob_missing_or_corrupt"),
        ("blob_corrupt", "referenced_blob_missing_or_corrupt"),
    ],
)
def test_scan_blob_gc_preview_blocks_all_candidates_on_protected_root_failure(
    tmp_path: Path,
    fault: str,
    reason: str,
) -> None:
    root = _make_runtime(tmp_path, with_ledger=False)
    referenced_ref = _publish_scan_blob(root, "AAA")
    orphan_ref = _publish_scan_blob(root, "BBB")
    manifest = root / "output_shared/research/dataset/manifest.json"
    _write_scan_blob_manifest(manifest, referenced_ref)
    if fault == "manifest_invalid":
        manifest.write_text("{", encoding="utf-8")
    elif fault == "blob_missing":
        (root / referenced_ref["blob_relpath"]).unlink()
    else:
        (root / referenced_ref["blob_relpath"]).write_bytes(b"corrupt")

    result = module.preview_scan_blob_gc(
        runtime_root=root,
        now_fn=lambda: datetime(2030, 2, 1, tzinfo=timezone.utc),
    )

    assert orphan_ref["blob_sha256"] not in {
        item["blob_sha256"] for item in result["candidates"]
    }
    assert result["status"] == "data_unavailable"
    assert result["deletion_allowed"] is False
    assert result["candidates"] == []
    assert reason in {item["reason"] for item in result["blockers"]}
    assert result["safety"]["mutation_operations"] == 0


@pytest.mark.parametrize("free_gib, critical", [(80, False), (4, True)])
def test_storage_baseline_qualifies_observed_disk_capacity(
    monkeypatch, tmp_path, storage_disk_capacity, free_gib, critical,
):
    observation = storage_disk_capacity(free_gib=free_gib)
    baseline = _collect(monkeypatch, tmp_path)
    thresholds = baseline["thresholds"]
    assert thresholds["current_free_bytes"] == observation.free
    assert ("current_free_space_below_critical_floor" in thresholds["critical_reasons"]) is critical
    assert thresholds["automatic_actions"] == []
