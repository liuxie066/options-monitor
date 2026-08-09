from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping

from src.application.ai_decision_advice.config import (
    EVIDENCE_BATCH_SIZE,
    EVIDENCE_LOOKBACK_DAYS,
    EVIDENCE_REFRESH_BUDGET_SECONDS,
)
from src.application.ai_decision_advice.evidence_store import (
    append_evidence_records,
    content_fingerprint,
)
from src.application.ai_decision_advice.identity import identity_by_symbol
from src.application.ai_decision_advice.prompts import (
    PROMPT_PACK_EVIDENCE,
    CompiledPromptPack,
    prompt_audit_payload,
)


EVIDENCE_OUTPUT_SCHEMA_NAME = "ai_decision_advice.external_evidence.v1"

EVIDENCE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["results"],
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["symbol", "evidence"],
                "properties": {
                    "symbol": {"type": "string"},
                    "evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["topic", "claim", "event_status", "source"],
                            "properties": {
                                "topic": {"type": "string"},
                                "claim": {"type": "string"},
                                "event_status": {
                                    "type": "string",
                                    "enum": ["developing", "resolved", "expired"],
                                },
                                "event_time": {"type": ["string", "null"]},
                                "source": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["title", "publisher", "url", "published_at"],
                                    "properties": {
                                        "title": {"type": "string"},
                                        "publisher": {"type": "string"},
                                        "url": {"type": "string"},
                                        "published_at": {"type": ["string", "null"]},
                                    },
                                },
                            },
                        },
                    },
                },
            },
        }
    },
}


@dataclass(frozen=True)
class ModelCallResult:
    raw_response: dict[str, Any]
    output_text: str
    usage: dict[str, Any]
    web_search_calls: tuple[dict[str, Any], ...] = ()


ModelRunner = Callable[[str, dict[str, Any], dict[str, Any] | None, int], ModelCallResult]


@dataclass
class CollectorRunSummary:
    evidence_run_id: str
    started_at: str
    finished_at: str | None = None
    budget_seconds: int = EVIDENCE_REFRESH_BUDGET_SECONDS
    budget_exhausted: bool = False
    completed_symbols: list[str] = field(default_factory=list)
    failed_symbols: list[str] = field(default_factory=list)
    unfinished_symbols: list[str] = field(default_factory=list)
    repair_attempts: int = 0
    records_appended: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_run_id": self.evidence_run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "budget_seconds": self.budget_seconds,
            "budget_exhausted": self.budget_exhausted,
            "completed_symbols": list(self.completed_symbols),
            "failed_symbols": list(self.failed_symbols),
            "unfinished_symbols": list(self.unfinished_symbols),
            "repair_attempts": self.repair_attempts,
            "records_appended": self.records_appended,
        }


def build_evidence_input(
    batch_symbols: list[str],
    *,
    identity_rows: Mapping[str, Mapping[str, Any]],
    cutoff_by_symbol: Mapping[str, str],
    lookback_days: int = EVIDENCE_LOOKBACK_DAYS,
) -> dict[str, Any]:
    """JSON data input for the evidence model call (never interpolated)."""

    items: list[dict[str, Any]] = []
    for symbol in batch_symbols:
        identity = identity_rows.get(symbol) or {}
        items.append(
            {
                "symbol": symbol,
                "market": identity.get("market"),
                "exchange": identity.get("exchange"),
                "company_name": identity.get("name"),
                "aliases": list(identity.get("aliases") or []),
                "query_cutoff": cutoff_by_symbol.get(symbol),
                "first_search_lookback_days": lookback_days,
            }
        )
    return {"symbols": items}


def validate_evidence_payload(payload: Any, *, batch_symbols: Iterable[str]) -> dict[str, Any]:
    """Strict structural validation of the model output (docs 6.7)."""

    if not isinstance(payload, dict):
        raise ValueError("evidence output must be a JSON object")
    if set(payload.keys()) != {"results"}:
        raise ValueError("evidence output must contain only 'results'")
    results = payload["results"]
    if not isinstance(results, list):
        raise ValueError("'results' must be an array")
    wanted = {str(symbol) for symbol in batch_symbols}
    seen: set[str] = set()
    out: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in wanted}
    for row in results:
        if not isinstance(row, dict):
            raise ValueError("each result must be an object")
        symbol = str(row.get("symbol") or "")
        if symbol not in wanted:
            raise ValueError(f"unexpected symbol in output: {symbol!r}")
        if symbol in seen:
            raise ValueError(f"duplicate symbol in output: {symbol}")
        seen.add(symbol)
        evidence_rows = row.get("evidence")
        if not isinstance(evidence_rows, list):
            raise ValueError("'evidence' must be an array")
        for item in evidence_rows:
            _validate_evidence_item(item)
        out[symbol] = [dict(item) for item in evidence_rows]
    missing = wanted - seen
    for symbol in missing:
        out[symbol] = []
    return {"results": out, "missing_symbols": sorted(missing)}


