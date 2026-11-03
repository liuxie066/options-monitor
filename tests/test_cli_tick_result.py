from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.application import daily_decision_brief_repository as repo
from src.application import multi_account_tick as tick
from src.interfaces.cli import main as cli
from src.interfaces.cli import run_ops


def _brief(run_id, *, status="ready", actionability="live_actionable", gaps=None):
    return {
        "schema_version": "daily_decision_brief.v1", "account": "lx", "market": "US",
        "market_trading_date": "2026-07-21", "run_id": run_id, "revision": 0,
        "generated_at_utc": "2026-07-21T14:00:00+00:00",
        "data_as_of_utc": "2026-07-21T13:59:00+00:00",
        "valid_until_utc": "2026-07-21T20:00:00+00:00",
        "status": status, "actionability": actionability, "actions": [], "positions": [],
        "capacity": {}, "candidates": {}, "rejections": {}, "events": [],
        "data_gaps": list(gaps or []), "source_artifacts": [],
    }


def _result(tmp_path, **kwargs):
    return tick.TickExecutionResult(
        run_created=True, run_id="current", runtime_root=tmp_path,
        config_path=tmp_path / "config.us.json", accounts=("lx",), market="US", **kwargs,
    )


def _dispatch():
    return cli.main(["run", "tick", "--config", "config.us.json", "--no-send", "--force"])


@pytest.mark.parametrize("kind,rc", [("zero", 0), ("partial", 0), ("failed", 7), ("failed", 0), ("missing", 4), ("missing", 0), ("corrupt", 0)])
def test_human_tick_displays_only_this_run_with_existing_renderer(monkeypatch, tmp_path, capsys, kind, rc):
    repo.persist_daily_decision_brief_success(base=tmp_path, brief=_brief("old-success"))
    if kind in {"zero", "partial", "corrupt"}:
        gaps = [{"scope": "strategy", "market": "US", "symbol": "NVDA", "strategy_family": "sell_put",
                 "reason": "quote_unavailable"}] if kind == "partial" else []
        saved = repo.persist_daily_decision_brief_success(
            base=tmp_path, brief=_brief("current", status="degraded" if gaps else "ready", gaps=gaps),
        )
        if kind == "corrupt":
            saved["paths"]["run_brief"].write_text("{")
    if kind == "failed":
        repo.persist_daily_decision_brief_failure(
            base=tmp_path, brief=_brief("current", status="blocked", actionability="blocked"),
        )
    # Advancing current after the invocation must not replace its result.
    repo.persist_daily_decision_brief_success(base=tmp_path, brief=_brief("newer-success"))
    monkeypatch.setattr(tick, "_CURRENT_RUN_ID", "newer-success")
    monkeypatch.setattr(run_ops, "execute_tick", lambda _argv: _result(tmp_path, rc=rc))
    rendered = []
    original = run_ops.render_query_brief

    def render(brief, **kwargs):
        rendered.append(brief)
        return original(brief, **kwargs)

    monkeypatch.setattr(run_ops, "render_query_brief", render)
    expected = rc or (2 if kind in {"failed", "missing", "corrupt"} else 0)
    assert _dispatch() == expected
    output = capsys.readouterr().out
    assert "运行 current" in output
    assert "old-success" not in output
    assert "newer-success" not in output
    if kind in {"missing", "corrupt"}:
        assert "本次结果来源不可用" in output
        assert not rendered
    else:
        assert rendered[0]["run_id"] == "current"
        # Historical/expired live snapshots must use the existing freshness rule.
        assert rendered[0]["actionability"] == ("blocked" if kind == "failed" else "planning_only")
        assert "年化非收益承诺" in output
        assert "估算非成交" in output
        if kind == "failed":
            assert "本次扫描失败" in output
        elif kind == "partial":
            assert rendered[0]["status"] == "degraded"
            assert rendered[0]["data_gaps"]
            assert "部分数据不可用" in output
        else:
            assert "本轮暂无符合条件的候选" in output


def test_human_tick_no_run_does_not_read_history(monkeypatch, capsys):
    monkeypatch.setattr(run_ops, "execute_tick", lambda _argv: tick.TickExecutionResult(rc=2, error="bad config"))
    monkeypatch.setattr(run_ops, "read_run_daily_decision_brief", lambda **_kw: pytest.fail("no run has no source"))
    assert _dispatch() == 2
    output = capsys.readouterr().out
    assert "本次未创建运行" in output
    assert "bad config" in output


