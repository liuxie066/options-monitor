from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
from pathlib import Path

from src.application.opend_symbol_fetching import FetchSymbolRequest, fetch_symbol_request
from src.application.opend_symbol_outputs import save_outputs
from src.application.opend_fetch_config import filter_opend_fetch_kwargs
from src.application.expiration_normalization import normalize_expiration_ymd
from src.application.required_data_planning import RequiredDataFetchSpec
from src.application.required_data_plan_identity import (
    required_data_request_sha256,
)


@dataclass(frozen=True)
class RequiredDataFetchRequest:
    symbol: str
    limit_expirations: int
    host: str = "127.0.0.1"
    port: int = 11111
    spot_override: float | None = None
    underlier_observation: dict[str, object] | None = None
    fetch_spot_if_missing: bool = True
    output_root: Path | None = None
    option_types: str = "put,call"
    min_strike: float | None = None
    max_strike: float | None = None
    side_strike_windows: dict[str, dict[str, float | None]] | None = None
    min_dte: int | None = None
    max_dte: int | None = None
    explicit_expirations: list[str] | None = None
    chain_cache: bool = True
    chain_cache_force_refresh: bool = False
    freshness_policy: str = "cache_first"
    max_wait_sec: float = 90.0
    option_chain_window_sec: float = 30.0
    option_chain_max_calls: int = 10
    snapshot_max_wait_sec: float = 30.0
    snapshot_window_sec: float = 30.0
    snapshot_max_calls: int = 60
    expiration_max_wait_sec: float = 30.0
    expiration_window_sec: float = 30.0
    expiration_max_calls: int = 60
    include_realized_volatility: bool = False
    no_retry: bool = False
    trading_date: str | None = None


def execute_required_data_opend(*, base: Path, request: RequiredDataFetchRequest) -> dict[str, object]:
    explicit_expirations = sorted({
        exp
        for exp in (normalize_expiration_ymd(x) for x in (request.explicit_expirations or []))
        if exp
    }) or None
    return fetch_symbol_request(
        FetchSymbolRequest(
            symbol=request.symbol,
            limit_expirations=int(request.limit_expirations),
            host=str(request.host),
            port=int(request.port),
            spot_override=request.spot_override,
            underlier_observation=(
                dict(request.underlier_observation)
                if request.underlier_observation is not None
                else None
            ),
            fetch_spot_if_missing=request.fetch_spot_if_missing,
            base_dir=Path(base),
            chain_cache=bool(request.chain_cache),
            chain_cache_force_refresh=bool(request.chain_cache_force_refresh),
            option_types=str(request.option_types),
            min_strike=request.min_strike,
            max_strike=request.max_strike,
            side_strike_windows=request.side_strike_windows,
            min_dte=request.min_dte,
            max_dte=request.max_dte,
            explicit_expirations=explicit_expirations,
            freshness_policy=str(request.freshness_policy or "cache_first"),
            max_wait_sec=float(request.max_wait_sec),
            option_chain_window_sec=float(request.option_chain_window_sec),
            option_chain_max_calls=int(request.option_chain_max_calls),
            snapshot_max_wait_sec=float(request.snapshot_max_wait_sec),
            snapshot_window_sec=float(request.snapshot_window_sec),
            snapshot_max_calls=int(request.snapshot_max_calls),
            expiration_max_wait_sec=float(request.expiration_max_wait_sec),
            expiration_window_sec=float(request.expiration_window_sec),
            expiration_max_calls=int(request.expiration_max_calls),
            include_realized_volatility=bool(request.include_realized_volatility),
            no_retry=bool(request.no_retry),
            trading_date=request.trading_date,
        )
    )


def fetch_required_data_opend(*, base: Path, request: RequiredDataFetchRequest) -> tuple[Path, Path]:
    payload = execute_required_data_opend(base=base, request=request)
    return save_outputs(
        Path(base),
        str(request.symbol),
        payload,
        output_root=(Path(request.output_root) if request.output_root is not None else None),
    )


