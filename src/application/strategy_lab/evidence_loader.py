from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable

from src.application.strategy_lab.contracts import (
    CandidateSnapshot,
    EvidenceArtifact,
    EvidenceRef,
    StrategyLabEvidence,
)


def load_strategy_lab_evidence(
    *,
    candidate_paths: Iterable[str | Path] | None = None,
    reject_log_paths: Iterable[str | Path] | None = None,
    trace_paths: Iterable[str | Path] | None = None,
    replay_paths: Iterable[str | Path] | None = None,
    base: Path | None = None,
    sample_limit: int = 5,
) -> StrategyLabEvidence:
    root = Path(base or Path.cwd()).resolve()
    artifacts: list[EvidenceArtifact] = []
    candidates: list[CandidateSnapshot] = []
    reject_logs: list[CandidateSnapshot] = []
    traces: list[dict[str, Any]] = []
    replay_rows: list[dict[str, Any]] = []
    warnings: list[str] = []

    for path in _paths(candidate_paths, base=root):
        rows = _read_rows(path)
        artifacts.append(_artifact("candidate", path, rows, base=root, sample_limit=sample_limit))
        candidates.extend(_candidate_snapshots(rows, kind="candidate", path=path, base=root))

    for path in _paths(reject_log_paths, base=root):
        rows = _read_rows(path)
        artifacts.append(_artifact("reject_log", path, rows, base=root, sample_limit=sample_limit))
        reject_logs.extend(_candidate_snapshots(rows, kind="reject_log", path=path, base=root))

    for path in _paths(trace_paths, base=root):
        rows = _read_rows(path)
        artifacts.append(_artifact("trace", path, rows, base=root, sample_limit=sample_limit))
        traces.extend(rows)

    for path in _paths(replay_paths, base=root):
        rows = _read_rows(path)
        artifacts.append(_artifact("strategy_replay", path, rows, base=root, sample_limit=sample_limit))
        replay_rows.extend(rows)

    if not candidates and not reject_logs and not traces and not replay_rows:
        warnings.append("strategy_lab_evidence_empty")

    return StrategyLabEvidence(
        artifacts=tuple(artifacts),
        candidates=tuple(candidates),
        reject_logs=tuple(reject_logs),
        traces=tuple(_freeze_rows(traces)),
        replay_rows=tuple(_freeze_rows(replay_rows)),
        warnings=tuple(warnings),
    )


def _paths(values: Iterable[str | Path] | None, *, base: Path) -> list[Path]:
    out: list[Path] = []
    for value in values or ():
        raw = str(value or "").strip()
        if not raw:
            continue
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = base / path
        out.append(path.resolve())
    return out


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(str(path))
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8", newline="") as fh:
            return [dict(row) for row in csv.DictReader(fh) if isinstance(row, dict)]
    if suffix == ".jsonl":
        rows: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                raw = line.strip()
                if not raw:
                    continue
                item = json.loads(raw)
                if isinstance(item, dict):
                    rows.append(item)
        return rows
    if suffix == ".json":
        item = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(item, list):
            return [dict(row) for row in item if isinstance(row, dict)]
        if isinstance(item, dict):
            rows = item.get("rows") or item.get("records") or item.get("data")
            if isinstance(rows, list):
                return [dict(row) for row in rows if isinstance(row, dict)]
            return [dict(item)]
    raise ValueError(f"unsupported strategy-lab evidence file: {path.name}")


def _artifact(kind: str, path: Path, rows: list[dict[str, Any]], *, base: Path, sample_limit: int) -> EvidenceArtifact:
    ref = EvidenceRef.from_path(kind=kind, path=path, base=base)
    return EvidenceArtifact(
        kind=kind,
        path=ref.path,
        row_count=len(rows),
        sample_rows=tuple(_freeze_rows(rows[: max(0, sample_limit)])),
    )


def _candidate_snapshots(rows: list[dict[str, Any]], *, kind: str, path: Path, base: Path) -> list[CandidateSnapshot]:
    out: list[CandidateSnapshot] = []
    for idx, row in enumerate(rows, start=1):
        strategy_type = _strategy_type(row, path=path)
        out.append(
            CandidateSnapshot(
                row_id=str(_first(row, "row_id", "candidate_id", "contract_symbol", "option_symbol") or f"{kind}-{idx}"),
                symbol=_text(_first(row, "symbol", "underlying", "ticker"), upper=True),
                account=_text(_first(row, "account", "account_label"), lower=True) or _account_from_path(path),
                strategy_type=strategy_type,
                contract_symbol=_text(_first(row, "contract_symbol", "option_symbol")),
                option_type=_option_type(row, strategy_type=strategy_type),
                side=_side(row, strategy_type=strategy_type),
                strike=_as_float(_first(row, "strike", "strike_price")),
                expiry=_text(_first(row, "expiry", "expiration", "exp")),
                dte=_as_int(_first(row, "dte", "DTE", "days_to_expiration")),
                premium=_as_float(_first(row, "premium", "net_premium", "bid", "mid")),
                delta=_as_float(_first(row, "delta", "abs_delta", "current_delta")),
                contracts=_as_int(_first(row, "contracts", "quantity", "qty")) or 1,
                multiplier=_as_float(_first(row, "multiplier", "contract_multiplier")),
                locked_cash=_as_float(
                    _first(
                        row,
                        "locked_cash",
                        "cash_required",
                        "required_cash",
                        "collateral",
                        "cash_basis",
                        "cash_required_usd",
                        "cash_required_cny",
                    )
                ),
                selected=_as_bool(_first(row, "selected", "accepted", "notified", "executed", "passed_filter")),
                reject_reasons=tuple(_split_reasons(_first(row, "reject_reason", "reject_rule", "engine_reject_reason", "filter_reason", "filter_reasons"))),
                evidence_ref=EvidenceRef.from_path(kind=kind, path=path, row_index=idx, base=base),
                raw=row,
            )
        )
    return out


