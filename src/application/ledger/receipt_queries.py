"""Read lifecycle notification evidence without opening the mutable repository."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from src.application.receipt_query import MAX_SOURCE_ROWS, receipt_digest, receipt_event, receipt_matches


def query_lifecycle_receipts(path: Path, *, accounts: list[str], query: dict[str, Any]) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError("ledger_missing")
    results = []
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, source, identity in (("trade_lifecycle_notification_outbox", "trade_lifecycle", "outbox_id"),
                                         ("trade_lifecycle_notification_delivery_batches", "trade_lifecycle_batch", "batch_id")):
            if table not in tables:
                raise ValueError("lifecycle_source_schema_unavailable")
            where, args = "", []
            if query.get("event_id"):
                where = f" WHERE {identity} = ?"
                args.append(str(query["event_id"]).removeprefix(source + ":"))
            from datetime import datetime
            for field, operator in (("start_time", ">="), ("end_time", "<=")):
                if query.get(field):
                    where += (" AND " if where else " WHERE ") + "created_at_ms " + operator + " ?"
                    args.append(int(datetime.fromisoformat(query[field]).timestamp() * 1000))
            max_size = conn.execute(f"SELECT max(length(payload_json) + coalesce(length(provider_receipt_json), 0)) FROM {table}{where}", args).fetchone()[0]
            if max_size and max_size > 262144:
                raise ValueError("lifecycle_receipt_size_limit")
            rows = conn.execute(f"SELECT *, length(payload_json) AS body_size FROM {table}{where} ORDER BY created_at_ms DESC LIMIT ?", [*args, MAX_SOURCE_ROWS + 1]).fetchall()
            if len(rows) > MAX_SOURCE_ROWS:
                raise ValueError("lifecycle_query_needs_narrowing")
            for raw in rows:
                row = dict(raw)
                if row["body_size"] > 262144:
                    raise ValueError("lifecycle_receipt_size_limit")
                payload = json.loads(row["payload_json"])
                members = [member.get("payload") or {} for member in payload.get("members", [])] if source.endswith("batch") else [payload]
                labels = {str(member.get("account") or "").lower() for member in members}
                if not labels or "" in labels:
                    raise ValueError("lifecycle_account_unlinkable")
                if query.get("account") and query["account"] not in labels:
                    continue
                if not labels.issubset(set(accounts)):
                    if labels.intersection(accounts):
                        raise ValueError("lifecycle_batch_account_scope_incomplete")
                    continue
                # A batch body may mention several accounts; only expose it when every owner is authorized.
                if source == "trade_lifecycle" and row.get("delivery_batch_id"):
                    continue  # the batch is the delivered message, not another send of each member
                provider = json.loads(row.get("provider_receipt_json") or "{}")
                body = provider.get("rendered_message")
                if body is not None:
                    import hashlib
                    if hashlib.sha256(body.encode()).hexdigest() != provider.get("message_sha256"):
                        raise ValueError("lifecycle_receipt_digest_mismatch")
                matched_members = members
                if query.get("deal_id"):
                    matched_members = []
                    for member in members:
                        deal = str(member.get("deal_id") or "")
                        broker_key = str(member.get("broker_deal_key") or "")
                        parts = broker_key.split(":", 3)
                        if len(parts) == 4 and parts[0] == "futu" and parts[1] == member.get("account") and parts[2]:
                            deal = parts[3]
                        linked = deal == query["deal_id"]
                        case_id = member.get("case_id") or row.get("case_id")
                        if not linked and case_id and "trade_lifecycle_evidence" in tables:
                            linked = conn.execute("SELECT 1 FROM trade_lifecycle_evidence WHERE case_id=? AND account=? AND (source_event_id=? OR json_extract(raw_json, '$.deal_id')=? OR json_extract(raw_json, '$.option_deal.deal_id')=?) LIMIT 1",
                                (case_id, member["account"], query["deal_id"], query["deal_id"], query["deal_id"])).fetchone() is not None
                        if linked:
                            matched_members.append(member)
                    if not matched_members:
                        continue
                if query.get("symbol"):
                    matched_members = [member for member in matched_members if str(member.get("symbol") or "").upper() == str(query["symbol"]).upper()]
                    if not matched_members:
                        continue
                source_market = payload.get("market")
                if source.endswith("batch"):
                    # The frozen message belongs to all members. Never fill historical
                    # scope from today's config or expose only part of a mixed body.
                    markets = {str(member.get("market") or "").upper() for member in members}
                    if "" in markets:
                        raise ValueError("receipt_market_unlinkable")
                    if len(markets) != 1:
                        raise ValueError("lifecycle_batch_market_scope_incomplete")
                    source_market = next(iter(markets))
                event = receipt_event(source=source, event_id=row[identity], account=next(iter(labels)) if len(labels) == 1 else "",
                    market=source_market, kind="trade", occurred=row["created_at_ms"],
                    recorded=row["updated_at_ms"], revision=row.get("resolution_revision") or row.get("payload_hash"),
                    body=body, business_result=payload, delivery=row["status"],
                    symbol=query.get("symbol") or payload.get("symbol"), deal_id=query.get("deal_id") or str(payload.get("deal_id") or "") or None,
                    run_id=payload.get("run_id"), diagnostic_code=payload.get("reason"),
                    related={"case_id": row.get("case_id"), "members": [member.get("outbox_id") for member in payload.get("members", [])]})
                event["accounts"] = sorted(labels)
                if receipt_matches(event, query):
                    results.append(event)
    return results
