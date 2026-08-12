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
    CandidateSnapshotManifestError,
    load_candidate_snapshot_bundle,
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
    STRATEGY_SCAN_STATUS_INDEX_SCHEMA,
    STRATEGY_SCAN_STATUS_INDEX_V2_FILE,
)
from src.application.tick_run_workspace import (
    ACCOUNT_RUN_CONFIG_NAME,
    AccountRunConfigError,
    account_run_config_paths,
    canonical_account_run_config_bytes,
    load_published_account_run_config,
    read_account_run_state_bytes_safely,
)
from src.application.yield_enhancement_config import resolve_yield_enhancement_cfg


CANDIDATE_EVIDENCE_CLASSIFICATION_SCHEMA = "candidate_evidence_compatibility.v1"
SUPPORTED = "supported"
SUPPORTED_LIMITED_LEGACY_SNAPSHOT = "supported_limited_legacy_snapshot"
UNSUPPORTED_LEGACY_CSV_ONLY = "unsupported_legacy_csv_only"
UNSUPPORTED_SNAPSHOT_MISSING = "unsupported_snapshot_missing"
UNSUPPORTED_SNAPSHOT_SCHEMA = "unsupported_snapshot_schema"
NOT_SCANNED = "not_scanned"
CANDIDATE_EVIDENCE_STATES = frozenset(
    {
        SUPPORTED,
        SUPPORTED_LIMITED_LEGACY_SNAPSHOT,
        UNSUPPORTED_LEGACY_CSV_ONLY,
        UNSUPPORTED_SNAPSHOT_MISSING,
        UNSUPPORTED_SNAPSHOT_SCHEMA,
        NOT_SCANNED,
    }
)

_LEGACY_COMBO_SCHEMA = "combo_yield_candidate_snapshot.v1"
_LEGACY_CC_LP_SCHEMA = "cc_lp_candidate_snapshot.v1"
_LEGACY_INDEX_FILE = "strategy_scan_status_index.v1.json"
_OWNER_FILES = {
    "opening": OPENING_CANDIDATE_SNAPSHOT_FILE,
    "sp_lc": COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE,
    "cc_lp": CC_LP_CANDIDATE_SNAPSHOT_FILE,
}
_MODERN_OWNER_SCHEMAS = {
    "opening": OPENING_CANDIDATE_SNAPSHOT_SCHEMA,
    "sp_lc": COMBO_YIELD_CANDIDATE_SNAPSHOT_SCHEMA,
    "cc_lp": CC_LP_CANDIDATE_SNAPSHOT_SCHEMA,
}
_LEGACY_OWNER_SCHEMAS = {
    "opening": OPENING_CANDIDATE_SNAPSHOT_SCHEMA,
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

    manifest_path = state_dir / CANDIDATE_SNAPSHOT_MANIFEST_FILE
    if manifest_path.exists():
        try:
            bundle = load_candidate_snapshot_bundle(
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
        return _result(
            account_dir=account_dir,
            common=common,
            status=SUPPORTED,
            reason_code="candidate_snapshot_manifest_valid",
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
    if (account_dir / STRATEGY_SCAN_STATUS_INDEX_V2_FILE).exists() or any(
        payload.get("schema_version") in set(_MODERN_OWNER_SCHEMAS.values()) - {OPENING_CANDIDATE_SNAPSHOT_SCHEMA}
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
                    "strict_replay_authority_unavailable",
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
        "strict_replay_authority": strict,
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
        "strict_replay_authority": status == SUPPORTED,
        "contributes_snapshot_facts": status in {SUPPORTED, SUPPORTED_LIMITED_LEGACY_SNAPSHOT},
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
    if payload.get("schema_version") != STRATEGY_SCAN_STATUS_INDEX_SCHEMA:
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
    state_path, compatibility_path = account_run_config_paths(
        base=base,
        run_id=run_id,
        account=account,
    )
    config = load_published_account_run_config(
        base=base,
        run_id=run_id,
        account=account,
        state_path=state_path,
        compatibility_path=compatibility_path,
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
            combo_cfg = resolve_yield_enhancement_cfg(symbol_cfg)
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
    }
    for path in account_dir.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        name = path.name.lower()
        if name in evidence_names or name.endswith("_scan_status.json"):
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
    "SUPPORTED",
    "SUPPORTED_LIMITED_LEGACY_SNAPSHOT",
    "UNSUPPORTED_LEGACY_CSV_ONLY",
    "UNSUPPORTED_SNAPSHOT_MISSING",
    "UNSUPPORTED_SNAPSHOT_SCHEMA",
    "load_account_candidate_evidence",
    "load_run_candidate_evidence",
    "summarize_run_candidate_evidence",
]
