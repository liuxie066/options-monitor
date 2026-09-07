from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from domain.domain.source_evidence import SOURCE_EVIDENCE_VERSION
from domain.domain.trade_execution import canonical_decimal, canonical_utc_instant


POSITION_SNAPSHOT_VERSION = "position_snapshot.v1"


def normalize_position_snapshot_input(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a scoped position observation without inferring missing holdings."""
    errors: list[str] = []

    def text(value: Any, name: str) -> str | None:
        result = str(value).strip() if value is not None else ""
        if not result:
            errors.append(f"missing:{name}")
        return result or None

    def instant(value: Any, name: str, *, required: bool = True) -> str | None:
        try:
            result = canonical_utc_instant(value)
        except ValueError as exc:
            errors.append(f"invalid:{name}:{exc}")
            return None
        if required and result is None:
            errors.append(f"missing:{name}")
        return result

    def decimal(value: Any, name: str, *, positive: bool = False, integer: bool = False) -> str | None:
        try:
            result = canonical_decimal(value)
        except ValueError as exc:
            errors.append(f"invalid:{name}:{exc}")
            return None
        if result is None:
            errors.append(f"missing:{name}")
        elif Decimal(result) < 0 or (positive and Decimal(result) == 0):
            errors.append(f"invalid:{name}:quantity_range")
        elif integer and Decimal(result) != Decimal(result).to_integral_value():
            errors.append(f"invalid:{name}:integer_required")
        return result

    if payload.get("schema_version") not in (None, POSITION_SNAPSHOT_VERSION):
        errors.append("unsupported:schema_version")
    raw_account = payload.get("broker_account_ref")
    raw_account = raw_account if isinstance(raw_account, Mapping) else {}
    account = {
        name: text(raw_account.get(name), f"broker_account_ref.{name}")
        for name in ("broker_account_id", "broker_id", "external_account_id", "environment", "account_label")
    }
    if isinstance(raw_account.get("external_account_id"), (float, bool)):
        errors.append("invalid:broker_account_ref.external_account_id:string_required")
    account["environment"] = str(account["environment"] or "").upper() or None
    account["broker_id"] = str(account["broker_id"] or "").lower() or None
    account["account_label"] = str(account["account_label"] or "").lower() or None
    raw_scope = payload.get("scope")
    raw_scope = raw_scope if isinstance(raw_scope, Mapping) else {}
    scope: dict[str, Any] = {}
    for name in ("markets", "asset_types"):
        values = raw_scope.get(name)
        if not isinstance(values, list) or not values or any(not isinstance(item, str) or not item.strip() for item in values):
            errors.append(f"missing:scope.{name}")
            values = []
        scope[name] = sorted({item.upper() if name == "markets" else item.lower() for item in values})
    if any(item not in {"stock", "option"} for item in scope["asset_types"]):
        errors.append("unsupported:scope.asset_types")
    scope["filtered"] = raw_scope.get("filtered")
    if not isinstance(scope["filtered"], bool):
        errors.append("missing:scope.filtered")
    if raw_scope.get("symbols"):
        scope["symbols"] = list(raw_scope["symbols"])
        scope["filtered"] = True
    completeness = payload.get("completeness")
    if completeness not in ("complete", "partial", "unknown"):
        errors.append("invalid:completeness")
        completeness = "unknown"
    quality = dict(payload.get("quality") or {}) if isinstance(payload.get("quality"), Mapping) else {}
    if quality.get("status") not in ("ready", "stale", "unavailable", "unknown"):
        errors.append("missing:quality.status")
    rows: list[dict[str, Any]] = []
    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list):
        errors.append("missing:rows")
        raw_rows = []
    for index, raw in enumerate(raw_rows):
        prefix = f"rows.{index}"
        if not isinstance(raw, Mapping):
            errors.append(f"invalid:{prefix}")
            continue
        row = dict(raw)
        raw_instrument = raw.get("instrument_ref")
        instrument = dict(raw_instrument) if isinstance(raw_instrument, Mapping) else {}
        for name in ("asset_type", "symbol", "market", "currency"):
            value = text(instrument.get(name), f"{prefix}.instrument_ref.{name}")
            instrument[name] = value.lower() if value and name == "asset_type" else value.upper() if value else None
        asset = instrument["asset_type"]
        if asset not in {"stock", "option"}:
            errors.append(f"unsupported:{prefix}.instrument_ref.asset_type")
        if instrument["market"] not in scope["markets"] or asset not in scope["asset_types"]:
            errors.append(f"outside_scope:{prefix}.instrument_ref")
        if asset == "option":
            if instrument.get("option_type") not in ("put", "call"):
                errors.append(f"invalid:{prefix}.instrument_ref.option_type")
            try:
                date.fromisoformat(str(instrument.get("expiration_ymd") or ""))
            except ValueError:
                errors.append(f"invalid:{prefix}.instrument_ref.expiration_ymd")
            for name in ("strike", "multiplier"):
                instrument[name] = decimal(instrument.get(name), f"{prefix}.instrument_ref.{name}", positive=True, integer=name == "multiplier")
            deliverable = instrument.get("deliverable")
            if deliverable is not None and not isinstance(deliverable, Mapping):
                errors.append(f"invalid:{prefix}.instrument_ref.deliverable")
            elif deliverable:
                errors.append(f"unsupported:{prefix}.instrument_ref.deliverable")
        row["instrument_ref"] = instrument
        row["quantity"] = decimal(row.get("quantity"), f"{prefix}.quantity", integer=asset == "option")
        if row.get("position_side") not in ("long", "short"):
            errors.append(f"invalid:{prefix}.position_side")
        if row.get("sellable_quantity") is not None:
            row["sellable_quantity"] = decimal(row["sellable_quantity"], f"{prefix}.sellable_quantity")
            if not row.get("sellable_quantity_source"):
                errors.append(f"missing:{prefix}.sellable_quantity_source")
        rows.append(row)
    raw_evidence_refs = payload.get("evidence_refs") or []
    if not isinstance(raw_evidence_refs, list) or any(not isinstance(ref, str) for ref in raw_evidence_refs):
        errors.append("invalid:evidence_refs")
        raw_evidence_refs = []
    raw_source_evidence = payload.get("source_evidence") or []
    if not isinstance(raw_source_evidence, list) or any(not isinstance(item, Mapping) for item in raw_source_evidence):
        errors.append("invalid:source_evidence")
        raw_source_evidence = []
    source_evidence = [dict(item) for item in raw_source_evidence]
    if any(item.get("schema_version") != SOURCE_EVIDENCE_VERSION or not item.get("evidence_id") for item in source_evidence):
        errors.append("invalid:source_evidence_contract")
    if any(item.get("evidence_id") not in raw_evidence_refs for item in source_evidence):
        errors.append("unlinked:source_evidence")
    return {
        "schema_version": POSITION_SNAPSHOT_VERSION,
        "snapshot_id": text(payload.get("snapshot_id"), "snapshot_id"),
        "source_id": text(payload.get("source_id"), "source_id"),
        "broker_account_ref": account,
        "scope": scope,
        "observed_at_utc": instant(payload.get("observed_at_utc"), "observed_at_utc"),
        "source_as_of_utc": instant(payload.get("source_as_of_utc"), "source_as_of_utc", required=False),
        "completeness": completeness,
        "quality": quality,
        "rows": rows,
        "evidence_refs": list(raw_evidence_refs),
        "source_evidence": source_evidence,
        "errors": sorted(set(errors + list(payload.get("errors") or []))),
        **({"source_payload": payload["source_payload"]} if "source_payload" in payload else {}),
    }


def position_snapshot_scope_errors(
    payload: Mapping[str, Any], *, account_label: str, environment: str, market: str,
    asset_type: str, now_utc: datetime, external_account_id: str | None = None,
    max_age_seconds: int = 300,
) -> list[str]:
    snapshot = normalize_position_snapshot_input(payload)
    errors = list(snapshot["errors"])
    account = snapshot["broker_account_ref"]
    if account["account_label"] != account_label.lower():
        errors.append("snapshot_account_mismatch")
    if external_account_id is not None and account["external_account_id"] != external_account_id:
        errors.append("snapshot_physical_account_mismatch")
    if account["environment"] != environment.upper():
        errors.append("snapshot_environment_mismatch")
    scope = snapshot["scope"]
    if market.upper() not in scope["markets"] or asset_type not in scope["asset_types"]:
        errors.append("snapshot_scope_mismatch")
    if scope["filtered"] is not False or snapshot["completeness"] != "complete":
        errors.append("snapshot_scope_incomplete")
    if snapshot["quality"].get("status") != "ready":
        errors.append("snapshot_quality_unavailable")
    observed = snapshot["observed_at_utc"]
    source = snapshot["source_as_of_utc"]
    if observed and source and datetime.fromisoformat(source.replace("Z", "+00:00")) > datetime.fromisoformat(observed.replace("Z", "+00:00")):
        errors.append("snapshot_source_after_observation")
    now = now_utc.astimezone(timezone.utc)
    for name in ("observed_at_utc", "source_as_of_utc"):
        value = snapshot[name]
        if not value:
            continue
        age = (now - datetime.fromisoformat(value.replace("Z", "+00:00"))).total_seconds()
        if age < 0 or age > max_age_seconds:
            errors.append(f"snapshot_{name}_stale_or_future")
    return sorted(set(errors))
