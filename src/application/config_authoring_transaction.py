from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shlex
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

import yaml

from src.application.agent_tool_contracts import AgentToolError
from src.application.config_primitives import path_for_metadata
from src.application.config_yaml import (
    GENERATED_KEY,
    RESOLVED_KEY,
    resolve_yaml_assistant_config,
    resolve_yaml_runtime_config,
)


TRANSACTION_SCHEMA_VERSION = "om-config-authoring-transaction-v1"


def config_source_sha256(path: str | Path) -> str:
    source = Path(path).expanduser().resolve()
    if not source.exists():
        raise AgentToolError(code="CONFIG_ERROR", message=f"config.yaml not found: {source}")
    return _bytes_sha256(source.read_bytes())


def publish_yaml_config_generation(
    *,
    repo_root: Path,
    config_yaml_path: str | Path,
    config_doc: dict[str, Any],
    runtime_root: str | Path,
    markets: list[str],
    include_assistant: bool = True,
    apply: bool = False,
    backup: bool = True,
    expected_source_sha256: str | None = None,
) -> dict[str, Any]:
    source_path = Path(config_yaml_path).expanduser().resolve()
    target_runtime_root = Path(runtime_root).expanduser().resolve()
    normalized_markets = _normalize_markets(markets)
    observed_source_sha = config_source_sha256(source_path)
    expected_source_sha = str(expected_source_sha256 or "").strip() or observed_source_sha
    if observed_source_sha != expected_source_sha:
        _raise_stale_source(
            source_path=source_path,
            expected_source_sha=expected_source_sha,
            actual_source_sha=observed_source_sha,
        )
    source_bytes = _yaml_bytes(config_doc)
    after_source_sha = _bytes_sha256(source_bytes)
    prepared = _prepare_generation(
        repo_root=repo_root,
        source_path=source_path,
        source_bytes=source_bytes,
        runtime_root=target_runtime_root,
        markets=normalized_markets,
        include_assistant=include_assistant,
    )
    source_sha_after_prepare = config_source_sha256(source_path)
    if source_sha_after_prepare != expected_source_sha:
        _raise_stale_source(
            source_path=source_path,
            expected_source_sha=expected_source_sha,
            actual_source_sha=source_sha_after_prepare,
        )
    result: dict[str, Any] = {
        "schema_version": TRANSACTION_SCHEMA_VERSION,
        "config_yaml_path": str(source_path),
        "runtime_root": str(target_runtime_root),
        "markets": prepared["markets"],
        "assistant": prepared["assistant"],
        "source_revision": {
            "before_sha256": observed_source_sha,
            "after_sha256": after_source_sha,
        },
        "dry_run": not bool(apply),
        "write_applied": False,
        "audit_id": None,
        "backup_path": None,
    }
    if not apply:
        return result

    state_root = target_runtime_root / "output_shared" / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    with _authoring_lock(state_root / "config_authoring.lock"):
        _recover_incomplete_transactions(state_root=state_root)
        before_source_sha = config_source_sha256(source_path)
        if before_source_sha != expected_source_sha:
            _raise_stale_source(
                source_path=source_path,
                expected_source_sha=expected_source_sha,
                actual_source_sha=before_source_sha,
            )

        audit_id = f"cfg-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"
        backup_path = _create_human_backup(source_path, audit_id=audit_id) if backup else None
        targets = [
            *prepared["target_payloads"],
            {
                "role": "config_yaml",
                "path": source_path,
                "payload": source_bytes,
                "source": True,
            },
        ]
        transaction_dir = state_root / "config_authoring_transactions" / audit_id
        manifest_path = _prepare_transaction_manifest(
            transaction_dir=transaction_dir,
            audit_id=audit_id,
            source_path=source_path,
            before_source_sha=before_source_sha,
            after_source_sha=after_source_sha,
            targets=targets,
        )
        try:
            _set_manifest_phase(manifest_path, "committing")
            manifest = _read_manifest(manifest_path)
            ordered_targets = sorted(
                manifest["targets"],
                key=lambda item: bool(item.get("source")),
            )
            for item in ordered_targets:
                _atomic_write_bytes(
                    Path(str(item["path"])),
                    Path(str(item["desired_path"])).read_bytes(),
                )
            _set_manifest_phase(manifest_path, "committed")
        except Exception as exc:
            recovery_error = None
            try:
                _recover_transaction(manifest_path)
            except Exception as recovery_exc:  # pragma: no cover - catastrophic filesystem failure
                recovery_error = f"{type(recovery_exc).__name__}: {recovery_exc}"
            raise AgentToolError(
                code="CONFIG_WRITE_FAILED",
                message="failed to publish config generation",
                details={
                    "audit_id": audit_id,
                    "error": f"{type(exc).__name__}: {exc}",
                    "recovery_error": recovery_error,
                    "transaction_manifest": str(manifest_path),
                },
                hint=(
                    "Retry the same authoring command; transaction recovery runs before the next write."
                    if recovery_error is None
                    else f"Inspect and recover transaction manifest: {manifest_path}"
                ),
            ) from exc
        else:
            shutil.rmtree(transaction_dir)

        result.update(
            {
                "write_applied": True,
                "audit_id": audit_id,
                "backup_path": str(backup_path) if backup_path else None,
                "source_revision": {
                    "before_sha256": before_source_sha,
                    "after_sha256": after_source_sha,
                },
            }
        )
        return result


