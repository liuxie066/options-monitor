from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from scripts.feishu_bitable import (
    bitable_create_record,
    bitable_list_records,
    bitable_update_record,
    get_tenant_access_token,
)
from scripts.option_positions_core.domain import (
    OpenPositionCommand,
    build_buy_to_close_patch,
    build_expire_auto_close_patch,
    build_open_fields,
    effective_expiration,
    exp_ms_to_datetime,
    now_ms,
)


REPO_BASE = Path(__file__).resolve().parents[2]
BACKUP_STATUS_DISABLED = "disabled"
BACKUP_STATUS_PENDING = "pending"
BACKUP_STATUS_SYNCED = "synced"
BACKUP_STATUS_ERROR = "error"
_KEEP = object()


@dataclass(frozen=True)
class OptionPositionsTableRef:
    app_id: str
    app_secret: str
    app_token: str
    table_id: str


class OptionPositionsRepoLike(Protocol):
    def list_records(self, *, page_size: int = 500) -> list[dict[str, Any]]: ...
    def create_record(self, fields: dict[str, Any]) -> dict[str, Any]: ...
    def update_record(self, record_id: str, fields: dict[str, Any]) -> dict[str, Any]: ...
    def get_record_fields(self, record_id: str) -> dict[str, Any]: ...


