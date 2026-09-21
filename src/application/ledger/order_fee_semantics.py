from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from domain.domain.ledger import TradeEvent
from domain.domain.trade_execution import futu_order_namespace_issue as futu_order_namespace_issue


def option_contract_identity(event: TradeEvent) -> tuple[Any, ...]:
    key = event.contract_key
    return (
        key.broker,
        key.account,
        key.underlying_symbol,
        key.option_type,
        event.position_side,
        key.strike,
        key.expiration_ymd,
        event.currency,
        event.multiplier,
    )


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


def option_fee_input_identity(event: TradeEvent) -> tuple[Any, ...]:
    """Economic inputs shared by source-deal fee allocation and estimation."""
    return (event.currency, event.price, event.multiplier, event.position_side)


def order_fee_currency_matches(currencies: Iterable[str], expected: str | None = None) -> bool:
    values = set(currencies)
    return (
        len(values) == 1
        and values <= {"CNY", "HKD", "USD"}
        and (expected is None or values == {expected})
    )


def source_deal_fee_group_problem(events: Sequence[TradeEvent]) -> str | None:
    rows = list(events)
    comparable = {
        (
            event.contract_key.broker,
            event.contract_key.account,
            option_fee_input_identity(event),
        )
        for event in rows
    }
    if len(comparable) != 1 or any(event.contracts <= 0 for event in rows):
        return "source_deal_fee_inputs_conflict"
    expected: set[int] = set()
    for event in rows:
        payload = event.raw_payload or {}
        completion = payload.get("broker_deal_completion")
        resolution = payload.get("close_target_resolution")
        raw_expected = (
            (completion or {}).get("expected_contracts")
            if isinstance(completion, Mapping)
            else None
        )
        if raw_expected in (None, "") and isinstance(resolution, Mapping):
            selector = resolution.get("selector")
            raw_expected = (
                selector.get("contracts_to_close")
                if isinstance(selector, Mapping)
                else None
            )
        if raw_expected in (None, ""):
            raw_expected = (resolution or {}).get("contracts_to_close") if isinstance(resolution, Mapping) else None
        try:
            if raw_expected not in (None, ""):
                expected.add(int(raw_expected))
        except (TypeError, ValueError):
            return "source_deal_fee_inputs_conflict"
    allocated = sum(event.contracts for event in rows)
    if not expected:
        return "source_deal_fee_contracts_unavailable"
    if len(expected) > 1 or expected != {allocated}:
        return "source_deal_fee_contracts_conflict"
    return None
