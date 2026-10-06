from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.candidate_snapshot_contract import (
    CandidateSnapshotContractError,
    assert_current_candidate_artifact_boundary,
    current_candidate_owner_schema,
    required_text,
    sha256_text,
    utc_timestamp,
    validate_candidate_run_mode,
)
from src.application.cc_lp_candidate_snapshot import (
    CC_LP_CANDIDATE_SNAPSHOT_FILE,
    CC_LP_CANDIDATE_SNAPSHOT_SCHEMA,
    CcLpCandidateSnapshotError,
    load_cc_lp_candidate_snapshot,
    validate_cc_lp_candidate_snapshot,
)
from src.application.combo_yield_candidate_snapshot import (
    COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE,
    COMBO_YIELD_CANDIDATE_SNAPSHOT_SCHEMA,
    ComboYieldCandidateSnapshotError,
    load_combo_yield_candidate_snapshot,
    validate_combo_yield_candidate_snapshot,
)
from src.application.opening_candidate_snapshot import (
    OPENING_CANDIDATE_SNAPSHOT_FILE,
    OPENING_CANDIDATE_SNAPSHOT_SCHEMA,
    OpeningCandidateSnapshotError,
    load_opening_candidate_snapshot,
    validate_opening_candidate_snapshot,
)
from src.application.wheel.candidate_snapshot import (
    WHEEL_CANDIDATE_SNAPSHOT_FILE,
    WHEEL_CANDIDATE_SNAPSHOT_FILE_V1,
    WHEEL_CANDIDATE_SNAPSHOT_FILE_V2,
    WHEEL_CANDIDATE_SNAPSHOT_SCHEMA,
    WheelCandidateSnapshotError,
    load_wheel_candidate_snapshot,
    validate_wheel_candidate_snapshot,
)
from src.application.source_receipts import sha256_bytes
from src.application.futu_quote_routing import runtime_config_market
from src.application.strategy_scan_status import (
    STRATEGY_SCAN_STATUS_INDEX_V2_FILE,
    STRATEGY_SCAN_STATUS_INDEX_V3_FILE,
    STRATEGY_SCAN_STATUS_INDEX_V4_FILE,
    STRATEGY_SCAN_STATUS_INDEX_V5_FILE,
    STRATEGY_SCAN_STATUS_INDEX_V5_SCHEMA,
    StrategyScanStatusError,
    load_strategy_scan_status_index,
    validate_strategy_scan_status_index,
)
from src.application.tick_run_workspace import (
    AccountRunConfigError,
    account_run_config_path,
    load_published_account_run_config,
    read_account_run_state_bytes_safely,
    write_account_run_state_bytes_once_safely,
)
from src.application.payload_helpers import readable_json_bytes as _canonical_json_bytes


CANDIDATE_SNAPSHOT_MANIFEST_V1_SCHEMA = "candidate_snapshot_manifest.v1"
CANDIDATE_SNAPSHOT_MANIFEST_V1_FILE = "candidate_snapshot_manifest.v1.json"
CANDIDATE_SNAPSHOT_MANIFEST_V3_SCHEMA = "candidate_snapshot_manifest.v3"
CANDIDATE_SNAPSHOT_MANIFEST_V3_FILE = "candidate_snapshot_manifest.v3.json"
CANDIDATE_SNAPSHOT_MANIFEST_V4_SCHEMA = "candidate_snapshot_manifest.v4"
CANDIDATE_SNAPSHOT_MANIFEST_V4_FILE = "candidate_snapshot_manifest.v4.json"
CANDIDATE_SNAPSHOT_MANIFEST_SCHEMA = CANDIDATE_SNAPSHOT_MANIFEST_V4_SCHEMA
CANDIDATE_SNAPSHOT_MANIFEST_FILE = CANDIDATE_SNAPSHOT_MANIFEST_V4_FILE
_BASE_OWNER_FILES = {
    "opening": OPENING_CANDIDATE_SNAPSHOT_FILE,
    "sp_lc": COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE,
    "cc_lp": CC_LP_CANDIDATE_SNAPSHOT_FILE,
}
_OWNER_FILES_V1 = {**_BASE_OWNER_FILES, "wheel": WHEEL_CANDIDATE_SNAPSHOT_FILE_V1}
_OWNER_FILES_V3 = {**_BASE_OWNER_FILES, "wheel": WHEEL_CANDIDATE_SNAPSHOT_FILE_V2}
_OWNER_FILES_CURRENT = {**_BASE_OWNER_FILES, "wheel": WHEEL_CANDIDATE_SNAPSHOT_FILE}
_OWNER_SCHEMAS_CURRENT = {
    owner: current_candidate_owner_schema(owner)
    for owner in _OWNER_FILES_CURRENT
}
_KNOWN_MANIFEST_FILES = (
    CANDIDATE_SNAPSHOT_MANIFEST_V1_FILE,
    "candidate_snapshot_manifest.v2.json",
    CANDIDATE_SNAPSHOT_MANIFEST_V3_FILE,
    CANDIDATE_SNAPSHOT_MANIFEST_V4_FILE,
)


class CandidateSnapshotManifestError(RuntimeError):
    """Raised when an account-run candidate commit cannot be trusted."""

    run_id: str | None = None
    account: str | None = None


def _run_account_dir(base: Path, run_id: str, account: str) -> Path:
    return (
        Path(base).resolve()
        / "output_runs"
        / run_id
        / "accounts"
        / account
    )