def _raise_stale_source(
    *,
    source_path: Path,
    expected_source_sha: str,
    actual_source_sha: str,
) -> None:
    raise AgentToolError(
        code="STALE_PREVIEW",
        message="config.yaml changed after it was read; refusing to publish a stale generation",
        details={
            "config_yaml_path": str(source_path),
            "expected_source_sha256": expected_source_sha,
            "actual_source_sha256": actual_source_sha,
        },
        hint="Generate a new preview from the current config.yaml and confirm that operation.",
    )


def _normalize_markets(markets: list[str]) -> list[str]:
    out: list[str] = []
    for raw in markets:
        market = str(raw or "").strip().lower()
        if market not in {"us", "hk"}:
            raise AgentToolError(code="CONFIG_ERROR", message=f"unsupported config market: {raw}")
        if market not in out:
            out.append(market)
    if not out:
        raise AgentToolError(code="CONFIG_ERROR", message="config.yaml must define at least one market")
    return out


def _prepare_generation(
    *,
    repo_root: Path,
    source_path: Path,
    source_bytes: bytes,
    runtime_root: Path,
    markets: list[str],
    include_assistant: bool,
) -> dict[str, Any]:
    source_sha = _bytes_sha256(source_bytes)
    target_payloads: list[dict[str, Any]] = []
    market_results: dict[str, Any] = {}
    assistant_result = None
    with tempfile.TemporaryDirectory(prefix="om-config-generation-") as temp_dir:
        staged_source = Path(temp_dir) / "config.yaml"
        staged_source.write_bytes(source_bytes)
        for market in markets:
            output_path = runtime_root / f"config.{market}.json"
            cfg, _meta = resolve_yaml_runtime_config(
                repo_root=repo_root,
                market=market,
                config_path=staged_source,
            )
            _retarget_runtime_metadata(
                cfg,
                repo_root=repo_root,
                source_path=source_path,
                source_sha=source_sha,
                output_path=output_path,
                market=market,
            )
            payload = _json_bytes(cfg)
            target_payloads.append(
                {
                    "role": f"runtime_{market}",
                    "path": output_path,
                    "payload": payload,
                    "source": False,
                }
            )
            market_results[market] = {
                "ok": True,
                "output_config_path": str(output_path),
                "sha256": _bytes_sha256(payload),
            }

        if include_assistant:
            output_path = runtime_root / "resolved" / "config.assistant.json"
            cfg, _meta = resolve_yaml_assistant_config(
                repo_root=repo_root,
                config_path=staged_source,
            )
            _retarget_assistant_metadata(
                cfg,
                repo_root=repo_root,
                source_path=source_path,
                source_sha=source_sha,
                output_path=output_path,
            )
            payload = _json_bytes(cfg)
            target_payloads.append(
                {
                    "role": "assistant",
                    "path": output_path,
                    "payload": payload,
                    "source": False,
                }
            )
            assistant_result = {
                "ok": True,
                "output_config_path": str(output_path),
                "sha256": _bytes_sha256(payload),
            }
    return {
        "markets": market_results,
        "assistant": assistant_result,
        "target_payloads": target_payloads,
    }


