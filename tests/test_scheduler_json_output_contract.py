from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from domain.domain.config_contract import ensure_runtime_schedule_matches_market
from src.application import scan_scheduler as scheduler
from src.application import tick_scheduler_context as context
from src.application.config_validator import validate_schedule_cfg
from src.application.multi_tick_audit import MultiTickAuditHelper
from src.application.scheduled_notification import build_multi_tick_scheduler_decision


class _Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 10, 16, 14, 0, tzinfo=timezone.utc)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


@pytest.mark.parametrize("enabled,force,market,trading", [
    (False, False, "us", True),
    (False, False, "auto", True),
    (True, False, "us", True),
    (False, True, "us", True),
    (True, True, "us", True),
    (False, False, "us", False),
])
def test_scheduler_stdout_reaches_real_tick_context(
    tmp_path, monkeypatch, enabled, force, market, trading,
):
    cfg = {"schedule": {"enabled": enabled, "timezone": "America/New_York"}}
    config = tmp_path / "config.us.json"
    config.write_text(json.dumps(cfg), encoding="utf-8")
    state = tmp_path / "state.json"
    validate_schedule_cfg(cfg["schedule"], "schedule")
    assert ensure_runtime_schedule_matches_market(
        cfg, config_path=config, market_config=market,
    )["validated"]
    calls, events, snapshots = [], [], []
    runlog = SimpleNamespace(safe_event=lambda *a, **kw: events.append((a, kw)))
    audit = SimpleNamespace(
        runlog=runlog,
        audit=lambda *a, **kw: events.append((a, kw)),
        build_failure_audit_fields=lambda **kw: {},
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected scheduler state mutation or failure")

    audit.guard_mark_failure = forbidden
    audit.fail_schema_validation = lambda **kw: MultiTickAuditHelper.fail_schema_validation(audit, **kw)

    def bridge(**kwargs):
        assert kwargs["jsonl"] is True and kwargs["capture_output"] is True
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            payload = scheduler.run_scheduler(
                config=kwargs["config"], state=kwargs["state"], state_dir=tmp_path / "state_dir",
                schedule_key=kwargs["schedule_key"], jsonl=kwargs["jsonl"],
                force=kwargs["force"], base_dir=tmp_path,
            )
        assert json.loads(out.getvalue()) == payload
        assert len(out.getvalue().splitlines()) == 1
        calls.append(payload)
        return SimpleNamespace(returncode=0, stdout=out.getvalue(), stderr=err.getvalue())

    monkeypatch.setattr(scheduler, "datetime", _Clock)
    monkeypatch.setattr(context, "datetime", _Clock)
    monkeypatch.setattr(scheduler, "write_state", forbidden)
    monkeypatch.setattr(context.state_repo, "shared_state_dir", lambda base: tmp_path)
    monkeypatch.setattr(context.storage_paths, "shared_state_path", lambda base, name: state)
    monkeypatch.setattr(context, "_ensure_scheduler_state_file", lambda *args: None)
    monkeypatch.setattr(context, "_write_scheduler_snapshot", lambda request, result: snapshots.append(result))
    request = context.TickSchedulerRequest(
        vpy=Path("/unused/python"), base=tmp_path, cfg_path=config, base_cfg=cfg,
        accounts=["qa_alpha", "qa_beta"], market_config=market, force_mode=force,
        smoke=False, run_id="synthetic-scheduler", runlog=runlog, audit_helper=audit,
        check_trading_day_for_market=lambda selected: (trading, selected),
        run_scan_scheduler_cli_fn=bridge,
    )
    result = context.build_tick_scheduler_context(request)
    assert result.should_continue is True and result.return_code == 0
    assert result.context is not None
    assert result.context.should_run_global is bool(trading and (enabled or force))
    assert snapshots == [result.context]
    assert not state.exists()
    assert len(calls) == int(trading)
    if trading and not enabled and not force:
        assert result.context.reason_global == calls[0]["reason"] == "调度已禁用：不扫描、不通知。"
        for account in request.accounts:
            decision = result.context.scan_decision_by_account[account]
            assert decision["source"] == "account_scheduler"
            assert decision["reason"] == calls[0]["reason"]
            assert decision["should_run"] is False
            assert result.context.notify_decision_by_account[account].is_notify_window_open is False


def test_disabled_scheduler_keeps_human_info(tmp_path, capsys):
    config = tmp_path / "synthetic.json"
    config.write_text(json.dumps({"schedule": {"enabled": False}}), encoding="utf-8")
    payload = scheduler.run_scheduler(
        config=config, state_dir=tmp_path / "state_dir", base_dir=tmp_path,
    )
    output = capsys.readouterr().out
    assert payload["should_run_scan"] is False
    assert "[INFO] schedule disabled in config; no scheduling enforcement applied." in output


def test_scheduler_consumer_still_rejects_trailing_text():
    with pytest.raises(json.JSONDecodeError):
        build_multi_tick_scheduler_decision(
            scheduler_stdout='{}\n[INFO] invalid trailing text',
            as_of_utc="2026-10-16T14:00:00Z",
        )
