from __future__ import annotations

import json
import hashlib
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from src.application.prepared_portfolio_context import (
    PreparedPortfolioContextError,
    load_prepared_portfolio_context,
    load_prepared_portfolio_context_receipt,
    prepare_portfolio_contexts,
)
from src.application.tick_run_workspace import publish_account_run_config


class _CompletedWorker:
    returncode = 0

    def __init__(self, command: list[str], **_kwargs):
        request_path = Path(command[-1])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        context = {
            "filters": {"account": request["account"]},
            "source_observed_at": "2026-08-16T00:00:00+00:00",
            "stocks_by_symbol": {
                "NVDA": {
                    "account": request["account"],
                    "avg_cost": 100 if request["account"] == "lx" else 120,
                }
            },
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
            "account_config_sha256": request["account_config_sha256"],
            "portfolio_context": context,
            "payload_sha256": hashlib.sha256(raw).hexdigest(),
            "portfolio_source_name": "futu",
            "portfolio_source_account": request["account"],
        }
        Path(request["result_path"]).write_text(
            json.dumps(result),
            encoding="utf-8",
        )

    def poll(self):
        return 0


def _state_dirs(tmp_path: Path, run_id: str) -> tuple[Path, dict[str, Path]]:
    run = tmp_path / "output_runs" / run_id
    return run / "state", {account: run / "accounts" / account / "state" for account in ("lx", "sy")}


def _config_authorities(tmp_path: Path, run_id: str):
    return {
        account: publish_account_run_config(
            base=tmp_path,
            run_id=run_id,
            account=account,
            config={
                "portfolio": {"account": account},
                "runtime": {},
                "symbols": [],
            },
        )
        for account in ("lx", "sy")
    }


def _artifact_bytes(payload: dict) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def test_prepare_promotes_only_valid_worker_payloads(tmp_path: Path) -> None:
    shared, states = _state_dirs(tmp_path, "run-1")
    authorities = _config_authorities(tmp_path, "run-1")
    manifests = prepare_portfolio_contexts(
        base=tmp_path,
        repo_root=tmp_path,
        run_id="run-1",
        account_config_authorities=authorities,
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
        expected_base=tmp_path,
        expected_run_id="run-1",
        expected_account="lx",
        expected_account_config_sha256=authorities[
            "lx"
        ].account_config_sha256,
        expected_manifest_sha256=manifests["lx"]["manifest_sha256"],
        expected_runtime_config=json.loads(
            authorities["lx"].canonical_bytes.decode("utf-8")
        ),
    )
    assert loaded["stocks_by_symbol"]["NVDA"]["avg_cost"] == 100
    receipt = load_prepared_portfolio_context_receipt(
        manifest_path=Path(manifests["lx"]["manifest_path"]),
        expected_base=tmp_path,
        expected_run_id="run-1",
        expected_account="lx",
        expected_account_config_sha256=authorities["lx"].account_config_sha256,
        expected_manifest_sha256=manifests["lx"]["manifest_sha256"],
        expected_runtime_config=json.loads(
            authorities["lx"].canonical_bytes.decode("utf-8")
        ),
    )
    assert receipt["payload"] == loaded
    assert receipt["manifest"]["source_as_of_utc"] == loaded["source_observed_at"]
    assert (
        receipt["manifest"]["promoted_at_utc"] == receipt["manifest"]["prepared_at_utc"]
    )

    manifest_path = Path(manifests["lx"]["manifest_path"])
    malformed_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    malformed_manifest["promoted_at_utc"] = "2026-08-16T00:00:09+00:00"
    malformed_bytes = _artifact_bytes(malformed_manifest)
    manifest_path.write_bytes(malformed_bytes)
    malformed_sha256 = hashlib.sha256(malformed_bytes).hexdigest()
    assert (
        load_prepared_portfolio_context(
            manifest_path=manifest_path,
            expected_base=tmp_path,
            expected_run_id="run-1",
            expected_account="lx",
            expected_account_config_sha256=authorities["lx"].account_config_sha256,
            expected_manifest_sha256=malformed_sha256,
            expected_runtime_config=json.loads(
                authorities["lx"].canonical_bytes.decode("utf-8")
            ),
        )
        == loaded
    )
    with pytest.raises(PreparedPortfolioContextError, match="alias mismatch"):
        load_prepared_portfolio_context_receipt(
            manifest_path=manifest_path,
            expected_base=tmp_path,
            expected_run_id="run-1",
            expected_account="lx",
            expected_account_config_sha256=authorities["lx"].account_config_sha256,
            expected_manifest_sha256=malformed_sha256,
            expected_runtime_config=json.loads(
                authorities["lx"].canonical_bytes.decode("utf-8")
            ),
        )

    manifest_path.write_bytes(_artifact_bytes(receipt["manifest"]))

    context_path = states["lx"] / manifests["lx"]["portfolio_context_relpath"]
    context_path.write_text("{}", encoding="utf-8")
    with pytest.raises(PreparedPortfolioContextError, match="hash mismatch"):
        load_prepared_portfolio_context(
            manifest_path=Path(manifests["lx"]["manifest_path"]),
            expected_base=tmp_path,
            expected_run_id="run-1",
            expected_account="lx",
            expected_account_config_sha256=authorities[
                "lx"
            ].account_config_sha256,
            expected_manifest_sha256=manifests["lx"]["manifest_sha256"],
            expected_runtime_config=json.loads(
                authorities["lx"].canonical_bytes.decode("utf-8")
            ),
        )


