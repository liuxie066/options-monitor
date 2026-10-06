from __future__ import annotations

"""Classify and load historical candidate evidence without parsing legacy CSVs."""

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.candidate_snapshot_manifest import (
    CANDIDATE_SNAPSHOT_MANIFEST_FILE,
    CANDIDATE_SNAPSHOT_MANIFEST_V1_FILE,
    CANDIDATE_SNAPSHOT_MANIFEST_V1_SCHEMA,
    CANDIDATE_SNAPSHOT_MANIFEST_V3_FILE,
    CANDIDATE_SNAPSHOT_MANIFEST_V3_SCHEMA,
    CandidateSnapshotManifestError,
    _expected_scopes,
    _load_latest_candidate_snapshot_bundle,
    _snapshot_strategy_scopes,
    load_candidate_snapshot_bundle,
)
from src.application.experience_candidate_snapshot import (
    EXPERIENCE_CANDIDATE_MANIFEST_FILE,
    EXPERIENCE_OWNER_FILES,
    _experience_projection,
    _validate_manifest as _validate_experience_manifest,
    _validate_owner as _validate_experience_owner,
    ExperienceCandidateSnapshotError,
    load_experience_candidate_snapshot_bundle,
)
from src.application.cc_lp_candidate_snapshot import (
    CC_LP_CANDIDATE_SNAPSHOT_FILE,
    CC_LP_CANDIDATE_SNAPSHOT_SCHEMA,
)
from src.application.combo_yield_candidate_snapshot import (
    COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE,
    COMBO_YIELD_CANDIDATE_SNAPSHOT_SCHEMA,
)
from src.application.config_sections import resolve_watchlist_config
from src.application.opening_candidate_snapshot import (
    OPENING_CANDIDATE_SNAPSHOT_FILE,
    OPENING_CANDIDATE_SNAPSHOT_SCHEMA,
    OpeningCandidateSnapshotError,
    load_opening_candidate_snapshot,
)
from src.application.strategy_scan_status import (
    STRATEGY_SCAN_STATUS_INDEX_V2_FILE,
    STRATEGY_SCAN_STATUS_INDEX_V2_SCHEMA,
    STRATEGY_SCAN_STATUS_INDEX_V3_FILE,
    STRATEGY_SCAN_STATUS_INDEX_V3_SCHEMA,
    STRATEGY_SCAN_STATUS_INDEX_V4_FILE,
    STRATEGY_SCAN_STATUS_INDEX_V4_SCHEMA,
    StrategyScanStatusError,
    validate_strategy_scan_status_index_v2,
    validate_strategy_scan_status_index_v3,
    validate_strategy_scan_status_index_v4,
)
from src.application.source_receipts import sha256_bytes
from src.application.tick_run_workspace import (
    ACCOUNT_RUN_CONFIG_NAME,
    AccountRunConfigError,
    account_run_config_path,
    canonical_account_run_config_bytes,
    load_published_account_run_config,
    read_account_run_state_bytes_safely,
)
from src.application.combo_yield_config import resolve_combo_yield_cfg


CANDIDATE_EVIDENCE_CLASSIFICATION_SCHEMA = "candidate_evidence_compatibility.v1"
SUPPORTED = "supported"
SUPPORTED_LIMITED_LEGACY_SNAPSHOT = "supported_limited_legacy_snapshot"
UNSUPPORTED_LEGACY_CSV_ONLY = "unsupported_legacy_csv_only"
UNSUPPORTED_SNAPSHOT_MISSING = "unsupported_snapshot_missing"
UNSUPPORTED_SNAPSHOT_SCHEMA = "unsupported_snapshot_schema"
NOT_SCANNED = "not_scanned"
NON_CONTRIBUTING_EXPERIENCE = "non_contributing_experience"
CANDIDATE_EVIDENCE_STATES = frozenset(
    {
        SUPPORTED,
        SUPPORTED_LIMITED_LEGACY_SNAPSHOT,
        UNSUPPORTED_LEGACY_CSV_ONLY,
        UNSUPPORTED_SNAPSHOT_MISSING,
        UNSUPPORTED_SNAPSHOT_SCHEMA,
        NOT_SCANNED,
        NON_CONTRIBUTING_EXPERIENCE,
    }
)

_LEGACY_COMBO_SCHEMA = "combo_yield_candidate_snapshot.v1"
_LEGACY_CC_LP_SCHEMA = "cc_lp_candidate_snapshot.v1"
_LEGACY_INDEX_FILE = "strategy_scan_status_index.v1.json"
_LEGACY_INDEX_SCHEMA = "strategy_scan_status_index.v1"
_OWNER_FILES = {
    "opening": OPENING_CANDIDATE_SNAPSHOT_FILE,
    "sp_lc": COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE,
    "cc_lp": CC_LP_CANDIDATE_SNAPSHOT_FILE,
    "wheel_v1": "wheel_candidate_snapshot.json",
    "wheel_v2": "wheel_candidate_snapshot.v2.json",
}
_FORMAL_HISTORY_MANIFESTS = {
    CANDIDATE_SNAPSHOT_MANIFEST_V1_FILE: CANDIDATE_SNAPSHOT_MANIFEST_V1_SCHEMA,
    CANDIDATE_SNAPSHOT_MANIFEST_V3_FILE: CANDIDATE_SNAPSHOT_MANIFEST_V3_SCHEMA,
}
_KNOWN_MANIFEST_FILES = (
    CANDIDATE_SNAPSHOT_MANIFEST_V1_FILE,
    EXPERIENCE_CANDIDATE_MANIFEST_FILE,
    CANDIDATE_SNAPSHOT_MANIFEST_V3_FILE,
    CANDIDATE_SNAPSHOT_MANIFEST_FILE,
)
_FORMAL_HISTORY_INDEXES = {
    CANDIDATE_SNAPSHOT_MANIFEST_V1_SCHEMA: {
        STRATEGY_SCAN_STATUS_INDEX_V2_SCHEMA: STRATEGY_SCAN_STATUS_INDEX_V2_FILE,
        STRATEGY_SCAN_STATUS_INDEX_V3_SCHEMA: STRATEGY_SCAN_STATUS_INDEX_V3_FILE,
    },
    CANDIDATE_SNAPSHOT_MANIFEST_V3_SCHEMA: {
        STRATEGY_SCAN_STATUS_INDEX_V4_SCHEMA: STRATEGY_SCAN_STATUS_INDEX_V4_FILE,
    },
}
_FORMAL_HISTORY_OWNER_FILES = {
    CANDIDATE_SNAPSHOT_MANIFEST_V1_SCHEMA: {
        "opening": OPENING_CANDIDATE_SNAPSHOT_FILE,
        "sp_lc": COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE,
        "cc_lp": CC_LP_CANDIDATE_SNAPSHOT_FILE,
        "wheel": "wheel_candidate_snapshot.json",
    },
    CANDIDATE_SNAPSHOT_MANIFEST_V3_SCHEMA: {
        "opening": OPENING_CANDIDATE_SNAPSHOT_FILE,
        "sp_lc": COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE,
        "cc_lp": CC_LP_CANDIDATE_SNAPSHOT_FILE,
        "wheel": "wheel_candidate_snapshot.v2.json",
    },
}
_FORMAL_HISTORY_OWNER_SCHEMAS = {
    CANDIDATE_SNAPSHOT_MANIFEST_V1_SCHEMA: {
        "opening": "opening_candidate_snapshot.v1",
        "sp_lc": "combo_yield_candidate_snapshot.v2",
        "cc_lp": "cc_lp_candidate_snapshot.v2",
        "wheel": "wheel_candidate_snapshot.v1",
    },
    CANDIDATE_SNAPSHOT_MANIFEST_V3_SCHEMA: {
        "opening": "opening_candidate_snapshot.v1",
        "sp_lc": "combo_yield_candidate_snapshot.v2",
        "cc_lp": "cc_lp_candidate_snapshot.v2",
        "wheel": "wheel_candidate_snapshot.v2",
    },
}
_MODERN_OWNER_SCHEMAS = {
    "opening": OPENING_CANDIDATE_SNAPSHOT_SCHEMA,
    "sp_lc": COMBO_YIELD_CANDIDATE_SNAPSHOT_SCHEMA,
    "cc_lp": CC_LP_CANDIDATE_SNAPSHOT_SCHEMA,
    "wheel_v1": "wheel_candidate_snapshot.v1",
    "wheel_v2": "wheel_candidate_snapshot.v2",
}
_SEALED_OWNER_SCHEMAS = set(_MODERN_OWNER_SCHEMAS.values()) | {
    "combo_yield_candidate_snapshot.v2",
    "combo_yield_candidate_snapshot.v3",
    "cc_lp_candidate_snapshot.v2",
    "cc_lp_candidate_snapshot.v3",
    "wheel_candidate_snapshot.v1",
    "wheel_candidate_snapshot.v2",
    "wheel_candidate_snapshot.v3",
}
_LEGACY_OWNER_SCHEMAS = {
    "opening": "opening_candidate_snapshot.v1",
    "sp_lc": _LEGACY_COMBO_SCHEMA,
    "cc_lp": _LEGACY_CC_LP_SCHEMA,
}
_TERMINAL_STATUSES = frozenset({"completed", "unavailable", "failed", "not_applicable"})
_LEGACY_CANDIDATE_SUFFIXES = (
    "_candidates.csv",
    "_candidates_labeled.csv",
    "_candidates_reject_log.csv",
    "_reject_log.csv",
    "_pair_diagnostics.csv",
    "_rank_shadow.csv",
    "_put_universe.csv",
    "_put_universe_labeled.csv",
    "_put_universe_cash_filtered.csv",
    "_put_universe_underwritten.csv",
)


