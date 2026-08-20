from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.candidate_snapshot_contract import (
    CandidateSnapshotContractError,
    required_text,
    sha256_text,
    utc_timestamp,
)
from src.application.cc_lp_candidate_snapshot import (
    CC_LP_CANDIDATE_SNAPSHOT_FILE,
    CC_LP_CANDIDATE_SNAPSHOT_SCHEMA,
    CcLpCandidateSnapshotError,
    load_cc_lp_candidate_snapshot,
)
from src.application.combo_yield_candidate_snapshot import (
    COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE,
    COMBO_YIELD_CANDIDATE_SNAPSHOT_SCHEMA,
    ComboYieldCandidateSnapshotError,
    load_combo_yield_candidate_snapshot,
)
from src.application.opening_candidate_snapshot import (
    OPENING_CANDIDATE_SNAPSHOT_FILE,
    OPENING_CANDIDATE_SNAPSHOT_SCHEMA,
    OpeningCandidateSnapshotError,
    load_opening_candidate_snapshot,
)
from src.application.wheel.candidate_snapshot import (
    WHEEL_CANDIDATE_SNAPSHOT_FILE,
    WHEEL_CANDIDATE_SNAPSHOT_SCHEMA,
    WheelCandidateSnapshotError,
    load_wheel_candidate_snapshot,
)
from src.application.source_receipts import sha256_bytes
from src.application.strategy_scan_status import (
    STRATEGY_SCAN_STATUS_INDEX_V2_FILE,
    STRATEGY_SCAN_STATUS_INDEX_V2_SCHEMA,
    StrategyScanStatusError,
    load_strategy_scan_status_index_v2,
)
from src.application.tick_run_workspace import (
    AccountRunConfigError,
    read_account_run_state_bytes_safely,
    write_account_run_state_bytes_once_safely,
)


CANDIDATE_SNAPSHOT_MANIFEST_SCHEMA = "candidate_snapshot_manifest.v1"
CANDIDATE_SNAPSHOT_MANIFEST_FILE = "candidate_snapshot_manifest.v1.json"
_OWNER_FILES = {
    "opening": OPENING_CANDIDATE_SNAPSHOT_FILE,
    "sp_lc": COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE,
    "cc_lp": CC_LP_CANDIDATE_SNAPSHOT_FILE,
    "wheel": WHEEL_CANDIDATE_SNAPSHOT_FILE,
}
_OWNER_SCHEMAS = {
    "opening": OPENING_CANDIDATE_SNAPSHOT_SCHEMA,
    "sp_lc": COMBO_YIELD_CANDIDATE_SNAPSHOT_SCHEMA,
    "cc_lp": CC_LP_CANDIDATE_SNAPSHOT_SCHEMA,
    "wheel": WHEEL_CANDIDATE_SNAPSHOT_SCHEMA,
}


class CandidateSnapshotManifestError(RuntimeError):
    """Raised when an account-run candidate commit cannot be trusted."""


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


def _run_account_dir(base: Path, run_id: str, account: str) -> Path:
    return (
        Path(base).resolve()
        / "output_runs"
        / run_id
        / "accounts"
        / account
    )


def _scope_projection(row: Mapping[str, Any]) -> dict[str, str]:
    return {
        "market": required_text(row.get("market"), "scope market").upper(),
        "symbol": required_text(row.get("symbol"), "scope symbol").upper(),
        "strategy_family": required_text(
            row.get("strategy_family"),
            "scope strategy_family",
        ).lower(),
        "strategy_mode": required_text(row.get("strategy_mode"), "scope strategy_mode").lower(),
        "candidate_owner": required_text(row.get("candidate_owner"), "scope candidate_owner").lower(),
    }


def _expected_scopes(index: Mapping[str, Any]) -> list[dict[str, str]]:
    return sorted(
        (_scope_projection(row) for row in index.get("items") or []),
        key=lambda row: (
            row["market"],
            row["symbol"],
            row["strategy_family"],
        ),
    )