def _scope_projection(
    row: Mapping[str, Any],
    *,
    require_wheel_direction: bool = False,
    adapt_legacy_wheel: bool = False,
) -> dict[str, str]:
    projected = {
        "market": required_text(row.get("market"), "scope market").upper(),
        "symbol": required_text(row.get("symbol"), "scope symbol").upper(),
        "strategy_family": required_text(
            row.get("strategy_family"),
            "scope strategy_family",
        ).lower(),
        "strategy_mode": required_text(row.get("strategy_mode"), "scope strategy_mode").lower(),
        "candidate_owner": required_text(row.get("candidate_owner"), "scope candidate_owner").lower(),
    }
    direction = str(row.get("direction") or "").strip().lower()
    if projected["strategy_family"] == "wheel":
        if not direction and adapt_legacy_wheel:
            direction = "call"
        if direction:
            if direction not in {"call", "put"}:
                raise CandidateSnapshotManifestError("candidate Wheel direction is invalid")
            projected["direction"] = direction
        elif require_wheel_direction:
            raise CandidateSnapshotManifestError("candidate Wheel direction is missing")
    elif direction:
        raise CandidateSnapshotManifestError("non-Wheel candidate scope has direction")
    return projected


def _expected_scopes(
    index: Mapping[str, Any],
    *,
    require_wheel_direction: bool = False,
    adapt_legacy_wheel: bool = False,
) -> list[dict[str, str]]:
    return sorted(
        (
            _scope_projection(
                row,
                require_wheel_direction=require_wheel_direction,
                adapt_legacy_wheel=adapt_legacy_wheel,
            )
            for row in index.get("items") or []
        ),
        key=lambda row: (
            row["market"],
            row["symbol"],
            row["strategy_family"],
            row.get("direction", ""),
        ),
    )


def _snapshot_strategy_scopes(
    snapshot: Mapping[str, Any],
    *,
    owner: str,
    index_items: list[Mapping[str, Any]],
    require_wheel_direction: bool = False,
) -> list[dict[str, str]]:
    expected_items = [
        dict(row)
        for row in index_items
        if str(row.get("candidate_owner") or "").strip().lower() == owner
    ]
    expected = [
        _scope_projection(row, require_wheel_direction=require_wheel_direction)
        for row in expected_items
    ]
    expected_markets = {row["market"] for row in expected}
    snapshot_market = str(snapshot.get("market") or "").strip().upper()
    if (
        not snapshot_market
        or len(expected_markets) != 1
        or snapshot_market not in expected_markets
    ):
        raise CandidateSnapshotManifestError(
            f"candidate owner market mismatch: {owner}"
        )
    expected_by_key = {
        (
            row["symbol"],
            row["strategy_mode"],
            row.get("direction", ""),
        ): raw
        for row, raw in zip(expected, expected_items, strict=True)
    }
    snapshot_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for raw in snapshot.get("scope_results") or []:
        if not isinstance(raw, Mapping) or raw.get("scope") != "strategy":
            continue
        row = dict(raw)
        key = (
            str(row.get("symbol") or "").strip().upper(),
            str(row.get("strategy_mode") or "").strip().lower(),
            str(row.get("direction") or "").strip().lower(),
        )
        if key in snapshot_by_key:
            raise CandidateSnapshotManifestError(
                f"candidate owner scope is duplicated: {owner}"
            )
        snapshot_by_key[key] = row
    if set(snapshot_by_key) != set(expected_by_key):
        raise CandidateSnapshotManifestError(
            f"candidate owner scope mismatch: {owner}"
        )
    candidate_counts: dict[tuple[str, str, str], int] = {
        key: 0 for key in expected_by_key
    }
    if owner == "opening":
        selected_rows = snapshot.get("ranked_candidates") or []
    elif owner == "wheel":
        selected_rows = [
            {
                **candidate,
                "_scope_symbol": candidate.get("symbol") or batch.get("symbol"),
                "_scope_direction": candidate.get("direction")
                or batch.get("direction"),
            }
            for batch in snapshot.get("batches") or []
            if isinstance(batch, Mapping)
            for candidate in batch.get("raw_candidates") or []
            if isinstance(candidate, Mapping)
        ]
    else:
        selected_rows = snapshot.get("ranked_pairs") or []
    for raw in selected_rows:
        if not isinstance(raw, Mapping):
            raise CandidateSnapshotManifestError(
                f"candidate owner selected rows are invalid: {owner}"
            )
        if owner == "opening":
            facts = raw.get("facts")
            facts_map = facts if isinstance(facts, Mapping) else {}
            key = (
                str(facts_map.get("symbol") or raw.get("symbol") or "").strip().upper(),
                str(raw.get("strategy_mode") or "").strip().lower(),
                "",
            )
        elif owner == "wheel":
            key = (
                str(raw.get("_scope_symbol") or raw.get("symbol") or "")
                .strip()
                .upper(),
                "wheel",
                str(raw.get("_scope_direction") or raw.get("direction") or "")
                .strip()
                .lower(),
            )
        else:
            key = (
                str(raw.get("symbol") or "").strip().upper(),
                "combo_yield",
                "",
            )
        if key not in candidate_counts:
            raise CandidateSnapshotManifestError(
                f"candidate owner selected row escapes scope: {owner}"
            )
        candidate_counts[key] += 1
    for key, index_row in expected_by_key.items():
        snapshot_row = snapshot_by_key[key]
        index_status = str(index_row.get("status") or "").strip().lower()
        snapshot_status = str(snapshot_row.get("status") or "").strip().lower()
        if snapshot_status != index_status:
            raise CandidateSnapshotManifestError(
                f"candidate owner terminal status mismatch: {owner}"
            )
        index_reason = str(
            index_row.get("reason_code") or index_row.get("reason") or ""
        ).strip()
        snapshot_reason = str(snapshot_row.get("reason_code") or "").strip()
        if snapshot_reason != index_reason:
            raise CandidateSnapshotManifestError(
                f"candidate owner terminal reason mismatch: {owner}"
            )
        for index_field, snapshot_field in (
            ("snapshot_id", "quote_snapshot_id"),
            ("receipt_relpath", "quote_receipt_relpath"),
        ):
            index_value = str(index_row.get(index_field) or "").strip() or None
            snapshot_value = (
                str(snapshot_row.get(snapshot_field) or "").strip() or None
            )
            if snapshot_value != index_value:
                raise CandidateSnapshotManifestError(
                    f"candidate owner quote binding mismatch: {owner}"
                )
        if index_status == "completed":
            try:
                indexed_count = int(index_row["candidate_count"])
            except (KeyError, TypeError, ValueError) as exc:
                raise CandidateSnapshotManifestError(
                    f"candidate owner terminal count is invalid: {owner}"
                ) from exc
            if indexed_count != candidate_counts[key]:
                raise CandidateSnapshotManifestError(
                    f"candidate owner terminal count mismatch: {owner}"
                )
        elif candidate_counts[key] != 0:
            raise CandidateSnapshotManifestError(
                f"candidate owner non-completed scope contains selected rows: {owner}"
            )
    return expected


