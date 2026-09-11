from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable

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


def validate_config_target_access_identity(paths: list[Path]) -> None:
    for path in dict.fromkeys(path.expanduser().resolve() for path in paths):
        if not path.exists():
            continue
        original = path.stat()
        if not stat.S_ISREG(original.st_mode) or original.st_uid != os.geteuid():
            raise ValueError(
                "config targets must belong to the deployment user; run as the original owner"
            )
        if stat.S_IMODE(original.st_mode) != (original.st_mode & 0o777):
            raise ValueError("special config mode requires deployment owner handling")
        with tempfile.NamedTemporaryFile(prefix=".om-owner-", dir=path.parent) as probe:
            identity = os.fstat(probe.fileno())
            if (identity.st_uid, identity.st_gid) != (original.st_uid, original.st_gid):
                raise ValueError(
                    "replacement file cannot preserve deployment uid/gid; use the original deployment user"
                )
            for candidate in (path, Path(probe.name)):
                if sys.platform == "darwin":
                    acl = subprocess.run(
                        ["ls", "-lde", str(candidate)],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    if len(acl.stdout.splitlines()) != 1:
                        raise ValueError("custom config ACL requires deployment owner handling")
                elif hasattr(os, "listxattr"):
                    if any("acl" in name.lower() for name in os.listxattr(candidate)):
                        raise ValueError("custom config ACL requires deployment owner handling")
                else:
                    raise ValueError("cannot verify config access identity on this platform")


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
    target_runtime_root = Path(runtime_root).expanduser().resolve()
    if not apply:
        return _publish_yaml_config_generation(
            repo_root=repo_root,
            config_yaml_path=config_yaml_path,
            config_doc=config_doc,
            runtime_root=target_runtime_root,
            markets=markets,
            include_assistant=include_assistant,
            apply=False,
            backup=backup,
            expected_source_sha256=expected_source_sha256,
            recovered_transactions=[],
        )

    with locked_config_authoring(runtime_root=target_runtime_root) as lock:
        return publish_yaml_config_generation_locked(
            lock=lock,
            repo_root=repo_root,
            config_yaml_path=config_yaml_path,
            config_doc=config_doc,
            runtime_root=target_runtime_root,
            markets=markets,
            include_assistant=include_assistant,
            backup=backup,
            expected_source_sha256=expected_source_sha256,
        )


def publish_yaml_config_generation_locked(
    *,
    lock: _AuthoringLock,
    repo_root: Path,
    config_yaml_path: str | Path,
    config_doc: dict[str, Any],
    runtime_root: str | Path,
    markets: list[str],
    include_assistant: bool = True,
    backup: bool = True,
    expected_source_sha256: str | None = None,
) -> dict[str, Any]:
    target_runtime_root = Path(runtime_root).expanduser().resolve()
    _require_live_authoring_lock(lock, runtime_root=target_runtime_root)
    try:
        return _publish_yaml_config_generation(
            repo_root=repo_root,
            config_yaml_path=config_yaml_path,
            config_doc=config_doc,
            runtime_root=target_runtime_root,
            markets=markets,
            include_assistant=include_assistant,
            apply=True,
            backup=backup,
            expected_source_sha256=expected_source_sha256,
            recovered_transactions=lock.recovered_transactions,
        )
    except AgentToolError as exc:
        enriched = _with_recovery_details(exc, lock.recovered_transactions)
        if enriched is exc:
            raise
        raise enriched from exc
    except Exception as exc:
        if not lock.recovered_transactions:
            raise
        enriched = AgentToolError(
            code="CONFIG_WRITE_FAILED",
            message="failed to publish config generation after transaction recovery",
            details={
                "error": f"{type(exc).__name__}: {exc}",
                "write_applied": None,
            },
        )
        raise _with_recovery_details(enriched, lock.recovered_transactions) from exc


def _publish_yaml_config_generation(
    *,
    repo_root: Path,
    config_yaml_path: str | Path,
    config_doc: dict[str, Any],
    runtime_root: Path,
    markets: list[str],
    include_assistant: bool,
    apply: bool,
    backup: bool,
    expected_source_sha256: str | None,
    recovered_transactions: list[dict[str, Any]],
) -> dict[str, Any]:
    source_path = Path(config_yaml_path).expanduser().resolve()
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
        runtime_root=runtime_root,
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
        "runtime_root": str(runtime_root),
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
        "recovered_transactions": list(recovered_transactions),
    }
    if not apply:
        return result

    state_root = runtime_root / "output_shared" / "state"
    before_source_sha = config_source_sha256(source_path)
    if before_source_sha != expected_source_sha:
        _raise_stale_source(
            source_path=source_path,
            expected_source_sha=expected_source_sha,
            actual_source_sha=before_source_sha,
        )

    audit_id = f"cfg-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"
    backup_path = source_path.with_name(f"{source_path.name}.bak.{audit_id}") if backup else None
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
    manifest_path = transaction_dir / "manifest.json"
    published_targets: list[dict[str, Any]] = []
    backup_write_applied: bool | None = False
    transaction_write_applied: bool | None = False
    failure_stage = "backup" if backup else "transaction_prepare"
    try:
        if backup:
            _create_human_backup(source_path, audit_id=audit_id)
            backup_write_applied = True
        failure_stage = "transaction_prepare"
        manifest_path = _prepare_transaction_manifest(
            transaction_dir=transaction_dir,
            audit_id=audit_id,
            source_path=source_path,
            before_source_sha=before_source_sha,
            after_source_sha=after_source_sha,
            targets=targets,
        )
        transaction_write_applied = True
        failure_stage = "commit"
        _set_manifest_phase(manifest_path, "committing")
        manifest = _read_manifest(manifest_path)
        validated_targets = _validated_manifest_targets(manifest)
        ordered_targets = sorted(
            validated_targets,
            key=lambda item: bool(item.get("source")),
        )
        for item in ordered_targets:
            effect = {
                "role": str(item.get("role") or ""),
                "path": str(item["path"]),
                "write_applied": None,
            }
            published_targets.append(effect)
            _atomic_write_bytes(Path(str(item["path"])), item["desired_payload"])
            effect["write_applied"] = True
        _set_manifest_phase(manifest_path, "committed")
    except Exception as exc:
        compensation: list[dict[str, Any]] = []
        recovery_error = None
        if backup and backup_write_applied is not True:
            backup_write_applied = _path_exists(backup_path)
        if transaction_write_applied is not True:
            transaction_write_applied = _path_exists(transaction_dir)
        manifest_exists = _path_exists(manifest_path)
        if manifest_exists is True:
            try:
                compensation.append(_recover_transaction(manifest_path))
            except Exception as recovery_exc:  # pragma: no cover - catastrophic filesystem failure
                recovery_error = f"{type(recovery_exc).__name__}: {recovery_exc}"
                if isinstance(recovery_exc, AgentToolError) and isinstance(recovery_exc.details, dict):
                    compensation.extend(recovery_exc.details.get("recovered_transactions") or [])
        elif manifest_exists is None:
            recovery_error = f"unable to determine whether transaction manifest exists: {manifest_path}"
        raise AgentToolError(
            code="CONFIG_WRITE_FAILED",
            message="failed to publish config generation",
            details={
                "audit_id": audit_id,
                "stage": failure_stage,
                "error": f"{type(exc).__name__}: {exc}",
                "recovery_error": recovery_error,
                "backup_path": str(backup_path) if backup_path else None,
                "backup_write_applied": backup_write_applied,
                "transaction_dir": str(transaction_dir),
                "transaction_manifest": str(manifest_path),
                "transaction_write_applied": transaction_write_applied,
                "targets": published_targets,
                "recovered_transactions": [*recovered_transactions, *compensation],
                "write_applied": _merge_write_effects(
                    [
                        backup_write_applied,
                        *(item.get("write_applied") for item in published_targets),
                        _recovery_write_applied(compensation),
                    ]
                ),
            },
            hint=(
                "Retry the same authoring command; transaction recovery runs before the next write."
                if recovery_error is None
                else f"Inspect and recover transaction manifest: {manifest_path}"
            ),
        ) from exc
    else:
        try:
            shutil.rmtree(transaction_dir)
        except Exception as exc:
            raise AgentToolError(
                code="CONFIG_WRITE_FAILED",
                message="config generation was published but transaction cleanup failed",
                details={
                    "audit_id": audit_id,
                    "error": f"{type(exc).__name__}: {exc}",
                    "transaction_manifest": str(manifest_path),
                    "targets": published_targets,
                    "recovered_transactions": list(recovered_transactions),
                    "write_applied": True,
                    "cleanup": False,
                },
                hint=f"Retry after inspecting transaction manifest: {manifest_path}",
            ) from exc

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
    def __init__(
        self,
        runtime_root: Path,
        *,
        preflight: Callable[[list[Path]], None] | None = None,
    ) -> None:
        self.runtime_root = runtime_root.expanduser().resolve()
        self.path = self.runtime_root / "output_shared" / "state" / "config_authoring.lock"
        self.handle: BinaryIO | None = None
        self.recovered_transactions: list[dict[str, Any]] = []
        self.preflight = preflight

    def __enter__(self) -> _AuthoringLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
            pending_targets = _pending_transaction_target_paths(state_root=self.path.parent)
            if self.preflight is not None:
                self.preflight(pending_targets)
            validate_config_target_access_identity(pending_targets)
            self.recovered_transactions.extend(_recover_incomplete_transactions(state_root=self.path.parent))
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> bool:
        handle = self.handle
        self.handle = None
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        return False


