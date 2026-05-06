#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Optional, TypedDict, cast


REPO_BASE = Path(__file__).resolve().parents[1]
if str(REPO_BASE) not in sys.path:
    sys.path.insert(0, str(REPO_BASE))

from scripts.config_loader import resolve_data_config_path
from scripts.option_positions_core.domain import normalize_broker, now_ms
from scripts.option_positions_core.service import resolve_option_positions_sqlite_path

TABLE_NAMES = ("position_lots", "option_positions")


class PersistedRow(TypedDict):
    record_id: str
    fields: dict[str, object]


class SampleRow(TypedDict, total=False):
    record_id: str
    symbol: str
    account: str
    market: str
    broker: str
    broker_to_set: str


class PlannedUpdate(TypedDict):
    table: str
    record_id: str
    fields: dict[str, object]


class TableAudit(TypedDict):
    exists: bool
    total_rows: int
    candidate_rows: int
    conflict_rows: int
    already_canonical_rows: int
    missing_both_rows: int
    candidate_samples: list[SampleRow]
    conflict_samples: list[SampleRow]


class AuditSummary(TypedDict):
    candidate_rows: int
    conflict_rows: int
    tables_with_candidates: list[str]


class BrokerBackfillAudit(TypedDict):
    resolved_data_config: str
    db_path: str
    sample_limit: int
    tables: dict[str, TableAudit]
    updates: list[PlannedUpdate]
    summary: AuditSummary
    missing_db: bool


class BrokerBackfillApplyResult(TypedDict):
    mode: str
    backup_path: str | None
    updated_rows: int
    pre_audit: BrokerBackfillAudit
    post_audit: BrokerBackfillAudit


def _empty_table_audit(*, exists: bool) -> TableAudit:
    return {
        "exists": exists,
        "total_rows": 0,
        "candidate_rows": 0,
        "conflict_rows": 0,
        "already_canonical_rows": 0,
        "missing_both_rows": 0,
        "candidate_samples": [],
        "conflict_samples": [],
    }


def _missing_db_audit(*, resolved_data_config: Path, db_path: Path, sample_limit: int) -> BrokerBackfillAudit:
    return {
        "resolved_data_config": str(resolved_data_config),
        "db_path": str(db_path),
        "sample_limit": max(int(sample_limit), 0),
        "tables": {table_name: _empty_table_audit(exists=False) for table_name in TABLE_NAMES},
        "updates": [],
        "summary": {
            "candidate_rows": 0,
            "conflict_rows": 0,
            "tables_with_candidates": [],
        },
        "missing_db": True,
    }


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _row_value(row: sqlite3.Row, key: str) -> object:
    return cast(object, row[key])


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = cast(
        Optional[sqlite3.Row],
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
            (str(table_name),),
        ).fetchone(),
    )
    return row is not None


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = cast(list[sqlite3.Row], conn.execute(f"PRAGMA table_info({table_name})").fetchall())
    return {str(_row_value(row, "name")) for row in rows}


def _list_rows(conn: sqlite3.Connection, table_name: str) -> list[PersistedRow]:
    if table_name not in TABLE_NAMES:
        raise ValueError(f"unsupported table: {table_name}")
    if not _table_exists(conn, table_name):
        return []
    rows = cast(
        list[sqlite3.Row],
        conn.execute(
            f"SELECT record_id, fields_json FROM {table_name} ORDER BY record_id ASC"
        ).fetchall(),
    )
    out: list[PersistedRow] = []
    for row in rows:
        raw_fields_obj = cast(object, json.loads(str(_row_value(row, "fields_json")) or "{}"))
        out.append(
            {
                "record_id": str(_row_value(row, "record_id")),
                "fields": (cast(dict[str, object], raw_fields_obj) if isinstance(raw_fields_obj, dict) else {}),
            }
        )
    return out


