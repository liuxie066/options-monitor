from __future__ import annotations

import json
import hashlib
from pathlib import Path
import subprocess
import sys
import time

import pytest

from src.application.prepared_portfolio_context import (
    PreparedPortfolioContextError,
    load_prepared_portfolio_context,
    prepare_portfolio_contexts,
)


class _CompletedWorker:
    returncode = 0

    def __init__(self, command: list[str], **_kwargs):
        request_path = Path(command[-1])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        context = {
            "stocks_by_symbol": {
                "NVDA": {"avg_cost": 100 if request["account"] == "lx" else 120}
            }
        }
        raw = json.dumps(
            context,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        result = {
            "schema_version": "prepared_portfolio_context_worker_result.v1",
            "token": request["token"],
            "run_id": request["run_id"],
            "account": request["account"],
            "status": "ready",
            "portfolio_context": context,
            "payload_sha256": hashlib.sha256(raw).hexdigest(),
        }
        Path(request["result_path"]).write_text(
            json.dumps(result),
            encoding="utf-8",
        )

    def poll(self):
        return 0


def _state_dirs(tmp_path: Path, run_id: str) -> tuple[Path, dict[str, Path]]:
    run = tmp_path / "output_runs" / run_id
    return run / "state", {
        account: run / "accounts" / account / "state"
        for account in ("lx", "sy")
    }


def test_prepare_promotes_only_valid_worker_payloads(tmp_path: Path) -> None:
    shared, states = _state_dirs(tmp_path, "run-1")
    manifests = prepare_portfolio_contexts(
        base=tmp_path,
        repo_root=tmp_path,
        run_id="run-1",
        account_configs={"sy": {}, "lx": {}},
        account_state_dirs=states,
        shared_state_dir=shared,
        timeout_sec=1,
        popen_factory=_CompletedWorker,
    )

    assert list(manifests) == ["lx", "sy"]
    assert manifests["lx"]["status"] == "ready"
    assert manifests["sy"]["status"] == "ready"
    loaded = load_prepared_portfolio_context(
        manifest_path=Path(manifests["lx"]["manifest_path"]),
        expected_run_id="run-1",
        expected_account="lx",
    )
    assert loaded["stocks_by_symbol"]["NVDA"]["avg_cost"] == 100

    context_path = states["lx"] / "portfolio_context.json"
    context_path.write_text("{}", encoding="utf-8")
    with pytest.raises(PreparedPortfolioContextError, match="hash mismatch"):
        load_prepared_portfolio_context(
            manifest_path=Path(manifests["lx"]["manifest_path"]),
            expected_run_id="run-1",
            expected_account="lx",
        )


def test_context_workers_share_one_absolute_deadline(tmp_path: Path) -> None:
    shared, states = _state_dirs(tmp_path, "run-timeout")

    def blocking_factory(_command, **kwargs):
        return subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            cwd=kwargs.get("cwd"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    started = time.monotonic()
    manifests = prepare_portfolio_contexts(
        base=tmp_path,
        repo_root=tmp_path,
        run_id="run-timeout",
        account_configs={"lx": {}, "sy": {}},
        account_state_dirs=states,
        shared_state_dir=shared,
        timeout_sec=0.15,
        kill_grace_sec=0.05,
        popen_factory=blocking_factory,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.8
    assert {
        item["reason"] for item in manifests.values()
    } == {"portfolio_context_deadline_exceeded"}
    assert not any(
        (state_dir / "portfolio_context.json").exists()
        for state_dir in states.values()
    )


def test_completed_context_is_promoted_while_slow_peer_is_killed(
    tmp_path: Path,
) -> None:
    shared, states = _state_dirs(tmp_path, "run-mixed")
    slow_processes: list[subprocess.Popen] = []

    def mixed_factory(command, **kwargs):
        request = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
        if request["account"] == "lx":
            return _CompletedWorker(command, **kwargs)
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            cwd=kwargs.get("cwd"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        slow_processes.append(process)
        return process

    manifests = prepare_portfolio_contexts(
        base=tmp_path,
        repo_root=tmp_path,
        run_id="run-mixed",
        account_configs={"lx": {}, "sy": {}},
        account_state_dirs=states,
        shared_state_dir=shared,
        timeout_sec=0.15,
        kill_grace_sec=0.05,
        popen_factory=mixed_factory,
    )

    assert manifests["lx"]["status"] == "ready"
    assert manifests["sy"]["status"] == "unavailable"
    assert manifests["sy"]["reason"] == "portfolio_context_deadline_exceeded"
    assert (states["lx"] / "portfolio_context.json").is_file()
    assert not (states["sy"] / "portfolio_context.json").exists()
    assert slow_processes and slow_processes[0].poll() is not None
    sy_manifest_path = states["sy"] / "prepared_portfolio_context.v1.json"
    published = sy_manifest_path.read_bytes()
    time.sleep(0.1)
    assert sy_manifest_path.read_bytes() == published
