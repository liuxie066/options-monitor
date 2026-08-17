from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from datetime import date, datetime, timezone
from typing import Any, NoReturn, cast

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.fee_calc import FUTU_HK_TERMINAL_FEE_SCHEDULE_VERSION
from src.application.shadow_replay.common import render_json_text
from src.application.strategy_lab.top1.contracts import (
    RECOMMENDATION_POINT_SELECTOR,
    RESEARCH_REQUIRED_DAYS,
    SEALED_HISTORICAL_DATASET_SCHEMA,
    Top1CoreContractError,
    build_research_spec_sha256,
    validate_experiment_spec,
)
from src.application.strategy_lab.top1.economics import calculate_expiry_efficiency
from src.application.strategy_lab.top1.ranking import (
    RANKING_PROJECTION_SCHEMA_VERSION,
    Top1RankingError,
    rerank_recommendation_point,
    validate_ranking_projection,
)
from src.application.strategy_lab.top1.statistics import (
    summarize_paired_daily_deltas,
)


RESEARCH_EVALUATION_INPUT_SCHEMA = "sell_put_top1_research_evaluation_input.v1"
RESEARCH_CLOSE_RECEIPT_SCHEMA = "sell_put_top1_research_close_receipt.v1"
RESEARCH_EVALUATION_SCHEMA = "sell_put_top1_research_evaluation.v1"
INTERNAL_RESEARCH_REVISION_SCHEMA = "sell_put_top1_research_revision.v1"
INTERNAL_RESEARCH_QUOTA_DECISION_SCHEMA = (
    "sell_put_top1_research_quota_decision.v1"
)

_HASH_64 = re.compile(r"[0-9a-f]{64}\Z")
_INPUT_KEYS = frozenset(
    {
        "schema_version",
        "experiment_spec",
        "dataset_ref",
        "sealed_dataset",
        "ranking_projections",
    }
)
_DATASET_KEYS = frozenset(
    {
        "schema_version",
        "market",
        "account",
        "cutoff_at_utc",
        "cutoff_trading_date",
        "required_days",
        "window_facts_content_sha256",
        "market_calendar_version",
        "market_calendar_ref",
        "market_calendar_sha256",
        "trading_calendar_dates_sha256",
        "latest_mature_trading_date",
        "maturity_evidence_ref",
        "maturity_evidence_sha256",
        "recommendation_point_selector",
        "ranking_projection_schema_version",
        "selected_trading_dates",
        "days",
        "content_sha256",
    }
)
_DAY_KEYS = frozenset(
    {
        "trading_date",
        "expectation_ref",
        "expectation_content_sha256",
        "expectation_file_sha256",
        "points",
    }
)
_POINT_KEYS = frozenset(
    {
        "recommendation_point_id",
        "projection_ref",
        "projection_content_sha256",
        "projection_file_sha256",
    }
)
_MATERIALIZED_PROJECTION_KEYS = frozenset({"projection_ref", "projection"})
_CLOSE_KEYS = frozenset(
    {
        "schema_version",
        "market",
        "account",
        "stock_owner",
        "expiration",
        "spot_source",
        "ktype",
        "autype",
        "price_field",
        "status",
        "underlier_close",
        "reason_detail",
    }
)
_CLOSE_FAILURE_DETAILS = frozenset(
    {
        "expiry_calendar_mismatch",
        "expiry_close_missing_after_deadline",
        "expiry_source_unavailable_after_deadline",
        "expiry_close_receipt_conflict",
        "expiry_outcome_conflict",
    }
)
_FEE_KEYS = frozenset(
    {"market", "account", "fee_schedule_version", "account_fee_plan"}
)
_FEE_PLAN_KEYS = frozenset({"commission_free", "platform_fee", "fee_plan_ref"})
_REVISION_KEYS = frozenset(
    {"schema_version", "evaluation", "fee_contract", "history_kline_evidence"}
)
_HISTORY_EVIDENCE_KEYS = frozenset(
    {"observed_at_utc", "page_complete", "quota_decision", "close_receipts"}
)
_QUOTA_DECISION_KEYS = frozenset(
    {
        "schema_version",
        "required_stock_owners",
        "already_counted_stock_owners",
        "new_stock_owners",
        "remain_quota",
    }
)
_STAT_FIELDS = (
    "required_days",
    "effective_days",
    "mean_daily_delta",
    "sample_std",
    "standard_error",
    "t_critical",
    "one_sided_lower_bound",
    "worst_k",
    "worst_tail_mean",
    "serial_correlation_unadjusted",
)


