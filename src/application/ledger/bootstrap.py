from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.ledger.position_fields import (
    effective_expiration_ymd,
    effective_strike,
    exp_ms_to_ymd,
    normalize_account,
    normalize_broker,
    normalize_currency,
    now_ms,
)
from domain.domain.trade_contract_identity import canonical_contract_symbol
from src.application.ledger.publisher import project_stored_trade_events_to_position_lots
from src.application.ledger.repository import (
    OptionPositionsTableRef,
    SQLiteOptionPositionsRepository,
    _has_feishu_option_positions_table_cfg,
    _load_data_config,
    _load_table_ref_from_cfg,
    _option_positions_bootstrap_from_feishu_enabled_from_cfg,
    _option_positions_bootstrap_from_legacy_sqlite_enabled_from_cfg,
    resolve_option_positions_sqlite_path,
    with_sqlite_repo_transaction,
)
from src.infrastructure.feishu_bitable import bitable_list_records, get_tenant_access_token, safe_float


def _canonical_trade_symbol(value: Any) -> str:
    return canonical_contract_symbol(value)


def _list_feishu_option_position_records(table_ref: OptionPositionsTableRef) -> list[dict[str, Any]]:
    token = get_tenant_access_token(table_ref.app_id, table_ref.app_secret)
    return bitable_list_records(token, table_ref.app_token, table_ref.table_id, page_size=500)


def _is_incomplete_option_bootstrap_fields(fields: dict[str, Any]) -> bool:
    option_type = str(fields.get("option_type") or "").strip().lower()
    if option_type not in {"put", "call"}:
        return False
    expiration = fields.get("expiration")
    strike = safe_float(fields.get("strike"))
    return expiration in (None, "") or strike is None


def _normalize_bootstrap_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    skipped = 0
    for item in records:
        record_id = str(item.get("record_id") or item.get("id") or "").strip()
        fields = item.get("fields") or {}
        if not record_id or not isinstance(fields, dict):
            skipped += 1
            continue
        broker = normalize_broker(fields.get("broker"))
        if not broker:
            broker = normalize_broker(fields.get("market"))
        if not broker:
            skipped += 1
            continue
        if _is_incomplete_option_bootstrap_fields(fields):
            skipped += 1
            print(
                (
                    f"[WARN] option_positions bootstrap skipped incomplete option row "
                    f"record_id={record_id or '(missing)'} symbol={fields.get('symbol') or ''} "
                    f"option_type={fields.get('option_type') or ''} expiration={fields.get('expiration') or ''} "
                    f"strike={fields.get('strike') or ''}"
                ),
                file=sys.stderr,
            )
            continue
        normalized_fields = dict(fields)
        normalized_fields["broker"] = broker
        normalized.append({"record_id": record_id, "fields": normalized_fields})
    if skipped:
        print(f"[WARN] option_positions bootstrap skipped {skipped} rows without broker/market", file=sys.stderr)
    return normalized