def _snapshot_strategy_scopes(
    snapshot: Mapping[str, Any],
    *,
    owner: str,
    index_items: list[Mapping[str, Any]],
) -> list[dict[str, str]]:
    expected_items = [
        dict(row)
        for row in index_items
        if str(row.get("candidate_owner") or "").strip().lower() == owner
    ]
    expected = [_scope_projection(row) for row in expected_items]
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
        (row["symbol"], row["strategy_mode"]): raw
        for row, raw in zip(expected, expected_items, strict=True)
    }
    snapshot_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in snapshot.get("scope_results") or []:
        if not isinstance(raw, Mapping) or raw.get("scope") != "strategy":
            continue
        row = dict(raw)
        key = (
            str(row.get("symbol") or "").strip().upper(),
            str(row.get("strategy_mode") or "").strip().lower(),
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
    candidate_counts: dict[tuple[str, str], int] = {
        key: 0 for key in expected_by_key
    }
    if owner == "opening":
        selected_rows = snapshot.get("ranked_candidates") or []
    elif owner == "wheel":
        selected_rows = [
            candidate
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
            )
        elif owner == "wheel":
            key = (
                str(raw.get("symbol") or "").strip().upper(),
                "wheel",
            )
        else:
            key = (
                str(raw.get("symbol") or "").strip().upper(),
                "combo_yield",
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
        return load_strategy_scan_status_index_v2(
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
) -> None:
    expected = set(expected_owners)
    unexpected = sorted(
        owner
        for owner, filename in _OWNER_FILES.items()
        if owner not in expected and (account_dir / "state" / filename).exists()
    )
    if unexpected:
        raise CandidateSnapshotManifestError(
            "candidate owner snapshot is unexpected: " + ",".join(unexpected)
        )


def publish_candidate_snapshot_manifest(
    *,
    base: Path,
    run_id: str,
    account: str,
    strategy_policy_sha256: str,
    sealed_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Commit all expected owner snapshots after every bound artifact validates."""

    try:
        run_id_norm = required_text(run_id, "run_id")
        account_norm = required_text(account, "account").lower()
        policy_hash = sha256_text(strategy_policy_sha256, "strategy_policy_sha256")
        seal_time = utc_timestamp(sealed_at or datetime.now(timezone.utc))
    except CandidateSnapshotContractError as exc:
        raise CandidateSnapshotManifestError(str(exc)) from exc
    account_dir = _run_account_dir(base, run_id_norm, account_norm)
    index_path = account_dir / STRATEGY_SCAN_STATUS_INDEX_V2_FILE
    index = _load_status_index(
        index_path,
        run_id=run_id_norm,
        account=account_norm,
    )
    config_hash = str(index["account_config_sha256"])
    scopes = _expected_scopes(index)
    expected_owners = sorted({row["candidate_owner"] for row in scopes})
    _assert_exact_owner_files(
        account_dir,
        expected_owners=expected_owners,
    )
    owner_entries: list[dict[str, Any]] = []
    for owner in expected_owners:
        snapshot = _load_owner_snapshot(
            base=Path(base),
            run_id=run_id_norm,
            account=account_norm,
            owner=owner,
        )
        if snapshot.get("schema_version") != _OWNER_SCHEMAS[owner]:
            raise CandidateSnapshotManifestError(
                f"candidate owner snapshot schema mismatch: {owner}"
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
        )
        relpath = f"state/{_OWNER_FILES[owner]}"
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
            }
        )
    payload: dict[str, Any] = {
        "schema_version": CANDIDATE_SNAPSHOT_MANIFEST_SCHEMA,
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
            "schema_version": STRATEGY_SCAN_STATUS_INDEX_V2_SCHEMA,
            "relpath": STRATEGY_SCAN_STATUS_INDEX_V2_FILE,
            "sha256": sha256_bytes(index_path.read_bytes()),
            "content_sha256": index["content_sha256"],
        },
        "owner_snapshots": owner_entries,
    }
    payload["content_sha256"] = canonical_sha256(payload)
    validate_candidate_snapshot_manifest(
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
            name=CANDIDATE_SNAPSHOT_MANIFEST_FILE,
            payload=encoded,
        )
    except AccountRunConfigError as exc:
        raise CandidateSnapshotManifestError(
            "terminal candidate snapshot manifest conflicts or cannot be published"
        ) from exc
    adopted = load_candidate_snapshot_bundle(
        base=Path(base),
        run_id=run_id_norm,
        account=account_norm,
    )["manifest"]
    if adopted != payload:
        raise CandidateSnapshotManifestError("candidate snapshot manifest adoption mismatch")
    return adopted


def validate_candidate_snapshot_manifest(
    payload: Mapping[str, Any],
    *,
    expected_run_id: str,
    expected_account: str,
) -> None:
    try:
        item = dict(payload or {})
        if item.get("schema_version") != CANDIDATE_SNAPSHOT_MANIFEST_SCHEMA:
            raise CandidateSnapshotManifestError("candidate snapshot manifest schema mismatch")
        if item.get("run_id") != expected_run_id:
            raise CandidateSnapshotManifestError("candidate snapshot manifest run mismatch")
        if item.get("account") != expected_account:
            raise CandidateSnapshotManifestError("candidate snapshot manifest account mismatch")
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
        projected = [_scope_projection(row) for row in scopes]
        if projected != sorted(
            projected,
            key=lambda row: (row["market"], row["symbol"], row["strategy_family"]),
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
        if index.get("schema_version") != STRATEGY_SCAN_STATUS_INDEX_V2_SCHEMA:
            raise CandidateSnapshotManifestError("candidate manifest status index schema mismatch")
        if index.get("relpath") != STRATEGY_SCAN_STATUS_INDEX_V2_FILE:
            raise CandidateSnapshotManifestError("candidate manifest status index path mismatch")
        sha256_text(index.get("sha256"), "status index sha256")
        sha256_text(index.get("content_sha256"), "status index content_sha256")
        for entry in entries:
            owner = str(entry.get("candidate_owner") or "")
            if owner not in _OWNER_FILES:
                raise CandidateSnapshotManifestError("candidate manifest owner is invalid")
            if entry.get("schema_version") != _OWNER_SCHEMAS[owner]:
                raise CandidateSnapshotManifestError("candidate manifest owner schema mismatch")
            if entry.get("relpath") != f"state/{_OWNER_FILES[owner]}":
                raise CandidateSnapshotManifestError("candidate manifest owner path mismatch")
            sha256_text(entry.get("sha256"), f"{owner} snapshot sha256")
            sha256_text(entry.get("content_sha256"), f"{owner} snapshot content_sha256")
            required_text(entry.get("opening_status"), f"{owner} opening_status")
            covered = entry.get("covered_scopes")
            expected = [row for row in projected if row["candidate_owner"] == owner]
            if covered != expected:
                raise CandidateSnapshotManifestError("candidate manifest covered scopes mismatch")
    except CandidateSnapshotContractError as exc:
        raise CandidateSnapshotManifestError(str(exc)) from exc


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
        encoded = read_account_run_state_bytes_safely(
            base=Path(base),
            run_id=run_id_norm,
            account=account_norm,
            name=CANDIDATE_SNAPSHOT_MANIFEST_FILE,
        )
        manifest = json.loads(encoded.decode("utf-8"))
    except Exception as exc:
        raise CandidateSnapshotManifestError("candidate snapshot manifest is unavailable") from exc
    if not isinstance(manifest, dict):
        raise CandidateSnapshotManifestError("candidate snapshot manifest must be an object")
    validate_candidate_snapshot_manifest(
        manifest,
        expected_run_id=run_id_norm,
        expected_account=account_norm,
    )
    account_dir = _run_account_dir(base, run_id_norm, account_norm)
    index_binding = dict(manifest["status_index"])
    index_path = account_dir / str(index_binding["relpath"])
    if not index_path.is_file() or index_path.is_symlink():
        raise CandidateSnapshotManifestError("candidate status index is unavailable")
    if sha256_bytes(index_path.read_bytes()) != index_binding["sha256"]:
        raise CandidateSnapshotManifestError("candidate status index hash mismatch")
    index = _load_status_index(
        index_path,
        run_id=run_id_norm,
        account=account_norm,
        account_config_sha256=str(manifest["account_config_sha256"]),
    )
    if index.get("content_sha256") != index_binding["content_sha256"]:
        raise CandidateSnapshotManifestError("candidate status index content binding mismatch")
    scopes = _expected_scopes(index)
    if scopes != manifest["expected_scopes"]:
        raise CandidateSnapshotManifestError("candidate status index scope binding mismatch")
    _assert_exact_owner_files(
        account_dir,
        expected_owners=list(manifest["expected_owners"]),
    )

    owners: dict[str, dict[str, Any]] = {}
    for raw_entry in manifest["owner_snapshots"]:
        entry = dict(raw_entry)
        owner = str(entry["candidate_owner"])
        snapshot_path = account_dir / str(entry["relpath"])
        if not snapshot_path.is_file() or snapshot_path.is_symlink():
            raise CandidateSnapshotManifestError(
                f"candidate owner snapshot is unavailable: {owner}"
            )
        if sha256_bytes(snapshot_path.read_bytes()) != entry["sha256"]:
            raise CandidateSnapshotManifestError(
                f"candidate owner snapshot hash mismatch: {owner}"
            )
        snapshot = _load_owner_snapshot(
            base=Path(base),
            run_id=run_id_norm,
            account=account_norm,
            owner=owner,
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
        covered = _snapshot_strategy_scopes(
            snapshot,
            owner=owner,
            index_items=list(index.get("items") or []),
        )
        if covered != entry["covered_scopes"]:
            raise CandidateSnapshotManifestError(
                f"candidate owner scope binding mismatch: {owner}"
            )
        if snapshot.get("opening_status") != entry.get("opening_status"):
            raise CandidateSnapshotManifestError(
                f"candidate owner status binding mismatch: {owner}"
            )
        owners[owner] = snapshot
    if sorted(owners) != manifest["expected_owners"]:
        raise CandidateSnapshotManifestError("candidate owner bundle is incomplete")
    return {"manifest": manifest, "status_index": index, "owners": owners}


def load_latest_candidate_snapshot_bundle(
    *,
    base: Path,
    account: str,
) -> dict[str, Any]:
    """Resolve one account's latest run and require its terminal manifest."""

    root = Path(base).resolve()
    try:
        account_norm = required_text(account, "account").lower()
    except CandidateSnapshotContractError as exc:
        raise CandidateSnapshotManifestError(str(exc)) from exc
    runs_root_path = root / "output_runs"
    if runs_root_path.is_symlink():
        raise CandidateSnapshotManifestError("output_runs may not be a symlink")
    runs_root = runs_root_path.resolve()
    pointer = root / "output_shared" / "state" / "last_run_dir.txt"
    if pointer.is_symlink():
        raise CandidateSnapshotManifestError("last-run pointer may not be a symlink")
    if pointer.exists():
        if not pointer.is_file():
            raise CandidateSnapshotManifestError("last-run pointer is invalid")
        try:
            pointed = Path(pointer.read_text(encoding="utf-8").strip())
        except OSError as exc:
            raise CandidateSnapshotManifestError("last-run pointer is unreadable") from exc
        if not pointed.is_absolute():
            pointed = (root / pointed).resolve()
        else:
            pointed = pointed.resolve()
        if pointed.parent != runs_root:
            raise CandidateSnapshotManifestError("last-run pointer is outside output_runs")
        return load_candidate_snapshot_bundle(
            base=root,
            run_id=pointed.name,
            account=account_norm,
        )

    if not runs_root.is_dir() or runs_root.is_symlink():
        raise CandidateSnapshotManifestError("no output runs are available")
    try:
        candidates = sorted(
            (
                item
                for item in runs_root.iterdir()
                if item.is_dir() and not item.is_symlink()
            ),
            key=lambda item: (item.stat().st_mtime_ns, item.name),
            reverse=True,
        )
    except OSError as exc:
        raise CandidateSnapshotManifestError("output runs are unreadable") from exc
    for run_dir in candidates:
        account_dir = run_dir / "accounts" / account_norm
        if account_dir.is_symlink():
            raise CandidateSnapshotManifestError("latest account run may not be a symlink")
        if not account_dir.is_dir():
            continue
        # The newest run containing the requested account is authoritative.
        # Do not skip an incomplete run and silently return stale evidence.
        return load_candidate_snapshot_bundle(
            base=root,
            run_id=run_dir.name,
            account=account_norm,
        )
    raise CandidateSnapshotManifestError(
        f"no candidate run is available for account {account_norm}"
    )


__all__ = [
    "CANDIDATE_SNAPSHOT_MANIFEST_FILE",
    "CANDIDATE_SNAPSHOT_MANIFEST_SCHEMA",
    "CandidateSnapshotManifestError",
    "load_candidate_snapshot_bundle",
    "load_latest_candidate_snapshot_bundle",
    "publish_candidate_snapshot_manifest",
    "validate_candidate_snapshot_manifest",
]
