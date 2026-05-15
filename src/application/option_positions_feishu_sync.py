from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.application.config_loader import load_config
from src.infrastructure.feishu_bitable import (
    FeishuAuthError,
    bitable_create_record,
    bitable_delete_record,
    bitable_fields,
    bitable_list_records,
    bitable_update_record,
    get_tenant_access_token,
)
from domain.domain.option_position_lots import normalize_account, normalize_broker, normalize_option_type, normalize_side, now_ms
from src.application.option_positions_service import (
    load_table_ref,
    require_option_positions_read_repo,
    require_option_positions_sync_meta_repo,
)
from src.application.option_positions_sync_config import (
    apply_option_positions_runtime_config,
    effective_option_positions_sync_to_feishu_enabled,
)
from src.application.option_positions_feishu_sync_receipt import (
    persist_option_positions_feishu_sync_last_run,
    safe_send_option_positions_feishu_sync_receipt,
    skipped_option_positions_feishu_sync_receipt,
)
from src.application.option_positions_facade import (
    canonicalize_option_position_fields,
    load_canonical_option_position_records,
    resolve_option_positions_repo,
)

SYNC_HASH_EXCLUDED_FIELDS = {
    "last_synced_at",
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
    return canonicalize_option_position_fields(fields)


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


def payload_hash(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def sync_payload_hash(payload: dict[str, Any]) -> str:
    comparable = {key: value for key, value in payload.items() if key not in SYNC_HASH_EXCLUDED_FIELDS}
    return payload_hash(comparable)


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


def _same_business_lot(fields: dict[str, Any], remote_fields: dict[str, Any]) -> bool:
    local_fields = normalize_local_fields(fields)
    local_account = normalize_account(local_fields.get("account"))
    remote_account = normalize_account(remote_fields.get("account"))
    if local_account != remote_account:
        return False
    return (
        normalize_broker(local_fields.get("broker")) == normalize_broker(remote_fields.get("broker"))
        and str(local_fields.get("symbol") or "").strip().upper() == str(remote_fields.get("symbol") or "").strip().upper()
        and normalize_option_type(local_fields.get("option_type")) == normalize_option_type(remote_fields.get("option_type"))
        and normalize_side(local_fields.get("side")) == normalize_side(remote_fields.get("side"))
    )


def _match_remote_record_by_position_id_and_account(
    *,
    fields: dict[str, Any],
    remote_records: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    local_position_id = str(fields.get("position_id") or "").strip()
    if not local_position_id:
        return None, None

    matches: list[str] = []
    for item in remote_records:
        record_id = extract_remote_record_id(item)
        remote_fields = item.get("fields") or {}
        if not record_id or not isinstance(remote_fields, dict):
            continue
        if str(remote_fields.get("position_id") or "").strip() != local_position_id:
            continue
        if _same_business_lot(fields, remote_fields):
            matches.append(record_id)

    unique_ids = sorted(set(matches))
    if not unique_ids:
        return None, None
    if len(unique_ids) > 1:
        return None, f"conflict: duplicate remote rows by account+position_id {unique_ids}"
    return unique_ids[0], "account+position_id"


def match_remote_record(local_record_id: str, fields: dict[str, Any], remote_records: list[dict[str, Any]]) -> tuple[str | None, str]:
    local_source_event_id = str(fields.get("source_event_id") or "").strip()
    for value, key in (
        (local_record_id, "local_record_id"),
        (local_source_event_id, "source_event_id"),
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
    matched_record_id, reason = _match_remote_record_by_position_id_and_account(
        fields=fields,
        remote_records=remote_records,
    )
    if matched_record_id is not None:
        return matched_record_id, str(reason or "account+position_id")
    if reason:
        return None, reason
    return None, "no_remote_match"


def build_outgoing_payload(record_id: str, fields: dict[str, Any], schema_fields: list[dict[str, Any]]) -> dict[str, Any]:
    payload = build_feishu_payload(record_id, fields)
    allowed_fields = {
        str(item.get("field_name") or "").strip()
        for item in schema_fields
        if str(item.get("field_name") or "").strip()
    }
    if allowed_fields:
        payload = {key: value for key, value in payload.items() if key in allowed_fields}
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
        fields = normalize_local_fields(item.get("fields") or {})
        if not record_id or not isinstance(fields, dict):
            continue
        if only_record_id and record_id != only_record_id:
            continue
        if only_open and str(fields.get("status") or "") != "open":
            continue
        synced_at = fields.get("feishu_last_synced_at_ms")
        if since_updated_ms is not None and synced_at is not None and int(synced_at) >= int(since_updated_ms):
            continue
        out.append(SyncCandidate(record_id=record_id, fields=dict(fields)))
        if limit is not None and len(out) >= limit:
            break
    return out


def summarize_result(action_rows: list[dict[str, Any]]) -> dict[str, int]:
    summary = {"scanned": len(action_rows), "create": 0, "update": 0, "delete": 0, "skip": 0, "conflict": 0, "failed": 0}
    for row in action_rows:
        action = str(row.get("action") or "")
        if action in summary:
            summary[action] += 1
    return summary


def build_sync_run_result(
    *,
    mode: str,
    dry_run: bool,
    data_config: Path,
    runtime_config_path: Path | None,
    filters: dict[str, Any],
    action_rows: list[dict[str, Any]],
    table_ref_hash: str | None,
    started_at: str,
    finished_at: str,
    error: BaseException | None = None,
) -> dict[str, Any]:
    summary = summarize_result(action_rows)
    if error is not None and summary.get("failed", 0) <= 0:
        summary["failed"] = 1
    status = _sync_status_from_summary(summary, error=error)
    result: dict[str, Any] = {
        "schema_kind": "option_positions_feishu_sync_run",
        "schema_version": "1.0",
        "mode": str(mode),
        "status": status,
        "dry_run": bool(dry_run),
        "data_config_path": str(Path(data_config).resolve()),
        "runtime_config_path": str(runtime_config_path.resolve()) if runtime_config_path is not None else None,
        "table_ref_hash": table_ref_hash,
        "filters": dict(filters),
        "summary": summary,
        "rows": [dict(row) for row in action_rows],
        "started_at": started_at,
        "finished_at": finished_at,
    }
    if error is not None:
        result["error"] = {"type": type(error).__name__, "message": str(error)}
    return result


def finalize_sync_run_result(
    *,
    state_base: Path,
    runtime_config: dict[str, Any] | None,
    dry_run: bool,
    no_send: bool,
    result: dict[str, Any],
) -> dict[str, Any]:
    if no_send:
        receipt = skipped_option_positions_feishu_sync_receipt(result=result, reason="skipped_no_send")
    else:
        receipt = safe_send_option_positions_feishu_sync_receipt(
            base=state_base,
            config=runtime_config,
            dry_run=dry_run,
            result=result,
        )
    result_with_receipt = dict(result)
    result_with_receipt["receipt"] = receipt
    persist_option_positions_feishu_sync_last_run(base=state_base, result=result_with_receipt)
    return result_with_receipt


def _with_table_token(table_ref: Any, fn: Any) -> Any:
    app_id = str(table_ref.app_id)
    app_secret = str(table_ref.app_secret)
    token = get_tenant_access_token(app_id, app_secret)
    try:
        return fn(token)
    except FeishuAuthError:
        refreshed = get_tenant_access_token(app_id, app_secret, force_refresh=True)
        return fn(refreshed)


def can_prune_remote_missing_local(
    *,
    prune_remote_missing_local: bool,
    only_record_id: str | None,
    only_open: bool,
    since_updated_ms: int | None,
    limit: int | None,
) -> bool:
    return bool(
        prune_remote_missing_local
        and not only_record_id
        and not only_open
        and since_updated_ms is None
        and limit is None
    )


def sync_writes_enabled(
    data_config: Path,
    *,
    runtime_config: dict[str, Any] | None = None,
    repo: Any | None = None,
) -> bool:
    return effective_option_positions_sync_to_feishu_enabled(
        data_config=data_config,
        runtime_config=runtime_config,
        repo=repo,
    )


def sync_option_positions(
    *,
    repo: Any,
    data_config: Path,
    base: Path | None = None,
    apply_mode: bool,
    only_record_id: str | None = None,
    only_open: bool = False,
    since_updated_ms: int | None = None,
    limit: int | None = None,
    prune_remote_missing_local: bool = False,
    runtime_config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    writes_enabled = sync_writes_enabled(data_config, runtime_config=runtime_config, repo=repo)
    effective_apply_mode = bool(apply_mode and writes_enabled)
    table_ref = load_table_ref(data_config)
    update_position_lot_fields: Callable[[str, dict[str, Any]], None] | None
    try:
        if effective_apply_mode:
            primary_repo = require_option_positions_sync_meta_repo(repo)
            update_position_lot_fields = primary_repo.update_position_lot_fields
        else:
            primary_repo = require_option_positions_read_repo(repo)
            update_position_lot_fields = None
    except TypeError as exc:
        raise SystemExit(str(exc))

    local_records = load_canonical_option_position_records(repo, base=base)
    candidates = select_candidates(
        local_records,
        only_record_id=only_record_id,
        only_open=bool(only_open),
        since_updated_ms=since_updated_ms,
        limit=limit,
    )

    schema_fields = _with_table_token(
        table_ref,
        lambda token: bitable_fields(token, table_ref.app_token, table_ref.table_id),
    )
    remote_records = _with_table_token(
        table_ref,
        lambda token: bitable_list_records(token, table_ref.app_token, table_ref.table_id, page_size=500),
    )

    action_rows: list[dict[str, Any]] = []
    for candidate in candidates:
        feishu_record_id = str(candidate.fields.get("feishu_record_id") or "").strip()
        outgoing_payload = build_outgoing_payload(candidate.record_id, candidate.fields, schema_fields)
        outgoing_hash = sync_payload_hash(outgoing_payload)
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
            if apply_mode and not writes_enabled:
                row["action"] = "skip"
                row["reason"] = "sync_to_feishu_disabled"
            elif effective_apply_mode:
                try:
                    if update_position_lot_fields is None:
                        raise TypeError("option_positions repo does not satisfy sync metadata repository interface")
                    created = _with_table_token(
                        table_ref,
                        lambda token: bitable_create_record(token, table_ref.app_token, table_ref.table_id, outgoing_payload),
                    )
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
        if apply_mode and not writes_enabled:
            row["action"] = "skip"
            row["reason"] = "sync_to_feishu_disabled"
        elif effective_apply_mode:
            try:
                if update_position_lot_fields is None:
                    raise TypeError("option_positions repo does not satisfy sync metadata repository interface")
                _with_table_token(
                    table_ref,
                    lambda token: bitable_update_record(token, table_ref.app_token, table_ref.table_id, feishu_record_id, outgoing_payload),
                )
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

    if can_prune_remote_missing_local(
        prune_remote_missing_local=prune_remote_missing_local,
        only_record_id=only_record_id,
        only_open=only_open,
        since_updated_ms=since_updated_ms,
        limit=limit,
    ):
        local_record_ids = {candidate.record_id for candidate in candidates}
        for item in remote_records:
            remote_record_id = extract_remote_record_id(item)
            remote_fields = item.get("fields") or {}
            if not remote_record_id or not isinstance(remote_fields, dict):
                continue
            remote_local_record_id = str(remote_fields.get("local_record_id") or "").strip()
            if not remote_local_record_id:
                continue
            if remote_local_record_id in local_record_ids:
                continue
            row = {
                "record_id": None,
                "remote_record_id": remote_record_id,
                "remote_local_record_id": remote_local_record_id,
                "action": "delete",
                "reason": "remote_local_record_missing_from_local_projection",
            }
            if apply_mode and not writes_enabled:
                row["action"] = "skip"
                row["reason"] = "sync_to_feishu_disabled"
            elif effective_apply_mode:
                try:
                    _with_table_token(
                        table_ref,
                        lambda token: bitable_delete_record(token, table_ref.app_token, table_ref.table_id, remote_record_id),
                    )
                except Exception as exc:
                    row["action"] = "failed"
                    row["reason"] = f"delete_failed: {exc}"
            action_rows.append(row)

    return action_rows


def sync_single_option_position_record(*, repo: Any, data_config: Path, record_id: str, apply_mode: bool = True) -> dict[str, Any]:
    rows = sync_option_positions(
        repo=repo,
        data_config=data_config,
        base=Path(data_config).resolve().parent,
        apply_mode=apply_mode,
        only_record_id=record_id,
        only_open=False,
        since_updated_ms=None,
        limit=1,
    )
    if not rows:
        return {"record_id": record_id, "action": "skip", "reason": "record_not_found"}
    return rows[0]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sync_status_from_summary(summary: dict[str, Any], *, error: BaseException | None) -> str:
    if error is not None:
        return "failed"
    failed = int(summary.get("failed") or 0)
    conflict = int(summary.get("conflict") or 0)
    changed = int(summary.get("create") or 0) + int(summary.get("update") or 0) + int(summary.get("delete") or 0)
    skipped = int(summary.get("skip") or 0)
    if failed > 0:
        return "partial_failed" if changed > 0 or conflict > 0 or skipped > 0 else "failed"
    if conflict > 0:
        return "partial_conflict" if changed > 0 or skipped > 0 else "conflict"
    if changed > 0:
        return "applied"
    return "noop"


def _table_ref_hash(data_config: Path) -> str | None:
    try:
        table_ref = load_table_ref(data_config)
    except Exception:
        return None
    raw = json.dumps(
        {
            "app_token": getattr(table_ref, "app_token", None),
            "table_id": getattr(table_ref, "table_id", None),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _receipt_summary(receipt: Any) -> dict[str, Any] | None:
    if not isinstance(receipt, dict):
        return None
    return {
        "status": receipt.get("status"),
        "reason": receipt.get("reason"),
        "delivery_confirmed": bool(receipt.get("delivery_confirmed")),
        "message_id": receipt.get("message_id"),
        "error_code": receipt.get("error_code"),
        "attempt_count": receipt.get("attempt_count"),
        "receipt_key": receipt.get("receipt_key"),
    }


def _filters_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "only_record_id": args.only_record_id,
        "only_open": bool(args.only_open),
        "since_updated_ms": args.since_updated_ms,
        "limit": args.limit,
        "prune_remote_missing_local": bool(args.prune_remote_missing_local),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Sync local option_positions SQLite lots to Feishu bitable")
    parser.add_argument("--config", default=None, help="runtime config path; when provided, portfolio.data_config and runtime sync switch are used")
    parser.add_argument("--data-config", default=None, help="portfolio data config path; auto-resolves when omitted")
    parser.add_argument("--apply", action="store_true", help="apply changes to Feishu and persist local sync metadata")
    parser.add_argument("--dry-run", action="store_true", help="preview actions without writing to Feishu")
    parser.add_argument("--limit", type=int, default=None, help="maximum number of local lots to inspect")
    parser.add_argument("--only-record-id", default=None, help="sync a single local record_id")
    parser.add_argument("--only-open", action="store_true", help="only sync open positions")
    parser.add_argument("--since-updated-ms", type=int, default=None, help="only include rows last synced before this ms watermark")
    parser.add_argument(
        "--prune-remote-missing-local",
        action="store_true",
        help="delete remote rows whose local_record_id no longer exists locally; disabled by default",
    )
    parser.add_argument("--no-send", action="store_true", help="do not send sync receipt notifications")
    parser.add_argument("--verbose", action="store_true", help="print payload details")
    args = parser.parse_args(argv)

    if args.apply and args.dry_run:
        raise SystemExit("--apply and --dry-run cannot be used together")

    apply_mode = bool(args.apply)
    dry_run = not apply_mode or bool(args.dry_run)

    base = Path(__file__).resolve().parents[2]
    runtime_config: dict[str, Any] | None = None
    runtime_config_path: Path | None = None
    data_config_ref = args.data_config
    if args.config:
        cfg_path = Path(args.config)
        if not cfg_path.is_absolute():
            cfg_path = (base / cfg_path).resolve()
        runtime_config_path = cfg_path
        runtime_config = load_config(base=base, config_path=cfg_path, is_scheduled=False, log=lambda msg: print(msg, file=sys.stderr))
        portfolio_cfg = runtime_config.get("portfolio") if isinstance(runtime_config.get("portfolio"), dict) else {}
        if data_config_ref is None or not str(data_config_ref).strip():
            data_config_ref = portfolio_cfg.get("data_config") if isinstance(portfolio_cfg, dict) else None

    data_config, repo = resolve_option_positions_repo(base=base, data_config=data_config_ref)
    if runtime_config is not None:
        apply_option_positions_runtime_config(repo, runtime_config)
    state_base = data_config.resolve().parent
    sync_state_base = base if runtime_config is not None else state_base
    filters = _filters_from_args(args)
    table_hash = _table_ref_hash(data_config)
    started_at = _utc_now()
    action_rows: list[dict[str, Any]] = []
    try:
        action_rows = sync_option_positions(
            repo=repo,
            data_config=data_config,
            base=state_base,
            apply_mode=apply_mode,
            only_record_id=args.only_record_id,
            only_open=bool(args.only_open),
            since_updated_ms=args.since_updated_ms,
            limit=args.limit,
            prune_remote_missing_local=bool(args.prune_remote_missing_local),
            runtime_config=runtime_config,
        )
    except Exception as exc:
        result = build_sync_run_result(
            mode="apply" if apply_mode else "dry_run",
            dry_run=dry_run,
            data_config=data_config,
            runtime_config_path=runtime_config_path,
            filters=filters,
            action_rows=action_rows,
            table_ref_hash=table_hash,
            started_at=started_at,
            finished_at=_utc_now(),
            error=exc,
        )
        result = finalize_sync_run_result(
            state_base=sync_state_base,
            runtime_config=runtime_config,
            dry_run=dry_run,
            no_send=bool(args.no_send),
            result=result,
        )
        failed_summary = dict(result.get("summary") or {})
        failed_summary["mode_apply"] = int(apply_mode)
        failed_summary["mode_dry_run"] = int(dry_run)
        summary_payload = {"summary": failed_summary, "receipt": _receipt_summary(result.get("receipt"))}
        print(json.dumps(summary_payload, ensure_ascii=False, sort_keys=True))
        raise

    schema_fields: list[dict[str, Any]] = []
    candidates: list[SyncCandidate] = []
    if args.verbose:
        table_ref = load_table_ref(data_config)
        schema_fields = _with_table_token(
            table_ref,
            lambda token: bitable_fields(token, table_ref.app_token, table_ref.table_id),
        )
        local_records = load_canonical_option_position_records(repo, base=state_base)
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
                payload = build_outgoing_payload(source.record_id, source.fields, schema_fields)
                printable["payload"] = payload
        print(json.dumps(printable, ensure_ascii=False, sort_keys=True))

    result = build_sync_run_result(
        mode="apply" if apply_mode else "dry_run",
        dry_run=dry_run,
        data_config=data_config,
        runtime_config_path=runtime_config_path,
        filters=filters,
        action_rows=action_rows,
        table_ref_hash=table_hash,
        started_at=started_at,
        finished_at=_utc_now(),
    )
    result = finalize_sync_run_result(
        state_base=sync_state_base,
        runtime_config=runtime_config,
        dry_run=dry_run,
        no_send=bool(args.no_send),
        result=result,
    )
    summary = dict(result.get("summary") or {})
    summary["mode_apply"] = int(apply_mode)
    summary["mode_dry_run"] = int(dry_run)
    print(json.dumps({"summary": summary, "receipt": _receipt_summary(result.get("receipt"))}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
