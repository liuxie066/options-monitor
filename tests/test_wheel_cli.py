import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

import src.application.config_authoring_transaction as config_transaction
import src.application.wheel.workflows as wheel_workflows
import src.interfaces.cli.main as cli_main
import src.interfaces.cli.wheel as wheel_cli
from src.application.agent_tool_contracts import AgentToolError
from src.application.config_authoring_transaction import publish_yaml_config_generation
from src.application.ledger.repository import SQLiteOptionPositionsRepository


REPO_ROOT = Path(__file__).resolve().parents[1]


def _activation_environment(
    tmp_path: Path, *, initialize_db: bool = True
) -> tuple[Path, Path, Path, Path]:
    source = tmp_path / "config.yaml"
    doc = {
        "accounts": {
            "lx": {"type": "futu", "futu_account_id": "12345678"},
            "sy": {"type": "external_holdings", "holdings_account": "sy"},
        },
        "markets": {
            "us": {
                "accounts": ["lx", "sy"],
                "features": {
                    "wheel": {
                        "enabled": False,
                        "accounts": ["sy"],
                        "call": {"min_dte": 21},
                    }
                },
                "symbols": ["NVDA"],
            }
        },
    }
    source.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    publish_yaml_config_generation(
        repo_root=REPO_ROOT,
        config_yaml_path=source,
        config_doc=doc,
        runtime_root=tmp_path,
        markets=["us"],
        include_assistant=False,
        apply=True,
        backup=False,
    )
    runtime = tmp_path / "config.us.json"
    data_config = tmp_path / "portfolio.runtime.json"
    sqlite_path = tmp_path / "output_shared" / "state" / "option_positions.sqlite3"
    if initialize_db:
        SQLiteOptionPositionsRepository(sqlite_path)
    return source, runtime, data_config, sqlite_path


def _activation_args(
    action: str,
    *,
    runtime: Path,
    data_config: Path,
    runtime_root: Path,
    account: str = "lx",
    generation: int = 0,
    request_id: str = "request-1",
    source_sha: str | None = None,
    apply: bool = False,
) -> list[str]:
    values = [
        "activation",
        action,
        "--market",
        "us",
        "--account",
        account,
        "--config",
        str(runtime),
        "--data-config",
        str(data_config),
        "--runtime-root",
        str(runtime_root),
    ]
    if action == "status":
        return values
    values += [
        "--expected-current-generation",
        str(generation),
        "--request-id",
        request_id,
        "--actor",
        "tester",
    ]
    if source_sha:
        values += ["--expected-source-sha256", source_sha]
    if apply:
        values += ["--apply", "--confirm"]
    return values


def _apply_activation(
    action: str,
    *,
    runtime: Path,
    data_config: Path,
    runtime_root: Path,
    generation: int,
    request_id: str,
) -> dict[str, Any]:
    preview = wheel_cli.execute(
        wheel_cli.parse_args(
            _activation_args(
                action,
                runtime=runtime,
                data_config=data_config,
                runtime_root=runtime_root,
                generation=generation,
                request_id=request_id,
            )
        )
    )
    return wheel_cli.execute(
        wheel_cli.parse_args(
            _activation_args(
                action,
                runtime=runtime,
                data_config=data_config,
                runtime_root=runtime_root,
                generation=generation,
                request_id=request_id,
                source_sha=preview["expected_source_sha256"],
                apply=True,
            )
        )
    )


def _prepare_activation_storage_case(
    tmp_path: Path,
    case: str,
) -> tuple[Path, Path, Path, Path]:
    source, runtime, data_config, sqlite_path = _activation_environment(
        tmp_path,
        initialize_db=case in {"available_no_window", "closed", "open"},
    )
    if case == "missing_table":
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(sqlite_path) as conn:
            conn.execute("CREATE TABLE unrelated (value INTEGER)")
    elif case == "unreadable":
        sqlite_path.mkdir(parents=True)
    elif case in {"closed", "open"}:
        _apply_activation(
            "enable",
            runtime=runtime,
            data_config=data_config,
            runtime_root=tmp_path,
            generation=0,
            request_id="status-enable",
        )
    if case == "closed":
        _apply_activation(
            "disable",
            runtime=runtime,
            data_config=data_config,
            runtime_root=tmp_path,
            generation=1,
            request_id="status-disable",
        )
    return source, runtime, data_config, sqlite_path