class CandidateEvidenceHistoryError(RuntimeError):
    """Raised when historical evidence cannot be classified safely."""


@dataclass(frozen=True)
class AccountCandidateEvidence:
    classification: dict[str, Any]
    owners: dict[str, dict[str, Any]]
    status_index: dict[str, Any] | None
    manifest: dict[str, Any] | None
    account_dir: Path

    @property
    def contributes_evidence(self) -> bool:
        return self.classification["status"] in {
            SUPPORTED,
            SUPPORTED_LIMITED_LEGACY_SNAPSHOT,
        }


def load_candidate_snapshot_bundle_for_inspection(
    *, base: Path, run_id: str, account: str,
) -> dict[str, Any]:
    """Load current or explicitly historical candidate evidence for inspection."""

    run_id_norm = _safe_identity(run_id, "run_id")
    account_norm = _safe_identity(account, "account").lower()
    state_dir = (
        Path(base).resolve()
        / "output_runs"
        / run_id_norm
        / "accounts"
        / account_norm
        / "state"
    )
    present = [
        name
        for name in _KNOWN_MANIFEST_FILES
        if (state_dir / name).exists() or (state_dir / name).is_symlink()
    ]
    if len(present) != 1:
        if present:
            raise CandidateSnapshotManifestError("artifact_version_mismatch")
        raise CandidateSnapshotManifestError(
            "candidate snapshot manifest is unavailable"
        )
    manifest_name = present[0]
    if manifest_name == CANDIDATE_SNAPSHOT_MANIFEST_FILE:
        return load_candidate_snapshot_bundle(
            base=base,
            run_id=run_id_norm,
            account=account_norm,
        )
    if manifest_name == EXPERIENCE_CANDIDATE_MANIFEST_FILE:
        try:
            return load_experience_candidate_snapshot_bundle(
                base=base,
                run_id=run_id_norm,
                account=account_norm,
            )
        except ExperienceCandidateSnapshotError as exc:
            raise CandidateSnapshotManifestError(str(exc)) from exc
    return load_historical_candidate_snapshot_bundle(
        base=base,
        run_id=run_id_norm,
        account=account_norm,
    )


def load_latest_candidate_snapshot_bundle_for_inspection(
    *, base: Path, account: str,
) -> dict[str, Any]:
    """Resolve the latest account run through the inspection-only loader."""

    return _load_latest_candidate_snapshot_bundle(
        base=base,
        account=account,
        loader=load_candidate_snapshot_bundle_for_inspection,
    )


def load_historical_candidate_snapshot_bundle(
    *, base: Path, run_id: str, account: str,
) -> dict[str, Any]:
    """Load one sealed v1/v3 formal bundle without admitting it to runtime."""

    root = Path(base).resolve()
    run_id_norm = _safe_identity(run_id, "run_id")
    account_norm = _safe_identity(account, "account").lower()
    account_dir = root / "output_runs" / run_id_norm / "accounts" / account_norm
    state_dir = account_dir / "state"
    present = [
        name
        for name in _KNOWN_MANIFEST_FILES
        if (state_dir / name).exists() or (state_dir / name).is_symlink()
    ]
    if len(present) != 1 or present[0] not in _FORMAL_HISTORY_MANIFESTS:
        raise CandidateSnapshotManifestError("historical candidate manifest is unavailable")
    manifest_name = present[0]
    files: dict[str, bytes] = {}
    for directory, prefix in ((account_dir, ""), (state_dir, "state/")):
        if not directory.is_dir() or directory.is_symlink():
            raise CandidateSnapshotManifestError("candidate account directory is unsafe")
        for path in directory.iterdir():
            if path.is_file() and not path.is_symlink():
                files[prefix + path.name] = path.read_bytes()
    manifest = _decoded_file(files, "state/" + manifest_name)
    dependencies: dict[str, bytes] = {}
    for entry in manifest.get("owner_snapshots") or []:
        if not isinstance(entry, Mapping):
            continue
        owner_path = str(entry.get("relpath") or "")
        if owner_path not in files:
            continue
        snapshot = _decoded_file(files, owner_path)
        for dependency in snapshot.get("dependencies") or []:
            if not isinstance(dependency, Mapping):
                continue
            relpath = str(dependency.get("relpath") or "").strip()
            if not relpath:
                continue
            target = (root / relpath).resolve()
            if root not in target.parents or not target.is_file() or target.is_symlink():
                raise CandidateSnapshotManifestError(
                    "candidate dependency binding is unavailable"
                )
            dependencies[relpath] = target.read_bytes()
    return validate_historical_candidate_snapshot_bundle_bytes(
        manifest_name=manifest_name,
        files=files,
        account_names=[item.name for item in account_dir.iterdir()],
        state_names=[item.name for item in state_dir.iterdir()],
        run_id=run_id_norm,
        account=account_norm,
        dependencies=dependencies,
    )


