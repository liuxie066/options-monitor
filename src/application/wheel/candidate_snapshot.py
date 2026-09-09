from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.candidate_snapshot_contract import (
    CANDIDATE_CAPTURE_STATUSES,
    CandidateSnapshotContractError,
    dependency_hash,
    normalize_dependencies,
    normalize_json_value,
    required_text,
    sha256_text,
    utc_timestamp,
)
from src.application.tick_run_workspace import (
    AccountRunConfigError,
    read_account_run_state_bytes_safely,
    write_account_run_state_bytes_once_safely,
)


WHEEL_CANDIDATE_SNAPSHOT_SCHEMA_V1 = "wheel_candidate_snapshot.v1"
WHEEL_CANDIDATE_SNAPSHOT_SCHEMA_V2 = "wheel_candidate_snapshot.v2"
WHEEL_CANDIDATE_SNAPSHOT_SCHEMA = WHEEL_CANDIDATE_SNAPSHOT_SCHEMA_V2
WHEEL_CANDIDATE_SNAPSHOT_FILE_V1 = "wheel_candidate_snapshot.json"
WHEEL_CANDIDATE_SNAPSHOT_FILE_V2 = "wheel_candidate_snapshot.v2.json"
WHEEL_CANDIDATE_SNAPSHOT_FILE = WHEEL_CANDIDATE_SNAPSHOT_FILE_V2


