from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.candidate_snapshot_contract import (
    combo_opening_status,
    normalize_combo_scope_results,
    normalize_dependencies,
    normalize_json_value,
    required_text,
    sha256_text,
    utc_timestamp,
)
from src.application.experience_mode import (
    experience_fields,
    validate_experience_fields,
)
from src.application.source_receipts import sha256_bytes
from src.application.strategy_scan_status import (
    STRATEGY_SCAN_STATUS_INDEX_V3_FILE,
    STRATEGY_SCAN_STATUS_INDEX_V3_SCHEMA,
    StrategyScanStatusError,
    validate_strategy_scan_status_index_v3,
)
from src.application.tick_run_workspace import (
    AccountRunConfigError,
    read_account_run_state_bytes_safely,
    write_account_run_state_bytes_once_safely,
)


EXPERIENCE_CANDIDATE_DEPENDENCIES = frozenset(
    {"required_data", "fx", "earnings_rv"}
)
EXPERIENCE_CANDIDATE_MANIFEST_SCHEMA = "candidate_snapshot_manifest.v2"
EXPERIENCE_CANDIDATE_MANIFEST_FILE = "candidate_snapshot_manifest.v2.json"
EXPERIENCE_OWNER_SCHEMAS = {
    "opening": "opening_candidate_snapshot.v2",
    "sp_lc": "combo_yield_candidate_snapshot.v3",
    "cc_lp": "cc_lp_candidate_snapshot.v3",
}
EXPERIENCE_OWNER_FILES = {
    "opening": "opening_candidate_snapshot.json",
    "sp_lc": "combo_yield_candidate_snapshot.json",
    "cc_lp": "cc_lp_candidate_snapshot.json",
}
_FORBIDDEN_AUTHORITY_KEYS = frozenset(
    {"physical_account", "futu_account_id", "cash_by_currency"}
)
_EXPERIENCE_FIELD_NAMES = frozenset(
    {"scan_mode", "capacity_source", "account_display_name", "executable"}
)


class ExperienceCandidateSnapshotError(RuntimeError):
    pass


def _identity(value: Any, field: str, *, lower: bool = False) -> str:
    text = required_text(value, field)
    if text in {".", ".."} or Path(text).name != text:
        raise ExperienceCandidateSnapshotError(f"experience {field} is invalid")
    return text.lower() if lower else text


def _experience_projection(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: payload.get(key) for key in _EXPERIENCE_FIELD_NAMES}


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
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


def _state_dir(base: Path, run_id: str, account: str) -> Path:
    return (
        Path(base).resolve()
        / "output_runs"
        / run_id
        / "accounts"
        / account
        / "state"
    )


def _scope_rows(
    statuses: Iterable[Mapping[str, Any]],
    *,
    owner: str,
) -> list[dict[str, Any]]:
    if owner in {"sp_lc", "cc_lp"}:
        source = [dict(item) for item in statuses]
        counts = {
            (
                str(item.get("symbol") or "").strip().upper(),
                str(item.get("strategy_mode") or "").strip().lower(),
            ): item.get("candidate_count")
            for item in source
        }
        rows = normalize_combo_scope_results(source, owner=owner)
        for row in rows:
            row["candidate_count"] = counts.get(
                (str(row["symbol"]), str(row["strategy_mode"]))
            )
        return rows
    rows: list[dict[str, Any]] = []
    for raw in statuses:
        row = dict(raw)
        rows.append(
            {
                "scope": "strategy",
                "symbol": required_text(row.get("symbol"), "scope symbol").upper(),
                "strategy_mode": required_text(
                    row.get("strategy_mode"), "scope strategy mode"
                ).lower(),
                "candidate_owner": "opening",
                "status": required_text(row.get("status"), "scope status").lower(),
                "reason_code": str(
                    row.get("reason") or row.get("reason_code") or ""
                ).strip()
                or None,
                "quote_snapshot_id": str(
                    row.get("quote_snapshot_id") or ""
                ).strip()
                or None,
                "quote_receipt_relpath": str(
                    row.get("quote_receipt_relpath") or ""
                ).strip()
                or None,
                "candidate_count": row.get("candidate_count"),
            }
        )
    return sorted(rows, key=lambda row: (row["symbol"], row["strategy_mode"]))