def _validate_evidence_item(item: Any) -> None:
    if not isinstance(item, dict):
        raise ValueError("evidence item must be an object")
    for key in ("topic", "claim", "event_status", "source"):
        if key not in item:
            raise ValueError(f"evidence item missing '{key}'")
    if item["event_status"] not in {"developing", "resolved", "expired"}:
        raise ValueError("event_status must be developing/resolved/expired")
    source = item["source"]
    if not isinstance(source, dict):
        raise ValueError("evidence source must be an object")
    for key in ("title", "publisher", "url", "published_at"):
        if key not in source:
            raise ValueError(f"evidence source missing '{key}'")
    if not str(source.get("url") or "").strip():
        raise ValueError("evidence source url must be non-empty")
    if not str(item.get("claim") or "").strip():
        raise ValueError("evidence claim must be non-empty")


def run_evidence_collector(
    *,
    base,
    queue_symbols: list[str],
    identity_snapshot: Mapping[str, Any],
    cutoff_by_symbol: Mapping[str, str] | None,
    compiled_prompt: CompiledPromptPack,
    model_runner: ModelRunner,
    evidence_run_id: str,
    budget_seconds: int = EVIDENCE_REFRESH_BUDGET_SECONDS,
    batch_size: int = EVIDENCE_BATCH_SIZE,
    now: datetime | None = None,
    monotonic: Callable[[], float] | None = None,
) -> CollectorRunSummary:
    """Run one evidence refresh within a single shared time budget.

    Batches run sequentially here; the budget is global, and any unfinished
    symbols are reported for requeue (docs 6.1 / 6.6). One in-budget format
    repair retry is allowed per batch.
    """

    started = now or datetime.now(timezone.utc)
    clock = monotonic or _default_clock()
    deadline = clock() + float(budget_seconds)
    summary = CollectorRunSummary(
        evidence_run_id=evidence_run_id,
        started_at=started.isoformat(),
        budget_seconds=budget_seconds,
    )
    identity_rows = identity_by_symbol(identity_snapshot)
    identity_hash = str(identity_snapshot.get("content_sha256") or "")
    cutoffs = dict(cutoff_by_symbol or {})
    records: list[dict[str, Any]] = []

    batches = [
        queue_symbols[start : start + batch_size]
        for start in range(0, len(queue_symbols), batch_size)
    ]
    for batch in batches:
        if clock() >= deadline:
            summary.budget_exhausted = True
            summary.unfinished_symbols.extend(batch)
            continue
        actionable = [
            symbol
            for symbol in batch
            if str(identity_rows.get(symbol, {}).get("status") or "") != "identity_unavailable"
        ]
        skipped = [symbol for symbol in batch if symbol not in actionable]
        for symbol in skipped:
            records.append(
                _status_record(
                    symbol,
                    evidence_run_id=evidence_run_id,
                    identity_status="identity_unavailable",
                    identity_snapshot_hash=identity_hash,
                    checked_at=started.isoformat(),
                )
            )
            summary.completed_symbols.append(symbol)
        if not actionable:
            continue

        remaining = max(1, int(deadline - clock()))
        payload_input = build_evidence_input(
            actionable,
            identity_rows=identity_rows,
            cutoff_by_symbol=cutoffs,
        )
        raw_records, repaired, failed = _call_with_repair(
            actionable,
            payload_input=payload_input,
            compiled_prompt=compiled_prompt,
            model_runner=model_runner,
            timeout=remaining,
            evidence_run_id=evidence_run_id,
            identity_snapshot_hash=identity_hash,
        )
        summary.repair_attempts += int(repaired)
        if failed:
            summary.failed_symbols.extend(actionable)
            for symbol in actionable:
                records.append(
                    _status_record(
                        symbol,
                        evidence_run_id=evidence_run_id,
                        identity_status="ok",
                        identity_snapshot_hash=identity_hash,
                        checked_at=started.isoformat(),
                        search_status="failed",
                    )
                )
            continue
        records.extend(raw_records)
        summary.completed_symbols.extend(actionable)

    finished = datetime.now(timezone.utc) if now is None else now
    summary.finished_at = finished.isoformat()
    summary.records_appended = append_evidence_records(
        base=base,
        records=records,
        evidence_run_id=evidence_run_id,
        appended_at=finished,
    )
    return summary


