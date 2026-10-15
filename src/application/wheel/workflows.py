from __future__ import annotations

from pathlib import Path


from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import tempfile

import yaml
from typing import Any, Mapping

from src.application.agent_tool_contracts import AgentToolError
from src.application.runtime_config_paths import authoritative_config_yaml_path
from src.application.settings import build_effective_env
from src.application.ledger.api import (
    change_wheel_activation_window as _change_wheel_activation_window,
    wheel_activation_result as _activation_result,
    end_wheel_lifecycle,
    decide_wheel_branch,
    reject_wheel_call_linkage,
    reject_wheel_linkage,
    read_wheel_activation_windows_read_only,
    ledger_store_write_guard,
    open_wheel_activation_repository,
    resolve_position_data_config_path,
    resolve_position_ledger_sqlite_path,
)
from domain.domain.wheel.config import (
    wheel_activation_request_hash as _activation_request_hash,
    resolve_wheel_activation_request as _activation_request_window,
    wheel_activation_window_descriptor as _activation_descriptor,
)
from src.application.wheel.config import (
    WHEEL_ACTIVATION_DESCRIPTOR_FIELDS, build_wheel_policy_hash, evaluate_wheel_activation_readiness,
    resolve_wheel_activation_descriptor, materialize_wheel_config, normalize_wheel_accounts,
)
from src.application.wheel.remediation import policy_remediation


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _require_market(value: str) -> str:
    market = str(value or "").strip().lower()
    if market not in {"us", "hk"}:
        raise ValueError("Wheel workflow market must be us or hk")
    return market


def _activation_status(
    cfg: dict[str, Any], *, sqlite_path: Path, market: str, account: str,
    runtime_root: Path | None = None, config_path: Path | None = None,
) -> dict[str, Any]:
    observed = read_wheel_activation_windows_read_only(sqlite_path, market=market, account=account)
    windows = observed["windows"]
    latest = windows[-1] if windows else None
    current = latest if latest and latest["deactivated_at_ms"] is None else None
    membership = False
    invalid_account_scope = False
    try:
        raw_wheel = cfg.get("wheel")
        if raw_wheel is None:
            raw_wheel = {}
        if not isinstance(raw_wheel, Mapping):
            raise ValueError("wheel must be an object")
        membership = account in normalize_wheel_accounts(raw_wheel.get("accounts", []))
    except (ValueError, TypeError):
        invalid_account_scope = True
    if invalid_account_scope:
        readiness = {"ready": False, "enabled_for_new_lifecycle": False,
                     "monitoring_gate": "config_mismatch", "reason_code": "invalid_account_scope",
                     "policy_drift": False}
    else:
        try:
            readiness = evaluate_wheel_activation_readiness(
                resolve_wheel_activation_descriptor(cfg, market=market, account=account), latest,
                account_configured=membership,
            )
        except (ValueError, TypeError):
            readiness = {"ready": False, "enabled_for_new_lifecycle": False,
                         "monitoring_gate": "config_mismatch", "reason_code": "descriptor_mismatch",
                         "policy_drift": False}
    if observed["source_status"] != "available" and readiness["monitoring_gate"] != "config_mismatch":
        readiness.update(ready=False, enabled_for_new_lifecycle=False,
                         monitoring_gate="disabled", reason_code=observed["source_status"])
    # After the overrides: a gate that was closed for another reason must never advertise
    # a policy command, and the overrides above rewrite `reason_code` to say so.
    readiness.update(
        policy_remediation(readiness, market=market, account=account,
                           config_path=config_path, runtime_root=runtime_root)
    )
    return {"windows": windows, "current_window": _activation_descriptor(current, include_binding=True),
            "latest_window": _activation_descriptor(latest, include_binding=True), "membership": membership,
            "storage_status": observed["source_status"], "readiness": readiness, **readiness}


def _activation_source_doc(path: Path) -> tuple[dict[str, Any], str]:
    # Hash the same bytes that are parsed; the publisher checks the hash again before installation.
    payload = path.read_bytes()
    doc = yaml.safe_load(payload)
    if not isinstance(doc, dict):
        raise ValueError("authoritative config.yaml must contain an object")
    return doc, hashlib.sha256(payload).hexdigest()


def _activation_yaml_wheel(doc: dict[str, Any], market: str) -> dict[str, Any]:
    return doc.setdefault("markets", {}).setdefault(market, {}).setdefault("features", {}).setdefault("wheel", {})


