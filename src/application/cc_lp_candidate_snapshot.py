from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.candidate_snapshot_contract import (
    CandidateSnapshotContractError,
    combo_opening_status,
    dependency_hash,
    normalize_combo_scope_results,
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


CC_LP_CANDIDATE_SNAPSHOT_SCHEMA = "cc_lp_candidate_snapshot.v2"
CC_LP_CANDIDATE_SNAPSHOT_FILE = "cc_lp_candidate_snapshot.json"
CC_LP_OPENING_STATUSES = frozenset(
    {
        "candidates_found",
        "no_candidate",
        "data_unavailable",
        "partial_data",
        "market_closed",
        "not_applicable",
    }
)


class CcLpCandidateSnapshotError(RuntimeError):
    """Raised when a CC+LP candidate snapshot cannot be trusted."""


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _pairs(
    payload: Any,
    *,
    run_id: str,
    account: str,
    allowed_symbols: set[str],
) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise CcLpCandidateSnapshotError("cc_lp ranked_pairs must be a list")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(payload):
        if not isinstance(raw, Mapping):
            raise CcLpCandidateSnapshotError("cc_lp ranked pair must be an object")
        try:
            pair = normalize_json_value(dict(raw), field=f"ranked_pairs[{index}]")
        except CandidateSnapshotContractError as exc:
            raise CcLpCandidateSnapshotError(str(exc)) from exc
        pair_id = str(pair.get("candidate_pair_id") or "").strip()
        if not pair_id:
            raise CcLpCandidateSnapshotError("cc_lp ranked pair identity is missing")
        if pair_id in seen:
            raise CcLpCandidateSnapshotError("cc_lp ranked pair identity is duplicated")
        pair_run_id = str(pair.get("run_id") or "").strip()
        if pair_run_id and pair_run_id != run_id:
            raise CcLpCandidateSnapshotError("cc_lp ranked pair run identity mismatch")
        pair_account = str(pair.get("account") or "").strip().lower()
        if pair_account and pair_account != account:
            raise CcLpCandidateSnapshotError("cc_lp ranked pair account identity mismatch")
        symbol = str(pair.get("symbol") or "").strip().upper()
        if not symbol or symbol not in allowed_symbols:
            raise CcLpCandidateSnapshotError("cc_lp ranked pair escapes snapshot scope")
        call_contract = str(pair.get("call_contract_symbol") or "").strip()
        put_contract = str(pair.get("put_contract_symbol") or "").strip()
        if call_contract and put_contract:
            expected_pair_id = (
                f"cc_lp:{symbol}:{call_contract}:{put_contract}"
            )
            if pair_id != expected_pair_id:
                raise CcLpCandidateSnapshotError(
                    "cc_lp ranked pair identity mismatch"
                )
        seen.add(pair_id)
        out.append(pair)
    return out


def validate_cc_lp_candidate_snapshot(
    payload: Mapping[str, Any],
    *,
    expected_run_id: str,
    expected_account: str,
    verify_dependency_root: Path | None = None,
) -> None:
    try:
        item = normalize_json_value(dict(payload or {}), field="cc_lp_snapshot")
        if item.get("schema_version") != CC_LP_CANDIDATE_SNAPSHOT_SCHEMA:
            raise CcLpCandidateSnapshotError("cc_lp candidate snapshot schema mismatch")
        if item.get("run_id") != expected_run_id:
            raise CcLpCandidateSnapshotError("cc_lp candidate snapshot run mismatch")
        if item.get("account") != expected_account:
            raise CcLpCandidateSnapshotError("cc_lp candidate snapshot account mismatch")
        if item.get("candidate_owner") != "cc_lp":
            raise CcLpCandidateSnapshotError("cc_lp candidate snapshot owner mismatch")
        for field in (
            "account_config_sha256",
            "strategy_policy_sha256",
            "required_data_manifest_sha256",
            "content_sha256",
        ):
            sha256_text(item.get(field), field)
        content_hash = str(item["content_sha256"])
        content = {key: value for key, value in item.items() if key != "content_sha256"}
        if canonical_sha256(content) != content_hash:
            raise CcLpCandidateSnapshotError("cc_lp candidate snapshot content hash mismatch")
        utc_timestamp(item.get("sealed_at_utc"))
        required_text(item.get("market"), "market")
        dependencies = normalize_dependencies(
            item.get("dependencies") or [],
            verify_root=verify_dependency_root,
        )
        if dependency_hash(dependencies, "required_data") != item.get(
            "required_data_manifest_sha256"
        ):
            raise CcLpCandidateSnapshotError("required-data dependency hash mismatch")
        scopes = normalize_combo_scope_results(item.get("scope_results") or [], owner="cc_lp")
        pairs = _pairs(
            item.get("ranked_pairs") or [],
            run_id=expected_run_id,
            account=expected_account,
            allowed_symbols={str(row["symbol"]) for row in scopes},
        )
        status = str(item.get("opening_status") or "").strip().lower()
        if status not in CC_LP_OPENING_STATUSES:
            raise CcLpCandidateSnapshotError("cc_lp candidate snapshot status is invalid")
        if combo_opening_status(scopes, selected_count=len(pairs)) != status:
            raise CcLpCandidateSnapshotError("cc_lp terminal status mismatch")
    except CandidateSnapshotContractError as exc:
        raise CcLpCandidateSnapshotError(str(exc)) from exc


def seal_cc_lp_candidate_snapshot(
    *,
    base: Path,
    run_id: str,
    account: str,
    market: str,
    account_config_sha256: str,
    strategy_policy_sha256: str,
    dependencies: Iterable[Mapping[str, Any]],
    scan_statuses: Iterable[Mapping[str, Any]],
    ranked_pairs: Iterable[Mapping[str, Any]],
    opening_status: str | None = None,
    sealed_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Assemble, validate, and immutably publish one account-run CC+LP snapshot."""

    try:
        run_id_norm = required_text(run_id, "run_id")
        account_norm = required_text(account, "account").lower()
        market_norm = required_text(market, "market").lower()
        account_config_hash = sha256_text(account_config_sha256, "account_config_sha256")
        policy_hash = sha256_text(strategy_policy_sha256, "strategy_policy_sha256")
        dependency_rows = normalize_dependencies(dependencies)
        scopes = normalize_combo_scope_results(scan_statuses, owner="cc_lp")
        seal_time = utc_timestamp(sealed_at or datetime.now(timezone.utc))
    except CandidateSnapshotContractError as exc:
        raise CcLpCandidateSnapshotError(str(exc)) from exc
    try:
        pairs = _pairs(
            list(ranked_pairs),
            run_id=run_id_norm,
            account=account_norm,
            allowed_symbols={str(row["symbol"]) for row in scopes},
        )
        resolved_status = combo_opening_status(scopes, selected_count=len(pairs))
    except CandidateSnapshotContractError as exc:
        raise CcLpCandidateSnapshotError(str(exc)) from exc
    if opening_status is not None and str(opening_status).strip().lower() != resolved_status:
        raise CcLpCandidateSnapshotError("cc_lp terminal status mismatch")

    payload: dict[str, Any] = {
        "schema_version": CC_LP_CANDIDATE_SNAPSHOT_SCHEMA,
        "run_id": run_id_norm,
        "account": account_norm,
        "market": market_norm,
        "candidate_owner": "cc_lp",
        "account_config_sha256": account_config_hash,
        "strategy_policy_sha256": policy_hash,
        "required_data_manifest_sha256": dependency_hash(
            dependency_rows,
            "required_data",
        ),
        "dependencies": dependency_rows,
        "sealed_at_utc": seal_time,
        "opening_status": resolved_status,
        "scope_results": scopes,
        "ranked_pairs": pairs,
    }
    payload["content_sha256"] = canonical_sha256(payload)
    validate_cc_lp_candidate_snapshot(
        payload,
        expected_run_id=run_id_norm,
        expected_account=account_norm,
    )
    encoded = _canonical_json_bytes(payload)
    try:
        write_account_run_state_bytes_once_safely(
            base=Path(base),
            run_id=run_id_norm,
            account=account_norm,
            name=CC_LP_CANDIDATE_SNAPSHOT_FILE,
            payload=encoded,
        )
    except AccountRunConfigError as exc:
        raise CcLpCandidateSnapshotError(
            "terminal cc_lp candidate snapshot conflicts or cannot be published"
        ) from exc
    adopted = load_cc_lp_candidate_snapshot(
        base=Path(base),
        run_id=run_id_norm,
        account=account_norm,
    )
    if adopted != payload:
        raise CcLpCandidateSnapshotError("cc_lp candidate snapshot adoption mismatch")
    return adopted


def load_cc_lp_candidate_snapshot(
    *,
    base: Path,
    run_id: str,
    account: str,
) -> dict[str, Any]:
    try:
        run_id_norm = required_text(run_id, "run_id")
        account_norm = required_text(account, "account").lower()
        encoded = read_account_run_state_bytes_safely(
            base=Path(base),
            run_id=run_id_norm,
            account=account_norm,
            name=CC_LP_CANDIDATE_SNAPSHOT_FILE,
        )
        payload = json.loads(encoded.decode("utf-8"))
    except Exception as exc:
        raise CcLpCandidateSnapshotError("cc_lp candidate snapshot is unavailable") from exc
    if not isinstance(payload, dict):
        raise CcLpCandidateSnapshotError("cc_lp candidate snapshot must be an object")
    validate_cc_lp_candidate_snapshot(
        payload,
        expected_run_id=run_id_norm,
        expected_account=account_norm,
        verify_dependency_root=Path(base).resolve(),
    )
    return payload


def project_cc_lp_candidates(
    snapshot: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Project the sealed CC+LP order without selecting or re-ranking rows."""

    return [
        dict(item)
        for item in snapshot.get("ranked_pairs") or []
        if isinstance(item, Mapping)
    ]


__all__ = [
    "CC_LP_CANDIDATE_SNAPSHOT_FILE",
    "CC_LP_CANDIDATE_SNAPSHOT_SCHEMA",
    "CcLpCandidateSnapshotError",
    "load_cc_lp_candidate_snapshot",
    "project_cc_lp_candidates",
    "seal_cc_lp_candidate_snapshot",
    "validate_cc_lp_candidate_snapshot",
]
