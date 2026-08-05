from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo


SETTLEMENT_SEMANTIC_SCHEMA = "settlement_observation_semantic.v1"
SETTLEMENT_ROW_NORMALIZER_SCHEMA = "settlement_source_rows.v1"


class SettlementSemanticUnavailable(ValueError):
    """Settlement evidence cannot be projected without guessing."""


class LegacySettlementSemanticUnavailable(
    SettlementSemanticUnavailable
):
    """The latest stored legacy settlement evidence cannot be compared."""


class SettlementAdmissionStateIncoherent(
    SettlementSemanticUnavailable
):
    """A duplicate semantic head does not match canonical lifecycle state."""


def canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def settlement_observation_semantic(
    observation: Mapping[str, Any],
    *,
    evidence_kind: str | None = None,
) -> dict[str, Any]:
    """Project one provider observation onto explicit business semantics.

    The allowlist deliberately excludes attempt time, raw errors, request ids,
    transport diagnostics, lifecycle generation, and mutable lifecycle output.
    """

    payload = dict(observation or {})
    case_id = _text(payload.get("case_id"))
    account = _lower(payload.get("account"))
    futu_account_id = _text(payload.get("futu_account_id"))
    market = _upper(payload.get("market"))
    deadline_ms = _required_int(
        payload.get("settlement_deadline_ms")
        or _legacy_settlement_deadline_ms(payload),
        field="settlement_deadline_ms",
    )
    observed_at_ms = _required_int(
        payload.get("observed_at_ms"),
        field="observed_at_ms",
    )
    if not case_id or not account or not futu_account_id or not market:
        raise SettlementSemanticUnavailable(
            "settlement semantic identity is incomplete"
        )

    contract = _contract_identity(payload.get("contract_identity"))
    targets = _contracts_by_lot(
        payload.get("target_contracts_by_lot"),
        field="target_contracts_by_lot",
    )
    frozen = _contracts_by_lot(
        payload.get("frozen_preterminal_remaining_by_lot"),
        field="frozen_preterminal_remaining_by_lot",
    )
    anchor_key = _text(payload.get("anchor_option_deal_key"))
    anchor_time_ms = _required_int(
        payload.get("anchor_execution_time_ms"),
        field="anchor_execution_time_ms",
    )
    if not anchor_key:
        raise SettlementSemanticUnavailable(
            "settlement semantic anchor is incomplete"
        )

    receipts = payload.get("source_receipts")
    if not isinstance(receipts, Mapping):
        raise SettlementSemanticUnavailable(
            "settlement source receipts are unavailable"
        )
    required_sources = sorted(
        {
            _text(item)
            for item in payload.get("required_sources") or ()
            if _text(item)
        }
    )
    if not required_sources:
        raise SettlementSemanticUnavailable(
            "settlement required sources are unavailable"
        )

    stock_candidates = _stock_candidates(
        payload.get("stock_settlement_candidates")
    )
    anchor_order_id = _anchor_order_id(receipts)
    source_semantics: dict[str, dict[str, Any]] = {}
    for source in required_sources:
        receipt = receipts.get(source)
        if not isinstance(receipt, Mapping):
            source_semantics[source] = {
                "status": "missing",
                "provider_code": None,
                "error_class": None,
                "query_scope": {},
                "coverage_complete": False,
                "pagination_complete": False,
                "stale": False,
                "fallback_cache": False,
                "row_normalizer_schema": SETTLEMENT_ROW_NORMALIZER_SCHEMA,
                "relevant_row_count": 0,
                "relevant_rows_hash": canonical_hash([]),
            }
            continue
        relevant_rows = _relevant_source_rows(
            source,
            receipt.get("rows"),
            anchor_key=anchor_key,
            anchor_order_id=anchor_order_id,
            contract_identity=contract,
            stock_candidates=stock_candidates,
        )
        source_semantics[source] = {
            "status": _lower(receipt.get("status")) or "incomplete",
            "provider_code": _upper(receipt.get("provider_code")) or None,
            "error_class": _lower(receipt.get("error_class")) or None,
            "query_scope": _query_scope(receipt.get("query_input")),
            "coverage_complete": bool(receipt.get("coverage_complete")),
            "pagination_complete": bool(receipt.get("pagination_complete")),
            "stale": bool(receipt.get("stale")),
            "fallback_cache": bool(receipt.get("fallback_cache")),
            "row_normalizer_schema": SETTLEMENT_ROW_NORMALIZER_SCHEMA,
            "relevant_row_count": len(relevant_rows),
            "relevant_rows_hash": canonical_hash(relevant_rows),
        }

    inferred_kind = (
        "expire_close"
        if bool(payload.get("complete"))
        else "settlement_observation"
    )
    kind = _lower(evidence_kind) or inferred_kind
    if kind not in {"settlement_observation", "expire_close"}:
        raise SettlementSemanticUnavailable(
            "settlement semantic evidence kind is invalid"
        )

    pairing_facts = {
        "anchor_option_deal_key": anchor_key,
        "anchor_execution_time_ms": anchor_time_ms,
        "contract_identity": contract,
        "target_contracts_by_lot": targets,
        "frozen_preterminal_remaining_by_lot": frozen,
    }
    return {
        "schema_version": SETTLEMENT_SEMANTIC_SCHEMA,
        "case_id": case_id,
        "account": account,
        "futu_account_id": futu_account_id,
        "market": market,
        "effective_pairing_facts_hash": canonical_hash(pairing_facts),
        "contract_identity": contract,
        "target_contracts_by_lot": targets,
        "frozen_preterminal_remaining_by_lot": frozen,
        "anchor_option_deal_key": anchor_key,
        "anchor_execution_time_ms": anchor_time_ms,
        "observation_start_ms": _optional_int(
            payload.get("observation_start_ms")
        ),
        "settlement_deadline_ms": deadline_ms,
        "observed_after_settlement_deadline": (
            observed_at_ms >= deadline_ms
        ),
        "calendar_hash": _text(payload.get("calendar_hash")),
        "query_window": _query_scope(payload.get("query_window")),
        "required_sources": required_sources,
        "sources": source_semantics,
        "stock_settlement_candidates": stock_candidates,
        "broker_option_position_absent": bool(
            payload.get("broker_option_position_absent")
        ),
        "projection_matches_frozen_remaining": bool(
            payload.get("projection_matches_frozen_remaining")
        ),
        "reservation_exclusive": bool(
            payload.get("reservation_exclusive")
        ),
        "competing_effective_consumption": bool(
            payload.get("competing_effective_consumption")
        ),
        "stock_settlement_present": bool(
            payload.get("stock_settlement_present")
        ),
        "normal_order_present": bool(
            payload.get("normal_order_present")
        ),
        "complete": bool(payload.get("complete")),
        "incomplete_reason_codes": sorted(
            {
                _text(item)
                for item in payload.get("incomplete_reason_codes") or ()
                if _text(item)
            }
        ),
        "evidence_kind": kind,
    }