def _activation_source_matches(
    doc: dict[str, Any], *, market: str, account: str, window: dict[str, Any], action: str,
) -> bool:
    raw = _activation_yaml_wheel(deepcopy(doc), market)
    descriptors = raw.get("activation_by_account") or {}
    descriptor = next((v for k, v in descriptors.items() if str(k).strip().lower() == account), None)
    expected = {key: window[key] for key in WHEEL_ACTIVATION_DESCRIPTOR_FIELDS}
    member = account in {str(v).strip().lower() for v in raw.get("accounts", [])}
    return descriptor == expected and (action == "disable" or member)


def _activation_complete(state: dict[str, Any], *, action: str) -> bool:
    if action == "enable":
        return bool(state["ready"] and state["enabled_for_new_lifecycle"] and state["membership"])
    return state["monitoring_gate"] == "disabled" and state["reason_code"] == "closed_window"


def _activation_effective_config(cfg: dict[str, Any], *, account: str | None = None) -> dict[str, Any]:
    effective = deepcopy(cfg)
    for key in ("_generated", "_resolved"):
        effective.pop(key, None)
    if "wheel" in effective:
        effective["wheel"] = materialize_wheel_config(effective["wheel"])
    if account is not None:
        wheel = effective.get("wheel", {})
        wheel["accounts"] = [value for value in wheel.get("accounts", []) if value != account]
        wheel.get("activation_by_account", {}).pop(account, None)
    return effective


