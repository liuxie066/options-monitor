"""Shared gzip storage wraps unmodified exports from the actual research owners."""

import argparse
import gzip
import json

import pytest

from src.application.research import watchlist_series as group
from src.interfaces.cli.research import add_research_commands, handle_research_command
from test_watchlist_research_series import planned, inputs, export  # noqa: F401


def test_real_fetcher_compressed_exports_reuse_objects_without_changing_originals(planned):
    root, _, plan, args, state = planned
    group.collect_watchlist_series(**args, confirm=True)
    raw_export = export(planned)
    originals = {path: path.read_bytes() for path in (root / "archive").rglob("*") if path.is_file()}
    kwargs = dict(
        series_dir=root / "archive",
        expected_plan_sha256=plan["watchlist_series_sha256"],
        cutoff="2026-10-30T19:56:00Z",
        object_root=root / "objects",
    )
    first = group.export_watchlist_series(**kwargs, output_dir=root / "packed-first")
    again = group.export_watchlist_series(**kwargs, output_dir=root / "packed-again")
    assert first["watchlist_export_sha256"] == again["watchlist_export_sha256"] == raw_export["watchlist_export_sha256"]
    assert first["storage"]["new_objects"] > 0
    assert again["storage"]["new_objects"] == again["storage"]["new_object_bytes"] == 0
    assert first["storage"]["new_object_bytes"] < first["storage"]["logical_bytes"]
    packed = json.loads((root / "packed-first/packed-export.json").read_bytes())
    assert (root / "packed-first/packed-export.json").read_bytes() == (
        root / "packed-again/packed-export.json"
    ).read_bytes()
    for record in packed["files"]:
        digest = record["sha256"]
        blob = root / "objects/sha256" / digest[:2] / (digest + ".json.gz")
        raw = gzip.decompress(blob.read_bytes())
        assert raw == (root / "export" / record["path"]).read_bytes()
        assert len(raw) == record["size"] and group.quote._sha256(raw) == digest
        assert blob.stat().st_mode & 0o777 == 0o600
    assert len(list((root / "objects").rglob("*.gz"))) == len({record["sha256"] for record in packed["files"]})
    assert originals == {path: path.read_bytes() for path in originals}
    assert len(state.calls) == 4
    for path in root.glob(".watchlist-export-*"):
        pytest.fail("temporary uncompressed export retained: " + str(path))


def test_compressed_export_conflict_and_output_reuse_never_overwrite(planned):
    root, _, plan, args, state = planned
    group.collect_watchlist_series(**args, confirm=True)
    kwargs = dict(
        series_dir=root / "archive",
        expected_plan_sha256=plan["watchlist_series_sha256"],
        cutoff="2026-10-30T19:56:00Z",
        object_root=root / "objects",
        output_dir=root / "packed",
    )
    group.export_watchlist_series(**kwargs)
    with pytest.raises(FileExistsError):
        group.export_watchlist_series(**kwargs)
    blob = next((root / "objects").rglob("*.gz"))
    blob.write_bytes(b"broken")
    with pytest.raises(ValueError, match="immutable"):
        group.export_watchlist_series(**{**kwargs, "output_dir": root / "conflict"})
    assert blob.read_bytes() == b"broken" and len(state.calls) == 4
    assert not (root / "conflict").exists()


@pytest.mark.parametrize("name", ["runtime", "prune", "archive", "packed"])
def test_compressed_objects_cannot_escape_research_boundaries(planned, name):
    root, _, plan, args, state = planned
    group.collect_watchlist_series(**args, confirm=True)
    with pytest.raises(ValueError, match="overlap"):
        group.export_watchlist_series(
            series_dir=root / "archive",
            expected_plan_sha256=plan["watchlist_series_sha256"],
            cutoff="2026-10-30T19:56:00Z",
            output_dir=root / "packed",
            object_root=root / name,
        )
    assert len(state.calls) == 4 and not (root / "packed").exists()


def test_existing_export_cli_supports_compression_without_production_entry(planned):
    root, _, plan, args, state = planned
    group.collect_watchlist_series(**args, confirm=True)
    parser = argparse.ArgumentParser()
    add_research_commands(parser.add_subparsers(dest="command"))
    command = ["research", "archive", "watchlist-export"]
    for key, value in (
        ("series-dir", root / "archive"),
        ("expected-plan-sha256", plan["watchlist_series_sha256"]),
        ("cutoff", "2026-10-30T19:56:00Z"),
        ("output-dir", root / "cli-packed"),
        ("object-root", root / "objects"),
    ):
        command += ["--" + key, str(value)]
    result = handle_research_command(parser.parse_args(command), repo_base_fn=lambda: pytest.fail("production"))
    assert result["ok"] and result["data"]["storage"]["new_objects"] > 0
    assert len(state.calls) == 4


def test_tiny_objects_cannot_evade_actual_disk_budget(tmp_path):
    root = tmp_path / "objects"
    root.mkdir(mode=0o700)
    for i in range(32):
        path = root / f"{i}.json.gz"
        path.write_bytes(gzip.compress(b"{}", mtime=0))
    logical = sum(p.stat().st_size for p in root.iterdir())
    allocated = sum(p.stat().st_blocks * 512 for p in root.iterdir())
    assert allocated > logical
    budget = {"max_bytes": (logical + allocated) // 2, "min_free_bytes": 1}
    with pytest.raises(ValueError, match="storage budget exceeded"):
        group._storage(root, [root], budget)


def test_disk_budget_reports_logical_and_allocated_without_nested_root_double_count(tmp_path):
    root = tmp_path / "objects"
    root.mkdir(mode=0o700)
    (root / "small.gz").write_bytes(gzip.compress(b"{}", mtime=0))
    (root / "empty").mkdir(mode=0o700)
    budget = {"max_bytes": 10**9, "min_free_bytes": 1}
    a = group._storage(root, [tmp_path], budget)
    b = group._storage(root, [tmp_path, root, root / "empty"], budget)
    assert a["logical_bytes"] == (root / "small.gz").stat().st_size
    assert a["allocated_bytes"] == sum(p.stat().st_blocks * 512 for p in [tmp_path, *tmp_path.rglob("*")])
    assert a["used_bytes"] == max(a["logical_bytes"], a["allocated_bytes"])
    assert {k: v for k, v in a.items() if k != "free_bytes"} == {k: v for k, v in b.items() if k != "free_bytes"}


def test_sparse_original_retains_logical_budget_ceiling(tmp_path):
    path = tmp_path / "sparse.json"
    with path.open("wb") as stream:
        stream.seek(1024 * 1024)
        stream.write(b" ")
    result = group._storage(tmp_path, [tmp_path], {"max_bytes": 10**9, "min_free_bytes": 1})
    assert result["logical_bytes"] == 1024 * 1024 + 1
    assert result["used_bytes"] >= result["logical_bytes"]
    with pytest.raises(ValueError, match="storage budget exceeded"):
        group._storage(tmp_path, [tmp_path], {"max_bytes": result["logical_bytes"], "min_free_bytes": 1})