class ResearchEvaluationError(ValueError):
    """Stable fail-closed error from the pure research boundary."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _fail(reason_code: str, message: str) -> NoReturn:
    raise ResearchEvaluationError(reason_code, message)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _fail("research_input_invalid", f"{label} must be a mapping")
    raw = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in raw):
        _fail("research_input_invalid", f"{label} keys must be strings")
    return cast(Mapping[str, object], raw)


def _exact_keys(
    value: Mapping[str, object],
    expected: frozenset[str],
    label: str,
    *,
    reason_code: str = "research_input_invalid",
) -> None:
    if set(value) != set(expected):
        _fail(reason_code, f"{label} keys are incomplete or unexpected")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _fail("research_input_invalid", f"{label} must be non-empty canonical text")
    return value


def _hash(value: object, label: str, *, reason_code: str) -> str:
    text = _text(value, label)
    if _HASH_64.fullmatch(text) is None:
        _fail(reason_code, f"{label} must be a lowercase SHA-256")
    return text


def _relative_ref(value: object, label: str) -> str:
    text = _text(value, label)
    parts = text.split("/")
    if text.startswith("/") or "\\" in text or any(
        part in {"", ".", ".."} for part in parts
    ):
        _fail("research_input_invalid", f"{label} must be a safe relative POSIX path")
    return text


def _iso_date(value: object, label: str) -> str:
    text = _text(value, label)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ResearchEvaluationError(
            "research_input_invalid", f"{label} must be a canonical ISO date"
        ) from exc
    if parsed.isoformat() != text:
        _fail("research_input_invalid", f"{label} must be a canonical ISO date")
    return text


def _canonical_file_sha256(payload: Mapping[str, object]) -> str:
    text = render_json_text(dict(payload))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _validate_dataset(
    value: object,
    *,
    dataset_ref: str,
    spec: Mapping[str, object],
) -> tuple[dict[str, object], list[dict[str, str]]]:
    item = _mapping(value, "sealed_dataset")
    _exact_keys(
        item,
        _DATASET_KEYS,
        "sealed_dataset",
        reason_code="research_corpus_conflict",
    )
    if item["schema_version"] != SEALED_HISTORICAL_DATASET_SCHEMA:
        _fail("research_corpus_conflict", "sealed dataset schema is unsupported")
    if item["market"] != spec["market"] or item["account"] != spec["account"]:
        _fail("research_corpus_conflict", "sealed dataset identity does not match spec")
    if item["required_days"] != RESEARCH_REQUIRED_DAYS:
        _fail(
            "research_corpus_conflict",
            f"sealed dataset must contain {RESEARCH_REQUIRED_DAYS} days",
        )
    if item["recommendation_point_selector"] != RECOMMENDATION_POINT_SELECTOR:
        _fail("research_corpus_conflict", "sealed dataset selector is unsupported")
    if item["ranking_projection_schema_version"] != RANKING_PROJECTION_SCHEMA_VERSION:
        _fail("research_corpus_conflict", "sealed dataset projection schema changed")

    source = _mapping(spec["research_source"], "experiment_spec.research_source")
    economics = _mapping(
        spec["economics_contracts"], "experiment_spec.economics_contracts"
    )
    if dataset_ref != source["dataset_ref"]:
        _fail("research_corpus_conflict", "dataset ref does not match spec")
    if item["cutoff_at_utc"] != source["research_cutoff_at"]:
        _fail("research_corpus_conflict", "dataset cutoff does not match spec")
    if item["market_calendar_version"] != economics["market_calendar_version"]:
        _fail("research_corpus_conflict", "dataset calendar does not match spec")

    supplied_content_hash = _hash(
        item["content_sha256"],
        "sealed_dataset.content_sha256",
        reason_code="research_corpus_conflict",
    )
    content_source = {
        key: raw_value for key, raw_value in item.items() if key != "content_sha256"
    }
    if canonical_sha256(content_source) != supplied_content_hash:
        _fail("research_corpus_conflict", "sealed dataset content hash mismatch")
    if _canonical_file_sha256(item) != source["dataset_sha256"]:
        _fail("research_corpus_conflict", "sealed dataset file hash mismatch")

    raw_dates = item["selected_trading_dates"]
    raw_days = item["days"]
    if not isinstance(raw_dates, list) or not isinstance(raw_days, list):
        _fail("research_corpus_conflict", "sealed dataset dates and days must be lists")
    selected_dates = [
        _iso_date(raw, f"sealed_dataset.selected_trading_dates[{index}]")
        for index, raw in enumerate(cast(list[object], raw_dates))
    ]
    if (
        len(selected_dates) != RESEARCH_REQUIRED_DAYS
        or len(set(selected_dates)) != RESEARCH_REQUIRED_DAYS
        or selected_dates != sorted(selected_dates)
    ):
        _fail("research_corpus_conflict", "sealed dataset dates are not exact and ordered")
    if (
        selected_dates[0] != source["start_trading_date"]
        or selected_dates[-1] != source["end_trading_date"]
        or item["latest_mature_trading_date"] != selected_dates[-1]
    ):
        _fail("research_corpus_conflict", "sealed dataset window does not match spec")
    if len(raw_days) != RESEARCH_REQUIRED_DAYS:
        _fail("research_corpus_conflict", "sealed dataset day count is incomplete")

    point_rows: list[dict[str, str]] = []
    point_ids: set[str] = set()
    for day_index, raw_day in enumerate(cast(list[object], raw_days)):
        day = _mapping(raw_day, f"sealed_dataset.days[{day_index}]")
        _exact_keys(
            day,
            _DAY_KEYS,
            f"sealed_dataset.days[{day_index}]",
            reason_code="research_corpus_conflict",
        )
        trading_date = _iso_date(
            day["trading_date"], f"sealed_dataset.days[{day_index}].trading_date"
        )
        if trading_date != selected_dates[day_index]:
            _fail("research_corpus_conflict", "sealed dataset day order changed")
        _ = _relative_ref(day["expectation_ref"], "sealed_dataset.expectation_ref")
        _ = _hash(
            day["expectation_content_sha256"],
            "sealed_dataset.expectation_content_sha256",
            reason_code="research_corpus_conflict",
        )
        _ = _hash(
            day["expectation_file_sha256"],
            "sealed_dataset.expectation_file_sha256",
            reason_code="research_corpus_conflict",
        )
        raw_points = day["points"]
        if not isinstance(raw_points, list) or not raw_points:
            _fail("research_corpus_conflict", "sealed dataset day has no point denominator")
        for point_index, raw_point in enumerate(cast(list[object], raw_points)):
            point = _mapping(
                raw_point,
                f"sealed_dataset.days[{day_index}].points[{point_index}]",
            )
            _exact_keys(
                point,
                _POINT_KEYS,
                "sealed dataset point",
                reason_code="research_corpus_conflict",
            )
            point_id = _text(point["recommendation_point_id"], "recommendation_point_id")
            if point_id in point_ids:
                _fail("research_corpus_conflict", "recommendation point is duplicated")
            point_ids.add(point_id)
            point_rows.append(
                {
                    "trading_date": trading_date,
                    "recommendation_point_id": point_id,
                    "projection_ref": _relative_ref(
                        point["projection_ref"], "projection_ref"
                    ),
                    "projection_content_sha256": _hash(
                        point["projection_content_sha256"],
                        "projection_content_sha256",
                        reason_code="research_corpus_conflict",
                    ),
                    "projection_file_sha256": _hash(
                        point["projection_file_sha256"],
                        "projection_file_sha256",
                        reason_code="research_corpus_conflict",
                    ),
                }
            )
    return dict(item), point_rows


def _validate_projections(
    value: object,
    *,
    point_rows: list[dict[str, str]],
    market: str,
    account: str,
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list):
        _fail("research_input_invalid", "ranking_projections must be a list")
    supplied: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(cast(list[object], value)):
        item = _mapping(raw, f"ranking_projections[{index}]")
        _exact_keys(item, _MATERIALIZED_PROJECTION_KEYS, f"ranking_projections[{index}]")
        ref = _relative_ref(item["projection_ref"], "ranking_projection.projection_ref")
        if ref in supplied:
            _fail("research_corpus_conflict", "materialized projection ref is duplicated")
        projection_mapping = _mapping(item["projection"], "ranking_projection.projection")
        try:
            projection = validate_ranking_projection(cast(Mapping[str, Any], projection_mapping))
        except Top1RankingError as exc:
            _fail(exc.reason_code, str(exc))
        supplied[ref] = projection

    expected_refs = {point["projection_ref"] for point in point_rows}
    if set(supplied) != expected_refs:
        _fail("research_corpus_conflict", "materialized projections do not match dataset")
    for point in point_rows:
        projection = supplied[point["projection_ref"]]
        if (
            projection["recommendation_point_id"] != point["recommendation_point_id"]
            or projection["market"] != market
            or projection["account"] != account
            or projection["artifact_provenance"]["content_sha256"]
            != point["projection_content_sha256"]
            or _canonical_file_sha256(projection) != point["projection_file_sha256"]
        ):
            _fail("research_corpus_conflict", "materialized projection binding changed")
    return supplied


def _validate_close_receipts(
    value: object, *, market: str, account: str
) -> dict[tuple[str, str], list[dict[str, object]]]:
    if not isinstance(value, list):
        _fail("research_input_invalid", "close_receipts must be a list")
    indexed: dict[tuple[str, str], list[dict[str, object]]] = {}
    for index, raw in enumerate(cast(list[object], value)):
        item = _mapping(raw, f"close_receipts[{index}]")
        _exact_keys(item, _CLOSE_KEYS, f"close_receipts[{index}]")
        if item["schema_version"] != RESEARCH_CLOSE_RECEIPT_SCHEMA:
            _fail("research_input_invalid", "close receipt schema is unsupported")
        if item["market"] != market or item["account"] != account:
            _fail("research_input_invalid", "close receipt identity does not match")
        if (
            item["spot_source"] != "opend_history_kline"
            or item["ktype"] != "K_DAY"
            or item["autype"] != "NONE"
            or item["price_field"] != "close"
        ):
            _fail("research_input_invalid", "close receipt source semantics changed")
        stock_owner = _text(item["stock_owner"], "close_receipt.stock_owner")
        expiration = _iso_date(item["expiration"], "close_receipt.expiration")
        status = item["status"]
        close = item["underlier_close"]
        reason = item["reason_detail"]
        if status == "available":
            if (
                isinstance(close, bool)
                or not isinstance(close, (int, float))
                or not math.isfinite(float(close))
                or float(close) <= 0
                or reason is not None
            ):
                _fail("research_input_invalid", "available close receipt is invalid")
        elif status == "unavailable":
            if close is not None or reason not in _CLOSE_FAILURE_DETAILS:
                _fail("research_input_invalid", "unavailable close receipt is invalid")
        else:
            _fail("research_input_invalid", "close receipt status is unsupported")
        indexed.setdefault((stock_owner, expiration), []).append(dict(item))
    return indexed


def _validate_fee_contract(
    value: object, *, spec: Mapping[str, object]
) -> Mapping[str, object] | None:
    item = _mapping(value, "fee_contract")
    _exact_keys(item, _FEE_KEYS, "fee_contract")
    if item["market"] != spec["market"] or item["account"] != spec["account"]:
        _fail("research_input_invalid", "fee contract identity does not match")
    economics = _mapping(spec["economics_contracts"], "experiment_spec.economics_contracts")
    if (
        item["fee_schedule_version"] != FUTU_HK_TERMINAL_FEE_SCHEDULE_VERSION
        or item["fee_schedule_version"] != economics["fee_schedule_version"]
    ):
        _fail("research_input_invalid", "fee schedule version does not match")
    raw_plan = item["account_fee_plan"]
    if raw_plan is None:
        return None
    plan = _mapping(raw_plan, "fee_contract.account_fee_plan")
    if not set(plan).issubset(_FEE_PLAN_KEYS):
        _fail("research_input_invalid", "account fee plan contains unexpected keys")
    return dict(plan)


def _candidate(
    projection: Mapping[str, Any], candidate_id: str | None
) -> Mapping[str, object] | None:
    if candidate_id is None:
        return None
    for raw in cast(list[object], projection["candidates"]):
        candidate = cast(Mapping[str, object], raw)
        if candidate["candidate_id"] == candidate_id:
            return candidate
    _fail("ranking_projection_incomplete", "selected candidate is absent from projection")


def _base_result(
    *,
    spec: Mapping[str, object],
    dataset_ref: str,
    sealed_dataset: Mapping[str, object],
    effective_days: int | None,
    selection: str,
    leader_variant_id: str | None,
    reason_codes: list[str],
    reason_details: list[str],
    variant_results: list[dict[str, object]],
    missing_receipts: list[dict[str, object]],
) -> dict[str, object]:
    source = _mapping(spec["research_source"], "experiment_spec.research_source")
    return {
        "schema_version": RESEARCH_EVALUATION_SCHEMA,
        "experiment_id": spec["experiment_id"],
        "research_spec_sha256": build_research_spec_sha256(spec),
        "dataset_ref": dataset_ref,
        "dataset_sha256": source["dataset_sha256"],
        "dataset_content_sha256": sealed_dataset["content_sha256"],
        "required_days": RESEARCH_REQUIRED_DAYS,
        "effective_days": effective_days,
        "research_fill_assumption": "t0_sell_limit",
        "research_is_counterfactual": True,
        "contract_terms_revalidated": False,
        "selection": selection,
        "leader_variant_id": leader_variant_id,
        "reason_codes": reason_codes,
        "reason_details": reason_details,
        "variant_results": variant_results,
        "missing_receipts": missing_receipts,
    }


def _insufficient_before_statistics(
    *,
    spec: Mapping[str, object],
    dataset_ref: str,
    sealed_dataset: Mapping[str, object],
    reasons: list[tuple[str, str | None]],
    missing_receipts: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    ordered = sorted(set(reasons), key=lambda item: (item[0], item[1] or ""))
    return _base_result(
        spec=spec,
        dataset_ref=dataset_ref,
        sealed_dataset=sealed_dataset,
        effective_days=None,
        selection="insufficient_evidence",
        leader_variant_id=None,
        reason_codes=_unique([reason for reason, _detail in ordered]),
        reason_details=_unique(
            [detail for _reason, detail in ordered if detail is not None]
        ),
        variant_results=[],
        missing_receipts=sorted(
            missing_receipts or [],
            key=lambda item: (
                str(item["stock_owner"]),
                str(item["expiration"]),
                str(item["reason_code"]),
                str(item["reason_detail"]),
            ),
        ),
    )


def _validated_research_input(
    dataset: object,
) -> tuple[
    dict[str, Any],
    str,
    dict[str, object],
    list[dict[str, str]],
    dict[str, dict[str, Any]],
]:
    envelope = _mapping(dataset, "dataset")
    _exact_keys(envelope, _INPUT_KEYS, "dataset")
    if envelope["schema_version"] != RESEARCH_EVALUATION_INPUT_SCHEMA:
        _fail("research_input_invalid", "research evaluation input schema is unsupported")
    try:
        spec = validate_experiment_spec(envelope["experiment_spec"])
    except Top1CoreContractError as exc:
        _fail("experiment_spec_invalid", str(exc))
    if "validation_evaluation" in spec:
        _fail("experiment_spec_invalid", "research evaluator requires a research-only spec")
    dataset_ref = _relative_ref(envelope["dataset_ref"], "dataset.dataset_ref")
    sealed_dataset, point_rows = _validate_dataset(
        envelope["sealed_dataset"], dataset_ref=dataset_ref, spec=spec
    )
    projections = _validate_projections(
        envelope["ranking_projections"],
        point_rows=point_rows,
        market=str(spec["market"]),
        account=str(spec["account"]),
    )
    return spec, dataset_ref, sealed_dataset, point_rows, projections


def _research_selections(
    *,
    spec: Mapping[str, object],
    point_rows: list[dict[str, str]],
    projections: Mapping[str, Mapping[str, Any]],
) -> tuple[
    list[tuple[str, str]],
    dict[str, list[dict[str, object]]],
    set[tuple[str, str]],
    bool,
]:

    variants: list[tuple[str, str]] = []
    for raw_variant in cast(list[object], spec["variants"])[1:]:
        variant = cast(Mapping[str, object], raw_variant)
        patch = cast(Mapping[str, object], variant["patch"])
        variants.append((str(variant["variant_id"]), str(patch["ranking_profile"])))

    selections: dict[str, list[dict[str, object]]] = {
        variant_id: [] for variant_id, _profile in variants
    }
    required_close_keys: set[tuple[str, str]] = set()
    currency_mismatch = False
    for point in point_rows:
        projection = projections[point["projection_ref"]]
        try:
            baseline_rank = rerank_recommendation_point(
                projection, ranking_profile="current_tie_break"
            )
        except Top1RankingError as exc:
            _fail(exc.reason_code, str(exc))
        baseline_id = cast(str | None, baseline_rank["top1_candidate_id"])
        baseline_candidate = _candidate(projection, baseline_id)
        for variant_id, profile in variants:
            try:
                challenger_rank = rerank_recommendation_point(
                    projection, ranking_profile=profile
                )
            except Top1RankingError as exc:
                _fail(exc.reason_code, str(exc))
            challenger_id = cast(str | None, challenger_rank["top1_candidate_id"])
            challenger_candidate = _candidate(projection, challenger_id)
            if (baseline_candidate is None) != (challenger_candidate is None):
                _fail(
                    "ranking_projection_incomplete",
                    "same accepted universe produced a one-sided Top1",
                )
            differs = baseline_id != challenger_id
            if differs:
                assert baseline_candidate is not None and challenger_candidate is not None
                for candidate in (baseline_candidate, challenger_candidate):
                    if candidate["currency"] != "HKD":
                        currency_mismatch = True
                    required_close_keys.add(
                        (str(candidate["stock_owner"]), str(candidate["expiration"]))
                    )
            selections[variant_id].append(
                {
                    "trading_date": point["trading_date"],
                    "recommendation_point_id": point["recommendation_point_id"],
                    "baseline_candidate_id": baseline_id,
                    "challenger_candidate_id": challenger_id,
                    "baseline_candidate": baseline_candidate,
                    "challenger_candidate": challenger_candidate,
                }
            )
    return variants, selections, required_close_keys, currency_mismatch


def required_research_close_keys(
    dataset: object,
    fee_contract: object,
) -> list[tuple[str, str]]:
    spec, _dataset_ref, _sealed_dataset, point_rows, projections = (
        _validated_research_input(dataset)
    )
    _ = _validate_fee_contract(fee_contract, spec=spec)
    _variants, _selections, required_close_keys, currency_mismatch = (
        _research_selections(
            spec=spec,
            point_rows=point_rows,
            projections=projections,
        )
    )
    return [] if currency_mismatch else sorted(required_close_keys)


def evaluate_research(
    dataset: object,
    close_receipts: object,
    fee_contract: object,
) -> dict[str, object]:
    spec, dataset_ref, sealed_dataset, point_rows, projections = (
        _validated_research_input(dataset)
    )
    receipts = _validate_close_receipts(
        close_receipts, market=str(spec["market"]), account=str(spec["account"])
    )
    fee_plan = _validate_fee_contract(fee_contract, spec=spec)
    variants, selections, required_close_keys, currency_mismatch = (
        _research_selections(
            spec=spec,
            point_rows=point_rows,
            projections=projections,
        )
    )
    if currency_mismatch:
        return _insufficient_before_statistics(
            spec=spec,
            dataset_ref=dataset_ref,
            sealed_dataset=sealed_dataset,
            reasons=[("ranking_projection_incomplete", "candidate_currency_mismatch")],
        )

    missing: list[dict[str, object]] = []
    for stock_owner, expiration in sorted(required_close_keys):
        matches = receipts.get((stock_owner, expiration), [])
        if not matches:
            missing.append(
                {
                    "stock_owner": stock_owner,
                    "expiration": expiration,
                    "reason_code": "research_expiry_close_missing",
                    "reason_detail": "expiry_close_missing_after_deadline",
                }
            )
        elif len(matches) > 1:
            missing.append(
                {
                    "stock_owner": stock_owner,
                    "expiration": expiration,
                    "reason_code": "required_outcome_missing",
                    "reason_detail": "expiry_close_receipt_conflict",
                }
            )
        elif matches[0]["status"] == "unavailable":
            missing.append(
                {
                    "stock_owner": stock_owner,
                    "expiration": expiration,
                    "reason_code": "required_outcome_missing",
                    "reason_detail": matches[0]["reason_detail"],
                }
            )
    if missing:
        return _insufficient_before_statistics(
            spec=spec,
            dataset_ref=dataset_ref,
            sealed_dataset=sealed_dataset,
            reasons=[
                (str(item["reason_code"]), str(item["reason_detail"]))
                for item in missing
            ],
            missing_receipts=missing,
        )

    economic_failures: list[tuple[str, str | None]] = []
    point_rows_by_variant: dict[str, list[dict[str, object]]] = {
        variant_id: [] for variant_id, _profile in variants
    }
    for variant_id, _profile in variants:
        for selection in selections[variant_id]:
            baseline = cast(Mapping[str, object] | None, selection["baseline_candidate"])
            challenger = cast(
                Mapping[str, object] | None, selection["challenger_candidate"]
            )
            baseline_efficiency: float | None = None
            challenger_efficiency: float | None = None
            if selection["baseline_candidate_id"] != selection["challenger_candidate_id"]:
                assert baseline is not None and challenger is not None
                results: list[dict[str, object]] = []
                for candidate in (baseline, challenger):
                    key = (str(candidate["stock_owner"]), str(candidate["expiration"]))
                    receipt = receipts[key][0]
                    try:
                        result = calculate_expiry_efficiency(
                            {
                                "stage": "research",
                                "fill_status": "t0_assumed_fill",
                                "holding_start_date": selection["trading_date"],
                                "expiration": candidate["expiration"],
                                "opening_net_premium": candidate["net_premium"],
                                "net_cash_basis": candidate["net_cash_basis"],
                                "strike": candidate["strike"],
                                "multiplier": candidate["multiplier"],
                                "underlier_close": receipt["underlier_close"],
                                "account_fee_plan": fee_plan,
                            }
                        )
                    except ValueError as exc:
                        _fail("ranking_projection_incomplete", str(exc))
                    results.append(result)
                    if result["status"] != "evaluable":
                        economic_failures.append(
                            (
                                str(result["reason_code"]),
                                (
                                    str(result["reason_detail"])
                                    if result["reason_detail"] is not None
                                    else None
                                ),
                            )
                        )
                baseline_efficiency = cast(float | None, results[0]["efficiency"])
                challenger_efficiency = cast(float | None, results[1]["efficiency"])
            point_rows_by_variant[variant_id].append(
                {
                    "recommendation_point_id": selection["recommendation_point_id"],
                    "trading_date": selection["trading_date"],
                    "baseline_candidate_id": selection["baseline_candidate_id"],
                    "challenger_candidate_id": selection["challenger_candidate_id"],
                    "baseline_efficiency": baseline_efficiency,
                    "challenger_efficiency": challenger_efficiency,
                    "hard_risk_status": "passed",
                    "baseline_concentration": (
                        baseline["symbol_concentration_after"]
                        if baseline is not None
                        else None
                    ),
                    "challenger_concentration": (
                        challenger["symbol_concentration_after"]
                        if challenger is not None
                        else None
                    ),
                }
            )
    if economic_failures:
        return _insufficient_before_statistics(
            spec=spec,
            dataset_ref=dataset_ref,
            sealed_dataset=sealed_dataset,
            reasons=economic_failures,
        )

    variant_results: list[dict[str, object]] = []
    for variant_id, profile in variants:
        summary = summarize_paired_daily_deltas(
            point_rows_by_variant[variant_id],
            {
                "required_days": RESEARCH_REQUIRED_DAYS,
                "confidence_level": 0.95,
                "worst_fraction": 0.20,
                "require_concentration_non_increase": True,
            },
        )
        result: dict[str, object] = {
            "variant_id": variant_id,
            "ranking_profile": profile,
            "decision": summary["decision"],
            "reason_codes": list(cast(list[str], summary["reason_codes"])),
            **{field: summary[field] for field in _STAT_FIELDS},
            "top1_change_count": sum(
                row["baseline_candidate_id"] != row["challenger_candidate_id"]
                for row in point_rows_by_variant[variant_id]
            ),
            "daily_deltas": list(cast(list[dict[str, object]], summary["daily_deltas"])),
        }
        variant_results.append(result)

    effective_days = min(int(result["effective_days"]) for result in variant_results)
    insufficient = [
        result
        for result in variant_results
        if result["decision"] == "insufficient_evidence"
    ]
    if insufficient:
        return _base_result(
            spec=spec,
            dataset_ref=dataset_ref,
            sealed_dataset=sealed_dataset,
            effective_days=effective_days,
            selection="insufficient_evidence",
            leader_variant_id=None,
            reason_codes=_unique(
                [
                    reason
                    for result in insufficient
                    for reason in cast(list[str], result["reason_codes"])
                ]
            ),
            reason_details=[],
            variant_results=variant_results,
            missing_receipts=[],
        )

    passing = [result for result in variant_results if result["decision"] == "pass"]
    if not passing:
        return _base_result(
            spec=spec,
            dataset_ref=dataset_ref,
            sealed_dataset=sealed_dataset,
            effective_days=effective_days,
            selection="no_research_winner",
            leader_variant_id=None,
            reason_codes=["no_research_winner"],
            reason_details=[],
            variant_results=variant_results,
            missing_receipts=[],
        )

    def leader_key(result: Mapping[str, object]) -> tuple[float, float, float, str]:
        return (
            -float(cast(float, result["mean_daily_delta"])),
            -float(cast(float, result["one_sided_lower_bound"])),
            -float(cast(float, result["worst_tail_mean"])),
            str(result["variant_id"]),
        )

    leader = min(passing, key=leader_key)
    return _base_result(
        spec=spec,
        dataset_ref=dataset_ref,
        sealed_dataset=sealed_dataset,
        effective_days=effective_days,
        selection="research_leader",
        leader_variant_id=str(leader["variant_id"]),
        reason_codes=list(cast(list[str], leader["reason_codes"])),
        reason_details=[],
        variant_results=variant_results,
        missing_receipts=[],
    )


def _utc_timestamp(value: object, label: str) -> str:
    text = _text(value, label)
    if not text.endswith("Z") or "T" not in text:
        _fail("research_revision_conflict", f"{label} must be an ISO-8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(f"{text[:-1]}+00:00")
    except ValueError as exc:
        raise ResearchEvaluationError(
            "research_revision_conflict",
            f"{label} must be an ISO-8601 UTC timestamp",
        ) from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        _fail("research_revision_conflict", f"{label} must be UTC")
    return text


def _normalized_fee_contract(
    value: object, *, spec: Mapping[str, object]
) -> dict[str, object]:
    item = _mapping(value, "research_revision.fee_contract")
    plan = _validate_fee_contract(item, spec=spec)
    return {
        "market": spec["market"],
        "account": spec["account"],
        "fee_schedule_version": item["fee_schedule_version"],
        "account_fee_plan": dict(plan) if plan is not None else None,
    }


def _sorted_owner_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list):
        _fail("research_revision_conflict", f"{label} must be a list")
    owners = [_text(item, label) for item in cast(list[object], value)]
    if owners != sorted(set(owners)):
        _fail("research_revision_conflict", f"{label} must be sorted and unique")
    return owners


def _validated_quota_decision(
    value: object,
    *,
    required_owners: list[str],
) -> dict[str, object]:
    item = _mapping(value, "research_revision.quota_decision")
    _exact_keys(
        item,
        _QUOTA_DECISION_KEYS,
        "research_revision.quota_decision",
        reason_code="research_revision_conflict",
    )
    if item["schema_version"] != INTERNAL_RESEARCH_QUOTA_DECISION_SCHEMA:
        _fail("research_revision_conflict", "quota decision schema is unsupported")
    required = _sorted_owner_list(
        item["required_stock_owners"], "quota_decision.required_stock_owners"
    )
    counted = _sorted_owner_list(
        item["already_counted_stock_owners"],
        "quota_decision.already_counted_stock_owners",
    )
    new = _sorted_owner_list(
        item["new_stock_owners"], "quota_decision.new_stock_owners"
    )
    remaining = item["remain_quota"]
    if isinstance(remaining, bool) or not isinstance(remaining, int) or remaining < 0:
        _fail("research_revision_conflict", "quota decision remaining value is invalid")
    if (
        required != required_owners
        or set(counted) & set(new)
        or sorted([*counted, *new]) != required
        or len(new) > remaining
    ):
        _fail("research_revision_conflict", "quota decision does not cover requirements")
    return {
        "schema_version": INTERNAL_RESEARCH_QUOTA_DECISION_SCHEMA,
        "required_stock_owners": required,
        "already_counted_stock_owners": counted,
        "new_stock_owners": new,
        "remain_quota": remaining,
    }


def validate_internal_research_revision(
    dataset: object,
    value: object,
) -> dict[str, object]:
    spec, _dataset_ref, _sealed_dataset, _point_rows, _projections = (
        _validated_research_input(dataset)
    )
    revision = _mapping(value, "research_revision")
    _exact_keys(
        revision,
        _REVISION_KEYS,
        "research_revision",
        reason_code="research_revision_conflict",
    )
    if revision["schema_version"] != INTERNAL_RESEARCH_REVISION_SCHEMA:
        _fail("research_revision_conflict", "research revision schema is unsupported")

    fee_contract = _normalized_fee_contract(revision["fee_contract"], spec=spec)
    requirements = required_research_close_keys(dataset, fee_contract)
    required_owners = sorted({owner for owner, _expiration in requirements})
    history = revision["history_kline_evidence"]
    close_receipts: list[dict[str, object]] = []
    normalized_history: dict[str, object] | None = None
    if requirements:
        history_item = _mapping(history, "research_revision.history_kline_evidence")
        _exact_keys(
            history_item,
            _HISTORY_EVIDENCE_KEYS,
            "research_revision.history_kline_evidence",
            reason_code="research_revision_conflict",
        )
        if history_item["page_complete"] is not True:
            _fail("research_revision_conflict", "history pages are incomplete")
        observed_at_utc = _utc_timestamp(
            history_item["observed_at_utc"], "history_kline_evidence.observed_at_utc"
        )
        quota_decision = _validated_quota_decision(
            history_item["quota_decision"], required_owners=required_owners
        )
        raw_receipts = history_item["close_receipts"]
        indexed = _validate_close_receipts(
            raw_receipts,
            market=str(spec["market"]),
            account=str(spec["account"]),
        )
        if set(indexed) != set(requirements) or any(
            len(indexed[key]) != 1 for key in requirements
        ):
            _fail("research_revision_conflict", "close receipts do not cover requirements")
        close_receipts = [indexed[key][0] for key in requirements]
        if raw_receipts != close_receipts:
            _fail("research_revision_conflict", "close receipts are not canonical")
        normalized_history = {
            "observed_at_utc": observed_at_utc,
            "page_complete": True,
            "quota_decision": quota_decision,
            "close_receipts": close_receipts,
        }
    elif history is not None:
        _fail("research_revision_conflict", "history evidence is unexpected")

    evaluation = dict(_mapping(revision["evaluation"], "research_revision.evaluation"))
    expected_evaluation = evaluate_research(dataset, close_receipts, fee_contract)
    if evaluation != expected_evaluation:
        _fail("research_revision_conflict", "research evaluation does not match evidence")
    normalized = {
        "schema_version": INTERNAL_RESEARCH_REVISION_SCHEMA,
        "evaluation": evaluation,
        "fee_contract": fee_contract,
        "history_kline_evidence": normalized_history,
    }
    if dict(revision) != normalized:
        _fail("research_revision_conflict", "research revision is not canonical")
    return normalized


def build_internal_research_revision(
    dataset: object,
    *,
    evaluation: object,
    fee_contract: object,
    close_receipts: list[dict[str, object]],
    quota_decision: object,
    observed_at_utc: str,
) -> dict[str, object]:
    spec, _dataset_ref, _sealed_dataset, _point_rows, _projections = (
        _validated_research_input(dataset)
    )
    normalized_fee = _normalized_fee_contract(fee_contract, spec=spec)
    history = (
        {
            "observed_at_utc": observed_at_utc,
            "page_complete": True,
            "quota_decision": quota_decision,
            "close_receipts": close_receipts,
        }
        if close_receipts
        else None
    )
    return validate_internal_research_revision(
        dataset,
        {
            "schema_version": INTERNAL_RESEARCH_REVISION_SCHEMA,
            "evaluation": evaluation,
            "fee_contract": normalized_fee,
            "history_kline_evidence": history,
        },
    )


__all__ = [
    "RESEARCH_CLOSE_RECEIPT_SCHEMA",
    "RESEARCH_EVALUATION_INPUT_SCHEMA",
    "RESEARCH_EVALUATION_SCHEMA",
    "ResearchEvaluationError",
    "evaluate_research",
    "required_research_close_keys",
]
