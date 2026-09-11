from __future__ import annotations

import json
import multiprocessing
from pathlib import Path
from queue import Empty
from typing import Any

import pytest
import yaml

import src.application.config_authoring_transaction as publishing
import src.application.wheel.workflows as workflows
import src.interfaces.cli.main as cli_main
from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tools import positions as position_tools
from src.application.config_yaml import resolve_yaml_runtime_config
from src.application.ledger.api import read_wheel_activation_windows_read_only


REPO_ROOT = Path(__file__).resolve().parents[1]


def _deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("OM_RUNTIME_ROOT", raising=False)
    monkeypatch.setattr(position_tools, "repo_base", lambda: REPO_ROOT)
    doc = {
        "accounts": {
            "lx": {
                "type": "futu",
                "futu_account_id": "12345678",
                "futu": {"host": "127.0.0.1", "port": 11111},
            }
        },
        "markets": {
            "us": {"accounts": ["lx"], "symbols": ["NVDA"]},
            "hk": {"accounts": ["lx"], "symbols": ["0700.HK"]},
        },
    }
    _install_config(tmp_path, doc)
    return tmp_path


def _install_config(root: Path, doc: dict[str, Any]) -> None:
    source = root / "config.yaml"
    source.write_bytes(publishing._yaml_bytes(doc))
    for market in ("us", "hk"):
        config, _ = resolve_yaml_runtime_config(repo_root=REPO_ROOT, market=market, config_path=source)
        (root / f"config.{market}.json").write_text(json.dumps(config), encoding="utf-8")


def _agent_call(
    root: Path,
    *,
    request_id: str = "enable-1",
    apply: bool = False,
    source_sha: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "config_key": "us",
        "config_path": str(root / "config.us.json"),
        "runtime_root": str(root),
        "market": "us",
        "account": "lx",
        "action": "enable",
        "expected_current_generation": 0,
        "request_id": request_id,
        "actor": "test",
        "apply": apply,
    }
    if apply:
        payload.update(confirm=True, expected_source_sha256=source_sha)
    result, _, _ = position_tools.WHEEL_ACTIVATION_TOOL.call(payload)
    return result


def _cli_apply(
    root: Path,
    *,
    source_sha: str,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, dict[str, Any]]:
    exit_code = cli_main.main(
        [
            "wheel",
            "activation",
            "enable",
            "--market",
            "us",
            "--account",
            "lx",
            "--config",
            str(root / "config.us.json"),
            "--runtime-root",
            str(root),
            "--expected-current-generation",
            "0",
            "--request-id",
            "enable-1",
            "--actor",
            "test",
            "--expected-source-sha256",
            source_sha,
            "--apply",
            "--confirm",
            "--format",
            "json",
        ]
    )
    return exit_code, json.loads(capsys.readouterr().out)


def _stage_journal(
    root: Path,
    *,
    after_doc: dict[str, Any],
    audit_id: str,
    targets: list[dict[str, Any]] | None = None,
) -> tuple[Path, bytes, list[dict[str, Any]]]:
    source = root / "config.yaml"
    before_sha = publishing.config_source_sha256(source)
    after_bytes = publishing._yaml_bytes(after_doc)
    prepared = publishing._prepare_generation(
        repo_root=REPO_ROOT,
        source_path=source,
        source_bytes=after_bytes,
        runtime_root=root,
        markets=["us", "hk"],
        include_assistant=False,
    )
    journal_targets = list(targets or prepared["target_payloads"])
    if targets is None:
        journal_targets.append(
            {"role": "config_yaml", "path": source, "payload": after_bytes, "source": True}
        )
    manifest = publishing._prepare_transaction_manifest(
        transaction_dir=root / "output_shared" / "state" / "config_authoring_transactions" / audit_id,
        audit_id=audit_id,
        source_path=source,
        before_source_sha=before_sha,
        after_source_sha=publishing._bytes_sha256(after_bytes),
        targets=journal_targets,
    )
    publishing._set_manifest_phase(manifest, "committing")
    return manifest, after_bytes, journal_targets


