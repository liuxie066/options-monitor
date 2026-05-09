from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
import sys
from typing import Any

from domain.domain.option_positions_v2 import (
    EVENT_KIND_CLOSE_TRADE,
    EVENT_KIND_MANUAL_ADJUSTMENT,
    EVENT_KIND_OPEN_TRADE,
    SNAPSHOT_TYPE_BASELINE,
    SNAPSHOT_TYPE_VERIFICATION,
    adapt_legacy_trade_events,
    build_baseline_snapshot_from_legacy_records,
    build_position_key,
    normalize_position_event,
    normalize_position_snapshot,
    project_current_positions,
    reconcile_snapshot_against_projection,
    utc_now_iso,
)
from domain.storage.repositories import option_positions_v2_repo
from scripts.option_positions_core.domain import (
    effective_expiration_ymd,
    effective_multiplier,
    effective_strike,
    normalize_account,
    normalize_broker,
    normalize_currency,
    normalize_option_type,
    normalize_side,
    normalize_status,
    parse_exp_to_ms,
)
from scripts.option_positions_core.service import require_option_positions_read_repo
from scripts.trade_contract_identity import canonical_contract_symbol


APP_BASE = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class OptionPositionsV2State:
    base: Path
    baseline_snapshot: dict[str, Any]
    events: list[dict[str, Any]]
    projection: dict[str, Any]
    skipped_legacy_events: list[dict[str, Any]]


@dataclass(frozen=True)
class CompatPositionRecords:
    state: OptionPositionsV2State
    records: list[dict[str, Any]]


def resolve_option_positions_v2_base(*, base: Path | None = None, repo: Any | None = None) -> Path:
    if base is not None:
        return Path(base).resolve()
    data_config = getattr(repo, "data_config_path", None)
    if data_config:
        try:
            return Path(str(data_config)).resolve().parent
        except Exception:
            pass
    db_path = getattr(getattr(repo, "primary_repo", repo), "db_path", None)
    if db_path:
        try:
            return Path(str(db_path)).resolve().parent
        except Exception:
            pass
    return APP_BASE


def _list_legacy_trade_events(repo: Any) -> list[dict[str, Any]]:
    list_trade_events = getattr(getattr(repo, "primary_repo", repo), "list_trade_events", None)
    if not callable(list_trade_events):
        return []
    rows = list_trade_events()
    return rows if isinstance(rows, list) else []


def _list_legacy_position_lots(repo: Any) -> list[dict[str, Any]]:
    try:
        primary_repo = require_option_positions_read_repo(repo)
    except Exception:
        primary_repo = getattr(repo, "primary_repo", repo)
    list_position_lots = getattr(primary_repo, "list_position_lots", None)
    if not callable(list_position_lots):
        return []
    rows = list_position_lots()
    return rows if isinstance(rows, list) else []


def _event_ms_to_iso(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value or 0) / 1000, tz=timezone.utc).isoformat()
    except Exception as exc:
        print(
            f"[WARN] option_positions_v2 invalid legacy trade_time_ms={value!r}; fallback to utc_now_iso ({type(exc).__name__}: {exc})",
            file=sys.stderr,
        )
        return utc_now_iso()


def _iso_sort_key(value: Any) -> tuple[float, str]:
    text = str(value or "").strip()
    if not text:
        return (0.0, "")
    try:
        return (datetime.fromisoformat(text).timestamp(), text)
    except ValueError:
        return (0.0, text)


def _empty_baseline_snapshot(*, snapshot_id: str = "legacy_baseline", snapshot_at_utc: str | None = None) -> dict[str, Any]:
    return normalize_position_snapshot(
        {
            "snapshot_id": snapshot_id,
            "snapshot_type": SNAPSHOT_TYPE_BASELINE,
            "snapshot_at_utc": snapshot_at_utc or utc_now_iso(),
            "source_name": "empty_baseline",
            "source_type": "system",
            "lots": [],
        }
    )


