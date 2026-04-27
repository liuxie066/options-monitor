from __future__ import annotations

from typing import Any, Iterable

from scripts.futu_gateway import build_futu_gateway


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
    for key in ("futu_account_id", "trd_acc_id", "acc_id", "account_id", "trade_acc_id"):
        value = _norm_str(row.get(key))
        if value:
            return value
    return fallback_acc_id


def enrich_trade_push_payload_with_account_id(
    payload: dict[str, Any] | Any,
    *,
    host: str,
    port: int,
    futu_account_ids: Iterable[str],
) -> dict[str, Any]:
    src = dict(payload) if isinstance(payload, dict) else {}
    if any(_norm_str(src.get(key)) for key in ("futu_account_id", "trd_acc_id", "account_id", "account")):
        return src

    order_id = _norm_str(src.get("order_id") or src.get("orderID"))
    deal_id = _norm_str(src.get("deal_id") or src.get("dealID"))
    if not order_id and not deal_id:
        return src

    gateway = build_futu_gateway(host=host, port=port, is_option_chain_cache_enabled=False)
    try:
        for acc_id in [str(x).strip() for x in futu_account_ids if str(x).strip()]:
            if order_id:
                try:
                    rows = _rows(gateway.get_order_list(acc_id=int(acc_id), order_id=order_id))
                except Exception:
                    rows = []
                for row in rows:
                    if _matches_identifier(row, order_id=order_id, deal_id=deal_id):
                        enriched = dict(src)
                        enriched["futu_account_id"] = _extract_account_id(row, fallback_acc_id=acc_id)
                        return enriched
            if deal_id:
                try:
                    rows = _rows(gateway.get_deal_list(acc_id=int(acc_id), deal_id=deal_id, order_id=order_id or None))
                except Exception:
                    rows = []
                for row in rows:
                    if _matches_identifier(row, order_id=order_id, deal_id=deal_id):
                        enriched = dict(src)
                        enriched["futu_account_id"] = _extract_account_id(row, fallback_acc_id=acc_id)
                        return enriched
    finally:
        gateway.close()
    return src