def _changed_doc(root: Path, symbol: str) -> dict[str, Any]:
    doc = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    doc["markets"]["us"]["symbols"].append(symbol)
    return doc


def _sqlite_path(root: Path) -> Path:
    return root / "output_shared" / "state" / "option_positions.sqlite3"


def test_agent_rolls_forward_pending_journal_before_rejecting_stale_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _deployment(tmp_path, monkeypatch)
    preview = _agent_call(root)
    manifest, after_bytes, targets = _stage_journal(
        root,
        after_doc=_changed_doc(root, "AMD"),
        audit_id="wheel-roll-forward",
    )
    (root / "config.yaml").write_bytes(after_bytes)

    with pytest.raises(AgentToolError) as exc:
        _agent_call(root, apply=True, source_sha=preview["expected_source_sha256"])

    assert exc.value.code == "STALE_PREVIEW"
    assert exc.value.details["failure_phase"] == "source_validation"
    assert exc.value.details["window_receipt"] is None
    assert exc.value.details["write_applied"] is True
    recovered = exc.value.details["recovered_transactions"]
    assert recovered[0]["audit_id"] == "wheel-roll-forward"
    assert recovered[0]["mode"] == "roll_forward"
    assert recovered[0]["cleanup"] is True
    assert recovered[0]["write_applied"] is True
    for target in targets:
        assert Path(target["path"]).read_bytes() == target["payload"]
    assert not manifest.exists()
    assert not _sqlite_path(root).exists()


def test_agent_rolls_back_pending_journal_before_rejecting_stale_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _deployment(tmp_path, monkeypatch)
    stale_preview = _agent_call(root)
    current_doc = _changed_doc(root, "AMD")
    _install_config(root, current_doc)
    before_runtime = {
        market: (root / f"config.{market}.json").read_bytes()
        for market in ("us", "hk")
    }
    manifest, _, targets = _stage_journal(
        root,
        after_doc=_changed_doc(root, "FUTU"),
        audit_id="wheel-roll-back",
    )
    for target in targets:
        if not target.get("source"):
            Path(target["path"]).write_bytes(target["payload"])

    with pytest.raises(AgentToolError) as exc:
        _agent_call(root, apply=True, source_sha=stale_preview["expected_source_sha256"])

    assert exc.value.code == "STALE_PREVIEW"
    recovered = exc.value.details["recovered_transactions"]
    assert recovered[0]["mode"] == "roll_back"
    assert recovered[0]["write_applied"] is True
    assert recovered[0]["cleanup"] is True
    for market, payload in before_runtime.items():
        assert (root / f"config.{market}.json").read_bytes() == payload
    assert not manifest.exists()
    assert not _sqlite_path(root).exists()


def test_agent_reports_partial_recovery_effects_without_writing_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _deployment(tmp_path, monkeypatch)
    preview = _agent_call(root)
    manifest, after_bytes, _ = _stage_journal(
        root,
        after_doc=_changed_doc(root, "AMD"),
        audit_id="wheel-partial-recovery",
    )
    (root / "config.yaml").write_bytes(after_bytes)
    failed_target = root / "config.hk.json"
    before_failed = failed_target.read_bytes()
    original_write = publishing._atomic_write_bytes

    def _fail_hk(path: Path, payload: bytes) -> None:
        if path.resolve() == failed_target.resolve():
            raise OSError("injected recovery failure")
        original_write(path, payload)

    monkeypatch.setattr(publishing, "_atomic_write_bytes", _fail_hk)

    with pytest.raises(AgentToolError) as exc:
        _agent_call(root, apply=True, source_sha=preview["expected_source_sha256"])

    assert exc.value.code == "CONFIG_TRANSACTION_RECOVERY_REQUIRED"
    assert exc.value.details["failure_phase"] == "config_recovery"
    assert exc.value.details["window_receipt"] is None
    assert exc.value.details["write_applied"] is True
    audit = exc.value.details["recovered_transactions"][0]
    assert audit["targets"][0]["write_applied"] is True
    assert audit["targets"][1]["write_applied"] is None
    assert audit["cleanup"] is False
    assert failed_target.read_bytes() == before_failed
    assert manifest.exists()
    assert not _sqlite_path(root).exists()


