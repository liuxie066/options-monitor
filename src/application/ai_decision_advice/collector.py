from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.ai_decision_advice.config import (
    EVIDENCE_BATCH_SIZE,
    EVIDENCE_LOOKBACK_DAYS,
    EVIDENCE_MAX_CONCURRENT_BATCHES,
    EVIDENCE_REFRESH_BUDGET_SECONDS,
)
from src.application.ai_decision_advice.evidence_store import (
    append_evidence_records,
    build_evidence_snapshot_hash,
    content_fingerprint,
    read_evidence_records,
    resolve_latest_success_snapshot,
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
    output_text: str
    usage: dict[str, Any]
    response_sha256: str | None = None
    web_search_audit: dict[str, Any] = field(default_factory=dict)
    native_citations: tuple[dict[str, Any], ...] = ()
    native_search_sources: tuple[dict[str, Any], ...] = ()


def model_response_audit(result: ModelCallResult) -> dict[str, Any]:
    """Build content-free audit evidence for one provider response."""

    output = str(result.output_text or "")
    audit: dict[str, Any] = {
        "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
        "output_char_count": len(output),
    }
    response_hash = str(result.response_sha256 or "").strip().lower()
    if len(response_hash) == 64 and all(char in "0123456789abcdef" for char in response_hash):
        audit["response_sha256"] = response_hash
    return audit


def minimized_usage(usage: Mapping[str, Any] | None) -> dict[str, int]:
    """Defend the persistence boundary even for injected model runners."""

    source = usage if isinstance(usage, Mapping) else {}
    result: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = source.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            result[key] = value
    return result


def minimized_web_search_audit(audit: Mapping[str, Any] | None) -> dict[str, Any]:
    """Retain content-free per-symbol status counts, never queries or call IDs."""

    source = audit if isinstance(audit, Mapping) else {}
    raw_count = source.get("count")
    count = raw_count if isinstance(raw_count, int) and not isinstance(raw_count, bool) and raw_count >= 0 else 0
    raw_unattributed = source.get("unattributed_count")
    unattributed_count = (
        raw_unattributed
        if isinstance(raw_unattributed, int)
        and not isinstance(raw_unattributed, bool)
        and raw_unattributed >= 0
        else 0
    )
    raw_auxiliary = source.get("auxiliary_count")
    auxiliary_count = (
        raw_auxiliary
        if isinstance(raw_auxiliary, int)
        and not isinstance(raw_auxiliary, bool)
        and raw_auxiliary >= 0
        else 0
    )
    status_counts: dict[str, int] = {}
    raw_statuses = source.get("status_counts")
    if isinstance(raw_statuses, Mapping):
        for status in ("completed", "failed", "in_progress", "unknown"):
            value = raw_statuses.get(status)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                status_counts[status] = value
    by_symbol: dict[str, dict[str, int]] = {}
    raw_symbols = source.get("symbols")
    if isinstance(raw_symbols, Mapping):
        for raw_symbol, raw_statuses in raw_symbols.items():
            symbol = str(raw_symbol or "")
            if not symbol or not isinstance(raw_statuses, Mapping):
                continue
            counts: dict[str, int] = {}
            for status in ("completed", "failed", "in_progress", "unknown"):
                value = raw_statuses.get(status)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    counts[status] = value
            by_symbol[symbol] = counts
    return {
        "count": count,
        "unattributed_count": unattributed_count,
        "auxiliary_count": auxiliary_count,
        "status_counts": status_counts,
        "symbols": by_symbol,
    }


ModelRunner = Callable[[str, dict[str, Any], dict[str, Any] | None, int], ModelCallResult]


@dataclass
class CollectorRunSummary:
    evidence_run_id: str
    started_at: str
    finished_at: str | None = None
    budget_seconds: int = EVIDENCE_REFRESH_BUDGET_SECONDS
    budget_exhausted: bool = False
    completed_symbols: list[str] = field(default_factory=list)
    identity_unavailable_symbols: list[str] = field(default_factory=list)
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
            "identity_unavailable_symbols": list(
                self.identity_unavailable_symbols
            ),
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
    wanted_list = [str(symbol) for symbol in batch_symbols]
    wanted = set(wanted_list)
    if len(wanted_list) != len(wanted):
        raise ValueError("batch symbols must be unique")
    seen: set[str] = set()
    out: dict[str, list[dict[str, Any]]] = {}
    for row in results:
        if not isinstance(row, dict):
            raise ValueError("each result must be an object")
        if set(row) != {"symbol", "evidence"}:
            raise ValueError("each result must contain only symbol/evidence")
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
    if missing:
        raise ValueError(f"missing symbols in output: {sorted(missing)}")
    if len(results) != len(wanted_list):
        raise ValueError("result cardinality does not match batch")
    return {"results": {symbol: out[symbol] for symbol in wanted_list}}


def _validate_evidence_item(item: Any) -> None:
    if not isinstance(item, dict):
        raise ValueError("evidence item must be an object")
    required = {"topic", "claim", "event_status", "source"}
    allowed = required | {"event_time"}
    if not set(item).issubset(allowed):
        raise ValueError("evidence item contains unexpected fields")
    for key in required:
        if key not in item:
            raise ValueError(f"evidence item missing '{key}'")
    if item["event_status"] not in {"developing", "resolved", "expired"}:
        raise ValueError("event_status must be developing/resolved/expired")
    source = item["source"]
    if not isinstance(source, dict):
        raise ValueError("evidence source must be an object")
    if set(source) != {"title", "publisher", "url", "published_at"}:
        raise ValueError("evidence source fields are invalid")
    for key in ("title", "publisher", "url", "published_at"):
        if key not in source:
            raise ValueError(f"evidence source missing '{key}'")
    if not str(source.get("url") or "").strip():
        raise ValueError("evidence source url must be non-empty")
    if not str(item.get("claim") or "").strip():
        raise ValueError("evidence claim must be non-empty")


def normalize_https_url(value: Any) -> str | None:
    """Parse and reserialize one provider-bound HTTPS URL."""

    raw = str(value or "").strip()
    if not raw or any(unicodedata.category(char).startswith("C") for char in raw):
        return None
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if parsed.scheme.lower() != "https" or not hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    try:
        host = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != 443:
        netloc = f"{netloc}:{port}"
    path = parsed.path or "/"
    return urlunsplit(("https", netloc, path, parsed.query, ""))


def sanitize_source_text(value: Any, *, fallback: str) -> str:
    """Flatten native citation labels and remove control/Markdown structure."""

    text = "".join(
        " " if unicodedata.category(char).startswith("C") else char
        for char in str(value or "")
    )
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[`*_~#>|\[\]]+", " ", text)
    text = " ".join(text.split()).strip(" -")
    return (text or fallback)[:300]


def _native_citations_by_url(
    citations: Iterable[Mapping[str, Any]],
    *,
    search_sources: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    source_symbols_by_url: dict[str, set[str]] = {}
    for source in search_sources:
        url = normalize_https_url(source.get("url"))
        symbol = str(source.get("symbol") or "")
        if url and symbol:
            source_symbols_by_url.setdefault(url, set()).add(symbol)

    bound: dict[str, dict[str, Any]] = {}
    for citation in citations:
        url = normalize_https_url(citation.get("url"))
        if not url:
            continue
        domain = str(urlsplit(url).hostname or "").lower()
        bound.setdefault(
            url,
            {
                "title": sanitize_source_text(
                    citation.get("title"), fallback=domain
                ),
                "publisher": sanitize_source_text(
                    citation.get("publisher"), fallback=domain
                ),
                "visible_domain": domain,
                "url": url,
                "symbols": tuple(sorted(source_symbols_by_url.get(url, set()))),
            },
        )
    return bound


def _search_audit_complete(
    audit: Mapping[str, Any], *, batch_symbols: Iterable[str]
) -> bool:
    minimized = minimized_web_search_audit(audit)
    if minimized["unattributed_count"] != 0:
        return False
    if any(
        int(minimized["status_counts"].get(key) or 0)
        for key in ("failed", "in_progress", "unknown")
    ):
        return False
    by_symbol = minimized["symbols"]
    for symbol in batch_symbols:
        counts = by_symbol.get(symbol)
        if not isinstance(counts, Mapping) or int(counts.get("completed") or 0) < 1:
            return False
        if any(int(counts.get(key) or 0) for key in ("failed", "in_progress", "unknown")):
            return False
    return True


def run_evidence_collector(
    *,
    base,
    queue_symbols: list[str],
    identity_snapshot: Mapping[str, Any],
    cutoff_by_symbol: Mapping[str, str] | None,
    compiled_prompt: CompiledPromptPack,
    model_runner: ModelRunner,
    evidence_run_id: str,
    search_mode_by_symbol: Mapping[str, str] | None = None,
    budget_seconds: int = EVIDENCE_REFRESH_BUDGET_SECONDS,
    batch_size: int = EVIDENCE_BATCH_SIZE,
    max_concurrent_batches: int = EVIDENCE_MAX_CONCURRENT_BATCHES,
    now: datetime | None = None,
    monotonic: Callable[[], float] | None = None,
) -> CollectorRunSummary:
    """Run one evidence refresh within a single shared time budget.

    At most the fixed v1 concurrency limit runs in one wave. Every batch shares
    the same global deadline, and unfinished symbols are reported for the next
    refresh (docs 6.1 / 6.6). One in-budget format repair retry is allowed per
    batch.
    """

    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if max_concurrent_batches <= 0:
        raise ValueError("max_concurrent_batches must be positive")
    if max_concurrent_batches > EVIDENCE_MAX_CONCURRENT_BATCHES:
        raise ValueError("max_concurrent_batches exceeds the v1 fixed limit")

    started = now or datetime.now(timezone.utc)
    clock = monotonic or _default_clock()
    deadline = clock() + float(budget_seconds)
    summary = CollectorRunSummary(
        evidence_run_id=evidence_run_id,
        started_at=started.isoformat(),
        budget_seconds=budget_seconds,
    )
    identity_rows = identity_by_symbol(identity_snapshot)
    identity_artifact_hash = str(identity_snapshot.get("content_sha256") or "")
    identity_snapshot_semantic_hash = str(
        identity_snapshot.get("semantic_sha256") or ""
    )
    existing_records = read_evidence_records(base)
    supplied_cutoffs = dict(cutoff_by_symbol or {})
    cutoffs = compute_cutoffs(
        {
            symbol: supplied_cutoffs.get(symbol)
            for symbol in queue_symbols
        },
        now=started,
    )
    supplied_modes = dict(search_mode_by_symbol or {})
    records: list[dict[str, Any]] = []

    batches = [
        queue_symbols[start : start + batch_size]
        for start in range(0, len(queue_symbols), batch_size)
    ]
    with ThreadPoolExecutor(max_workers=max_concurrent_batches) as executor:
        for wave_start in range(0, len(batches), max_concurrent_batches):
            wave = batches[wave_start : wave_start + max_concurrent_batches]
            if clock() >= deadline:
                summary.budget_exhausted = True
                for remaining_batch in batches[wave_start:]:
                    summary.unfinished_symbols.extend(remaining_batch)
                break

            pending: list[
                tuple[
                    list[str],
                    Future[
                        tuple[
                            list[dict[str, Any]],
                            bool,
                            list[str],
                            list[str],
                            bool,
                        ]
                    ],
                ]
            ] = []
            existing_for_wave = [*existing_records, *records]
            for batch in wave:
                actionable = [
                    symbol
                    for symbol in batch
                    if str(identity_rows.get(symbol, {}).get("status") or "")
                    != "identity_unavailable"
                ]
                skipped = [symbol for symbol in batch if symbol not in actionable]
                for symbol in skipped:
                    identity_semantic_hash = str(
                        identity_rows.get(symbol, {}).get(
                            "identity_semantic_sha256"
                        )
                        or ""
                    )
                    records.append(
                        _status_record(
                            symbol,
                            evidence_run_id=evidence_run_id,
                            identity_status="identity_unavailable",
                            identity_semantic_sha256=identity_semantic_hash,
                            checked_at=started.isoformat(),
                        )
                    )
                    summary.identity_unavailable_symbols.append(symbol)
                if not actionable:
                    continue

                payload_input = build_evidence_input(
                    actionable,
                    identity_rows=identity_rows,
                    cutoff_by_symbol=cutoffs,
                )
                modes: dict[str, str] = {}
                for symbol in actionable:
                    supplied_mode = str(supplied_modes.get(symbol) or "")
                    if supplied_mode in {"incremental", "full"}:
                        modes[symbol] = supplied_mode
                        continue
                    identity_hash = str(
                        identity_rows[symbol].get("identity_semantic_sha256")
                        or ""
                    )
                    previous, _rows, _error = resolve_latest_success_snapshot(
                        existing_records,
                        symbol=symbol,
                        identity_semantic_sha256=identity_hash,
                    )
                    modes[symbol] = (
                        "incremental" if previous is not None else "full"
                    )
                future = executor.submit(
                    _call_with_repair,
                    actionable,
                    payload_input=payload_input,
                    compiled_prompt=compiled_prompt,
                    model_runner=model_runner,
                    clock=clock,
                    deadline=deadline,
                    evidence_run_id=evidence_run_id,
                    identity_artifact_hash=identity_artifact_hash,
                    identity_snapshot_semantic_hash=identity_snapshot_semantic_hash,
                    identity_rows=identity_rows,
                    cutoff_by_symbol=cutoffs,
                    search_mode_by_symbol=modes,
                    existing_records=existing_for_wave,
                )
                pending.append((actionable, future))

            for actionable, future in pending:
                try:
                    (
                        raw_records,
                        repaired,
                        completed_symbols,
                        failed_symbols,
                        timed_out,
                    ) = future.result()
                except Exception:
                    raw_records = []
                    repaired = False
                    completed_symbols = []
                    failed_symbols = list(actionable)
                    timed_out = False
                summary.repair_attempts += int(repaired)
                records.extend(raw_records)
                summary.completed_symbols.extend(completed_symbols)
                summary.failed_symbols.extend(failed_symbols)
                if timed_out:
                    summary.budget_exhausted = True
                    summary.unfinished_symbols.extend(
                        symbol
                        for symbol in actionable
                        if symbol not in completed_symbols
                        and symbol not in failed_symbols
                    )
                for symbol in failed_symbols:
                    records.append(
                        _status_record(
                            symbol,
                            evidence_run_id=evidence_run_id,
                            identity_status="ok",
                            identity_semantic_sha256=str(
                                identity_rows[symbol].get(
                                    "identity_semantic_sha256"
                                )
                                or ""
                            ),
                            checked_at=started.isoformat(),
                            search_status="failed",
                        )
                    )

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
    clock: Callable[[], float],
    deadline: float,
    evidence_run_id: str,
    identity_artifact_hash: str,
    identity_snapshot_semantic_hash: str,
    identity_rows: Mapping[str, Mapping[str, Any]],
    cutoff_by_symbol: Mapping[str, str],
    search_mode_by_symbol: Mapping[str, str],
    existing_records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool, list[str], list[str], bool]:
    """Call the model once, allow one in-budget format repair, then fail."""

    instructions = compiled_prompt.prompt
    repair_attempted = False
    for attempt in (1, 2):
        remaining = deadline - clock()
        if remaining <= 0:
            return [], repair_attempted, [], [], True
        if attempt == 2:
            repair_attempted = True
        try:
            result = model_runner(
                instructions,
                payload_input,
                EVIDENCE_OUTPUT_SCHEMA,
                max(1, int(remaining)),
            )
        except Exception:
            return [], repair_attempted, [], list(batch_symbols), False
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
            return [], repair_attempted, [], list(batch_symbols), False
        checked_at = datetime.now(timezone.utc).isoformat()
        minimized_audit = minimized_web_search_audit(result.web_search_audit)
        records = [
            {
                "kind": "batch_audit",
                "evidence_run_id": evidence_run_id,
                "symbols": list(batch_symbols),
                "prompt": prompt_audit_payload(compiled_prompt),
                "identity_artifact_sha256": identity_artifact_hash,
                "identity_snapshot_semantic_sha256": identity_snapshot_semantic_hash,
                "usage": minimized_usage(result.usage),
                "model_response_audit": model_response_audit(result),
                "web_search_audit": minimized_audit,
                "repair_attempted": attempt == 2,
            }
        ]
        if not _search_audit_complete(
            result.web_search_audit, batch_symbols=batch_symbols
        ):
            return records, repair_attempted, [], list(batch_symbols), False

        citation_by_url = _native_citations_by_url(
            result.native_citations,
            search_sources=result.native_search_sources,
        )
        completed_symbols: list[str] = []
        failed_symbols: list[str] = []
        for symbol in batch_symbols:
            identity_hash = str(
                identity_rows.get(symbol, {}).get("identity_semantic_sha256") or ""
            )
            if not identity_hash:
                failed_symbols.append(symbol)
                continue
            current_rows = _bound_evidence_rows(
                symbol=symbol,
                identity_semantic_sha256=identity_hash,
                evidence_run_id=evidence_run_id,
                evidence_rows=validated["results"].get(symbol, []),
                citation_by_url=citation_by_url,
            )
            mode = str(search_mode_by_symbol.get(symbol) or "")
            previous_rows: tuple[dict[str, Any], ...] = ()
            if mode == "incremental":
                _previous_status, previous_rows, snapshot_error = (
                    resolve_latest_success_snapshot(
                        existing_records,
                        symbol=symbol,
                        identity_semantic_sha256=identity_hash,
                    )
                )
                if snapshot_error:
                    failed_symbols.append(symbol)
                    continue
            elif mode != "full":
                failed_symbols.append(symbol)
                continue

            active_by_ref = (
                {str(row["ref"]): dict(row) for row in previous_rows}
                if mode == "incremental"
                else {}
            )
            novel_rows: list[dict[str, Any]] = []
            collision = False
            for row in current_rows:
                ref = str(row["ref"])
                historical = [
                    existing
                    for existing in existing_records
                    if existing.get("kind") == "symbol_evidence"
                    and existing.get("ref") == ref
                ]
                if len(historical) > 1:
                    collision = True
                    break
                if historical:
                    old = historical[0]
                    old_hash = build_evidence_snapshot_hash(
                        symbol=symbol,
                        identity_semantic_sha256=identity_hash,
                        evidence_rows=[old],
                    )
                    new_hash = build_evidence_snapshot_hash(
                        symbol=symbol,
                        identity_semantic_sha256=identity_hash,
                        evidence_rows=[row],
                    )
                    if old_hash != new_hash:
                        collision = True
                        break
                    active_by_ref[ref] = dict(old)
                else:
                    active_by_ref[ref] = row
                    novel_rows.append(row)
            if collision:
                failed_symbols.append(symbol)
                continue
            active_rows = [active_by_ref[ref] for ref in sorted(active_by_ref)]
            active_refs = [str(row["ref"]) for row in active_rows]
            semantic_snapshot_hash = build_evidence_snapshot_hash(
                symbol=symbol,
                identity_semantic_sha256=identity_hash,
                evidence_rows=active_rows,
            )
            records.extend(novel_rows)
            records.append(
                _status_record(
                    symbol,
                    evidence_run_id=evidence_run_id,
                    identity_status="ok",
                    identity_semantic_sha256=identity_hash,
                    checked_at=checked_at,
                    search_status="completed",
                    evidence_count=len(active_refs),
                    search_mode=mode,
                    query_cutoff=str(cutoff_by_symbol.get(symbol) or ""),
                    active_evidence_refs=active_refs,
                    semantic_snapshot_hash=semantic_snapshot_hash,
                )
            )
            completed_symbols.append(symbol)
        return (
            records,
            repair_attempted,
            completed_symbols,
            failed_symbols,
            False,
        )
    return [], repair_attempted, [], list(batch_symbols), False


def _bound_evidence_rows(
    *,
    symbol: str,
    identity_semantic_sha256: str,
    evidence_run_id: str,
    evidence_rows: Iterable[Mapping[str, Any]],
    citation_by_url: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Bind model claims only to native citations in the same response."""

    by_ref: dict[str, dict[str, Any]] = {}
    for item in evidence_rows:
        model_source = item.get("source")
        source = model_source if isinstance(model_source, Mapping) else {}
        url = normalize_https_url(source.get("url"))
        citation = citation_by_url.get(url or "")
        citation_symbols = citation.get("symbols") if isinstance(citation, Mapping) else ()
        if (
            not url
            or not isinstance(citation, Mapping)
            or symbol not in citation_symbols
        ):
            continue
        normalized_source = {
            "title": citation.get("title"),
            "publisher": citation.get("publisher"),
            "visible_domain": citation.get("visible_domain"),
            "url": url,
            "published_at": _flatten_optional_text(source.get("published_at"), 80),
        }
        topic = _flatten_optional_text(item.get("topic"), 120) or "event"
        claim = _flatten_optional_text(item.get("claim"), 2000)
        if not claim:
            continue
        event_time = _flatten_optional_text(item.get("event_time"), 80)
        fingerprint = content_fingerprint(
            url,
            canonical_sha256(
                {
                    "topic": topic,
                    "claim": claim,
                    "event_status": item.get("event_status"),
                    "event_time": event_time,
                    "source": normalized_source,
                }
            ),
        )
        ref = "ev-" + canonical_sha256(
            {
                "symbol": symbol,
                "identity_semantic_sha256": identity_semantic_sha256,
                "content_fingerprint": fingerprint,
            }
        )[:24]
        by_ref[ref] = {
            "kind": "symbol_evidence",
            "symbol": symbol,
            "evidence_run_id": evidence_run_id,
            "identity_semantic_sha256": identity_semantic_sha256,
            "ref": ref,
            "topic": topic,
            "claim": claim,
            "event_status": item["event_status"],
            "event_time": event_time,
            "source": normalized_source,
            "url": url,
            "content_fingerprint": fingerprint,
        }
    return [by_ref[ref] for ref in sorted(by_ref)]


def _flatten_optional_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = "".join(
        " " if unicodedata.category(char).startswith("C") else char
        for char in str(value)
    )
    text = " ".join(text.split())
    return text[:limit] or None


def _status_record(
    symbol: str,
    *,
    evidence_run_id: str,
    identity_status: str,
    identity_semantic_sha256: str,
    checked_at: str,
    search_status: str | None = None,
    evidence_count: int | None = None,
    search_mode: str | None = None,
    query_cutoff: str | None = None,
    active_evidence_refs: list[str] | None = None,
    semantic_snapshot_hash: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "kind": "symbol_status",
        "symbol": symbol,
        "evidence_run_id": evidence_run_id,
        "identity_status": identity_status,
        "identity_semantic_sha256": identity_semantic_sha256,
        "last_checked_at": checked_at,
    }
    if search_status == "completed":
        record["last_success_at"] = checked_at
        record["search_status"] = "completed"
        record["evidence_count"] = int(evidence_count or 0)
        record["search_mode"] = search_mode
        record["query_cutoff"] = query_cutoff
        record["active_evidence_refs"] = list(active_evidence_refs or [])
        record["semantic_snapshot_hash"] = semantic_snapshot_hash
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
