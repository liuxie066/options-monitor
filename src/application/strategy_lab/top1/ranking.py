from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import date, datetime, timezone
from typing import Any

from domain.domain.engine import (
    SELL_PUT_RANKING_CONTRACT_VERSION,
    rank_candidate_rows,
)
from src.application.opening_candidate_snapshot import (
    OpeningCandidateSnapshotError,
    ranked_opening_candidate_decisions,
    validate_opening_candidate_snapshot,
)
from src.application.shadow_replay.common import (
    attach_artifact_provenance,
    validate_artifact_provenance,
)


RANKING_PROJECTION_SCHEMA_VERSION = "sell_put_ranking_projection.v1"
RANKING_RESULT_SCHEMA_VERSION = "sell_put_recommendation_ranking_result.v1"
RANKING_PROJECTION_ARTIFACT_KIND = "sell_put_ranking_projection"

_POINT_KEYS = frozenset(
    {
        "recommendation_point_id",
        "market",
        "account",
        "run_id",
        "opening_snapshot_ref",
        "opening_snapshot_sha256",
        "decision_at_utc",
        "source_commit_sha",
    }
)
_CANDIDATE_SOURCE_FIELDS = (
    "symbol",
    "contract_symbol",
    "period_net_return_on_cash_basis",
    "net_assignment_discount_pct",
    "spread_ratio",
    "open_interest",
    "net_income_cny",
    "net_income",
    "symbol_concentration_after",
    "sell_limit",
    "net_premium",
    "net_cash_basis",
    "expiration",
    "strike",
    "multiplier",
    "currency",
    "stock_owner",
    "fee_schedule_version",
    "fee_basis",
    "fee_schedule_url",
)
_CANDIDATE_KEYS = frozenset(
    {"candidate_id", "producer_rank", *_CANDIDATE_SOURCE_FIELDS}
)
_PROJECTION_KEYS = frozenset(
    {
        "schema_version",
        *_POINT_KEYS,
        "account_config_sha256",
        "strategy_policy_sha256",
        "sell_put_ranking_contract_version",
        "producer_accepted_candidate_ids",
        "candidates",
        "artifact_provenance",
    }
)
_PROVENANCE_KEYS = frozenset(
    {
        "schema_version",
        "artifact_kind",
        "artifact_id",
        "content_sha256",
        "source_generation",
    }
)
_SOURCE_GENERATION_KEYS = frozenset(
    {"generation_id", "revision", "source_ref", "source_sha256"}
)
_RANKING_NUMERIC_FIELDS = (
    "period_net_return_on_cash_basis",
    "net_assignment_discount_pct",
    "spread_ratio",
    "open_interest",
    "net_income_cny",
    "net_income",
    "symbol_concentration_after",
)
_POSITIVE_NUMERIC_FIELDS = (
    "sell_limit",
    "net_premium",
    "net_cash_basis",
    "strike",
    "multiplier",
)
_HASH_64 = re.compile(r"[0-9a-f]{64}\Z")
_HASH_40 = re.compile(r"[0-9a-f]{40}\Z")