def test_agent_preserves_recovery_effects_when_source_readback_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _deployment(tmp_path, monkeypatch)
    preview = _agent_call(root)
    manifest, after_bytes, _ = _stage_journal(
        root,
        after_doc=_changed_doc(root, "AMD"),
        audit_id="wheel-source-readback-failure",
    )
    (root / "config.yaml").write_bytes(after_bytes)
    original_source_sha = publishing.config_source_sha256
    reads = 0

    def _fail_second_source_read(path: str | Path) -> str:
        nonlocal reads
        reads += 1
        if reads == 2:
            raise OSError("injected source readback failure")
        return original_source_sha(path)

    monkeypatch.setattr(publishing, "config_source_sha256", _fail_second_source_read)

    with pytest.raises(AgentToolError) as exc:
        _agent_call(root, apply=True, source_sha=preview["expected_source_sha256"])

    assert exc.value.code == "CONFIG_TRANSACTION_RECOVERY_REQUIRED"
    assert exc.value.details["failure_phase"] == "config_recovery"
    assert exc.value.details["window_receipt"] is None
    assert exc.value.details["write_applied"] is True
    audit = exc.value.details["recovered_transactions"][0]
    assert audit["audit_id"] == "wheel-source-readback-failure"
    assert audit["mode"] == "roll_forward"
    assert audit["write_applied"] is True
    assert audit["cleanup"] is False
    assert manifest.exists()
    assert not _sqlite_path(root).exists()


def test_agent_preserves_completed_recovery_when_later_manifest_read_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _deployment(tmp_path, monkeypatch)
    preview = _agent_call(root)
    first_manifest, _, first_targets = _stage_journal(
        root,
        after_doc=_changed_doc(root, "AMD"),
        audit_id="a-wheel-first-recovery",
    )
    second_manifest, _, _ = _stage_journal(
        root,
        after_doc=_changed_doc(root, "FUTU"),
        audit_id="b-wheel-read-failure",
    )
    first_target = Path(first_targets[0]["path"])
    first_before = first_target.read_bytes()
    first_target.write_bytes(first_targets[0]["payload"])
    original_read_manifest = publishing._read_manifest
    second_reads = 0

    def _fail_second_recovery_read(path: Path) -> dict[str, Any]:
        nonlocal second_reads
        if path == second_manifest:
            second_reads += 1
            if second_reads == 2:
                raise OSError("injected later manifest read failure")
        return original_read_manifest(path)

    monkeypatch.setattr(publishing, "_read_manifest", _fail_second_recovery_read)

    with pytest.raises(AgentToolError) as exc:
        _agent_call(root, apply=True, source_sha=preview["expected_source_sha256"])

    assert exc.value.code == "CONFIG_TRANSACTION_RECOVERY_REQUIRED"
    assert exc.value.details["failure_phase"] == "config_recovery"
    assert exc.value.details["window_receipt"] is None
    assert exc.value.details["write_applied"] is True
    audits = exc.value.details["recovered_transactions"]
    assert audits[0]["audit_id"] == "a-wheel-first-recovery"
    assert audits[0]["write_applied"] is True
    assert audits[1]["audit_id"] == "b-wheel-read-failure"
    assert audits[1]["write_applied"] is None
    assert first_target.read_bytes() == first_before
    assert not first_manifest.exists()
    assert second_manifest.exists()
    assert not _sqlite_path(root).exists()