def validate_historical_candidate_snapshot_bundle_bytes(
    *,
    manifest_name: str,
    files: Mapping[str, bytes],
    account_names: list[str],
    state_names: list[str],
    run_id: str,
    account: str,
    dependencies: Mapping[str, bytes],
    check: Any = lambda: None,
    require_external_bindings: bool = True,
) -> dict[str, Any]:
    """Validate sealed formal v1/v3 bytes at the explicit history boundary."""

    if manifest_name == EXPERIENCE_CANDIDATE_MANIFEST_FILE:
        return _validate_historical_experience_bundle_bytes(
            manifest_name=manifest_name,
            files=files,
            account_names=account_names,
            state_names=state_names,
            run_id=run_id,
            account=account,
            dependencies=dependencies,
            check=check,
            require_external_bindings=require_external_bindings,
        )
    expected_schema = _FORMAL_HISTORY_MANIFESTS.get(manifest_name)
    if expected_schema is None:
        raise CandidateSnapshotManifestError("historical candidate manifest is unsupported")
    manifest = _decoded_file(files, "state/" + manifest_name, check=check)
    _validate_historical_manifest(
        manifest,
        manifest_name=manifest_name,
        expected_schema=expected_schema,
        run_id=run_id,
        account=account,
    )
    if set(state_names).intersection(_KNOWN_MANIFEST_FILES) != {manifest_name}:
        raise CandidateSnapshotManifestError("artifact_version_mismatch")
    owner_files = _FORMAL_HISTORY_OWNER_FILES[expected_schema]
    expected_files = {owner_files[owner] for owner in manifest["expected_owners"]}
    all_owner_files = {
        filename
        for mapping in _FORMAL_HISTORY_OWNER_FILES.values()
        for filename in mapping.values()
    } | {"wheel_candidate_snapshot.v3.json"}
    if set(state_names).intersection(all_owner_files) != expected_files:
        raise CandidateSnapshotManifestError("artifact_version_mismatch")
    allowed_indexes = set(_FORMAL_HISTORY_INDEXES[expected_schema].values())
    all_indexes = {
        STRATEGY_SCAN_STATUS_INDEX_V2_FILE,
        STRATEGY_SCAN_STATUS_INDEX_V3_FILE,
        STRATEGY_SCAN_STATUS_INDEX_V4_FILE,
        "strategy_scan_status_index.v5.json",
    }
    binding = dict(manifest["status_index"])
    if set(account_names).intersection(all_indexes) != {binding["relpath"]}:
        raise CandidateSnapshotManifestError("artifact_version_mismatch")
    if binding["relpath"] not in allowed_indexes:
        raise CandidateSnapshotManifestError("artifact_version_mismatch")
    index = _decoded_file(files, binding["relpath"], check=check)
    validators = {
        STRATEGY_SCAN_STATUS_INDEX_V2_SCHEMA: validate_strategy_scan_status_index_v2,
        STRATEGY_SCAN_STATUS_INDEX_V3_SCHEMA: validate_strategy_scan_status_index_v3,
        STRATEGY_SCAN_STATUS_INDEX_V4_SCHEMA: validate_strategy_scan_status_index_v4,
    }
    try:
        validators[binding["schema_version"]](
            index,
            expected_run_id=run_id,
            expected_account=account,
            expected_account_config_sha256=manifest["account_config_sha256"],
        )
    except (KeyError, StrategyScanStatusError) as exc:
        raise CandidateSnapshotManifestError(
            "historical candidate status index is invalid"
        ) from exc
    if (
        sha256_bytes(files[binding["relpath"]]) != binding["sha256"]
        or index.get("content_sha256") != binding["content_sha256"]
    ):
        raise CandidateSnapshotManifestError("candidate status index binding mismatch")
    requires_direction = expected_schema == CANDIDATE_SNAPSHOT_MANIFEST_V3_SCHEMA
    if _expected_scopes(
        index,
        require_wheel_direction=requires_direction,
    ) != manifest["expected_scopes"]:
        raise CandidateSnapshotManifestError("candidate status index scope binding mismatch")
    if requires_direction:
        for row in index.get("items") or []:
            check()
            source_name = str(row["source_status_path"])
            if source_name in files:
                _validate_historical_source_status_bytes(
                    row,
                    files[source_name],
                )
            elif require_external_bindings:
                raise CandidateSnapshotManifestError(
                    "candidate source status is unavailable"
                )
    owners: dict[str, dict[str, Any]] = {}
    for raw_entry in manifest["owner_snapshots"]:
        check()
        entry = dict(raw_entry)
        owner = str(entry["candidate_owner"])
        snapshot = _decoded_file(files, str(entry["relpath"]), check=check)
        _validate_historical_owner(
            snapshot,
            entry=entry,
            manifest=manifest,
            index=index,
            owner=owner,
            encoded=files[str(entry["relpath"])],
            dependencies=dependencies,
            require_wheel_direction=requires_direction,
            check=check,
            require_external_bindings=require_external_bindings,
        )
        owners[owner] = snapshot
    bundle = {"manifest": manifest, "status_index": index, "owners": owners}
    return bundle if requires_direction else _adapt_legacy_wheel_bundle(bundle)


def _validate_historical_experience_bundle_bytes(
    *,
    manifest_name: str,
    files: Mapping[str, bytes],
    account_names: list[str],
    state_names: list[str],
    run_id: str,
    account: str,
    dependencies: Mapping[str, bytes],
    check: Any,
    require_external_bindings: bool,
) -> dict[str, Any]:
    manifest = _decoded_file(files, "state/" + manifest_name, check=check)
    try:
        _validate_experience_manifest(
            manifest,
            expected_run_id=run_id,
            expected_account=account,
        )
    except ExperienceCandidateSnapshotError as exc:
        raise CandidateSnapshotManifestError(str(exc)) from exc
    if set(state_names).intersection(_KNOWN_MANIFEST_FILES) != {manifest_name}:
        raise CandidateSnapshotManifestError("artifact_version_mismatch")
    expected_files = {
        EXPERIENCE_OWNER_FILES[owner]
        for owner in manifest["expected_owners"]
    }
    all_owner_files = set(EXPERIENCE_OWNER_FILES.values()) | {
        "wheel_candidate_snapshot.json",
        "wheel_candidate_snapshot.v2.json",
        "wheel_candidate_snapshot.v3.json",
    }
    if set(state_names).intersection(all_owner_files) != expected_files:
        raise CandidateSnapshotManifestError("artifact_version_mismatch")
    if set(account_names).intersection(
        {
            STRATEGY_SCAN_STATUS_INDEX_V2_FILE,
            STRATEGY_SCAN_STATUS_INDEX_V3_FILE,
            STRATEGY_SCAN_STATUS_INDEX_V4_FILE,
            "strategy_scan_status_index.v5.json",
        }
    ) != {STRATEGY_SCAN_STATUS_INDEX_V3_FILE}:
        raise CandidateSnapshotManifestError("artifact_version_mismatch")
    binding = dict(manifest["status_index"])
    index = _decoded_file(files, binding["relpath"], check=check)
    try:
        validate_strategy_scan_status_index_v3(
            index,
            expected_run_id=run_id,
            expected_account=account,
            expected_account_config_sha256=manifest["account_config_sha256"],
        )
    except StrategyScanStatusError as exc:
        raise CandidateSnapshotManifestError(
            "experience status index is invalid"
        ) from exc
    if (
        sha256_bytes(files[binding["relpath"]]) != binding["sha256"]
        or index.get("content_sha256") != binding["content_sha256"]
        or _experience_projection(index) != _experience_projection(manifest)
    ):
        raise CandidateSnapshotManifestError(
            "experience status index binding mismatch"
        )
    index_owners = sorted(
        {
            str(row.get("candidate_owner") or "").strip().lower()
            for row in index.get("items") or []
        }
    )
    index_markets = sorted(
        {
            str(row.get("market") or "").strip().upper()
            for row in index.get("items") or []
            if str(row.get("market") or "").strip()
        }
    )
    if (
        index_owners != manifest["expected_owners"]
        or index_markets != manifest["markets"]
    ):
        raise CandidateSnapshotManifestError("experience status scope mismatch")
    owners: dict[str, dict[str, Any]] = {}
    for entry in manifest["owner_snapshots"]:
        check()
        owner = str(entry["candidate_owner"])
        relpath = str(entry["relpath"])
        snapshot = _decoded_file(files, relpath, check=check)
        try:
            _validate_experience_owner(snapshot, owner=owner)
        except ExperienceCandidateSnapshotError as exc:
            raise CandidateSnapshotManifestError(str(exc)) from exc
        if (
            sha256_bytes(files[relpath]) != entry["sha256"]
            or snapshot.get("run_id") != run_id
            or snapshot.get("account") != account
            or snapshot.get("candidate_owner") != owner
            or snapshot.get("account_config_sha256")
            != manifest["account_config_sha256"]
            or snapshot.get("strategy_policy_sha256")
            != manifest["strategy_policy_sha256"]
            or snapshot.get("sealed_at_utc") != manifest["sealed_at_utc"]
            or snapshot.get("content_sha256") != entry["content_sha256"]
            or snapshot.get("opening_status") != entry["opening_status"]
            or _experience_projection(snapshot) != _experience_projection(manifest)
        ):
            raise CandidateSnapshotManifestError(
                "experience owner binding mismatch"
            )
        owner_markets = {
            str(row.get("market") or "").strip().upper()
            for row in index.get("items") or []
            if str(row.get("candidate_owner") or "").strip().lower() == owner
        }
        expected_market = next(iter(owner_markets)) if len(owner_markets) == 1 else "MULTI"
        if snapshot.get("market") != expected_market:
            raise CandidateSnapshotManifestError("experience owner market mismatch")
        expected_scopes = sorted(
            (
                str(row.get("symbol") or "").strip().upper(),
                str(row.get("strategy_mode") or "").strip().lower(),
                str(row.get("status") or "").strip().lower(),
                str(row.get("reason") or row.get("reason_code") or "").strip(),
                row.get("candidate_count"),
            )
            for row in index.get("items") or []
            if str(row.get("candidate_owner") or "").strip().lower() == owner
        )
        actual_scopes = sorted(
            (
                str(row.get("symbol") or "").strip().upper(),
                str(row.get("strategy_mode") or "").strip().lower(),
                str(row.get("status") or "").strip().lower(),
                str(row.get("reason_code") or "").strip(),
                row.get("candidate_count"),
            )
            for row in snapshot.get("scope_results") or []
            if isinstance(row, Mapping)
        )
        if actual_scopes != expected_scopes:
            raise CandidateSnapshotManifestError("experience owner scope mismatch")
        for dependency in snapshot.get("dependencies") or []:
            check()
            relpath = str(dependency.get("relpath") or "").strip()
            if relpath and relpath in dependencies and (
                sha256_bytes(dependencies[relpath]) != dependency.get("sha256")
            ):
                raise CandidateSnapshotManifestError(
                    "candidate dependency binding mismatch"
                )
            if relpath and relpath not in dependencies and require_external_bindings:
                raise CandidateSnapshotManifestError(
                    "candidate dependency binding mismatch"
                )
        owners[owner] = snapshot
    return {"manifest": manifest, "status_index": index, "owners": owners}


