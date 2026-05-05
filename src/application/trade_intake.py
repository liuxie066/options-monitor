from __future__ import annotations

from typing import Any, Callable


def _payload_deal_id(payload: dict[str, Any] | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("deal_id", "dealID", "id"):
        raw = payload.get(key)
        value = str(raw or "").strip()
        if value:
            return value
    return None


def _exception_result_dict(
    exc: Exception,
    *,
    payload: dict[str, Any] | None = None,
    deal: object | None = None,
    stage: str,
) -> dict[str, Any]:
    action = None
    if deal is not None:
        position_effect = str(getattr(deal, "position_effect", "") or "").strip().lower()
        if position_effect in {"open", "close"}:
            action = position_effect
    return {
        "status": "failed",
        "action": action,
        "reason": f"exception:{type(exc).__name__}",
        "deal_id": (str(getattr(deal, "deal_id", "") or "").strip() or _payload_deal_id(payload)),
        "account": (str(getattr(deal, "internal_account", "") or "").strip() or None),
        "operations": [],
        "diagnostics": {
            "exception_stage": str(stage),
            "exception_type": type(exc).__name__,
            "exception_message": str(exc),
        },
    }


def _record_failed_deal_state(
    *,
    state: dict[str, Any],
    state_path: Any,
    result_dict: dict[str, Any],
    write_trade_intake_state_fn: Callable[[Any, dict[str, Any]], Any],
    upsert_deal_state_fn: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    deal_id = str(result_dict.get("deal_id") or "").strip()
    if not deal_id:
        return state
    state = upsert_deal_state_fn(
        state,
        bucket="failed_deal_ids",
        deal_id=deal_id,
        payload={
            "status": "failed",
            "action": result_dict.get("action"),
            "account": result_dict.get("account"),
            "applied_record_ids": [],
            "reason": result_dict.get("reason"),
            "diagnostics": dict(result_dict.get("diagnostics") or {}),
        },
    )
    write_trade_intake_state_fn(state_path, state)
    return state


def build_trade_intake_audit_event(
    phase: str,
    *,
    payload: dict | None = None,
    deal: object | None = None,
    result: dict | None = None,
    extra: dict | None = None,
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
        out["futu_account_id"] = deal_dict.get("futu_account_id")
        out["visible_account_fields"] = deal_dict.get("visible_account_fields")
        out["account_mapping_keys"] = deal_dict.get("account_mapping_keys")
        if deal_dict.get("normalization_diagnostics"):
            out["normalization_diagnostics"] = deal_dict.get("normalization_diagnostics")
    if isinstance(result, dict):
        out["result"] = result
        out["deal_id"] = out.get("deal_id") or result.get("deal_id")
        out["account"] = out.get("account") or result.get("account")
        out["action"] = result.get("action")
        out["status"] = result.get("status")
        out["reason"] = result.get("reason")
        out["futu_account_id"] = out.get("futu_account_id") or result.get("diagnostics", {}).get("futu_account_id")
        if result.get("diagnostics"):
            out["diagnostics"] = result.get("diagnostics")
    if isinstance(extra, dict) and extra:
        out.update(dict(extra))
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
    enrich_trade_payload_fn: Callable[[dict[str, Any]], dict[str, Any]] | None,
    normalize_trade_deal_fn: Callable[..., Any],
    resolve_trade_deal_fn: Callable[..., Any],
) -> dict[str, Any]:
    state = load_trade_intake_state_fn(state_path) if apply_changes else {}
    append_trade_intake_audit_fn(audit_path, build_trade_intake_audit_event("received", payload=payload))
    effective_payload = dict(payload)
    if enrich_trade_payload_fn is not None:
        enrich_result = enrich_trade_payload_fn(effective_payload)
        enrich_diagnostics: dict[str, Any] = {}
        if hasattr(enrich_result, "payload") and hasattr(enrich_result, "diagnostics"):
            effective_payload = dict(getattr(enrich_result, "payload") or {})
            enrich_diagnostics = dict(getattr(enrich_result, "diagnostics") or {})
        else:
            effective_payload = enrich_result
        if effective_payload != payload:
            append_trade_intake_audit_fn(audit_path, build_trade_intake_audit_event("enriched", payload=effective_payload))
        if enrich_diagnostics:
            append_trade_intake_audit_fn(
                audit_path,
                build_trade_intake_audit_event("enrichment_lookup", payload=effective_payload, extra={"enrichment": enrich_diagnostics}),
            )
    try:
        deal = normalize_trade_deal_fn(effective_payload, futu_account_mapping=account_mapping)
    except Exception as exc:
        result_dict = _exception_result_dict(exc, payload=effective_payload, stage="normalize")
        append_trade_intake_audit_fn(
            audit_path,
            build_trade_intake_audit_event("failed", payload=effective_payload, result=result_dict),
        )
        if apply_changes:
            state = _record_failed_deal_state(
                state=state,
                state_path=state_path,
                result_dict=result_dict,
                write_trade_intake_state_fn=write_trade_intake_state_fn,
                upsert_deal_state_fn=upsert_deal_state_fn,
            )
        return result_dict
    append_trade_intake_audit_fn(audit_path, build_trade_intake_audit_event("normalized", deal=deal))
    try:
        result = resolve_trade_deal_fn(deal, repo=repo, state=state, apply_changes=apply_changes)
        result_dict = result.to_dict()
        append_trade_intake_audit_fn(audit_path, build_trade_intake_audit_event("resolved", deal=deal, result=result_dict))
    except Exception as exc:
        result_dict = _exception_result_dict(exc, payload=effective_payload, deal=deal, stage="resolve")
        append_trade_intake_audit_fn(
            audit_path,
            build_trade_intake_audit_event("failed", deal=deal, result=result_dict),
        )
        if apply_changes:
            state = _record_failed_deal_state(
                state=state,
                state_path=state_path,
                result_dict=result_dict,
                write_trade_intake_state_fn=write_trade_intake_state_fn,
                upsert_deal_state_fn=upsert_deal_state_fn,
            )
        return result_dict

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
            try:
                prior = dict((state.get("unresolved_deal_ids") or {}).get(deal.deal_id) or {})
            except Exception:
                prior = {}
            diagnostics = dict(result_dict.get("diagnostics") or {})
            retryable = bool(diagnostics.get("retryable"))
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
                    "retryable": retryable,
                    "attempt_count": int(prior.get("attempt_count") or 0) + 1,
                    "diagnostics": diagnostics,
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
                    "diagnostics": dict(result_dict.get("diagnostics") or {}),
                },
            )
            write_trade_intake_state_fn(state_path, state)
    return result_dict
