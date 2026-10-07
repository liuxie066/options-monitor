"""Read immutable decisions with an authorization-bound, append-stable page range."""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from domain.domain.daily_decision_brief import daily_brief_compatible_digests
from src.application.ledger.api import decode_evidence_cursor, encode_evidence_cursor, TradeEventPaginationError
from src.infrastructure.decision_history_sqlite import DecisionHistoryStore, DecisionHistoryError, content_hash, history_path


def _original_candidates(brief: dict[str, Any]) -> list[dict[str, Any]]:
    """Retain origin fields from this run, excluding carried-forward display actions."""
    candidates = list(brief.get("actions") or [])
    for family, rows in (brief.get("candidates") or {}).items():
        candidates.extend({**row, "strategy_family": family} for row in rows)
    seen, out = set(), []
    for candidate in candidates:
        if candidate.get("action_type") and candidate["action_type"] not in {"open_candidate", "open_combo_yield"}:
            continue
        source = candidate.get("source") or {}
        key = (source.get("final_candidate_id"), source.get("candidate_snapshot_hash"), candidate.get("wheel_branch_id"))
        if not all(key):
            # A displayed action is sufficient to describe a non-linked ordinary recommendation.
            if not candidate.get("action_id"):
                continue
            key = (candidate["action_id"],)
        if key in seen:
            continue
        seen.add(key)
        out.append({k: candidate.get(k) for k in ("action_id", "symbol", "strategy_family", "option_type", "wheel_branch_id", "source")})
    return out