def _stage_activation_journal(
    runtime_root: Path,
    *,
    symbol: str,
    audit_id: str,
    extra_targets: list[dict[str, Any]] | None = None,
) -> tuple[Path, bytes, list[dict[str, Any]]]:
    source = runtime_root / "config.yaml"
    doc = yaml.safe_load(source.read_text(encoding="utf-8"))
    doc["markets"]["us"]["symbols"].append(symbol)
    after_bytes = config_transaction._yaml_bytes(doc)
    prepared = config_transaction._prepare_generation(
        repo_root=REPO_ROOT,
        source_path=source,
        source_bytes=after_bytes,
        runtime_root=runtime_root,
        markets=["us"],
        include_assistant=False,
    )
    targets = [
        *prepared["target_payloads"],
        *(extra_targets or []),
        {
            "role": "config_yaml",
            "path": source,
            "payload": after_bytes,
            "source": True,
        },
    ]
    manifest = config_transaction._prepare_transaction_manifest(
        transaction_dir=(
            runtime_root
            / "output_shared"
            / "state"
            / "config_authoring_transactions"
            / audit_id
        ),
        audit_id=audit_id,
        source_path=source,
        before_source_sha=config_transaction.config_source_sha256(source),
        after_source_sha=config_transaction._bytes_sha256(after_bytes),
        targets=targets,
    )
    config_transaction._set_manifest_phase(manifest, "committing")
    return manifest, after_bytes, targets


def _run_public_wheel_cli(args: list[str], capsys: pytest.CaptureFixture[str]):
    exit_code = cli_main.main(["wheel", *args, "--format", "json"])
    return exit_code, json.loads(capsys.readouterr().out)


def _deployment_file_bytes(runtime_root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(runtime_root)): path.read_bytes()
        for path in runtime_root.rglob("*")
        if path.is_file()
    }


def _malformed_activation_status_environment(
    tmp_path: Path,
) -> tuple[Path, Path, Path, dict[str, Any]]:
    _source, runtime, data_config, sqlite_path = _activation_environment(tmp_path)
    enabled = _apply_activation(
        "enable",
        runtime=runtime,
        data_config=data_config,
        runtime_root=tmp_path,
        generation=0,
        request_id="malformed-status-enable",
    )
    _stage_activation_journal(
        tmp_path,
        symbol="AMD",
        audit_id="malformed-status-pending",
    )
    config = json.loads(runtime.read_text(encoding="utf-8"))
    config["wheel"]["activation_by_account"]["lx"] = {"generation": 1}
    runtime.write_text(json.dumps(config), encoding="utf-8")
    return runtime, data_config, sqlite_path, enabled["expected_config_descriptor"]


def _end_args(*extra: str):
    return wheel_cli.parse_args(
        [
            "end",
            "--account",
            "lx",
            "--stock-lot-id",
            "assigned-stock-1",
            "--expected-batch-generation-hash",
            "generation-1",
            "--request-id",
            "request-1",
            "--actor",
            "tester",
            "--config-key",
            "us",
            *extra,
        ]
    )


def _branch_args(action: str, *extra: str):
    return wheel_cli.parse_args(
        [
            "branch",
            action,
            "--account",
            "lx",
            "--stock-lot-id",
            "assigned-stock-1",
            "--expected-branch-generation-hash",
            "branch-generation-1",
            "--request-id",
            "request-1",
            "--actor",
            "tester",
            "--config-key",
            "us",
            *extra,
        ]
    )


def _put_linkage_args(action: str, *extra: str):
    args = [
        "linkage",
        action,
        "--account",
        "lx",
        "--wheel-branch-id",
        "wheel-put-1",
        "--direction",
        "put",
        "--expected-branch-generation-hash",
        "generation-1",
        "--request-id",
        "request-1",
        "--actor",
        "tester",
        "--option-record-id",
        "put-lot-1",
        "--linkage-candidate-id",
        "candidate-1",
        "--expected-input-hash",
        "input-1",
        "--config-key",
        "us",
    ]
    if action == "reject":
        args.extend(("--reason", "not this cycle"))
    return wheel_cli.parse_args([*args, *extra])


