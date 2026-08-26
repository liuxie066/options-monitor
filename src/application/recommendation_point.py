from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, NoReturn

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.candidate_snapshot_contract import (
    CandidateSnapshotContractError,
    utc_timestamp,
)
from src.application.candidate_snapshot_manifest import (
    CANDIDATE_SNAPSHOT_MANIFEST_FILE,
    CandidateSnapshotManifestError,
    load_candidate_snapshot_bundle,
    validate_candidate_snapshot_manifest,
)
from src.application.opening_candidate_snapshot import (
    OPENING_CANDIDATE_SNAPSHOT_FILE,
    OpeningCandidateSnapshotError,
    candidate_universe_summary,
    ranked_opening_candidate_decisions,
    validate_opening_candidate_snapshot,
)
from src.application.prepared_option_positions_context import (
    PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA_V2,
    PREPARED_OPTION_POSITIONS_MANIFEST_NAME_V2,
    PreparedOptionPositionsContextError,
    find_prepared_option_positions_manifest,
    load_prepared_option_positions_context_receipt,
)
from src.application.required_data_snapshot import (
    RequiredDataSnapshotError,
    load_required_data_snapshot_manifest_snapshot,
)
from src.application.source_receipts import sha256_bytes
from src.application.strategy_lab.top1.ranking import (
    Top1RankingError,
    build_ranking_projection,
)
from src.application.tick_run_workspace import (
    AccountRunConfigError,
    read_account_run_state_bytes_safely,
    write_account_run_state_bytes_once_safely,
)


RECOMMENDATION_POINT_SCHEMA_V1 = "recommendation_point.v1"
RECOMMENDATION_POINT_SCHEMA_V2 = "recommendation_point.v2"
RECOMMENDATION_POINT_SCHEMA_V3 = "recommendation_point.v3"
RECOMMENDATION_POINT_SCHEMA = RECOMMENDATION_POINT_SCHEMA_V1
RECOMMENDATION_POINT_FILE = "recommendation_point.sell_put.json"
STRATEGY_FAMILY = "sell_put"
AVAILABILITY_ENV = "OM_STRATEGY_LAB_TOP1_AVAILABLE"

_POINT_FIELDS_V1 = frozenset(
    {
        "schema_version",
        "recommendation_point_id",
        "strategy_family",
        "market",
        "account",
        "run_id",
        "scheduled_scan_target_market",
        "decision_at_utc",
        "terminal_sell_put_status",
        "account_config_sha256",
        "strategy_policy_sha256",
        "terminal_manifest_ref",
        "terminal_manifest_sha256",
        "opening_snapshot_ref",
        "opening_snapshot_sha256",
        "source_commit_sha",
        "producer_accepted_candidate_ids",
        "content_sha256",
    }
)
_POINT_FIELDS_V2 = frozenset(
    {
        *_POINT_FIELDS_V1,
        "option_market_evidence_ref",
        "option_market_evidence_manifest_sha256",
        "option_market_evidence_payload_sha256",
    }
)
_POINT_FIELDS_V3 = frozenset(
    {
        *_POINT_FIELDS_V1,
        "required_data_manifest_ref",
        "required_data_manifest_sha256",
        "prepared_context_manifest_ref",
        "prepared_context_manifest_sha256",
        "prepared_context_payload_sha256",
        "formal_point_time_coherence",
    }
)
_POINT_BINDING_FIELDS = (
    "recommendation_point_id",
    "market",
    "account",
    "run_id",
    "opening_snapshot_ref",
    "opening_snapshot_sha256",
    "decision_at_utc",
    "source_commit_sha",
)
_POINT_BINDING_FIELDS_V2 = (
    *_POINT_BINDING_FIELDS,
    "option_market_evidence_ref",
    "option_market_evidence_manifest_sha256",
    "option_market_evidence_payload_sha256",
)
_POINT_BINDING_FIELDS_V3 = (
    *_POINT_BINDING_FIELDS,
    "required_data_manifest_ref",
    "required_data_manifest_sha256",
    "prepared_context_manifest_ref",
    "prepared_context_manifest_sha256",
    "prepared_context_payload_sha256",
    "formal_point_time_coherence",
)
_TERMINAL_STATUSES = frozenset(
    {"candidates_found", "no_candidate", "partial_data", "data_unavailable"}
)
_CLEAN_STATUSES = frozenset({"candidates_found", "no_candidate"})
_HASH_64 = re.compile(r"[0-9a-f]{64}\Z")
_HASH_40 = re.compile(r"[0-9a-f]{40}\Z")
_TIME_COHERENCE_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "reason_code",
        "minimum_observed_at_utc",
        "maximum_observed_at_utc",
        "observation_count",
        "skew_ms",
        "max_skew_ms",
    }
)
_TIME_COHERENCE_SCHEMA = "formal_point_time_coherence.v1"
_TIME_COHERENCE_MAX_SKEW_MS = 300_000