def test_prepared_manifest_is_bound_to_expected_account_config_hash(
    tmp_path: Path,
) -> None:
    shared, states = _state_dirs(tmp_path, "run-config-binding")
    authorities = _config_authorities(tmp_path, "run-config-binding")
    manifests = prepare_portfolio_contexts(
        base=tmp_path,
        repo_root=tmp_path,
        run_id="run-config-binding",
        account_config_authorities=authorities,
        account_state_dirs=states,
        shared_state_dir=shared,
        timeout_sec=1,
        popen_factory=_CompletedWorker,
    )

    with pytest.raises(PreparedPortfolioContextError, match="config hash mismatch"):
        load_prepared_portfolio_context(
            manifest_path=Path(manifests["lx"]["manifest_path"]),
            expected_base=tmp_path,
            expected_run_id="run-config-binding",
            expected_account="lx",
            expected_account_config_sha256="0" * 64,
            expected_manifest_sha256=manifests["lx"]["manifest_sha256"],
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
        account_config_authorities=_config_authorities(
            tmp_path,
            "run-timeout",
        ),
        account_state_dirs=states,
        shared_state_dir=shared,
        timeout_sec=0.15,
        kill_grace_sec=0.05,
        popen_factory=blocking_factory,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.8
    assert {item["reason"] for item in manifests.values()} == {"portfolio_context_deadline_exceeded"}
    assert not any((state_dir / "portfolio_context.json").exists() for state_dir in states.values())


def test_worker_exit_race_during_timeout_cleanup_is_isolated(
    tmp_path: Path,
) -> None:
    shared, states = _state_dirs(tmp_path, "run-timeout-exit-race")

    class _ExitedDuringTerminate:
        def __init__(self) -> None:
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0
            raise ProcessLookupError("worker already exited")

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    manifests = prepare_portfolio_contexts(
        base=tmp_path,
        repo_root=tmp_path,
        run_id="run-timeout-exit-race",
        account_config_authorities=_config_authorities(
            tmp_path,
            "run-timeout-exit-race",
        ),
        account_state_dirs=states,
        shared_state_dir=shared,
        timeout_sec=0.01,
        kill_grace_sec=0.01,
        popen_factory=lambda *_args, **_kwargs: _ExitedDuringTerminate(),
    )

    assert {item["reason"] for item in manifests.values()} == {
        "portfolio_context_deadline_exceeded"
    }


def test_worker_finishing_after_deadline_check_is_not_promoted(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import prepared_portfolio_context as mod

    shared, states = _state_dirs(tmp_path, "run-deadline-race")

    class _FinishesBetweenDeadlineAndCleanup(_CompletedWorker):
        def __init__(self, command, **kwargs) -> None:
            super().__init__(command, **kwargs)
            self.returncode = None
            self.poll_calls = 0

        def poll(self):
            self.poll_calls += 1
            if self.poll_calls >= 3:
                self.returncode = 0
            return self.returncode

        def terminate(self):
            self.returncode = 0
            raise ProcessLookupError("worker exited after the deadline check")

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monotonic_values = iter((0.0, 0.0, 1.0, 1.0, 1.0))
    monkeypatch.setattr(
        mod.time,
        "monotonic",
        lambda: next(monotonic_values, 1.0),
    )
    monkeypatch.setattr(mod.time, "sleep", lambda _seconds: None)

    manifests = prepare_portfolio_contexts(
        base=tmp_path,
        repo_root=tmp_path,
        run_id="run-deadline-race",
        account_config_authorities=_config_authorities(
            tmp_path,
            "run-deadline-race",
        ),
        account_state_dirs=states,
        shared_state_dir=shared,
        timeout_sec=0.5,
        kill_grace_sec=0.1,
        popen_factory=_FinishesBetweenDeadlineAndCleanup,
    )

    assert {item["status"] for item in manifests.values()} == {"unavailable"}
    assert {item["reason"] for item in manifests.values()} == {
        "portfolio_context_deadline_exceeded"
    }


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
        account_config_authorities=_config_authorities(
            tmp_path,
            "run-mixed",
        ),
        account_state_dirs=states,
        shared_state_dir=shared,
        timeout_sec=0.15,
        kill_grace_sec=0.05,
        popen_factory=mixed_factory,
    )

    assert manifests["lx"]["status"] == "ready"
    assert manifests["sy"]["status"] == "unavailable"
    assert manifests["sy"]["reason"] == "portfolio_context_deadline_exceeded"
    assert (states["lx"] / manifests["lx"]["portfolio_context_relpath"]).is_file()
    assert not list(states["sy"].glob("portfolio_context.*.json"))
    assert slow_processes and slow_processes[0].poll() is not None
    sy_manifest_path = states["sy"] / "prepared_portfolio_context.v1.json"
    published = sy_manifest_path.read_bytes()
    time.sleep(0.1)
    assert sy_manifest_path.read_bytes() == published


def test_worker_request_uses_exact_published_config_authority(
    tmp_path: Path,
) -> None:
    shared, states = _state_dirs(tmp_path, "run-authority")
    authorities = _config_authorities(tmp_path, "run-authority")
    requests: list[dict] = []

    def _capture(command, **kwargs):
        requests.append(json.loads(Path(command[-1]).read_text(encoding="utf-8")))
        return _CompletedWorker(command, **kwargs)

    manifests = prepare_portfolio_contexts(
        base=tmp_path,
        repo_root=tmp_path,
        run_id="run-authority",
        account_config_authorities=authorities,
        account_state_dirs=states,
        shared_state_dir=shared,
        timeout_sec=1,
        popen_factory=_capture,
    )

    assert {item["account"] for item in requests} == {"lx", "sy"}
    for worker_request in requests:
        account = worker_request["account"]
        authority = authorities[account]
        assert "runtime_config" not in worker_request
        assert worker_request["account_config_path"] == str(authority.state_path)
        assert worker_request["account_config_compatibility_path"] == str(authority.compatibility_path)
        assert worker_request["account_config_sha256"] == (authority.account_config_sha256)
        assert manifests[account]["account_config_sha256"] == (authority.account_config_sha256)


def test_invalid_config_authority_is_isolated_from_healthy_prepared_worker(
    tmp_path: Path,
) -> None:
    shared, states = _state_dirs(tmp_path, "run-invalid")
    authorities = _config_authorities(tmp_path, "run-invalid")
    authorities["lx"].compatibility_path.write_text(
        "{}\n",
        encoding="utf-8",
    )

    started_accounts: list[str] = []

    def _capture(command, **kwargs):
        request = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
        started_accounts.append(request["account"])
        return _CompletedWorker(command, **kwargs)

    manifests = prepare_portfolio_contexts(
        base=tmp_path,
        repo_root=tmp_path,
        run_id="run-invalid",
        account_config_authorities=authorities,
        account_state_dirs=states,
        shared_state_dir=shared,
        timeout_sec=1,
        popen_factory=_capture,
    )

    assert started_accounts == ["sy"]
    assert manifests["sy"]["status"] == "ready"
    assert manifests["lx"]["status"] == "unavailable"
    assert manifests["lx"]["error_code"] == "ACCOUNT_CONFIG_ARTIFACT_MISMATCH"


def test_worker_consumes_published_config_bytes_and_hash(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import prepared_portfolio_context as mod

    authority = publish_account_run_config(
        base=tmp_path,
        run_id="run-worker",
        account="lx",
        config={
            "portfolio": {"account": "lx", "broker": "futu"},
            "runtime": {"marker": "exact-published-bytes"},
            "symbols": [],
        },
    )
    request_path = tmp_path / "worker-request.json"
    result_path = tmp_path / "worker-result.json"
    request_path.write_text(
        json.dumps(
            {
                "schema_version": "prepared_portfolio_context_worker_request.v1",
                "token": "token-1",
                "run_id": "run-worker",
                "account": "lx",
                "base": str(tmp_path),
                "state_dir": str(authority.state_path.parent),
                "shared_state_dir": str(tmp_path / "output_runs" / "run-worker" / "state"),
                "account_config_path": str(authority.state_path),
                "account_config_compatibility_path": str(authority.compatibility_path),
                "account_config_sha256": authority.account_config_sha256,
                "account_config_canonical_json": authority.canonical_bytes.decode(
                    "utf-8"
                ),
                "result_path": str(result_path),
            }
        ),
        encoding="utf-8",
    )
    observed: dict = {}
    monkeypatch.setattr(
        mod,
        "resolve_data_config_path",
        lambda **_kwargs: tmp_path / "portfolio.json",
    )
    monkeypatch.setattr(
        mod,
        "build_account_portfolio_source_plan",
        lambda *_args, **_kwargs: SimpleNamespace(
            requested_source="futu",
            primary_source="futu",
            account_type="futu",
            holdings_account=None,
        ),
    )

    def _load_account_portfolio_context(**kwargs):
        observed.update(kwargs["runtime_config"])
        return {"filters": {"account": "lx"}, "stocks_by_symbol": {}}

    monkeypatch.setattr(
        mod,
        "load_account_portfolio_context",
        _load_account_portfolio_context,
    )
    monkeypatch.setattr(mod, "wants_global_path_risk_context", lambda _cfg: False)

    assert mod.run_worker(request_path) == 0

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["status"] == "ready"
    assert result["account_config_sha256"] == authority.account_config_sha256
    assert observed["runtime"]["marker"] == "exact-published-bytes"


def test_worker_fails_closed_when_config_changes_after_spawn(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import prepared_portfolio_context as mod

    authority = publish_account_run_config(
        base=tmp_path,
        run_id="run-worker-tamper",
        account="lx",
        config={"portfolio": {"account": "lx"}, "symbols": []},
    )
    request_path = tmp_path / "tampered-worker-request.json"
    result_path = tmp_path / "tampered-worker-result.json"
    request_path.write_text(
        json.dumps(
            {
                "schema_version": "prepared_portfolio_context_worker_request.v1",
                "token": "token-2",
                "run_id": "run-worker-tamper",
                "account": "lx",
                "base": str(tmp_path),
                "state_dir": str(authority.state_path.parent),
                "shared_state_dir": str(tmp_path / "output_runs" / "run-worker-tamper" / "state"),
                "account_config_path": str(authority.state_path),
                "account_config_compatibility_path": str(authority.compatibility_path),
                "account_config_sha256": authority.account_config_sha256,
                "account_config_canonical_json": authority.canonical_bytes.decode(
                    "utf-8"
                ),
                "result_path": str(result_path),
            }
        ),
        encoding="utf-8",
    )
    authority.state_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        mod,
        "load_account_portfolio_context",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("portfolio child must not consume invalid config")),
    )

    assert mod.run_worker(request_path) == 0

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["status"] == "unavailable"
    assert result["error_type"] == "AccountRunConfigError"
    assert result["account_config_sha256"] == authority.account_config_sha256


def test_loader_rejects_coherent_manifest_and_payload_generation_replacement(
    tmp_path: Path,
) -> None:
    shared, states = _state_dirs(tmp_path, "run-generation")
    authorities = _config_authorities(tmp_path, "run-generation")
    manifests = prepare_portfolio_contexts(
        base=tmp_path,
        repo_root=tmp_path,
        run_id="run-generation",
        account_config_authorities=authorities,
        account_state_dirs=states,
        shared_state_dir=shared,
        timeout_sec=1,
        popen_factory=_CompletedWorker,
    )
    original = manifests["lx"]
    manifest_path = Path(original["manifest_path"])

    parent_payload = load_prepared_portfolio_context(
        manifest_path=manifest_path,
        expected_base=tmp_path,
        expected_run_id="run-generation",
        expected_account="lx",
        expected_account_config_sha256=authorities["lx"].account_config_sha256,
        expected_manifest_sha256=original["manifest_sha256"],
        expected_runtime_config=json.loads(
            authorities["lx"].canonical_bytes.decode("utf-8")
        ),
    )
    assert parent_payload is not None
    assert parent_payload["stocks_by_symbol"]["NVDA"]["avg_cost"] == 100

    replacement_payload = {
        "filters": {"account": "lx"},
        "portfolio_source_name": "futu",
        "stocks_by_symbol": {
            "NVDA": {"account": "lx", "avg_cost": 999},
        },
    }
    replacement_bytes = _artifact_bytes(replacement_payload)
    replacement_digest = hashlib.sha256(replacement_bytes).hexdigest()
    replacement_name = f"portfolio_context.{replacement_digest}.json"
    (manifest_path.parent / replacement_name).write_bytes(replacement_bytes)
    replacement_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    replacement_manifest["portfolio_context_relpath"] = replacement_name
    replacement_manifest["payload_sha256"] = replacement_digest
    manifest_path.write_bytes(_artifact_bytes(replacement_manifest))

    with pytest.raises(
        PreparedPortfolioContextError,
        match="manifest generation mismatch",
    ):
        load_prepared_portfolio_context(
            manifest_path=manifest_path,
            expected_base=tmp_path,
            expected_run_id="run-generation",
            expected_account="lx",
            expected_account_config_sha256=authorities[
                "lx"
            ].account_config_sha256,
            expected_manifest_sha256=original["manifest_sha256"],
            expected_runtime_config=json.loads(
                authorities["lx"].canonical_bytes.decode("utf-8")
            ),
        )


def test_same_run_reentry_adopts_existing_prepared_generation(
    tmp_path: Path,
) -> None:
    shared, states = _state_dirs(tmp_path, "run-reentry")
    authorities = _config_authorities(tmp_path, "run-reentry")
    first = prepare_portfolio_contexts(
        base=tmp_path,
        repo_root=tmp_path,
        run_id="run-reentry",
        account_config_authorities=authorities,
        account_state_dirs=states,
        shared_state_dir=shared,
        timeout_sec=1,
        popen_factory=_CompletedWorker,
    )

    second = prepare_portfolio_contexts(
        base=tmp_path,
        repo_root=tmp_path,
        run_id="run-reentry",
        account_config_authorities=authorities,
        account_state_dirs=states,
        shared_state_dir=shared,
        timeout_sec=1,
        popen_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("an immutable prepared generation must be adopted")
        ),
    )

    assert second["lx"]["manifest_sha256"] == first["lx"]["manifest_sha256"]
    assert second["sy"]["manifest_sha256"] == first["sy"]["manifest_sha256"]


