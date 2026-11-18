from __future__ import annotations

from functools import partial
import json
import os
from pathlib import Path

import pytest

from src.application import cc_lp_candidate_snapshot as cc_lp
from src.application import combo_yield_candidate_snapshot as sp_lc
from src.application.candidate_snapshot_manifest import (
    load_candidate_snapshot_bundle,
    publish_candidate_snapshot_manifest,
)
from src.application.payload_helpers import canonical_json_bytes_lines, readable_json_bytes
from src.application.research.archive_bounded import bounded_pull, restore_batch
from src.application.strategy_scan_status import (
    publish_strategy_scan_status,
    publish_strategy_scan_status_index,
)
from tests.test_archive_bounded import limits_file
from tests.test_cc_lp_candidate_snapshot import _seal as seal_cc_lp
from tests.test_combo_yield_candidate_snapshot import _seal as seal_sp_lc


SEALED_AT = "2026-08-12T01:00:00Z"


@pytest.fixture(params=["sp_lc", "cc_lp"])
def owner(request):
    if request.param == "sp_lc":
        return (
            request.param, sp_lc, partial(seal_sp_lc, sealed_at=SEALED_AT),
            sp_lc.load_combo_yield_candidate_snapshot,
            sp_lc.COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE,
            sp_lc.ComboYieldCandidateSnapshotError,
        )
    return (
        request.param, cc_lp, partial(seal_cc_lp, sealed_at=SEALED_AT),
        cc_lp.load_cc_lp_candidate_snapshot, cc_lp.CC_LP_CANDIDATE_SNAPSHOT_FILE,
        cc_lp.CcLpCandidateSnapshotError,
    )


@pytest.mark.parametrize("pairs", [True, False])
def test_new_snapshot_is_smaller_with_identical_payload(tmp_path, owner, pairs):
    _, _, seal, load, filename, _ = owner
    payload = seal(tmp_path, pairs=pairs)
    path = tmp_path / "output_runs/run-1/accounts/lx/state" / filename
    raw = path.read_bytes()
    assert raw == canonical_json_bytes_lines(payload)
    assert len(raw) < len(readable_json_bytes(payload))
    assert json.loads(raw) == json.loads(readable_json_bytes(payload))
    assert load(base=tmp_path, run_id="run-1", account="lx") == payload
    assert seal(tmp_path, pairs=pairs) == payload
    assert path.read_bytes() == raw


def test_old_pretty_snapshot_retry_preserves_exact_bytes(tmp_path, monkeypatch, owner):
    _, module, seal, load, filename, _ = owner
    with monkeypatch.context() as legacy:
        legacy.setattr(module, "_canonical_json_bytes", readable_json_bytes)
        payload = seal(tmp_path)
    path = tmp_path / "output_runs/run-1/accounts/lx/state" / filename
    before, original = path.read_bytes(), path.stat()
    assert before == readable_json_bytes(payload)
    assert seal(tmp_path) == payload
    assert load(base=tmp_path, run_id="run-1", account="lx") == payload
    assert path.read_bytes() == before
    assert (path.stat().st_ino, path.stat().st_mtime_ns) == (original.st_ino, original.st_mtime_ns)


@pytest.mark.parametrize("failure", ["changed_request", "tampered", "symlink"])
def test_retry_does_not_adopt_conflicting_or_unsafe_file(tmp_path, monkeypatch, owner, failure):
    _, module, seal, _, filename, error = owner
    with monkeypatch.context() as legacy:
        legacy.setattr(module, "_canonical_json_bytes", readable_json_bytes)
        payload = seal(tmp_path)
    path = tmp_path / "output_runs/run-1/accounts/lx/state" / filename
    if failure == "tampered":
        payload["strategy_policy_sha256"] = "f" * 64
        path.write_bytes(readable_json_bytes(payload))
    elif failure == "symlink":
        original = tmp_path / "original.json"
        path.rename(original)
        path.symlink_to(original)
    before = path.read_bytes()
    with pytest.raises(error):
        seal(tmp_path, sealed_at="2026-08-12T01:00:02Z") if failure == "changed_request" else seal(tmp_path)
    assert path.read_bytes() == before
    if failure == "symlink":
        assert path.is_symlink()


def test_compact_snapshot_manifest_and_independent_archive_restore(tmp_path, owner):
    name, _, seal, load, filename, _ = owner
    source = tmp_path / "source"
    source.mkdir()
    payload = seal(source)
    account_dir = source / "output_runs/run-1/accounts/lx"
    publish_strategy_scan_status(
        report_dir=account_dir, run_id="run-1", account="lx", market="US", symbol="NVDA",
        strategy_family="combo_yield", status="completed", candidate_count=1,
        snapshot_id="quote-1", receipt_relpath="quotes/quote-1/receipt.json",
    )
    publish_strategy_scan_status_index(
        report_dir=account_dir, run_id="run-1", account="lx", account_config_sha256="a" * 64,
        run_mode={"scan_mode": "standard", "executable": True},
        expected=[dict(market="US", symbol="NVDA", strategy_family="combo_yield",
                       strategy_mode="combo_yield", candidate_owner=name, account_config_sha256="a" * 64)],
    )
    manifest = publish_candidate_snapshot_manifest(
        base=source, run_id="run-1", account="lx", strategy_policy_sha256="b" * 64,
        sealed_at="2026-08-12T01:00:01Z",
    )
    bundle = load_candidate_snapshot_bundle(base=source, run_id="run-1", account="lx")
    assert bundle["manifest"] == manifest
    run = source / "output_runs/run-1"
    os.utime(run, (1, 1))
    for index in range(200):
        (source / "output_runs" / f"new-{index}").mkdir()
    before = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    budget = limits_file(tmp_path)
    batch, restored = tmp_path / "batch", tmp_path / "restored"
    copied = bounded_pull(
        repo_root=Path.cwd(), archive_root=batch, source_root=source, run_ids=["run-1"],
        include_logs=False, write=True, limits_file=budget, include_fixed_shared=False,
    )
    assert copied["ok"], copied
    result = restore_batch(archive_root=batch, target=restored, limits_file=budget)
    assert result["ok"], result
    assert result["source_deleted"] is False
    for relative, raw in before.items():
        assert (source / relative).read_bytes() == raw
        assert (restored / relative).read_bytes() == raw
    assert load(base=restored, run_id="run-1", account="lx") == payload
    assert load_candidate_snapshot_bundle(base=restored, run_id="run-1", account="lx") == bundle
    assert (restored / "output_runs/run-1/accounts/lx/state" / filename).read_bytes() == canonical_json_bytes_lines(payload)
