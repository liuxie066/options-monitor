from __future__ import annotations

import math
from typing import Any, Mapping

from domain.domain.symbol_identity import canonical_symbol
from domain.domain.trade_contract_identity import normalize_contract_expiration
from src.application.opend_normalize import normalize_opend_option_type


_MARKET_SNAPSHOT_BATCH_SIZE = 200


class OpenDOptionEvidenceError(RuntimeError):
    code = "OPEND_OPTION_MULTIPLIER_EVIDENCE_INCOMPLETE"


class OpenDOptionTermsEvidenceError(RuntimeError):
    code = "OPEND_OPTION_TERMS_EVIDENCE_INCOMPLETE"


def _rows(value: Any, *, strict: bool = False) -> list[dict[str, Any]]:
    if hasattr(value, "to_dict"):
        try:
            records = value.to_dict("records")
        except Exception:
            records = None
        if isinstance(records, list):
            if strict and any(not isinstance(item, dict) for item in records):
                raise ValueError("OpenD position response contains malformed rows")
            return [dict(item) for item in records if isinstance(item, dict)]
    if isinstance(value, list):
        if strict and any(not isinstance(item, dict) for item in value):
            raise ValueError("OpenD position response contains malformed rows")
        return [dict(item) for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [dict(value)]
    if strict:
        raise ValueError("OpenD position response completeness is unknown")
    return []



def _positive_number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _position_quantity(row: Mapping[str, Any]) -> float | None:
    raw = row.get("qty") if "qty" in row else row.get("quantity")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _enrich_option_contract_terms(
    gateway: Any,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    required_codes = sorted(
        {
            str(row.get("code") or row.get("symbol") or row.get("stock_code") or "")
            .strip()
            .upper()
            for row in rows
            if _position_quantity(row) not in (None, 0.0)
        }
        - {""}
    )
    required_code_set = set(required_codes)
    snapshot_by_code: dict[str, dict[str, Any]] = {}
    for start in range(0, len(required_codes), _MARKET_SNAPSHOT_BATCH_SIZE):
        batch = required_codes[start : start + _MARKET_SNAPSHOT_BATCH_SIZE]
        snapshot_rows = _rows(gateway.get_snapshot(batch))
        for snapshot_row in snapshot_rows:
            code = str(snapshot_row.get("code") or "").strip().upper()
            if not code or code not in required_code_set:
                continue
            if code in snapshot_by_code:
                raise OpenDOptionTermsEvidenceError(
                    f"OpenD market snapshot returned duplicate option terms for {code}."
                )
            snapshot_by_code[code] = dict(snapshot_row)

    enriched: list[dict[str, Any]] = []
    missing_multiplier_codes: list[str] = []
    incomplete_terms_codes: list[str] = []
    for row in rows:
        item = dict(row)
        quantity = _position_quantity(item)
        if quantity in (None, 0.0):
            enriched.append(item)
            continue
        code = str(
            item.get("code") or item.get("symbol") or item.get("stock_code") or ""
        ).strip().upper()
        snapshot_row = snapshot_by_code.get(code)
        if snapshot_row is None:
            incomplete_terms_codes.append(code or "unknown")
            enriched.append(item)
            continue

        multiplier = _snapshot_multiplier(snapshot_row)
        if multiplier is None:
            missing_multiplier_codes.append(code or "unknown")
            enriched.append(item)
            continue
        if not _has_complete_current_option_terms(snapshot_row):
            incomplete_terms_codes.append(code or "unknown")
            enriched.append(item)
            continue

        _copy_snapshot_option_terms(item, snapshot_row)
        item["options_per_contract"] = multiplier
        enriched.append(item)

    if missing_multiplier_codes:
        raise OpenDOptionEvidenceError(
            "OpenD market snapshot did not provide multiplier evidence for "
            f"{len(missing_multiplier_codes)} non-zero option position(s)."
        )
    if incomplete_terms_codes:
        raise OpenDOptionTermsEvidenceError(
            "OpenD market snapshot did not provide complete current terms for "
            f"{len(incomplete_terms_codes)} non-zero option position(s)."
        )
    return enriched


def _copy_snapshot_option_terms(
    target: dict[str, Any],
    snapshot: Mapping[str, Any],
) -> None:
    aliases = {
        "stock_owner": ("stock_owner", "owner_code", "underlying"),
        "option_type": ("option_type",),
        "strike_time": ("strike_time", "expiration_ymd", "expiration"),
        "option_strike_price": ("option_strike_price", "strike_price"),
        "option_contract_size": ("option_contract_size", "contract_size"),
        "option_contract_multiplier": (
            "option_contract_multiplier",
            "contract_multiplier",
        ),
        "lot_size": ("lot_size",),
        "option_valid": ("option_valid",),
    }
    for field, candidates in aliases.items():
        for candidate in candidates:
            value = snapshot.get(candidate)
            if value not in (None, ""):
                target[field] = value
                break
    target["option_terms_source"] = "market_snapshot"


def _snapshot_multiplier(row: Mapping[str, Any]) -> float | None:
    values = {
        value
        for key in (
            "option_contract_multiplier",
            "option_contract_size",
        )
        if (value := _positive_number(row.get(key))) is not None
    }
    if len(values) == 1:
        return next(iter(values))
    if len(values) > 1:
        return None
    return _positive_number(row.get("lot_size"))


def _has_complete_current_option_terms(row: Mapping[str, Any]) -> bool:
    if row.get("option_valid") is not True:
        return False
    option_type = normalize_opend_option_type(row.get("option_type"))
    expiration = normalize_contract_expiration(
        row.get("strike_time")
        or row.get("expiration_ymd")
        or row.get("expiration")
    )
    strike = _positive_number(
        row.get("option_strike_price")
        if row.get("option_strike_price") not in (None, "")
        else row.get("strike_price")
    )
    multiplier = _snapshot_multiplier(row)
    owner = canonical_symbol(
        row.get("stock_owner")
        or row.get("owner_code")
        or row.get("underlying")
    )
    return bool(
        owner
        and option_type in {"put", "call"}
        and expiration
        and strike is not None
        and multiplier is not None
    )
