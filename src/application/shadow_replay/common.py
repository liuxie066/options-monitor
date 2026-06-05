from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DATASET_SCHEMA_VERSION = "shadow_replay_dataset.v1"
ANALYSIS_SCHEMA_VERSION = "shadow_replay_analysis.v1"
READINESS_SCHEMA_VERSION = "shadow_replay_readiness.v1"

CANDIDATE_SNAPSHOT_SCHEMA_VERSION = "shadow_replay_candidate_snapshot.v1"
FILTER_DECISION_SCHEMA_VERSION = "shadow_replay_filter_decision.v1"
RANK_SNAPSHOT_SCHEMA_VERSION = "shadow_replay_rank_snapshot.v1"
MARK_PATH_SCHEMA_VERSION = "shadow_replay_mark_path_snapshot.v1"
OUTCOME_FACT_SCHEMA_VERSION = "shadow_replay_outcome_fact.v1"

DATASET_FILES = (
    "candidate_snapshots.jsonl",
    "filter_decisions.jsonl",
    "rank_snapshots.jsonl",
    "mark_path_snapshots.jsonl",
    "outcome_facts.jsonl",
)


def dataset_dir_from_arg(dataset: str | Path) -> Path:
    path = Path(dataset).expanduser().resolve()
    if path.is_file():
        return path.parent
    return path


def dataset_output_dir(output_dir: str | Path | None, *, dataset_id: str, base: Path) -> Path:
    if output_dir:
        return resolve_path(output_dir, base=base)
    return (base / "output_shared" / "research" / "shadow_replay" / "datasets" / dataset_id).resolve()


def resolve_output_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def default_dataset_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def resolve_optional(value: str | Path | None, *, base: Path) -> Path | None:
    if value is None or not str(value).strip():
        return None
    return resolve_path(value, base=base)


def resolve_many(values: list[str | Path] | tuple[str | Path, ...] | None, *, base: Path) -> list[Path]:
    return [resolve_path(value, base=base) for value in (values or []) if str(value or "").strip()]


def resolve_path(value: str | Path, *, base: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    return path


def glob_many(directory: Path, patterns: tuple[str, ...]) -> list[Path]:
    if not directory.exists() or not directory.is_dir():
        return []
    out: list[Path] = []
    for pattern in patterns:
        out.extend(path.resolve() for path in directory.glob(pattern) if path.is_file())
    return out


def unique(paths: Any) -> list[Path]:
    out: list[Path] = []
    seen: set[Path] = set()
    for raw in paths or []:
        path = Path(raw).resolve()
        if path in seen:
            continue
        seen.add(path)
        out.append(path)
    return out


def safe_rel(path: Path | None, *, base: Path) -> str | None:
    if path is None:
        return None
    try:
        return str(path.resolve().relative_to(base.resolve()))
    except Exception:
        return str(path)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            text = line.strip()
            if not text:
                continue
            try:
                item = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                out.append(item)
    return out


def read_csv_rows(path: Path) -> list[dict[str, Any]]:
    import csv

    if not path.exists() or not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def safety_payload(*, writes_local_dataset: bool) -> dict[str, Any]:
    return {
        "offline_only": True,
        "read_only_sources": True,
        "writes_local_dataset_only": bool(writes_local_dataset),
        "writes_runtime_config": False,
        "writes_trade_state": False,
        "sends_notifications": False,
    }


def instrument_key(row: dict[str, Any]) -> str:
    contract = text(row.get("contract_symbol") or row.get("option_symbol"))
    if contract:
        return contract.upper()
    parts = [
        text(row.get("account")).lower(),
        text(row.get("symbol") or row.get("underlying_symbol")).upper(),
        text(row.get("option_type") or row.get("mode")).lower(),
        text(row.get("expiration") or row.get("exp")),
        text(row.get("strike")),
    ]
    return "|".join(parts).strip("|")


def first_float(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = float_or_none(row.get(key))
        if value is not None:
            return value
    return None


def abs_first_float(row: dict[str, Any], *keys: str) -> float | None:
    value = first_float(row, *keys)
    return abs(value) if value is not None else None


def float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    raw = str(value).strip()
    text_value = raw.rstrip("%")
    if not text_value:
        return None
    try:
        parsed = float(text_value)
    except Exception:
        return None
    if raw.endswith("%"):
        return parsed / 100.0
    return parsed


def text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def parse_date(value: str) -> Any:
    value_text = text(value)
    if not value_text:
        return None
    if len(value_text) >= 10 and value_text[4:5] == "-" and value_text[7:8] == "-":
        value_text = value_text[:10]
    try:
        return datetime.strptime(value_text, "%Y-%m-%d").date()
    except Exception:
        return None


def account_hint(path: Path) -> str | None:
    parts = list(path.parts)
    for marker in ("accounts", "output_accounts"):
        if marker in parts:
            idx = parts.index(marker)
            if idx + 1 < len(parts):
                return str(parts[idx + 1]).strip().lower() or None
    return None


def strategy_hint(path: Path) -> str | None:
    name = path.name.lower()
    if "combo_yield" in name or "yield_enhancement" in name:
        return "combo_yield"
    if "sell_call" in name:
        return "sell_call"
    if "sell_put" in name:
        return "sell_put"
    return None


def strategy_mode(strategy: str | None) -> str | None:
    if strategy == "sell_put":
        return "put"
    if strategy == "sell_call":
        return "call"
    return None


def normal_status(value: Any) -> str:
    value_text = text(value).lower()
    if value_text in {"accepted", "rejected", "post_filtered", "ranked_below", "notified"}:
        return value_text
    return value_text or "unknown"