def test_human_tick_smoke_keeps_execution_rc_without_a_brief(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(run_ops, "execute_tick", lambda _argv: _result(tmp_path, rc=0))
    assert cli.main(["run", "tick", "--config", "config.us.json", "--no-send", "--smoke"]) == 0
    assert "本次结果来源不可用" in capsys.readouterr().out


@pytest.mark.parametrize("completed,rc", [(True, 0), (False, 2), (False, 0)])
def test_human_tick_experience_is_explicit_and_never_reads_ordinary_brief(monkeypatch, tmp_path, capsys, completed, rc):
    monkeypatch.setattr(run_ops, "execute_tick", lambda _argv: _result(
        tmp_path, rc=rc, experience=True, experience_completed=completed,
    ))
    monkeypatch.setattr(run_ops, "read_run_daily_decision_brief", lambda **_kw: pytest.fail("experience has no ordinary brief"))
    assert _dispatch() == (rc or (0 if completed else 2))
    output = capsys.readouterr().out
    assert ("模拟体验完成" if completed else "模拟体验失败／未完成") in output
    assert "不代表真实账户容量" in output
    assert "不生成普通决策简报" in output


def test_cli_default_executor_to_repository_and_renderer_without_external_effects(monkeypatch, tmp_path, capsys):
    config = tmp_path / "config.us.json"
    config.write_text(json.dumps({
        "_generated": {"schema_version": "1.0", "market": "us", "source_format": "yaml", "generator": "options-monitor"},
        "accounts": ["lx"], "symbols": [], "portfolio": {}, "schedule": {"enabled": True},
    }))
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setattr(tick, "resolve_config_contract", lambda *_a, **_kw: {})
    monkeypatch.setattr(tick, "ensure_runtime_canonical_config", lambda *_a, **_kw: None)
    monkeypatch.setattr(tick, "ensure_runtime_schedule_matches_market", lambda *_a, **_kw: {"market": "us"})
    monkeypatch.setattr(tick, "ensure_runtime_config_freshness", lambda *_a, **_kw: {})

    class Log:
        run_id = "facade-run"
        def __init__(self, _base):
            pass
        def safe_event(self, *_a, **_kw):
            pass

    def guard(request):
        repo.persist_daily_decision_brief_success(base=request.base, brief=_brief(request.runlog.run_id))
        return SimpleNamespace(should_continue=False, return_code=0)

    monkeypatch.setattr(tick, "RunLogger", Log)
    monkeypatch.setattr(tick, "run_tick_guard_flow", guard)
    monkeypatch.setattr(tick, "run_tick_account_execution", lambda *_a, **_kw: pytest.fail("no real scan"))
    monkeypatch.setattr(tick, "run_tick_notification_flow", lambda *_a, **_kw: pytest.fail("no send"))
    assert cli.main(["run", "tick", "--config", str(config), "--no-send"]) == 0
    output = capsys.readouterr().out
    assert "运行 facade-run" in output
    assert "本轮暂无符合条件的候选" in output
    assert "本次结果来源不可用" not in output


@pytest.mark.parametrize("kind,rc,skipped,expected", [
    ("missing", 0, ("lx",), 0),
    ("missing", 0, ("sy",), 2),
    ("missing", 0, (), 2),
    ("missing", 7, ("lx",), 7),
    ("corrupt", 0, ("lx",), 2),
    ("failed", 0, ("lx",), 2),
])
def test_scheduler_skip_only_exempts_missing_result_for_exact_successful_account(
    monkeypatch, tmp_path, capsys, kind, rc, skipped, expected,
):
    if kind == "corrupt":
        saved = repo.persist_daily_decision_brief_success(base=tmp_path, brief=_brief("current"))
        saved["paths"]["run_brief"].write_text("{")
    elif kind == "failed":
        repo.persist_daily_decision_brief_failure(
            base=tmp_path, brief=_brief("current", status="blocked", actionability="blocked"),
        )
    monkeypatch.setattr(run_ops, "execute_tick", lambda _argv: _result(
        tmp_path, rc=rc, scheduler_skipped_accounts=skipped,
    ))
    assert _dispatch() == expected
    output = capsys.readouterr().out
    assert ("调度跳过" in output) is (expected == 0)
