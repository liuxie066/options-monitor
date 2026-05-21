from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from contextlib import closing
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
    SQLiteOptionPositionsRepository,
    _load_data_config,
    with_sqlite_repo_transaction,
)
from src.application.ledger.store_resolution import resolve_ledger_store
from src.infrastructure.feishu_bitable import safe_float


def _canonical_trade_symbol(value: Any) -> str:
    return canonical_contract_symbol(value)


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


def _has_retired_feishu_bootstrap_opt_in(cfg: dict[str, Any]) -> bool:
    option_positions_cfg = cfg.get("option_positions")
    if not isinstance(option_positions_cfg, dict):
        return False
    bootstrap_cfg = option_positions_cfg.get("bootstrap_from_feishu")
    if not isinstance(bootstrap_cfg, dict):
        return False
    return bool(bootstrap_cfg.get("enabled") is True)


def _raise_if_local_bootstrap_projection_failed(events: list[Any], projection: Any) -> None:
    position_lot_sources = {"sqlite_position_lots", "legacy_position_lots"}
    if not any(_bootstrap_event_source(event) in position_lot_sources for event in events):
        return
    if not bool(getattr(projection, "has_errors", False)):
        return
    missing_fields: list[str] = []
    for event in events:
        if _bootstrap_event_source(event) not in position_lot_sources:
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


def _legacy_sqlite_connect(path: Path) -> sqlite3.Connection:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _legacy_table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (str(name),),
    ).fetchone()
    return row is not None


def _legacy_table_count(conn: sqlite3.Connection, name: str) -> int:
    if not _legacy_table_exists(conn, name):
        return 0
    row = conn.execute(f"SELECT COUNT(*) AS cnt FROM {name}").fetchone()
    return int((row["cnt"] if row is not None else 0) or 0)


def _legacy_sqlite_counts(path: Path) -> dict[str, int] | None:
    if not path.exists():
        return None
    with closing(_legacy_sqlite_connect(path)) as conn:
        return {
            "trade_events": _legacy_table_count(conn, "trade_events"),
            "position_lots": _legacy_table_count(conn, "position_lots"),
            "option_positions": _legacy_table_count(conn, "option_positions"),
        }


def _legacy_position_lot_row(row: sqlite3.Row) -> dict[str, Any]:
    fields = json.loads(str(row["fields_json"]) or "{}")
    if not isinstance(fields, dict):
        fields = {}
    if fields.get("expiration") in (None, "") and row["expiration"] not in (None, ""):
        fields["expiration"] = int(row["expiration"])
    if fields.get("strike") is None and row["strike"] is not None:
        fields["strike"] = float(row["strike"])
    if fields.get("multiplier") is None and row["multiplier"] is not None:
        raw_multiplier = float(row["multiplier"])
        fields["multiplier"] = int(raw_multiplier) if raw_multiplier.is_integer() else raw_multiplier
    return {
        "record_id": str(row["record_id"]),
        "fields": fields,
    }


