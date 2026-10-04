"""Author only the ordinary environment fields used by optional CLI features."""
from __future__ import annotations

import fcntl
import os
import stat
import tempfile
from hashlib import sha256
from pathlib import Path
from typing import Mapping

from src.application.agent_tool_contracts import AgentToolError
from src.application.settings.effective import parse_env_file
from src.infrastructure.portfolio_management_client import PortfolioManagementConfigError, resolve_portfolio_service_origin

ALLOWED_ENV_FIELDS = frozenset({"OM_FEISHU_BOT_APP_ID", "OM_FEISHU_BOT_USER_OPEN_ID",
                                "OM_FEISHU_BOT_ALLOWED_OPEN_IDS", "PORTFOLIO_SERVICE_URL"})


def feature_env_path(runtime_root: Path, env_file: str | Path | None = None) -> Path:
    return Path(env_file).expanduser().absolute() if env_file else runtime_root / "options-monitor.env"


def _read(path: Path) -> bytes:
    try:
        if path.is_symlink():
            raise AgentToolError(code="CONFIG_ERROR", message="env-file must not be a symbolic link")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise AgentToolError(code="CONFIG_ERROR", message="env-file must belong to the deployment user")
            return stream.read()
    except FileNotFoundError:
        return b""
    except OSError as exc:
        raise AgentToolError(code="CONFIG_ERROR", message=f"Cannot read ordinary env file: {exc}",
                             details={"env_file": str(path)}) from exc


def env_source_sha256(path: Path) -> str:
    return sha256(_read(path)).hexdigest()


def _updated(data: bytes, updates: Mapping[str, str]) -> bytes:
    unknown = set(updates) - ALLOWED_ENV_FIELDS
    if unknown:
        raise AgentToolError(code="INPUT_ERROR", message="unsupported ordinary env fields", details={"fields": sorted(unknown)})
    values = {key: str(value).strip() for key, value in updates.items()}
    if any("\r" in str(value) or "\n" in str(value) or "\0" in str(value) for value in updates.values()):
        raise AgentToolError(code="INPUT_ERROR", message="env values must fit on one line")
    if "PORTFOLIO_SERVICE_URL" in values:
        try:
            values["PORTFOLIO_SERVICE_URL"] = resolve_portfolio_service_origin(values["PORTFOLIO_SERVICE_URL"])
        except PortfolioManagementConfigError as exc:
            raise AgentToolError(code="INPUT_ERROR", message=str(exc)) from exc
    try:
        lines = data.decode("utf-8").splitlines(keepends=True)
    except UnicodeError as exc:
        raise AgentToolError(code="CONFIG_ERROR", message="env-file must contain UTF-8 text") from exc
    seen: set[str] = set()
    result = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        name = stripped.split("=", 1)[0].strip() if "=" in stripped and not stripped.startswith("#") else ""
        if name in values:
            if name in seen:
                raise AgentToolError(code="CONFIG_ERROR", message=f"duplicate env field: {name}; resolve ambiguity before editing")
            seen.add(name)
            result.append(_line(name, values[name]))
        else:
            result.append(line)
    if result and not result[-1].endswith("\n"):
        result[-1] += "\n"
    result.extend(_line(name, value) for name, value in values.items() if name not in seen)
    candidate = "".join(result)
    parsed = parse_env_file(candidate)
    if any(parsed.get(key) != value for key, value in values.items()):
        raise AgentToolError(code="CONFIG_ERROR", message="env values cannot be represented safely")
    return candidate.encode("utf-8")


def _line(key: str, value: str) -> str:
    # systemd EnvironmentFile and OM's parser both accept this quoted representation.
    return f'{key}="' + value.replace("\\", "\\\\").replace('"', '\\"') + '"\n'


def _atomic(path: Path, data: bytes) -> None:
    descriptor, raw = tempfile.mkstemp(prefix=".om-env-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(raw, path)
    finally:
        if os.path.exists(raw):
            os.unlink(raw)


def write_feature_env(*, path: Path, updates: Mapping[str, str], apply: bool = False,
                      expected_source_sha256: str | None = None) -> dict:
    before = _read(path)
    before_sha = sha256(before).hexdigest()
    if expected_source_sha256 is not None and before_sha != expected_source_sha256:
        raise AgentToolError(code="STALE_PREVIEW", message="env-file changed after preview")
    after = _updated(before, updates)
    result = {"env_file": str(path), "fields": sorted(updates), "changes": dict(updates), "source_revision": {
        "before_sha256": before_sha, "after_sha256": sha256(after).hexdigest()},
        "write_applied": False, "dry_run": not apply, "changed": before != after,
        "backup_path": None, "secret_values_exposed": False, "restart_performed": False}
    if not apply:
        return result
    if not expected_source_sha256:
        raise AgentToolError(code="CONFIRMATION_REQUIRED", message="env apply requires the source SHA from preview")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(path.name + ".lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "rb") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if env_source_sha256(path) != before_sha:
                raise AgentToolError(code="STALE_PREVIEW", message="env-file changed before publication")
            if path.exists():
                backup = path.with_name(path.name + ".bak." + before_sha[:16])
                if backup.exists() and _read(backup) != before:
                    raise AgentToolError(code="CONFIG_ERROR", message="env backup content conflicts")
                _atomic(backup, before)
                result["backup_path"] = str(backup)
            _atomic(path, after)
            result["write_applied"] = True
            if _read(path) != after or stat.S_IMODE(path.stat().st_mode) != 0o600:
                raise AgentToolError(code="CONFIG_READBACK_FAILED", message="env publication readback failed")
    except AgentToolError as exc:
        raise AgentToolError(code=exc.code, message=exc.message, hint=exc.hint,
                             details={**result, **(exc.details or {})}) from exc
    except OSError as exc:
        raise AgentToolError(code="CONFIG_ERROR", message=f"Cannot publish ordinary env file: {exc}",
                             details=result, hint="Inspect the env file and its backup before retrying") from exc
    result["verified"] = True
    return result
