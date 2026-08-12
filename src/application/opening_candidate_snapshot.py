from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.engine import (
    evaluate_opening_candidate_policy,
    explain_candidate_rank,
    rank_candidate_rows,
    validate_candidate_decision_payload,
)
from domain.domain.engine.candidate_engine import (
    EARNINGS_NEAR_EXPIRY_POLICY_VERSION,
    EARNINGS_NEAR_EXPIRY_WINDOW_DAYS,
    REJECT_EVIDENCE_UNAVAILABLE,
    REJECT_INPUT_INVALID,
    REJECT_INPUT_MISSING,
    REJECT_RISK_EARNINGS_UNAVAILABLE,
)
from domain.domain.symbol_identity import symbol_market
from src.application.tick_run_workspace import (
    AccountRunConfigError,
    read_account_run_state_bytes_safely,
    write_account_run_state_bytes_once_safely,
)


OPENING_CANDIDATE_SNAPSHOT_SCHEMA = "opening_candidate_snapshot.v1"
OPENING_CANDIDATE_SNAPSHOT_FILE = "opening_candidate_snapshot.json"
OPENING_STATUSES = frozenset(
    {
        "candidates_found",
        "no_candidate",
        "data_unavailable",
        "partial_data",
        "market_closed",
    }
)
STRATEGY_STATUSES = frozenset(
    {
        "candidates_found",
        "no_candidate",
        "data_unavailable",
        "partial_data",
        "not_applicable",
    }
)
_CLEAN_NO_CANDIDATE_REASONS = frozenset(
    {
        "no_candidate",
        "market_closed",
        "no_expirations",
        "no_contract_rows",
        "covered_call_underlying_not_held",
        "",
    }
)
_BENIGN_ACCOUNT_NOT_APPLICABLE_REASONS = frozenset(
    {"covered_call_underlying_not_held"}
)


class OpeningCandidateSnapshotError(RuntimeError):
    """Raised when an opening-candidate snapshot cannot be trusted."""


def strategy_policy_hash(config: Mapping[str, Any]) -> str:
    """Hash only the authored inputs that can change opening policy."""

    cfg = dict(config or {})
    return canonical_sha256(
        {
            "schema": "opening_candidate_strategy_policy.v2",
            "earnings_policy": {
                "version": EARNINGS_NEAR_EXPIRY_POLICY_VERSION,
                "near_expiry_window_days": EARNINGS_NEAR_EXPIRY_WINDOW_DAYS,
            },
            "templates": cfg.get("templates"),
            "profiles": cfg.get("profiles"),
            "symbols": cfg.get("symbols"),
            "watchlist": cfg.get("watchlist"),
            "outputs": cfg.get("outputs"),
        }
    )


