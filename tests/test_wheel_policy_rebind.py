from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from src.application.agent_tool_contracts import AgentToolError
from src.application.ledger.api import read_wheel_activation_windows_read_only
from src.application.wheel.policy_acceptance import (
    accept_policy_request_id,
    accept_wheel_policy,
)
from src.application.wheel.policy_binding import rebind_wheel_policy
from src.application.wheel.workflows import change_wheel_activation
from test_wheel_activation_operation import REPO_ROOT, _deployment, _build


def _activation(root, market, account, action="enable", generation=0, request="enable"):
    args = dict(repo_root=REPO_ROOT, market=market, account=account, action=action,
                config_path=root / f"config.{market}.json", runtime_root=root,
                expected_current_generation=generation, request_id=request, actor="test")
    preview = change_wheel_activation(**args)
    return change_wheel_activation(**args, apply_changes=True, expected_source_sha256=preview["expected_source_sha256"])


def _change(root, market="us", dte=90):
    source = root / "config.yaml"
    doc = yaml.safe_load(source.read_text())
    wheel = doc["markets"][market]["features"]["wheel"]
    for side in ("call", "put"):
        wheel[side] = {"min_dte": 7, "max_dte": dte}
    source.write_text(yaml.safe_dump(doc))
    _build(root)


def _rebind(root, market="us", account="lx", request="rebind", apply=False, preview=None, **kwargs):
    return rebind_wheel_policy(repo_root=REPO_ROOT, market=market, account=account,
                               config_path=root / f"config.{market}.json", runtime_root=root,
                               request_id=request, actor="test", apply_changes=apply,
                               expected_preview_hash=preview, **kwargs)


def _edit(root, market="us", *, policy=None, descriptor_account=None, descriptor=None):
    """Change the authoritative YAML without rebuilding, so the snapshot goes stale."""
    source = root / "config.yaml"
    doc = yaml.safe_load(source.read_text())
    wheel = doc["markets"][market]["features"]["wheel"]
    if policy is not None:
        for side in ("call", "put"):
            wheel[side] = dict(policy)
    if descriptor_account is not None:
        wheel["activation_by_account"][descriptor_account] = dict(descriptor)
    source.write_text(yaml.safe_dump(doc))


def _accept(root, market="us", *, account=None, apply=False, plan=None, **kwargs):
    return accept_wheel_policy(
        repo_root=REPO_ROOT, market=market, actor="test", account=account,
        config_path=root / f"config.{market}.json", runtime_root=root,
        expected_plan_hash=plan, apply_changes=apply, **kwargs,
    )


def _read(root, market="us", account="lx"):
    return read_wheel_activation_windows_read_only(root / "output_shared/state/option_positions.sqlite3", market, account)


