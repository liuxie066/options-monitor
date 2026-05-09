from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from scripts.futu_gateway import build_futu_gateway
from scripts.trade_account_identity import extract_primary_account_id
from scripts.trade_symbol_identity import resolve_symbol_identity


def _rows(data: Any) -> list[dict[str, Any]]:
    if hasattr(data, "to_dict"):
        try:
            recs = data.to_dict("records")
            if isinstance(recs, list):
                return [dict(r) for r in recs if isinstance(r, dict)]
        except Exception:
            pass
    if isinstance(data, list):
        return [dict(r) for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        return [dict(data)]
    return []


def _norm_str(value: Any) -> str:
    return str(value or "").strip()


def _matches_identifier(row: dict[str, Any], *, order_id: str, deal_id: str) -> bool:
    if order_id:
        row_order = _norm_str(row.get("order_id") or row.get("orderID"))
        if row_order and row_order == order_id:
            return True
    if deal_id:
        row_deal = _norm_str(row.get("deal_id") or row.get("dealID") or row.get("id"))
        if row_deal and row_deal == deal_id:
            return True
    return False


def _extract_account_id(row: dict[str, Any], *, fallback_acc_id: str) -> str:
    return extract_primary_account_id(row) or fallback_acc_id


def _numeric_account_id(value: str) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


_SYMBOL_CANDIDATE_KEYS = (
    "symbol",
    "underlying_symbol",
    "owner_symbol",
    "owner_stock_code",
    "owner_stock_code_full",
    "underlying_stock_code",
    "owner_code",
    "underlying_code",
    "stock_code",
    "code",
    "owner_stock_name",
    "underlying_stock_name",
    "owner_name",
    "stock_name",
    "name",
    "underlying",
)

_AUTHORITATIVE_UNDERLIER_KEYS = {
    "underlying_symbol",
    "owner_symbol",
    "owner_stock_code",
    "owner_stock_code_full",
    "underlying_stock_code",
    "owner_code",
    "underlying_code",
}

_DISPLAY_NAME_KEYS = {
    "owner_stock_name",
    "underlying_stock_name",
    "owner_name",
    "stock_name",
    "name",
    "underlying",
}


def _symbol_candidate_rank(key: str, source_kind: str) -> int:
    if key in _AUTHORITATIVE_UNDERLIER_KEYS and source_kind != "option_code":
        return 0
    if key in {"symbol", "stock_code"} and source_kind != "option_code":
        return 1
    if key in _DISPLAY_NAME_KEYS and source_kind != "option_code":
        return 2
    if source_kind == "option_code":
        return 4
    return 3


def _resolve_unified_symbol(src: dict[str, Any], row: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for source_priority, (source_name, container) in enumerate((("futu_lookup_row", row), ("payload", src))):
        if not isinstance(container, dict):
            continue
        for key in _SYMBOL_CANDIDATE_KEYS:
            raw_value = container.get(key)
            identity = resolve_symbol_identity(raw_value)
            if identity is None:
                continue
            candidates.append(
                {
                    "rank": _symbol_candidate_rank(key, identity.source_kind),
                    "source_priority": source_priority,
                    "source": source_name,
                    "key": key,
                    "raw": raw_value,
                    "canonical": identity.canonical,
                    "source_kind": identity.source_kind,
                    "market": identity.market,
                    "futu_code": identity.futu_code,
                }
            )
    if not candidates:
        return None, {"selected": None, "candidates": []}
    candidates.sort(key=lambda item: (int(item["rank"]), int(item["source_priority"]), str(item["key"])))
    selected = candidates[0]
    conflicts = sorted(
        {
            str(item["canonical"])
            for item in candidates
            if int(item["rank"]) <= 2 and str(item["canonical"]) != str(selected["canonical"])
        }
    )
    return str(selected["canonical"]), {
        "selected": {
            key: selected[key]
            for key in ("source", "key", "raw", "canonical", "source_kind", "market", "futu_code")
        },
        "conflicting_high_confidence_symbols": conflicts,
        "candidates": [
            {
                key: item[key]
                for key in ("source", "key", "raw", "canonical", "source_kind", "rank")
            }
            for item in candidates
        ],
    }


@dataclass(frozen=True)
class TradePushAccountLookupResult:
    payload: dict[str, Any]
    diagnostics: dict[str, Any] = field(default_factory=dict)


def _query_rows(gateway: Any, method_name: str, **kwargs: Any) -> tuple[list[dict[str, Any]], str | None]:
    method = getattr(gateway, method_name)
    try:
        rows = _rows(method(**kwargs))
        return rows, None
    except Exception as exc:
        return [], str(exc)


def _merge_lookup_row(
    src: dict[str, Any],
    row: dict[str, Any],
    *,
    fallback_acc_id: str,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    enriched = dict(src)
    enriched["futu_account_id"] = _extract_account_id(row, fallback_acc_id=fallback_acc_id)
    for key, value in row.items():
        if key in enriched and enriched.get(key) not in (None, ""):
            continue
        if value in (None, ""):
            continue
        enriched[key] = value
    symbol, symbol_diagnostics = _resolve_unified_symbol(src, row)
    if symbol:
        enriched["symbol"] = symbol
    if diagnostics is not None:
        diagnostics["symbol_resolution"] = symbol_diagnostics
    return enriched


def enrich_trade_push_payload_with_account_id(
    payload: dict[str, Any] | Any,
    *,
    host: str,
    port: int,
    futu_account_ids: Iterable[str],
) -> TradePushAccountLookupResult:
    src = dict(payload) if isinstance(payload, dict) else {}
    existing_account_id = extract_primary_account_id(src) or ""
    candidate_ids: list[str] = []
    for raw_id in (existing_account_id, *futu_account_ids):
        acc_id = str(raw_id or "").strip()
        if acc_id and acc_id not in candidate_ids:
            candidate_ids.append(acc_id)
    diagnostics: dict[str, Any] = {
        "existing_account_id": existing_account_id or None,
        "candidate_account_ids": candidate_ids,
        "order_id": None,
        "deal_id": None,
        "matched_via": None,
        "query_errors": [],
        "tried_queries": [],
    }
    order_id = _norm_str(src.get("order_id") or src.get("orderID"))
    deal_id = _norm_str(src.get("deal_id") or src.get("dealID"))
    diagnostics["order_id"] = order_id or None
    diagnostics["deal_id"] = deal_id or None
    fallback_payload = dict(src)
    if existing_account_id:
        fallback_payload["futu_account_id"] = existing_account_id
    if not order_id and not deal_id:
        diagnostics["matched_via"] = "payload" if existing_account_id else "missing_identifiers"
        return TradePushAccountLookupResult(payload=fallback_payload, diagnostics=diagnostics)

    gateway = build_futu_gateway(host=host, port=port, is_option_chain_cache_enabled=False)
    try:
        candidate_ids = diagnostics["candidate_account_ids"]
        for acc_id in candidate_ids:
            numeric_acc_id = _numeric_account_id(str(acc_id))
            if numeric_acc_id is None:
                diagnostics["tried_queries"].append(
                    {"method": "account_scoped_lookup", "acc_id": acc_id, "skipped": "non_numeric_account_id"}
                )
                continue
            if order_id:
                query_kwargs = {"acc_id": numeric_acc_id, "order_id": order_id}
                rows, error = _query_rows(gateway, "get_order_list", **query_kwargs)
                diagnostics["tried_queries"].append({"method": "get_order_list", **query_kwargs, "rows": len(rows)})
                if error:
                    diagnostics["query_errors"].append({"method": "get_order_list", **query_kwargs, "error": error})
                for row in rows:
                    if _matches_identifier(row, order_id=order_id, deal_id=deal_id):
                        enriched = _merge_lookup_row(src, row, fallback_acc_id=acc_id, diagnostics=diagnostics)
                        diagnostics["matched_via"] = "order_lookup_by_acc_id"
                        return TradePushAccountLookupResult(payload=enriched, diagnostics=diagnostics)
            if deal_id:
                query_kwargs = {"acc_id": numeric_acc_id, "deal_id": deal_id, "order_id": order_id or None}
                rows, error = _query_rows(gateway, "get_deal_list", **query_kwargs)
                diagnostics["tried_queries"].append({"method": "get_deal_list", **query_kwargs, "rows": len(rows)})
                if error:
                    diagnostics["query_errors"].append({"method": "get_deal_list", **query_kwargs, "error": error})
                for row in rows:
                    if _matches_identifier(row, order_id=order_id, deal_id=deal_id):
                        enriched = _merge_lookup_row(src, row, fallback_acc_id=acc_id, diagnostics=diagnostics)
                        diagnostics["matched_via"] = "deal_lookup_by_acc_id"
                        return TradePushAccountLookupResult(payload=enriched, diagnostics=diagnostics)
        if order_id:
            query_kwargs = {"order_id": order_id}
            rows, error = _query_rows(gateway, "get_order_list", **query_kwargs)
            diagnostics["tried_queries"].append({"method": "get_order_list", **query_kwargs, "rows": len(rows)})
            if error:
                diagnostics["query_errors"].append({"method": "get_order_list", **query_kwargs, "error": error})
            for row in rows:
                if _matches_identifier(row, order_id=order_id, deal_id=deal_id):
                    resolved_acc_id = _extract_account_id(row, fallback_acc_id="")
                    if resolved_acc_id:
                        enriched = _merge_lookup_row(src, row, fallback_acc_id=resolved_acc_id, diagnostics=diagnostics)
                        diagnostics["matched_via"] = "order_lookup_without_acc_id"
                        return TradePushAccountLookupResult(payload=enriched, diagnostics=diagnostics)
        if deal_id:
            query_kwargs = {"deal_id": deal_id, "order_id": order_id or None}
            rows, error = _query_rows(gateway, "get_deal_list", **query_kwargs)
            diagnostics["tried_queries"].append({"method": "get_deal_list", **query_kwargs, "rows": len(rows)})
            if error:
                diagnostics["query_errors"].append({"method": "get_deal_list", **query_kwargs, "error": error})
            for row in rows:
                if _matches_identifier(row, order_id=order_id, deal_id=deal_id):
                    resolved_acc_id = _extract_account_id(row, fallback_acc_id="")
                    if resolved_acc_id:
                        enriched = _merge_lookup_row(src, row, fallback_acc_id=resolved_acc_id, diagnostics=diagnostics)
                        diagnostics["matched_via"] = "deal_lookup_without_acc_id"
                        return TradePushAccountLookupResult(payload=enriched, diagnostics=diagnostics)
    finally:
        gateway.close()
    diagnostics["matched_via"] = "payload" if existing_account_id else "not_found"
    return TradePushAccountLookupResult(payload=fallback_payload, diagnostics=diagnostics)
