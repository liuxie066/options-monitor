from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


class _FakeRunlog:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def safe_event(self, step: str, status: str, **kwargs) -> None:
        event = {"step": step, "status": status}
        event.update(kwargs)
        self.events.append(event)


def _make_request(
    tmp_path: Path,
    *,
    prefetch_done: bool = False,
    force_mode: bool = False,
    allow_mutations: bool = True,
    allow_notifications: bool = True,
    close_advice_enabled: bool = False,
) -> Any:
    from src.application.account_run import (
        AccountRunRequest,
        build_account_runtime_config,
    )
    from src.application.tick_run_workspace import publish_account_run_config

    base = tmp_path / "repo"
    base.mkdir()
    cfg_path = base / "config.us.json"
    cfg_path.write_text("{}", encoding="utf-8")
    run_dir = base / "output_runs" / "run-1"
    run_dir.mkdir(parents=True)
    shared_required = base / "output_shared" / "required_data"
    shared_required.mkdir(parents=True)
    accounts_root = run_dir / "accounts"
    accounts_root.mkdir(parents=True)
    base_cfg = {
        "symbols": [{"symbol": "NVDA", "market": "US"}],
        "portfolio": {},
        "close_advice": {"enabled": close_advice_enabled},
    }
    account_config_authority = publish_account_run_config(
        base=base,
        run_id="run-1",
        account="lx",
        config=build_account_runtime_config(
            base_cfg=base_cfg,
            cfg_path=cfg_path,
            account="lx",
            markets_to_run=["US"],
        ),
    )
    return AccountRunRequest(
        acct="lx",
        base=base,
        account_config_authority=account_config_authority,
        vpy=base / ".venv/bin/python",
        markets_to_run=["US"],
        scheduler_ms=12,
        scheduler_view={"schema_kind": "scheduler_decision"},
        notify_decision_by_account={},
        should_run_global=True,
        reason_global="scheduled",
        run_id="run-1",
        run_dir=run_dir,
        shared_required=shared_required,
        accounts_root=accounts_root,
        prefetch_done=prefetch_done,
        force_mode=force_mode,
        allow_mutations=allow_mutations,
        allow_notifications=allow_notifications,
    )


def _request_for_account(request: Any, account: str, **changes: Any) -> Any:
    from src.application.tick_run_workspace import publish_account_run_config

    config = json.loads(
        request.account_config_authority.canonical_bytes.decode("utf-8")
    )
    config["portfolio"]["account"] = account
    authority = publish_account_run_config(
        base=request.base,
        run_id=request.run_id,
        account=account,
        config=config,
    )
    return replace(
        request,
        acct=account,
        account_config_authority=authority,
        **changes,
    )


def _install_common_patches(monkeypatch, request: Any) -> dict[str, Any]:
    from src.application import account_run as mod

    audit_events: list[dict[str, Any]] = []
    state_writes: list[tuple[str, dict[str, Any]]] = []
    event_prefetch_calls: list[dict[str, Any]] = []

    acct_report_dir = request.accounts_root / request.acct / "reports"
    acct_state_dir = request.accounts_root / request.acct / "state"
    shared_state_dir = request.run_dir / "state"

    def _audit(event_type: str, action: str, **kwargs) -> None:
        payload = {"event_type": event_type, "action": action}
        payload.update(kwargs)
        audit_events.append(payload)

    monkeypatch.setattr(mod, "ensure_account_output_dir", lambda path: path.mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: list(cfg.get("symbols") or []))
    monkeypatch.setattr(mod, "set_watchlist_config", lambda cfg, syms: cfg.__setitem__("symbols", list(syms)))
    monkeypatch.setattr(mod, "utc_now", lambda: "2026-04-25T00:00:00Z")
    monkeypatch.setattr(mod, "decide_should_notify", lambda **kwargs: True)

    monkeypatch.setattr(mod.run_repo, "get_run_account_dir", lambda *args: acct_report_dir)
    monkeypatch.setattr(mod.run_repo, "get_run_account_state_dir", lambda *args: acct_state_dir)
    monkeypatch.setattr(mod.run_repo, "ensure_run_account_state_dir", lambda *args: acct_state_dir.mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(mod.run_repo, "get_run_state_dir", lambda *args: shared_state_dir)
    monkeypatch.setattr(mod.run_repo, "write_run_account_text", lambda *args: None)
    monkeypatch.setattr(mod.state_repo, "write_account_run_state", lambda base, run_id, acct, name, payload: state_writes.append((name, dict(payload))))
    monkeypatch.setattr(mod.state_repo, "write_shared_state", lambda base, name, payload: state_writes.append((name, dict(payload))))
    monkeypatch.setattr(mod.state_repo, "append_run_audit_jsonl", lambda *args, **kwargs: None)

    def _prefetch_event_data(**kwargs):
        event_prefetch_calls.append(dict(kwargs))
        return {
            "summary": {
                "errors": 0,
                "unique_symbols_total": 1,
                "fetch_attempts": 0,
            },
            "symbols": {},
        }

    monkeypatch.setattr(mod, "prefetch_event_data", _prefetch_event_data)

    return {
        "mod": mod,
        "audit_fn": _audit,
        "audit_events": audit_events,
        "event_prefetch_calls": event_prefetch_calls,
        "state_writes": state_writes,
        "acct_report_dir": acct_report_dir,
        "acct_state_dir": acct_state_dir,
    }


def test_build_account_runtime_config_is_pure_and_applies_shared_filters(
    tmp_path: Path,
) -> None:
    from src.application.account_run import build_account_runtime_config

    base_cfg = {
        "portfolio": {"broker": "富途"},
        "symbols": [
            {"symbol": "NVDA", "broker": "US"},
            {"symbol": "0700.HK", "broker": "HK"},
        ],
    }
    original = deepcopy(base_cfg)
    cfg = build_account_runtime_config(
        base_cfg=base_cfg,
        cfg_path=tmp_path / "config.us.json",
        account="LX",
        markets_to_run=["US"],
        symbols_arg="NVDA",
    )

    assert base_cfg == original
    assert cfg["portfolio"]["account"] == "lx"
    assert cfg["symbols"] == [{"symbol": "NVDA", "broker": "US"}]
    assert cfg["config_source_path"] == str(
        (tmp_path / "config.us.json").resolve()
    )


def test_run_one_account_rejects_tampered_prepublished_config_before_children(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import account_run as mod
    from src.application.account_run import run_one_account
    from src.application.tick_run_workspace import AccountRunConfigError

    request = _make_request(
        tmp_path,
        prefetch_done=True,
        close_advice_enabled=True,
    )
    request.account_config_authority.state_path.write_text(
        "{}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        mod,
        "ensure_account_output_dir",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("account child workspace must not be touched")
        ),
    )
    monkeypatch.setattr(
        mod,
        "run_pipeline_script",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("pipeline must not start")
        ),
    )
    monkeypatch.setattr(
        mod,
        "run_close_advice",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("Close Advice must not start")
        ),
    )

    with pytest.raises(AccountRunConfigError) as raised:
        run_one_account(
            request=request,
            runlog=_FakeRunlog(),
            audit_fn=lambda *_args, **_kwargs: None,
            fail_schema_validation=lambda **_kwargs: None,
        )

    assert raised.value.code == "ACCOUNT_CONFIG_ARTIFACT_MISMATCH"


