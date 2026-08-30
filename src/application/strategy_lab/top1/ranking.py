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
from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.short_vol_assessment import (
    OPTION_MARKET_CONCENTRATION_METRIC_VERSION,
    calculate_option_market_concentration_after,
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
from src.application.strategy_lab.top1.economics import (
    build_fx_rate_binding_from_projection,
    validate_fx_rate_binding,
)


RANKING_PROJECTION_SCHEMA_V1 = "sell_put_ranking_projection.v1"
RANKING_PROJECTION_SCHEMA_V2 = "sell_put_ranking_projection.v2"
RANKING_PROJECTION_SCHEMA_V3 = "sell_put_ranking_projection.v3"
RANKING_PROJECTION_SCHEMA_VERSION = RANKING_PROJECTION_SCHEMA_V1
RANKING_RESULT_SCHEMA_VERSION = "sell_put_recommendation_ranking_result.v1"
RANKING_PROJECTION_ARTIFACT_KIND = "sell_put_ranking_projection"
RECIPE_ID = "sell_put_top1_option_market_concentration"
RECIPE_VERSION = "v1"
_RECIPE_PROJECTION_BEHAVIOR_SCHEMA = "sell_put_top1_recipe_projection_behavior.v1"

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
_POINT_KEYS_V2 = frozenset(
    {
        *_POINT_KEYS,
        "option_market_evidence_ref",
        "option_market_evidence_manifest_sha256",
        "option_market_evidence_payload_sha256",
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
_CANDIDATE_MARKET_FIELDS = (
    "option_type",
    "bid",
    "ask",
    "bid_volume",
    "ask_volume",
    "last",
    "implied_volatility",
    "delta",
    "gamma",
    "theta",
    "vega",
    "rho",
    "volume",
    "spot",
    "quote_effective_at_utc",
    "quote_observed_at_utc",
    "quote_status",
)
_OPTION_MARKET_CANDIDATE_FIELDS = (
    "option_market_concentration_after",
    "option_market_value_cny",
    "option_market_concentration_metric_version",
    "option_market_evidence_refs",
    "opening_fx_binding",
)
_CANDIDATE_KEYS_V2 = frozenset(
    {
        *_CANDIDATE_KEYS,
        *_CANDIDATE_MARKET_FIELDS,
        *_OPTION_MARKET_CANDIDATE_FIELDS,
    }
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
_PROJECTION_KEYS_V2 = frozenset(
    {
        *_PROJECTION_KEYS,
        "option_market_evidence_ref",
        "option_market_evidence_manifest_sha256",
        "option_market_evidence_payload_sha256",
    }
)
_PROJECTION_KEYS_V3 = frozenset(
    {
        "schema_version",
        "formal_point_ref",
        "formal_point_content_sha256",
        "recipe_id",
        "recipe_version",
        "behavior_binding_sha256",
        "materialized_input_content_sha256",
        "producer_accepted_candidate_ids",
        "candidates",
        "artifact_provenance",
    }
)
_CANDIDATE_KEYS_V3 = frozenset(
    {
        "candidate_id",
        "option_market_concentration_after",
        "option_market_value_cny",
        "option_market_concentration_metric_version",
        "option_market_evidence_refs",
        "opening_fx_binding",
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
_CANDIDATE_MARKET_NUMERIC_FIELDS = (
    "bid",
    "ask",
    "bid_volume",
    "ask_volume",
    "last",
    "implied_volatility",
    "delta",
    "gamma",
    "theta",
    "vega",
    "rho",
    "volume",
    "spot",
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


def _optional_utc_timestamp(value: Any, label: str) -> str | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        _fail(f"{label} must be an ISO-8601 UTC timestamp")
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        _fail(f"{label} must be UTC")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _relative_posix_path(value: Any, label: str) -> str:
    text = _text(value, label)
    parts = text.split("/")
    if text.startswith("/") or "\\" in text or any(
        part in {"", ".", ".."} for part in parts
    ):
        _fail(f"{label} must be a safe relative POSIX path")
    return text


def _validate_point_binding(
    value: Any,
    *,
    require_option_market_evidence: bool = False,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail("point_binding must be an object")
    item = dict(value)
    expected_keys = _POINT_KEYS_V2 if require_option_market_evidence else _POINT_KEYS
    _exact_keys(item, expected_keys, "point_binding")
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
    if require_option_market_evidence:
        _relative_posix_path(
            item["option_market_evidence_ref"],
            "option_market_evidence_ref",
        )
        for field in (
            "option_market_evidence_manifest_sha256",
            "option_market_evidence_payload_sha256",
        ):
            _hash(item[field], field, _HASH_64)
    return item


def recipe_projection_behavior_sha256() -> str:
    return canonical_sha256(
        {
            "schema_version": _RECIPE_PROJECTION_BEHAVIOR_SCHEMA,
            "recipe_id": RECIPE_ID,
            "recipe_version": RECIPE_VERSION,
            "accepted_set_contract": "same_point_producer_accepted_set.v1",
            "ranking_contract": SELL_PUT_RANKING_CONTRACT_VERSION,
            "metric_contract": OPTION_MARKET_CONCENTRATION_METRIC_VERSION,
        }
    )


def build_ranking_projection(
    opening_snapshot: Mapping[str, Any],
    *,
    point_binding: Mapping[str, Any],
    option_market_evidence: Mapping[str, Any] | None = None,
    require_option_market_evidence: bool = False,
) -> dict[str, Any]:
    strict_evidence = option_market_evidence is not None
    if require_option_market_evidence and not strict_evidence:
        _fail("option market evidence is required")
    binding = _validate_point_binding(
        point_binding,
        require_option_market_evidence=strict_evidence,
    )
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

    evidence: dict[str, Any] | None = None
    evidence_at_ms: int | None = None
    if strict_evidence:
        assert option_market_evidence is not None
        evidence = dict(option_market_evidence)
        content_sha256 = evidence.get("content_sha256")
        if (
            evidence.get("status") != "ready"
            or evidence.get("run_id") != binding["run_id"]
            or evidence.get("account") != binding["account"]
            or evidence.get("account_config_sha256")
            != snapshot.get("account_config_sha256")
            or not isinstance(content_sha256, str)
            or canonical_sha256(
                {
                    key: value
                    for key, value in evidence.items()
                    if key != "content_sha256"
                }
            )
            != content_sha256
        ):
            _fail("option market evidence is invalid")
        for field in (
            "open_option_positions",
            "valuation_mark_facts",
            "fx_rate_facts",
        ):
            if not isinstance(evidence.get(field), list):
                _fail(f"option market evidence {field} must be a list")
        try:
            evidence_at = datetime.fromisoformat(
                str(evidence["evidence_at_utc"]).replace("Z", "+00:00")
            )
        except (KeyError, ValueError) as exc:
            raise Top1RankingError(
                "ranking_projection_incomplete",
                "option market evidence timestamp is invalid",
            ) from exc
        if evidence_at.utcoffset() != timezone.utc.utcoffset(evidence_at):
            _fail("option market evidence timestamp must be UTC")
        evidence_at_ms = int(evidence_at.timestamp() * 1000)

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
            candidate = {
                "candidate_id": decision["candidate_id"],
                "producer_rank": decision["opening_snapshot_rank"],
                **{field: normalized[field] for field in _CANDIDATE_SOURCE_FIELDS},
            }
            if evidence is not None:
                assert evidence_at_ms is not None
                candidate.update(
                    {
                        "option_type": normalized.get("option_type"),
                        "bid": normalized.get("bid"),
                        "ask": normalized.get("ask"),
                        "bid_volume": normalized.get("bid_volume"),
                        "ask_volume": normalized.get("ask_volume"),
                        "last": normalized.get("last_price"),
                        "implied_volatility": normalized.get(
                            "implied_volatility"
                        ),
                        "delta": normalized.get("delta"),
                        "gamma": normalized.get("gamma"),
                        "theta": normalized.get("theta"),
                        "vega": normalized.get("vega"),
                        "rho": normalized.get("rho"),
                        "volume": normalized.get("volume"),
                        "spot": normalized.get("spot"),
                        "quote_effective_at_utc": _optional_utc_timestamp(
                            normalized.get("snapshot_requested_at_utc"),
                            "quote_effective_at_utc",
                        ),
                        "quote_observed_at_utc": _optional_utc_timestamp(
                            normalized.get("snapshot_received_at_utc"),
                            "quote_observed_at_utc",
                        ),
                        "quote_status": normalized.get(
                            "opening_contract_status"
                        ),
                    }
                )
                currency = str(candidate["currency"])
                fx_fact = next(
                    (
                        row
                        for row in evidence["fx_rate_facts"]
                        if isinstance(row, Mapping)
                        and row.get("base_currency") == currency
                        and row.get("quote_currency") == "CNY"
                    ),
                    None,
                )
                opening_fx_binding = (
                    None
                    if currency == "CNY"
                    else build_fx_rate_binding_from_projection(
                        fx_fact,
                        selected_at_ms=evidence_at_ms,
                    )
                )
                metric = calculate_option_market_concentration_after(
                    candidate=candidate,
                    open_option_positions=evidence["open_option_positions"],
                    valuation_mark_facts=evidence["valuation_mark_facts"],
                    fx_rate_facts=evidence["fx_rate_facts"],
                )
                candidate.update(
                    {
                        "option_market_concentration_after": metric[
                            "option_market_concentration_after"
                        ],
                        "option_market_value_cny": metric[
                            "option_market_value_cny"
                        ],
                        "option_market_concentration_metric_version": metric[
                            "metric_version"
                        ],
                        "option_market_evidence_refs": {
                            "prepared_evidence_ref": binding[
                                "option_market_evidence_ref"
                            ],
                            "prepared_evidence_content_sha256": evidence[
                                "content_sha256"
                            ],
                            **metric["evidence_refs"],
                        },
                        "opening_fx_binding": opening_fx_binding,
                    }
                )
            candidates.append(candidate)
    except (KeyError, TypeError, ValueError, OpeningCandidateSnapshotError) as exc:
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
        "schema_version": (
            RANKING_PROJECTION_SCHEMA_V2
            if evidence is not None
            else RANKING_PROJECTION_SCHEMA_V1
        ),
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


def _formal_point_ranking_input(
    formal_point: Mapping[str, Any],
) -> dict[str, Any]:
    point = formal_point.get("recommendation_point")
    opening = formal_point.get("opening_snapshot")
    evidence = formal_point.get("option_position_evidence_binding")
    if not all(isinstance(value, Mapping) for value in (point, opening, evidence)):
        _fail("formal point facts are incomplete")
    assert isinstance(point, Mapping)
    projection = build_ranking_projection(
        opening,
        point_binding={
            "recommendation_point_id": point["recommendation_point_id"],
            "market": point["market"],
            "account": point["account"],
            "run_id": point["run_id"],
            "opening_snapshot_ref": point["opening_snapshot_ref"],
            "opening_snapshot_sha256": point["opening_snapshot_sha256"],
            "decision_at_utc": point["decision_at_utc"],
            "source_commit_sha": point["source_commit_sha"],
            "option_market_evidence_ref": point[
                "prepared_context_manifest_ref"
            ],
            "option_market_evidence_manifest_sha256": point[
                "prepared_context_manifest_sha256"
            ],
            "option_market_evidence_payload_sha256": point[
                "prepared_context_payload_sha256"
            ],
        },
        option_market_evidence=evidence,
        require_option_market_evidence=True,
    )
    if projection["producer_accepted_candidate_ids"] != point[
        "producer_accepted_candidate_ids"
    ]:
        _fail("formal point accepted candidate IDs changed")
    return projection


def build_top1_recipe_projection(
    formal_point: Mapping[str, Any],
    *,
    formal_point_ref: str,
) -> dict[str, Any]:
    formal_ref = _relative_posix_path(formal_point_ref, "formal_point_ref")
    formal_hash = _hash(
        formal_point.get("content_sha256"),
        "formal_point_content_sha256",
        _HASH_64,
    )
    materialized = _formal_point_ranking_input(formal_point)
    payload = {
        "schema_version": RANKING_PROJECTION_SCHEMA_V3,
        "formal_point_ref": formal_ref,
        "formal_point_content_sha256": formal_hash,
        "recipe_id": RECIPE_ID,
        "recipe_version": RECIPE_VERSION,
        "behavior_binding_sha256": recipe_projection_behavior_sha256(),
        "materialized_input_content_sha256": materialized["artifact_provenance"][
            "content_sha256"
        ],
        "producer_accepted_candidate_ids": list(
            materialized["producer_accepted_candidate_ids"]
        ),
        "candidates": [
            {key: candidate[key] for key in _CANDIDATE_KEYS_V3}
            for candidate in materialized["candidates"]
        ],
    }
    attach_artifact_provenance(
        payload,
        artifact_kind=RANKING_PROJECTION_ARTIFACT_KIND,
        source_generation={
            "generation_id": f"formal_point:{formal_hash}",
            "revision": 1,
            "source_ref": formal_ref,
            "source_sha256": formal_hash,
        },
    )
    return validate_ranking_projection(payload)


def materialize_top1_recipe_input(
    formal_point: Mapping[str, Any],
    recipe_projection: Mapping[str, Any],
) -> dict[str, Any]:
    compact = validate_ranking_projection(recipe_projection)
    if compact["schema_version"] != RANKING_PROJECTION_SCHEMA_V3:
        _fail("recipe projection must use v3")
    if compact["formal_point_content_sha256"] != formal_point.get("content_sha256"):
        _fail("recipe projection formal point binding changed")
    materialized = _formal_point_ranking_input(formal_point)
    if compact["materialized_input_content_sha256"] != materialized[
        "artifact_provenance"
    ]["content_sha256"]:
        _fail("recipe projection materialized input binding changed")
    expected = {
        candidate["candidate_id"]: {
            key: candidate[key] for key in _CANDIDATE_KEYS_V3
        }
        for candidate in materialized["candidates"]
    }
    if compact["producer_accepted_candidate_ids"] != materialized[
        "producer_accepted_candidate_ids"
    ] or compact["candidates"] != [
        expected[candidate_id]
        for candidate_id in materialized["producer_accepted_candidate_ids"]
    ]:
        _fail("recipe projection metrics changed")
    return materialized


def _validate_recipe_projection(payload: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(payload)
    _exact_keys(item, _PROJECTION_KEYS_V3, "recipe projection")
    _relative_posix_path(item["formal_point_ref"], "formal_point_ref")
    formal_hash = _hash(
        item["formal_point_content_sha256"],
        "formal_point_content_sha256",
        _HASH_64,
    )
    if item["recipe_id"] != RECIPE_ID or item["recipe_version"] != RECIPE_VERSION:
        _fail("recipe projection identity is invalid")
    if item["behavior_binding_sha256"] != recipe_projection_behavior_sha256():
        _fail("recipe projection behavior binding changed")
    _hash(
        item["materialized_input_content_sha256"],
        "materialized_input_content_sha256",
        _HASH_64,
    )
    candidate_ids = item["producer_accepted_candidate_ids"]
    candidates = item["candidates"]
    if (
        not isinstance(candidate_ids, list)
        or any(not isinstance(value, str) or not value for value in candidate_ids)
        or len(candidate_ids) != len(set(candidate_ids))
        or not isinstance(candidates, list)
        or len(candidates) != len(candidate_ids)
    ):
        _fail("recipe projection candidates are invalid")
    projected_ids: list[str] = []
    for raw_candidate in candidates:
        if not isinstance(raw_candidate, Mapping):
            _fail("recipe projection candidate must be an object")
        candidate = dict(raw_candidate)
        _exact_keys(candidate, _CANDIDATE_KEYS_V3, "recipe projection candidate")
        projected_ids.append(_text(candidate["candidate_id"], "candidate_id"))
        _finite_number(
            candidate["option_market_concentration_after"],
            "option_market_concentration_after",
        )
        if not 0 <= float(candidate["option_market_concentration_after"]) <= 1:
            _fail("option market concentration must be between zero and one")
        _finite_number(
            candidate["option_market_value_cny"],
            "option_market_value_cny",
            positive=True,
        )
        if (
            candidate["option_market_concentration_metric_version"]
            != OPTION_MARKET_CONCENTRATION_METRIC_VERSION
        ):
            _fail("option market concentration metric version is invalid")
        refs = candidate["option_market_evidence_refs"]
        if not isinstance(refs, Mapping) or set(refs) != {
            "prepared_evidence_ref",
            "prepared_evidence_content_sha256",
            "position_lot_ids",
            "valuation_mark_fact_ids",
            "fx_rate_fact_ids",
        }:
            _fail("option market evidence refs are incomplete or unexpected")
        _relative_posix_path(
            refs["prepared_evidence_ref"], "prepared_evidence_ref"
        )
        _hash(
            refs["prepared_evidence_content_sha256"],
            "prepared_evidence_content_sha256",
            _HASH_64,
        )
        for field in (
            "position_lot_ids",
            "valuation_mark_fact_ids",
            "fx_rate_fact_ids",
        ):
            values = refs[field]
            if (
                not isinstance(values, list)
                or any(not isinstance(value, str) or not value for value in values)
                or values != sorted(set(values))
            ):
                _fail(f"{field} must be a sorted unique string list")
        binding = candidate["opening_fx_binding"]
        if binding is not None:
            try:
                validate_fx_rate_binding(binding)
            except ValueError as exc:
                _fail(f"opening FX binding is invalid: {exc}")
    if projected_ids != candidate_ids:
        _fail("recipe projection candidate order changed")
    provenance = item["artifact_provenance"]
    if not isinstance(provenance, Mapping):
        _fail("artifact provenance must be an object")
    source = provenance.get("source_generation")
    if source != {
        "generation_id": f"formal_point:{formal_hash}",
        "revision": 1,
        "source_ref": item["formal_point_ref"],
        "source_sha256": formal_hash,
    }:
        _fail("recipe projection source generation changed")
    try:
        validation = validate_artifact_provenance(
            item,
            artifact_kind=RANKING_PROJECTION_ARTIFACT_KIND,
            schema_version=RANKING_PROJECTION_SCHEMA_V3,
        )
    except (TypeError, ValueError) as exc:
        _fail(f"artifact provenance is invalid: {exc}")
    if not validation["trusted"]:
        _fail("artifact provenance is invalid")
    return item


def validate_ranking_projection(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        _fail("ranking projection must be an object")
    item = dict(payload)
    schema = item.get("schema_version")
    if schema == RANKING_PROJECTION_SCHEMA_V3:
        return _validate_recipe_projection(item)
    if schema not in {
        RANKING_PROJECTION_SCHEMA_V1,
        RANKING_PROJECTION_SCHEMA_V2,
    }:
        _fail("ranking projection schema is invalid")
    is_v2 = schema == RANKING_PROJECTION_SCHEMA_V2
    expected_projection_keys = _PROJECTION_KEYS_V2 if is_v2 else _PROJECTION_KEYS
    expected_point_keys = _POINT_KEYS_V2 if is_v2 else _POINT_KEYS
    expected_candidate_keys = _CANDIDATE_KEYS_V2 if is_v2 else _CANDIDATE_KEYS
    _exact_keys(item, expected_projection_keys, "ranking projection")
    _validate_point_binding(
        {field: item[field] for field in expected_point_keys},
        require_option_market_evidence=is_v2,
    )
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
        _exact_keys(candidate, expected_candidate_keys, "projected candidate")
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
        if is_v2:
            if candidate["option_type"] != "put":
                _fail("projected option type must equal put")
            for field in _CANDIDATE_MARKET_NUMERIC_FIELDS:
                value = candidate[field]
                if value is not None:
                    _finite_number(value, field)
            for field in ("bid", "ask", "spot"):
                value = candidate[field]
                if value is not None and float(value) <= 0:
                    _fail(f"{field} must be positive when available")
            for field in ("bid_volume", "ask_volume", "volume"):
                value = candidate[field]
                if value is not None and float(value) < 0:
                    _fail(f"{field} cannot be negative")
            for field in (
                "quote_effective_at_utc",
                "quote_observed_at_utc",
            ):
                value = candidate[field]
                if value is not None:
                    _utc_timestamp(value, field)
            if candidate["quote_status"] is not None:
                _text(candidate["quote_status"], "quote_status")
            _finite_number(
                candidate["option_market_concentration_after"],
                "option_market_concentration_after",
            )
            concentration = float(candidate["option_market_concentration_after"])
            if not 0 <= concentration <= 1:
                _fail("option market concentration must be between zero and one")
            _finite_number(
                candidate["option_market_value_cny"],
                "option_market_value_cny",
                positive=True,
            )
            if (
                candidate["option_market_concentration_metric_version"]
                != OPTION_MARKET_CONCENTRATION_METRIC_VERSION
            ):
                _fail("option market concentration metric version is invalid")
            refs = candidate["option_market_evidence_refs"]
            if not isinstance(refs, Mapping):
                _fail("option market evidence refs must be an object")
            if set(refs) != {
                "prepared_evidence_ref",
                "prepared_evidence_content_sha256",
                "position_lot_ids",
                "valuation_mark_fact_ids",
                "fx_rate_fact_ids",
            }:
                _fail("option market evidence refs are incomplete or unexpected")
            if refs["prepared_evidence_ref"] != item["option_market_evidence_ref"]:
                _fail("prepared option evidence ref does not match")
            _hash(
                refs["prepared_evidence_content_sha256"],
                "prepared_evidence_content_sha256",
                _HASH_64,
            )
            for field in (
                "position_lot_ids",
                "valuation_mark_fact_ids",
                "fx_rate_fact_ids",
            ):
                values = refs[field]
                if (
                    not isinstance(values, list)
                    or any(not isinstance(value, str) or not value for value in values)
                    or values != sorted(values)
                    or len(values) != len(set(values))
                ):
                    _fail(f"{field} must be a sorted unique string list")
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
            schema_version=str(schema),
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
    near_return_threshold: float = 0.002,
) -> dict[str, Any]:
    item = validate_ranking_projection(projection)
    if item["schema_version"] == RANKING_PROJECTION_SCHEMA_V3:
        _fail("recipe projection must be materialized before reranking")
    ranked = rank_candidate_rows(
        [dict(candidate) for candidate in item["candidates"]],
        mode="put",
        sell_put_ranking_profile=ranking_profile,
        near_return_threshold=near_return_threshold,
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
    result = {
        "schema_version": (
            "sell_put_recommendation_ranking_result.v2"
            if item["schema_version"] == RANKING_PROJECTION_SCHEMA_V2
            else RANKING_RESULT_SCHEMA_VERSION
        ),
        "ranking_profile": ranking_profile,
        "ranking_projection_sha256": item["artifact_provenance"]["content_sha256"],
        "ordered_candidate_ids": ranked_ids,
        "top1_candidate_id": ranked_ids[0] if ranked_ids else None,
        "parity_status": (
            "matched" if ranking_profile == "current_tie_break" else "not_applicable"
        ),
    }
    if item["schema_version"] == RANKING_PROJECTION_SCHEMA_V2:
        result["near_return_threshold"] = float(near_return_threshold)
    return result
