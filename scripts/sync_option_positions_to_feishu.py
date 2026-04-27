#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.feishu_bitable import (
    bitable_create_record,
    bitable_fields,
    bitable_list_records,
    bitable_update_record,
    get_tenant_access_token,
)
from scripts.option_positions_core.domain import (
    normalize_account,
    normalize_broker,
    normalize_close_type,
    normalize_currency,
    normalize_option_type,
    normalize_side,
    normalize_status,
    now_ms,
)
from scripts.option_positions_core.service import load_table_ref
from src.application.option_positions_facade import resolve_option_positions_repo


SYNC_META_KEYS = {
    "feishu_record_id",
    "feishu_sync_hash",
    "feishu_last_synced_at_ms",
}

INTEGER_PAYLOAD_FIELDS = {
    "contracts",
    "contracts_open",
    "contracts_closed",
    "underlying_share_locked",
    "opened_at",
    "closed_at",
    "last_action_at",
    "last_synced_at",
}

NUMBER_PAYLOAD_FIELDS = {
    "strike",
    "premium",
    "cash_secured_amount",
}

SCHEMA_NUMERIC_TYPE_HINTS = {
    "number",
    "currency",
    "percent",
    "rating",
    "progress",
}

SCHEMA_INTEGER_FIELD_NAMES = {
    "contracts",
    "contracts_open",
    "contracts_closed",
    "underlying_share_locked",
    "opened_at",
    "closed_at",
    "last_action_at",
    "last_synced_at",
    "expiration",
}


@dataclass(frozen=True)
class SyncCandidate:
    record_id: str
    fields: dict[str, Any]


