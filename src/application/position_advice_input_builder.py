from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.position_advice_authority import (
    AUTHORITY_MODES,
    AuthorityResolution,
    normalize_account_label,
    normalize_portfolio_source,
    scope_for,
)
from src.application.ledger.api import (
    validate_position_fact_snapshot_contract,
)
from src.application.position_advice_authority_service import (
    read_authority_resolution_under_lock,
)
from src.application.position_advice_source_receipts import (
    FRESHNESS_POLICY_SCHEMA,
    PositionAdviceSourceError,
    safe_existing_relative_path,
    sha256_bytes,
    validate_source_manifest,
)
from src.infrastructure.io_utils import atomic_write_json
from src.infrastructure.position_advice_manifest_lock import (
    portfolio_scope_state_dir,
    position_advice_manifest_locks,
)


POSITION_ADVICE_INPUT_SCHEMA = "position_advice_input.v2"
POSITION_ADVICE_OUTPUT_SCHEMA = "position_advice.output.v2"
POSITION_ADVICE_CURRENT_SCHEMA = "account_decision_current.v2"


class PositionAdviceInputError(RuntimeError):
    """Raised when an immutable Position Advice input or publication is unsafe."""


def build_immutable_input(
    *,
    account_run_id: str,
    normalized_account: str,
    broker: str,
    included_markets: list[str] | tuple[str, ...],
    portfolio_scope_id: str,
    normalized_portfolio_source: str,
    portfolio_account_identity_hash: str,
    capacity_pool_authority_id: str | None,
    authority_resolution: AuthorityResolution,
    source_manifest_relpath: str,
    source_manifest: Mapping[str, Any],
    decision_state_snapshot: Mapping[str, Any],
    candidate_inputs: Mapping[str, Any],
    economic_inputs: Mapping[str, Any],
    built_at: datetime | str,
) -> dict[str, Any]:
    account = normalize_account_label(normalized_account)
    expected_scope = scope_for(account)
    if portfolio_scope_id != expected_scope:
        raise PositionAdviceInputError("input scope does not match account label")
    source_manifest_hash = str(source_manifest.get("source_manifest_hash") or "")
    if len(source_manifest_hash) != 64:
        raise PositionAdviceInputError("input source manifest hash is missing")
    if source_manifest.get("account_run_id") != account_run_id:
        raise PositionAdviceInputError("input source manifest run mismatch")
    if source_manifest.get("portfolio_scope_id") != portfolio_scope_id:
        raise PositionAdviceInputError("input source manifest scope mismatch")
    if (
        source_manifest.get("portfolio_account_identity_hash")
        != portfolio_account_identity_hash
    ):
        raise PositionAdviceInputError("input source manifest identity mismatch")

    snapshot = dict(decision_state_snapshot or {})
    fingerprint = str(snapshot.get("decision_state_fingerprint") or "")
    if snapshot.get("snapshot_status") != "trusted" or snapshot.get("actionable") is not True:
        raise PositionAdviceInputError("decision state snapshot is not trusted")
    if len(fingerprint) != 64:
        raise PositionAdviceInputError("decision state fingerprint is missing")
    position_fact_reasons = validate_position_fact_snapshot_contract(
        snapshot
    )
    if position_fact_reasons:
        raise PositionAdviceInputError(
            "decision state position facts are invalid: "
            + ",".join(position_fact_reasons)
        )

    if authority_resolution.portfolio_scope_id != portfolio_scope_id:
        raise PositionAdviceInputError("authority resolution scope mismatch")
    if authority_resolution.resolution_status not in {
        "resolved",
        "first_use_default_v1",
    }:
        raise PositionAdviceInputError("authority resolution is conflicted")
    if authority_resolution.mode not in AUTHORITY_MODES:
        raise PositionAdviceInputError("authority resolution mode is invalid")

    markets = sorted({str(item or "").strip().upper() for item in included_markets})
    if not markets or any(item not in {"US", "HK"} for item in markets):
        raise PositionAdviceInputError("included markets are invalid")
    payload = {
        "schema_version": POSITION_ADVICE_INPUT_SCHEMA,
        "freshness_policy": FRESHNESS_POLICY_SCHEMA,
        "account_run_id": str(account_run_id),
        "normalized_account": account,
        "account": account,
        "broker": str(broker or "").strip().lower(),
        "included_markets": markets,
        "portfolio_scope_id": portfolio_scope_id,
        "normalized_portfolio_source": normalize_portfolio_source(
            normalized_portfolio_source
        ),
        "portfolio_account_identity_hash": str(
            portfolio_account_identity_hash
        ),
        "capacity_pool_authority_id": capacity_pool_authority_id,
        "authority_mode": authority_resolution.mode,
        "authority_generation": authority_resolution.generation,
        "authority_policy_hash": authority_resolution.policy_hash,
        "authority_resolution_status": authority_resolution.resolution_status,
        "authority_covered_strategy_families": list(
            authority_resolution.covered_strategy_families
        ),
        "source_manifest_relpath": str(source_manifest_relpath),
        "source_manifest_hash": source_manifest_hash,
        "source_manifest": [
            dict(item)
            for item in source_manifest.get("source_manifest", [])
            if isinstance(item, Mapping)
        ],
        "source_receipt_hashes": sorted(
            str(item.get("receipt_hash"))
            for item in source_manifest.get("source_manifest", [])
        ),
        "source_artifact_hashes": sorted(
            str(item.get("payload_sha256"))
            for item in source_manifest.get("source_manifest", [])
        ),
        "input_snapshot_ids": sorted(
            str(item.get("snapshot_id"))
            for item in source_manifest.get("source_manifest", [])
        ),
        "decision_state_fingerprint": fingerprint,
        "decision_snapshot_status": snapshot.get("snapshot_status"),
        "decision_state_snapshot": snapshot,
        "candidate_inputs": dict(candidate_inputs or {}),
        "economic_inputs": dict(economic_inputs or {}),
        "positions": [
            dict(item)
            for item in snapshot.get("account_position_lots", [])
            if isinstance(item, Mapping)
        ],
        "combo_groups": [
            dict(item)
            for item in snapshot.get("account_combo_identities", [])
            if isinstance(item, Mapping)
        ],
        "candidates": [
            dict(item)
            for item in candidate_inputs.get("candidates", [])
            if isinstance(item, Mapping)
        ],
        "candidate_decisions": [
            dict(item)
            for item in candidate_inputs.get("candidate_decisions", [])
            if isinstance(item, Mapping)
        ],
        "capacity": dict(economic_inputs.get("capacity") or {}),
        "fees": dict(economic_inputs.get("fees") or {}),
        "quote_quality": dict(economic_inputs.get("quote_quality") or {}),
        "generated_at": _timestamp(built_at),
        "built_at": _timestamp(built_at),
    }
    if not payload["broker"]:
        raise PositionAdviceInputError("broker is required")
    payload["input_hash"] = canonical_sha256(payload)
    return payload


