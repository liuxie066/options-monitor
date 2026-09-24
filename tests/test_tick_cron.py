from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from src.application.config_yaml import build_yaml_runtime_config_file


def _write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _tick(tmp_path: Path, run_cmd, *, market: str = "hk", **overrides):
    """Call run_tick_cron with this file's shared lock path and empty environ."""
    from src.application.tick_cron import run_tick_cron

    return run_tick_cron(
        market=market,
        lock_path=str(tmp_path / "tick.lock"),
        runtime_root=tmp_path,
        run_cmd=run_cmd,
        environ={},
        **overrides,
    )


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
        environ={"OM_RUNTIME_ROOT": str(tmp_path)},
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
    assert len(calls) == 1
    assert not (tmp_path / "output_shared").exists()


def test_run_tick_cron_reports_locked_without_running(monkeypatch, tmp_path, capsys) -> None:
    import src.application.tick_cron as mod

    def _locked(*_args, **_kwargs):
        raise BlockingIOError("locked")

    monkeypatch.setattr(mod.fcntl, "flock", _locked)

    rc = _tick(
        tmp_path,
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not run")),
        preflight_config_fn=None,
    )

    assert rc == 0
    assert capsys.readouterr().out.strip() == "SKIP_LOCKED"


def test_run_tick_cron_reports_timeout(tmp_path, capsys) -> None:
    def _timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    rc = _tick(tmp_path, _timeout, preflight_config_fn=None)

    assert rc == 124
    stderr = capsys.readouterr().err
    assert "<3>SYSTEM_ALERT_UNCONFIRMED TICK_TIMEOUT" in stderr
    assert stderr.strip().endswith("<3>EXEC_TIMEOUT_RC_124")
    events = list((tmp_path / "output_runs").glob("*/state/audit_events.jsonl"))
    assert len(events) == 1
    event = json.loads(events[0].read_text(encoding="utf-8").splitlines()[0])
    assert event["error_code"] == "TICK_TIMEOUT"
    assert event["extra"]["stage"] == "timeout"
    assert event["extra"]["rc"] == 124


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
    def _failed(command, **_kwargs):
        return subprocess.CompletedProcess(command, 1)

    rc = _tick(tmp_path, _failed, preflight_config_fn=None)

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "<3>SYSTEM_ALERT_UNCONFIRMED TICK_EXEC_FAILED" in captured.err
    assert captured.err.strip().endswith("<3>EXEC_FAILED_RC_1")
    events = list((tmp_path / "output_runs").glob("*/state/audit_events.jsonl"))
    assert len(events) == 1
    event = json.loads(events[0].read_text(encoding="utf-8").splitlines()[0])
    assert event["extra"]["failure_code"] == "TICK_EXEC_FAILED"
    assert event["extra"]["stage"] == "child_exit"
    assert event["extra"]["trigger_source"] == "cron"
    assert event["extra"]["rc"] == 1
    assert event["extra"]["first_error_at"]
    assert event["extra"]["opend_login_state"] == "unknown"


def test_tick_cron_clears_failure_only_with_matching_full_completion(tmp_path) -> None:
    from src.application.tick_cron import run_tick_cron

    latest = tmp_path / "output_shared" / "state" / "current" / "tick_cron_last_result.hk.current.json"
    config = _write_json(tmp_path / "config.hk.json", {"accounts": ["lx"]})

    def _failed(command, **_kwargs):
        return subprocess.CompletedProcess(command, 2)

    assert run_tick_cron(
        market="hk", accounts=["lx"], lock_path=str(tmp_path / "tick.lock"),
        runtime_root=tmp_path, run_cmd=_failed, preflight_config_fn=None, environ={},
    ) == 2
    assert json.loads(latest.read_text())["status"] == "failed"

    def _skipped(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0)

    assert run_tick_cron(
        market="hk", accounts=["lx"], lock_path=str(tmp_path / "tick.lock"),
        runtime_root=tmp_path, run_cmd=_skipped, preflight_config_fn=None, environ={},
    ) == 0
    assert json.loads(latest.read_text())["status"] == "failed"

    def _completed(command, **kwargs):
        wrapper_id = kwargs["env"]["OM_TICK_CRON_RUN_ID"]
        receipt = tmp_path / "output_runs" / wrapper_id / "state" / "child_tick_completion.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({"status": "ok", "wrapper_run_id": wrapper_id,
                                       "inner_run_id": "child-1", "market": "hk", "accounts": ["lx"]}))
        return subprocess.CompletedProcess(command, 0)

    assert run_tick_cron(
        market="hk", accounts=["lx"], lock_path=str(tmp_path / "tick.lock"),
        runtime_root=tmp_path, run_cmd=_completed, preflight_config_fn=None, environ={},
        config_path=str(config),
    ) == 0
    assert json.loads(latest.read_text())["status"] == "ok"

    _write_json(config, {"accounts": ["lx", "sy"]})
    latest.write_text(json.dumps({"status": "failed", "error_code": "TICK_EXEC_FAILED"}))
    assert run_tick_cron(
        market="hk", accounts=["lx"], lock_path=str(tmp_path / "tick.lock"),
        runtime_root=tmp_path, run_cmd=_completed, preflight_config_fn=None, environ={},
        config_path=str(config),
    ) == 0
    assert json.loads(latest.read_text())["status"] == "failed"