def normalize_local_fields(fields: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(fields)
    normalized["broker"] = normalize_broker(fields.get("broker"))
    normalized["account"] = normalize_account(fields.get("account"))
    normalized["symbol"] = str(fields.get("symbol") or "").strip().upper() or None
    normalized["option_type"] = normalize_option_type(fields.get("option_type")) or None
    normalized["side"] = normalize_side(fields.get("side")) or None
    normalized["status"] = normalize_status(fields.get("status")) or None
    normalized["currency"] = normalize_currency(fields.get("currency")) or None
    if fields.get("close_type"):
        normalized["close_type"] = normalize_close_type(fields.get("close_type"))
    return normalized


def _coerce_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except Exception:
        return None


def _coerce_number(value: Any) -> float | int | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except Exception:
        return None
    if number.is_integer():
        return int(number)
    return number


def normalize_payload_types(payload: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in payload.items():
        if key in INTEGER_PAYLOAD_FIELDS:
            coerced = _coerce_int(value)
            normalized[key] = coerced if coerced is not None else value
            continue
        if key in NUMBER_PAYLOAD_FIELDS:
            coerced = _coerce_number(value)
            normalized[key] = coerced if coerced is not None else value
            continue
        normalized[key] = value
    return normalized


def _schema_type_hints(field_meta: dict[str, Any]) -> set[str]:
    hints: set[str] = set()
    for key in ("type", "field_type", "ui_type", "property_type", "data_type"):
        value = field_meta.get(key)
        if value is None:
            continue
        text = str(value).strip().lower()
        if text:
            hints.add(text)
    return hints


def normalize_payload_types_by_schema(payload: dict[str, Any], schema_fields: list[dict[str, Any]]) -> dict[str, Any]:
    schema_by_name = {
        str(item.get("field_name") or "").strip(): item
        for item in schema_fields
        if str(item.get("field_name") or "").strip()
    }
    normalized = dict(payload)
    for key, value in payload.items():
        field_meta = schema_by_name.get(key)
        if not field_meta:
            continue
        hints = _schema_type_hints(field_meta)
        if not hints:
            continue
        if any(hint in SCHEMA_NUMERIC_TYPE_HINTS for hint in hints):
            if key in SCHEMA_INTEGER_FIELD_NAMES:
                coerced = _coerce_int(value)
            else:
                coerced = _coerce_number(value)
            if coerced is not None:
                normalized[key] = coerced
    return normalized


def build_feishu_payload(record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_local_fields(fields)
    payload: dict[str, Any] = {
        "position_id": normalized.get("position_id"),
        "source_event_id": normalized.get("source_event_id"),
        "broker": normalized.get("broker"),
        "account": normalized.get("account"),
        "symbol": normalized.get("symbol"),
        "option_type": normalized.get("option_type"),
        "side": normalized.get("side"),
        "contracts": normalized.get("contracts"),
        "contracts_open": normalized.get("contracts_open"),
        "contracts_closed": normalized.get("contracts_closed"),
        "currency": normalized.get("currency"),
        "strike": normalized.get("strike"),
        "expiration": normalized.get("expiration"),
        "premium": normalized.get("premium"),
        "cash_secured_amount": normalized.get("cash_secured_amount"),
        "underlying_share_locked": normalized.get("underlying_share_locked"),
        "status": normalized.get("status"),
        "opened_at": normalized.get("opened_at"),
        "closed_at": normalized.get("closed_at"),
        "last_action_at": normalized.get("last_action_at"),
        "close_type": normalized.get("close_type"),
        "close_reason": normalized.get("close_reason"),
        "last_close_event_id": normalized.get("last_close_event_id"),
        "note": normalized.get("note"),
        "local_record_id": record_id,
        "last_synced_at": int(now_ms()),
    }
    filtered = {key: value for key, value in payload.items() if value is not None and value != ""}
    return normalize_payload_types(filtered)


def filter_payload_for_schema(payload: dict[str, Any], allowed_fields: set[str]) -> dict[str, Any]:
    if not allowed_fields:
        return payload
    return {key: value for key, value in payload.items() if key in allowed_fields}


def payload_hash(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def extract_remote_record_id(item: dict[str, Any]) -> str:
    return str(item.get("record_id") or item.get("id") or "").strip()


def _match_remote_record_by_key(
    *,
    local_value: str,
    remote_key: str,
    remote_records: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    needle = str(local_value or "").strip()
    if not needle:
        return None, None

    matches: list[str] = []
    for item in remote_records:
        record_id = extract_remote_record_id(item)
        remote_fields = item.get("fields") or {}
        if not record_id or not isinstance(remote_fields, dict):
            continue
        if str(remote_fields.get(remote_key) or "").strip() == needle:
            matches.append(record_id)

    unique_ids = sorted(set(matches))
    if not unique_ids:
        return None, None
    if len(unique_ids) > 1:
        return None, f"conflict: duplicate remote rows by {remote_key} {unique_ids}"
    return unique_ids[0], remote_key


def match_remote_record(local_record_id: str, fields: dict[str, Any], remote_records: list[dict[str, Any]]) -> tuple[str | None, str]:
    local_position_id = str(fields.get("position_id") or "").strip()
    local_source_event_id = str(fields.get("source_event_id") or "").strip()
    for value, key in (
        (local_record_id, "local_record_id"),
        (local_source_event_id, "source_event_id"),
        (local_position_id, "position_id"),
    ):
        matched_record_id, reason = _match_remote_record_by_key(
            local_value=value,
            remote_key=key,
            remote_records=remote_records,
        )
        if matched_record_id is not None:
            return matched_record_id, str(reason or key)
        if reason:
            return None, reason
    return None, "no_remote_match"


def finalize_outgoing_payload(payload: dict[str, Any], schema_fields: list[dict[str, Any]]) -> dict[str, Any]:
    return normalize_payload_types_by_schema(payload, schema_fields)


def sync_meta_patch(fields: dict[str, Any], *, feishu_record_id: str, sync_hash: str, synced_at_ms: int) -> dict[str, Any]:
    patched = dict(fields)
    patched["feishu_record_id"] = feishu_record_id
    patched["feishu_sync_hash"] = sync_hash
    patched["feishu_last_synced_at_ms"] = int(synced_at_ms)
    return patched


def select_candidates(
    records: list[dict[str, Any]],
    *,
    only_record_id: str | None,
    only_open: bool,
    since_updated_ms: int | None,
    limit: int | None,
) -> list[SyncCandidate]:
    out: list[SyncCandidate] = []
    for item in records:
        record_id = str(item.get("record_id") or "").strip()
        fields = item.get("fields") or {}
        if not record_id or not isinstance(fields, dict):
            continue
        if only_record_id and record_id != only_record_id:
            continue
        if only_open and normalize_status(fields.get("status")) != "open":
            continue
        synced_at = fields.get("feishu_last_synced_at_ms")
        if since_updated_ms is not None and synced_at is not None and int(synced_at) < int(since_updated_ms):
            continue
        out.append(SyncCandidate(record_id=record_id, fields=dict(fields)))
        if limit is not None and len(out) >= limit:
            break
    return out


def summarize_result(action_rows: list[dict[str, Any]]) -> dict[str, int]:
    summary = {"scanned": len(action_rows), "create": 0, "update": 0, "skip": 0, "conflict": 0, "failed": 0}
    for row in action_rows:
        action = str(row.get("action") or "")
        if action in summary:
            summary[action] += 1
    return summary


def sync_option_positions(
    *,
    repo: Any,
    data_config: Path,
    apply_mode: bool,
    only_record_id: str | None = None,
    only_open: bool = False,
    since_updated_ms: int | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    table_ref = load_table_ref(data_config)
    primary_repo = getattr(repo, "primary_repo", repo)
    list_position_lots = getattr(primary_repo, "list_position_lots", None)
    if not callable(list_position_lots):
        raise SystemExit("option_positions repo does not support list_position_lots")
    update_position_lot_fields = getattr(primary_repo, "update_position_lot_fields", None)
    if apply_mode and not callable(update_position_lot_fields):
        raise SystemExit("option_positions repo does not support update_position_lot_fields")

    local_records = list_position_lots()
    candidates = select_candidates(
        local_records,
        only_record_id=only_record_id,
        only_open=bool(only_open),
        since_updated_ms=since_updated_ms,
        limit=limit,
    )

    token = get_tenant_access_token(table_ref.app_id, table_ref.app_secret)
    schema_fields = bitable_fields(token, table_ref.app_token, table_ref.table_id)
    allowed_fields = {str(item.get("field_name") or "").strip() for item in schema_fields if str(item.get("field_name") or "").strip()}
    remote_records = bitable_list_records(token, table_ref.app_token, table_ref.table_id, page_size=500)

    action_rows: list[dict[str, Any]] = []
    for candidate in candidates:
        feishu_record_id = str(candidate.fields.get("feishu_record_id") or "").strip()
        outgoing_payload = filter_payload_for_schema(build_feishu_payload(candidate.record_id, candidate.fields), allowed_fields)
        outgoing_payload = finalize_outgoing_payload(outgoing_payload, schema_fields)
        outgoing_hash = payload_hash(outgoing_payload)
        existing_hash = str(candidate.fields.get("feishu_sync_hash") or "").strip()
        row: dict[str, Any] = {
            "record_id": candidate.record_id,
            "position_id": candidate.fields.get("position_id"),
            "feishu_record_id": feishu_record_id or None,
            "payload_hash": outgoing_hash,
        }

        if not outgoing_payload:
            row["action"] = "skip"
            row["reason"] = "empty_payload_after_schema_filter"
            action_rows.append(row)
            continue

        if feishu_record_id and existing_hash == outgoing_hash:
            row["action"] = "skip"
            row["reason"] = "payload_unchanged"
            action_rows.append(row)
            continue

        if not feishu_record_id:
            matched_record_id, match_reason = match_remote_record(candidate.record_id, candidate.fields, remote_records)
            if matched_record_id is None and match_reason.startswith("conflict:"):
                row["action"] = "conflict"
                row["reason"] = match_reason
                row["match_key"] = "duplicate_remote_business_key"
                action_rows.append(row)
                continue
            if matched_record_id:
                feishu_record_id = matched_record_id
                row["feishu_record_id"] = matched_record_id
                row["match_reason"] = match_reason

        if not feishu_record_id:
            row["action"] = "create"
            row["reason"] = "missing_feishu_record_id"
            if apply_mode:
                try:
                    created = bitable_create_record(token, table_ref.app_token, table_ref.table_id, outgoing_payload)
                    created_record = created.get("record") if isinstance(created.get("record"), dict) else created
                    feishu_record_id = extract_remote_record_id(created_record if isinstance(created_record, dict) else {})
                    if not feishu_record_id:
                        raise ValueError("feishu create response missing record_id")
                    synced_at_ms = int(now_ms())
                    patched_fields = sync_meta_patch(
                        candidate.fields,
                        feishu_record_id=feishu_record_id,
                        sync_hash=outgoing_hash,
                        synced_at_ms=synced_at_ms,
                    )
                    update_position_lot_fields(candidate.record_id, patched_fields)
                    remote_records.append({"record_id": feishu_record_id, "fields": dict(outgoing_payload)})
                    row["feishu_record_id"] = feishu_record_id
                    row["synced_at_ms"] = synced_at_ms
                except Exception as exc:
                    row["action"] = "failed"
                    row["reason"] = f"create_failed: {exc}"
            action_rows.append(row)
            continue

        row["action"] = "update"
        row["reason"] = "has_feishu_record_id"
        if apply_mode:
            try:
                bitable_update_record(token, table_ref.app_token, table_ref.table_id, feishu_record_id, outgoing_payload)
                synced_at_ms = int(now_ms())
                patched_fields = sync_meta_patch(
                    candidate.fields,
                    feishu_record_id=feishu_record_id,
                    sync_hash=outgoing_hash,
                    synced_at_ms=synced_at_ms,
                )
                update_position_lot_fields(candidate.record_id, patched_fields)
                row["synced_at_ms"] = synced_at_ms
            except Exception as exc:
                row["action"] = "failed"
                row["reason"] = f"update_failed: {exc}"
        action_rows.append(row)

    return action_rows


def sync_single_option_position_record(*, repo: Any, data_config: Path, record_id: str, apply_mode: bool = True) -> dict[str, Any]:
    rows = sync_option_positions(
        repo=repo,
        data_config=data_config,
        apply_mode=apply_mode,
        only_record_id=record_id,
        only_open=False,
        since_updated_ms=None,
        limit=1,
    )
    if not rows:
        return {"record_id": record_id, "action": "skip", "reason": "record_not_found"}
    return rows[0]


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync local option_positions SQLite lots to Feishu bitable")
    parser.add_argument("--data-config", default=None, help="portfolio data config path; auto-resolves when omitted")
    parser.add_argument("--apply", action="store_true", help="apply changes to Feishu and persist local sync metadata")
    parser.add_argument("--dry-run", action="store_true", help="preview actions without writing to Feishu")
    parser.add_argument("--limit", type=int, default=None, help="maximum number of local lots to inspect")
    parser.add_argument("--only-record-id", default=None, help="sync a single local record_id")
    parser.add_argument("--only-open", action="store_true", help="only sync open positions")
    parser.add_argument("--since-updated-ms", type=int, default=None, help="only include rows last synced before this ms watermark")
    parser.add_argument("--verbose", action="store_true", help="print payload details")
    args = parser.parse_args()

    apply_mode = bool(args.apply)
    dry_run = not apply_mode or bool(args.dry_run)

    base = Path(__file__).resolve().parents[1]
    data_config, repo = resolve_option_positions_repo(base=base, data_config=args.data_config)
    action_rows = sync_option_positions(
        repo=repo,
        data_config=data_config,
        apply_mode=apply_mode,
        only_record_id=args.only_record_id,
        only_open=bool(args.only_open),
        since_updated_ms=args.since_updated_ms,
        limit=args.limit,
    )

    table_ref = load_table_ref(data_config)
    token = get_tenant_access_token(table_ref.app_id, table_ref.app_secret)
    schema_fields = bitable_fields(token, table_ref.app_token, table_ref.table_id)
    allowed_fields = {str(item.get("field_name") or "").strip() for item in schema_fields if str(item.get("field_name") or "").strip()}
    local_records = getattr(getattr(repo, "primary_repo", repo), "list_position_lots")()
    candidates = select_candidates(
        local_records,
        only_record_id=args.only_record_id,
        only_open=bool(args.only_open),
        since_updated_ms=args.since_updated_ms,
        limit=args.limit,
    )
    for row in action_rows:
        printable = dict(row)
        if args.verbose:
            source = next((candidate for candidate in candidates if candidate.record_id == row.get("record_id")), None)
            if source is not None:
                payload = filter_payload_for_schema(build_feishu_payload(source.record_id, source.fields), allowed_fields)
                payload = finalize_outgoing_payload(payload, schema_fields)
                printable["payload"] = payload
        print(json.dumps(printable, ensure_ascii=False, sort_keys=True))

    summary = summarize_result(action_rows)
    summary["mode_apply"] = int(apply_mode)
    summary["mode_dry_run"] = int(dry_run)
    print(json.dumps({"summary": summary}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