def _load_owner_snapshot(
    *,
    base: Path,
    run_id: str,
    account: str,
    owner: str,
) -> dict[str, Any]:
    try:
        if owner == "opening":
            return load_opening_candidate_snapshot(
                base=base,
                run_id=run_id,
                account=account,
                require_current_contract=True,
            )
        if owner == "sp_lc":
            return load_combo_yield_candidate_snapshot(base=base, run_id=run_id, account=account)
        if owner == "cc_lp":
            return load_cc_lp_candidate_snapshot(base=base, run_id=run_id, account=account)
        if owner == "wheel":
            return load_wheel_candidate_snapshot(base=base, run_id=run_id, account=account)
    except (
        OpeningCandidateSnapshotError,
        ComboYieldCandidateSnapshotError,
        CcLpCandidateSnapshotError,
        WheelCandidateSnapshotError,
    ) as exc:
        raise CandidateSnapshotManifestError(
            f"candidate owner snapshot is invalid: {owner}"
        ) from exc
    raise CandidateSnapshotManifestError(f"unknown candidate owner: {owner}")


def _load_status_index(
    path: Path,
    *,
    run_id: str,
    account: str,
    account_config_sha256: str | None = None,
) -> dict[str, Any]:
    try:
        if path.name != STRATEGY_SCAN_STATUS_INDEX_V5_FILE:
            raise CandidateSnapshotManifestError("candidate status index version is unsupported")
        return load_strategy_scan_status_index(
            path,
            expected_run_id=run_id,
            expected_account=account,
            expected_account_config_sha256=account_config_sha256,
        )
    except StrategyScanStatusError as exc:
        raise CandidateSnapshotManifestError(
            "candidate status index is invalid"
        ) from exc


def _assert_exact_owner_files(
    account_dir: Path,
    *,
    expected_owners: list[str],
    owner_files: Mapping[str, str],
) -> None:
    expected = set(expected_owners)
    unexpected = sorted(
        owner
        for owner, filename in owner_files.items()
        if owner not in expected and (account_dir / "state" / filename).exists()
    )
    if unexpected:
        raise CandidateSnapshotManifestError(
            "candidate owner snapshot is unexpected: " + ",".join(unexpected)
        )
    expected_wheel = owner_files["wheel"]
    conflicting_wheel = (
        WHEEL_CANDIDATE_SNAPSHOT_FILE_V2
        if expected_wheel == WHEEL_CANDIDATE_SNAPSHOT_FILE_V1
        else WHEEL_CANDIDATE_SNAPSHOT_FILE_V1
    )
    if (account_dir / "state" / conflicting_wheel).exists():
        raise CandidateSnapshotManifestError("artifact_version_mismatch")


def _assert_status_version_files(account_dir: Path, *, manifest_v3: bool) -> None:
    conflicting_pattern = (
        "*_wheel_scan_status.json"
        if manifest_v3
        else "*_wheel_*_scan_status.v2.json"
    )
    if any(account_dir.glob(conflicting_pattern)):
        raise CandidateSnapshotManifestError("artifact_version_mismatch")


