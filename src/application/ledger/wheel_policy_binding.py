"""Atomic policy-binding CAS; configuration observations expose no ledger handle."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256 as request_fingerprint
from .repository import with_sqlite_repo_transaction


class WheelPolicyBindingReplayError(ValueError):
    """A matched durable receipt remains available when replay verification fails."""

    def __init__(self, message: str, receipt: dict[str, Any]) -> None:
        super().__init__(message)
        self.receipt = receipt


def wheel_policy_file_identity(path: Path, *, content: bool = False) -> dict[str, Any]:
    stat = path.stat()
    if not path.is_file():
        raise ValueError("Wheel policy binding requires regular files")
    result = {"path": str(path.resolve()), "device": stat.st_dev, "inode": stat.st_ino}
    if content:
        result["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def build_wheel_policy_binding_request(
    *, observed: Mapping[str, Any], configuration: Mapping[str, Any],
    sqlite_path: str | Path, market: str, account: str, request_id: str, actor: str,
) -> dict[str, Any]:
    from src.application.wheel.config import WHEEL_ACTIVATION_DESCRIPTOR_FIELDS

    windows = observed["windows"]
    if not windows or windows[-1]["deactivated_at_ms"] is not None:
        raise ValueError("Wheel policy binding requires the latest open window")
    current = windows[-1]
    descriptor = configuration["descriptor"]
    if descriptor is None or any(
        descriptor[k] != current[k]
        for k in ("market", "account", *WHEEL_ACTIVATION_DESCRIPTOR_FIELDS)
    ):
        raise ValueError("Wheel activation descriptor boundary mismatch")
    original = {
        k: v for k, v in current.items()
        if k not in {"effective_policy_hash", "policy_binding_revision"}
    }
    return {
        "schema_version": "wheel_policy_rebind_request.v1", "market": market,
        "account": account, "request_id": request_id, "actor": actor,
        "window": original, "expected_revision": current.get("policy_binding_revision", 0),
        "effective_policy_hash": current.get("effective_policy_hash", current["policy_hash"]),
        "target_policy_hash": configuration["target_policy_hash"],
        "source": configuration["source"], "runtime": configuration["runtime"],
        "ledger": wheel_policy_file_identity(Path(sqlite_path)),
    }


def record_wheel_policy_binding(
    repo: Any, *, request: Mapping[str, Any], expected_preview_hash: str,
    read_configuration: Callable[[], Mapping[str, Any]],
) -> dict[str, Any]:
    # The observer revalidates external config only; no connection/rows leave ledger.
    market, account = request["market"], request["account"]
    request_id, actor = request["request_id"], request["actor"]

    def commit(sqlite_repo: Any, conn: Any) -> dict[str, Any]:
        windows = sqlite_repo.list_wheel_activation_windows(
            market=market, account=account, conn=conn,
        )
        current = sqlite_repo.get_current_wheel_activation_window(
            market=market, account=account, conn=conn,
        )
        if current:
            windows[-1] = current
        bindings = sqlite_repo.list_wheel_policy_bindings(
            market=market, account=account, conn=conn,
        )
        observed = {"windows": windows, "policy_bindings": bindings}
        matches = [r for r in bindings if r["request_id"] == request_id]
        if matches:
            receipt = matches[0]
            if len(matches) != 1 or receipt["actor"] != actor:
                raise ValueError("Wheel policy binding request identity conflict")
            if expected_preview_hash != receipt["request_hash"]:
                raise ValueError("Wheel policy binding replay preview mismatch")
            if receipt["request"]["ledger"] != wheel_policy_file_identity(sqlite_repo.db_path):
                raise ValueError("Wheel policy binding ledger identity changed")
            try:
                if (not current or current["deactivated_at_ms"] is not None
                        or receipt["generation"] != current["generation"]
                        or receipt["revision"] != current.get("policy_binding_revision", 0)):
                    raise ValueError("Wheel policy binding request superseded")
                checked = build_wheel_policy_binding_request(
                    observed=observed, configuration=read_configuration(),
                    sqlite_path=sqlite_repo.db_path, market=market, account=account,
                    request_id=request_id, actor=actor,
                )
                if receipt["policy_hash"] != checked["target_policy_hash"]:
                    raise ValueError("Wheel policy binding request identity conflict")
            except Exception as exc:
                raise WheelPolicyBindingReplayError(str(exc), receipt) from exc
            return {"receipt": receipt, "idempotent": True}
        checked = build_wheel_policy_binding_request(
            observed=observed, configuration=read_configuration(),
            sqlite_path=sqlite_repo.db_path, market=market, account=account,
            request_id=request_id, actor=actor,
        )
        if request_fingerprint(checked) != expected_preview_hash:
            raise ValueError("stale Wheel policy binding preview")
        receipt = sqlite_repo.append_wheel_policy_binding(
            request=checked, request_hash=expected_preview_hash, conn=conn,
        )
        return {"receipt": receipt, "idempotent": False}

    return with_sqlite_repo_transaction(repo, commit)
