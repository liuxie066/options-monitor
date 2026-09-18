"""Accept the configured Wheel policy for every drifting account in one market.

One operator command instead of the build/preview/apply/readback sequence: classify what
is actually acceptable, rebuild the snapshot only when a rebind needs it, and then delegate
each account to the existing append-only `rebind_wheel_policy`.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.agent_tool_config import load_runtime_config
from src.application.agent_tool_contracts import AgentToolError
from src.application.config_yaml import (
    build_yaml_runtime_config_file,
    resolve_yaml_runtime_config,
)
from src.application.ledger.api import (
    ledger_store_write_guard,
    read_wheel_activation_windows_read_only,
    resolve_position_data_config_path,
    resolve_position_ledger_sqlite_path,
)
from src.application.runtime_config_freshness import build_rebuild_command
from src.application.runtime_config_paths import authoritative_config_yaml_path
from src.application.settings import build_effective_env
from src.application.wheel.config import (
    build_wheel_policy_hash,
    evaluate_wheel_activation_readiness,
    normalize_wheel_accounts,
    normalize_wheel_activation_by_account,
    resolve_wheel_activation_descriptor,
)
from src.application.wheel.policy_binding import rebind_wheel_policy
from src.application.wheel.runtime_readiness import build_wheel_activation_readiness
from src.application.wheel.workflows import (
    _activation_any_effect,
    _activation_effective_config,
    _activation_owner_preflight,
)

ACCEPT_POLICY_SCHEMA = "wheel_accept_policy_result.v1"
_BOUNDARY_FIELDS = ("market", "account", "generation", "activated_at_ms", "deactivated_at_ms")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def accept_policy_request_id(
    *,
    market: str,
    account: str,
    generation: int,
    revision: int,
    actor: str,
    target_policy_hash: str,
) -> str:
    """Derive one stable binding request id per (window revision, actor, target policy).

    Deterministic so a crashed run replays its own binding instead of appending a second
    one, and actor-bound so two operators accepting the same revision cannot collide.
    """

    digest = canonical_sha256(
        {
            "schema_version": "wheel_accept_policy_request_identity.v1",
            "market": market,
            "account": account,
            "generation": generation,
            "revision": revision,
            "actor": actor,
            "target_policy_hash": target_policy_hash,
        }
    )[:16]
    return f"accept-policy:{market}:{account}:g{generation}:r{revision}:{digest}"


def _classify(
    *,
    authored: Mapping[str, Any],
    market: str,
    account: str,
    latest: Mapping[str, Any],
) -> dict[str, Any]:
    """Decide what accepting this account would do, judged against the post-rebuild state.

    The authored YAML is the right input, not the installed snapshot: accepting requires
    the snapshot to already match the YAML, so this is the state acceptance will act on.
    """

    target_policy_hash = build_wheel_policy_hash(authored, market=market, account=account)
    try:
        descriptor = resolve_wheel_activation_descriptor(authored, market=market, account=account)
    except (TypeError, ValueError) as exc:
        return {
            "classification": "refused_invalid_descriptor",
            "detail": f"{type(exc).__name__}: {exc}",
            "target_policy_hash": target_policy_hash,
        }
    if descriptor is None:
        return {
            "classification": "refused_invalid_descriptor",
            "detail": "no activation descriptor for this account",
            "target_policy_hash": target_policy_hash,
        }
    readiness = evaluate_wheel_activation_readiness(descriptor, latest)
    boundary_matches = all(descriptor.get(key) == latest.get(key) for key in _BOUNDARY_FIELDS)
    if readiness["ready"]:
        classification = "already_current"
    elif readiness.get("reason_code") == "closed_window":
        classification = "refused_closed"
    elif readiness.get("reason_code") == "descriptor_mismatch":
        if readiness.get("policy_drift") is True:
            classification = "accept"
        elif boundary_matches:
            # The boundary agrees yet the durable identity cannot be parsed: corrupt state,
            # not a boundary edit, and still not something a policy rebind can repair.
            classification = "refused_invalid_descriptor"
        else:
            classification = "refused_boundary"
    else:
        classification = f"refused_{readiness.get('reason_code') or 'unknown'}"
    return {
        "classification": classification,
        "target_policy_hash": target_policy_hash,
        "current_policy_hash": latest.get("effective_policy_hash") or latest.get("policy_hash"),
        "policy_drift": readiness.get("policy_drift"),
        "reason_code": readiness.get("reason_code"),
        "generation": latest.get("generation"),
        "revision": latest.get("policy_binding_revision", 0),
    }


def _sibling_market_state(
    *, repo_root: Path, market: str, source: Path, root: Path
) -> dict[str, Any]:
    """Report, never repair, the other market's snapshot (advisory only)."""

    sibling = "hk" if market == "us" else "us"
    sibling_path = root / f"config.{sibling}.json"
    if not sibling_path.is_file():
        return {"sibling_market_status": "not_present", "sibling_rebuild_command": None}
    command = build_rebuild_command(market=sibling, runtime_config_path=sibling_path)
    try:
        _, installed = load_runtime_config(config_path=sibling_path, expected_market=sibling)
        if authoritative_config_yaml_path(installed, repo_root=repo_root) != source:
            return {"sibling_market_status": "unknown", "sibling_rebuild_command": command}
        authored, _ = resolve_yaml_runtime_config(
            repo_root=repo_root, market=sibling, config_path=source
        )
        stale = _activation_effective_config(installed) != _activation_effective_config(authored)
    except Exception:
        return {"sibling_market_status": "unknown", "sibling_rebuild_command": command}
    return {
        "sibling_market_status": "stale" if stale else "fresh",
        "sibling_rebuild_command": command,
    }