def _opening_status(scopes: list[dict[str, Any]], selected_count: int) -> str:
    if not scopes:
        raise ExperienceCandidateSnapshotError("experience candidate scopes are missing")
    observed = [row for row in scopes if row.get("status") != "not_applicable"]
    if observed and all(row.get("reason_code") == "market_closed" for row in observed):
        return "market_closed"
    states = {str(row.get("status") or "") for row in scopes}
    if states == {"completed"}:
        if any(row.get("reason_code") == "partial_data" for row in scopes):
            return "partial_data"
        return "candidates_found" if selected_count else "no_candidate"
    if "completed" in states or "not_applicable" in states:
        return "partial_data"
    return "data_unavailable"


def _tag_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    fields: Mapping[str, Any],
) -> list[dict[str, Any]]:
    return [
        normalize_json_value({**dict(row), **dict(fields)}, field="experience row")
        for row in rows
    ]


def _assert_counts(
    *,
    scopes: list[dict[str, Any]],
    selected: list[dict[str, Any]],
) -> None:
    counts: dict[tuple[str, str], int] = {}
    for row in selected:
        key = (
            str(row.get("symbol") or "").strip().upper(),
            str(row.get("strategy_mode") or "combo_yield").strip().lower(),
        )
        counts[key] = counts.get(key, 0) + 1
    for scope in scopes:
        key = (str(scope["symbol"]), str(scope["strategy_mode"]))
        count = counts.get(key, 0)
        if scope["status"] == "completed":
            expected = scope.get("candidate_count")
            if expected is not None and int(expected) != count:
                raise ExperienceCandidateSnapshotError(
                    f"experience candidate count mismatch: {key[0]}:{key[1]}"
                )
        elif count:
            raise ExperienceCandidateSnapshotError(
                f"experience non-completed scope contains candidates: {key[0]}:{key[1]}"
            )


def _seal_owner(
    *,
    base: Path,
    run_id: str,
    account: str,
    market: str,
    owner: str,
    account_config_sha256: str,
    strategy_policy_sha256: str,
    dependencies: list[dict[str, Any]],
    scopes: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    evidence: Mapping[str, Any],
    fields: Mapping[str, Any],
    sealed_at: str,
) -> dict[str, Any]:
    _assert_counts(scopes=scopes, selected=selected)
    opening_status = (
        _opening_status(scopes, len(selected))
        if owner == "opening"
        else combo_opening_status(scopes, selected_count=len(selected))
    )
    payload = normalize_json_value(
        {
            "schema_version": EXPERIENCE_OWNER_SCHEMAS[owner],
            "run_id": run_id,
            "account": account,
            "market": market,
            "candidate_owner": owner,
            "account_config_sha256": account_config_sha256,
            "strategy_policy_sha256": strategy_policy_sha256,
            "dependencies": dependencies,
            "sealed_at_utc": sealed_at,
            "opening_status": opening_status,
            "scope_results": scopes,
            "ranked_candidates" if owner == "opening" else "ranked_pairs": selected,
            **dict(evidence),
            **dict(fields),
        },
        field=f"{owner} experience snapshot",
    )
    payload["content_sha256"] = canonical_sha256(payload)
    _validate_owner(payload, owner=owner)
    try:
        write_account_run_state_bytes_once_safely(
            base=base,
            run_id=run_id,
            account=account,
            name=EXPERIENCE_OWNER_FILES[owner],
            payload=_json_bytes(payload),
        )
    except AccountRunConfigError as exc:
        raise ExperienceCandidateSnapshotError(
            f"experience candidate snapshot cannot be published: {owner}"
        ) from exc
    return payload


