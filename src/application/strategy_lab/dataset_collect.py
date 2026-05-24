from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from src.application.strategy_lab.contracts import validate_strategy_type
from src.application.strategy_lab.dataset_contracts import (
    StrategyLabDataset,
    candidate_snapshot_to_dict,
)
from src.application.strategy_lab.evidence_loader import load_strategy_lab_evidence


def collect_strategy_lab_dataset(
    payload: Mapping[str, Any],
    *,
    repo_root: Path,
    runtime_root: Path,
    now_fn: Callable[[], datetime] | None = None,
) -> StrategyLabDataset:
    payload_dict = dict(payload)
    strategy_type = validate_strategy_type(str(payload_dict.get("strategy_type") or "sell_put"))
    market = str(payload_dict.get("market") or payload_dict.get("config_key") or "").strip().lower() or None
    account = str(payload_dict.get("account") or "").strip().lower() or None
    start_date = _text(payload_dict.get("start_date"))
    end_date = _text(payload_dict.get("end_date"))
    now = (now_fn or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)

    candidate_paths = _explicit_or_discovered_paths(
        payload_dict,
        keys=("candidate_path", "candidate_paths", "strategy_candidate_path", "strategy_candidate_paths"),
        runtime_root=runtime_root,
        account=account,
        strategy_type=strategy_type,
        kind="candidate",
    )
    reject_paths = _explicit_or_discovered_paths(
        payload_dict,
        keys=("reject_log_path", "reject_log_paths", "strategy_reject_log_path", "strategy_reject_log_paths"),
        runtime_root=runtime_root,
        account=account,
        strategy_type=strategy_type,
        kind="reject",
    )
    trace_paths = _explicit_or_discovered_paths(
        payload_dict,
        keys=("trace_path", "trace_paths", "strategy_trace_path", "strategy_trace_paths"),
        runtime_root=runtime_root,
        account=account,
        strategy_type=strategy_type,
        kind="trace",
    )
    replay_paths = _explicit_or_discovered_paths(
        payload_dict,
        keys=(
            "replay_path",
            "replay_paths",
            "strategy_replay_path",
            "strategy_replay_paths",
            "outcome_path",
            "outcome_paths",
            "strategy_outcome_path",
            "strategy_outcome_paths",
        ),
        runtime_root=runtime_root,
        account=account,
        strategy_type=strategy_type,
        kind="outcome",
    )

    evidence = load_strategy_lab_evidence(
        candidate_paths=candidate_paths,
        reject_log_paths=reject_paths,
        trace_paths=trace_paths,
        replay_paths=replay_paths,
        base=runtime_root,
        sample_limit=_positive_int(payload_dict.get("sample_limit"), default=5),
    )
    sqlite_path = _resolve_sqlite_path(payload_dict, runtime_root=runtime_root)
    trade_events, position_lots, ledger_warnings = _read_ledger(sqlite_path, account=account, strategy_type=strategy_type)
    outcomes = _build_outcomes(evidence.replay_rows, trade_events, position_lots)
    warnings = [*evidence.warnings, *ledger_warnings]
    if not candidate_paths:
        warnings.append("strategy_lab_candidate_sources_empty")
    if not outcomes:
        warnings.append("strategy_lab_outcomes_empty")

    scope = {
        "market": market,
        "account": account,
        "strategy_type": strategy_type,
        "start_date": start_date,
        "end_date": end_date,
    }
    sources = {
        "runtime_root": str(runtime_root),
        "repo_root": str(repo_root),
        "artifacts": [
            {
                "kind": artifact.kind,
                "path": artifact.path,
                "row_count": artifact.row_count,
                "sample_rows": [dict(row) for row in artifact.sample_rows],
            }
            for artifact in evidence.artifacts
        ],
        "candidate_paths": [str(path) for path in candidate_paths],
        "reject_log_paths": [str(path) for path in reject_paths],
        "trace_paths": [str(path) for path in trace_paths],
        "replay_paths": [str(path) for path in replay_paths],
        "sqlite_path": str(sqlite_path) if sqlite_path else None,
    }
    dataset_id = _dataset_id(scope=scope, sources=sources, created_at=now)
    return StrategyLabDataset(
        dataset_id=dataset_id,
        created_at=now.isoformat().replace("+00:00", "Z"),
        scope=scope,
        sources=sources,
        candidates=tuple(candidate_snapshot_to_dict(item) for item in evidence.candidates),
        rejects=tuple(candidate_snapshot_to_dict(item) for item in evidence.reject_logs),
        traces=tuple(dict(item) for item in evidence.traces),
        replay_rows=tuple(dict(item) for item in evidence.replay_rows),
        outcomes=tuple(outcomes),
        trade_events=tuple(trade_events),
        position_lots=tuple(position_lots),
        warnings=tuple(warnings),
    )


