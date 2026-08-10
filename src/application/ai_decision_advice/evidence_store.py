from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.ai_decision_advice.config import (
    EVIDENCE_STALE_SECONDS,
    EXTERNAL_EVIDENCE_FILE,
    SHARED_STATE_DIRNAME,
)
from src.infrastructure.private_storage import append_private_text, open_private_text


EVIDENCE_RECORD_KINDS = frozenset({"batch_audit", "symbol_evidence", "symbol_status"})

COVERAGE_COMPLETED = "completed"
COVERAGE_NO_EVIDENCE = "no_evidence"
COVERAGE_STALE = "stale"
COVERAGE_IDENTITY_UNAVAILABLE = "identity_unavailable"
COVERAGE_IDENTITY_CHANGED = "identity_changed_pending_refresh"
COVERAGE_SNAPSHOT_INVALID = "evidence_snapshot_invalid"

EVIDENCE_SNAPSHOT_SCHEMA = "ai_decision_advice.evidence_snapshot.v1"


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
    appended_at_text = _utc_iso(appended_at)
    encoded_rows: list[str] = []
    for record in rows:
        existing_run_id = str(record.get("evidence_run_id") or "")
        if existing_run_id and existing_run_id != evidence_run_id:
            raise ValueError("evidence record run binding mismatch")
        record["evidence_run_id"] = evidence_run_id
        record.setdefault("appended_at", appended_at_text)
        encoded_rows.append(json.dumps(record, ensure_ascii=False, sort_keys=True))
    append_private_text(evidence_path(base), "\n".join(encoded_rows) + "\n")
    return len(rows)


def read_evidence_records(base: Path) -> list[dict[str, Any]]:
    path = evidence_path(base)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with open_private_text(path) as fh:
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
    query_cutoff: str | None = None
    search_mode: str | None = None
    evidence_run_id: str | None = None
    semantic_snapshot_hash: str | None = None

    @property
    def semantic_hash(self) -> str:
        if self.semantic_snapshot_hash:
            return self.semantic_snapshot_hash
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
            }
            for symbol, view in sorted(self.views.items())
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    @property
    def evidence_as_of(self) -> str | None:
        """Conservative coverage time from actual usable symbol successes."""

        successes = [
            view.last_success_at
            for view in self.views.values()
            if view.coverage == COVERAGE_COMPLETED and view.last_success_at
        ]
        return min(successes) if successes else None


