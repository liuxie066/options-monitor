from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


HELPER_PATH = (
    Path(__file__).resolve().parents[1]
    / "services"
    / "systemd"
    / "options-monitor-materialize-service-credentials"
)


def _load_helper() -> ModuleType:
    loader = importlib.machinery.SourceFileLoader(
        "options_monitor_service_credential_materializer",
        str(HELPER_PATH),
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _secure_store(tmp_path: Path, credential_id: str) -> tuple[Path, Path]:
    store = tmp_path / "credstore.encrypted"
    store.mkdir(mode=0o755)
    source = store / credential_id
    source.write_text("encrypted-fixture", encoding="utf-8")
    source.chmod(0o600)
    return store, source


def test_materializer_writes_only_allowlisted_runtime_files(tmp_path: Path) -> None:
    helper = _load_helper()
    credential_id = "om-llm-deepseek-api-key"
    store, source = _secure_store(tmp_path, credential_id)
    runtime_root = tmp_path / "run" / "options-monitor" / "credentials"
    observed: list[tuple[str, Path, Path]] = []

    def fake_decrypt(name: str, encrypted_path: Path, output_path: Path) -> None:
        observed.append((name, encrypted_path, output_path))
        output_path.write_text("test-secret-value\n", encoding="utf-8")

    result = helper.materialize_credentials(
        unit_name="options-monitor-ai-evidence-collector.service",
        credential_ids=(credential_id,),
        store_root=store,
        runtime_root=runtime_root,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
        required_source_uid=os.getuid(),
        verify_runtime_filesystem=lambda _path: None,
        decrypt_credential=fake_decrypt,
    )

    target_dir = runtime_root / "options-monitor-ai-evidence-collector.service"
    target = target_dir / credential_id
    assert target.read_text(encoding="utf-8") == "test-secret-value\n"
    assert stat.S_IMODE(target_dir.stat().st_mode) == 0o510
    assert stat.S_IMODE(target.stat().st_mode) == 0o400
    assert target.stat().st_uid == os.getuid()
    assert observed == [(credential_id, source, observed[0][2])]
    assert observed[0][2].parent != target_dir
    assert result == {
        "action": "materialize",
        "unit": "options-monitor-ai-evidence-collector.service",
        "credential_count": 1,
        "values_exposed": False,
    }
    assert "test-secret-value" not in json.dumps(result)

    rotated = helper.materialize_credentials(
        unit_name="options-monitor-ai-evidence-collector.service",
        credential_ids=(credential_id,),
        store_root=store,
        runtime_root=runtime_root,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
        required_source_uid=os.getuid(),
        verify_runtime_filesystem=lambda _path: None,
        decrypt_credential=(
            lambda _name, _source, output_path: output_path.write_text(
                "rotated-secret-value\n",
                encoding="utf-8",
            )
        ),
    )
    assert rotated["credential_count"] == 1
    assert target.read_text(encoding="utf-8") == "rotated-secret-value\n"
    assert not any(path.name.startswith(".options-monitor-") for path in runtime_root.iterdir())

    cleaned = helper.cleanup_credentials(
        unit_name="options-monitor-ai-evidence-collector.service",
        credential_ids=(credential_id,),
        runtime_root=runtime_root,
        required_owner_uid=os.getuid(),
    )
    assert cleaned["removed"] is True
    assert not target_dir.exists()


def test_materializer_rejects_symlinked_encrypted_source(tmp_path: Path) -> None:
    helper = _load_helper()
    credential_id = "om-quality-read-token"
    store = tmp_path / "credstore.encrypted"
    store.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("encrypted-fixture", encoding="utf-8")
    (store / credential_id).symlink_to(outside)

    with pytest.raises(helper.MaterializerError, match="regular file"):
        helper.materialize_credentials(
            unit_name="options-monitor-quality-http.service",
            credential_ids=(credential_id,),
            store_root=store,
            runtime_root=tmp_path / "run" / "credentials",
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
            required_source_uid=os.getuid(),
            verify_runtime_filesystem=lambda _path: None,
            decrypt_credential=lambda _name, _source, _target: None,
        )


def test_materializer_rejects_symlinked_encrypted_store_ancestor(
    tmp_path: Path,
) -> None:
    helper = _load_helper()
    credential_id = "om-quality-read-token"
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    store, _source = _secure_store(real_parent, credential_id)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(helper.MaterializerError, match="symbolic links"):
        helper.materialize_credentials(
            unit_name="options-monitor-quality-http.service",
            credential_ids=(credential_id,),
            store_root=linked_parent / store.name,
            runtime_root=tmp_path / "run" / "credentials",
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
            required_source_uid=os.getuid(),
            verify_runtime_filesystem=lambda _path: None,
            decrypt_credential=lambda _name, _source, _target: None,
        )


def test_materializer_cleanup_refuses_unexpected_entries(tmp_path: Path) -> None:
    helper = _load_helper()
    runtime_root = tmp_path / "run" / "credentials"
    target = runtime_root / "options-monitor-quality-http.service"
    target.mkdir(parents=True)
    unexpected = target / "not-a-registered-credential"
    unexpected.write_text("must-remain", encoding="utf-8")

    with pytest.raises(helper.MaterializerError, match="unexpected runtime credential entry"):
        helper.cleanup_credentials(
            unit_name="options-monitor-quality-http.service",
            credential_ids=("om-quality-read-token",),
            runtime_root=runtime_root,
            required_owner_uid=os.getuid(),
        )

    assert unexpected.read_text(encoding="utf-8") == "must-remain"


def test_materializer_decrypt_never_places_plaintext_in_process_arguments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    helper = _load_helper()
    source = tmp_path / "encrypted"
    output = tmp_path / "plaintext"
    source.write_text("encrypted-fixture", encoding="utf-8")
    observed: dict[str, object] = {}

    def fake_run(args, **kwargs):
        observed["args"] = list(args)
        observed["kwargs"] = dict(kwargs)
        Path(args[-1]).write_text("test-secret-value", encoding="utf-8")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(helper.subprocess, "run", fake_run)
    helper._decrypt_with_systemd_creds(
        "om-quality-read-token",
        source,
        output,
    )

    assert output.read_text(encoding="utf-8") == "test-secret-value"
    assert "test-secret-value" not in json.dumps(observed["args"])
    kwargs = observed["kwargs"]
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL


def test_materializer_fails_closed_when_runtime_root_is_not_tmpfs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load_helper()

    monkeypatch.setattr(
        helper.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            stdout="ext4\n",
        ),
    )

    with pytest.raises(helper.MaterializerError, match="must be backed by tmpfs"):
        helper.verify_runtime_filesystem(helper.DEFAULT_RUNTIME_ROOT)


