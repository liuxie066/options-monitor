from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


CURRENT_PYTHON = sys.executable

PYTHON_MINOR = f"{sys.version_info.major}.{sys.version_info.minor}"


def _futu_service_account(
    *,
    host: str = "127.0.0.1",
    port: object = 11111,
    opend_root: str | Path | None = None,
) -> dict[str, object]:
    futu: dict[str, object] = {"host": host, "port": port}
    if opend_root is not None:
        futu["opend_root"] = str(opend_root)
    return {"type": "futu", "futu": futu}


def _write_service_account_config(path: Path, settings: dict[str, dict[str, object]]) -> Path:
    path.write_text(
        json.dumps({"accounts": list(settings), "account_settings": settings}),
        encoding="utf-8",
    )
    return path


def _two_futu_service_accounts(
    tmp_path: Path,
    *,
    lx_port: object = 11111,
    sy_port: object = 11112,
) -> dict[str, dict[str, object]]:
    return {
        "lx": _futu_service_account(port=lx_port, opend_root=tmp_path / "opend-lx"),
        "sy": _futu_service_account(port=sy_port, opend_root=tmp_path / "opend-sy"),
    }


def _write_upgrade_release_skeleton(path: Path, version: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    (path / "configs").mkdir(exist_ok=True)
    (path / "requirements").mkdir(exist_ok=True)
    (path / "constraints").mkdir(exist_ok=True)
    (path / "requirements.txt").write_text("-r requirements/runtime.txt\n", encoding="utf-8")
    (path / "constraints.txt").write_text("-c constraints/release.txt\n", encoding="utf-8")
    (path / "requirements" / "runtime.txt").write_text("", encoding="utf-8")
    (path / "constraints" / "release.txt").write_text("", encoding="utf-8")
    (path / "constraints" / "runtime.txt").write_text("-c release.txt\n", encoding="utf-8")
    (path / "agent-runtime").mkdir(exist_ok=True)
    (path / "agent-runtime" / "package-lock.json").write_text('{"lockfileVersion":3}\n', encoding="utf-8")
    (path / "scripts").mkdir(exist_ok=True)
    smoke = path / "scripts" / "pi_runtime_smoke.sh"
    smoke.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    smoke.chmod(0o755)


def _write_systemd_units_from_bundle(bundle: dict, systemd_root: Path, *, skip: set[str] | None = None) -> None:
    skip = skip or set()
    systemd_root.mkdir(parents=True, exist_ok=True)
    for item in bundle["files"]:
        if item.get("kind") not in {"systemd_service", "systemd_timer"}:
            continue
        name = Path(item["install_path"]).name
        if name in skip:
            continue
        (systemd_root / name).write_text(item["content"], encoding="utf-8")


def _write_runtime_target_with_server_deps(path: Path) -> None:
    _write_upgrade_release_skeleton(path, "1.0.1")
    (path / "requirements" / "server.txt").write_text("", encoding="utf-8")
    (path / "constraints" / "server.txt").write_text("-c release.txt\n", encoding="utf-8")


def _create_fake_venv_python_at(venv_dir: Path) -> None:
    venv_python = venv_dir / "bin" / "python"
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    venv_python.write_text("#!/bin/sh\n", encoding="utf-8")
    venv_python.chmod(0o755)


def _fake_git_cache_materialize(command: list[str], *, version: str = "1.0.1") -> subprocess.CompletedProcess | None:
    pi_runtime = _fake_pi_runtime_prepare(command)
    if pi_runtime is not None:
        return pi_runtime
    if command[:3] == ["git", "clone", "--mirror"]:
        Path(command[-1]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(command, 0, stdout="mirrored\n", stderr="")
    if len(command) >= 3 and command[0] == "git" and str(command[1]).startswith("--git-dir=") and command[2] == "fetch":
        return subprocess.CompletedProcess(command, 0, stdout="fetched\n", stderr="")
    if (
        len(command) >= 4
        and command[0] == "git"
        and str(command[1]).startswith("--git-dir=")
        and command[2] == "archive"
    ):
        tar_path = Path(command[command.index("-o") + 1])
        tar_path.parent.mkdir(parents=True, exist_ok=True)
        tar_path.write_text("fake tar\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="archived\n", stderr="")
    if command[:2] == ["tar", "-xf"]:
        target = Path(command[command.index("-C") + 1])
        _write_upgrade_release_skeleton(target, version)
        (target / "requirements" / "server.txt").write_text("", encoding="utf-8")
        (target / "constraints" / "server.txt").write_text("-c release.txt\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="extracted\n", stderr="")
    return None


def _fake_pi_runtime_prepare(command: list[str]) -> subprocess.CompletedProcess | None:
    if command == ["node", "--version"]:
        return subprocess.CompletedProcess(command, 0, stdout="v22.19.0\n", stderr="")
    if command == ["npm", "--version"]:
        return subprocess.CompletedProcess(command, 0, stdout="10.8.2\n", stderr="")
    if command[:2] == ["npm", "ci"] or command[:2] == ["bash", "scripts/pi_runtime_smoke.sh"]:
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")
    return None


def _fake_release_target_query(
    command: list[str],
    *,
    tags: tuple[str, ...] = ("1.0.0", "1.0.1"),
    remote_url: str = "https://example.invalid/repo.git",
) -> subprocess.CompletedProcess | None:
    if command[:3] == ["git", "config", "--get"]:
        return subprocess.CompletedProcess(command, 0, stdout=f"{remote_url}\n", stderr="")
    if (
        len(command) >= 4
        and command[0] == "git"
        and str(command[1]).startswith("--git-dir=")
        and command[2:4] == ["config", "--get"]
    ):
        return subprocess.CompletedProcess(command, 0, stdout=f"{remote_url}\n", stderr="")
    if (
        len(command) >= 3
        and command[0] == "git"
        and str(command[1]).startswith("--git-dir=")
        and command[2] == "for-each-ref"
    ):
        stdout = "".join(f"{index:x} refs/tags/v{version}\n" for index, version in enumerate(tags, start=1))
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")
    return None


def _install_complete_systemd_bundle(bundle: dict, systemd_root: Path) -> None:
    systemd_root.mkdir(parents=True, exist_ok=True)
    live_root = Path("/etc/systemd/system")
    for item in bundle["files"]:
        kind = str(item.get("kind") or "")
        if kind in {"systemd_service", "systemd_timer"}:
            path = systemd_root / Path(item["install_path"]).name
        elif kind in {"systemd_dropin", "systemd_secret_dropin"}:
            path = systemd_root / Path(item["install_path"]).relative_to(live_root)
        elif kind == "systemd_executable":
            path = Path(item["install_path"])
        else:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(item["content"], encoding="utf-8")
        if item.get("mode") is not None:
            path.chmod(int(item["mode"]))


def _legacy_credential_migration_fixture(monkeypatch, tmp_path: Path) -> dict[str, object]:
    import src.application.service_deploy as service_deploy_module
    import src.application.service_drift as service_drift_module

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    libexec = tmp_path / "libexec"
    legacy_helper = libexec / "options-monitor-materialize-feishu-agent-credential"
    target_helper = libexec / "options-monitor-materialize-service-credentials"
    legacy_env = tmp_path / "run" / "options-monitor-feishu-agent.env"
    repo.mkdir()
    runtime.mkdir()
    monkeypatch.setattr(
        service_deploy_module,
        "DEFAULT_SECRET_CREDENTIAL_HELPER",
        target_helper,
    )
    monkeypatch.setattr(
        service_drift_module,
        "DEFAULT_SECRET_CREDENTIAL_HELPER",
        target_helper,
    )
    bundle = service_deploy_module.render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["us"],
        include_quality_monitoring=True,
        include_feishu_agent_credential=True,
        feishu_agent_credential_helper_path=legacy_helper,
        feishu_agent_credential_env_file=legacy_env,
        use_default_deploy_user=False,
    )
    files = {item["relative_path"]: item for item in bundle["files"]}
    (runtime / "service.profile.json").write_text(
        files["service.profile.json"]["content"],
        encoding="utf-8",
    )
    _install_complete_systemd_bundle(bundle, systemd_root)
    legacy_env.parent.mkdir(parents=True, exist_ok=True)
    legacy_env.write_text("OM_FEISHU_BOT_APP_SECRET=not-a-real-secret\n", encoding="utf-8")
    compat = systemd_root / "options-monitor-quality-http.service.d" / service_drift_module.SECRET_BACKEND_COMPAT_DROPIN
    compat.parent.mkdir(parents=True, exist_ok=True)
    compat.write_text(
        "[Service]\nLoadCredential=\nLoadCredentialEncrypted=\nEnvironment=OM_SECRET_BACKEND=env\n",
        encoding="utf-8",
    )
    return {
        "repo": repo,
        "runtime": runtime,
        "systemd_root": systemd_root,
        "legacy_helper": legacy_helper,
        "target_helper": target_helper,
        "legacy_env": legacy_env,
        "compat": compat,
        "store": tmp_path / "credstore.encrypted",
    }


def _credential_migration_runner(
    calls: list[list[str]],
    *,
    decrypt_ok: bool = True,
    restart_failures: list[int] | None = None,
):  # type: ignore[no-untyped-def]
    failures = restart_failures if restart_failures is not None else []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        command = list(command)
        calls.append(command)
        if command[:2] == ["/usr/bin/findmnt", "--noheadings"]:
            return subprocess.CompletedProcess(command, 0, stdout="tmpfs\n", stderr="")
        if "/usr/bin/systemd-creds" in command:
            return subprocess.CompletedProcess(
                command,
                0 if decrypt_ok else 1,
                stdout="must-not-be-captured",
                stderr="must-not-be-captured",
            )
        if "is-enabled" in command:
            return subprocess.CompletedProcess(command, 0, stdout="enabled\n", stderr="")
        if "is-active" in command:
            return subprocess.CompletedProcess(command, 0, stdout="active\n", stderr="")
        if "--property=Result" in command:
            return subprocess.CompletedProcess(command, 0, stdout="success\n", stderr="")
        if "restart" in command and command[-1].endswith(".service") and failures:
            failures[0] -= 1
            if failures[0] >= 0:
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="failed")
            failures.clear()
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    return _run_cmd