class RecommendationPointError(RuntimeError):
    """Stable fail-closed error at the official recommendation-point boundary."""

    reason_code: str

    def __init__(self, reason_code: str, message: str) -> None:
        self.reason_code = str(reason_code)
        super().__init__(message)


def _fail(reason_code: str, message: str) -> NoReturn:
    raise RecommendationPointError(reason_code, message)


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


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _fail("official_point_invalid", f"{label} must be canonical text")
    return value


def _identity(value: Any, label: str) -> str:
    text = _text(value, label)
    if text in {".", ".."} or "/" in text or "\\" in text:
        _fail("official_point_invalid", f"{label} is invalid")
    return text


def _account(value: Any) -> str:
    account = _identity(value, "account")
    if account != account.lower():
        _fail("official_point_invalid", "account must be lowercase")
    return account


def _market(value: Any) -> str:
    market = _text(value, "market")
    if market not in {"US", "HK"}:
        _fail("official_point_invalid", "market must be US or HK")
    return market


def _hash(value: Any, label: str, pattern: re.Pattern[str]) -> str:
    text = _text(value, label)
    if pattern.fullmatch(text) is None:
        _fail("official_point_invalid", f"{label} is invalid")
    return text


def _canonical_timestamp(value: Any, label: str) -> str:
    try:
        return utc_timestamp(value, label)
    except CandidateSnapshotContractError as exc:
        _fail("official_point_identity_missing", str(exc))


def _strict_timestamp(value: Any, label: str) -> str:
    text = _text(value, label)
    try:
        canonical = utc_timestamp(text, label)
    except CandidateSnapshotContractError as exc:
        _fail("official_point_invalid", str(exc))
    if text != canonical:
        _fail("official_point_invalid", f"{label} must be canonical UTC")
    return text


def _terminal_manifest_ref(run_id: str, account: str) -> str:
    return (
        f"output_runs/{run_id}/accounts/{account}/state/"
        f"{CANDIDATE_SNAPSHOT_MANIFEST_FILE}"
    )


def _opening_snapshot_ref(run_id: str, account: str) -> str:
    return (
        f"output_runs/{run_id}/accounts/{account}/state/"
        f"{OPENING_CANDIDATE_SNAPSHOT_FILE}"
    )


def _option_market_evidence_ref(run_id: str, account: str) -> str:
    return (
        f"output_runs/{run_id}/accounts/{account}/state/"
        f"{PREPARED_OPTION_POSITIONS_MANIFEST_NAME_V2}"
    )


def strategy_lab_top1_available(environ: Mapping[str, str] | None = None) -> bool:
    source = os.environ if environ is None else environ
    return source.get(AVAILABILITY_ENV) == "1"


def build_recommendation_point_id(
    market: str,
    account: str,
    scheduled_scan_target_market: Any,
    *,
    schema_version: str = RECOMMENDATION_POINT_SCHEMA,
) -> str:
    if schema_version not in {
        RECOMMENDATION_POINT_SCHEMA_V1,
        RECOMMENDATION_POINT_SCHEMA_V2,
        RECOMMENDATION_POINT_SCHEMA_V3,
    }:
        _fail("official_point_invalid", "recommendation point schema is invalid")
    target = _canonical_timestamp(
        scheduled_scan_target_market,
        "scheduled_scan_target_market",
    )
    return canonical_sha256(
        {
            # Point identity describes the scheduled decision, not its envelope.
            "schema_version": RECOMMENDATION_POINT_SCHEMA_V1,
            "market": _market(market),
            "account": _account(account),
            "strategy_family": STRATEGY_FAMILY,
            "scheduled_scan_target_market": target,
        }
    )


