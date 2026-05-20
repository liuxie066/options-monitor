from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

from src.application.settings import build_effective_env


REPO_BASE = Path(__file__).resolve().parents[3]
LEDGER_DB_RELATIVE_PATH = Path("output_shared") / "state" / "option_positions.sqlite3"


@dataclass(frozen=True)
class LedgerStoreResolution:
    runtime_root: Path
    data_config_path: Path
    sqlite_path: Path
    runtime_root_source: str
    sqlite_path_source: str
    db_exists: bool
    db_size_bytes: int | None
    trade_event_count: int | None
    position_lot_count: int | None
    warnings: tuple[str, ...] = ()
    legacy_sqlite_path: Path | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "runtime_root": str(self.runtime_root),
            "data_config_path": str(self.data_config_path),
            "sqlite_path": str(self.sqlite_path),
            "runtime_root_source": self.runtime_root_source,
            "sqlite_path_source": self.sqlite_path_source,
            "db_exists": self.db_exists,
            "db_size_bytes": self.db_size_bytes,
            "trade_event_count": self.trade_event_count,
            "position_lot_count": self.position_lot_count,
            "legacy_sqlite_path": str(self.legacy_sqlite_path) if self.legacy_sqlite_path is not None else None,
            "warnings": list(self.warnings),
        }


@runtime_checkable
class _RepoWithLedgerStore(Protocol):
    ledger_store: LedgerStoreResolution


@runtime_checkable
class _RepoWithDbPath(Protocol):
    db_path: str | Path


@runtime_checkable
class _RepoWithCounts(Protocol):
    def count_trade_events(self) -> object: ...
    def count_position_lots(self) -> object: ...


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        payload = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    payload_map = cast(dict[object, object], payload)
    return {str(key): value for key, value in payload_map.items()}


def _configured_legacy_sqlite_path(cfg: dict[str, object]) -> Path | None:
    option_positions = cfg.get("option_positions")
    if not isinstance(option_positions, dict):
        return None
    option_positions_map = cast(dict[object, object], option_positions)
    option_positions_cfg = {str(key): value for key, value in option_positions_map.items()}
    raw = str(option_positions_cfg.get("sqlite_path") or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (REPO_BASE / path).resolve()


def _count_table(conn: sqlite3.Connection, table: str) -> int | None:
    row = cast(sqlite3.Row | None, conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (table,),
    ).fetchone())
    if row is None:
        return 0
    count_row = cast(sqlite3.Row | None, conn.execute(f"SELECT COUNT(*) AS cnt FROM {table}").fetchone())
    raw_count: object = count_row["cnt"] if count_row is not None else 0
    return _coerce_int(raw_count or 0)


def _sqlite_counts(path: Path) -> tuple[int | None, int | None]:
    if not path.exists():
        return None, None
    uri = f"file:{path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return _count_table(conn, "trade_events"), _count_table(conn, "position_lots")
        finally:
            conn.close()
    except Exception:
        return None, None


def _coerce_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        return int(value)
    return int(str(value))


def _resolve_runtime_root(
    *,
    data_config_path: Path,
    runtime_root: str | Path | None = None,
    config_path: str | Path | None = None,
) -> tuple[Path, str]:
    if runtime_root is not None and str(runtime_root).strip():
        return Path(runtime_root).expanduser().resolve(), "argument"

    env_root = str(build_effective_env().get("OM_RUNTIME_ROOT") or "").strip()
    if env_root:
        return Path(env_root).expanduser().resolve(), "env:OM_RUNTIME_ROOT"

    if config_path is not None and str(config_path).strip():
        resolved_config = Path(config_path).expanduser()
        if not resolved_config.is_absolute():
            resolved_config = resolved_config.resolve()
        return resolved_config.parent.resolve(), "runtime_config_dir"

    return data_config_path.parent.resolve(), "data_config_parent"


def resolve_ledger_store(
    data_config: str | Path,
    *,
    runtime_root: str | Path | None = None,
    config_path: str | Path | None = None,
) -> LedgerStoreResolution:
    data_config_path = Path(data_config).expanduser()
    if not data_config_path.is_absolute():
        data_config_path = data_config_path.resolve()
    cfg = _read_json_object(data_config_path)
    legacy_sqlite_path = _configured_legacy_sqlite_path(cfg)
    runtime_root_path, runtime_root_source = _resolve_runtime_root(
        data_config_path=data_config_path,
        runtime_root=runtime_root,
        config_path=config_path,
    )

    warnings: list[str] = []
    sqlite_path = (runtime_root_path / LEDGER_DB_RELATIVE_PATH).resolve()
    sqlite_path_source = "runtime_root"

    if legacy_sqlite_path is not None:
        warnings.append(
            (
                "option_positions.sqlite_path ignored; ledger DB is fixed under "
                "<runtime_root>/output_shared/state/option_positions.sqlite3"
            )
        )

    db_exists = sqlite_path.exists()
    db_size_bytes = sqlite_path.stat().st_size if db_exists else None
    trade_event_count, position_lot_count = _sqlite_counts(sqlite_path)
    return LedgerStoreResolution(
        runtime_root=runtime_root_path,
        data_config_path=data_config_path,
        sqlite_path=sqlite_path,
        runtime_root_source=runtime_root_source,
        sqlite_path_source=sqlite_path_source,
        db_exists=db_exists,
        db_size_bytes=db_size_bytes,
        trade_event_count=trade_event_count,
        position_lot_count=position_lot_count,
        warnings=tuple(warnings),
        legacy_sqlite_path=legacy_sqlite_path,
    )