def build_fetch_request_from_spec(
    *,
    spec: RequiredDataFetchSpec,
    output_root: Path | None = None,
    chain_cache: bool = True,
    chain_cache_force_refresh: bool = False,
    opend_fetch_config: dict[str, float | int] | None = None,
    spot_override: float | None = None,
    underlier_observation: dict[str, object] | None = None,
    fetch_spot_if_missing: bool = True,
) -> RequiredDataFetchRequest:
    if not spec.explicit_expirations:
        raise RuntimeError(
            "required-data executable fetch spec lacks expiration targets"
        )
    if spec.trading_date is not None:
        if (
            not isinstance(spec.trading_date, str)
            or not spec.trading_date
            or spec.trading_date != spec.trading_date.strip()
        ):
            raise RuntimeError(
                "required-data executable fetch spec has invalid trading date"
            )
        try:
            parsed_trading_date = date.fromisoformat(spec.trading_date)
        except ValueError as exc:
            raise RuntimeError(
                "required-data executable fetch spec has invalid trading date"
            ) from exc
        if parsed_trading_date.isoformat() != spec.trading_date:
            raise RuntimeError(
                "required-data executable fetch spec has invalid trading date"
            )
    if not isinstance(spec.include_realized_volatility, bool):
        raise RuntimeError(
            "required-data executable fetch spec has invalid RV authority"
        )
    kwargs = filter_opend_fetch_kwargs(opend_fetch_config)
    return RequiredDataFetchRequest(
        symbol=spec.symbol,
        limit_expirations=int(spec.limit_expirations),
        host=str(spec.host),
        port=int(spec.port),
        spot_override=spot_override,
        underlier_observation=(
            dict(underlier_observation)
            if underlier_observation is not None
            else None
        ),
        fetch_spot_if_missing=fetch_spot_if_missing,
        output_root=output_root,
        option_types=",".join(spec.option_types),
        side_strike_windows={k: dict(v) for k, v in spec.side_strike_windows.items()},
        min_dte=(int(spec.min_dte) if spec.min_dte is not None else None),
        max_dte=(int(spec.max_dte) if spec.max_dte is not None else None),
        explicit_expirations=list(spec.explicit_expirations),
        chain_cache=bool(chain_cache),
        chain_cache_force_refresh=bool(chain_cache_force_refresh),
        freshness_policy=("force_refresh" if chain_cache_force_refresh else "cache_first"),
        include_realized_volatility=spec.include_realized_volatility,
        trading_date=spec.trading_date,
        **kwargs,
    )


def merge_required_data_payloads(*, symbol: str, payloads: list[dict[str, object]]) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    seen_rows: dict[str, str] = {}
    reconciled_overlaps: set[str] = set()
    meta_items: list[dict[str, object]] = []
    expirations: set[str] = set()
    spot: float | None = None
    underlier_code: str | None = None
    underlier_observation: dict[str, object] | None = None
    for payload_index, payload in enumerate(payloads):
        if not isinstance(payload, dict):
            continue
        if spot is None:
            try:
                spot = float(payload.get("spot")) if payload.get("spot") is not None else None
            except Exception:
                spot = spot
        if underlier_code is None and payload.get("underlier_code"):
            underlier_code = str(payload.get("underlier_code"))
        for exp in payload.get("expirations") or []:
            if exp:
                expirations.add(str(exp))
        meta = payload.get("meta")
        if isinstance(meta, dict):
            meta_items.append(meta)
            child_underlier_observation = meta.get("underlier_observation")
            if child_underlier_observation is not None:
                if not isinstance(child_underlier_observation, dict):
                    raise RuntimeError(
                        "required-data child underlier observation is invalid"
                    )
                normalized_observation = dict(child_underlier_observation)
                if underlier_observation is None:
                    underlier_observation = normalized_observation
                elif underlier_observation != normalized_observation:
                    raise RuntimeError(
                        "required-data child underlier observations conflict"
                    )
        child_contracts: set[str] = set()
        for row in payload.get("rows") or []:
            if not isinstance(row, dict):
                continue
            contract_identity = _required_data_contract_identity(row)
            if contract_identity in child_contracts:
                raise RuntimeError(
                    "required-data child contains duplicate contract identity "
                    f"at request index {payload_index}: {contract_identity}"
                )
            child_contracts.add(contract_identity)
            canonical_row = _canonical_required_data_row(row)
            existing_row = seen_rows.get(contract_identity)
            if existing_row is not None:
                if existing_row != canonical_row:
                    raise RuntimeError(
                        "required-data child requests contain conflicting "
                        f"contract overlap: {contract_identity}"
                    )
                reconciled_overlaps.add(contract_identity)
                continue
            seen_rows[contract_identity] = canonical_row
            rows.append(dict(row))
    return {
        "symbol": symbol,
        "underlier_code": underlier_code,
        "spot": spot,
        "expiration_count": len(expirations),
        "expirations": sorted(expirations),
        "rows": rows,
        "meta": {
            "source": "opend",
            "request_count": len(payloads),
            "requests": meta_items,
            "underlier_observation": underlier_observation,
            "reconciled_contract_overlap_count": len(reconciled_overlaps),
            "reconciled_contract_overlaps": sorted(reconciled_overlaps),
        },
    }