def seal_opening_candidate_snapshot(
    *,
    base: Path,
    run_id: str,
    account: str,
    market: str,
    physical_account: Mapping[str, Any],
    account_config_sha256: str,
    strategy_policy_sha256: str,
    dependencies: Iterable[Mapping[str, Any]],
    scan_statuses: Iterable[Mapping[str, Any]],
    final_candidates: Mapping[str, Iterable[Mapping[str, Any]]],
    candidate_evaluations: (
        Mapping[str, Iterable[Mapping[str, Any]]] | None
    ) = None,
    sealed_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Assemble, validate, and immutably publish one account-run snapshot."""

    run_id_norm = _required_text(run_id, "run_id")
    account_norm = _required_text(account, "account").lower()
    market_norm = _market(market)
    account_config_hash = _sha256(account_config_sha256, "account_config_sha256")
    policy_hash = _sha256(strategy_policy_sha256, "strategy_policy_hash")
    authority = _physical_account(physical_account, account=account_norm, market=market_norm)
    dependency_rows = _dependencies(dependencies)
    statuses = _scan_statuses(scan_statuses)
    modes = sorted({str(item["strategy_mode"]) for item in statuses})
    if not modes:
        raise OpeningCandidateSnapshotError("opening candidate strategy modes are missing")

    decisions, decision_index = _snapshot_decisions(
        final_candidates,
        statuses=statuses,
        account=account_norm,
        futu_account_id=str(authority["futu_account_id"]),
        market=market_norm,
        risk_policy_hash=policy_hash,
        candidate_evaluations=candidate_evaluations,
    )
    ranked = _ranked_candidates(
        final_candidates,
        modes=modes,
        decision_index=decision_index,
        account=account_norm,
        futu_account_id=str(authority["futu_account_id"]),
        market=market_norm,
    )
    accepted_decision_ids = {
        str(item["candidate_id"])
        for item in decisions
        if bool((item.get("opening_decision") or {}).get("accepted"))
    }
    ranked_candidate_ids = {str(item["candidate_id"]) for item in ranked}
    if accepted_decision_ids != ranked_candidate_ids:
        raise OpeningCandidateSnapshotError(
            "accepted opening decisions and final candidates do not match"
        )
    strategy_results = _strategy_results(
        modes=modes,
        statuses=statuses,
        ranked=ranked,
        decisions=decisions,
        authority=authority,
    )
    opening_status = _opening_status(
        strategy_results,
        scan_statuses=statuses,
    )
    scope_results = _scope_results(statuses=statuses, decisions=decisions)
    seal_time = _timestamp(sealed_at or datetime.now(timezone.utc))

    payload: dict[str, Any] = {
        "schema_version": OPENING_CANDIDATE_SNAPSHOT_SCHEMA,
        "run_id": run_id_norm,
        "account": account_norm,
        "futu_account_id": authority["futu_account_id"],
        "trade_env": authority["trd_env"],
        "market": market_norm,
        "strategy_modes": modes,
        "account_config_sha256": account_config_hash,
        "strategy_policy_sha256": policy_hash,
        "required_data_manifest_sha256": _dependency_hash(
            dependency_rows,
            "required_data",
        ),
        "dependencies": dependency_rows,
        "sealed_at_utc": seal_time,
        "opening_status": opening_status,
        "strategy_results": strategy_results,
        "scope_results": scope_results,
        "candidate_decisions": decisions,
        "ranked_candidates": ranked,
    }
    payload["content_sha256"] = canonical_sha256(payload)
    validate_opening_candidate_snapshot(
        payload,
        expected_run_id=run_id_norm,
        expected_account=account_norm,
        verify_dependency_root=Path(base).resolve(),
    )
    encoded = _canonical_json_bytes(payload)
    try:
        write_account_run_state_bytes_once_safely(
            base=Path(base),
            run_id=run_id_norm,
            account=account_norm,
            name=OPENING_CANDIDATE_SNAPSHOT_FILE,
            payload=encoded,
        )
    except AccountRunConfigError as exc:
        raise OpeningCandidateSnapshotError(
            "terminal opening candidate snapshot conflicts or cannot be published"
        ) from exc
    adopted = load_opening_candidate_snapshot(
        base=Path(base),
        run_id=run_id_norm,
        account=account_norm,
    )
    if adopted != payload:
        raise OpeningCandidateSnapshotError("opening candidate snapshot adoption mismatch")
    return adopted


def load_opening_candidate_snapshot(
    *,
    base: Path,
    run_id: str,
    account: str,
) -> dict[str, Any]:
    run_id_norm = _required_text(run_id, "run_id")
    account_norm = _required_text(account, "account").lower()
    try:
        encoded = read_account_run_state_bytes_safely(
            base=Path(base),
            run_id=run_id_norm,
            account=account_norm,
            name=OPENING_CANDIDATE_SNAPSHOT_FILE,
        )
        payload = json.loads(encoded.decode("utf-8"))
    except Exception as exc:
        raise OpeningCandidateSnapshotError(
            "opening candidate snapshot is unavailable"
        ) from exc
    if not isinstance(payload, dict):
        raise OpeningCandidateSnapshotError("opening candidate snapshot must be an object")
    validate_opening_candidate_snapshot(
        payload,
        expected_run_id=run_id_norm,
        expected_account=account_norm,
        verify_dependency_root=Path(base).resolve(),
    )
    return payload


def load_latest_opening_candidate_snapshot(
    *,
    base: Path,
    account: str,
) -> dict[str, Any]:
    """Resolve the latest sealed snapshot without falling back past a bad pointer."""

    root = Path(base).resolve()
    account_norm = _required_text(account, "account").lower()
    pointer = root / "output_shared" / "state" / "last_run_dir.txt"
    if pointer.exists():
        if pointer.is_symlink():
            raise OpeningCandidateSnapshotError("last-run pointer may not be a symlink")
        try:
            pointed = Path(pointer.read_text(encoding="utf-8").strip())
        except OSError as exc:
            raise OpeningCandidateSnapshotError("last-run pointer is unreadable") from exc
        if not pointed.is_absolute():
            pointed = (root / pointed).resolve()
        else:
            pointed = pointed.resolve()
        runs_root = (root / "output_runs").resolve()
        if pointed.parent != runs_root:
            raise OpeningCandidateSnapshotError("last-run pointer is outside output_runs")
        return load_opening_candidate_snapshot(
            base=root,
            run_id=pointed.name,
            account=account_norm,
        )

    runs_root = root / "output_runs"
    if not runs_root.is_dir():
        raise OpeningCandidateSnapshotError("no output runs are available")
    candidates = sorted(
        (item for item in runs_root.iterdir() if item.is_dir() and not item.is_symlink()),
        key=lambda item: (item.stat().st_mtime_ns, item.name),
        reverse=True,
    )
    for run_dir in candidates:
        snapshot_path = (
            run_dir
            / "accounts"
            / account_norm
            / "state"
            / OPENING_CANDIDATE_SNAPSHOT_FILE
        )
        if not snapshot_path.is_file() or snapshot_path.is_symlink():
            continue
        return load_opening_candidate_snapshot(
            base=root,
            run_id=run_dir.name,
            account=account_norm,
        )
    raise OpeningCandidateSnapshotError(
        f"no sealed opening candidate snapshot is available for account {account_norm}"
    )


def ranked_opening_candidates(
    snapshot: Mapping[str, Any],
    *,
    mode: str | None = None,
) -> list[dict[str, Any]]:
    """Project the sealed order without re-ranking or filling missing facts."""

    mode_norm = _mode(mode) if mode is not None else None
    return [
        dict(item)
        for item in snapshot.get("ranked_candidates") or []
        if isinstance(item, Mapping)
        and (mode_norm is None or item.get("strategy_mode") == mode_norm)
    ]


def ranked_opening_candidate_decisions(
    snapshot: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Project accepted decision facts in exactly the sealed candidate order."""

    decision_by_id = {
        str(item.get("candidate_id") or ""): dict(item)
        for item in snapshot.get("candidate_decisions") or []
        if isinstance(item, Mapping)
    }
    out: list[dict[str, Any]] = []
    for ranked in ranked_opening_candidates(snapshot):
        candidate_id = str(ranked.get("candidate_id") or "")
        decision = decision_by_id.get(candidate_id)
        if decision is None:
            raise OpeningCandidateSnapshotError(
                "ranked opening candidate decision is unavailable"
            )
        out.append(
            {
                **decision,
                "opening_snapshot_rank": ranked.get("rank"),
            }
        )
    return out


def validate_opening_candidate_snapshot(
    payload: Mapping[str, Any],
    *,
    expected_run_id: str,
    expected_account: str,
    verify_dependency_root: Path | None = None,
) -> None:
    item = dict(payload or {})
    if item.get("schema_version") != OPENING_CANDIDATE_SNAPSHOT_SCHEMA:
        raise OpeningCandidateSnapshotError("opening candidate snapshot schema mismatch")
    if item.get("run_id") != expected_run_id:
        raise OpeningCandidateSnapshotError("opening candidate snapshot run mismatch")
    if item.get("account") != expected_account:
        raise OpeningCandidateSnapshotError("opening candidate snapshot account mismatch")
    for field in (
        "account_config_sha256",
        "strategy_policy_sha256",
        "required_data_manifest_sha256",
        "content_sha256",
    ):
        _sha256(item.get(field), field)
    content_hash = str(item["content_sha256"])
    content = {key: value for key, value in item.items() if key != "content_sha256"}
    if canonical_sha256(content) != content_hash:
        raise OpeningCandidateSnapshotError("opening candidate snapshot content hash mismatch")
    _timestamp(item.get("sealed_at_utc"))
    if item.get("opening_status") not in OPENING_STATUSES:
        raise OpeningCandidateSnapshotError("opening candidate snapshot status is invalid")
    authority = _physical_account(
        {
            "status": "available",
            "logical_account": item.get("account"),
            "futu_account_id": item.get("futu_account_id"),
            "trd_env": item.get("trade_env"),
            "market": item.get("market"),
            "source": "opend",
        },
        account=expected_account,
        market=_market(item.get("market")),
    )
    _ = authority
    dependencies = _dependencies(item.get("dependencies") or [])
    if _dependency_hash(dependencies, "required_data") != item.get(
        "required_data_manifest_sha256"
    ):
        raise OpeningCandidateSnapshotError("required-data dependency hash mismatch")
    if verify_dependency_root is not None:
        _verify_dependency_files(
            dependencies,
            base=Path(verify_dependency_root).resolve(),
            run_id=expected_run_id,
            account=expected_account,
        )
    modes = item.get("strategy_modes")
    if not isinstance(modes, list) or not modes or any(
        mode not in {"put", "call"} for mode in modes
    ):
        raise OpeningCandidateSnapshotError("opening candidate strategy modes are invalid")
    results = item.get("strategy_results")
    if (
        not isinstance(results, list)
        or any(not isinstance(row, Mapping) for row in results)
        or {row.get("strategy_mode") for row in results} != set(modes)
    ):
        raise OpeningCandidateSnapshotError(
            "opening candidate strategy results are invalid"
        )
    for row in results:
        if row.get("strategy_status") not in STRATEGY_STATUSES:
            raise OpeningCandidateSnapshotError("opening candidate strategy status is invalid")
    ranked = item.get("ranked_candidates")
    decisions = item.get("candidate_decisions")
    scopes = item.get("scope_results")
    if (
        not isinstance(ranked, list)
        or not isinstance(decisions, list)
        or not isinstance(scopes, list)
        or any(not isinstance(row, Mapping) for row in ranked)
        or any(not isinstance(row, Mapping) for row in decisions)
        or any(not isinstance(row, Mapping) for row in scopes)
    ):
        raise OpeningCandidateSnapshotError("opening candidate snapshot collections are invalid")
    candidate_ids = [str(row.get("candidate_id") or "") for row in decisions]
    if any(not value for value in candidate_ids) or len(candidate_ids) != len(set(candidate_ids)):
        raise OpeningCandidateSnapshotError("opening candidate identities are invalid")
    decision_by_id: dict[str, dict[str, Any]] = {}
    validated_decision_ids: set[str] = set()
    accepted_decision_ids: set[str] = set()
    for raw_decision in decisions:
        decision = dict(raw_decision)
        candidate_id = str(decision.get("candidate_id") or "")
        decision_by_id[candidate_id] = decision
        # opening_candidate_snapshot.v1 historically allowed a minimal
        # candidate-id record for read-only ranked projections. New seals use
        # the complete opening_candidate_decision.v1 contract below.
        if decision.get("schema_version") != "opening_candidate_decision.v1":
            continue
        mode = _mode(decision.get("strategy_mode"))
        normalized = decision.get("normalized_input")
        if not isinstance(normalized, dict):
            raise OpeningCandidateSnapshotError(
                "opening candidate normalized input is invalid"
            )
        try:
            opening = validate_candidate_decision_payload(
                dict(decision.get("opening_decision") or {})
            )
        except (TypeError, ValueError) as exc:
            raise OpeningCandidateSnapshotError(
                "opening candidate decision payload is invalid"
            ) from exc
        if opening.get("mode") != mode:
            raise OpeningCandidateSnapshotError(
                "opening candidate decision mode mismatch"
            )
        if canonical_sha256(normalized) != decision.get("normalized_input_hash"):
            raise OpeningCandidateSnapshotError(
                "opening candidate normalized input hash mismatch"
            )
        if canonical_sha256(opening) != decision.get("decision_hash"):
            raise OpeningCandidateSnapshotError(
                "opening candidate decision hash mismatch"
            )
        if decision.get("risk_policy_hash") != item.get("strategy_policy_sha256"):
            raise OpeningCandidateSnapshotError(
                "opening candidate policy binding mismatch"
            )
        if candidate_id != _bound_candidate_id(
            account=expected_account,
            futu_account_id=str(item.get("futu_account_id") or ""),
            market=_market(item.get("market")),
            mode=mode,
            normalized=normalized,
        ):
            raise OpeningCandidateSnapshotError(
                "opening candidate account identity mismatch"
            )
        validated_decision_ids.add(candidate_id)
        if opening.get("accepted") is True:
            accepted_decision_ids.add(candidate_id)

    decision_ids = set(candidate_ids)
    mode_positions: dict[str, int] = {}
    validated_ranked_ids: set[str] = set()
    for row in ranked:
        mode = str(row.get("strategy_mode") or "")
        mode_positions[mode] = mode_positions.get(mode, 0) + 1
        if row.get("rank") != mode_positions[mode]:
            raise OpeningCandidateSnapshotError("opening candidate rank order is invalid")
        if row.get("candidate_id") not in decision_ids:
            raise OpeningCandidateSnapshotError("ranked candidate is not bound to a decision")
        candidate_id = str(row.get("candidate_id") or "")
        decision = decision_by_id[candidate_id]
        if candidate_id not in validated_decision_ids:
            continue
        if not bool((decision.get("opening_decision") or {}).get("accepted")):
            raise OpeningCandidateSnapshotError(
                "ranked candidate decision is not accepted"
            )
        if row.get("decision_hash") != decision.get("decision_hash"):
            raise OpeningCandidateSnapshotError(
                "ranked candidate decision hash mismatch"
            )
        validated_ranked_ids.add(candidate_id)
    if validated_decision_ids and validated_decision_ids != decision_ids:
        raise OpeningCandidateSnapshotError(
            "opening candidate decision contracts may not be mixed"
        )
    if validated_ranked_ids != accepted_decision_ids:
        raise OpeningCandidateSnapshotError(
            "accepted opening decisions and ranked candidates do not match"
        )
    contract_scope_ids = {
        str(row.get("candidate_id") or "")
        for row in scopes
        if isinstance(row, Mapping) and row.get("scope") == "contract"
    }
    if validated_decision_ids and contract_scope_ids != decision_ids:
        raise OpeningCandidateSnapshotError(
            "opening candidate contract scopes do not match decisions"
        )
    has_strategy_scopes = any(
        isinstance(row, Mapping) and row.get("scope") == "strategy"
        for row in scopes
    )
    full_current_contract = has_strategy_scopes and all(
        isinstance(row, Mapping)
        and {
            "strategy_mode",
            "strategy_status",
            "capacity_status",
            "candidate_count",
            "scope_count",
        }
        <= set(row)
        for row in results
    ) and (not decision_ids or validated_decision_ids == decision_ids)
    if full_current_contract:
        strategy_scopes = [
            row
            for row in scopes
            if isinstance(row, Mapping) and row.get("scope") == "strategy"
        ]
        try:
            reconstructed_statuses = _scan_statuses(
                {
                    "symbol": row.get("symbol"),
                    "strategy_mode": row.get("strategy_mode"),
                    "status": row.get("status"),
                    "reason": row.get("reason_code"),
                    "quote_snapshot_id": row.get("quote_snapshot_id"),
                    "quote_receipt_relpath": row.get("quote_receipt_relpath"),
                }
                for row in strategy_scopes
            )
        except (TypeError, ValueError) as exc:
            raise OpeningCandidateSnapshotError(
                "opening candidate strategy scopes are invalid"
            ) from exc
        expected_scopes = _scope_results(
            statuses=reconstructed_statuses,
            decisions=[dict(row) for row in decisions],
        )
        if scopes != expected_scopes:
            raise OpeningCandidateSnapshotError(
                "opening candidate scope results are inconsistent"
            )
        expected_results = _strategy_results(
            modes=[str(mode) for mode in modes],
            statuses=reconstructed_statuses,
            ranked=[dict(row) for row in ranked],
            decisions=[dict(row) for row in decisions],
            authority=authority,
        )
        if results != expected_results:
            raise OpeningCandidateSnapshotError(
                "opening candidate strategy results are inconsistent"
            )
        if item.get("opening_status") != _opening_status(
            expected_results,
            scan_statuses=reconstructed_statuses,
        ):
            raise OpeningCandidateSnapshotError(
                "opening candidate aggregate status is inconsistent"
            )


_GENERIC_STRATEGY_GAP_REASONS = frozenset(
    {"partial_data", "data_unavailable", "failed", "incomplete", "unavailable"}
)


def _contract_scope_reason_code(scope: Mapping[str, Any]) -> str | None:
    plural_codes: set[str] = set()
    singular_codes: set[str] = set()
    top_level_reasons: set[str] = set()
    rejects = scope.get("rejects")
    if isinstance(rejects, list):
        for reject in rejects:
            if not isinstance(reject, Mapping):
                continue
            value = reject.get("metric_value")
            if isinstance(value, Mapping):
                raw_codes = value.get("reason_codes")
                if isinstance(raw_codes, (list, tuple)) and raw_codes:
                    first_code = next(
                        (str(item).strip() for item in raw_codes if str(item).strip()),
                        "",
                    )
                    if first_code:
                        plural_codes.add(first_code)
                reason_code = str(value.get("reason_code") or "").strip()
                if reason_code:
                    singular_codes.add(reason_code)
            top_level_reason = str(reject.get("reason") or "").strip()
            if top_level_reason:
                top_level_reasons.add(top_level_reason)
    for candidates in (plural_codes, singular_codes, top_level_reasons):
        if candidates:
            return sorted(candidates)[0]
    return None


def candidate_universe_summary(
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Derive model/report completeness from frozen strategy scope facts."""

    scopes = snapshot.get("scope_results")
    if scopes is None:
        # Read-only compatibility for legacy/minimal callers. Canonical current
        # snapshots are validated before this projection and always carry the
        # complete scope collection.
        scopes = []
    if not isinstance(scopes, list):
        raise OpeningCandidateSnapshotError(
            "opening candidate scope results are invalid"
        )
    affected_by_scope: dict[tuple[str, str], dict[str, Any]] = {}
    seen: set[tuple[str, str]] = set()
    for raw in scopes:
        if not isinstance(raw, Mapping) or raw.get("scope") != "strategy":
            continue
        symbol = _required_text(raw.get("symbol"), "scope symbol").upper()
        mode = _mode(raw.get("strategy_mode"))
        key = (symbol, mode)
        if key in seen:
            raise OpeningCandidateSnapshotError(
                "opening candidate strategy scope is duplicated"
            )
        seen.add(key)
        status = _required_text(raw.get("status"), "scope status")
        if status not in {
            "completed",
            "not_applicable",
            "failed",
            "incomplete",
            "unavailable",
        }:
            raise OpeningCandidateSnapshotError(
                "opening candidate strategy scope status is invalid"
            )
        reason = str(raw.get("reason_code") or "").strip() or None
        incomplete = status in {"failed", "incomplete", "unavailable"}
        if status == "completed":
            incomplete = (reason or "") not in _CLEAN_NO_CANDIDATE_REASONS
        elif status == "not_applicable":
            incomplete = bool(
                reason
                and reason not in _BENIGN_ACCOUNT_NOT_APPLICABLE_REASONS
            )
        if incomplete:
            affected_by_scope[key] = {
                "symbol": symbol,
                "strategy_mode": mode,
                "reason_code": reason or status,
            }
    unavailable_reasons = {
        REJECT_EVIDENCE_UNAVAILABLE,
        REJECT_INPUT_INVALID,
        REJECT_INPUT_MISSING,
        REJECT_RISK_EARNINGS_UNAVAILABLE,
    }
    contract_reasons_by_scope: dict[tuple[str, str], set[str]] = {}
    for raw in scopes:
        if not isinstance(raw, Mapping) or raw.get("scope") != "contract":
            continue
        raw_reason_codes = raw.get("reason_codes")
        if not isinstance(raw_reason_codes, (list, tuple)):
            raise OpeningCandidateSnapshotError(
                "opening candidate contract scope reasons are invalid"
            )
        reasons = {
            str(item)
            for item in raw_reason_codes
            if str(item)
        }
        unresolved_reasons = reasons & unavailable_reasons
        if not unresolved_reasons or reasons - unavailable_reasons:
            continue
        symbol = _required_text(raw.get("symbol"), "scope symbol").upper()
        mode = _mode(raw.get("strategy_mode"))
        key = (symbol, mode)
        reason_code = _contract_scope_reason_code(raw) or sorted(unresolved_reasons)[0]
        contract_reasons_by_scope.setdefault(key, set()).add(reason_code)
    for key, reason_codes in contract_reasons_by_scope.items():
        existing = affected_by_scope.get(key)
        if (
            existing is None
            or str(existing.get("reason_code") or "")
            in _GENERIC_STRATEGY_GAP_REASONS
        ):
            symbol, mode = key
            affected_by_scope[key] = {
                "symbol": symbol,
                "strategy_mode": mode,
                "reason_code": sorted(reason_codes)[0],
            }
    affected = list(affected_by_scope.values())
    affected.sort(
        key=lambda row: (
            str(row["strategy_mode"]),
            str(row["symbol"]),
            str(row["reason_code"]),
        )
    )
    return {
        "status": "partial" if affected else "complete",
        "affected_scopes": affected,
    }


def dependency_from_file(
    *,
    kind: str,
    path: Path,
    base: Path,
    required: bool = True,
) -> dict[str, Any]:
    target = Path(path).resolve()
    root = Path(base).resolve()
    if not target.is_file() or target.is_symlink():
        if required:
            raise OpeningCandidateSnapshotError(f"{kind} dependency is unavailable")
        return {}
    try:
        relpath = target.relative_to(root).as_posix()
    except ValueError as exc:
        raise OpeningCandidateSnapshotError(f"{kind} dependency is outside runtime root") from exc
    return {
        "kind": _required_text(kind, "dependency kind"),
        "relpath": relpath,
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
    }


def dependency_from_hash(*, kind: str, sha256: str) -> dict[str, Any]:
    return {
        "kind": _required_text(kind, "dependency kind"),
        "relpath": None,
        "sha256": _sha256(sha256, f"{kind} dependency hash"),
    }


def _snapshot_decisions(
    rows_by_mode: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    statuses: Iterable[Mapping[str, Any]],
    account: str,
    futu_account_id: str,
    market: str,
    risk_policy_hash: str,
    candidate_evaluations: (
        Mapping[str, Iterable[Mapping[str, Any]]] | None
    ),
) -> tuple[list[dict[str, Any]], dict[tuple[str, str, str, str], dict[str, Any]]]:
    status_rows = list(statuses)
    expected_modes = {str(item["strategy_mode"]) for item in status_rows}
    quote_bindings = {
        (
            str(item.get("symbol") or "").strip().upper(),
            _mode(item.get("strategy_mode")),
        ): str(item.get("quote_snapshot_id") or "").strip() or None
        for item in status_rows
    }
    evaluation_rows: dict[str, list[dict[str, Any]]] = {}
    if candidate_evaluations is None:
        for raw_mode, rows in rows_by_mode.items():
            mode = _mode(raw_mode)
            for raw in rows:
                normalized = dict(raw or {})
                opening = evaluate_opening_candidate_policy(normalized, mode=mode)
                if opening.get("accepted") is not True:
                    raise OpeningCandidateSnapshotError(
                        "final candidate is not accepted by opening policy"
                    )
                evaluation_rows.setdefault(mode, []).append(
                    {
                        "normalized_input": normalized,
                        "opening_decision": opening,
                    }
                )
    else:
        for raw_mode, rows in candidate_evaluations.items():
            mode = _mode(raw_mode)
            materialized = [dict(item or {}) for item in rows]
            if mode not in expected_modes and materialized:
                raise OpeningCandidateSnapshotError(
                    "candidate decision strategy scope was not scanned"
                )
            if mode in expected_modes:
                evaluation_rows.setdefault(mode, []).extend(materialized)

    out: list[dict[str, Any]] = []
    index: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for mode, rows in evaluation_rows.items():
        for record in rows:
            normalized = dict(record.get("normalized_input") or {})
            key = _contract_key(mode, normalized)
            if key in index:
                raise OpeningCandidateSnapshotError(
                    "opening candidate contract identity is duplicated"
                )
            try:
                opening = validate_candidate_decision_payload(
                    dict(record.get("opening_decision") or {})
                )
            except (TypeError, ValueError) as exc:
                raise OpeningCandidateSnapshotError(
                    "opening candidate decision payload is invalid"
                ) from exc
            if opening.get("mode") != mode:
                raise OpeningCandidateSnapshotError(
                    "opening candidate decision mode mismatch"
                )
            decision_input = opening.get("normalized_input")
            if not isinstance(decision_input, dict) or canonical_sha256(
                decision_input
            ) != canonical_sha256(normalized):
                raise OpeningCandidateSnapshotError(
                    "opening candidate normalized input mismatch"
                )
            if (
                str(opening.get("symbol") or "").strip().upper()
                != str(normalized.get("symbol") or "").strip().upper()
                or str(opening.get("contract_symbol") or "").strip()
                != str(normalized.get("contract_symbol") or "").strip()
            ):
                raise OpeningCandidateSnapshotError(
                    "opening candidate decision identity mismatch"
                )
            candidate_id = _bound_candidate_id(
                account=account,
                futu_account_id=futu_account_id,
                market=market,
                mode=mode,
                normalized=normalized,
            )
            decision = {
                "schema_version": "opening_candidate_decision.v1",
                "candidate_id": candidate_id,
                "strategy_mode": mode,
                "normalized_input": normalized,
                "normalized_input_hash": canonical_sha256(normalized),
                "decision_hash": canonical_sha256(opening),
                "risk_policy_hash": risk_policy_hash,
                "quote_snapshot_id": quote_bindings.get(
                    (str(normalized.get("symbol") or "").strip().upper(), mode)
                ),
                "opening_decision": opening,
            }
            out.append(decision)
            index[key] = decision
    out.sort(key=lambda row: (str(row.get("strategy_mode")), str(row.get("candidate_id"))))
    return out, index


def _ranked_candidates(
    rows_by_mode: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    modes: list[str],
    decision_index: Mapping[tuple[str, str, str, str], Mapping[str, Any]],
    account: str,
    futu_account_id: str,
    market: str,
) -> list[dict[str, Any]]:
    combined: list[dict[str, Any]] = []
    for mode in modes:
        source_rows: list[dict[str, Any]] = []
        for item in rows_by_mode.get(mode, []):
            final_row = dict(item)
            decision = decision_index.get(_contract_key(mode, final_row))
            if decision is None:
                raise OpeningCandidateSnapshotError(
                    "final candidate is not represented in candidate decisions"
                )
            opening = dict(decision.get("opening_decision") or {})
            if opening.get("accepted") is not True:
                raise OpeningCandidateSnapshotError(
                    "final candidate is not accepted by opening policy"
                )
            source_rows.append(dict(decision.get("normalized_input") or {}))
        for mode_rank, ranked_row in enumerate(
            rank_candidate_rows(source_rows, mode=mode),
            start=1,
        ):
            key = _contract_key(mode, ranked_row)
            decision = decision_index.get(key)
            if decision is None:
                raise OpeningCandidateSnapshotError(
                    "final candidate is not represented in candidate decisions"
                )
            opening = dict(decision.get("opening_decision") or {})
            if opening.get("accepted") is not True:
                raise OpeningCandidateSnapshotError(
                    "final candidate is not accepted by opening policy"
                )
            candidate_id = _bound_candidate_id(
                account=account,
                futu_account_id=futu_account_id,
                market=market,
                mode=mode,
                normalized=ranked_row,
            )
            if candidate_id != decision.get("candidate_id"):
                raise OpeningCandidateSnapshotError("final candidate identity mismatch")
            combined.append(
                {
                    "candidate_id": candidate_id,
                    "strategy_mode": mode,
                    "rank": mode_rank,
                    "facts": ranked_row,
                    "ranking": explain_candidate_rank(ranked_row, mode=mode),
                    "decision_hash": decision.get("decision_hash"),
                    "risk_policy_hash": decision.get("risk_policy_hash"),
                    "quote_snapshot_id": decision.get("quote_snapshot_id"),
                }
            )
    # A strategy rank is authoritative; cross-strategy interleaving is stable only.
    # Consumers expect each strategy's contiguous order, not a cross-strategy rank.
    return sorted(combined, key=lambda row: (modes.index(str(row["strategy_mode"])), int(row["rank"])))


def _strategy_results(
    *,
    modes: list[str],
    statuses: list[dict[str, Any]],
    ranked: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    authority: Mapping[str, Any],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for mode in modes:
        scoped = [item for item in statuses if item["strategy_mode"] == mode]
        counts = sum(1 for item in ranked if item["strategy_mode"] == mode)
        values = {str(item["status"]) for item in scoped}
        reasons = {
            str(item.get("reason") or "")
            for item in scoped
            if str(item.get("reason") or "")
        }
        unavailable_input_status = _unavailable_input_strategy_status(
            decisions,
            mode=mode,
        )
        partial_reasons = _CLEAN_NO_CANDIDATE_REASONS | {"partial_data"}
        if values == {"not_applicable"}:
            status = (
                "no_candidate"
                if reasons
                and reasons <= _BENIGN_ACCOUNT_NOT_APPLICABLE_REASONS
                else "not_applicable"
            )
        elif (
            values <= {"completed", "not_applicable"}
            and "partial_data" in reasons
            and reasons <= partial_reasons
        ):
            status = "partial_data" if not counts else "candidates_found"
        elif not counts and unavailable_input_status is not None:
            # A contract that could not pass input normalization is missing
            # decision evidence. It cannot support a clean zero-candidate seal,
            # even if an upstream scope accidentally reported ``no_candidate``.
            # Preserve mixed usable/unavailable evidence as ``partial_data``.
            status = unavailable_input_status
        elif (
            values <= {"completed", "not_applicable"}
            and reasons <= _CLEAN_NO_CANDIDATE_REASONS
        ):
            status = "candidates_found" if counts else "no_candidate"
        elif values <= {"completed", "not_applicable"}:
            # Completed scopes that carry an evidence-availability reason other
            # than a clean no-candidate must not collapse into a silent empty.
            status = "candidates_found" if counts else "data_unavailable"
        else:
            status = "data_unavailable"
        out.append(
            {
                "strategy_mode": mode,
                "strategy_status": status,
                "capacity_status": (
                    "not_applicable"
                    if status == "not_applicable"
                    else str(authority.get("status") or "unavailable")
                ),
                "candidate_count": counts,
                "scope_count": len(scoped),
            }
        )
    return out


def _unavailable_input_strategy_status(
    decisions: Iterable[Mapping[str, Any]],
    *,
    mode: str,
) -> str | None:
    unavailable_reasons = {REJECT_INPUT_INVALID, REJECT_INPUT_MISSING}
    evaluated_count = 0
    unavailable_count = 0
    for decision in decisions:
        if str(decision.get("strategy_mode") or "") != mode:
            continue
        opening = decision.get("opening_decision")
        if not isinstance(opening, Mapping):
            continue
        evaluated_count += 1
        reasons = {
            str(reject.get("reason") or "")
            for reject in opening.get("rejects") or []
            if isinstance(reject, Mapping)
        }
        if reasons & unavailable_reasons:
            unavailable_count += 1
    if unavailable_count == 0:
        return None
    if unavailable_count < evaluated_count:
        return "partial_data"
    return "data_unavailable"


def _opening_status(
    results: list[dict[str, Any]],
    *,
    scan_statuses: list[dict[str, Any]],
) -> str:
    observed_scopes = [
        item
        for item in scan_statuses
        if str(item.get("status") or "") != "not_applicable"
    ]
    if observed_scopes and all(
        str(item.get("reason") or "") == "market_closed"
        for item in observed_scopes
    ):
        return "market_closed"
    if any(int(item.get("candidate_count") or 0) > 0 for item in results):
        # Accepted rows are fully evaluated Candidate Engine outcomes. A
        # failed sibling scope makes the universe partial, but must not erase
        # those candidates or turn the account-wide opening result into an
        # unavailable input. Completeness is projected separately from the
        # content-hashed strategy scope rows.
        return "candidates_found"
    scope_states = {str(item.get("status") or "") for item in scan_statuses}
    if scope_states & {"completed", "not_applicable"} and scope_states & {
        "failed",
        "incomplete",
        "unavailable",
    }:
        return "partial_data"
    statuses = {str(item["strategy_status"]) for item in results}
    if "partial_data" in statuses:
        return "partial_data"
    if statuses & {"candidates_found", "no_candidate"} and "data_unavailable" in statuses:
        return "partial_data"
    if statuses <= {"data_unavailable", "not_applicable"}:
        return "data_unavailable"
    return "no_candidate"


def _scope_results(
    *,
    statuses: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for status in statuses:
        out.append(
            {
                "scope": "strategy",
                "symbol": status["symbol"],
                "strategy_mode": status["strategy_mode"],
                "status": status["status"],
                "reason_code": status.get("reason") or None,
                "quote_snapshot_id": status.get("quote_snapshot_id"),
                "quote_receipt_relpath": status.get("quote_receipt_relpath"),
            }
        )
    for decision in decisions:
        normalized = dict(decision.get("normalized_input") or {})
        opening = dict(decision.get("opening_decision") or {})
        rejects = [dict(item) for item in opening.get("rejects") or []]
        out.append(
            {
                "scope": "contract",
                "candidate_id": decision.get("candidate_id"),
                "symbol": normalized.get("symbol"),
                "strategy_mode": decision.get("strategy_mode"),
                "expiration": normalized.get("expiration"),
                "strike": normalized.get("strike"),
                "contract_symbol": normalized.get("contract_symbol"),
                "status": "accepted" if opening.get("accepted") is True else "rejected",
                "reason_codes": sorted(
                    {
                        str(item.get("reason") or "")
                        for item in rejects
                        if str(item.get("reason") or "")
                    }
                ),
                "rejects": rejects,
                "normalized_input_hash": decision.get("normalized_input_hash"),
                "decision_hash": decision.get("decision_hash"),
            }
        )
    return sorted(
        out,
        key=lambda row: (
            str(row.get("scope")),
            str(row.get("strategy_mode")),
            str(row.get("symbol")),
            str(row.get("expiration")),
            str(row.get("contract_symbol")),
        ),
    )


def _dependencies(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        item = dict(raw or {})
        kind = _required_text(item.get("kind"), "dependency kind")
        if kind in seen:
            raise OpeningCandidateSnapshotError(f"duplicate dependency kind: {kind}")
        seen.add(kind)
        relpath = item.get("relpath")
        if relpath not in (None, ""):
            relpath = str(relpath)
            path = Path(relpath)
            if path.is_absolute() or ".." in path.parts:
                raise OpeningCandidateSnapshotError(f"invalid dependency path: {kind}")
        else:
            relpath = None
        out.append(
            {
                "kind": kind,
                "relpath": relpath,
                "sha256": _sha256(item.get("sha256"), f"{kind} dependency hash"),
            }
        )
    required = {"required_data", "portfolio", "ledger", "fx", "earnings_rv"}
    missing = sorted(required - seen)
    if missing:
        raise OpeningCandidateSnapshotError(
            "opening candidate dependencies are incomplete: " + ",".join(missing)
        )
    return sorted(out, key=lambda row: str(row["kind"]))


def _verify_dependency_files(
    rows: list[dict[str, Any]],
    *,
    base: Path,
    run_id: str,
    account: str,
) -> None:
    account_state = base / "output_runs" / run_id / "accounts" / account / "state"
    for item in rows:
        relpath = item.get("relpath")
        if not relpath:
            continue
        target = (base / str(relpath)).resolve()
        try:
            target.relative_to(base)
        except ValueError as exc:
            raise OpeningCandidateSnapshotError("opening candidate dependency escapes runtime root") from exc
        if not target.is_file() or target.is_symlink():
            raise OpeningCandidateSnapshotError(
                f"opening candidate dependency is missing: {item['kind']}"
            )
        if hashlib.sha256(target.read_bytes()).hexdigest() != item["sha256"]:
            raise OpeningCandidateSnapshotError(
                f"opening candidate dependency hash mismatch: {item['kind']}"
            )
    if not account_state.parent.is_dir():
        raise OpeningCandidateSnapshotError("opening candidate account-run path is unavailable")


def _dependency_hash(rows: list[dict[str, Any]], kind: str) -> str:
    for item in rows:
        if item["kind"] == kind:
            return str(item["sha256"])
    raise OpeningCandidateSnapshotError(f"opening candidate dependency is missing: {kind}")


def _scan_statuses(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in rows:
        item = dict(raw or {})
        symbol = _required_text(item.get("symbol"), "scan status symbol").upper()
        mode = _mode(item.get("strategy_mode"))
        key = (symbol, mode)
        if key in seen:
            raise OpeningCandidateSnapshotError("opening candidate scan scope is duplicated")
        seen.add(key)
        status = _required_text(item.get("status"), "scan status")
        if status not in {"completed", "not_applicable", "failed", "incomplete", "unavailable"}:
            raise OpeningCandidateSnapshotError("opening candidate scan status is invalid")
        out.append(
            {
                "symbol": symbol,
                "strategy_mode": mode,
                "status": status,
                "reason": str(item.get("reason") or "").strip() or None,
                "quote_snapshot_id": str(item.get("quote_snapshot_id") or "").strip() or None,
                "quote_receipt_relpath": str(item.get("quote_receipt_relpath") or "").strip() or None,
            }
        )
    return sorted(out, key=lambda row: (str(row["symbol"]), str(row["strategy_mode"])))


def _physical_account(
    raw: Mapping[str, Any],
    *,
    account: str,
    market: str,
) -> dict[str, Any]:
    item = dict(raw or {})
    authority_status = str(item.get("status") or "").strip().lower()
    if authority_status not in {"available", "unavailable"}:
        raise OpeningCandidateSnapshotError("physical account authority status is invalid")
    if str(item.get("source") or "").lower() != "opend":
        raise OpeningCandidateSnapshotError("physical OpenD account authority is unavailable")
    if str(item.get("logical_account") or "").lower() != account:
        raise OpeningCandidateSnapshotError("physical account logical binding mismatch")
    authority_market = _market(item.get("market"))
    if authority_market != market:
        raise OpeningCandidateSnapshotError("physical account market binding mismatch")
    futu_account_id = _required_text(item.get("futu_account_id"), "futu_account_id")
    trd_env = _required_text(item.get("trd_env"), "trade_env").upper()
    if trd_env not in {"REAL", "SIMULATE"}:
        raise OpeningCandidateSnapshotError("physical account trade environment is invalid")
    return {
        **item,
        "logical_account": account,
        "futu_account_id": futu_account_id,
        "trd_env": trd_env,
        "market": market,
    }


def _bound_candidate_id(
    *,
    account: str,
    futu_account_id: str,
    market: str,
    mode: str,
    normalized: Mapping[str, Any],
) -> str:
    return canonical_sha256(
        {
            "schema": "opening_candidate_identity.v1",
            "account": account,
            "futu_account_id": futu_account_id,
            "market": market,
            "strategy_mode": mode,
            "symbol": str(normalized.get("symbol") or "").upper(),
            "contract_symbol": str(normalized.get("contract_symbol") or ""),
            "expiration": str(normalized.get("expiration") or ""),
            "strike": normalized.get("strike"),
        }
    )


def _contract_key(mode: str, row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    symbol = _required_text(row.get("symbol"), "candidate symbol").upper()
    contract = _required_text(row.get("contract_symbol"), "candidate contract_symbol")
    expiration = _required_text(row.get("expiration"), "candidate expiration")
    return mode, symbol, expiration, contract


def _market(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if raw in {"US", "HK"}:
        return raw
    inferred = symbol_market(raw)
    if inferred in {"US", "HK"}:
        return str(inferred)
    raise OpeningCandidateSnapshotError("opening candidate market is invalid")


def _mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    if mode not in {"put", "call"}:
        raise OpeningCandidateSnapshotError("opening candidate strategy mode is invalid")
    return mode


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise OpeningCandidateSnapshotError(f"{field} is required")
    return text


def _sha256(value: Any, field: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise OpeningCandidateSnapshotError(f"{field} is invalid")
    return text


def _timestamp(value: datetime | str | Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = _required_text(value, "sealed_at_utc")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise OpeningCandidateSnapshotError("sealed_at_utc is invalid") from exc
    if parsed.tzinfo is None:
        raise OpeningCandidateSnapshotError("sealed_at_utc must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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


__all__ = [
    "OPENING_CANDIDATE_SNAPSHOT_FILE",
    "OPENING_CANDIDATE_SNAPSHOT_SCHEMA",
    "OpeningCandidateSnapshotError",
    "candidate_universe_summary",
    "dependency_from_file",
    "dependency_from_hash",
    "load_latest_opening_candidate_snapshot",
    "load_opening_candidate_snapshot",
    "seal_opening_candidate_snapshot",
    "strategy_policy_hash",
    "validate_opening_candidate_snapshot",
]