def _plan_table_backfill(
    rows: list[PersistedRow],
    *,
    table_name: str,
    sample_limit: int,
) -> tuple[TableAudit, list[PlannedUpdate]]:
    updates: list[PlannedUpdate] = []
    candidate_samples: list[SampleRow] = []
    conflict_samples: list[SampleRow] = []
    candidate_rows = 0
    conflict_rows = 0
    already_canonical_rows = 0
    missing_both_rows = 0

    for item in rows:
        record_id = str(item.get("record_id") or "").strip()
        fields = dict(item.get("fields") or {})
        broker = str(fields.get("broker") or "").strip()
        market = str(fields.get("market") or "").strip()
        normalized_market = normalize_broker(market) if market else ""

        if not broker and normalized_market:
            candidate_rows += 1
            patched_fields = dict(fields)
            patched_fields["broker"] = normalized_market
            updates.append(
                {
                    "table": table_name,
                    "record_id": record_id,
                    "fields": patched_fields,
                }
            )
            if len(candidate_samples) < sample_limit:
                candidate_samples.append(
                    {
                        "record_id": record_id,
                        "symbol": str(fields.get("symbol") or ""),
                        "account": str(fields.get("account") or ""),
                        "market": market,
                        "broker_to_set": normalized_market,
                    }
                )
            continue

        if broker:
            already_canonical_rows += 1
            normalized_broker = normalize_broker(broker)
            if normalized_market and normalized_broker != normalized_market:
                conflict_rows += 1
                if len(conflict_samples) < sample_limit:
                    conflict_samples.append(
                        {
                            "record_id": record_id,
                            "symbol": str(fields.get("symbol") or ""),
                            "account": str(fields.get("account") or ""),
                            "broker": broker,
                            "market": market,
                        }
                    )
            continue

        missing_both_rows += 1

    return (
        {
            "exists": True,
            "total_rows": len(rows),
            "candidate_rows": candidate_rows,
            "conflict_rows": conflict_rows,
            "already_canonical_rows": already_canonical_rows,
            "missing_both_rows": missing_both_rows,
            "candidate_samples": candidate_samples,
            "conflict_samples": conflict_samples,
        },
        updates,
    )


def build_option_positions_broker_backfill_audit(
    *,
    base: Path = REPO_BASE,
    data_config: str | Path | None = None,
    sample_limit: int = 20,
) -> BrokerBackfillAudit:
    resolved_data_config = resolve_data_config_path(base=base, data_config=data_config)
    db_path = resolve_option_positions_sqlite_path(resolved_data_config)
    if not db_path.exists():
        return _missing_db_audit(
            resolved_data_config=resolved_data_config,
            db_path=db_path,
            sample_limit=sample_limit,
        )
    tables: dict[str, TableAudit] = {}
    updates: list[PlannedUpdate] = []

    with _connect(db_path) as conn:
        for table_name in TABLE_NAMES:
            if not _table_exists(conn, table_name):
                tables[table_name] = _empty_table_audit(exists=False)
                continue
            table_summary, table_updates = _plan_table_backfill(
                _list_rows(conn, table_name),
                table_name=table_name,
                sample_limit=max(int(sample_limit), 0),
            )
            tables[table_name] = table_summary
            updates.extend(table_updates)

    return {
        "resolved_data_config": str(resolved_data_config),
        "db_path": str(db_path),
        "sample_limit": max(int(sample_limit), 0),
        "tables": tables,
        "updates": updates,
        "summary": {
            "candidate_rows": len(updates),
            "conflict_rows": sum(int(item["conflict_rows"]) for item in tables.values()),
            "tables_with_candidates": [
                table_name
                for table_name, item in tables.items()
                if bool(item.get("candidate_rows"))
            ],
        },
        "missing_db": False,
    }