def _files(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("market,account", [(m,a) for m in ("us","hk") for a in ("lx","sy")])
def test_rebind_keeps_window_and_config_and_replays_once(tmp_path, monkeypatch, market, account):
    root = _deployment(tmp_path, monkeypatch)
    _activation(root, market, account)
    original = _read(root, market, account)["windows"][0]
    _change(root, market)
    before = _files(root)
    preview = _rebind(root, market, account)
    assert not preview["ready"] and preview["status"] == "planned"
    assert _files(root) == before
    applied = _rebind(root, market, account, apply=True, preview=preview["preview_hash"])
    assert applied["ready"] and applied["write_applied"]
    after = _read(root, market, account)
    for key, value in original.items():
        if key not in {"effective_policy_hash", "policy_binding_revision"}:
            assert after["windows"][0][key] == value
    assert len(after["policy_bindings"]) == 1
    for path, digest in before.items():
        if not path.startswith("output_shared/"):
            assert _files(root)[path] == digest
    repeated = _rebind(root, market, account, apply=True, preview=preview["preview_hash"])
    assert repeated["status"] == "idempotent" and not repeated["write_applied"]
    assert repeated["receipt"] == applied["receipt"]
    assert not _read(root, market, "sy" if account == "lx" else "lx")["policy_bindings"]


def test_stale_config_and_pending_journal_have_no_binding_effect(tmp_path, monkeypatch):
    root = _deployment(tmp_path, monkeypatch)
    _activation(root, "us", "lx")
    _change(root)
    preview = _rebind(root)
    _change(root, dte=80)
    with pytest.raises(AgentToolError, match="stale"):
        _rebind(root, apply=True, preview=preview["preview_hash"])
    assert _read(root)["policy_bindings"] == []
    journal = root / "output_shared/state/config_authoring_transactions/pending/manifest.json"
    journal.parent.mkdir(parents=True)
    journal.write_text('{}')
    before = _files(root)
    with pytest.raises(AgentToolError, match="pending config"):
        _rebind(root)
    assert _files(root) == before


def test_aba_and_enable_replay_are_superseded_disable_still_works(tmp_path, monkeypatch):
    root = _deployment(tmp_path, monkeypatch)
    enabled = _activation(root, "us", "lx")
    original_source = (root / "config.yaml").read_bytes()
    _change(root)
    preview = _rebind(root)
    _rebind(root, apply=True, preview=preview["preview_hash"])
    with pytest.raises(AgentToolError, match="superseded"):
        change_wheel_activation(repo_root=REPO_ROOT, action="enable", market="us", account="lx",
                                config_path=root / "config.us.json", runtime_root=root,
                                expected_current_generation=0, request_id="enable", actor="test",
                                expected_source_sha256=enabled["expected_source_sha256"], apply_changes=True)
    (root / "config.yaml").write_bytes(original_source)
    _build(root)
    reverse = _rebind(root, request="reverse")
    _rebind(root, request="reverse", apply=True, preview=reverse["preview_hash"])
    assert _read(root)["windows"][0]["policy_binding_revision"] == 2
    with pytest.raises(AgentToolError):
        _rebind(root, apply=True, preview=preview["preview_hash"])
    assert _activation(root, "us", "lx", action="disable", generation=1, request="disable")["status"] == "applied"
    assert len(_read(root)["policy_bindings"]) == 2
    assert _activation(root, "us", "lx", generation=1, request="enable-new")["ready"]


def test_cli_requires_preview_and_confirms_real_binding(tmp_path, monkeypatch):
    root = _deployment(tmp_path, monkeypatch)
    _activation(root, "us", "lx")
    _change(root)
    cmd = [sys.executable, "-m", "src.interfaces.cli.wheel", "activation", "rebind-policy", "--market", "us",
           "--account", "lx", "--config", str(root / "config.us.json"), "--runtime-root", str(root),
           "--request-id", "cli-rebind", "--actor", "test", "--format", "json"]
    preview = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    assert preview.returncode == 0, preview.stdout + preview.stderr
    body = json.loads(preview.stdout)
    # CLI uses the shared success envelope.
    body = body.get("data", body)
    applied = subprocess.run(cmd + ["--apply", "--confirm", "--expected-preview-hash", body["preview_hash"]],
                             cwd=REPO_ROOT, capture_output=True, text=True)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert _read(root)["windows"][0]["policy_binding_revision"] == 1


def test_committed_readback_failure_retries_original_receipt(tmp_path, monkeypatch):
    from src.application.wheel import policy_binding as operation

    root = _deployment(tmp_path, monkeypatch)
    _activation(root, "us", "lx")
    _change(root)
    preview = _rebind(root)
    original_read = operation.read_wheel_activation_windows_read_only

    def fail_after_commit(*args, **kwargs):
        result = original_read(*args, **kwargs)
        if result.get("policy_bindings"):
            raise OSError("readback interrupted")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(operation, "read_wheel_activation_windows_read_only", fail_after_commit)
        with pytest.raises(AgentToolError, match="readback interrupted") as error:
            _rebind(root, apply=True, preview=preview["preview_hash"])
    assert error.value.details["failure_phase"] == "readback"
    assert error.value.details["write_applied"] is True
    receipt = error.value.details["receipt"]
    replay = _rebind(root, apply=True, preview=preview["preview_hash"])
    assert replay["status"] == "idempotent" and replay["receipt"] == receipt
    assert len(_read(root)["policy_bindings"]) == 1


def test_identical_runtime_replacement_invalidates_preview(tmp_path, monkeypatch):
    root = _deployment(tmp_path, monkeypatch)
    _activation(root, "us", "lx")
    _change(root)
    preview = _rebind(root)
    runtime = root / "config.us.json"
    replacement = root / "replacement.json"
    replacement.write_bytes(runtime.read_bytes())
    replacement.replace(runtime)
    with pytest.raises(AgentToolError, match="stale"):
        _rebind(root, apply=True, preview=preview["preview_hash"])
    assert _read(root)["policy_bindings"] == []


def test_preview_missing_ledger_does_not_initialize(tmp_path, monkeypatch):
    root = _deployment(tmp_path, monkeypatch)
    before = _files(root)
    with pytest.raises(AgentToolError, match="missing_database"):
        _rebind(root)
    assert _files(root) == before


def test_same_request_committed_while_waiting_for_lock_returns_receipt(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from src.application.wheel import policy_binding as operation

    root = _deployment(tmp_path, monkeypatch)
    _activation(root, "us", "lx")
    _change(root)
    preview = _rebind(root)
    original_lock = operation.locked_config_authoring
    winner = {}

    @contextmanager
    def commit_before_lock(**kwargs):
        with monkeypatch.context() as patch:
            patch.setattr(operation, "locked_config_authoring", original_lock)
            winner.update(_rebind(root, apply=True, preview=preview["preview_hash"]))
        with original_lock(**kwargs) as lock:
            yield lock

    monkeypatch.setattr(operation, "locked_config_authoring", commit_before_lock)
    result = _rebind(root, apply=True, preview=preview["preview_hash"])
    assert result["status"] == "idempotent" and not result["write_applied"]
    assert result["receipt"] == winner["receipt"]
    assert len(_read(root)["policy_bindings"]) == 1


def test_closed_replay_reports_superseded_with_original_receipt(tmp_path, monkeypatch):
    root = _deployment(tmp_path, monkeypatch)
    _activation(root, "us", "lx")
    _change(root)
    preview = _rebind(root)
    applied = _rebind(root, apply=True, preview=preview["preview_hash"])
    _activation(root, "us", "lx", action="disable", generation=1, request="disable")
    with pytest.raises(AgentToolError, match="superseded") as error:
        _rebind(root, apply=True, preview=preview["preview_hash"])
    assert error.value.details["receipt"] == applied["receipt"]
    assert error.value.details["write_applied"] is False


def test_journal_arriving_before_lock_is_not_recovered(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from src.application.wheel import policy_binding as operation

    root = _deployment(tmp_path, monkeypatch)
    _activation(root, "us", "lx")
    _change(root)
    preview = _rebind(root)
    original_lock = operation.locked_config_authoring
    journal = root / "output_shared/state/config_authoring_transactions/pending/manifest.json"
    before_config = (root / "config.yaml").read_bytes()

    @contextmanager
    def add_journal_before_lock(**kwargs):
        journal.parent.mkdir(parents=True)
        journal.write_text('{}')
        with original_lock(**kwargs) as lock:
            yield lock

    monkeypatch.setattr(operation, "locked_config_authoring", add_journal_before_lock)
    with pytest.raises(AgentToolError):
        _rebind(root, apply=True, preview=preview["preview_hash"])
    assert journal.read_text() == '{}'
    assert (root / "config.yaml").read_bytes() == before_config
    assert _read(root)["policy_bindings"] == []


@pytest.mark.parametrize("market,account", [(m, a) for m in ("us", "hk") for a in ("lx", "sy")])
def test_accept_policy_is_one_command_for_a_stale_snapshot(tmp_path, monkeypatch, market, account):
    root = _deployment(tmp_path, monkeypatch)
    _activation(root, market, account)
    original = _read(root, market, account)["windows"][0]
    _edit(root, market, policy={"min_dte": 7, "max_dte": 90})
    before = _files(root)

    plan = _accept(root, market, account=account)
    assert plan["status"] == "planned" and plan["dry_run"]
    assert plan["write_applied"] is False
    assert plan["plan"]["snapshot_stale"] is True
    assert [item["classification"] for item in plan["plan"]["accounts"]] == ["accept"]
    assert _files(root) == before

    applied = _accept(root, market, account=account, apply=True, plan=plan["plan_hash"])
    assert applied["status"] == "applied" and applied["write_applied"] is True
    assert applied["snapshot"]["written"] is True
    assert applied["readiness_after"]["monitoring_gate"] == "enabled"
    assert [item["status"] for item in applied["accounts"]] == ["applied"]
    after = _read(root, market, account)
    for key, value in original.items():
        if key not in {"effective_policy_hash", "policy_binding_revision"}:
            assert after["windows"][0][key] == value
    assert len(after["policy_bindings"]) == 1


def test_accept_policy_repeats_without_another_binding(tmp_path, monkeypatch):
    root = _deployment(tmp_path, monkeypatch)
    _activation(root, "us", "lx")
    _edit(root, "us", policy={"min_dte": 7, "max_dte": 90})
    applied = _accept(root, "us", apply=True, plan=_accept(root, "us")["plan_hash"])
    assert applied["status"] == "applied"
    settled = _files(root)

    repeat = _accept(root, "us")
    assert repeat["status"] == "no_drift" and repeat["write_applied"] is False
    assert [item["classification"] for item in repeat["plan"]["accounts"]] == ["already_current"]
    assert _files(root) == settled
    assert len(_read(root)["policy_bindings"]) == 1


def test_accept_policy_leaves_one_binding_after_a_committed_readback_failure(tmp_path, monkeypatch):
    from src.application.wheel import policy_binding as operation

    root = _deployment(tmp_path, monkeypatch)
    _activation(root, "us", "lx")
    _edit(root, "us", policy={"min_dte": 7, "max_dte": 90})
    plan = _accept(root, "us")
    original_read = operation.read_wheel_activation_windows_read_only

    def fail_after_commit(*args, **kwargs):
        result = original_read(*args, **kwargs)
        if result.get("policy_bindings"):
            raise OSError("readback interrupted")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(operation, "read_wheel_activation_windows_read_only", fail_after_commit)
        interrupted = _accept(root, "us", apply=True, plan=plan["plan_hash"])
    assert interrupted["status"] == "partial"
    assert interrupted["write_applied"] is True
    assert interrupted["accounts"][0]["status"] == "failed"
    assert "readback interrupted" in interrupted["accounts"][0]["error"]["message"]
    assert len(_read(root)["policy_bindings"]) == 1

    settled = _accept(root, "us")
    assert settled["status"] == "no_drift"
    assert len(_read(root)["policy_bindings"]) == 1


def test_accept_policy_reports_a_boundary_refusal_as_partial(tmp_path, monkeypatch):
    root = _deployment(tmp_path, monkeypatch)
    _activation(root, "us", "lx")
    _activation(root, "us", "sy", request="enable-sy")
    _edit(root, "us", policy={"min_dte": 7, "max_dte": 90},
          descriptor_account="sy",
          descriptor={"generation": 1, "activated_at_ms": 999, "deactivated_at_ms": None})

    plan = _accept(root, "us")
    by_account = {item["account"]: item for item in plan["plan"]["accounts"]}
    assert plan["status"] == "planned"
    assert by_account["lx"]["classification"] == "accept"
    assert by_account["sy"]["classification"] == "refused_boundary"
    assert "request_id" not in by_account["sy"]

    applied = _accept(root, "us", apply=True, plan=plan["plan_hash"])
    assert applied["status"] == "partial"
    assert len(_read(root, "us", "lx")["policy_bindings"]) == 1
    assert _read(root, "us", "sy")["policy_bindings"] == []
    assert applied["readiness_after"]["monitoring_gate"] == "config_mismatch"


def test_accept_policy_refuses_before_writing_on_a_stale_plan_and_a_pending_journal(tmp_path, monkeypatch):
    root = _deployment(tmp_path, monkeypatch)
    _activation(root, "us", "lx")
    _edit(root, "us", policy={"min_dte": 7, "max_dte": 90})
    plan = _accept(root, "us")
    _edit(root, "us", policy={"min_dte": 7, "max_dte": 80})
    before = _files(root)
    with pytest.raises(AgentToolError, match="stale"):
        _accept(root, "us", apply=True, plan=plan["plan_hash"])
    assert _files(root) == before

    journal = root / "output_shared/state/config_authoring_transactions/pending/manifest.json"
    journal.parent.mkdir(parents=True)
    journal.write_text("{}")
    before_journal = _files(root)
    with pytest.raises(AgentToolError, match="pending config"):
        _accept(root, "us", apply=True)
    assert _files(root) == before_journal
    journal.unlink()
    assert _read(root)["policy_bindings"] == []


def test_accept_policy_account_filter_and_concurrent_runs_append_once(tmp_path, monkeypatch):
    import threading

    root = _deployment(tmp_path, monkeypatch)
    _activation(root, "us", "lx")
    _activation(root, "us", "sy", request="enable-sy")
    # Rebuild up front so the concurrent runs contend on the binding, not on the snapshot.
    _change(root, "us")

    filtered = _accept(root, "us", account="lx")
    assert [item["account"] for item in filtered["plan"]["accounts"]] == ["lx"]
    assert filtered["plan"]["skipped"] == []
    with pytest.raises(AgentToolError, match="nothing to accept"):
        _accept(root, "us", account="nobody")

    failures: list[Exception] = []

    def run() -> None:
        try:
            _accept(root, "us", apply=True)
        except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
            failures.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    for account in ("lx", "sy"):
        assert len(_read(root, "us", account)["policy_bindings"]) == 1


def test_accept_policy_request_id_is_stable_and_discriminating():
    base = {"market": "us", "account": "lx", "generation": 1, "revision": 0,
            "actor": "test", "target_policy_hash": "a" * 64}
    request_id = accept_policy_request_id(**base)
    assert request_id == accept_policy_request_id(**base)
    assert request_id == request_id.strip() and " " not in request_id
    variants = [
        {**base, "market": "hk"}, {**base, "account": "sy"}, {**base, "generation": 2},
        {**base, "revision": 1}, {**base, "actor": "other"},
        {**base, "target_policy_hash": "b" * 64},
    ]
    assert len({accept_policy_request_id(**item) for item in variants} | {request_id}) == 1 + len(variants)