def _retarget_runtime_metadata(
    cfg: dict[str, Any],
    *,
    repo_root: Path,
    source_path: Path,
    source_sha: str,
    output_path: Path,
    market: str,
) -> None:
    source_ref = path_for_metadata(source_path, repo_root=repo_root)
    generated = cfg.get(GENERATED_KEY)
    if isinstance(generated, dict):
        for item in generated.get("sources") or []:
            if isinstance(item, dict) and str(item.get("role") or "") == "market_user":
                item["path"] = source_ref
                item["sha256"] = source_sha
        generated["rebuild_command"] = " ".join(
            shlex.quote(part)
            for part in (
                "./om",
                "config",
                "build",
                "--source",
                "yaml",
                "--market",
                market,
                "--config-yaml",
                str(source_path),
                "--output",
                str(output_path),
            )
        )
    resolved = cfg.get(RESOLVED_KEY)
    if isinstance(resolved, dict):
        resolved["config_yaml_path"] = source_ref
        resolved["config_yaml_sha256"] = source_sha


def _retarget_assistant_metadata(
    cfg: dict[str, Any],
    *,
    repo_root: Path,
    source_path: Path,
    source_sha: str,
    output_path: Path,
) -> None:
    source_ref = path_for_metadata(source_path, repo_root=repo_root)
    generated = cfg.get(GENERATED_KEY)
    if isinstance(generated, dict):
        for item in generated.get("sources") or []:
            if isinstance(item, dict) and str(item.get("role") or "") == "config_yaml":
                item["path"] = source_ref
                item["sha256"] = source_sha
        generated["rebuild_command"] = " ".join(
            shlex.quote(part)
            for part in (
                "./om",
                "config",
                "build-assistant",
                "--source",
                "yaml",
                "--config-yaml",
                str(source_path),
                "--output",
                str(output_path),
            )
        )
    resolved = cfg.get(RESOLVED_KEY)
    if isinstance(resolved, dict):
        resolved["config_yaml_path"] = source_ref
        resolved["config_yaml_sha256"] = source_sha


class _AuthoringLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: BinaryIO | None = None

    def __enter__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> bool:
        handle = self.handle
        self.handle = None
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        return False


def _authoring_lock(path: Path) -> _AuthoringLock:
    return _AuthoringLock(path)


def _prepare_transaction_manifest(
    *,
    transaction_dir: Path,
    audit_id: str,
    source_path: Path,
    before_source_sha: str,
    after_source_sha: str,
    targets: list[dict[str, Any]],
) -> Path:
    transaction_dir.mkdir(parents=True, exist_ok=False)
    manifest_targets: list[dict[str, Any]] = []
    for index, item in enumerate(targets):
        target = Path(item["path"]).expanduser().resolve()
        desired_path = transaction_dir / f"{index:02d}.desired"
        desired_path.write_bytes(bytes(item["payload"]))
        before_exists = target.exists()
        backup_path = transaction_dir / f"{index:02d}.before"
        before_sha = None
        if before_exists:
            before_payload = target.read_bytes()
            backup_path.write_bytes(before_payload)
            before_sha = _bytes_sha256(before_payload)
        manifest_targets.append(
            {
                "role": str(item["role"]),
                "path": str(target),
                "source": bool(item.get("source")),
                "before_exists": before_exists,
                "before_sha256": before_sha,
                "backup_path": str(backup_path) if before_exists else None,
                "desired_path": str(desired_path),
                "after_sha256": _bytes_sha256(bytes(item["payload"])),
            }
        )
    manifest = {
        "schema_version": TRANSACTION_SCHEMA_VERSION,
        "audit_id": audit_id,
        "phase": "prepared",
        "source_path": str(source_path),
        "before_source_sha256": before_source_sha,
        "after_source_sha256": after_source_sha,
        "targets": manifest_targets,
    }
    manifest_path = transaction_dir / "manifest.json"
    _atomic_write_bytes(manifest_path, _json_bytes(manifest))
    return manifest_path


