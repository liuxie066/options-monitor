from __future__ import annotations

import json
import subprocess
import sys
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


def test_build_tick_cron_plan_symbol_scope_forces_no_send() -> None:
    from src.application.tick_cron import build_tick_cron_plan

    plan = build_tick_cron_plan(market="us", accounts=["sy"], symbols=["PDD"], no_send=False)

    assert plan.symbols == ["PDD"]
    assert plan.trigger_env["OM_TRIGGER_SOURCE"] == "diagnostic"
    assert plan.tick_argv == [
        "./om",
        "run",
        "tick",
        "--config",
        "config.us.json",
        "--market-config",
        "us",
        "--accounts",
        "sy",
        "--symbols",
        "PDD",
        "--no-send",
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


def test_run_tick_cron_seals_current_market_expectation_before_tick(tmp_path) -> None:
    from src.application.tick_cron import run_tick_cron

    calls: list[str] = []

    def _seal(runtime_root, **kwargs):
        calls.append("seal")
        assert runtime_root == tmp_path
        assert kwargs["profile"] == {
            "markets": ["us"],
            "accounts": ["user1", "user2"],
            "config_paths": {"us": str(tmp_path / "config.us.json")},
        }
        assert kwargs["artifact_root"] == (tmp_path / "output_shared" / "research" / "strategy_lab")
        return {"status": "ok", "results": []}

    def _run_cmd(command, **kwargs):
        calls.append("tick")
        return subprocess.CompletedProcess(command, 0)

    rc = run_tick_cron(
        market="us",
        accounts=["user1", "user2"],
        config_path=str(tmp_path / "config.us.json"),
        lock_path=str(tmp_path / "tick.lock"),
        run_cmd=_run_cmd,
        preflight_config_fn=None,
        seal_formal_expectations_fn=_seal,
        environ={"OM_RUNTIME_ROOT": str(tmp_path)},
    )

    assert rc == 0
    assert calls == ["seal", "tick"]


def test_run_tick_cron_seals_configured_accounts_when_cli_scope_omitted(tmp_path) -> None:
    from src.application.tick_cron import run_tick_cron

    config_path = _write_json(
        tmp_path / "config.us.json", {"accounts": ["user1", "user2"]}
    )
    profiles: list[dict] = []

    def _seal(_runtime_root, **kwargs):
        profiles.append(kwargs["profile"])
        return {"status": "ok", "results": []}

    rc = run_tick_cron(
        market="us",
        config_path=str(config_path),
        lock_path=str(tmp_path / "tick.lock"),
        run_cmd=lambda command, **_kwargs: subprocess.CompletedProcess(command, 0),
        preflight_config_fn=None,
        seal_formal_expectations_fn=_seal,
        environ={"OM_RUNTIME_ROOT": str(tmp_path)},
    )

    assert rc == 0
    assert profiles == [
        {
            "markets": ["us"],
            "accounts": ["user1", "user2"],
            "config_paths": {"us": str(config_path)},
        }
    ]


def test_formal_expectation_failure_does_not_block_tick(tmp_path, capsys) -> None:
    from src.application.tick_cron import run_tick_cron

    rc = run_tick_cron(
        market="hk",
        accounts=["lx"],
        config_path=str(tmp_path / "config.hk.json"),
        lock_path=str(tmp_path / "tick.lock"),
        run_cmd=lambda command, **_kwargs: subprocess.CompletedProcess(command, 0),
        preflight_config_fn=None,
        seal_formal_expectations_fn=lambda *_args, **_kwargs: {
            "status": "degraded",
            "results": [],
        },
        environ={"OM_RUNTIME_ROOT": str(tmp_path)},
    )

    assert rc == 0
    assert capsys.readouterr().err.strip() == "FORMAL_EXPECTATION_DEGRADED"


def test_default_formal_expectation_import_failure_does_not_block_tick(monkeypatch, tmp_path, capsys) -> None:
    from src.application.tick_cron import run_tick_cron

    calls: list[list[str]] = []
    monkeypatch.setitem(sys.modules, "src.application.research.formal_corpus", None)

    rc = run_tick_cron(
        market="hk",
        accounts=["lx"],
        config_path=str(tmp_path / "config.hk.json"),
        lock_path=str(tmp_path / "tick.lock"),
        run_cmd=lambda command, **_kwargs: calls.append(command) or subprocess.CompletedProcess(command, 0),
        preflight_config_fn=None,
        environ={"OM_RUNTIME_ROOT": str(tmp_path)},
    )

    assert rc == 0
    assert len(calls) == 1
    assert capsys.readouterr().err.strip() == "FORMAL_EXPECTATION_DEGRADED_formal_expectation_failed"


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


def test_default_tick_process_uses_session_and_terminates_process_group(
    monkeypatch,
) -> None:
    import src.application.tick_cron as mod

    calls: list[tuple] = []

    class FakeProcess:
        pid = 4321

        def __init__(self) -> None:
            self.wait_count = 0

        def wait(self, timeout=None):
            self.wait_count += 1
            if self.wait_count <= 2:
                raise subprocess.TimeoutExpired("tick", timeout)
            return -9

    fake = FakeProcess()
    monkeypatch.setattr(
        mod.subprocess,
        "Popen",
        lambda *args, **kwargs: calls.append(("popen", args, kwargs)) or fake,
    )
    monkeypatch.setattr(
        mod.os,
        "killpg",
        lambda pid, sig: calls.append(("killpg", pid, sig)),
    )

    try:
        mod._run_tick_process_group(
            command=["./om", "run", "tick"],
            cwd=None,
            env={},
            timeout_seconds=1,
            terminate_grace_seconds=0.1,
        )
        raise AssertionError("expected timeout")
    except subprocess.TimeoutExpired:
        pass

    assert calls[0][0] == "popen"
    assert calls[0][2]["start_new_session"] is True
    assert ("killpg", 4321, mod.signal.SIGTERM) in calls
    assert ("killpg", 4321, mod.signal.SIGKILL) in calls


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
    assert "rebuild: ./om config build --source yaml --market hk" in captured.err


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


def test_scan_scheduler_external_adapter_forwards_force_flag(monkeypatch, tmp_path) -> None:
    from pathlib import Path

    import src.infrastructure.external_services as mod

    calls = []
    monkeypatch.setattr(
        mod,
        'run_command',
        lambda cmd, **kwargs: calls.append((cmd, kwargs)) or type('Result', (), {'returncode': 0})(),
    )

    mod.run_scan_scheduler_cli(
        vpy=Path('python3'),
        base=tmp_path,
        config=tmp_path / 'config.us.json',
        state=tmp_path / 'state.json',
        force=True,
    )

    assert '--force' in calls[0][0]
