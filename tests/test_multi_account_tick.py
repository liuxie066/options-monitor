from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def test_run_tick_forwards_cli_argv_and_returns_main_exit_code(monkeypatch) -> None:
    from src.application import multi_account_tick as mod

    seen: dict[str, Any] = {}

    def fake_main(argv: list[str] | None = None) -> int:
        seen["argv"] = list(argv or [])
        return 7

    monkeypatch.setattr(mod, "multi_tick_main", fake_main)

    out = mod.run_tick(["--config", "config.us.json", "--accounts", "lx", "sy"])

    assert out == 7
    assert seen["argv"] == [
        "--config",
        "config.us.json",
        "--accounts",
        "lx",
        "sy",
    ]


def test_run_tick_uses_empty_argv_when_argv_is_none(monkeypatch) -> None:
    from src.application import multi_account_tick as mod

    seen: dict[str, Any] = {}

    def fake_main(argv: list[str] | None = None) -> int:
        seen["argv"] = list(argv or [])
        return 0

    monkeypatch.setattr(mod, "multi_tick_main", fake_main)

    out = mod.run_tick()

    assert out == 0
    assert seen["argv"] == []


def test_run_tick_restores_sys_argv_after_success(monkeypatch) -> None:
    from src.application import multi_account_tick as mod

    original = ["pytest", "-k", "multi-account"]
    monkeypatch.setattr(sys, "argv", list(original))

    def fake_main(argv: list[str] | None = None) -> int:
        assert argv == ["--config", "config.hk.json"]
        return 3

    monkeypatch.setattr(mod, "multi_tick_main", fake_main)

    out = mod.run_tick(["--config", "config.hk.json"])

    assert out == 3
    assert sys.argv == original


def test_run_tick_restores_sys_argv_after_exception(monkeypatch) -> None:
    from src.application import multi_account_tick as mod

    original = ["pytest", "tests/test_multi_account_tick.py"]
    monkeypatch.setattr(sys, "argv", list(original))

    def fake_main(argv: list[str] | None = None) -> int:
        assert argv == ["--no-send"]
        raise RuntimeError("boom")

    monkeypatch.setattr(mod, "multi_tick_main", fake_main)

    try:
        mod.run_tick(["--no-send"])
        raise AssertionError("expected runtime error")
    except RuntimeError as exc:
        assert str(exc) == "boom"

    assert sys.argv == original


def test_current_run_id_is_reexported_from_multi_tick_main() -> None:
    from src.application import multi_account_tick as mod

    assert callable(mod.current_run_id)


def test_explicit_empty_cli_account_fails_before_run_artifacts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import multi_account_tick as mod

    config_path = tmp_path / "config.us.json"
    config_path.write_text('{"symbols": []}\n', encoding="utf-8")
    monkeypatch.setattr(
        mod,
        "resolve_runtime_root",
        lambda **_kwargs: SimpleNamespace(
            runtime_root=tmp_path,
            source="test",
        ),
    )
    monkeypatch.setattr(mod, "resolve_config_contract", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        mod,
        "ensure_runtime_canonical_config",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        mod,
        "ensure_runtime_config_identity",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        mod,
        "ensure_runtime_schedule_matches_market",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        mod,
        "RunLogger",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("run logger must not start")
        ),
    )

    with pytest.raises(SystemExit, match="invalid account scope"):
        mod.main(["--config", str(config_path), "--accounts", ""])

    assert not (tmp_path / "output_runs").exists()