def _explicit_or_discovered_paths(
    payload: dict[str, Any],
    *,
    keys: tuple[str, ...],
    runtime_root: Path,
    account: str | None,
    strategy_type: str,
    kind: str,
) -> list[Path]:
    explicit: list[Path] = []
    for key in keys:
        explicit.extend(_path_list(payload.get(key), base=runtime_root))
    if explicit:
        return _dedupe_paths(explicit)
    return _discover_paths(runtime_root=runtime_root, account=account, strategy_type=strategy_type, kind=kind)


def _discover_paths(*, runtime_root: Path, account: str | None, strategy_type: str, kind: str) -> list[Path]:
    dirs = [runtime_root / "output_shared" / "reports"]
    if account:
        dirs.append(runtime_root / "output_accounts" / account / "reports")
        dirs.extend((runtime_root / "output_runs").glob(f"*/accounts/{account}"))
        dirs.extend((runtime_root / "output_runs").glob(f"*/accounts/{account}/reports"))
    patterns = _patterns(strategy_type=strategy_type, kind=kind)
    out: list[Path] = []
    for directory in dirs:
        if not directory.exists() or not directory.is_dir():
            continue
        for pattern in patterns:
            for path in sorted(directory.glob(pattern)):
                if path.is_file() and _path_kind_matches(path, kind=kind):
                    out.append(path.resolve())
    return _dedupe_paths(out)


def _patterns(*, strategy_type: str, kind: str) -> tuple[str, ...]:
    if kind == "candidate":
        return (f"*{strategy_type}*candidates*.csv", f"*{strategy_type}*candidates*.json", f"*{strategy_type}*candidates*.jsonl")
    if kind == "reject":
        return (f"*{strategy_type}*reject*.csv", f"*{strategy_type}*reject*.json", f"*{strategy_type}*reject*.jsonl", "*reject_log*.csv")
    if kind == "trace":
        return ("candidate_filter_trace.jsonl", "candidate_filter_trace.json")
    return ("strategy_replay.csv", "strategy_replay.json", "strategy_replay.jsonl", "*outcome*.csv", "*outcome*.json", "*outcome*.jsonl")


def _path_kind_matches(path: Path, *, kind: str) -> bool:
    name = path.name.lower()
    if kind == "candidate" and "reject" in name:
        return False
    if kind == "reject":
        return "reject" in name
    if kind == "trace":
        return "trace" in name
    return True


def _resolve_sqlite_path(payload: dict[str, Any], *, runtime_root: Path) -> Path | None:
    raw = str(payload.get("sqlite_path") or "").strip()
    if raw:
        path = Path(raw).expanduser()
        return path if path.is_absolute() else (runtime_root / path).resolve()
    return (runtime_root / "output_shared" / "state" / "option_positions.sqlite3").resolve()