def _first(row: dict[str, Any], *names: str) -> Any:
    lower = {str(key).strip().lower(): value for key, value in row.items()}
    for name in names:
        if name in row and not _missing(row.get(name)):
            return row.get(name)
        key = str(name).strip().lower()
        if key in lower and not _missing(lower.get(key)):
            return lower.get(key)
    return None


def _strategy_type(row: dict[str, Any], *, path: Path | None = None) -> str | None:
    raw = str(_first(row, "strategy_type", "strategy", "mode", "option_strategy") or "").strip().lower().replace("-", "_")
    parsed = _strategy_type_from_text(raw)
    if parsed:
        return parsed
    parsed = _strategy_type_from_option_fields(row)
    if parsed:
        return parsed
    if path is not None:
        return _strategy_type_from_path(path)
    return None


def _strategy_type_from_text(raw: str) -> str | None:
    if raw in {"put", "short_put", "sell_put", "cash_secured_put"}:
        return "sell_put"
    if raw in {"call", "short_call", "sell_call", "covered_call"}:
        return "sell_call"
    if raw in {"ye", "yield", "yield_enhancement"}:
        return "yield_enhancement"
    if raw in {"close", "close_advice"}:
        return "close_advice"
    return None


def _strategy_type_from_path(path: Path) -> str | None:
    name = path.name.lower().replace("-", "_")
    if "yield_enhancement" in name:
        return "yield_enhancement"
    if "close_advice" in name:
        return "close_advice"
    if "sell_put" in name:
        return "sell_put"
    if "sell_call" in name:
        return "sell_call"
    return None


def _account_from_path(path: Path) -> str | None:
    parent = path.parent
    if parent.parent.name == "accounts":
        return _text(parent.name, lower=True)
    return None


def _option_type(row: dict[str, Any], *, strategy_type: str | None) -> str | None:
    option_type = str(_first(row, "option_type", "type") or "").strip().lower()
    if option_type:
        return option_type
    if strategy_type == "sell_put":
        return "put"
    if strategy_type == "sell_call":
        return "call"
    return None


def _side(row: dict[str, Any], *, strategy_type: str | None) -> str | None:
    side = str(_first(row, "side") or "").strip().lower()
    if side:
        return side
    if strategy_type in {"sell_put", "sell_call"}:
        return "short"
    return None


def _strategy_type_from_option_fields(row: dict[str, Any]) -> str | None:
    option_type = str(_first(row, "option_type", "type") or "").strip().lower()
    side = str(_first(row, "side") or "").strip().lower()
    if option_type == "put" and side in {"short", "sell"}:
        return "sell_put"
    if option_type == "call" and side in {"short", "sell"}:
        return "sell_call"
    return None


def _text(value: Any, *, upper: bool = False, lower: bool = False) -> str | None:
    if _missing(value):
        return None
    text = str(value).strip()
    if not text:
        return None
    if upper:
        return text.upper()
    if lower:
        return text.lower()
    return text


def _as_float(value: Any) -> float | None:
    if _missing(value) or isinstance(value, bool):
        return None
    raw = str(value).strip().replace(",", "")
    if not raw:
        return None
    if raw.endswith("%"):
        raw = raw[:-1].strip()
        try:
            return float(raw) / 100.0
        except Exception:
            return None
    try:
        return float(raw)
    except Exception:
        return None


def _as_int(value: Any) -> int | None:
    parsed = _as_float(value)
    if parsed is None:
        return None
    return int(parsed)


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if _missing(value):
        return None
    raw = str(value).strip().lower()
    if raw in {"1", "true", "yes", "y", "on", "selected", "accepted", "notified", "executed", "passed"}:
        return True
    if raw in {"0", "false", "no", "n", "off", "rejected", "filtered"}:
        return False
    return None


def _split_reasons(value: Any) -> list[str]:
    if _missing(value):
        return []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_split_reasons(item))
        return _dedupe(out)
    raw = str(value).strip()
    if not raw:
        return []
    normalized = raw.replace("|", ";").replace(",", ";")
    return _dedupe([part.strip() for part in normalized.split(";") if part.strip()])


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            out.append(value)
            seen.add(value)
    return out


def _missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    try:
        return bool(value != value)
    except Exception:
        return False


def _freeze_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]
