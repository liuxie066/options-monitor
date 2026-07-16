from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from domain.domain.strategy_vocab import STRATEGY_COMBO_YIELD
from domain.domain.symbol_identity import canonical_symbol
from src.application.strategy_policy import YIELD_ENHANCEMENT_INCOME_UPSIDE_MODE
from src.application.ledger.api import (
    BrokerTradeOperation,
    CloseTargetResolution,
    LotCloseMatch as CloseMatch,
    LotCloseResolutionError,
    find_unique_open_position_lot,
    list_close_lot_candidates,
    record_normalized_trade_event,
    resolve_broker_trade_close_targets,
    summarize_broker_trade_close_candidates,
)
from src.application.trades.normalizer import NormalizedTradeDeal
from src.application.trades.lifecycle import LifecycleTradeResolution, resolve_lifecycle_trade_deal
from src.application.trades.state import is_failed_deal, is_retryable_unresolved_deal, lookup_deal_state
from src.application.trades.workflows import (
    BrokerAssignedStockSaleMatchError,
    apply_trade_close_with,
    apply_trade_open_with,
    execute_broker_assigned_stock_sale,
    preview_trade_close,
    preview_trade_open,
)


class OptionPositionsRepoLike(Protocol):
    def list_position_lots(self) -> list[dict[str, Any]]: ...
    def get_record_fields(self, record_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class IntakeResolution:
    status: str
    action: str | None
    reason: str
    deal_id: str | None
    account: str | None
    operations: list[BrokerTradeOperation]
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "action": self.action,
            "reason": self.reason,
            "deal_id": self.deal_id,
            "account": self.account,
            "operations": [item.to_payload() for item in self.operations],
            "diagnostics": dict(self.diagnostics),
        }


@dataclass(frozen=True)
class _PositionEffectInference:
    deal: NormalizedTradeDeal | None
    reason: str
    diagnostics: dict[str, Any] = field(default_factory=dict)


