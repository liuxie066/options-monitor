from __future__ import annotations

import base64
import binascii
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import io
import json
import math
from numbers import Number
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.fetch_source import normalize_fetch_source
from domain.domain.symbol_identity import symbol_market
from src.application.required_data_coverage import (
    evaluate_required_data_frame_fetch_plan_debug,
)
from src.application.required_data_plan_identity import (
    validate_required_data_expected_fetch_contract,
)
from src.application.required_data_blobs import (
    RequiredDataBlobError,
    load_required_data_scan_blob,
    publish_required_data_scan_blob,
    required_data_shadow_base64_matches,
    required_data_shadow_file_matches,
)
from src.application.source_receipts import (
    SourceReceiptError,
    SOURCE_MAX_AGE_SECONDS,
    publish_source_receipt,
    safe_existing_relative_path,
    sha256_bytes,
    source_snapshot_id,
    validate_source_receipt,
)
from src.infrastructure.io_utils import atomic_write_text
from src.application.close_advice_quote_cache import (
    QUOTE_CACHE_METADATA_SCHEMA,
    publish_quote_cache_metadata,
    quote_cache_metadata_path,
)


REQUIRED_DATA_QUOTE_SNAPSHOT_SCHEMA = "required_data_quote_snapshot.v2"
SUCCESS_EMPTY_REASON_CODES = frozenset(
    {"no_expirations", "no_contract_rows"}
)
_VALIDATION_REASON_CODES = frozenset(
    {
        "provider_incomplete",
        "required_contract_missing",
        "scope_identity_mismatch",
        "invalid_row_identity",
        "stale_data",
        "freshness_unproven",
        "internal_contract_error",
    }
)


class _StaleRequiredDataError(SourceReceiptError):
    """Internal typed signal for the stale-data reason code."""


REQUIRED_DATA_COLUMNS = [
    "symbol",
    "market",
    "option_type",
    "expiration",
    "dte",
    "contract_symbol",
    "strike",
    "spot",
    "spot_update_time",
    "spot_observed_at_utc",
    "spot_age_seconds",
    "market_state",
    "underlier_sec_status",
    "underlier_suspension",
    "underlier_observation_status",
    "underlier_observation_reason_code",
    "bid",
    "ask",
    "last_price",
    "mid",
    "last_price_update_time",
    "last_price_observed_at_utc",
    "last_price_age_seconds",
    "last_price_activity_status",
    "snapshot_requested_at_utc",
    "snapshot_received_at_utc",
    "snapshot_age_seconds",
    "price_tick",
    "volume",
    "open_interest",
    "implied_volatility",
    "realized_volatility_20",
    "realized_volatility_60",
    "realized_volatility_120",
    "realized_volatility_estimate",
    "term_matched_rv",
    "term_matched_rv_status",
    "term_matched_rv_reason",
    "term_matched_rv_remaining_sessions",
    "term_matched_rv_lookback_sessions",
    "term_matched_rv_input_start",
    "term_matched_rv_input_end",
    "term_matched_rv_input_session_count",
    "term_matched_rv_input_hash",
    "term_matched_rv_legacy_shadow",
    "term_matched_rv_shadow_difference",
    "in_the_money",
    "currency",
    "otm_pct",
    "delta",
    "option_standard_type",
    "stock_owner",
    "stock_type",
    "option_sec_status",
    "option_suspension",
    "chain_multiplier",
    "snapshot_multiplier",
    "multiplier",
    "opening_contract_status",
    "opening_contract_reason_codes",
]


def validate_required_data_source_outcome(
    *,
    rows: list[Any],
    source_outcome: object,
    reason_code: object,
    subject: str,
) -> tuple[str, str | None]:
    """Normalize one complete required-data result or reject contradictory evidence."""

    outcome = str(source_outcome or "").strip().lower()
    reason = str(reason_code or "").strip().lower() or None
    if rows:
        if outcome not in {"", "success_rows"} or reason is not None:
            raise SourceReceiptError(
                f"non-success required-data {subject} contains rows"
            )
        return outcome or "success_rows", None
    if outcome != "success_empty" or reason not in SUCCESS_EMPTY_REASON_CODES:
        raise SourceReceiptError(
            f"zero-row required-data {subject} lacks success-empty evidence"
        )
    return outcome, reason


def _validate_required_data_payload_candidate_impl(
    *,
    raw_payload: Mapping[str, Any],
    expected_fetch_contract: Mapping[str, Any],
) -> tuple[dict[str, Any], list[Any], str, dict[str, Any]]:
    if not isinstance(raw_payload, dict):
        raise SourceReceiptError("required-data JSON must be an object")
    meta = raw_payload.get("meta")
    meta = dict(meta) if isinstance(meta, Mapping) else {}
    if str(meta.get("status") or "").strip().lower() != "ok":
        raise SourceReceiptError(
            "provider_incomplete: incomplete required-data payload cannot "
            "produce a quote receipt"
        )
    rows = raw_payload.get("rows")
    if not isinstance(rows, list):
        raise SourceReceiptError("required-data rows are invalid")
    _validate_rows_persist_without_loss(rows)
    source_outcome, _reason = validate_required_data_source_outcome(
        rows=rows,
        source_outcome=meta.get("source_outcome"),
        reason_code=meta.get("reason_code"),
        subject="payload",
    )
    if rows and str(meta.get("source_outcome") or "").strip().lower() != "success_rows":
        raise SourceReceiptError(
            "required-data payload lacks explicit success-rows evidence"
        )
    contract = validate_required_data_expected_fetch_contract(
        expected_fetch_contract,
        expected_symbol=str(expected_fetch_contract.get("symbol") or ""),
    )
    expected_symbol = str(contract["symbol"])
    raw_symbol = str(raw_payload.get("symbol") or "").strip().upper()
    if raw_symbol != expected_symbol:
        raise SourceReceiptError(
            "scope_identity_mismatch: required-data payload symbol mismatch"
        )
    try:
        _validate_raw_binding(
            meta=meta,
            expected_fetch_contract=contract,
            strict=True,
        )
    except SourceReceiptError as exc:
        raise SourceReceiptError(
            f"scope_identity_mismatch: {exc}"
        ) from exc
    try:
        _validate_trading_date_binding(
            meta=meta,
            expected_fetch_contract=contract,
        )
    except SourceReceiptError as exc:
        raise SourceReceiptError(
            f"scope_identity_mismatch: {exc}"
        ) from exc
    _validate_raw_underlier_binding(
        raw_payload=raw_payload,
        contract=contract,
    )
    _validate_timestamp_evidence(meta)
    projection_outcome = str(
        (contract.get("coverage_policy") or {}).get("projection_outcome")
        or ""
    ).strip()
    require_realized_volatility = bool(
        (contract.get("coverage_policy") or {}).get(
            "require_realized_volatility"
        )
    )
    if source_outcome == "success_empty":
        empty_reason = str(meta.get("reason_code") or "").strip().lower()
        if empty_reason == "no_expirations" and projection_outcome != "success_empty":
            raise SourceReceiptError(
                "success-empty payload contradicts expected fetch contract"
            )
        if empty_reason == "no_contract_rows" and projection_outcome != "success_rows":
            raise SourceReceiptError(
                "success-empty payload contradicts expected fetch contract"
            )
        rv_meta = meta.get("realized_volatility")
        if not isinstance(rv_meta, Mapping):
            raise SourceReceiptError(
                "success-empty required-data lacks no-contract RV evidence"
            )
        rv_status = str(rv_meta.get("status") or "").strip().lower()
        rv_reason = str(rv_meta.get("reason") or "").strip().lower()
        if require_realized_volatility:
            valid_empty_rv = rv_status == "not_applicable_no_contracts"
        else:
            valid_empty_rv = (
                rv_status == "not_applicable_no_contracts"
                or (rv_status == "skipped" and rv_reason == "not_requested")
            )
        if not valid_empty_rv:
            raise SourceReceiptError(
                "success-empty required-data RV evidence contradicts fetch contract"
            )
        discovery = contract.get("fetch_plan", {}).get("expiration_discovery")
        if empty_reason == "no_expirations":
            _validate_snapshot_completeness(meta=meta, rows=rows)
            if not isinstance(discovery, Mapping):
                raise SourceReceiptError(
                    "success-empty required-data lacks expected discovery evidence"
                )
            if (
                str(discovery.get("outcome") or "").strip().lower()
                != "success_empty"
                or str(discovery.get("reason_code") or "").strip().lower()
                != empty_reason
                or list(discovery.get("expirations") or [])
                or list(raw_payload.get("expirations") or [])
                or int(raw_payload.get("expiration_count") or 0) != 0
                or not _same_datetime(
                    discovery.get("observed_at_utc"),
                    meta.get("source_observed_at"),
                )
                or not _same_datetime(
                    discovery.get("completed_at_utc"),
                    meta.get("completed_at_utc"),
                )
            ):
                raise SourceReceiptError(
                    "success-empty required-data discovery evidence mismatch"
                )
        elif empty_reason == "no_contract_rows":
            coverage = evaluate_required_data_frame_fetch_plan_debug(
                pd.DataFrame(),
                dict(contract.get("fetch_plan") or {}),
                option_chain_evidence=meta,
            )
            if not coverage.accepted or coverage.status != "success_empty":
                raise SourceReceiptError(
                    f"{coverage.reason_code or 'internal_contract_error'}: "
                    "required-data filtered-empty evidence is invalid"
                )
            _validate_child_request_bindings(
                meta=meta,
                contract=contract,
                canonical_realized_volatility=None,
            )
        else:
            raise SourceReceiptError(
                "success-empty required-data reason is invalid"
            )
    else:
        if projection_outcome != "success_rows":
            raise SourceReceiptError(
                "row payload contradicts expected fetch contract"
            )
        identities = _row_identity_counter(rows)
        if any(identity[0] != expected_symbol for identity in identities):
            raise SourceReceiptError("required-data row symbol mismatch")
        coverage = evaluate_required_data_frame_fetch_plan_debug(
            pd.DataFrame(rows),
            dict(contract.get("fetch_plan") or {}),
            option_chain_evidence=meta,
        )
        if not coverage.accepted:
            merged_requests = (contract.get("fetch_plan") or {}).get("merged_requests")
            subject = (
                "child request evidence"
                if isinstance(merged_requests, list) and len(merged_requests) > 1
                else "payload"
            )
            raise SourceReceiptError(
                f"{coverage.reason_code or 'internal_contract_error'}: "
                f"required-data {subject} does not cover expected fetch contract"
            )
        canonical_realized_volatility = (
            _validate_required_realized_volatility(meta=meta, rows=rows)
            if require_realized_volatility
            else None
        )
        _validate_child_request_bindings(
            meta=meta,
            contract=contract,
            canonical_realized_volatility=canonical_realized_volatility,
        )
    return meta, rows, source_outcome, contract