def test_same_run_config_failure_does_not_degrade_healthy_existing_generation(
    tmp_path: Path,
) -> None:
    from src.application.tick_run_workspace import canonical_account_run_config_bytes

    shared, states = _state_dirs(tmp_path, "run-reentry-config-failure")
    authorities = _config_authorities(tmp_path, "run-reentry-config-failure")
    first = prepare_portfolio_contexts(
        base=tmp_path,
        repo_root=tmp_path,
        run_id="run-reentry-config-failure",
        account_config_authorities=authorities,
        account_state_dirs=states,
        shared_state_dir=shared,
        timeout_sec=1,
        popen_factory=_CompletedWorker,
    )
    replacement = json.loads(authorities["lx"].canonical_bytes.decode("utf-8"))
    replacement.setdefault("runtime", {})["generation"] = "drifted"
    replacement_bytes = canonical_account_run_config_bytes(replacement)
    authorities["lx"].state_path.write_bytes(replacement_bytes)
    authorities["lx"].compatibility_path.write_bytes(replacement_bytes)

    second = prepare_portfolio_contexts(
        base=tmp_path,
        repo_root=tmp_path,
        run_id="run-reentry-config-failure",
        account_config_authorities=authorities,
        account_state_dirs=states,
        shared_state_dir=shared,
        timeout_sec=1,
        popen_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("existing generations must not spawn new workers")
        ),
    )

    assert second["lx"]["status"] == "unavailable"
    assert second["lx"]["error_code"] == "ACCOUNT_CONFIG_PARENT_BYTES_MISMATCH"
    assert second["lx"]["publication_status"] == "existing_immutable_generation"
    assert second["sy"]["status"] == "ready"
    assert second["sy"]["manifest_sha256"] == first["sy"]["manifest_sha256"]


