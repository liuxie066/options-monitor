from __future__ import annotations

from typing import Any, Callable, Protocol, cast

from src.application.positions.context_cache import invalidate_option_positions_context_cache
from src.application.trades.deal_identity import broker_deal_key
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
    deal_key: str | None = None,
) -> dict[str, Any]:
    deal_id = str(deal_key or result_dict.get("deal_id") or "").strip()
    if not deal_id:
        return state
    prior_receipt = _prior_receipt(state, deal_id)
    payload = {
        "status": "failed",
        "action": result_dict.get("action"),
        "account": result_dict.get("account"),
        "applied_record_ids": [],
        "reason": result_dict.get("reason"),
        "diagnostics": dict(result_dict.get("diagnostics") or {}),
    }
    if prior_receipt:
        payload["receipt"] = prior_receipt
    state = upsert_deal_state_fn(
        state,
        bucket="failed_deal_ids",
        deal_id=deal_id,
        payload=payload,
    )
    write_trade_intake_state_fn(state_path, state)
    return state


def _prior_receipt(state: dict[str, Any] | None, deal_id: str | None) -> dict[str, Any]:
    key = str(deal_id or "").strip()
    if not key or not isinstance(state, dict):
        return {}
    for bucket_name in ("processed_deal_ids", "failed_deal_ids", "unresolved_deal_ids"):
        bucket = state.get(bucket_name)
        if not isinstance(bucket, dict):
            continue
        item = bucket.get(key)
        if not isinstance(item, dict):
            continue
        receipt = item.get("receipt")
        return dict(receipt) if isinstance(receipt, dict) else {}
    return {}


def _migrate_compatible_legacy_deal_state(
    state: dict[str, Any],
    *,
    deal: object,
) -> dict[str, Any]:
    scoped_key = str(broker_deal_key(deal) or "").strip()
    legacy_key = str(getattr(deal, "deal_id", "") or "").strip()
    account = str(getattr(deal, "internal_account", "") or "").strip().lower()
    if not scoped_key or not legacy_key or scoped_key == legacy_key or not account:
        return state
    out = {
        name: dict((state or {}).get(name) or {})
        for name in ("processed_deal_ids", "failed_deal_ids", "unresolved_deal_ids")
    }
    if any(scoped_key in out[name] for name in out):
        return out
    for name in out:
        item = out[name].get(legacy_key)
        item_account = (
            str(item.get("account") or "").strip().lower()
            if isinstance(item, dict)
            else ""
        )
        if item_account == account:
            out[name][scoped_key] = dict(item)
            out[name].pop(legacy_key, None)
            break
    return out


