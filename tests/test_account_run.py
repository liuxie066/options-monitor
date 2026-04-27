from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any


class _FakeRunlog:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def safe_event(self, step: str, status: str, **kwargs) -> None:
        event = {"step": step, "status": status}
        event.update(kwargs)
        self.events.append(event)


def _make_request(tmp_path: Path, *, prefetch_done: bool = False) -> Any:
    from src.application.account_run import AccountRunRequest

    base = tmp_path / "repo"
    base.mkdir()
    cfg_path = base / "config.us.json"
    cfg_path.write_text("{}", encoding="utf-8")
    run_dir = base / "output_runs" / "run-1"
    run_dir.mkdir(parents=True)
    shared_required = base / "output"
    shared_required.mkdir()
    out_link = base / "output"
    legacy_tmp = base / ".tmp-links"
    legacy_tmp.mkdir()
    accounts_root = run_dir / "accounts"
    accounts_root.mkdir(parents=True)
    return AccountRunRequest(
        acct="lx",
        base=base,
        base_cfg={
            "symbols": [{"symbol": "NVDA", "market": "US"}],
            "portfolio": {},
            "close_advice": {"enabled": False},
        },
        cfg_path=cfg_path,
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
        out_link=out_link,
        legacy_output_tmp_dir=legacy_tmp,
        accounts_root=accounts_root,
        prefetch_done=prefetch_done,
    )


def _install_common_patches(monkeypatch, request: Any) -> dict[str, Any]:
    from src.application import account_run as mod

    audit_events: list[dict[str, Any]] = []
    state_writes: list[tuple[str, dict[str, Any]]] = []

    acct_report_dir = request.accounts_root / request.acct / "reports"
    acct_state_dir = request.accounts_root / request.acct / "state"
    shared_state_dir = request.run_dir / "state"

    def _audit(event_type: str, action: str, **kwargs) -> None:
        payload = {"event_type": event_type, "action": action}
        payload.update(kwargs)
        audit_events.append(payload)

    monkeypatch.setattr(mod, "ensure_account_output_dir", lambda path: path.mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(mod, "update_legacy_output_link", lambda *args, **kwargs: True)
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: list(cfg.get("symbols") or []))
    monkeypatch.setattr(mod, "set_watchlist_config", lambda cfg, syms: cfg.__setitem__("symbols", list(syms)))
    monkeypatch.setattr(mod, "utc_now", lambda: "2026-04-25T00:00:00Z")
    monkeypatch.setattr(mod, "decide_should_notify", lambda **kwargs: True)

    monkeypatch.setattr(mod.run_repo, "get_run_account_dir", lambda *args: acct_report_dir)
    monkeypatch.setattr(mod.run_repo, "get_run_account_state_dir", lambda *args: acct_state_dir)
    monkeypatch.setattr(mod.run_repo, "ensure_run_account_state_dir", lambda *args: acct_state_dir.mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(mod.run_repo, "get_run_state_dir", lambda *args: shared_state_dir)
    monkeypatch.setattr(mod.run_repo, "write_run_account_text", lambda *args: None)
    monkeypatch.setattr(mod.run_repo, "copy_to_run_account", lambda *args: None)

    def _write_account_state_json_text(base, acct, name, payload):
        acct_dir = request.base / "output_accounts" / acct / "state"
        acct_dir.mkdir(parents=True, exist_ok=True)
        target = acct_dir / name
        target.write_text("{}", encoding="utf-8")
        return target

    monkeypatch.setattr(mod.state_repo, "write_account_state_json_text", _write_account_state_json_text)
    monkeypatch.setattr(mod.state_repo, "write_account_run_state", lambda base, run_id, acct, name, payload: state_writes.append((name, dict(payload))))
    monkeypatch.setattr(mod.state_repo, "append_run_audit_jsonl", lambda *args, **kwargs: None)

    return {
        "mod": mod,
        "audit_fn": _audit,
        "audit_events": audit_events,
        "state_writes": state_writes,
        "acct_report_dir": acct_report_dir,
        "acct_state_dir": acct_state_dir,
    }


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
    monkeypatch.setattr(env["mod"], "prefetch_required_data", lambda **kwargs: (_ for _ in ()).throw(AssertionError("prefetch should not run")))
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
    assert any(name == "account_metrics.json" for name, _ in env["state_writes"])


def test_run_one_account_prefetches_and_runs_pipeline_successfully(monkeypatch, tmp_path: Path) -> None:
    from src.application.account_run import run_one_account

    request = _make_request(tmp_path)
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
        "prefetch_required_data",
        lambda **kwargs: {"errors": 0, "audit": [{"ok": True, "tool_name": "required_data_prefetch", "symbol": "NVDA", "message": "ok"}]},
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
    assert any(evt["action"] == "required_data_prefetch" for evt in env["audit_events"])
    assert any(evt["step"] == "fetch_chain_cache" and evt["status"] == "start" for evt in runlog.events)
    assert any(evt["step"] == "snapshot_batches" and evt["status"] == "ok" for evt in runlog.events)


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

    request = _make_request(tmp_path, prefetch_done=True)
    request.base_cfg["close_advice"] = {"enabled": True}
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
    assert "missing_quote 表示持仓已获取，但未取得可用报价，不是持仓缺失" in outcome.result.notification_text
    assert "样例: 0700.HK put 2026-04-29 480.00P: OpenD 限频" in outcome.result.notification_text
    close_events = [evt for evt in env["audit_events"] if evt["action"] == "close_advice"]
    assert close_events
    assert close_events[-1]["extra"]["quote_issue_rows"] == 2
