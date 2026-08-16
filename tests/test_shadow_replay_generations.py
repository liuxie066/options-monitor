from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from src.application.shadow_replay.common import (
    DATASET_FILES,
    refresh_dataset_manifest,
    validate_dataset_integrity,
    write_jsonl,
)
from src.application.shadow_replay.generations import (
    ResearchGenerationError,
    materialized_dataset_generation,
    resolve_dataset_generation,
)


def _dataset(tmp_path: Path, rows: list[dict] | None = None) -> Path:
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    for name in DATASET_FILES:
        write_jsonl(dataset / name, [])
    write_jsonl(dataset / DATASET_FILES[0], list(rows or []))
    return dataset


def _row(index: int, *, date: str = "2026-08-16") -> dict:
    return {
        "schema_version": "shadow_replay_candidate_snapshot.v1",
        "run_id": "run-1",
        "account": "lx",
        "market": "US",
        "decision_at_utc": f"{date}T00:00:00Z",
        "symbol": f"TEST{index}",
        "contract_symbol": f"TEST{index}260919P00100000",
        "option_type": "put",
        "status": "accepted",
    }


def _store_bytes(dataset: Path) -> int:
    return sum(
        path.stat().st_size
        for root in (dataset / "partitions", dataset / "generations")
        if root.exists()
        for path in root.rglob("*")
        if path.is_file()
    )


def test_generation_append_reuses_parent_and_is_idempotent(tmp_path: Path) -> None:
    rows = [_row(index) for index in range(257)]
    dataset = _dataset(tmp_path, rows)
    first_manifest = refresh_dataset_manifest(dataset)
    first_ref = first_manifest["generation"]
    first = resolve_dataset_generation(dataset, first_ref)
    first_bytes = _store_bytes(dataset)

    rows.append(_row(257))
    write_jsonl(dataset / DATASET_FILES[0], rows)
    second_manifest = refresh_dataset_manifest(dataset)
    second_ref = second_manifest["generation"]
    second = resolve_dataset_generation(dataset, second_ref)
    growth = _store_bytes(dataset) - first_bytes
    delta = json.loads((dataset / second_ref["relpath"]).read_text(encoding="utf-8"))

    assert first["depth"] == 0
    assert second["depth"] == 1
    assert second["manifest_read_count"] == 2
    assert second["partition_payload_read_count"] == 0
    assert second["files"][DATASET_FILES[0]][0] == first["files"][DATASET_FILES[0]][0]
    assert delta["kind"] == "delta"
    assert len(delta["added_partition_sha256"]) == 1
    added_size = delta["changes"][0]["added"][0]["size_bytes"]
    assert growth <= added_size + 64 * 1024
    assert validate_dataset_integrity(dataset)["generation_ref"] == second_ref

    before_revision = second_manifest["integrity"]["revision"]
    before_bytes = _store_bytes(dataset)
    unchanged = refresh_dataset_manifest(dataset)
    assert unchanged["generation"] == second_ref
    assert unchanged["integrity"]["revision"] == before_revision
    assert _store_bytes(dataset) == before_bytes

    with materialized_dataset_generation(dataset, second_ref) as restored:
        for name in DATASET_FILES:
            assert (restored / name).read_bytes() == (dataset / name).read_bytes()


def test_generation_depth_rolls_to_metadata_only_base_and_old_ids_survive(
    tmp_path: Path,
) -> None:
    dataset = _dataset(tmp_path)
    original = refresh_dataset_manifest(dataset)["generation"]
    rows: list[dict] = []
    depths: list[int] = []
    for index in range(33):
        rows.append(_row(index, date=f"2026-09-{index + 1:02d}"))
        write_jsonl(dataset / DATASET_FILES[0], rows)
        ref = refresh_dataset_manifest(dataset)["generation"]
        depths.append(resolve_dataset_generation(dataset, ref)["depth"])

    assert depths[:32] == list(range(1, 33))
    assert depths[32] == 0
    assert resolve_dataset_generation(dataset, original)["generation_id"] == original["generation_id"]

    restored = tmp_path / "restored" / "dataset"
    restored.parent.mkdir()
    shutil.copytree(dataset, restored)
    current_ref = json.loads((restored / "manifest.json").read_text(encoding="utf-8"))["generation"]
    resolved = resolve_dataset_generation(restored, current_ref)
    assert resolved["generation_id"] == current_ref["generation_id"]
    assert resolved["manifest_read_count"] == 1


def test_generation_payload_corruption_fails_closed_only_when_materialized(
    tmp_path: Path,
) -> None:
    dataset = _dataset(tmp_path, [_row(1)])
    ref = refresh_dataset_manifest(dataset)["generation"]
    resolved = resolve_dataset_generation(dataset, ref)
    partition = resolved["files"][DATASET_FILES[0]][0]

    assert resolved["partition_payload_read_count"] == 0
    (dataset / partition["relpath"]).write_bytes(b"corrupt")
    assert resolve_dataset_generation(dataset, ref)["generation_id"] == ref["generation_id"]
    with pytest.raises(ResearchGenerationError, match="partition compressed hash or size mismatch"):
        with materialized_dataset_generation(dataset, ref):
            pass


def test_generation_manifest_symlink_is_rejected(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, [_row(1)])
    ref = refresh_dataset_manifest(dataset)["generation"]
    manifest_path = dataset / ref["relpath"]
    replacement = dataset / "foreign.json"
    replacement.write_bytes(manifest_path.read_bytes())
    manifest_path.unlink()
    manifest_path.symlink_to(replacement)

    with pytest.raises(ResearchGenerationError, match="object is unavailable"):
        resolve_dataset_generation(dataset, ref)


def test_generation_tracks_optional_file_addition_and_removal(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, [_row(1)])
    base_ref = refresh_dataset_manifest(dataset)["generation"]
    optional = dataset / "combo_pair_decisions.jsonl"
    write_jsonl(
        optional,
        [
            {
                "schema_version": "combo_pair_decision.v1",
                "market": "US",
                "account": "lx",
                "decision_at_utc": "2026-08-16T00:00:00Z",
            }
        ],
    )

    added_ref = refresh_dataset_manifest(dataset)["generation"]
    added = resolve_dataset_generation(dataset, added_ref)
    assert "combo_pair_decisions.jsonl" in added["files"]

    optional.unlink()
    removed_ref = refresh_dataset_manifest(dataset)["generation"]
    removed = resolve_dataset_generation(dataset, removed_ref)
    assert "combo_pair_decisions.jsonl" not in removed["files"]
    assert resolve_dataset_generation(dataset, base_ref)["generation_id"] == base_ref["generation_id"]


def test_generation_changes_when_bound_manifest_metadata_changes(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path, [_row(1)])
    first = refresh_dataset_manifest(dataset)
    first_ref = first["generation"]
    first["source_run_ids"] = ["run-2"]
    (dataset / "manifest.json").write_text(
        json.dumps(first, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    second = refresh_dataset_manifest(dataset)
    resolved = resolve_dataset_generation(dataset, second["generation"])

    assert second["generation"]["generation_id"] != first_ref["generation_id"]
    assert resolved["manifest_projection"]["source_run_ids"] == ["run-2"]