def apply_option_positions_broker_backfill(
    *,
    base: Path = REPO_BASE,
    data_config: str | Path | None = None,
    sample_limit: int = 20,
) -> BrokerBackfillApplyResult:
    audit = build_option_positions_broker_backfill_audit(
        base=base,
        data_config=data_config,
        sample_limit=sample_limit,
    )
    if audit["missing_db"]:
        raise FileNotFoundError(f"option positions sqlite db not found: {audit['db_path']}")
    db_path = Path(audit["db_path"]).resolve()
    updates = list(audit["updates"])
    backup_path: Path | None = None

    if updates:
        backup_path = db_path.with_name(f"{db_path.name}.broker-backfill-{now_ms()}.bak")
        _ = shutil.copy2(db_path, backup_path)
        with _connect(db_path) as conn:
            for item in updates:
                table_name = str(item["table"])
                record_id = str(item["record_id"])
                fields_json = json.dumps(item["fields"], ensure_ascii=False, sort_keys=True)
                columns = _table_columns(conn, table_name)
                if "updated_at_ms" in columns:
                    _ = conn.execute(
                        f"UPDATE {table_name} SET fields_json = ?, updated_at_ms = ? WHERE record_id = ?",
                        (fields_json, int(now_ms()), record_id),
                    )
                else:
                    _ = conn.execute(
                        f"UPDATE {table_name} SET fields_json = ? WHERE record_id = ?",
                        (fields_json, record_id),
                    )
            conn.commit()

    post_audit = build_option_positions_broker_backfill_audit(
        base=base,
        data_config=data_config,
        sample_limit=sample_limit,
    )
    return {
        "mode": "applied",
        "backup_path": (str(backup_path) if backup_path is not None else None),
        "updated_rows": len(updates),
        "pre_audit": audit,
        "post_audit": post_audit,
    }


def _render_text(payload: BrokerBackfillAudit) -> str:
    lines: list[str] = []
    summary = payload["summary"]
    lines.append(f"data_config: {payload.get('resolved_data_config')}")
    lines.append(f"sqlite_path: {payload.get('db_path')}")
    lines.append(
        (
            f"candidate_rows={int(summary.get('candidate_rows') or 0)} "
            f"conflict_rows={int(summary.get('conflict_rows') or 0)}"
        )
    )
    for table_name in TABLE_NAMES:
        table = payload["tables"].get(table_name)
        if table is None:
            continue
        exists = bool(table["exists"])
        lines.append(
            (
                f"[{table_name}] exists={str(exists).lower()} total={table['total_rows']} "
                f"candidates={table['candidate_rows']} conflicts={table['conflict_rows']} "
                f"canonical={table['already_canonical_rows']} missing_both={table['missing_both_rows']}"
            )
        )
        for sample in table["candidate_samples"]:
            lines.append(
                (
                    f"  - candidate {sample.get('record_id')} account={sample.get('account') or '-'} "
                    f"symbol={sample.get('symbol') or '-'} market={sample.get('market') or '-'} "
                    f"broker_to_set={sample.get('broker_to_set') or '-'}"
                )
            )
        for sample in table["conflict_samples"]:
            lines.append(
                (
                    f"  - conflict {sample.get('record_id')} account={sample.get('account') or '-'} "
                    f"symbol={sample.get('symbol') or '-'} broker={sample.get('broker') or '-'} "
                    f"market={sample.get('market') or '-'}"
                )
            )
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Audit and optionally backfill broker from legacy market in persisted option-position rows"
    )
    _ = ap.add_argument("--data-config", default=None, help="portfolio data config path; auto-resolves when omitted")
    _ = ap.add_argument("--sample-limit", type=int, default=20, help="max candidate/conflict samples per table")
    _ = ap.add_argument("--format", choices=["text", "json"], default="text")
    _ = ap.add_argument("--apply", action="store_true", help="write broker backfill into persisted sqlite tables")
    args = ap.parse_args()
    arg_map = vars(args)
    data_config = cast(Optional[str], args.data_config)
    sample_limit = int(cast(int, arg_map["sample_limit"]))
    output_format = cast(str, args.format)
    should_apply = bool(cast(bool, arg_map["apply"]))

    if should_apply:
        result = apply_option_positions_broker_backfill(
            base=REPO_BASE,
            data_config=data_config,
            sample_limit=sample_limit,
        )
        if output_format == "json":
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        print(f"[APPLIED] updated_rows={result['updated_rows']} backup_path={result.get('backup_path') or '-'}")
        print(_render_text(result["post_audit"]))
        return

    audit = build_option_positions_broker_backfill_audit(
        base=REPO_BASE,
        data_config=data_config,
        sample_limit=sample_limit,
    )
    if output_format == "json":
        print(json.dumps(audit, ensure_ascii=False, indent=2))
        return
    print("[DRY_RUN] no changes written")
    print(_render_text(audit))


if __name__ == "__main__":
    main()