def _failure(
    *,
    status: str,
    action: str | None,
    reason: str,
    deal: NormalizedTradeDeal,
    operations: list[BrokerTradeOperation] | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> IntakeResolution:
    return IntakeResolution(
        status=status,
        action=action,
        reason=reason,
        deal_id=deal.deal_id,
        account=deal.internal_account,
        operations=list(operations or []),
        diagnostics=dict(diagnostics or {}),
    )


def _from_lifecycle_resolution(deal: NormalizedTradeDeal, result: LifecycleTradeResolution) -> IntakeResolution:
    return IntakeResolution(
        status=result.status,
        action=result.action,
        reason=result.reason,
        deal_id=deal.deal_id,
        account=deal.internal_account,
        operations=list(result.operations),
        diagnostics=dict(result.diagnostics),
    )


def _assigned_stock_sale_operation(out: dict[str, Any]) -> BrokerTradeOperation:
    sale_event = dict(out.get("sale_event") or {})
    stock_lot_id = str(sale_event.get("target_stock_lot_id") or "").strip() or None
    stock_event_id = str(sale_event.get("stock_event_id") or "").strip() or None
    raw_result = out.get("result") if isinstance(out.get("result"), dict) else None
    result = None
    if raw_result is not None:
        result = {
            "event_id": stock_event_id,
            "record_id": stock_lot_id,
            **dict(raw_result),
        }
    return BrokerTradeOperation(
        action="assigned_stock_sale",
        record_id=stock_lot_id,
        fields=sale_event,
        matched_by="assigned_stock_lot",
        event_id=stock_event_id,
        result=result,
        details={
            "write_model": out.get("write_model"),
            "mode": out.get("mode"),
        },
    )


def _resolve_broker_assigned_stock_sale(
    deal: NormalizedTradeDeal,
    *,
    repo: OptionPositionsRepoLike,
    apply_changes: bool,
) -> IntakeResolution | None:
    if str(deal.side or "").strip().lower() != "sell":
        return None
    try:
        out = execute_broker_assigned_stock_sale(repo, deal, dry_run=not apply_changes)
    except BrokerAssignedStockSaleMatchError as exc:
        if exc.code in {"no_match", "unsupported_deal"}:
            return None
        reason_by_code = {
            "missing_required_fields": "assigned_stock_sale_missing_required_fields",
            "no_safe_match": "assigned_stock_sale_no_safe_match",
            "ambiguous_match": "ambiguous_assigned_stock_sale",
        }
        reason = reason_by_code.get(exc.code, f"assigned_stock_sale_{exc.code}")
        return _failure(
            status="unresolved",
            action="assigned_stock_sale",
            reason=reason,
            deal=deal,
            diagnostics=dict(exc.diagnostics),
        )
    except ValueError as exc:
        return _failure(
            status="failed",
            action="assigned_stock_sale",
            reason="assigned_stock_sale_failed",
            deal=deal,
            diagnostics={"error": str(exc)},
        )

    status = "applied" if apply_changes else "dry_run"
    reason = "applied_assigned_stock_sale" if apply_changes else "preview_assigned_stock_sale"
    operation = _assigned_stock_sale_operation(out)
    diagnostics = {
        "assigned_stock_sale": {
            "mode": out.get("mode"),
            "write_model": out.get("write_model"),
            "sale_event": out.get("sale_event"),
            "stock_lot_before": out.get("stock_lot_before"),
            "stock_lot_after": out.get("stock_lot_after"),
            "match": out.get("match"),
            "review_rows": out.get("review_rows"),
            "result": out.get("result"),
            "idempotent_duplicate": out.get("idempotent_duplicate"),
        }
    }
    return IntakeResolution(
        status=status,
        action="assigned_stock_sale",
        reason=reason,
        deal_id=deal.deal_id,
        account=deal.internal_account,
        operations=[operation],
        diagnostics=diagnostics,
    )


def _missing_account_mapping_diagnostics(deal: NormalizedTradeDeal) -> dict[str, Any]:
    return {
        "futu_account_id": deal.futu_account_id,
        "visible_account_fields": dict(deal.visible_account_fields or {}),
        "account_mapping_keys": list(deal.account_mapping_keys or []),
    }


def _missing_required_fields_diagnostics(deal: NormalizedTradeDeal, missing: list[str]) -> dict[str, Any]:
    normalization = dict(getattr(deal, "normalization_diagnostics", {}) or {})
    multiplier_resolution = dict(normalization.get("multiplier_resolution") or {})
    symbol_info = dict(normalization.get("symbol") or {})
    retryable = set(missing) == {"multiplier"}
    return {
        "retryable": retryable,
        "missing_fields": list(missing),
        "canonical_symbol": deal.symbol,
        "raw_symbol_fields": dict(symbol_info.get("raw_fields") or {}),
        "multiplier_resolution": multiplier_resolution,
        "futu_account_id": deal.futu_account_id,
        "visible_account_fields": dict(deal.visible_account_fields or {}),
    }


def _invalid_required_fields_diagnostics(invalid: list[str]) -> dict[str, Any]:
    return {
        "retryable": False,
        "invalid_fields": list(invalid),
    }


def _required_open_missing(deal: NormalizedTradeDeal) -> list[str]:
    src = {
        "deal_id": deal.deal_id,
        "account": deal.internal_account,
        "symbol": deal.symbol,
        "option_type": deal.option_type,
        "contracts": deal.contracts,
        "price": deal.price,
        "strike": deal.strike,
        "multiplier": deal.multiplier,
        "expiration_ymd": deal.expiration_ymd,
        "currency": deal.currency,
        "trade_time_ms": deal.trade_time_ms,
    }
    return [k for k, v in src.items() if v in (None, "")]


def _required_open_invalid(deal: NormalizedTradeDeal) -> list[str]:
    invalid: list[str] = []
    try:
        if deal.contracts is not None and int(deal.contracts) <= 0:
            invalid.append("contracts")
    except Exception:
        invalid.append("contracts")
    try:
        if deal.strike is not None and float(deal.strike) <= 0:
            invalid.append("strike")
    except Exception:
        invalid.append("strike")
    try:
        if deal.multiplier is not None and int(deal.multiplier) <= 0:
            invalid.append("multiplier")
    except Exception:
        invalid.append("multiplier")
    return invalid


def _required_close_missing(deal: NormalizedTradeDeal) -> list[str]:
    src = {
        "deal_id": deal.deal_id,
        "account": deal.internal_account,
        "symbol": deal.symbol,
        "option_type": deal.option_type,
        "contracts": deal.contracts,
        "price": deal.price,
        "strike": deal.strike,
        "expiration_ymd": deal.expiration_ymd,
        "trade_time_ms": deal.trade_time_ms,
    }
    return [k for k, v in src.items() if v in (None, "")]


def load_close_candidate_records(repo: OptionPositionsRepoLike) -> list[dict[str, Any]]:
    return list_close_lot_candidates(repo)


def match_close_positions(repo: OptionPositionsRepoLike, deal: NormalizedTradeDeal) -> list[CloseMatch]:
    return list(match_close_targets(repo, deal).matches)


def match_close_targets(repo: OptionPositionsRepoLike, deal: NormalizedTradeDeal) -> CloseTargetResolution:
    try:
        return resolve_broker_trade_close_targets(repo, deal=deal)
    except LotCloseResolutionError as exc:
        if exc.code == "invalid_quantity":
            raise ValueError("contracts must be > 0 for close matching") from exc
        if exc.code == "insufficient_contracts":
            remaining = exc.remaining_contracts
            if remaining is not None:
                raise ValueError(f"close_match_insufficient_contracts: remaining={remaining}") from exc
            raise ValueError("close_match_insufficient_contracts") from exc
        if exc.code == "not_found":
            raise ValueError("close_match_not_found") from exc
        raise ValueError(str(exc)) from exc


def resolve_trade_deal(
    deal: NormalizedTradeDeal,
    *,
    repo: OptionPositionsRepoLike,
    state: dict[str, Any] | None,
    apply_changes: bool,
    persist_trade_event_fn=None,
    retry_failed_deal: bool = False,
) -> IntakeResolution:
    persist_fn = persist_trade_event_fn or record_normalized_trade_event
    can_retry_existing_deal = is_retryable_unresolved_deal(state, deal.deal_id) or (
        retry_failed_deal and is_failed_deal(state, deal.deal_id)
    )
    if lookup_deal_state(state, deal.deal_id) is not None and not can_retry_existing_deal:
        return _failure(status="skipped", action=None, reason="duplicate_deal_id", deal=deal)

    if not deal.deal_id:
        return _failure(status="unresolved", action=None, reason="missing_required_fields:deal_id", deal=deal)
    if deal.symbol and not deal.option_type and not deal.internal_account:
        return _failure(status="skipped", action=None, reason="not_option_deal", deal=deal)
    if not deal.internal_account:
        diagnostics = _missing_account_mapping_diagnostics(deal)
        futu_account_id = str(diagnostics.get("futu_account_id") or "").strip()
        reason = "missing_account_mapping"
        if futu_account_id:
            reason += f":futu_account_id={futu_account_id}"
        return _failure(
            status="unresolved",
            action=None,
            reason=reason,
            deal=deal,
                diagnostics=diagnostics,
        )
    lifecycle_result = resolve_lifecycle_trade_deal(deal, repo=repo, apply_changes=apply_changes)
    if lifecycle_result is not None and lifecycle_result.handled:
        return _from_lifecycle_resolution(deal, lifecycle_result)
    if deal.symbol and not deal.option_type:
        assigned_stock_sale = _resolve_broker_assigned_stock_sale(deal, repo=repo, apply_changes=apply_changes)
        if assigned_stock_sale is not None:
            return assigned_stock_sale
        return _failure(status="skipped", action=None, reason="not_option_deal", deal=deal)
    if not deal.symbol or not deal.option_type:
        return _failure(status="skipped", action=None, reason="not_option_deal", deal=deal)
    position_effect_diagnostics: dict[str, Any] = {}
    if deal.position_effect not in ("open", "close"):
        inference = _infer_missing_position_effect(deal, repo=repo)
        if inference.deal is None:
            return _failure(
                status="unresolved",
                action=None,
                reason=inference.reason,
                deal=deal,
                diagnostics=inference.diagnostics,
            )
        deal = inference.deal
        position_effect_diagnostics = {"position_effect_inference": inference.diagnostics}

    if deal.position_effect == "open":
        combo_enrichment = _enrich_combo_yield_open(deal, repo=repo)
        if combo_enrichment.deal is not None:
            deal = combo_enrichment.deal
            position_effect_diagnostics = {
                **position_effect_diagnostics,
                "combo_yield_enrichment": combo_enrichment.diagnostics,
            }
        if deal.side not in {"sell", "buy"}:
            return _failure(status="unresolved", action="open", reason="unsupported_open_side", deal=deal)
        missing = _required_open_missing(deal)
        if missing:
            return _failure(
                status="unresolved",
                action="open",
                reason="missing_required_fields:" + ",".join(missing),
                deal=deal,
                diagnostics=_missing_required_fields_diagnostics(deal, missing),
            )
        invalid = _required_open_invalid(deal)
        if invalid:
            return _failure(
                status="unresolved",
                action="open",
                reason="invalid_required_fields:" + ",".join(invalid),
                deal=deal,
                diagnostics=_invalid_required_fields_diagnostics(invalid),
            )
        if apply_changes:
            return IntakeResolution(
                status="applied",
                action="open",
                reason="applied_open",
                deal_id=deal.deal_id,
                account=deal.internal_account,
                operations=[apply_trade_open_with(repo, deal, persist_trade_event_fn=persist_fn)],
                diagnostics=position_effect_diagnostics,
            )
        preview = preview_trade_open(deal)
        return IntakeResolution(
            status="dry_run",
            action="open",
            reason="preview_open",
            deal_id=deal.deal_id,
            account=deal.internal_account,
            operations=[BrokerTradeOperation(action="open", fields=preview.fields)],
            diagnostics=position_effect_diagnostics,
        )

    missing = _required_close_missing(deal)
    if deal.side not in {"buy", "sell"}:
        return _failure(status="unresolved", action="close", reason="unsupported_close_side", deal=deal)
    if missing:
        return _failure(
            status="unresolved",
            action="close",
            reason="missing_required_fields:" + ",".join(missing),
            deal=deal,
        )
    try:
        close_target_resolution = match_close_targets(repo, deal)
    except ValueError as exc:
        return _failure(status="unresolved", action="close", reason=str(exc), deal=deal)
    matches = list(close_target_resolution.matches)
    close_target_diagnostics = {"close_target_resolution": close_target_resolution.to_dict()}

    operations: list[BrokerTradeOperation] = []
    if apply_changes:
        operations = apply_trade_close_with(
            repo,
            matches=matches,
            deal=deal,
            persist_trade_event_fn=persist_fn,
            close_target_resolution=close_target_resolution,
        )
        verification = _verify_applied_close_projection(repo=repo, operations=operations)
        if not verification["ok"]:
            return _failure(
                status="failed",
                action="close",
                reason="projection_verification_failed",
                deal=deal,
                operations=operations,
                diagnostics={**close_target_diagnostics, "post_write_projection_verification": verification},
            )
        return IntakeResolution(
            status="applied",
            action="close",
            reason="applied_close",
            deal_id=deal.deal_id,
            account=deal.internal_account,
            operations=operations,
            diagnostics={
                **position_effect_diagnostics,
                **close_target_diagnostics,
                "post_write_projection_verification": verification,
            },
        )

    operations = preview_trade_close(
        repo,
        matches=matches,
        deal=deal,
        close_target_resolution=close_target_resolution,
    )
    return IntakeResolution(
        status="dry_run",
        action="close",
        reason="preview_close",
        deal_id=deal.deal_id,
        account=deal.internal_account,
        operations=operations,
        diagnostics={**position_effect_diagnostics, **close_target_diagnostics},
    )


def _infer_missing_position_effect(
    deal: NormalizedTradeDeal,
    *,
    repo: OptionPositionsRepoLike,
) -> _PositionEffectInference:
    base_diagnostics = {
        "source": "ledger_context",
        "original_position_effect": deal.position_effect,
        "side": deal.side,
        "option_type": deal.option_type,
    }
    if deal.side not in {"buy", "sell"}:
        return _PositionEffectInference(
            deal=None,
            reason="unknown_position_effect",
            diagnostics={**base_diagnostics, "decision": "unsupported_side"},
        )
    missing_identity = [
        key
        for key, value in {
            "contracts": deal.contracts,
            "strike": deal.strike,
            "expiration_ymd": deal.expiration_ymd,
        }.items()
        if value in (None, "")
    ]
    if missing_identity:
        return _PositionEffectInference(
            deal=None,
            reason="unknown_position_effect",
            diagnostics={**base_diagnostics, "decision": "missing_inference_identity", "missing_fields": missing_identity},
        )

    close_deal = replace(deal, position_effect="close")
    close_candidate_summary = _close_candidate_summary(repo, close_deal)
    try:
        close_target_resolution = resolve_broker_trade_close_targets(repo, deal=close_deal)
    except LotCloseResolutionError as exc:
        close_diagnostics = {
            "close_match_error": exc.code,
            "close_candidate_summary": close_candidate_summary,
        }
        if close_candidate_summary["exact_contract_count"] > 0:
            return _PositionEffectInference(
                deal=None,
                reason=f"unknown_position_effect:{exc.code}",
                diagnostics={**base_diagnostics, **close_diagnostics, "decision": "close_target_unresolved"},
            )
    else:
        inferred = replace(
            close_deal,
            raw_payload=_with_position_effect_inference_payload(
                close_deal.raw_payload,
                inferred_effect="close",
                reason="matched_existing_position_lot",
            ),
        )
        return _PositionEffectInference(
            deal=inferred,
            reason="inferred_close",
            diagnostics={
                **base_diagnostics,
                "decision": "close",
                "close_target_resolution": close_target_resolution.to_dict(),
            },
        )

    if deal.side == "buy" and deal.option_type == "call":
        companion = _combo_yield_companion_short_put(repo, deal)
        inferred = replace(
            deal,
            position_effect="open",
            raw_payload=_with_combo_yield_long_call_payload(
                deal.raw_payload,
                deal=deal,
                companion=companion,
                inferred_position_effect=True,
            ),
        )
        return _PositionEffectInference(
            deal=inferred,
            reason="inferred_combo_yield_long_call_open",
            diagnostics={
                **base_diagnostics,
                "decision": "open",
                "open_reason": (
                    "buy_call_with_companion_short_put"
                    if companion is not None
                    else "buy_call_without_close_target"
                ),
                "close_candidate_summary": close_candidate_summary,
                "companion_short_put": companion,
            },
        )

    return _PositionEffectInference(
        deal=None,
        reason="unknown_position_effect",
        diagnostics={
            **base_diagnostics,
            "decision": "not_inferred",
            "close_candidate_summary": close_candidate_summary,
        },
    )


def _with_position_effect_inference_payload(
    raw_payload: dict[str, Any],
    *,
    inferred_effect: str,
    reason: str,
) -> dict[str, Any]:
    payload = dict(raw_payload or {})
    payload.setdefault(
        "position_effect_inference",
        {
            "source": "ledger_context",
            "inferred_position_effect": inferred_effect,
            "reason": reason,
        },
    )
    return payload


def _enrich_combo_yield_open(
    deal: NormalizedTradeDeal,
    *,
    repo: OptionPositionsRepoLike,
) -> _PositionEffectInference:
    if deal.side == "buy" and deal.option_type == "call":
        companion = _combo_yield_companion_short_put(repo, deal)
        return _PositionEffectInference(
            deal=replace(
                deal,
                raw_payload=_with_combo_yield_long_call_payload(
                    deal.raw_payload,
                    deal=deal,
                    companion=companion,
                    inferred_position_effect=False,
                ),
            ),
            reason="combo_yield_long_call",
            diagnostics={
                "decision": "tag_long_call",
                "companion_short_put": companion,
                "strategy_group_id": _stable_combo_yield_group_id(deal),
            },
        )
    if deal.side == "sell" and deal.option_type == "put":
        companion = _combo_yield_companion_long_call(repo, deal)
        if companion is None:
            return _PositionEffectInference(deal=None, reason="not_combo_yield_open")
        return _PositionEffectInference(
            deal=replace(
                deal,
                raw_payload=_with_combo_yield_sell_put_payload(deal.raw_payload, deal=deal, companion=companion),
            ),
            reason="combo_yield_sell_put",
            diagnostics={
                "decision": "tag_sell_put",
                "companion_long_call": companion,
                "strategy_group_id": _stable_combo_yield_group_id(deal),
            },
        )
    return _PositionEffectInference(deal=None, reason="not_combo_yield_open")


def _with_combo_yield_long_call_payload(
    raw_payload: dict[str, Any],
    *,
    deal: NormalizedTradeDeal,
    companion: dict[str, Any] | None,
    inferred_position_effect: bool,
) -> dict[str, Any]:
    payload = dict(raw_payload or {})
    if inferred_position_effect:
        payload = _with_position_effect_inference_payload(
            payload,
            inferred_effect="open",
            reason=(
                "buy_call_with_companion_short_put"
                if companion is not None
                else "buy_call_without_close_target"
            ),
        )
    group_id = _stable_combo_yield_group_id(deal)
    payload.setdefault("strategy", STRATEGY_COMBO_YIELD)
    payload.setdefault("leg_role", "enhancement_call")
    payload.setdefault("yield_enhancement_mode", YIELD_ENHANCEMENT_INCOME_UPSIDE_MODE)
    if group_id:
        payload.setdefault("strategy_group_id", group_id)
    if companion is not None:
        paired_record_id = str(companion.get("record_id") or "").strip()
        if paired_record_id:
            payload.setdefault("paired_short_put_record_id", paired_record_id)
    snapshot = payload.get("strategy_snapshot")
    if not isinstance(snapshot, dict):
        payload["strategy_snapshot"] = {
            "strategy": STRATEGY_COMBO_YIELD,
            "strategy_source": "trade_intake_inference",
            "leg_role": "enhancement_call",
            "yield_enhancement_mode": YIELD_ENHANCEMENT_INCOME_UPSIDE_MODE,
        }
        if group_id:
            payload["strategy_snapshot"]["strategy_group_id"] = group_id
    return payload


def _with_combo_yield_sell_put_payload(
    raw_payload: dict[str, Any],
    *,
    deal: NormalizedTradeDeal,
    companion: dict[str, Any],
) -> dict[str, Any]:
    payload = dict(raw_payload or {})
    group_id = _stable_combo_yield_group_id(deal)
    payload.setdefault("strategy", STRATEGY_COMBO_YIELD)
    payload.setdefault("leg_role", "sell_put")
    payload.setdefault("yield_enhancement_mode", YIELD_ENHANCEMENT_INCOME_UPSIDE_MODE)
    if group_id:
        payload.setdefault("strategy_group_id", group_id)
    paired_record_id = str(companion.get("record_id") or "").strip()
    if paired_record_id:
        payload.setdefault("paired_long_call_record_id", paired_record_id)
    snapshot = payload.get("strategy_snapshot")
    if not isinstance(snapshot, dict):
        payload["strategy_snapshot"] = {
            "strategy": STRATEGY_COMBO_YIELD,
            "strategy_source": "trade_intake_inference",
            "leg_role": "sell_put",
            "yield_enhancement_mode": YIELD_ENHANCEMENT_INCOME_UPSIDE_MODE,
        }
        if group_id:
            payload["strategy_snapshot"]["strategy_group_id"] = group_id
    return payload


def _stable_combo_yield_group_id(deal: NormalizedTradeDeal) -> str:
    account = str(deal.internal_account or "").strip().lower()
    symbol = canonical_symbol(deal.symbol) or str(deal.symbol or "").strip().upper()
    expiration_ymd = str(deal.expiration_ymd or "").strip()
    return f"combo_yield:{account}:{symbol}:{expiration_ymd}"


def _combo_yield_companion_short_put(repo: OptionPositionsRepoLike, deal: NormalizedTradeDeal) -> dict[str, Any] | None:
    return find_unique_open_position_lot(
        repo,
        broker=deal.broker,
        account=deal.internal_account,
        symbol=deal.symbol,
        option_type="put",
        side="short",
        expiration_ymd=deal.expiration_ymd,
    )


def _combo_yield_companion_long_call(repo: OptionPositionsRepoLike, deal: NormalizedTradeDeal) -> dict[str, Any] | None:
    return find_unique_open_position_lot(
        repo,
        broker=deal.broker,
        account=deal.internal_account,
        symbol=deal.symbol,
        option_type="call",
        side="long",
        expiration_ymd=deal.expiration_ymd,
    )


def _close_candidate_summary(repo: OptionPositionsRepoLike, deal: NormalizedTradeDeal) -> dict[str, Any]:
    return summarize_broker_trade_close_candidates(repo, deal=deal)


def _verify_applied_close_projection(*, repo: OptionPositionsRepoLike, operations: list[BrokerTradeOperation]) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    for operation in operations:
        payload = operation.to_payload()
        record_id = str(payload.get("record_id") or "").strip()
        raw_result = payload.get("result")
        result: dict[str, Any] = raw_result if isinstance(raw_result, dict) else {}
        raw_ledger_preflight = payload.get("ledger_preflight")
        ledger_preflight: dict[str, Any] = raw_ledger_preflight if isinstance(raw_ledger_preflight, dict) else {}

        explicit_unmatched = _safe_int(result.get("unmatched_explicit_close_count"))
        heuristic_unmatched = _safe_int(result.get("unmatched_heuristic_close_count"))
        if explicit_unmatched or heuristic_unmatched:
            errors.append(
                {
                    "record_id": record_id or None,
                    "code": "projection_unmatched_close",
                    "unmatched_explicit_close_count": explicit_unmatched,
                    "unmatched_heuristic_close_count": heuristic_unmatched,
                    "projection_diagnostics": result.get("projection_diagnostics") or [],
                }
            )
        projection_errors = [
            dict(item)
            for item in list(result.get("projection_diagnostics") or [])
            if isinstance(item, dict) and str(item.get("severity") or "").strip().lower() == "error"
        ]
        if projection_errors:
            errors.append(
                {
                    "record_id": record_id or None,
                    "code": "projection_error",
                    "projection_diagnostics": projection_errors,
                }
            )

        expected_after = ledger_preflight.get("contracts_open_after")
        has_projection_result = result.get("position_lot_count") is not None or "projection_diagnostic_count" in result
        if record_id and expected_after is not None and has_projection_result:
            try:
                fields = repo.get_record_fields(record_id)
                actual_after = _contracts_open(fields)
            except Exception as exc:
                errors.append(
                    {
                        "record_id": record_id,
                        "code": "target_lot_read_failed",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            check = {
                "record_id": record_id,
                "contracts_open_before": ledger_preflight.get("contracts_open_before"),
                "contracts_to_close": ledger_preflight.get("contracts_to_close"),
                "expected_contracts_open_after": _safe_int(expected_after),
                "actual_contracts_open_after": actual_after,
            }
            checks.append(check)
            if actual_after != _safe_int(expected_after):
                errors.append({"record_id": record_id, "code": "target_lot_contracts_open_mismatch", **check})

    return {"ok": not errors, "checks": checks, "errors": errors}


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _contracts_open(fields: dict[str, Any]) -> int:
    if fields.get("contracts_open") not in (None, ""):
        return _safe_int(fields.get("contracts_open"))
    return _safe_int(fields.get("contracts"))