def _load_persisted_snapshots(base: Path) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for item in option_positions_v2_repo.load_position_snapshots(base):
        try:
            snapshots.append(normalize_position_snapshot(item))
        except Exception:
            continue
    snapshots.sort(key=lambda item: (_iso_sort_key(item.get("snapshot_at_utc")), str(item.get("snapshot_id") or "")))
    return snapshots


def _load_persisted_events(base: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for item in option_positions_v2_repo.load_position_events(base):
        try:
            events.append(normalize_position_event(item))
        except Exception:
            continue
    events.sort(key=lambda item: (_iso_sort_key(item.get("event_at_utc")), str(item.get("event_id") or "")))
    return events


def _latest_snapshot_by_type(
    snapshots: list[dict[str, Any]], snapshot_type: str
) -> dict[str, Any] | None:
    matches = [item for item in snapshots if str(item.get("snapshot_type") or "") == snapshot_type]
    return matches[-1] if matches else None


def _native_baseline_snapshot(
    *,
    legacy_events: list[dict[str, Any]],
    legacy_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    baseline_snapshot = _baseline_snapshot_from_bootstrap_events(legacy_events)
    if baseline_snapshot is not None:
        return baseline_snapshot
    non_bootstrap_events = [
        item
        for item in legacy_events
        if str((item or {}).get("source_type") or "").strip().lower() != "bootstrap_snapshot"
    ]
    if not non_bootstrap_events and legacy_rows:
        return build_baseline_snapshot_from_legacy_records(
            legacy_rows,
            snapshot_id="legacy_baseline",
            snapshot_at_utc=utc_now_iso(),
            source_name="legacy_position_lots",
        )
    earliest_event_ms: int | None = None
    for item in non_bootstrap_events:
        try:
            event_ms = int(item.get("trade_time_ms") or 0)
        except Exception:
            continue
        if earliest_event_ms is None or event_ms < earliest_event_ms:
            earliest_event_ms = event_ms
    snapshot_at_utc = _event_ms_to_iso(max(int(earliest_event_ms or 0) - 1, 0)) if earliest_event_ms is not None else utc_now_iso()
    return _empty_baseline_snapshot(snapshot_at_utc=snapshot_at_utc)


def _projection_checkpoint_snapshot(
    baseline_snapshot: dict[str, Any],
    verification_snapshots: list[dict[str, Any]],
) -> dict[str, Any]:
    latest_verification = _latest_snapshot_by_type(verification_snapshots, SNAPSHOT_TYPE_VERIFICATION)
    if latest_verification is None:
        return baseline_snapshot
    return normalize_position_snapshot(
        {
            "snapshot_id": latest_verification.get("snapshot_id"),
            "snapshot_type": SNAPSHOT_TYPE_BASELINE,
            "snapshot_at_utc": latest_verification.get("snapshot_at_utc"),
            "source_name": latest_verification.get("source_name") or "verification_checkpoint",
            "source_type": latest_verification.get("source_type"),
            "note": latest_verification.get("note"),
            "lots": list(latest_verification.get("lots") or []),
        }
    )


def _merge_native_and_legacy_events(
    native_events: list[dict[str, Any]],
    legacy_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    for event in legacy_events:
        event_id = str(event.get("event_id") or "").strip()
        if not event_id or event_id in merged:
            continue
        merged[event_id] = event
        ordered.append(event)
    for event in native_events:
        event_id = str(event.get("event_id") or "").strip()
        if not event_id:
            continue
        if event_id in merged:
            merged[event_id] = event
            for idx, current in enumerate(ordered):
                if str(current.get("event_id") or "").strip() == event_id:
                    ordered[idx] = event
                    break
            continue
        merged[event_id] = event
        ordered.append(event)
    ordered.sort(key=lambda item: (_iso_sort_key(item.get("event_at_utc")), str(item.get("event_id") or "")))
    return ordered


def _events_after_snapshot(events: list[dict[str, Any]], snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    snapshot_key = _iso_sort_key(snapshot.get("snapshot_at_utc"))
    filtered: list[dict[str, Any]] = []
    for event in events:
        event_key = _iso_sort_key(event.get("event_at_utc"))
        if event_key > snapshot_key:
            filtered.append(event)
    return filtered


def _build_snapshot_from_legacy_rows(
    rows: list[dict[str, Any]],
    *,
    snapshot_id: str,
    snapshot_type: str,
    snapshot_at_utc: str,
    source_name: str,
    source_type: str,
    note: str | None = None,
) -> dict[str, Any]:
    baseline = build_baseline_snapshot_from_legacy_records(
        rows,
        snapshot_id=snapshot_id,
        snapshot_at_utc=snapshot_at_utc,
        source_name=source_name,
    )
    return normalize_position_snapshot(
        {
            "snapshot_id": snapshot_id,
            "snapshot_type": snapshot_type,
            "snapshot_at_utc": snapshot_at_utc,
            "source_name": source_name,
            "source_type": source_type,
            "note": note,
            "lots": list(baseline.get("lots") or []),
        }
    )


def _baseline_snapshot_from_bootstrap_events(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    lots: list[dict[str, Any]] = []
    bootstrap_events = [
        item for item in events
        if str((item or {}).get("source_type") or "").strip().lower() == "bootstrap_snapshot"
    ]
    if not bootstrap_events:
        return None
    snapshot_at_utc = min(_event_ms_to_iso(item.get("trade_time_ms")) for item in bootstrap_events)
    for event in bootstrap_events:
        raw_payload = event.get("raw_payload") or {}
        if not isinstance(raw_payload, dict):
            raw_payload = {}
        fields = raw_payload.get("fields") or {}
        if not isinstance(fields, dict):
            fields = {}
        lot_record_id = str(raw_payload.get("lot_record_id") or raw_payload.get("record_id") or "").strip() or None
        symbol = fields.get("symbol") or event.get("symbol")
        expiration_ymd = (
            fields.get("expiration_ymd")
            or fields.get("exp")
            or effective_expiration_ymd(fields)
            or event.get("expiration_ymd")
        )
        strike = effective_strike(fields)
        if strike is None:
            strike = event.get("strike")
        multiplier = effective_multiplier(fields)
        if multiplier is None:
            multiplier = event.get("multiplier")
        contracts = fields.get("contracts_open", fields.get("contracts"))
        if contracts in (None, ""):
            contracts = event.get("contracts")
        lots.append(
            {
                "snapshot_lot_id": lot_record_id,
                "source_record_id": lot_record_id,
                "account": fields.get("account") or event.get("account"),
                "broker": fields.get("broker") or fields.get("market") or event.get("broker"),
                "symbol": symbol,
                "option_type": fields.get("option_type") or event.get("option_type"),
                "side": fields.get("side") or event.get("side"),
                "strike": strike,
                "expiration_ymd": expiration_ymd,
                "currency": fields.get("currency") or event.get("currency"),
                "multiplier": multiplier,
                "contracts": contracts,
                "verification_status": "unverified",
                "evidence_ref": f"legacy_bootstrap_event:{event.get('event_id')}",
            }
        )
    return normalize_position_snapshot(
        {
            "snapshot_id": "legacy_baseline",
            "snapshot_type": SNAPSHOT_TYPE_BASELINE,
            "snapshot_at_utc": snapshot_at_utc,
            "source_name": "legacy_bootstrap_events",
            "source_type": "legacy_import",
            "lots": lots,
        }
    )


def _fallback_baseline_snapshot(repo: Any) -> dict[str, Any]:
    legacy_rows = _list_legacy_position_lots(repo)
    if legacy_rows:
        return build_baseline_snapshot_from_legacy_records(
            legacy_rows,
            snapshot_id="legacy_baseline",
            snapshot_at_utc=utc_now_iso(),
            source_name="legacy_position_lots",
        )
    return normalize_position_snapshot(
        {
            "snapshot_id": "legacy_baseline",
            "snapshot_type": SNAPSHOT_TYPE_BASELINE,
            "snapshot_at_utc": utc_now_iso(),
            "source_name": "empty_baseline",
            "source_type": "system",
            "lots": [],
        }
    )


def refresh_option_positions_v2_state(*, base: Path | None = None, repo: Any) -> OptionPositionsV2State:
    resolved_base = resolve_option_positions_v2_base(base=base, repo=repo)
    legacy_events = _list_legacy_trade_events(repo)
    legacy_rows = _list_legacy_position_lots(repo)
    persisted_snapshots = _load_persisted_snapshots(resolved_base)
    baseline_snapshot = _latest_snapshot_by_type(persisted_snapshots, SNAPSHOT_TYPE_BASELINE)
    verification_snapshots = [
        item for item in persisted_snapshots if str(item.get("snapshot_type") or "") == SNAPSHOT_TYPE_VERIFICATION
    ]
    if baseline_snapshot is None:
        baseline_snapshot = _native_baseline_snapshot(legacy_events=legacy_events, legacy_rows=legacy_rows)
    persisted_events = _load_persisted_events(resolved_base)
    adapted = adapt_legacy_trade_events(legacy_events)
    merged_events = _merge_native_and_legacy_events(persisted_events, list(adapted["events"]))
    checkpoint_snapshot = _projection_checkpoint_snapshot(baseline_snapshot, verification_snapshots)
    projection = project_current_positions(checkpoint_snapshot, _events_after_snapshot(merged_events, checkpoint_snapshot))
    option_positions_v2_repo.replace_position_snapshots(
        resolved_base,
        [baseline_snapshot, *verification_snapshots],
    )
    option_positions_v2_repo.replace_position_events(resolved_base, merged_events)
    option_positions_v2_repo.write_current_projection(resolved_base, projection)
    return OptionPositionsV2State(
        base=resolved_base,
        baseline_snapshot=checkpoint_snapshot,
        events=list(merged_events),
        projection=projection,
        skipped_legacy_events=list(adapted["skipped"]),
    )


def append_option_positions_v2_event(
    *,
    base: Path | None = None,
    repo: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    resolved_base = resolve_option_positions_v2_base(base=base, repo=repo)
    event = normalize_position_event(payload)
    persisted_events = _load_persisted_events(resolved_base)
    events = _merge_native_and_legacy_events(
        [item for item in persisted_events if str(item.get("event_id") or "").strip() != str(event.get("event_id") or "").strip()] + [event],
        [],
    )
    option_positions_v2_repo.replace_position_events(resolved_base, events)
    return event


def snapshot_current_positions_as_verification(
    *,
    base: Path | None = None,
    repo: Any,
    snapshot_id: str,
    source_name: str,
    source_type: str,
    snapshot_at_utc: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    resolved_base = resolve_option_positions_v2_base(base=base, repo=repo)
    persisted_snapshots = _load_persisted_snapshots(resolved_base)
    baseline_snapshot = _latest_snapshot_by_type(persisted_snapshots, SNAPSHOT_TYPE_BASELINE)
    verification_snapshots = [
        item for item in persisted_snapshots if str(item.get("snapshot_type") or "") == SNAPSHOT_TYPE_VERIFICATION
    ]
    if baseline_snapshot is None:
        baseline_snapshot = _native_baseline_snapshot(
            legacy_events=_list_legacy_trade_events(repo),
            legacy_rows=_list_legacy_position_lots(repo),
        )
    verification_snapshot = _build_snapshot_from_legacy_rows(
        _list_legacy_position_lots(repo),
        snapshot_id=snapshot_id,
        snapshot_type=SNAPSHOT_TYPE_VERIFICATION,
        snapshot_at_utc=snapshot_at_utc or utc_now_iso(),
        source_name=source_name,
        source_type=source_type,
        note=note,
    )
    verification_snapshots = [
        item for item in verification_snapshots if str(item.get("snapshot_id") or "").strip() != str(snapshot_id).strip()
    ]
    verification_snapshots.append(verification_snapshot)
    option_positions_v2_repo.replace_position_snapshots(
        resolved_base,
        [baseline_snapshot, *verification_snapshots],
    )
    return verification_snapshot


def _legacy_position_key(fields: dict[str, Any]) -> str | None:
    try:
        broker = normalize_broker(fields.get("broker"))
        account = normalize_account(fields.get("account"))
        symbol = canonical_contract_symbol(fields.get("symbol"))
        option_type = normalize_option_type(fields.get("option_type"), strict=True)
        side = normalize_side(fields.get("side"), strict=True)
        strike = effective_strike(fields)
        expiration_ymd = effective_expiration_ymd(fields)
        if not broker or not account or not symbol or strike is None or not expiration_ymd:
            return None
        return build_position_key(
            broker=broker,
            account=account,
            symbol=symbol,
            option_type=option_type,
            side=side,
            strike=float(strike),
            expiration_ymd=expiration_ymd,
        )
    except Exception:
        return None


def _legacy_row_maps(rows: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_key: dict[str, dict[str, Any]] = {}
    by_record_id: dict[str, dict[str, Any]] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        record_id = str(item.get("record_id") or "").strip()
        fields = item.get("fields") or {}
        if not isinstance(fields, dict):
            continue
        if record_id:
            by_record_id[record_id] = item
        position_key = _legacy_position_key(fields)
        if position_key and position_key not in by_key:
            by_key[position_key] = item
    return by_key, by_record_id


def _source_event_id_from_position(position: dict[str, Any]) -> str | None:
    for item in position.get("applied_events") or []:
        if str(item.get("event_kind") or "") == "open_trade":
            event_id = str(item.get("event_id") or "").strip()
            if event_id:
                return event_id
    snapshot_lot_id = str(position.get("baseline_snapshot_lot_id") or "").strip()
    return snapshot_lot_id or None


def _last_close_event_id_from_position(position: dict[str, Any]) -> str | None:
    close_events = [
        str(item.get("event_id") or "").strip()
        for item in (position.get("applied_events") or [])
        if str(item.get("event_kind") or "") == "close_trade" and str(item.get("event_id") or "").strip()
    ]
    return close_events[-1] if close_events else None


def _compat_record_id(position: dict[str, Any], legacy_row: dict[str, Any] | None) -> str:
    if legacy_row is not None:
        record_id = str(legacy_row.get("record_id") or "").strip()
        if record_id:
            return record_id
    snapshot_lot_id = str(position.get("baseline_snapshot_lot_id") or "").strip()
    if snapshot_lot_id:
        return snapshot_lot_id
    digest = sha1(str(position.get("position_key") or "").encode("utf-8")).hexdigest()[:16]
    return f"v2-{digest}"


def _compat_fields_from_projection(position: dict[str, Any], legacy_row: dict[str, Any] | None) -> dict[str, Any]:
    legacy_fields = dict((legacy_row or {}).get("fields") or {})
    current_contracts = int(position.get("current_contracts") or 0)
    closed_contracts = legacy_fields.get("contracts_closed")
    if closed_contracts in (None, ""):
        base_total = legacy_fields.get("contracts")
        if base_total in (None, ""):
            base_total = max(current_contracts, int(position.get("baseline_contracts") or 0))
        closed_contracts = max(0, int(base_total or 0) - current_contracts)
    total_contracts = legacy_fields.get("contracts")
    if total_contracts in (None, ""):
        total_contracts = current_contracts + int(closed_contracts or 0)
    fields = dict(legacy_fields)
    fields.update(
        {
            "position_key": position.get("position_key"),
            "broker": position.get("broker"),
            "account": position.get("account"),
            "symbol": position.get("symbol"),
            "option_type": position.get("option_type"),
            "side": position.get("side"),
            "strike": position.get("strike"),
            "expiration": parse_exp_to_ms(position.get("expiration_ymd")),
            "expiration_ymd": position.get("expiration_ymd"),
            "currency": normalize_currency(position.get("currency")),
            "multiplier": position.get("multiplier"),
            "contracts": int(total_contracts or 0),
            "contracts_open": current_contracts,
            "contracts_closed": int(closed_contracts or 0),
            "status": "open" if current_contracts > 0 else "close",
            "source_event_id": legacy_fields.get("source_event_id") or _source_event_id_from_position(position),
            "last_close_event_id": legacy_fields.get("last_close_event_id") or _last_close_event_id_from_position(position),
        }
    )
    if fields.get("position_id") in (None, ""):
        fields["position_id"] = str(position.get("position_key") or "")
    if current_contracts > 0:
        fields.pop("closed_at", None)
    return fields


def _should_materialize_compat_position(position: dict[str, Any], legacy_row: dict[str, Any] | None) -> bool:
    if legacy_row is not None:
        return True
    if int(position.get("baseline_contracts") or 0) > 0:
        return True
    if int(position.get("current_contracts") or 0) > 0:
        return True
    return bool(position.get("applied_events"))


def load_option_positions_v2_records(*, base: Path | None = None, repo: Any) -> CompatPositionRecords:
    state = refresh_option_positions_v2_state(base=base, repo=repo)
    legacy_rows = _list_legacy_position_lots(repo)
    by_key, _by_record_id = _legacy_row_maps(legacy_rows)
    matched_record_ids: set[str] = set()
    records: list[dict[str, Any]] = []
    for position in state.projection.get("positions") or []:
        if not isinstance(position, dict):
            continue
        legacy_row = by_key.get(str(position.get("position_key") or ""))
        if not _should_materialize_compat_position(position, legacy_row):
            continue
        if legacy_row is not None:
            record_id = str(legacy_row.get("record_id") or "").strip()
            if record_id:
                matched_record_ids.add(record_id)
        records.append(
            {
                "record_id": _compat_record_id(position, legacy_row),
                "fields": _compat_fields_from_projection(position, legacy_row),
            }
        )
    for item in legacy_rows:
        record_id = str(item.get("record_id") or "").strip()
        fields = item.get("fields") or {}
        if record_id in matched_record_ids or not isinstance(fields, dict):
            continue
        if normalize_status(fields.get("status")) == "close":
            records.append({"record_id": record_id, "fields": dict(fields)})
    records.sort(
        key=lambda item: (
            str(((item.get("fields") or {}).get("account") or "")),
            str(((item.get("fields") or {}).get("symbol") or "")),
            str(item.get("record_id") or ""),
        )
    )
    return CompatPositionRecords(state=state, records=records)


def reconcile_option_positions_snapshot(
    *,
    base: Path | None = None,
    repo: Any,
    verification_snapshot: dict[str, Any],
) -> dict[str, Any]:
    snapshot = normalize_position_snapshot(verification_snapshot)
    if str(snapshot.get("snapshot_type") or "") != SNAPSHOT_TYPE_VERIFICATION:
        raise ValueError("verification snapshot is required for reconciliation")
    resolved_base = resolve_option_positions_v2_base(base=base, repo=repo)
    state = refresh_option_positions_v2_state(base=resolved_base, repo=repo)
    report = reconcile_snapshot_against_projection(snapshot, state.projection)
    persisted_snapshots = _load_persisted_snapshots(resolved_base)
    baseline_snapshot = _latest_snapshot_by_type(persisted_snapshots, SNAPSHOT_TYPE_BASELINE)
    verification_snapshots = [
        item for item in persisted_snapshots if str(item.get("snapshot_type") or "") == SNAPSHOT_TYPE_VERIFICATION
    ]
    if baseline_snapshot is None:
        baseline_snapshot = _native_baseline_snapshot(
            legacy_events=_list_legacy_trade_events(repo),
            legacy_rows=_list_legacy_position_lots(repo),
        )
    verification_snapshots = [
        item
        for item in verification_snapshots
        if str(item.get("snapshot_id") or "").strip() != str(snapshot.get("snapshot_id") or "").strip()
    ]
    verification_snapshots.append(snapshot)
    option_positions_v2_repo.replace_position_snapshots(resolved_base, [baseline_snapshot, *verification_snapshots])
    option_positions_v2_repo.write_reconciliation_report(resolved_base, report)
    return report
