"""Cancellation reaches the real prepared worker and its SDK process tree."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest

from src.application import prepared_portfolio_context as prepared, tick_cron
from src.application.tick_run_workspace import publish_account_run_config
from src.infrastructure import futu_gateway
from tests.cash_evidence_helpers import cash_config


class _BlockingSDK:
    def __init__(self, proof: str):
        self.proof = Path(proof)
        self.closed = threading.Event()
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

    def get_sync_conn_id(self):
        return 1

    def accinfo_query(self, **_kwargs):
        self.proof.with_suffix(".started").write_text(json.dumps({
            "pid": os.getpid(), "pgid": os.getpgid(0),
        }))
        while not self.closed.wait(.01):
            self.proof.write_text(str(time.monotonic()))

    def close(self):
        self.closed.set()


def _blocking_sdk_factory(_kind, _kwargs):
    return _BlockingSDK(os.environ["OM_WORKER_FIXTURE_PROOF"])


def _request(base: Path):
    cfg = cash_config()
    cfg["portfolio"].update(account="lx", broker="futu")
    cfg["account_settings"]["lx"]["futu"].update(host="offline", port=11111)
    authority = publish_account_run_config(base=base, run_id="cleanup", account="lx", config=cfg)
    request_path = base / "request.json"
    request_path.write_text(json.dumps({
        "schema_version": "prepared_portfolio_context_worker_request.v1",
        "token": "offline", "run_id": "cleanup", "account": "lx", "base": str(base),
        "state_dir": str(authority.state_path.parent),
        "shared_state_dir": str(base / "output_runs" / "cleanup" / "state"),
        "account_config_path": str(authority.state_path),
        "account_config_sha256": authority.account_config_sha256,
        "account_config_canonical_json": authority.canonical_bytes.decode(),
        "result_path": str(base / "result.json"),
    }))
    return authority, request_path


def _command(mode: str, base: Path):
    return [sys.executable, "-B", "-m", "tests.test_tick_execution_worker_cleanup", mode, str(base)]


def _env(base: Path, deadline=None):
    env = dict(os.environ, OM_WORKER_FIXTURE_PROOF=str(base / "heartbeat"),
               PYTHONPATH=str(Path(__file__).resolve().parents[1]), PYTHONDONTWRITEBYTECODE="1")
    env.pop("OM_TICK_HANG_TRACEBACK_PATH", None)
    env.pop("OM_TICK_DEADLINE_MONOTONIC", None)
    if deadline is not None:
        env["OM_TICK_DEADLINE_MONOTONIC"] = repr(deadline)
    return env


def _wait(predicate, seconds=5):
    end = time.monotonic() + seconds
    while not predicate() and time.monotonic() < end:
        time.sleep(.01)
    return bool(predicate())


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _cleanup_group(pgid):
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


@pytest.mark.parametrize("expired", [True, False], ids=["expired", "inflight"])
def test_prepared_worker_consumes_inherited_deadline_and_reaps_sdk(tmp_path, expired):
    _request(tmp_path)
    proof = tmp_path / "heartbeat"
    deadline = time.monotonic() + (-1 if expired else 2.0)
    with (tmp_path / "worker.log").open("w") as log:
        proc = subprocess.Popen(_command("worker", tmp_path), cwd=Path(__file__).resolve().parents[1],
                                env=_env(tmp_path, deadline), start_new_session=True, stdout=log, stderr=log)
        try:
            if not expired:
                assert _wait(proof.with_suffix(".started").exists), (tmp_path / "worker.log").read_text()
            assert _wait(lambda: proc.poll() is not None, 5), "worker ignored inherited deadline"
            assert proc.wait() == 0, (tmp_path / "worker.log").read_text()
            result = json.loads((tmp_path / "result.json").read_text())
            assert result["status"] == "unavailable"
            assert result["error_type"] == "_FetchControlTimeout"
            if expired:
                assert not proof.with_suffix(".started").exists()
            else:
                pid = json.loads(proof.with_suffix(".started").read_text())["pid"]
                assert _wait(lambda: not _alive(pid), 2), "SDK proxy survived worker completion"
        finally:
            _cleanup_group(proc.pid)
            proc.wait()


def test_prepared_parent_cancellation_reaps_blocked_sdk_without_a_new_group(tmp_path):
    with (tmp_path / "coordinator.log").open("w") as log:
        proc = subprocess.Popen(_command("cancel", tmp_path), cwd=Path(__file__).resolve().parents[1],
                                env=_env(tmp_path), start_new_session=True, stdout=log, stderr=log)
        try:
            assert _wait(lambda: proc.poll() is not None, 8), (tmp_path / "coordinator.log").read_text()
            assert proc.wait() == 0, (tmp_path / "coordinator.log").read_text()
            result = json.loads((tmp_path / "coordinator.json").read_text())
            assert result["cancelled"] is True
            assert result["worker_new_session"] is False
            sdk = json.loads((tmp_path / "heartbeat.started").read_text())
            assert sdk["pgid"] == proc.pid
            assert _wait(lambda: not _alive(sdk["pid"]), 2), "cancelled prepared worker left its SDK child"
        finally:
            _cleanup_group(proc.pid)
            proc.wait()


@pytest.mark.parametrize("mode", ["failed", "hard-timeout"])
def test_cron_reaps_its_sdk_group_on_return_and_hard_timeout(tmp_path, mode):
    phases = {}
    group_file = tmp_path / "coordinator.pid"
    try:
        kwargs = dict(command=_command(mode, tmp_path), cwd=Path(__file__).resolve().parents[1],
                      env=_env(tmp_path), timeout_seconds=5, terminate_grace_seconds=.2, timings=phases)
        if mode == "hard-timeout":
            with pytest.raises(subprocess.TimeoutExpired):
                tick_cron._run_tick_process_group(**kwargs)
        else:
            assert tick_cron._run_tick_process_group(**kwargs).returncode == 2
        started = tmp_path / "heartbeat.started"
        assert started.exists(), "offline SDK query did not reach the blocked transport"
        sdk = json.loads(started.read_text())
        assert sdk["pgid"] == int(group_file.read_text())
        first = (tmp_path / "heartbeat").read_text()
        time.sleep(.1)
        assert (tmp_path / "heartbeat").read_text() == first, "SDK continued after cron released its process group"
        assert phases["cleanup_seconds"] > 0
    finally:
        if group_file.exists():
            _cleanup_group(int(group_file.read_text()))


def _worker_main(mode: str, base: Path):
    def no_network(*_args, **_kwargs):
        raise AssertionError("real provider/network is forbidden")

    socket.socket.connect = no_network
    socket.socket.connect_ex = no_network
    socket.create_connection = no_network
    if mode == "worker":
        futu_gateway._ensure_futu_api_importable = lambda: None
        futu_gateway.port_open = lambda *_args, **_kwargs: True
        futu_gateway._create_futu_sdk_context = _blocking_sdk_factory
        futu_gateway.FutuGateway.ensure_broker_ready = lambda *_args, **_kwargs: {}
        return prepared.run_worker(base / "request.json")

    (base / "coordinator.pid").write_text(str(os.getpid()))
    authority, _ = _request(base)
    spawned = []

    def popen(command, **kwargs):
        spawned.append(bool(kwargs.get("start_new_session")))
        return subprocess.Popen(_command("worker", base), **kwargs)

    if mode == "cancel":
        try:
            prepared.prepare_portfolio_contexts(
                base=base, repo_root=Path(__file__).resolve().parents[1], run_id="cleanup",
                account_config_authorities={"lx": authority},
                account_state_dirs={"lx": authority.state_path.parent},
                shared_state_dir=base / "output_runs" / "cleanup" / "state",
                popen_factory=popen, timeout_sec=60,
                cancelled=(base / "heartbeat.started").exists,
            )
        except TimeoutError:
            (base / "coordinator.json").write_text(json.dumps({
                "cancelled": True, "worker_new_session": any(spawned),
            }))
            return 0
        raise AssertionError("prepared parent did not observe cancellation")

    worker = popen([], cwd=str(Path(__file__).resolve().parents[1]), env=_env(base))
    assert _wait((base / "heartbeat.started").exists)
    if mode == "failed":
        worker.kill()
        worker.wait()
        return 2
    threading.Event().wait()


if __name__ == "__main__":
    raise SystemExit(_worker_main(sys.argv[1], Path(sys.argv[2])))