def point_binding_from_recommendation_point(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    item = validate_recommendation_point(payload)
    fields = {
        RECOMMENDATION_POINT_SCHEMA_V1: _POINT_BINDING_FIELDS,
        RECOMMENDATION_POINT_SCHEMA_V2: _POINT_BINDING_FIELDS_V2,
        RECOMMENDATION_POINT_SCHEMA_V3: _POINT_BINDING_FIELDS_V3,
    }[item["schema_version"]]
    return {field: item[field] for field in fields}


def _required_data_binding(opening: Mapping[str, Any]) -> tuple[str, str]:
    rows = [
        row
        for row in opening.get("dependencies") or []
        if isinstance(row, Mapping) and row.get("kind") == "required_data"
    ]
    if len(rows) != 1:
        _fail("required_data_contract_missing", "required-data dependency is missing")
    ref = rows[0].get("relpath")
    digest = rows[0].get("sha256")
    if (
        not isinstance(ref, str)
        or not ref
        or ref.startswith("/")
        or "\\" in ref
        or any(part in {"", ".", ".."} for part in ref.split("/"))
    ):
        _fail("required_data_contract_missing", "required-data dependency ref is invalid")
    return ref, _hash(digest, "required_data_manifest_sha256", _HASH_64)


def _prepared_context_binding(
    receipt: Mapping[str, Any],
    *,
    run_id: str,
    account: str,
    account_config_sha256: str,
    opening_sealed_at_utc: str,
) -> dict[str, str]:
    existing = _prepared_option_binding(
        receipt,
        run_id=run_id,
        account=account,
        account_config_sha256=account_config_sha256,
        opening_sealed_at_utc=opening_sealed_at_utc,
    )
    return {
        "prepared_context_manifest_ref": existing["option_market_evidence_ref"],
        "prepared_context_manifest_sha256": existing[
            "option_market_evidence_manifest_sha256"
        ],
        "prepared_context_payload_sha256": existing[
            "option_market_evidence_payload_sha256"
        ],
    }


def _milliseconds_to_utc(value: Any, label: str) -> str:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _fail("formal_point_time_skew", f"{label} is missing")
    return (
        datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def build_formal_point_time_coherence(
    opening: Mapping[str, Any],
    required_data_manifest: Mapping[str, Any],
    prepared_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    timestamps: list[str] = []
    missing = False
    symbols = required_data_manifest.get("symbols")
    if not isinstance(symbols, Mapping) or not symbols:
        missing = True
    else:
        for row in symbols.values():
            if not isinstance(row, Mapping) or row.get("status") != "ready":
                missing = True
                continue
            try:
                timestamps.append(
                    _strict_timestamp(row.get("source_observed_at"), "source_observed_at")
                )
            except RecommendationPointError:
                missing = True
    for row in opening.get("candidate_decisions") or []:
        normalized = row.get("normalized_input") if isinstance(row, Mapping) else None
        try:
            timestamps.append(
                _strict_timestamp(
                    normalized.get("snapshot_received_at_utc")
                    if isinstance(normalized, Mapping)
                    else None,
                    "snapshot_received_at_utc",
                )
            )
        except RecommendationPointError:
            missing = True
    payload = prepared_receipt.get("payload")
    evidence = (
        payload.get("strategy_lab_option_market_evidence")
        if isinstance(payload, Mapping)
        else None
    )
    marks = evidence.get("valuation_mark_facts") if isinstance(evidence, Mapping) else None
    if not isinstance(marks, list):
        missing = True
        marks = []
    for row in marks:
        if not isinstance(row, Mapping):
            missing = True
            continue
        for field in ("effective_at_ms", "observed_at_ms"):
            try:
                timestamps.append(_milliseconds_to_utc(row.get(field), field))
            except RecommendationPointError:
                missing = True
    parsed = sorted(
        datetime.fromisoformat(value.replace("Z", "+00:00")) for value in timestamps
    )
    minimum = parsed[0].isoformat().replace("+00:00", "Z") if parsed else None
    maximum = parsed[-1].isoformat().replace("+00:00", "Z") if parsed else None
    skew_ms = int((parsed[-1] - parsed[0]).total_seconds() * 1000) if parsed else None
    ready = not missing and skew_ms is not None and skew_ms <= _TIME_COHERENCE_MAX_SKEW_MS
    return {
        "schema_version": _TIME_COHERENCE_SCHEMA,
        "status": "ready" if ready else "not_evaluable",
        "reason_code": None if ready else "formal_point_time_skew",
        "minimum_observed_at_utc": minimum,
        "maximum_observed_at_utc": maximum,
        "observation_count": len(timestamps),
        "skew_ms": skew_ms,
        "max_skew_ms": _TIME_COHERENCE_MAX_SKEW_MS,
    }


def _prepared_option_binding(
    receipt: Mapping[str, Any],
    *,
    run_id: str,
    account: str,
    account_config_sha256: str,
    opening_sealed_at_utc: str,
) -> dict[str, str]:
    manifest = receipt.get("manifest")
    payload = receipt.get("payload")
    manifest_bytes = receipt.get("manifest_bytes")
    payload_bytes = receipt.get("payload_bytes")
    if (
        not isinstance(manifest, Mapping)
        or not isinstance(payload, Mapping)
        or not isinstance(manifest_bytes, bytes)
        or not isinstance(payload_bytes, bytes)
    ):
        _fail(
            "option_market_evidence_contract_missing",
            "prepared option receipt is incomplete",
        )
    if (
        manifest.get("schema_version")
        != PREPARED_OPTION_POSITIONS_CONTEXT_SCHEMA_V2
        or manifest.get("status") != "ready"
        or manifest.get("run_id") != run_id
        or manifest.get("account") != account
        or manifest.get("account_config_sha256") != account_config_sha256
    ):
        _fail(
            "option_market_evidence_conflict",
            "prepared option receipt identity does not match",
        )
    evidence = payload.get("strategy_lab_option_market_evidence")
    if not isinstance(evidence, Mapping) or evidence.get("status") != "ready":
        _fail(
            "option_market_evidence_missing",
            str(
                (evidence or {}).get("reason_code")
                if isinstance(evidence, Mapping)
                else "option market evidence is missing"
            ),
        )
    try:
        manifest_matches = json.loads(manifest_bytes.decode("utf-8")) == dict(
            manifest
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        manifest_matches = False
    if (
        sha256_bytes(payload_bytes) != manifest.get("payload_sha256")
        or not manifest_matches
    ):
        _fail(
            "option_market_evidence_conflict",
            "prepared option receipt hash does not match",
        )
    received = _canonical_timestamp(
        manifest.get("application_received_at_utc"),
        "prepared option application_received_at_utc",
    )
    sealed = _canonical_timestamp(
        opening_sealed_at_utc,
        "opening sealed_at_utc",
    )
    if datetime.fromisoformat(received.replace("Z", "+00:00")) > (
        datetime.fromisoformat(sealed.replace("Z", "+00:00"))
    ):
        _fail(
            "option_market_evidence_time_conflict",
            "prepared option receipt is later than the opening snapshot",
        )
    return {
        "option_market_evidence_ref": _option_market_evidence_ref(
            run_id,
            account,
        ),
        "option_market_evidence_manifest_sha256": sha256_bytes(
            manifest_bytes
        ),
        "option_market_evidence_payload_sha256": str(
            manifest["payload_sha256"]
        ),
    }


def build_recommendation_point(
    scheduler_decision: Mapping[str, Any],
    terminal_manifest: Mapping[str, Any],
    opening_snapshot: Mapping[str, Any],
    *,
    terminal_manifest_sha256: str,
    source_commit_sha: str,
    prepared_option_receipt: Mapping[str, Any] | None = None,
    required_data_manifest: Mapping[str, Any] | None = None,
    required_data_manifest_ref: str | None = None,
    required_data_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    if not isinstance(scheduler_decision, Mapping):
        _fail("official_point_identity_missing", "scheduler decision is missing")
    if scheduler_decision.get("should_run_scan") is not True:
        _fail("official_point_identity_missing", "scheduler decision did not run")
    target = _canonical_timestamp(
        scheduler_decision.get("scheduled_scan_target_market"),
        "scheduled_scan_target_market",
    )
    decision_at = _canonical_timestamp(
        scheduler_decision.get("now_utc"),
        "decision_at_utc",
    )
    try:
        source_sha = _hash(source_commit_sha, "source_commit_sha", _HASH_40)
    except RecommendationPointError as exc:
        _fail("official_point_source_unavailable", str(exc))

    if not isinstance(opening_snapshot, Mapping):
        _fail("official_point_invalid", "opening snapshot must be an object")
    opening = dict(opening_snapshot)
    run_id = _identity(opening.get("run_id"), "run_id")
    account = _account(opening.get("account"))
    market = _market(opening.get("market"))
    try:
        validate_opening_candidate_snapshot(
            opening,
            expected_run_id=run_id,
            expected_account=account,
            require_current_contract=True,
        )
    except (KeyError, TypeError, ValueError, OpeningCandidateSnapshotError) as exc:
        _fail("official_point_invalid", f"opening snapshot is invalid: {exc}")

    if not isinstance(terminal_manifest, Mapping):
        _fail("official_point_invalid", "terminal manifest must be an object")
    manifest = dict(terminal_manifest)
    try:
        validate_candidate_snapshot_manifest(
            manifest,
            expected_run_id=run_id,
            expected_account=account,
        )
    except (KeyError, TypeError, ValueError, CandidateSnapshotManifestError) as exc:
        _fail("official_point_invalid", f"terminal manifest is invalid: {exc}")
    manifest_sha = _hash(
        terminal_manifest_sha256,
        "terminal_manifest_sha256",
        _HASH_64,
    )
    if hashlib.sha256(_canonical_json_bytes(manifest)).hexdigest() != manifest_sha:
        _fail("official_point_invalid", "terminal manifest byte hash does not match")

    for field in ("account_config_sha256", "strategy_policy_sha256"):
        if manifest.get(field) != opening.get(field):
            _fail("official_point_invalid", f"{field} binding does not match")
    opening_entries = [
        row
        for row in manifest.get("owner_snapshots") or []
        if isinstance(row, Mapping) and row.get("candidate_owner") == "opening"
    ]
    if len(opening_entries) != 1:
        _fail("official_point_unavailable", "opening owner is unavailable")
    opening_entry = dict(opening_entries[0])
    if (
        opening_entry.get("relpath") != f"state/{OPENING_CANDIDATE_SNAPSHOT_FILE}"
        or opening_entry.get("content_sha256") != opening.get("content_sha256")
    ):
        _fail("official_point_invalid", "opening owner binding does not match")
    sell_put_scopes = [
        row
        for row in manifest.get("expected_scopes") or []
        if isinstance(row, Mapping)
        and row.get("strategy_family") == STRATEGY_FAMILY
        and row.get("strategy_mode") == "put"
        and row.get("candidate_owner") == "opening"
    ]
    if not sell_put_scopes:
        _fail("official_point_unavailable", "terminal Sell Put scope is unavailable")
    if any(row.get("market") != market for row in sell_put_scopes):
        _fail("official_point_invalid", "Sell Put scope market does not match")

    put_results = [
        row
        for row in opening.get("strategy_results") or []
        if isinstance(row, Mapping) and row.get("strategy_mode") == "put"
    ]
    if len(put_results) != 1:
        _fail("official_point_invalid", "Sell Put strategy result is invalid")
    aggregate_status = str(put_results[0].get("strategy_status") or "")
    try:
        incomplete_put = any(
            row.get("strategy_mode") == "put"
            for row in candidate_universe_summary(opening)["affected_scopes"]
        )
        accepted_ids = [
            _text(row.get("candidate_id"), "candidate_id")
            for row in ranked_opening_candidate_decisions(opening)
            if row.get("strategy_mode") == "put"
        ]
    except (KeyError, TypeError, ValueError, OpeningCandidateSnapshotError) as exc:
        _fail("official_point_invalid", f"Sell Put producer facts are invalid: {exc}")
    if len(accepted_ids) != len(set(accepted_ids)):
        _fail("official_point_invalid", "Sell Put candidate IDs are duplicated")
    if aggregate_status == "data_unavailable":
        point_status = "data_unavailable"
    elif incomplete_put:
        point_status = "partial_data"
    elif aggregate_status in _TERMINAL_STATUSES:
        point_status = aggregate_status
    else:
        _fail("official_point_invalid", "Sell Put terminal status is unsupported")

    formal = required_data_manifest is not None
    if formal and prepared_option_receipt is None:
        _fail("option_market_evidence_contract_missing", "formal point requires prepared evidence")
    point_schema = (
        RECOMMENDATION_POINT_SCHEMA_V3
        if formal
        else RECOMMENDATION_POINT_SCHEMA_V2
        if prepared_option_receipt is not None
        else RECOMMENDATION_POINT_SCHEMA_V1
    )
    point_id = build_recommendation_point_id(
        market,
        account,
        target,
        schema_version=point_schema,
    )
    payload: dict[str, Any] = {
        "schema_version": point_schema,
        "recommendation_point_id": point_id,
        "strategy_family": STRATEGY_FAMILY,
        "market": market,
        "account": account,
        "run_id": run_id,
        "scheduled_scan_target_market": target,
        "decision_at_utc": decision_at,
        "terminal_sell_put_status": point_status,
        "account_config_sha256": opening["account_config_sha256"],
        "strategy_policy_sha256": opening["strategy_policy_sha256"],
        "terminal_manifest_ref": _terminal_manifest_ref(run_id, account),
        "terminal_manifest_sha256": manifest_sha,
        "opening_snapshot_ref": _opening_snapshot_ref(run_id, account),
        "opening_snapshot_sha256": opening["content_sha256"],
        "source_commit_sha": source_sha,
        "producer_accepted_candidate_ids": accepted_ids,
    }
    if point_schema == RECOMMENDATION_POINT_SCHEMA_V3:
        dependency_ref, dependency_hash = _required_data_binding(opening)
        if (
            required_data_manifest_ref != dependency_ref
            or required_data_manifest_sha256 != dependency_hash
            or required_data_manifest.get("run_id") != run_id
        ):
            _fail("required_data_conflict", "required-data manifest binding does not match")
        payload.update(
            {
                "required_data_manifest_ref": dependency_ref,
                "required_data_manifest_sha256": dependency_hash,
                **_prepared_context_binding(
                    prepared_option_receipt,
                    run_id=run_id,
                    account=account,
                    account_config_sha256=opening["account_config_sha256"],
                    opening_sealed_at_utc=opening["sealed_at_utc"],
                ),
                "formal_point_time_coherence": build_formal_point_time_coherence(
                    opening,
                    required_data_manifest,
                    prepared_option_receipt,
                ),
            }
        )
    elif prepared_option_receipt is not None:
        payload.update(
            _prepared_option_binding(
                prepared_option_receipt,
                run_id=run_id,
                account=account,
                account_config_sha256=opening["account_config_sha256"],
                opening_sealed_at_utc=opening["sealed_at_utc"],
            )
        )

    if point_schema != RECOMMENDATION_POINT_SCHEMA_V3:
        projection: dict[str, Any] | None = None
        try:
            projection = build_ranking_projection(
                opening,
                point_binding={field: payload[field] for field in _POINT_BINDING_FIELDS},
            )
        except Top1RankingError as exc:
            if point_status in _CLEAN_STATUSES:
                _fail("official_point_invalid", f"clean point is not rankable: {exc}")
        if (
            projection is not None
            and projection.get("producer_accepted_candidate_ids") != accepted_ids
        ):
            _fail("official_point_invalid", "W1A accepted candidate IDs do not match")

    payload["content_sha256"] = canonical_sha256(payload)
    return validate_recommendation_point(payload)


def validate_recommendation_point(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        _fail("official_point_invalid", "recommendation point must be an object")
    item = dict(payload)
    schema = item.get("schema_version")
    expected_fields = {
        RECOMMENDATION_POINT_SCHEMA_V1: _POINT_FIELDS_V1,
        RECOMMENDATION_POINT_SCHEMA_V2: _POINT_FIELDS_V2,
        RECOMMENDATION_POINT_SCHEMA_V3: _POINT_FIELDS_V3,
    }.get(schema, _POINT_FIELDS_V1)
    if set(item) != expected_fields:
        _fail("official_point_invalid", "recommendation point keys are incomplete or unexpected")
    if schema not in {
        RECOMMENDATION_POINT_SCHEMA_V1,
        RECOMMENDATION_POINT_SCHEMA_V2,
        RECOMMENDATION_POINT_SCHEMA_V3,
    }:
        _fail("official_point_invalid", "recommendation point schema is invalid")
    if item["strategy_family"] != STRATEGY_FAMILY:
        _fail("official_point_invalid", "strategy family is invalid")
    market = _market(item["market"])
    account = _account(item["account"])
    run_id = _identity(item["run_id"], "run_id")
    target = _strict_timestamp(
        item["scheduled_scan_target_market"],
        "scheduled_scan_target_market",
    )
    _strict_timestamp(item["decision_at_utc"], "decision_at_utc")
    point_id = _hash(item["recommendation_point_id"], "recommendation_point_id", _HASH_64)
    if point_id != build_recommendation_point_id(
        market,
        account,
        target,
        schema_version=str(schema),
    ):
        _fail("official_point_invalid", "recommendation point identity does not match")
    status = item["terminal_sell_put_status"]
    if status not in _TERMINAL_STATUSES:
        _fail("official_point_invalid", "terminal Sell Put status is invalid")
    for field in (
        "account_config_sha256",
        "strategy_policy_sha256",
        "terminal_manifest_sha256",
        "opening_snapshot_sha256",
        "content_sha256",
    ):
        _hash(item[field], field, _HASH_64)
    _hash(item["source_commit_sha"], "source_commit_sha", _HASH_40)
    if item["terminal_manifest_ref"] != _terminal_manifest_ref(run_id, account):
        _fail("official_point_invalid", "terminal manifest ref is invalid")
    if item["opening_snapshot_ref"] != _opening_snapshot_ref(run_id, account):
        _fail("official_point_invalid", "opening snapshot ref is invalid")
    if schema == RECOMMENDATION_POINT_SCHEMA_V2:
        if item["option_market_evidence_ref"] != _option_market_evidence_ref(
            run_id,
            account,
        ):
            _fail(
                "official_point_invalid",
                "option market evidence ref is invalid",
            )
        for field in (
            "option_market_evidence_manifest_sha256",
            "option_market_evidence_payload_sha256",
        ):
            _hash(item[field], field, _HASH_64)
    if schema == RECOMMENDATION_POINT_SCHEMA_V3:
        for field in ("required_data_manifest_ref", "prepared_context_manifest_ref"):
            ref = _text(item[field], field)
            if ref.startswith("/") or "\\" in ref or any(
                part in {"", ".", ".."} for part in ref.split("/")
            ):
                _fail("official_point_invalid", f"{field} is invalid")
        for field in (
            "required_data_manifest_sha256",
            "prepared_context_manifest_sha256",
            "prepared_context_payload_sha256",
        ):
            _hash(item[field], field, _HASH_64)
        coherence = item["formal_point_time_coherence"]
        if not isinstance(coherence, Mapping) or set(coherence) != _TIME_COHERENCE_FIELDS:
            _fail("official_point_invalid", "formal point time coherence is invalid")
        if coherence.get("schema_version") != _TIME_COHERENCE_SCHEMA:
            _fail("official_point_invalid", "formal point time coherence schema is invalid")
        if coherence.get("status") not in {"ready", "not_evaluable"}:
            _fail("official_point_invalid", "formal point time coherence status is invalid")
        if (coherence["status"] == "ready") != (coherence.get("reason_code") is None):
            _fail("official_point_invalid", "formal point time coherence reason is invalid")
        if coherence["status"] == "not_evaluable" and coherence.get("reason_code") != "formal_point_time_skew":
            _fail("official_point_invalid", "formal point time coherence reason is unsupported")
        count = coherence.get("observation_count")
        skew = coherence.get("skew_ms")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            _fail("official_point_invalid", "formal point observation count is invalid")
        if skew is not None and (isinstance(skew, bool) or not isinstance(skew, int) or skew < 0):
            _fail("official_point_invalid", "formal point skew is invalid")
        if coherence.get("max_skew_ms") != _TIME_COHERENCE_MAX_SKEW_MS:
            _fail("official_point_invalid", "formal point skew limit is invalid")
        minimum = coherence.get("minimum_observed_at_utc")
        maximum = coherence.get("maximum_observed_at_utc")
        parsed_minimum = (
            datetime.fromisoformat(_strict_timestamp(minimum, "minimum_observed_at_utc").replace("Z", "+00:00"))
            if minimum is not None
            else None
        )
        parsed_maximum = (
            datetime.fromisoformat(_strict_timestamp(maximum, "maximum_observed_at_utc").replace("Z", "+00:00"))
            if maximum is not None
            else None
        )
        if count == 0:
            if minimum is not None or maximum is not None or skew is not None:
                _fail("official_point_invalid", "empty formal point coherence is inconsistent")
        elif parsed_minimum is None or parsed_maximum is None or skew is None:
            _fail("official_point_invalid", "formal point coherence range is incomplete")
        elif parsed_minimum > parsed_maximum or skew != int(
            (parsed_maximum - parsed_minimum).total_seconds() * 1000
        ):
            _fail("official_point_invalid", "formal point coherence range is inconsistent")
        if coherence["status"] == "ready" and (
            count == 0
            or
            skew is None
            or skew > _TIME_COHERENCE_MAX_SKEW_MS
            or minimum is None
            or maximum is None
        ):
            _fail("official_point_invalid", "formal point ready coherence is incomplete")
    candidate_ids = item["producer_accepted_candidate_ids"]
    if not isinstance(candidate_ids, list):
        _fail("official_point_invalid", "producer candidate IDs must be a list")
    canonical_ids = [_text(value, "candidate_id") for value in candidate_ids]
    if len(canonical_ids) != len(set(canonical_ids)):
        _fail("official_point_invalid", "producer candidate IDs are duplicated")
    if status == "candidates_found" and not canonical_ids:
        _fail("official_point_invalid", "candidates_found requires an accepted candidate")
    if status == "no_candidate" and canonical_ids:
        _fail("official_point_invalid", "no_candidate cannot contain accepted candidates")
    content = {key: value for key, value in item.items() if key != "content_sha256"}
    if canonical_sha256(content) != item["content_sha256"]:
        _fail("official_point_invalid", "recommendation point content hash does not match")
    return item


def _decode_point_bytes(encoded: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail("official_point_invalid", f"recommendation point bytes are invalid: {exc}")
    if not isinstance(payload, dict):
        _fail("official_point_invalid", "recommendation point must be an object")
    item = validate_recommendation_point(payload)
    if encoded != _canonical_json_bytes(item):
        _fail("official_point_invalid", "recommendation point bytes are not canonical")
    return item


def publish_recommendation_point(
    base: Path,
    payload: Mapping[str, Any],
) -> str:
    item = validate_recommendation_point(payload)
    encoded = _canonical_json_bytes(item)
    identity = {
        "run_id": item["run_id"],
        "account": item["account"],
        "name": RECOMMENDATION_POINT_FILE,
    }
    try:
        existing = read_account_run_state_bytes_safely(base=Path(base), **identity)
    except AccountRunConfigError:
        existing = None
    if existing is not None:
        if existing != encoded:
            _fail("official_point_conflict", "recommendation point already exists with different bytes")
        _decode_point_bytes(existing)
        return "idempotent"
    try:
        write_account_run_state_bytes_once_safely(
            base=Path(base),
            payload=encoded,
            **identity,
        )
    except AccountRunConfigError as exc:
        try:
            adopted = read_account_run_state_bytes_safely(base=Path(base), **identity)
        except AccountRunConfigError:
            adopted = None
        if adopted is not None and adopted == encoded:
            _decode_point_bytes(adopted)
            return "idempotent"
        reason = (
            "official_point_conflict"
            if exc.code == "ACCOUNT_RUN_STATE_CONFLICT"
            else "official_point_unavailable"
        )
        _fail(reason, "recommendation point cannot be published")
    return "published"


def load_recommendation_point(
    base: Path,
    run_id: str,
    account: str,
) -> dict[str, Any]:
    try:
        encoded = read_account_run_state_bytes_safely(
            base=Path(base),
            run_id=_identity(run_id, "run_id"),
            account=_account(account),
            name=RECOMMENDATION_POINT_FILE,
        )
    except AccountRunConfigError as exc:
        _fail("official_point_unavailable", f"recommendation point is unavailable: {exc}")
    return _decode_point_bytes(encoded)


def capture_scheduled_recommendation_point(
    base: Path,
    run_id: str,
    account: str,
    scheduler_decision: Mapping[str, Any],
    *,
    source_commit_sha: str,
    require_option_market_evidence: bool = False,
    require_formal_contract: bool = False,
) -> tuple[str, dict[str, Any]]:
    try:
        bundle = load_candidate_snapshot_bundle(
            base=Path(base),
            run_id=run_id,
            account=account,
        )
        manifest_bytes = read_account_run_state_bytes_safely(
            base=Path(base),
            run_id=run_id,
            account=account,
            name=CANDIDATE_SNAPSHOT_MANIFEST_FILE,
        )
    except (AccountRunConfigError, CandidateSnapshotManifestError) as exc:
        _fail("official_point_unavailable", f"terminal candidate bundle is unavailable: {exc}")
    manifest = dict(bundle["manifest"])
    if manifest_bytes != _canonical_json_bytes(manifest):
        _fail("official_point_invalid", "terminal manifest bytes are not canonical")
    owners = bundle.get("owners")
    opening = owners.get("opening") if isinstance(owners, Mapping) else None
    if not isinstance(opening, Mapping):
        _fail("official_point_unavailable", "opening owner is unavailable")
    prepared_receipt: Mapping[str, Any] | None = None
    required_manifest: Mapping[str, Any] | None = None
    required_manifest_ref: str | None = None
    required_manifest_sha256: str | None = None
    if require_option_market_evidence or require_formal_contract:
        prepared_manifest_path = find_prepared_option_positions_manifest(
            base=Path(base),
            run_id=run_id,
            account=account,
        )
        if prepared_manifest_path is None:
            _fail(
                "option_market_evidence_contract_missing",
                "prepared option v2 receipt is unavailable",
            )
        try:
            if require_formal_contract:
                required_manifest_ref, required_manifest_sha256 = _required_data_binding(
                    opening
                )
                required_path = Path(base).resolve().joinpath(
                    *required_manifest_ref.split("/")
                )
                required_manifest, _required_root, required_bytes = (
                    load_required_data_snapshot_manifest_snapshot(
                        manifest_path=required_path,
                        expected_run_id=run_id,
                    )
                )
                if (
                    hashlib.sha256(required_bytes).hexdigest()
                    != required_manifest_sha256
                ):
                    _fail(
                        "required_data_conflict",
                        "required-data manifest hash does not match",
                    )
        except (OSError, RequiredDataSnapshotError) as exc:
            _fail("required_data_contract_missing", f"required-data manifest is invalid: {exc}")
        try:
            prepared_receipt = load_prepared_option_positions_context_receipt(
                manifest_path=prepared_manifest_path,
                expected_base=Path(base),
                expected_run_id=run_id,
                expected_account=account,
                expected_account_config_sha256=str(
                    opening.get("account_config_sha256") or ""
                ),
                require_option_market_evidence=True,
            )
        except PreparedOptionPositionsContextError as exc:
            reason = str(exc)
            _fail(
                reason
                if reason.startswith("option_market_evidence_")
                else "option_market_evidence_contract_missing",
                f"prepared option v2 receipt is invalid: {exc}",
            )
    point = build_recommendation_point(
        scheduler_decision,
        manifest,
        opening,
        terminal_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        source_commit_sha=source_commit_sha,
        prepared_option_receipt=prepared_receipt,
        required_data_manifest=required_manifest,
        required_data_manifest_ref=required_manifest_ref,
        required_data_manifest_sha256=required_manifest_sha256,
    )
    return publish_recommendation_point(Path(base), point), point
