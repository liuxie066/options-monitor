from __future__ import annotations

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
                "schema_version": "shadow_fixture.v1",
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


def test_shadow_replay_integrity_file_map_counts_keyed_references(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _make_runtime(tmp_path)
    dataset = root / "output_shared/research/shadow_replay/dataset-1"
    dataset.mkdir(parents=True)
    (dataset / "candidate_snapshots.jsonl").write_bytes(b"{}\n")
    (dataset / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "shadow_replay_dataset.v1",
                "dataset_id": "dataset-1",
                "integrity": {
                    "generation_id": "generation-1",
                    "files": {
                        "candidate_snapshots.jsonl": {
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


def test_combo_facet_parallel_file_and_hash_maps_are_joined(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _make_runtime(tmp_path)
    dataset = root / "output_shared/research/shadow_replay/dataset-1"
    dataset.mkdir(parents=True)
    facet_file = dataset / "combo_pair_decisions.jsonl"
    facet_file.write_bytes(b"{}\n")
    (dataset / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "shadow_replay_dataset.v1",
                "combo_pair_facet": {
                    "files": {
                        facet_file.name: str(facet_file),
                    },
                    "file_sha256": {
                        facet_file.name: "f" * 64,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    result = _collect(monkeypatch, tmp_path, root=root)
    research = result["research_storage"]

    assert research["protected_reference_failures"] == []
    assert research["declared_hash_reference_count"] == 1
    assert research["declared_hash_unknown_size_count"] == 1
    assert research["unmanifested_file_count"] == 0


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