def _stub_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        wheel_cli,
        "load_runtime_config",
        lambda **_kwargs: (tmp_path / "config.us.json", {"portfolio": {}}),
    )
    monkeypatch.setattr(
        wheel_cli,
        "resolve_position_data_config_path",
        lambda **_kwargs: tmp_path / "portfolio.runtime.json",
    )
    monkeypatch.setattr(
        wheel_cli,
        "open_position_ledger_from_runtime_config",
        lambda **_kwargs: (tmp_path / "portfolio.runtime.json", object()),
    )


def test_wheel_cli_end_previews_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setattr(
        wheel_cli,
        "load_runtime_config",
        lambda **_kwargs: (tmp_path / "config.us.json", {"portfolio": {}}),
    )
    monkeypatch.setattr(
        wheel_cli,
        "resolve_position_data_config_path",
        lambda **_kwargs: tmp_path / "portfolio.runtime.json",
    )
    monkeypatch.setattr(
        wheel_cli,
        "open_position_ledger_from_runtime_config",
        lambda **_kwargs: (tmp_path / "portfolio.runtime.json", object()),
    )

    def _end(_repo, **kwargs):
        calls.append(kwargs)
        return {"dry_run": True, "write_applied": False}

    monkeypatch.setattr(wheel_cli, "end_wheel_lifecycle", _end)

    assert wheel_cli.execute(_end_args()) == {"dry_run": True, "write_applied": False}
    assert calls[0]["apply_changes"] is False
    assert calls[0]["stock_lot_id"] == "assigned-stock-1"


def test_wheel_cli_requires_apply_with_confirmation() -> None:
    with pytest.raises(SystemExit, match="require --apply"):
        wheel_cli.execute(_end_args("--confirm"))


