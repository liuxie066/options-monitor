"""Weekly maintenance for explicitly selected, versioned Linux OpenD services."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import platform
import pwd
import re
import shutil
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET

from src.application.agent_tool_contracts import AgentToolError
from src.application.file_locks import single_instance_lock
from src.application.service_deploy import validate_opend_upgrade_units

_OFFICIAL = "https://www.futunn.com/download/fetch-lasted-link?name=opend-ubuntu"
_PACKAGE = r"Futu_OpenD_(\d+\.\d+\.\d+)_Ubuntu18\.04"
_CURL = ("curl", "-fsS", "--proto", "=https", "--connect-timeout", "10")
_SHANGHAI = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class OpenDInstance:
    unit: str
    current: Path
    port: int
    release: str


def _run(*args: str, timeout: int = 20) -> str:
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=timeout).stdout.strip()


def _version(release: str) -> tuple[int, ...]:
    match = re.fullmatch(_PACKAGE, release)
    if not match:
        raise ValueError("unsupported OpenD version directory")
    return tuple(map(int, match[1].split(".")))


def _version_text(release: str) -> str:
    return ".".join(map(str, _version(release)))


def _instance(unit: str) -> OpenDInstance:
    current = Path(_run("systemctl", "show", unit, "-p", "WorkingDirectory", "--value"))
    if not current.is_absolute() or current.name != "current" or not current.is_symlink():
        raise ValueError(f"{unit} needs a versioned current symlink")
    current = current.parent.resolve() / "current"
    release = os.readlink(current)
    _version(release)
    config = ET.parse(current.parent / "FutuOpenD.xml")
    port = int(config.findtext("api_port") or "0")
    if config.findtext("ip") != "127.0.0.1" or not 1 <= port <= 65535:
        raise ValueError(f"{unit} needs an external loopback OpenD configuration")
    user = _run("systemctl", "show", unit, "-p", "User", "--value")
    if not user or pwd.getpwnam(user).pw_uid != os.geteuid():
        raise ValueError(f"{unit} must run as the same deployment user")
    return OpenDInstance(unit, current, port, release)


def _verify(instance: OpenDInstance, release: str) -> None:
    pid = _run("systemctl", "show", instance.unit, "-p", "MainPID", "--value")
    executable = instance.current.parent / release / "FutuOpenD"
    listener = re.search(rf"\b127\.0\.0\.1:{instance.port}\s+.*?pid=(\d+),",
                         _run("ss", "-ltnpH"))
    if (instance.current.readlink() != Path(release) or not pid.isdecimal() or pid == "0"
            or Path(f"/proc/{pid}/exe").resolve(strict=True) != executable
            or not listener or Path(f"/proc/{listener[1]}/exe").resolve(strict=True) != executable):
        raise RuntimeError(f"{instance.unit} running version or listener mismatch")
    output = _run(
        sys.executable, "-m", "src.infrastructure.opend_watchdog", "--json",
        "--host", "127.0.0.1", "--port", str(instance.port), "--required-capability", "both",
        "--retry-enabled", "--retry-timeout-sec", "25", "--retry-interval-sec", "5",
        "--success-threshold", "2", timeout=60,
    )
    if json.loads(output.splitlines()[-1]).get("ok") is not True:
        raise RuntimeError(f"{instance.unit} OpenD login verification failed")


def _latest() -> tuple[str, str]:
    url = _run(*_CURL, "--head", "--output", "/dev/null", "--write-out", "%{redirect_url}", _OFFICIAL)
    match = re.fullmatch(rf"https://softwaredownload\.futunn\.com/({_PACKAGE})\.tar\.gz", url)
    if not match:
        raise ValueError("unsupported official OpenD package URL")
    return match[1], url


def _stage(instances: list[OpenDInstance], release: str, url: str) -> None:
    for instance in instances:
        if not os.access(instance.current.parent, os.W_OK | os.X_OK):
            raise RuntimeError(f"{instance.unit} installation directory is not writable by the deployment user")
        if os.path.lexists(instance.current.parent / release):
            raise FileExistsError(f"{instance.unit} target release already exists")
        try:
            _run("sudo", "-n", "-l", "systemctl", "restart", instance.unit)
        except subprocess.CalledProcessError:
            raise RuntimeError(f"{instance.unit} non-interactive sudo restart permission is missing") from None
    with TemporaryDirectory(prefix="om-opend-download-") as private:
        directory = Path(private)
        archive = directory / "opend.tar.gz"
        _run(*_CURL, "--max-time", "90", "--max-filesize", "250000000",
             "--output", str(archive), url, timeout=95)
        with tarfile.open(archive, "r:gz") as package:
            package.extractall(directory, filter="data")
        matches = [path.parent for path in directory.rglob("FutuOpenD")
                   if path.is_file() and not path.is_symlink() and path.parent.name == release]
        if len(matches) != 1 or not (matches[0] / "AppData.dat").is_file():
            raise ValueError("unsupported official OpenD archive layout")
        for instance in instances:
            root = instance.current.parent
            with TemporaryDirectory(prefix=".om-opend-stage-", dir=root) as staging:
                candidate = Path(staging) / release
                shutil.copytree(matches[0], candidate, symlinks=True,
                                ignore=shutil.ignore_patterns("FutuOpenD.xml"))
                candidate.rename(root / release)


def _switch(instance: OpenDInstance, release: str) -> None:
    with TemporaryDirectory(prefix=".om-opend-link-", dir=instance.current.parent) as staging:
        link = Path(staging) / "current"
        link.symlink_to(release)
        link.replace(instance.current)


def _restart(instance: OpenDInstance, release: str) -> None:
    _run("sudo", "-n", "systemctl", "restart", instance.unit, timeout=200)
    _verify(instance, release)


def _maintenance_window() -> bool:
    now = datetime.now(_SHANGHAI)
    return now.weekday() == 6 and now.hour == 10 and now.minute < 20


def _require_idle() -> None:
    active = _run("systemctl", "list-units", "--type=service", "--state=running,activating",
                  "--plain", "--no-legend")
    if re.search(r"^options-monitor-(?:upgrade|tick-|auto-close-)", active, re.MULTILINE):
        raise RuntimeError("OM upgrade or market job is running")


def _check(units: list[str], *, confirm: bool, receipt: dict) -> None:
    validate_opend_upgrade_units(units)
    if platform.system() != "Linux" or platform.machine() != "x86_64" or os.geteuid() == 0:
        raise ValueError("OpenD maintenance requires Linux x86_64 and the non-root deployment user")
    instances = [_instance(unit) for unit in units]
    if (len({item.current for item in instances}) != len(instances)
            or len({item.port for item in instances}) != len(instances)):
        raise ValueError("OpenD instances must have separate installation roots and ports")
    receipt["installed"] = {item.unit: _version_text(item.release) for item in instances}
    for instance in instances:
        _verify(instance, instance.release)
    release, url = _latest()
    receipt["official"] = _version_text(release)
    targets = [item for item in instances if _version(item.release) < _version(release)]
    if not targets or not confirm:
        receipt["status"] = "update_available" if targets else "already_current"
        return
    if not _maintenance_window():
        raise RuntimeError("outside the Sunday OpenD maintenance window")
    _require_idle()
    _stage(targets, release, url)
    changed = []
    try:
        for instance in targets:
            _require_idle()
            if not _maintenance_window() or instance.current.readlink() != Path(instance.release):
                raise RuntimeError("maintenance window or active release changed")
            _switch(instance, release)
            changed.append(instance)
            _restart(instance, release)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        receipt.update(status="upgrade_failed", error=type(exc).__name__, rollback_verified={})
        for instance in reversed(changed):
            try:
                _switch(instance, instance.release)
                _restart(instance, instance.release)
                receipt["rollback_verified"][instance.unit] = True
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                receipt["rollback_verified"][instance.unit] = False
        return
    receipt.update(status="upgraded", verified=[item.unit for item in changed])


def run_upgrade(*, units: list[str], runtime_root: Path, confirm: bool) -> dict:
    receipt = {"checked_at": datetime.now(_SHANGHAI).isoformat(), "units": units, "status": "blocked"}
    try:
        if confirm:
            with single_instance_lock(runtime_root / "locks" / "upgrade.lock.guard",
                                      busy_message="another OM service transition is running"):
                _check(units, confirm=True, receipt=receipt)
        else:
            _check(units, confirm=False, receipt=receipt)
    except (AgentToolError, OSError, ValueError, RuntimeError, KeyError,
            ET.ParseError, subprocess.SubprocessError, tarfile.TarError) as exc:
        receipt["error"] = type(exc).__name__
        if type(exc) in (ValueError, RuntimeError, FileExistsError):
            receipt["detail"] = str(exc)
    return receipt