class WheelCandidateSnapshotError(RuntimeError):
    pass


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def _scopes(
    rows: Iterable[Mapping[str, Any]],
    *,
    schema_version: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in rows:
        symbol = required_text(raw.get("symbol"), "Wheel scope symbol").upper()
        direction = (
            required_text(raw.get("direction"), "Wheel scope direction").lower()
            if schema_version == WHEEL_CANDIDATE_SNAPSHOT_SCHEMA_V2
            else "call"
        )
        if direction not in {"call", "put"}:
            raise CandidateSnapshotContractError("Wheel candidate scope direction is invalid")
        identity = (symbol, direction)
        if identity in seen:
            raise CandidateSnapshotContractError("Wheel candidate scope is duplicated")
        seen.add(identity)
        status = required_text(raw.get("status"), "Wheel scope status").lower()
        if status not in CANDIDATE_CAPTURE_STATUSES:
            raise CandidateSnapshotContractError("Wheel candidate scope status is invalid")
        candidate_count = int(raw.get("candidate_count") or 0)
        if candidate_count < 0 or (status != "completed" and candidate_count):
            raise CandidateSnapshotContractError("Wheel candidate scope count is invalid")
        out.append(
            {
                "scope": "strategy",
                "symbol": symbol,
                **(
                    {"direction": direction}
                    if schema_version == WHEEL_CANDIDATE_SNAPSHOT_SCHEMA_V2
                    else {}
                ),
                "strategy_mode": "wheel",
                "candidate_owner": "wheel",
                "status": status,
                "reason_code": str(raw.get("reason_code") or raw.get("reason") or "").strip() or None,
                "candidate_count": candidate_count,
                "quote_snapshot_id": str(raw.get("quote_snapshot_id") or "").strip() or None,
                "quote_receipt_relpath": str(raw.get("quote_receipt_relpath") or "").strip() or None,
            }
        )
    return sorted(
        out,
        key=lambda row: (str(row["symbol"]), str(row.get("direction") or "call")),
    )


def _batches(
    rows: Iterable[Mapping[str, Any]],
    *,
    account: str,
    schema_version: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(rows):
        try:
            row = normalize_json_value(dict(raw), field=f"batches[{index}]")
            if schema_version == WHEEL_CANDIDATE_SNAPSHOT_SCHEMA_V2:
                branch_id = required_text(row.get("wheel_branch_id"), "wheel_branch_id")
                direction = required_text(row.get("direction"), "direction").lower()
                if direction not in {"call", "put"}:
                    raise CandidateSnapshotContractError("Wheel candidate batch direction is invalid")
                generation_hash = row.get("branch_generation_hash")
            else:
                branch_id = required_text(row.get("stock_lot_id"), "stock_lot_id")
                direction = "call"
                generation_hash = row.get("batch_generation_hash")
            identity = (branch_id, direction)
            if identity in seen:
                raise CandidateSnapshotContractError("Wheel candidate batch is duplicated")
            seen.add(identity)
            if str(row.get("account") or account).strip().lower() != account:
                raise CandidateSnapshotContractError("Wheel candidate batch account mismatch")
            sha256_text(generation_hash, "branch_generation_hash")
            sha256_text(row.get("projection_hash"), "projection_hash")
            raw_candidates = row.get("raw_candidates") or []
            if not isinstance(raw_candidates, list):
                raise CandidateSnapshotContractError("Wheel raw_candidates must be a list")
            candidate_ids = [required_text(item.get("candidate_id"), "candidate_id") for item in raw_candidates]
            if len(candidate_ids) != len(set(candidate_ids)):
                raise CandidateSnapshotContractError("Wheel candidate identity is duplicated")
            final = row.get("final_candidate")
            granted = int(row.get("granted_contracts") or 0)
            if final is None:
                if granted != 0:
                    raise CandidateSnapshotContractError("Wheel grant requires final candidate")
            else:
                if not isinstance(final, Mapping) or not raw_candidates:
                    raise CandidateSnapshotContractError("Wheel final candidate is invalid")
                final_id = required_text(
                    final.get("final_candidate_id") or final.get("candidate_id"),
                    "final_candidate_id",
                )
                if final_id != candidate_ids[0] or granted <= 0 or int(final.get("granted_contracts") or 0) != granted:
                    raise CandidateSnapshotContractError("Wheel final candidate does not match allocation")
                if schema_version == WHEEL_CANDIDATE_SNAPSHOT_SCHEMA_V2:
                    if str(final.get("wheel_branch_id") or "").strip() != branch_id:
                        raise CandidateSnapshotContractError("Wheel final candidate branch mismatch")
                    final_direction = str(final.get("direction") or direction).strip().lower()
                    if final_direction != direction:
                        raise CandidateSnapshotContractError("Wheel final candidate direction mismatch")
                    if direction == "put":
                        for field in (
                            "capacity_identity_hash",
                            "allocation_input_hash",
                        ):
                            sha256_text(final.get(field), field)
                        required_text(
                            final.get("cash_reservation_currency"),
                            "cash_reservation_currency",
                        )
                        if float(final.get("cash_reservation_amount") or 0) <= 0:
                            raise CandidateSnapshotContractError(
                                "Wheel Put cash reservation is invalid"
                            )
            out.append(row)
        except (TypeError, ValueError) as exc:
            if isinstance(exc, CandidateSnapshotContractError):
                raise
            raise CandidateSnapshotContractError(str(exc)) from exc
    return sorted(
        out,
        key=lambda row: (
            str(row.get("wheel_branch_id") or row.get("stock_lot_id") or ""),
            str(row.get("direction") or "call"),
        ),
    )


def _opening_status(scopes: list[Mapping[str, Any]], batches: list[Mapping[str, Any]]) -> str:
    if not scopes:
        raise CandidateSnapshotContractError("Wheel candidate scopes are missing")
    states = {str(row.get("status") or "") for row in scopes}
    candidate_count = sum(len(row.get("raw_candidates") or []) for row in batches)
    if states == {"completed"}:
        if any(row.get("reason_code") == "partial_data" for row in scopes):
            return "partial_data"
        return "candidates_found" if candidate_count else "no_candidate"
    if states == {"not_applicable"}:
        return "not_applicable"
    if "completed" in states or "not_applicable" in states:
        return "partial_data"
    return "data_unavailable"


def _allocations(
    rows: Any,
    *,
    schema_version: str,
) -> list[dict[str, Any]]:
    normalized = normalize_json_value(rows or [], field="capacity_allocations")
    if not isinstance(normalized, list) or any(
        not isinstance(row, Mapping) for row in normalized
    ):
        raise CandidateSnapshotContractError("Wheel capacity allocations are invalid")
    allocations = [dict(row) for row in normalized]
    if schema_version == WHEEL_CANDIDATE_SNAPSHOT_SCHEMA_V2:
        seen: set[tuple[str, str]] = set()
        for row in allocations:
            if str(row.get("strategy_family") or "").strip().lower() != "wheel":
                continue
            direction = required_text(row.get("direction"), "allocation direction").lower()
            if direction not in {"call", "put"}:
                raise CandidateSnapshotContractError("Wheel allocation direction is invalid")
            branch_id = required_text(row.get("wheel_branch_id"), "wheel_branch_id")
            identity = (branch_id, direction)
            if identity in seen:
                raise CandidateSnapshotContractError("Wheel capacity allocation is duplicated")
            seen.add(identity)
            if direction == "put":
                sha256_text(row.get("capacity_identity_hash"), "capacity_identity_hash")
                sha256_text(row.get("allocation_input_hash"), "allocation_input_hash")
    return sorted(
        allocations,
        key=lambda row: (
            str(row.get("wheel_branch_id") or row.get("stock_lot_id") or ""),
            str(row.get("direction") or "call"),
            str(row.get("claim_id") or ""),
        ),
    )


def validate_wheel_candidate_snapshot(
    payload: Mapping[str, Any],
    *,
    expected_run_id: str,
    expected_account: str,
    verify_dependency_root: Path | None = None,
) -> None:
    try:
        item = normalize_json_value(dict(payload or {}), field="wheel_snapshot")
        schema_version = str(item.get("schema_version") or "")
        if schema_version not in {
            WHEEL_CANDIDATE_SNAPSHOT_SCHEMA_V1,
            WHEEL_CANDIDATE_SNAPSHOT_SCHEMA_V2,
        }:
            raise WheelCandidateSnapshotError("Wheel candidate snapshot schema mismatch")
        if item.get("run_id") != expected_run_id or item.get("account") != expected_account:
            raise WheelCandidateSnapshotError("Wheel candidate snapshot identity mismatch")
        if item.get("candidate_owner") != "wheel":
            raise WheelCandidateSnapshotError("Wheel candidate snapshot owner mismatch")
        for field in ("account_config_sha256", "strategy_policy_sha256", "required_data_manifest_sha256", "snapshot_hash", "content_sha256"):
            sha256_text(item.get(field), field)
        content = {key: value for key, value in item.items() if key != "content_sha256"}
        if canonical_sha256(content) != item["content_sha256"]:
            raise WheelCandidateSnapshotError("Wheel candidate snapshot content hash mismatch")
        utc_timestamp(item.get("sealed_at_utc"))
        required_text(item.get("market"), "market")
        dependencies = normalize_dependencies(item.get("dependencies") or [], verify_root=verify_dependency_root)
        if dependency_hash(dependencies, "required_data") != item["required_data_manifest_sha256"]:
            raise WheelCandidateSnapshotError("Wheel required-data dependency hash mismatch")
        scopes = _scopes(
            item.get("scope_results") or [],
            schema_version=schema_version,
        )
        batches = _batches(
            item.get("batches") or [],
            account=expected_account,
            schema_version=schema_version,
        )
        allocations = _allocations(
            item.get("capacity_allocations") or [],
            schema_version=schema_version,
        )
        if item.get("opening_status") != _opening_status(scopes, batches):
            raise WheelCandidateSnapshotError("Wheel candidate snapshot terminal status mismatch")
        binding = {
            "run_id": item["run_id"],
            "account": item["account"],
            "account_config_sha256": item["account_config_sha256"],
            "strategy_policy_sha256": item["strategy_policy_sha256"],
            "required_data_manifest_sha256": item["required_data_manifest_sha256"],
            "scope_results": scopes,
            "batches": batches,
            "capacity_allocations": allocations,
        }
        if canonical_sha256(binding) != item["snapshot_hash"]:
            raise WheelCandidateSnapshotError("Wheel candidate snapshot input hash mismatch")
    except CandidateSnapshotContractError as exc:
        raise WheelCandidateSnapshotError(str(exc)) from exc


def seal_wheel_candidate_snapshot(
    *,
    base: Path,
    run_id: str,
    account: str,
    market: str,
    account_config_sha256: str,
    strategy_policy_sha256: str,
    dependencies: Iterable[Mapping[str, Any]],
    scope_results: Iterable[Mapping[str, Any]],
    batches: Iterable[Mapping[str, Any]],
    capacity_allocations: Iterable[Mapping[str, Any]] = (),
    sealed_at: datetime | str | None = None,
) -> dict[str, Any]:
    try:
        run = required_text(run_id, "run_id")
        acct = required_text(account, "account").lower()
        market_value = required_text(market, "market").lower()
        config_hash = sha256_text(account_config_sha256, "account_config_sha256")
        policy_hash = sha256_text(strategy_policy_sha256, "strategy_policy_sha256")
        dependency_rows = normalize_dependencies(dependencies)
        scopes = _scopes(
            scope_results,
            schema_version=WHEEL_CANDIDATE_SNAPSHOT_SCHEMA_V2,
        )
        batch_rows = _batches(
            batches,
            account=acct,
            schema_version=WHEEL_CANDIDATE_SNAPSHOT_SCHEMA_V2,
        )
        allocation_rows = _allocations(
            [dict(row) for row in capacity_allocations],
            schema_version=WHEEL_CANDIDATE_SNAPSHOT_SCHEMA_V2,
        )
        seal_time = utc_timestamp(sealed_at or datetime.now(timezone.utc))
    except CandidateSnapshotContractError as exc:
        raise WheelCandidateSnapshotError(str(exc)) from exc
    required_data_hash = dependency_hash(dependency_rows, "required_data")
    binding = {
        "run_id": run,
        "account": acct,
        "account_config_sha256": config_hash,
        "strategy_policy_sha256": policy_hash,
        "required_data_manifest_sha256": required_data_hash,
        "scope_results": scopes,
        "batches": batch_rows,
        "capacity_allocations": allocation_rows,
    }
    payload = {
        "schema_version": WHEEL_CANDIDATE_SNAPSHOT_SCHEMA,
        "run_id": run,
        "account": acct,
        "market": market_value,
        "candidate_owner": "wheel",
        "account_config_sha256": config_hash,
        "strategy_policy_sha256": policy_hash,
        "required_data_manifest_sha256": required_data_hash,
        "dependencies": dependency_rows,
        "sealed_at_utc": seal_time,
        "opening_status": _opening_status(scopes, batch_rows),
        "scope_results": scopes,
        "batches": batch_rows,
        "capacity_allocations": allocation_rows,
        "snapshot_hash": canonical_sha256(binding),
    }
    payload["content_sha256"] = canonical_sha256(payload)
    validate_wheel_candidate_snapshot(payload, expected_run_id=run, expected_account=acct)
    try:
        write_account_run_state_bytes_once_safely(
            base=Path(base), run_id=run, account=acct, name=WHEEL_CANDIDATE_SNAPSHOT_FILE, payload=_canonical_bytes(payload)
        )
    except AccountRunConfigError as exc:
        raise WheelCandidateSnapshotError("terminal Wheel candidate snapshot conflicts") from exc
    adopted = load_wheel_candidate_snapshot(base=base, run_id=run, account=acct)
    if adopted != payload:
        raise WheelCandidateSnapshotError("Wheel candidate snapshot adoption mismatch")
    return adopted


def load_wheel_candidate_snapshot(*, base: Path, run_id: str, account: str) -> dict[str, Any]:
    try:
        run = required_text(run_id, "run_id")
        acct = required_text(account, "account").lower()
        payloads = []
        for name in (
            WHEEL_CANDIDATE_SNAPSHOT_FILE_V2,
            WHEEL_CANDIDATE_SNAPSHOT_FILE_V1,
        ):
            try:
                payloads.append(json.loads(
                    read_account_run_state_bytes_safely(
                        base=Path(base),
                        run_id=run,
                        account=acct,
                        name=name,
                    ).decode()
                ))
            except AccountRunConfigError:
                continue
        if not payloads:
            raise FileNotFoundError("Wheel candidate snapshot is missing")
        if len(payloads) != 1:
            raise WheelCandidateSnapshotError("artifact_version_mismatch")
        payload = payloads[0]
    except WheelCandidateSnapshotError:
        raise
    except Exception as exc:
        raise WheelCandidateSnapshotError("Wheel candidate snapshot is unavailable") from exc
    if not isinstance(payload, dict):
        raise WheelCandidateSnapshotError("Wheel candidate snapshot must be an object")
    validate_wheel_candidate_snapshot(
        payload,
        expected_run_id=run,
        expected_account=acct,
        verify_dependency_root=Path(base).resolve(),
    )
    return payload


__all__ = [
    "WHEEL_CANDIDATE_SNAPSHOT_FILE",
    "WHEEL_CANDIDATE_SNAPSHOT_FILE_V1",
    "WHEEL_CANDIDATE_SNAPSHOT_FILE_V2",
    "WHEEL_CANDIDATE_SNAPSHOT_SCHEMA",
    "WHEEL_CANDIDATE_SNAPSHOT_SCHEMA_V1",
    "WHEEL_CANDIDATE_SNAPSHOT_SCHEMA_V2",
    "WheelCandidateSnapshotError",
    "load_wheel_candidate_snapshot",
    "seal_wheel_candidate_snapshot",
    "validate_wheel_candidate_snapshot",
]
