from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from scripts.feishu_bitable import bitable_list_records, get_tenant_access_token, parse_note_kv, safe_float
from scripts.option_positions_core.domain import (
    OpenPositionCommand,
    build_expire_auto_close_patch,
    effective_expiration,
    exp_ms_to_datetime,
    normalize_broker,
    now_ms,
)
from scripts.option_positions_core.ledger import TradeEvent, project_position_lot_records, trade_event_from_normalized_deal


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
        normalized_fields = dict(fields)
        normalized_fields["broker"] = broker
        normalized.append({"record_id": record_id, "fields": normalized_fields})
    if skipped:
        print(f"[WARN] option_positions bootstrap skipped {skipped} rows without broker/market", file=sys.stderr)
    return normalized


class SQLiteOptionPositionsRepository:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

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
                  updated_at_ms INTEGER NOT NULL
                )
                """
            )
            conn.commit()

    def count_position_lots(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM position_lots").fetchone()
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

    def upsert_trade_event(self, event: TradeEvent) -> bool:
        payload = event.to_dict()
        ts = int(now_ms())
        trade_time_ms = int(event.trade_time_ms or 0)
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT event_id FROM trade_events WHERE event_id = ?",
                (str(event.event_id),),
            ).fetchone()
            conn.execute(
                """
                INSERT OR REPLACE INTO trade_events (
                  event_id, event_json, trade_time_ms, created_at_ms, updated_at_ms
                ) VALUES (
                  ?, ?, ?, COALESCE((SELECT created_at_ms FROM trade_events WHERE event_id = ?), ?), ?
                )
                """,
                (
                    str(event.event_id),
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    trade_time_ms,
                    str(event.event_id),
                    ts,
                    ts,
                ),
            )
            conn.commit()
        return existing is None

    def list_trade_events(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
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

    def replace_position_lots(self, records: list[dict[str, Any]]) -> int:
        ts = int(now_ms())
        with self._connect() as conn:
            conn.execute("DELETE FROM position_lots")
            for item in records:
                record_id = str(item.get("record_id") or "").strip()
                fields = item.get("fields") or {}
                if not record_id or not isinstance(fields, dict):
                    continue
                conn.execute(
                    """
                    INSERT INTO position_lots (record_id, fields_json, source_event_id, updated_at_ms)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        record_id,
                        json.dumps(fields, ensure_ascii=False, sort_keys=True),
                        (str(fields.get("source_event_id")) if fields.get("source_event_id") else None),
                        ts,
                    ),
                )
            conn.commit()
        return len(records)

    def list_position_lots(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT record_id, fields_json
                FROM position_lots
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

    def get_position_lot_fields(self, record_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT fields_json
                FROM position_lots
                WHERE record_id = ?
                """,
                (str(record_id),),
            ).fetchone()
        if row is None:
            raise ValueError(f"position lot not found: {record_id}")
        fields = json.loads(str(row["fields_json"]) or "{}")
        return fields if isinstance(fields, dict) else {}

    def list_records(self, *, page_size: int = 500) -> list[dict[str, Any]]:
        return self.list_position_lots()

    def get_record_fields(self, record_id: str) -> dict[str, Any]:
        return self.get_position_lot_fields(record_id)


OptionPositionsRepository = SQLiteOptionPositionsRepository


def load_option_positions_repo(data_config: Path) -> SQLiteOptionPositionsRepository:
    repo = SQLiteOptionPositionsRepository(resolve_option_positions_sqlite_path(data_config))
    if repo.count_position_lots() > 0:
        return repo

    feishu_ref = _try_load_table_ref(data_config)
    if feishu_ref is not None:
        try:
            repo.replace_position_lots(_normalize_bootstrap_records(_list_feishu_option_position_records(feishu_ref)))
        except Exception as exc:
            print(
                f"[WARN] option_positions bootstrap skipped for {repo.db_path}: {exc}",
                file=sys.stderr,
            )

    if repo.count_position_lots() == 0 and repo.count_legacy_records() > 0:
        try:
            repo.replace_position_lots(_normalize_bootstrap_records(repo.list_legacy_records()))
        except Exception as exc:
            print(
                f"[WARN] option_positions legacy migration skipped for {repo.db_path}: {exc}",
                file=sys.stderr,
            )
    return repo


def _persist_trade_event_object(repo: Any, event: TradeEvent) -> dict[str, Any]:
    sqlite_repo = getattr(repo, "primary_repo", repo)
    if not isinstance(sqlite_repo, SQLiteOptionPositionsRepository):
        raise TypeError("event persistence requires SQLiteOptionPositionsRepository or primary_repo wrapper")
    created = sqlite_repo.upsert_trade_event(event)
    records = project_position_lot_records(sqlite_repo.list_trade_events())
    lot_count = sqlite_repo.replace_position_lots(records)
    return {
        "event_id": event.event_id,
        "created": bool(created),
        "position_lot_count": int(lot_count),
    }


def persist_trade_event(repo: Any, deal: Any) -> dict[str, Any]:
    return _persist_trade_event_object(repo, trade_event_from_normalized_deal(deal))


def persist_manual_open_event(repo: Any, command: OpenPositionCommand) -> dict[str, Any]:
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
        currency=str(command.currency).strip().upper(),
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
    exp_ms, _exp_source = effective_expiration(fields)
    exp_dt = exp_ms_to_datetime(exp_ms)
    multiplier = safe_float(fields.get("multiplier"))
    if multiplier is None:
        multiplier = safe_float(parse_note_kv(fields.get("note") or "", "multiplier"))
    strike = safe_float(fields.get("strike"))
    if strike is None:
        strike = safe_float(parse_note_kv(fields.get("note") or "", "strike"))
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
        expiration_ymd=(exp_dt.date().isoformat() if exp_dt is not None else None),
        currency=str(fields.get("currency") or "").strip().upper(),
        trade_time_ms=int(as_of_ms or now_ms()),
        order_id=None,
        multiplier_source=("payload" if multiplier is not None else None),
        raw_payload={
            "source": "option_positions.py",
            "mode": "manual_close",
            "record_id": str(record_id),
            "close_reason": str(close_reason or ""),
        },
    )
    return _persist_trade_event_object(repo, event)


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

        exp_dt = exp_ms_to_datetime(exp_ms)
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
                    f"expired: exp={exp_dt.date().isoformat() if exp_dt else exp_ms} "
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
            persist_manual_close_event(
                repo,
                record_id=str(decision["record_id"]),
                fields=fields,
                contracts_to_close=int(fields.get("contracts_open") or 0),
                close_price=None,
                close_reason="expired",
                as_of_ms=as_of_ms,
            )
            applied.append(decision)
        except Exception as exc:
            errors.append(f"{decision.get('record_id')} {decision.get('position_id')}: {exc}")
    return decisions, applied, errors