def locked_config_authoring(
    *,
    runtime_root: str | Path,
    preflight: Callable[[list[Path]], None] | None = None,
) -> _AuthoringLock:
    return _AuthoringLock(Path(runtime_root), preflight=preflight)


def _require_live_authoring_lock(lock: _AuthoringLock, *, runtime_root: Path) -> None:
    if (
        not isinstance(lock, _AuthoringLock)
        or lock.handle is None
        or lock.handle.closed
        or lock.runtime_root != runtime_root
    ):
        raise AgentToolError(
            code="CONFIG_ERROR",
            message="a live config authoring lock for the same runtime root is required",
            details={"runtime_root": str(runtime_root)},
        )


def _with_recovery_details(
    exc: AgentToolError,
    recovered_transactions: list[dict[str, Any]],
) -> AgentToolError:
    if not recovered_transactions:
        return exc
    details = dict(exc.details or {})
    combined = list(details.get("recovered_transactions") or [])
    for audit in reversed(recovered_transactions):
        if audit not in combined:
            combined.insert(0, audit)
    details["recovered_transactions"] = combined
    details["write_applied"] = _merge_write_effects(
        [
            *([details["write_applied"]] if "write_applied" in details else []),
            _recovery_write_applied(combined),
        ]
    )
    return AgentToolError(code=exc.code, message=exc.message, hint=exc.hint, details=details)