def freeze_evidence_index(
    base: Path,
    *,
    symbols: Iterable[str] = (),
    now: datetime | str | None = None,
    stale_after_seconds: int = EVIDENCE_STALE_SECONDS,
    identity_hash_by_symbol: Mapping[str, str] | None = None,
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
    requested = [str(symbol) for symbol in symbols]
    wanted = list(dict.fromkeys(requested))
    if not wanted:
        wanted = sorted(
            {
                str(record.get("symbol") or "")
                for record in records
                if str(record.get("symbol") or "")
            }
        )
    wanted_set = set(wanted)

    status_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        kind = str(record.get("kind") or "")
        if kind not in EVIDENCE_RECORD_KINDS:
            continue
        symbol = str(record.get("symbol") or "")
        if wanted_set and symbol not in wanted_set:
            continue
        if not symbol:
            continue
        if kind == "symbol_status":
            status_by_symbol.setdefault(symbol, []).append(record)

    views: dict[str, SymbolEvidenceView] = {}
    for symbol in wanted:
        current_identity_hash = str(
            (identity_hash_by_symbol or {}).get(symbol) or ""
        )
        statuses = status_by_symbol.get(symbol, [])
        current_statuses = [
            row
            for row in statuses
            if not current_identity_hash
            or str(row.get("identity_semantic_sha256") or "")
            == current_identity_hash
        ]
        latest_current = _latest_record(current_statuses)
        if str((latest_current or {}).get("identity_status") or "") == "identity_unavailable":
            views[symbol] = SymbolEvidenceView(
                symbol=symbol,
                coverage=COVERAGE_IDENTITY_UNAVAILABLE,
                unavailable_reason="identity_unavailable",
                last_checked_at=(latest_current or {}).get("last_checked_at"),
                identity_snapshot_hash=current_identity_hash or None,
            )
            continue
        status, evidence_rows, snapshot_error = resolve_latest_success_snapshot(
            records,
            symbol=symbol,
            identity_semantic_sha256=current_identity_hash or None,
        )
        if status is None:
            has_other_identity_success = any(
                row.get("search_status") == "completed"
                and str(row.get("identity_semantic_sha256") or "")
                != current_identity_hash
                for row in statuses
            )
            if current_identity_hash and has_other_identity_success:
                coverage = COVERAGE_IDENTITY_CHANGED
                reason = COVERAGE_IDENTITY_CHANGED
            else:
                coverage = COVERAGE_NO_EVIDENCE
                reason = "no_evidence"
            views[symbol] = SymbolEvidenceView(
                symbol=symbol,
                coverage=coverage,
                unavailable_reason=reason,
                last_checked_at=(latest_current or {}).get("last_checked_at"),
                identity_snapshot_hash=current_identity_hash or None,
            )
            continue
        if snapshot_error:
            views[symbol] = SymbolEvidenceView(
                symbol=symbol,
                coverage=COVERAGE_SNAPSHOT_INVALID,
                unavailable_reason=COVERAGE_SNAPSHOT_INVALID,
                last_checked_at=(latest_current or status).get("last_checked_at"),
                last_success_at=status.get("last_success_at"),
                identity_snapshot_hash=str(
                    status.get("identity_semantic_sha256") or ""
                )
                or None,
                query_cutoff=status.get("query_cutoff"),
                search_mode=status.get("search_mode"),
                evidence_run_id=status.get("evidence_run_id"),
            )
            continue
        last_success = _parse_dt(status.get("last_success_at"))
        if last_success is None:
            views[symbol] = SymbolEvidenceView(
                symbol=symbol,
                coverage=COVERAGE_SNAPSHOT_INVALID,
                unavailable_reason=COVERAGE_SNAPSHOT_INVALID,
            )
            continue
        age = (now_dt - last_success).total_seconds()
        if age > stale_after_seconds:
            views[symbol] = SymbolEvidenceView(
                symbol=symbol,
                coverage=COVERAGE_STALE,
                last_success_at=_utc_iso(last_success),
                last_checked_at=(latest_current or status).get("last_checked_at"),
                unavailable_reason="evidence_stale",
                identity_snapshot_hash=status.get("identity_semantic_sha256"),
                query_cutoff=status.get("query_cutoff"),
                search_mode=status.get("search_mode"),
                evidence_run_id=status.get("evidence_run_id"),
                semantic_snapshot_hash=status.get("semantic_snapshot_hash"),
            )
            continue
        views[symbol] = SymbolEvidenceView(
            symbol=symbol,
            coverage=COVERAGE_COMPLETED,
            evidence=evidence_rows,
            last_checked_at=(latest_current or status).get("last_checked_at"),
            last_success_at=_utc_iso(last_success),
            identity_snapshot_hash=status.get("identity_semantic_sha256"),
            query_cutoff=status.get("query_cutoff"),
            search_mode=status.get("search_mode"),
            evidence_run_id=status.get("evidence_run_id"),
            semantic_snapshot_hash=status.get("semantic_snapshot_hash"),
        )
    return EvidenceIndex(frozen_at=_utc_iso(now_dt), views=views)


def build_evidence_snapshot_hash(
    *,
    symbol: str,
    identity_semantic_sha256: str,
    evidence_rows: Iterable[Mapping[str, Any]],
) -> str:
    """Hash only the declared semantic members of one symbol snapshot."""

    members = [_evidence_semantics(row) for row in evidence_rows]
    return canonical_sha256(
        {
            "schema_version": EVIDENCE_SNAPSHOT_SCHEMA,
            "symbol": symbol,
            "identity_semantic_sha256": identity_semantic_sha256,
            "evidence": members,
        }
    )


def resolve_latest_success_snapshot(
    records: Iterable[Mapping[str, Any]],
    *,
    symbol: str,
    identity_semantic_sha256: str | None,
) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], ...], str | None]:
    """Resolve the newest declared success without inferring historical members."""

    rows = [dict(row) for row in records]
    successes = [
        row
        for row in rows
        if row.get("kind") == "symbol_status"
        and row.get("symbol") == symbol
        and row.get("search_status") == "completed"
        and (
            identity_semantic_sha256 is None
            or str(row.get("identity_semantic_sha256") or "")
            == identity_semantic_sha256
        )
    ]
    status = _latest_record(successes)
    if status is None:
        return None, (), None
    active_refs = status.get("active_evidence_refs")
    if not isinstance(active_refs, list) or any(
        not isinstance(ref, str) or not ref for ref in active_refs
    ):
        return status, (), COVERAGE_SNAPSHOT_INVALID
    if active_refs != sorted(active_refs) or len(active_refs) != len(set(active_refs)):
        return status, (), COVERAGE_SNAPSHOT_INVALID
    identity_hash = str(status.get("identity_semantic_sha256") or "")
    if not identity_hash:
        return status, (), COVERAGE_SNAPSHOT_INVALID
    evidence_rows: list[dict[str, Any]] = []
    for ref in active_refs:
        matching = [
            row
            for row in rows
            if row.get("kind") == "symbol_evidence" and row.get("ref") == ref
        ]
        if len(matching) != 1:
            return status, (), COVERAGE_SNAPSHOT_INVALID
        row = matching[0]
        if (
            row.get("symbol") != symbol
            or str(row.get("identity_semantic_sha256") or "") != identity_hash
            or not str(row.get("evidence_run_id") or "")
        ):
            return status, (), COVERAGE_SNAPSHOT_INVALID
        evidence_rows.append(row)
    expected_hash = build_evidence_snapshot_hash(
        symbol=symbol,
        identity_semantic_sha256=identity_hash,
        evidence_rows=evidence_rows,
    )
    if str(status.get("semantic_snapshot_hash") or "") != expected_hash:
        return status, (), COVERAGE_SNAPSHOT_INVALID
    if str(status.get("search_mode") or "") not in {"incremental", "full"}:
        return status, (), COVERAGE_SNAPSHOT_INVALID
    if _parse_dt(status.get("last_success_at")) is None:
        return status, (), COVERAGE_SNAPSHOT_INVALID
    if _parse_dt(status.get("query_cutoff")) is None:
        return status, (), COVERAGE_SNAPSHOT_INVALID
    return status, tuple(evidence_rows), None


def _evidence_semantics(row: Mapping[str, Any]) -> dict[str, Any]:
    source = row.get("source")
    source_row = source if isinstance(source, Mapping) else {}
    return {
        "ref": row.get("ref"),
        "topic": row.get("topic"),
        "claim": row.get("claim"),
        "event_status": row.get("event_status"),
        "event_time": row.get("event_time"),
        "source": {
            "title": source_row.get("title"),
            "publisher": source_row.get("publisher"),
            "visible_domain": source_row.get("visible_domain"),
            "url": source_row.get("url") or row.get("url"),
            "published_at": source_row.get("published_at"),
        },
        "content_fingerprint": row.get("content_fingerprint"),
    }


def _latest_record(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
    candidates = [dict(row) for row in rows]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda row: (
            str(row.get("last_success_at") or row.get("last_checked_at") or ""),
            str(row.get("appended_at") or ""),
        ),
    )


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