def seal_experience_candidate_bundle(
    *,
    base: Path,
    run_id: str,
    account: str,
    market: str,
    account_config_sha256: str,
    strategy_policy_sha256: str,
    dependencies: Iterable[Mapping[str, Any]],
    status_index: Mapping[str, Any],
    statuses_by_owner: Mapping[str, list[dict[str, Any]]],
    opening_candidates: Mapping[str, list[dict[str, Any]]],
    opening_decisions: Mapping[str, list[dict[str, Any]]],
    combo_evidence_by_owner: Mapping[str, list[dict[str, Any]]],
    account_display_name: str,
    sealed_at: datetime | str | None = None,
) -> dict[str, Any]:
    run_id_norm = required_text(run_id, "run_id")
    account_norm = required_text(account, "account").lower()
    market_norm = required_text(market, "market").upper()
    config_hash = sha256_text(account_config_sha256, "account_config_sha256")
    policy_hash = sha256_text(strategy_policy_sha256, "strategy_policy_sha256")
    dependency_rows = normalize_dependencies(
        dependencies,
        verify_root=Path(base).resolve(),
        required_kinds=EXPERIENCE_CANDIDATE_DEPENDENCIES,
    )
    fields = experience_fields(account_display_name)
    seal_time = utc_timestamp(sealed_at or datetime.now(timezone.utc))
    owners = sorted(
        {
            str(item.get("candidate_owner") or "").strip().lower()
            for item in status_index.get("items") or []
        }
        & set(EXPERIENCE_OWNER_SCHEMAS)
    )
    entries: list[dict[str, Any]] = []
    for owner in owners:
        scopes = _scope_rows(statuses_by_owner.get(owner) or [], owner=owner)
        if owner == "opening":
            selected = [
                {**row, "strategy_mode": mode, "rank": rank}
                for mode, rows in opening_candidates.items()
                for rank, row in enumerate(rows, start=1)
            ]
            evidence: dict[str, Any] = {
                "candidate_decisions": _tag_rows(
                    (
                        {**row, "strategy_mode": mode}
                        for mode, rows in opening_decisions.items()
                        for row in rows
                    ),
                    fields=fields,
                )
            }
        else:
            owner_evidence = list(combo_evidence_by_owner.get(owner) or [])
            selected = [
                row
                for evidence_row in owner_evidence
                for row in evidence_row.get("ranked_pairs") or []
            ]
            evidence = {}
            if owner == "sp_lc":
                for key in (
                    "funding_put_decisions",
                    "pair_evaluations",
                    "rank_records",
                ):
                    evidence[key] = _tag_rows(
                        (
                            row
                            for evidence_row in owner_evidence
                            for row in evidence_row.get(key) or []
                        ),
                        fields=fields,
                    )
        selected_tagged = _tag_rows(selected, fields=fields)
        snapshot = _seal_owner(
            base=Path(base),
            run_id=run_id_norm,
            account=account_norm,
            market=market_norm,
            owner=owner,
            account_config_sha256=config_hash,
            strategy_policy_sha256=policy_hash,
            dependencies=dependency_rows,
            scopes=scopes,
            selected=selected_tagged,
            evidence=evidence,
            fields=fields,
            sealed_at=seal_time,
        )
        path = _state_dir(base, run_id_norm, account_norm) / EXPERIENCE_OWNER_FILES[owner]
        entries.append(
            {
                "candidate_owner": owner,
                "schema_version": snapshot["schema_version"],
                "relpath": f"state/{path.name}",
                "sha256": sha256_bytes(path.read_bytes()),
                "content_sha256": snapshot["content_sha256"],
                "opening_status": snapshot["opening_status"],
                **fields,
            }
        )
    state_dir = _state_dir(base, run_id_norm, account_norm)
    index_path = state_dir.parent / STRATEGY_SCAN_STATUS_INDEX_V3_FILE
    manifest: dict[str, Any] = {
        "schema_version": EXPERIENCE_CANDIDATE_MANIFEST_SCHEMA,
        "run_id": run_id_norm,
        "account": account_norm,
        "markets": sorted(
            {
                str(item.get("market") or "").strip().upper()
                for item in status_index.get("items") or []
                if str(item.get("market") or "").strip()
            }
        ),
        "account_config_sha256": config_hash,
        "strategy_policy_sha256": policy_hash,
        "sealed_at_utc": seal_time,
        "expected_owners": owners,
        "status_index": {
            "schema_version": STRATEGY_SCAN_STATUS_INDEX_V3_SCHEMA,
            "relpath": STRATEGY_SCAN_STATUS_INDEX_V3_FILE,
            "sha256": sha256_bytes(index_path.read_bytes()),
            "content_sha256": status_index["content_sha256"],
        },
        "owner_snapshots": entries,
        **fields,
    }
    manifest["content_sha256"] = canonical_sha256(manifest)
    _validate_manifest(manifest)
    try:
        write_account_run_state_bytes_once_safely(
            base=Path(base),
            run_id=run_id_norm,
            account=account_norm,
            name=EXPERIENCE_CANDIDATE_MANIFEST_FILE,
            payload=_json_bytes(manifest),
        )
    except AccountRunConfigError as exc:
        raise ExperienceCandidateSnapshotError(
            "experience candidate manifest cannot be published"
        ) from exc
    adopted = load_experience_candidate_snapshot_bundle(
        base=Path(base), run_id=run_id_norm, account=account_norm
    )["manifest"]
    if adopted != manifest:
        raise ExperienceCandidateSnapshotError("experience manifest adoption mismatch")
    return adopted