def _pending_transaction_target_paths(*, state_root: Path) -> list[Path]:
    transaction_root = state_root / "config_authoring_transactions"
    if not transaction_root.exists():
        return []
    targets: list[Path] = []
    for manifest_path in sorted(transaction_root.glob("*/manifest.json")):
        manifest: dict[str, Any] | None = None
        try:
            manifest = _read_manifest(manifest_path)
            phase = str(manifest.get("phase") or "")
            for item in manifest.get("targets") or []:
                if not isinstance(item, dict):
                    raise ValueError("config transaction target is invalid")
                target = Path(str(item["path"])).expanduser().resolve()
                if phase != "committed" and bool(item.get("before_exists")) and not target.exists():
                    audit = _recovery_audit(manifest=manifest, mode="unresolved")
                    raise AgentToolError(
                        code="CONFIG_TRANSACTION_RECOVERY_REQUIRED",
                        message="an existing config transaction target disappeared before recovery",
                        details={
                            "manifest": str(manifest_path),
                            "target": str(target),
                            "stage": "target_preflight",
                            "recovered_transactions": [audit],
                            "write_applied": False,
                        },
                        hint=f"Restore the missing target, then retry recovery: {target}",
                    )
                if target not in targets:
                    targets.append(target)
        except Exception as exc:
            if (
                isinstance(exc, AgentToolError)
                and isinstance(exc.details, dict)
                and "recovered_transactions" in exc.details
            ):
                raise
            audit = _recovery_audit(
                manifest=manifest or {"audit_id": manifest_path.parent.name},
                mode="unresolved",
            )
            audit["write_applied"] = False if manifest is not None else None
            raise AgentToolError(
                code="CONFIG_TRANSACTION_RECOVERY_REQUIRED",
                message="failed to inspect an incomplete config authoring transaction",
                details={
                    "audit_id": audit["audit_id"],
                    "manifest": str(manifest_path),
                    "stage": "recovery",
                    "error": f"{type(exc).__name__}: {exc}",
                    "recovered_transactions": [audit],
                    "write_applied": audit["write_applied"],
                },
                hint=f"Inspect and recover transaction manifest: {manifest_path}",
            ) from exc
    return targets


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
    _fsync_directory(transaction_dir.parent)
    manifest_targets: list[dict[str, Any]] = []
    for index, item in enumerate(targets):
        target = Path(item["path"]).expanduser().resolve()
        desired_path = transaction_dir / f"{index:02d}.desired"
        desired_payload = bytes(item["payload"])
        _write_journal_artifact(desired_path, desired_payload)
        before_exists = target.exists()
        backup_path = transaction_dir / f"{index:02d}.before"
        before_sha = None
        if before_exists:
            before_payload = target.read_bytes()
            _write_journal_artifact(backup_path, before_payload)
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
                "after_sha256": _bytes_sha256(desired_payload),
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
    _fsync_directory(transaction_dir)
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


