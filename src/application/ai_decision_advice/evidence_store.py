from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from src.application.ai_decision_advice.config import (
    EVIDENCE_STALE_SECONDS,
    EXTERNAL_EVIDENCE_FILE,
    SHARED_STATE_DIRNAME,
)


EVIDENCE_RECORD_KINDS = frozenset({"batch_audit", "symbol_evidence", "symbol_status"})

COVERAGE_COMPLETED = "completed"
COVERAGE_NO_EVIDENCE = "no_evidence"
COVERAGE_STALE = "stale"
COVERAGE_IDENTITY_UNAVAILABLE = "identity_unavailable"


def evidence_path(base: Path) -> Path:
    return Path(base) / "output_shared" / "state" / SHARED_STATE_DIRNAME / EXTERNAL_EVIDENCE_FILE


def content_fingerprint(*parts: Any) -> str:
    """Stable SHA-256 fingerprint for dedupe (URL + normalized content)."""

    h = hashlib.sha256()
    for part in parts:
        text = "" if part is None else str(part)
        h.update(text.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def append_evidence_records(
    *,
    base: Path,
    records: Iterable[Mapping[str, Any]],
    evidence_run_id: str,
    appended_at: datetime | str | None = None,
) -> int:
    """Append self-contained records to the shared evidence JSONL log.

    Every line carries ``evidence_run_id`` and ``appended_at`` so the latest
    per-symbol index can always be rebuilt from the log alone. Returns the
    number of records appended.
    """

    rows = [dict(record) for record in records]
    if not rows:
        return 0
    path = evidence_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    appended_at_text = _utc_iso(appended_at)
    with path.open("a", encoding="utf-8") as fh:
        for record in rows:
            record.setdefault("evidence_run_id", evidence_run_id)
            record.setdefault("appended_at", appended_at_text)
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            fh.write("\n")
    return len(rows)


def read_evidence_records(base: Path) -> list[dict[str, Any]]:
    path = evidence_path(base)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


@dataclass(frozen=True)
class SymbolEvidenceView:
    symbol: str
    coverage: str
    evidence: tuple[dict[str, Any], ...] = ()
    last_checked_at: str | None = None
    last_success_at: str | None = None
    identity_snapshot_hash: str | None = None
    unavailable_reason: str | None = None

    @property
    def semantic_hash(self) -> str:
        payload = json.dumps(list(self.evidence), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class EvidenceIndex:
    """Frozen per-symbol view derived from the append-only log (docs 7.5)."""

    frozen_at: str
    views: dict[str, SymbolEvidenceView] = field(default_factory=dict)

    def view_for(self, symbol: str) -> SymbolEvidenceView | None:
        return self.views.get(symbol)

    def index_hash(self) -> str:
        payload = {
            symbol: {
                "coverage": view.coverage,
                "semantic_hash": view.semantic_hash,
                "last_success_at": view.last_success_at,
            }
            for symbol, view in sorted(self.views.items())
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()


def freeze_evidence_index(
    base: Path,
    *,
    symbols: Iterable[str] = (),
    now: datetime | str | None = None,
    stale_after_seconds: int = EVIDENCE_STALE_SECONDS,
) -> EvidenceIndex:
    """Rebuild the latest per-symbol view and mark coverage.

    Coverage rules (docs 6.6.1 / 6.6.2):

    - ``completed``: a successful search with auditable cutoff, regardless of
      whether any event was found, not older than ``stale_after_seconds``;
    - ``no_evidence``: symbol never successfully searched;
    - ``stale``: last successful search older than ``stale_after_seconds``;
    - ``identity_unavailable``: identity could not be established.
    """

    now_dt = _utc_dt(now)
    records = read_evidence_records(base)
    wanted = [str(symbol) for symbol in symbols]
    wanted_set = set(wanted)

    latest_status: dict[str, dict[str, Any]] = {}
    evidence_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        kind = str(record.get("kind") or "")
        if kind not in EVIDENCE_RECORD_KINDS:
            continue
        symbol = str(record.get("symbol") or "")
        if wanted_set and symbol not in wanted_set:
            continue
        if not symbol:
            continue
        if kind == "symbol_evidence":
            evidence_by_symbol.setdefault(symbol, []).append(record)
            continue
        if kind == "symbol_status":
            current = latest_status.get(symbol)
            appended = str(record.get("appended_at") or "")
            if current is None or appended >= str(current.get("appended_at") or ""):
                latest_status[symbol] = record

    views: dict[str, SymbolEvidenceView] = {}
    for symbol in wanted:
        status = latest_status.get(symbol)
        last_success = _parse_dt((status or {}).get("last_success_at"))
        identity_status = str((status or {}).get("identity_status") or "")
        if identity_status == "identity_unavailable":
            views[symbol] = SymbolEvidenceView(
                symbol=symbol,
                coverage=COVERAGE_IDENTITY_UNAVAILABLE,
                unavailable_reason="identity_unavailable",
                identity_snapshot_hash=(status or {}).get("identity_snapshot_hash"),
            )
            continue
        if status is None or last_success is None:
            views[symbol] = SymbolEvidenceView(
                symbol=symbol,
                coverage=COVERAGE_NO_EVIDENCE,
                unavailable_reason="no_evidence",
            )
            continue
        age = (now_dt - last_success).total_seconds()
        if age > stale_after_seconds:
            views[symbol] = SymbolEvidenceView(
                symbol=symbol,
                coverage=COVERAGE_STALE,
                last_success_at=_utc_iso(last_success),
                last_checked_at=(status or {}).get("last_checked_at"),
                unavailable_reason="evidence_stale",
                identity_snapshot_hash=(status or {}).get("identity_snapshot_hash"),
            )
            continue
        views[symbol] = SymbolEvidenceView(
            symbol=symbol,
            coverage=COVERAGE_COMPLETED,
            evidence=tuple(_dedupe_evidence(evidence_by_symbol.get(symbol, []))),
            last_checked_at=(status or {}).get("last_checked_at"),
            last_success_at=_utc_iso(last_success),
            identity_snapshot_hash=(status or {}).get("identity_snapshot_hash"),
        )
    return EvidenceIndex(frozen_at=_utc_iso(now_dt), views=views)


def _dedupe_evidence(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """URL + content fingerprint dedupe, keeping the latest record per fingerprint."""

    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        fingerprint = str(row.get("content_fingerprint") or "") or content_fingerprint(
            row.get("url"), row.get("claim")
        )
        current = seen.get(fingerprint)
        if current is None or str(row.get("appended_at") or "") >= str(current.get("appended_at") or ""):
            seen[fingerprint] = row
    return [seen[key] for key in sorted(seen)]


def _utc_dt(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    parsed = _parse_dt(value)
    if parsed is None:
        raise ValueError(f"invalid UTC timestamp: {value}")
    return parsed


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _utc_iso(value: datetime | str | None) -> str:
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    return str(value)