def test_wheel_cli_branch_resolves_legacy_call_alias_and_previews(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub_runtime(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(
        wheel_cli,
        "build_wheel_read_model",
        lambda *_args, **_kwargs: {
            "wheel_branches": [
                {
                    "wheel_branch_id": "wheel-call-1",
                    "direction": "call",
                    "stock_lot_id": "assigned-stock-1",
                }
            ]
        },
    )
    monkeypatch.setattr(
        wheel_cli,
        "resolve_wheel_config",
        lambda *_args, **_kwargs: {
            "market": "us",
            "activation_descriptor": {"generation": 1},
            "policy_sha256": "a" * 64,
        },
    )

    def _decide(_repo, **kwargs):
        calls.append(kwargs)
        return {"dry_run": True, "write_applied": False}

    monkeypatch.setattr(wheel_cli.wheel_application, "decide_wheel_branch", _decide)

    assert wheel_cli.execute(_branch_args("start"))["dry_run"] is True
    assert calls == [
        {
            "account": "lx",
            "wheel_branch_id": "wheel-call-1",
            "decision": "start",
            "expected_branch_generation_hash": "branch-generation-1",
            "request_id": "request-1",
            "actor": "tester",
            "market": "us",
            "apply_changes": False,
            "as_of_ms": calls[0]["as_of_ms"],
            "market": "us",
            "activation_descriptor": {"generation": 1},
            "policy_sha256": "a" * 64,
        }
    ]


def test_wheel_cli_branch_identity_is_exactly_one() -> None:
    with pytest.raises(SystemExit):
        wheel_cli.parse_args(
            [
                "branch",
                "end",
                "--account",
                "lx",
                "--wheel-branch-id",
                "branch-1",
                "--stock-lot-id",
                "lot-1",
                "--expected-branch-generation-hash",
                "generation-1",
                "--request-id",
                "request-1",
                "--actor",
                "tester",
                "--config-key",
                "us",
            ]
        )


def test_wheel_cli_branch_apply_requires_confirmation() -> None:
    with pytest.raises(SystemExit, match="use --confirm or --yes"):
        wheel_cli.execute(_branch_args("end", "--apply"))


def test_wheel_cli_activation_apply_requires_preview_source_sha() -> None:
    args = wheel_cli.parse_args(
        [
            "activation",
            "enable",
            "--market",
            "us",
            "--account",
            "lx",
            "--expected-current-generation",
            "0",
            "--request-id",
            "enable-1",
            "--actor",
            "tester",
            "--apply",
            "--confirm",
        ]
    )

    with pytest.raises(SystemExit, match="expected-source-sha256"):
        wheel_cli.execute(args)


def test_wheel_cli_activation_status_and_enable_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    monkeypatch.setattr(
        wheel_cli,
        "_open_runtime",
        lambda *_args, **_kwargs: pytest.fail("activation must use the shared facade"),
    )

    def _change(**kwargs):
        calls.append(kwargs)
        return {"dry_run": True, "write_applied": False}

    monkeypatch.setattr(wheel_cli.wheel_application, "change_wheel_activation", _change)

    status = wheel_cli.parse_args(
        ["activation", "status", "--market", "us", "--account", "lx"]
    )
    enable = wheel_cli.parse_args(
        [
            "activation",
            "enable",
            "--market",
            "us",
            "--account",
            "lx",
            "--expected-current-generation",
            "0",
            "--request-id",
            "enable-1",
            "--actor",
            "tester",
            "--expected-source-sha256",
            "b" * 64,
        ]
    )

    assert wheel_cli.execute(status)["dry_run"] is True
    assert wheel_cli.execute(enable)["dry_run"] is True
    assert calls == [
        {
            "repo_root": Path(wheel_cli.__file__).resolve().parents[3],
            "action": "status",
            "market": "us",
            "account": "lx",
            "config_path": None,
            "config_key": "us",
            "data_config": None,
            "runtime_root": None,
            "expected_current_generation": None,
            "request_id": None,
            "actor": None,
            "expected_source_sha256": None,
            "apply_changes": False,
        },
        {
            "repo_root": Path(wheel_cli.__file__).resolve().parents[3],
            "action": "enable",
            "market": "us",
            "account": "lx",
            "config_path": None,
            "config_key": "us",
            "data_config": None,
            "runtime_root": None,
            "expected_current_generation": 0,
            "request_id": "enable-1",
            "actor": "tester",
            "expected_source_sha256": "b" * 64,
            "apply_changes": False,
        },
    ]


def test_wheel_cli_activation_json_uses_existing_formatter(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = {
        "status": "closed",
        "paths": {"config_path": "/runtime/config.us.json"},
        "readiness": {"monitoring_gate": "disabled"},
    }
    monkeypatch.setattr(
        wheel_cli.wheel_application,
        "change_wheel_activation",
        lambda **_kwargs: result,
    )

    assert wheel_cli.main(
        [
            "activation",
            "status",
            "--market",
            "us",
            "--account",
            "lx",
            "--format",
            "json",
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out) == result


@pytest.mark.parametrize(
    ("account", "mismatched_runtime_root", "message"),
    [
        ("lx", True, "different deployments"),
        ("unknown", False, "account is not configured"),
    ],
)
def test_public_wheel_cli_maps_activation_input_errors_without_writes(
    account: str,
    mismatched_runtime_root: bool,
    message: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _source, runtime, data_config, sqlite_path = _activation_environment(
        tmp_path,
        initialize_db=False,
    )
    before = {
        str(path.relative_to(tmp_path)): (
            "directory" if path.is_dir() else path.read_bytes()
        )
        for path in tmp_path.rglob("*")
    }
    runtime_root = tmp_path / "other-deployment" if mismatched_runtime_root else tmp_path

    exit_code, payload = _run_public_wheel_cli(
        _activation_args(
            "status",
            runtime=runtime,
            data_config=data_config,
            runtime_root=runtime_root,
            account=account,
        ),
        capsys,
    )

    assert exit_code == 2
    assert payload["ok"] is False
    assert payload["tool_name"] == "wheel"
    assert payload["error"] == {
        "code": "INPUT_ERROR",
        "message": payload["error"]["message"],
    }
    assert message in payload["error"]["message"]
    assert {
        str(path.relative_to(tmp_path)): (
            "directory" if path.is_dir() else path.read_bytes()
        )
        for path in tmp_path.rglob("*")
    } == before
    assert not sqlite_path.exists()


def test_wheel_cli_activation_public_entry_completes_full_lifecycle(
    tmp_path: Path,
) -> None:
    source, runtime, data_config, sqlite_path = _activation_environment(tmp_path)

    enable_preview = wheel_cli.execute(
        wheel_cli.parse_args(
            _activation_args(
                "enable",
                runtime=runtime,
                data_config=data_config,
                runtime_root=tmp_path,
            )
        )
    )
    enabled = wheel_cli.execute(
        wheel_cli.parse_args(
            _activation_args(
                "enable",
                runtime=runtime,
                data_config=data_config,
                runtime_root=tmp_path,
                source_sha=enable_preview["expected_source_sha256"],
                apply=True,
            )
        )
    )
    replayed = wheel_cli.execute(
        wheel_cli.parse_args(
            _activation_args(
                "enable",
                runtime=runtime,
                data_config=data_config,
                runtime_root=tmp_path,
                source_sha=enable_preview["expected_source_sha256"],
                apply=True,
            )
        )
    )

    assert enabled["status"] == "applied"
    assert enabled["ready"] is True
    assert enabled["membership"] is True
    assert enabled["window_receipt"]["expected_config_descriptor"]["generation"] == 1
    assert enabled["config_audit"]["write_applied"] is True
    assert replayed["status"] == "idempotent"
    assert replayed["window_receipt"]["expected_config_descriptor"] == (
        enabled["window_receipt"]["expected_config_descriptor"]
    )
    assert replayed["write_applied"] is False

    disable_preview = wheel_cli.execute(
        wheel_cli.parse_args(
            _activation_args(
                "disable",
                runtime=runtime,
                data_config=data_config,
                runtime_root=tmp_path,
                generation=1,
                request_id="disable-1",
            )
        )
    )
    disabled = wheel_cli.execute(
        wheel_cli.parse_args(
            _activation_args(
                "disable",
                runtime=runtime,
                data_config=data_config,
                runtime_root=tmp_path,
                generation=1,
                request_id="disable-1",
                source_sha=disable_preview["expected_source_sha256"],
                apply=True,
            )
        )
    )
    assert disabled["status"] == "applied"
    assert disabled["ready"] is False
    assert disabled["monitoring_gate"] == "disabled"
    assert disabled["reason_code"] == "closed_window"

    reenable_preview = wheel_cli.execute(
        wheel_cli.parse_args(
            _activation_args(
                "enable",
                runtime=runtime,
                data_config=data_config,
                runtime_root=tmp_path,
                generation=1,
                request_id="enable-2",
            )
        )
    )
    reenabled = wheel_cli.execute(
        wheel_cli.parse_args(
            _activation_args(
                "enable",
                runtime=runtime,
                data_config=data_config,
                runtime_root=tmp_path,
                generation=1,
                request_id="enable-2",
                source_sha=reenable_preview["expected_source_sha256"],
                apply=True,
            )
        )
    )
    assert reenabled["status"] == "applied"
    assert reenabled["ready"] is True
    assert reenabled["latest_window"]["generation"] == 2
    history = SQLiteOptionPositionsRepository(sqlite_path).list_wheel_activation_windows(
        market="us", account="lx"
    )
    assert len(history) == 2
    assert history[0]["deactivated_at_ms"] is not None
    assert history[1]["deactivated_at_ms"] is None
    wheel = yaml.safe_load(source.read_text(encoding="utf-8"))["markets"]["us"][
        "features"
    ]["wheel"]
    assert set(wheel["accounts"]) == {"lx", "sy"}
    assert wheel["call"]["min_dte"] == 21


def test_public_wheel_cli_preserves_committed_window_config_failure_and_retries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _source, runtime, data_config, sqlite_path = _activation_environment(
        tmp_path, initialize_db=False
    )
    preview = wheel_cli.execute(
        wheel_cli.parse_args(
            _activation_args(
                "enable",
                runtime=runtime,
                data_config=data_config,
                runtime_root=tmp_path,
            )
        )
    )
    publisher = config_transaction.publish_yaml_config_generation_locked
    monkeypatch.setattr(
        config_transaction,
        "publish_yaml_config_generation_locked",
        lambda **_kwargs: (_ for _ in ()).throw(
            AgentToolError(
                code="CONFIG_WRITE_FAILED",
                message="injected config interruption",
                details={"write_applied": False, "targets": []},
            )
        ),
    )

    args = _activation_args(
        "enable",
        runtime=runtime,
        data_config=data_config,
        runtime_root=tmp_path,
        source_sha=preview["expected_source_sha256"],
        apply=True,
    )
    exit_code, payload = _run_public_wheel_cli(args, capsys)

    assert exit_code == 2
    assert payload["ok"] is False
    assert payload["tool_name"] == "wheel"
    assert payload["error"]["code"] == "CONFIG_WRITE_FAILED"
    details = payload["error"]["details"]
    assert details["failure_phase"] == "config_publish"
    assert details["window_receipt"]["write_applied"] is True
    assert details["config_audit"] == {"write_applied": False, "targets": []}
    assert details["write_applied"] is True
    assert details["original_request"] == {
        "action": "enable",
        "market": "us",
        "account": "lx",
        "request_id": "request-1",
        "actor": "tester",
        "expected_current_generation": 0,
        "policy_sha256": details["original_request"]["policy_sha256"],
    }
    assert "same request_id" in details["retry_hint"]

    monkeypatch.setattr(
        config_transaction,
        "publish_yaml_config_generation_locked",
        publisher,
    )
    retry_preview = wheel_cli.execute(wheel_cli.parse_args(args[:-2]))
    retried = wheel_cli.execute(
        wheel_cli.parse_args(
            _activation_args(
                "enable",
                runtime=runtime,
                data_config=data_config,
                runtime_root=tmp_path,
                source_sha=retry_preview["expected_source_sha256"],
                apply=True,
            )
        )
    )
    rows = SQLiteOptionPositionsRepository(sqlite_path).list_wheel_activation_windows(
        market="us", account="lx"
    )
    assert retry_preview["status"] == "configuration_pending"
    assert retried["ready"] is True
    assert retried["window_receipt"]["write_applied"] is False
    assert len(rows) == 1


def test_public_wheel_cli_preserves_readback_failure_and_idempotent_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _source, runtime, data_config, sqlite_path = _activation_environment(tmp_path)
    preview = wheel_cli.execute(
        wheel_cli.parse_args(
            _activation_args(
                "enable",
                runtime=runtime,
                data_config=data_config,
                runtime_root=tmp_path,
            )
        )
    )
    original_status = wheel_workflows._activation_status
    failed = False

    def _fail_after_publish(cfg, **kwargs):
        nonlocal failed
        if cfg["wheel"].get("activation_by_account") and not failed:
            failed = True
            raise OSError("injected readback failure")
        return original_status(cfg, **kwargs)

    monkeypatch.setattr(wheel_workflows, "_activation_status", _fail_after_publish)
    args = _activation_args(
        "enable",
        runtime=runtime,
        data_config=data_config,
        runtime_root=tmp_path,
        source_sha=preview["expected_source_sha256"],
        apply=True,
    )

    exit_code, payload = _run_public_wheel_cli(args, capsys)

    assert exit_code == 2
    details = payload["error"]["details"]
    assert details["failure_phase"] == "readback"
    assert details["window_receipt"]["write_applied"] is True
    assert details["config_audit"]["write_applied"] is True
    assert details["write_applied"] is True
    assert details["original_request"]["request_id"] == "request-1"
    assert "same request_id" in details["retry_hint"]

    retried = wheel_cli.execute(wheel_cli.parse_args(args))
    rows = SQLiteOptionPositionsRepository(sqlite_path).list_wheel_activation_windows(
        market="us", account="lx"
    )
    assert retried["status"] == "idempotent"
    assert retried["write_applied"] is False
    assert len(rows) == 1


@pytest.mark.parametrize(
    ("case", "expected_status", "storage_status"),
    [
        ("missing_database", "unavailable", "missing_database"),
        ("missing_table", "unavailable", "missing_table"),
        ("unreadable", "unavailable", "unreadable"),
        ("available_no_window", "no_window", "available"),
        ("open", "open", "available"),
        ("closed", "closed", "available"),
    ],
)
def test_public_wheel_cli_activation_status_distinguishes_storage_and_window_state(
    case: str,
    expected_status: str,
    storage_status: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _source, runtime, data_config, sqlite_path = _prepare_activation_storage_case(
        tmp_path,
        case,
    )

    exit_code, result = _run_public_wheel_cli(
        _activation_args(
            "status",
            runtime=runtime,
            data_config=data_config,
            runtime_root=tmp_path,
        ),
        capsys,
    )

    assert exit_code == 0
    assert result["status"] == expected_status
    assert result["storage_status"] == storage_status
    assert result["source_status"] == "available"
    assert result["pending_authoring_journal"] is False
    if case == "missing_database":
        assert not sqlite_path.exists()
        assert not sqlite_path.with_name(sqlite_path.name + "-wal").exists()
        assert not sqlite_path.with_name(sqlite_path.name + "-shm").exists()


def test_public_wheel_cli_status_preserves_known_window_for_malformed_descriptor(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime, data_config, _sqlite_path, expected_window = (
        _malformed_activation_status_environment(tmp_path)
    )
    before = _deployment_file_bytes(tmp_path)

    exit_code, status = _run_public_wheel_cli(
        _activation_args(
            "status",
            runtime=runtime,
            data_config=data_config,
            runtime_root=tmp_path,
        ),
        capsys,
    )

    assert exit_code == 0
    assert status["current_window"] == expected_window
    assert status["latest_window"] == expected_window
    assert status["membership"] is True
    assert status["ready"] is False
    assert status["monitoring_gate"] == "config_mismatch"
    assert status["reason_code"] == "descriptor_mismatch"
    assert status["pending_authoring_journal"] is True
    assert _deployment_file_bytes(tmp_path) == before


def test_public_wheel_cli_rolls_forward_journal_before_stale_preview_rejection(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _source, runtime, data_config, sqlite_path = _activation_environment(
        tmp_path, initialize_db=False
    )
    preview = wheel_cli.execute(
        wheel_cli.parse_args(
            _activation_args(
                "enable",
                runtime=runtime,
                data_config=data_config,
                runtime_root=tmp_path,
            )
        )
    )
    manifest, after_bytes, targets = _stage_activation_journal(
        tmp_path,
        symbol="AMD",
        audit_id="cli-roll-forward",
    )
    (tmp_path / "config.yaml").write_bytes(after_bytes)

    exit_code, payload = _run_public_wheel_cli(
        _activation_args(
            "enable",
            runtime=runtime,
            data_config=data_config,
            runtime_root=tmp_path,
            source_sha=preview["expected_source_sha256"],
            apply=True,
        ),
        capsys,
    )

    assert exit_code == 2
    assert payload["error"]["code"] == "STALE_PREVIEW"
    details = payload["error"]["details"]
    assert details["failure_phase"] == "source_validation"
    assert details["window_receipt"] is None
    assert details["write_applied"] is True
    assert details["pending_authoring_journal"] is False
    assert details["recovered_transactions"][0]["mode"] == "roll_forward"
    for target in targets:
        assert Path(target["path"]).read_bytes() == target["payload"]
    assert not manifest.exists()
    assert not sqlite_path.exists()


def test_public_wheel_cli_rolls_back_journal_before_stale_preview_rejection(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, runtime, data_config, sqlite_path = _activation_environment(
        tmp_path, initialize_db=False
    )
    stale_preview = wheel_cli.execute(
        wheel_cli.parse_args(
            _activation_args(
                "enable",
                runtime=runtime,
                data_config=data_config,
                runtime_root=tmp_path,
            )
        )
    )
    current_doc = yaml.safe_load(source.read_text(encoding="utf-8"))
    current_doc["markets"]["us"]["symbols"].append("AMD")
    publish_yaml_config_generation(
        repo_root=REPO_ROOT,
        config_yaml_path=source,
        config_doc=current_doc,
        runtime_root=tmp_path,
        markets=["us"],
        include_assistant=False,
        apply=True,
        backup=False,
    )
    before_runtime = runtime.read_bytes()
    manifest, _after_bytes, targets = _stage_activation_journal(
        tmp_path,
        symbol="FUTU",
        audit_id="cli-roll-back",
    )
    for target in targets:
        if not target.get("source"):
            Path(target["path"]).write_bytes(target["payload"])

    exit_code, payload = _run_public_wheel_cli(
        _activation_args(
            "enable",
            runtime=runtime,
            data_config=data_config,
            runtime_root=tmp_path,
            source_sha=stale_preview["expected_source_sha256"],
            apply=True,
        ),
        capsys,
    )

    assert exit_code == 2
    assert payload["error"]["code"] == "STALE_PREVIEW"
    details = payload["error"]["details"]
    assert details["write_applied"] is True
    assert details["recovered_transactions"][0]["mode"] == "roll_back"
    assert details["recovered_transactions"][0]["cleanup"] is True
    assert runtime.read_bytes() == before_runtime
    assert not manifest.exists()
    assert not sqlite_path.exists()


def test_public_wheel_cli_reports_partial_recovery_without_writing_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _source, runtime, data_config, sqlite_path = _activation_environment(
        tmp_path, initialize_db=False
    )
    preview = wheel_cli.execute(
        wheel_cli.parse_args(
            _activation_args(
                "enable",
                runtime=runtime,
                data_config=data_config,
                runtime_root=tmp_path,
            )
        )
    )
    extra_target = tmp_path / "recovery-extra.json"
    extra_target.write_bytes(b'{"old":true}\n')
    manifest, after_bytes, _targets = _stage_activation_journal(
        tmp_path,
        symbol="AMD",
        audit_id="cli-partial-recovery",
        extra_targets=[
            {
                "role": "runtime_extra",
                "path": tmp_path / "recovery-extra.json",
                "payload": b'{"new":true}\n',
                "source": False,
            }
        ],
    )
    (tmp_path / "config.yaml").write_bytes(after_bytes)
    writer = config_transaction._atomic_write_bytes

    def _fail_extra_target(path: Path, payload: bytes) -> None:
        if path.resolve() == extra_target.resolve():
            raise OSError("injected recovery interruption")
        writer(path, payload)

    monkeypatch.setattr(
        config_transaction,
        "_atomic_write_bytes",
        _fail_extra_target,
    )

    exit_code, payload = _run_public_wheel_cli(
        _activation_args(
            "enable",
            runtime=runtime,
            data_config=data_config,
            runtime_root=tmp_path,
            source_sha=preview["expected_source_sha256"],
            apply=True,
        ),
        capsys,
    )

    assert exit_code == 2
    assert payload["error"]["code"] == "CONFIG_TRANSACTION_RECOVERY_REQUIRED"
    details = payload["error"]["details"]
    assert details["failure_phase"] == "config_recovery"
    assert details["window_receipt"] is None
    assert details["write_applied"] is True
    recovery = details["recovered_transactions"][0]
    assert recovery["targets"][0]["write_applied"] is True
    assert any(target["write_applied"] is None for target in recovery["targets"])
    assert recovery["cleanup"] is False
    assert extra_target.read_bytes() == b'{"old":true}\n'
    assert manifest.exists()
    assert not sqlite_path.exists()


def test_wheel_cli_put_linkage_reject_previews_canonical_branch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub_runtime(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(
        wheel_cli,
        "build_wheel_read_model",
        lambda *_args, **_kwargs: {
            "wheel_branches": [
                {
                    "wheel_branch_id": "wheel-put-1",
                    "direction": "put",
                    "stock_lot_id": None,
                }
            ]
        },
    )

    def _reject(_repo, **kwargs):
        calls.append(kwargs)
        return {"dry_run": True, "write_applied": False}

    monkeypatch.setattr(wheel_cli, "reject_wheel_linkage", _reject)

    assert wheel_cli.execute(_put_linkage_args("reject"))["dry_run"] is True
    assert calls[0] == {
        "account": "lx",
        "wheel_branch_id": "wheel-put-1",
        "direction": "put",
        "expected_branch_generation_hash": "generation-1",
        "request_id": "request-1",
        "actor": "tester",
        "market": "us",
        "apply_changes": False,
        "as_of_ms": calls[0]["as_of_ms"],
        "option_record_id": "put-lot-1",
        "linkage_candidate_id": "candidate-1",
        "expected_input_hash": "input-1",
        "reason": "not this cycle",
    }


def test_wheel_cli_put_rejects_legacy_stock_lot_alias(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _stub_runtime(monkeypatch, tmp_path)
    args = _put_linkage_args("reject")
    args.wheel_branch_id = None
    args.stock_lot_id = "legacy-lot"
    with pytest.raises(ValueError, match="Call-only alias"):
        wheel_cli.execute(args)