def _call_with_repair(
    batch_symbols: list[str],
    *,
    payload_input: dict[str, Any],
    compiled_prompt: CompiledPromptPack,
    model_runner: ModelRunner,
    timeout: int,
    evidence_run_id: str,
    identity_snapshot_hash: str,
) -> tuple[list[dict[str, Any]], bool, bool]:
    """Call the model once, allow one in-budget format repair, then fail."""

    instructions = compiled_prompt.prompt
    for attempt in (1, 2):
        result = model_runner(instructions, payload_input, EVIDENCE_OUTPUT_SCHEMA, timeout)
        try:
            parsed = json.loads(result.output_text or "null")
            validated = validate_evidence_payload(parsed, batch_symbols=batch_symbols)
        except (json.JSONDecodeError, ValueError):
            if attempt == 1:
                instructions = (
                    compiled_prompt.prompt
                    + "\n\n上次输出不符合要求的 JSON 结构。只重新输出符合 JSON Schema"
                    " 的一个 JSON 值，不要输出其他内容。"
                )
                continue
            return [], True, True
        checked_at = datetime.now(timezone.utc).isoformat()
        records = [
            {
                "kind": "batch_audit",
                "evidence_run_id": evidence_run_id,
                "symbols": list(batch_symbols),
                "prompt": prompt_audit_payload(compiled_prompt),
                "identity_snapshot_hash": identity_snapshot_hash,
                "usage": dict(result.usage),
                "web_search_calls": [dict(call) for call in result.web_search_calls],
                "repair_attempted": attempt == 2,
            }
        ]
        for symbol in batch_symbols:
            evidence_rows = validated["results"].get(symbol, [])
            for item in evidence_rows:
                fingerprint = content_fingerprint(
                    item["source"].get("url"), item["claim"]
                )
                records.append(
                    {
                        "kind": "symbol_evidence",
                        "symbol": symbol,
                        "evidence_run_id": evidence_run_id,
                        "identity_snapshot_hash": identity_snapshot_hash,
                        "ref": f"ev-{fingerprint[:12]}",
                        "topic": item["topic"],
                        "claim": item["claim"],
                        "event_status": item["event_status"],
                        "event_time": item.get("event_time"),
                        "source": dict(item["source"]),
                        "url": item["source"].get("url"),
                        "content_fingerprint": fingerprint,
                    }
                )
            records.append(
                _status_record(
                    symbol,
                    evidence_run_id=evidence_run_id,
                    identity_status="ok",
                    identity_snapshot_hash=identity_snapshot_hash,
                    checked_at=checked_at,
                    search_status="completed",
                    evidence_count=len(evidence_rows),
                )
            )
        return records, attempt == 2, False
    return [], False, True


def _status_record(
    symbol: str,
    *,
    evidence_run_id: str,
    identity_status: str,
    identity_snapshot_hash: str,
    checked_at: str,
    search_status: str | None = None,
    evidence_count: int | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "kind": "symbol_status",
        "symbol": symbol,
        "evidence_run_id": evidence_run_id,
        "identity_status": identity_status,
        "identity_snapshot_hash": identity_snapshot_hash,
        "last_checked_at": checked_at,
    }
    if search_status == "completed":
        record["last_success_at"] = checked_at
        record["search_status"] = "completed"
        record["evidence_count"] = int(evidence_count or 0)
    elif search_status:
        record["search_status"] = search_status
    return record


def compute_cutoffs(
    last_success_by_symbol: Mapping[str, str | None],
    *,
    now: datetime,
    lookback_days: int = EVIDENCE_LOOKBACK_DAYS,
) -> dict[str, str]:
    """Per-symbol query cutoff: last success, else first-search lookback."""

    first_cutoff = (now - timedelta(days=lookback_days)).isoformat()
    return {
        str(symbol): (str(value) if value else first_cutoff)
        for symbol, value in last_success_by_symbol.items()
    }


def _default_clock() -> Callable[[], float]:
    import time

    return time.monotonic
