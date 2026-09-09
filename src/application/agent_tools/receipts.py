"""The fixed trade / monitor / scheduled receipt read contract."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from src.application.account_config import accounts_from_config
from src.application.agent_tool_config import load_runtime_config, repo_base
from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tools.base import build_agent_tool
from src.application.daily_decision_brief_repository import query_daily_brief_receipts
from src.application.ledger.api import (decode_evidence_cursor, encode_evidence_cursor,
    query_lifecycle_receipts, resolve_position_ledger_sqlite_path, TradeEventPaginationError)
from src.application.positions.maintenance_receipt import query_maintenance_receipts
from src.application.receipt_query import receipt_digest, receipt_time
from src.application.research.redaction import redact_text
from src.application.runtime_config_freshness import infer_runtime_config_market
from src.application.runtime_paths import resolve_runtime_root
from src.application.secret_store import INBOUND_OPERATION_HMAC_KEY, SecretError, resolve_secret
from src.application.trades.inbox import query_trade_receipts
from src.application.trades.inbox_authority import resolve_execution_inbox_path
from src.application.trades.account_mapping import resolve_trade_intake_config
from src.application.agent_tools.runtime_status_impl import query_run_receipts

_FILTERS = ("type", "account", "market", "start_time", "end_time", "deal_id", "run_id", "event_id", "symbol")
_BODY_CHARS = 1800
_PAGE_BYTES = 5000


def _cursor_key() -> str:
    try:
        key = resolve_secret(INBOUND_OPERATION_HMAC_KEY)
    except SecretError as exc:
        raise AgentToolError(code="DEPENDENCY_MISSING", message="receipt cursor signing key unavailable") from exc
    if not key:
        raise AgentToolError(code="DEPENDENCY_MISSING", message="receipt cursor signing key unavailable")
    return hmac.new(key.encode(), b"options-monitor/bot/receipt-cursor/v1", hashlib.sha256).hexdigest()


def _sources(*, base: Path, cfg: dict[str, Any], config_path: Path, accounts: list[str], market: str,
             query: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    rows, missing = [], []
    def read(source: str, reader: Any) -> None:
        try:
            rows.extend(reader())
        except (OSError, ValueError, KeyError, TypeError) as exc:
            missing.append({"source": source, "reason": str(exc) if re.fullmatch(r"[a-z][a-z_]{1,80}", str(exc)) else type(exc).__name__})
        except Exception as exc:
            # Source diagnostics are data; never expose raw paths, SQL, routes, or credentials.
            missing.append({"source": source, "reason": str(exc) if re.fullmatch(r"[a-z][a-z_]{1,80}", str(exc)) else type(exc).__name__})
    kind = query.get("type")
    if kind in (None, "trade"):
        def trades() -> list[dict[str, Any]]:
            ledger = resolve_position_ledger_sqlite_path(base=base, cfg=cfg, config_path=config_path, runtime_root=base)
            intake = resolve_trade_intake_config(cfg)
            requested = [Path(source["inbox_path"]) for source in intake.get("sources") or []]
            if not requested:
                requested = [base / "output_shared/state/trade_intake_inbox.sqlite3"]
            paths = {resolve_execution_inbox_path(SimpleNamespace(db_path=ledger), path if path.is_absolute() else base / path) for path in requested}
            return [row for path in paths for row in query_trade_receipts(path, accounts=accounts, query=query)]
        read("trade_inbox", trades)
        read("trade_lifecycle", lambda: query_lifecycle_receipts(
            resolve_position_ledger_sqlite_path(base=base, cfg=cfg, config_path=config_path, runtime_root=base), accounts=accounts, query=query))
    if kind in (None, "monitor", "scheduled"):
        for account in accounts:
            read("daily_brief:" + account, lambda account=account: query_daily_brief_receipts(base=base, account=account, market=market, query=query))
    if kind in (None, "monitor"):
        for account in accounts:
            read("maintenance:" + account, lambda account=account: query_maintenance_receipts(base=base, account=account, query=query))
    if kind in (None, "scheduled"):
        read("scheduled_run", lambda: query_run_receipts(base=base, accounts=accounts, query=query))
    return rows, missing


def _compact(row: dict[str, Any], *, detail: bool) -> dict[str, Any]:
    out = {key: value for key, value in row.items() if value is not None and key not in {"receipt_body", "related", "business_result"}}
    result = row.get("business_result")
    if isinstance(result, dict):
        out["business_result"] = {key: result[key] for key in ("status", "reason", "receipt_kind", "delivery_kind", "source_kind", "diagnostics") if key in result}
        if len(json.dumps(out["business_result"], ensure_ascii=False)) > 600:
            out["business_result"] = {key: result[key] for key in ("status", "reason", "receipt_kind") if key in result}
    if not detail:
        out.pop("recorded_at", None)
        out.pop("business_result", None)
    return out


def _receipt_read(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    path, cfg = load_runtime_config(config_key=payload.get("config_key"), config_path=payload.get("config_path"))
    allowed = sorted(set(accounts_from_config(cfg, fallback=())))
    if not allowed:
        raise AgentToolError(code="SCOPE_DENIED", message="receipt query has no configured accounts")
    market = infer_runtime_config_market(config=cfg, config_key=payload.get("config_key"), config_path=path)
    if market not in {"us", "hk"}:
        raise AgentToolError(code="SCOPE_DENIED", message="receipt query market is unbound")
    query = {key: value for key in _FILTERS if (value := payload.get(key)) is not None}
    if query.get("account"):
        query["account"] = str(query["account"]).lower()
    if query.get("market"):
        query["market"] = str(query["market"]).upper()
    for field in ("start_time", "end_time"):
        if field in query:
            normalized = receipt_time(query[field])
            if normalized is None:
                raise AgentToolError(code="INPUT_ERROR", message=field + " requires an explicit timezone")
            query[field] = normalized
    identity = {key: payload.get(key) for key in ("authenticated_channel", "authenticated_sender_id", "authenticated_conversation_id", "authority_scope")}
    scope_hash = receipt_digest({"config": str(path.resolve()), "accounts": allowed, "market": market, "identity": identity})
    state = None
    key = None
    if payload.get("cursor"):
        key = _cursor_key()
        try:
            state = decode_evidence_cursor(payload["cursor"], key)
            if state.get("tool") != "receipt_read" or state.get("scope") != scope_hash:
                raise ValueError("scope changed")
            prior = state["query"]
            if any(prior.get(field) != value for field, value in query.items()):
                raise ValueError("query changed")
            query = prior
        except (TradeEventPaginationError, ValueError, KeyError, TypeError) as exc:
            raise AgentToolError(code="cursor_invalidated", message="receipt cursor changed, expired, or is invalid; start a new query") from exc
    if query.get("account") and query["account"] not in allowed:
        raise AgentToolError(code="SCOPE_DENIED", message="receipt account outside configured scope")
    if query.get("market") and query["market"].lower() != market:
        raise AgentToolError(code="SCOPE_DENIED", message="receipt market outside configured scope")
    if query.get("start_time") and query.get("end_time") and query["start_time"] > query["end_time"]:
        raise AgentToolError(code="INPUT_ERROR", message="start_time must precede end_time")
    # Market is trusted scope even when the model omits it; cursors retain it.
    query["market"] = market.upper()
    accounts = [query["account"]] if query.get("account") else allowed
    base = resolve_runtime_root(repo_root=repo_base()).runtime_root
    rows, missing = _sources(base=base, cfg=cfg, config_path=path, accounts=allowed, market=market.upper(), query=query)
    bounded_rows = []
    for row in rows:
        if len(json.dumps(_compact(row, detail=True), ensure_ascii=False).encode("utf-8")) > 2200:
            missing.append({"source": row["event_ref"]["source"], "reason": "receipt_metadata_size_limit"})
        else:
            bounded_rows.append(row)
    unique = {(row["source_ref"], str(row.get("revision")), row["account"]): row for row in bounded_rows}
    rows = sorted(unique.values(), key=lambda row: (row.get("occurred_at") or "", row["source_ref"], str(row.get("revision"))), reverse=True)
    watermark = receipt_digest({"rows": rows, "missing": missing})
    if state and state.get("watermark") != watermark:
        raise AgentToolError(code="cursor_invalidated", message="receipt source revision changed; start a new query")
    limit = int(payload.get("limit", 10))
    offset = int(state.get("offset", 0)) if state else 0
    detail = bool(rows) and (len(rows) == 1 or (state and state.get("kind") == "body"))
    next_state = None
    scope: dict[str, Any] = {"query": query, "accounts": accounts, "market": market.upper()}
    if detail:
        row = rows[0]
        # Redact complete syntax before splitting; the watermark still binds raw source facts.
        body = redact_text(str(row.get("receipt_body") or ""))
        body_hash = hashlib.sha256(body.encode()).hexdigest()
        if state and (state.get("kind") != "body" or state.get("body_hash") != body_hash or state.get("event_ref") != row["event_ref"]):
            raise AgentToolError(code="cursor_invalidated", message="receipt body identity changed")
        end = min(len(body), offset + _BODY_CHARS)
        projected = _compact(row, detail=True)
        projected["receipt_body"] = body[offset:end]
        body_range = {"start": offset, "end": end, "total": len(body)}
        scope.update(event_ref=row["event_ref"], revision=row.get("revision"), body_range=body_range,
                     owner_accounts=row.get("accounts") or [row["account"]])
        value = {"rows": [projected], "body_range": body_range, "body_complete": end == len(body)}
        if end < len(body):
            next_state = {"kind": "body", "offset": end, "event_ref": row["event_ref"], "body_hash": body_hash}
        complete_for = "point"
    else:
        selected = []
        for row in rows[offset:offset + min(limit, 12)]:
            compact = _compact(row, detail=False)
            if selected and len(json.dumps([*selected, compact], ensure_ascii=False).encode("utf-8")) > _PAGE_BYTES:
                break
            selected.append(compact)
        end = offset + len(selected)
        value = {"rows": selected}
        if end < len(rows):
            next_state = {"kind": "events", "offset": end}
        complete_for = "requested_page" if next_state or offset else "full_query"
        scope["page_range"] = {"start": offset, "end": end}
    if next_state:
        key = key or _cursor_key()
        value["next_cursor"] = encode_evidence_cursor({"tool": "receipt_read", "scope": scope_hash,
            "query": query, "watermark": watermark, **next_state}, key)
    else:
        value["next_cursor"] = None
    status = "partial" if missing and rows else ("unavailable" if missing else ("ok" if rows else "empty"))
    included = len(value["rows"])
    value.update(status=status, missing_sources=missing,
        coverage={"status": "partial" if missing else "complete", "complete_for": complete_for,
                  "included_count": included, "total_count": None if missing else len(rows),
                  "omitted_count": None if missing else len(rows) - included, "has_more": next_state is not None, "scope": scope},
        freshness={"status": "historical", "as_of": datetime.now(timezone.utc).isoformat()})
    return value, (["receipt sources partially unavailable"] if missing else []), {"read_only": True}


_INPUT = {field: {"type": "string", "minLength": 1, "maxLength": 200} for field in _FILTERS}
_INPUT.update({
    "type": {"type": "string", "enum": ["trade", "monitor", "scheduled"]},
    "market": {"type": "string", "enum": ["US", "HK", "us", "hk"]},
    "cursor": {"type": "string", "minLength": 1, "maxLength": 8192},
    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
    "config_key": {"type": "string", "enum": ["us", "hk"]},
    "config_path": {"type": "string", "minLength": 1},
    **{field: {"type": "string"} for field in ("authenticated_channel", "authenticated_sender_id", "authenticated_conversation_id", "authority_scope")},
})
RECEIPT_READ_TOOL = build_agent_tool(
    name="receipt_read", catalog_summary="读取成交、监控和定时任务回执原文或已保存业务内容，支持候选和正文续页。",
    description="Read retained business receipts. Frozen text is the original; reconstructed text is retained source facts. Receipt arrival never triggers analysis. Continue with next_cursor; body coverage applies only to this chunk. Re-query current business tools for current state.",
    requires=("receipt_sources",), capabilities=("receipts", "read_only", "runtime_artifacts"),
    input_schema=_INPUT, handler=_receipt_read, pure_read=True, allow_additional_input=False,
    safe_default_input={"limit": 10}, bot_input_fields=(*_FILTERS, "cursor", "limit"),
    output_contract={"schema_version": "receipt_read.output.v1", "evidence_type": "collection",
        "bounded_projection": "contract_fields", "coverage": "source_declared", "freshness": "source_declared",
        "primary_rows": "rows", "pagination": {"mode": "keyset"},
        "fact_fields": ["rows", "status", "next_cursor", "body_range", "body_complete", "missing_sources"],
        "model_preview_fields": ["status", "rows", "next_cursor", "body_range", "body_complete", "missing_sources", "coverage", "freshness"],
        "freshness_fields": ["freshness.as_of"], "missing_data_fields": ["missing_sources"]},
)
TOOLS = (RECEIPT_READ_TOOL,)