def _read_ledger(sqlite_path: Path | None, *, account: str | None, strategy_type: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    if sqlite_path is None:
        return [], [], []
    if not sqlite_path.exists():
        return [], [], [f"strategy_lab_ledger_sqlite_missing:{sqlite_path.name}"]
    warnings: list[str] = []
    try:
        conn = sqlite3.connect(str(sqlite_path))
        conn.row_factory = sqlite3.Row
        trade_events = _read_trade_events(conn)
        position_lots = _read_position_lots(conn)
    except Exception as exc:
        return [], [], [f"strategy_lab_ledger_read_failed:{type(exc).__name__}"]
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return (
        [_event for _event in trade_events if _matches_scope(_event, account=account, strategy_type=strategy_type)],
        [_lot for _lot in position_lots if _matches_scope(dict(_lot.get("fields") or {}), account=account, strategy_type=strategy_type)],
        warnings,
    )


def _read_trade_events(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    try:
        rows = conn.execute("SELECT event_json FROM trade_events ORDER BY trade_time_ms ASC, event_id ASC").fetchall()
    except sqlite3.Error:
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            item = json.loads(str(row["event_json"]) or "{}")
        except Exception:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def _read_position_lots(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    try:
        rows = conn.execute("SELECT record_id, fields_json FROM position_lots ORDER BY updated_at_ms DESC, record_id DESC").fetchall()
    except sqlite3.Error:
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            fields = json.loads(str(row["fields_json"]) or "{}")
        except Exception:
            fields = {}
        if isinstance(fields, dict):
            out.append({"record_id": str(row["record_id"]), "fields": fields})
    return out


def _matches_scope(row: dict[str, Any], *, account: str | None, strategy_type: str) -> bool:
    if account and str(row.get("account") or "").strip().lower() != account:
        return False
    option_type = str(row.get("option_type") or row.get("type") or "").strip().lower()
    side = str(row.get("side") or "").strip().lower()
    if strategy_type == "sell_put":
        return option_type == "put" and side in {"sell", "short"}
    if strategy_type == "sell_call":
        return option_type == "call" and side in {"sell", "short"}
    return str(row.get("strategy_type") or "").strip().lower().replace("-", "_") == strategy_type


def _build_outcomes(
    replay_rows: tuple[Mapping[str, Any], ...],
    trade_events: list[dict[str, Any]],
    position_lots: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    outcomes: list[dict[str, Any]] = [{"source": "strategy_replay", **dict(row)} for row in replay_rows]
    for event in trade_events:
        effect = str(event.get("position_effect") or event.get("event_type") or "").strip().lower()
        if effect in {"close", "expire", "expired", "assignment", "assigned"}:
            outcomes.append({"source": "trade_event", **event})
    for lot in position_lots:
        fields = dict(lot.get("fields") or {})
        status = str(fields.get("status") or fields.get("state") or "").strip().lower()
        open_qty = _as_float(fields.get("open_contracts") or fields.get("open_qty") or fields.get("remaining_contracts"))
        if status in {"closed", "expired", "assigned"} or open_qty == 0:
            outcomes.append({"source": "position_lot", **lot})
    return outcomes


def _path_list(value: Any, *, base: Path) -> list[Path]:
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple, set)) else str(value).replace("|", ",").split(",")
    out: list[Path] = []
    for item in values:
        text = str(item or "").strip()
        if not text:
            continue
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = (base / path).resolve()
        out.append(path)
    return out


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        marker = str(path)
        if marker not in seen:
            out.append(path)
            seen.add(marker)
    return out


def _dataset_id(*, scope: dict[str, Any], sources: dict[str, Any], created_at: datetime) -> str:
    start = str(scope.get("start_date") or "na").replace("-", "")
    end = str(scope.get("end_date") or "na").replace("-", "")
    prefix = "_".join(
        part
        for part in (
            str(scope.get("market") or "market"),
            str(scope.get("account") or "all"),
            str(scope.get("strategy_type") or "strategy"),
            start,
            end,
        )
        if part
    )
    digest = hashlib.sha1(
        json.dumps({"scope": scope, "sources": sources, "created_at": created_at.isoformat()}, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:10]
    return f"{_safe_token(prefix)}_{digest}"


def _safe_token(value: str) -> str:
    out = []
    for ch in value.lower().replace("-", "_"):
        out.append(ch if ch.isalnum() or ch == "_" else "_")
    return "_".join(part for part in "".join(out).split("_") if part)


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        return default
    return parsed if parsed > 0 else default


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", ""))
    except Exception:
        return None