def _validate_historical_manifest(
    manifest: Mapping[str, Any],
    *,
    manifest_name: str,
    expected_schema: str,
    run_id: str,
    account: str,
) -> None:
    item = dict(manifest)
    if (
        item.get("schema_version") != expected_schema
        or item.get("run_id") != run_id
        or str(item.get("account") or "").lower() != account
    ):
        raise CandidateSnapshotManifestError(
            "historical candidate manifest identity mismatch"
        )
    _manifest_sha256(item.get("account_config_sha256"), "account_config_sha256")
    _manifest_sha256(item.get("strategy_policy_sha256"), "strategy_policy_sha256")
    content_hash = _manifest_sha256(item.get("content_sha256"), "content_sha256")
    content = {key: value for key, value in item.items() if key != "content_sha256"}
    if canonical_sha256(content) != content_hash:
        raise CandidateSnapshotManifestError(
            "historical candidate manifest content hash mismatch"
        )
    _timestamp(item.get("sealed_at_utc"), "sealed_at_utc")
    scopes = item.get("expected_scopes")
    owners = item.get("expected_owners")
    entries = item.get("owner_snapshots")
    if (
        not isinstance(scopes, list)
        or any(not isinstance(row, Mapping) for row in scopes)
        or not isinstance(owners, list)
        or not isinstance(entries, list)
        or any(not isinstance(row, Mapping) for row in entries)
    ):
        raise CandidateSnapshotManifestError(
            "historical candidate manifest structure is invalid"
        )
    requires_direction = expected_schema == CANDIDATE_SNAPSHOT_MANIFEST_V3_SCHEMA
    projected = [
        _historical_scope(row, require_wheel_direction=requires_direction)
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
        raise CandidateSnapshotManifestError(
            "historical candidate manifest scopes are not canonical"
        )
    if len({tuple(sorted(row.items())) for row in projected}) != len(projected):
        raise CandidateSnapshotManifestError(
            "historical candidate manifest scopes are duplicated"
        )
    projected_owners = sorted({row["candidate_owner"] for row in projected})
    if (
        item.get("markets") != sorted({row["market"] for row in projected})
        or owners != projected_owners
        or [row.get("candidate_owner") for row in entries] != projected_owners
        or item.get("completion_reason")
        != ("complete" if projected else "no_applicable_scope")
    ):
        raise CandidateSnapshotManifestError(
            "historical candidate manifest scope set mismatch"
        )
    index = item.get("status_index")
    allowed_indexes = _FORMAL_HISTORY_INDEXES[expected_schema]
    if (
        not isinstance(index, Mapping)
        or index.get("schema_version") not in allowed_indexes
        or index.get("relpath") != allowed_indexes.get(index.get("schema_version"))
    ):
        raise CandidateSnapshotManifestError(
            "historical candidate manifest status index mismatch"
        )
    _manifest_sha256(index.get("sha256"), "status index sha256")
    _manifest_sha256(index.get("content_sha256"), "status index content_sha256")
    owner_files = _FORMAL_HISTORY_OWNER_FILES[expected_schema]
    owner_schemas = _FORMAL_HISTORY_OWNER_SCHEMAS[expected_schema]
    for entry in entries:
        owner = str(entry.get("candidate_owner") or "")
        expected = [row for row in projected if row["candidate_owner"] == owner]
        if (
            owner not in owner_files
            or entry.get("schema_version") != owner_schemas[owner]
            or entry.get("relpath") != f"state/{owner_files[owner]}"
            or entry.get("covered_scopes") != expected
        ):
            raise CandidateSnapshotManifestError(
                "historical candidate manifest owner binding mismatch"
            )
        _manifest_sha256(entry.get("sha256"), f"{owner} snapshot sha256")
        _manifest_sha256(entry.get("content_sha256"), f"{owner} content sha256")
        _required(entry.get("opening_status"), f"{owner} opening_status")
    if manifest_name != {
        CANDIDATE_SNAPSHOT_MANIFEST_V1_SCHEMA: CANDIDATE_SNAPSHOT_MANIFEST_V1_FILE,
        CANDIDATE_SNAPSHOT_MANIFEST_V3_SCHEMA: CANDIDATE_SNAPSHOT_MANIFEST_V3_FILE,
    }[expected_schema]:
        raise CandidateSnapshotManifestError("artifact_version_mismatch")


def _validate_historical_owner(
    snapshot: Mapping[str, Any],
    *,
    entry: Mapping[str, Any],
    manifest: Mapping[str, Any],
    index: Mapping[str, Any],
    owner: str,
    encoded: bytes,
    dependencies: Mapping[str, bytes],
    require_wheel_direction: bool,
    check: Any,
    require_external_bindings: bool,
) -> None:
    if (
        sha256_bytes(encoded) != entry["sha256"]
        or snapshot.get("schema_version") != entry["schema_version"]
        or snapshot.get("run_id") != manifest["run_id"]
        or str(snapshot.get("account") or "").lower() != manifest["account"]
        or snapshot.get("account_config_sha256")
        != manifest["account_config_sha256"]
        or snapshot.get("strategy_policy_sha256")
        != manifest["strategy_policy_sha256"]
        or snapshot.get("opening_status") != entry["opening_status"]
    ):
        raise CandidateSnapshotManifestError(
            f"historical candidate owner binding mismatch: {owner}"
        )
    content_hash = _manifest_sha256(
        snapshot.get("content_sha256"),
        f"{owner} content_sha256",
    )
    content = {
        key: value for key, value in snapshot.items() if key != "content_sha256"
    }
    if content_hash != entry["content_sha256"] or canonical_sha256(content) != content_hash:
        raise CandidateSnapshotManifestError(
            f"historical candidate owner content mismatch: {owner}"
        )
    _timestamp(snapshot.get("sealed_at_utc"), f"{owner} sealed_at_utc")
    covered = _snapshot_strategy_scopes(
        snapshot,
        owner=owner,
        index_items=list(index.get("items") or []),
        require_wheel_direction=require_wheel_direction,
    )
    if covered != entry["covered_scopes"]:
        raise CandidateSnapshotManifestError(
            f"historical candidate owner scope mismatch: {owner}"
        )
    for dependency in snapshot.get("dependencies") or []:
        check()
        if not isinstance(dependency, Mapping):
            raise CandidateSnapshotManifestError(
                "candidate dependency binding is invalid"
            )
        relpath = str(dependency.get("relpath") or "").strip()
        if relpath and relpath in dependencies and (
            sha256_bytes(dependencies[relpath]) != dependency.get("sha256")
        ):
            raise CandidateSnapshotManifestError(
                "candidate dependency binding mismatch"
            )
        if relpath and relpath not in dependencies and require_external_bindings:
            raise CandidateSnapshotManifestError(
                "candidate dependency binding mismatch"
            )


def _historical_scope(
    row: Mapping[str, Any], *, require_wheel_direction: bool,
) -> dict[str, str]:
    result = {
        "market": _required(row.get("market"), "scope market").upper(),
        "symbol": _required(row.get("symbol"), "scope symbol").upper(),
        "strategy_family": _required(
            row.get("strategy_family"), "scope strategy_family"
        ).lower(),
        "strategy_mode": _required(
            row.get("strategy_mode"), "scope strategy_mode"
        ).lower(),
        "candidate_owner": _required(
            row.get("candidate_owner"), "scope candidate_owner"
        ).lower(),
    }
    direction = str(row.get("direction") or "").strip().lower()
    if result["strategy_family"] == "wheel":
        if require_wheel_direction and direction not in {"call", "put"}:
            raise CandidateSnapshotManifestError(
                "historical Wheel direction is invalid"
            )
        if direction:
            result["direction"] = direction
    elif direction:
        raise CandidateSnapshotManifestError(
            "historical non-Wheel scope has direction"
        )
    return result


def _adapt_legacy_wheel_bundle(bundle: Mapping[str, Any]) -> dict[str, Any]:
    adapted = json.loads(json.dumps(dict(bundle), ensure_ascii=False, allow_nan=False))
    if "wheel" not in adapted.get("owners", {}):
        return adapted
    for row in adapted["status_index"].get("items") or []:
        if row.get("strategy_family") == "wheel":
            row["direction"] = "call"
    for row in adapted["manifest"].get("expected_scopes") or []:
        if row.get("strategy_family") == "wheel":
            row["direction"] = "call"
    for entry in adapted["manifest"].get("owner_snapshots") or []:
        if entry.get("candidate_owner") == "wheel":
            for row in entry.get("covered_scopes") or []:
                row["direction"] = "call"
    snapshot = adapted["owners"]["wheel"]
    for row in snapshot.get("scope_results") or []:
        if row.get("scope") == "strategy":
            row["direction"] = "call"
    for batch in snapshot.get("batches") or []:
        batch.setdefault("direction", "call")
        for candidate in batch.get("raw_candidates") or []:
            candidate.setdefault("direction", "call")
        if isinstance(batch.get("final_candidate"), dict):
            batch["final_candidate"].setdefault("direction", "call")
    return adapted


def _decoded_file(
    files: Mapping[str, bytes], name: str, *, check: Any = lambda: None,
) -> dict[str, Any]:
    check()
    try:
        payload = json.loads(files[name].decode("utf-8"))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateSnapshotManifestError(
            "historical candidate bundle data is unavailable"
        ) from exc
    if not isinstance(payload, dict):
        raise CandidateSnapshotManifestError(
            "historical candidate bundle data is invalid"
        )
    return payload


def _validate_historical_source_status_bytes(
    row: Mapping[str, Any],
    encoded: bytes,
) -> None:
    """Validate source bytes according to the fixed v4-index history contract."""

    if sha256_bytes(encoded) != row.get("source_status_sha256"):
        raise CandidateSnapshotManifestError(
            "historical candidate source status hash mismatch"
        )
    if row.get("strategy_family") != "wheel":
        return
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateSnapshotManifestError(
            "historical candidate Wheel source status is unreadable"
        ) from exc
    if not isinstance(payload, dict):
        raise CandidateSnapshotManifestError(
            "historical candidate Wheel source status is invalid"
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
            "historical candidate Wheel source status content binding mismatch"
        )


def _safe_identity(value: Any, field: str) -> str:
    text = _required(value, field)
    if text in {".", ".."} or Path(text).name != text:
        raise CandidateSnapshotManifestError(
            "candidate snapshot identity is invalid"
        )
    return text


def _timestamp(value: Any, field: str) -> str:
    text = _required(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CandidateSnapshotManifestError(f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise CandidateSnapshotManifestError(f"{field} has no timezone")
    return text


def _manifest_sha256(value: Any, field: str) -> str:
    digest = str(value or "").strip().lower()
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise CandidateSnapshotManifestError(f"{field} is invalid")
    return digest


def load_account_candidate_evidence(
    *,
    base: Path,
    run_id: str,
    account: str,
    runs_root: Path | None = None,
) -> AccountCandidateEvidence:
    """Load modern or tightly bounded legacy snapshot evidence for one account run.

    Legacy candidate CSV names are inspected only as directory metadata. Their
    bytes are never opened and cannot contribute candidate facts.
    """

    root = Path(base).resolve()
    run_id_norm = _required(run_id, "run_id")
    account_norm = _required(account, "account").lower()
    resolved_runs_root, authority_base = _runs_root_and_authority_base(
        base=root,
        runs_root=runs_root,
    )
    account_dir = resolved_runs_root / run_id_norm / "accounts" / account_norm
    state_dir = account_dir / "state"
    legacy_csv_names = _legacy_candidate_names(account_dir)
    common = {
        "schema_version": CANDIDATE_EVIDENCE_CLASSIFICATION_SCHEMA,
        "run_id": run_id_norm,
        "account": account_norm,
        "legacy_candidate_files": legacy_csv_names,
    }

    manifest_paths = tuple(
        state_dir / name
        for name in (
            CANDIDATE_SNAPSHOT_MANIFEST_V1_FILE,
            CANDIDATE_SNAPSHOT_MANIFEST_V3_FILE,
            CANDIDATE_SNAPSHOT_MANIFEST_FILE,
        )
    )
    formal_manifests_present = [
        path for path in manifest_paths if path.exists() or path.is_symlink()
    ]
    experience_manifest_path = state_dir / EXPERIENCE_CANDIDATE_MANIFEST_FILE
    if experience_manifest_path.exists() or experience_manifest_path.is_symlink():
        if formal_manifests_present:
            return _result(
                account_dir=account_dir,
                common=common,
                status=UNSUPPORTED_SNAPSHOT_SCHEMA,
                reason_code="formal_and_experience_manifests_conflict",
            )
        try:
            bundle = load_experience_candidate_snapshot_bundle(
                base=authority_base,
                run_id=run_id_norm,
                account=account_norm,
            )
        except ExperienceCandidateSnapshotError as exc:
            return _result(
                account_dir=account_dir,
                common=common,
                status=UNSUPPORTED_SNAPSHOT_SCHEMA,
                reason_code="experience_candidate_manifest_invalid",
                detail=str(exc),
            )
        return _result(
            account_dir=account_dir,
            common=common,
            status=NON_CONTRIBUTING_EXPERIENCE,
            reason_code="experience_candidate_not_executable",
            owners=dict(bundle["owners"]),
            status_index=dict(bundle["status_index"]),
            manifest=dict(bundle["manifest"]),
        )
    if formal_manifests_present:
        try:
            bundle = load_candidate_snapshot_bundle_for_inspection(
                base=authority_base,
                run_id=run_id_norm,
                account=account_norm,
            )
        except CandidateSnapshotManifestError as exc:
            return _result(
                account_dir=account_dir,
                common=common,
                status=UNSUPPORTED_SNAPSHOT_SCHEMA,
                reason_code="candidate_snapshot_manifest_invalid",
                detail=str(exc),
            )
        manifest_schema = str(bundle["manifest"].get("schema_version") or "")
        is_experience = bundle["manifest"].get("scan_mode") == "experience"
        is_historical = manifest_schema in set(_FORMAL_HISTORY_MANIFESTS.values())
        classification_status = (
            NON_CONTRIBUTING_EXPERIENCE
            if is_experience
            else (
                SUPPORTED_LIMITED_LEGACY_SNAPSHOT
                if is_historical
                else SUPPORTED
            )
        )
        return _result(
            account_dir=account_dir,
            common=common,
            status=classification_status,
            reason_code=(
                "experience_candidate_not_executable"
                if is_experience
                else (
                    "historical_candidate_snapshot_valid"
                    if is_historical
                    else "candidate_snapshot_manifest_valid"
                )
            ),
            owners=dict(bundle["owners"]),
            status_index=dict(bundle["status_index"]),
            manifest=dict(bundle["manifest"]),
            extra={
                "markets": sorted(
                    str(value).strip().lower()
                    for value in bundle["manifest"].get("markets") or []
                    if str(value).strip()
                ),
            },
        )

    owner_payloads, owner_read_errors = _read_owner_payloads(state_dir)
    if any(
        (account_dir / filename).exists()
        for filename in (
            STRATEGY_SCAN_STATUS_INDEX_V2_FILE,
            STRATEGY_SCAN_STATUS_INDEX_V3_FILE,
            STRATEGY_SCAN_STATUS_INDEX_V4_FILE,
            "strategy_scan_status_index.v5.json",
        )
    ) or any(
        payload.get("schema_version") in _SEALED_OWNER_SCHEMAS
        for payload in owner_payloads.values()
    ):
        return _result(
            account_dir=account_dir,
            common=common,
            status=UNSUPPORTED_SNAPSHOT_MISSING,
            reason_code="candidate_snapshot_manifest_missing",
        )

    legacy_index_path = account_dir / _LEGACY_INDEX_FILE
    if legacy_index_path.exists():
        try:
            index = _load_legacy_status_index(
                legacy_index_path,
                run_id=run_id_norm,
                account=account_norm,
            )
        except CandidateEvidenceHistoryError as exc:
            return _result(
                account_dir=account_dir,
                common=common,
                status=UNSUPPORTED_SNAPSHOT_SCHEMA,
                reason_code="legacy_status_index_invalid",
                detail=str(exc),
            )
        try:
            config, config_hash = _load_legacy_account_config(
                base=authority_base,
                run_id=run_id_norm,
                account=account_norm,
            )
            owners_by_scope = _legacy_expected_owners(index, config=config)
        except (CandidateEvidenceHistoryError, AccountRunConfigError) as exc:
            return _result(
                account_dir=account_dir,
                common=common,
                status=UNSUPPORTED_SNAPSHOT_SCHEMA,
                reason_code="legacy_config_authority_invalid",
                detail=str(exc),
            )
        expected_owners = sorted(set(owners_by_scope.values()))
        missing = [owner for owner in expected_owners if owner not in owner_payloads]
        if missing:
            return _result(
                account_dir=account_dir,
                common=common,
                status=UNSUPPORTED_SNAPSHOT_MISSING,
                reason_code="legacy_owner_snapshot_missing",
                detail=",".join(missing),
            )
        if owner_read_errors:
            return _result(
                account_dir=account_dir,
                common=common,
                status=UNSUPPORTED_SNAPSHOT_SCHEMA,
                reason_code="legacy_owner_snapshot_unreadable",
                detail=owner_read_errors[0],
            )
        try:
            owners = _validate_legacy_owner_bundle(
                authority_base=authority_base,
                run_id=run_id_norm,
                account=account_norm,
                account_config_sha256=config_hash,
                index=index,
                owners_by_scope=owners_by_scope,
                payloads=owner_payloads,
            )
        except (CandidateEvidenceHistoryError, OpeningCandidateSnapshotError) as exc:
            return _result(
                account_dir=account_dir,
                common=common,
                status=UNSUPPORTED_SNAPSHOT_SCHEMA,
                reason_code="legacy_owner_snapshot_invalid",
                detail=str(exc),
            )
        return _result(
            account_dir=account_dir,
            common=common,
            status=SUPPORTED_LIMITED_LEGACY_SNAPSHOT,
            reason_code="legacy_snapshot_bundle_valid_limited",
            owners=owners,
            status_index=index,
            extra={
                "limitations": [
                    "terminal_manifest_unavailable",
                    "combo_pair_diagnostics_unavailable",
                ],
                "account_config_sha256": config_hash,
                "markets": sorted(
                    {
                        str(row.get("market") or "").strip().lower()
                        for row in index.get("items") or []
                        if str(row.get("market") or "").strip()
                    }
                ),
            },
        )

    if owner_payloads or owner_read_errors:
        return _result(
            account_dir=account_dir,
            common=common,
            status=UNSUPPORTED_SNAPSHOT_SCHEMA,
            reason_code="orphan_candidate_snapshot_invalid",
            detail=owner_read_errors[0] if owner_read_errors else None,
        )
    if legacy_csv_names:
        return _result(
            account_dir=account_dir,
            common=common,
            status=UNSUPPORTED_LEGACY_CSV_ONLY,
            reason_code="legacy_candidate_csv_without_sealed_snapshot",
        )
    if _has_other_scan_evidence(account_dir):
        return _result(
            account_dir=account_dir,
            common=common,
            status=UNSUPPORTED_SNAPSHOT_MISSING,
            reason_code="scan_evidence_without_candidate_snapshot",
        )
    return _result(
        account_dir=account_dir,
        common=common,
        status=NOT_SCANNED,
        reason_code="candidate_scan_evidence_absent",
    )


def load_run_candidate_evidence(
    *,
    base: Path,
    run_id: str,
    runs_root: Path | None = None,
) -> list[AccountCandidateEvidence]:
    root = Path(base).resolve()
    run_id_norm = _required(run_id, "run_id")
    resolved_runs_root, _authority_base = _runs_root_and_authority_base(
        base=root,
        runs_root=runs_root,
    )
    accounts_dir = resolved_runs_root / run_id_norm / "accounts"
    if not accounts_dir.is_dir():
        return []
    return [
        load_account_candidate_evidence(
            base=root,
            run_id=run_id_norm,
            account=path.name,
            runs_root=resolved_runs_root,
        )
        for path in sorted(accounts_dir.iterdir(), key=lambda item: item.name)
        if path.is_dir() and not path.is_symlink()
    ]


def summarize_run_candidate_evidence(
    *,
    base: Path,
    run_id: str,
    runs_root: Path | None = None,
) -> dict[str, Any]:
    evidence = load_run_candidate_evidence(
        base=base,
        run_id=run_id,
        runs_root=runs_root,
    )
    counts = {
        state: sum(item.classification["status"] == state for item in evidence)
        for state in sorted(CANDIDATE_EVIDENCE_STATES)
    }
    strict = bool(evidence) and all(item.classification["status"] == SUPPORTED for item in evidence)
    return {
        "schema_version": CANDIDATE_EVIDENCE_CLASSIFICATION_SCHEMA,
        "run_id": _required(run_id, "run_id"),
        "accounts": [item.classification for item in evidence],
        "counts": counts,
        "reason_code": (
            "all_accounts_manifest_supported"
            if strict
            else "candidate_evidence_coverage_incomplete"
            if evidence
            else "run_has_no_account_candidate_evidence"
        ),
    }


def _result(
    *,
    account_dir: Path,
    common: dict[str, Any],
    status: str,
    reason_code: str,
    owners: dict[str, dict[str, Any]] | None = None,
    status_index: dict[str, Any] | None = None,
    manifest: dict[str, Any] | None = None,
    detail: str | None = None,
    extra: dict[str, Any] | None = None,
) -> AccountCandidateEvidence:
    classification = {
        **common,
        "status": status,
        "reason_code": reason_code,
        "contributes_snapshot_facts": status in {SUPPORTED, SUPPORTED_LIMITED_LEGACY_SNAPSHOT},
        "contributes_evidence": status in {SUPPORTED, SUPPORTED_LIMITED_LEGACY_SNAPSHOT},
        "owner_snapshots": sorted((owners or {}).keys()),
    }
    if detail:
        classification["detail"] = detail
    if extra:
        classification.update(extra)
    return AccountCandidateEvidence(
        classification=classification,
        owners=owners or {},
        status_index=status_index,
        manifest=manifest,
        account_dir=account_dir,
    )


def _read_owner_payloads(
    state_dir: Path,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    payloads: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for owner, filename in _OWNER_FILES.items():
        path = state_dir / filename
        if not path.exists():
            continue
        if not path.is_file() or path.is_symlink():
            errors.append(f"{filename}:not_regular")
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(f"{filename}:{type(exc).__name__}")
            continue
        if not isinstance(payload, dict):
            errors.append(f"{filename}:not_object")
            continue
        payloads[owner] = payload
    return payloads, errors


def _load_legacy_status_index(
    path: Path,
    *,
    run_id: str,
    account: str,
) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise CandidateEvidenceHistoryError("legacy status index is not a regular file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateEvidenceHistoryError("legacy status index is unreadable") from exc
    if not isinstance(payload, dict):
        raise CandidateEvidenceHistoryError("legacy status index must be an object")
    if payload.get("schema_version") != _LEGACY_INDEX_SCHEMA:
        raise CandidateEvidenceHistoryError("legacy status index schema mismatch")
    if payload.get("run_id") != run_id or str(payload.get("account") or "").lower() != account:
        raise CandidateEvidenceHistoryError("legacy status index identity mismatch")
    rows = payload.get("items")
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise CandidateEvidenceHistoryError("legacy status index items are invalid")
    expected_count = payload.get("expected_count")
    if isinstance(expected_count, bool) or not isinstance(expected_count, int) or expected_count != len(rows):
        raise CandidateEvidenceHistoryError("legacy status index count mismatch")
    seen: set[tuple[str, str]] = set()
    normalized: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        if row.get("run_id") != run_id or str(row.get("account") or "").lower() != account:
            raise CandidateEvidenceHistoryError("legacy status item identity mismatch")
        market = _required(row.get("market"), "legacy status market").upper()
        symbol = _required(row.get("symbol"), "legacy status symbol").upper()
        family = _required(row.get("strategy_family"), "legacy status family").lower()
        if family not in {"sell_put", "covered_call", "combo_yield"}:
            raise CandidateEvidenceHistoryError("legacy status family is unsupported")
        if row.get("status") not in _TERMINAL_STATUSES:
            raise CandidateEvidenceHistoryError("legacy status item is not terminal")
        if row.get("status") == "completed":
            count = row.get("candidate_count")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise CandidateEvidenceHistoryError("legacy completed count is invalid")
        elif not str(row.get("reason") or "").strip():
            raise CandidateEvidenceHistoryError("legacy non-completed reason is missing")
        key = (symbol, family)
        if key in seen:
            raise CandidateEvidenceHistoryError("legacy status scope is duplicated")
        seen.add(key)
        normalized.append({**row, "market": market, "symbol": symbol, "strategy_family": family})
    expected_counts = {
        status: sum(row.get("status") == status for row in normalized)
        for status in ("completed", "unavailable", "failed", "not_applicable")
    }
    if payload.get("counts") != expected_counts:
        raise CandidateEvidenceHistoryError("legacy status counts mismatch")
    return {**payload, "items": normalized}


def _load_legacy_account_config(
    *,
    base: Path,
    run_id: str,
    account: str,
) -> tuple[dict[str, Any], str]:
    state_bytes = read_account_run_state_bytes_safely(
        base=base,
        run_id=run_id,
        account=account,
        name=ACCOUNT_RUN_CONFIG_NAME,
    )
    digest = sha256(state_bytes).hexdigest()
    state_path = account_run_config_path(
        base=base,
        run_id=run_id,
        account=account,
    )
    config = load_published_account_run_config(
        base=base,
        run_id=run_id,
        account=account,
        state_path=state_path,
        account_config_sha256=digest,
        expected_bytes=state_bytes,
    )
    if canonical_account_run_config_bytes(config) != state_bytes:
        raise CandidateEvidenceHistoryError("legacy account config is not canonical")
    return config, digest


def _legacy_expected_owners(
    index: Mapping[str, Any],
    *,
    config: Mapping[str, Any],
) -> dict[tuple[str, str], str]:
    symbol_cfgs: dict[str, dict[str, Any]] = {}
    for raw in resolve_watchlist_config(dict(config)):
        symbol = str(raw.get("symbol") or "").strip().upper()
        if not symbol or symbol in symbol_cfgs:
            raise CandidateEvidenceHistoryError("legacy config symbol mapping is ambiguous")
        symbol_cfgs[symbol] = raw
    out: dict[tuple[str, str], str] = {}
    for row in index.get("items") or []:
        symbol = str(row["symbol"])
        family = str(row["strategy_family"])
        if family in {"sell_put", "covered_call"}:
            owner = "opening"
        else:
            symbol_cfg = symbol_cfgs.get(symbol)
            if symbol_cfg is None:
                raise CandidateEvidenceHistoryError(f"legacy Combo symbol is absent from immutable config: {symbol}")
            if "combo_yield" not in symbol_cfg and isinstance(symbol_cfg.get("yield_enhancement"), dict):
                symbol_cfg = {**symbol_cfg, "combo_yield": dict(symbol_cfg["yield_enhancement"])}
            combo_cfg = resolve_combo_yield_cfg(symbol_cfg)
            variant = str(combo_cfg.get("variant") or "").strip().lower()
            if not combo_cfg or not bool(combo_cfg.get("enabled")) or variant not in {"sp_lc", "cc_lp"}:
                raise CandidateEvidenceHistoryError(f"legacy Combo variant cannot be resolved: {symbol}")
            owner = variant
        out[(symbol, family)] = owner
    return out


def _validate_legacy_owner_bundle(
    *,
    authority_base: Path,
    run_id: str,
    account: str,
    account_config_sha256: str,
    index: Mapping[str, Any],
    owners_by_scope: Mapping[tuple[str, str], str],
    payloads: Mapping[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    expected_owners = sorted(set(owners_by_scope.values()))
    unexpected = sorted(set(payloads) - set(expected_owners))
    if unexpected:
        raise CandidateEvidenceHistoryError("legacy owner snapshot is unexpected: " + ",".join(unexpected))
    scopes_by_owner: dict[str, set[str]] = {owner: set() for owner in expected_owners}
    markets_by_owner: dict[str, set[str]] = {owner: set() for owner in expected_owners}
    for row in index.get("items") or []:
        key = (str(row["symbol"]), str(row["strategy_family"]))
        owner = owners_by_scope[key]
        scopes_by_owner[owner].add(str(row["symbol"]))
        markets_by_owner[owner].add(str(row["market"]))
    owners: dict[str, dict[str, Any]] = {}
    for owner in expected_owners:
        payload = dict(payloads[owner])
        if payload.get("schema_version") != _LEGACY_OWNER_SCHEMAS[owner]:
            raise CandidateEvidenceHistoryError(f"legacy owner schema mismatch: {owner}")
        if payload.get("account_config_sha256") != account_config_sha256:
            raise CandidateEvidenceHistoryError(f"legacy owner config mismatch: {owner}")
        if owner == "opening":
            loaded = load_opening_candidate_snapshot(
                base=authority_base,
                run_id=run_id,
                account=account,
            )
            modes = {
                "put" if family == "sell_put" else "call"
                for symbol, family in owners_by_scope
                if owners_by_scope[(symbol, family)] == owner
            }
            if set(loaded.get("strategy_modes") or []) != modes:
                raise CandidateEvidenceHistoryError("legacy opening strategy scope mismatch")
            expected_scopes = {
                (
                    str(row["symbol"]),
                    "put" if row["strategy_family"] == "sell_put" else "call",
                    str(row["status"]),
                )
                for row in index.get("items") or []
                if owners_by_scope[(str(row["symbol"]), str(row["strategy_family"]))] == owner
            }
            actual_scopes = {
                (
                    str(row.get("symbol") or "").upper(),
                    str(row.get("strategy_mode") or "").lower(),
                    str(row.get("status") or "").lower(),
                )
                for row in loaded.get("scope_results") or []
                if isinstance(row, Mapping) and row.get("scope") == "strategy"
            }
            if actual_scopes != expected_scopes:
                raise CandidateEvidenceHistoryError("legacy opening terminal scope mismatch")
            owners[owner] = loaded
            continue
        _validate_legacy_pair_snapshot(
            payload,
            owner=owner,
            run_id=run_id,
            account=account,
            symbols=scopes_by_owner[owner],
            markets=markets_by_owner[owner],
        )
        owners[owner] = payload
    return owners


def _validate_legacy_pair_snapshot(
    payload: Mapping[str, Any],
    *,
    owner: str,
    run_id: str,
    account: str,
    symbols: set[str],
    markets: set[str],
) -> None:
    if payload.get("run_id") != run_id or str(payload.get("account") or "").lower() != account:
        raise CandidateEvidenceHistoryError(f"legacy owner identity mismatch: {owner}")
    if len(markets) != 1 or str(payload.get("market") or "").upper() not in markets:
        raise CandidateEvidenceHistoryError(f"legacy owner market mismatch: {owner}")
    for field in ("account_config_sha256", "strategy_policy_sha256", "content_sha256"):
        _sha256(payload.get(field), f"legacy {owner} {field}")
    content_hash = str(payload["content_sha256"])
    content = {key: value for key, value in payload.items() if key != "content_sha256"}
    if canonical_sha256(content) != content_hash:
        raise CandidateEvidenceHistoryError(f"legacy owner content hash mismatch: {owner}")
    sealed_at = _required(payload.get("sealed_at_utc"), f"legacy {owner} sealed_at_utc")
    try:
        parsed = datetime.fromisoformat(sealed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CandidateEvidenceHistoryError(f"legacy owner sealed timestamp is invalid: {owner}") from exc
    if parsed.tzinfo is None:
        raise CandidateEvidenceHistoryError(f"legacy owner sealed timestamp has no timezone: {owner}")
    _required(payload.get("opening_status"), f"legacy {owner} opening_status")
    pairs = payload.get("ranked_pairs")
    rejects = payload.get("reject_reasons")
    if not isinstance(pairs, list) or any(not isinstance(row, Mapping) for row in pairs):
        raise CandidateEvidenceHistoryError(f"legacy owner pairs are invalid: {owner}")
    if not isinstance(rejects, list) or any(not isinstance(row, Mapping) for row in rejects):
        raise CandidateEvidenceHistoryError(f"legacy owner rejects are invalid: {owner}")
    seen: set[str] = set()
    for raw in pairs:
        pair_id = _required(raw.get("candidate_pair_id"), "legacy candidate_pair_id")
        symbol = _required(raw.get("symbol"), "legacy pair symbol").upper()
        if symbol not in symbols or pair_id in seen:
            raise CandidateEvidenceHistoryError(f"legacy owner pair scope is invalid: {owner}")
        seen.add(pair_id)


def _legacy_candidate_names(account_dir: Path) -> list[str]:
    if not account_dir.is_dir():
        return []
    names: list[str] = []
    for path in account_dir.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        name = path.name.lower()
        if name.endswith(_LEGACY_CANDIDATE_SUFFIXES):
            names.append(path.relative_to(account_dir).as_posix())
    return sorted(set(names))


def _has_other_scan_evidence(account_dir: Path) -> bool:
    if not account_dir.is_dir():
        return False
    evidence_names = {
        "candidate_filter_trace.jsonl",
        _LEGACY_INDEX_FILE,
        STRATEGY_SCAN_STATUS_INDEX_V2_FILE,
        STRATEGY_SCAN_STATUS_INDEX_V4_FILE,
    }
    for path in account_dir.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        name = path.name.lower()
        if (
            name in evidence_names
            or name.endswith("_scan_status.json")
            or name.endswith("_scan_status.v2.json")
        ):
            return True
    return False


def _required(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise CandidateEvidenceHistoryError(f"{field} is required")
    return text


def _runs_root_and_authority_base(
    *,
    base: Path,
    runs_root: Path | None,
) -> tuple[Path, Path]:
    root = Path(base).resolve()
    resolved = Path(runs_root).expanduser().resolve() if runs_root is not None else (root / "output_runs").resolve()
    if resolved.name != "output_runs":
        raise CandidateEvidenceHistoryError("candidate evidence runs_root must name an output_runs directory")
    return resolved, resolved.parent


def _sha256(value: Any, field: str) -> str:
    digest = str(value or "").strip().lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise CandidateEvidenceHistoryError(f"{field} is invalid")
    return digest


__all__ = [
    "AccountCandidateEvidence",
    "CANDIDATE_EVIDENCE_CLASSIFICATION_SCHEMA",
    "CANDIDATE_EVIDENCE_STATES",
    "CandidateEvidenceHistoryError",
    "NOT_SCANNED",
    "NON_CONTRIBUTING_EXPERIENCE",
    "SUPPORTED",
    "SUPPORTED_LIMITED_LEGACY_SNAPSHOT",
    "UNSUPPORTED_LEGACY_CSV_ONLY",
    "UNSUPPORTED_SNAPSHOT_MISSING",
    "UNSUPPORTED_SNAPSHOT_SCHEMA",
    "load_account_candidate_evidence",
    "load_candidate_snapshot_bundle_for_inspection",
    "load_historical_candidate_snapshot_bundle",
    "load_latest_candidate_snapshot_bundle_for_inspection",
    "load_run_candidate_evidence",
    "summarize_run_candidate_evidence",
    "validate_historical_candidate_snapshot_bundle_bytes",
]
