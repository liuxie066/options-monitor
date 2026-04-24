from __future__ import annotations

from typing import Any, Callable


def build_trade_intake_audit_event(
    phase: str,
    *,
    payload: dict | None = None,
    deal: object | None = None,
    result: dict | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"phase": str(phase)}
    if isinstance(payload, dict):
        out["payload"] = payload
    if deal is not None and hasattr(deal, "to_dict"):
        deal_dict = deal.to_dict()
        out["deal"] = deal_dict
        out["deal_id"] = deal_dict.get("deal_id")
        out["account"] = deal_dict.get("internal_account")
        out["symbol"] = deal_dict.get("symbol")
        out["position_effect"] = deal_dict.get("position_effect")
        out["multiplier"] = deal_dict.get("multiplier")
        out["multiplier_source"] = deal_dict.get("multiplier_source")
    if isinstance(result, dict):
        out["result"] = result
        out["deal_id"] = out.get("deal_id") or result.get("deal_id")
        out["account"] = out.get("account") or result.get("account")
        out["action"] = result.get("action")
        out["status"] = result.get("status")
        out["reason"] = result.get("reason")
    return out


def process_trade_payload(
    payload: dict[str, Any],
    *,
    repo: Any,
    state_path: Any,
    audit_path: Any,
    account_mapping: dict[str, str],
    apply_changes: bool,
    load_trade_intake_state_fn: Callable[[Any], dict[str, Any]],
    write_trade_intake_state_fn: Callable[[Any, dict[str, Any]], Any],
    upsert_deal_state_fn: Callable[..., dict[str, Any]],
    append_trade_intake_audit_fn: Callable[[Any, dict[str, Any]], Any],
    normalize_trade_deal_fn: Callable[..., Any],
    resolve_trade_deal_fn: Callable[..., Any],
) -> dict[str, Any]:
    state = load_trade_intake_state_fn(state_path) if apply_changes else {}
    append_trade_intake_audit_fn(audit_path, build_trade_intake_audit_event("received", payload=payload))
    deal = normalize_trade_deal_fn(payload, futu_account_mapping=account_mapping)
    append_trade_intake_audit_fn(audit_path, build_trade_intake_audit_event("normalized", deal=deal))
    result = resolve_trade_deal_fn(deal, repo=repo, state=state, apply_changes=apply_changes)
    result_dict = result.to_dict()
    append_trade_intake_audit_fn(audit_path, build_trade_intake_audit_event("resolved", deal=deal, result=result_dict))

    if apply_changes and deal.deal_id:
        if result.status == "applied":
            state = upsert_deal_state_fn(
                state,
                bucket="processed_deal_ids",
                deal_id=deal.deal_id,
                payload={
                    "status": "applied",
                    "action": result.action,
                    "account": result.account,
                    "applied_record_ids": [op.get("record_id") for op in result.operations if op.get("record_id")],
                    "reason": result.reason,
                },
            )
            write_trade_intake_state_fn(state_path, state)
            append_trade_intake_audit_fn(
                audit_path,
                {
                    "phase": "ledger_persisted",
                    "deal_id": deal.deal_id,
                    "account": result.account,
                    "event_id": deal.deal_id,
                },
            )
        elif result.status == "unresolved":
            state = upsert_deal_state_fn(
                state,
                bucket="unresolved_deal_ids",
                deal_id=deal.deal_id,
                payload={
                    "status": "unresolved",
                    "action": result.action,
                    "account": result.account,
                    "applied_record_ids": [],
                    "reason": result.reason,
                },
            )
            write_trade_intake_state_fn(state_path, state)
        elif result.status == "failed":
            state = upsert_deal_state_fn(
                state,
                bucket="failed_deal_ids",
                deal_id=deal.deal_id,
                payload={
                    "status": "failed",
                    "action": result.action,
                    "account": result.account,
                    "applied_record_ids": [],
                    "reason": result.reason,
                },
            )
            write_trade_intake_state_fn(state_path, state)
    return result_dict