@pytest.mark.parametrize(
    "foreign_payload",
    [
        {
            "filters": {"account": "sy"},
            "stocks_by_symbol": {
                "NVDA": {"account": "sy", "avg_cost": 120},
            },
        },
        {"stocks_by_symbol": {}},
    ],
)
def test_loader_rejects_foreign_or_missing_prepared_account_identity(
    tmp_path: Path,
    foreign_payload: dict,
) -> None:
    shared, states = _state_dirs(tmp_path, "run-foreign")
    authorities = _config_authorities(tmp_path, "run-foreign")
    manifests = prepare_portfolio_contexts(
        base=tmp_path,
        repo_root=tmp_path,
        run_id="run-foreign",
        account_config_authorities=authorities,
        account_state_dirs=states,
        shared_state_dir=shared,
        timeout_sec=1,
        popen_factory=_CompletedWorker,
    )
    manifest_path = Path(manifests["lx"]["manifest_path"])
    payload_bytes = _artifact_bytes(foreign_payload)
    payload_digest = hashlib.sha256(payload_bytes).hexdigest()
    payload_name = f"portfolio_context.{payload_digest}.json"
    (manifest_path.parent / payload_name).write_bytes(payload_bytes)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["portfolio_context_relpath"] = payload_name
    manifest["payload_sha256"] = payload_digest
    manifest_bytes = _artifact_bytes(manifest)
    manifest_path.write_bytes(manifest_bytes)

    with pytest.raises(PreparedPortfolioContextError, match="account mismatch"):
        load_prepared_portfolio_context(
            manifest_path=manifest_path,
            expected_base=tmp_path,
            expected_run_id="run-foreign",
            expected_account="lx",
            expected_account_config_sha256=authorities[
                "lx"
            ].account_config_sha256,
            expected_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            expected_runtime_config=json.loads(
                authorities["lx"].canonical_bytes.decode("utf-8")
            ),
        )