def _list_legacy_trade_events(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _legacy_table_exists(conn, "trade_events"):
        return []
    rows = conn.execute(
        """
        SELECT event_json
        FROM trade_events
        ORDER BY trade_time_ms ASC, event_id ASC
        """
    ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        item = json.loads(str(row["event_json"]) or "{}")
        if isinstance(item, dict):
            events.append(item)
    return events


def _list_legacy_position_lots(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _legacy_table_exists(conn, "position_lots"):
        return []
    rows = conn.execute(
        """
        SELECT record_id, fields_json, expiration, strike, multiplier
        FROM position_lots
        ORDER BY updated_at_ms DESC, record_id DESC
        """
    ).fetchall()
    return [_legacy_position_lot_row(row) for row in rows]


def _list_legacy_option_positions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _legacy_table_exists(conn, "option_positions"):
        return []
    rows = conn.execute(
        """
        SELECT record_id, fields_json
        FROM option_positions
        ORDER BY updated_at_ms DESC, record_id DESC
        """
    ).fetchall()
    records: list[dict[str, Any]] = []
    for row in rows:
        fields = json.loads(str(row["fields_json"]) or "{}")
        records.append(
            {
                "record_id": str(row["record_id"]),
                "fields": fields if isinstance(fields, dict) else {},
            }
        )
    return records


def _legacy_source_table(counts: dict[str, int] | None) -> str | None:
    if not counts:
        return None
    if int(counts.get("trade_events") or 0) > 0:
        return "trade_events"
    if int(counts.get("position_lots") or 0) > 0:
        return "position_lots"
    if int(counts.get("option_positions") or 0) > 0:
        return "option_positions"
    return None


def inspect_legacy_sqlite_migration(legacy_path: str | Path | None) -> dict[str, Any]:
    path_text = str(legacy_path or "").strip()
    if not path_text:
        return {
            "ok": False,
            "legacy_sqlite_path": None,
            "exists": False,
            "counts": None,
            "source_table": None,
            "message": "legacy SQLite path is required",
        }
    path = Path(path_text).expanduser().resolve()
    try:
        counts = _legacy_sqlite_counts(path)
    except Exception as exc:
        return {
            "ok": False,
            "legacy_sqlite_path": str(path),
            "exists": path.exists(),
            "counts": None,
            "source_table": None,
            "message": f"failed to inspect legacy SQLite: {exc}",
        }
    if counts is None:
        return {
            "ok": False,
            "legacy_sqlite_path": str(path),
            "exists": False,
            "counts": None,
            "source_table": None,
            "message": "legacy SQLite database not found",
        }
    source_table = _legacy_source_table(counts)
    return {
        "ok": source_table is not None,
        "legacy_sqlite_path": str(path),
        "exists": True,
        "counts": counts,
        "source_table": source_table,
        "message": "legacy SQLite has migratable rows" if source_table else "legacy SQLite database has no bootstrap rows",
    }


def migrate_legacy_sqlite_to_repo(
    repo: SQLiteOptionPositionsRepository,
    *,
    legacy_path: str | Path | None,
    apply: bool = False,
) -> dict[str, Any]:
    inspection = inspect_legacy_sqlite_migration(legacy_path)
    payload: dict[str, Any] = {
        **inspection,
        "active_sqlite_path": str(repo.db_path),
        "applied": False,
        "migrated_count": 0,
        "bootstrap_status": getattr(repo, "bootstrap_status", None),
        "bootstrap_message": getattr(repo, "bootstrap_message", None),
    }
    if not bool(inspection.get("ok")):
        return payload
    if not apply:
        payload["message"] = "dry run; pass --confirm or --yes to migrate legacy SQLite into active trade_events"
        return payload

    path = Path(str(inspection["legacy_sqlite_path"])).expanduser().resolve()
    counts = inspection.get("counts") if isinstance(inspection.get("counts"), dict) else {}
    source_table = str(inspection.get("source_table") or "")

    try:
        with closing(_legacy_sqlite_connect(path)) as conn:
            if source_table == "trade_events":
                count = materialize_bootstrap_events(repo, _list_legacy_trade_events(conn))
                repo.bootstrap_status = "migrated_legacy_trade_events"
                repo.bootstrap_message = f"migrated {count} trade events from legacy SQLite trade_events"
            elif source_table == "position_lots":
                events = _bootstrap_trade_events(
                    _list_legacy_position_lots(conn),
                    source_name="legacy_position_lots",
                )
                count = materialize_bootstrap_events(repo, events)
                repo.bootstrap_status = "migrated_legacy_position_lots"
                repo.bootstrap_message = f"migrated {count} bootstrap events from legacy SQLite position_lots"
            else:
                events = _bootstrap_trade_events(
                    _normalize_bootstrap_records(_list_legacy_option_positions(conn)),
                    source_name="legacy_option_positions",
                )
                count = materialize_bootstrap_events(repo, events)
                repo.bootstrap_status = "migrated_legacy_option_positions"
                repo.bootstrap_message = f"migrated {count} trade events from legacy SQLite option_positions"
    except Exception as exc:
        repo.bootstrap_status = "degraded_legacy_sqlite_migration_failed"
        repo.bootstrap_message = f"legacy SQLite migration failed: {exc}"
        payload.update(
            {
                "ok": False,
                "applied": False,
                "migrated_count": 0,
                "bootstrap_status": repo.bootstrap_status,
                "bootstrap_message": repo.bootstrap_message,
                "message": f"legacy SQLite migration failed: {exc}",
            }
        )
        print(
            f"[WARN] option_positions legacy SQLite migration skipped for {repo.db_path}: {exc}",
            file=sys.stderr,
        )
        return payload

    payload.update(
        {
            "ok": True,
            "applied": True,
            "migrated_count": count,
            "counts": counts,
            "bootstrap_status": repo.bootstrap_status,
            "bootstrap_message": repo.bootstrap_message,
            "message": repo.bootstrap_message,
        }
    )
    return payload


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


def load_option_positions_repo(
    data_config: Path,
    *,
    config_path: str | Path | None = None,
    runtime_root: str | Path | None = None,
) -> SQLiteOptionPositionsRepository:
    store = resolve_ledger_store(data_config, config_path=config_path, runtime_root=runtime_root)
    repo = SQLiteOptionPositionsRepository(store.sqlite_path)
    repo.data_config_path = store.data_config_path
    setattr(repo, "ledger_store", store)
    data_cfg = _load_data_config(data_config)
    if repo.count_trade_events() > 0:
        repo.bootstrap_status = "skipped_existing_trade_events"
        repo.bootstrap_message = "trade_events already present"
        if repo.count_position_lots() == 0:
            projection = project_stored_trade_events_to_position_lots(repo.list_trade_events())
            repo.replace_position_lots(projection.lots)
        return repo

    if repo.count_position_lots() > 0:
        repo.bootstrap_status = "sqlite_only_legacy_position_lots_present"
        repo.bootstrap_message = (
            "position_lots exist without trade_events; run explicit "
            "option-positions store migrate-legacy before ledger writes"
        )
        return repo

    if _has_retired_feishu_bootstrap_opt_in(data_cfg):
        repo.bootstrap_status = "sqlite_only_feishu_bootstrap_retired"
        repo.bootstrap_message = "feishu option_positions bootstrap is retired; local trade_events remain source of truth"
    else:
        repo.bootstrap_status = "sqlite_only_no_feishu_bootstrap"
        repo.bootstrap_message = "feishu option_positions bootstrap is not used; local trade_events remain source of truth"

    return repo
