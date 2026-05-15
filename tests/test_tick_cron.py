from __future__ import annotations

import subprocess


def test_build_tick_cron_plan_sets_hk_defaults() -> None:
    from src.application.tick_cron import build_tick_cron_plan

    plan = build_tick_cron_plan(market="hk", accounts=["lx", "sy"], timeout_seconds=600)

    assert plan.config_path == "config.hk.json"
    assert plan.lock_path == "/tmp/om-tick-hk.lock"
    assert plan.trigger_env["OM_TRIGGER_SOURCE"] == "cron"
    assert plan.trigger_env["OM_TRIGGER_JOB_ID"] == "om-tick-hk"
    assert plan.trigger_env["OM_TRIGGER_TIMEZONE"] == "Asia/Hong_Kong"
    assert plan.trigger_env["OM_TIMEOUT_SECONDS"] == "600"
    assert plan.tick_argv == [
        "./om",
        "run",
        "tick",
        "--config",
        "config.hk.json",
        "--market-config",
        "hk",
        "--accounts",
        "lx",
        "sy",
    ]


def test_run_tick_cron_invokes_tick_with_trigger_environment(tmp_path) -> None:
    from src.application.tick_cron import run_tick_cron

    calls: list[dict] = []

    def _run_cmd(command, **kwargs):
        calls.append({"command": command, **kwargs})
        return subprocess.CompletedProcess(command, 0)

    rc = run_tick_cron(
        market="us",
        accounts=["lx"],
        timeout_seconds=700,
        lock_path=str(tmp_path / "tick.lock"),
        run_cmd=_run_cmd,
        environ={},
    )

    assert rc == 0
    assert calls[0]["command"] == [
        "./om",
        "run",
        "tick",
        "--config",
        "config.us.json",
        "--market-config",
        "us",
        "--accounts",
        "lx",
    ]
    assert calls[0]["timeout"] == 700
    assert calls[0]["env"]["OM_TRIGGER_SOURCE"] == "cron"
    assert calls[0]["env"]["OM_TRIGGER_JOB_ID"] == "om-tick-us"
    assert calls[0]["env"]["OM_TRIGGER_TIMEZONE"] == "America/New_York"
    assert calls[0]["env"]["OM_TIMEOUT_SECONDS"] == "700"


def test_run_tick_cron_reports_locked_without_running(monkeypatch, tmp_path, capsys) -> None:
    import src.application.tick_cron as mod

    def _locked(*_args, **_kwargs):
        raise BlockingIOError("locked")

    monkeypatch.setattr(mod.fcntl, "flock", _locked)

    rc = mod.run_tick_cron(
        market="hk",
        lock_path=str(tmp_path / "tick.lock"),
        run_cmd=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not run")),
        environ={},
    )

    assert rc == 0
    assert capsys.readouterr().out.strip() == "SKIP_LOCKED"


def test_run_tick_cron_reports_timeout(tmp_path, capsys) -> None:
    from src.application.tick_cron import run_tick_cron

    def _timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    rc = run_tick_cron(
        market="hk",
        lock_path=str(tmp_path / "tick.lock"),
        run_cmd=_timeout,
        environ={},
    )

    assert rc == 124
    assert capsys.readouterr().err.strip() == "EXEC_TIMEOUT_RC_124"


def test_run_tick_cron_reports_process_failure_distinct_from_lock(tmp_path, capsys) -> None:
    from src.application.tick_cron import run_tick_cron

    def _failed(command, **_kwargs):
        return subprocess.CompletedProcess(command, 1)

    rc = run_tick_cron(
        market="hk",
        lock_path=str(tmp_path / "tick.lock"),
        run_cmd=_failed,
        environ={},
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert captured.err.strip() == "EXEC_FAILED_RC_1"
