from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from src.application.agent_tool_contracts import AgentToolError
from src.application.config_yaml import resolve_yaml_runtime_config
from src.application.ledger.api import read_wheel_activation_windows_read_only
import src.application.wheel.workflows as workflows
import src.application.config_authoring_transaction as publishing

REPO_ROOT = Path(__file__).resolve().parents[1]


def _deployment(tmp_path, monkeypatch):
    monkeypatch.delenv("OM_RUNTIME_ROOT", raising=False)
    source = tmp_path / "config.yaml"
    doc = {"accounts": {"lx": {"type": "futu", "futu_account_id": "12345678", "futu": {"host": "127.0.0.1", "port": 11111}},
                        "sy": {"type": "futu", "futu_account_id": "87654321", "futu": {"host": "127.0.0.2", "port": 11112}}},
           "markets": {"us": {"accounts": ["lx", "sy"], "symbols": ["NVDA"]},
                       "hk": {"accounts": ["lx", "sy"], "symbols": ["0700.HK"]}}}
    source.write_text(yaml.safe_dump(doc), encoding="utf-8")
    _build(tmp_path)
    return tmp_path


def _build(root):
    for market in ("us", "hk"):
        cfg, _ = resolve_yaml_runtime_config(repo_root=REPO_ROOT, market=market, config_path=root / "config.yaml")
        (root / f"config.{market}.json").write_text(json.dumps(cfg), encoding="utf-8")


def _call(root, *, action="enable", generation=0, request="enable-1", apply=False, sha=None, **kwargs):
    return workflows.change_wheel_activation(
        repo_root=REPO_ROOT, config_path=root / "config.us.json", market="us", account="lx",
        action=action, expected_current_generation=generation, request_id=request, actor="test",
        apply_changes=apply, expected_source_sha256=sha, **kwargs,
    )


def _apply(root, **kwargs):
    preview = _call(root, **kwargs)
    return _call(root, **kwargs, apply=True, sha=preview["expected_source_sha256"])


def test_enable_disable_reenable_and_response_lost_preserve_identity(tmp_path, monkeypatch):
    root = _deployment(tmp_path, monkeypatch)
    sibling = (root / "config.hk.json").read_bytes()
    before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    preview = _call(root)
    assert before == sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    assert preview["storage_status"] == "missing_database"
    enabled = _call(root, apply=True, sha=preview["expected_source_sha256"])
    assert enabled["ready"] and enabled["membership"] and enabled["write_applied"]
    replay = _call(root, apply=True, sha=preview["expected_source_sha256"])
    assert replay["status"] == "idempotent" and replay["write_applied"] is False
    assert replay["expected_config_descriptor"] == enabled["expected_config_descriptor"]
    assert replay["config_audit"] is None
    disabled = _apply(root, action="disable", generation=1, request="disable-1")
    assert disabled["reason_code"] == "closed_window" and not disabled["ready"]
    assert disabled["membership"]
    with pytest.raises(AgentToolError, match="superseded"):
        _call(root, apply=True, sha=preview["expected_source_sha256"])
    next_window = _apply(root, generation=1, request="enable-2")
    assert next_window["expected_config_descriptor"]["generation"] == 2
    with pytest.raises(AgentToolError, match="superseded"):
        _call(root, action="disable", generation=1, request="disable-1")
    assert (root / "config.hk.json").read_bytes() == sibling
    assert not (root / "resolved/config.assistant.json").exists()
    cfg = json.loads((root / "config.us.json").read_text())
    assert cfg["wheel"]["accounts"] == ["lx"]
    assert "sy" not in cfg["wheel"]["activation_by_account"]