def _set_manifest_phase(path: Path, phase: str) -> None:
    manifest = _read_manifest(path)
    manifest["phase"] = str(phase)
    _atomic_write_bytes(path, _json_bytes(manifest))


def _read_manifest(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != TRANSACTION_SCHEMA_VERSION:
        raise AgentToolError(code="CONFIG_ERROR", message=f"invalid config transaction manifest: {path}")
    return raw


def _recover_incomplete_transactions(*, state_root: Path) -> None:
    transaction_root = state_root / "config_authoring_transactions"
    if not transaction_root.exists():
        return
    for manifest_path in sorted(transaction_root.glob("*/manifest.json")):
        manifest = _read_manifest(manifest_path)
        phase = str(manifest.get("phase") or "")
        if phase == "committed":
            shutil.rmtree(manifest_path.parent)
            continue
        _recover_transaction(manifest_path)


def _recover_transaction(manifest_path: Path) -> None:
    manifest = _read_manifest(manifest_path)
    source_path = Path(str(manifest["source_path"]))
    current_source_sha = config_source_sha256(source_path)
    before_source_sha = str(manifest.get("before_source_sha256") or "")
    after_source_sha = str(manifest.get("after_source_sha256") or "")
    if current_source_sha == after_source_sha:
        mode = "roll_forward"
    elif current_source_sha == before_source_sha:
        mode = "roll_back"
    else:
        raise AgentToolError(
            code="CONFIG_TRANSACTION_RECOVERY_REQUIRED",
            message="config source changed outside an incomplete authoring transaction",
            details={
                "manifest": str(manifest_path),
                "current_source_sha256": current_source_sha,
                "before_source_sha256": before_source_sha,
                "after_source_sha256": after_source_sha,
            },
        )
    for item in manifest.get("targets") or []:
        if not isinstance(item, dict):
            continue
        target = Path(str(item["path"]))
        if mode == "roll_forward":
            _atomic_write_bytes(target, Path(str(item["desired_path"])).read_bytes())
            continue
        if bool(item.get("before_exists")):
            _atomic_write_bytes(target, Path(str(item["backup_path"])).read_bytes())
        elif target.exists():
            target.unlink()
    shutil.rmtree(manifest_path.parent)


def _create_human_backup(source_path: Path, *, audit_id: str) -> Path:
    backup_path = source_path.with_name(f"{source_path.name}.bak.{audit_id}")
    shutil.copy2(source_path, backup_path)
    return backup_path


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    previous_mode = (path.stat().st_mode & 0o777) if path.exists() else None
    fd, raw_temp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(raw_temp_path)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if previous_mode is not None:
            os.chmod(temp_path, previous_mode)
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _fsync_directory(path: Path) -> None:
    flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _yaml_bytes(config_doc: dict[str, Any]) -> bytes:
    text = yaml.safe_dump(config_doc, allow_unicode=True, sort_keys=False, default_flow_style=False)
    if not text.endswith("\n"):
        text += "\n"
    return text.encode("utf-8")


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _bytes_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "TRANSACTION_SCHEMA_VERSION",
    "config_source_sha256",
    "publish_yaml_config_generation",
]