def _required_data_contract_identity(row: Mapping[str, object]) -> str:
    identity = str(row.get("contract_symbol") or "").strip().upper()
    if not identity:
        raise RuntimeError("required-data row contract identity is missing")
    return identity


def _canonical_required_data_row(row: Mapping[str, object]) -> str:
    try:
        return json.dumps(
            dict(row),
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "required-data contract row cannot be canonicalized"
        ) from exc


_CHILD_REQUEST_EVIDENCE_FIELDS = frozenset(
    {
        "request_index",
        "planned_request_sha256",
        "request_symbol",
        "request_underlier_code",
    }
)


def bind_required_data_child_request_evidence(
    *,
    payload: Mapping[str, object],
    planned_request: Mapping[str, object],
    request_index: int,
) -> dict[str, object]:
    """Bind one actual provider result to the exact request just invoked.

    Provider-owned status, outcome, binding, and timestamps are preserved. The
    coordinator contributes only the ordered request identity; symbol and
    underlier evidence are copied from the actual provider payload.
    """

    if isinstance(request_index, bool) or int(request_index) < 0:
        raise RuntimeError("required-data child request index is invalid")
    if not isinstance(payload, Mapping):
        raise RuntimeError("required-data child payload is invalid")
    raw_meta = payload.get("meta")
    if not isinstance(raw_meta, Mapping):
        raise RuntimeError("required-data child provider evidence is missing")
    conflicts = sorted(_CHILD_REQUEST_EVIDENCE_FIELDS.intersection(raw_meta))
    if conflicts:
        raise RuntimeError(
            "required-data child provider evidence uses reserved fields: "
            + ", ".join(conflicts)
        )
    symbol = str(payload.get("symbol") or "").strip().upper()
    underlier_code = str(payload.get("underlier_code") or "").strip()
    if not symbol:
        raise RuntimeError("required-data child payload symbol is missing")
    if not underlier_code:
        raise RuntimeError("required-data child payload underlier is missing")

    bound_meta = dict(raw_meta)
    bound_meta.update(
        {
            "request_index": int(request_index),
            "planned_request_sha256": required_data_request_sha256(
                planned_request
            ),
            "request_symbol": symbol,
            "request_underlier_code": underlier_code,
        }
    )
    bound_payload = dict(payload)
    bound_payload["meta"] = bound_meta
    return bound_payload


def _parse_required_data_evidence_time(
    value: object,
    *,
    field: str,
) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"required-data {field} is missing or invalid") from exc
    if parsed.tzinfo is None:
        raise RuntimeError(f"required-data {field} must include timezone")
    return parsed.astimezone(timezone.utc)


