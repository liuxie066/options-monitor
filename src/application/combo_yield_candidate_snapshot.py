from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.engine import validate_candidate_decision_payload
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


COMBO_YIELD_CANDIDATE_SNAPSHOT_SCHEMA = "combo_yield_candidate_snapshot.v2"
COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE = "combo_yield_candidate_snapshot.json"
COMBO_YIELD_OPENING_STATUSES = frozenset(
    {
        "candidates_found",
        "no_candidate",
        "data_unavailable",
        "partial_data",
        "market_closed",
        "not_applicable",
    }
)
_PAIR_EVALUATION_FIELDS = frozenset(
    {
        "run_id",
        "account",
        "diagnostic_scope",
        "diagnostic_stage",
        "accepted",
        "reject_reasons",
        "symbol",
        "expiration",
        "dte",
        "spot",
        "currency",
        "multiplier",
        "candidate_pair_id",
        "put_contract_symbol",
        "put_strike",
        "put_delta",
        "put_open_interest",
        "put_volume",
        "put_spread_ratio",
        "call_contract_symbol",
        "call_strike",
        "call_delta",
        "call_open_interest",
        "call_volume",
        "call_spread_ratio",
        "put_only_net_credit",
        "call_total_cost",
        "combo_net_credit",
        "net_credit",
        "net_debit",
        "net_credit_retention",
        "call_cost_to_put_credit",
        "annualized_net_credit_yield",
        "combo_spread_ratio",
        "funding_accepted",
        "funding_reject_reasons",
        "expected_move",
        "lottery_budget_ratio",
        "residual_premium_ratio",
        "call_payoff_multiple_at_1_5_sigma",
        "call_payoff_multiple_at_2_0_sigma",
        "funding_put_min_annualized_return",
        "put_only_annualized_net_return",
        "yield_enhancement_mode",
        "put_strategy_profile",
        "policy_call_min_delta",
        "policy_call_max_delta",
        "policy_call_min_strike",
        "policy_call_max_strike",
        "policy_call_min_open_interest",
        "policy_call_min_volume",
        "policy_call_max_spread_ratio",
        "policy_min_net_credit_retention",
        "policy_min_net_credit_annualized",
        "policy_max_combo_spread_ratio",
    }
)
_RANK_FIELDS = (
    "candidate_pair_id",
    "run_id",
    "account",
    "symbol",
    "put_contract_symbol",
    "call_contract_symbol",
    "baseline_rank",
    "shadow_rank",
    "baseline_selected",
    "shadow_selected",
    "rank_changed",
)


class ComboYieldCandidateSnapshotError(RuntimeError):
    """Raised when a Combo Yield candidate snapshot cannot be trusted."""


def _contract_error(exc: Exception) -> ComboYieldCandidateSnapshotError:
    return ComboYieldCandidateSnapshotError(str(exc))


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


def _pair_id(raw: Mapping[str, Any], *, required: bool) -> str | None:
    pair_id = str(raw.get("candidate_pair_id") or raw.get("strategy_group_id") or "").strip()
    if not pair_id:
        symbol = str(raw.get("symbol") or "").strip().upper()
        put_contract = str(raw.get("put_contract_symbol") or "").strip()
        call_contract = str(raw.get("call_contract_symbol") or "").strip()
        if symbol and put_contract and call_contract:
            pair_id = f"combo_yield:{symbol}:{put_contract}:{call_contract}"
    if required and not pair_id:
        raise ComboYieldCandidateSnapshotError("combo pair identity is missing")
    return pair_id or None


