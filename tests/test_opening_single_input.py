"""Single-input persistence against frozen outputs of the 4.6.42 writer."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.opening_candidate_snapshot import (
    OpeningCandidateSnapshotError,
    load_opening_candidate_snapshot,
    ranked_opening_candidate_decisions,
    ranked_opening_candidates,
    rejected_opening_candidate_decisions,
    validate_opening_candidate_snapshot,
)
from tests.test_broker_candidate_contract import _seal as seal_broker
from tests.test_opening_candidate_snapshot import _dependencies, _seal


FIXTURES = Path(__file__).parent / "fixtures/opening_single_input"
LEGACY_SHA256 = {
    "v3": "5772847afb7be44d0091132f2a7af936bcac2390252d3a308b6685d7bc05513d",
    "v4": "11afdd01debdd85a21813da537dae362f94f9c2bb06dea88bf3844fdf1ba2b8a",
}


@pytest.mark.parametrize("version,seal", [("v3", _seal), ("v4", seal_broker)])
def test_single_input_writer_preserves_frozen_business_views(tmp_path, version, seal):
    legacy = json.loads((FIXTURES / f"{version}.json").read_bytes())
    payload = seal(tmp_path)
    assert payload["schema_version"] == "opening_candidate_snapshot.v5"
    assert all(d["schema_version"] == "opening_candidate_decision.v2" for d in payload["candidate_decisions"])
    assert all("normalized_input" not in d for d in payload["candidate_decisions"])
    assert all("facts" not in r for r in payload["ranked_candidates"])
    assert ranked_opening_candidates(payload) == ranked_opening_candidates(legacy)
    assert ranked_opening_candidate_decisions(payload) == ranked_opening_candidate_decisions(legacy)
    assert rejected_opening_candidate_decisions(payload) == rejected_opening_candidate_decisions(legacy)
    for field in ("scope_results", "strategy_results", "ranking_inputs", "opening_status", "dependencies"):
        assert payload[field] == legacy[field]
    for new, old in zip(payload["candidate_decisions"], legacy["candidate_decisions"], strict=True):
        assert new["opening_decision"] == old["opening_decision"]
        assert new["decision_hash"] == old["decision_hash"]
        assert new["normalized_input_hash"] == old["normalized_input_hash"]
    path = tmp_path / "output_runs" / payload["run_id"] / "accounts/lx/state/opening_candidate_snapshot.json"
    raw = path.read_bytes()
    assert len(raw) < len((FIXTURES / f"{version}.json").read_bytes())
    assert seal(tmp_path) == payload
    assert path.read_bytes() == raw


@pytest.mark.parametrize("version", ["v3", "v4"])
def test_frozen_legacy_reader_keeps_original_bytes(tmp_path, version):
    raw = (FIXTURES / f"{version}.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == LEGACY_SHA256[version]
    payload = json.loads(raw)
    _dependencies(tmp_path, payload["run_id"], "lx")
    path = tmp_path / "output_runs" / payload["run_id"] / "accounts/lx/state/opening_candidate_snapshot.json"
    path.write_bytes(raw)
    assert load_opening_candidate_snapshot(base=tmp_path, run_id=payload["run_id"], account="lx") == payload
    assert path.read_bytes() == raw


@pytest.mark.parametrize("mutation", [
    "missing_input", "outer_input", "ranked_facts", "old_decision_schema", "missing_reference",
    "duplicate_rank", "experience", "downgrade", "invalid_decision_type",
])
def test_new_structure_rejects_rehashed_damage(tmp_path, mutation):
    payload = _seal(tmp_path)
    first = payload["candidate_decisions"][0]
    if mutation == "missing_input":
        first["opening_decision"].pop("normalized_input")
    elif mutation == "invalid_decision_type":
        first["opening_decision"] = ["not a decision"]
    elif mutation == "outer_input":
        first["normalized_input"] = deepcopy(first["opening_decision"]["normalized_input"])
    elif mutation == "ranked_facts":
        payload["ranked_candidates"][0]["facts"] = {}
    elif mutation == "old_decision_schema":
        first["schema_version"] = "opening_candidate_decision.v1"
    elif mutation == "missing_reference":
        payload["ranked_candidates"][0]["candidate_id"] = "missing"
    elif mutation == "duplicate_rank":
        payload["ranked_candidates"][1]["rank"] = 1
    elif mutation == "experience":
        payload.update(scan_mode="experience", capacity_source="demo_scenario", account_display_name="demo", executable=False)
    else:
        payload["schema_version"] = "opening_candidate_snapshot.v3"
    payload["content_sha256"] = canonical_sha256({k: v for k, v in payload.items() if k != "content_sha256"})
    with pytest.raises(OpeningCandidateSnapshotError):
        validate_opening_candidate_snapshot(payload, expected_run_id="run-1", expected_account="lx")


def test_rank_projection_does_not_alias_single_persisted_input(tmp_path):
    payload = _seal(tmp_path)
    before = deepcopy(payload)
    ranked = ranked_opening_candidates(payload)
    ranked[0]["facts"]["earnings_events"].append({"synthetic": True})
    assert payload == before
    decisions = ranked_opening_candidate_decisions(payload)
    decisions[0]["normalized_input"]["earnings_events"].append({"synthetic": True})
    assert payload == before


def test_legacy_minimal_decision_status_still_comes_from_ranked_facts():
    legacy = {
        "schema_version": "opening_candidate_snapshot.v3",
        "candidate_decisions": [{"candidate_id": "legacy"}],
        "ranked_candidates": [{"candidate_id": "legacy", "rank": 1,
                               "facts": {"strategy_core": {"core_version": "old"}}}],
    }
    assert ranked_opening_candidate_decisions(legacy)[0]["strategy_core_status"] == "source"


@pytest.mark.parametrize("version,seal", [("v3", _seal), ("v4", seal_broker)])
def test_new_writer_never_reseals_an_existing_legacy_run(tmp_path, version, seal):
    raw = (FIXTURES / f"{version}.json").read_bytes()
    payload = json.loads(raw)
    _dependencies(tmp_path, payload["run_id"], "lx")
    path = tmp_path / "output_runs" / payload["run_id"] / "accounts/lx/state/opening_candidate_snapshot.json"
    path.write_bytes(raw)
    before = path.stat()
    with pytest.raises(OpeningCandidateSnapshotError):
        seal(tmp_path)
    assert path.read_bytes() == raw
    assert path.stat().st_ino == before.st_ino
    assert path.stat().st_mtime_ns == before.st_mtime_ns


def test_single_input_bundle_copy_and_independent_restore(tmp_path):
    import os
    from src.application.candidate_snapshot_manifest import load_candidate_snapshot_bundle
    from src.application.research.archive_bounded import bounded_pull, restore_batch
    from tests.candidate_evidence_helpers import seal_opening_candidate_fixture
    from tests.test_archive_bounded import limits_file
    from tests.test_opening_candidate_snapshot import _candidate

    source = tmp_path / "source"
    accepted = _candidate(contract_symbol="NVDA260918P00085000", period_return=.03)
    rejected = _candidate(contract_symbol="NVDA260918P00090000", period_return=.04)
    seal_opening_candidate_fixture(source, run_id="old", accepted_rows=[accepted], rejected_rows=[rejected])
    bundle = load_candidate_snapshot_bundle(base=source, run_id="old", account="lx")
    snapshot = load_opening_candidate_snapshot(base=source, run_id="old", account="lx")
    assert snapshot["schema_version"] == "opening_candidate_snapshot.v5"
    assert len(ranked_opening_candidates(snapshot)) == 1
    assert len(rejected_opening_candidate_decisions(snapshot)) == 1
    before = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    os.utime(source / "output_runs/old", (1, 1))
    for i in range(200):
        (source / "output_runs" / f"new-{i}").mkdir()
    budget = limits_file(tmp_path)
    batch, restored = tmp_path / "batch", tmp_path / "restored"
    copied = bounded_pull(repo_root=Path.cwd(), archive_root=batch, source_root=source,
                          run_ids=["old"], include_logs=False, write=True,
                          limits_file=budget, include_fixed_shared=False)
    assert copied["ok"], copied
    result = restore_batch(archive_root=batch, target=restored, limits_file=budget)
    assert result["ok"], result
    assert result["source_deleted"] is False
    assert load_candidate_snapshot_bundle(base=restored, run_id="old", account="lx") == bundle
    assert load_opening_candidate_snapshot(base=restored, run_id="old", account="lx") == snapshot
    for relative, raw in before.items():
        assert (source / relative).read_bytes() == raw
        assert (restored / relative).read_bytes() == raw