def bind_merged_payload_evidence(
    *,
    merged_payload: dict[str, object],
    payloads: list[dict[str, object]],
) -> None:
    """Preserve only evidence common to every provider result in the merged raw."""

    request_meta = [
        dict(payload.get("meta"))
        for payload in payloads
        if isinstance(payload, dict) and isinstance(payload.get("meta"), dict)
    ]
    meta = merged_payload.get("meta")
    aggregate = dict(meta) if isinstance(meta, dict) else {}
    rows = merged_payload.get("rows")
    has_rows = isinstance(rows, list) and bool(rows)
    statuses = [str(item.get("status") or "").strip().lower() for item in request_meta]
    rv_meta, rv_consistent = _merged_realized_volatility_meta(request_meta)
    all_ok = (
        bool(request_meta)
        and len(request_meta) == len(payloads)
        and all(status == "ok" for status in statuses)
        and all(
            _provider_payload_outcome_is_consistent(payload)
            for payload in payloads
        )
        and all(
            _provider_snapshot_evidence_is_consistent(payload)
            for payload in payloads
        )
        and rv_consistent
    )
    requested_codes = _merged_provider_code_set(
        request_meta,
        "snapshot_requested_code_set",
    )
    returned_codes = _merged_provider_code_set(
        request_meta,
        "snapshot_returned_code_set",
    )
    code_evidence_complete = all(
        isinstance(item.get(field), list)
        for item in request_meta
        for field in (
            "snapshot_requested_code_set",
            "snapshot_returned_code_set",
            "snapshot_missing_code_set",
            "snapshot_unexpected_code_set",
        )
    )
    missing_codes = requested_codes.difference(returned_codes)
    unexpected_codes = returned_codes.difference(requested_codes)
    aggregate.update(
        {
            "status": "ok" if all_ok else "error",
            "source": _common_provider_meta_value(request_meta, "source"),
            "host": _common_provider_meta_value(request_meta, "host"),
            "port": _common_provider_meta_value(request_meta, "port"),
            "trading_date": _common_provider_meta_value(
                request_meta,
                "trading_date",
            ),
            "source_outcome": (
                "success_rows"
                if has_rows and all_ok
                else _merged_empty_source_outcome(request_meta, all_ok=all_ok)
            ),
            "reason_code": (
                None
                if has_rows
                else _merged_empty_reason_code(request_meta, all_ok=all_ok)
            ),
            "snapshot_requested_code_set": sorted(requested_codes),
            "snapshot_returned_code_set": sorted(returned_codes),
            "snapshot_missing_code_set": sorted(missing_codes),
            "snapshot_unexpected_code_set": sorted(unexpected_codes),
            "snapshot_requested_codes": len(requested_codes),
            "snapshot_returned_codes": len(returned_codes),
            "snapshot_missing_codes": len(missing_codes),
            "snapshot_unexpected_codes": len(unexpected_codes),
            "snapshot_complete": not missing_codes
            and code_evidence_complete
            and bool(request_meta)
            and len(request_meta) == len(payloads)
            and all(item.get("snapshot_complete") is True for item in request_meta),
        }
    )
    if rv_meta is not None:
        aggregate["realized_volatility"] = rv_meta
    observed_at = _provider_meta_time(
        request_meta,
        "source_observed_at",
        latest=False,
    )
    completed_at = _provider_meta_time(
        request_meta,
        "completed_at_utc",
        latest=True,
    )
    if observed_at is not None:
        aggregate["source_observed_at"] = observed_at.isoformat()
    if completed_at is not None:
        aggregate["completed_at_utc"] = completed_at.isoformat()
    merged_payload["meta"] = aggregate