def test_committed_window_publish_failure_retries_same_generation(tmp_path, monkeypatch):
    root = _deployment(tmp_path, monkeypatch)
    preview = _call(root)
    publisher = publishing.publish_yaml_config_generation_locked
    monkeypatch.setattr(publishing, "publish_yaml_config_generation_locked", lambda **kw: (_ for _ in ()).throw(OSError("publish fault")))
    with pytest.raises(AgentToolError) as failure:
        _call(root, apply=True, sha=preview["expected_source_sha256"])
    facts = failure.value.details
    assert facts["write_applied"] is True and facts["window_receipt"]["write_applied"] is True
    assert facts["failure_phase"] == "config_publish" and not facts["ready"]
    assert facts["latest_window"]["generation"] == 1 and not facts["membership"]
    status = _call(root, action="status")
    assert status["current_window"] == facts["latest_window"] and not status["ready"]
    # An unrelated comment invalidates the old source token but not the durable request identity.
    source = root / "config.yaml"
    source.write_text(source.read_text() + "\n# edited after failure\n")
    monkeypatch.setattr(publishing, "publish_yaml_config_generation_locked", publisher)
    with pytest.raises(AgentToolError) as stale:
        _call(root, apply=True, sha=preview["expected_source_sha256"])
    assert stale.value.code == "STALE_PREVIEW" and stale.value.details["write_applied"] is False
    completed = _apply(root)
    assert completed["ready"] and completed["window_receipt"]["write_applied"] is False
    assert completed["config_audit"]["write_applied"] is True
    assert completed["expected_config_descriptor"] == facts["expected_config_descriptor"]
    rows = read_wheel_activation_windows_read_only(completed["paths"]["sqlite_path"], market="us", account="lx")["windows"]
    assert len(rows) == 1


def test_drift_rejected_before_window_and_policy_drift_can_disable(tmp_path, monkeypatch):
    root = _deployment(tmp_path, monkeypatch)
    source = root / "config.yaml"
    doc = yaml.safe_load(source.read_text())
    doc["markets"]["us"]["symbols"].append("AMD")
    source.write_text(yaml.safe_dump(doc))
    with pytest.raises(AgentToolError) as drift:
        _call(root)
    assert drift.value.code == "CONFIG_DRIFT"
    assert not (root / "output_shared").exists()
    _build(root)
    original = _apply(root)
    doc = yaml.safe_load(source.read_text())
    doc["markets"]["us"]["features"]["wheel"]["call"] = {"min_dte": 31}
    source.write_text(yaml.safe_dump(doc))
    _build(root)
    assert _call(root, action="status")["ready"] is False
    disabled = _apply(root, action="disable", generation=1, request="disable-1")
    assert disabled["reason_code"] == "closed_window" and disabled["policy_drift"] is True
    assert disabled["expected_config_descriptor"]["policy_sha256"] == original["expected_config_descriptor"]["policy_sha256"]
    enabled = _apply(root, generation=1, request="enable-2")
    assert enabled["ready"] is True and not enabled.get("policy_drift", False)
    assert enabled.get("policy_drift") == enabled["readiness"].get("policy_drift")


def test_read_only_missing_source_and_deployment_conflicts(tmp_path, monkeypatch):
    root = _deployment(tmp_path, monkeypatch)
    source = root / "config.yaml"
    source.unlink()
    before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    state = _call(root, action="status")
    assert state["source_status"] == "unavailable" and state["storage_status"] == "missing_database"
    with pytest.raises(AgentToolError):
        _call(root)
    with pytest.raises(ValueError, match="different deployments"):
        _call(root, runtime_root=root / "wrong")
    assert before == sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def test_legacy_sibling_metadata_refresh_preserves_effective_values_and_modes(tmp_path, monkeypatch):
    root = _deployment(tmp_path, monkeypatch)
    sibling_path = root / "config.hk.json"
    sibling = json.loads(sibling_path.read_text())
    next(item for item in sibling["_generated"]["sources"] if item["role"] == "market_user").pop("effective")
    sibling_path.write_text(json.dumps(sibling))
    paths = [root / "config.yaml", root / "config.us.json", sibling_path]
    for path in paths:
        path.chmod(0o600)
    identities = {p: (p.stat().st_uid, p.stat().st_gid, p.stat().st_mode) for p in paths}
    completed = _apply(root)
    assert str(sibling_path) in completed["planned_changes"]["files"]
    after = json.loads(sibling_path.read_text())
    assert workflows._activation_effective_config(after) == workflows._activation_effective_config(sibling)
    assert identities == {p: (p.stat().st_uid, p.stat().st_gid, p.stat().st_mode) for p in paths}