def read_decision_history(*, base: Path, account: str, market: str, scope: dict[str, str],
                          start_date: str, end_date: str, cursor_key: Callable[[], str],
                          symbol: str | None = None, limit: int = 10, cursor: str | None = None,
                          reference: dict[str, Any] | None = None, authority: str = "",
                          cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    if not scope.get("futu_account_id") or scope.get("trade_env") not in {"REAL", "SIMULATE"}:
        raise ValueError("history_scope_unproven")
    if (date.fromisoformat(start_date).isoformat() != start_date or date.fromisoformat(end_date).isoformat() != end_date
            or date.fromisoformat(start_date) > date.fromisoformat(end_date)):
        raise ValueError("history_date_range_invalid")
    if not 1 <= limit <= 50:
        raise ValueError("history_page_size_invalid")
    symbol = symbol.upper() if symbol else None
    if reference and "source_run_id" in reference:
        # These are original source fields from notification perception, never its attempt run_id.
        reference = {"account": reference.get("account"), "market": reference.get("market"),
                     "market_trading_date": reference.get("market_date"), "revision": reference.get("revision"),
                     "run_id": reference.get("source_run_id"), "source_digest": reference.get("source_digest")}
    query = dict(account=account, market=market, scope=scope, start_date=start_date, end_date=end_date, symbol=symbol, reference=reference)
    binding = content_hash({"query": query, "authority": authority, "base": str(Path(base).resolve())})
    state = None
    if cursor:
        try:
            state = decode_evidence_cursor(cursor, cursor_key())
            if state.get("tool") != "decision_history_read" or state.get("binding") != binding:
                raise ValueError("history_cursor_scope_changed")
        except (TradeEventPaginationError, KeyError, ValueError) as exc:
            raise ValueError("history_cursor_invalid") from exc
    if cancelled and cancelled():
        raise ValueError("history_query_cancelled")
    result: dict[str, Any] = {"status": "unavailable", "rows": [], "missing": [], "next_cursor": None,
        "coverage": {"status": "partial", "complete_for": "requested_page", "scope": query},
        "freshness": {"status": "historical", "as_of": datetime.now(timezone.utc).isoformat()}}
    store = DecisionHistoryStore(history_path(base))
    try:
        with store.connect() as conn:
            if cancelled:
                conn.set_progress_handler(lambda: 1 if cancelled() else 0, 1000)
            imports = store.import_reports(conn, account=account, market=market)
            if imports:
                rejected = [row for row in imports[-1]["rows"] if row["status"] == "rejected"]
                if rejected:
                    result["missing"].append({"reason": "historical_sources_rejected", "count": len(rejected)})
            watermark = conn.execute("SELECT id,payload_hash,scope_hash FROM decisions ORDER BY id DESC LIMIT 1").fetchone()
            if state:
                ceiling = int(state["ceiling"])
                anchor = conn.execute("SELECT id,payload_hash,scope_hash FROM decisions WHERE id=?", (ceiling,)).fetchone()
                if not anchor or content_hash(list(anchor)) != state["anchor"]:
                    raise ValueError("history_cursor_invalid")
            else:
                ceiling = int(watermark[0]) if watermark else 0
                anchor = watermark
            after = int(state["after"]) if state else 0
            where = "account=? AND market=? AND market_date>=? AND market_date<=? AND id<=?"
            params: list[Any] = [account, market, start_date, end_date, ceiling]
            unknown = conn.execute("SELECT COUNT(*) FROM decisions WHERE " + where +
                " AND (json_extract(scope_json,'$.futu_account_id') IS NULL OR json_extract(scope_json,'$.futu_account_id')=''"
                " OR COALESCE(json_extract(scope_json,'$.trade_env'),'') NOT IN ('REAL','SIMULATE'))", params).fetchone()[0]
            if unknown:
                result["missing"].append({"reason": "historical_scope_unproven", "count": unknown})
            where += " AND json_extract(scope_json,'$.futu_account_id')=? AND json_extract(scope_json,'$.trade_env')=?"
            params.extend([scope["futu_account_id"], scope["trade_env"]])
            if symbol:
                where += " AND EXISTS(SELECT 1 FROM json_tree(decisions.payload_json) WHERE key='symbol' AND upper(value)=?)"
                params.append(symbol)
            if reference is not None:
                required = {"account", "market", "market_trading_date", "revision", "run_id", "source_digest"}
                if not required <= reference.keys() or reference["account"] != account or reference["market"] != market:
                    result["missing"].append({"reason": "notification_reference_unverifiable"})
                    result["history_query_available"] = True
                    return result
                where += " AND market_date=? AND revision=? AND run_id=?"
                params.extend([reference["market_trading_date"], reference["revision"], reference["run_id"]])
            total = conn.execute("SELECT COUNT(*) FROM decisions WHERE " + where, params).fetchone()[0]
            rows = conn.execute("SELECT * FROM decisions WHERE " + where + " AND id>? ORDER BY id LIMIT ?", [*params, after, limit + 1]).fetchall()
            for raw in rows[:limit]:
                if cancelled and cancelled():
                    raise ValueError("history_query_cancelled")
                try:
                    row = store.decode(raw)
                except DecisionHistoryError as exc:
                    result["missing"].append({"reason": str(exc), "revision": raw["revision"]})
                    continue
                brief = row["payload"]
                if reference is not None and reference["source_digest"] not in daily_brief_compatible_digests(brief):
                    result["missing"].append({"reason": "notification_reference_mismatch"})
                    continue
                result["rows"].append({"id": row["id"], "brief": brief, "scope": row["scope"],
                    "original_candidate_sources": _original_candidates(row["input"]),
                    "reference": {k: brief[k] for k in ("account", "market", "market_trading_date", "revision", "run_id")},
                    "source_digest": daily_brief_compatible_digests(brief)[0]})
            if reference is not None and not result["rows"]:
                result["missing"].append({"reason": "notification_reference_unverifiable"})
                result["history_query_available"] = True
            if len(rows) > limit:
                result["next_cursor"] = encode_evidence_cursor({"tool": "decision_history_read", "binding": binding,
                    "ceiling": ceiling, "anchor": content_hash(list(anchor)), "after": rows[limit-1]["id"]}, cursor_key())
            result["coverage"].update(total_count=total, included_count=len(result["rows"]),
                has_more=result["next_cursor"] is not None, decision_watermark=ceiling)
    except (DecisionHistoryError, OSError) as exc:
        result["missing"].append({"reason": str(exc) if isinstance(exc, DecisionHistoryError) else "history_read_failed"})
        result["rows"] = []
        return result
    gaps = any(row["brief"].get("data_gaps") for row in result["rows"])
    result["status"] = ("partial" if result["rows"] else "unavailable") if result["missing"] else ("partial" if gaps else "ok") if result["rows"] else "empty"
    result["coverage"]["status"] = "partial" if result["missing"] else "complete"
    return result
