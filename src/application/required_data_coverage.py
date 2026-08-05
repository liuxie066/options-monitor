from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
from pandas.api.types import is_bool

from src.application.opend_normalize import normalize_opend_option_type
from src.application.required_data_plan_identity import (
    required_data_expiration_dtes,
    required_data_request_sha256,
)
from src.application.required_data_planning import RequiredDataFetchPlanBundle


_INVALID = object()
_EXACT_STRIKE_ABS_TOLERANCE = 1e-9
_COMPLETE_OPTION_CHAIN_STATUSES = frozenset({"cache", "fetched"})


@dataclass(frozen=True)
class RequiredDataCoverageResult:
    status: str
    reason_code: str | None
    provider_coverage: str
    internal_integrity: str
    freshness: str
    strategy_readiness: str
    warnings: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return self.status in {"success", "success_empty"}


@dataclass(frozen=True)
class _SnapshotEvidenceResult:
    requested_codes: frozenset[str] | None
    reason_code: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ScopeEvidenceResult:
    resolved: Mapping[tuple[int, str, str], frozenset[str]] | None
    reason_code: str | None = None
    warnings: tuple[str, ...] = ()


def build_required_data_coverage(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    coverage: dict[str, dict[str, Any]] = {}
    for option_type in ("put", "call"):
        side_rows = [
            row
            for row in rows
            if isinstance(row, dict) and normalize_opend_option_type(row.get("option_type")) == option_type
        ]
        strikes = [
            float(row.get("strike"))
            for row in side_rows
            if _safe_float(row.get("strike")) is not None
        ]
        dtes = [
            int(float(row.get("dte")))
            for row in side_rows
            if _safe_float(row.get("dte")) is not None
        ]
        expirations = sorted({
            str(row.get("expiration"))
            for row in side_rows
            if str(row.get("expiration") or "").strip()
        })
        coverage[option_type] = {
            "row_count": len(side_rows),
            "min_strike": (min(strikes) if strikes else None),
            "max_strike": (max(strikes) if strikes else None),
            "min_dte": (min(dtes) if dtes else None),
            "max_dte": (max(dtes) if dtes else None),
            "expirations": expirations,
        }
    return coverage


def load_required_data_payload_from_csv(*, parsed: Path, symbol: str) -> dict[str, object]:
    df = _read_required_data_csv(parsed)
    rows = df.to_dict(orient="records") if not df.empty else []
    expirations = sorted({
        str(row.get("expiration"))
        for row in rows
        if isinstance(row, dict) and row.get("expiration")
    })
    return {
        "symbol": symbol,
        "rows": rows,
        "expirations": expirations,
        "expiration_count": len(expirations),
    }


def required_data_csv_covers_fetch_plan(
    *,
    parsed: Path,
    fetch_plan: RequiredDataFetchPlanBundle,
    option_chain_evidence: Mapping[str, Any] | None = None,
) -> bool:
    df = _read_required_data_csv(parsed)
    return required_data_frame_covers_fetch_plan(
        df=df,
        fetch_plan=fetch_plan,
        option_chain_evidence=option_chain_evidence,
    )


def required_data_frame_covers_fetch_plan(
    *,
    df: pd.DataFrame,
    fetch_plan: RequiredDataFetchPlanBundle,
    option_chain_evidence: Mapping[str, Any] | None = None,
) -> bool:
    return required_data_frame_covers_fetch_plan_debug(
        df,
        fetch_plan.to_debug_dict(),
        option_chain_evidence=option_chain_evidence,
    )


def _evaluate_required_data_rows(
    df: pd.DataFrame,
    fetch_plan: Mapping[str, Any],
    *,
    scope_evidence: Mapping[tuple[int, str, str], frozenset[str]] | None,
) -> tuple[str | None, bool]:
    """Validate plan rows once and return ``(reason_code, is_empty)``."""

    if not isinstance(fetch_plan, Mapping):
        return "internal_contract_error", False
    spot_reference = fetch_plan.get("spot_reference")
    if spot_reference is not None:
        normalized_spot = _strict_finite_float(spot_reference)
        if normalized_spot is None or normalized_spot <= 0:
            return "internal_contract_error", False
    if not df.empty and (
        not _spot_reference_matches_frame(
            df=df,
            spot_reference=spot_reference,
        )
        or _numeric_series(df, "strike").empty
    ):
        return "invalid_row_identity", False

    trading_date = _fetch_plan_trading_date(fetch_plan)
    if not isinstance(trading_date, date):
        return "internal_contract_error", False

    merged_requests = fetch_plan.get("merged_requests")
    if not isinstance(merged_requests, list) or not merged_requests:
        return "internal_contract_error", False
    require_realized_volatility = fetch_plan.get("require_realized_volatility")
    if not isinstance(require_realized_volatility, bool):
        return "internal_contract_error", False
    active_request_count = 0
    any_rows = False
    for request_index, raw_request in enumerate(merged_requests):
        if not isinstance(raw_request, Mapping):
            return "internal_contract_error", False
        if raw_request.get("trading_date") != trading_date.isoformat():
            return "internal_contract_error", False
        requested_expirations = _strict_expiration_list(
            raw_request.get("explicit_expirations")
        )
        if not requested_expirations:
            return "internal_contract_error", False
        raw_rv_flag = raw_request.get("include_realized_volatility")
        if (
            not isinstance(raw_rv_flag, bool)
            or raw_rv_flag != require_realized_volatility
        ):
            return "internal_contract_error", False

        active_request_count += 1
        request_min_dte = _strict_optional_nonnegative_int(
            raw_request,
            "min_dte",
        )
        request_max_dte = _strict_optional_nonnegative_int(
            raw_request,
            "max_dte",
        )
        if request_min_dte is _INVALID or request_max_dte is _INVALID:
            return "internal_contract_error", False
        if not _valid_optional_range(request_min_dte, request_max_dte):
            return "internal_contract_error", False

        option_types = raw_request.get("option_types")
        if (
            not isinstance(option_types, list)
            or not option_types
            or any(
                not isinstance(option_type, str) or option_type not in {"put", "call"}
                for option_type in option_types
            )
            or len(option_types) != len(set(option_types))
        ):
            return "internal_contract_error", False
        raw_side_plans = raw_request.get("side_plans")
        if not isinstance(raw_side_plans, list):
            return "internal_contract_error", False
        side_plans: dict[str, Mapping[str, Any]] = {}
        for raw_side_plan in raw_side_plans:
            if not isinstance(raw_side_plan, Mapping):
                return "internal_contract_error", False
            option_type = raw_side_plan.get("option_type")
            if option_type not in option_types or option_type in side_plans:
                return "internal_contract_error", False
            side_plans[str(option_type)] = raw_side_plan
        if set(side_plans) != set(option_types):
            return "internal_contract_error", False

        raw_request_windows = raw_request.get("side_strike_windows")
        if not isinstance(raw_request_windows, Mapping) or set(
            raw_request_windows
        ) != set(option_types):
            return "internal_contract_error", False

        try:
            expected_dtes = required_data_expiration_dtes(
                trading_date=trading_date,
                expirations=requested_expirations,
            )
        except ValueError:
            return "internal_contract_error", False
        if any(
            not _value_within_optional_range(
                value=dte,
                minimum=request_min_dte,
                maximum=request_max_dte,
            )
            for dte in expected_dtes.values()
        ):
            return "internal_contract_error", False

        for option_type in option_types:
            raw_request_window = raw_request_windows.get(option_type)
            if not _valid_request_strike_window(raw_request_window):
                return "internal_contract_error", False
            request_scope_min = _strict_optional_positive_float(
                raw_request_window, "min_strike"
            )
            request_scope_max = _strict_optional_positive_float(
                raw_request_window, "max_strike"
            )
            raw_side_plan = side_plans[option_type]
            side_expirations = _strict_expiration_list(
                raw_side_plan.get("explicit_expirations")
            )
            if side_expirations != requested_expirations:
                return "internal_contract_error", False
            exact_strikes_by_expiration = _strict_exact_strikes_by_expiration(
                raw_side_plan.get("required_exact_strikes_by_expiration"),
                allowed_expirations=side_expirations,
            )
            if exact_strikes_by_expiration is None:
                return "internal_contract_error", False
            side_min_dte = _strict_optional_nonnegative_int(
                raw_side_plan,
                "min_dte",
            )
            side_max_dte = _strict_optional_nonnegative_int(
                raw_side_plan,
                "max_dte",
            )
            if side_min_dte is _INVALID or side_max_dte is _INVALID:
                return "internal_contract_error", False
            if not _valid_optional_range(side_min_dte, side_max_dte):
                return "internal_contract_error", False

            effective_bounds = _effective_side_strike_bounds(raw_side_plan)
            if effective_bounds is None:
                return "internal_contract_error", False
            effective_min, effective_max = effective_bounds
            side_df = _filter_option_type(df, option_type)
            if scope_evidence is None and (
                side_df.empty or "expiration" not in side_df.columns
            ):
                return "invalid_row_identity", False
            for expiration in requested_expirations:
                proven_codes = (
                    scope_evidence.get((request_index, option_type, expiration))
                    if scope_evidence is not None
                    else None
                )
                expected_dte = expected_dtes[expiration]
                if not _value_within_optional_range(
                    value=expected_dte,
                    minimum=side_min_dte,
                    maximum=side_max_dte,
                ):
                    return "internal_contract_error", False
                if proven_codes is not None:
                    if not proven_codes:
                        exp_df = df.iloc[0:0].copy()
                    elif "contract_symbol" not in df.columns:
                        return "invalid_row_identity", False
                    else:
                        exp_df = df[
                            df["contract_symbol"]
                            .astype(str)
                            .str.strip()
                            .isin(proven_codes)
                        ].copy()
                else:
                    exp_df = side_df[
                        side_df["expiration"].astype(str) == expiration
                    ].copy()
                if exp_df.empty:
                    if proven_codes != frozenset():
                        return "invalid_row_identity", False
                    if exact_strikes_by_expiration.get(expiration):
                        return "required_contract_missing", False
                    continue
                any_rows = True
                if not _frame_dte_matches(
                    df=exp_df,
                    expected_dte=expected_dte,
                    minimum=request_min_dte,
                    maximum=request_max_dte,
                ):
                    return "invalid_row_identity", False
                if not _frame_dte_matches(
                    df=exp_df,
                    expected_dte=expected_dte,
                    minimum=side_min_dte,
                    maximum=side_max_dte,
                ):
                    return "invalid_row_identity", False
                strikes = _numeric_series(exp_df, "strike")
                row_codes = _frame_contract_codes(exp_df)
                if proven_codes is not None and row_codes != proven_codes:
                    return "invalid_row_identity", False
                if proven_codes is not None and not _strikes_within_bounds(
                    strikes=strikes,
                    minimum=request_scope_min,
                    maximum=request_scope_max,
                ):
                    return "invalid_row_identity", False
                if not _strikes_cover_bounds(
                    strikes=strikes,
                    base_min=effective_min,
                    base_max=effective_max,
                ) and (
                    proven_codes is None
                    or not _strikes_overlap_bounds(
                        strikes=strikes,
                        base_min=effective_min,
                        base_max=effective_max,
                    )
                ):
                    return "invalid_row_identity", False
                if not _strikes_cover_exact_requirements(
                    strikes=strikes,
                    required_strikes=exact_strikes_by_expiration.get(
                        expiration,
                        [],
                    ),
                ):
                    return "required_contract_missing", False

    if active_request_count <= 0:
        return "internal_contract_error", False
    if not any_rows:
        return None, True
    if require_realized_volatility and not _has_realized_volatility(df):
        return "invalid_row_identity", False
    return None, False


def required_data_frame_covers_fetch_plan_debug(
    df: pd.DataFrame,
    fetch_plan: Mapping[str, Any],
    *,
    option_chain_evidence: Mapping[str, Any] | None = None,
) -> bool:
    """Compatibility facade for callers that only need an acceptance boolean."""

    return evaluate_required_data_frame_fetch_plan_debug(
        df,
        fetch_plan,
        option_chain_evidence=option_chain_evidence,
    ).accepted


def evaluate_required_data_frame_fetch_plan_debug(
    df: pd.DataFrame,
    fetch_plan: Mapping[str, Any],
    *,
    option_chain_evidence: Mapping[str, Any] | None = None,
) -> RequiredDataCoverageResult:
    """Return the smallest decision contract needed by required-data consumers."""

    if not isinstance(fetch_plan, Mapping):
        return _blocked_coverage("internal_contract_error")
    scope_validation = _validated_option_chain_scope_evidence(
        df=df,
        fetch_plan=fetch_plan,
        evidence=option_chain_evidence,
    )
    if scope_validation.reason_code is not None:
        return _blocked_coverage(
            scope_validation.reason_code,
            warnings=scope_validation.warnings,
        )
    reason_code, is_empty = _evaluate_required_data_rows(
        df,
        fetch_plan,
        scope_evidence=scope_validation.resolved,
    )
    if reason_code is not None:
        return _blocked_coverage(
            reason_code,
            provider_coverage=(
                "complete"
                if scope_validation.resolved is not None
                and reason_code != "internal_contract_error"
                else "unproven"
            ),
            warnings=scope_validation.warnings,
        )
    return RequiredDataCoverageResult(
        status=("success_empty" if is_empty else "success"),
        reason_code=None,
        provider_coverage=(
            "complete" if scope_validation.resolved is not None else "unproven"
        ),
        internal_integrity="valid",
        freshness=_freshness_evidence_strength(option_chain_evidence),
        strategy_readiness=("empty" if is_empty else "ready"),
        warnings=scope_validation.warnings,
        details={"completion_unit": "request_option_type_expiration"},
    )


def _blocked_coverage(
    reason_code: str,
    *,
    provider_coverage: str | None = None,
    strategy_readiness: str = "blocked",
    warnings: tuple[str, ...] = (),
) -> RequiredDataCoverageResult:
    if provider_coverage is None:
        provider_coverage = (
            "incomplete" if reason_code == "provider_incomplete" else "unproven"
        )
    return RequiredDataCoverageResult(
        status="blocked",
        reason_code=reason_code,
        provider_coverage=provider_coverage,
        internal_integrity=(
            "invalid"
            if reason_code
            in {
                "internal_contract_error",
                "scope_identity_mismatch",
                "invalid_row_identity",
            }
            else "valid"
        ),
        freshness=("stale" if reason_code == "stale_data" else "unproven"),
        strategy_readiness=strategy_readiness,
        warnings=warnings,
        details={},
    )


def _freshness_evidence_strength(
    evidence: Mapping[str, Any] | None,
) -> str:
    if not isinstance(evidence, Mapping):
        return "unproven"
    return (
        "system_observed_at"
        if str(evidence.get("source_observed_at") or "").strip()
        else "unproven"
    )


def _validated_option_chain_scope_evidence(
    *,
    df: pd.DataFrame,
    fetch_plan: Mapping[str, Any],
    evidence: Mapping[str, Any] | None,
) -> _ScopeEvidenceResult:
    """Validate request-scoped complete-chain proof for finite or empty grids."""

    if not isinstance(evidence, Mapping):
        return _ScopeEvidenceResult(None)
    has_scope_evidence = _contains_option_chain_scope_evidence(evidence)
    if evidence.get("stale_cache_expirations") not in (None, []):
        return _ScopeEvidenceResult(None, "stale_data")
    if (
        str(evidence.get("status") or "").strip().lower() != "ok"
        or evidence.get("errors") not in (None, [])
    ):
        return _ScopeEvidenceResult(None, "provider_incomplete")
    if not _valid_scope_source_outcome(evidence):
        return _ScopeEvidenceResult(None, "internal_contract_error")

    merged_requests = fetch_plan.get("merged_requests")
    if not isinstance(merged_requests, list) or not merged_requests:
        return _ScopeEvidenceResult(None, "internal_contract_error")
    indexed_children: list[tuple[int, Mapping[str, Any], Mapping[str, Any]]] = []
    if len(merged_requests) == 1:
        request = merged_requests[0]
        if not isinstance(request, Mapping):
            return _ScopeEvidenceResult(None, "internal_contract_error")
        indexed_children = [(0, request, evidence)]
    else:
        raw_children = evidence.get("requests")
        request_count = evidence.get("request_count")
        if (
            not isinstance(raw_children, list)
            or len(raw_children) != len(merged_requests)
            or any(not isinstance(item, Mapping) for item in raw_children)
            or isinstance(request_count, bool)
            or not isinstance(request_count, int)
            or request_count != len(merged_requests)
        ):
            return _ScopeEvidenceResult(None, "internal_contract_error")
        expected_by_hash: dict[str, tuple[int, Mapping[str, Any]]] = {}
        for request_index, request in enumerate(merged_requests):
            if not isinstance(request, Mapping):
                return _ScopeEvidenceResult(None, "internal_contract_error")
            try:
                request_hash = required_data_request_sha256(request)
            except (TypeError, ValueError):
                return _ScopeEvidenceResult(None, "internal_contract_error")
            if request_hash in expected_by_hash:
                return _ScopeEvidenceResult(None, "internal_contract_error")
            expected_by_hash[request_hash] = (request_index, request)
        observed_by_hash: dict[str, Mapping[str, Any]] = {}
        observed_indexes: set[int] = set()
        for child in raw_children:
            assert isinstance(child, Mapping)
            child_hash = str(child.get("planned_request_sha256") or "").strip()
            child_index = child.get("request_index")
            if (
                not child_hash
                or child_hash in observed_by_hash
                or isinstance(child_index, bool)
                or not isinstance(child_index, int)
                or child_index < 0
                or child_index >= len(merged_requests)
                or child_index in observed_indexes
            ):
                return _ScopeEvidenceResult(None, "scope_identity_mismatch")
            observed_by_hash[child_hash] = child
            observed_indexes.add(child_index)
        if set(observed_by_hash) != set(expected_by_hash):
            return _ScopeEvidenceResult(None, "scope_identity_mismatch")
        indexed_children = [
            (request_index, request, observed_by_hash[request_hash])
            for request_hash, (request_index, request) in expected_by_hash.items()
        ]
        if any(
            child.get("request_index") != request_index
            for request_index, _request, child in indexed_children
        ):
            return _ScopeEvidenceResult(None, "internal_contract_error")

    aggregate_snapshot = _validated_snapshot_codes(evidence)
    if aggregate_snapshot.reason_code is not None:
        return _ScopeEvidenceResult(None, aggregate_snapshot.reason_code)
    aggregate_codes = aggregate_snapshot.requested_codes
    assert aggregate_codes is not None
    frame_codes = frozenset() if df.empty else _frame_contract_codes(df)
    if frame_codes is None or aggregate_codes != frame_codes:
        return _ScopeEvidenceResult(None, "invalid_row_identity")

    resolved: dict[tuple[int, str, str], frozenset[str]] = {}
    child_union: set[str] = set()
    warnings = list(aggregate_snapshot.warnings)
    for request_index, request, child in indexed_children:
        if child.get("stale_cache_expirations") not in (None, []):
            return _ScopeEvidenceResult(None, "stale_data")
        if (
            str(child.get("status") or "").strip().lower() != "ok"
            or child.get("errors") not in (None, [])
        ):
            return _ScopeEvidenceResult(None, "provider_incomplete")
        if not _valid_scope_source_outcome(child):
            return _ScopeEvidenceResult(None, "internal_contract_error")
        child_snapshot = _validated_snapshot_codes(child)
        if child_snapshot.reason_code is not None:
            return _ScopeEvidenceResult(None, child_snapshot.reason_code)
        child_codes = child_snapshot.requested_codes
        assert child_codes is not None
        warnings.extend(child_snapshot.warnings)
        scope_payload = child.get("option_chain_scope_coverage")
        if not has_scope_evidence:
            child_union.update(child_codes)
            continue
        if (
            not isinstance(scope_payload, Mapping)
            or scope_payload.get("schema_version")
            != "option_chain_scope_coverage.v1"
        ):
            return _ScopeEvidenceResult(None, "internal_contract_error")
        raw_scopes = scope_payload.get("scopes")
        option_types = request.get("option_types")
        expirations = request.get("explicit_expirations")
        if (
            not isinstance(raw_scopes, list)
            or not isinstance(option_types, list)
            or not isinstance(expirations, list)
        ):
            return _ScopeEvidenceResult(None, "internal_contract_error")
        expected_keys = {
            (str(option_type), str(expiration))
            for option_type in option_types
            for expiration in expirations
        }
        scope_union: set[str] = set()
        observed_keys: set[tuple[str, str]] = set()
        for raw_scope in raw_scopes:
            if not isinstance(raw_scope, Mapping):
                return _ScopeEvidenceResult(None, "internal_contract_error")
            key = (
                str(raw_scope.get("option_type") or ""),
                str(raw_scope.get("expiration") or ""),
            )
            if key in observed_keys:
                return _ScopeEvidenceResult(None, "scope_identity_mismatch")
            observed_keys.add(key)
            status = str(raw_scope.get("chain_status") or "").strip()
            codes = _strict_code_list(raw_scope.get("filtered_contract_codes"))
            count = raw_scope.get("filtered_contract_count")
            if status == "stale_cache":
                return _ScopeEvidenceResult(None, "stale_data")
            if status not in _COMPLETE_OPTION_CHAIN_STATUSES:
                return _ScopeEvidenceResult(None, "provider_incomplete")
            if (
                codes is None
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count != len(codes)
            ):
                return _ScopeEvidenceResult(None, "internal_contract_error")
            if scope_union.intersection(codes):
                return _ScopeEvidenceResult(None, "scope_identity_mismatch")
            scope_union.update(codes)
            resolved[(request_index, *key)] = frozenset(codes)
        if observed_keys != expected_keys or scope_union != set(child_codes):
            return _ScopeEvidenceResult(None, "scope_identity_mismatch")
        child_union.update(child_codes)
    if child_union != set(aggregate_codes):
        return _ScopeEvidenceResult(None, "internal_contract_error")
    if not has_scope_evidence:
        return _ScopeEvidenceResult(
            None,
            warnings=tuple(dict.fromkeys(warnings)),
        )
    for (request_index, option_type, expiration), codes in resolved.items():
        for code in codes:
            matches = df[df["contract_symbol"].astype(str).str.strip() == code]
            if len(matches) != 1:
                return _ScopeEvidenceResult(None, "invalid_row_identity")
            row = matches.iloc[0]
            if (
                normalize_opend_option_type(row.get("option_type")) != option_type
                or str(row.get("expiration") or "").strip() != expiration
            ):
                return _ScopeEvidenceResult(None, "scope_identity_mismatch")
    return _ScopeEvidenceResult(
        resolved,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _valid_scope_source_outcome(evidence: Mapping[str, Any]) -> bool:
    outcome = str(evidence.get("source_outcome") or "").strip().lower()
    reason = str(evidence.get("reason_code") or "").strip().lower()
    return (outcome == "success_rows" and not reason) or (
        outcome == "success_empty" and reason == "no_contract_rows"
    )


def _contains_option_chain_scope_evidence(
    evidence: Mapping[str, Any] | None,
) -> bool:
    if not isinstance(evidence, Mapping):
        return False
    if "option_chain_scope_coverage" in evidence:
        return True
    requests = evidence.get("requests")
    return isinstance(requests, list) and any(
        isinstance(item, Mapping) and "option_chain_scope_coverage" in item
        for item in requests
    )


def _strict_code_list(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    codes = [str(code).strip() for code in value]
    if any(not code for code in codes) or len(codes) != len(set(codes)):
        return None
    return codes


def _validated_snapshot_codes(
    evidence: Mapping[str, Any],
) -> _SnapshotEvidenceResult:
    requested = _strict_code_list(evidence.get("snapshot_requested_code_set"))
    returned = _strict_code_list(evidence.get("snapshot_returned_code_set"))
    missing = _strict_code_list(evidence.get("snapshot_missing_code_set"))
    unexpected = _strict_code_list(evidence.get("snapshot_unexpected_code_set"))
    if None in (requested, returned, missing, unexpected):
        return _SnapshotEvidenceResult(None, "internal_contract_error")
    assert requested is not None and returned is not None
    assert missing is not None and unexpected is not None
    counts = (
        evidence.get("snapshot_requested_codes"),
        evidence.get("snapshot_returned_codes"),
        evidence.get("snapshot_missing_codes"),
        evidence.get("snapshot_unexpected_codes"),
    )
    expected_counts = (len(requested), len(returned), len(missing), len(unexpected))
    if (
        any(isinstance(value, bool) or not isinstance(value, int) for value in counts)
        or counts != expected_counts
        or set(missing) != set(requested).difference(returned)
        or set(unexpected) != set(returned).difference(requested)
        or (
            evidence.get("option_codes") is not None
            and evidence.get("option_codes") != len(requested)
        )
    ):
        return _SnapshotEvidenceResult(None, "internal_contract_error")
    if missing or not set(requested).issubset(returned):
        return _SnapshotEvidenceResult(None, "provider_incomplete")
    if evidence.get("snapshot_complete") is not True:
        return _SnapshotEvidenceResult(None, "provider_incomplete")
    warnings = (
        (f"unexpected_snapshot_codes:{len(unexpected)}",)
        if unexpected
        else ()
    )
    return _SnapshotEvidenceResult(frozenset(requested), warnings=warnings)


def _frame_contract_codes(df: pd.DataFrame) -> frozenset[str] | None:
    if "contract_symbol" not in df.columns:
        return None
    codes = [str(value).strip() for value in df["contract_symbol"].tolist()]
    if any(not code for code in codes) or len(codes) != len(set(codes)):
        return None
    return frozenset(codes)


def _strikes_within_bounds(
    *,
    strikes: pd.Series,
    minimum: float | None | object,
    maximum: float | None | object,
) -> bool:
    if strikes.empty or minimum is _INVALID or maximum is _INVALID:
        return False
    return not (
        (minimum is not None and bool((strikes < minimum).any()))
        or (maximum is not None and bool((strikes > maximum).any()))
    )


def required_data_csv_covers_strategy_bounds(
    *,
    parsed: Path,
    option_types: str,
    min_dte: int | None = None,
    max_dte: int | None = None,
    min_strike: float | None = None,
    max_strike: float | None = None,
    side_strike_windows: dict[str, dict[str, float | None]] | None = None,
    require_realized_volatility: bool = False,
) -> bool:
    df = _read_required_data_csv(parsed)
    return required_data_frame_covers_strategy_bounds(
        df=df,
        option_types=option_types,
        min_dte=min_dte,
        max_dte=max_dte,
        min_strike=min_strike,
        max_strike=max_strike,
        side_strike_windows=side_strike_windows,
        require_realized_volatility=require_realized_volatility,
    )


def required_data_frame_covers_strategy_bounds(
    *,
    df: pd.DataFrame,
    option_types: str,
    min_dte: int | None = None,
    max_dte: int | None = None,
    min_strike: float | None = None,
    max_strike: float | None = None,
    side_strike_windows: dict[str, dict[str, float | None]] | None = None,
    require_realized_volatility: bool = False,
) -> bool:
    if df.empty:
        return False
    if require_realized_volatility and not _has_realized_volatility(df):
        return False
    wanted_types = _parse_option_types(option_types)
    if not wanted_types:
        wanted_types = ("put", "call")

    for option_type in wanted_types:
        side_df = _filter_option_type(df, option_type)
        if side_df.empty:
            return False
        if "dte" not in side_df.columns and (min_dte is not None or max_dte is not None):
            return False
        if "dte" in side_df.columns and (min_dte is not None or max_dte is not None):
            dtes = pd.to_numeric(side_df["dte"], errors="coerce")
            if dtes.dropna().empty:
                return False
            if max_dte is not None and float(dtes.dropna().max()) < float(max_dte):
                return False
            if min_dte is not None:
                side_df = side_df[dtes >= int(min_dte)].copy()
                dtes = pd.to_numeric(side_df["dte"], errors="coerce") if not side_df.empty else dtes.iloc[0:0]
            if max_dte is not None:
                side_df = side_df[dtes <= int(max_dte)].copy()
        if side_df.empty:
            return False

        side_window = (side_strike_windows or {}).get(option_type)
        side_min = _safe_float((side_window or {}).get("min_strike")) if isinstance(side_window, dict) else None
        side_max = _safe_float((side_window or {}).get("max_strike")) if isinstance(side_window, dict) else None
        effective_min = side_min if side_min is not None else _safe_float(min_strike)
        effective_max = side_max if side_max is not None else _safe_float(max_strike)
        strikes = _numeric_series(side_df, "strike")
        if not _strikes_cover_bounds(
            strikes=strikes,
            base_min=effective_min,
            base_max=effective_max,
        ):
            return False
    return True


def _fetch_plan_trading_date(
    fetch_plan: Mapping[str, Any],
) -> date | object:
    discovery = fetch_plan.get("expiration_discovery")
    if discovery is None:
        return _INVALID
    if not isinstance(discovery, Mapping):
        return _INVALID
    identity = discovery.get("request_identity")
    if not isinstance(identity, Mapping):
        return _INVALID
    trading_date = _strict_iso_date(identity.get("trading_date"))
    return trading_date if trading_date is not None else _INVALID


def _strict_expiration_list(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    expirations: list[str] = []
    for raw_expiration in value:
        if not isinstance(raw_expiration, str):
            return None
        expiration = raw_expiration.strip()
        if (
            not expiration
            or expiration != raw_expiration
            or _strict_iso_date(expiration) is None
            or expiration in expirations
        ):
            return None
        expirations.append(expiration)
    return expirations


def _strict_iso_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    if parsed.isoformat() != value:
        return None
    return parsed


def _strict_exact_strikes_by_expiration(
    value: Any,
    *,
    allowed_expirations: list[str],
) -> dict[str, list[float]] | None:
    if not isinstance(value, Mapping):
        return None
    allowed = set(allowed_expirations)
    normalized: dict[str, list[float]] = {}
    for raw_expiration, raw_strikes in value.items():
        expiration = _strict_iso_date(raw_expiration)
        if expiration is None or raw_expiration not in allowed:
            return None
        if not isinstance(raw_strikes, list) or not raw_strikes:
            return None
        strikes: list[float] = []
        for raw_strike in raw_strikes:
            strike = _strict_finite_float(raw_strike)
            if strike is None or strike <= 0:
                return None
            strikes.append(strike)
        if strikes != sorted(strikes) or len(strikes) != len(set(strikes)):
            return None
        normalized[str(raw_expiration)] = strikes
    if list(value) != sorted(value):
        return None
    return normalized


def _strict_optional_nonnegative_int(
    mapping: Mapping[str, Any],
    key: str,
) -> int | None | object:
    value = mapping.get(key)
    if value is None:
        return None
    parsed = _strict_finite_float(value)
    if parsed is None or parsed < 0 or not parsed.is_integer():
        return _INVALID
    return int(parsed)


def _strict_optional_positive_float(
    mapping: Mapping[str, Any],
    key: str,
) -> float | None | object:
    value = mapping.get(key)
    if value is None:
        return None
    parsed = _strict_finite_float(value)
    if parsed is None or parsed <= 0:
        return _INVALID
    return parsed


def _strict_finite_float(value: Any) -> float | None:
    if (
        value is None
        or is_bool(value)
        or isinstance(value, (str, bytes, bytearray))
    ):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _valid_optional_range(minimum: Any, maximum: Any) -> bool:
    return minimum is None or maximum is None or minimum <= maximum


def _value_within_optional_range(
    *,
    value: int,
    minimum: Any,
    maximum: Any,
) -> bool:
    if minimum is not None and value < minimum:
        return False
    if maximum is not None and value > maximum:
        return False
    return True


def _valid_request_strike_window(value: Any) -> bool:
    if (
        not isinstance(value, Mapping)
        or "min_strike" not in value
        or "max_strike" not in value
    ):
        return False
    minimum = _strict_optional_positive_float(value, "min_strike")
    maximum = _strict_optional_positive_float(value, "max_strike")
    if minimum is _INVALID or maximum is _INVALID:
        return False
    return _valid_optional_range(minimum, maximum)


def _effective_side_strike_bounds(
    raw_side_plan: Mapping[str, Any],
) -> tuple[float | None, float | None] | None:
    raw_window = raw_side_plan.get("strike_window")
    required_keys = {
        "min_strike",
        "max_strike",
        "base_min_strike",
        "base_max_strike",
    }
    if (
        not isinstance(raw_window, Mapping)
        or not required_keys.issubset(raw_window)
    ):
        return None
    fetch_min = _strict_optional_positive_float(raw_window, "min_strike")
    fetch_max = _strict_optional_positive_float(raw_window, "max_strike")
    base_min = _strict_optional_positive_float(raw_window, "base_min_strike")
    base_max = _strict_optional_positive_float(raw_window, "base_max_strike")
    if any(
        value is _INVALID
        for value in (fetch_min, fetch_max, base_min, base_max)
    ):
        return None
    if not _valid_optional_range(fetch_min, fetch_max):
        return None
    if not _valid_optional_range(base_min, base_max):
        return None
    effective_min = base_min if base_min is not None else fetch_min
    effective_max = base_max if base_max is not None else fetch_max
    if not _valid_optional_range(effective_min, effective_max):
        return None
    return effective_min, effective_max


def _frame_dte_matches(
    *,
    df: pd.DataFrame,
    expected_dte: int | None,
    minimum: Any,
    maximum: Any,
) -> bool:
    if "dte" not in df.columns or df.empty:
        return False
    for raw_value in df["dte"].tolist():
        value = _strict_finite_float(raw_value)
        if (
            value is None
            or value < 0
            or not value.is_integer()
        ):
            return False
        normalized = int(value)
        if expected_dte is not None and normalized != expected_dte:
            return False
        if not _value_within_optional_range(
            value=normalized,
            minimum=minimum,
            maximum=maximum,
        ):
            return False
    return True


def _strict_positive_frame_values(
    *,
    df: pd.DataFrame,
    column: str,
) -> list[float] | None:
    if column not in df.columns or df.empty:
        return None
    values: list[float] = []
    for raw_value in df[column].tolist():
        value = _strict_finite_float(raw_value)
        if value is None or value <= 0:
            return None
        values.append(value)
    return values or None


def _spot_reference_matches_frame(*, df: pd.DataFrame, spot_reference: Any) -> bool:
    expected: float | None
    if spot_reference is None:
        expected = None
    else:
        expected = _strict_finite_float(spot_reference)
        if expected is None or expected <= 0:
            return False
    values = _strict_positive_frame_values(df=df, column="spot")
    if values is None:
        return False
    if expected is None:
        return True
    tolerance = max(1e-6, abs(float(expected)) * 1e-6)
    return all(abs(value - expected) <= tolerance for value in values)


def _has_realized_volatility(df: pd.DataFrame) -> bool:
    return (
        _strict_positive_frame_values(
            df=df,
            column="realized_volatility_estimate",
        )
        is not None
    )


def _read_required_data_csv(parsed: Path) -> pd.DataFrame:
    try:
        path = Path(parsed)
        if not path.exists() or path.stat().st_size <= 0:
            return pd.DataFrame()
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def _filter_option_type(df: pd.DataFrame, option_type: str) -> pd.DataFrame:
    if "option_type" not in df.columns:
        return pd.DataFrame()
    normalized = df["option_type"].apply(normalize_opend_option_type)
    return df[normalized == str(option_type)].copy()


def _parse_option_types(value: str) -> tuple[str, ...]:
    out: list[str] = []
    for item in str(value or "").split(","):
        option_type = normalize_opend_option_type(item)
        if option_type in {"put", "call"} and option_type not in out:
            out.append(option_type)
    return tuple(out)


def _numeric_series(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series(dtype=float)
    values: list[float] = []
    for raw_value in df[column].tolist():
        value = _strict_finite_float(raw_value)
        if value is None or value <= 0:
            return pd.Series(dtype=float)
        values.append(value)
    return pd.Series(values, dtype=float)


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return _strict_finite_float(value)


def _strikes_cover_bounds(*, strikes: pd.Series, base_min: float | None, base_max: float | None) -> bool:
    if strikes.empty:
        return False
    normalized_min = None
    if base_min is not None:
        normalized_min = _strict_finite_float(base_min)
        if normalized_min is None or normalized_min <= 0:
            return False
    normalized_max = None
    if base_max is not None:
        normalized_max = _strict_finite_float(base_max)
        if normalized_max is None or normalized_max <= 0:
            return False
    if not _valid_optional_range(normalized_min, normalized_max):
        return False
    unique_strikes = sorted({float(v) for v in strikes.tolist()})
    if not unique_strikes:
        return False
    if any(not math.isfinite(strike) or strike <= 0 for strike in unique_strikes):
        return False
    if normalized_min is not None and max(unique_strikes) < normalized_min:
        return False
    if normalized_max is not None and min(unique_strikes) > normalized_max:
        return False

    in_bounds = [
        strike
        for strike in unique_strikes
        if (normalized_min is None or strike >= normalized_min)
        and (normalized_max is None or strike <= normalized_max)
    ]

    if (
        normalized_min is not None
        and normalized_max is not None
        and normalized_max > normalized_min
    ):
        return _strikes_cover_bounded_edges(
            unique_strikes=unique_strikes,
            base_min=normalized_min,
            base_max=normalized_max,
        )
    if normalized_min is not None or normalized_max is not None:
        return len(in_bounds) >= 1
    return len(unique_strikes) >= 1


def _strikes_cover_exact_requirements(
    *,
    strikes: pd.Series,
    required_strikes: list[float],
) -> bool:
    if not required_strikes:
        return True
    if strikes.empty:
        return False
    actual_strikes = [float(value) for value in strikes.tolist()]
    return all(
        any(
            math.isclose(
                actual,
                expected,
                rel_tol=0.0,
                abs_tol=_EXACT_STRIKE_ABS_TOLERANCE,
            )
            for actual in actual_strikes
        )
        for expected in required_strikes
    )


def _strikes_cover_bounded_edges(*, unique_strikes: list[float], base_min: float, base_max: float) -> bool:
    tolerance = _strike_edge_tolerance(
        unique_strikes=unique_strikes,
        base_min=base_min,
        base_max=base_max,
    )
    nearest_lower_gap = min(abs(strike - base_min) for strike in unique_strikes)
    nearest_upper_gap = min(abs(strike - base_max) for strike in unique_strikes)
    return nearest_lower_gap <= tolerance and nearest_upper_gap <= tolerance


def _strikes_overlap_bounds(
    *,
    strikes: pd.Series,
    base_min: float | None,
    base_max: float | None,
) -> bool:
    if strikes.empty:
        return False
    normalized_min = None
    if base_min is not None:
        normalized_min = _strict_finite_float(base_min)
        if normalized_min is None or normalized_min <= 0:
            return False
    normalized_max = None
    if base_max is not None:
        normalized_max = _strict_finite_float(base_max)
        if normalized_max is None or normalized_max <= 0:
            return False
    if not _valid_optional_range(normalized_min, normalized_max):
        return False
    return any(
        (normalized_min is None or float(value) >= normalized_min)
        and (normalized_max is None or float(value) <= normalized_max)
        for value in strikes.tolist()
    )


def _strike_edge_tolerance(*, unique_strikes: list[float], base_min: float, base_max: float) -> float:
    width = max(0.0, float(base_max) - float(base_min))
    gaps = [
        abs(float(right) - float(left))
        for left, right in zip(unique_strikes, unique_strikes[1:])
        if abs(float(right) - float(left)) > 0
    ]
    if gaps:
        step = min(gaps)
        if width > 0:
            return max(1e-9, min(float(step), width * 0.25))
        return max(1e-9, float(step))
    if width > 0:
        return max(1e-9, width * 0.05)
    return max(1e-9, abs(float(base_min)) * 0.005)