def _load_pm_config(pm_config: Path) -> dict[str, Any]:
    cfg = json.loads(pm_config.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise SystemExit("pm config must be a JSON object")
    return cfg


def _get_feishu_cfg(cfg: dict[str, Any], *, allow_missing: bool) -> dict[str, Any] | None:
    raw = cfg.get("feishu")
    if raw is None:
        return None if allow_missing else {}
    if not isinstance(raw, dict):
        raise SystemExit("pm config feishu must be a JSON object")
    return raw


def _load_table_ref_from_cfg(cfg: dict[str, Any]) -> OptionPositionsTableRef:
    feishu_cfg = _get_feishu_cfg(cfg, allow_missing=False) or {}
    app_id = feishu_cfg.get("app_id")
    app_secret = feishu_cfg.get("app_secret")
    ref = (feishu_cfg.get("tables", {}) or {}).get("option_positions")
    if not (app_id and app_secret and ref and "/" in ref):
        raise SystemExit("pm config missing feishu app_id/app_secret/option_positions")
    app_token, table_id = ref.split("/", 1)
    return OptionPositionsTableRef(str(app_id), str(app_secret), str(app_token), str(table_id))


def load_table_ref(pm_config: Path) -> OptionPositionsTableRef:
    cfg = _load_pm_config(pm_config)
    return _load_table_ref_from_cfg(cfg)


def _try_load_table_ref(pm_config: Path) -> OptionPositionsTableRef | None:
    cfg = _load_pm_config(pm_config)
    feishu_cfg = _get_feishu_cfg(cfg, allow_missing=True)
    if feishu_cfg is None or feishu_cfg == {}:
        return None
    return _load_table_ref_from_cfg(cfg)


def resolve_option_positions_sqlite_path(pm_config: Path) -> Path:
    cfg = _load_pm_config(pm_config)
    raw = ((cfg.get("option_positions") or {}) if isinstance(cfg.get("option_positions"), dict) else {}).get("sqlite_path")
    if raw is None or not str(raw).strip():
        path = (REPO_BASE / "output_shared" / "state" / "option_positions.sqlite3").resolve()
    else:
        path = Path(str(raw))
        if not path.is_absolute():
            path = (REPO_BASE / path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class FeishuOptionPositionsRepository:
    def __init__(self, table_ref: OptionPositionsTableRef):
        self.table_ref = table_ref
        self._token: str | None = None

    @property
    def token(self) -> str:
        if not self._token:
            self._token = get_tenant_access_token(self.table_ref.app_id, self.table_ref.app_secret)
        return self._token

    def list_records(self, *, page_size: int = 500) -> list[dict[str, Any]]:
        return bitable_list_records(self.token, self.table_ref.app_token, self.table_ref.table_id, page_size=page_size)

    def create_record(self, fields: dict[str, Any]) -> dict[str, Any]:
        return bitable_create_record(self.token, self.table_ref.app_token, self.table_ref.table_id, fields)

    def update_record(self, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        return bitable_update_record(self.token, self.table_ref.app_token, self.table_ref.table_id, record_id, fields)

    def get_record_fields(self, record_id: str) -> dict[str, Any]:
        for item in self.list_records(page_size=500):
            if str(item.get("record_id") or item.get("id") or "") == str(record_id):
                fields = item.get("fields") or {}
                if isinstance(fields, dict):
                    return fields
        raise ValueError(f"record not found: {record_id}")


class SQLiteOptionPositionsRepository:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS option_positions (
                  record_id TEXT PRIMARY KEY,
                  fields_json TEXT NOT NULL,
                  created_at_ms INTEGER NOT NULL,
                  updated_at_ms INTEGER NOT NULL,
                  backup_status TEXT NOT NULL,
                  backup_record_id TEXT,
                  backup_synced_at_ms INTEGER,
                  backup_error TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_option_positions_backup_status ON option_positions(backup_status, updated_at_ms)"
            )
            conn.commit()

    def count_records(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM option_positions").fetchone()
        return int((row["cnt"] if row is not None else 0) or 0)

    def _entry_from_row(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        fields = json.loads(str(row["fields_json"]) or "{}")
        if not isinstance(fields, dict):
            fields = {}
        return {
            "record_id": str(row["record_id"]),
            "fields": fields,
            "created_at_ms": int(row["created_at_ms"] or 0),
            "updated_at_ms": int(row["updated_at_ms"] or 0),
            "backup_status": str(row["backup_status"] or BACKUP_STATUS_PENDING),
            "backup_record_id": (str(row["backup_record_id"]) if row["backup_record_id"] else None),
            "backup_synced_at_ms": (int(row["backup_synced_at_ms"]) if row["backup_synced_at_ms"] is not None else None),
            "backup_error": (str(row["backup_error"]) if row["backup_error"] else None),
        }

    def get_entry(self, record_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT record_id, fields_json, created_at_ms, updated_at_ms,
                       backup_status, backup_record_id, backup_synced_at_ms, backup_error
                FROM option_positions
                WHERE record_id = ?
                """,
                (str(record_id),),
            ).fetchone()
        entry = self._entry_from_row(row)
        if entry is None:
            raise ValueError(f"record not found: {record_id}")
        return entry

    def list_records(self, *, page_size: int = 500) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT record_id, fields_json, created_at_ms, updated_at_ms,
                       backup_status, backup_record_id, backup_synced_at_ms, backup_error
                FROM option_positions
                ORDER BY updated_at_ms DESC, record_id DESC
                """,
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            entry = self._entry_from_row(row)
            if entry is None:
                continue
            out.append({"record_id": entry["record_id"], "fields": entry["fields"]})
        return out

    def get_record_fields(self, record_id: str) -> dict[str, Any]:
        return dict(self.get_entry(record_id).get("fields") or {})

    def create_record(
        self,
        fields: dict[str, Any],
        *,
        record_id: str | None = None,
        backup_status: str = BACKUP_STATUS_PENDING,
        backup_record_id: str | None = None,
        backup_synced_at_ms: int | None = None,
        backup_error: str | None = None,
    ) -> dict[str, Any]:
        rid = str(record_id or f"op_{uuid.uuid4().hex}")
        ts = int(now_ms())
        payload = json.dumps(fields, ensure_ascii=False, sort_keys=True)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO option_positions (
                  record_id, fields_json, created_at_ms, updated_at_ms,
                  backup_status, backup_record_id, backup_synced_at_ms, backup_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rid,
                    payload,
                    ts,
                    ts,
                    str(backup_status),
                    (str(backup_record_id) if backup_record_id else None),
                    (int(backup_synced_at_ms) if backup_synced_at_ms is not None else None),
                    (str(backup_error) if backup_error else None),
                ),
            )
            conn.commit()
        return {"record": {"record_id": rid}}

    def update_record(
        self,
        record_id: str,
        fields: dict[str, Any],
        *,
        backup_status: str | object = _KEEP,
        backup_record_id: str | object = _KEEP,
        backup_synced_at_ms: int | object = _KEEP,
        backup_error: str | object = _KEEP,
    ) -> dict[str, Any]:
        current = self.get_entry(record_id)
        merged_fields = dict(current.get("fields") or {})
        merged_fields.update(fields)
        new_backup_status = current.get("backup_status") if backup_status is _KEEP else str(backup_status)
        new_backup_record_id = current.get("backup_record_id") if backup_record_id is _KEEP else (
            str(backup_record_id) if backup_record_id else None
        )
        new_backup_synced_at_ms = current.get("backup_synced_at_ms") if backup_synced_at_ms is _KEEP else (
            int(backup_synced_at_ms) if backup_synced_at_ms is not None else None
        )
        new_backup_error = current.get("backup_error") if backup_error is _KEEP else (str(backup_error) if backup_error else None)
        ts = int(now_ms())
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE option_positions
                SET fields_json = ?, updated_at_ms = ?, backup_status = ?, backup_record_id = ?,
                    backup_synced_at_ms = ?, backup_error = ?
                WHERE record_id = ?
                """,
                (
                    json.dumps(merged_fields, ensure_ascii=False, sort_keys=True),
                    ts,
                    new_backup_status,
                    new_backup_record_id,
                    new_backup_synced_at_ms,
                    new_backup_error,
                    str(record_id),
                ),
            )
            conn.commit()
        return {"record": {"record_id": str(record_id)}}

    def set_backup_state(
        self,
        record_id: str,
        *,
        backup_status: str,
        backup_record_id: str | None = None,
        backup_synced_at_ms: int | None = None,
        backup_error: str | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"backup_status": str(backup_status)}
        kwargs["backup_record_id"] = backup_record_id
        kwargs["backup_synced_at_ms"] = backup_synced_at_ms
        kwargs["backup_error"] = backup_error
        return self.update_record(str(record_id), {}, **kwargs)

    def list_backup_candidates(self, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT record_id, fields_json, created_at_ms, updated_at_ms,
                       backup_status, backup_record_id, backup_synced_at_ms, backup_error
                FROM option_positions
                WHERE backup_status != ?
                ORDER BY updated_at_ms ASC, record_id ASC
                LIMIT ?
                """,
                (BACKUP_STATUS_SYNCED, int(limit)),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            entry = self._entry_from_row(row)
            if entry is not None:
                out.append(entry)
        return out

    def import_backup_records(self, records: list[dict[str, Any]]) -> int:
        imported = 0
        ts = int(now_ms())
        with self._connect() as conn:
            for item in records:
                record_id = str(item.get("record_id") or item.get("id") or "").strip()
                fields = item.get("fields") or {}
                if not record_id or not isinstance(fields, dict):
                    continue
                conn.execute(
                    """
                    INSERT OR REPLACE INTO option_positions (
                      record_id, fields_json, created_at_ms, updated_at_ms,
                      backup_status, backup_record_id, backup_synced_at_ms, backup_error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record_id,
                        json.dumps(fields, ensure_ascii=False, sort_keys=True),
                        int(fields.get("opened_at") or ts),
                        int(fields.get("last_action_at") or fields.get("opened_at") or ts),
                        BACKUP_STATUS_SYNCED,
                        record_id,
                        ts,
                        None,
                    ),
                )
                imported += 1
            conn.commit()
        return imported


class PrimaryWithBackupOptionPositionsRepository:
    def __init__(
        self,
        *,
        primary_repo: SQLiteOptionPositionsRepository,
        backup_repo: FeishuOptionPositionsRepository | None = None,
    ):
        self.primary_repo = primary_repo
        self.backup_repo = backup_repo

    def list_records(self, *, page_size: int = 500) -> list[dict[str, Any]]:
        return self.primary_repo.list_records(page_size=page_size)

    def get_record_fields(self, record_id: str) -> dict[str, Any]:
        return self.primary_repo.get_record_fields(record_id)

    def _sync_entry(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        if self.backup_repo is None:
            self.primary_repo.set_backup_state(
                str(entry["record_id"]),
                backup_status=BACKUP_STATUS_DISABLED,
                backup_record_id=(entry.get("backup_record_id") or None),
                backup_synced_at_ms=None,
                backup_error=None,
            )
            return None

        rid = str(entry["record_id"])
        fields = dict(entry.get("fields") or {})
        backup_record_id = entry.get("backup_record_id")
        if backup_record_id:
            res = self.backup_repo.update_record(str(backup_record_id), fields)
            synced_record_id = str(backup_record_id)
        else:
            res = self.backup_repo.create_record(fields)
            record = (res.get("record") or {}) if isinstance(res, dict) else {}
            synced_record_id = str((record or {}).get("record_id") or "").strip()
            if not synced_record_id:
                raise RuntimeError("backup create missing record_id")

        self.primary_repo.set_backup_state(
            rid,
            backup_status=BACKUP_STATUS_SYNCED,
            backup_record_id=synced_record_id,
            backup_synced_at_ms=int(now_ms()),
            backup_error=None,
        )
        return res if isinstance(res, dict) else None

    def create_record(self, fields: dict[str, Any]) -> dict[str, Any]:
        status = BACKUP_STATUS_PENDING if self.backup_repo is not None else BACKUP_STATUS_DISABLED
        res = self.primary_repo.create_record(fields, backup_status=status)
        rid = str(((res.get("record") or {}) if isinstance(res, dict) else {}).get("record_id") or "")
        entry = self.primary_repo.get_entry(rid)
        try:
            self._sync_entry(entry)
        except Exception as exc:
            self.primary_repo.set_backup_state(
                rid,
                backup_status=BACKUP_STATUS_ERROR,
                backup_record_id=(entry.get("backup_record_id") or None),
                backup_synced_at_ms=None,
                backup_error=str(exc),
            )
        return res

    def update_record(self, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        entry_before = self.primary_repo.get_entry(record_id)
        status = BACKUP_STATUS_PENDING if self.backup_repo is not None else BACKUP_STATUS_DISABLED
        res = self.primary_repo.update_record(
            record_id,
            fields,
            backup_status=status,
            backup_error=None,
        )
        entry_after = self.primary_repo.get_entry(record_id)
        try:
            self._sync_entry(entry_after)
        except Exception as exc:
            self.primary_repo.set_backup_state(
                str(record_id),
                backup_status=BACKUP_STATUS_ERROR,
                backup_record_id=(entry_before.get("backup_record_id") or entry_after.get("backup_record_id") or None),
                backup_synced_at_ms=None,
                backup_error=str(exc),
            )
        return res

    def sync_backup(self, *, limit: int = 200, record_id: str | None = None, dry_run: bool = False) -> dict[str, Any]:
        if self.backup_repo is None:
            return {"ok": True, "mode": "disabled", "processed": 0, "synced": 0, "failed": 0, "records": []}

        if record_id:
            candidates = [self.primary_repo.get_entry(record_id)]
        else:
            candidates = self.primary_repo.list_backup_candidates(limit=limit)

        processed = 0
        synced = 0
        failed = 0
        rows: list[dict[str, Any]] = []
        for entry in candidates:
            processed += 1
            rid = str(entry["record_id"])
            if dry_run:
                rows.append(
                    {
                        "record_id": rid,
                        "backup_status": entry.get("backup_status"),
                        "backup_record_id": entry.get("backup_record_id"),
                        "action": ("update" if entry.get("backup_record_id") else "create"),
                    }
                )
                continue
            try:
                self._sync_entry(entry)
                synced += 1
                updated = self.primary_repo.get_entry(rid)
                rows.append(
                    {
                        "record_id": rid,
                        "backup_status": updated.get("backup_status"),
                        "backup_record_id": updated.get("backup_record_id"),
                        "action": ("update" if entry.get("backup_record_id") else "create"),
                    }
                )
            except Exception as exc:
                failed += 1
                self.primary_repo.set_backup_state(
                    rid,
                    backup_status=BACKUP_STATUS_ERROR,
                    backup_record_id=(entry.get("backup_record_id") or None),
                    backup_synced_at_ms=None,
                    backup_error=str(exc),
                )
                rows.append(
                    {
                        "record_id": rid,
                        "backup_status": BACKUP_STATUS_ERROR,
                        "backup_record_id": entry.get("backup_record_id"),
                        "action": ("update" if entry.get("backup_record_id") else "create"),
                        "error": str(exc),
                    }
                )
        return {
            "ok": True,
            "mode": ("dry_run" if dry_run else "apply"),
            "processed": processed,
            "synced": synced,
            "failed": failed,
            "records": rows,
        }


OptionPositionsRepository = PrimaryWithBackupOptionPositionsRepository


def load_option_positions_repo(pm_config: Path) -> PrimaryWithBackupOptionPositionsRepository:
    sqlite_repo = SQLiteOptionPositionsRepository(resolve_option_positions_sqlite_path(pm_config))
    feishu_ref = _try_load_table_ref(pm_config)
    feishu_repo = FeishuOptionPositionsRepository(feishu_ref) if feishu_ref is not None else None

    if sqlite_repo.count_records() == 0 and feishu_repo is not None:
        try:
            records = feishu_repo.list_records(page_size=500)
            sqlite_repo.import_backup_records(records)
        except Exception as exc:
            print(
                f"[WARN] option_positions bootstrap skipped for {sqlite_repo.db_path}: {exc}",
                file=sys.stderr,
            )

    return PrimaryWithBackupOptionPositionsRepository(primary_repo=sqlite_repo, backup_repo=feishu_repo)


def open_position(repo: OptionPositionsRepoLike, command: OpenPositionCommand) -> dict[str, Any]:
    fields = build_open_fields(command)
    return repo.create_record(fields)


def buy_to_close_position(
    repo: OptionPositionsRepoLike,
    *,
    record_id: str,
    contracts_to_close: int,
    close_price: float | None = None,
    close_reason: str = "manual_buy_to_close",
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    fields = repo.get_record_fields(record_id)
    patch = build_buy_to_close_patch(
        fields,
        contracts_to_close=int(contracts_to_close),
        close_price=close_price,
        close_reason=close_reason,
        as_of_ms=as_of_ms,
    )
    return repo.update_record(record_id, patch)


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
        reason = (
            f"expired: exp={exp_dt.date().isoformat() if exp_dt else exp_ms} "
            f"grace_days={grace_days} as_of={as_of_dt.date().isoformat()}"
        )
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
                "reason": reason,
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
    errors: list[str] = []
    applied: list[dict[str, Any]] = []
    if len(to_close) > int(max_close):
        return decisions, applied, [f"too many to close: {len(to_close)} > max_close={max_close}; abort"]
    for decision in to_close:
        patch = decision.get("patch")
        if not isinstance(patch, dict):
            continue
        try:
            repo.update_record(str(decision["record_id"]), patch)
            applied.append(decision)
        except Exception as exc:
            errors.append(f"{decision.get('record_id')} {decision.get('position_id')}: {exc}")
    return decisions, applied, errors