def _provider_payload_outcome_is_consistent(
    payload: dict[str, object],
) -> bool:
    rows = payload.get("rows")
    has_rows = isinstance(rows, list) and bool(rows)
    outcome = _payload_source_outcome(payload)
    reason = ""
    meta = payload.get("meta")
    if isinstance(meta, dict):
        reason = str(meta.get("reason_code") or "").strip().lower()
    if has_rows:
        return outcome == "success_rows" and not reason
    if outcome == "success_rows" and not reason:
        return _provider_proves_filtered_empty(meta)
    return (
        outcome == "success_empty"
        and reason in {"no_expirations", "no_contract_rows"}
    )


def _provider_proves_filtered_empty(meta: object) -> bool:
    if not isinstance(meta, dict):
        return False
    scope_evidence = meta.get("option_chain_scope_coverage")
    if (
        not isinstance(scope_evidence, dict)
        or scope_evidence.get("schema_version")
        != "option_chain_scope_coverage.v1"
    ):
        return False
    scopes = scope_evidence.get("scopes")
    if not isinstance(scopes, list) or not scopes:
        return False
    for scope in scopes:
        if not isinstance(scope, dict):
            return False
        codes = scope.get("filtered_contract_codes")
        if (
            scope.get("chain_status") not in {"cache", "fetched"}
            or codes != []
            or scope.get("filtered_contract_count") != 0
        ):
            return False
    return (
        meta.get("snapshot_requested_code_set") == []
        and meta.get("snapshot_returned_code_set") == []
        and meta.get("snapshot_missing_code_set") == []
        and meta.get("snapshot_unexpected_code_set") == []
        and meta.get("snapshot_complete") is True
    )


def _payload_source_outcome(payload: dict[str, object]) -> str:
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return ""
    return str(meta.get("source_outcome") or "").strip().lower()


def _provider_snapshot_evidence_is_consistent(
    payload: dict[str, object],
) -> bool:
    meta = payload.get("meta")
    rows = payload.get("rows")
    if not isinstance(meta, dict) or not isinstance(rows, list):
        return False
    sets: dict[str, set[str]] = {}
    for name in ("requested", "returned", "missing", "unexpected"):
        values = meta.get(f"snapshot_{name}_code_set")
        if not isinstance(values, list):
            return False
        normalized = [str(value or "").strip() for value in values]
        if (
            any(not value for value in normalized)
            or len(normalized) != len(set(normalized))
        ):
            return False
        sets[name] = set(normalized)
        try:
            if int(meta.get(f"snapshot_{name}_codes")) != len(normalized):
                return False
        except (TypeError, ValueError):
            return False
    if sets["missing"] != sets["requested"].difference(sets["returned"]):
        return False
    if sets["unexpected"] != sets["returned"].difference(sets["requested"]):
        return False
    if bool(meta.get("snapshot_complete")) != (not sets["missing"]):
        return False
    row_codes = {
        str(row.get("contract_symbol") or "").strip()
        for row in rows
        if isinstance(row, dict)
    }
    row_codes.discard("")
    return row_codes == sets["requested"]


def _merged_provider_code_set(
    request_meta: list[dict[str, object]],
    field: str,
) -> set[str]:
    merged: set[str] = set()
    for item in request_meta:
        values = item.get(field)
        if not isinstance(values, list):
            return set()
        normalized = [str(value or "").strip() for value in values]
        if any(not value for value in normalized):
            return set()
        merged.update(normalized)
    return merged