def test_run_one_account_skips_pipeline_when_scan_gate_blocks(monkeypatch, tmp_path: Path) -> None:
    from src.application.account_run import run_one_account

    request = _make_request(tmp_path)
    env = _install_common_patches(monkeypatch, request)
    runlog = _FakeRunlog()

    monkeypatch.setattr(
        env["mod"],
        "decide_account_scan_gate",
        lambda **kwargs: {
            "run_pipeline": False,
            "ran_scan": False,
            "meaningful": False,
            "result_reason": "scheduler_skip",
        },
    )
    monkeypatch.setattr(env["mod"], "run_pipeline_script", lambda **kwargs: (_ for _ in ()).throw(AssertionError("pipeline should not run")))

    outcome = run_one_account(
        request=request,
        runlog=runlog,
        audit_fn=env["audit_fn"],
        fail_schema_validation=lambda **kwargs: (_ for _ in ()).throw(AssertionError("schema validation should not fail")),
    )

    assert outcome.ran_pipeline is False
    assert outcome.prefetch_done is False
    assert outcome.result.account == "lx"
    assert outcome.result.decision_reason == "scheduler_skip"
    assert outcome.result.notification_text == ""
    assert outcome.acct_metrics["reason"] == "scheduler_skip"
    assert not any(name == "expired_position_maintenance.json" for name, _ in env["state_writes"])
    assert any(name == "account_metrics.json" for name, _ in env["state_writes"])