def attach_settlement_semantics(
    observation: Mapping[str, Any],
    *,
    evidence_kind: str | None = None,
) -> dict[str, Any]:
    payload = dict(observation or {})
    semantic = settlement_observation_semantic(
        payload,
        evidence_kind=evidence_kind,
    )
    return {
        **payload,
        "semantic_schema": SETTLEMENT_SEMANTIC_SCHEMA,
        "semantic_fingerprint": canonical_hash(semantic),
        "semantic_projection": semantic,
    }


def settlement_evidence_id(
    *,
    case_id: str,
    semantic_fingerprint: str,
    expected_generation_token: str,
    previous_evidence_id: str | None,
) -> str:
    case_value = _text(case_id)
    fingerprint = _text(semantic_fingerprint)
    generation = _text(expected_generation_token)
    if not case_value or not fingerprint or not generation:
        raise SettlementSemanticUnavailable(
            "settlement evidence identity is incomplete"
        )
    return "observation_" + canonical_hash(
        {
            "schema_version": "settlement_evidence_id.v1",
            "case_id": case_value,
            "semantic_schema": SETTLEMENT_SEMANTIC_SCHEMA,
            "semantic_fingerprint": fingerprint,
            "expected_generation_token": generation,
            "previous_evidence_id": _text(previous_evidence_id) or None,
        }
    )