def test_allow_stale_config_still_rejects_removed_runtime_fields_before_run_artifacts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import json

    from src.application import multi_account_tick as mod

    config_path = tmp_path / "config.us.json"
    config_path.write_text(
        json.dumps(
            {
                "accounts": ["lx"],
                "symbols": [
                    {
                        "symbol": "NVDA",
                        "sell_put": {"enabled": False},
                        "sell_call": {"enabled": False},
                        "combo_yield": {
                            "enabled": True,
                            "output_mode": "separate",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        mod,
        "resolve_runtime_root",
        lambda **_kwargs: SimpleNamespace(runtime_root=tmp_path, source="test"),
    )
    monkeypatch.setattr(mod, "resolve_config_contract", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        mod,
        "ensure_runtime_canonical_config",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        mod,
        "ensure_runtime_config_identity",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        mod,
        "ensure_runtime_schedule_matches_market",
        lambda *_args, **_kwargs: {"market": "us"},
    )
    monkeypatch.setattr(
        mod,
        "ensure_runtime_config_freshness",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("allow-stale-config must skip freshness only")
        ),
    )
    monkeypatch.setattr(
        mod,
        "RunLogger",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("run logger must not start")
        ),
    )

    with pytest.raises(SystemExit, match="output_mode has been removed"):
        mod.main(
            [
                "--config",
                str(config_path),
                "--market-config",
                "us",
                "--allow-stale-config",
            ]
        )

    assert not (tmp_path / "output_runs").exists()


def test_run_account_outcomes_runs_parallel_and_preserves_account_order() -> None:
    from src.application import tick_account_execution as mod

    started: list[str] = []
    lock = threading.Lock()
    both_started = threading.Event()

    def run_account(acct: str) -> str:
        with lock:
            started.append(acct)
            if len(started) == 2:
                both_started.set()
        assert both_started.wait(1.0), "account runs did not overlap"
        return f"done-{acct}"

    out = mod.run_account_outcomes(
        account_ids=["lx", "sy"],
        max_workers=2,
        run_account_fn=run_account,
    )

    assert out == ["done-lx", "done-sy"]
    assert sorted(started) == ["lx", "sy"]


def test_tick_idempotency_separates_symbol_diagnostic_from_full_schedule(
    tmp_path,
) -> None:
    from datetime import datetime, timezone
    from src.application.tick_run_context import build_tick_idempotency_context

    now = datetime(2026, 7, 29, 1, 23, tzinfo=timezone.utc)
    common = {
        "cfg_path": tmp_path / "config.us.json",
        "market_config": "us",
        "accounts": ["lx"],
        "now_utc": now,
    }
    scheduled = build_tick_idempotency_context(
        **common,
        trigger_kind="scheduled",
        no_send=False,
        trigger_job_id="om-tick-us",
    )
    diagnostic = build_tick_idempotency_context(
        **common,
        trigger_kind="manual",
        symbols="NVDA",
        no_send=True,
        trigger_job_id="om-tick-us",
    )
    other_symbol = build_tick_idempotency_context(
        **common,
        trigger_kind="manual",
        symbols="AAPL",
        no_send=True,
        trigger_job_id="om-tick-us",
    )

    assert scheduled.key != diagnostic.key
    assert diagnostic.key != other_symbol.key


def test_tick_account_execution_isolates_one_account_exception(
    monkeypatch,
    tmp_path,
) -> None:
    from src.application import tick_account_execution as mod
    from src.application.account_run import AccountRunOutcome
    from src.application.multi_tick.misc import AccountResult

    events: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []

    def fake_run_one_account(*, request, **_kwargs):
        if request.acct == "lx":
            raise OSError("lx output unavailable")
        return AccountRunOutcome(
            result=AccountResult("sy", False, False, "not_due", ""),
            acct_metrics={"account": "sy", "ran_pipeline": False},
            prefetch_done=False,
            ran_pipeline=False,
        )

    monkeypatch.setattr(mod, "run_one_account", fake_run_one_account)
    outcome = mod.run_tick_account_execution(
        mod.TickAccountExecutionRequest(
            account_ids=["lx", "sy"],
            account_workers=2,
            base=tmp_path,
            base_cfg={},
            cfg_path=tmp_path / "config.us.json",
            vpy=tmp_path / ".venv" / "bin" / "python",
            markets_to_run=["US"],
            scheduler_ms=1,
            scheduler_view={},
            notify_decision_by_account={},
            should_run_global=False,
            reason_global="not_due",
            run_id="isolated-run",
            run_dir=tmp_path / "output_runs" / "isolated-run",
            shared_required=tmp_path / "required",
            accounts_root=tmp_path / "accounts",
            prefetch_done=False,
            force_mode=False,
            smoke=False,
            no_send=True,
            scan_decision_by_account={
                "lx": {"should_run": False, "reason": "not_due"},
                "sy": {"should_run": False, "reason": "not_due"},
            },
            state_path=tmp_path / "scheduler.json",
            scheduler_schedule_key="schedule",
            runlog=SimpleNamespace(
                safe_event=lambda step, status, **kwargs: events.append(
                    {"step": step, "status": status, **kwargs}
                )
            ),
            audit_helper=SimpleNamespace(
                audit=lambda event_type, action, **kwargs: audits.append(
                    {"event_type": event_type, "action": action, **kwargs}
                ),
                fail_schema_validation=lambda **_kwargs: None,
            ),
        )
    )

    assert [item.account for item in outcome.results] == ["lx", "sy"]
    assert outcome.results[0].decision_reason == "account_execution_exception:OSError"
    assert outcome.results[1].decision_reason == "not_due"
    assert any(item["action"] == "account_execution_exception" for item in audits)


def test_account_worker_count_is_bounded_by_runtime_config() -> None:
    from src.application import multi_account_tick as mod

    # 缺省：全并行（=账户数），加账户零配置
    assert mod._resolve_account_run_max_workers({"runtime": {}}, 3) == 3
    assert mod._resolve_account_run_max_workers({}, 4) == 4
    # 显式压低并行度（合法的运营选择）：尊重设置值
    assert mod._resolve_account_run_max_workers({"runtime": {"multi_account_max_workers": 2}}, 5) == 2
    # 显式值被账户数封顶
    assert mod._resolve_account_run_max_workers({"runtime": {"multi_account_max_workers": 9}}, 2) == 2
    # 显式 0/负数被 to_positive_int 收敛为 1（显式串行）
    assert mod._resolve_account_run_max_workers({"runtime": {"multi_account_max_workers": 0}}, 5) == 1
    # 兼容旧键 account_max_workers
    assert mod._resolve_account_run_max_workers({"runtime": {"account_max_workers": 2}}, 5) == 2
    # 单账户不并行
    assert mod._resolve_account_run_max_workers({"runtime": {}}, 1) == 1


def test_default_account_must_be_active_account() -> None:
    from src.application import multi_account_tick as mod

    assert mod._resolve_default_account(None, ["lx", "sy"]) == "lx"
    assert mod._resolve_default_account("SY", ["lx", "sy"]) == "sy"

    try:
        mod._resolve_default_account("other", ["lx", "sy"])
        raise AssertionError("expected config error")
    except SystemExit as exc:
        assert "--default-account must be one of active accounts" in str(exc)


def test_mark_scheduler_accounts_records_exact_target_and_completion(tmp_path) -> None:
    import json
    from datetime import datetime, timezone
    from src.application.scan_scheduler import mark_scheduler_accounts

    config = tmp_path / "config.us.json"
    config.write_text(json.dumps({"schedule": {"enabled": True}}), encoding="utf-8")
    state = tmp_path / "scheduler_state.json"
    completed_at = datetime(2026, 7, 21, 14, 1, tzinfo=timezone.utc)

    mark_scheduler_accounts(
        config=config,
        state=state,
        schedule_key="schedule",
        accounts=["lx", "sy"],
        mark_scanned=True,
        processed_scan_targets_by_account={
            "lx": "2026-07-21T10:00:00-04:00",
            "sy": "2026-07-21T10:30:00-04:00",
        },
        base_dir=tmp_path,
        now_utc=completed_at,
    )

    data = json.loads(state.read_text(encoding="utf-8"))
    assert data["last_run_utc_by_account"] == {
        "lx": completed_at.isoformat(),
        "sy": completed_at.isoformat(),
    }
    assert data["last_processed_scan_target_utc_by_account"] == {
        "lx": "2026-07-21T14:00:00+00:00",
        "sy": "2026-07-21T14:30:00+00:00",
    }



def test_mark_scheduler_accounts_does_not_regress_processed_target(tmp_path) -> None:
    import json
    from datetime import datetime, timezone
    from src.application.scan_scheduler import mark_scheduler_accounts

    config = tmp_path / "config.us.json"
    config.write_text(json.dumps({"schedule": {"enabled": True}}), encoding="utf-8")
    state = tmp_path / "scheduler_state.json"
    state.write_text(
        json.dumps(
            {
                "last_run_utc_by_account": {"lx": "2026-07-21T14:31:00+00:00"},
                "last_processed_scan_target_utc_by_account": {"lx": "2026-07-21T14:30:00+00:00"},
            }
        ),
        encoding="utf-8",
    )

    mark_scheduler_accounts(
        config=config,
        state=state,
        schedule_key="schedule",
        accounts=["lx"],
        mark_scanned=True,
        processed_scan_targets_by_account={"lx": "2026-07-21T14:00:00+00:00"},
        base_dir=tmp_path,
        now_utc=datetime(2026, 7, 21, 14, 32, tzinfo=timezone.utc),
    )

    data = json.loads(state.read_text(encoding="utf-8"))
    assert data["last_processed_scan_target_utc_by_account"]["lx"] == "2026-07-21T14:30:00+00:00"


def test_tick_account_execution_keeps_prefetch_done_after_later_scheduler_skip(monkeypatch, tmp_path) -> None:
    from src.application import tick_account_execution as mod
    from src.application.tick_account_execution import TickAccountExecutionRequest

    def fake_run_account_outcomes(**_kwargs):
        return [
            SimpleNamespace(
                result=SimpleNamespace(account="lx"),
                acct_metrics={"account": "lx"},
                prefetch_done=True,
                ran_pipeline=True,
            ),
            SimpleNamespace(
                result=SimpleNamespace(account="sy"),
                acct_metrics={"account": "sy"},
                prefetch_done=False,
                ran_pipeline=False,
            ),
        ]

    monkeypatch.setattr(mod, "run_account_outcomes", fake_run_account_outcomes)

    outcome = mod.run_tick_account_execution(
        TickAccountExecutionRequest(
            account_ids=["lx", "sy"],
            account_workers=1,
            base=tmp_path,
            base_cfg={},
            cfg_path=tmp_path / "config.us.json",
            vpy=tmp_path / ".venv" / "bin" / "python",
            markets_to_run=["US"],
            scheduler_ms=1,
            scheduler_view={},
            notify_decision_by_account={},
            should_run_global=False,
            reason_global="mixed_account_decisions",
            run_id="run-1",
            run_dir=tmp_path / "output_runs" / "run-1",
            shared_required=tmp_path / "output_runs" / "run-1" / "required_data",
            accounts_root=tmp_path / "output_accounts",
            prefetch_done=False,
            force_mode=False,
            smoke=False,
            no_send=True,
            scan_decision_by_account={
                "lx": {
                    "should_run": True,
                    "scheduler_decision": {
                        "scheduled_scan_target_market": "2026-07-21T10:00:00-04:00",
                    },
                },
                "sy": {
                    "should_run": True,
                    "scheduler_decision": {"scheduled_scan_target_market": None},
                },
            },
            state_path=tmp_path / "scheduler_state.json",
            scheduler_schedule_key="schedule",
            runlog=SimpleNamespace(),
            audit_helper=SimpleNamespace(audit=lambda *_args, **_kwargs: None),
        )
    )

    assert outcome.prefetch_done is True
    assert outcome.ran_any_pipeline is True
    assert outcome.ran_pipeline_accounts == ["lx"]
    assert outcome.scheduled_scan_targets_by_account == {
        "lx": "2026-07-21T10:00:00-04:00",
        "sy": None,
    }
    assert not (tmp_path / "scheduler_state.json").exists()


def test_main_uses_env_runtime_root_for_stateful_tick_flows(monkeypatch, tmp_path) -> None:
    import json
    from zoneinfo import ZoneInfo

    from src.application import multi_account_tick as mod
    from src.application.tick_guard_flow import TickGuardOutcome

    cfg = tmp_path / "config.us.json"
    cfg.write_text(
        json.dumps(
            {
                "_generated": {
                    "schema_version": "1.0",
                    "generator": "options-monitor",
                    "source_format": "yaml",
                    "market": "us",
                },
                "accounts": ["lx"],
                "symbols": [],
                "schedule": {"enabled": True},
                "portfolio": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    runtime_root = tmp_path / "runtime"
    captured: dict[str, Any] = {}

    class _RunLogger:
        def __init__(self, base):
            captured["runlog_base"] = base
            self.run_id = "run-1"

        def safe_event(self, *args, **kwargs):
            captured.setdefault("events", []).append((args, kwargs))

    def _run_tick_guard_flow(request):
        captured["guard_base"] = request.base
        captured["guard_vpy"] = request.vpy
        return TickGuardOutcome(
            should_continue=False,
            return_code=0,
            base_cfg=request.base_cfg,
            accounts=request.accounts,
            default_account=request.default_account,
            bj_tz=ZoneInfo("Asia/Shanghai"),
        )

    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setattr(mod, "RunLogger", _RunLogger)
    monkeypatch.setattr(mod, "resolve_config_contract", lambda *args, **kwargs: {})
    monkeypatch.setattr(mod, "ensure_runtime_canonical_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod, "ensure_runtime_schedule_matches_market", lambda *args, **kwargs: {"market": ""})
    monkeypatch.setattr(mod.state_repo, "claim_idempotency_record", lambda *args, **kwargs: {"claimed": True})
    monkeypatch.setattr(mod, "run_tick_guard_flow", _run_tick_guard_flow)

    rc = mod.main(["--config", str(cfg), "--accounts", "lx"])

    assert rc == 0
    assert captured["runlog_base"] == runtime_root.resolve()
    assert captured["guard_base"] == runtime_root.resolve()
    assert captured["guard_vpy"] == Path(sys.executable)


def test_main_scheduler_skip_does_not_create_output_run_workspace(monkeypatch, tmp_path) -> None:
    import json
    from zoneinfo import ZoneInfo

    from domain.domain.engine import SchedulerDecisionView
    from src.application import multi_account_tick as mod
    from src.application.tick_guard_flow import TickGuardOutcome
    from src.application.tick_scheduler_context import TickSchedulerContext, TickSchedulerOutcome

    cfg = tmp_path / "config.us.json"
    cfg.write_text(
        json.dumps(
            {
                "_generated": {
                    "schema_version": "1.0",
                    "generator": "options-monitor",
                    "source_format": "yaml",
                    "market": "us",
                },
                "accounts": ["lx"],
                "symbols": [{"symbol": "NVDA", "broker": "US"}],
                "schedule": {"enabled": True},
                "portfolio": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    runtime_root = tmp_path / "runtime"

    class _RunLogger:
        def __init__(self, base):
            self.base = base
            self.run_id = "run-skip"

        def safe_event(self, *args, **kwargs):
            pass

    def _run_tick_guard_flow(request):
        return TickGuardOutcome(
            should_continue=True,
            return_code=0,
            base_cfg=request.base_cfg,
            accounts=request.accounts,
            default_account=request.default_account,
            bj_tz=ZoneInfo("Asia/Shanghai"),
        )

    def _scheduler_context(_request):
        decision = {
            "schema_kind": "scheduler_decision",
            "schema_version": "1.0",
            "should_run_scan": False,
            "is_notify_window_open": False,
            "reason": "当前运行点已处理，等待下一个运行点。",
        }
        return TickSchedulerOutcome(
            should_continue=True,
            return_code=0,
            results=[],
            context=TickSchedulerContext(
                markets_to_run=["US"],
                scheduler_markets=["US"],
                state_path=runtime_root / "output_shared" / "state" / "scheduler_state.json",
                scheduler_schedule_key="schedule",
                scheduler_ms=1,
                scheduler_decision=decision,
                scheduler_view=SchedulerDecisionView.from_payload(decision),
                notify_decision_by_account={},
                scan_decision_by_account={"lx": {"should_run": False, "reason": decision["reason"]}},
                should_run_global=False,
                reason_global=decision["reason"],
            ),
        )

    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setattr(mod, "RunLogger", _RunLogger)
    monkeypatch.setattr(mod, "resolve_config_contract", lambda *args, **kwargs: {})
    monkeypatch.setattr(mod, "ensure_runtime_canonical_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod, "ensure_runtime_schedule_matches_market", lambda *args, **kwargs: {"market": ""})
    monkeypatch.setattr(mod.state_repo, "claim_idempotency_record", lambda *args, **kwargs: {"claimed": True})
    monkeypatch.setattr(mod, "run_tick_guard_flow", _run_tick_guard_flow)
    monkeypatch.setattr(mod, "build_tick_scheduler_context", _scheduler_context)
    monkeypatch.setattr(
        mod,
        "prepare_tick_run_workspace",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("skip must not create run workspace")),
    )
    monkeypatch.setattr(
        mod,
        "run_tick_account_execution",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("skip must not run account execution")),
    )
    monkeypatch.setattr(
        mod,
        "run_tick_notification_flow",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("skip must not run notification flow")),
    )

    rc = mod.main(["--config", str(cfg), "--accounts", "lx"])

    assert rc == 0
    assert not (runtime_root / "output_runs").exists()


def test_daily_brief_trigger_kind_distinguishes_schedule_manual_and_force() -> None:
    import src.application.multi_account_tick as mod

    assert (
        mod._resolve_daily_brief_trigger_kind(
            force_mode=False,
            trigger_context={'source': 'cron'},
        )
        == 'scheduled'
    )
    assert (
        mod._resolve_daily_brief_trigger_kind(
            force_mode=False,
            trigger_context={},
        )
        == 'manual'
    )
    assert (
        mod._resolve_daily_brief_trigger_kind(
            force_mode=True,
            trigger_context={'source': 'cron'},
        )
        == 'force'
    )


def test_main_scheduler_no_scan_enters_daily_brief_delivery_only_without_workspace(monkeypatch, tmp_path) -> None:
    import json
    from zoneinfo import ZoneInfo

    from domain.domain.engine import SchedulerDecisionView
    from src.application import multi_account_tick as mod
    from src.application.tick_guard_flow import TickGuardOutcome
    from src.application.tick_scheduler_context import TickSchedulerContext, TickSchedulerOutcome

    cfg = tmp_path / "config.us.json"
    cfg.write_text(
        json.dumps(
            {
                "_generated": {"schema_version": "1.0", "generator": "options-monitor", "source_format": "yaml", "market": "us"},
                "accounts": ["lx"],
                "symbols": [{"symbol": "NVDA", "broker": "US"}],
                "schedule": {"enabled": True},
                "notifications": {"daily_brief": {"enabled": True}},
                "portfolio": {},
            }
        ),
        encoding="utf-8",
    )
    runtime_root = tmp_path / "runtime"
    captured = {}

    class _RunLogger:
        def __init__(self, _base):
            self.run_id = "run-delivery-only"

        def safe_event(self, *_args, **_kwargs):
            pass

    def guard(request):
        return TickGuardOutcome(True, 0, request.base_cfg, request.accounts, request.default_account, ZoneInfo("Asia/Shanghai"))

    decision = {
        "schema_kind": "scheduler_decision",
        "schema_version": "1.0",
        "should_run_scan": False,
        "is_notify_window_open": False,
        "in_run_window": True,
        "now_market": "2026-07-21T14:10:00-04:00",
        "reason": "当前没有待执行运行点。",
    }

    def scheduler(_request):
        return TickSchedulerOutcome(
            True,
            0,
            TickSchedulerContext(
                markets_to_run=["US"],
                scheduler_markets=["US"],
                state_path=runtime_root / "output_shared/state/scheduler_state.json",
                scheduler_schedule_key="schedule",
                scheduler_ms=1,
                scheduler_decision=decision,
                scheduler_view=SchedulerDecisionView.from_payload(decision),
                notify_decision_by_account={},
                scan_decision_by_account={"lx": {"should_run": False, "reason": decision["reason"], "scheduler_decision": decision}},
                should_run_global=False,
                reason_global=decision["reason"],
            ),
            [],
        )

    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setenv("OM_TRIGGER_SOURCE", "cron")
    monkeypatch.setattr(mod, "RunLogger", _RunLogger)
    monkeypatch.setattr(mod, "resolve_config_contract", lambda *args, **kwargs: {})
    monkeypatch.setattr(mod, "ensure_runtime_canonical_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod, "ensure_runtime_schedule_matches_market", lambda *args, **kwargs: {"market": ""})
    monkeypatch.setattr(mod.state_repo, "claim_idempotency_record", lambda *args, **kwargs: {"claimed": True})
    monkeypatch.setattr(mod, "run_tick_guard_flow", guard)
    monkeypatch.setattr(mod, "build_tick_scheduler_context", scheduler)
    monkeypatch.setattr(mod, "prepare_tick_run_workspace", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("no workspace")))
    monkeypatch.setattr(mod, "run_tick_account_execution", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no pipeline")))

    def notification(request):
        captured["request"] = request
        return 0

    monkeypatch.setattr(mod, "run_tick_notification_flow", notification)
    assert mod.main(["--config", str(cfg), "--accounts", "lx"]) == 0
    assert captured["request"].delivery_only is True
    assert captured["request"].account_ids == ("lx",)
    assert not (runtime_root / "output_runs").exists()


def test_duplicate_unsupported_tick_failure_returns_nonzero_without_rerun(monkeypatch, tmp_path) -> None:
    import json

    from src.application import multi_account_tick as mod

    cfg = tmp_path / "config.us.json"
    cfg.write_text(
        json.dumps(
            {
                "_generated": {
                    "schema_version": "1.0",
                    "generator": "options-monitor",
                    "source_format": "yaml",
                    "market": "us",
                },
                "accounts": ["lx"],
                "symbols": [],
                "schedule": {"enabled": True},
                "portfolio": {},
            }
        ),
        encoding="utf-8",
    )
    runtime_root = tmp_path / "runtime"
    events: list[dict] = []

    class _RunLogger:
        def __init__(self, _base):
            self.run_id = "run-duplicate"

        def safe_event(self, step, status, **kwargs):
            events.append({"step": step, "status": status, **kwargs})

    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setenv("OM_TRIGGER_SOURCE", "cron")
    monkeypatch.setattr(mod, "RunLogger", _RunLogger)
    monkeypatch.setattr(mod, "resolve_config_contract", lambda *args, **kwargs: {})
    monkeypatch.setattr(mod, "ensure_runtime_canonical_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod, "ensure_runtime_schedule_matches_market", lambda *args, **kwargs: {"market": ""})
    monkeypatch.setattr(
        mod.state_repo,
        "claim_idempotency_record",
        lambda *args, **kwargs: {
            "claimed": False,
            "record": {
                "status": "unsupported_failed",
                "error_code": "daily_brief_multi_market_delivery_unsupported",
            },
        },
    )
    monkeypatch.setattr(
        mod,
        "run_tick_guard_flow",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("terminal duplicate must not rerun")),
    )

    assert mod.main(["--config", str(cfg), "--accounts", "lx", "--market-config", "all"]) == 2
    assert any(
        event.get("step") == "run_end"
        and event.get("status") == "error"
        and event.get("error_code") == "daily_brief_multi_market_delivery_unsupported"
        for event in events
    )


def test_terminal_idempotency_write_failure_is_not_silently_swallowed(monkeypatch, tmp_path) -> None:
    import json

    import pytest

    from src.application import multi_account_tick as mod

    cfg = tmp_path / "config.us.json"
    cfg.write_text(
        json.dumps(
            {
                "_generated": {
                    "schema_version": "1.0",
                    "generator": "options-monitor",
                    "source_format": "yaml",
                    "market": "us",
                },
                "accounts": ["lx"],
                "symbols": [],
                "schedule": {"enabled": True},
                "portfolio": {},
            }
        ),
        encoding="utf-8",
    )
    events: list[dict] = []

    class _RunLogger:
        def __init__(self, _base):
            self.run_id = "run-terminal-write-failure"

        def safe_event(self, step, status, **kwargs):
            events.append({"step": step, "status": status, **kwargs})

    def _guard(request):
        request.complete_tick_idempotency_fn(
            status="unsupported_failed",
            message="daily_brief_multi_market_delivery_unsupported",
            ok=False,
            error_code="daily_brief_multi_market_delivery_unsupported",
        )
        raise AssertionError("terminal completion failure must escape")

    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setenv("OM_TRIGGER_SOURCE", "cron")
    monkeypatch.setattr(mod, "RunLogger", _RunLogger)
    monkeypatch.setattr(mod, "resolve_config_contract", lambda *args, **kwargs: {})
    monkeypatch.setattr(mod, "ensure_runtime_canonical_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod, "ensure_runtime_schedule_matches_market", lambda *args, **kwargs: {"market": ""})
    monkeypatch.setattr(mod.state_repo, "claim_idempotency_record", lambda *args, **kwargs: {"claimed": True})
    monkeypatch.setattr(mod, "_complete_tick_idempotency", lambda **_kwargs: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(mod, "run_tick_guard_flow", _guard)

    with pytest.raises(OSError, match="disk full"):
        mod.main(["--config", str(cfg), "--accounts", "lx"])

    assert any(
        event.get("step") == "idempotency"
        and event.get("status") == "error"
        and event.get("error_code") == "TICK_IDEMPOTENCY_TERMINAL_WRITE_FAILED"
        for event in events
    )
