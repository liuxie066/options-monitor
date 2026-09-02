from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from domain.domain.ledger import (
    TradeEvent,
    fee_fact_for_event,
    fee_fact_from_persisted_evidence,
)
from domain.domain.option_position_identity import normalize_broker
from domain.domain.performance.models import FeeBasis, FeeComponent, quantize_money, to_decimal
from src.application.ledger.api import (
    enrich_order_fees,
    zero_option_fee_lifecycle_reason,
)
from src.infrastructure.futu_gateway import FutuGatewayRateLimitError


_PROVIDER_CUTOFF_MS = int(
    datetime(2018, 1, 1, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000
)


def fee_target_from_trusted_payload(
    payload: Mapping[str, Any],
) -> tuple[str, str, str, str] | None:
    source = payload.get("_trade_intake_source")
    if not isinstance(source, Mapping) or source.get("schema_version") != "trade_intake_source.v1":
        return None
    order_id = str(
        payload.get("order_id")
        or payload.get("orderID")
        or payload.get("orderId")
        or ""
    ).strip()
    return _identity(
        "富途",
        source.get("account"),
        source.get("futu_account_id"),
        order_id,
    )


def sync_order_fees(
    repo: Any,
    *,
    account: str,
    start_ms: int | None = None,
    end_exclusive_ms: int | None = None,
    provider: Any | None,
    apply: bool,
    observed_at_ms: int,
    futu_account_id: str | None = None,
    allowed_futu_account_ids: Sequence[str] | None = None,
    selection_after: str | None = None,
    max_orders: int | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    target_identity: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Run canonical ledger selection, normalized provider admission, and enrichment."""

    account_value = str(account or "").strip().lower()
    if not account_value:
        raise ValueError("fee sync account is required")
    target = _target_identity(target_identity)
    if target is None:
        start = int(start_ms or 0)
        end = int(end_exclusive_ms or 0)
        if start <= 0 or end <= start:
            raise ValueError("fee sync range is invalid")
    else:
        start = end = None
        if target[1] != account_value:
            raise ValueError("fee sync target account is outside scope")
    candidates, selection_issues = _select_candidates(
        repo,
        account=account_value,
        start_ms=start,
        end_exclusive_ms=end,
        target_identity=target,
    )
    provider_accounts: set[str] | None = None
    if allowed_futu_account_ids is not None:
        provider_accounts = {
            str(value or "").strip()
            for value in allowed_futu_account_ids
            if str(value or "").strip()
        }
        if not provider_accounts:
            raise ValueError("allowed_futu_account_ids must not be empty")
    if futu_account_id not in (None, ""):
        provider_account = str(futu_account_id).strip()
        provider_accounts = (
            {provider_account}
            if provider_accounts is None
            else provider_accounts & {provider_account}
        )
    outside_scope: list[dict[str, Any]] = []
    if provider_accounts is not None:
        outside_scope = [
            item
            for item in candidates
            if str(item.get("futu_account_id") or "") not in provider_accounts
        ]
        candidates = [
            item
            for item in candidates
            if str(item.get("futu_account_id") or "") in provider_accounts
        ]
        selection_issues.extend(
            _issue(item, "provider_account_outside_scope")
            for item in outside_scope
        )
    selected, cursor = (
        (candidates, {"before": None, "after": None, "wrapped": False})
        if target is not None
        else _cursor_select(
            candidates,
            selection_after=selection_after,
            limit=max_orders,
        )
    )
    actual: list[dict[str, Any]] = []
    provider_issues: list[dict[str, Any]] = []
    provider_attempted = False
    fee_call_count = 0
    provider_call_count = 0

    def before_provider_call() -> None:
        nonlocal provider_call_count
        if provider_call_count and provider_call_count % 10 == 0:
            sleep_fn(30.0)
        provider_call_count += 1

    if provider is None:
        provider_issues.extend(
            _issue(item, "provider_unavailable") for item in selected
        )
    else:
        for futu_account_id, account_rows in _by_provider_account(selected).items():
            for batch in _chunks(account_rows, 400):
                provider_attempted = True
                order_ids = [str(item["order_id"]) for item in batch]
                query_start, query_end = _provider_dates(
                    int(batch[0]["oldest_event_time_ms"] if target is not None else start),
                    int(batch[0]["newest_event_time_ms"] + 1 if target is not None else end),
                )
                if target is None:
                    before_provider_call()
                else:
                    provider_call_count += 1
                try:
                    terminal_rows, terminal_diagnostics = provider.fetch_terminal_orders(
                        futu_account_id=futu_account_id,
                        order_ids=order_ids,
                        start=query_start,
                        end=query_end,
                        **({"exact": True} if target is not None else {}),
                    )
                except Exception as exc:
                    provider_issues.extend(
                        _issue(
                            item,
                            _provider_failure_reason("provider_order_query_failed", exc),
                            error=exc,
                        )
                        for item in batch
                    )
                    continue
                terminal_map = dict(terminal_rows or {})
                admitted: list[dict[str, Any]] = []
                for item in batch:
                    order_id = str(item["order_id"])
                    terminal = terminal_map.get(order_id)
                    reason = _admission_problem(item, terminal)
                    if reason is not None:
                        provider_issues.append(_issue(item, reason))
                    else:
                        admitted.append({**item, "provider_currency": terminal["currency"]})
                if not admitted:
                    continue
                fee_call_count += 1
                admitted_ids = [str(item["order_id"]) for item in admitted]
                if target is None:
                    before_provider_call()
                else:
                    provider_call_count += 1
                try:
                    fee_rows, fee_diagnostics = provider.fetch_order_fees(
                        futu_account_id=futu_account_id,
                        order_ids=admitted_ids,
                    )
                except Exception as exc:
                    provider_issues.extend(
                        _issue(
                            item,
                            _provider_failure_reason("provider_fee_query_failed", exc),
                            error=exc,
                        )
                        for item in admitted
                    )
                    continue
                fee_map = dict(fee_rows or {})
                batch_id = _provider_batch_id(
                    account_value,
                    futu_account_id,
                    admitted_ids,
                    observed_at_ms,
                )
                for item in admitted:
                    fee = fee_map.get(str(item["order_id"]))
                    if not isinstance(fee, Mapping):
                        provider_issues.append(
                            _issue(
                                item,
                                "fee_pending" if target is not None else "order_fee_missing",
                            )
                        )
                        continue
                    try:
                        amount = quantize_money(
                            to_decimal(fee.get("fee_amount"), field_name="fee_amount")
                        )
                    except (TypeError, ValueError):
                        provider_issues.append(_issue(item, "order_fee_invalid"))
                        continue
                    if amount < 0:
                        provider_issues.append(_issue(item, "order_fee_invalid"))
                        continue
                    actual.append(
                        {
                            "broker": item["broker"],
                            "account": item["account"],
                            "futu_account_id": item["futu_account_id"],
                            "order_id": item["order_id"],
                            "fee_amount": canonical_decimal(amount),
                            "currency": item["provider_currency"],
                            "event_kind": item["event_kind"],
                            "dealt_quantity": canonical_decimal(
                                Decimal(int(item["quantity"]))
                            ),
                            "observed_at_ms": int(observed_at_ms),
                            "provider_batch_id": batch_id,
                            "fee_details_sha256": _details_sha256(
                                fee.get("fee_details")
                            ),
                        }
                    )

    migration = enrich_order_fees(
        repo,
        account=account_value,
        start_ms=start,
        end_exclusive_ms=end,
        actual_fees=actual,
        apply=apply,
        applied_at_ms=int(observed_at_ms),
        target_identity=target,
    )
    issues = _dedupe_issues(
        [*selection_issues, *provider_issues, *migration.get("unresolved", [])]
    )
    reason_counts: dict[str, int] = {}
    for item in issues:
        reason = str(item.get("reason") or "unknown")
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    return {
        "schema_version": "order_fee_sync_receipt.v1",
        "account": account_value,
        "futu_account_id": str(futu_account_id or "").strip() or None,
        "target_identity_sha256": _identity_hash(target) if target is not None else None,
        "start_ms": start,
        "end_exclusive_ms": end,
        "applied": bool(apply),
        "candidate_order_count": len(candidates),
        "outside_scope_order_count": len(outside_scope),
        "selected_order_count": len(selected),
        "actual_observation_count": len(actual),
        "provider_attempted": provider_attempted,
        "provider_call_count": provider_call_count,
        "provider_fee_call_count": fee_call_count,
        "selection_cursor": cursor,
        "reason_counts": reason_counts,
        "issues": issues,
        "migration": migration,
    }


def _select_candidates(
    repo: Any,
    *,
    account: str,
    start_ms: int | None,
    end_exclusive_ms: int | None,
    target_identity: tuple[str, str, str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidate = getattr(repo, "primary_repo", repo)
    trade_rows = list(candidate.list_trade_events())
    stock_rows = list(candidate.list_assigned_stock_events())
    events: list[TradeEvent] = []
    issues: list[dict[str, Any]] = []
    for row in trade_rows:
        try:
            events.append(TradeEvent.from_dict(row))
        except (TypeError, ValueError):
            continue
    voided = {
        str(event.target_event_id)
        for event in events
        if event.event_type == "void" and event.target_event_id
    }
    grouped: dict[tuple[str, str, str, str], list[tuple[str, Any]]] = {}
    for event in events:
        if (
            event.event_id in voided
            or event.contract_key.account != account
            or event.event_type
            not in {"open", "close", "expire_close", "assignment", "exercise"}
        ):
            continue
        if zero_option_fee_lifecycle_reason(event):
            continue
        if normalize_broker(event.contract_key.broker) != "富途":
            if target_identity is None and _in_range(
                event.event_time_ms, int(start_ms or 0), int(end_exclusive_ms or 0)
            ):
                issues.append(
                    {
                        "event_kind": "option_trade",
                        "event_id": event.event_id,
                        "reason": "unsupported_broker_fee_schedule",
                    }
                )
            continue
        raw = event.raw_payload or {}
        identity = _identity(
            event.contract_key.broker,
            event.contract_key.account,
            raw.get("futu_account_id"),
            raw.get("order_id"),
        )
        if identity is None:
            if target_identity is None and _in_range(
                event.event_time_ms, int(start_ms or 0), int(end_exclusive_ms or 0)
            ):
                issues.append(
                    {
                        "event_kind": "option_trade",
                        "event_id": event.event_id,
                        "reason": "order_identity_missing",
                    }
                )
            continue
        grouped.setdefault(identity, []).append(("option_trade", event))
    for raw in stock_rows:
        row = dict(raw)
        if (
            str(row.get("account") or "").strip().lower() != account
            or str(row.get("event_type") or "").strip().lower() != "sale"
        ):
            continue
        if normalize_broker(row.get("broker")) != "富途":
            instant = int(row.get("trade_time_ms") or 0)
            if target_identity is None and _in_range(
                instant, int(start_ms or 0), int(end_exclusive_ms or 0)
            ):
                issues.append(
                    {
                        "event_kind": "assigned_stock_sale",
                        "event_id": _row_id(row),
                        "reason": "unsupported_broker_fee_schedule",
                    }
                )
            continue
        identity = _identity(
            row.get("broker"),
            row.get("account"),
            row.get("futu_account_id"),
            row.get("order_id"),
        )
        instant = int(row.get("trade_time_ms") or 0)
        if identity is None:
            if target_identity is None and _in_range(
                instant, int(start_ms or 0), int(end_exclusive_ms or 0)
            ):
                issues.append(
                    {
                        "event_kind": "assigned_stock_sale",
                        "event_id": _row_id(row),
                        "reason": "order_identity_missing",
                    }
                )
            continue
        grouped.setdefault(identity, []).append(("assigned_stock_sale", row))

    candidates: list[dict[str, Any]] = []
    target_seen = False
    for identity, typed_rows in grouped.items():
        times = [_time_ms(value) for _kind, value in typed_rows]
        if target_identity is not None:
            if identity != target_identity:
                continue
            target_seen = True
        elif not any(
            _in_range(value, int(start_ms or 0), int(end_exclusive_ms or 0))
            for value in times
        ):
            continue
        base = {
            "broker": identity[0],
            "account": identity[1],
            "futu_account_id": identity[2],
            "order_id": identity[3],
            "identity_sha256": _identity_hash(identity),
            "oldest_event_time_ms": min(times),
            "newest_event_time_ms": max(times),
            "row_count": len(typed_rows),
        }
        if target_identity is None and (
            min(times) < int(start_ms or 0)
            or max(times) >= int(end_exclusive_ms or 0)
        ):
            issues.append(
                {
                    **_redacted(base),
                    "reason": "order_group_outside_requested_range",
                    "group_min_event_time_ms": min(times),
                    "group_max_event_time_ms": max(times),
                }
            )
            continue
        kinds = {kind for kind, _row in typed_rows}
        if len(kinds) != 1:
            issues.append({**_redacted(base), "reason": "order_identity_cross_type_conflict"})
            continue
        kind = next(iter(kinds))
        if kind == "option_trade":
            option_rows = [value for _kind, value in typed_rows]
            if len({_contract(value) for value in option_rows}) != 1:
                issues.append({**_redacted(base), "reason": "combo_fee_allocation_unproven"})
                continue
            facts = [fee_fact_for_event(value) for value in option_rows]
            quantity = sum(int(value.contracts) for value in option_rows)
            currencies = {value.currency for value in option_rows}
        else:
            stock = [value for _kind, value in typed_rows]
            if len(stock) != 1:
                issues.append({**_redacted(base), "reason": "stock_sale_order_group_unsupported"})
                continue
            facts = [
                fee_fact_from_persisted_evidence(
                    event_id=_row_id(stock[0]),
                    component=FeeComponent.STOCK_SALE,
                    provenance=stock[0].get("fee_provenance"),
                    compatibility_amount=stock[0].get("fees") or 0,
                )
            ]
            quantity = int(stock[0].get("shares") or 0)
            currencies = {str(stock[0].get("currency") or "").strip().upper()}
        if all(fact.basis == FeeBasis.ACTUAL for fact in facts):
            if target_identity is not None:
                issues.append(
                    _issue({**base, "event_kind": kind}, "already_actual")
                )
            continue
        if min(times) < _PROVIDER_CUTOFF_MS:
            issues.append({**_redacted(base), "reason": "provider_date_unsupported"})
            continue
        if quantity <= 0:
            issues.append({**_redacted(base), "reason": "ledger_quantity_invalid"})
            continue
        if len(currencies) != 1 or next(iter(currencies)) not in {"CNY", "HKD", "USD"}:
            issues.append({**_redacted(base), "reason": "order_currency_mismatch"})
            continue
        candidates.append(
            {
                **base,
                "event_kind": kind,
                "quantity": quantity,
                "currency": next(iter(currencies)),
                "sort_key": f"{min(times):020d}:{_identity_hash(identity)}",
            }
        )
    candidates.sort(key=lambda item: str(item["sort_key"]))
    if target_identity is not None and not target_seen:
        issues.append(
            {
                "event_kind": "order_group",
                "identity_sha256": _identity_hash(target_identity),
                "reason": "target_order_group_missing",
            }
        )
    return candidates, issues


def _admission_problem(item: Mapping[str, Any], terminal: Any) -> str | None:
    if not isinstance(terminal, Mapping):
        return "terminal_order_missing"
    status = str(terminal.get("status") or "").strip()
    if status == "retryable":
        return "terminal_pending"
    if status == "terminal_no_fill":
        return "terminal_no_fill_conflict"
    if status != "terminal_with_fill":
        return "order_status_unknown"
    provider_currency = str(terminal.get("currency") or "").strip().upper()
    if provider_currency not in {"CNY", "HKD", "USD"}:
        return "order_currency_missing"
    if provider_currency != str(item.get("currency") or ""):
        return "order_currency_mismatch"
    try:
        dealt_qty = to_decimal(terminal.get("dealt_qty"), field_name="dealt_qty")
    except (TypeError, ValueError):
        return "order_quantity_invalid"
    if dealt_qty <= 0:
        return "terminal_fill_quantity_missing"
    if dealt_qty != Decimal(int(item.get("quantity") or 0)):
        return (
            "stock_sale_quantity_mismatch"
            if item.get("event_kind") == "assigned_stock_sale"
            else "option_order_quantity_mismatch"
        )
    return None


def _cursor_select(
    candidates: Sequence[dict[str, Any]],
    *,
    selection_after: str | None,
    limit: int | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if limit is not None and int(limit) <= 0:
        raise ValueError("max_orders must be positive")
    rows = list(candidates)
    if not rows:
        return [], {
            "before": selection_after,
            "after": selection_after,
            "wrapped": False,
        }
    boundary = str(selection_after or "")
    after_rows = [item for item in rows if str(item["sort_key"]) > boundary]
    before_rows = [item for item in rows if str(item["sort_key"]) <= boundary]
    ordered = [*after_rows, *before_rows]
    selected = ordered[: int(limit)] if limit is not None else ordered
    wrapped = bool(boundary and len(selected) > len(after_rows))
    return selected, {
        "before": selection_after,
        "after": str(selected[-1]["sort_key"]) if selected else selection_after,
        "wrapped": wrapped,
    }


def _by_provider_account(
    candidates: Sequence[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for item in candidates:
        out.setdefault(str(item["futu_account_id"]), []).append(item)
    return out


def _chunks(values: Sequence[Any], size: int) -> list[list[Any]]:
    return [list(values[index : index + size]) for index in range(0, len(values), size)]


def _provider_dates(start_ms: int, end_exclusive_ms: int) -> tuple[str, str]:
    tz = ZoneInfo("Asia/Hong_Kong")
    start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).astimezone(tz)
    end = datetime.fromtimestamp((end_exclusive_ms - 1) / 1000, tz=timezone.utc).astimezone(tz)
    return start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")


def _identity(broker: Any, account: Any, futu_account_id: Any, order_id: Any) -> tuple[str, str, str, str] | None:
    values = (
        normalize_broker(broker),
        str(account or "").strip(),
        str(futu_account_id or "").strip(),
        str(order_id or "").strip(),
    )
    return (values[0], values[1].lower(), values[2], values[3]) if all(values) else None


def _target_identity(value: Sequence[str] | None) -> tuple[str, str, str, str] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or len(value) != 4:
        raise ValueError("fee sync target identity must contain four fields")
    target = _identity(*value)
    if target is None:
        raise ValueError("fee sync target identity is incomplete")
    return target


def _identity_hash(identity: Sequence[str]) -> str:
    return hashlib.sha256(chr(31).join(identity).encode()).hexdigest()


def _contract(event: TradeEvent) -> tuple[Any, ...]:
    key = event.contract_key
    return (
        key.broker,
        key.account,
        key.underlying_symbol,
        key.option_type,
        key.position_side,
        key.strike,
        key.expiration_ymd,
        event.currency,
        event.multiplier,
    )


def _time_ms(value: Any) -> int:
    return value.event_time_ms if isinstance(value, TradeEvent) else int(value.get("trade_time_ms") or 0)


def _row_id(value: Mapping[str, Any]) -> str:
    return str(value.get("stock_event_id") or value.get("event_id") or "").strip()


def _in_range(value: int, start_ms: int, end_exclusive_ms: int) -> bool:
    return start_ms <= int(value) < end_exclusive_ms


def _redacted(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_kind": value.get("event_kind") or "order_group",
        "identity_sha256": value["identity_sha256"],
        "row_count": value["row_count"],
    }


def _issue(item: Mapping[str, Any], reason: str, *, error: Exception | None = None) -> dict[str, Any]:
    out = {
        "event_kind": item.get("event_kind") or "order_group",
        "identity_sha256": item["identity_sha256"],
        "reason": reason,
    }
    if error is not None:
        out["error_type"] = type(error).__name__
    return out


def _provider_failure_reason(default: str, error: Exception) -> str:
    return "provider_rate_limited" if isinstance(error, FutuGatewayRateLimitError) else default


def _dedupe_issues(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        row = dict(item)
        key = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


def _provider_batch_id(account: str, futu_account_id: str, order_ids: Sequence[str], observed_at_ms: int) -> str:
    digest = hashlib.sha256(
        json.dumps(
            [account, futu_account_id, sorted(order_ids), int(observed_at_ms)],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()[:20]
    return f"opendfee_{digest}"


def _details_sha256(value: Any) -> str | None:
    if value in (None, "", [], {}):
        return None
    try:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        payload = str(value)
    return hashlib.sha256(payload.encode()).hexdigest()


def canonical_decimal(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000001")), "f")


__all__ = ["fee_target_from_trusted_payload", "sync_order_fees"]