class Top1RankingError(ValueError):
    """Stable fail-closed error from the Top1 ranking boundary."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _fail(message: str, *, reason_code: str = "ranking_projection_incomplete") -> None:
    raise Top1RankingError(reason_code, message)


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    if set(value) != expected:
        _fail(f"{label} keys are incomplete or unexpected")


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        _fail(f"{label} must be non-empty canonical text")
    return value


def _hash(value: Any, label: str, pattern: re.Pattern[str]) -> str:
    text = _text(value, label)
    if pattern.fullmatch(text) is None:
        _fail(f"{label} is invalid")
    return text


def _finite_number(value: Any, label: str, *, positive: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{label} must be numeric")
    if not math.isfinite(float(value)) or (positive and float(value) <= 0):
        _fail(f"{label} is invalid")


def _utc_timestamp(value: Any, label: str) -> str:
    text = _text(value, label)
    if not text.endswith("Z") or "T" not in text:
        _fail(f"{label} must be an ISO-8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(f"{text[:-1]}+00:00")
    except ValueError:
        _fail(f"{label} must be an ISO-8601 UTC timestamp")
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        _fail(f"{label} must be UTC")
    return text


def _relative_posix_path(value: Any, label: str) -> str:
    text = _text(value, label)
    parts = text.split("/")
    if text.startswith("/") or "\\" in text or any(
        part in {"", ".", ".."} for part in parts
    ):
        _fail(f"{label} must be a safe relative POSIX path")
    return text


def _validate_point_binding(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail("point_binding must be an object")
    item = dict(value)
    _exact_keys(item, _POINT_KEYS, "point_binding")
    _hash(item["recommendation_point_id"], "recommendation_point_id", _HASH_64)
    market = _text(item["market"], "market")
    if market not in {"US", "HK"}:
        _fail("market must be US or HK")
    account = _text(item["account"], "account")
    if account != account.lower():
        _fail("account must be lowercase")
    _text(item["run_id"], "run_id")
    _relative_posix_path(item["opening_snapshot_ref"], "opening_snapshot_ref")
    _hash(item["opening_snapshot_sha256"], "opening_snapshot_sha256", _HASH_64)
    _utc_timestamp(item["decision_at_utc"], "decision_at_utc")
    _hash(item["source_commit_sha"], "source_commit_sha", _HASH_40)
    return item


def build_ranking_projection(
    opening_snapshot: Mapping[str, Any],
    *,
    point_binding: Mapping[str, Any],
) -> dict[str, Any]:
    binding = _validate_point_binding(point_binding)
    if not isinstance(opening_snapshot, Mapping):
        _fail("opening snapshot must be an object")
    snapshot = dict(opening_snapshot)
    try:
        validate_opening_candidate_snapshot(
            snapshot,
            expected_run_id=binding["run_id"],
            expected_account=binding["account"],
            require_current_contract=True,
        )
    except (KeyError, TypeError, ValueError, OpeningCandidateSnapshotError) as exc:
        _fail(f"opening snapshot is invalid: {exc}")
    for field in ("market", "account", "run_id"):
        if binding[field] != snapshot.get(field):
            _fail(f"point binding {field} does not match opening snapshot")
    if binding["opening_snapshot_sha256"] != snapshot.get("content_sha256"):
        _fail("point binding opening snapshot hash does not match")

    try:
        decisions = [
            decision
            for decision in ranked_opening_candidate_decisions(snapshot)
            if decision.get("strategy_mode") == "put"
        ]
        candidates = []
        for decision in decisions:
            if (decision.get("opening_decision") or {}).get("accepted") is not True:
                _fail("ranked Sell Put decision is not accepted")
            normalized = decision.get("normalized_input")
            if not isinstance(normalized, Mapping):
                _fail("ranked Sell Put normalized input is unavailable")
            candidates.append(
                {
                    "candidate_id": decision["candidate_id"],
                    "producer_rank": decision["opening_snapshot_rank"],
                    **{field: normalized[field] for field in _CANDIDATE_SOURCE_FIELDS},
                }
            )
    except (KeyError, TypeError, OpeningCandidateSnapshotError) as exc:
        _fail(f"Sell Put projection facts are incomplete: {exc}")
    put_results = [
        row
        for row in snapshot["strategy_results"]
        if row.get("strategy_mode") == "put"
    ]
    expected_status = "candidates_found" if candidates else "no_candidate"
    if len(put_results) != 1 or put_results[0].get("strategy_status") != expected_status:
        _fail("Sell Put strategy status does not support a ranking projection")

    payload = {
        "schema_version": RANKING_PROJECTION_SCHEMA_VERSION,
        **binding,
        "account_config_sha256": snapshot["account_config_sha256"],
        "strategy_policy_sha256": snapshot["strategy_policy_sha256"],
        "sell_put_ranking_contract_version": SELL_PUT_RANKING_CONTRACT_VERSION,
        "producer_accepted_candidate_ids": [
            candidate["candidate_id"] for candidate in candidates
        ],
        "candidates": candidates,
    }
    attach_artifact_provenance(
        payload,
        artifact_kind=RANKING_PROJECTION_ARTIFACT_KIND,
        source_generation={
            "generation_id": (
                f"opening_candidate_snapshot:{binding['opening_snapshot_sha256']}"
            ),
            "revision": 1,
            "source_ref": binding["opening_snapshot_ref"],
            "source_sha256": binding["opening_snapshot_sha256"],
        },
    )
    return validate_ranking_projection(payload)


def validate_ranking_projection(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        _fail("ranking projection must be an object")
    item = dict(payload)
    _exact_keys(item, _PROJECTION_KEYS, "ranking projection")
    if item["schema_version"] != RANKING_PROJECTION_SCHEMA_VERSION:
        _fail("ranking projection schema is invalid")
    _validate_point_binding({field: item[field] for field in _POINT_KEYS})
    _hash(item["account_config_sha256"], "account_config_sha256", _HASH_64)
    _hash(item["strategy_policy_sha256"], "strategy_policy_sha256", _HASH_64)
    if item["sell_put_ranking_contract_version"] != SELL_PUT_RANKING_CONTRACT_VERSION:
        _fail("Sell Put ranking contract version is invalid")

    candidate_ids = item["producer_accepted_candidate_ids"]
    candidates = item["candidates"]
    if not isinstance(candidate_ids, list) or not isinstance(candidates, list):
        _fail("ranking projection candidates must be lists")
    if any(not isinstance(candidate_id, str) or not candidate_id for candidate_id in candidate_ids):
        _fail("producer candidate IDs are invalid")
    if len(candidate_ids) != len(set(candidate_ids)):
        _fail("producer candidate IDs must be unique")
    if any(not isinstance(candidate, Mapping) for candidate in candidates):
        _fail("projected candidate must be an object")

    projected_ids: list[str] = []
    for expected_rank, raw_candidate in enumerate(candidates, start=1):
        candidate = dict(raw_candidate)
        _exact_keys(candidate, _CANDIDATE_KEYS, "projected candidate")
        candidate_id = _text(candidate["candidate_id"], "candidate_id")
        projected_ids.append(candidate_id)
        rank = candidate["producer_rank"]
        if isinstance(rank, bool) or not isinstance(rank, int) or rank != expected_rank:
            _fail("producer ranks must be contiguous from one")
        for field in ("symbol", "contract_symbol", "currency", "stock_owner"):
            _text(candidate[field], field)
        if candidate["currency"] != candidate["currency"].upper():
            _fail("currency must be uppercase")
        for field in ("fee_schedule_version", "fee_basis", "fee_schedule_url"):
            _text(candidate[field], field)
        expiration = _text(candidate["expiration"], "expiration")
        try:
            if date.fromisoformat(expiration).isoformat() != expiration:
                _fail("expiration must be an ISO date")
        except ValueError:
            _fail("expiration must be an ISO date")
        for field in _RANKING_NUMERIC_FIELDS:
            value = candidate[field]
            if value is not None:
                _finite_number(value, field)
        for field in _POSITIVE_NUMERIC_FIELDS:
            _finite_number(candidate[field], field, positive=True)
    if projected_ids != candidate_ids or len(projected_ids) != len(set(projected_ids)):
        _fail("projected candidate order or identity is invalid")

    provenance = item["artifact_provenance"]
    if not isinstance(provenance, Mapping):
        _fail("artifact provenance must be an object")
    provenance_item = dict(provenance)
    _exact_keys(provenance_item, _PROVENANCE_KEYS, "artifact provenance")
    _hash(provenance_item["content_sha256"], "provenance content_sha256", _HASH_64)
    source = provenance_item["source_generation"]
    if not isinstance(source, Mapping):
        _fail("source generation must be an object")
    source_item = dict(source)
    _exact_keys(source_item, _SOURCE_GENERATION_KEYS, "source generation")
    if source_item != {
        "generation_id": (
            f"opening_candidate_snapshot:{item['opening_snapshot_sha256']}"
        ),
        "revision": 1,
        "source_ref": item["opening_snapshot_ref"],
        "source_sha256": item["opening_snapshot_sha256"],
    }:
        _fail("source generation does not match opening snapshot")
    try:
        validation = validate_artifact_provenance(
            item,
            artifact_kind=RANKING_PROJECTION_ARTIFACT_KIND,
            schema_version=RANKING_PROJECTION_SCHEMA_VERSION,
        )
    except (TypeError, ValueError) as exc:
        _fail(f"artifact provenance is invalid: {exc}")
    if not validation["trusted"]:
        _fail("artifact provenance is invalid")
    return item


def rerank_recommendation_point(
    projection: Mapping[str, Any],
    *,
    ranking_profile: str = "current_tie_break",
) -> dict[str, Any]:
    item = validate_ranking_projection(projection)
    ranked = rank_candidate_rows(
        [dict(candidate) for candidate in item["candidates"]],
        mode="put",
        sell_put_ranking_profile=ranking_profile,
    )
    ranked_ids = [str(candidate["candidate_id"]) for candidate in ranked]
    producer_ids = list(item["producer_accepted_candidate_ids"])
    if set(ranked_ids) != set(producer_ids):
        _fail("reranking changed the accepted candidate universe")
    if ranking_profile == "current_tie_break" and ranked_ids != producer_ids:
        _fail(
            "Candidate Engine default order does not match producer order",
            reason_code="baseline_rank_parity_mismatch",
        )
    return {
        "schema_version": RANKING_RESULT_SCHEMA_VERSION,
        "ranking_profile": ranking_profile,
        "ranking_projection_sha256": item["artifact_provenance"]["content_sha256"],
        "ordered_candidate_ids": ranked_ids,
        "top1_candidate_id": ranked_ids[0] if ranked_ids else None,
        "parity_status": (
            "matched" if ranking_profile == "current_tie_break" else "not_applicable"
        ),
    }
