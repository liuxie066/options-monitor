from __future__ import annotations

from domain.domain.ledger import TradeEvent


def is_unexecuted_expire_close(event: TradeEvent) -> bool:
    payload = event.raw_payload or {}
    return bool(
        event.event_type == "expire_close"
        and not str(payload.get("order_id") or "").strip()
        and event.source != "opend_push"
        and not any(
            str(payload.get(key) or "").strip()
            for key in ("source_deal_id", "deal_id", "futu_deal_id")
        )
        and str(payload.get("source_type") or "").strip().lower()
        == "system_trade_event"
    )
