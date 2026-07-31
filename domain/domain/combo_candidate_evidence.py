from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from numbers import Integral, Real
from typing import Any, Iterable, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.symbol_identity import canonical_symbol


COMBO_CANDIDATE_OCCURRENCE_SCHEMA = "combo_candidate_occurrence.v1"
COMBO_CANDIDATE_EXPOSURE_SCHEMA = "combo_candidate_exposure.v1"

_OCCURRENCE_FIELDS = frozenset(
    {
        "candidate_occurrence_schema",
        "candidate_occurrence_id",
        "candidate_occurrence_generated_at_utc",
        "candidate_occurrence_data_as_of_utc",
        "candidate_row_content_hash",
    }
)


def build_combo_candidate_occurrence(
    row: Mapping[str, Any],
    *,
    account: str,
    market: str,
    run_id: str,
    generated_at_utc: datetime | str,
    data_as_of_utc: datetime | str | None = None,
) -> dict[str, Any]:
    """Build immutable occurrence metadata for one published Combo candidate row."""

    source = dict(row or {})
    account_value = str(account or "").strip().lower()
    market_value = str(market or "").strip().upper()
    run_value = str(run_id or "").strip()
    pair_id = str(
        source.get("candidate_pair_id")
        or source.get("strategy_group_id")
        or ""
    ).strip()
    structure_mode = str(source.get("structure_mode") or "").strip().lower()
    currency = str(source.get("currency") or "").strip().upper()
    multiplier = _decimal_text(source.get("multiplier"))
    if not all((account_value, market_value, run_value, pair_id, structure_mode, currency, multiplier)):
        raise ValueError("combo candidate occurrence identity is incomplete")
    generated_at = _utc_iso(generated_at_utc)
    data_as_of = _utc_iso(data_as_of_utc or generated_at)
    identity_payload = _occurrence_identity_payload(
        source,
        account=account_value,
        market=market_value,
        run_id=run_value,
    )
    row_payload = {
        str(key): value
        for key, value in source.items()
        if str(key) not in _OCCURRENCE_FIELDS
    }
    return {
        "candidate_occurrence_schema": COMBO_CANDIDATE_OCCURRENCE_SCHEMA,
        "candidate_occurrence_id": canonical_sha256(identity_payload),
        "candidate_occurrence_generated_at_utc": generated_at,
        "candidate_occurrence_data_as_of_utc": data_as_of,
        "candidate_row_content_hash": _row_content_hash(row_payload),
    }