def accept_wheel_policy(
    *,
    repo_root: Path,
    market: str,
    actor: str,
    account: str | None = None,
    config_path: str | Path | None = None,
    runtime_root: str | Path | None = None,
    data_config: str | Path | None = None,
    expected_plan_hash: str | None = None,
    apply_changes: bool = False,
) -> dict[str, Any]:
    """Preview or apply the policy acceptance for one market's drifting Wheel accounts."""

    market = str(market or "").strip().lower()
    actor = str(actor or "").strip()
    account_filter = str(account or "").strip().lower() or None
    if market not in {"us", "hk"} or not actor:
        raise ValueError("Wheel policy acceptance requires market us/hk and actor")
    # Optional by design: the acceptor is usually the author of the change, so requiring a
    # dry run would add back the step this command exists to remove. When it is supplied it
    # is still enforced exactly, and `--apply --confirm` plus the ledger guard stay required.
    if expected_plan_hash is not None and (
        not isinstance(expected_plan_hash, str)
        or len(expected_plan_hash) != 64
        or any(c not in "0123456789abcdef" for c in expected_plan_hash)
    ):
        raise ValueError("Wheel policy acceptance --expected-plan-hash must be 64 lowercase hex")

    repo_root = Path(repo_root).resolve()
    runtime_path, cfg = load_runtime_config(
        config_path=config_path, config_key=market, expected_market=market
    )
    runtime_path = runtime_path.resolve()
    root = runtime_path.parent
    for value in (runtime_root, build_effective_env().get("OM_RUNTIME_ROOT")):
        if value and Path(value).expanduser().resolve() != root:
            raise ValueError("Wheel policy acceptance runtime roots disagree")
    if runtime_path != root / f"config.{market}.json":
        raise ValueError("Wheel policy acceptance requires the canonical runtime snapshot")
    source = authoritative_config_yaml_path(cfg, repo_root=repo_root).resolve()
    if source.parent != root:
        raise ValueError("Wheel policy acceptance YAML belongs to another deployment")
    data_path = resolve_position_data_config_path(
        base=repo_root, cfg=cfg, data_config=data_config, config_path=runtime_path
    )
    sqlite_path = Path(
        resolve_position_ledger_sqlite_path(
            base=repo_root, cfg=cfg, data_config=data_path,
            config_path=runtime_path, runtime_root=root,
        )
    ).resolve()
    paths = {
        "runtime_root": str(root),
        "config_path": str(runtime_path),
        "config_yaml_path": str(source),
        "sqlite_path": str(sqlite_path),
        "data_config": str(data_path),
    }
    result: dict[str, Any] = {
        "schema_version": ACCEPT_POLICY_SCHEMA,
        "market": market,
        "actor": actor,
        "account_filter": account_filter,
        "dry_run": not apply_changes,
        "write_applied": False,
        "plan_hash": None,
        "expected_plan_hash": expected_plan_hash,
        "plan": {"accounts": [], "skipped": [], "snapshot_stale": False},
        "snapshot": None,
        "accounts": [],
        "readiness_after": None,
        "paths": paths,
        "failure_phase": None,
        "retry_hint": None,
        "audit_id": None,
    }
    phase = "read"
    authored: dict[str, Any] | None = None

    def authored_config() -> dict[str, Any]:
        nonlocal authored
        if authored is None:
            authored, _ = resolve_yaml_runtime_config(
                repo_root=repo_root, market=market, config_path=source
            )
        return authored

    def candidate_accounts() -> tuple[list[str], list[dict[str, Any]]]:
        config = authored_config()
        raw_wheel = config.get("wheel")
        raw_wheel = raw_wheel if isinstance(raw_wheel, Mapping) else {}
        members = set(normalize_wheel_accounts(raw_wheel.get("accounts", [])))
        descriptors = set(normalize_wheel_activation_by_account(raw_wheel.get("activation_by_account")))
        # The resolved market config carries `accounts` as a list; a user-authored mapping is
        # also accepted, matching how `rebind_wheel_policy` probes the same field.
        raw_accounts = config.get("accounts")
        if isinstance(raw_accounts, Mapping):
            configured = {str(key).strip().lower() for key in raw_accounts}
        elif isinstance(raw_accounts, (list, tuple)):
            configured = {str(value).strip().lower() for value in raw_accounts}
        else:
            configured = set()
        skipped: list[dict[str, Any]] = []
        candidates: list[str] = []
        for account in sorted(members | descriptors):
            if account not in members:
                skipped.append({"account": account, "reason": "not_a_wheel_member"})
            elif account not in descriptors:
                skipped.append({"account": account, "reason": "no_descriptor"})
            elif account not in configured:
                skipped.append({"account": account, "reason": "account_not_configured"})
            else:
                candidates.append(account)
        return candidates, skipped

    try:
        candidates, skipped = candidate_accounts()
        if account_filter is not None:
            if account_filter not in candidates:
                detail = next(
                    (item["reason"] for item in skipped if item["account"] == account_filter),
                    "it is not a Wheel account in this market",
                )
                raise ValueError(
                    f"Wheel policy acceptance account {account_filter} has nothing to accept: {detail}"
                )
            skipped = [item for item in skipped if item["account"] != account_filter]
            candidates = [account_filter]

        windows: dict[str, dict[str, Any]] = {}
        for name in candidates:
            observed = read_wheel_activation_windows_read_only(sqlite_path, market, name)
            if observed["source_status"] != "available":
                raise ValueError(f"Wheel activation storage is {observed['source_status']}")
            if not observed["windows"]:
                skipped.append({"account": name, "reason": "no_active_window"})
                continue
            latest = observed["windows"][-1]
            if latest.get("deactivated_at_ms") is not None:
                skipped.append({"account": name, "reason": "no_active_window"})
                continue
            windows[name] = latest

        phase = "plan"
        raw_installed = json.loads(runtime_path.read_text(encoding="utf-8"))
        snapshot_stale = _activation_effective_config(raw_installed) != _activation_effective_config(
            authored_config()
        )
        plan_accounts: list[dict[str, Any]] = []
        for name in sorted(windows):
            entry = {
                "account": name,
                **_classify(
                    authored=authored_config(), market=market, account=name, latest=windows[name]
                ),
            }
            if entry["classification"] == "accept":
                entry["request_id"] = accept_policy_request_id(
                    market=market, account=name,
                    generation=int(windows[name]["generation"]),
                    revision=int(windows[name].get("policy_binding_revision", 0)),
                    actor=actor, target_policy_hash=entry["target_policy_hash"],
                )
            plan_accounts.append(entry)
        sibling = _sibling_market_state(repo_root=repo_root, market=market, source=source, root=root)
        result["plan"] = {
            "accounts": plan_accounts,
            "skipped": sorted(skipped, key=lambda item: (item["account"], item["reason"])),
            "snapshot_stale": snapshot_stale,
            "snapshot_rebuild_command": build_rebuild_command(
                market=market, runtime_config_path=runtime_path
            ),
            **sibling,
        }
        plan_hash = canonical_sha256(
            {
                "schema_version": "wheel_accept_policy_plan.v1",
                "market": market,
                "source_sha256": _sha256_file(source),
                "snapshot_sha256": _sha256_file(runtime_path),
                "accounts": plan_accounts,
                "skipped": result["plan"]["skipped"],
            }
        )
        result["plan_hash"] = plan_hash
        result["audit_id"] = f"wheel-accept-policy:{market}:{plan_hash[:12]}"
        acceptable = [entry for entry in plan_accounts if entry["classification"] == "accept"]
        refusals = [
            entry for entry in plan_accounts if entry["classification"] not in {"accept", "already_current"}
        ]
        if not acceptable:
            result["status"] = "refused" if refusals else "no_drift"
            return result
        if not apply_changes:
            result["status"] = "planned"
            return result
        if expected_plan_hash is not None and expected_plan_hash != plan_hash:
            raise ValueError("stale Wheel policy acceptance plan")

        phase = "preflight"
        if any((root / "output_shared/state/config_authoring_transactions").glob("*/manifest.json")):
            raise ValueError("pending config authoring journal: recover the original operation first")
        guard = ledger_store_write_guard(data_path, runtime_root=root, config_path=runtime_path)
        if not guard["ok"]:
            raise AgentToolError(
                code="LEDGER_STORE_UNSAFE",
                message="ledger write guard rejected policy acceptance",
                details=guard,
            )
        _activation_owner_preflight([source, runtime_path], runtime_root=root)

        phase = "snapshot"
        if snapshot_stale:
            before_sha256 = _sha256_file(runtime_path)
            build_yaml_runtime_config_file(
                repo_root=repo_root,
                market=market,
                config_path=source,
                output_config_path=runtime_path,
                dry_run=False,
            )
            result["snapshot"] = {
                "written": True,
                "output_config_path": str(runtime_path),
                "before_sha256": before_sha256,
                "after_sha256": _sha256_file(runtime_path),
            }
        else:
            result["snapshot"] = {
                "written": False,
                "output_config_path": str(runtime_path),
                "before_sha256": _sha256_file(runtime_path),
                "after_sha256": None,
            }

        phase = "accept"
        account_results: list[dict[str, Any]] = []
        for entry in acceptable:
            name = entry["account"]
            outcome: dict[str, Any] = {"account": name, "request_id": entry["request_id"]}
            try:
                preview = rebind_wheel_policy(
                    repo_root=repo_root, market=market, account=name,
                    request_id=entry["request_id"], actor=actor,
                    config_path=runtime_path, runtime_root=root, data_config=data_path,
                    apply_changes=False,
                )
                outcome["preview_hash"] = preview.get("preview_hash")
                if preview.get("status") != "planned":
                    # Somebody else already accepted this revision, or the hash converged.
                    outcome["status"] = preview.get("status")
                    outcome["readiness"] = preview.get("readiness")
                    account_results.append(outcome)
                    continue
                applied = rebind_wheel_policy(
                    repo_root=repo_root, market=market, account=name,
                    request_id=entry["request_id"], actor=actor,
                    config_path=runtime_path, runtime_root=root, data_config=data_path,
                    expected_preview_hash=preview["preview_hash"], apply_changes=True,
                )
                outcome["status"] = applied.get("status")
                outcome["receipt"] = applied.get("receipt")
                outcome["readiness"] = applied.get("readiness")
                outcome["write_applied"] = applied.get("write_applied")
            except Exception as exc:  # one account's refusal must not strand the others
                outcome["status"] = "failed"
                outcome["error"] = {
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "code": exc.code if isinstance(exc, AgentToolError) else None,
                }
            account_results.append(outcome)
        result["accounts"] = account_results

        phase = "readback"
        _, refreshed = load_runtime_config(config_path=runtime_path, expected_market=market)
        readiness_after = build_wheel_activation_readiness(
            config=refreshed, market=market, accounts=[item["account"] for item in plan_accounts],
            sqlite_path=sqlite_path,
        )
        result["readiness_after"] = readiness_after
        result["write_applied"] = _activation_any_effect(
            [result["snapshot"]["written"], *[item.get("write_applied") for item in account_results]]
        )
        failed = any(item["status"] == "failed" for item in account_results)
        if failed or refusals:
            result["status"] = "partial"
        elif readiness_after["monitoring_gate"] != "enabled":
            result["status"] = "partial"
            result["retry_hint"] = (
                "Accepted bindings are durable, but the gate is still closed; "
                f"reason_code={readiness_after.get('reason_code')}."
            )
        else:
            result["status"] = "applied"
        return result
    except Exception as exc:
        result["status"] = "incomplete"
        result["failure_phase"] = phase
        if isinstance(exc, AgentToolError):
            result["cause"] = exc.details
        raise AgentToolError(
            code="WHEEL_ACCEPT_POLICY_FAILED", message=str(exc), details=result
        ) from exc


__all__ = ["ACCEPT_POLICY_SCHEMA", "accept_policy_request_id", "accept_wheel_policy"]