def _mtime_utc(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    except Exception:
        return None


def _sqlite_file_payload(path: Path) -> dict[str, object]:
    resolved = path.resolve()
    exists = resolved.exists()
    trade_event_count, position_lot_count = _sqlite_counts(resolved)
    return {
        "path": str(resolved),
        "exists": exists,
        "db_size_bytes": resolved.stat().st_size if exists else None,
        "mtime_utc": _mtime_utc(resolved),
        "trade_event_count": trade_event_count,
        "position_lot_count": position_lot_count,
        "wal_exists": resolved.with_name(resolved.name + "-wal").exists(),
        "shm_exists": resolved.with_name(resolved.name + "-shm").exists(),
    }


def _candidate_has_rows(candidate: dict[str, object]) -> bool:
    for key in ("trade_event_count", "position_lot_count"):
        value = candidate.get(key)
        if isinstance(value, int) and value > 0:
            return True
    return False


def inspect_ledger_stores(
    data_config: str | Path,
    *,
    runtime_root: str | Path | None = None,
    config_path: str | Path | None = None,
) -> dict[str, object]:
    resolution = resolve_ledger_store(
        data_config,
        runtime_root=runtime_root,
        config_path=config_path,
    )
    candidate_map: dict[str, dict[str, object]] = {}
    ordered_paths: list[str] = []

    def add_candidate(role: str, path: Path) -> None:
        resolved = path.resolve()
        key = str(resolved)
        if key not in candidate_map:
            item = _sqlite_file_payload(resolved)
            item["roles"] = []
            item["is_active"] = resolved == resolution.sqlite_path.resolve()
            candidate_map[key] = item
            ordered_paths.append(key)
        roles = candidate_map[key].get("roles")
        if isinstance(roles, list) and role not in roles:
            roles.append(role)

    add_candidate("active", resolution.sqlite_path)
    if resolution.legacy_sqlite_path is not None:
        add_candidate("legacy_configured_sqlite_path", resolution.legacy_sqlite_path)
    add_candidate("repository_default_runtime_root", REPO_BASE / LEDGER_DB_RELATIVE_PATH)

    candidates = [candidate_map[key] for key in ordered_paths]
    existing_candidates = [item for item in candidates if bool(item.get("exists"))]
    populated_candidates = [item for item in candidates if _candidate_has_rows(item)]
    active_candidates = [item for item in candidates if bool(item.get("is_active"))]
    active_candidate = active_candidates[0] if active_candidates else _sqlite_file_payload(resolution.sqlite_path)
    active_has_rows = _candidate_has_rows(active_candidate)
    other_populated = [
        item for item in populated_candidates
        if not bool(item.get("is_active"))
    ]

    warnings = list(resolution.warnings)
    if len(existing_candidates) > 1:
        warnings.append("multiple ledger sqlite candidates exist; inspect paths before trusting online state")
    if len(populated_candidates) > 1:
        warnings.append("multiple ledger sqlite candidates contain rows; old and active stores may be diverged")
    if not active_has_rows and other_populated:
        warnings.append("active ledger sqlite has no rows while another candidate is populated")
    for item in other_populated:
        roles = item.get("roles")
        if isinstance(roles, list) and "legacy_configured_sqlite_path" in roles:
            warnings.append("deprecated option_positions.sqlite_path contains rows but is ignored")

    return {
        "schema_kind": "option_positions_store_inspect_v1",
        "active": resolution.to_dict(),
        "candidates": candidates,
        "summary": {
            "candidate_count": len(candidates),
            "existing_candidate_count": len(existing_candidates),
            "populated_candidate_count": len(populated_candidates),
            "active_has_rows": active_has_rows,
            "multiple_existing": len(existing_candidates) > 1,
            "multiple_populated": len(populated_candidates) > 1,
            "active_empty_but_other_populated": (not active_has_rows and bool(other_populated)),
        },
        "warnings": warnings,
    }


def ledger_store_payload(data_config: str | Path, repo: object | None = None) -> dict[str, object]:
    if isinstance(repo, _RepoWithLedgerStore):
        resolution = repo.ledger_store
    else:
        resolution = resolve_ledger_store(data_config)

    payload = resolution.to_dict()
    if isinstance(repo, _RepoWithDbPath):
        db_path = repo.db_path
        payload["sqlite_path"] = str(Path(db_path).resolve())
        payload["db_exists"] = Path(db_path).exists()
        payload["db_size_bytes"] = Path(db_path).stat().st_size if Path(db_path).exists() else None
    if isinstance(repo, _RepoWithCounts):
        try:
            payload["trade_event_count"] = _coerce_int(repo.count_trade_events())
        except Exception:
            pass
        try:
            payload["position_lot_count"] = _coerce_int(repo.count_position_lots())
        except Exception:
            pass
    return payload
