from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from test_candidate_snapshot_manifest import _seal_combo_bundle
from src.application.agent_tools import project_reader
from src.application.agent_tools.project_reader import ProjectReaderError
from src.application.agent_tools.project_runs import discover_runs, load_run_bundle, project_run_files
from src.application.runtime_paths import RuntimeRootResolution


def _kwargs(root: Path) -> dict:
    return {"runtime_root": RuntimeRootResolution(root, "argument"), "run_id": "run-1", "account": "lx",
            "market": "us", "authorized_accounts": ["lx", "sy"]}


def _hashes(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def test_same_bytes_formal_bundle_and_facade(tmp_path, monkeypatch):
    _seal_combo_bundle(tmp_path)
    before = _hashes(tmp_path)
    bundle = load_run_bundle(**_kwargs(tmp_path))
    assert bundle["scope"]["account"] == "lx"
    assert len(bundle["resources"]) == 2
    assert all("manifest" not in name for name in bundle["resources"])
    assert all(isinstance(raw, bytes) for raw in bundle["resources"].values())
    monkeypatch.setattr(project_reader, "resolve_secret", lambda *_: "fixture-key")
    scope = {"market": "us", "accounts": ["lx", "sy"], "config_revision": "abc"}
    kwargs = {"scope": scope, "run_id": "run-1", "account": "lx"}
    root = _kwargs(tmp_path)["runtime_root"]
    listing = project_run_files(root, **kwargs)
    target = next(row["relative_name"] for row in listing["entries"] if "combo" in row["relative_name"])
    page = project_run_files(root, **kwargs, action="read", relative_name=target, max_lines=1)
    assert page["freshness"]["status"] == "historical"
    assert page["next_cursor"]
    chunks = [page["text"]]
    for _ in range(200):
        if not page["next_cursor"]:
            break
        page = project_run_files(root, **kwargs, action="read", relative_name=target, max_lines=1, cursor=page["next_cursor"])
        chunks.append(page["text"])
    assert page["body_complete"]
    assert "".join(chunks) == project_reader.redacted_text(bundle["resources"][target])
    found = project_run_files(root, **kwargs, action="search", query="NVDA")
    assert found["entries"] and found["scanned"] == 2
    with pytest.raises(ProjectReaderError):
        project_run_files(root, **kwargs, action="read", relative_name="state/account_run_config.json")
    assert before == _hashes(tmp_path)


@pytest.mark.parametrize("change,code", [
    ({"account": "other"}, "permission_denied"), ({"market": "hk"}, "permission_denied"),
    ({"run_id": "../run-1"}, "invalid_run_id"),
    ({"cancelled": lambda: True}, "cancelled"),
    ({"deadline_monotonic": 0}, "time_deadline"),
])
def test_scope_and_execution_boundaries(tmp_path, change, code):
    _seal_combo_bundle(tmp_path)
    with pytest.raises(ProjectReaderError, match=code):
        load_run_bundle(**{**_kwargs(tmp_path), **change})


def test_root_and_bound_source_are_not_guessed(tmp_path):
    _seal_combo_bundle(tmp_path)
    with pytest.raises(ProjectReaderError, match="runtime_root_unavailable"):
        load_run_bundle(**{**_kwargs(tmp_path), "runtime_root": RuntimeRootResolution(tmp_path, "repo_default")})
    state = tmp_path / "output_runs/run-1/accounts/lx/state"
    target = next(state.glob("*combo*json"))
    original = target.read_bytes()
    target.write_bytes(original + b" ")
    with pytest.raises(ProjectReaderError, match="bundle_invalid"):
        load_run_bundle(**_kwargs(tmp_path))
    target.write_bytes(original)
    target.rename(state / "original")
    target.symlink_to(state / "original")
    with pytest.raises(ProjectReaderError, match="permission_denied"):
        load_run_bundle(**_kwargs(tmp_path))


def test_discovery_orders_entire_directory_and_continues(tmp_path):
    _seal_combo_bundle(tmp_path)
    runs = tmp_path / "output_runs"
    for i in range(401):
        (runs / f"a{i:03d}").mkdir()
    kwargs = {"runtime_root": RuntimeRootResolution(tmp_path, "argument"), "market": "us", "authorized_accounts": ["lx"]}
    first = discover_runs(**kwargs)
    assert first["entries"][0]["run_id"] == "run-1"
    assert first["next_state"]["index"] == 200
    second = discover_runs(**kwargs, cursor=first["next_state"])
    assert second["next_state"]["index"] == 400
    final = discover_runs(**kwargs, cursor=second["next_state"])
    assert final["next_state"] is None
    assert final["unverified_newer_count"] == 401
    (runs / "new").mkdir()
    with pytest.raises(ProjectReaderError, match="source_changed"):
        discover_runs(**kwargs, cursor=first["next_state"])


def test_account_cursor_cannot_cross_scope(tmp_path, monkeypatch):
    _seal_combo_bundle(tmp_path)
    monkeypatch.setattr(project_reader, "resolve_secret", lambda *_: "fixture-key")
    root = RuntimeRootResolution(tmp_path, "argument")
    scope = {"market": "us", "accounts": ["lx", "sy"], "config_revision": "abc"}
    listed = project_run_files(root, scope=scope, account="lx", run_id="run-1")
    name = listed["entries"][0]["relative_name"]
    page = project_run_files(root, scope=scope, account="lx", run_id="run-1", action="read", relative_name=name, max_lines=1)
    assert page["next_cursor"]
    with pytest.raises(ProjectReaderError):
        project_run_files(root, scope={**scope, "config_revision": "changed"}, account="lx", run_id="run-1",
                          action="read", relative_name=name, max_lines=1, cursor=page["next_cursor"])


def test_v3_wheel_source_status_is_bound_and_not_exposed(tmp_path):
    from test_candidate_snapshot_manifest import (
        CONFIG_HASH, POLICY_HASH, _account_dir, _dependencies, _publish_wheel_v4_statuses,
    )
    from src.application.candidate_snapshot_manifest import publish_candidate_snapshot_manifest_v3
    from src.application.wheel.candidate_snapshot import seal_wheel_candidate_snapshot

    account_dir = _account_dir(tmp_path)
    _publish_wheel_v4_statuses(account_dir)
    seal_wheel_candidate_snapshot(
        base=tmp_path, run_id="run-1", account="lx", market="us", account_config_sha256=CONFIG_HASH,
        strategy_policy_sha256=POLICY_HASH, dependencies=_dependencies(),
        scope_results=[{"symbol": "NVDA", "direction": direction, "status": "completed", "candidate_count": 0}
                       for direction in ("call", "put")], batches=[],
    )
    publish_candidate_snapshot_manifest_v3(base=tmp_path, run_id="run-1", account="lx",
                                           strategy_policy_sha256=POLICY_HASH, sealed_at="2026-08-12T01:00:01Z")
    bundle = load_run_bundle(**_kwargs(tmp_path))
    assert len(bundle["resources"]) == 2
    assert any("wheel_candidate_snapshot.v2" in name for name in bundle["resources"])
    source = account_dir / "nvda_wheel_put_scan_status.v2.json"
    source.write_bytes(source.read_bytes() + b" ")
    with pytest.raises(ProjectReaderError, match="bundle_invalid"):
        load_run_bundle(**_kwargs(tmp_path))


def test_same_run_accounts_have_separate_validated_resources(tmp_path):
    from test_candidate_snapshot_manifest import CONFIG_HASH, POLICY_HASH
    from src.application.strategy_scan_status import publish_strategy_scan_status_index_v2, publish_strategy_scan_status
    from src.application.combo_yield_candidate_snapshot import seal_combo_yield_candidate_snapshot
    from test_candidate_snapshot_manifest import _dependencies, _expected
    from src.application.candidate_snapshot_manifest import publish_candidate_snapshot_manifest

    _seal_combo_bundle(tmp_path)
    other = tmp_path / "output_runs/run-1/accounts/sy"
    (other / "state").mkdir(parents=True)
    publish_strategy_scan_status(report_dir=other, run_id="run-1", account="sy", market="US",
                                 symbol="NVDA", strategy_family="combo_yield", status="completed", candidate_count=0)
    publish_strategy_scan_status_index_v2(report_dir=other, run_id="run-1", account="sy",
                                         account_config_sha256=CONFIG_HASH, expected=_expected())
    seal_combo_yield_candidate_snapshot(base=tmp_path, run_id="run-1", account="sy", market="us",
                                        account_config_sha256=CONFIG_HASH, strategy_policy_sha256=POLICY_HASH,
                                        dependencies=_dependencies(), scan_statuses=[{
                                            "symbol": "NVDA", "strategy_mode": "combo_yield", "variant": "sp_lc", "status": "completed",
                                        }], ranked_pairs=[], sealed_at="2026-08-12T01:00:00Z")
    publish_candidate_snapshot_manifest(base=tmp_path, run_id="run-1", account="sy",
                                         strategy_policy_sha256=POLICY_HASH, sealed_at="2026-08-12T01:00:01Z")
    sy = load_run_bundle(**{**_kwargs(tmp_path), "account": "sy"})
    assert sy["scope"]["account"] == "sy" and len(sy["resources"]) == 2
    assert all(b'"account": "sy"' in raw for raw in sy["resources"].values())
    found = discover_runs(runtime_root=RuntimeRootResolution(tmp_path, "argument"), market="us",
                          authorized_accounts=["lx", "sy"])
    assert [row["account"] for row in found["entries"]] == ["lx", "sy"]
    assert found["unverified_newer_count"] == 0


def test_runtime_hardlink_fifo_and_oversize_rejected(tmp_path):
    import os

    _seal_combo_bundle(tmp_path)
    state = tmp_path / "output_runs/run-1/accounts/lx/state"
    target = next(state.glob("*combo*json"))
    raw = target.read_bytes()
    os.link(target, state / "duplicate")
    with pytest.raises(ProjectReaderError, match="permission_denied"):
        load_run_bundle(**_kwargs(tmp_path))
    (state / "duplicate").unlink()
    target.unlink()
    os.mkfifo(target)
    with pytest.raises(ProjectReaderError, match="unsupported"):
        load_run_bundle(**_kwargs(tmp_path))
    target.unlink()
    target.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(ProjectReaderError, match="file_too_large"):
        load_run_bundle(**_kwargs(tmp_path))
    target.write_bytes(raw)
    assert load_run_bundle(**_kwargs(tmp_path))["resources"]


def test_discovery_budget_exhaustion_retries_same_candidate(tmp_path, monkeypatch):
    from src.application.agent_tools import project_runs

    runs = tmp_path / "output_runs"
    runs.mkdir()
    for name in ("run-2", "run-1"):
        (runs / name).mkdir()
    calls = []

    def bounded_candidate(**kwargs):
        calls.append(kwargs["run_id"])
        budget = kwargs["_budget"]
        if budget["bytes"] < 1024:
            raise ProjectReaderError("file_too_large")
        budget["bytes"] = 10
        return {"manifest": {"run_id": kwargs["run_id"], "account": "lx"}}

    # Exercise discovery accounting independently; filesystem safety is tested above with real bytes.
    monkeypatch.setattr(project_runs, "load_run_bundle", bounded_candidate)
    kwargs = {"runtime_root": RuntimeRootResolution(tmp_path, "argument"), "market": "us", "authorized_accounts": ["lx"]}
    first = discover_runs(**kwargs)
    assert first["next_state"]["index"] == 1
    assert first["unverified_newer_count"] == 0
    second = discover_runs(**kwargs, cursor=first["next_state"])
    assert second["entries"][0]["run_id"] == "run-1" and second["next_state"] is None
    assert calls == ["run-2", "run-1", "run-1"]


def test_query_keeps_root_descriptor_after_path_replacement(tmp_path, monkeypatch):
    from src.application.agent_tools import project_runs

    root = tmp_path / "runtime"
    root.mkdir()
    _seal_combo_bundle(root)
    original = project_runs.read_bytes
    replaced = False

    def replace_after_first_read(descriptor, name, **kwargs):
        nonlocal replaced
        raw = original(descriptor, name, **kwargs)
        if not replaced:
            replaced = True
            root.rename(tmp_path / "old-runtime")
            root.mkdir()
        return raw

    monkeypatch.setattr(project_runs, "read_bytes", replace_after_first_read)
    result = project_run_files(RuntimeRootResolution(root, "argument"),
                               scope={"market": "us", "accounts": ["lx"], "config_revision": "a"},
                               run_id="run-1", account="lx")
    assert replaced and len(result["entries"]) == 2


def test_empty_manifest_cannot_claim_requested_market(tmp_path):
    from test_candidate_snapshot_manifest import CONFIG_HASH, POLICY_HASH
    from src.application.strategy_scan_status import publish_strategy_scan_status_index_v2
    from src.application.candidate_snapshot_manifest import publish_candidate_snapshot_manifest

    account_dir = tmp_path / "output_runs/run-1/accounts/lx"
    (account_dir / "state").mkdir(parents=True)
    publish_strategy_scan_status_index_v2(report_dir=account_dir, run_id="run-1", account="lx",
                                         account_config_sha256=CONFIG_HASH, expected=[])
    publish_candidate_snapshot_manifest(base=tmp_path, run_id="run-1", account="lx",
                                         strategy_policy_sha256=POLICY_HASH, sealed_at="2026-08-12T01:00:01Z")
    with pytest.raises(ProjectReaderError, match="market_unverifiable"):
        load_run_bundle(**_kwargs(tmp_path))


def test_dependency_bytes_verified_but_never_exposed(tmp_path):
    import json
    from domain.domain.decision_state_fingerprint import canonical_sha256

    _seal_combo_bundle(tmp_path)
    state = tmp_path / "output_runs/run-1/accounts/lx/state"
    target = next(state.glob("*combo*json"))
    dep = state / "fx-evidence.json"
    dep.write_text('{"private":"fixture-only"}')
    snapshot = json.loads(target.read_bytes())
    for row in snapshot["dependencies"]:
        if row["kind"] == "fx":
            row.update(relpath=str(dep.relative_to(tmp_path)), sha256=hashlib.sha256(dep.read_bytes()).hexdigest())
    snapshot["content_sha256"] = canonical_sha256({key: value for key, value in snapshot.items() if key != "content_sha256"})
    target.write_text(json.dumps(snapshot))
    manifest_path = next(state.glob("candidate_snapshot_manifest.*json"))
    manifest = json.loads(manifest_path.read_bytes())
    manifest["owner_snapshots"][0].update(sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
                                          content_sha256=snapshot["content_sha256"])
    manifest["content_sha256"] = canonical_sha256({key: value for key, value in manifest.items() if key != "content_sha256"})
    manifest_path.write_text(json.dumps(manifest))
    bundle = load_run_bundle(**_kwargs(tmp_path))
    assert all(b"fixture-only" not in raw for raw in bundle["resources"].values())
    dep.write_text('{"private":"changed"}')
    with pytest.raises(ProjectReaderError, match="bundle_invalid"):
        load_run_bundle(**_kwargs(tmp_path))


def test_legacy_loader_rejects_dangling_conflicting_status(tmp_path):
    from src.application.candidate_snapshot_manifest import (
        CandidateSnapshotManifestError, load_candidate_snapshot_bundle,
    )

    _seal_combo_bundle(tmp_path)
    account_dir = tmp_path / "output_runs/run-1/accounts/lx"
    (account_dir / "nvda_wheel_put_scan_status.v2.json").symlink_to(account_dir / "missing.json")
    with pytest.raises(CandidateSnapshotManifestError, match="artifact_version_mismatch"):
        load_candidate_snapshot_bundle(base=tmp_path, run_id="run-1", account="lx")
    with pytest.raises(ProjectReaderError, match="bundle_invalid"):
        load_run_bundle(**_kwargs(tmp_path))