def publish_candidate_snapshot_manifest(
    *,
    base: Path,
    run_id: str,
    account: str,
    strategy_policy_sha256: str,
    sealed_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Publish the single current candidate manifest after exact validation."""

    try:
        run_id_norm = required_text(run_id, "run_id")
        account_norm = required_text(account, "account").lower()
        policy_hash = sha256_text(strategy_policy_sha256, "strategy_policy_sha256")
        seal_time = utc_timestamp(sealed_at or datetime.now(timezone.utc))
    except CandidateSnapshotContractError as exc:
        raise CandidateSnapshotManifestError(str(exc)) from exc
    account_dir = _run_account_dir(base, run_id_norm, account_norm)
    state_dir = account_dir / "state"
    try:
        assert_current_candidate_artifact_boundary(
            account_dir=account_dir,
            target=state_dir / CANDIDATE_SNAPSHOT_MANIFEST_V4_FILE,
        )
    except CandidateSnapshotContractError as exc:
        raise CandidateSnapshotManifestError("artifact_version_mismatch") from exc
    index_path = account_dir / STRATEGY_SCAN_STATUS_INDEX_V5_FILE
    index = _load_status_index(
        index_path,
        run_id=run_id_norm,
        account=account_norm,
    )
    try:
        mode_fields = validate_candidate_run_mode(index)
    except CandidateSnapshotContractError as exc:
        raise CandidateSnapshotManifestError(str(exc)) from exc
    config_hash = str(index["account_config_sha256"])
    scopes = _expected_scopes(index, require_wheel_direction=True)
    expected_owners = sorted({row["candidate_owner"] for row in scopes})
    _assert_exact_owner_files(
        account_dir,
        expected_owners=expected_owners,
        owner_files=_OWNER_FILES_CURRENT,
    )
    owner_entries: list[dict[str, Any]] = []
    for owner in expected_owners:
        snapshot = _load_owner_snapshot(
            base=Path(base),
            run_id=run_id_norm,
            account=account_norm,
            owner=owner,
        )
        if snapshot.get("schema_version") != _OWNER_SCHEMAS_CURRENT[owner]:
            raise CandidateSnapshotManifestError(
                f"candidate owner snapshot schema mismatch: {owner}"
            )
        try:
            snapshot_mode = validate_candidate_run_mode(snapshot)
        except CandidateSnapshotContractError as exc:
            raise CandidateSnapshotManifestError(str(exc)) from exc
        if snapshot_mode != mode_fields:
            raise CandidateSnapshotManifestError(
                f"candidate owner run mode mismatch: {owner}"
            )
        if snapshot.get("account_config_sha256") != config_hash:
            raise CandidateSnapshotManifestError(
                f"candidate owner config mismatch: {owner}"
            )
        if snapshot.get("strategy_policy_sha256") != policy_hash:
            raise CandidateSnapshotManifestError(
                f"candidate owner policy mismatch: {owner}"
            )
        covered_scopes = _snapshot_strategy_scopes(
            snapshot,
            owner=owner,
            index_items=list(index.get("items") or []),
            require_wheel_direction=True,
        )
        relpath = f"state/{_OWNER_FILES_CURRENT[owner]}"
        snapshot_path = account_dir / relpath
        if not snapshot_path.is_file() or snapshot_path.is_symlink():
            raise CandidateSnapshotManifestError(
                f"candidate owner snapshot is unavailable: {owner}"
            )
        owner_entries.append(
            {
                "candidate_owner": owner,
                "schema_version": snapshot["schema_version"],
                "relpath": relpath,
                "sha256": sha256_bytes(snapshot_path.read_bytes()),
                "content_sha256": snapshot["content_sha256"],
                "opening_status": snapshot["opening_status"],
                "covered_scopes": covered_scopes,
                **mode_fields,
            }
        )
    payload: dict[str, Any] = {
        "schema_version": CANDIDATE_SNAPSHOT_MANIFEST_V4_SCHEMA,
        "run_id": run_id_norm,
        "account": account_norm,
        "markets": sorted({row["market"] for row in scopes}),
        "account_config_sha256": config_hash,
        "strategy_policy_sha256": policy_hash,
        "sealed_at_utc": seal_time,
        "completion_reason": "complete" if scopes else "no_applicable_scope",
        "expected_scopes": scopes,
        "expected_owners": expected_owners,
        "status_index": {
            "schema_version": STRATEGY_SCAN_STATUS_INDEX_V5_SCHEMA,
            "relpath": STRATEGY_SCAN_STATUS_INDEX_V5_FILE,
            "sha256": sha256_bytes(index_path.read_bytes()),
            "content_sha256": index["content_sha256"],
        },
        "owner_snapshots": owner_entries,
        **mode_fields,
    }
    payload["content_sha256"] = canonical_sha256(payload)
    validate_candidate_snapshot_manifest(
        payload,
        expected_run_id=run_id_norm,
        expected_account=account_norm,
    )
    try:
        write_account_run_state_bytes_once_safely(
            base=Path(base),
            run_id=run_id_norm,
            account=account_norm,
            name=CANDIDATE_SNAPSHOT_MANIFEST_V4_FILE,
            payload=_canonical_json_bytes(payload),
        )
        adopted = json.loads(
            read_account_run_state_bytes_safely(
                base=Path(base),
                run_id=run_id_norm,
                account=account_norm,
                name=CANDIDATE_SNAPSHOT_MANIFEST_V4_FILE,
            ).decode("utf-8")
        )
    except Exception as exc:
        raise CandidateSnapshotManifestError(
            "terminal candidate snapshot manifest conflicts or cannot be published"
        ) from exc
    if adopted != payload:
        raise CandidateSnapshotManifestError("candidate snapshot manifest adoption mismatch")
    return payload


def validate_candidate_snapshot_manifest(
    payload: Mapping[str, Any],
    *,
    expected_run_id: str,
    expected_account: str,
) -> None:
    try:
        item = dict(payload or {})
        schema = item.get("schema_version")
        if schema != CANDIDATE_SNAPSHOT_MANIFEST_V4_SCHEMA:
            raise CandidateSnapshotManifestError("candidate snapshot manifest schema mismatch")
        owner_files = _OWNER_FILES_CURRENT
        owner_schemas = _OWNER_SCHEMAS_CURRENT
        if item.get("run_id") != expected_run_id:
            raise CandidateSnapshotManifestError("candidate snapshot manifest run mismatch")
        if item.get("account") != expected_account:
            raise CandidateSnapshotManifestError("candidate snapshot manifest account mismatch")
        mode_fields = validate_candidate_run_mode(item)
        sha256_text(item.get("account_config_sha256"), "account_config_sha256")
        sha256_text(item.get("strategy_policy_sha256"), "strategy_policy_sha256")
        content_hash = sha256_text(item.get("content_sha256"), "content_sha256")
        content = {key: value for key, value in item.items() if key != "content_sha256"}
        if canonical_sha256(content) != content_hash:
            raise CandidateSnapshotManifestError("candidate snapshot manifest content hash mismatch")
        utc_timestamp(item.get("sealed_at_utc"))
        scopes = item.get("expected_scopes")
        owners = item.get("expected_owners")
        entries = item.get("owner_snapshots")
        if not isinstance(scopes, list) or any(not isinstance(row, Mapping) for row in scopes):
            raise CandidateSnapshotManifestError("candidate manifest scopes are invalid")
        projected = [
            _scope_projection(row, require_wheel_direction=True)
            for row in scopes
        ]
        if projected != sorted(
            projected,
            key=lambda row: (
                row["market"],
                row["symbol"],
                row["strategy_family"],
                row.get("direction", ""),
            ),
        ):
            raise CandidateSnapshotManifestError("candidate manifest scopes are not canonical")
        projected_owners = sorted({row["candidate_owner"] for row in projected})
        scope_keys = {
            (
                row["market"],
                row["symbol"],
                row["strategy_family"],
                row["strategy_mode"],
                row["candidate_owner"],
                row.get("direction", ""),
            )
            for row in projected
        }
        if len(scope_keys) != len(projected):
            raise CandidateSnapshotManifestError("candidate manifest scopes are duplicated")
        markets = item.get("markets")
        if markets != sorted({row["market"] for row in projected}):
            raise CandidateSnapshotManifestError("candidate manifest market set mismatch")
        if owners != projected_owners:
            raise CandidateSnapshotManifestError("candidate manifest owner set mismatch")
        if not isinstance(entries, list) or any(not isinstance(row, Mapping) for row in entries):
            raise CandidateSnapshotManifestError("candidate manifest owner snapshots are invalid")
        if [row.get("candidate_owner") for row in entries] != projected_owners:
            raise CandidateSnapshotManifestError("candidate manifest owner entries mismatch")
        completion = str(item.get("completion_reason") or "")
        if completion != ("complete" if projected else "no_applicable_scope"):
            raise CandidateSnapshotManifestError("candidate manifest completion reason mismatch")
        index = item.get("status_index")
        if not isinstance(index, Mapping):
            raise CandidateSnapshotManifestError("candidate manifest status index is invalid")
        if index.get("schema_version") != STRATEGY_SCAN_STATUS_INDEX_V5_SCHEMA:
            raise CandidateSnapshotManifestError("candidate manifest status index schema mismatch")
        if index.get("relpath") != STRATEGY_SCAN_STATUS_INDEX_V5_FILE:
            raise CandidateSnapshotManifestError("candidate manifest status index path mismatch")
        sha256_text(index.get("sha256"), "status index sha256")
        sha256_text(index.get("content_sha256"), "status index content_sha256")
        for entry in entries:
            owner = str(entry.get("candidate_owner") or "")
            if owner not in owner_files:
                raise CandidateSnapshotManifestError("candidate manifest owner is invalid")
            if entry.get("schema_version") != owner_schemas[owner]:
                if owner == "wheel":
                    raise CandidateSnapshotManifestError("artifact_version_mismatch")
                raise CandidateSnapshotManifestError("candidate manifest owner schema mismatch")
            if entry.get("relpath") != f"state/{owner_files[owner]}":
                if owner == "wheel":
                    raise CandidateSnapshotManifestError("artifact_version_mismatch")
                raise CandidateSnapshotManifestError("candidate manifest owner path mismatch")
            sha256_text(entry.get("sha256"), f"{owner} snapshot sha256")
            sha256_text(entry.get("content_sha256"), f"{owner} snapshot content_sha256")
            required_text(entry.get("opening_status"), f"{owner} opening_status")
            if validate_candidate_run_mode(entry) != mode_fields:
                raise CandidateSnapshotManifestError(
                    "candidate manifest owner run mode mismatch"
                )
            covered = entry.get("covered_scopes")
            expected = [row for row in projected if row["candidate_owner"] == owner]
            if covered != expected:
                raise CandidateSnapshotManifestError("candidate manifest covered scopes mismatch")
    except CandidateSnapshotContractError as exc:
        raise CandidateSnapshotManifestError(str(exc)) from exc


def _validate_source_status_bytes(row: Mapping[str, Any], encoded: bytes) -> None:
    if sha256_bytes(encoded) != row["source_status_sha256"]:
        raise CandidateSnapshotManifestError("candidate source status hash mismatch")
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateSnapshotManifestError(
            "candidate source status is unreadable"
        ) from exc
    if not isinstance(payload, dict):
        raise CandidateSnapshotManifestError(
            "candidate source status is invalid"
        )
    content_hash = payload.get("content_sha256")
    content = {key: value for key, value in payload.items() if key != "content_sha256"}
    computed = sha256_bytes(
        json.dumps(
            content,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )
    if (
        content_hash != row.get("source_status_content_sha256")
        or computed != content_hash
    ):
        raise CandidateSnapshotManifestError(
            "candidate source status content binding mismatch"
        )

def _validate_owner_binding(snapshot: Mapping[str, Any], entry: Mapping[str, Any],
                            manifest: Mapping[str, Any], index: Mapping[str, Any], encoded: bytes) -> None:
    owner = str(entry["candidate_owner"])
    if sha256_bytes(encoded) != entry["sha256"]:
        raise CandidateSnapshotManifestError(f"candidate owner snapshot hash mismatch: {owner}")
    if snapshot.get("schema_version") != entry.get("schema_version"):
        if owner == "wheel":
            raise CandidateSnapshotManifestError("artifact_version_mismatch")
        raise CandidateSnapshotManifestError(
            f"candidate owner schema binding mismatch: {owner}"
        )
    if snapshot.get("content_sha256") != entry["content_sha256"]:
        raise CandidateSnapshotManifestError(
            f"candidate owner content binding mismatch: {owner}"
        )
    if snapshot.get("account_config_sha256") != manifest["account_config_sha256"]:
        raise CandidateSnapshotManifestError(
            f"candidate owner config binding mismatch: {owner}"
        )
    if snapshot.get("strategy_policy_sha256") != manifest["strategy_policy_sha256"]:
        raise CandidateSnapshotManifestError(
            f"candidate owner policy binding mismatch: {owner}"
        )
    try:
        if (
            validate_candidate_run_mode(snapshot)
            != validate_candidate_run_mode(entry)
            or validate_candidate_run_mode(snapshot)
            != validate_candidate_run_mode(manifest)
        ):
            raise CandidateSnapshotManifestError(
                f"candidate owner run mode binding mismatch: {owner}"
            )
    except CandidateSnapshotContractError as exc:
        raise CandidateSnapshotManifestError(str(exc)) from exc
    covered = _snapshot_strategy_scopes(
        snapshot,
        owner=owner,
        index_items=list(index.get("items") or []),
        require_wheel_direction=True,
    )
    if covered != entry["covered_scopes"]:
        raise CandidateSnapshotManifestError(
            f"candidate owner scope binding mismatch: {owner}"
        )
    if snapshot.get("opening_status") != entry.get("opening_status"):
        raise CandidateSnapshotManifestError(
            f"candidate owner status binding mismatch: {owner}"
        )

def _validate_index_binding(manifest: Mapping[str, Any], index: Mapping[str, Any], encoded: bytes,
                            *, run_id: str, account: str) -> None:
    binding = manifest["status_index"]
    validate_strategy_scan_status_index(
        index, expected_run_id=run_id, expected_account=account,
        expected_account_config_sha256=manifest["account_config_sha256"],
    )
    if sha256_bytes(encoded) != binding["sha256"]:
        raise CandidateSnapshotManifestError("candidate status index hash mismatch")
    if index["content_sha256"] != binding["content_sha256"]:
        raise CandidateSnapshotManifestError("candidate status index content binding mismatch")
    if _expected_scopes(index, require_wheel_direction=True) != manifest["expected_scopes"]:
        raise CandidateSnapshotManifestError("candidate status index scope binding mismatch")
    try:
        if validate_candidate_run_mode(index) != validate_candidate_run_mode(manifest):
            raise CandidateSnapshotManifestError(
                "candidate status index run mode binding mismatch"
            )
    except CandidateSnapshotContractError as exc:
        raise CandidateSnapshotManifestError(str(exc)) from exc

def _validate_bundle_inventory(manifest_name: str, manifest: Mapping[str, Any],
                               account_names: list[str], state_names: list[str]) -> None:
    expected_name = CANDIDATE_SNAPSHOT_MANIFEST_V4_FILE
    if manifest_name != expected_name or set(state_names).intersection(_KNOWN_MANIFEST_FILES) != {expected_name}:
        raise CandidateSnapshotManifestError("artifact_version_mismatch")
    owner_files = _OWNER_FILES_CURRENT
    expected_files = {owner_files[owner] for owner in manifest["expected_owners"]}
    all_files = (
        set(_OWNER_FILES_V1.values())
        | set(_OWNER_FILES_V3.values())
        | set(_OWNER_FILES_CURRENT.values())
    )
    if set(state_names).intersection(all_files) != expected_files:
        raise CandidateSnapshotManifestError("artifact_version_mismatch")
    from fnmatch import fnmatchcase
    conflict_patterns = (
        "*_scan_status.json",
        "*_scan_status.v2.json",
    )
    if any(
        fnmatchcase(name, pattern)
        for name in account_names
        for pattern in conflict_patterns
    ):
        raise CandidateSnapshotManifestError("artifact_version_mismatch")
    conflicting_indexes = {
        "strategy_scan_status_index.v1.json",
        STRATEGY_SCAN_STATUS_INDEX_V2_FILE,
        STRATEGY_SCAN_STATUS_INDEX_V3_FILE,
        STRATEGY_SCAN_STATUS_INDEX_V4_FILE,
    }
    if conflicting_indexes.intersection(account_names):
        raise CandidateSnapshotManifestError("artifact_version_mismatch")

def validate_candidate_snapshot_bundle_bytes(
    *, manifest_name: str, files: Mapping[str, bytes],
    account_names: list[str], state_names: list[str],
    run_id: str, account: str,
    dependencies: Mapping[str, bytes],
    check: Callable[[], None] = lambda: None,
) -> dict[str, Any]:
    """Validate exactly the supplied account-relative bytes without filesystem I/O."""
    def decoded(name: str) -> dict[str, Any]:
        check()
        try:
            value = json.loads(files[name].decode("utf-8"))
        except (KeyError, UnicodeDecodeError, ValueError) as exc:
            raise CandidateSnapshotManifestError("candidate bundle data unavailable") from exc
        if not isinstance(value, dict):
            raise CandidateSnapshotManifestError("candidate bundle data invalid")
        return value

    manifest = decoded("state/" + manifest_name)
    validate_candidate_snapshot_manifest(manifest, expected_run_id=run_id, expected_account=account)
    _validate_bundle_inventory(manifest_name, manifest, account_names, state_names)
    binding = manifest["status_index"]
    index = decoded(binding["relpath"])
    _validate_index_binding(manifest, index, files[binding["relpath"]], run_id=run_id, account=account)
    for row in index["items"]:
        check()
        _validate_source_status_bytes(row, files[row["source_status_path"]])
    owner_validators = {
        "opening": validate_opening_candidate_snapshot,
        "sp_lc": validate_combo_yield_candidate_snapshot,
        "cc_lp": validate_cc_lp_candidate_snapshot,
        "wheel": validate_wheel_candidate_snapshot,
    }
    owners = {}
    for entry in manifest["owner_snapshots"]:
        owner = entry["candidate_owner"]
        snapshot = decoded(entry["relpath"])
        kwargs = {"require_current_contract": True} if owner == "opening" else {}
        owner_validators[owner](snapshot, expected_run_id=run_id, expected_account=account, **kwargs)
        for dependency in snapshot.get("dependencies") or []:
            check()
            name = dependency.get("relpath")
            if name and (name not in dependencies or sha256_bytes(dependencies[name]) != dependency["sha256"]):
                raise CandidateSnapshotManifestError("candidate dependency binding mismatch")
        _validate_owner_binding(snapshot, entry, manifest, index, files[entry["relpath"]])
        owners[owner] = snapshot
    check()
    # Keep original JSON identity; legacy adaptation is for typed business reads.
    return {"manifest": manifest, "status_index": index, "owners": owners}


def load_candidate_snapshot_bundle(
    *,
    base: Path,
    run_id: str,
    account: str,
) -> dict[str, Any]:
    """Load the terminal manifest first, then only its exact bound owner set."""

    try:
        run_id_norm = required_text(run_id, "run_id")
        account_norm = required_text(account, "account").lower()
        state_dir = _run_account_dir(base, run_id_norm, account_norm) / "state"
        present_manifests = [
            filename
            for filename in _KNOWN_MANIFEST_FILES
            if (state_dir / filename).exists() or (state_dir / filename).is_symlink()
        ]
        if len(present_manifests) != 1:
            if present_manifests:
                raise CandidateSnapshotManifestError("artifact_version_mismatch")
            raise CandidateSnapshotManifestError("candidate snapshot manifest is unavailable")
        manifest_filename = present_manifests[0]
        encoded = read_account_run_state_bytes_safely(
            base=Path(base),
            run_id=run_id_norm,
            account=account_norm,
            name=manifest_filename,
        )
        manifest = json.loads(encoded.decode("utf-8"))
    except CandidateSnapshotManifestError:
        raise
    except Exception as exc:
        raise CandidateSnapshotManifestError("candidate snapshot manifest is unavailable") from exc
    if not isinstance(manifest, dict):
        raise CandidateSnapshotManifestError("candidate snapshot manifest must be an object")
    validate_candidate_snapshot_manifest(
        manifest,
        expected_run_id=run_id_norm,
        expected_account=account_norm,
    )
    expected_manifest_filename = CANDIDATE_SNAPSHOT_MANIFEST_V4_FILE
    if manifest_filename != expected_manifest_filename:
        raise CandidateSnapshotManifestError("artifact_version_mismatch")
    account_dir = _run_account_dir(base, run_id_norm, account_norm)
    _validate_bundle_inventory(
        manifest_filename, manifest,
        [
            item.name
            for item in account_dir.iterdir()
            if item.exists() or item.is_symlink()
        ],
        list(
            set(present_manifests)
            | {
                item.name
                for item in state_dir.iterdir()
                if item.exists() or item.is_symlink()
            }
        ),
    )
    index_binding = dict(manifest["status_index"])
    index_path = account_dir / str(index_binding["relpath"])
    if not index_path.is_file() or index_path.is_symlink():
        raise CandidateSnapshotManifestError("candidate status index is unavailable")
    index_encoded = index_path.read_bytes()
    index = _load_status_index(
        index_path,
        run_id=run_id_norm,
        account=account_norm,
        account_config_sha256=str(manifest["account_config_sha256"]),
    )
    _validate_index_binding(manifest, index, index_encoded, run_id=run_id_norm, account=account_norm)
    _validate_v4_source_status_bindings(account_dir, index)

    owners: dict[str, dict[str, Any]] = {}
    for raw_entry in manifest["owner_snapshots"]:
        entry = dict(raw_entry)
        owner = str(entry["candidate_owner"])
        snapshot_path = account_dir / str(entry["relpath"])
        if not snapshot_path.is_file() or snapshot_path.is_symlink():
            raise CandidateSnapshotManifestError(
                f"candidate owner snapshot is unavailable: {owner}"
            )
        snapshot_encoded = snapshot_path.read_bytes()
        snapshot = _load_owner_snapshot(
            base=Path(base),
            run_id=run_id_norm,
            account=account_norm,
            owner=owner,
        )
        _validate_owner_binding(snapshot, entry, manifest, index, snapshot_encoded)
        owners[owner] = snapshot
    if sorted(owners) != manifest["expected_owners"]:
        raise CandidateSnapshotManifestError("candidate owner bundle is incomplete")
    return {"manifest": manifest, "status_index": index, "owners": owners}


def _validate_v4_source_status_bindings(
    account_dir: Path,
    index: Mapping[str, Any],
) -> None:
    for raw in index.get("items") or []:
        row = dict(raw)
        source_path = account_dir / str(row["source_status_path"])
        if not source_path.is_file() or source_path.is_symlink():
            raise CandidateSnapshotManifestError("candidate source status is unavailable")
        encoded = source_path.read_bytes()
        _validate_source_status_bytes(row, encoded)


def _frozen_account_market(
    *, base: Path, run_id: str, account: str, config_hash: str,
) -> str:
    state_path = account_run_config_path(
        base=base, run_id=run_id, account=account,
    )
    config = load_published_account_run_config(
        base=base,
        run_id=run_id,
        account=account,
        state_path=state_path,
        account_config_sha256=config_hash,
    )
    market = runtime_config_market(config)
    if market not in {"US", "HK"}:
        raise CandidateSnapshotManifestError("frozen account market is unavailable")
    for metadata_key in ("_generated", "_resolved"):
        metadata = config.get(metadata_key)
        if isinstance(metadata, dict) and metadata.get("market") is not None:
            if str(metadata["market"]).strip().upper() != market:
                raise CandidateSnapshotManifestError("frozen account market conflicts")
    if config.get("market") is not None and str(config["market"]).strip().upper() != market:
        raise CandidateSnapshotManifestError("frozen account market conflicts")
    return market


def _scheduler_skipped_market(*, base: Path, run_id: str, account: str) -> str | None:
    account_dir = _run_account_dir(base, run_id, account)
    metrics_path = account_dir / "state" / "account_metrics.json"
    if not (metrics_path.exists() or metrics_path.is_symlink()):
        return None
    metrics = json.loads(read_account_run_state_bytes_safely(
        base=base, run_id=run_id, account=account, name="account_metrics.json",
    ))
    if not isinstance(metrics, dict):
        raise CandidateSnapshotManifestError("account metrics must be an object")
    if metrics.get("scan_outcome") != "scheduler_skipped":
        return None
    if (
        metrics.get("run_id") != run_id
        or metrics.get("account") != account
        or metrics.get("ran_scan") is not False
        or metrics.get("ran_pipeline") is not False
        or metrics.get("scan_mode") == "experience"
        or metrics.get("pipeline_started_at_utc") is not None
        or metrics.get("pipeline_ms") is not None
        or any(metrics.get(key) is not None for key in ("error", "error_code", "typed_reason"))
        or metrics.get("snapshot_status") not in (None, "complete", "partial")
    ):
        raise CandidateSnapshotManifestError("scheduler skip metrics conflict")
    # A terminal skip cannot coexist with evidence that its pipeline started.
    for directory in (account_dir, account_dir / "state"):
        for path in directory.iterdir():
            name = path.name
            if (
                "candidate" in name
                or "scan_status" in name
                or name == "symbols_notification.txt"
            ):
                raise CandidateSnapshotManifestError("scheduler skip has pipeline output")
    market = _frozen_account_market(
        base=base, run_id=run_id, account=account,
        config_hash=metrics.get("account_config_sha256"),
    )
    markets = metrics.get("markets_to_run")
    if not isinstance(markets, list) or markets != [market]:
        raise CandidateSnapshotManifestError("scheduler skip market binding mismatch")
    return market


def _load_latest_candidate_snapshot_bundle(
    *,
    base: Path,
    account: str,
    loader: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    root = Path(base).resolve()
    try:
        account_norm = required_text(account, "account").lower()
        account_run_config_path(base=root, run_id="identity-check", account=account_norm)
    except (CandidateSnapshotContractError, AccountRunConfigError) as exc:
        raise CandidateSnapshotManifestError("candidate account identity is invalid") from exc
    runs_root = root / "output_runs"
    if runs_root.is_symlink():
        raise CandidateSnapshotManifestError("output_runs may not be a symlink")
    pointer = root / "output_shared" / "state" / "last_run_dir.txt"
    if any(path.is_symlink() for path in (pointer, pointer.parent, pointer.parent.parent)):
        raise CandidateSnapshotManifestError("last-run pointer may not be a symlink")
    pointed = None
    if pointer.exists():
        if not pointer.is_file():
            raise CandidateSnapshotManifestError("last-run pointer is invalid")
        try:
            pointed = Path(pointer.read_text(encoding="utf-8").strip())
        except (OSError, UnicodeError) as exc:
            raise CandidateSnapshotManifestError("last-run pointer is unreadable") from exc
        if not pointed.is_absolute():
            pointed = root / pointed
        if pointed.is_symlink() or pointed.parent.resolve() != runs_root:
            raise CandidateSnapshotManifestError("last-run pointer is outside output_runs or unsafe")
        pointed = pointed.parent.resolve() / pointed.name

    skipped = 0
    skipped_market = None
    attempted_run_id = None

    def load_or_skip(run_dir: Path) -> dict[str, Any] | None:
        nonlocal skipped, skipped_market, attempted_run_id
        attempted_run_id = run_dir.name
        try:
            market = _scheduler_skipped_market(
                base=root, run_id=run_dir.name, account=account_norm,
            )
            if market is not None:
                if skipped_market is not None and market != skipped_market:
                    raise CandidateSnapshotManifestError("scheduler skip account market changed")
                skipped_market = market
                skipped += 1
                return None
            bundle = loader(base=root, run_id=run_dir.name, account=account_norm)
            if skipped:
                manifest = bundle["manifest"]
                market = _frozen_account_market(
                    base=root, run_id=run_dir.name, account=account_norm,
                    config_hash=manifest["account_config_sha256"],
                )
                if market != skipped_market or any(
                    scope.get("market") != market
                    for scope in manifest.get("expected_scopes", [])
                ):
                    raise CandidateSnapshotManifestError("candidate account market binding mismatch")
            return {**bundle, "source_selection": {"skipped_non_scan_runs": skipped}}
        except (CandidateSnapshotManifestError, AccountRunConfigError, OSError, ValueError) as exc:
            error = CandidateSnapshotManifestError(str(exc))
            error.run_id = run_dir.name
            error.account = account_norm
            raise error from exc

    if pointed is not None:
        bundle = load_or_skip(pointed)
        if bundle is not None:
            return bundle
    if not runs_root.is_dir():
        raise CandidateSnapshotManifestError("no output runs are available")
    try:
        candidates = sorted(
            (item for item in runs_root.iterdir() if item.is_dir() and not item.is_symlink()),
            key=lambda item: (item.stat().st_mtime_ns, item.name),
            reverse=True,
        )
        if pointed is not None:
            candidates = candidates[candidates.index(pointed) + 1:]
        for run_dir in candidates:
            account_dir = run_dir / "accounts" / account_norm
            if account_dir.is_symlink() or account_dir.parent.is_symlink():
                raise CandidateSnapshotManifestError("latest account run may not be a symlink")
            if not account_dir.is_dir():
                continue
            bundle = load_or_skip(run_dir)
            if bundle is not None:
                return bundle
    except (OSError, ValueError) as exc:
        raise CandidateSnapshotManifestError("output runs are unreadable") from exc
    error = CandidateSnapshotManifestError(
        f"no candidate run is available for account {account_norm}"
    )
    error.run_id = attempted_run_id
    error.account = account_norm
    raise error


def load_latest_candidate_snapshot_bundle(
    *,
    base: Path,
    account: str,
) -> dict[str, Any]:
    """Resolve one account's latest run and require its formal terminal manifest."""

    return _load_latest_candidate_snapshot_bundle(
        base=base,
        account=account,
        loader=load_candidate_snapshot_bundle,
    )




__all__ = [
    "CANDIDATE_SNAPSHOT_MANIFEST_FILE",
    "CANDIDATE_SNAPSHOT_MANIFEST_SCHEMA",
    "CANDIDATE_SNAPSHOT_MANIFEST_V1_FILE",
    "CANDIDATE_SNAPSHOT_MANIFEST_V1_SCHEMA",
    "CANDIDATE_SNAPSHOT_MANIFEST_V3_FILE",
    "CANDIDATE_SNAPSHOT_MANIFEST_V3_SCHEMA",
    "CANDIDATE_SNAPSHOT_MANIFEST_V4_FILE",
    "CANDIDATE_SNAPSHOT_MANIFEST_V4_SCHEMA",
    "CandidateSnapshotManifestError",
    "load_candidate_snapshot_bundle",
    "load_latest_candidate_snapshot_bundle",
    "publish_candidate_snapshot_manifest",
    "validate_candidate_snapshot_manifest",
]