def test_run_one_account_consumes_barrier_snapshot_and_runs_pipeline_successfully(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application.account_run import run_one_account

    request = _make_request(tmp_path, prefetch_done=True)
    env = _install_common_patches(monkeypatch, request)
    runlog = _FakeRunlog()

    monkeypatch.setattr(
        env["mod"],
        "decide_account_scan_gate",
        lambda **kwargs: {
            "run_pipeline": True,
            "ran_scan": True,
            "meaningful": True,
            "result_reason": "run",
        },
    )
    def _run_pipeline_script(**kwargs):
        report_dir = kwargs["report_dir"]
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "symbols_notification.txt").write_text("hello world\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(env["mod"], "run_pipeline_script", _run_pipeline_script)
    monkeypatch.setattr(env["mod"], "normalize_pipeline_subprocess_output", lambda **kwargs: {"returncode": kwargs["returncode"], "adapter": "pipeline"})
    monkeypatch.setattr(env["mod"], "decide_pipeline_execution_result", lambda **kwargs: {"ok": True, "ran_scan": True, "meaningful": True, "reason": "ok"})

    outcome = run_one_account(
        request=request,
        runlog=runlog,
        audit_fn=env["audit_fn"],
        fail_schema_validation=lambda **kwargs: (_ for _ in ()).throw(AssertionError("schema validation should not fail")),
    )

    assert outcome.ran_pipeline is True
    assert outcome.prefetch_done is True
    assert outcome.result.notification_text == "hello world"
    assert outcome.acct_metrics["ran_scan"] is True
    assert any(evt["action"] == "event_prefetch" for evt in env["audit_events"])
    assert any(
        evt["step"] == "event_prefetch" and evt["status"] == "start"
        for evt in runlog.events
    )
    assert not any(evt["step"] == "fetch_chain_cache" for evt in runlog.events)
    assert any(evt["step"] == "snapshot_batches" and evt["status"] == "ok" for evt in runlog.events)


def test_frozen_account_run_keeps_parent_generation_after_late_path_drift(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application.account_run import run_one_account
    from src.application.tick_run_workspace import canonical_account_run_config_bytes

    request = replace(
        _make_request(tmp_path, prefetch_done=True),
        account_config_generation_frozen=True,
    )
    env = _install_common_patches(monkeypatch, request)
    runlog = _FakeRunlog()
    monkeypatch.setattr(
        env["mod"],
        "decide_account_scan_gate",
        lambda **_kwargs: {
            "run_pipeline": True,
            "ran_scan": True,
            "meaningful": True,
            "result_reason": "run",
        },
    )

    def _drift_during_event_prefetch(**_kwargs):
        replacement = json.loads(
            request.account_config_authority.canonical_bytes.decode("utf-8")
        )
        replacement.setdefault("runtime", {})["generation"] = "late-replacement"
        replacement_bytes = canonical_account_run_config_bytes(replacement)
        request.account_config_authority.state_path.write_bytes(replacement_bytes)
        request.account_config_authority.compatibility_path.write_bytes(
            replacement_bytes
        )
        return {
            "summary": {"errors": 0, "unique_symbols_total": 1},
            "symbols": {},
        }

    observed_pipeline: dict[str, Any] = {}

    def _run_pipeline_script(**kwargs):
        observed_pipeline.update(kwargs)
        kwargs["report_dir"].mkdir(parents=True, exist_ok=True)
        (kwargs["report_dir"] / "symbols_notification.txt").write_text(
            "retained generation\n",
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(env["mod"], "prefetch_event_data", _drift_during_event_prefetch)
    monkeypatch.setattr(env["mod"], "run_pipeline_script", _run_pipeline_script)
    monkeypatch.setattr(
        env["mod"],
        "normalize_pipeline_subprocess_output",
        lambda **kwargs: {"returncode": kwargs["returncode"], "adapter": "pipeline"},
    )
    monkeypatch.setattr(
        env["mod"],
        "decide_pipeline_execution_result",
        lambda **_kwargs: {
            "ok": True,
            "ran_scan": True,
            "meaningful": True,
            "reason": "ok",
        },
    )

    outcome = run_one_account(
        request=request,
        runlog=runlog,
        audit_fn=env["audit_fn"],
        fail_schema_validation=lambda **_kwargs: None,
    )

    assert outcome.ran_pipeline is True
    assert outcome.result.ran_scan is True
    assert observed_pipeline["account_config_canonical_bytes"] == (
        request.account_config_authority.canonical_bytes
    )
    assert observed_pipeline["account_config_sha256"] == (
        request.account_config_authority.account_config_sha256
    )


def test_pipeline_child_account_config_failure_preserves_typed_reason(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application.account_run import run_one_account

    request = replace(
        _make_request(tmp_path, prefetch_done=True),
        account_config_generation_frozen=True,
    )
    env = _install_common_patches(monkeypatch, request)
    monkeypatch.setattr(
        env["mod"],
        "decide_account_scan_gate",
        lambda **_kwargs: {
            "run_pipeline": True,
            "ran_scan": True,
            "meaningful": True,
            "result_reason": "run",
        },
    )
    monkeypatch.setattr(
        env["mod"],
        "run_pipeline_script",
        lambda **_kwargs: SimpleNamespace(
            returncode=2,
            stdout="",
            stderr=(
                "[CONFIG_ERROR] ACCOUNT_CONFIG_HASH_MISMATCH: "
                "retained account config hash mismatch"
            ),
        ),
    )
    monkeypatch.setattr(
        env["mod"],
        "normalize_pipeline_subprocess_output",
        lambda **kwargs: {"returncode": kwargs["returncode"], "adapter": "pipeline"},
    )
    monkeypatch.setattr(
        env["mod"],
        "decide_pipeline_execution_result",
        lambda **_kwargs: {
            "ok": False,
            "ran_scan": True,
            "meaningful": False,
            "reason": "pipeline failed",
        },
    )

    outcome = run_one_account(
        request=request,
        runlog=_FakeRunlog(),
        audit_fn=env["audit_fn"],
        fail_schema_validation=lambda **_kwargs: None,
    )

    assert outcome.ran_pipeline is False
    assert outcome.result.ran_scan is False
    assert outcome.result.should_notify is False
    assert outcome.result.decision_reason == "account_config_hash_mismatch"
    assert outcome.acct_metrics["error_code"] == "ACCOUNT_CONFIG_HASH_MISMATCH"
    assert outcome.acct_metrics["ran_scan"] is False


def test_run_one_account_uses_runtime_root_for_state_and_repo_root_for_process(monkeypatch, tmp_path: Path) -> None:
    from src.application.account_run import run_one_account

    repo_root = tmp_path / "code"
    repo_root.mkdir()
    request = replace(
        _make_request(tmp_path, prefetch_done=True),
        repo_root=repo_root,
    )
    env = _install_common_patches(monkeypatch, request)
    runlog = _FakeRunlog()

    monkeypatch.setattr(
        env["mod"],
        "decide_account_scan_gate",
        lambda **kwargs: {
            "run_pipeline": True,
            "ran_scan": True,
            "meaningful": True,
            "result_reason": "run",
        },
    )
    seen_pipeline: dict[str, Any] = {}

    def _run_pipeline_script(**kwargs):
        seen_pipeline.update(kwargs)
        report_dir = kwargs["report_dir"]
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "symbols_notification.txt").write_text("process split\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(env["mod"], "run_pipeline_script", _run_pipeline_script)
    monkeypatch.setattr(env["mod"], "normalize_pipeline_subprocess_output", lambda **kwargs: {"returncode": kwargs["returncode"], "adapter": "pipeline"})
    monkeypatch.setattr(env["mod"], "decide_pipeline_execution_result", lambda **kwargs: {"ok": True, "ran_scan": True, "meaningful": True, "reason": "ok"})

    outcome = run_one_account(
        request=request,
        runlog=runlog,
        audit_fn=env["audit_fn"],
        fail_schema_validation=lambda **kwargs: (_ for _ in ()).throw(AssertionError("schema validation should not fail")),
    )

    assert outcome.ran_pipeline is True
    assert seen_pipeline["base"] == repo_root
    assert seen_pipeline["env"]["PYTHONPATH"] == str(repo_root)
    assert seen_pipeline["config"] == request.account_config_authority.state_path
    assert seen_pipeline["account_config_base"] == request.base
    assert seen_pipeline["account_config_run_id"] == request.run_id
    assert seen_pipeline["account_config_account"] == "lx"
    assert seen_pipeline["account_config_compatibility_path"] == (
        request.account_config_authority.compatibility_path
    )
    assert seen_pipeline["account_config_sha256"] == (
        request.account_config_authority.account_config_sha256
    )
    assert (
        request.account_config_authority.state_path.read_bytes()
        == request.account_config_authority.compatibility_path.read_bytes()
        == request.account_config_authority.canonical_bytes
    )
    assert outcome.acct_metrics["account_config_sha256"] == (
        request.account_config_authority.account_config_sha256
    )
    assert seen_pipeline["shared_context_dir"] == request.base / "output_runs" / "run-1" / "state"


def test_run_one_account_uses_account_scan_decision_over_global_skip(monkeypatch, tmp_path: Path) -> None:
    from src.application.account_run import run_one_account

    request = replace(
        _make_request(tmp_path, prefetch_done=True),
        should_run_global=False,
        reason_global="global_not_due",
        scan_decision_by_account={
            "lx": {
                "should_run": True,
                "reason": "lx_due",
            }
        },
    )
    env = _install_common_patches(monkeypatch, request)
    runlog = _FakeRunlog()
    seen_gate: dict[str, Any] = {}

    def _decide_account_scan_gate(**kwargs):
        seen_gate.update(kwargs)
        return {
            "run_pipeline": True,
            "ran_scan": True,
            "meaningful": True,
            "result_reason": "run",
        }

    monkeypatch.setattr(env["mod"], "decide_account_scan_gate", _decide_account_scan_gate)

    def _run_pipeline_script(**kwargs):
        report_dir = kwargs["report_dir"]
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "symbols_notification.txt").write_text("account due\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(env["mod"], "run_pipeline_script", _run_pipeline_script)
    monkeypatch.setattr(env["mod"], "normalize_pipeline_subprocess_output", lambda **kwargs: {"returncode": kwargs["returncode"], "adapter": "pipeline"})
    monkeypatch.setattr(env["mod"], "decide_pipeline_execution_result", lambda **kwargs: {"ok": True, "ran_scan": True, "meaningful": True, "reason": "ok"})

    outcome = run_one_account(
        request=request,
        runlog=runlog,
        audit_fn=env["audit_fn"],
        fail_schema_validation=lambda **kwargs: (_ for _ in ()).throw(AssertionError("schema validation should not fail")),
    )

    assert seen_gate["should_run"] is True
    assert seen_gate["reason"] == "lx_due"
    assert outcome.ran_pipeline is True
    assert outcome.result.notification_text == "account due"


def test_run_one_account_appends_candidate_reject_summary_for_no_candidate_run(monkeypatch, tmp_path: Path) -> None:
    from src.application.account_run import run_one_account
    from src.application.candidate_filter_trace import append_candidate_filter_trace_rows, build_candidate_filter_trace_row

    request = _make_request(tmp_path, prefetch_done=True)
    env = _install_common_patches(monkeypatch, request)
    runlog = _FakeRunlog()

    monkeypatch.setattr(
        env["mod"],
        "decide_account_scan_gate",
        lambda **kwargs: {
            "run_pipeline": True,
            "ran_scan": True,
            "meaningful": True,
            "result_reason": "run",
        },
    )

    def _run_pipeline_script(**kwargs):
        report_dir = kwargs["report_dir"]
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "symbols_notification.txt").write_text("📋 本轮扫描完成，暂无符合条件的候选。\n", encoding="utf-8")
        append_candidate_filter_trace_rows(
            report_dir / "candidate_filter_trace.jsonl",
            [
                build_candidate_filter_trace_row(
                    run_id="run-1",
                    account="lx",
                    symbol="PDD",
                    function="sell_put",
                    mode="put",
                    status="post_filtered",
                    stage="post_filter",
                    rule="volatility_estimate_missing",
                ),
                build_candidate_filter_trace_row(
                    run_id="run-1",
                    account="lx",
                    symbol="FUTU",
                    function="sell_call",
                    mode="call",
                    status="rejected",
                    stage="stage3_risk_filter",
                    rule="risk_spread",
                ),
            ],
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(env["mod"], "run_pipeline_script", _run_pipeline_script)
    monkeypatch.setattr(env["mod"], "normalize_pipeline_subprocess_output", lambda **kwargs: {"returncode": kwargs["returncode"], "adapter": "pipeline"})
    monkeypatch.setattr(env["mod"], "decide_pipeline_execution_result", lambda **kwargs: {"ok": True, "ran_scan": True, "meaningful": True, "reason": "ok"})

    outcome = run_one_account(
        request=request,
        runlog=runlog,
        audit_fn=env["audit_fn"],
        fail_schema_validation=lambda **kwargs: (_ for _ in ()).throw(AssertionError("schema validation should not fail")),
    )

    assert "### 拒绝摘要" in outcome.result.notification_text
    assert "过滤 2 条" in outcome.result.notification_text
    assert "主要原因：数据缺失 1、流动性不足 1" in outcome.result.notification_text
    assert "涉及模块" not in outcome.result.notification_text
    assert "样例" not in outcome.result.notification_text


def test_run_one_account_passes_force_mode_only_to_event_prefetch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application.account_run import run_one_account

    request = _make_request(
        tmp_path,
        prefetch_done=True,
        force_mode=True,
    )
    env = _install_common_patches(monkeypatch, request)
    runlog = _FakeRunlog()

    monkeypatch.setattr(
        env["mod"],
        "decide_account_scan_gate",
        lambda **kwargs: {
            "run_pipeline": True,
            "ran_scan": True,
            "meaningful": True,
            "result_reason": "run",
        },
    )

    def _run_pipeline_script(**kwargs):
        report_dir = kwargs["report_dir"]
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "symbols_notification.txt").write_text("hello world\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(env["mod"], "run_pipeline_script", _run_pipeline_script)
    monkeypatch.setattr(env["mod"], "normalize_pipeline_subprocess_output", lambda **kwargs: {"returncode": kwargs["returncode"], "adapter": "pipeline"})
    monkeypatch.setattr(env["mod"], "decide_pipeline_execution_result", lambda **kwargs: {"ok": True, "ran_scan": True, "meaningful": True, "reason": "ok"})

    outcome = run_one_account(
        request=request,
        runlog=runlog,
        audit_fn=env["audit_fn"],
        fail_schema_validation=lambda **kwargs: (_ for _ in ()).throw(AssertionError("schema validation should not fail")),
    )

    assert outcome.ran_pipeline is True
    assert env["event_prefetch_calls"][0]["force_refresh"] is True
    assert outcome.prefetch_done is True


def test_run_one_account_force_event_prefetch_uses_shared_state_once(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application.account_run import run_one_account

    base_request = _make_request(
        tmp_path,
        prefetch_done=True,
        force_mode=True,
    )
    shared_prefetch_state: dict[str, Any] = {"done": False}
    prefetch_calls: list[str] = []

    def _run(acct: str):
        request = _request_for_account(
            base_request,
            acct,
            prefetch_state=shared_prefetch_state,
        )
        env = _install_common_patches(monkeypatch, request)
        runlog = _FakeRunlog()

        monkeypatch.setattr(
            env["mod"],
            "decide_account_scan_gate",
            lambda **kwargs: {
                "run_pipeline": True,
                "ran_scan": True,
                "meaningful": True,
                "result_reason": "run",
            },
        )

        def _prefetch_event_data(**kwargs):
            prefetch_calls.append(acct)
            return {
                "summary": {"errors": 0},
                "symbols": {},
            }

        monkeypatch.setattr(env["mod"], "prefetch_event_data", _prefetch_event_data)

        def _run_pipeline_script(**kwargs):
            report_dir = kwargs["report_dir"]
            report_dir.mkdir(parents=True, exist_ok=True)
            (report_dir / "symbols_notification.txt").write_text(f"{acct} ok\n", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(env["mod"], "run_pipeline_script", _run_pipeline_script)
        monkeypatch.setattr(
            env["mod"],
            "normalize_pipeline_subprocess_output",
            lambda **kwargs: {"returncode": kwargs["returncode"], "adapter": "pipeline"},
        )
        monkeypatch.setattr(
            env["mod"],
            "decide_pipeline_execution_result",
            lambda **kwargs: {"ok": True, "ran_scan": True, "meaningful": True, "reason": "ok"},
        )

        return run_one_account(
            request=request,
            runlog=runlog,
            audit_fn=env["audit_fn"],
            fail_schema_validation=lambda **kwargs: (_ for _ in ()).throw(AssertionError("schema validation should not fail")),
        )

    first = _run("lx")
    second = _run("sy")

    assert first.ran_pipeline is True
    assert second.ran_pipeline is True
    assert first.prefetch_done is True
    assert second.prefetch_done is True
    assert shared_prefetch_state["event_done"] is True
    assert shared_prefetch_state["event_force_done"] is True
    assert prefetch_calls == ["lx"]


def test_run_one_account_force_event_prefetch_failure_does_not_mark_shared_done(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application.account_run import run_one_account

    base_request = _make_request(
        tmp_path,
        prefetch_done=True,
        force_mode=True,
    )
    shared_prefetch_state: dict[str, Any] = {"done": False}
    prefetch_results: list[object] = [
        RuntimeError("event provider unavailable"),
        {"summary": {"errors": 0}, "symbols": {}},
    ]
    prefetch_calls: list[str] = []

    def _run(acct: str):
        request = _request_for_account(
            base_request,
            acct,
            prefetch_state=shared_prefetch_state,
        )
        env = _install_common_patches(monkeypatch, request)
        runlog = _FakeRunlog()

        monkeypatch.setattr(
            env["mod"],
            "decide_account_scan_gate",
            lambda **kwargs: {
                "run_pipeline": True,
                "ran_scan": True,
                "meaningful": True,
                "result_reason": "run",
            },
        )

        def _prefetch_event_data(**kwargs):
            prefetch_calls.append(acct)
            result = prefetch_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(env["mod"], "prefetch_event_data", _prefetch_event_data)

        def _run_pipeline_script(**kwargs):
            report_dir = kwargs["report_dir"]
            report_dir.mkdir(parents=True, exist_ok=True)
            (report_dir / "symbols_notification.txt").write_text(f"{acct} ok\n", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(env["mod"], "run_pipeline_script", _run_pipeline_script)
        monkeypatch.setattr(
            env["mod"],
            "normalize_pipeline_subprocess_output",
            lambda **kwargs: {"returncode": kwargs["returncode"], "adapter": "pipeline"},
        )
        monkeypatch.setattr(
            env["mod"],
            "decide_pipeline_execution_result",
            lambda **kwargs: {"ok": True, "ran_scan": True, "meaningful": True, "reason": "ok"},
        )

        return run_one_account(
            request=request,
            runlog=runlog,
            audit_fn=env["audit_fn"],
            fail_schema_validation=lambda **kwargs: (_ for _ in ()).throw(AssertionError("schema validation should not fail")),
        )

    first = _run("lx")
    assert first.ran_pipeline is True
    assert first.prefetch_done is True
    assert "event_done" not in shared_prefetch_state
    assert "event_force_done" not in shared_prefetch_state

    second = _run("sy")
    assert second.ran_pipeline is True
    assert second.prefetch_done is True
    assert shared_prefetch_state["event_done"] is True
    assert shared_prefetch_state["event_force_done"] is True
    assert prefetch_calls == ["lx", "sy"]


def test_run_one_account_returns_failed_outcome_when_pipeline_fails(monkeypatch, tmp_path: Path) -> None:
    from src.application.account_run import run_one_account

    request = _make_request(tmp_path, prefetch_done=True)
    env = _install_common_patches(monkeypatch, request)
    runlog = _FakeRunlog()

    monkeypatch.setattr(
        env["mod"],
        "decide_account_scan_gate",
        lambda **kwargs: {
            "run_pipeline": True,
            "ran_scan": True,
            "meaningful": True,
            "result_reason": "run",
        },
    )
    monkeypatch.setattr(
        env["mod"],
        "run_pipeline_script",
        lambda **kwargs: SimpleNamespace(returncode=9, stdout="oops\nline2", stderr="stderr-msg"),
    )
    monkeypatch.setattr(env["mod"], "normalize_pipeline_subprocess_output", lambda **kwargs: {"returncode": kwargs["returncode"], "adapter": "pipeline"})
    monkeypatch.setattr(env["mod"], "decide_pipeline_execution_result", lambda **kwargs: {"ok": False, "ran_scan": True, "meaningful": False, "reason": "pipeline failed"})

    outcome = run_one_account(
        request=request,
        runlog=runlog,
        audit_fn=env["audit_fn"],
        fail_schema_validation=lambda **kwargs: (_ for _ in ()).throw(AssertionError("schema validation should not fail")),
    )

    assert outcome.ran_pipeline is False
    assert outcome.prefetch_done is True
    assert outcome.result.notification_text == ""
    assert outcome.result.decision_reason == "pipeline failed"
    assert any(evt["step"] == "snapshot_batches" and evt["status"] == "error" for evt in runlog.events)
    assert any(evt["action"] == "run_pipeline_result" for evt in env["audit_events"])


def test_run_one_account_emits_degraded_event_when_artifact_write_fails(monkeypatch, tmp_path: Path) -> None:
    from src.application.account_run import run_one_account

    request = _make_request(tmp_path, prefetch_done=True)
    env = _install_common_patches(monkeypatch, request)
    runlog = _FakeRunlog()

    monkeypatch.setattr(
        env["mod"],
        "decide_account_scan_gate",
        lambda **kwargs: {
            "run_pipeline": True,
            "ran_scan": True,
            "meaningful": True,
            "result_reason": "run",
        },
    )

    def _run_pipeline_script(**kwargs):
        report_dir = kwargs["report_dir"]
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "symbols_notification.txt").write_text("artifact text\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(env["mod"], "run_pipeline_script", _run_pipeline_script)
    monkeypatch.setattr(env["mod"], "normalize_pipeline_subprocess_output", lambda **kwargs: {"returncode": kwargs["returncode"], "adapter": "pipeline"})
    monkeypatch.setattr(env["mod"], "decide_pipeline_execution_result", lambda **kwargs: {"ok": True, "ran_scan": True, "meaningful": True, "reason": "ok"})
    monkeypatch.setattr(env["mod"].run_repo, "write_run_account_text", lambda *args: (_ for _ in ()).throw(OSError("disk full")))

    outcome = run_one_account(
        request=request,
        runlog=runlog,
        audit_fn=env["audit_fn"],
        fail_schema_validation=lambda **kwargs: (_ for _ in ()).throw(AssertionError("schema validation should not fail")),
    )

    assert outcome.ran_pipeline is True
    assert outcome.result.notification_text == "artifact text"
    degraded = [evt for evt in runlog.events if evt["step"] == "account_run" and evt["status"] == "degraded"]
    assert degraded
    assert degraded[-1]["message"].startswith("write_run_account_artifacts failed for lx")
    assert any(evt["action"] == "write_run_account_artifacts" and evt.get("status") == "error" for evt in env["audit_events"])


def test_run_one_account_appends_close_advice_quote_issue_summary(monkeypatch, tmp_path: Path) -> None:
    from src.application.account_run import run_one_account

    request = _make_request(
        tmp_path,
        prefetch_done=True,
        close_advice_enabled=True,
    )
    env = _install_common_patches(monkeypatch, request)
    runlog = _FakeRunlog()

    def _write_run_account_text(base, run_id, acct, name, text):
        target = request.accounts_root / acct / "reports" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    monkeypatch.setattr(env["mod"].run_repo, "write_run_account_text", _write_run_account_text)

    monkeypatch.setattr(
        env["mod"],
        "decide_account_scan_gate",
        lambda **kwargs: {
            "run_pipeline": True,
            "ran_scan": True,
            "meaningful": True,
            "result_reason": "run",
        },
    )

    def _run_pipeline_script(**kwargs):
        report_dir = kwargs["report_dir"]
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "symbols_notification.txt").write_text("", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(env["mod"], "run_pipeline_script", _run_pipeline_script)
    monkeypatch.setattr(env["mod"], "normalize_pipeline_subprocess_output", lambda **kwargs: {"returncode": kwargs["returncode"], "adapter": "pipeline"})
    monkeypatch.setattr(env["mod"], "decide_pipeline_execution_result", lambda **kwargs: {"ok": True, "ran_scan": True, "meaningful": True, "reason": "ok"})
    monkeypatch.setattr(
        env["mod"],
        "run_close_advice",
        lambda **kwargs: {
            "enabled": True,
            "rows": 3,
            "notify_rows": 0,
            "quote_issue_rows": 2,
            "tier_counts": {"none": 3},
            "flag_counts": {
                "missing_quote": 1,
                "missing_mid": 1,
                "opend_fetch_error": 0,
                "opend_fetch_no_usable_quote": 0,
                "invalid_spread": 0,
                "spread_too_wide": 1,
            },
            "quote_issue_samples": ["0700.HK put 2026-04-29 480.00P: OpenD 限频"],
        },
    )

    outcome = run_one_account(
        request=request,
        runlog=runlog,
        audit_fn=env["audit_fn"],
        fail_schema_validation=lambda **kwargs: (_ for _ in ()).throw(AssertionError("schema validation should not fail")),
    )

    assert "本次未生成 strong/medium 提醒" in outcome.result.notification_text
    assert "系统异常 0 条，行情质量不足 3 条" in outcome.result.notification_text
    assert "系统异常表示数据拉取/字段覆盖失败，行情质量不足表示有行情但定价可信度不够" in outcome.result.notification_text
    assert "spread_too_wide=1" in outcome.result.notification_text
    assert "样例: 0700.HK put 2026-04-29 480.00P: OpenD 限频" in outcome.result.notification_text
    final_text = (request.accounts_root / "lx" / "reports" / "symbols_notification.txt").read_text(encoding="utf-8")
    assert final_text == outcome.result.notification_text + "\n"
    close_events = [evt for evt in env["audit_events"] if evt["action"] == "close_advice"]
    assert close_events
    assert close_events[-1]["extra"]["quote_issue_rows"] == 2


def test_run_one_account_projects_frozen_close_advice_integrity_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application.account_run import run_one_account

    request = _make_request(
        tmp_path,
        prefetch_done=True,
        close_advice_enabled=True,
    )
    env = _install_common_patches(monkeypatch, request)
    runlog = _FakeRunlog()

    monkeypatch.setattr(
        env["mod"],
        "decide_account_scan_gate",
        lambda **_kwargs: {
            "run_pipeline": True,
            "ran_scan": True,
            "meaningful": True,
            "result_reason": "run",
        },
    )

    def _run_pipeline_script(**kwargs):
        report_dir = kwargs["report_dir"]
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "symbols_notification.txt").write_text(
            "candidate text\n",
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        env["mod"],
        "run_pipeline_script",
        _run_pipeline_script,
    )
    monkeypatch.setattr(
        env["mod"],
        "normalize_pipeline_subprocess_output",
        lambda **kwargs: {
            "returncode": kwargs["returncode"],
            "adapter": "pipeline",
        },
    )
    monkeypatch.setattr(
        env["mod"],
        "decide_pipeline_execution_result",
        lambda **_kwargs: {
            "ok": True,
            "ran_scan": True,
            "meaningful": True,
            "reason": "ok",
        },
    )
    monkeypatch.setattr(
        env["mod"],
        "run_close_advice",
        lambda **_kwargs: {
            "enabled": True,
            "status": "snapshot_integrity_failed",
            "snapshot_authority": "invalid",
            "rows": 0,
            "notify_rows": 0,
            "quote_issue_rows": 0,
            "tier_counts": {},
            "flag_counts": {
                "required_data_snapshot_integrity_failed": 1
            },
            "integrity_failure": {
                "reason": "required_data_snapshot_integrity_failed"
            },
        },
    )

    outcome = run_one_account(
        request=request,
        runlog=runlog,
        audit_fn=env["audit_fn"],
        fail_schema_validation=lambda **_kwargs: None,
    )

    assert outcome.ran_pipeline is False
    assert outcome.result.ran_scan is True
    assert outcome.result.should_notify is False
    assert (
        outcome.result.decision_reason
        == "required_data_snapshot_integrity_failed"
    )
    assert outcome.result.notification_text == ""
    assert outcome.acct_metrics["ran_pipeline"] is False
    close_events = [
        event
        for event in env["audit_events"]
        if event["action"] == "close_advice"
    ]
    assert close_events[-1]["status"] == "error"


def test_run_one_account_suppresses_close_advice_spread_only_quality_summary(monkeypatch, tmp_path: Path) -> None:
    from src.application.account_run import run_one_account

    request = _make_request(
        tmp_path,
        prefetch_done=True,
        close_advice_enabled=True,
    )
    env = _install_common_patches(monkeypatch, request)
    runlog = _FakeRunlog()

    def _write_run_account_text(base, run_id, acct, name, text):
        target = request.accounts_root / acct / "reports" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    monkeypatch.setattr(env["mod"].run_repo, "write_run_account_text", _write_run_account_text)

    monkeypatch.setattr(
        env["mod"],
        "decide_account_scan_gate",
        lambda **kwargs: {
            "run_pipeline": True,
            "ran_scan": True,
            "meaningful": True,
            "result_reason": "run",
        },
    )

    def _run_pipeline_script(**kwargs):
        report_dir = kwargs["report_dir"]
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "symbols_notification.txt").write_text("", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(env["mod"], "run_pipeline_script", _run_pipeline_script)
    monkeypatch.setattr(env["mod"], "normalize_pipeline_subprocess_output", lambda **kwargs: {"returncode": kwargs["returncode"], "adapter": "pipeline"})
    monkeypatch.setattr(env["mod"], "decide_pipeline_execution_result", lambda **kwargs: {"ok": True, "ran_scan": True, "meaningful": True, "reason": "ok"})
    monkeypatch.setattr(
        env["mod"],
        "run_close_advice",
        lambda **kwargs: {
            "enabled": True,
            "rows": 1,
            "notify_rows": 0,
            "quote_issue_rows": 1,
            "tier_counts": {"none": 1},
            "flag_counts": {
                "missing_quote": 0,
                "missing_mid": 0,
                "required_data_missing_expiration": 0,
                "required_data_missing_contract": 0,
                "required_data_fetch_error": 0,
                "opend_fetch_error": 0,
                "opend_fetch_no_usable_quote": 0,
                "invalid_spread": 0,
                "spread_too_wide": 1,
            },
            "quote_issue_samples": ["0700.HK put 2026-04-29 480.00P: spread too wide"],
        },
    )

    outcome = run_one_account(
        request=request,
        runlog=runlog,
        audit_fn=env["audit_fn"],
        fail_schema_validation=lambda **kwargs: (_ for _ in ()).throw(AssertionError("schema validation should not fail")),
    )

    assert "### [lx] 平仓建议" in outcome.result.notification_text
    assert "本次未生成 strong/medium 提醒" in outcome.result.notification_text
    assert "行情质量不足" not in outcome.result.notification_text
    assert "spread_too_wide=1" not in outcome.result.notification_text
    assert "样例:" not in outcome.result.notification_text
    final_text = (request.accounts_root / "lx" / "reports" / "symbols_notification.txt").read_text(encoding="utf-8")
    assert final_text == outcome.result.notification_text + "\n"
