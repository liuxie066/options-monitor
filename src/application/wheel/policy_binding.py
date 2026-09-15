"""Explicitly accept a configured policy without reopening its activation window."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.agent_tool_config import load_runtime_config
from src.application.agent_tool_contracts import AgentToolError
from src.application.config_authoring_transaction import locked_config_authoring
from src.application.config_yaml import resolve_yaml_runtime_config
from src.application.ledger.api import (
    ledger_store_write_guard,
    open_wheel_activation_repository,
    read_wheel_activation_windows_read_only,
    resolve_position_data_config_path,
    resolve_position_ledger_sqlite_path,
    with_sqlite_repo_transaction,
)
from src.application.runtime_config_paths import authoritative_config_yaml_path
from src.application.settings import build_effective_env
from src.application.wheel.config import (
    WHEEL_ACTIVATION_DESCRIPTOR_FIELDS,
    build_wheel_policy_hash,
    evaluate_wheel_activation_readiness,
    resolve_wheel_activation_descriptor,
)
from src.application.wheel.workflows import _activation_effective_config, _activation_owner_preflight


def _file_identity(path: Path, *, content: bool = False) -> dict[str, Any]:
    stat = path.stat()
    if not path.is_file():
        raise ValueError("Wheel policy binding requires regular files")
    result = {"path": str(path.resolve()), "device": stat.st_dev, "inode": stat.st_ino}
    if content:
        result["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def rebind_wheel_policy(
    *, repo_root: Path, market: str, account: str, request_id: str, actor: str,
    config_path: str | Path | None = None, runtime_root: str | Path | None = None,
    data_config: str | Path | None = None, expected_preview_hash: str | None = None,
    apply_changes: bool = False,
) -> dict[str, Any]:
    """Preview/CAS one append-only binding; never publish configuration or trade facts."""
    market, account = str(market).strip().lower(), str(account).strip()
    request_id, actor = str(request_id or "").strip(), str(actor or "").strip()
    if market not in {"us", "hk"} or not account or account != account.lower() or not request_id or not actor:
        raise ValueError("Wheel policy binding requires market, lowercase account, request_id and actor")
    if apply_changes and (
        not isinstance(expected_preview_hash, str) or len(expected_preview_hash) != 64
        or any(c not in "0123456789abcdef" for c in expected_preview_hash)
    ):
        raise ValueError("Wheel policy binding apply requires --expected-preview-hash")
    repo_root = Path(repo_root).resolve()
    runtime_path, cfg = load_runtime_config(config_path=config_path, config_key=market, expected_market=market)
    runtime_path = runtime_path.resolve()
    root = runtime_path.parent
    for value in (runtime_root, build_effective_env().get("OM_RUNTIME_ROOT")):
        if value and Path(value).expanduser().resolve() != root:
            raise ValueError("Wheel policy binding runtime roots disagree")
    if runtime_path != root / f"config.{market}.json":
        raise ValueError("Wheel policy binding requires the canonical runtime snapshot")
    source = authoritative_config_yaml_path(cfg, repo_root=repo_root).resolve()
    if source.parent != root:
        raise ValueError("Wheel policy binding YAML belongs to another deployment")
    data_path = resolve_position_data_config_path(base=repo_root, cfg=cfg, data_config=data_config, config_path=runtime_path)
    sqlite_path = Path(resolve_position_ledger_sqlite_path(
        base=repo_root, cfg=cfg, data_config=data_path, config_path=runtime_path, runtime_root=root,
    )).resolve()
    paths = {"runtime_root": str(root), "config_path": str(runtime_path), "config_yaml_path": str(source),
             "sqlite_path": str(sqlite_path), "data_config": str(data_path)}
    result: dict[str, Any] = {
        "schema_version": "wheel_policy_binding_result.v1", "market": market, "account": account,
        "request_id": request_id, "actor": actor, "paths": paths, "dry_run": not apply_changes,
        "write_applied": False, "receipt": None,
    }

    def no_pending_journal(_targets: list[Path] | None = None) -> None:
        if any((root / "output_shared/state/config_authoring_transactions").glob("*/manifest.json")):
            raise ValueError("pending config authoring journal: recover the original operation before policy rebind")

    def observe() -> dict[str, Any]:
        value = read_wheel_activation_windows_read_only(sqlite_path, market, account)
        if value["source_status"] != "available":
            raise ValueError(f"Wheel activation storage is {value['source_status']}")
        return value

    def prepare(observed: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        source_identity = _file_identity(source, content=True)
        runtime_identity = _file_identity(runtime_path, content=True)
        _, installed = load_runtime_config(config_path=runtime_path, expected_market=market)
        if authoritative_config_yaml_path(installed, repo_root=repo_root).resolve() != source:
            raise ValueError("Wheel YAML identity changed")
        authored, _ = resolve_yaml_runtime_config(repo_root=repo_root, market=market, config_path=source)
        raw_installed = json.loads(runtime_path.read_text())
        if _activation_effective_config(raw_installed) != _activation_effective_config(authored):
            raise ValueError("Wheel runtime configuration differs from canonical YAML; rebuild before rebind")
        if account not in installed.get("accounts", {}) or account not in installed.get("wheel", {}).get("accounts", []):
            raise ValueError("Wheel account is not configured")
        windows = observed["windows"]
        if not windows or windows[-1]["deactivated_at_ms"] is not None:
            raise ValueError("Wheel policy binding requires the latest open window")
        current = windows[-1]
        descriptor = resolve_wheel_activation_descriptor(installed, market=market, account=account)
        if descriptor is None or any(descriptor[k] != current[k] for k in ("market", "account", *WHEEL_ACTIVATION_DESCRIPTOR_FIELDS)):
            raise ValueError("Wheel activation descriptor boundary mismatch")
        target = build_wheel_policy_hash(authored, market=market, account=account)
        if target != build_wheel_policy_hash(installed, market=market, account=account):
            raise ValueError("Wheel source/runtime policy mismatch")
        original = {k: v for k, v in current.items() if k not in {"effective_policy_hash", "policy_binding_revision"}}
        request = {
            "schema_version": "wheel_policy_rebind_request.v1", "market": market, "account": account,
            "request_id": request_id, "actor": actor, "window": original,
            "expected_revision": current.get("policy_binding_revision", 0),
            "effective_policy_hash": current.get("effective_policy_hash", current["policy_hash"]),
            "target_policy_hash": target, "source": source_identity, "runtime": runtime_identity,
            "ledger": _file_identity(sqlite_path),
        }
        if source_identity != _file_identity(source, content=True) or runtime_identity != _file_identity(runtime_path, content=True):
            raise ValueError("Wheel configuration changed while preparing policy binding")
        return request, evaluate_wheel_activation_readiness(descriptor, current)

    def replay(observed: dict[str, Any]) -> bool:
        matches = [r for r in observed.get("policy_bindings", []) if r["request_id"] == request_id]
        if not matches:
            return False
        receipt = matches[0]
        if len(matches) != 1 or receipt["actor"] != actor:
            raise ValueError("Wheel policy binding request identity conflict")
        if apply_changes and expected_preview_hash != receipt["request_hash"]:
            raise ValueError("Wheel policy binding replay preview mismatch")
        if receipt["request"]["ledger"] != _file_identity(sqlite_path):
            raise ValueError("Wheel policy binding ledger identity changed")
        result["receipt"] = receipt
        current = observed["windows"][-1] if observed["windows"] else None
        if (not current or current["deactivated_at_ms"] is not None
                or receipt["generation"] != current["generation"]
                or receipt["revision"] != current.get("policy_binding_revision", 0)):
            raise ValueError("Wheel policy binding request superseded")
        request, readiness = prepare(observed)
        if receipt["policy_hash"] != request["target_policy_hash"]:
            raise ValueError("Wheel policy binding request identity conflict")
        result.update(status="idempotent", receipt=receipt, preview_hash=receipt["request_hash"],
                      readiness=readiness, ready=readiness["ready"])
        return True

    phase = "preview"
    try:
        observed = observe()
        # Lost-response confirmation is read-only, including when config journals are pending.
        if replay(observed):
            return result
        request, readiness = prepare(observed)
        no_pending_journal()
        preview_hash = canonical_sha256(request)
        result.update(preview_hash=preview_hash, preview=request, readiness=readiness, ready=readiness["ready"],
                      status="no_drift" if request["effective_policy_hash"] == request["target_policy_hash"] else "planned")
        if not apply_changes:
            return result
        if expected_preview_hash != preview_hash:
            raise ValueError("stale Wheel policy binding preview")
        if result["status"] == "no_drift":
            return result
        phase = "preflight"
        guard = ledger_store_write_guard(data_path, runtime_root=root, config_path=runtime_path)
        if not guard["ok"]:
            raise AgentToolError(code="LEDGER_STORE_UNSAFE", message="ledger write guard rejected policy binding", details=guard)
        _activation_owner_preflight([source, runtime_path], runtime_root=root)
        with locked_config_authoring(runtime_root=root, preflight=no_pending_journal):
            observed = observe()
            if replay(observed):
                return result
            checked, _ = prepare(observed)
            if canonical_sha256(checked) != expected_preview_hash:
                raise ValueError("stale Wheel policy binding preview")
            phase = "ledger_commit"
            result["write_applied"] = None  # Writer initialization/commit can fail with effects.
            repo = open_wheel_activation_repository(sqlite_path)

            def commit(sqlite_repo: Any, conn: Any) -> dict[str, Any]:
                windows = sqlite_repo.list_wheel_activation_windows(market=market, account=account, conn=conn)
                current = sqlite_repo.get_current_wheel_activation_window(market=market, account=account, conn=conn)
                if current:
                    windows[-1] = current
                tx_observed = {"windows": windows, "policy_bindings": sqlite_repo.list_wheel_policy_bindings(market=market, account=account, conn=conn)}
                if replay(tx_observed):
                    return result["receipt"]
                checked, _ = prepare(tx_observed)
                if canonical_sha256(checked) != expected_preview_hash:
                    raise ValueError("stale Wheel policy binding preview")
                return sqlite_repo.append_wheel_policy_binding(request=checked, request_hash=expected_preview_hash, conn=conn)

            receipt = with_sqlite_repo_transaction(repo, commit)
            result.update(receipt=receipt, write_applied=result.get("status") != "idempotent")
            phase = "readback"
            after_request, after_ready = prepare(observe())
            result.update(readiness=after_ready, ready=after_ready["ready"])
            if not after_ready["ready"] or after_request["window"] != request["window"] or after_request["expected_revision"] != receipt["revision"]:
                raise ValueError("Wheel policy binding committed but readiness verification is incomplete")
        if result["status"] != "idempotent":
            result["status"] = "applied"
        return result
    except Exception as exc:
        result.update(status="incomplete", failure_phase=phase)
        if isinstance(exc, AgentToolError):
            result["cause"] = exc.details
        raise AgentToolError(code="WHEEL_POLICY_BINDING_FAILED", message=str(exc), details=result) from exc