def _assert_no_authority_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        forbidden = _FORBIDDEN_AUTHORITY_KEYS & set(value)
        if forbidden:
            raise ExperienceCandidateSnapshotError(
                "experience snapshot contains account authority fields"
            )
        for child in value.values():
            _assert_no_authority_keys(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_authority_keys(child)


def _validate_owner(payload: Mapping[str, Any], *, owner: str) -> None:
    item = dict(payload)
    if item.get("schema_version") != EXPERIENCE_OWNER_SCHEMAS[owner]:
        raise ExperienceCandidateSnapshotError("experience owner schema mismatch")
    validate_experience_fields(item)
    _identity(item.get("run_id"), "run_id")
    account = _identity(item.get("account"), "account", lower=True)
    if item.get("account") != account or item.get("candidate_owner") != owner:
        raise ExperienceCandidateSnapshotError("experience owner identity is invalid")
    if item.get("market") not in {"US", "HK", "MULTI"}:
        raise ExperienceCandidateSnapshotError("experience owner market is invalid")
    sha256_text(item.get("account_config_sha256"), "account_config_sha256")
    sha256_text(item.get("strategy_policy_sha256"), "strategy_policy_sha256")
    utc_timestamp(item.get("sealed_at_utc"))
    content_hash = sha256_text(item.get("content_sha256"), "content_sha256")
    content = {key: value for key, value in item.items() if key != "content_sha256"}
    if canonical_sha256(content) != content_hash:
        raise ExperienceCandidateSnapshotError("experience owner content hash mismatch")
    normalize_dependencies(
        item.get("dependencies") or [],
        required_kinds=EXPERIENCE_CANDIDATE_DEPENDENCIES,
    )
    selected_key = "ranked_candidates" if owner == "opening" else "ranked_pairs"
    selected = item.get(selected_key)
    scopes = item.get("scope_results")
    if (
        not isinstance(selected, list)
        or any(not isinstance(row, Mapping) for row in selected)
        or not isinstance(scopes, list)
        or any(not isinstance(row, Mapping) for row in scopes)
    ):
        raise ExperienceCandidateSnapshotError("experience owner rows are invalid")
    normalized_scopes = _scope_rows(scopes, owner=owner)
    if normalized_scopes != scopes:
        raise ExperienceCandidateSnapshotError("experience owner scopes are not canonical")
    _assert_counts(scopes=normalized_scopes, selected=selected)
    expected_status = (
        _opening_status(normalized_scopes, len(selected))
        if owner == "opening"
        else combo_opening_status(normalized_scopes, selected_count=len(selected))
    )
    if item.get("opening_status") != expected_status:
        raise ExperienceCandidateSnapshotError("experience owner status is invalid")
    for row in selected:
        validate_experience_fields(row)
    for key in (
        "candidate_decisions",
        "funding_put_decisions",
        "pair_evaluations",
        "rank_records",
    ):
        rows = item.get(key)
        if rows is None:
            continue
        if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
            raise ExperienceCandidateSnapshotError("experience owner evidence is invalid")
        for row in rows:
            validate_experience_fields(row)
    _assert_no_authority_keys(item)


def _validate_manifest(
    payload: Mapping[str, Any],
    *,
    expected_run_id: str | None = None,
    expected_account: str | None = None,
) -> None:
    item = dict(payload)
    if item.get("schema_version") != EXPERIENCE_CANDIDATE_MANIFEST_SCHEMA:
        raise ExperienceCandidateSnapshotError("experience manifest schema mismatch")
    validate_experience_fields(item)
    run_id = _identity(item.get("run_id"), "run_id")
    account = _identity(item.get("account"), "account", lower=True)
    if item.get("account") != account:
        raise ExperienceCandidateSnapshotError("experience manifest account is invalid")
    if expected_run_id is not None and run_id != expected_run_id:
        raise ExperienceCandidateSnapshotError("experience manifest run mismatch")
    if expected_account is not None and account != expected_account:
        raise ExperienceCandidateSnapshotError("experience manifest account mismatch")
    sha256_text(item.get("account_config_sha256"), "account_config_sha256")
    sha256_text(item.get("strategy_policy_sha256"), "strategy_policy_sha256")
    utc_timestamp(item.get("sealed_at_utc"))
    content_hash = sha256_text(item.get("content_sha256"), "content_sha256")
    content = {key: value for key, value in item.items() if key != "content_sha256"}
    if canonical_sha256(content) != content_hash:
        raise ExperienceCandidateSnapshotError("experience manifest content hash mismatch")
    owners = item.get("expected_owners")
    entries = item.get("owner_snapshots")
    markets = item.get("markets")
    if (
        not isinstance(markets, list)
        or markets != sorted(set(markets))
        or any(market not in {"US", "HK"} for market in markets)
        or not isinstance(owners, list)
        or owners != sorted(set(owners))
        or any(owner not in EXPERIENCE_OWNER_SCHEMAS for owner in owners or [])
        or not isinstance(entries, list)
        or any(not isinstance(entry, Mapping) for entry in entries)
    ):
        raise ExperienceCandidateSnapshotError("experience manifest owners are invalid")
    if [entry.get("candidate_owner") for entry in entries] != owners:
        raise ExperienceCandidateSnapshotError("experience manifest owner entries mismatch")
    for entry in entries:
        owner = str(entry.get("candidate_owner") or "")
        if owner not in EXPERIENCE_OWNER_SCHEMAS:
            raise ExperienceCandidateSnapshotError("experience manifest owner is invalid")
        if entry.get("schema_version") != EXPERIENCE_OWNER_SCHEMAS[owner]:
            raise ExperienceCandidateSnapshotError("experience manifest owner schema mismatch")
        if entry.get("relpath") != f"state/{EXPERIENCE_OWNER_FILES[owner]}":
            raise ExperienceCandidateSnapshotError("experience manifest owner path mismatch")
        sha256_text(entry.get("sha256"), f"{owner} file hash")
        sha256_text(entry.get("content_sha256"), f"{owner} content hash")
        required_text(entry.get("opening_status"), f"{owner} opening status")
        validate_experience_fields(entry)
        if _experience_projection(entry) != _experience_projection(item):
            raise ExperienceCandidateSnapshotError("experience owner fields mismatch")
    status_index = item.get("status_index")
    if not isinstance(status_index, Mapping):
        raise ExperienceCandidateSnapshotError("experience status index is invalid")
    if (
        status_index.get("schema_version") != STRATEGY_SCAN_STATUS_INDEX_V3_SCHEMA
        or status_index.get("relpath") != STRATEGY_SCAN_STATUS_INDEX_V3_FILE
    ):
        raise ExperienceCandidateSnapshotError("experience status index binding is invalid")
    sha256_text(status_index.get("sha256"), "status index file hash")
    sha256_text(status_index.get("content_sha256"), "status index content hash")
    _assert_no_authority_keys(item)


def load_experience_candidate_snapshot_bundle(
    *, base: Path, run_id: str, account: str
) -> dict[str, Any]:
    run_id_norm = _identity(run_id, "run_id")
    account_norm = _identity(account, "account", lower=True)
    try:
        encoded = read_account_run_state_bytes_safely(
            base=Path(base),
            run_id=run_id_norm,
            account=account_norm,
            name=EXPERIENCE_CANDIDATE_MANIFEST_FILE,
        )
        manifest = json.loads(encoded.decode("utf-8"))
    except Exception as exc:
        raise ExperienceCandidateSnapshotError(
            "experience candidate manifest is unavailable"
        ) from exc
    if not isinstance(manifest, dict):
        raise ExperienceCandidateSnapshotError("experience manifest is invalid")
    try:
        _validate_manifest(
            manifest,
            expected_run_id=run_id_norm,
            expected_account=account_norm,
        )
    except ExperienceCandidateSnapshotError:
        raise
    except Exception as exc:
        raise ExperienceCandidateSnapshotError(
            "experience manifest is invalid"
        ) from exc
    state_dir = _state_dir(base, run_id_norm, account_norm)
    index_path = state_dir.parent / STRATEGY_SCAN_STATUS_INDEX_V3_FILE
    if not index_path.is_file() or index_path.is_symlink():
        raise ExperienceCandidateSnapshotError("experience status index is unavailable")
    try:
        index_bytes = index_path.read_bytes()
    except OSError as exc:
        raise ExperienceCandidateSnapshotError(
            "experience status index is unavailable"
        ) from exc
    index_ref = manifest["status_index"]
    if sha256_bytes(index_bytes) != index_ref["sha256"]:
        raise ExperienceCandidateSnapshotError("experience status index hash mismatch")
    try:
        index = json.loads(index_bytes.decode("utf-8"))
        validate_strategy_scan_status_index_v3(
            index,
            expected_run_id=run_id_norm,
            expected_account=account_norm,
            expected_account_config_sha256=str(manifest["account_config_sha256"]),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, StrategyScanStatusError) as exc:
        raise ExperienceCandidateSnapshotError(
            "experience status index is invalid"
        ) from exc
    if _experience_projection(index) != _experience_projection(manifest):
        raise ExperienceCandidateSnapshotError("experience status fields mismatch")
    if index.get("content_sha256") != index_ref.get("content_sha256"):
        raise ExperienceCandidateSnapshotError("experience status content mismatch")
    index_owners = sorted(
        {
            str(item.get("candidate_owner") or "").strip().lower()
            for item in index.get("items") or []
        }
    )
    if index_owners != manifest["expected_owners"]:
        raise ExperienceCandidateSnapshotError("experience status owners mismatch")
    index_markets = sorted(
        {
            str(item.get("market") or "").strip().upper()
            for item in index.get("items") or []
            if str(item.get("market") or "").strip()
        }
    )
    if index_markets != manifest["markets"]:
        raise ExperienceCandidateSnapshotError("experience status markets mismatch")
    owners: dict[str, dict[str, Any]] = {}
    for entry in manifest["owner_snapshots"]:
        owner = str(entry["candidate_owner"])
        path = state_dir / EXPERIENCE_OWNER_FILES[owner]
        if not path.is_file() or path.is_symlink():
            raise ExperienceCandidateSnapshotError(
                "experience owner snapshot is unavailable"
            )
        try:
            encoded_owner = path.read_bytes()
        except OSError as exc:
            raise ExperienceCandidateSnapshotError(
                "experience owner snapshot is unavailable"
            ) from exc
        if sha256_bytes(encoded_owner) != entry["sha256"]:
            raise ExperienceCandidateSnapshotError("experience owner file hash mismatch")
        try:
            snapshot = json.loads(encoded_owner.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExperienceCandidateSnapshotError(
                "experience owner snapshot is invalid"
            ) from exc
        if not isinstance(snapshot, dict):
            raise ExperienceCandidateSnapshotError("experience owner snapshot is invalid")
        try:
            _validate_owner(snapshot, owner=owner)
        except ExperienceCandidateSnapshotError:
            raise
        except Exception as exc:
            raise ExperienceCandidateSnapshotError(
                "experience owner snapshot is invalid"
            ) from exc
        if (
            snapshot.get("run_id") != run_id_norm
            or snapshot.get("account") != account_norm
            or snapshot.get("candidate_owner") != owner
            or snapshot.get("account_config_sha256")
            != manifest.get("account_config_sha256")
            or snapshot.get("strategy_policy_sha256")
            != manifest.get("strategy_policy_sha256")
            or snapshot.get("sealed_at_utc") != manifest.get("sealed_at_utc")
            or _experience_projection(snapshot) != _experience_projection(manifest)
        ):
            raise ExperienceCandidateSnapshotError("experience owner identity mismatch")
        if snapshot["content_sha256"] != entry["content_sha256"]:
            raise ExperienceCandidateSnapshotError("experience owner binding mismatch")
        if snapshot.get("opening_status") != entry.get("opening_status"):
            raise ExperienceCandidateSnapshotError("experience owner status mismatch")
        owner_markets = {
            str(item.get("market") or "").strip().upper()
            for item in index.get("items") or []
            if str(item.get("candidate_owner") or "").strip().lower() == owner
        }
        expected_market = next(iter(owner_markets)) if len(owner_markets) == 1 else "MULTI"
        if snapshot.get("market") != expected_market:
            raise ExperienceCandidateSnapshotError("experience owner market mismatch")
        expected_scopes = sorted(
            (
                str(item.get("symbol") or "").strip().upper(),
                str(item.get("strategy_mode") or "").strip().lower(),
                str(item.get("status") or "").strip().lower(),
                str(item.get("reason") or item.get("reason_code") or "").strip(),
                item.get("candidate_count"),
            )
            for item in index.get("items") or []
            if str(item.get("candidate_owner") or "").strip().lower() == owner
        )
        actual_scopes = sorted(
            (
                str(item.get("symbol") or "").strip().upper(),
                str(item.get("strategy_mode") or "").strip().lower(),
                str(item.get("status") or "").strip().lower(),
                str(item.get("reason_code") or "").strip(),
                item.get("candidate_count"),
            )
            for item in snapshot.get("scope_results") or []
            if isinstance(item, Mapping)
        )
        if actual_scopes != expected_scopes:
            raise ExperienceCandidateSnapshotError("experience owner scope mismatch")
        owners[owner] = snapshot
    for owner, filename in EXPERIENCE_OWNER_FILES.items():
        path = state_dir / filename
        if owner not in owners and (path.exists() or path.is_symlink()):
            raise ExperienceCandidateSnapshotError(
                "experience manifest has an unbound owner snapshot"
            )
    return {"manifest": manifest, "status_index": index, "owners": owners}


__all__ = [
    "EXPERIENCE_CANDIDATE_DEPENDENCIES",
    "EXPERIENCE_CANDIDATE_MANIFEST_FILE",
    "EXPERIENCE_CANDIDATE_MANIFEST_SCHEMA",
    "ExperienceCandidateSnapshotError",
    "load_experience_candidate_snapshot_bundle",
    "seal_experience_candidate_bundle",
]