def _stable_bootstrap_event_id(source_name: str, record_id: str, fields: dict[str, Any]) -> str:
    seed = json.dumps({"record_id": record_id, "fields": fields}, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
    return f"bootstrap:{source_name}:{record_id}:{digest}"


def _safe_bootstrap_trade_time_ms(record_id: str, fields: dict[str, Any]) -> int | None:
    saw_nonempty = False
    for key in ("opened_at", "last_action_at"):
        raw = fields.get(key)
        if raw in (None, ""):
            continue
        saw_nonempty = True
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    if not saw_nonempty:
        return now_ms()
    print(
        (
            f"[WARN] option_positions bootstrap skipped row with invalid timestamps "
            f"record_id={record_id or '(missing)'} opened_at={fields.get('opened_at') or ''} "
            f"last_action_at={fields.get('last_action_at') or ''}"
        ),
        file=sys.stderr,
    )
    return None


def _bootstrap_trade_event(item: dict[str, Any], *, source_name: str) -> Any | None:
    record_id = str(item.get("record_id") or "").strip()
    fields = item.get("fields") or {}
    if not record_id or not isinstance(fields, dict):
        return None
    broker = normalize_broker(fields.get("broker") or fields.get("market"))
    if not broker:
        return None
    trade_time_ms = _safe_bootstrap_trade_time_ms(record_id, fields)
    if trade_time_ms is None:
        return None
    raw_fields = dict(fields)
    raw_fields["broker"] = broker
    raw_multiplier = safe_float(fields.get("multiplier"))
    expiration_ymd = str(fields.get("expiration_ymd") or exp_ms_to_ymd(fields.get("expiration")) or "").strip() or None
    event_id = _stable_bootstrap_event_id(source_name, record_id, raw_fields)
    raw_payload = {
        "source_type": "bootstrap_snapshot",
        "lot_record_id": record_id,
        "fields": raw_fields,
        "source": source_name,
        "multiplier_source": "bootstrap_snapshot" if raw_multiplier is not None else None,
    }
    try:
        contract_key = ContractKey.from_values(
            broker=broker,
            account=normalize_account(fields.get("account")),
            underlying_symbol=_canonical_trade_symbol(fields.get("symbol")),
            option_type=str(fields.get("option_type") or ""),
            position_side=str(fields.get("side") or "").strip().lower(),
            strike=safe_float(fields.get("strike")),
            expiration_ymd=expiration_ymd,
        )
    except Exception:
        return {
            "event_id": event_id,
            "source_type": "bootstrap_snapshot",
            "source_name": source_name,
            "broker": broker,
            "account": normalize_account(fields.get("account")),
            "symbol": _canonical_trade_symbol(fields.get("symbol")),
            "option_type": str(fields.get("option_type") or ""),
            "side": "sell" if str(fields.get("side") or "").strip().lower() == "short" else str(fields.get("side") or "").strip().lower(),
            "position_effect": "open",
            "contracts": max(0, int(safe_float(fields.get("contracts")) or safe_float(fields.get("contracts_open")) or 0)),
            "price": float(safe_float(fields.get("premium")) or 0.0),
            "strike": safe_float(fields.get("strike")),
            "multiplier": (int(float(raw_multiplier)) if raw_multiplier is not None else None),
            "expiration_ymd": expiration_ymd,
            "currency": normalize_currency(fields.get("currency")),
            "trade_time_ms": trade_time_ms,
            "order_id": None,
            "multiplier_source": "bootstrap_snapshot" if raw_multiplier is not None else None,
            "raw_payload": raw_payload,
        }
    return TradeEvent(
        event_id=event_id,
        event_type="open",
        event_time_ms=trade_time_ms,
        contract_key=contract_key,
        contracts=max(0, int(safe_float(fields.get("contracts")) or safe_float(fields.get("contracts_open")) or 0)),
        price=float(safe_float(fields.get("premium")) or 0.0),
        currency=normalize_currency(fields.get("currency")),
        source=source_name,
        multiplier=(float(raw_multiplier) if raw_multiplier is not None else 100.0),
        lot_id=record_id,
        raw_payload=raw_payload,
    )


def _bootstrap_trade_events(records: list[dict[str, Any]], *, source_name: str) -> list[Any]:
    events: list[Any] = []
    for item in records:
        event = _bootstrap_trade_event(item, source_name=source_name)
        if event is not None:
            events.append(event)
    return events


def _raise_if_local_bootstrap_projection_failed(events: list[Any], projection: Any) -> None:
    if not any(_bootstrap_event_source(event) == "sqlite_position_lots" for event in events):
        return
    if not bool(getattr(projection, "has_errors", False)):
        return
    missing_fields: list[str] = []
    for event in events:
        if _bootstrap_event_source(event) != "sqlite_position_lots":
            continue
        fields = _bootstrap_event_raw_fields(event)
        if not isinstance(fields, dict):
            continue
        if not effective_expiration_ymd(fields) and "expiration" not in missing_fields:
            missing_fields.append("expiration")
        if effective_strike(fields) is None and "strike" not in missing_fields:
            missing_fields.append("strike")
    if missing_fields:
        raise ValueError(f"local position_lots bootstrap projection invalid: missing {', '.join(missing_fields)}")
    diagnostics = getattr(projection, "diagnostics", [])
    codes = ", ".join(str(getattr(item, "code", "") or "") for item in diagnostics if getattr(item, "severity", "") == "error")
    raise ValueError(f"local position_lots bootstrap projection invalid: {codes or 'unknown'}")


def _bootstrap_event_source(event: Any) -> str:
    if isinstance(event, dict):
        return str(event.get("source_name") or (event.get("raw_payload") or {}).get("source") or "").strip()
    return str(getattr(event, "source", "") or "").strip()


def _bootstrap_event_raw_fields(event: Any) -> dict[str, Any]:
    raw_payload = event.get("raw_payload") if isinstance(event, dict) else getattr(event, "raw_payload", None)
    if not isinstance(raw_payload, dict):
        return {}
    fields = raw_payload.get("fields")
    return dict(fields) if isinstance(fields, dict) else {}


def materialize_bootstrap_events(repo: SQLiteOptionPositionsRepository, events: list[Any]) -> int:
    def _run(sqlite_repo: Any, conn: sqlite3.Connection | None) -> int:
        if conn is not None:
            for event in events:
                sqlite_repo.upsert_trade_event(event, conn=conn)
            projection = project_stored_trade_events_to_position_lots(sqlite_repo.list_trade_events(conn=conn))
            _raise_if_local_bootstrap_projection_failed(events, projection)
            sqlite_repo.replace_position_lots(projection.lots, conn=conn)
        else:
            for event in events:
                sqlite_repo.upsert_trade_event(event)
            projection = project_stored_trade_events_to_position_lots(sqlite_repo.list_trade_events())
            _raise_if_local_bootstrap_projection_failed(events, projection)
            sqlite_repo.replace_position_lots(projection.lots)
        return len(events)

    return int(with_sqlite_repo_transaction(repo, _run))


def apply_bootstrap_snapshot(
    repo: Any,
    *,
    records: list[dict[str, Any]],
    source_name: str,
    success_status: str,
    success_message: str,
    failure_status: str,
    failure_message: str,
    failure_log_prefix: str,
) -> bool:
    try:
        count = materialize_bootstrap_events(repo, _bootstrap_trade_events(records, source_name=source_name))
        repo.bootstrap_status = success_status
        repo.bootstrap_message = success_message.format(count=count)
        return True
    except Exception as exc:
        repo.bootstrap_status = failure_status
        repo.bootstrap_message = failure_message.format(error=exc)
        print(
            f"[WARN] {failure_log_prefix} for {repo.db_path}: {exc}",
            file=sys.stderr,
        )
        return False


def load_option_positions_repo(data_config: Path) -> SQLiteOptionPositionsRepository:
    repo = SQLiteOptionPositionsRepository(resolve_option_positions_sqlite_path(data_config))
    repo.data_config_path = Path(data_config).resolve()
    data_cfg = _load_data_config(data_config)
    if repo.count_trade_events() > 0:
        repo.bootstrap_status = "skipped_existing_trade_events"
        repo.bootstrap_message = "trade_events already present"
        if repo.count_position_lots() == 0:
            projection = project_stored_trade_events_to_position_lots(repo.list_trade_events())
            repo.replace_position_lots(projection.lots)
        return repo

    if repo.count_position_lots() > 0:
        apply_bootstrap_snapshot(
            repo,
            records=repo.list_position_lots(),
            source_name="sqlite_position_lots",
            success_status="migrated_local_position_lots",
            success_message="migrated {count} bootstrap events from local position_lots",
            failure_status="degraded_local_position_lots_migration_failed",
            failure_message="local position_lots migration failed: {error}",
            failure_log_prefix="option_positions local snapshot migration skipped",
        )
        return repo

    if _option_positions_bootstrap_from_feishu_enabled_from_cfg(data_cfg):
        feishu_ref = _load_table_ref_from_cfg(data_cfg)
        try:
            bootstrap_records = _normalize_bootstrap_records(_list_feishu_option_position_records(feishu_ref))
        except Exception as exc:
            repo.bootstrap_status = "degraded_feishu_bootstrap_failed"
            repo.bootstrap_message = f"feishu bootstrap failed: {exc}"
            print(
                f"[WARN] option_positions bootstrap skipped for {repo.db_path}: {exc}",
                file=sys.stderr,
            )
        else:
            apply_bootstrap_snapshot(
                repo,
                records=bootstrap_records,
                source_name="feishu_bootstrap",
                success_status="bootstrapped_from_feishu",
                success_message="bootstrapped {count} trade events from feishu",
                failure_status="degraded_feishu_bootstrap_failed",
                failure_message="feishu bootstrap failed: {error}",
                failure_log_prefix="option_positions bootstrap skipped",
            )
    else:
        if _has_feishu_option_positions_table_cfg(data_cfg):
            repo.bootstrap_status = "sqlite_only_feishu_bootstrap_disabled"
            repo.bootstrap_message = "feishu option_positions bootstrap disabled; local trade_events remain source of truth"
        else:
            repo.bootstrap_status = "sqlite_only_no_feishu_bootstrap"
            repo.bootstrap_message = "no feishu option_positions bootstrap configured"

    if repo.count_trade_events() == 0 and repo.count_legacy_records() > 0:
        if _option_positions_bootstrap_from_legacy_sqlite_enabled_from_cfg(data_cfg):
            apply_bootstrap_snapshot(
                repo,
                records=_normalize_bootstrap_records(repo.list_legacy_records()),
                source_name="legacy_option_positions",
                success_status="migrated_legacy_option_positions",
                success_message="migrated {count} trade events from legacy option_positions",
                failure_status="degraded_legacy_option_positions_migration_failed",
                failure_message="legacy option_positions migration failed: {error}",
                failure_log_prefix="option_positions legacy migration skipped",
            )
        else:
            repo.bootstrap_status = "sqlite_only_legacy_option_positions_bootstrap_disabled"
            repo.bootstrap_message = (
                "legacy option_positions bootstrap disabled; local trade_events remain source of truth"
            )
    return repo