def settlement_semantic_from_evidence(
    evidence: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    payload = dict(evidence or {})
    observation = payload.get("observation")
    if not isinstance(observation, Mapping):
        raise SettlementSemanticUnavailable(
            "legacy settlement observation payload is unavailable"
        )
    semantic = settlement_observation_semantic(
        observation,
        evidence_kind=_lower(payload.get("evidence_type")) or None,
    )
    fingerprint = canonical_hash(semantic)
    _validate_semantic_metadata(
        payload,
        semantic=semantic,
        fingerprint=fingerprint,
        location="evidence",
    )
    _validate_semantic_metadata(
        observation,
        semantic=semantic,
        fingerprint=fingerprint,
        location="observation",
    )
    return semantic, fingerprint


def _validate_semantic_metadata(
    payload: Mapping[str, Any],
    *,
    semantic: Mapping[str, Any],
    fingerprint: str,
    location: str,
) -> None:
    metadata_keys = {
        "semantic_schema",
        "semantic_fingerprint",
        "semantic_projection",
    }
    if not any(key in payload for key in metadata_keys):
        return
    stored_schema = _text(payload.get("semantic_schema"))
    if stored_schema != SETTLEMENT_SEMANTIC_SCHEMA:
        raise SettlementSemanticUnavailable(
            f"unsupported settlement semantic schema in {location}"
        )
    stored_fingerprint = _text(payload.get("semantic_fingerprint"))
    if stored_fingerprint != fingerprint:
        raise SettlementSemanticUnavailable(
            f"settlement semantic fingerprint mismatch in {location}"
        )
    stored_projection = payload.get("semantic_projection")
    if not isinstance(stored_projection, Mapping):
        raise SettlementSemanticUnavailable(
            f"settlement semantic projection is unavailable in {location}"
        )
    try:
        projection_fingerprint = canonical_hash(dict(stored_projection))
        expected_fingerprint = canonical_hash(dict(semantic))
    except (TypeError, ValueError) as exc:
        raise SettlementSemanticUnavailable(
            f"settlement semantic projection is invalid in {location}"
        ) from exc
    if (
        projection_fingerprint != fingerprint
        or expected_fingerprint != fingerprint
    ):
        raise SettlementSemanticUnavailable(
            f"settlement semantic projection mismatch in {location}"
        )


def _contract_identity(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SettlementSemanticUnavailable(
            "settlement contract identity is unavailable"
        )
    result = {
        "symbol": _upper(value.get("symbol")),
        "option_contract_code": _upper(
            value.get("option_contract_code")
        ),
        "option_type": _lower(value.get("option_type")),
        "position_side": _lower(value.get("position_side")),
        "strike": _decimal(value.get("strike")),
        "expiration_ymd": _text(value.get("expiration_ymd")),
        "multiplier": _decimal(value.get("multiplier")),
    }
    if (
        not result["symbol"]
        or not result["option_type"]
        or not result["position_side"]
        or not result["strike"]
        or not result["expiration_ymd"]
    ):
        raise SettlementSemanticUnavailable(
            "settlement contract identity is incomplete"
        )
    return result


def _contracts_by_lot(value: Any, *, field: str) -> dict[str, int]:
    if not isinstance(value, Mapping) or not value:
        raise SettlementSemanticUnavailable(f"{field} is unavailable")
    output: dict[str, int] = {}
    for raw_key, raw_value in value.items():
        key = _text(raw_key)
        parsed = _optional_int(raw_value)
        if not key or parsed is None or parsed < 0:
            raise SettlementSemanticUnavailable(f"{field} is invalid")
        output[key] = parsed
    return {key: output[key] for key in sorted(output)}


def _query_scope(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    text_keys = (
        "market",
        "start",
        "end",
        "trd_env",
        "acc_id",
        "source_key",
        "case_id",
        "policy_schema",
    )
    output = {
        key: _text(value.get(key))
        for key in text_keys
        if value.get(key) not in (None, "")
    }
    if "refresh_cache" in value:
        output["refresh_cache"] = bool(value.get("refresh_cache"))
    clearing_dates = value.get("clearing_dates")
    if isinstance(clearing_dates, Iterable) and not isinstance(
        clearing_dates, (str, bytes, Mapping)
    ):
        output["clearing_dates"] = sorted(
            {_text(item) for item in clearing_dates if _text(item)}
        )
    return output


def _relevant_source_rows(
    source: str,
    rows: Any,
    *,
    anchor_key: str,
    anchor_order_id: str,
    contract_identity: Mapping[str, Any],
    stock_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    raw_rows = [
        dict(item)
        for item in rows or ()
        if isinstance(item, Mapping)
    ]
    anchor_deal_id = anchor_key.split(":", 3)[-1]
    stock_deal_ids = {
        _text(item.get("source_event_id")).split(":", 3)[-1]
        for item in stock_candidates
        if _text(item.get("source_event_id"))
    }
    normalized: list[dict[str, Any]] = []
    for row in raw_rows:
        item: dict[str, Any] | None
        if source == "anchor_option_close":
            item = _normalize_anchor(row)
        elif source == "history_deals":
            item = _normalize_deal(row)
            if item.get("deal_id") not in {
                anchor_deal_id,
                *stock_deal_ids,
            }:
                item = None
        elif source == "history_orders":
            item = _normalize_order(row)
            if not anchor_order_id or item.get("order_id") != anchor_order_id:
                item = None
        elif source == "fresh_positions":
            item = _normalize_position(row)
            if not _position_matches_contract(
                item,
                contract_identity=contract_identity,
            ):
                item = None
        elif source == "trading_calendar":
            item = _normalize_calendar(row)
        elif source == "contract_metadata":
            item = _normalize_contract_metadata(row)
        else:
            item = None
        if item is not None:
            normalized.append(item)
    return sorted(
        normalized,
        key=lambda item: json.dumps(
            item,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _position_matches_contract(
    position: Mapping[str, Any],
    *,
    contract_identity: Mapping[str, Any],
) -> bool:
    contract_code = _upper(
        contract_identity.get("option_contract_code")
    )
    if contract_code:
        return _upper(position.get("code")) == contract_code
    return (
        _lower(position.get("option_type"))
        == _lower(contract_identity.get("option_type"))
        and _text(position.get("expiration_ymd"))
        == _text(contract_identity.get("expiration_ymd"))
        and _decimal(position.get("strike"))
        == _decimal(contract_identity.get("strike"))
    )


def _normalize_anchor(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "evidence_id": _text(row.get("evidence_id")),
        "source_event_id": _text(row.get("source_event_id")),
        "account": _lower(row.get("account")),
        "futu_account_id": _text(row.get("futu_account_id")),
        "symbol": _upper(row.get("symbol")),
        "option_type": _lower(row.get("option_type")),
        "position_side": _lower(row.get("position_side")),
        "strike": _decimal(row.get("strike")),
        "expiration_ymd": _text(row.get("expiration_ymd")),
        "contracts": _optional_int(row.get("contracts")),
        "price": _decimal(row.get("price")),
        "event_time_ms": _optional_int(row.get("event_time_ms")),
        "received_at_ms": _optional_int(row.get("received_at_ms")),
        "order_id": _text(row.get("order_id")) or None,
        "clearing_date": _text(row.get("clearing_date")) or None,
    }


def _normalize_deal(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "deal_id": _first_text(row, "deal_id", "dealID", "id"),
        "futu_account_id": _first_text(
            row, "acc_id", "futu_account_id"
        ),
        "code": _upper(_first(row, "code", "stock_code", "symbol")),
        "side": _lower(_first(row, "side", "trd_side", "trade_side")),
        "quantity": _decimal(
            _first(row, "qty", "quantity", "contracts", "shares")
        ),
        "price": _decimal(_first(row, "price", "deal_price")),
        "trade_time_ms": _optional_int(
            _first(row, "trade_time_ms", "event_time_ms")
        ),
        "trade_time": _first_text(row, "create_time", "trade_time") or None,
        "order_id": _first_text(row, "order_id", "orderID") or None,
        "clearing_date": _first_text(
            row, "clearing_date", "settlement_date"
        )
        or None,
    }


def _normalize_order(row: Mapping[str, Any]) -> dict[str, Any]:
    automatic = row.get("is_broker_auto")
    return {
        "order_id": _first_text(row, "order_id", "orderID", "id"),
        "is_broker_auto": (
            bool(automatic) if isinstance(automatic, bool) else None
        ),
        "order_origin": _lower(
            _first(row, "order_origin", "source", "order_source")
        )
        or None,
    }


def _normalize_position(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "futu_account_id": _first_text(
            row, "acc_id", "futu_account_id"
        ),
        "code": _upper(_first(row, "code", "stock_code", "symbol")),
        "quantity": _decimal(
            _first(
                row,
                "qty",
                "quantity",
                "position_qty",
                "can_sell_qty",
            )
        ),
        "option_type": _lower(row.get("option_type")) or None,
        "expiration_ymd": _first_text(
            row, "expiration_ymd", "expiration"
        )
        or None,
        "strike": _decimal(row.get("strike")),
    }


def _normalize_calendar(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "date": _first_text(row, "date", "time", "trade_date"),
        "type": _upper(
            _first(row, "type", "trade_date_type", "trade_type")
        ),
    }


def _normalize_contract_metadata(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "settlement_style": _lower(row.get("settlement_style")),
        "underlying_security_type": _lower(
            row.get("underlying_security_type")
        ),
        "last_trade_cutoff_ms": _optional_int(
            row.get("last_trade_cutoff_ms")
        ),
        "last_trade_cutoff_source": _lower(
            row.get("last_trade_cutoff_source")
        ),
        "calendar_hash": _text(row.get("calendar_hash")),
    }


def _stock_candidates(value: Any) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in value or ():
        if not isinstance(row, Mapping):
            continue
        output.append(
            {
                "source_event_id": _text(row.get("source_event_id")),
                "account": _lower(row.get("account")),
                "futu_account_id": _text(row.get("futu_account_id")),
                "symbol": _upper(row.get("symbol")),
                "side": _lower(row.get("side")),
                "stock_qty": _optional_int(row.get("stock_qty")),
                "stock_price": _decimal(row.get("stock_price")),
                "trade_time_ms": _optional_int(row.get("trade_time_ms")),
                "order_id": _text(row.get("order_id")) or None,
                "clearing_date": _text(row.get("clearing_date")) or None,
            }
        )
    return sorted(
        output,
        key=lambda item: json.dumps(
            item,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _anchor_order_id(receipts: Mapping[str, Any]) -> str:
    receipt = receipts.get("anchor_option_close")
    if not isinstance(receipt, Mapping):
        return ""
    for row in receipt.get("rows") or ():
        if isinstance(row, Mapping):
            value = _text(row.get("order_id"))
            if value:
                return value
    return ""


def _legacy_settlement_deadline_ms(
    observation: Mapping[str, Any],
) -> int | None:
    contract = observation.get("contract_identity")
    receipts = observation.get("source_receipts")
    if not isinstance(contract, Mapping) or not isinstance(
        receipts, Mapping
    ):
        return None
    expiration_text = _text(contract.get("expiration_ymd"))
    market = _upper(observation.get("market"))
    timezone_name = {"US": "America/New_York", "HK": "Asia/Hong_Kong"}.get(
        market
    )
    calendar = receipts.get("trading_calendar")
    if (
        not expiration_text
        or timezone_name is None
        or not isinstance(calendar, Mapping)
    ):
        return None
    try:
        expiration = date.fromisoformat(expiration_text)
    except ValueError:
        return None
    following: list[date] = []
    for row in calendar.get("rows") or ():
        if not isinstance(row, Mapping):
            continue
        day_text = _first_text(row, "date", "time", "trade_date")
        kind = _upper(
            _first(row, "type", "trade_date_type", "trade_type")
        )
        try:
            day = date.fromisoformat(day_text)
        except ValueError:
            continue
        if day > expiration and kind in {"WHOLE", "TRADING"}:
            following.append(day)
    unique_following = sorted(set(following))
    if len(unique_following) < 2:
        return None
    deadline = datetime.combine(
        unique_following[1] + timedelta(days=1),
        time.min,
        tzinfo=ZoneInfo(timezone_name),
    )
    return int(deadline.timestamp() * 1000)


def _first(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _first_text(row: Mapping[str, Any], *keys: str) -> str:
    return _text(_first(row, *keys))


def _text(value: Any) -> str:
    return str(value or "").strip()


def _lower(value: Any) -> str:
    return _text(value).lower()


def _upper(value: Any) -> str:
    return _text(value).upper()


def _decimal(value: Any) -> str | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite():
        return None
    normalized = format(parsed.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _required_int(value: Any, *, field: str) -> int:
    parsed = _optional_int(value)
    if parsed is None or parsed <= 0:
        raise SettlementSemanticUnavailable(f"{field} is invalid")
    return parsed


__all__ = [
    "LegacySettlementSemanticUnavailable",
    "SETTLEMENT_ROW_NORMALIZER_SCHEMA",
    "SETTLEMENT_SEMANTIC_SCHEMA",
    "SettlementAdmissionStateIncoherent",
    "SettlementSemanticUnavailable",
    "attach_settlement_semantics",
    "canonical_hash",
    "settlement_evidence_id",
    "settlement_observation_semantic",
    "settlement_semantic_from_evidence",
]
