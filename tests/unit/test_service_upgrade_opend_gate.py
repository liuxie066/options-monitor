from __future__ import annotations

import inspect
import json
import subprocess
from pathlib import Path

from src.application import service_upgrade


SDK_LOG_LINE = (
    "2026-09-25 10:03:20,276 | 3695623 | 127673116840064 | "
    "[open_context_base.py:411] _init_connect_sync: New connect ready: "
    "conn=123 context=<futu.quote.open_quote_context.OpenQuoteContext>"
)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _profile(*, opend: bool = True, config_path: Path | None = None) -> dict:
    service = "options-monitor-opend-lx.service" if opend else "options-monitor-trade-intake.service"
    profile = {"service_provider": "systemd", "services": [{"name": service}],
               "opend": {"services": [{"service_name": service, "host": "127.0.0.1", "port": 11111}]}}
    if config_path:
        profile["config_paths"] = {"us": str(config_path)}
    return profile


def _gate(tmp_path: Path, profile: dict, clock: Clock, run_cmd):
    operations: list[dict] = []
    outcome = service_upgrade._post_upgrade_service_health(
        profile=profile, repo_root=tmp_path, run_cmd=run_cmd, operations=operations,
        monotonic_fn=clock.monotonic, sleep_fn=clock.sleep)
    return outcome, operations


def test_gate_waits_through_port_and_login_then_records_only_last_probe(tmp_path: Path) -> None:
    clock = Clock()
    probes: list[list[str]] = []
    reasons = ["OPEND_PORT_CLOSED", "OPEND_LOGIN_INVALID", None]

    def run_cmd(command, **kwargs):
        if "src.infrastructure.opend_watchdog" not in command:
            return subprocess.CompletedProcess(command, 0, "ok", "")
        probes.append(command)
        reason = reasons[len(probes) - 1]
        payload = {"ok": reason is None, "error_code": reason}
        assert command[1:3] == ["-m", "src.infrastructure.opend_watchdog"]
        assert "--retry-enabled" in command and "--ensure" not in command
        assert command[command.index("--retry-interval-sec") + 1] == "5"
        remaining = float(command[command.index("--retry-timeout-sec") + 1])
        assert remaining == 300 - clock.now
        assert kwargs["timeout"] == int(remaining) + 35
        return subprocess.CompletedProcess(command, 0 if reason is None else 2, json.dumps(payload), "")

    outcome, operations = _gate(tmp_path, _profile(), clock, run_cmd)
    assert outcome["ok"] is True
    assert len(probes) == 3 and clock.sleeps == [5, 5]
    assert [operation["check"] for operation in operations].count("opend-login-check") == 1
    assert operations[-1]["attempts"] == 3
    assert operations[-1]["waited_seconds"] == 10
    assert operations[-1]["retry_budget_seconds"] == 300


def test_gate_exhausts_exact_budget_with_last_reason(tmp_path: Path) -> None:
    clock = Clock()
    probes = 0

    def run_cmd(command, **_kwargs):
        nonlocal probes
        if "src.infrastructure.opend_watchdog" in command:
            probes += 1
            return subprocess.CompletedProcess(command, 2, '{"ok":false,"error_code":"OPEND_PORT_CLOSED"}', "")
        return subprocess.CompletedProcess(command, 0, "ok", "")

    outcome, operations = _gate(tmp_path, _profile(), clock, run_cmd)
    assert outcome["ok"] is False
    assert outcome["failed_checks"][0]["reason_code"] == "OPEND_PORT_CLOSED"
    assert probes == 300 // 5 + 1
    assert clock.now == 300 and len(clock.sleeps) == 60
    assert operations[-1]["attempts"] == probes
    assert operations[-1]["waited_seconds"] == 300


