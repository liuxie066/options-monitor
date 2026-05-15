from __future__ import annotations

import json
import subprocess
from pathlib import Path


def _write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


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
        preflight_config_fn=None,
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
        preflight_config_fn=None,
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
        preflight_config_fn=None,
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
        preflight_config_fn=None,
        environ={},
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert captured.err.strip() == "EXEC_FAILED_RC_1"


def test_run_tick_cron_preflight_rejects_config_missing_generation_metadata(tmp_path, capsys) -> None:
    from src.application.tick_cron import run_tick_cron

    config = _write_json(
        tmp_path / "config.hk.json",
        {
            "schedule": {
                "timezone": "Asia/Hong_Kong",
                "run_window": {"start": "09:30", "end": "16:00", "breaks": []},
            }
        },
    )

    rc = run_tick_cron(
        market="hk",
        config_path=str(config),
        lock_path=str(tmp_path / "tick.lock"),
        run_cmd=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not run")),
        environ={},
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "[CONFIG_ERROR] runtime config is missing generation metadata" in captured.err
    assert "rebuild: ./om config build --market hk" in captured.err


def test_run_tick_cron_allow_stale_config_forwards_emergency_override(tmp_path) -> None:
    from src.application.tick_cron import run_tick_cron

    calls: list[dict] = []

    def _run_cmd(command, **kwargs):
        calls.append({"command": command, **kwargs})
        return subprocess.CompletedProcess(command, 0)

    rc = run_tick_cron(
        market="hk",
        lock_path=str(tmp_path / "tick.lock"),
        run_cmd=_run_cmd,
        allow_stale_config=True,
        environ={},
    )

    assert rc == 0
    assert calls[0]["command"][-1] == "--allow-stale-config"