def _validated_manifest_targets(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    for item in manifest.get("targets") or []:
        if not isinstance(item, dict):
            raise ValueError("config transaction target is invalid")
        desired_payload = _read_journal_artifact(
            Path(str(item["desired_path"])),
            expected_sha256=str(item.get("after_sha256") or ""),
        )
        before_payload = None
        if bool(item.get("before_exists")):
            before_payload = _read_journal_artifact(
                Path(str(item["backup_path"])),
                expected_sha256=str(item.get("before_sha256") or ""),
            )
        validated.append(
            {**item, "desired_payload": desired_payload, "before_payload": before_payload}
        )
    return validated


def _read_journal_artifact(path: Path, *, expected_sha256: str) -> bytes:
    payload = path.read_bytes()
    actual_sha256 = _bytes_sha256(payload)
    if not expected_sha256 or actual_sha256 != expected_sha256:
        raise ValueError(
            f"config transaction artifact hash mismatch: {path} "
            f"(expected {expected_sha256 or '<missing>'}, actual {actual_sha256})"
        )
    return payload


def _recover_incomplete_transactions(*, state_root: Path) -> list[dict[str, Any]]:
    transaction_root = state_root / "config_authoring_transactions"
    if not transaction_root.exists():
        return []
    recovered: list[dict[str, Any]] = []
    for manifest_path in sorted(transaction_root.glob("*/manifest.json")):
        try:
            manifest = _read_manifest(manifest_path)
            phase = str(manifest.get("phase") or "")
            if phase == "committed":
                audit = _recovery_audit(manifest=manifest, mode="cleanup_committed")
                try:
                    shutil.rmtree(manifest_path.parent)
                except Exception as exc:
                    raise _recovery_error(
                        manifest_path=manifest_path,
                        audit=audit,
                        exc=exc,
                        stage="cleanup",
                    ) from exc
                audit["cleanup"] = True
                recovered.append(audit)
                continue
            recovered.append(_recover_transaction(manifest_path))
        except AgentToolError as exc:
            details = dict(exc.details or {})
            failed = list(details.get("recovered_transactions") or [])
            details["recovered_transactions"] = [*recovered, *failed]
            details["write_applied"] = _merge_write_effects(
                [
                    *([details["write_applied"]] if "write_applied" in details else []),
                    _recovery_write_applied(details["recovered_transactions"]),
                ]
            )
            raise AgentToolError(code=exc.code, message=exc.message, hint=exc.hint, details=details) from exc
        except Exception as exc:
            audit = _recovery_audit(
                manifest={"audit_id": manifest_path.parent.name},
                mode="unresolved",
            )
            audit["write_applied"] = None
            audits = [*recovered, audit]
            raise AgentToolError(
                code="CONFIG_TRANSACTION_RECOVERY_REQUIRED",
                message="failed to recover an incomplete config authoring transaction",
                details={
                    "manifest": str(manifest_path),
                    "stage": "recovery",
                    "error": f"{type(exc).__name__}: {exc}",
                    "recovered_transactions": audits,
                    "write_applied": _recovery_write_applied(audits),
                },
                hint=f"Inspect and recover transaction manifest: {manifest_path}",
            ) from exc
    return recovered


def _recover_transaction(manifest_path: Path) -> dict[str, Any]:
    manifest = _read_manifest(manifest_path)
    source_path = Path(str(manifest["source_path"]))
    audit = _recovery_audit(manifest=manifest, mode="unresolved")
    try:
        current_source_sha = config_source_sha256(source_path)
    except Exception as exc:
        raise _recovery_error(
            manifest_path=manifest_path,
            audit=audit,
            exc=exc,
            stage="source_read",
        ) from exc
    before_source_sha = str(manifest.get("before_source_sha256") or "")
    after_source_sha = str(manifest.get("after_source_sha256") or "")
    if current_source_sha == after_source_sha:
        mode = "roll_forward"
    elif current_source_sha == before_source_sha:
        mode = "roll_back"
    else:
        audit["mode"] = "conflict"
        audit["observed_source_sha256"] = current_source_sha
        raise AgentToolError(
            code="CONFIG_TRANSACTION_RECOVERY_REQUIRED",
            message="config source changed outside an incomplete authoring transaction",
            details={
                "manifest": str(manifest_path),
                "current_source_sha256": current_source_sha,
                "before_source_sha256": before_source_sha,
                "after_source_sha256": after_source_sha,
                "recovered_transactions": [audit],
                "write_applied": False,
            },
        )
    audit["mode"] = mode
    audit["observed_source_sha256"] = current_source_sha
    try:
        targets = _validated_manifest_targets(manifest)
    except Exception as exc:
        raise _recovery_error(
            manifest_path=manifest_path,
            audit=audit,
            exc=exc,
            stage="journal_validation",
        ) from exc
    for item in targets:
        target = Path(str(item["path"])).expanduser().resolve()
        action = "write_desired" if mode == "roll_forward" else "restore"
        effect = {
            "role": str(item.get("role") or ""),
            "path": str(target),
            "action": action,
            "write_applied": None,
        }
        audit["targets"].append(effect)
        try:
            if mode == "roll_forward":
                effect["write_applied"] = _write_if_changed(target, item["desired_payload"])
            elif bool(item.get("before_exists")):
                effect["write_applied"] = _write_if_changed(target, item["before_payload"])
            elif target.exists():
                effect["action"] = "delete"
                target.unlink()
                effect["write_applied"] = True
            else:
                effect["action"] = "already_absent"
                effect["write_applied"] = False
        except Exception as exc:
            raise _recovery_error(
                manifest_path=manifest_path,
                audit=audit,
                exc=exc,
                stage="target",
            ) from exc
    audit["write_applied"] = _merge_write_effects(
        [item.get("write_applied") for item in audit["targets"]]
    )
    try:
        audit["observed_source_sha256_after"] = config_source_sha256(source_path)
        expected_source_sha = after_source_sha if mode == "roll_forward" else before_source_sha
        if audit["observed_source_sha256_after"] != expected_source_sha:
            raise ValueError(
                "config source changed during transaction recovery "
                f"(expected {expected_source_sha}, actual {audit['observed_source_sha256_after']})"
            )
    except Exception as exc:
        raise _recovery_error(
            manifest_path=manifest_path,
            audit=audit,
            exc=exc,
            stage="source_readback",
        ) from exc
    try:
        shutil.rmtree(manifest_path.parent)
    except Exception as exc:
        raise _recovery_error(
            manifest_path=manifest_path,
            audit=audit,
            exc=exc,
            stage="cleanup",
        ) from exc
    audit["cleanup"] = True
    return audit


def _recovery_audit(*, manifest: dict[str, Any], mode: str) -> dict[str, Any]:
    return {
        "audit_id": str(manifest.get("audit_id") or ""),
        "mode": mode,
        "targets": [],
        "source_revision": {
            "before_sha256": str(manifest.get("before_source_sha256") or ""),
            "after_sha256": str(manifest.get("after_source_sha256") or ""),
        },
        "write_applied": False,
        "cleanup": False,
    }


def _recovery_error(
    *,
    manifest_path: Path,
    audit: dict[str, Any],
    exc: Exception,
    stage: str,
) -> AgentToolError:
    audit["write_applied"] = _merge_write_effects(
        [item.get("write_applied") for item in audit.get("targets") or []]
    )
    return AgentToolError(
        code="CONFIG_TRANSACTION_RECOVERY_REQUIRED",
        message="failed to recover an incomplete config authoring transaction",
        details={
            "manifest": str(manifest_path),
            "stage": stage,
            "error": f"{type(exc).__name__}: {exc}",
            "recovered_transactions": [audit],
            "write_applied": audit["write_applied"],
        },
        hint=f"Inspect and recover transaction manifest: {manifest_path}",
    )


def _write_if_changed(path: Path, payload: bytes) -> bool:
    if path.exists() and path.read_bytes() == payload:
        return False
    _atomic_write_bytes(path, payload)
    return True


def _merge_write_effects(values: list[Any]) -> bool | None:
    if any(value is True for value in values):
        return True
    if any(value is None for value in values):
        return None
    return False


def _recovery_write_applied(audits: list[dict[str, Any]]) -> bool | None:
    return _merge_write_effects([audit.get("write_applied", False) for audit in audits])


def _path_exists(path: Path | None) -> bool | None:
    if path is None:
        return False
    try:
        path.stat()
    except FileNotFoundError:
        return False
    except OSError:
        return None
    return True


def _create_human_backup(source_path: Path, *, audit_id: str) -> Path:
    backup_path = source_path.with_name(f"{source_path.name}.bak.{audit_id}")
    shutil.copy2(source_path, backup_path)
    return backup_path


def _write_journal_artifact(path: Path, payload: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


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
    "locked_config_authoring",
    "publish_yaml_config_generation",
    "publish_yaml_config_generation_locked",
    "validate_config_target_access_identity",
]
