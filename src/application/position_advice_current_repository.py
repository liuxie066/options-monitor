from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from domain.domain.position_advice_authority import validate_authority_policy
from src.application.position_advice_input_builder import (
    PositionAdviceInputError,
    validate_current_manifest_hash,
)
from src.application.position_advice_source_receipts import (
    PositionAdviceSourceError,
    safe_existing_relative_path,
    sha256_bytes,
    validate_source_manifest,
)
from src.infrastructure.position_advice_manifest_lock import (
    position_advice_state_root,
)


class PositionAdviceCurrentError(RuntimeError):
    """Raised when a current manifest cannot safely protect or serve its run."""


def validate_current_artifacts_under_lock(
    *,
    base: Path,
    portfolio_scope_id: str,
    now: datetime | str | None = None,
    require_fresh: bool = False,
) -> dict[str, Any]:
    """Validate one current pointer while its caller holds manifest locks."""

    base_path = Path(base).resolve()
    scope_id = str(portfolio_scope_id or "").strip()
    current_path = (
        position_advice_state_root(base_path)
        / scope_id
        / "account_decision_current.v2.json"
    )
    try:
        current = _read_json_object(current_path)
        validate_current_manifest_hash(current)
        if current.get("portfolio_scope_id") != scope_id:
            raise PositionAdviceCurrentError("current manifest path scope mismatch")
        run_id = str(current.get("account_run_id") or "").strip()
        if not run_id or "/" in run_id or "\\" in run_id or run_id in {".", ".."}:
            raise PositionAdviceCurrentError("current run id is invalid")
        runs_root = base_path / "output_runs"
        run_root = runs_root / run_id
        if (
            not run_root.exists()
            or not run_root.is_dir()
            or run_root.is_symlink()
            or run_root.resolve().parent != runs_root.resolve()
        ):
            raise PositionAdviceCurrentError("current run root is missing or unsafe")

        account = str(current.get("account") or "").strip().lower()
        expected_account_root_relpath = f"accounts/{account}"
        if current.get("account_run_root_relpath") != expected_account_root_relpath:
            raise PositionAdviceCurrentError("current account run root is noncanonical")
        account_root = run_root / expected_account_root_relpath
        if (
            not account_root.exists()
            or not account_root.is_dir()
            or account_root.is_symlink()
        ):
            raise PositionAdviceCurrentError("current account run root is unavailable")

        source_path = safe_existing_relative_path(
            run_root,
            current.get("source_manifest_relpath"),
        )
        advice_path = safe_existing_relative_path(
            run_root,
            current.get("advice_artifact_relpath"),
        )
        input_path = safe_existing_relative_path(
            run_root,
            current.get("input_artifact_relpath"),
        )
        if sha256_bytes(advice_path.read_bytes()) != current.get(
            "advice_artifact_sha256"
        ):
            raise PositionAdviceCurrentError("current advice artifact hash mismatch")
        if sha256_bytes(input_path.read_bytes()) != current.get(
            "input_artifact_sha256"
        ):
            raise PositionAdviceCurrentError("current input artifact hash mismatch")
        source_manifest = _read_json_object(source_path)
        validated_source = validate_source_manifest(
            source_manifest,
            consumer_run_root=account_root,
            now=now or datetime.now(timezone.utc),
            expected_account_run_id=run_id,
            expected_scope_id=scope_id,
            expected_identity_hash=str(
                current.get("portfolio_account_identity_hash") or ""
            ),
            require_fresh=require_fresh,
        )
        if (
            validated_source["source_manifest_hash"]
            != current.get("source_manifest_hash")
        ):
            raise PositionAdviceCurrentError("current source manifest hash mismatch")
        return {
            "current": current,
            "current_path": current_path,
            "run_root": run_root.resolve(),
            "account_root": account_root.resolve(),
            "source_manifest": validated_source,
            "source_manifest_path": source_path,
            "advice": _read_json_object(advice_path),
            "advice_path": advice_path,
            "immutable_input": _read_json_object(input_path),
            "input_path": input_path,
        }
    except (
        OSError,
        ValueError,
        PositionAdviceCurrentError,
        PositionAdviceInputError,
        PositionAdviceSourceError,
    ) as exc:
        if isinstance(exc, PositionAdviceCurrentError):
            raise
        raise PositionAdviceCurrentError(str(exc)) from exc


def collect_protected_current_runs_under_global_lock(*, base: Path) -> set[Path]:
    """Protect only runs referenced by a validated current manifest."""

    root = position_advice_state_root(base)
    if not root.exists():
        return set()
    if not root.is_dir() or root.is_symlink():
        raise PositionAdviceCurrentError("position advice state root is unsafe")
    protected: set[Path] = set()
    for child in sorted(root.iterdir()):
        if child.name == ".manifest.lock":
            continue
        if child.is_symlink():
            raise PositionAdviceCurrentError("position advice scope may not be a symlink")
        if not child.is_dir():
            raise PositionAdviceCurrentError("unexpected position advice control-plane file")
        current_path = child / "account_decision_current.v2.json"
        if current_path.exists():
            validated = validate_current_artifacts_under_lock(
                base=base,
                portfolio_scope_id=child.name,
                require_fresh=False,
            )
            protected.add(Path(validated["run_root"]).resolve())
        policy_path = child / "authority_policy.v1.json"
        if not policy_path.exists():
            continue
        policy = _read_json_object(policy_path)
        policy_reasons = validate_authority_policy(
            policy,
            expected_scope_id=child.name,
        )
        if policy_reasons:
            raise PositionAdviceCurrentError(
                "authority policy is invalid during cleanup: "
                + ",".join(policy_reasons)
            )
    return protected


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file() or path.is_symlink():
        raise PositionAdviceCurrentError(f"current artifact is unavailable: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PositionAdviceCurrentError(f"current artifact is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise PositionAdviceCurrentError(f"current artifact is not an object: {path}")
    return payload


__all__ = [
    "PositionAdviceCurrentError",
    "collect_protected_current_runs_under_global_lock",
    "validate_current_artifacts_under_lock",
]
