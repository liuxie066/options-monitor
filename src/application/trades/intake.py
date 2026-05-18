from __future__ import annotations

from typing import Any, Callable, Protocol

from src.infrastructure.io_utils import utc_now


class TradePayloadEnrichmentResult(Protocol):
    payload: dict[str, Any]
    diagnostics: dict[str, Any]


TradePayloadEnrichmentReturn = dict[str, Any] | TradePayloadEnrichmentResult


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


def _attach_receipt_state(
    state: dict[str, Any],
    *,
    deal_id: str,
    result_dict: dict[str, Any],
    receipt_result: dict[str, Any],
) -> dict[str, Any]:
    key = str(deal_id or "").strip()
    if not key:
        return state
    out = {name: dict((state or {}).get(name) or {}) for name in ("processed_deal_ids", "failed_deal_ids", "unresolved_deal_ids")}
    bucket_name = None
    for candidate in ("processed_deal_ids", "failed_deal_ids", "unresolved_deal_ids"):
        if key in out[candidate]:
            bucket_name = candidate
            break
    if bucket_name is None:
        status = str(result_dict.get("status") or "").strip().lower()
        bucket_name = {
            "applied": "processed_deal_ids",
            "skipped": "processed_deal_ids",
            "failed": "failed_deal_ids",
            "unresolved": "unresolved_deal_ids",
        }.get(status)
    if bucket_name is None:
        return state
    item = dict(out[bucket_name].get(key) or {})
    receipt = dict(receipt_result or {})
    prior_receipt = item.get("receipt") if isinstance(item.get("receipt"), dict) else {}
    if (
        str(receipt.get("status") or "").strip().lower() == "skipped"
        and str(receipt.get("reason") or "").strip().lower() == "skipped_duplicate"
        and prior_receipt
    ):
        return state
    if receipt.get("status") != "skipped":
        receipt["attempt_count"] = int((prior_receipt or {}).get("attempt_count") or 0) + 1
    receipt.setdefault("updated_at", utc_now())
    item["receipt"] = receipt
    out[bucket_name][key] = item
    return out


def _receipt_audit_phase(receipt_result: dict[str, Any]) -> str:
    status = str(receipt_result.get("status") or "").strip().lower()
    if status == "sent":
        return "receipt_sent"
    if status in {"failed", "unconfirmed"}:
        return "receipt_failed"
    return "receipt_skipped"


def _finalize_trade_payload_result(
    *,
    result_dict: dict[str, Any],
    state: dict[str, Any],
    state_path: Any,
    audit_path: Any,
    payload: dict[str, Any],
    effective_payload: dict[str, Any],
    deal: object | None,
    apply_changes: bool,
    write_trade_intake_state_fn: Callable[[Any, dict[str, Any]], Any],
    append_trade_intake_audit_fn: Callable[[Any, dict[str, Any]], Any],
    on_result_fn: Callable[[dict[str, Any]], dict[str, Any] | None] | None,
) -> dict[str, Any]:
    if on_result_fn is None:
        return result_dict
    try:
        receipt_result = on_result_fn(
            {
                "payload": payload,
                "effective_payload": effective_payload,
                "deal": deal,
                "result": dict(result_dict),
                "state": state,
                "apply_changes": apply_changes,
                "state_path": state_path,
                "audit_path": audit_path,
            }
        )
    except Exception as exc:
        receipt_result = {
            "enabled": True,
            "status": "failed",
            "reason": "receipt_callback_exception",
            "delivery_confirmed": False,
            "message_id": None,
            "error_code": "RECEIPT_CALLBACK_EXCEPTION",
            "send_message": f"{type(exc).__name__}: {exc}",
        }
    if not isinstance(receipt_result, dict):
        return result_dict
    result_with_receipt = dict(result_dict)
    result_with_receipt["receipt"] = receipt_result
    append_trade_intake_audit_fn(
        audit_path,
        build_trade_intake_audit_event(
            _receipt_audit_phase(receipt_result),
            payload=effective_payload if deal is None else None,
            deal=deal,
            result=result_with_receipt,
            extra={"receipt": receipt_result},
        ),
    )
    if apply_changes:
        deal_id = (
            str(result_dict.get("deal_id") or "").strip()
            or str(getattr(deal, "deal_id", "") or "").strip()
            or _payload_deal_id(effective_payload)
            or _payload_deal_id(payload)
            or ""
        )
        if deal_id:
            state = _attach_receipt_state(
                state,
                deal_id=deal_id,
                result_dict=result_dict,
                receipt_result=receipt_result,
            )
            write_trade_intake_state_fn(state_path, state)
    return result_with_receipt


def build_trade_intake_audit_event(
    phase: str,
    *,
    payload: dict[str, Any] | None = None,
    deal: object | None = None,
    result: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"phase": str(phase)}
    if isinstance(payload, dict):
        out["payload"] = payload
    to_dict = getattr(deal, "to_dict", None)
    if callable(to_dict):
        raw_deal_dict = to_dict()
        deal_dict: dict[str, Any] = raw_deal_dict if isinstance(raw_deal_dict, dict) else {}
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
    enrich_trade_payload_fn: Callable[[dict[str, Any]], TradePayloadEnrichmentReturn] | None,
    normalize_trade_deal_fn: Callable[..., Any],
    resolve_trade_deal_fn: Callable[..., Any],
    on_result_fn: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
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
        elif isinstance(enrich_result, dict):
            effective_payload = enrich_result
        else:
            raise TypeError("enrich_trade_payload_fn must return a dict or an object with payload and diagnostics")
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
        return _finalize_trade_payload_result(
            result_dict=result_dict,
            state=state,
            state_path=state_path,
            audit_path=audit_path,
            payload=payload,
            effective_payload=effective_payload,
            deal=None,
            apply_changes=apply_changes,
            write_trade_intake_state_fn=write_trade_intake_state_fn,
            append_trade_intake_audit_fn=append_trade_intake_audit_fn,
            on_result_fn=on_result_fn,
        )
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
        return _finalize_trade_payload_result(
            result_dict=result_dict,
            state=state,
            state_path=state_path,
            audit_path=audit_path,
            payload=payload,
            effective_payload=effective_payload,
            deal=deal,
            apply_changes=apply_changes,
            write_trade_intake_state_fn=write_trade_intake_state_fn,
            append_trade_intake_audit_fn=append_trade_intake_audit_fn,
            on_result_fn=on_result_fn,
        )

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
    return _finalize_trade_payload_result(
        result_dict=result_dict,
        state=state,
        state_path=state_path,
        audit_path=audit_path,
        payload=payload,
        effective_payload=effective_payload,
        deal=deal,
        apply_changes=apply_changes,
        write_trade_intake_state_fn=write_trade_intake_state_fn,
        append_trade_intake_audit_fn=append_trade_intake_audit_fn,
        on_result_fn=on_result_fn,
    )