def _pairs(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise ComboYieldCandidateSnapshotError("combo ranked_pairs must be a list")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(payload):
        if not isinstance(raw, Mapping):
            raise ComboYieldCandidateSnapshotError("combo ranked pair must be an object")
        pair = normalize_json_value(dict(raw), field=f"ranked_pairs[{index}]")
        pair_id = _pair_id(pair, required=True)
        assert pair_id is not None
        if pair_id in seen:
            raise ComboYieldCandidateSnapshotError("combo ranked pair identity is duplicated")
        seen.add(pair_id)
        pair["candidate_pair_id"] = pair_id
        out.append(pair)
    return out


def _funding_put_decisions(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise ComboYieldCandidateSnapshotError("funding_put_decisions must be a list")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(payload):
        if not isinstance(raw, Mapping):
            raise ComboYieldCandidateSnapshotError("funding put decision must be an object")
        item = normalize_json_value(dict(raw), field=f"funding_put_decisions[{index}]")
        if not isinstance(item.get("normalized_input"), dict) or not isinstance(
            item.get("opening_decision"), dict
        ):
            raise ComboYieldCandidateSnapshotError("funding put decision payload is invalid")
        normalized = dict(item["normalized_input"])
        try:
            opening = validate_candidate_decision_payload(
                dict(item["opening_decision"])
            )
        except (TypeError, ValueError) as exc:
            raise ComboYieldCandidateSnapshotError(
                "funding put Candidate Engine decision is invalid"
            ) from exc
        if opening.get("mode") != "put":
            raise ComboYieldCandidateSnapshotError(
                "funding put decision mode is invalid"
            )
        decision_input = opening.get("normalized_input")
        if not isinstance(decision_input, dict) or canonical_sha256(
            decision_input
        ) != canonical_sha256(normalized):
            raise ComboYieldCandidateSnapshotError(
                "funding put normalized input mismatch"
            )
        if (
            str(opening.get("symbol") or "").strip().upper()
            != str(normalized.get("symbol") or "").strip().upper()
            or str(opening.get("contract_symbol") or "").strip()
            != str(normalized.get("contract_symbol") or "").strip()
        ):
            raise ComboYieldCandidateSnapshotError(
                "funding put decision identity mismatch"
            )
        item["opening_decision"] = opening
        decision_id = canonical_sha256(
            {
                "schema_version": "combo_funding_put_decision.v1",
                "normalized_input": item["normalized_input"],
                "opening_decision": item["opening_decision"],
            }
        )
        if decision_id in seen:
            continue
        seen.add(decision_id)
        out.append({**item, "decision_id": decision_id})
    return sorted(out, key=lambda row: str(row["decision_id"]))


def _reject_reason_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        values = value.split("|")
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        raise ComboYieldCandidateSnapshotError("pair evaluation reject_reasons is invalid")
    return sorted({str(item).strip() for item in values if str(item).strip()})


def _pair_evaluations(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise ComboYieldCandidateSnapshotError("pair_evaluations must be a list")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(payload):
        if not isinstance(raw, Mapping):
            raise ComboYieldCandidateSnapshotError("pair evaluation must be an object")
        projected = {
            key: value
            for key, value in raw.items()
            if key in _PAIR_EVALUATION_FIELDS
        }
        projected = normalize_json_value(projected, field=f"pair_evaluations[{index}]")
        scope = required_text(projected.get("diagnostic_scope"), "diagnostic_scope").lower()
        stage = required_text(projected.get("diagnostic_stage"), "diagnostic_stage").lower()
        accepted = projected.get("accepted")
        if not isinstance(accepted, bool):
            raise ComboYieldCandidateSnapshotError("pair evaluation accepted flag is invalid")
        reasons = _reject_reason_list(projected.get("reject_reasons"))
        if accepted and reasons:
            raise ComboYieldCandidateSnapshotError("accepted pair evaluation has reject reasons")
        if not accepted and not reasons:
            raise ComboYieldCandidateSnapshotError("rejected pair evaluation has no reason")
        projected["diagnostic_scope"] = scope
        projected["diagnostic_stage"] = stage
        projected["reject_reasons"] = reasons
        pair_id = _pair_id(projected, required=bool(accepted and scope == "pair"))
        projected["candidate_pair_id"] = pair_id
        evaluation_id = canonical_sha256(
            {
                "schema_version": "combo_pair_evaluation.v1",
                "diagnostic_scope": scope,
                "diagnostic_stage": stage,
                "symbol": projected.get("symbol"),
                "candidate_pair_id": pair_id,
                "put_contract_symbol": projected.get("put_contract_symbol"),
                "call_contract_symbol": projected.get("call_contract_symbol"),
                "accepted": accepted,
                "reject_reasons": reasons,
            }
        )
        if evaluation_id in seen:
            raise ComboYieldCandidateSnapshotError("pair evaluation identity is duplicated")
        seen.add(evaluation_id)
        out.append({"evaluation_id": evaluation_id, **projected})
    return sorted(out, key=lambda row: str(row["evaluation_id"]))


def _rank_records(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise ComboYieldCandidateSnapshotError("rank_records must be a list")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(payload):
        if not isinstance(raw, Mapping):
            raise ComboYieldCandidateSnapshotError("rank record must be an object")
        item = normalize_json_value(
            {field: raw.get(field) for field in _RANK_FIELDS},
            field=f"rank_records[{index}]",
        )
        pair_id = _pair_id(item, required=True)
        assert pair_id is not None
        if pair_id in seen:
            raise ComboYieldCandidateSnapshotError("rank record pair identity is duplicated")
        seen.add(pair_id)
        item["candidate_pair_id"] = pair_id
        for field in ("baseline_selected", "shadow_selected", "rank_changed"):
            if not isinstance(item.get(field), bool):
                raise ComboYieldCandidateSnapshotError(f"rank record {field} is invalid")
        for field in ("baseline_rank", "shadow_rank"):
            rank = item.get(field)
            if rank is not None and (not isinstance(rank, int) or isinstance(rank, bool) or rank <= 0):
                raise ComboYieldCandidateSnapshotError(f"rank record {field} is invalid")
        if bool(item["baseline_selected"]) != (item.get("baseline_rank") is not None):
            raise ComboYieldCandidateSnapshotError("baseline rank selection binding is invalid")
        if bool(item["shadow_selected"]) != (item.get("shadow_rank") is not None):
            raise ComboYieldCandidateSnapshotError("shadow rank selection binding is invalid")
        out.append(item)
    return sorted(out, key=lambda row: str(row["candidate_pair_id"]))


def _bind_selection_states(
    evaluations: list[dict[str, Any]],
    *,
    rank_records: list[dict[str, Any]],
    ranked_pairs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    eligible_ids = {
        str(item.get("candidate_pair_id") or "")
        for item in evaluations
        if item.get("diagnostic_scope") == "pair" and item.get("accepted") is True
    } - {""}
    rank_by_id = {str(item["candidate_pair_id"]): item for item in rank_records}
    if set(rank_by_id) != eligible_ids:
        raise ComboYieldCandidateSnapshotError(
            "eligible pair evaluations and rank records do not match"
        )
    selected_ids = {str(item["candidate_pair_id"]) for item in ranked_pairs}
    unknown_selected = selected_ids - eligible_ids
    if unknown_selected:
        raise ComboYieldCandidateSnapshotError("selected combo pair is not eligible")
    if any(not bool(rank_by_id[pair_id].get("baseline_selected")) for pair_id in selected_ids):
        raise ComboYieldCandidateSnapshotError(
            "selected combo pair is not baseline-selected"
        )
    out: list[dict[str, Any]] = []
    for item in evaluations:
        row = dict(item)
        pair_id = str(row.get("candidate_pair_id") or "")
        if row.get("diagnostic_scope") == "pair" and row.get("accepted") is True:
            row["eligibility_status"] = "eligible"
            row["selection_state"] = "selected" if pair_id in selected_ids else "ranked_below"
        else:
            row["eligibility_status"] = "rejected"
            row["selection_state"] = None
        out.append(row)
    return out


def _validate_evidence_identity(
    *,
    run_id: str,
    account: str,
    scopes: list[dict[str, Any]],
    funding_put_decisions: list[dict[str, Any]],
    pair_evaluations: list[dict[str, Any]],
    rank_records: list[dict[str, Any]],
    ranked_pairs: list[dict[str, Any]],
) -> None:
    allowed_symbols = {str(item["symbol"]).strip().upper() for item in scopes}

    def _assert_identity_fields(row: Mapping[str, Any], *, label: str) -> None:
        row_run_id = str(row.get("run_id") or "").strip()
        row_account = str(row.get("account") or "").strip().lower()
        if row_run_id and row_run_id != run_id:
            raise ComboYieldCandidateSnapshotError(
                f"{label} run identity mismatch"
            )
        if row_account and row_account != account:
            raise ComboYieldCandidateSnapshotError(
                f"{label} account identity mismatch"
            )

    def _assert_pair(row: Mapping[str, Any], *, label: str) -> None:
        _assert_identity_fields(row, label=label)
        symbol = required_text(row.get("symbol"), f"{label} symbol").upper()
        if symbol not in allowed_symbols:
            raise ComboYieldCandidateSnapshotError(
                f"{label} escapes snapshot scope"
            )
        put_contract = str(row.get("put_contract_symbol") or "").strip()
        call_contract = str(row.get("call_contract_symbol") or "").strip()
        if put_contract and call_contract:
            expected_pair_id = (
                f"combo_yield:{symbol}:{put_contract}:{call_contract}"
            )
            if _pair_id(row, required=True) != expected_pair_id:
                raise ComboYieldCandidateSnapshotError(
                    f"{label} pair identity mismatch"
                )

    for item in funding_put_decisions:
        normalized = dict(item.get("normalized_input") or {})
        opening = dict(item.get("opening_decision") or {})
        symbol = required_text(
            normalized.get("symbol") or opening.get("symbol"),
            "funding put decision symbol",
        ).upper()
        if symbol not in allowed_symbols:
            raise ComboYieldCandidateSnapshotError(
                "funding put decision escapes snapshot scope"
            )
        for row in (item, normalized, opening):
            _assert_identity_fields(row, label="funding put decision")
    for label, rows in (
        ("pair evaluation", pair_evaluations),
        ("rank record", rank_records),
        ("ranked pair", ranked_pairs),
    ):
        for item in rows:
            _assert_pair(item, label=label)


def validate_combo_yield_candidate_snapshot(
    payload: Mapping[str, Any],
    *,
    expected_run_id: str,
    expected_account: str,
    verify_dependency_root: Path | None = None,
) -> None:
    try:
        item = normalize_json_value(dict(payload or {}), field="combo_snapshot")
        if item.get("schema_version") != COMBO_YIELD_CANDIDATE_SNAPSHOT_SCHEMA:
            raise ComboYieldCandidateSnapshotError("combo yield candidate snapshot schema mismatch")
        if item.get("run_id") != expected_run_id:
            raise ComboYieldCandidateSnapshotError("combo yield candidate snapshot run mismatch")
        if item.get("account") != expected_account:
            raise ComboYieldCandidateSnapshotError("combo yield candidate snapshot account mismatch")
        if item.get("candidate_owner") != "sp_lc":
            raise ComboYieldCandidateSnapshotError(
                "combo yield candidate snapshot owner mismatch"
            )
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
            raise ComboYieldCandidateSnapshotError("combo yield candidate snapshot content hash mismatch")
        utc_timestamp(item.get("sealed_at_utc"))
        required_text(item.get("market"), "market")
        dependencies = normalize_dependencies(
            item.get("dependencies") or [],
            verify_root=verify_dependency_root,
        )
        if dependency_hash(dependencies, "required_data") != item.get(
            "required_data_manifest_sha256"
        ):
            raise ComboYieldCandidateSnapshotError("required-data dependency hash mismatch")
        scopes = normalize_combo_scope_results(item.get("scope_results") or [], owner="sp_lc")
        decisions = _funding_put_decisions(item.get("funding_put_decisions") or [])
        evaluations = _pair_evaluations(item.get("pair_evaluations") or [])
        ranks = _rank_records(item.get("rank_records") or [])
        pairs = _pairs(item.get("ranked_pairs") or [])
        if scopes != item.get("scope_results"):
            raise ComboYieldCandidateSnapshotError("combo scope results are not canonical")
        if decisions != item.get("funding_put_decisions"):
            raise ComboYieldCandidateSnapshotError("funding put decisions are not canonical")
        if ranks != item.get("rank_records"):
            raise ComboYieldCandidateSnapshotError("combo rank records are not canonical")
        if pairs != item.get("ranked_pairs"):
            raise ComboYieldCandidateSnapshotError("combo ranked pairs are not canonical")
        _validate_evidence_identity(
            run_id=expected_run_id,
            account=expected_account,
            scopes=scopes,
            funding_put_decisions=decisions,
            pair_evaluations=evaluations,
            rank_records=ranks,
            ranked_pairs=pairs,
        )
        expected_evaluations = _bind_selection_states(
            evaluations,
            rank_records=ranks,
            ranked_pairs=pairs,
        )
        if expected_evaluations != item.get("pair_evaluations"):
            raise ComboYieldCandidateSnapshotError("combo pair selection state is invalid")
        status = str(item.get("opening_status") or "").strip().lower()
        if status not in COMBO_YIELD_OPENING_STATUSES:
            raise ComboYieldCandidateSnapshotError("combo yield candidate snapshot status is invalid")
        if combo_opening_status(scopes, selected_count=len(pairs)) != status:
            raise ComboYieldCandidateSnapshotError("combo yield terminal status mismatch")
    except CandidateSnapshotContractError as exc:
        raise _contract_error(exc) from exc


def seal_combo_yield_candidate_snapshot(
    *,
    base: Path,
    run_id: str,
    account: str,
    market: str,
    account_config_sha256: str,
    strategy_policy_sha256: str,
    dependencies: Iterable[Mapping[str, Any]],
    scan_statuses: Iterable[Mapping[str, Any]],
    funding_put_decisions: Iterable[Mapping[str, Any]] = (),
    pair_evaluations: Iterable[Mapping[str, Any]] = (),
    rank_records: Iterable[Mapping[str, Any]] = (),
    ranked_pairs: Iterable[Mapping[str, Any]] = (),
    opening_status: str | None = None,
    sealed_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Assemble, validate, and immutably publish one account-run SP+LC snapshot."""

    try:
        run_id_norm = required_text(run_id, "run_id")
        account_norm = required_text(account, "account").lower()
        market_norm = required_text(market, "market").lower()
        account_config_hash = sha256_text(account_config_sha256, "account_config_sha256")
        policy_hash = sha256_text(strategy_policy_sha256, "strategy_policy_sha256")
        dependency_rows = normalize_dependencies(dependencies)
        scopes = normalize_combo_scope_results(scan_statuses, owner="sp_lc")
    except CandidateSnapshotContractError as exc:
        raise _contract_error(exc) from exc
    try:
        pairs = _pairs(list(ranked_pairs))
        decisions = _funding_put_decisions(list(funding_put_decisions))
        evaluations = _pair_evaluations(list(pair_evaluations))
        ranks = _rank_records(list(rank_records))
        evaluations = _bind_selection_states(
            evaluations,
            rank_records=ranks,
            ranked_pairs=pairs,
        )
        _validate_evidence_identity(
            run_id=run_id_norm,
            account=account_norm,
            scopes=scopes,
            funding_put_decisions=decisions,
            pair_evaluations=evaluations,
            rank_records=ranks,
            ranked_pairs=pairs,
        )
        resolved_status = combo_opening_status(scopes, selected_count=len(pairs))
    except CandidateSnapshotContractError as exc:
        raise _contract_error(exc) from exc
    if opening_status is not None and str(opening_status).strip().lower() != resolved_status:
        raise ComboYieldCandidateSnapshotError("combo yield terminal status mismatch")
    try:
        seal_time = utc_timestamp(sealed_at or datetime.now(timezone.utc))
    except CandidateSnapshotContractError as exc:
        raise _contract_error(exc) from exc

    payload: dict[str, Any] = {
        "schema_version": COMBO_YIELD_CANDIDATE_SNAPSHOT_SCHEMA,
        "run_id": run_id_norm,
        "account": account_norm,
        "market": market_norm,
        "candidate_owner": "sp_lc",
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
        "funding_put_decisions": decisions,
        "pair_evaluations": evaluations,
        "rank_records": ranks,
        "ranked_pairs": pairs,
    }
    payload["content_sha256"] = canonical_sha256(payload)
    validate_combo_yield_candidate_snapshot(
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
            name=COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE,
            payload=encoded,
        )
    except AccountRunConfigError as exc:
        raise ComboYieldCandidateSnapshotError(
            "terminal combo yield candidate snapshot conflicts or cannot be published"
        ) from exc
    adopted = load_combo_yield_candidate_snapshot(
        base=Path(base),
        run_id=run_id_norm,
        account=account_norm,
    )
    if adopted != payload:
        raise ComboYieldCandidateSnapshotError("combo yield candidate snapshot adoption mismatch")
    return adopted


def load_combo_yield_candidate_snapshot(
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
            name=COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE,
        )
        payload = json.loads(encoded.decode("utf-8"))
    except Exception as exc:
        raise ComboYieldCandidateSnapshotError("combo yield candidate snapshot is unavailable") from exc
    if not isinstance(payload, dict):
        raise ComboYieldCandidateSnapshotError("combo yield candidate snapshot must be an object")
    validate_combo_yield_candidate_snapshot(
        payload,
        expected_run_id=run_id_norm,
        expected_account=account_norm,
        verify_dependency_root=Path(base).resolve(),
    )
    return payload


def project_combo_yield_candidates(
    snapshot: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Project the sealed final order without selecting or re-ranking rows."""

    return [
        dict(item)
        for item in snapshot.get("ranked_pairs") or []
        if isinstance(item, Mapping)
    ]


def project_combo_yield_rejections(
    snapshot: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Project only terminally rejected pair diagnostics from sealed evidence."""

    return [
        dict(item)
        for item in snapshot.get("pair_evaluations") or []
        if isinstance(item, Mapping)
        and str(item.get("eligibility_status") or "") == "rejected"
    ]


def project_combo_yield_pair_diagnostics(
    snapshot: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Project all sealed pair-diagnostic rows for summary consumers."""

    return [
        dict(item)
        for item in snapshot.get("pair_evaluations") or []
        if isinstance(item, Mapping)
    ]


def project_combo_yield_rank_evidence(
    snapshot: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Project baseline-vs-shadow ranks exactly as sealed."""

    return [
        dict(item)
        for item in snapshot.get("rank_records") or []
        if isinstance(item, Mapping)
    ]


def project_combo_yield_funding_put_decisions(
    snapshot: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Project Candidate Engine Funding Put decisions without recomputation."""

    return [
        dict(item)
        for item in snapshot.get("funding_put_decisions") or []
        if isinstance(item, Mapping)
    ]


__all__ = [
    "COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE",
    "COMBO_YIELD_CANDIDATE_SNAPSHOT_SCHEMA",
    "ComboYieldCandidateSnapshotError",
    "load_combo_yield_candidate_snapshot",
    "project_combo_yield_candidates",
    "project_combo_yield_funding_put_decisions",
    "project_combo_yield_pair_diagnostics",
    "project_combo_yield_rank_evidence",
    "project_combo_yield_rejections",
    "seal_combo_yield_candidate_snapshot",
    "validate_combo_yield_candidate_snapshot",
]