def derive_combo_candidate_exposures(
    brief: Mapping[str, Any],
    *,
    candidate_identities: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Derive exact rendered Combo exposures from one frozen Brief revision."""

    source = dict(brief or {})
    if str(source.get("actionability") or "").strip().lower() != "live_actionable":
        return []
    account = str(source.get("account") or "").strip().lower()
    market = str(source.get("market") or "").strip().upper()
    run_id = str(source.get("run_id") or "").strip()
    market_date = str(source.get("market_trading_date") or "").strip()
    revision = _positive_or_zero_int(source.get("revision"))
    generated_at = _utc_iso(source.get("generated_at_utc"))
    data_as_of = _utc_iso(source.get("data_as_of_utc"))
    valid_until = _utc_iso(source.get("valid_until_utc"))
    if not all((account, market, run_id, market_date)) or revision is None:
        return []
    generated_at_ms = _utc_ms(generated_at)
    valid_until_ms = _utc_ms(valid_until)
    if generated_at_ms <= 0 or valid_until_ms < generated_at_ms:
        return []
    allowed = None
    if candidate_identities is not None:
        allowed = {
            str(item).strip()
            for item in candidate_identities
            if str(item).strip()
        }
    rows = [
        dict(item)
        for item in ((source.get("candidates") or {}).get("combo_yield") or [])
        if isinstance(item, Mapping)
    ]
    indexed = [
        dict(item)
        for item in (source.get("candidate_index") or [])
        if isinstance(item, Mapping)
        and str(item.get("strategy_family") or "").strip().lower() == "combo_yield"
        and (allowed is None or str(item.get("identity") or "").strip() in allowed)
    ]
    brief_id = canonical_sha256(
        {
            "schema_version": "daily_decision_brief_identity.v1",
            "account": account,
            "market": market,
            "market_trading_date": market_date,
            "run_id": run_id,
        }
    )
    out: list[dict[str, Any]] = []
    for item in indexed:
        representative = item.get("representative")
        if not isinstance(representative, Mapping):
            continue
        representative_key = _brief_combo_key(representative)
        if representative_key is None:
            continue
        matches = [row for row in rows if _brief_combo_key(row) == representative_key]
        if len(matches) != 1:
            continue
        row = matches[0]
        if not _valid_occurrence_row(
            row,
            account=account,
            market=market,
            run_id=run_id,
        ):
            continue
        occurrence_id = str(row["candidate_occurrence_id"])
        exposure_identity = {
            "schema_version": COMBO_CANDIDATE_EXPOSURE_SCHEMA,
            "candidate_occurrence_id": occurrence_id,
            "brief_id": brief_id,
            "revision": revision,
            "generated_at_utc": generated_at,
            "data_as_of_utc": data_as_of,
            "valid_until_utc": valid_until,
            "actionability": "live_actionable",
        }
        out.append(
            {
                "schema_version": COMBO_CANDIDATE_EXPOSURE_SCHEMA,
                "candidate_exposure_id": canonical_sha256(exposure_identity),
                "candidate_occurrence_id": occurrence_id,
                "candidate_identity": str(item.get("identity") or "").strip(),
                "brief_id": brief_id,
                "revision": revision,
                "account": account,
                "market": market,
                "market_trading_date": market_date,
                "put_contract_key": representative_key[0],
                "call_contract_key": representative_key[1],
                "currency": representative_key[2],
                "multiplier": representative_key[3],
                "generated_at_utc": generated_at,
                "data_as_of_utc": data_as_of,
                "valid_until_utc": valid_until,
                "generated_at_ms": generated_at_ms,
                "valid_until_ms": valid_until_ms,
                "delivery_confirmed": False,
            }
        )
    return sorted(out, key=lambda item: str(item["candidate_exposure_id"]))


def combo_exposure_render_context(exposures: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = [dict(item) for item in exposures]
    return {
        "candidate_occurrence_ids": sorted(
            {str(item.get("candidate_occurrence_id") or "").strip() for item in rows}
            - {""}
        ),
        "candidate_exposure_ids": sorted(
            {str(item.get("candidate_exposure_id") or "").strip() for item in rows}
            - {""}
        ),
    }


def combo_candidate_identities_for_rendered_rows(
    brief: Mapping[str, Any],
    rendered_combo_rows: Iterable[Mapping[str, Any]],
) -> list[str]:
    """Map actually rendered Combo rows back to exact Brief candidate identities."""

    indexed = [
        dict(item)
        for item in (brief.get("candidate_index") or [])
        if isinstance(item, Mapping)
        and str(item.get("strategy_family") or "").strip().lower()
        == "combo_yield"
        and isinstance(item.get("representative"), Mapping)
    ]
    identities: set[str] = set()
    for row in rendered_combo_rows:
        rendered_key = _brief_combo_key(row)
        if rendered_key is None:
            continue
        matches = [
            item
            for item in indexed
            if _brief_combo_key(item["representative"]) == rendered_key
        ]
        if len(matches) != 1:
            continue
        identity = str(matches[0].get("identity") or "").strip()
        if identity:
            identities.add(identity)
    return sorted(identities)


def _valid_occurrence_row(
    row: Mapping[str, Any],
    *,
    account: str,
    market: str,
    run_id: str,
) -> bool:
    if str(row.get("candidate_occurrence_schema") or "") != COMBO_CANDIDATE_OCCURRENCE_SCHEMA:
        return False
    occurrence_id = str(row.get("candidate_occurrence_id") or "").strip()
    content_hash = str(row.get("candidate_row_content_hash") or "").strip()
    if len(occurrence_id) != 64 or len(content_hash) != 64:
        return False
    try:
        expected = canonical_sha256(
            _occurrence_identity_payload(
                row,
                account=account,
                market=market,
                run_id=run_id,
            )
        )
    except ValueError:
        return False
    return occurrence_id == expected


def _occurrence_identity_payload(
    row: Mapping[str, Any],
    *,
    account: str,
    market: str,
    run_id: str,
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    market_value = str(market or "").strip().upper()
    run_value = str(run_id or "").strip()
    pair_id = str(
        row.get("candidate_pair_id")
        or row.get("strategy_group_id")
        or ""
    ).strip()
    structure_mode = str(row.get("structure_mode") or "").strip().lower()
    currency = str(row.get("currency") or "").strip().upper()
    multiplier = _decimal_text(row.get("multiplier"))
    if not all((account_value, market_value, run_value, pair_id, structure_mode, currency, multiplier)):
        raise ValueError("combo candidate occurrence identity is incomplete")
    return {
        "schema_version": COMBO_CANDIDATE_OCCURRENCE_SCHEMA,
        "account": account_value,
        "market": market_value,
        "run_id": run_value,
        "candidate_pair_id": pair_id,
        "structure_mode": structure_mode,
        "put_contract_key": _candidate_contract_key(row, option_type="put"),
        "call_contract_key": _candidate_contract_key(row, option_type="call"),
        "currency": currency,
        "multiplier": multiplier,
    }


def _brief_combo_key(
    row: Mapping[str, Any],
) -> tuple[dict[str, str], dict[str, str], str, str] | None:
    try:
        put_key = _candidate_contract_key(row, option_type="put")
        call_key = _candidate_contract_key(row, option_type="call")
        currency = str(row.get("currency") or "").strip().upper()
        multiplier = _decimal_text(row.get("multiplier"))
    except ValueError:
        return None
    if not currency or not multiplier:
        return None
    return put_key, call_key, currency, multiplier


def _candidate_contract_key(row: Mapping[str, Any], *, option_type: str) -> dict[str, str]:
    prefix = "put" if option_type == "put" else "call"
    symbol = canonical_symbol(row.get("symbol") or row.get("underlying_symbol"))
    expiration = str(
        row.get(f"{prefix}_expiration")
        or row.get("expiration")
        or ""
    ).strip()
    strike = _decimal_text(row.get(f"{prefix}_strike"))
    if not all((symbol, expiration, strike)):
        raise ValueError("combo candidate contract key is incomplete")
    return {
        "underlying_symbol": symbol,
        "option_type": option_type,
        "expiration_ymd": expiration,
        "strike": strike,
    }


def _row_content_hash(value: Mapping[str, Any]) -> str:
    normalized = _canonical_row_value(dict(value))
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_row_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, Decimal):
        return _decimal_text(value) if value.is_finite() else None
    if isinstance(value, Integral):
        return _decimal_text(value)
    if isinstance(value, Real):
        numeric = float(value)
        return _decimal_text(value) if math.isfinite(numeric) else None
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _canonical_row_value(item())
        except (TypeError, ValueError):
            return None
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_row_value(item_value)
            for key, item_value in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_row_value(item_value) for item_value in value]
    if isinstance(value, datetime):
        return _utc_iso(value)
    return str(value)


def _decimal_text(value: Any) -> str:
    if value in (None, "") or isinstance(value, bool):
        return ""
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return ""
    if not number.is_finite():
        return ""
    if number == 0:
        return "0"
    rendered = format(number.normalize(), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _utc_iso(value: datetime | str | Any) -> str:
    if isinstance(value, datetime):
        observed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            raise ValueError("UTC timestamp is required")
        observed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    return observed.astimezone(timezone.utc).isoformat()


def _utc_ms(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def _positive_or_zero_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


__all__ = [
    "COMBO_CANDIDATE_EXPOSURE_SCHEMA",
    "COMBO_CANDIDATE_OCCURRENCE_SCHEMA",
    "build_combo_candidate_occurrence",
    "combo_candidate_identities_for_rendered_rows",
    "combo_exposure_render_context",
    "derive_combo_candidate_exposures",
]
