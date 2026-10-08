from __future__ import annotations

from dataclasses import replace
from typing import Any

from domain.domain.trade_execution import option_execution_allocation
from domain.domain.ledger.events import lot_id_for_open_event
from domain.domain.trade_contract_identity import require_option_multiplier
from src.application.ledger.projection_verify import compare_projection_lots, _blocking_count
from src.application.ledger.commands import resolve_broker_trade_close_targets, summarize_broker_trade_close_candidates
from src.application.ledger.external_event_key import broker_deal_completion_payload
from src.application.ledger.preflight import (
    _current_record_fields, _split_close_deal_for_target, _preview_append_projection,
    _trade_open_ledger_inputs, preflight_broker_trade_close,
)
from src.application.ledger.results import BrokerTradeOperation
from src.application.ledger.repository_core import with_sqlite_repo_writer_lock
from src.application.ledger.writer_trade_events import (
    _trade_event_from_normalized_deal, persist_trade_event_objects_atomically,
)


def allocate_broker_option_execution(repo: Any, deal: Any, *, apply_changes: bool) -> list[BrokerTradeOperation]:
    """Allocate an ordinary, missing-effect option fill using the ledger's FIFO owner."""
    from contextlib import nullcontext
    with with_sqlite_repo_writer_lock(repo) if apply_changes else nullcontext():
        return _allocate(repo, deal, apply_changes=apply_changes)


def _allocate(repo: Any, deal: Any, *, apply_changes: bool) -> list[BrokerTradeOperation]:
    close_deal = replace(deal, position_effect="close")
    summary = summarize_broker_trade_close_candidates(repo, deal=close_deal)
    closing, opening = option_execution_allocation(int(deal.contracts), int(summary["exact_open_contracts"]))
    resolution = (resolve_broker_trade_close_targets(repo, deal=replace(close_deal, contracts=closing))
                  if closing else None)
    raw = dict(deal.raw_payload)
    raw.setdefault("qty", int(deal.contracts))
    raw.setdefault("source_deal_id", deal.deal_id)
    raw["position_effect_inference"] = {"source": "ledger_context", "reason": "opposing_lots_then_residual_open"}
    parent = replace(deal, raw_payload=raw)
    children = []
    preflights = []
    fields = []
    if resolution:
        for match in resolution.matches:
            current = _current_record_fields(repo, lot_id=match.lot_id)
            if require_option_multiplier(current.get("multiplier")) != require_option_multiplier(deal.multiplier):
                raise ValueError("unsupported_contract_multiplier")
            if current.get("deliverable"):
                raise ValueError("unsupported_contract_deliverable")
            if not match.candidate or not match.candidate.opened_at or match.candidate.opened_at >= deal.trade_time_ms:
                raise ValueError("unknown_position_effect:close_history_unproven")
            preflights.append(preflight_broker_trade_close(repo, lot_id=match.lot_id, fields=current,
                contracts_to_close=match.contracts_to_close, close_price=deal.price, as_of_ms=deal.trade_time_ms))
            children.append(_split_close_deal_for_target(replace(parent, position_effect="close"),
                lot_id=match.lot_id, fields=current, contracts_to_close=match.contracts_to_close,
                close_target_resolution=resolution.to_dict()))
            fields.append(None)
    if opening:
        child = replace(parent, position_effect="open", contracts=opening)
        child, opening_fields, _ = _trade_open_ledger_inputs(child)
        children.append(child)
        fields.append(opening_fields)
        preflights.append(None)
    events = []
    for index, child in enumerate(children, 1):
        child_raw = dict(child.raw_payload)
        child_raw["broker_deal_completion"] = broker_deal_completion_payload(
            source_deal_id=deal.deal_id, expected_contracts=int(deal.contracts), split_count=len(children),
            split_index=index, allocated_contracts=int(child.contracts))
        events.append(_trade_event_from_normalized_deal(replace(child, raw_payload=child_raw)))
    preview = _preview_append_projection(repo, events=events, operation_label="broker option allocation",
                               details={"source_deal_id": deal.deal_id})
    current = preview.current_projection
    comparison = compare_projection_lots(projected_lots=current.lots,
        current_lots=repo.list_position_lots(), diagnostics=current.diagnostics)
    if getattr(getattr(repo, "primary_repo", repo), "db_path", None) and _blocking_count(comparison["summary"]):
        raise ValueError("ledger_position_projection_mismatch")
    results = persist_trade_event_objects_atomically(repo, events) if apply_changes else [None] * len(events)
    return [BrokerTradeOperation(action=f"{deal.side}_{event.event_type}",
            event_id=event.event_id, lot_id=event.target_lot_id or lot_id_for_open_event(event.to_dict()),
            contracts_to_close=int(event.contracts) if event.event_type == "close" else None,
            fields=field, ledger_preflight=preflight,
            result=result if result is not None else {"event": event.to_dict()},
            details={"contracts": int(event.contracts), "position_side": event.position_side})
            for event, result, preflight, field in zip(events, results, preflights, fields, strict=True)]
