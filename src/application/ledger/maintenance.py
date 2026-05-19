from __future__ import annotations

import importlib
import uuid
from typing import Any

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.ledger.position_fields import (
    EXPIRE_AUTO_CLOSE,
    build_expire_auto_close_patch_contract,
    effective_contracts_open,
    effective_expiration,
    effective_expiration_ymd,
    effective_multiplier,
    effective_strike,
    exp_ms_to_datetime,
    exp_ms_to_ymd,
    normalize_account,
    normalize_broker,
    normalize_close_type,
    normalize_currency,
    normalize_status,
    now_ms,
    parse_exp_to_ms,
)
from domain.domain.trade_contract_identity import canonical_contract_symbol
from src.application.ledger.errors import LedgerPreflightError
from src.application.ledger.lot_resolver import LotCloseResolutionError, resolve_explicit_close_target
from src.application.ledger.repository import (
    require_option_positions_event_write_repo,
    require_option_positions_read_repo,
)
from src.application.ledger.results import (
    ExpiredCloseApplyResult,
    ExpiredCloseDecision,
    ExpiredCloseRunResult,
    LedgerWriteResult,
    ProjectionRefreshResult,
)
from src.application.ledger.targets import assert_position_lot_target_matches_current_state
from src.application.ledger.writer import (
    persist_trade_event_object,
    rebuild_position_lots_from_trade_events,
    safe_int_count,
)


def _canonical_trade_symbol(value: Any) -> str:
    return canonical_contract_symbol(value)


def _close_event_trade_time_ms(repo: Any, *, target_source_event_id: str, as_of_ms: int | None) -> int:
    ts = int(as_of_ms or now_ms())
    if not target_source_event_id:
        return ts
    list_trade_events = getattr(repo, "list_trade_events", None)
    if not callable(list_trade_events):
        return ts
    try:
        raw_events = list_trade_events()
        events = raw_events if isinstance(raw_events, list) else []
        for item in events:
            if not isinstance(item, dict):
                continue
            if str(item.get("event_id") or "").strip() != target_source_event_id:
                continue
            source_ts = int(item.get("trade_time_ms") or 0)
            if source_ts >= ts:
                return source_ts + 1
            return ts
    except Exception:
        return ts
    return ts


def persist_expire_auto_close_event(
    repo: Any,
    *,
    record_id: str,
    fields: dict[str, Any],
    contracts_to_close: int,
    close_reason: str,
    as_of_ms: int | None = None,
    exp_source: str | None = None,
    grace_days: int | None = None,
    close_target_resolution: dict[str, Any] | None = None,
) -> LedgerWriteResult:
    broker = normalize_broker(fields.get("broker"))
    if not broker:
        raise ValueError(f"position lot missing broker: {record_id}")
    fields = assert_position_lot_target_matches_current_state(
        repo,
        record_id=record_id,
        fields=fields,
        operation="expire_auto_close",
    )
    multiplier = effective_multiplier(fields)
    strike = effective_strike(fields)
    target_source_event_id = str(fields.get("source_event_id") or "").strip()
    trade_time_ms = _close_event_trade_time_ms(
        repo,
        target_source_event_id=target_source_event_id,
        as_of_ms=as_of_ms,
    )
    event = TradeEvent(
        event_id=f"auto-close-{record_id}-{uuid.uuid4().hex}",
        event_type="expire_close",
        event_time_ms=trade_time_ms,
        contract_key=ContractKey.from_values(
            broker=broker,
            account=normalize_account(fields.get("account")),
            underlying_symbol=_canonical_trade_symbol(fields.get("symbol")),
            option_type=str(fields.get("option_type") or ""),
            position_side=str(fields.get("side") or "").strip().lower(),
            strike=(float(strike) if strike is not None else None),
            expiration_ymd=effective_expiration_ymd(fields),
        ),
        contracts=int(contracts_to_close),
        price=0.0,
        currency=normalize_currency(fields.get("currency")),
        source="auto_close_expired_positions",
        multiplier=(float(multiplier) if multiplier is not None else 100.0),
        target_lot_id=str(record_id),
        raw_payload={
            "source": "om option-positions",
            "source_type": "system_trade_event",
            "mode": EXPIRE_AUTO_CLOSE,
            "record_id": str(record_id),
            "target_lot_id": str(record_id),
            "close_target_source_event_id": target_source_event_id,
            "close_target_account": normalize_account(fields.get("account")),
            "close_target_broker": broker,
            "close_type": EXPIRE_AUTO_CLOSE,
            "close_reason": str(close_reason or "expired"),
            "auto_close_exp_src": str(exp_source or ""),
            "auto_close_grace_days": int(grace_days) if grace_days is not None else None,
            "close_target_resolution": close_target_resolution,
        },
    )
    return persist_trade_event_object(repo, event)


