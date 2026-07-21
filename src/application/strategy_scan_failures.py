from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "strategy_scan_failure.v1"
ARTIFACT_NAME = "strategy_scan_failures.jsonl"
FAILURE_STAGE = "strategy_execution"
FAILURE_REASON = "strategy_step_failed"


def append_strategy_scan_failure(
    *,
    report_dir: Path | str,
    symbol: Any,
    strategy_family: Any,
    error: BaseException,
) -> None:
    output_dir = Path(report_dir).resolve()
    scope = _infer_scope(output_dir)
    row = {
        "schema_version": SCHEMA_VERSION,
        "run_id": scope["run_id"],
        "account": scope["account"],
        "symbol": str(symbol or "").strip().upper(),
        "strategy_family": str(strategy_family or "").strip().lower(),
        "stage": FAILURE_STAGE,
        "reason": FAILURE_REASON,
        "error_type": type(error).__name__,
        "message": str(error),
    }
    path = output_dir / ARTIFACT_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def read_strategy_scan_failures(path: Path | str) -> list[dict[str, Any]]:
    source = Path(path).expanduser()
    if not source.exists():
        return []
    rows: list[dict[str, Any]] = []
    with source.open("r", encoding="utf-8") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, Mapping) and row.get("schema_version") == SCHEMA_VERSION:
                rows.append(dict(row))
    return rows


def _infer_scope(path: Path) -> dict[str, str | None]:
    parts = list(path.parts)
    try:
        run_index = parts.index("output_runs")
    except ValueError:
        return {"run_id": None, "account": None}
    run_id = parts[run_index + 1] if run_index + 1 < len(parts) else None
    account = None
    if run_index + 2 < len(parts) and parts[run_index + 2] == "accounts":
        account = parts[run_index + 3] if run_index + 3 < len(parts) else None
    return {"run_id": run_id, "account": account}