def test_missing_endpoint_and_non_opend_checks_never_wait(tmp_path: Path) -> None:
    clock = Clock()
    commands: list[list[str]] = []

    def run_cmd(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "ok", "")

    missing = _profile()
    missing["opend"]["services"][0].pop("port")
    outcome, operations = _gate(tmp_path, missing, clock, run_cmd)
    assert outcome["failed_checks"][0]["reason_code"] == "OPEND_ENDPOINT_MISSING"
    assert not any("src.infrastructure.opend_watchdog" in command for command in commands)
    assert len(operations) == 2 and clock.sleeps == []
    commands.clear()
    outcome, operations = _gate(tmp_path, _profile(opend=False), clock, run_cmd)
    assert outcome["ok"] is True and len(commands) == len(operations) == 2
    assert clock.sleeps == []


def test_large_finite_budget_is_capped_and_actual_wait_is_recorded(tmp_path: Path) -> None:
    config_path = tmp_path / "config.us.json"
    config_path.write_text('{"watchdog":{"retry_timeout_sec":1e9}}', encoding="utf-8")
    clock = Clock()
    commands: list[list[str]] = []

    def run_cmd(command, **kwargs):
        if "src.infrastructure.opend_watchdog" not in command:
            return subprocess.CompletedProcess(command, 0, "ok", "")
        commands.append(command)
        assert kwargs["timeout"] == (635 if len(commands) == 1 else 630)
        return subprocess.CompletedProcess(command, 2 if len(commands) == 1 else 0,
                                           '{"ok":false}' if len(commands) == 1 else '{"ok":true}', "")

    outcome, operations = _gate(tmp_path, _profile(config_path=config_path), clock, run_cmd)
    assert outcome["ok"] is True and clock.now == 5
    assert operations[-1]["retry_budget_seconds"] == 600
    assert operations[-1]["retry_budget_requested_seconds"] == 1e9
    assert operations[-1]["waited_seconds"] == 5
    assert float(commands[0][commands[0].index("--retry-timeout-sec") + 1]) == 600
    assert float(commands[1][commands[1].index("--retry-timeout-sec") + 1]) == 595


def test_nonfinite_budget_ignored_and_child_time_counts_against_deadline(tmp_path: Path) -> None:
    config_path = tmp_path / "config.us.json"
    for candidate in ("Infinity", "NaN"):
        config_path.write_text('{"watchdog":{"retry_timeout_sec":' + candidate + '}}', encoding="utf-8")
        clock = Clock()
        commands: list[list[str]] = []

        def run_cmd(command, **_kwargs):
            if "src.infrastructure.opend_watchdog" not in command:
                return subprocess.CompletedProcess(command, 0, "ok", "")
            commands.append(command)
            if len(commands) == 1:
                clock.now += 297
                return subprocess.CompletedProcess(command, 2, '{"ok":false}', "")
            return subprocess.CompletedProcess(command, 0, '{"ok":true}', "")

        outcome, operations = _gate(tmp_path, _profile(config_path=config_path), clock, run_cmd)
        assert outcome["ok"] is True and clock.sleeps == [3]
        assert float(commands[0][commands[0].index("--retry-timeout-sec") + 1]) == 300
        assert float(commands[1][commands[1].index("--retry-timeout-sec") + 1]) == 0
        assert operations[-1]["waited_seconds"] == 300
        assert "retry_budget_requested_seconds" not in operations[-1]


def test_long_json_is_parsed_before_operation_output_is_clipped(tmp_path: Path) -> None:
    clock = Clock()

    def run_cmd(command, **_kwargs):
        if "src.infrastructure.opend_watchdog" in command:
            return subprocess.CompletedProcess(command, 0, json.dumps({"ok": True, "detail": "x" * 5000}), "")
        return subprocess.CompletedProcess(command, 0, "ok", "")

    outcome, operations = _gate(tmp_path, _profile(), clock, run_cmd)
    assert outcome["ok"] is True and len(operations[-1]["stdout"]) == 4000
    assert "time.sleep(" not in inspect.getsource(service_upgrade)