def test_agent_rejects_pending_target_owner_preflight_before_recovery_or_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _deployment(tmp_path, monkeypatch)
    preview = _agent_call(root)
    extra_target = root / "recovery-only.json"
    extra_target.write_text('{"old":true}\n', encoding="utf-8")
    before_extra = extra_target.read_bytes()
    manifest, _, _ = _stage_journal(
        root,
        after_doc=_changed_doc(root, "AMD"),
        audit_id="wheel-owner-mismatch",
        targets=[
            {
                "role": "runtime_extra",
                "path": extra_target,
                "payload": b'{"new":true}\n',
                "source": False,
            }
        ],
    )
    original_preflight = workflows._activation_owner_preflight
    calls: list[list[Path]] = []

    def _owner_preflight(paths: list[Path], *, runtime_root: Path) -> None:
        calls.append([path.resolve() for path in paths])
        if extra_target.resolve() in calls[-1]:
            raise ValueError("config targets must belong to the deployment user; run as the original owner")
        original_preflight(paths, runtime_root=runtime_root)

    monkeypatch.setattr(workflows, "_activation_owner_preflight", _owner_preflight)

    with pytest.raises(AgentToolError) as exc:
        _agent_call(root, apply=True, source_sha=preview["expected_source_sha256"])

    assert exc.value.details["failure_phase"] == "config_recovery"
    assert exc.value.details["write_applied"] is False
    assert calls[-1] == [extra_target.resolve()]
    assert extra_target.read_bytes() == before_extra
    assert manifest.exists()
    assert not _sqlite_path(root).exists()


