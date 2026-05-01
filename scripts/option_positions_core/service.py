from __future__ import annotations

import json
import hashlib
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from scripts.feishu_bitable import bitable_list_records, get_tenant_access_token, parse_note_kv, safe_float
from scripts.option_positions_core.domain import (
    OpenPositionCommand,
    build_expire_auto_close_patch,
    build_open_adjustment_patch,
    effective_contracts_open,
    effective_expiration,
    effective_expiration_ymd,
    effective_multiplier,
    effective_strike,
    exp_ms_to_datetime,
    exp_ms_to_ymd,
    normalize_broker,
    now_ms,
    resolve_open_currency,
)
from scripts.option_positions_core.ledger import (
    ProjectionDiagnostic,
    TradeEvent,
    project_position_lot_records,
    project_position_lot_records_with_diagnostics,
    trade_event_from_normalized_deal,
)


REPO_BASE = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class OptionPositionsTableRef:
    app_id: str
    app_secret: str
    app_token: str
    table_id: str


class OptionPositionsRepoLike(Protocol):
    def list_records(self, *, page_size: int = 500) -> list[dict[str, Any]]: ...
    def get_record_fields(self, record_id: str) -> dict[str, Any]: ...


class OptionPositionsReadRepo(Protocol):
    def list_position_lots(self) -> list[dict[str, Any]]: ...


class OptionPositionsSyncMetaRepo(OptionPositionsReadRepo, Protocol):
    def update_position_lot_fields(self, record_id: str, fields: dict[str, Any]) -> None: ...


class OptionPositionsEventWriteRepo(OptionPositionsSyncMetaRepo, Protocol):
    def list_trade_events(self, *, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]: ...
    def upsert_trade_event(self, event: "TradeEvent", *, conn: sqlite3.Connection | None = None) -> bool: ...
    def replace_position_lots(self, records: list[dict[str, Any]], *, conn: sqlite3.Connection | None = None) -> int: ...