def test_materializer_cli_requires_root_authority(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    helper = _load_helper()
    monkeypatch.setattr(helper.os, "geteuid", lambda: 501)

    rc = helper.main([
        "cleanup",
        "--unit",
        "options-monitor-quality-http.service",
        "--credential-id",
        "om-quality-read-token",
    ])

    assert rc == 78
    assert "requires root authority" in capsys.readouterr().err


def test_materializer_cli_grants_plaintext_only_to_deploy_user_uid(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    helper = _load_helper()
    observed: dict[str, object] = {}

    monkeypatch.setattr(helper.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        helper.pwd,
        "getpwnam",
        lambda _name: SimpleNamespace(pw_uid=1234, pw_gid=2345),
    )

    def fake_materialize(**kwargs):
        observed.update(kwargs)
        return {
            "action": "materialize",
            "unit": kwargs["unit_name"],
            "credential_count": 1,
            "values_exposed": False,
        }

    monkeypatch.setattr(helper, "materialize_credentials", fake_materialize)

    rc = helper.main([
        "materialize",
        "--unit",
        "options-monitor-quality-http.service",
        "--deploy-user",
        "liuxie",
        "--store-root",
        "/etc/credstore.encrypted",
        "--credential-id",
        "om-quality-read-token",
    ])

    assert rc == 0
    assert observed["owner_uid"] == 0
    assert observed["owner_gid"] == 2345
    assert observed["credential_owner_uid"] == 1234
    assert observed["credential_owner_gid"] == 0
    assert json.loads(capsys.readouterr().out)["values_exposed"] is False


@pytest.mark.parametrize(
    "unit_name",
    ("../options-monitor-tick-us.service", "options-monitor/tick.service", "ssh.service"),
)
def test_materializer_rejects_non_options_monitor_unit_names(unit_name: str) -> None:
    helper = _load_helper()

    with pytest.raises(helper.MaterializerError, match="unit name"):
        helper.validate_unit_name(unit_name)