def test_malformed_payload_cannot_report_success_or_loop_forever(tmp_path: Path) -> None:
    probes = 0

    def run_cmd(command, **_kwargs):
        nonlocal probes
        if "src.infrastructure.opend_watchdog" in command:
            probes += 1
            return subprocess.CompletedProcess(command, 0, "not-json", "")
        return subprocess.CompletedProcess(command, 0, "ok", "")

    operations: list[dict] = []
    outcome = service_upgrade._post_upgrade_service_health(
        profile=_profile(), repo_root=tmp_path, run_cmd=run_cmd, operations=operations,
        monotonic_fn=lambda: 0.0, sleep_fn=lambda _seconds: None)
    assert outcome["ok"] is False and operations[-1]["ok"] is False
    assert probes == operations[-1]["attempts"] == 61


def test_sdk_stdout_log_before_success_json_passes_first_probe(tmp_path: Path) -> None:
    clock = Clock()
    probes = 0

    def run_cmd(command, **_kwargs):
        nonlocal probes
        if "src.infrastructure.opend_watchdog" in command:
            probes += 1
            return subprocess.CompletedProcess(command, 0, SDK_LOG_LINE + '\n{"ok": true}', "")
        return subprocess.CompletedProcess(command, 0, "ok", "")

    outcome, operations = _gate(tmp_path, _profile(), clock, run_cmd)
    assert outcome["ok"] is True and probes == operations[-1]["attempts"] == 1
    assert clock.sleeps == []


def test_sdk_stdout_log_before_failure_json_retries_then_passes(tmp_path: Path) -> None:
    clock = Clock()
    probes = 0

    def run_cmd(command, **_kwargs):
        nonlocal probes
        if "src.infrastructure.opend_watchdog" in command:
            probes += 1
            if probes == 1:
                return subprocess.CompletedProcess(
                    command, 2, SDK_LOG_LINE + '\n{"ok": false, "error_code": "OPEND_LOGIN_INVALID"}', "")
            return subprocess.CompletedProcess(command, 0, '{"ok": true}', "")
        return subprocess.CompletedProcess(command, 0, "ok", "")

    outcome, operations = _gate(tmp_path, _profile(), clock, run_cmd)
    assert outcome["ok"] is True and probes == operations[-1]["attempts"] == 2
    assert clock.sleeps == [5]


def test_sdk_stdout_log_without_json_remains_fail_closed(tmp_path: Path) -> None:
    clock = Clock()
    probes = 0

    def run_cmd(command, **_kwargs):
        nonlocal probes
        if "src.infrastructure.opend_watchdog" in command:
            probes += 1
            return subprocess.CompletedProcess(command, 0, SDK_LOG_LINE + "\n", "")
        return subprocess.CompletedProcess(command, 0, "ok", "")

    outcome, operations = _gate(tmp_path, _profile(), clock, run_cmd)
    assert outcome["ok"] is False and operations[-1]["ok"] is False
    assert probes == operations[-1]["attempts"] == 61
    assert clock.now == 300


def test_exhaustion_uses_last_non_port_closed_reason(tmp_path: Path) -> None:
    clock = Clock()
    probes = 0

    def run_cmd(command, **_kwargs):
        nonlocal probes
        if "src.infrastructure.opend_watchdog" in command:
            probes += 1
            reason = "OPEND_LOGIN_INVALID" if probes == 61 else "OPEND_PORT_CLOSED"
            stdout = json.dumps({"ok": False, "error_code": reason})
            if probes == 61:
                stdout = SDK_LOG_LINE + "\n" + stdout
            return subprocess.CompletedProcess(command, 2, stdout, "")
        return subprocess.CompletedProcess(command, 0, "ok", "")

    outcome, operations = _gate(tmp_path, _profile(), clock, run_cmd)
    assert outcome["ok"] is False and probes == operations[-1]["attempts"] == 61
    assert outcome["failed_checks"][0]["reason_code"] == "OPEND_LOGIN_INVALID"