def test_tick_cron_shared_audit_failure_marks_evidence_incomplete(monkeypatch, tmp_path, capsys) -> None:
    from src.application import tick_cron

    def _unwritable(*_args, **_kwargs):
        raise OSError("shared audit unavailable")

    monkeypatch.setattr(tick_cron.state_repo, "append_shared_audit_jsonl", _unwritable)
    rc = _tick(tmp_path, lambda command, **_kwargs: subprocess.CompletedProcess(command, 2),
               preflight_config_fn=None)

    assert rc == 2
    latest = tmp_path / "output_shared" / "state" / "current" / "tick_cron_last_result.hk.current.json"
    assert json.loads(latest.read_text())["status"] == "evidence_incomplete"
    assert "FAILURE_RECORD_WRITE_FAILED stages=shared" in capsys.readouterr().err


def test_run_tick_cron_preflight_rejects_config_missing_generation_metadata(tmp_path, capsys) -> None:
    config = _write_json(
        tmp_path / "config.hk.json",
        {
            "schedule": {
                "timezone": "Asia/Hong_Kong",
                "run_window": {"start": "09:30", "end": "16:00", "breaks": []},
            }
        },
    )

    rc = _tick(
        tmp_path,
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not run")),
        config_path=str(config),
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "[CONFIG_ERROR] runtime config is missing generation metadata" in captured.err
    assert "rebuild: ./om config build --source yaml --market hk" in captured.err


def test_run_tick_cron_allow_stale_config_forwards_emergency_override(tmp_path) -> None:
    calls: list[dict] = []

    def _run_cmd(command, **kwargs):
        calls.append({"command": command, **kwargs})
        return subprocess.CompletedProcess(command, 0)

    rc = _tick(tmp_path, _run_cmd, allow_stale_config=True)

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


@pytest.mark.parametrize("market,symbol", [("us", "NVDA"), ("hk", "0700.HK")])
@pytest.mark.parametrize("invalid_yaml", [False, True], ids=["assistant-only", "invalid-yaml"])
def test_tick_cron_real_preflight_after_yaml_edit(
    tmp_path: Path, capsys, market: str, symbol: str, invalid_yaml: bool,
) -> None:
    source = tmp_path / "config.yaml"
    original = (
        "accounts:\n  lx:\n    type: futu\n    futu_account_id: '12345678'\n"
        f"markets:\n  {market}:\n    accounts: [lx]\n    symbols: [{symbol}]\n"
    )
    source.write_text(original, encoding="utf-8")
    runtime = tmp_path / f"config.{market}.json"
    build_yaml_runtime_config_file(
        repo_root=Path(__file__).resolve().parents[1], market=market,
        config_path=source, output_config_path=runtime,
    )
    source.write_text(
        "markets: [PRIVATE_CONFIG_VALUE\n" if invalid_yaml
        else original + "assistant:\n  enabled: false\n  context_window_messages: 8\n",
        encoding="utf-8",
    )
    calls = []

    def run_command(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    rc = _tick(tmp_path, run_command, market=market, config_path=str(runtime), no_send=True)

    captured = capsys.readouterr()
    if invalid_yaml:
        assert rc == 1
        assert calls == []
        assert "[CONFIG_ERROR]" in captured.err
        assert f"rebuild: ./om config build --source yaml --market {market}" in captured.err
        assert "Traceback" not in captured.err
        assert "PRIVATE_CONFIG_VALUE" not in captured.err
    else:
        assert rc == 0
        assert len(calls) == 1
        assert "--no-send" in calls[0][0]
        assert captured.err == ""