def _merged_realized_volatility_meta(
    request_meta: list[dict[str, object]],
) -> tuple[dict[str, object] | None, bool]:
    nonempty_items = [
        item
        for item in request_meta
        if isinstance(item.get("snapshot_requested_code_set"), list)
        and bool(item.get("snapshot_requested_code_set"))
    ]
    values = [
        item.get("realized_volatility")
        for item in (nonempty_items or request_meta)
    ]
    if not values:
        return None, True
    if any(not isinstance(value, dict) for value in values):
        return None, False
    rv_items = [dict(value) for value in values if isinstance(value, dict)]
    if len(rv_items) == 1:
        return rv_items[0], True
    if all(item == rv_items[0] for item in rv_items[1:]):
        return rv_items[0], True
    diagnostic_fields = (
        "realized_volatility_20",
        "realized_volatility_60",
        "realized_volatility_120",
        "realized_volatility_estimate",
        "estimation_policy",
    )
    if any(
        any(item.get(field) != rv_items[0].get(field) for field in diagnostic_fields)
        for item in rv_items[1:]
    ):
        return None, False
    merged_terms: dict[str, object] = {}
    for item in rv_items:
        terms = item.get("term_matched")
        if not isinstance(terms, dict):
            return None, False
        for expiration, observation in terms.items():
            existing = merged_terms.get(str(expiration))
            if existing is not None and existing != observation:
                return None, False
            merged_terms[str(expiration)] = observation
    history_items = [item.get("qfq_history") for item in rv_items]
    if any(not isinstance(item, dict) for item in history_items):
        return None, False
    history_core_fields = (
        "status",
        "market",
        "underlier_code",
        "autype",
        "cache_identity",
        "completed_before",
    )
    first_history = history_items[0]
    assert isinstance(first_history, dict)
    if any(
        any(item.get(field) != first_history.get(field) for field in history_core_fields)
        for item in history_items[1:]
        if isinstance(item, dict)
    ):
        return None, False
    calendar_items = [item.get("trading_calendar") for item in rv_items]
    if any(
        not isinstance(item, dict)
        or str(item.get("status") or "").strip().lower() != "ok"
        for item in calendar_items
    ):
        return None, False
    merged = dict(rv_items[0])
    merged["status"] = (
        "ok"
        if all(
            isinstance(item, dict)
            and str(item.get("status") or "").strip().lower() == "ok"
            for item in merged_terms.values()
        )
        else "partial"
    )
    merged["reason"] = (
        None
        if merged["status"] == "ok"
        else "term_matched_rv_incomplete"
    )
    merged["sample_count"] = max(
        int(item.get("sample_count") or 0) for item in rv_items
    )
    merged["term_matched"] = dict(sorted(merged_terms.items()))
    merged["qfq_history"] = {
        **{field: first_history.get(field) for field in history_core_fields},
        "cache_status": "merged_requests",
        "request_count": len(history_items),
        "requests": history_items,
        "revision_detected": any(
            bool(item.get("revision_detected"))
            for item in history_items
            if isinstance(item, dict)
        ),
    }
    merged["trading_calendar"] = {
        "status": "ok",
        "request_count": len(calendar_items),
        "requests": calendar_items,
    }
    return merged, True


def _common_provider_meta_value(
    request_meta: list[dict[str, object]],
    field: str,
) -> object:
    values = [item.get(field) for item in request_meta]
    if not values or any(value in (None, "") for value in values):
        return None
    first = values[0]
    if all(str(value) == str(first) for value in values[1:]):
        return first
    return None


def _provider_meta_time(
    request_meta: list[dict[str, object]],
    field: str,
    *,
    latest: bool,
) -> datetime | None:
    parsed: list[datetime] = []
    for item in request_meta:
        try:
            parsed.append(
                _parse_required_data_evidence_time(item.get(field), field=field)
            )
        except RuntimeError:
            return None
    if not parsed:
        return None
    return max(parsed) if latest else min(parsed)


def _merged_empty_source_outcome(
    request_meta: list[dict[str, object]],
    *,
    all_ok: bool,
) -> str:
    outcomes = {
        str(item.get("source_outcome") or "").strip().lower()
        for item in request_meta
    }
    if all_ok and outcomes == {"success_empty"}:
        return "success_empty"
    return "provider_error"


def _merged_empty_reason_code(
    request_meta: list[dict[str, object]],
    *,
    all_ok: bool,
) -> str | None:
    if not all_ok:
        return None
    reasons = {
        str(item.get("reason_code") or "").strip().lower()
        for item in request_meta
    }
    reasons.discard("")
    if len(reasons) == 1:
        return next(iter(reasons))
    if reasons and reasons <= {"no_expirations", "no_contract_rows"}:
        return "no_contract_rows"
    return None