def _auto_close_expiration_anchor(fields: dict[str, Any]) -> tuple[int | None, str, str | None, int | None]:
    exp_ms, exp_source = effective_expiration(fields)
    if exp_ms is None:
        return None, "none", None, None
    exp_ymd = exp_ms_to_ymd(exp_ms)
    normalized_ms = parse_exp_to_ms(exp_ymd) if exp_ymd else None
    if normalized_ms is None:
        normalized_ms = int(exp_ms)
    return int(normalized_ms), exp_source, exp_ymd, int(exp_ms)


def _raise_if_legacy_position_lots_without_trade_events(repo: Any) -> None:
    candidate = require_option_positions_event_write_repo(repo)
    count_trade_events = getattr(candidate, "count_trade_events", None)
    count_position_lots = getattr(candidate, "count_position_lots", None)
    if not callable(count_trade_events) or not callable(count_position_lots):
        return
    if safe_int_count(count_trade_events()) > 0 or safe_int_count(count_position_lots()) <= 0:
        return
    raise ValueError(
        "position_lots exist without trade_events; run explicit "
        "option-positions store migrate-legacy --apply before auto-close"
    )


def build_expired_close_decisions(
    positions: list[dict[str, Any]],
    *,
    as_of_ms: int,
    grace_days: int,
) -> list[ExpiredCloseDecision]:
    decisions: list[ExpiredCloseDecision] = []
    as_of_dt = exp_ms_to_datetime(as_of_ms)
    if as_of_dt is None:
        raise ValueError("invalid as_of_ms")
    cutoff_ms = int((as_of_dt.timestamp() - int(grace_days) * 86400) * 1000)

    for item in positions:
        fields = dict(item)
        record_id = str(fields.get("record_id") or "").strip()
        position_id = str(fields.get("position_id") or "").strip() or "(no position_id)"
        if not record_id:
            decisions.append(
                ExpiredCloseDecision(
                    record_id="",
                    position_id=position_id,
                    expiration_ms=None,
                    effective_exp_source="none",
                    should_close=False,
                    reason="missing record_id",
                    patch=None,
                )
            )
            continue

        if str(fields.get("_auto_close_skip_reason") or "") == "not_current_position_lot":
            decisions.append(
                ExpiredCloseDecision(
                    record_id=record_id,
                    position_id=position_id,
                    expiration_ms=None,
                    effective_exp_source="none",
                    should_close=False,
                    reason="record_id not found in current position_lots",
                    skip_reason="not_current_position_lot",
                    contracts_open=0,
                    patch=None,
                )
            )
            continue

        exp_ms, exp_source, exp_ymd, raw_exp_ms = _auto_close_expiration_anchor(fields)
        contracts_open = effective_contracts_open(fields)
        if normalize_status(fields.get("status")) == "close" or contracts_open <= 0:
            decisions.append(
                ExpiredCloseDecision(
                    record_id=record_id,
                    position_id=position_id,
                    expiration_ms=int(exp_ms) if exp_ms is not None else None,
                    raw_expiration_ms=raw_exp_ms,
                    expiration_ymd=exp_ymd,
                    effective_exp_source=exp_source if exp_ms is not None else "none",
                    should_close=False,
                    reason="already closed or no open contracts",
                    skip_reason="already_closed_or_zero_open",
                    contracts_open=contracts_open,
                    patch=None,
                )
            )
            continue
        if exp_ms is None:
            decisions.append(
                ExpiredCloseDecision(
                    record_id=record_id,
                    position_id=position_id,
                    expiration_ms=None,
                    effective_exp_source="none",
                    should_close=False,
                    reason="missing expiration (field and note)",
                    patch=None,
                )
            )
            continue

        should_close = int(exp_ms) <= cutoff_ms
        patch_contract = (
            build_expire_auto_close_patch_contract(
                fields,
                as_of_ms=as_of_ms,
                close_reason="expired",
                exp_source=exp_source,
                grace_days=grace_days,
            )
            if should_close
            else None
        )
        decisions.append(
            ExpiredCloseDecision(
                record_id=record_id,
                position_id=position_id,
                expiration_ms=int(exp_ms),
                raw_expiration_ms=raw_exp_ms,
                expiration_ymd=exp_ymd,
                effective_exp_source=exp_source,
                should_close=should_close,
                reason=(
                    f"expired: exp={exp_ms_to_ymd(exp_ms) or exp_ms} "
                    f"grace_days={grace_days} as_of_utc={as_of_dt.isoformat()}"
                ),
                patch=patch_contract,
            )
        )
    return decisions