def _load_data_config(data_config: Path) -> dict[str, Any]:
    cfg = json.loads(data_config.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise SystemExit("data config must be a JSON object")
    return cfg


def _get_feishu_cfg(cfg: dict[str, Any], *, allow_missing: bool) -> dict[str, Any] | None:
    raw = cfg.get("feishu")
    if raw is None:
        return None if allow_missing else {}
    if not isinstance(raw, dict):
        raise SystemExit("data config feishu must be a JSON object")
    return raw


def _load_table_ref_from_cfg(cfg: dict[str, Any]) -> OptionPositionsTableRef:
    feishu_cfg = _get_feishu_cfg(cfg, allow_missing=False) or {}
    app_id = feishu_cfg.get("app_id")
    app_secret = feishu_cfg.get("app_secret")
    ref = (feishu_cfg.get("tables", {}) or {}).get("option_positions")
    if not (app_id and app_secret and ref and "/" in ref):
        raise SystemExit("data config missing feishu app_id/app_secret/option_positions")
    app_token, table_id = ref.split("/", 1)
    return OptionPositionsTableRef(str(app_id), str(app_secret), str(app_token), str(table_id))


def load_table_ref(data_config: Path) -> OptionPositionsTableRef:
    return _load_table_ref_from_cfg(_load_data_config(data_config))


def _try_load_table_ref(data_config: Path) -> OptionPositionsTableRef | None:
    cfg = _load_data_config(data_config)
    feishu_cfg = _get_feishu_cfg(cfg, allow_missing=True)
    if feishu_cfg is None or feishu_cfg == {}:
        return None
    tables = feishu_cfg.get("tables") if isinstance(feishu_cfg.get("tables"), dict) else {}
    if tables and not str(tables.get("option_positions") or "").strip():
        return None
    return _load_table_ref_from_cfg(cfg)


def resolve_option_positions_sqlite_path(data_config: Path) -> Path:
    cfg = _load_data_config(data_config)
    raw = ((cfg.get("option_positions") or {}) if isinstance(cfg.get("option_positions"), dict) else {}).get("sqlite_path")
    if raw is None or not str(raw).strip():
        path = (REPO_BASE / "output_shared" / "state" / "option_positions.sqlite3").resolve()
    else:
        path = Path(str(raw))
        if not path.is_absolute():
            path = (REPO_BASE / path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


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


def _validate_position_lot_fields(*, record_id: str, fields: dict[str, Any]) -> None:
    option_type = str(fields.get("option_type") or "").strip().lower()
    if option_type not in {"put", "call"}:
        return
    expiration = fields.get("expiration")
    strike = safe_float(fields.get("strike"))
    missing: list[str] = []
    if expiration in (None, ""):
        missing.append("expiration")
    if strike is None:
        missing.append("strike")
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"incomplete option position lot {record_id}: missing {joined}")


def _position_lot_contract_scalars(fields: dict[str, Any]) -> tuple[int | None, float | None, float | None]:
    expiration_ms, _ = effective_expiration(fields)
    strike = safe_float(fields.get("strike"))
    multiplier = safe_float(fields.get("multiplier"))
    if multiplier is None:
        multiplier = safe_float(parse_note_kv(fields.get("note") or "", "multiplier"))
    return expiration_ms, strike, multiplier


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cols = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _row_to_position_lot(row: sqlite3.Row) -> dict[str, Any]:
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


def _bootstrap_trade_event(item: dict[str, Any], *, source_name: str) -> TradeEvent | None:
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
    return TradeEvent(
        event_id=_stable_bootstrap_event_id(source_name, record_id, raw_fields),
        source_type="bootstrap_snapshot",
        source_name=source_name,
        broker=broker,
        account=str(fields.get("account") or ""),
        symbol=str(fields.get("symbol") or "").strip().upper(),
        option_type=str(fields.get("option_type") or ""),
        side="sell" if str(fields.get("side") or "").strip().lower() == "short" else str(fields.get("side") or "").strip().lower(),
        position_effect="open",
        contracts=max(0, int(safe_float(fields.get("contracts")) or safe_float(fields.get("contracts_open")) or 0)),
        price=float(safe_float(fields.get("premium")) or 0.0),
        strike=safe_float(fields.get("strike")),
        multiplier=(int(float(raw_multiplier)) if (raw_multiplier := safe_float(fields.get("multiplier"))) is not None else None),
        expiration_ymd=exp_ms_to_ymd(fields.get("expiration")),
        currency=str(fields.get("currency") or "").strip().upper(),
        trade_time_ms=trade_time_ms,
        order_id=None,
        multiplier_source="bootstrap_snapshot" if raw_multiplier is not None else None,
        raw_payload={
            "lot_record_id": record_id,
            "fields": raw_fields,
            "source": source_name,
        },
    )


def _bootstrap_trade_events(records: list[dict[str, Any]], *, source_name: str) -> list[TradeEvent]:
    events: list[TradeEvent] = []
    for item in records:
        event = _bootstrap_trade_event(item, source_name=source_name)
        if event is not None:
            events.append(event)
    return events


PRESERVED_POSITION_LOT_META_KEYS = (
    "feishu_record_id",
    "feishu_sync_hash",
    "feishu_last_synced_at_ms",
)


def _sync_meta_only_patch(existing_fields: dict[str, Any], candidate_fields: dict[str, Any]) -> dict[str, Any]:
    patched = dict(existing_fields)
    for key in PRESERVED_POSITION_LOT_META_KEYS:
        if key in candidate_fields:
            value = candidate_fields.get(key)
            if value in (None, ""):
                patched.pop(key, None)
            else:
                patched[key] = value
    return patched


def _projection_diagnostics_summary(diagnostics: list[ProjectionDiagnostic]) -> dict[str, Any]:
    explicit_close_codes = {
        "close_explicit_target_not_found",
        "close_explicit_target_conflict",
        "close_explicit_target_already_closed",
        "close_explicit_target_mismatch",
        "close_explicit_target_oversized",
        "close_explicit_source_event_target_not_found",
        "close_explicit_source_event_target_already_closed",
        "close_explicit_source_event_target_mismatch",
        "close_explicit_source_event_target_oversized",
    }
    return {
        "projection_diagnostic_count": int(len(diagnostics)),
        "unmatched_explicit_close_count": int(sum(1 for item in diagnostics if item.code in explicit_close_codes)),
        "unmatched_heuristic_close_count": int(sum(1 for item in diagnostics if item.code == "close_unmatched_contracts")),
        "projection_diagnostics": [item.to_dict() for item in diagnostics],
    }


def _merge_preserved_position_lot_metadata(
    records: list[dict[str, Any]],
    existing_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    existing_by_record_id: dict[str, dict[str, Any]] = {}
    for item in existing_rows:
        record_id = str(item.get("record_id") or "").strip()
        fields = item.get("fields") or {}
        if record_id and isinstance(fields, dict):
            existing_by_record_id[record_id] = fields

    merged: list[dict[str, Any]] = []
    for item in records:
        record_id = str(item.get("record_id") or "").strip()
        fields = item.get("fields") or {}
        if not record_id or not isinstance(fields, dict):
            merged.append(item)
            continue
        existing_fields = existing_by_record_id.get(record_id) or {}
        patched_fields = dict(fields)
        for key in PRESERVED_POSITION_LOT_META_KEYS:
            value = existing_fields.get(key)
            if value not in (None, ""):
                patched_fields[key] = value
        merged.append({"record_id": record_id, "fields": patched_fields})
    return merged


class SQLiteOptionPositionsRepository:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.bootstrap_status = "not_started"
        self.bootstrap_message: str | None = None
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @contextmanager
    def _optional_conn(self, conn: sqlite3.Connection | None, *, commit: bool = False):
        owned = conn is None
        if conn is None:
            conn = self._connect()
        try:
            yield conn
            if owned and commit:
                conn.commit()
        finally:
            if owned:
                conn.close()

    def _table_exists(self, name: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
                (str(name),),
            ).fetchone()
        return row is not None

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_events (
                  event_id TEXT PRIMARY KEY,
                  event_json TEXT NOT NULL,
                  trade_time_ms INTEGER NOT NULL,
                  created_at_ms INTEGER NOT NULL,
                  updated_at_ms INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trade_events_trade_time ON trade_events(trade_time_ms, event_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS position_lots (
                  record_id TEXT PRIMARY KEY,
                  fields_json TEXT NOT NULL,
                  source_event_id TEXT,
                  expiration INTEGER,
                  strike REAL,
                  multiplier REAL,
                  updated_at_ms INTEGER NOT NULL
                )
                """
            )
            _add_column_if_missing(conn, "position_lots", "expiration", "INTEGER")
            _add_column_if_missing(conn, "position_lots", "strike", "REAL")
            _add_column_if_missing(conn, "position_lots", "multiplier", "REAL")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_position_lots_expiration ON position_lots(expiration, record_id)"
            )
            self._backfill_position_lot_contract_columns(conn)
            conn.commit()

    def _backfill_position_lot_contract_columns(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            SELECT record_id, fields_json, expiration, strike, multiplier
            FROM position_lots
            """
        ).fetchall()
        for row in rows:
            fields = json.loads(str(row["fields_json"]) or "{}")
            if not isinstance(fields, dict):
                fields = {}
            expiration_ms, strike, multiplier = _position_lot_contract_scalars(fields)
            if (
                row["expiration"] == expiration_ms
                and (
                    (row["strike"] is None and strike is None)
                    or (row["strike"] is not None and strike is not None and abs(float(row["strike"]) - float(strike)) < 1e-9)
                )
                and (
                    (row["multiplier"] is None and multiplier is None)
                    or (
                        row["multiplier"] is not None
                        and multiplier is not None
                        and abs(float(row["multiplier"]) - float(multiplier)) < 1e-9
                    )
                )
            ):
                continue
            conn.execute(
                """
                UPDATE position_lots
                SET expiration = ?, strike = ?, multiplier = ?
                WHERE record_id = ?
                """,
                (
                    int(expiration_ms) if expiration_ms is not None else None,
                    float(strike) if strike is not None else None,
                    float(multiplier) if multiplier is not None else None,
                    str(row["record_id"]),
                ),
            )

    def count_position_lots(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM position_lots").fetchone()
        return int((row["cnt"] if row is not None else 0) or 0)

    def count_trade_events(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM trade_events").fetchone()
        return int((row["cnt"] if row is not None else 0) or 0)

    def count_legacy_records(self) -> int:
        if not self._table_exists("option_positions"):
            return 0
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM option_positions").fetchone()
        return int((row["cnt"] if row is not None else 0) or 0)

    def list_legacy_records(self) -> list[dict[str, Any]]:
        if not self._table_exists("option_positions"):
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT record_id, fields_json
                FROM option_positions
                ORDER BY updated_at_ms DESC, record_id DESC
                """
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            fields = json.loads(str(row["fields_json"]) or "{}")
            out.append(
                {
                    "record_id": str(row["record_id"]),
                    "fields": fields if isinstance(fields, dict) else {},
                }
            )
        return out

    def upsert_trade_event(self, event: TradeEvent, *, conn: sqlite3.Connection | None = None) -> bool:
        payload = event.to_dict()
        event_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        ts = int(now_ms())
        trade_time_ms = int(event.trade_time_ms or 0)
        with self._optional_conn(conn, commit=True) as active_conn:
            existing = active_conn.execute(
                "SELECT event_json FROM trade_events WHERE event_id = ?",
                (str(event.event_id),),
            ).fetchone()
            if existing is not None:
                try:
                    existing_payload = json.loads(str(existing["event_json"]) or "{}")
                except json.JSONDecodeError as exc:
                    raise ValueError(f"existing trade event JSON is invalid: event_id={event.event_id}") from exc
                existing_json = json.dumps(existing_payload, ensure_ascii=False, sort_keys=True)
                if existing_json != event_json:
                    raise ValueError(f"trade event conflict for event_id={event.event_id}")
                return False
            active_conn.execute(
                """
                INSERT INTO trade_events (
                  event_id, event_json, trade_time_ms, created_at_ms, updated_at_ms
                ) VALUES (
                  ?, ?, ?, ?, ?
                )
                """,
                (
                    str(event.event_id),
                    event_json,
                    trade_time_ms,
                    ts,
                    ts,
                ),
            )
        return True

    def list_trade_events(self, *, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        with self._optional_conn(conn) as active_conn:
            rows = active_conn.execute(
                """
                SELECT event_json
                FROM trade_events
                ORDER BY trade_time_ms ASC, event_id ASC
                """
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = json.loads(str(row["event_json"]) or "{}")
            if isinstance(item, dict):
                out.append(item)
        return out

    def replace_position_lots(self, records: list[dict[str, Any]], *, conn: sqlite3.Connection | None = None) -> int:
        ts = int(now_ms())
        inserted = 0
        with self._optional_conn(conn, commit=True) as active_conn:
            active_conn.execute("DELETE FROM position_lots")
            for item in records:
                record_id = str(item.get("record_id") or "").strip()
                fields = item.get("fields") or {}
                if not record_id or not isinstance(fields, dict):
                    continue
                _validate_position_lot_fields(record_id=record_id, fields=fields)
                expiration_ms, strike, multiplier = _position_lot_contract_scalars(fields)
                active_conn.execute(
                    """
                    INSERT INTO position_lots (
                      record_id, fields_json, source_event_id, expiration, strike, multiplier, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record_id,
                        json.dumps(fields, ensure_ascii=False, sort_keys=True),
                        (str(fields.get("source_event_id")) if fields.get("source_event_id") else None),
                        int(expiration_ms) if expiration_ms is not None else None,
                        float(strike) if strike is not None else None,
                        float(multiplier) if multiplier is not None else None,
                        ts,
                    ),
                )
                inserted += 1
        return inserted

    def list_position_lots(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT record_id, fields_json, expiration, strike, multiplier
                FROM position_lots
                ORDER BY updated_at_ms DESC, record_id DESC
                """
            ).fetchall()
        return [_row_to_position_lot(row) for row in rows]

    def get_position_lot_fields(self, record_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT record_id, fields_json, expiration, strike, multiplier
                FROM position_lots
                WHERE record_id = ?
                """,
                (str(record_id),),
            ).fetchone()
        if row is None:
            raise ValueError(f"position lot not found: {record_id}")
        return _row_to_position_lot(row)["fields"]

    def update_position_lot_fields(self, record_id: str, fields: dict[str, Any]) -> None:
        normalized_record_id = str(record_id or "").strip()
        if not normalized_record_id:
            raise ValueError("record_id is required")
        if not isinstance(fields, dict):
            raise TypeError("fields must be a dict")
        existing_fields = self.get_position_lot_fields(normalized_record_id)
        patched_fields = _sync_meta_only_patch(existing_fields, fields)
        ts = int(now_ms())
        expiration_ms, strike, multiplier = _position_lot_contract_scalars(patched_fields)
        with self._connect() as conn:
            updated = conn.execute(
                """
                UPDATE position_lots
                SET fields_json = ?, source_event_id = ?, expiration = ?, strike = ?, multiplier = ?, updated_at_ms = ?
                WHERE record_id = ?
                """,
                (
                    json.dumps(patched_fields, ensure_ascii=False, sort_keys=True),
                    (str(patched_fields.get("source_event_id")) if patched_fields.get("source_event_id") else None),
                    int(expiration_ms) if expiration_ms is not None else None,
                    float(strike) if strike is not None else None,
                    float(multiplier) if multiplier is not None else None,
                    ts,
                    normalized_record_id,
                ),
            )
            conn.commit()
        if int(updated.rowcount or 0) <= 0:
            raise ValueError(f"position lot not found: {normalized_record_id}")

    def list_records(self, *, page_size: int = 500) -> list[dict[str, Any]]:
        return self.list_position_lots()

    def get_record_fields(self, record_id: str) -> dict[str, Any]:
        return self.get_position_lot_fields(record_id)


OptionPositionsRepository = SQLiteOptionPositionsRepository


def _materialize_bootstrap_events(repo: SQLiteOptionPositionsRepository, events: list[TradeEvent]) -> int:
    def _run(sqlite_repo: Any, conn: sqlite3.Connection | None) -> int:
        if conn is not None:
            for event in events:
                sqlite_repo.upsert_trade_event(event, conn=conn)
            sqlite_repo.replace_position_lots(project_position_lot_records(sqlite_repo.list_trade_events(conn=conn)), conn=conn)
        else:
            for event in events:
                sqlite_repo.upsert_trade_event(event)
            sqlite_repo.replace_position_lots(project_position_lot_records(sqlite_repo.list_trade_events()))
        return len(events)

    return int(_with_sqlite_repo_transaction(repo, _run))


def _apply_bootstrap_snapshot(
    repo: SQLiteOptionPositionsRepository,
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
        count = _materialize_bootstrap_events(repo, _bootstrap_trade_events(records, source_name=source_name))
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
    repo.data_config_path = Path(data_config).resolve()  # type: ignore[attr-defined]
    if repo.count_trade_events() > 0:
        repo.bootstrap_status = "skipped_existing_trade_events"
        repo.bootstrap_message = "trade_events already present"
        if repo.count_position_lots() == 0:
            repo.replace_position_lots(project_position_lot_records(repo.list_trade_events()))
        return repo

    if repo.count_position_lots() > 0:
        _apply_bootstrap_snapshot(
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

    feishu_ref = _try_load_table_ref(data_config)
    if feishu_ref is not None:
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
            _apply_bootstrap_snapshot(
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
        repo.bootstrap_status = "sqlite_only_no_feishu_bootstrap"
        repo.bootstrap_message = "no feishu option_positions bootstrap configured"

    if repo.count_trade_events() == 0 and repo.count_legacy_records() > 0:
        _apply_bootstrap_snapshot(
            repo,
            records=_normalize_bootstrap_records(repo.list_legacy_records()),
            source_name="legacy_option_positions",
            success_status="migrated_legacy_option_positions",
            success_message="migrated {count} trade events from legacy option_positions",
            failure_status="degraded_legacy_option_positions_migration_failed",
            failure_message="legacy option_positions migration failed: {error}",
            failure_log_prefix="option_positions legacy migration skipped",
        )
    return repo


def require_option_positions_read_repo(repo: Any) -> OptionPositionsReadRepo:
    candidate = getattr(repo, "primary_repo", repo)
    if callable(getattr(candidate, "list_position_lots", None)):
        return candidate
    raise TypeError("option_positions repo does not satisfy read repository interface")


def require_option_positions_sync_meta_repo(repo: Any) -> OptionPositionsSyncMetaRepo:
    candidate = require_option_positions_read_repo(repo)
    if callable(getattr(candidate, "update_position_lot_fields", None)):
        return candidate
    raise TypeError("option_positions repo does not satisfy sync metadata repository interface")


def require_option_positions_event_write_repo(repo: Any) -> OptionPositionsEventWriteRepo:
    candidate = require_option_positions_sync_meta_repo(repo)
    required = (
        "list_trade_events",
        "upsert_trade_event",
        "replace_position_lots",
    )
    if all(callable(getattr(candidate, name, None)) for name in required):
        return candidate
    raise TypeError("option_positions repo does not satisfy event write repository interface")


def _assert_position_lot_target_matches_current_state(
    repo: Any,
    *,
    record_id: str,
    fields: dict[str, Any],
    operation: str,
) -> dict[str, Any]:
    get_record_fields = getattr(repo, "get_record_fields", None)
    if not callable(get_record_fields):
        raise TypeError("option_positions repo does not expose get_record_fields")
    current_fields = get_record_fields(str(record_id))
    comparisons = (
        ("broker", normalize_broker(current_fields.get("broker")), normalize_broker(fields.get("broker"))),
        ("account", str(current_fields.get("account") or "").strip(), str(fields.get("account") or "").strip()),
        ("symbol", str(current_fields.get("symbol") or "").strip().upper(), str(fields.get("symbol") or "").strip().upper()),
        ("option_type", str(current_fields.get("option_type") or "").strip().lower(), str(fields.get("option_type") or "").strip().lower()),
        ("side", str(current_fields.get("side") or "").strip().lower(), str(fields.get("side") or "").strip().lower()),
        ("currency", str(current_fields.get("currency") or "").strip().upper(), str(fields.get("currency") or "").strip().upper()),
        ("strike", effective_strike(current_fields), effective_strike(fields)),
        ("expiration_ymd", effective_expiration_ymd(current_fields), effective_expiration_ymd(fields)),
        (
            "source_event_id",
            str(current_fields.get("source_event_id") or "").strip(),
            str(fields.get("source_event_id") or "").strip(),
        ),
    )
    mismatches = [name for name, left, right in comparisons if left != right]
    if mismatches:
        joined = ", ".join(mismatches)
        raise ValueError(f"{operation} target fields do not match current lot state: {record_id} ({joined})")
    return current_fields


def _with_sqlite_repo_transaction(repo: Any, fn: Any) -> Any:
    sqlite_repo = require_option_positions_event_write_repo(repo)
    conn = sqlite_repo._connect() if isinstance(sqlite_repo, SQLiteOptionPositionsRepository) else None
    try:
        result = fn(sqlite_repo, conn)
        if conn is not None:
            conn.commit()
        return result
    except Exception:
        if conn is not None:
            conn.rollback()
        raise
    finally:
        if conn is not None:
            conn.close()


def rebuild_position_lots_from_trade_events(repo: Any) -> dict[str, Any]:
    def _run(sqlite_repo: Any, conn: sqlite3.Connection | None) -> dict[str, Any]:
        if conn is not None:
            existing_rows = sqlite_repo.list_position_lots()
            events = sqlite_repo.list_trade_events(conn=conn)
            projection = project_position_lot_records_with_diagnostics(events)
            merged = _merge_preserved_position_lot_metadata(projection.lots, existing_rows)
            inserted = sqlite_repo.replace_position_lots(merged, conn=conn)
        else:
            existing_rows = sqlite_repo.list_position_lots()
            events = sqlite_repo.list_trade_events()
            projection = project_position_lot_records_with_diagnostics(events)
            merged = _merge_preserved_position_lot_metadata(projection.lots, existing_rows)
            inserted = sqlite_repo.replace_position_lots(merged)
        result = {
            "trade_event_count": int(len(events)),
            "position_lot_count": int(inserted),
            "preserved_sync_meta_record_count": int(
                sum(
                    1
                    for item in merged
                    if any((item.get("fields") or {}).get(key) not in (None, "") for key in PRESERVED_POSITION_LOT_META_KEYS)
                )
            ),
        }
        result.update(_projection_diagnostics_summary(projection.diagnostics))
        return result

    return _with_sqlite_repo_transaction(repo, _run)


def _persist_trade_event_object(repo: Any, event: TradeEvent) -> dict[str, Any]:
    def _run(sqlite_repo: Any, conn: sqlite3.Connection | None) -> dict[str, Any]:
        if conn is not None:
            created = sqlite_repo.upsert_trade_event(event, conn=conn)
            existing_rows = sqlite_repo.list_position_lots()
            projection = project_position_lot_records_with_diagnostics(sqlite_repo.list_trade_events(conn=conn))
            records = _merge_preserved_position_lot_metadata(projection.lots, existing_rows)
            lot_count = sqlite_repo.replace_position_lots(records, conn=conn)
        else:
            created = sqlite_repo.upsert_trade_event(event)
            existing_rows = sqlite_repo.list_position_lots()
            projection = project_position_lot_records_with_diagnostics(sqlite_repo.list_trade_events())
            records = _merge_preserved_position_lot_metadata(projection.lots, existing_rows)
            lot_count = sqlite_repo.replace_position_lots(records)
        payload = event.raw_payload or {}
        explicit_record_id = str(payload.get("record_id") or "").strip()
        record_id = explicit_record_id or next(
            (
                str(item.get("record_id") or "").strip()
                for item in records
                if str((item.get("fields") or {}).get("source_event_id") or "").strip() == str(event.event_id).strip()
            ),
            "",
        )
        result = {
            "event_id": event.event_id,
            "record_id": record_id or None,
            "created": bool(created),
            "position_lot_count": int(lot_count),
        }
        result.update(_projection_diagnostics_summary(projection.diagnostics))
        return result

    return _with_sqlite_repo_transaction(repo, _run)


def persist_trade_event(repo: Any, deal: Any) -> dict[str, Any]:
    return _persist_trade_event_object(repo, trade_event_from_normalized_deal(deal))


def persist_manual_open_event(repo: Any, command: OpenPositionCommand) -> dict[str, Any]:
    currency = resolve_open_currency(command.symbol, command.currency)
    event = TradeEvent(
        event_id=f"manual-open-{uuid.uuid4().hex}",
        source_type="manual_trade_event",
        source_name="cli_manual_open",
        broker=str(command.broker),
        account=str(command.account),
        symbol=str(command.symbol).strip().upper(),
        option_type=str(command.option_type),
        side="sell" if str(command.side).strip().lower() == "short" else "buy",
        position_effect="open",
        contracts=int(command.contracts),
        price=float(command.premium_per_share or 0.0),
        strike=(float(command.strike) if command.strike is not None else None),
        multiplier=(int(float(command.multiplier)) if command.multiplier is not None else None),
        expiration_ymd=(str(command.expiration_ymd or "").strip() or None),
        currency=currency,
        trade_time_ms=int(command.opened_at_ms or now_ms()),
        order_id=None,
        multiplier_source=("payload" if command.multiplier is not None else None),
        raw_payload={"source": "option_positions.py", "mode": "manual_open"},
    )
    return _persist_trade_event_object(repo, event)


def persist_manual_close_event(
    repo: Any,
    *,
    record_id: str,
    fields: dict[str, Any],
    contracts_to_close: int,
    close_price: float | None,
    close_reason: str,
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    broker = normalize_broker(fields.get("broker"))
    if not broker:
        raise ValueError(f"position lot missing broker: {record_id}")
    fields = _assert_position_lot_target_matches_current_state(
        repo,
        record_id=record_id,
        fields=fields,
        operation="manual_close",
    )
    multiplier = effective_multiplier(fields)
    strike = effective_strike(fields)
    target_source_event_id = str(fields.get("source_event_id") or "").strip()
    event = TradeEvent(
        event_id=f"manual-close-{record_id}-{uuid.uuid4().hex}",
        source_type="manual_trade_event",
        source_name="cli_manual_close",
        broker=broker,
        account=str(fields.get("account") or ""),
        symbol=str(fields.get("symbol") or "").strip().upper(),
        option_type=str(fields.get("option_type") or ""),
        side="buy" if str(fields.get("side") or "").strip().lower() == "short" else "sell",
        position_effect="close",
        contracts=int(contracts_to_close),
        price=float(close_price or 0.0),
        strike=(float(strike) if strike is not None else None),
        multiplier=(int(float(multiplier)) if multiplier is not None else None),
        expiration_ymd=effective_expiration_ymd(fields),
        currency=str(fields.get("currency") or "").strip().upper(),
        trade_time_ms=int(as_of_ms or now_ms()),
        order_id=None,
        multiplier_source=("payload" if multiplier is not None else None),
        raw_payload={
            "source": "option_positions.py",
            "mode": "manual_close",
            "record_id": str(record_id),
            "close_target_source_event_id": target_source_event_id,
            "close_target_account": str(fields.get("account") or ""),
            "close_target_broker": broker,
            "close_reason": str(close_reason or ""),
        },
    )
    return _persist_trade_event_object(repo, event)


def persist_manual_void_event(
    repo: Any,
    *,
    target_event_id: str,
    void_reason: str,
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    sqlite_repo = require_option_positions_event_write_repo(repo)
    target = next(
        (
            item
            for item in sqlite_repo.list_trade_events()
            if str(item.get("event_id") or "").strip() == str(target_event_id or "").strip()
        ),
        None,
    )
    if target is None:
        raise ValueError(f"trade event not found: {target_event_id}")
    if str(target.get("position_effect") or "").strip().lower() == "void":
        raise ValueError(f"cannot void a void event: {target_event_id}")

    event = TradeEvent(
        event_id=f"manual-void-{target_event_id}-{uuid.uuid4().hex}",
        source_type="manual_trade_event",
        source_name="cli_manual_void",
        broker=str(target.get("broker") or ""),
        account=str(target.get("account") or ""),
        symbol=str(target.get("symbol") or "").strip().upper(),
        option_type=str(target.get("option_type") or ""),
        side=str(target.get("side") or "").strip().lower(),
        position_effect="void",
        contracts=0,
        price=0.0,
        strike=(float(target["strike"]) if target.get("strike") is not None else None),
        multiplier=(int(target["multiplier"]) if target.get("multiplier") is not None else None),
        expiration_ymd=(str(target.get("expiration_ymd") or "").strip() or None),
        currency=str(target.get("currency") or "").strip().upper(),
        trade_time_ms=int(as_of_ms or now_ms()),
        order_id=None,
        multiplier_source=None,
        raw_payload={
            "source": "option_positions.py",
            "mode": "manual_void",
            "void_target_event_id": str(target_event_id),
            "void_reason": str(void_reason or ""),
        },
    )
    return _persist_trade_event_object(repo, event)


def persist_manual_adjust_event(
    repo: Any,
    *,
    record_id: str,
    fields: dict[str, Any],
    contracts: int | None = None,
    strike: float | None = None,
    expiration_ymd: str | None = None,
    premium_per_share: float | None = None,
    multiplier: float | None = None,
    opened_at_ms: int | None = None,
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    fields = _assert_position_lot_target_matches_current_state(
        repo,
        record_id=record_id,
        fields=fields,
        operation="manual_adjust",
    )
    target_source_event_id = str(fields.get("source_event_id") or "").strip()
    patch = build_open_adjustment_patch(
        fields,
        contracts=contracts,
        strike=strike,
        expiration_ymd=expiration_ymd,
        premium_per_share=premium_per_share,
        multiplier=multiplier,
        opened_at_ms=opened_at_ms,
        as_of_ms=as_of_ms,
    )
    event = TradeEvent(
        event_id=f"manual-adjust-{record_id}-{uuid.uuid4().hex}",
        source_type="manual_trade_event",
        source_name="cli_manual_adjust",
        broker=normalize_broker(fields.get("broker")),
        account=str(fields.get("account") or ""),
        symbol=str(fields.get("symbol") or "").strip().upper(),
        option_type=str(fields.get("option_type") or ""),
        side=str(fields.get("side") or "").strip().lower(),
        position_effect="adjust",
        contracts=0,
        price=0.0,
        strike=(float(fields["strike"]) if fields.get("strike") is not None else None),
        multiplier=(int(float(raw_multiplier)) if (raw_multiplier := safe_float(fields.get("multiplier"))) is not None else None),
        expiration_ymd=exp_ms_to_ymd(fields.get("expiration")),
        currency=str(fields.get("currency") or "").strip().upper(),
        trade_time_ms=int(as_of_ms or now_ms()),
        order_id=None,
        multiplier_source=None,
        raw_payload={
            "source": "option_positions.py",
            "mode": "manual_adjust",
            "record_id": str(record_id),
            "adjust_target_source_event_id": target_source_event_id or None,
            "patch": patch,
        },
    )
    result = _persist_trade_event_object(repo, event)
    result["record_id"] = str(record_id)
    result["patch"] = patch
    return result


def build_expired_close_decisions(
    positions: list[dict[str, Any]],
    *,
    as_of_ms: int,
    grace_days: int,
) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    as_of_dt = exp_ms_to_datetime(as_of_ms)
    if as_of_dt is None:
        raise ValueError("invalid as_of_ms")
    cutoff_ms = int((as_of_dt.timestamp() - int(grace_days) * 86400) * 1000)

    for item in positions:
        fields = dict(item)
        record_id = str(fields.get("record_id") or "").strip()
        position_id = str(fields.get("position_id") or "").strip() or "(no position_id)"
        if not record_id:
            decisions.append(
                {
                    "record_id": "",
                    "position_id": position_id,
                    "expiration_ms": None,
                    "effective_exp_source": "none",
                    "should_close": False,
                    "reason": "missing record_id",
                    "patch": None,
                }
            )
            continue

        exp_ms, exp_source = effective_expiration(fields)
        if exp_ms is None:
            decisions.append(
                {
                    "record_id": record_id,
                    "position_id": position_id,
                    "expiration_ms": None,
                    "effective_exp_source": "none",
                    "should_close": False,
                    "reason": "missing expiration (field and note)",
                    "patch": None,
                }
            )
            continue

        should_close = int(exp_ms) <= cutoff_ms
        patch = (
            build_expire_auto_close_patch(
                fields,
                as_of_ms=as_of_ms,
                close_reason="expired",
                exp_source=exp_source,
                grace_days=grace_days,
            )
            if should_close
            else None
        )
        decisions.append(
            {
                "record_id": record_id,
                "position_id": position_id,
                "expiration_ms": int(exp_ms),
                "effective_exp_source": exp_source,
                "should_close": should_close,
                "reason": (
                    f"expired: exp={exp_ms_to_ymd(exp_ms) or exp_ms} "
                    f"grace_days={grace_days} as_of={as_of_dt.date().isoformat()}"
                ),
                "patch": patch,
            }
        )
    return decisions


def auto_close_expired_positions(
    repo: OptionPositionsRepoLike,
    positions: list[dict[str, Any]],
    *,
    as_of_ms: int,
    grace_days: int,
    max_close: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    decisions = build_expired_close_decisions(positions, as_of_ms=as_of_ms, grace_days=grace_days)
    to_close = [d for d in decisions if bool(d.get("should_close")) and d.get("record_id")]
    applied: list[dict[str, Any]] = []
    errors: list[str] = []
    if len(to_close) > int(max_close):
        return decisions, applied, [f"too many to close: {len(to_close)} > max_close={max_close}; abort"]
    for decision in to_close:
        try:
            fields = repo.get_record_fields(str(decision["record_id"]))
            contracts_to_close = effective_contracts_open(fields)
            if contracts_to_close <= 0:
                errors.append(
                    f"{decision.get('record_id')} {decision.get('position_id')}: contracts_open resolved to <= 0"
                )
                continue
            persist_manual_close_event(
                repo,
                record_id=str(decision["record_id"]),
                fields=fields,
                contracts_to_close=contracts_to_close,
                close_price=None,
                close_reason="expired",
                as_of_ms=as_of_ms,
            )
            applied.append(decision)
        except Exception as exc:
            errors.append(f"{decision.get('record_id')} {decision.get('position_id')}: {exc}")
    return decisions, applied, errors