def _validate_required_data_payload_candidate(
    *,
    raw_payload: Mapping[str, Any],
    expected_fetch_contract: Mapping[str, Any],
) -> tuple[dict[str, Any], list[Any], str, dict[str, Any]]:
    try:
        return _validate_required_data_payload_candidate_impl(
            raw_payload=raw_payload,
            expected_fetch_contract=expected_fetch_contract,
        )
    except (
        SourceReceiptError,
        TypeError,
        ValueError,
        OverflowError,
    ) as exc:
        message = str(exc)
        prefix = message.partition(":")[0].strip()
        if prefix in _VALIDATION_REASON_CODES:
            raise
        raise SourceReceiptError(
            f"internal_contract_error: {message}"
        ) from exc


def validate_required_data_payload_candidate(
    *,
    payload: Mapping[str, Any],
    expected_fetch_contract: Mapping[str, Any],
    now: datetime | str | None = None,
    require_fresh: bool = False,
) -> None:
    """Validate one provider payload before gateway health or cache mutation."""

    meta, _rows, _outcome, _contract = _validate_required_data_payload_candidate(
        raw_payload=payload,
        expected_fetch_contract=expected_fetch_contract,
    )
    if require_fresh:
        _validate_payload_freshness_reason_coded(meta=meta, now=now)