def write_immutable_json(path: Path, payload: Mapping[str, Any], *, hash_field: str) -> None:
    """Write one immutable JSON object; identical replay is accepted."""

    value = dict(payload)
    expected_hash = str(value.get(hash_field) or "")
    without_hash = {key: item for key, item in value.items() if key != hash_field}
    if expected_hash != canonical_sha256(without_hash):
        raise PositionAdviceInputError(f"{hash_field} mismatch")
    destination = Path(path)
    if destination.exists():
        if destination.is_symlink():
            raise PositionAdviceInputError("immutable artifact may not be a symlink")
        try:
            existing = json.loads(destination.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PositionAdviceInputError("immutable artifact is unreadable") from exc
        if existing != value:
            raise PositionAdviceInputError("immutable artifact conflicts with existing bytes")
        return
    atomic_write_json(destination, value, sort_keys=True)


def build_with_stable_inputs(
    *,
    decision_snapshot_reader: Callable[[], Mapping[str, Any]],
    source_manifest_reader: Callable[[], Mapping[str, Any]],
    build: Callable[[dict[str, Any], dict[str, Any]], Mapping[str, Any]],
    max_attempts: int = 2,
) -> dict[str, Any]:
    """Run a pure build only when decision and source identities remain unchanged."""

    attempts = int(max_attempts)
    if attempts < 1 or attempts > 2:
        raise ValueError("stable build supports one attempt plus at most one retry")
    for attempt in range(attempts):
        state_a = dict(decision_snapshot_reader() or {})
        source_a = dict(source_manifest_reader() or {})
        state_fingerprint_a = _trusted_fingerprint(state_a)
        source_hash_a = _manifest_hash(source_a)
        artifact = dict(build(state_a, source_a) or {})
        state_b = dict(decision_snapshot_reader() or {})
        source_b = dict(source_manifest_reader() or {})
        if (
            state_fingerprint_a == _trusted_fingerprint(state_b)
            and source_hash_a == _manifest_hash(source_b)
        ):
            return {
                "artifact": artifact,
                "decision_state_snapshot": state_a,
                "source_manifest": source_a,
                "attempt": attempt + 1,
            }
    raise PositionAdviceInputError("input_changed_during_build")


def publish_current_manifest(
    *,
    base: Path,
    run_id: str,
    account_run_root: Path,
    normalized_account: str,
    broker: str,
    included_markets: list[str] | tuple[str, ...],
    normalized_portfolio_source: str,
    portfolio_account_identity_hash: str,
    source_manifest_relpath: str,
    advice_artifact_relpath: str,
    input_artifact_relpath: str,
    expected_decision_state_fingerprint: str,
    decision_snapshot_reader: Callable[[], Mapping[str, Any]],
    now: datetime | str,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    """Recheck all authority and freshness facts, then atomically switch current."""

    base_path = Path(base).resolve()
    run_id_value = str(run_id or "").strip()
    if not run_id_value:
        raise ValueError("run_id is required")
    run_root = base_path / "output_runs" / run_id_value
    if (
        not run_root.exists()
        or not run_root.is_dir()
        or run_root.is_symlink()
        or run_root.parent != (base_path / "output_runs")
    ):
        raise PositionAdviceInputError("current run root is invalid")
    account_root = Path(account_run_root).resolve()
    try:
        account_root.relative_to(run_root.resolve())
    except ValueError as exc:
        raise PositionAdviceInputError("account run root escapes output run") from exc
    account = normalize_account_label(normalized_account)
    scope_id = scope_for(account)
    source = normalize_portfolio_source(normalized_portfolio_source)
    identity_hash = str(portfolio_account_identity_hash or "")
    now_value = _timestamp(now)

    with position_advice_manifest_locks(
        base=base_path,
        portfolio_scope_id=scope_id,
        global_mode="shared",
        scope_mode="exclusive",
        timeout_seconds=timeout_seconds,
    ):
        resolution = read_authority_resolution_under_lock(
            base=base_path,
            normalized_account=account,
            normalized_portfolio_source=source,
            portfolio_account_identity_hash=identity_hash,
        )
        if (
            resolution.resolution_status != "resolved"
            or resolution.mode not in {"v2_shadow", "v2"}
            or not resolution.policy_hash
        ):
            raise PositionAdviceInputError("authority_conflict_or_v2_inactive")

        source_manifest_path = safe_existing_relative_path(
            run_root,
            source_manifest_relpath,
        )
        source_manifest = _read_json_object(source_manifest_path)
        validated_source = validate_source_manifest(
            source_manifest,
            consumer_run_root=account_root,
            now=now_value,
            expected_account_run_id=run_id_value,
            expected_scope_id=scope_id,
            expected_identity_hash=identity_hash,
        )

        advice_path = safe_existing_relative_path(run_root, advice_artifact_relpath)
        input_path = safe_existing_relative_path(run_root, input_artifact_relpath)
        advice = _read_json_object(advice_path)
        immutable_input = _read_json_object(input_path)
        _validate_artifact_binding(
            advice=advice,
            immutable_input=immutable_input,
            account_run_id=run_id_value,
            normalized_account=account,
            portfolio_scope_id=scope_id,
            portfolio_account_identity_hash=identity_hash,
            source_manifest_hash=validated_source["source_manifest_hash"],
            authority_resolution=resolution,
            expected_decision_state_fingerprint=expected_decision_state_fingerprint,
        )
        requested_markets = sorted(
            {
                str(item or "").strip().upper()
                for item in included_markets
                if str(item or "").strip().upper() in {"US", "HK"}
            }
        )
        if immutable_input.get("included_markets") != requested_markets:
            raise PositionAdviceInputError(
                "current manifest market binding mismatch"
            )

        state_c = dict(decision_snapshot_reader() or {})
        fingerprint_c = _trusted_fingerprint(state_c)
        if fingerprint_c != expected_decision_state_fingerprint:
            raise PositionAdviceInputError("input_changed_before_current_switch")

        current_payload = {
            "schema_version": POSITION_ADVICE_CURRENT_SCHEMA,
            "broker": str(broker or "").strip().lower(),
            "account": account,
            "included_markets": requested_markets,
            "portfolio_scope_id": scope_id,
            "normalized_portfolio_source": source,
            "portfolio_account_identity_hash": identity_hash,
            "account_run_id": run_id_value,
            "account_run_root_relpath": _relative_directory_to_run(
                run_root,
                account_root,
            ),
            "source_manifest_relpath": _relative_to_run(
                run_root,
                source_manifest_path,
            ),
            "source_manifest_hash": validated_source["source_manifest_hash"],
            "source_observed_at_max": max(
                str(item.get("source_observed_at") or "")
                for item in validated_source["source_manifest"]
                if isinstance(item, Mapping)
            ),
            "advice_artifact_relpath": _relative_to_run(run_root, advice_path),
            "advice_artifact_sha256": sha256_bytes(advice_path.read_bytes()),
            "input_artifact_relpath": _relative_to_run(run_root, input_path),
            "input_artifact_sha256": sha256_bytes(input_path.read_bytes()),
            "decision_state_fingerprint": fingerprint_c,
            "authority_mode": resolution.mode,
            "authority_generation": resolution.generation,
            "authority_policy_hash": resolution.policy_hash,
            "switched_at": now_value,
        }
        markets = requested_markets
        if not markets:
            raise PositionAdviceInputError(
                "current manifest included_markets is empty"
            )
        published: dict[str, Path] = {}
        manifests: dict[str, dict[str, Any]] = {}
        for market in markets:
            market_payload = {
                **current_payload,
                "current_market": market,
            }
            market_payload["current_manifest_hash"] = canonical_sha256(
                market_payload
            )
            current_path = (
                portfolio_scope_state_dir(base_path, scope_id)
                / f"account_decision_current.{market}.v2.json"
            )
            if current_path.exists():
                previous = _read_json_object(current_path)
                validate_current_manifest_hash(previous)
                if previous == market_payload:
                    published[market] = current_path
                    manifests[market] = market_payload
                    continue
                if (
                    str(market_payload["source_observed_at_max"])
                    <= str(previous.get("source_observed_at_max") or "")
                ):
                    raise PositionAdviceInputError(
                        "current manifest source generation is not newer"
                    )
            atomic_write_json(current_path, market_payload, sort_keys=True)
            readback = _read_json_object(current_path)
            if readback != market_payload:
                raise PositionAdviceInputError(
                    "current manifest readback mismatch"
                )
            published[market] = current_path
            manifests[market] = market_payload
        primary_market = markets[0]
        return {
            "manifest": manifests[primary_market],
            "path": published[primary_market],
            "manifests": manifests,
            "paths": published,
        }


def validate_current_manifest_hash(manifest: Mapping[str, Any]) -> None:
    payload = dict(manifest or {})
    actual = payload.pop("current_manifest_hash", None)
    if payload.get("schema_version") != POSITION_ADVICE_CURRENT_SCHEMA:
        raise PositionAdviceInputError("current manifest schema is invalid")
    if actual != canonical_sha256(payload):
        raise PositionAdviceInputError("current manifest hash mismatch")


def validate_artifact_binding(
    *,
    advice: Mapping[str, Any],
    immutable_input: Mapping[str, Any],
    account_run_id: str,
    normalized_account: str,
    portfolio_scope_id: str,
    portfolio_account_identity_hash: str,
    source_manifest_hash: str,
    authority_resolution: AuthorityResolution,
    expected_decision_state_fingerprint: str,
) -> None:
    """Validate the immutable input/advice pair against current authority facts."""

    _validate_artifact_binding(
        advice=advice,
        immutable_input=immutable_input,
        account_run_id=account_run_id,
        normalized_account=normalized_account,
        portfolio_scope_id=portfolio_scope_id,
        portfolio_account_identity_hash=portfolio_account_identity_hash,
        source_manifest_hash=source_manifest_hash,
        authority_resolution=authority_resolution,
        expected_decision_state_fingerprint=(
            expected_decision_state_fingerprint
        ),
    )


def _validate_artifact_binding(
    *,
    advice: Mapping[str, Any],
    immutable_input: Mapping[str, Any],
    account_run_id: str,
    normalized_account: str,
    portfolio_scope_id: str,
    portfolio_account_identity_hash: str,
    source_manifest_hash: str,
    authority_resolution: AuthorityResolution,
    expected_decision_state_fingerprint: str,
) -> None:
    input_payload = dict(immutable_input)
    input_hash = input_payload.pop("input_hash", None)
    if input_payload.get("schema_version") != POSITION_ADVICE_INPUT_SCHEMA:
        raise PositionAdviceInputError("immutable input schema is invalid")
    if input_hash != canonical_sha256(input_payload):
        raise PositionAdviceInputError("immutable input hash mismatch")
    advice_payload = dict(advice)
    artifact_hash = advice_payload.pop("artifact_hash", None)
    if advice_payload.get("schema_version") != POSITION_ADVICE_OUTPUT_SCHEMA:
        raise PositionAdviceInputError("advice artifact schema is invalid")
    if artifact_hash != canonical_sha256(advice_payload):
        raise PositionAdviceInputError("advice artifact hash mismatch")
    common_expected = {
        "account_run_id": account_run_id,
        "normalized_account": normalized_account,
        "portfolio_scope_id": portfolio_scope_id,
        "portfolio_account_identity_hash": portfolio_account_identity_hash,
        "source_manifest_hash": source_manifest_hash,
        "decision_state_fingerprint": expected_decision_state_fingerprint,
        "authority_mode": authority_resolution.mode,
        "authority_generation": authority_resolution.generation,
        "authority_policy_hash": authority_resolution.policy_hash,
    }
    for field, expected in common_expected.items():
        if input_payload.get(field) != expected or advice_payload.get(field) != expected:
            raise PositionAdviceInputError(f"artifact binding mismatch: {field}")
    if advice_payload.get("input_hash") != input_hash:
        raise PositionAdviceInputError("advice artifact input hash mismatch")


def _trusted_fingerprint(snapshot: Mapping[str, Any]) -> str:
    if snapshot.get("snapshot_status") != "trusted" or snapshot.get("actionable") is not True:
        raise PositionAdviceInputError("decision state snapshot is unavailable")
    fingerprint = str(snapshot.get("decision_state_fingerprint") or "")
    if len(fingerprint) != 64:
        raise PositionAdviceInputError("decision state fingerprint is invalid")
    return fingerprint


def _manifest_hash(manifest: Mapping[str, Any]) -> str:
    payload = dict(manifest or {})
    actual = payload.pop("source_manifest_hash", None)
    if actual != canonical_sha256(payload):
        raise PositionAdviceInputError("source manifest hash mismatch")
    return str(actual)


def _read_json_object(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise PositionAdviceInputError("artifact may not be a symlink")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PositionAdviceInputError(f"artifact is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise PositionAdviceInputError(f"artifact is not an object: {path}")
    return payload


def _relative_to_run(run_root: Path, path: Path) -> str:
    try:
        return path.resolve(strict=True).relative_to(run_root.resolve()).as_posix()
    except ValueError as exc:
        raise PositionAdviceInputError("artifact escapes output run") from exc


def _relative_directory_to_run(run_root: Path, path: Path) -> str:
    resolved = path.resolve(strict=True)
    if not resolved.is_dir() or resolved.is_symlink():
        raise PositionAdviceInputError("account run root is invalid")
    try:
        return resolved.relative_to(run_root.resolve()).as_posix()
    except ValueError as exc:
        raise PositionAdviceInputError("account run root escapes output run") from exc


def _timestamp(value: datetime | str) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone aware")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "POSITION_ADVICE_CURRENT_SCHEMA",
    "POSITION_ADVICE_INPUT_SCHEMA",
    "POSITION_ADVICE_OUTPUT_SCHEMA",
    "PositionAdviceInputError",
    "build_immutable_input",
    "build_with_stable_inputs",
    "publish_current_manifest",
    "validate_artifact_binding",
    "validate_current_manifest_hash",
    "write_immutable_json",
]