def test_source_edit_during_prebuild_stops_before_window(tmp_path, monkeypatch):
    root = _deployment(tmp_path, monkeypatch)
    preview = _call(root)
    planner = workflows._activation_config_plan

    def plan_then_edit(**kwargs):
        planned = planner(**kwargs)
        source = root / "config.yaml"
        source.write_text(source.read_text() + "\n# concurrent edit\n")
        return planned

    monkeypatch.setattr(workflows, "_activation_config_plan", plan_then_edit)
    with pytest.raises(AgentToolError) as failure:
        _call(root, apply=True, sha=preview["expected_source_sha256"])
    assert failure.value.code == "STALE_PREVIEW"
    assert failure.value.details["window_receipt"] is None
    assert not Path(failure.value.details["paths"]["sqlite_path"]).exists()


def test_readback_failure_retry_does_not_repeat_either_write(tmp_path, monkeypatch):
    root = _deployment(tmp_path, monkeypatch)
    preview = _call(root)
    original = workflows._activation_status
    failed = False

    def fail_after_publish(cfg, **kwargs):
        nonlocal failed
        if cfg["wheel"].get("activation_by_account") and not failed:
            failed = True
            raise OSError("readback unavailable")
        return original(cfg, **kwargs)

    monkeypatch.setattr(workflows, "_activation_status", fail_after_publish)
    with pytest.raises(AgentToolError) as failure:
        _call(root, apply=True, sha=preview["expected_source_sha256"])
    assert failure.value.details["failure_phase"] == "readback"
    assert failure.value.details["write_applied"] is True
    replay = _call(root, apply=True, sha=preview["expected_source_sha256"])
    assert replay["ready"] and replay["write_applied"] is False and replay["config_audit"] is None


@pytest.mark.parametrize("changed", ["actor", "expected_current_generation"])
def test_request_identity_changes_are_rejected_without_rewriting(tmp_path, monkeypatch, changed):
    root = _deployment(tmp_path, monkeypatch)
    first = _apply(root)
    args = dict(repo_root=REPO_ROOT, config_path=root / "config.us.json", market="us", account="lx",
                action="enable", expected_current_generation=0, request_id="enable-1", actor="test")
    args[changed] = "another" if changed == "actor" else 1
    with pytest.raises(AgentToolError, match="identity conflict"):
        workflows.change_wheel_activation(**args)
    assert _call(root, action="status")["current_window"] == first["expected_config_descriptor"]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS inherited ACL integration")
def test_inherited_replacement_acl_rejected_before_window(tmp_path, monkeypatch):
    root = _deployment(tmp_path, monkeypatch)
    preview = _call(root)
    subprocess.run(["chmod", "+a", "everyone allow read,file_inherit", str(root)], check=True)
    try:
        with pytest.raises(AgentToolError, match="ACL") as failure:
            _call(root, apply=True, sha=preview["expected_source_sha256"])
        assert failure.value.details["failure_phase"] == "owner_preflight"
        assert failure.value.details["write_applied"] is False
        assert not Path(preview["paths"]["sqlite_path"]).exists()
    finally:
        subprocess.run(["chmod", "-N", str(root)], check=True)


def test_special_file_mode_rejected_before_window(tmp_path, monkeypatch):
    root = _deployment(tmp_path, monkeypatch)
    preview = _call(root)
    (root / "config.yaml").chmod(0o1600)
    with pytest.raises(AgentToolError, match="special config mode") as failure:
        _call(root, apply=True, sha=preview["expected_source_sha256"])
    assert failure.value.details["write_applied"] is False
    assert not Path(preview["paths"]["sqlite_path"]).exists()


def test_pending_journal_visible_without_recovery_in_status_and_failed_preview(tmp_path, monkeypatch):
    root = _deployment(tmp_path, monkeypatch)
    journal = root / "output_shared/state/config_authoring_transactions/pending/manifest.json"
    journal.parent.mkdir(parents=True)
    journal.write_text("{}")
    state = _call(root, action="status")
    assert state["pending_authoring_journal"] is True
    assert state["status"] == "unavailable"
    source = root / "config.yaml"
    source.write_text("invalid: [")
    with pytest.raises(AgentToolError) as failure:
        _call(root)
    assert failure.value.details["pending_authoring_journal"] is True
    assert journal.read_text() == "{}"
    assert not Path(state["paths"]["sqlite_path"]).exists()
