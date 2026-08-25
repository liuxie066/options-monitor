from __future__ import annotations

from typing import Any, Callable, Mapping

from domain.domain.option_position_identity import normalize_account, normalize_broker
from domain.domain.performance.engine import build_period_performance
from domain.domain.performance.models import validate_evidence_facts
from domain.domain.performance.period import PeriodRequest, PeriodWindow, normalize_period
from src.application.performance.adapters import (
    assigned_stock_instruments,
    load_assigned_stock_projection,
    load_ledger_performance_inputs,
    load_option_valuation_inputs,
)
from src.application.performance.evidence_collection import (
    CurrentEvidenceCollection,
    collect_current_performance_evidence,
)


def build_option_period_performance(
    repo: Any,
    *,
    period: PeriodWindow | PeriodRequest | dict[str, Any],
    account: str | None = None,
    broker: str | None = None,
    now_ms: int | None = None,
    include_rows: bool = True,
    evidence_repo: Any | None = None,
    refresh_quotes: bool = False,
    collection_cfg: Mapping[str, Any] | None = None,
    collection_base_dir: Any | None = None,
    evidence_collector: Callable[..., CurrentEvidenceCollection] | None = None,
    evidence_collector_kwargs: Mapping[str, Any] | None = None,
    scope_proven: bool = False,
) -> dict[str, Any]:
    window = period if isinstance(period, PeriodWindow) else normalize_period(period, now_ms=now_ms)
    account_filter = normalize_account(account) if account else None
    broker_filter = normalize_broker(broker) if broker else None
    inputs = load_ledger_performance_inputs(repo)
    opening = load_option_valuation_inputs(
        inputs,
        as_of_ms=window.valuation_open_at_ms,
        account=account_filter,
        broker=broker_filter,
    )
    ending = load_option_valuation_inputs(
        inputs,
        as_of_ms=window.valuation_end_at_ms,
        account=account_filter,
        broker=broker_filter,
    )

    if evidence_repo is None:
        schema_state = "not_provided"
        persisted_marks = ()
        persisted_rates = ()
        evidence_message = None
    else:
        bundle = evidence_repo.read_all()
        schema_state = str(bundle.schema_state)
        persisted_marks = tuple(bundle.valuation_marks)
        persisted_rates = tuple(bundle.fx_rates)
        evidence_message = getattr(bundle, "message", None)
    evidence_diagnostics: list[dict[str, Any]] = []
    if schema_state == "unsupported_schema":
        evidence_diagnostics.append(
            {
                "context": "valuation",
                "code": "performance_evidence_schema_unsupported",
                "message": str(evidence_message or "unsupported evidence schema"),
                "account": account_filter or "",
                "broker": broker_filter or "",
            }
        )

    preliminary_ending_assigned_stock = load_assigned_stock_projection(
        inputs,
        as_of_ms=window.valuation_end_at_ms,
        valuation_marks=persisted_marks,
        account=account_filter,
        broker=broker_filter,
    )
    stock_instruments = assigned_stock_instruments(preliminary_ending_assigned_stock)

    collector = evidence_collector or collect_current_performance_evidence
    if window.status != "partial_current":
        collection = CurrentEvidenceCollection(status="skipped_historical")
    elif not refresh_quotes:
        collection = CurrentEvidenceCollection(status="skipped_refresh_disabled")
    else:
        collection = collector(
            period_status=window.status,
            refresh_quotes=True,
            option_positions=ending.positions,
            stock_instruments=stock_instruments,
            now_ms=int(now_ms if now_ms is not None else window.valuation_end_at_ms),
            cfg=collection_cfg or {},
            base_dir=collection_base_dir,
            **dict(evidence_collector_kwargs or {}),
        )
    live_marks = collection.valuation_marks if window.status == "partial_current" else ()
    live_rates = collection.fx_rates if window.status == "partial_current" else ()
    if live_marks or live_rates:
        try:
            validate_evidence_facts(
                live_marks,
                live_rates,
                existing_marks=persisted_marks,
                existing_rates=persisted_rates,
            )
        except ValueError as exc:
            merge_diagnostic = {
                "code": "performance_evidence_merge_conflict",
                "error": str(exc),
            }
            collection = CurrentEvidenceCollection(
                status="evidence_conflict",
                diagnostics=(*collection.diagnostics, merge_diagnostic),
            )
            live_marks = ()
            live_rates = ()
    diagnostics = [
        *inputs.diagnostics,
        *opening.diagnostics,
        *ending.diagnostics,
        *evidence_diagnostics,
        *(
            {
                "code": str(item.get("code") or "performance_evidence_collection"),
                "message": str(item.get("error") or item.get("message") or ""),
                "event_time_ms": window.valuation_end_at_ms,
                "account": account_filter or "",
                "broker": broker_filter or "",
            }
            for item in collection.diagnostics
        ),
    ]
    combined_marks = (*persisted_marks, *live_marks)
    opening_assigned_stock = load_assigned_stock_projection(
        inputs,
        as_of_ms=window.valuation_open_at_ms,
        valuation_marks=combined_marks,
        account=account_filter,
        broker=broker_filter,
    )
    ending_assigned_stock = load_assigned_stock_projection(
        inputs,
        as_of_ms=window.valuation_end_at_ms,
        valuation_marks=combined_marks,
        account=account_filter,
        broker=broker_filter,
    )
    result = build_period_performance(
        events=inputs.events,
        allocations=inputs.allocations,
        period=window,
        account=account_filter,
        broker=broker_filter,
        diagnostics=diagnostics,
        opening_positions=opening.positions,
        ending_positions=ending.positions,
        valuation_marks=combined_marks,
        fx_rates=(*persisted_rates, *live_rates),
        opening_assigned_stock=opening_assigned_stock,
        ending_assigned_stock=ending_assigned_stock,
    )
    payload = result.to_dict(include_rows=include_rows)
    if scope_proven and _can_apply_proven_zero_semantics(payload):
        payload = _apply_proven_zero_semantics(payload)
    payload["evidence"] = {
        "schema_state": schema_state,
        "message": evidence_message,
        "persisted_valuation_mark_count": len(persisted_marks),
        "persisted_fx_rate_count": len(persisted_rates),
        "live_unpersisted_valuation_mark_count": len(live_marks),
        "live_unpersisted_fx_rate_count": len(live_rates),
        "collection": collection.to_dict(),
    }
    return payload


def _can_apply_proven_zero_semantics(payload: Mapping[str, Any]) -> bool:
    quality = payload.get("quality")
    if not isinstance(quality, Mapping):
        return False
    warnings = quality.get("warnings")
    return isinstance(warnings, list) and not warnings


def _apply_proven_zero_semantics(value: Any) -> Any:
    if isinstance(value, list):
        return [_apply_proven_zero_semantics(item) for item in value]
    if not isinstance(value, Mapping):
        return value
    out = {
        key: item
        if key in {"option_net_cashflow", "cashflow_return"}
        else _apply_proven_zero_semantics(item)
        for key, item in value.items()
    }
    if {"by_currency", "cny", "status", "missing"}.issubset(out) and out.get("status") == "not_observed":
        out["by_currency"] = {}
        out["cny"] = 0.0
        out["status"] = "observed"
        out["missing"] = []
    if {"status", "missing", "warnings", "evidence_fact_ids"}.issubset(out) and out.get("status") == "not_observed":
        out["status"] = "observed"
    return out


__all__ = ["build_option_period_performance"]