def _activation_config_plan(
    *, repo_root: Path, runtime_root: Path, source_path: Path, source_doc: dict[str, Any],
    market: str, account: str, action: str, window: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    from src.application.agent_tool_config import load_runtime_config
    from src.application.config_yaml import resolve_yaml_runtime_config
    from src.application.runtime_config_freshness import check_runtime_config_freshness

    candidate = deepcopy(source_doc)
    wheel = _activation_yaml_wheel(candidate, market)
    members = wheel.setdefault("accounts", [])
    if action == "enable" and account not in {str(v).strip().lower() for v in members}:
        members.append(account)
    descriptors = wheel.setdefault("activation_by_account", {})
    key = next((key for key in descriptors if str(key).strip().lower() == account), account)
    descriptors[key] = {field: window[field] for field in WHEEL_ACTIVATION_DESCRIPTOR_FIELDS}
    markets = [market]
    with tempfile.TemporaryDirectory(prefix="om-wheel-preview-") as temp_dir:
        preview_path = Path(temp_dir) / "config.yaml"
        preview_path.write_text(yaml.safe_dump(candidate, allow_unicode=True, sort_keys=False), encoding="utf-8")
        for target in (market, "hk" if market == "us" else "us"):
            runtime_path = runtime_root / f"config.{target}.json"
            if target != market and not runtime_path.exists():
                continue
            _, installed = load_runtime_config(config_path=runtime_path, expected_market=target)
            if authoritative_config_yaml_path(installed, repo_root=repo_root) != source_path:
                raise ValueError("runtime snapshots must share the authoritative YAML source")
            raw_installed = json.loads(runtime_path.read_text(encoding="utf-8"))
            built, _ = resolve_yaml_runtime_config(repo_root=repo_root, market=target, config_path=preview_path)
            mask_account = account if target == market else None
            if _activation_effective_config(raw_installed, account=mask_account) != _activation_effective_config(built, account=mask_account):
                raise AgentToolError(code="CONFIG_DRIFT", message="Wheel activation would publish unrelated effective configuration changes",
                                     details={"market": target}, hint="Publish the unrelated configuration separately, then preview again.")
            if target != market:
                sources = raw_installed.get("_generated", {}).get("sources", [])
                source = next((item for item in sources if item.get("role") == "market_user"), {})
                fresh_source = next(item for item in built["_generated"]["sources"] if item["role"] == "market_user")
                freshness = check_runtime_config_freshness(raw_installed, repo_root=repo_root,
                                                          market=target, runtime_config_path=runtime_path)
                allowed_stale_legacy = "effective" not in source and all(
                    item.get("code") == "source_changed" and item.get("role") == "market_user"
                    for item in freshness.get("errors", [])
                )
                if not freshness["ok"] and not allowed_stale_legacy:
                    raise ValueError("other market runtime freshness metadata is invalid or stale")
                if "effective" in source:
                    if source["effective"] != fresh_source["effective"]:
                        raise ValueError("other market YAML freshness fingerprint mismatch")
                else:
                    markets.append(target)
    return candidate, markets


def _activation_owner_preflight(paths: list[Path], *, runtime_root: Path) -> None:
    from src.application.config_authoring_transaction import validate_config_target_access_identity

    for path in paths:
        if not path.resolve().is_relative_to(runtime_root):
            raise ValueError("config recovery target belongs to another deployment")
    validate_config_target_access_identity(paths)


def _activation_any_effect(effects: list[Any]) -> bool | None:
    if any(effect is True for effect in effects):
        return True
    return None if any(effect is None for effect in effects) else False


def change_wheel_activation(
    *, repo_root: Path, action: str, market: str, account: str,
    config_path: str | Path | None = None, config_key: str | None = None,
    data_config: str | Path | None = None, runtime_root: str | Path | None = None,
    expected_current_generation: int | None = None, request_id: str | None = None,
    actor: str | None = None, expected_source_sha256: str | None = None,
    apply_changes: bool = False,
) -> dict[str, Any]:
    from src.application.agent_tool_config import load_runtime_config
    from src.application.config_authoring_transaction import locked_config_authoring, publish_yaml_config_generation_locked
    from src.application.config_yaml import resolve_yaml_runtime_config

    action, market, account = (str(value or "").strip().lower() for value in (action, market, account))
    if action not in {"status", "enable", "disable"} or market not in {"us", "hk"} or not account:
        raise ValueError("Wheel activation requires action status/enable/disable, market us/hk, and account")
    if action == "status" and apply_changes:
        raise ValueError("Wheel activation status is read-only")
    request_id, actor = str(request_id or "").strip(), str(actor or "").strip()
    if action != "status":
        if (type(expected_current_generation) is not int or expected_current_generation < 0 or not request_id or not actor):
            raise ValueError("Wheel activation requires nonnegative generation, request_id, and actor")
        if apply_changes and (not isinstance(expected_source_sha256, str) or len(expected_source_sha256) != 64
                              or any(c not in "0123456789abcdef" for c in expected_source_sha256)):
            raise AgentToolError(code="INPUT_ERROR", message="expected_source_sha256 from preview is required for apply")
    repo_root = Path(repo_root).resolve()
    runtime_path, cfg = load_runtime_config(config_path=config_path, config_key=config_key or market, expected_market=market)
    runtime_path = runtime_path.resolve()
    root = runtime_path.parent
    env_root = str(build_effective_env().get("OM_RUNTIME_ROOT") or "").strip()
    for value in (runtime_root, env_root):
        if value and Path(value).expanduser().resolve() != root:
            raise ValueError("Wheel activation config and runtime root identify different deployments")
    if runtime_path != root / f"config.{market}.json":
        raise ValueError("Wheel activation must target the canonical runtime snapshot")
    if account not in cfg.get("accounts", {}):
        raise ValueError("Wheel activation account is not configured in the target market")
    source_path = authoritative_config_yaml_path(cfg, repo_root=repo_root, require_exists=False)
    if source_path.parent != root:
        raise ValueError("Wheel activation YAML and runtime must belong to the same deployment")
    data_path = resolve_position_data_config_path(base=repo_root, cfg=cfg, data_config=data_config, config_path=runtime_path)
    sqlite_path = resolve_position_ledger_sqlite_path(base=repo_root, cfg=cfg, data_config=data_path,
                                                    config_path=runtime_path, runtime_root=root)
    result: dict[str, Any] = {
        "schema_version": "wheel_activation_result.v1", "action": action, "market": market, "account": account,
        "request_id": request_id, "actor": actor, "expected_current_generation": expected_current_generation,
        "original_request": {"action": action, "market": market, "account": account, "request_id": request_id,
                             "actor": actor, "expected_current_generation": expected_current_generation},
        "paths": {"runtime_root": str(root), "config_path": str(runtime_path), "config_yaml_path": str(source_path),
                  "sqlite_path": str(sqlite_path), "data_config": str(data_path)},
        "dry_run": not apply_changes, "write_applied": False, "config_audit": None, "window_receipt": None,
        "recovered_transactions": [], "audit_id": f"wheel-activation:{market}:{account}:{request_id}",
    }
    phase = "read"
    window_write_started = False

    def refresh() -> dict[str, Any]:
        _, current_cfg = load_runtime_config(config_path=runtime_path, expected_market=market)
        if authoritative_config_yaml_path(current_cfg, repo_root=repo_root, require_exists=False) != source_path:
            raise ValueError("runtime YAML source identity changed during activation")
        state = _activation_status(current_cfg, sqlite_path=sqlite_path, market=market, account=account,
                                   runtime_root=root, config_path=runtime_path)
        for key in result.get("readiness", {}):
            result.pop(key, None)
        result.update({key: value for key, value in state.items() if key != "windows"})
        result["source_status"] = "available" if source_path.is_file() else "unavailable"
        result["pending_authoring_journal"] = any((root / "output_shared/state/config_authoring_transactions").glob("*/manifest.json"))
        return state

    def prepare(lock: Any = None) -> dict[str, Any]:
        nonlocal phase, window_write_started
        state = refresh()
        if action == "status":
            result["status"] = ("unavailable" if state["storage_status"] != "available" else
                                "open" if state["current_window"] else "closed" if state["latest_window"] else "no_window")
            return result
        phase = "source_validation"
        doc, source_sha = _activation_source_doc(source_path)
        result["expected_source_sha256"] = source_sha
        if state["storage_status"] == "unreadable":
            raise ValueError("Wheel activation ledger is unreadable")
        windows = state["windows"]
        latest = windows[-1] if windows else None
        matched = next((row for row in windows if request_id in (
            row.get("activation_request_id"), row.get("deactivation_request_id"),
        )), None)
        source_cfg, _ = resolve_yaml_runtime_config(repo_root=repo_root, market=market, config_path=source_path)
        if account not in source_cfg.get("accounts", {}):
            raise ValueError("Wheel activation account is absent from canonical YAML market")
        current_policy = build_wheel_policy_hash(source_cfg, market=market, account=account)
        policy_sha = matched["policy_hash"] if matched else latest["policy_hash"] if action == "disable" and latest else current_policy
        request_hash = _activation_request_hash(action=action, market=market, account=account,
                                               expected_current_generation=expected_current_generation,
                                               request_id=request_id, actor=actor, policy_sha256=policy_sha)
        result["request_hash"] = request_hash
        result["original_request"]["policy_sha256"] = policy_sha
        replay = _activation_request_window(windows, action=action, request_id=request_id, request_hash=request_hash,
                                            policy_sha256=policy_sha, expected_generation=expected_current_generation)
        if replay:
            receipt = _activation_result(action=action, status="idempotent", market=market, account=account,
                                         request_id=request_id, actor=actor, request_hash=request_hash, window=replay,
                                         dry_run=not apply_changes, write_applied=False, idempotent=True)
            result.update(window_receipt=receipt, expected_config_descriptor=receipt["expected_config_descriptor"], idempotent=True)
            if _activation_complete(state, action=action) and _activation_source_matches(doc, market=market, account=account, window=replay, action=action):
                result["status"] = "idempotent"
                return result
        if apply_changes and expected_source_sha256 != source_sha:
            raise AgentToolError(code="STALE_PREVIEW", message="canonical YAML changed since preview",
                                 hint="Preview again with the same request_id, actor, and original expected generation.")
        if action == "enable" and policy_sha != current_policy:
            raise ValueError("activation policy changed; disable the original window before enabling the new policy")
        placeholder = dict(replay or latest or {})
        if not replay:
            if action == "enable":
                placeholder = {"generation": expected_current_generation + 1,
                               "activated_at_ms": max(1, int((latest or {}).get("deactivated_at_ms") or 0) + 1),
                               "deactivated_at_ms": None}
            else:
                placeholder["deactivated_at_ms"] = int(placeholder["activated_at_ms"]) + 1
        candidate, markets = _activation_config_plan(repo_root=repo_root, runtime_root=root, source_path=source_path,
                                                    source_doc=doc, market=market, account=account, action=action, window=placeholder)
        result["planned_changes"] = {"files": [str(source_path), *[str(root / f"config.{m}.json") for m in markets]],
                                     "fields": [f"markets.{market}.features.wheel.activation_by_account.{account}"]
                                     + ([f"markets.{market}.features.wheel.accounts"] if action == "enable" else [])}
        result["status"] = "configuration_pending" if replay else "planned"
        result.setdefault("idempotent", False)
        if not apply_changes:
            return result
        phase = "window_commit"
        _activation_owner_preflight([source_path, *[root / f"config.{m}.json" for m in markets]], runtime_root=root)
        if hashlib.sha256(source_path.read_bytes()).hexdigest() != source_sha:
            raise AgentToolError(code="STALE_PREVIEW", message="canonical YAML changed during preparation")
        window_write_started = True
        repo = open_wheel_activation_repository(sqlite_path)
        receipt = _change_wheel_activation_window(repo, action=action, market=market, account=account,
                                                  expected_current_generation=expected_current_generation,
                                                  request_id=request_id, actor=actor, policy_sha256=policy_sha)
        result.update(window_receipt=receipt, expected_config_descriptor=receipt["expected_config_descriptor"],
                      write_applied=bool(result["write_applied"] or receipt["write_applied"]), idempotent=receipt["idempotent"])
        phase = "config_publish"
        candidate, markets = _activation_config_plan(repo_root=repo_root, runtime_root=root, source_path=source_path,
                                                    source_doc=doc, market=market, account=account, action=action,
                                                    window=receipt["expected_config_descriptor"])
        result["config_audit"] = publish_yaml_config_generation_locked(lock=lock, repo_root=repo_root,
            config_yaml_path=source_path, config_doc=candidate, runtime_root=root, markets=markets,
            include_bot=False, expected_source_sha256=source_sha)
        result["write_applied"] = bool(result["write_applied"] or result["config_audit"]["write_applied"])
        phase = "readback"
        final = refresh()
        _activation_request_window(final["windows"], action=action, request_id=request_id, request_hash=request_hash,
                                   policy_sha256=policy_sha, expected_generation=expected_current_generation)
        final_doc, _ = _activation_source_doc(source_path)
        if not _activation_complete(final, action=action) or not _activation_source_matches(
            final_doc, market=market, account=account, window=final["windows"][-1], action=action,
        ):
            raise ValueError("Wheel activation postcondition is incomplete")
        result["status"] = "applied"
        return result

    try:
        if not apply_changes:
            return prepare()
        phase = "ledger_guard"
        guard = ledger_store_write_guard(data_path, runtime_root=root, config_path=runtime_path)
        if not guard["ok"]:
            raise AgentToolError(code="LEDGER_STORE_UNSAFE", message="ledger write guard rejected activation", details=guard)
        phase = "owner_preflight"
        possible_targets = [source_path, runtime_path]
        sibling = root / f"config.{'hk' if market == 'us' else 'us'}.json"
        if sibling.exists():
            possible_targets.append(sibling)
        _activation_owner_preflight(possible_targets, runtime_root=root)
        phase = "config_recovery"
        with locked_config_authoring(runtime_root=root,
                                     preflight=lambda paths: _activation_owner_preflight(paths, runtime_root=root)) as lock:
            result["recovered_transactions"] = lock.recovered_transactions
            result["write_applied"] = _activation_any_effect([item.get("write_applied", False) for item in lock.recovered_transactions])
            return prepare(lock)
    except Exception as exc:
        details = dict(exc.details or {}) if isinstance(exc, AgentToolError) else {}
        recovered = list(result["recovered_transactions"])
        for item in details.get("recovered_transactions") or []:
            if item not in recovered:
                recovered.append(item)
        result["recovered_transactions"] = recovered
        if phase == "config_publish":
            result["config_audit"] = details or {"write_applied": None, "evidence": "unavailable"}
        if window_write_started and result["window_receipt"] is None:
            result["window_receipt"] = {"write_applied": None, "evidence": "commit_outcome_unavailable"}
        result["write_applied"] = _activation_any_effect([
            result["write_applied"], *(item.get("write_applied", False) for item in recovered),
            (result["window_receipt"] or {}).get("write_applied", False),
            (result["config_audit"] or {}).get("write_applied", False),
        ])
        try:
            refresh()
        except Exception:
            result["readback_status"] = "unavailable"
        result.update(status="incomplete", failure_phase=phase,
                      retry_hint="Preview with the same request_id, actor and original expected generation; apply with the new source SHA.")
        raise AgentToolError(code=exc.code if isinstance(exc, AgentToolError) else "WHEEL_ACTIVATION_FAILED",
                             message=exc.message if isinstance(exc, AgentToolError) else str(exc),
                             hint=exc.hint if isinstance(exc, AgentToolError) else result["retry_hint"],
                             details={**details, **result}) from exc


__all__ = [
    "cancel_wheel_intent",
    "cancel_wheel_call_intent",
    "change_wheel_activation",
    "create_wheel_intent",
    "create_wheel_call_intent",
    "decide_wheel_branch",
    "end_wheel_lifecycle",
    "reject_wheel_linkage",
    "reject_wheel_call_linkage",
]

from src.application.ledger.api import (
    create_wheel_call_intent as create_wheel_call_intent,
    cancel_wheel_call_intent as cancel_wheel_call_intent,
    create_wheel_intent as create_wheel_intent,
    cancel_wheel_intent as cancel_wheel_intent,
)