def test_second_worker_spawn_failure_preserves_completed_healthy_peer(
    tmp_path: Path,
) -> None:
    shared, states = _state_dirs(tmp_path, "run-partial-spawn")
    calls = 0

    def _partial_factory(command, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("spawn failed")
        return _CompletedWorker(command, **kwargs)

    manifests = prepare_portfolio_contexts(
        base=tmp_path,
        repo_root=tmp_path,
        run_id="run-partial-spawn",
        account_config_authorities=_config_authorities(
            tmp_path,
            "run-partial-spawn",
        ),
        account_state_dirs=states,
        shared_state_dir=shared,
        timeout_sec=1,
        popen_factory=_partial_factory,
    )

    assert manifests["lx"]["status"] == "ready"
    assert manifests["sy"]["status"] == "unavailable"
    assert manifests["sy"]["reason"] == "portfolio_context_worker_spawn_failed"


def test_partial_spawn_failure_reaps_already_running_worker(
    tmp_path: Path,
) -> None:
    shared, states = _state_dirs(tmp_path, "run-partial-reap")

    class _RunningWorker:
        def __init__(self) -> None:
            self.returncode = None
            self.terminated = False
            self.killed = False
            self.waited = 0

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def wait(self, timeout=None):
            self.waited += 1
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9

    running = _RunningWorker()
    calls = 0

    def _partial_factory(_command, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("spawn failed")
        return running

    manifests = prepare_portfolio_contexts(
        base=tmp_path,
        repo_root=tmp_path,
        run_id="run-partial-reap",
        account_config_authorities=_config_authorities(
            tmp_path,
            "run-partial-reap",
        ),
        account_state_dirs=states,
        shared_state_dir=shared,
        timeout_sec=0.03,
        kill_grace_sec=0.01,
        popen_factory=_partial_factory,
    )

    assert manifests["lx"]["reason"] == "portfolio_context_deadline_exceeded"
    assert manifests["sy"]["reason"] == "portfolio_context_worker_spawn_failed"
    assert running.terminated is True
    assert running.waited >= 1
    assert running.poll() is not None