def _refresh_position_lot_projection_from_trade_events(repo: Any) -> ProjectionRefreshResult | None:
    candidate = getattr(repo, "primary_repo", repo)
    count_trade_events = getattr(candidate, "count_trade_events", None)
    if not callable(count_trade_events):
        return None
    if safe_int_count(count_trade_events()) <= 0:
        return None
    return rebuild_position_lots_from_trade_events(candidate)


def _fresh_auto_close_positions(repo: Any, positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    current_lot_source_available = False
    try:
        current_lots = require_option_positions_read_repo(repo).list_position_lots()
        current_lot_source_available = True
    except Exception:
        current_lots = []
    current_by_record_id: dict[str, dict[str, Any]] = {}
    if isinstance(current_lots, list):
        for lot in current_lots:
            if not isinstance(lot, dict):
                continue
            fields = lot.get("fields") if isinstance(lot.get("fields"), dict) else lot
            if not isinstance(fields, dict):
                continue
            record_id = str(lot.get("record_id") or fields.get("record_id") or "").strip()
            if not record_id:
                continue
            row = dict(fields)
            row["record_id"] = record_id
            current_by_record_id[record_id] = row

    get_record_fields = getattr(repo, "get_record_fields", None)
    if not callable(get_record_fields):
        return [dict(item) for item in positions if isinstance(item, dict)]

    out: list[dict[str, Any]] = []
    for item in positions:
        if not isinstance(item, dict):
            continue
        original = dict(item)
        record_id = str(original.get("record_id") or "").strip()
        if not record_id:
            out.append(original)
            continue
        if current_lot_source_available:
            current_lot = current_by_record_id.get(record_id)
            if current_lot is None:
                stale = dict(original)
                stale["_auto_close_skip_reason"] = "not_current_position_lot"
                out.append(stale)
                continue
            if current_lot.get("position_id") in (None, "") and original.get("position_id") not in (None, ""):
                current_lot = dict(current_lot)
                current_lot["position_id"] = original.get("position_id")
            out.append(current_lot)
            continue
        try:
            raw_current = get_record_fields(record_id)
        except Exception:
            out.append(original)
            continue
        if not isinstance(raw_current, dict):
            out.append(original)
            continue
        current = dict(raw_current)
        current["record_id"] = record_id
        if current.get("position_id") in (None, "") and original.get("position_id") not in (None, ""):
            current["position_id"] = original.get("position_id")
        out.append(current)
    return out


def _mark_auto_close_decision_skipped_already_closed(
    decision: ExpiredCloseDecision,
    fields: dict[str, Any],
) -> ExpiredCloseDecision:
    return decision.with_skip(
        reason="already closed or no open contracts",
        skip_reason="already_closed_or_zero_open",
        contracts_open=effective_contracts_open(fields),
    )


def _ledger_preflight_error_payload(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, LedgerPreflightError):
        return {
            "status": "blocked",
            "fail_closed": True,
            "code": exc.code,
            "message": str(exc),
            "details": dict(exc.details),
        }
    if isinstance(exc, LotCloseResolutionError):
        return {
            "status": "blocked",
            "fail_closed": True,
            "code": exc.code,
            "message": str(exc),
            "details": {
                "selector": exc.selector.to_dict(),
                "candidates": [candidate.to_dict() for candidate in exc.candidates],
                "remaining_contracts": exc.remaining_contracts,
            },
        }
    return {
        "status": "blocked",
        "fail_closed": True,
        "code": type(exc).__name__,
        "message": str(exc),
        "details": {},
    }


def _resolve_preflight_expire_auto_close() -> Any:
    return getattr(importlib.import_module("src.application.ledger.preflight"), "preflight_expire_auto_close")


def auto_close_expired_positions(
    repo: Any,
    positions: list[dict[str, Any]],
    *,
    as_of_ms: int,
    grace_days: int,
    max_close: int,
) -> ExpiredCloseRunResult:
    preflight_expire_auto_close = _resolve_preflight_expire_auto_close()
    try:
        _refresh_position_lot_projection_from_trade_events(repo)
    except Exception as exc:
        decisions = build_expired_close_decisions(positions, as_of_ms=as_of_ms, grace_days=grace_days)
        return ExpiredCloseRunResult(
            decisions=decisions,
            applied=[],
            errors=[f"projection refresh failed before auto-close: {exc}"],
        )

    fresh_positions = _fresh_auto_close_positions(repo, positions)
    decisions = build_expired_close_decisions(fresh_positions, as_of_ms=as_of_ms, grace_days=grace_days)
    to_close_indexes = [idx for idx, decision in enumerate(decisions) if decision.should_close and decision.record_id]
    applied: list[ExpiredCloseApplyResult] = []
    errors: list[str] = []
    if len(to_close_indexes) > int(max_close):
        return ExpiredCloseRunResult(
            decisions=decisions,
            applied=applied,
            errors=[f"too many to close: {len(to_close_indexes)} > max_close={max_close}; abort"],
        )
    if to_close_indexes:
        try:
            _raise_if_legacy_position_lots_without_trade_events(repo)
        except Exception as exc:
            return ExpiredCloseRunResult(
                decisions=decisions,
                applied=applied,
                errors=[f"explicit legacy migration required before auto-close: {exc}"],
            )
    for index in to_close_indexes:
        decision = decisions[index]
        try:
            record_id = str(decision.record_id)
            fields = repo.get_record_fields(record_id)
            contracts_to_close = effective_contracts_open(fields)
            if contracts_to_close <= 0:
                decisions[index] = _mark_auto_close_decision_skipped_already_closed(decision, fields)
                continue
            close_target_resolution = resolve_explicit_close_target(
                repo,
                record_id=record_id,
                contracts_to_close=contracts_to_close,
                source="auto_close_expired",
                fields=fields,
            )
            decision = decision.with_close_target_resolution(close_target_resolution.to_dict())
            decisions[index] = decision
            ledger_preflight = preflight_expire_auto_close(
                repo,
                record_id=record_id,
                fields=close_target_resolution.single_candidate.raw_fields,
                contracts_to_close=contracts_to_close,
                as_of_ms=as_of_ms,
                exp_source=str(decision.effective_exp_source or ""),
                grace_days=grace_days,
            )
            decision = decision.with_ledger_preflight(ledger_preflight)
            decisions[index] = decision
            result = persist_expire_auto_close_event(
                repo,
                record_id=record_id,
                fields=close_target_resolution.single_candidate.raw_fields,
                contracts_to_close=contracts_to_close,
                close_reason="expired",
                as_of_ms=int(ledger_preflight["event_time_ms"]),
                exp_source=str(decision.effective_exp_source or ""),
                grace_days=grace_days,
                close_target_resolution=close_target_resolution.to_dict(),
            )
            updated_fields = repo.get_record_fields(record_id)
            if effective_contracts_open(updated_fields) > 0 or normalize_status(updated_fields.get("status")) != "close":
                errors.append(f"{record_id} {decision.position_id}: auto-close event did not close target lot")
                continue
            if normalize_close_type(updated_fields.get("close_type")) != EXPIRE_AUTO_CLOSE:
                errors.append(f"{record_id} {decision.position_id}: auto-close projected wrong close_type")
                continue
            applied.append(ExpiredCloseApplyResult(decision=decision, result=result))
        except Exception as exc:
            if decision.ledger_preflight is None:
                decision = decision.with_ledger_preflight(_ledger_preflight_error_payload(exc))
                decisions[index] = decision
            errors.append(f"{decision.record_id} {decision.position_id}: {exc}")
    return ExpiredCloseRunResult(decisions=decisions, applied=applied, errors=errors)