@pytest.mark.parametrize("surface", ["agent", "cli"])
def test_public_activation_rejects_missing_existing_target_before_any_recovery(
    surface: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _deployment(tmp_path, monkeypatch)
    preview = _agent_call(root)
    earlier_target = root / "earlier-recovery.json"
    missing_target = root / "missing-recovery.json"
    earlier_target.write_bytes(b'{"old":"earlier"}\n')
    missing_target.write_bytes(b'{"old":"missing"}\n')
    earlier_manifest, _, _ = _stage_journal(
        root,
        after_doc=_changed_doc(root, "AMD"),
        audit_id="a-wheel-earlier-recovery",
        targets=[
            {
                "role": "earlier",
                "path": earlier_target,
                "payload": b'{"new":"earlier"}\n',
            }
        ],
    )
    missing_manifest, _, _ = _stage_journal(
        root,
        after_doc=_changed_doc(root, "FUTU"),
        audit_id="b-wheel-missing-target",
        targets=[
            {
                "role": "missing",
                "path": missing_target,
                "payload": b'{"new":"missing"}\n',
            }
        ],
    )
    earlier_target.write_bytes(b'{"partially-committed":"earlier"}\n')
    watched = {
        path: path.read_bytes()
        for path in (
            root / "config.yaml",
            root / "config.us.json",
            root / "config.hk.json",
            earlier_target,
        )
    }
    missing_target.unlink()
    journal_root = root / "output_shared" / "state" / "config_authoring_transactions"
    journals_before = {
        path.relative_to(journal_root): path.read_bytes()
        for path in journal_root.rglob("*")
        if path.is_file()
    }

    if surface == "agent":
        with pytest.raises(AgentToolError) as exc:
            _agent_call(root, apply=True, source_sha=preview["expected_source_sha256"])
        details = exc.value.details
        assert exc.value.code == "CONFIG_TRANSACTION_RECOVERY_REQUIRED"
    else:
        exit_code, payload = _cli_apply(
            root,
            source_sha=preview["expected_source_sha256"],
            capsys=capsys,
        )
        assert exit_code == 2
        assert payload["error"]["code"] == "CONFIG_TRANSACTION_RECOVERY_REQUIRED"
        details = payload["error"]["details"]

    assert details["failure_phase"] == "config_recovery"
    assert details["window_receipt"] is None
    assert details["current_window"] is None
    assert details["write_applied"] is False
    assert details["target"] == str(missing_target)
    assert details["recovered_transactions"][0]["audit_id"] == "b-wheel-missing-target"
    assert {path: path.read_bytes() for path in watched} == watched
    assert not missing_target.exists()
    assert earlier_manifest.exists()
    assert missing_manifest.exists()
    assert {
        path.relative_to(journal_root): path.read_bytes()
        for path in journal_root.rglob("*")
        if path.is_file()
    } == journals_before
    assert not _sqlite_path(root).exists()


def test_same_request_recovers_pending_journal_after_durable_window_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _deployment(tmp_path, monkeypatch)
    preview = _agent_call(root)
    original_publisher = publishing.publish_yaml_config_generation_locked
    pending: list[Path] = []

    def _stage_then_fail(**kwargs: Any) -> dict[str, Any]:
        manifest, after_bytes, _ = _stage_journal(
            root,
            after_doc=kwargs["config_doc"],
            audit_id="durable-window-pending-config",
        )
        (root / "config.yaml").write_bytes(after_bytes)
        pending.append(manifest)
        raise OSError("injected crash after durable window and source replace")

    monkeypatch.setattr(publishing, "publish_yaml_config_generation_locked", _stage_then_fail)
    with pytest.raises(AgentToolError) as failed:
        _agent_call(root, apply=True, source_sha=preview["expected_source_sha256"])

    assert failed.value.details["failure_phase"] == "config_publish"
    assert failed.value.details["window_receipt"]["write_applied"] is True
    assert failed.value.details["pending_authoring_journal"] is True
    assert pending[0].exists()
    monkeypatch.setattr(publishing, "publish_yaml_config_generation_locked", original_publisher)

    replay = _agent_call(root, apply=True, source_sha=preview["expected_source_sha256"])

    assert replay["status"] == "idempotent"
    assert replay["ready"] is True
    assert replay["window_receipt"]["write_applied"] is False
    assert replay["config_audit"] is None
    assert replay["write_applied"] is True
    assert replay["recovered_transactions"][0]["audit_id"] == "durable-window-pending-config"
    assert replay["recovered_transactions"][0]["mode"] == "roll_forward"
    assert replay["recovered_transactions"][0]["cleanup"] is True
    assert not pending[0].exists()
    windows = read_wheel_activation_windows_read_only(
        _sqlite_path(root),
        market="us",
        account="lx",
    )["windows"]
    assert len(windows) == 1


@pytest.mark.parametrize("surface", ["agent", "cli"])
def test_public_activation_preserves_malformed_target_audit(
    surface: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _deployment(tmp_path, monkeypatch)
    preview = _agent_call(root)
    manifest, _, _ = _stage_journal(
        root, after_doc=_changed_doc(root, "AMD"), audit_id="malformed-target",
    )
    journal = json.loads(manifest.read_text())
    del journal["targets"][0]["path"]
    manifest.write_text(json.dumps(journal))
    watched = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}

    if surface == "agent":
        with pytest.raises(AgentToolError) as exc:
            _agent_call(root, apply=True, source_sha=preview["expected_source_sha256"])
        assert exc.value.code == "CONFIG_TRANSACTION_RECOVERY_REQUIRED"
        details = exc.value.details
    else:
        exit_code, payload = _cli_apply(root, source_sha=preview["expected_source_sha256"], capsys=capsys)
        assert exit_code == 2
        assert payload["error"]["code"] == "CONFIG_TRANSACTION_RECOVERY_REQUIRED"
        details = payload["error"]["details"]

    assert details["failure_phase"] == "config_recovery"
    assert details["manifest"] == str(manifest)
    assert details["stage"] == "recovery"
    assert details["window_receipt"] is None
    assert details["write_applied"] is False
    assert details["recovered_transactions"][0]["audit_id"] == "malformed-target"
    assert details["recovered_transactions"][0]["write_applied"] is False
    assert {path: path.read_bytes() for path in watched} == watched
    assert not _sqlite_path(root).exists()


@pytest.mark.parametrize("surface", ["agent", "cli"])
@pytest.mark.parametrize("failure", ["manifest_prepare", "partial_backup"])
def test_public_activation_preserves_preparation_audit_and_retries_same_window(
    surface: str,
    failure: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _deployment(tmp_path, monkeypatch)
    preview = _agent_call(root)
    watched = {path: path.read_bytes() for path in root.glob("config.*")}
    original_prepare = publishing._prepare_transaction_manifest
    original_copy = publishing.shutil.copy2

    def fail_prepare(**kwargs: Any) -> Path:
        raise OSError("injected manifest preparation failure")

    def partial_copy(source: Path, destination: Path) -> None:
        Path(destination).write_bytes(b"partial backup")
        raise OSError("injected partial backup failure")

    if failure == "manifest_prepare":
        monkeypatch.setattr(publishing, "_prepare_transaction_manifest", fail_prepare)
    else:
        monkeypatch.setattr(publishing.shutil, "copy2", partial_copy)

    if surface == "agent":
        with pytest.raises(AgentToolError) as exc:
            _agent_call(root, apply=True, source_sha=preview["expected_source_sha256"])
        assert exc.value.code == "CONFIG_WRITE_FAILED"
        details = exc.value.details
    else:
        exit_code, payload = _cli_apply(root, source_sha=preview["expected_source_sha256"], capsys=capsys)
        assert exit_code == 2
        assert payload["error"]["code"] == "CONFIG_WRITE_FAILED"
        details = payload["error"]["details"]

    assert details["failure_phase"] == "config_publish"
    assert details["window_receipt"]["write_applied"] is True
    audit = details["config_audit"]
    assert audit["audit_id"].startswith("cfg-")
    assert audit["stage"] == ("transaction_prepare" if failure == "manifest_prepare" else "backup")
    assert audit["backup_write_applied"] is True
    assert audit["transaction_write_applied"] is False
    backup = Path(audit["backup_path"])
    assert backup == root / f"config.yaml.bak.{audit['audit_id']}"
    assert backup.read_bytes() == (watched[root / "config.yaml"] if failure == "manifest_prepare" else b"partial backup")
    assert audit["write_applied"] is True
    assert {path: path.read_bytes() for path in watched} == watched
    first_window = details["window_receipt"]["expected_config_descriptor"]

    monkeypatch.setattr(publishing, "_prepare_transaction_manifest", original_prepare)
    monkeypatch.setattr(publishing.shutil, "copy2", original_copy)
    replay = _agent_call(root, apply=True, source_sha=preview["expected_source_sha256"])
    assert replay["ready"] is True
    assert replay["window_receipt"]["write_applied"] is False
    assert replay["window_receipt"]["expected_config_descriptor"] == first_window
    assert len(read_wheel_activation_windows_read_only(_sqlite_path(root), market="us", account="lx")["windows"]) == 1


def _process_agent_apply(payload: dict[str, Any], start: Any, output: Any) -> None:
    start.wait(10)
    try:
        data, _, _ = position_tools.WHEEL_ACTIVATION_TOOL.call(payload)
    except AgentToolError as exc:
        output.put(("error", exc.code, exc.message, exc.details))
    except Exception as exc:  # pragma: no cover - child diagnostic
        output.put(("unexpected", type(exc).__name__, str(exc), None))
    else:
        output.put(("ok", data, None, None))


def test_two_agent_processes_serialize_activation_and_only_one_window_wins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _deployment(tmp_path, monkeypatch)
    preview = _agent_call(root)
    base_payload = {
        "config_key": "us",
        "config_path": str(root / "config.us.json"),
        "runtime_root": str(root),
        "market": "us",
        "account": "lx",
        "action": "enable",
        "expected_current_generation": 0,
        "actor": "test",
        "expected_source_sha256": preview["expected_source_sha256"],
        "apply": True,
        "confirm": True,
    }
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    output = context.Queue()
    processes = [
        context.Process(
            target=_process_agent_apply,
            args=({**base_payload, "request_id": f"enable-{index}"}, start, output),
        )
        for index in (1, 2)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(15)
    alive = [process for process in processes if process.is_alive()]
    for process in alive:
        process.terminate()
        process.join()
    assert not alive, "activation process did not finish"
    try:
        results = [output.get(timeout=2) for _ in processes]
    except Empty:
        pytest.fail("activation process returned no result")

    assert [item[0] for item in results].count("ok") == 1
    assert [item[0] for item in results].count("error") == 1
    error = next(item for item in results if item[0] == "error")
    assert "generation conflict" in error[2]
    windows = read_wheel_activation_windows_read_only(
        _sqlite_path(root),
        market="us",
        account="lx",
    )["windows"]
    assert len(windows) == 1
    status = position_tools.WHEEL_ACTIVATION_TOOL.call(
        {
            "config_key": "us",
            "config_path": str(root / "config.us.json"),
            "runtime_root": str(root),
            "market": "us",
            "account": "lx",
            "action": "status",
        }
    )[0]
    assert status["ready"] is True