def _validate_required_data_quote_candidate(
    *,
    raw: Path,
    csv: Path,
    expected_fetch_contract: Mapping[str, Any],
) -> None:
    try:
        raw_payload = json.loads(raw.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceReceiptError("required-data JSON is unreadable") from exc
    try:
        frame = pd.read_csv(csv)
    except Exception as exc:
        raise SourceReceiptError(
            "required-data CSV is unreadable"
        ) from exc
    _validate_required_data_quote_content(
        raw_payload=raw_payload,
        frame=frame,
        expected_fetch_contract=expected_fetch_contract,
        csv_path=csv,
    )


def validate_required_data_quote_bytes(
    *,
    raw_json_bytes: bytes,
    required_data_csv_bytes: bytes,
    expected_fetch_contract: Mapping[str, Any],
) -> None:
    """Validate exact bytes after the canonical blob verified multiplier overrides."""

    try:
        raw_payload = json.loads(bytes(raw_json_bytes).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceReceiptError("required-data canonical JSON is unreadable") from exc
    try:
        frame = pd.read_csv(io.BytesIO(bytes(required_data_csv_bytes)))
    except Exception as exc:
        raise SourceReceiptError("required-data canonical CSV is unreadable") from exc
    _validate_required_data_quote_content(
        raw_payload=raw_payload,
        frame=frame,
        expected_fetch_contract=expected_fetch_contract,
        csv_path=None,
    )


def _validate_required_data_quote_content(
    *,
    raw_payload: Mapping[str, Any],
    frame: pd.DataFrame,
    expected_fetch_contract: Mapping[str, Any],
    csv_path: Path | None,
) -> None:
    meta, rows, source_outcome, contract = _validate_required_data_payload_candidate(
        raw_payload=raw_payload,
        expected_fetch_contract=expected_fetch_contract,
    )
    if source_outcome == "success_empty":
        if not frame.empty or list(frame.columns) != REQUIRED_DATA_COLUMNS:
            raise SourceReceiptError(
                "success-empty required-data CSV is not header-only"
            )
        return
    _validate_consumer_csv_projection(
        rows=rows,
        frame=frame,
        csv=csv_path,
        symbol=str(contract["symbol"]),
        raw_meta=meta,
    )
    coverage = evaluate_required_data_frame_fetch_plan_debug(
        df=frame,
        fetch_plan=dict(contract.get("fetch_plan") or {}),
        option_chain_evidence=meta,
    )
    if not coverage.accepted:
        raise SourceReceiptError(
            f"{coverage.reason_code or 'internal_contract_error'}: "
            "required-data CSV does not cover expected fetch contract"
        )


def validate_required_data_quote_candidate(
    *,
    producer_root: Path,
    raw_path: Path,
    csv_path: Path,
    expected_fetch_contract: Mapping[str, Any],
    now: datetime | str | None = None,
    require_fresh: bool = False,
) -> None:
    """Validate cached/fresh bytes without publishing a receipt."""

    root_input = Path(producer_root)
    if (
        not root_input.exists()
        or not root_input.is_dir()
        or root_input.is_symlink()
    ):
        raise SourceReceiptError("quote producer root is invalid")
    root = root_input.resolve()
    try:
        raw_relpath = Path(raw_path).absolute().relative_to(root).as_posix()
        csv_relpath = Path(csv_path).absolute().relative_to(root).as_posix()
    except ValueError as exc:
        raise SourceReceiptError(
            "required-data quote files escape producer root"
        ) from exc
    _validate_required_data_quote_candidate(
        raw=safe_existing_relative_path(root, raw_relpath),
        csv=safe_existing_relative_path(root, csv_relpath),
        expected_fetch_contract=expected_fetch_contract,
    )
    if require_fresh:
        try:
            raw_payload = json.loads(
                safe_existing_relative_path(root, raw_relpath).read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceReceiptError(
                "required-data JSON is unreadable"
            ) from exc
        meta = raw_payload.get("meta") if isinstance(raw_payload, Mapping) else None
        if not isinstance(meta, Mapping):
            raise SourceReceiptError(
                "required-data payload metadata is invalid"
            )
        _validate_payload_freshness_reason_coded(
            meta=meta,
            now=now,
        )


def _snapshot_complete(meta: Mapping[str, Any]) -> bool:
    if meta.get("snapshot_complete") is True:
        return True
    coverage = meta.get("snapshot_coverage")
    if isinstance(coverage, Mapping) and coverage.get("complete") is True:
        return True
    return False


def _validate_snapshot_completeness(
    *,
    meta: Mapping[str, Any],
    rows: list[Any],
) -> None:
    requested = _code_set(meta.get("snapshot_requested_code_set"))
    returned = _code_set(meta.get("snapshot_returned_code_set"))
    missing = _code_set(meta.get("snapshot_missing_code_set"))
    unexpected = _code_set(meta.get("snapshot_unexpected_code_set"))
    declared_counts = {
        "requested": meta.get("snapshot_requested_codes"),
        "returned": meta.get("snapshot_returned_codes"),
        "missing": meta.get("snapshot_missing_codes"),
        "unexpected": meta.get("snapshot_unexpected_codes"),
    }
    actual_counts = {
        "requested": len(requested),
        "returned": len(returned),
        "missing": len(missing),
        "unexpected": len(unexpected),
    }
    if any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in declared_counts.values()
    ):
        raise SourceReceiptError(
            "required-data snapshot coverage counts are invalid"
        )
    if declared_counts != actual_counts:
        raise SourceReceiptError(
            "required-data snapshot coverage counts mismatch"
        )
    if missing != requested.difference(returned):
        raise SourceReceiptError(
            "required-data snapshot missing-code evidence mismatch"
        )
    if unexpected != returned.difference(requested):
        raise SourceReceiptError(
            "required-data snapshot unexpected-code evidence mismatch"
        )
    if missing or not requested.issubset(returned) or not _snapshot_complete(meta):
        raise SourceReceiptError(
            "provider_incomplete: required-data payload lacks complete "
            "snapshot evidence"
        )
    row_code_items = [
        str(row.get("contract_symbol") or "").strip()
        for row in rows
        if isinstance(row, Mapping)
    ]
    row_codes = set(row_code_items)
    row_codes.discard("")
    if len(row_code_items) != len(rows) or any(not code for code in row_code_items):
        raise SourceReceiptError(
            "required-data rows contain invalid snapshot codes"
        )
    if len(row_code_items) != len(row_codes):
        raise SourceReceiptError(
            "required-data rows contain duplicate snapshot codes"
        )
    if rows and (row_codes != requested or len(rows) != len(requested)):
        raise SourceReceiptError(
            "required-data rows do not match requested snapshot codes"
        )
    if not rows and requested:
        raise SourceReceiptError(
            "success-empty required-data requested option snapshots"
        )


def _validate_required_realized_volatility(
    *,
    meta: Mapping[str, Any],
    rows: list[Any],
) -> dict[str, float | None]:
    meta_values = _required_realized_volatility_values(meta)
    rv_meta = meta.get("realized_volatility")
    assert isinstance(rv_meta, Mapping)
    term_matched = rv_meta.get("term_matched")
    if not isinstance(term_matched, Mapping):
        raise SourceReceiptError(
            "required-data payload lacks term-matched realized volatility"
        )
    rv_window_fields = (
        "realized_volatility_20",
        "realized_volatility_60",
        "realized_volatility_120",
    )
    for row in rows:
        if not isinstance(row, Mapping) or any(
            field not in row
            for field in (
                *rv_window_fields,
                "realized_volatility_estimate",
                "term_matched_rv",
                "term_matched_rv_status",
                "term_matched_rv_reason",
                "term_matched_rv_remaining_sessions",
                "term_matched_rv_lookback_sessions",
                "term_matched_rv_input_start",
                "term_matched_rv_input_end",
                "term_matched_rv_input_session_count",
                "term_matched_rv_input_hash",
                "expiration",
                "dte",
            )
        ):
            raise SourceReceiptError(
                "required-data rows lack canonical realized volatility"
            )
        row_windows = {
            field: _normalize_realized_volatility_value(
                row.get(field),
                allow_none=True,
            )
            for field in rv_window_fields
        }
        meta_windows = {field: meta_values[field] for field in rv_window_fields}
        if row_windows != meta_windows:
            raise SourceReceiptError(
                "required-data rows contradict canonical realized volatility"
            )
        estimate = _normalize_realized_volatility_value(
            row.get("realized_volatility_estimate"),
            allow_none=True,
        )
        from src.application.short_vol_metrics import (
            realized_volatility_estimate_for_dte,
        )

        expected = realized_volatility_estimate_for_dte(
            dte=row.get("dte"),
            rv_20=row_windows["realized_volatility_20"],
            rv_60=row_windows["realized_volatility_60"],
            rv_120=row_windows["realized_volatility_120"],
        )
        estimates_disagree = (estimate is None) != (expected is None)
        if (
            not estimates_disagree
            and estimate is not None
            and expected is not None
        ):
            estimates_disagree = not math.isclose(
                estimate,
                expected,
                rel_tol=0.0,
                abs_tol=1e-9,
            )
        if estimates_disagree:
            raise SourceReceiptError(
                "required-data row realized volatility does not match dte policy"
            )
        expiration = str(row.get("expiration") or "").strip()
        term_entry = term_matched.get(expiration)
        if not isinstance(term_entry, Mapping):
            raise SourceReceiptError(
                "required-data payload lacks expiry term-matched realized volatility"
            )
        _validate_term_matched_rv_binding(row=row, term_entry=term_entry)
    return meta_values


def _validate_term_matched_rv_binding(
    *,
    row: Mapping[str, Any],
    term_entry: Mapping[str, Any],
) -> None:
    expiration = str(row.get("expiration") or "").strip()
    if (
        term_entry.get("schema_version") != "term_matched_rv.v1"
        or str(term_entry.get("expiration") or "").strip() != expiration
    ):
        raise SourceReceiptError(
            "required-data term-matched realized volatility is invalid"
        )
    status = str(term_entry.get("status") or "").strip().lower()
    if status not in {"ok", "data_unavailable"}:
        raise SourceReceiptError(
            "required-data term-matched realized volatility is invalid"
        )
    reason = term_entry.get("reason")
    unavailable_reason_invalid = status == "data_unavailable" and (
        not isinstance(reason, str) or not str(reason).strip()
    )
    if (status == "ok" and reason is not None) or unavailable_reason_invalid:
        raise SourceReceiptError(
            "required-data term-matched realized volatility is invalid"
        )
    meta_rv = _normalize_realized_volatility_value(
        term_entry.get("term_matched_rv"),
        allow_none=(status == "data_unavailable"),
    )
    row_rv = _normalize_realized_volatility_value(
        row.get("term_matched_rv"),
        allow_none=(status == "data_unavailable"),
    )
    if (
        meta_rv != row_rv
        or str(row.get("term_matched_rv_status")) != status
        or row.get("term_matched_rv_reason") != reason
    ):
        raise SourceReceiptError(
            "required-data row contradicts term-matched realized volatility"
        )
    remaining = term_entry.get("remaining_sessions")
    lookback = term_entry.get("lookback_sessions")
    close_count = term_entry.get("input_close_session_count")
    return_count = term_entry.get("input_return_count")
    if status == "data_unavailable":
        known_horizon_invalid = remaining is not None and (
            isinstance(remaining, bool)
            or not isinstance(remaining, int)
            or remaining < 0
            or isinstance(lookback, bool)
            or not isinstance(lookback, int)
            or lookback != max(20, remaining)
        )
        row_bindings = {
            "term_matched_rv_remaining_sessions": remaining,
            "term_matched_rv_lookback_sessions": lookback,
            "term_matched_rv_input_start": None,
            "term_matched_rv_input_end": None,
            "term_matched_rv_input_session_count": 0,
            "term_matched_rv_input_hash": None,
        }
        if (
            meta_rv is not None
            or (remaining is None) != (lookback is None)
            or known_horizon_invalid
            or term_entry.get("input_start") is not None
            or term_entry.get("input_end") is not None
            or term_entry.get("input_close_session_count") != 0
            or term_entry.get("input_return_count") != 0
            or term_entry.get("input_hash") is not None
            or any(row.get(field) != value for field, value in row_bindings.items())
        ):
            raise SourceReceiptError(
                "required-data unavailable term-matched volatility evidence is invalid"
            )
        return
    if (
        isinstance(remaining, bool)
        or not isinstance(remaining, int)
        or remaining <= 0
        or isinstance(lookback, bool)
        or not isinstance(lookback, int)
        or lookback != max(20, remaining)
        or isinstance(close_count, bool)
        or not isinstance(close_count, int)
        or close_count != lookback + 1
        or isinstance(return_count, bool)
        or not isinstance(return_count, int)
        or return_count != lookback
    ):
        raise SourceReceiptError(
            "required-data term-matched realized volatility horizon is invalid"
        )
    try:
        input_start = date.fromisoformat(str(term_entry.get("input_start") or ""))
        input_end = date.fromisoformat(str(term_entry.get("input_end") or ""))
    except ValueError as exc:
        raise SourceReceiptError(
            "required-data term-matched realized volatility input range is invalid"
        ) from exc
    input_hash = str(term_entry.get("input_hash") or "")
    if (
        input_start > input_end
        or len(input_hash) != 64
        or any(char not in "0123456789abcdef" for char in input_hash)
    ):
        raise SourceReceiptError(
            "required-data term-matched realized volatility evidence is invalid"
        )
    row_bindings = {
        "term_matched_rv_remaining_sessions": remaining,
        "term_matched_rv_lookback_sessions": lookback,
        "term_matched_rv_input_start": input_start.isoformat(),
        "term_matched_rv_input_end": input_end.isoformat(),
        "term_matched_rv_input_session_count": close_count,
        "term_matched_rv_input_hash": input_hash,
    }
    if any(row.get(field) != value for field, value in row_bindings.items()):
        raise SourceReceiptError(
            "required-data row term-matched realized volatility evidence mismatch"
        )


def _required_realized_volatility_values(
    meta: Mapping[str, Any],
) -> dict[str, float | None]:
    rv_meta = meta.get("realized_volatility")
    if not isinstance(rv_meta, Mapping):
        raise SourceReceiptError(
            "required-data payload lacks required realized volatility"
        )
    rv_fields = (
        "realized_volatility_20",
        "realized_volatility_60",
        "realized_volatility_120",
        "realized_volatility_estimate",
    )
    if any(field not in rv_meta for field in rv_fields):
        raise SourceReceiptError(
            "required-data payload lacks required realized volatility"
        )
    meta_values = {
        field: _normalize_realized_volatility_value(
            rv_meta.get(field),
            allow_none=True,
        )
        for field in rv_fields
    }
    if str(rv_meta.get("status") or "").strip().lower() not in {"ok", "partial"}:
        raise SourceReceiptError(
            "required-data payload lacks required realized volatility"
        )
    for evidence_field in ("qfq_history", "trading_calendar"):
        evidence = rv_meta.get(evidence_field)
        if (
            not isinstance(evidence, Mapping)
            or str(evidence.get("status") or "").strip().lower() != "ok"
        ):
            raise SourceReceiptError(
                "required-data payload lacks term-matched realized volatility evidence"
            )
    return meta_values


def _normalize_realized_volatility_value(
    value: Any,
    *,
    allow_none: bool,
) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SourceReceiptError(
            "required-data payload lacks required realized volatility"
        )
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise SourceReceiptError(
            "required-data payload lacks required realized volatility"
        )
    return normalized


def _validate_timestamp_evidence(meta: Mapping[str, Any]) -> None:
    observed_at = meta.get("source_observed_at")
    completed_at = meta.get("completed_at_utc")
    try:
        observed = _parse_datetime(observed_at)
        completed = _parse_datetime(completed_at)
    except (TypeError, ValueError) as exc:
        raise SourceReceiptError(
            "required-data payload lacks stable observation timestamps"
        ) from exc
    if completed < observed:
        raise SourceReceiptError(
            "required-data completion precedes source observation"
        )
    requests = meta.get("requests")
    if requests is None:
        return
    if (
        not isinstance(requests, list)
        or not requests
        or any(not isinstance(item, Mapping) for item in requests)
    ):
        raise SourceReceiptError(
            "required-data child timestamp evidence is invalid"
        )
    child_observed: list[datetime] = []
    child_completed: list[datetime] = []
    for item in requests:
        _validate_timestamp_evidence(item)
        child_observed.append(_parse_datetime(item.get("source_observed_at")))
        child_completed.append(_parse_datetime(item.get("completed_at_utc")))
    if observed != min(child_observed) or completed != max(child_completed):
        raise SourceReceiptError(
            "required-data aggregate timestamp evidence mismatch"
        )


def _validate_child_request_bindings(
    *,
    meta: Mapping[str, Any],
    contract: Mapping[str, Any],
    canonical_realized_volatility: Mapping[str, float | None] | None,
) -> None:
    merged_requests = (contract.get("fetch_plan") or {}).get("merged_requests")
    if not isinstance(merged_requests, list) or len(merged_requests) <= 1:
        return
    requests = meta.get("requests")
    assert isinstance(requests, list)
    children_by_index = {
        child["request_index"]: child
        for child in requests
        if isinstance(child, Mapping)
    }
    discovery = (contract.get("fetch_plan") or {}).get("expiration_discovery")
    discovery_identity = (
        discovery.get("request_identity") if isinstance(discovery, Mapping) else None
    )
    expected_underlier = (
        str(discovery_identity.get("underlier") or "").strip()
        if isinstance(discovery_identity, Mapping)
        else ""
    )
    expected_symbol = str(contract.get("symbol") or "").strip().upper()
    for expected_index, planned_request in enumerate(merged_requests):
        assert isinstance(planned_request, Mapping)
        child = children_by_index[expected_index]
        if (
            str(child.get("request_symbol") or "").strip().upper() != expected_symbol
            or str(child.get("request_underlier_code") or "").strip()
            != expected_underlier
        ):
            raise SourceReceiptError(
                "scope_identity_mismatch: required-data child request symbol identity mismatch"
            )
        try:
            _validate_raw_binding(
                meta=child,
                expected_fetch_contract=contract,
                strict=True,
            )
        except SourceReceiptError as exc:
            raise SourceReceiptError(
                "scope_identity_mismatch: required-data child request binding mismatch"
            ) from exc
        try:
            _validate_trading_date_binding(
                meta=child,
                expected_fetch_contract=contract,
                planned_request=planned_request,
            )
        except SourceReceiptError as exc:
            raise SourceReceiptError(f"scope_identity_mismatch: {exc}") from exc
        if canonical_realized_volatility is not None and child.get(
            "snapshot_requested_code_set"
        ):
            child_realized_volatility = _required_realized_volatility_values(child)
            if child_realized_volatility != dict(canonical_realized_volatility):
                raise SourceReceiptError(
                    "required-data child request realized volatility mismatch"
                )


def _validate_raw_underlier_binding(
    *,
    raw_payload: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> str:
    discovery = (contract.get("fetch_plan") or {}).get(
        "expiration_discovery"
    )
    identity = (
        discovery.get("request_identity")
        if isinstance(discovery, Mapping)
        else None
    )
    expected_underlier = (
        str(identity.get("underlier") or "").strip()
        if isinstance(identity, Mapping)
        else ""
    )
    if (
        not expected_underlier
        or str(raw_payload.get("underlier_code") or "").strip()
        != expected_underlier
    ):
        raise SourceReceiptError(
            "scope_identity_mismatch: required-data payload underlier identity "
            "mismatch"
        )
    fetch_plan = contract.get("fetch_plan")
    expected_observation = (
        fetch_plan.get("underlier_observation")
        if isinstance(fetch_plan, Mapping)
        else None
    )
    if expected_observation is not None:
        meta = raw_payload.get("meta")
        observed = meta.get("underlier_observation") if isinstance(meta, Mapping) else None
        if observed != expected_observation:
            raise SourceReceiptError(
                "scope_identity_mismatch: required-data underlier observation mismatch"
            )
    return expected_underlier


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError("timestamp is missing")
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp timezone is missing")
    return parsed.astimezone(timezone.utc)


def _validate_quote_freshness(
    *,
    source_observed_at: Any,
    now: datetime | str | None,
) -> datetime:
    try:
        observed = _parse_datetime(source_observed_at)
        now_value = _parse_datetime(now or datetime.now(timezone.utc))
    except (TypeError, ValueError) as exc:
        raise SourceReceiptError(
            "required-data quote freshness evidence is invalid"
        ) from exc
    if observed > now_value:
        raise SourceReceiptError(
            "required-data quote observation is in the future"
        )
    expires_at = observed + timedelta(
        seconds=int(SOURCE_MAX_AGE_SECONDS["quotes"])
    )
    if now_value >= expires_at:
        raise _StaleRequiredDataError("required-data quote observation is stale")
    return now_value


def _validate_payload_freshness(
    *,
    meta: Mapping[str, Any],
    now: datetime | str | None,
) -> datetime:
    now_value = _validate_quote_freshness(
        source_observed_at=meta.get("source_observed_at"),
        now=now,
    )
    requests = meta.get("requests")
    if requests is not None:
        if not isinstance(requests, list):
            raise SourceReceiptError(
                "required-data child freshness evidence is invalid"
            )
        for item in requests:
            if not isinstance(item, Mapping):
                raise SourceReceiptError(
                    "required-data child freshness evidence is invalid"
                )
            _validate_payload_freshness(
                meta=item,
                now=now_value,
            )
    try:
        completed_at = _parse_datetime(meta.get("completed_at_utc"))
    except (TypeError, ValueError) as exc:
        raise SourceReceiptError(
            "required-data completion timestamp is invalid"
        ) from exc
    if completed_at > now_value:
        raise SourceReceiptError(
            "required-data completion is in the future"
        )
    return now_value


def _validate_payload_freshness_reason_coded(
    *,
    meta: Mapping[str, Any],
    now: datetime | str | None,
) -> datetime:
    try:
        return _validate_payload_freshness(meta=meta, now=now)
    except _StaleRequiredDataError as exc:
        raise SourceReceiptError(f"stale_data: {exc}") from exc
    except SourceReceiptError as exc:
        raise SourceReceiptError(f"freshness_unproven: {exc}") from exc


def _validate_rows_persist_without_loss(rows: list[Any]) -> None:
    from src.application.required_data_validation import validate_required_rows

    validated, stats = validate_required_rows(rows)
    if stats.dropped_rows or len(validated) != len(rows):
        raise SourceReceiptError(
            "required-data rows would be dropped during persistence"
        )


def _code_set(value: Any) -> frozenset[str]:
    if not isinstance(value, list):
        raise SourceReceiptError(
            "required-data snapshot code-set evidence is invalid"
        )
    normalized = [str(item or "").strip() for item in value]
    if any(not item for item in normalized) or len(normalized) != len(set(normalized)):
        raise SourceReceiptError(
            "required-data snapshot code-set evidence is invalid"
        )
    return frozenset(normalized)


def _row_identity_counter(rows: list[Any]) -> Counter[tuple[Any, ...]]:
    identities: Counter[tuple[Any, ...]] = Counter()
    for row in rows:
        if not isinstance(row, Mapping):
            raise SourceReceiptError("required-data JSON rows are invalid")
        identities[_row_identity(row)] += 1
    return identities


def _validate_consumer_csv_projection(
    *,
    rows: list[Any],
    frame: pd.DataFrame,
    csv: Path | None,
    symbol: str,
    raw_meta: Mapping[str, Any],
) -> None:
    if list(frame.columns) != REQUIRED_DATA_COLUMNS:
        raise SourceReceiptError(
            "required-data CSV columns differ from canonical projection"
        )
    expected = _csv_roundtrip_frame(rows)
    if len(frame.index) != len(expected.index):
        raise SourceReceiptError(
            "required-data JSON and CSV row counts differ"
        )
    if frame.equals(expected):
        return
    exact_columns = [
        column for column in REQUIRED_DATA_COLUMNS if column != "multiplier"
    ]
    if frame[exact_columns].equals(expected[exact_columns]):
        multiplier_enriched = False
        for raw_value, csv_value in zip(
            expected["multiplier"],
            frame["multiplier"],
            strict=True,
        ):
            if _canonical_csv_value(raw_value) == _canonical_csv_value(csv_value):
                continue
            if _is_valid_multiplier_enrichment(
                raw_value=raw_value,
                csv_value=csv_value,
            ):
                multiplier_enriched = True
                continue
            break
        else:
            if multiplier_enriched and csv is not None:
                _validate_quote_cache_metadata_binding(
                    csv=csv,
                    symbol=symbol,
                    raw_meta=raw_meta,
                )
            return
    multiplier_enriched = False
    for row_index in range(len(expected.index)):
        # Preserve mixed-dtype row coercion while constructing each Series once.
        expected_row = expected.iloc[row_index]
        actual_row = frame.iloc[row_index]
        for column, raw_value, csv_value in zip(
            REQUIRED_DATA_COLUMNS,
            expected_row,
            actual_row,
            strict=True,
        ):
            expected_value = _canonical_csv_value(raw_value)
            actual_value = _canonical_csv_value(csv_value)
            if expected_value == actual_value:
                continue
            if column == "multiplier" and _is_valid_multiplier_enrichment(
                raw_value=raw_value,
                csv_value=csv_value,
            ):
                multiplier_enriched = True
                continue
            raise SourceReceiptError(
                "required-data JSON and CSV canonical projections differ"
            )
    if multiplier_enriched and csv is not None:
        _validate_quote_cache_metadata_binding(
            csv=csv,
            symbol=symbol,
            raw_meta=raw_meta,
        )


def _csv_roundtrip_frame(rows: list[Any]) -> pd.DataFrame:
    projected = pd.DataFrame(rows)
    for column in REQUIRED_DATA_COLUMNS:
        if column not in projected.columns:
            projected[column] = pd.NA
    projected = projected[REQUIRED_DATA_COLUMNS]
    buffer = io.StringIO()
    projected.to_csv(buffer, index=False)
    return pd.read_csv(io.StringIO(buffer.getvalue()))


def _canonical_csv_value(value: Any) -> tuple[str, Any]:
    try:
        if pd.isna(value):
            return "null", None
    except (TypeError, ValueError):
        pass
    if isinstance(value, bool) or type(value).__name__ == "bool_":
        return "bool", bool(value)
    if isinstance(value, Number):
        return "number", float(value)
    return "string", str(value)


def _is_valid_multiplier_enrichment(*, raw_value: Any, csv_value: Any) -> bool:
    try:
        raw_number = float(raw_value)
    except (TypeError, ValueError):
        raw_number = math.nan
    try:
        csv_number = float(csv_value)
    except (TypeError, ValueError):
        return False
    return (
        (not math.isfinite(raw_number) or raw_number <= 0)
        and math.isfinite(csv_number)
        and csv_number > 0
    )


def _validate_quote_cache_metadata_binding(
    *,
    csv: Path,
    symbol: str,
    raw_meta: Mapping[str, Any],
) -> dict[str, Any]:
    metadata_path = quote_cache_metadata_path(csv)
    if not metadata_path.is_file() or metadata_path.is_symlink():
        raise SourceReceiptError(
            "required-data final CSV metadata is unavailable"
        )
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceReceiptError(
            "required-data final CSV metadata is unreadable"
        ) from exc
    if not isinstance(metadata, Mapping):
        raise SourceReceiptError(
            "required-data final CSV metadata is invalid"
        )
    expected_market = str(symbol_market(symbol) or "").strip().upper()
    if (
        metadata.get("schema_version") != QUOTE_CACHE_METADATA_SCHEMA
        or str(metadata.get("symbol") or "").strip().upper()
        != str(symbol or "").strip().upper()
        or str(metadata.get("market") or "").strip().upper() != expected_market
        or str(metadata.get("source") or "").strip().lower() != "opend"
        or not str(metadata.get("source_run_id") or "").strip()
        or str(metadata.get("csv_sha256") or "") != sha256_bytes(csv.read_bytes())
        or not _same_datetime(
            metadata.get("source_observed_at"),
            raw_meta.get("source_observed_at"),
        )
    ):
        raise SourceReceiptError(
            "required-data final CSV metadata does not bind canonical bytes"
        )
    return dict(metadata)


def _row_identity(row: Mapping[str, Any]) -> tuple[Any, ...]:
    symbol = str(row.get("symbol") or "").strip().upper()
    option_type = str(row.get("option_type") or "").strip().lower()
    expiration = str(row.get("expiration") or "").strip()[:10]
    contract_symbol = str(row.get("contract_symbol") or "").strip()
    try:
        strike = float(row.get("strike"))
    except (TypeError, ValueError) as exc:
        raise SourceReceiptError(
            "required-data row identity is incomplete"
        ) from exc
    if (
        not symbol
        or option_type not in {"put", "call"}
        or len(expiration) != 10
        or not contract_symbol
        or not math.isfinite(strike)
    ):
        raise SourceReceiptError(
            "required-data row identity is incomplete"
        )
    return symbol, option_type, expiration, contract_symbol, strike


def _validate_raw_binding(
    *,
    meta: Mapping[str, Any],
    expected_fetch_contract: Mapping[str, Any],
    strict: bool,
) -> None:
    binding = dict(expected_fetch_contract.get("fetch_binding") or {})
    observed_source = str(meta.get("source") or "").strip()
    observed_host = str(meta.get("host") or "").strip()
    observed_port = meta.get("port")
    if strict and (not observed_source or not observed_host or observed_port in (None, "")):
        raise SourceReceiptError(
            "required-data payload lacks physical binding evidence"
        )
    if observed_source and _normalize_fetch_source(observed_source) != str(
        binding.get("source") or ""
    ):
        raise SourceReceiptError("required-data payload source mismatch")
    if observed_host and observed_host != str(binding.get("host") or ""):
        raise SourceReceiptError("required-data payload host mismatch")
    if observed_port not in (None, ""):
        if isinstance(observed_port, bool) or not isinstance(observed_port, int):
            raise SourceReceiptError(
                "required-data payload port is invalid"
            )
        if observed_port != binding.get("port"):
            raise SourceReceiptError(
                "required-data payload port mismatch"
            )


def _validate_trading_date_binding(
    *,
    meta: Mapping[str, Any],
    expected_fetch_contract: Mapping[str, Any],
    planned_request: Mapping[str, Any] | None = None,
) -> None:
    fetch_plan = expected_fetch_contract.get("fetch_plan")
    discovery = (
        fetch_plan.get("expiration_discovery")
        if isinstance(fetch_plan, Mapping)
        else None
    )
    identity = (
        discovery.get("request_identity")
        if isinstance(discovery, Mapping)
        else None
    )
    expected = (
        identity.get("trading_date")
        if isinstance(identity, Mapping)
        else None
    )
    if not isinstance(expected, str) or not expected or expected != expected.strip():
        raise SourceReceiptError(
            "required-data expected trading date is invalid"
        )
    try:
        parsed_expected = date.fromisoformat(expected)
    except ValueError as exc:
        raise SourceReceiptError(
            "required-data expected trading date is invalid"
        ) from exc
    if parsed_expected.isoformat() != expected:
        raise SourceReceiptError(
            "required-data expected trading date is invalid"
        )
    if (
        planned_request is not None
        and planned_request.get("trading_date") != expected
    ):
        raise SourceReceiptError(
            "required-data planned request trading date mismatch"
        )
    if meta.get("trading_date") != expected:
        raise SourceReceiptError(
            "required-data payload trading date mismatch"
        )


def _validate_fetch_policy_binding(
    *,
    fetch_policy: Mapping[str, Any],
    expected_fetch_contract: Mapping[str, Any],
) -> None:
    binding = dict(expected_fetch_contract.get("fetch_binding") or {})
    if any(fetch_policy.get(field) in (None, "") for field in ("source", "host", "port")):
        raise SourceReceiptError(
            "required-data fetch policy lacks physical binding"
        )
    actual_port = fetch_policy.get("port")
    expected_port = binding.get("port")
    if (
        isinstance(actual_port, bool)
        or not isinstance(actual_port, int)
        or isinstance(expected_port, bool)
        or not isinstance(expected_port, int)
    ):
        raise SourceReceiptError(
            "required-data fetch policy binding is invalid"
        )
    actual = {
        "source": _normalize_fetch_source(fetch_policy.get("source")),
        "host": str(fetch_policy.get("host") or "").strip(),
        "port": actual_port,
    }
    expected = {
        "source": str(binding.get("source") or ""),
        "host": str(binding.get("host") or ""),
        "port": expected_port,
    }
    if actual != expected:
        raise SourceReceiptError(
            "required-data fetch policy binding mismatch"
        )


def _normalize_fetch_source(value: Any) -> str:
    if not str(value or "").strip():
        return ""
    return normalize_fetch_source(value)


def append_metrics_json(metrics_path: Path, payload: dict[str, Any], max_entries: int = 400) -> None:
    """Append payload into a bounded JSON list file. Keeps last max_entries records."""
    try:
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        arr = []
        if metrics_path.exists() and metrics_path.stat().st_size > 0:
            try:
                obj = json.loads(metrics_path.read_text(encoding="utf-8"))
                if isinstance(obj, list):
                    arr = obj
            except Exception:
                arr = []
        arr.append(payload)
        if len(arr) > int(max_entries):
            arr = arr[-int(max_entries) :]
        metrics_path.write_text(json.dumps(arr, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        pass


def save_outputs(
    base: Path,
    symbol: str,
    payload: dict[str, Any],
    *,
    output_root: Path | None = None,
    publish_cache_metadata: bool = True,
) -> tuple[Path, Path]:
    root = output_root.resolve() if output_root is not None else (base / "output_shared" / "required_data").resolve()
    raw_dir = root / "raw"
    parsed_dir = root / "parsed"
    raw_dir.mkdir(parents=True, exist_ok=True)
    parsed_dir.mkdir(parents=True, exist_ok=True)

    raw_path = raw_dir / f"{symbol}_required_data.json"
    csv_path = parsed_dir / f"{symbol}_required_data.csv"

    try:
        from src.application.required_data_validation import validate_required_rows

        rows0 = payload.get("rows") or []
        rows1, st = validate_required_rows(rows0)
        payload["rows"] = rows1
        meta = payload.get("meta") or {}
        if not isinstance(meta, dict):
            meta = {"meta": str(meta)}
        meta["validation"] = {
            "total_rows": int(st.total_rows),
            "kept_rows": int(st.kept_rows),
            "dropped_rows": int(st.dropped_rows),
            "missing_strike": int(st.missing_strike),
            "missing_expiration": int(st.missing_expiration),
            "missing_dte": int(st.missing_dte),
            "missing_option_type": int(st.missing_option_type),
        }
        payload["meta"] = meta
    except Exception as exc:
        meta = payload.get("meta")
        if not isinstance(meta, dict):
            meta = {}
        meta.update(
            {
                "status": "error",
                "source_outcome": "parse_error",
                "error_code": "ROW_VALIDATION_ERROR",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        payload["meta"] = meta
        payload["rows"] = []

    atomic_write_text(raw_path, json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")

    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    is_error_payload = str((meta or {}).get("status") or "").lower() in {"error", "fail", "failed"}
    if is_error_payload and csv_path.exists() and csv_path.stat().st_size > 0:
        return raw_path, csv_path

    df = pd.DataFrame(payload.get("rows") or [])
    if is_error_payload:
        df = pd.DataFrame()

    if df.empty:
        df_out = pd.DataFrame(columns=REQUIRED_DATA_COLUMNS)
    else:
        for column in REQUIRED_DATA_COLUMNS:
            if column not in df.columns:
                df[column] = pd.NA
        df_out = df[REQUIRED_DATA_COLUMNS]

    buf = io.StringIO()
    df_out.to_csv(buf, index=False)
    atomic_write_text(csv_path, buf.getvalue(), encoding="utf-8")
    observed_at = _metadata_observed_at(
        meta=(meta or {}),
        fallback=datetime.now(timezone.utc),
    )
    source_run_id = str(
        (meta or {}).get("producer_run_id")
        or (meta or {}).get("run_id")
        or f"opend-save-{observed_at.strftime('%Y%m%dT%H%M%S%fZ')}"
    )
    if not is_error_payload and publish_cache_metadata:
        publish_quote_cache_metadata(
            csv_path=csv_path,
            symbol=symbol,
            source="opend",
            source_run_id=source_run_id,
            observed_at=observed_at,
        )
    return raw_path, csv_path


def _metadata_observed_at(
    *,
    meta: Mapping[str, Any],
    fallback: datetime,
) -> datetime:
    try:
        return _parse_datetime(meta.get("source_observed_at"))
    except (TypeError, ValueError):
        return fallback.astimezone(timezone.utc)


def _candidate_source_run_id(
    *,
    csv_path: Path,
    meta: Mapping[str, Any],
    producer_run_id: str | None,
    preserve_existing: bool,
) -> str:
    if preserve_existing:
        metadata_path = quote_cache_metadata_path(csv_path)
        if metadata_path.is_file() and not metadata_path.is_symlink():
            try:
                existing = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                existing = None
            if isinstance(existing, Mapping):
                existing_run_id = str(existing.get("source_run_id") or "").strip()
                if existing_run_id:
                    return existing_run_id
    declared_run_id = str(
        meta.get("producer_run_id") or meta.get("run_id") or ""
    ).strip()
    if declared_run_id:
        return declared_run_id
    current_run_id = str(producer_run_id or "").strip()
    if current_run_id:
        return current_run_id
    observed = _metadata_observed_at(
        meta=meta,
        fallback=datetime.now(timezone.utc),
    )
    return f"opend-save-{observed.strftime('%Y%m%dT%H%M%S%fZ')}"


def _publish_final_quote_cache_metadata(
    *,
    csv_path: Path,
    symbol: str,
    meta: Mapping[str, Any],
    producer_run_id: str | None,
    preserve_existing_run_id: bool,
) -> dict[str, Any]:
    source_run_id = _candidate_source_run_id(
        csv_path=csv_path,
        meta=meta,
        producer_run_id=producer_run_id,
        preserve_existing=preserve_existing_run_id,
    )
    observed_at = _metadata_observed_at(
        meta=meta,
        fallback=datetime.now(timezone.utc),
    )
    return publish_quote_cache_metadata(
        csv_path=csv_path,
        symbol=symbol,
        source="opend",
        source_run_id=source_run_id,
        observed_at=observed_at,
    )


def finalize_unplanned_required_data_candidate(
    *,
    base: Path,
    producer_root: Path,
    symbol: str,
    payload: dict[str, Any],
    source: str,
    host: str,
    port: int,
    require_realized_volatility: bool,
    now: datetime | str | None = None,
) -> dict[str, Any]:
    """Persist a manual candidate without creating or adopting receipt authority."""

    root = Path(producer_root).resolve()
    symbol_norm = str(symbol or "").strip().upper()
    binding = {
        "source": _normalize_fetch_source(source),
        "host": str(host or "").strip(),
        "port": int(port),
    }
    meta, _rows, _outcome = _validate_unplanned_required_data_payload(
        payload=payload,
        expected_symbol=symbol_norm,
        expected_binding=binding,
        require_realized_volatility=require_realized_volatility,
    )
    _validate_payload_freshness(meta=meta, now=now)
    raw_path, csv_path = save_outputs(
        Path(base),
        symbol_norm,
        payload,
        output_root=root,
        publish_cache_metadata=False,
    )
    _validate_unplanned_required_data_files(
        raw_path=raw_path,
        csv_path=csv_path,
        expected_symbol=symbol_norm,
        expected_binding=binding,
        require_realized_volatility=require_realized_volatility,
        now=now,
    )

    from src.application.multiplier_steps import (
        apply_multiplier_cache_to_required_data_csv,
    )

    apply_multiplier_cache_to_required_data_csv(
        base=Path(base),
        required_data_dir=root,
        symbol=symbol_norm,
    )
    _publish_final_quote_cache_metadata(
        csv_path=csv_path,
        symbol=symbol_norm,
        meta=meta,
        producer_run_id=None,
        preserve_existing_run_id=False,
    )
    _validate_unplanned_required_data_files(
        raw_path=raw_path,
        csv_path=csv_path,
        expected_symbol=symbol_norm,
        expected_binding=binding,
        require_realized_volatility=require_realized_volatility,
        now=now,
    )
    return {
        "mode": "manual_unplanned",
        "raw_path": raw_path,
        "csv_path": csv_path,
        "quote_receipt_path": None,
        "quote_receipt": None,
        "evidence": None,
    }


def _validate_unplanned_required_data_payload(
    *,
    payload: Mapping[str, Any],
    expected_symbol: str,
    expected_binding: Mapping[str, Any],
    require_realized_volatility: bool,
) -> tuple[dict[str, Any], list[Any], str]:
    if not isinstance(payload, dict):
        raise SourceReceiptError("required-data JSON must be an object")
    meta = payload.get("meta")
    meta = dict(meta) if isinstance(meta, Mapping) else {}
    if str(meta.get("status") or "").strip().lower() != "ok":
        raise SourceReceiptError(
            "incomplete required-data payload cannot be persisted"
        )
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise SourceReceiptError("required-data rows are invalid")
    _validate_rows_persist_without_loss(rows)
    source_outcome, _reason = validate_required_data_source_outcome(
        rows=rows,
        source_outcome=meta.get("source_outcome"),
        reason_code=meta.get("reason_code"),
        subject="manual payload",
    )
    if rows and str(meta.get("source_outcome") or "").strip().lower() != "success_rows":
        raise SourceReceiptError(
            "required-data payload lacks explicit success-rows evidence"
        )
    if str(payload.get("symbol") or "").strip().upper() != expected_symbol:
        raise SourceReceiptError("required-data payload symbol mismatch")
    _validate_raw_binding(
        meta=meta,
        expected_fetch_contract={"fetch_binding": dict(expected_binding)},
        strict=True,
    )
    _validate_timestamp_evidence(meta)
    _validate_snapshot_completeness(meta=meta, rows=rows)

    expirations = payload.get("expirations")
    if not isinstance(expirations, list):
        raise SourceReceiptError(
            "required-data expiration evidence is invalid"
        )
    normalized_expirations = [str(item or "").strip() for item in expirations]
    try:
        expiration_count = int(payload.get("expiration_count") or 0)
    except (TypeError, ValueError) as exc:
        raise SourceReceiptError(
            "required-data expiration evidence is invalid"
        ) from exc
    if (
        any(not item for item in normalized_expirations)
        or len(normalized_expirations) != len(set(normalized_expirations))
        or expiration_count != len(normalized_expirations)
    ):
        raise SourceReceiptError(
            "required-data expiration evidence is invalid"
        )
    if source_outcome == "success_empty":
        if normalized_expirations:
            raise SourceReceiptError(
                "success-empty required-data contains expirations"
            )
        if require_realized_volatility:
            rv_meta = meta.get("realized_volatility")
            if (
                not isinstance(rv_meta, Mapping)
                or str(rv_meta.get("status") or "").strip().lower()
                != "not_applicable_no_contracts"
            ):
                raise SourceReceiptError(
                    "success-empty required-data lacks no-contract RV evidence"
                )
    elif require_realized_volatility:
        _validate_required_realized_volatility(meta=meta, rows=rows)
    return meta, rows, source_outcome


def _validate_unplanned_required_data_files(
    *,
    raw_path: Path,
    csv_path: Path,
    expected_symbol: str,
    expected_binding: Mapping[str, Any],
    require_realized_volatility: bool,
    now: datetime | str | None,
) -> None:
    try:
        payload = json.loads(raw_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceReceiptError("required-data JSON is unreadable") from exc
    meta, rows, source_outcome = _validate_unplanned_required_data_payload(
        payload=payload,
        expected_symbol=expected_symbol,
        expected_binding=expected_binding,
        require_realized_volatility=require_realized_volatility,
    )
    _validate_payload_freshness(meta=meta, now=now)
    try:
        frame = pd.read_csv(csv_path)
    except Exception as exc:
        raise SourceReceiptError("required-data CSV is unreadable") from exc
    if source_outcome == "success_empty":
        if not frame.empty or list(frame.columns) != REQUIRED_DATA_COLUMNS:
            raise SourceReceiptError(
                "success-empty required-data CSV is not header-only"
            )
        return
    _validate_consumer_csv_projection(
        rows=rows,
        frame=frame,
        csv=csv_path,
        symbol=expected_symbol,
        raw_meta=meta,
    )


def finalize_required_data_quote_candidate(
    *,
    base: Path,
    producer_root: Path,
    producer_run_id: str | None,
    symbol: str,
    expected_fetch_contract: Mapping[str, Any],
    fetch_policy: Mapping[str, Any],
    mode: str,
    payload: dict[str, Any] | None = None,
    now: datetime | str | None = None,
) -> dict[str, Any]:
    """Finalize one provider/cache candidate before it gains receipt authority."""

    mode_norm = str(mode or "").strip().lower()
    if mode_norm not in {"fresh", "cached", "subprocess", "success_empty"}:
        raise SourceReceiptError("required-data finalizer mode is invalid")
    root = Path(producer_root).resolve()
    symbol_norm = str(symbol or "").strip().upper()
    contract = validate_required_data_expected_fetch_contract(
        expected_fetch_contract,
        expected_symbol=symbol_norm,
    )
    now_value = _parse_datetime(now or datetime.now(timezone.utc))
    if mode_norm in {"fresh", "success_empty"}:
        if not isinstance(payload, dict):
            raise SourceReceiptError(
                "fresh required-data finalization lacks provider payload"
            )
        candidate_meta, _rows, _outcome, _contract = (
            _validate_required_data_payload_candidate(
                raw_payload=payload,
                expected_fetch_contract=contract,
            )
        )
        _validate_payload_freshness_reason_coded(
            meta=candidate_meta,
            now=now_value,
        )
        raw_path, csv_path = save_outputs(
            Path(base),
            symbol_norm,
            payload,
            output_root=root,
            publish_cache_metadata=False,
        )
    else:
        if payload is not None:
            raise SourceReceiptError(
                "cached/subprocess finalization must not resave provider payload"
            )
        raw_path = root / "raw" / f"{symbol_norm}_required_data.json"
        csv_path = root / "parsed" / f"{symbol_norm}_required_data.csv"

    validate_required_data_quote_candidate(
        producer_root=root,
        raw_path=raw_path,
        csv_path=csv_path,
        expected_fetch_contract=contract,
        now=now,
        require_fresh=True,
    )

    from src.application.multiplier_steps import (
        apply_multiplier_cache_to_required_data_csv,
    )

    apply_multiplier_cache_to_required_data_csv(
        base=Path(base),
        required_data_dir=root,
        symbol=symbol_norm,
    )
    raw_payload = json.loads(raw_path.read_text(encoding="utf-8"))
    raw_meta = raw_payload.get("meta") if isinstance(raw_payload, dict) else None
    if not isinstance(raw_meta, Mapping):
        raise SourceReceiptError("required-data payload metadata is invalid")
    _publish_final_quote_cache_metadata(
        csv_path=csv_path,
        symbol=symbol_norm,
        meta=raw_meta,
        producer_run_id=producer_run_id,
        preserve_existing_run_id=mode_norm in {"cached", "subprocess"},
    )
    validate_required_data_quote_candidate(
        producer_root=root,
        raw_path=raw_path,
        csv_path=csv_path,
        expected_fetch_contract=contract,
        now=now,
        require_fresh=True,
    )
    source_observed_at = str(raw_meta.get("source_observed_at") or "").strip()
    completed_at = str(raw_meta.get("completed_at_utc") or "").strip()
    result: dict[str, Any] = {
        "mode": mode_norm,
        "raw_path": raw_path,
        "csv_path": csv_path,
        "source_observed_at": source_observed_at,
        "completed_at": completed_at,
        "expected_fetch_contract": contract,
        "quote_receipt_path": None,
        "quote_receipt": None,
        "evidence": None,
    }
    run_id = str(producer_run_id or "").strip()
    if not run_id:
        return result
    receipt_path, receipt = publish_required_data_quote_snapshot(
        runtime_root=Path(base),
        producer_root=root,
        producer_run_id=run_id,
        symbol=symbol_norm,
        raw_path=raw_path,
        csv_path=csv_path,
        fetch_plan=dict(contract["fetch_plan"]),
        fetch_policy=dict(fetch_policy or {}),
        expected_fetch_contract=contract,
        source_observed_at=source_observed_at,
        completed_at=completed_at,
        now=now,
    )
    evidence = resolve_exact_fresh_required_data_quote_receipt(
        runtime_root=Path(base),
        producer_root=root,
        symbol=symbol_norm,
        expected_producer_run_id=run_id,
        expected_fetch_contract=contract,
        expected_source_observed_at=source_observed_at,
        expected_completed_at=completed_at,
        now=now,
    )
    expected_relpath = receipt_path.resolve().relative_to(root).as_posix()
    if evidence is None or evidence.get("receipt_relpath") != expected_relpath:
        raise SourceReceiptError(
            "finalized quote receipt does not bind current required-data bytes"
        )
    result.update(
        {
            "quote_receipt_path": receipt_path,
            "quote_receipt": receipt,
            "evidence": evidence,
        }
    )
    return result


def publish_required_data_quote_snapshot(
    *,
    runtime_root: Path | None = None,
    producer_root: Path,
    producer_run_id: str,
    symbol: str,
    raw_path: Path,
    csv_path: Path,
    fetch_plan: dict[str, Any],
    fetch_policy: dict[str, Any],
    expected_fetch_contract: Mapping[str, Any],
    source_observed_at: datetime | str,
    completed_at: datetime | str | None = None,
    now: datetime | str | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Bind the exact required-data JSON/CSV bytes and fetch policy immutably."""

    root_input = Path(producer_root)
    if (
        not root_input.exists()
        or not root_input.is_dir()
        or root_input.is_symlink()
    ):
        raise SourceReceiptError("quote producer root is invalid")
    root = root_input.resolve()
    raw_input = Path(raw_path).absolute()
    csv_input = Path(csv_path).absolute()
    try:
        raw_relpath = raw_input.relative_to(root).as_posix()
        csv_relpath = csv_input.relative_to(root).as_posix()
    except ValueError as exc:
        raise SourceReceiptError(
            "required-data quote files escape producer root"
        ) from exc
    raw = safe_existing_relative_path(root, raw_relpath)
    csv = safe_existing_relative_path(root, csv_relpath)
    symbol_norm = str(symbol or "").strip().upper()
    run_id = str(producer_run_id or "").strip()
    market = str(symbol_market(symbol_norm) or "").strip().upper()
    if not symbol_norm or not run_id or market not in {"US", "HK"}:
        raise SourceReceiptError(
            "quote producer run, symbol, or market is unavailable"
        )
    policy_input = dict(fetch_policy or {})
    contract = validate_required_data_expected_fetch_contract(
        expected_fetch_contract,
        expected_symbol=symbol_norm,
    )
    if dict(fetch_plan or {}) != dict(contract.get("fetch_plan") or {}):
        raise SourceReceiptError(
            "required-data fetch plan contradicts expected contract"
        )
    _validate_required_data_quote_candidate(
        raw=raw,
        csv=csv,
        expected_fetch_contract=contract,
    )
    raw_bytes = raw.read_bytes()
    csv_bytes = csv.read_bytes()
    raw_payload = json.loads(raw_bytes.decode("utf-8"))
    raw_meta = raw_payload.get("meta") if isinstance(raw_payload, dict) else {}
    raw_meta = raw_meta if isinstance(raw_meta, Mapping) else {}
    _validate_quote_cache_metadata_binding(
        csv=csv,
        symbol=symbol_norm,
        raw_meta=raw_meta,
    )
    if not _same_datetime(raw_meta.get("source_observed_at"), source_observed_at):
        raise SourceReceiptError(
            "required-data source observation timestamp mismatch"
        )
    effective_completed_at = completed_at or raw_meta.get("completed_at_utc")
    if not _same_datetime(raw_meta.get("completed_at_utc"), effective_completed_at):
        raise SourceReceiptError(
            "required-data completion timestamp mismatch"
        )
    now_value = _validate_payload_freshness_reason_coded(
        meta=raw_meta,
        now=now,
    )
    _validate_fetch_policy_binding(
        fetch_policy=policy_input,
        expected_fetch_contract=contract,
    )
    policy_payload = {
        "schema": "required_data_fetch_policy.v2",
        "expected_fetch_contract": contract,
        "fetch_policy": policy_input,
    }
    policy_hash = canonical_sha256(policy_payload)
    bundle: dict[str, Any] = {
        "schema_version": REQUIRED_DATA_QUOTE_SNAPSHOT_SCHEMA,
        "symbol": symbol_norm,
        "market": market,
        "fetch_plan": dict(contract["fetch_plan"]),
        "expected_fetch_contract": contract,
        "expected_fetch_contract_sha256": contract["contract_sha256"],
        "fetch_policy": policy_payload["fetch_policy"],
        "fetch_policy_hash": policy_hash,
        "raw_json_relpath": raw_relpath,
        "required_data_csv_relpath": csv_relpath,
    }
    if runtime_root is None:
        bundle.update(
            {
                "raw_json_base64": base64.b64encode(raw_bytes).decode("ascii"),
                "required_data_csv_base64": base64.b64encode(csv_bytes).decode(
                    "ascii"
                ),
            }
        )
    else:
        try:
            bundle["scan_blob_ref"] = publish_required_data_scan_blob(
                runtime_root=Path(runtime_root),
                symbol=symbol_norm,
                market=market,
                raw_json_bytes=raw_bytes,
                required_data_csv_bytes=csv_bytes,
                columns=REQUIRED_DATA_COLUMNS,
            )
        except RequiredDataBlobError as exc:
            raise SourceReceiptError(
                "required-data canonical blob publication failed"
            ) from exc
    bundle_bytes = (
        json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    bundle_hash = canonical_sha256(bundle)
    run_key = canonical_sha256({"producer_run_id": run_id})
    symbol_key = canonical_sha256({"symbol": symbol_norm})
    source_native_id = f"opend-required-data:{symbol_norm}:{bundle_hash}"
    snapshot_key = source_snapshot_id(
        source_kind="quotes",
        source_native_id=source_native_id,
        source_observed_at=source_observed_at,
        payload_sha256=sha256_bytes(bundle_bytes),
        producer_policy_hash=policy_hash,
    )
    prefix = (
        f"source_receipts/quotes/{run_key}/{symbol_key}/{snapshot_key}"
    )
    committed_paths = _same_run_symbol_receipt_paths(
        root=root,
        producer_run_id=run_id,
        symbol=symbol_norm,
    )
    existing = (
        resolve_exact_fresh_required_data_quote_receipt(
            runtime_root=runtime_root,
            producer_root=root,
            symbol=symbol_norm,
            expected_producer_run_id=run_id,
            expected_fetch_contract=contract,
            expected_source_observed_at=source_observed_at,
            expected_completed_at=effective_completed_at,
            now=now_value,
        )
        if committed_paths
        else None
    )
    if existing is not None and len(committed_paths) == 1:
        existing_path = safe_existing_relative_path(
            root,
            str(existing["receipt_relpath"]),
        )
        try:
            existing_receipt = json.loads(
                existing_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceReceiptError(
                "existing required-data quote receipt is unreadable"
            ) from exc
        return existing_path, existing_receipt
    if committed_paths:
        raise SourceReceiptError(
            "required-data quote receipt conflicts with committed run observation"
        )
    _validate_payload_freshness_reason_coded(
        meta=raw_meta,
        now=(now if now is not None else datetime.now(timezone.utc)),
    )

    def _validate_before_receipt_commit(_receipt: Mapping[str, Any]) -> None:
        _validate_payload_freshness_reason_coded(
            meta=raw_meta,
            now=(now if now is not None else datetime.now(timezone.utc)),
        )

    receipt = publish_source_receipt(
        producer_root=root,
        receipt_relpath=f"{prefix}/receipt.json",
        payload_relpath=f"{prefix}/payload.json",
        payload_bytes=bundle_bytes,
        source_kind="quotes",
        producer_schema_version=REQUIRED_DATA_QUOTE_SNAPSHOT_SCHEMA,
        producer_run_id=run_id,
        broker="futu",
        included_markets=[market],
        source_native_id=source_native_id,
        source_observed_at=source_observed_at,
        completed_at=effective_completed_at,
        producer_policy_hash=policy_hash,
        before_receipt_commit=_validate_before_receipt_commit,
    )
    return root / f"{prefix}/receipt.json", receipt


def _same_run_symbol_receipt_paths(
    *,
    root: Path,
    producer_run_id: str,
    symbol: str,
) -> list[Path]:
    run_key = canonical_sha256({"producer_run_id": producer_run_id})
    symbol_key = canonical_sha256({"symbol": symbol})
    receipt_root = (
        root
        / "source_receipts"
        / "quotes"
        / run_key
        / symbol_key
    )
    if not receipt_root.exists():
        return []
    return sorted(receipt_root.glob("*/receipt.json"))


def find_fresh_required_data_quote_receipts(
    *,
    producer_root: Path,
    symbols: list[str],
    now: datetime | str | None = None,
) -> dict[str, str]:
    """Discover the newest valid immutable receipt per symbol without refreshing it."""

    root_input = Path(producer_root)
    if (
        not root_input.exists()
        or not root_input.is_dir()
        or root_input.is_symlink()
    ):
        return {}
    root = root_input.resolve()
    receipt_root = root / "source_receipts" / "quotes"
    if not receipt_root.exists():
        return {}
    expected = {str(symbol or "").strip().upper() for symbol in symbols}
    expected.discard("")
    found: dict[str, tuple[datetime, str]] = {}
    now_value = now or datetime.now(timezone.utc)
    for receipt_path in receipt_root.glob("*/*/*/receipt.json"):
        try:
            receipt_relpath = receipt_path.relative_to(root).as_posix()
            validated_receipt_path = safe_existing_relative_path(
                root,
                receipt_relpath,
            )
            receipt = json.loads(
                validated_receipt_path.read_text(encoding="utf-8")
            )
            validated = validate_source_receipt(
                receipt,
                producer_root=root,
                now=now_value,
                expected_source_kind="quotes",
            )
            native_id = str(receipt.get("source_native_id") or "")
            if not native_id.startswith("opend-required-data:"):
                continue
            symbol = native_id.split(":", 2)[1].strip().upper()
            if symbol not in expected:
                continue
            observed = datetime.fromisoformat(
                str(validated["source_observed_at"]).replace("Z", "+00:00")
            )
            current = found.get(symbol)
            if current is None or observed > current[0]:
                found[symbol] = (
                    observed,
                    receipt_relpath,
                )
        except (
            OSError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
            SourceReceiptError,
        ):
            continue
    return {symbol: item[1] for symbol, item in sorted(found.items())}


def resolve_exact_fresh_required_data_quote_receipt(
    *,
    runtime_root: Path | None = None,
    producer_root: Path,
    symbol: str,
    now: datetime | str | None = None,
    expected_producer_run_id: str | None = None,
    expected_fetch_contract: Mapping[str, Any] | None = None,
    expected_source_observed_at: datetime | str | None = None,
    expected_completed_at: datetime | str | None = None,
) -> dict[str, Any] | None:
    """Return a fresh receipt only when it binds the exact current scan bytes."""

    root_input = Path(producer_root)
    if (
        not root_input.exists()
        or not root_input.is_dir()
        or root_input.is_symlink()
    ):
        return None
    root = root_input.resolve()
    symbol_norm = str(symbol or "").strip().upper()
    if not symbol_norm:
        return None
    receipt_root = root / "source_receipts" / "quotes"
    raw_path = root / "raw" / f"{symbol_norm}_required_data.json"
    csv_path = root / "parsed" / f"{symbol_norm}_required_data.csv"
    if not receipt_root.exists():
        return None

    now_value = now or datetime.now(timezone.utc)
    expected_contract = (
        validate_required_data_expected_fetch_contract(
            expected_fetch_contract,
            expected_symbol=symbol_norm,
        )
        if expected_fetch_contract is not None
        else None
    )
    matches: list[tuple[datetime, dict[str, Any]]] = []
    for receipt_path in receipt_root.glob("*/*/*/receipt.json"):
        try:
            receipt_relpath = receipt_path.relative_to(root).as_posix()
            validated_receipt_path = safe_existing_relative_path(
                root,
                receipt_relpath,
            )
            receipt_bytes = validated_receipt_path.read_bytes()
            receipt = json.loads(receipt_bytes.decode("utf-8"))
            validated = validate_source_receipt(
                receipt,
                producer_root=root,
                now=now_value,
                expected_source_kind="quotes",
            )
            if (
                expected_producer_run_id is not None
                and str(validated.get("producer_run_id") or "").strip()
                != str(expected_producer_run_id).strip()
            ):
                continue
            if (
                expected_source_observed_at is not None
                and not _same_datetime(
                    validated.get("source_observed_at"),
                    expected_source_observed_at,
                )
            ):
                continue
            if (
                expected_completed_at is not None
                and not _same_datetime(
                    receipt.get("completed_at"),
                    expected_completed_at,
                )
            ):
                continue
            payload = json.loads(validated["payload_bytes"])
            if (
                not isinstance(payload, dict)
                or payload.get("schema_version")
                != REQUIRED_DATA_QUOTE_SNAPSHOT_SCHEMA
                or str(payload.get("symbol") or "").strip().upper()
                != symbol_norm
            ):
                continue
            contract_payload = payload.get("expected_fetch_contract")
            if not isinstance(contract_payload, Mapping):
                if expected_contract is not None:
                    continue
                contract = None
            else:
                contract = validate_required_data_expected_fetch_contract(
                    contract_payload,
                    expected_symbol=symbol_norm,
                )
            if expected_contract is not None and contract != expected_contract:
                continue
            policy_payload = {
                "schema": "required_data_fetch_policy.v2",
                "expected_fetch_contract": contract,
                "fetch_policy": dict(payload.get("fetch_policy") or {}),
            }
            if contract is not None:
                policy_hash = canonical_sha256(policy_payload)
                if (
                    str(payload.get("fetch_policy_hash") or "")
                    != policy_hash
                    or str(receipt.get("producer_policy_hash") or "")
                    != policy_hash
                ):
                    continue
            scan_blob_ref = payload.get("scan_blob_ref")
            read_source = "legacy_snapshot"
            legacy_shadow_match: bool | None = None
            if scan_blob_ref is not None:
                if runtime_root is None or not isinstance(scan_blob_ref, Mapping):
                    return None
                try:
                    loaded = load_required_data_scan_blob(
                        runtime_root=Path(runtime_root),
                        blob_ref=scan_blob_ref,
                    )
                except RequiredDataBlobError:
                    return None
                raw_bytes = loaded["raw_json_bytes"]
                csv_bytes = loaded["required_data_csv_bytes"]
                read_source = "canonical_blob"
                legacy_shadow_match = None
                inline_pairs = (
                    ("raw_json_base64", raw_bytes),
                    ("required_data_csv_base64", csv_bytes),
                )
                for field, expected_bytes in inline_pairs:
                    if field not in payload:
                        continue
                    if not required_data_shadow_base64_matches(
                        payload.get(field),
                        expected_bytes,
                    ):
                        return None
                    legacy_shadow_match = True
                for legacy_path, expected_bytes in (
                    (raw_path, raw_bytes),
                    (csv_path, csv_bytes),
                ):
                    if legacy_path.exists() or legacy_path.is_symlink():
                        if not required_data_shadow_file_matches(
                            legacy_path,
                            expected_bytes,
                        ):
                            return None
                        legacy_shadow_match = True
            else:
                captured_raw = base64.b64decode(
                    str(payload.get("raw_json_base64") or ""),
                    validate=True,
                )
                captured_csv = base64.b64decode(
                    str(payload.get("required_data_csv_base64") or ""),
                    validate=True,
                )
                if (
                    not raw_path.is_file()
                    or raw_path.is_symlink()
                    or not csv_path.is_file()
                    or csv_path.is_symlink()
                ):
                    continue
                try:
                    raw_bytes = raw_path.read_bytes()
                    csv_bytes = csv_path.read_bytes()
                except OSError:
                    continue
                if captured_raw != raw_bytes or captured_csv != csv_bytes:
                    continue
            observed = datetime.fromisoformat(
                str(validated["source_observed_at"]).replace("Z", "+00:00")
            )
            matches.append(
                (
                    observed,
                    {
                        "receipt_relpath": receipt_relpath,
                        "receipt_hash": sha256_bytes(receipt_bytes),
                        "snapshot_id": validated["snapshot_id"],
                        "payload_sha256": validated["payload_sha256"],
                        "source_observed_at": validated["source_observed_at"],
                        "completed_at": receipt.get("completed_at"),
                        "expires_at": validated["expires_at"],
                        "expected_fetch_contract_sha256": (
                            str(contract.get("contract_sha256"))
                            if contract is not None
                            else None
                        ),
                        "scan_blob_ref": (
                            dict(scan_blob_ref)
                            if isinstance(scan_blob_ref, Mapping)
                            else None
                        ),
                        "read_source": read_source,
                        "legacy_shadow_match": legacy_shadow_match,
                    },
                )
            )
        except (
            OSError,
            ValueError,
            TypeError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            binascii.Error,
            SourceReceiptError,
        ):
            continue
    if not matches:
        return None
    matches.sort(key=lambda item: (item[0], item[1]["snapshot_id"]))
    return matches[-1][1]


def _same_datetime(left: Any, right: Any) -> bool:
    try:
        left_dt = (
            left
            if isinstance(left, datetime)
            else datetime.fromisoformat(str(left).replace("Z", "+00:00"))
        )
        right_dt = (
            right
            if isinstance(right, datetime)
            else datetime.fromisoformat(str(right).replace("Z", "+00:00"))
        )
        if left_dt.tzinfo is None:
            left_dt = left_dt.replace(tzinfo=timezone.utc)
        if right_dt.tzinfo is None:
            right_dt = right_dt.replace(tzinfo=timezone.utc)
        return left_dt.astimezone(timezone.utc) == right_dt.astimezone(
            timezone.utc
        )
    except (TypeError, ValueError):
        return False


__all__ = [
    "REQUIRED_DATA_COLUMNS",
    "REQUIRED_DATA_QUOTE_SNAPSHOT_SCHEMA",
    "SUCCESS_EMPTY_REASON_CODES",
    "append_metrics_json",
    "find_fresh_required_data_quote_receipts",
    "finalize_required_data_quote_candidate",
    "finalize_unplanned_required_data_candidate",
    "publish_required_data_quote_snapshot",
    "resolve_exact_fresh_required_data_quote_receipt",
    "save_outputs",
    "validate_required_data_payload_candidate",
    "validate_required_data_quote_bytes",
    "validate_required_data_quote_candidate",
    "validate_required_data_source_outcome",
]
