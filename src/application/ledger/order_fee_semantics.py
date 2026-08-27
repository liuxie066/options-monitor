from __future__ import annotations

from domain.domain.ledger import TradeEvent


def is_unexecuted_expire_close(event: TradeEvent) -> bool:
    payload = event.raw_payload or {}
    if not (
        event.event_type == "expire_close"
        and not str(payload.get("order_id") or "").strip()
        and event.source != "opend_push"
        and not any(
            str(payload.get(key) or "").strip()
            for key in ("source_deal_id", "deal_id", "futu_deal_id")
        )
        and not payload.get("stock_settlement")
    ):
        return False
    source_type = str(payload.get("source_type") or "").strip().lower()
    if source_type == "system_trade_event":
        return True
    return bool(
        source_type == "broker_settlement_observation"
        and str(payload.get("schema_version") or "").strip()
        == "lifecycle_terminal_event.v2"
        and str(payload.get("close_type") or "").strip().lower()
        == "expire_auto_close"
    )


def zero_option_fee_lifecycle_reason(event: TradeEvent) -> str | None:
    if event.event_type == "assignment":
        return "assignment_without_option_trade"
    if is_unexecuted_expire_close(event):
        return "expired_without_executed_order"
    return None
