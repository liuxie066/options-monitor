"""Synthetic cron-to-CLI regression; never starts a child or external client."""
from __future__ import annotations

import json
import socket
import subprocess
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from domain.domain.engine import SchedulerDecisionView
from src.application import multi_account_tick as tick
from src.application import tick_cron
from src.application.tick_guard_flow import TickGuardOutcome
from src.application.tick_scheduler_context import TickSchedulerContext, TickSchedulerOutcome
from src.interfaces.cli import main as cli


@pytest.mark.parametrize("mode,expected", [
    ("skip", 0), ("early", 0), ("delivery_only", 0),
    ("mixed", 2), ("missing", 2), ("incomplete_metric", 2),
    ("early_unknown", 2), ("failure", 7), ("exception", 2),
    ("cancel", 130), ("delivery_failure", 7),
])
def test_cron_cli_preserves_proven_skip_and_keeps_failures(monkeypatch, tmp_path, capsys, mode, expected):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    config = runtime / "config.hk.json"
    config.write_text(json.dumps({
        "_generated": {"schema_version": "1.0", "market": "hk", "source_format": "yaml", "generator": "options-monitor"},
        "accounts": ["lx", "sy"], "symbols": [], "portfolio": {}, "schedule_hk": {"enabled": True},
    }))
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime))
    for name in ("resolve_config_contract", "ensure_runtime_canonical_config", "ensure_runtime_schedule_matches_market", "ensure_runtime_config_freshness"):
        monkeypatch.setattr(tick, name, lambda *_a, **_kw: {})

    class Log:
        run_id = "inner-current"
        def __init__(self, _base):
            pass
        def safe_event(self, *_a, **_kw):
            pass

    monkeypatch.setattr(tick, "RunLogger", Log)
    monkeypatch.setattr(tick, "run_tick_guard_flow", lambda r: TickGuardOutcome(
        True, 0, r.base_cfg, r.accounts, r.default_account, ZoneInfo("Asia/Shanghai"),
    ))
    early = mode in {"early", "early_unknown", "delivery_only", "delivery_failure"}
    delivery_only = mode in {"delivery_only", "delivery_failure"}
    decision = {"schema_kind": "scheduler_decision", "schema_version": "1.0", "should_run_scan": False,
                "is_notify_window_open": False, "in_run_window": delivery_only, "reason": "fixture skip"}
    per_account = {a: {"source": "account_scheduler", "should_run": False, "scheduler_decision": decision} for a in ("lx", "sy")}
    if mode == "early_unknown":
        per_account["sy"] = {}
    monkeypatch.setattr(tick, "build_tick_scheduler_context", lambda _r: TickSchedulerOutcome(
        True, 0, TickSchedulerContext(
            markets_to_run=["HK"], scheduler_markets=["HK"], state_path=runtime / "scheduler.json",
            scheduler_schedule_key="schedule_hk", scheduler_ms=1, scheduler_decision=decision,
            scheduler_view=SchedulerDecisionView.from_payload(decision), notify_decision_by_account={},
            scan_decision_by_account=per_account, should_run_global=not early, reason_global="fixture skip",
        ), [],
    ))
    forbidden = []
    def reject(name):
        def call(*_a, **_kw):
            forbidden.append(name)
            raise AssertionError(name)
        return call
    for name in ("run_opend_watchdog", "send_opend_alert", "send_opend_recovery_notice",
                 "warm_required_data_chain_cache", "publish_runtime_portfolio_snapshot_shadows"):
        monkeypatch.setattr(tick, name, reject(name))
    monkeypatch.setattr(tick, "_opening_chain_warmup_context", lambda **_kw: None)
    # No child process can escape even if an orchestration branch regresses.
    monkeypatch.setattr(subprocess, "Popen", reject("subprocess"))
    monkeypatch.setattr(socket.socket, "connect", reject("network"))
    monkeypatch.setattr(socket.socket, "connect_ex", reject("network"))

    def execution(_request):
        assert not early
        rows = [{"account": a, "ran_scan": False, "ran_pipeline": False, "scan_outcome": "scheduler_skipped"} for a in ("lx", "sy")]
        if mode == "mixed":
            rows[1]["scan_outcome"] = "success"
            rows[1]["ran_scan"] = rows[1]["ran_pipeline"] = True
        if mode == "missing":
            for row in rows:
                row.pop("scan_outcome")
        if mode == "incomplete_metric":
            rows[1].pop("ran_pipeline")
        return SimpleNamespace(account_metrics=rows,
                               scan_outcomes={row["account"]: row["scan_outcome"] for row in rows
                                              if "scan_outcome" in row and "ran_pipeline" in row},
                               results=[], prefetch_invocation_count=0,
                               snapshot_status=None, snapshot_manifest_sha256=None, prepared_context_metrics=[],
                               ran_pipeline_accounts=[], scheduled_scan_targets_by_account={},
                               runtime_portfolio_snapshot_shadow_tasks=[])
    monkeypatch.setattr(tick, "run_tick_account_execution", execution)
    # The tested contract starts after account scheduling; external work is never invoked.
    monkeypatch.setattr(tick, "publish_runtime_portfolio_snapshot_shadows", lambda **_kw: None)
    def notification(*, notification_request, **_kwargs):
        if mode == "exception":
            raise OSError("fixture failure")
        if mode == "cancel":
            raise KeyboardInterrupt
        return 7 if mode in {"failure", "delivery_failure"} else 0
    monkeypatch.setattr(tick, "_run_notification_with_post_delivery_fallback", notification)
    failures = []
    recoveries = []
    monkeypatch.setattr(tick_cron, "report_system_failure", lambda **kw: failures.append(kw))
    monkeypatch.setattr(tick_cron, "report_system_recovery", lambda **kw: recoveries.append(kw))
    observed = []
    real_execute = tick.execute_tick
    from src.interfaces.cli import run_ops
    def observe(argv):
        result = real_execute(argv)
        observed.append(result)
        return result
    monkeypatch.setattr(run_ops, "execute_tick", observe)
    def child(command, **kw):
        for key, value in kw["env"].items():
            if key.startswith("OM_"):
                monkeypatch.setenv(key, value)
        return subprocess.CompletedProcess(command, cli.main(command[1:]))
    monkeypatch.setattr(tick_cron, "_run_tick_process_group", child)
    # Config preflight is separately tested; this fixture has no production authoring source.
    real_cron = tick_cron.run_tick_cron
    def cron(**kw):
        return real_cron(**kw, preflight_config_fn=None, runtime_root=runtime)
    monkeypatch.setattr(cli, "run_tick_cron", cron)
    rc = cli.main(["run", "tick-cron", "--market", "hk", "--config", str(config),
                                          "--accounts", "lx", "sy", "--lock-path", str(runtime / "tick.lock")])
    assert rc == expected
    assert observed and observed[0].run_id == "inner-current"
    assert len(failures) == (0 if expected == 0 else 2)
    assert not recoveries
    assert not forbidden
    text = capsys.readouterr().out
    if expected == 0:
        assert "调度跳过" in text
        assert "本次结果来源不可用" not in text