def _ledger_store_from_repo(repo: Any) -> dict[str, Any] | None:
    store = getattr(repo, "ledger_store", None)
    to_dict = getattr(store, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        return dict(payload) if isinstance(payload, dict) else None
    return None


def _projection_status(result_dict: dict[str, Any]) -> str:
    status = str(result_dict.get("status") or "").strip().lower()
    reason = str(result_dict.get("reason") or "").strip().lower()
    if status == "applied":
        return "recorded_and_projected"
    if status == "failed" and reason == "projection_verification_failed":
        return "recorded_needs_review"
    return "not_recorded"


def _is_ignored_non_option_result(result_dict: dict[str, Any]) -> bool:
    status = str(result_dict.get("status") or "").strip().lower()
    reason = str(result_dict.get("reason") or "").strip().lower()
    return status == "skipped" and reason == "not_option_deal"


def _is_terminal_ledger_result(result_dict: dict[str, Any]) -> bool:
    status = str(result_dict.get("status") or "").strip().lower()
    reason = str(result_dict.get("reason") or "").strip().lower()
    return status == "skipped" and reason == "lifecycle_already_written"


def _attach_projection_check_fields(out: dict[str, Any]) -> None:
    diagnostics_raw = out.get("diagnostics")
    diagnostics = cast(dict[str, Any], diagnostics_raw) if isinstance(diagnostics_raw, dict) else {}
    verification_raw = diagnostics.get("post_write_projection_verification")
    verification = cast(dict[str, Any], verification_raw) if isinstance(verification_raw, dict) else {}
    checks_raw = verification.get("checks")
    checks = checks_raw if isinstance(checks_raw, list) else []
    first_check = cast(dict[str, Any], checks[0]) if checks and isinstance(checks[0], dict) else {}
    if "projection_diagnostic_count" in verification:
        out["projection_diagnostic_count"] = verification.get("projection_diagnostic_count")
    else:
        operations_raw = out.get("operations")
        operations = operations_raw if isinstance(operations_raw, list) else []
        first_operation = cast(dict[str, Any], operations[0]) if operations and isinstance(operations[0], dict) else {}
        result_raw = first_operation.get("result")
        result = cast(dict[str, Any], result_raw) if isinstance(result_raw, dict) else {}
        if "projection_diagnostic_count" in result:
            out["projection_diagnostic_count"] = result.get("projection_diagnostic_count")
    if not first_check:
        return
    out["target_lot_before"] = {
        "record_id": first_check.get("record_id"),
        "contracts_open": first_check.get("contracts_open_before"),
    }
    out["target_lot_after"] = {
        "record_id": first_check.get("record_id"),
        "contracts_open": first_check.get("actual_contracts_open_after"),
    }
    out["contracts_open_before"] = first_check.get("contracts_open_before")
    out["contracts_open_after"] = first_check.get("actual_contracts_open_after")


def _attach_runtime_write_diagnostics(
    *,
    result_dict: dict[str, Any],
    repo: Any,
    apply_changes: bool,
) -> dict[str, Any]:
    out = dict(result_dict)
    store = _ledger_store_from_repo(repo)
    if store is not None:
        out["ledger_store"] = store
    out["projection_status"] = _projection_status(out)
    _attach_projection_check_fields(out)
    if not apply_changes:
        return out
    if str(out.get("status") or "").strip().lower() != "applied":
        return out
    runtime_root = str((store or {}).get("runtime_root") or "").strip()
    if not runtime_root:
        return out
    try:
        out["context_invalidation"] = invalidate_option_positions_context_cache(
            runtime_root=runtime_root,
            account=str(out.get("account") or "").strip().lower() or None,
        )
    except Exception as exc:
        out["context_invalidation"] = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return out


def _attach_receipt_state(
    state: dict[str, Any],
    *,
    deal_id: str,
    result_dict: dict[str, Any],
    receipt_result: dict[str, Any],
) -> dict[str, Any]:
    if _is_ignored_non_option_result(result_dict):
        return state
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
    source: str,
) -> dict[str, Any]:
    if _is_ignored_non_option_result(result_dict):
        return result_dict
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
                "source": source,
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
            source=source,
            payload=effective_payload if deal is None else None,
            deal=deal,
            result=result_with_receipt,
            extra={"receipt": receipt_result},
        ),
    )
    if apply_changes:
        deal_id = (
            str(broker_deal_key(deal) if deal is not None else "").strip()
            or str(result_dict.get("deal_id") or "").strip()
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
    source: str | None = None,
    payload: dict[str, Any] | None = None,
    deal: object | None = None,
    result: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"phase": str(phase)}
    source_text = str(source or "").strip()
    if source_text:
        out["source"] = source_text
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
    on_stock_holdings_sync_fn: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    retry_failed_deal: bool = False,
    source: str = "push",
) -> dict[str, Any]:
    state = load_trade_intake_state_fn(state_path) if apply_changes else {}
    append_trade_intake_audit_fn(audit_path, build_trade_intake_audit_event("received", source=source, payload=payload))
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
            append_trade_intake_audit_fn(audit_path, build_trade_intake_audit_event("enriched", source=source, payload=effective_payload))
        if enrich_diagnostics:
            append_trade_intake_audit_fn(
                audit_path,
                build_trade_intake_audit_event(
                    "enrichment_lookup",
                    source=source,
                    payload=effective_payload,
                    extra={"enrichment": enrich_diagnostics},
                ),
            )
    try:
        deal = normalize_trade_deal_fn(effective_payload, futu_account_mapping=account_mapping)
    except Exception as exc:
        result_dict = _exception_result_dict(exc, payload=effective_payload, stage="normalize")
        append_trade_intake_audit_fn(
            audit_path,
            build_trade_intake_audit_event("failed", source=source, payload=effective_payload, result=result_dict),
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
            source=source,
        )
    append_trade_intake_audit_fn(audit_path, build_trade_intake_audit_event("normalized", source=source, deal=deal))
    if apply_changes:
        state = _migrate_compatible_legacy_deal_state(state, deal=deal)
    holdings_sync_intent: dict[str, Any] | None = None
    if on_stock_holdings_sync_fn is not None:
        try:
            callback_result = on_stock_holdings_sync_fn(
                {
                    "payload": payload,
                    "effective_payload": effective_payload,
                    "deal": deal,
                    "apply_changes": apply_changes,
                    "state_path": state_path,
                    "audit_path": audit_path,
                    "source": source,
                }
            )
            if isinstance(callback_result, dict):
                holdings_sync_intent = dict(callback_result)
        except Exception as exc:
            holdings_sync_intent = {
                "status": "failed",
                "reason": "dispatcher_callback_exception",
                "error": f"{type(exc).__name__}: {exc}",
            }
        if holdings_sync_intent is not None:
            append_trade_intake_audit_fn(
                audit_path,
                build_trade_intake_audit_event(
                    "stock_holdings_sync_intent",
                    source=source,
                    deal=deal,
                    extra={"stock_holdings_sync": holdings_sync_intent},
                ),
            )
    try:
        result = resolve_trade_deal_fn(
            deal,
            repo=repo,
            state=state,
            apply_changes=apply_changes,
            retry_failed_deal=retry_failed_deal,
        )
        result_dict = result.to_dict()
        result_dict = _attach_runtime_write_diagnostics(
            result_dict=result_dict,
            repo=repo,
            apply_changes=apply_changes,
        )
        if holdings_sync_intent is not None:
            result_dict["stock_holdings_sync"] = holdings_sync_intent
        append_trade_intake_audit_fn(audit_path, build_trade_intake_audit_event("resolved", source=source, deal=deal, result=result_dict))
    except Exception as exc:
        result_dict = _exception_result_dict(exc, payload=effective_payload, deal=deal, stage="resolve")
        if holdings_sync_intent is not None:
            result_dict["stock_holdings_sync"] = holdings_sync_intent
        append_trade_intake_audit_fn(
            audit_path,
            build_trade_intake_audit_event("failed", source=source, deal=deal, result=result_dict),
        )
        if apply_changes:
            state = _record_failed_deal_state(
                state=state,
                state_path=state_path,
                result_dict=result_dict,
                write_trade_intake_state_fn=write_trade_intake_state_fn,
                upsert_deal_state_fn=upsert_deal_state_fn,
                deal_key=broker_deal_key(deal),
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
            source=source,
        )

    deal_key = broker_deal_key(deal)
    if apply_changes and deal_key:
        if result.status == "applied" or _is_terminal_ledger_result(result_dict):
            reconciled_terminal = result.status != "applied"
            state = upsert_deal_state_fn(
                state,
                bucket="processed_deal_ids",
                deal_id=deal_key,
                payload={
                    "status": "reconciled" if reconciled_terminal else "applied",
                    "action": result.action,
                    "account": result.account,
                    "source_deal_id": deal.deal_id,
                    "futu_account_id": deal.futu_account_id,
                    "broker_deal_key": deal_key,
                    "applied_record_ids": [op.record_id for op in result.operations if op.record_id],
                    "reason": result.reason,
                    "diagnostics": (
                        {
                            "reconciled_from": "terminal_ledger_result",
                            **dict(result_dict.get("diagnostics") or {}),
                        }
                        if reconciled_terminal
                        else {}
                    ),
                },
            )
            write_trade_intake_state_fn(state_path, state)
            append_trade_intake_audit_fn(
                audit_path,
                {
                    "phase": "ledger_persisted",
                    "source": source,
                    "deal_id": deal.deal_id,
                    "account": result.account,
                    "event_id": deal_key,
                },
            )
        elif result.status == "unresolved":
            try:
                prior = dict((state.get("unresolved_deal_ids") or {}).get(deal_key) or {})
            except Exception:
                prior = {}
            diagnostics = dict(result_dict.get("diagnostics") or {})
            retryable = bool(diagnostics.get("retryable"))
            payload = {
                "status": "unresolved",
                "action": result.action,
                "account": result.account,
                "source_deal_id": deal.deal_id,
                "futu_account_id": deal.futu_account_id,
                "broker_deal_key": deal_key,
                "applied_record_ids": [],
                "reason": result.reason,
                "retryable": retryable,
                "attempt_count": int(prior.get("attempt_count") or 0) + 1,
                "diagnostics": diagnostics,
            }
            prior_receipt = _prior_receipt(state, deal_key)
            if prior_receipt:
                payload["receipt"] = prior_receipt
            state = upsert_deal_state_fn(
                state,
                bucket="unresolved_deal_ids",
                deal_id=deal_key,
                payload=payload,
            )
            write_trade_intake_state_fn(state_path, state)
        elif result.status == "failed":
            prior_receipt = _prior_receipt(state, deal_key)
            payload = {
                "status": "failed",
                "action": result.action,
                "account": result.account,
                "source_deal_id": deal.deal_id,
                "futu_account_id": deal.futu_account_id,
                "broker_deal_key": deal_key,
                "applied_record_ids": [],
                "reason": result.reason,
                "diagnostics": dict(result_dict.get("diagnostics") or {}),
            }
            if prior_receipt:
                payload["receipt"] = prior_receipt
            state = upsert_deal_state_fn(
                state,
                bucket="failed_deal_ids",
                deal_id=deal_key,
                payload=payload,
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
        source=source,
    )
