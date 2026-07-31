#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

import argparse
from datetime import datetime, timezone
from typing import Any, Mapping

from domain.domain.expiration_dates import (
    EXPIRATION_DATE_TZ,
)
from domain.domain.combo_yield_lifecycle import build_option_group_inventory
from domain.domain.lifecycle_allocation import resolve_allocations
from domain.domain.ledger.position_fields import (
    normalize_account,
    normalize_broker,
)
from domain.domain.option_lifecycle import derive_lifecycle_read_model
from domain.domain.option_position_identity import normalize_currency
from domain.domain.symbol_identity import symbol_market
from domain.domain.symbol_identity import canonical_symbol
from domain.domain.risk_capacity import (
    compute_short_call_locked_shares,
    compute_short_put_cash_secured,
)
from src.infrastructure.io_utils import atomic_write_json
from src.application.ledger.api import (
    RiskPositionView,
    lifecycle_evidence_facts,
    position_lot_risk_view,
    position_lot_snapshot,
    resolve_position_lot_snapshots,
    summarize_position_lot_shadow_status,
    validate_account_lifecycle_resolution,
)
from src.application.trades.lifecycle_reconciliation import (
    build_lifecycle_read_models_from_resolved_account,
)

from src.infrastructure.exchange_rates import get_exchange_rates_or_fetch_latest

JsonDict = dict[str, Any]


def _empty_context(
    *,
    broker_norm: str,
    account: str | None,
    account_norm: str | None,
    rates: JsonDict | None,
    raw_selected_count: int,
    ledger_status: JsonDict | None = None,
) -> JsonDict:
    out = {
        "context_status": "unavailable",
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "filters": {"broker": broker_norm, "account": account_norm or account},
        "locked_shares_status": "unavailable",
        "locked_shares_unavailable_reason": "option_position_ledger_unavailable",
        "locked_shares_by_symbol": {},
        "locked_shares_unavailable_by_symbol": {},
        "cash_secured_by_symbol_by_ccy": {},
        "cash_secured_total_by_ccy": {},
        "cash_secured_unavailable_by_symbol": {},
        "cash_secured_total_cny": 0.0,
        "exchange_rates": (rates or {}),
        "raw_selected_count": raw_selected_count,
        "open_positions_min": [],
        "combo_yield_groups": [],
        "position_lifecycle_by_lot": {},
        "assigned_stock_events": [],
        "strategy_group_identities": [],
        "decision_state_fingerprint": None,
        "decision_snapshot_status": "snapshot_unavailable",
    }
    if ledger_status is not None:
        out["ledger"] = ledger_status
    return out


def _position_records_from_views(items: list[RiskPositionView]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        record = item.as_shadow_record()
        if record is not None:
            rows.append(record)
    return rows


def _rate_value(rates_map: JsonDict, key: str) -> float | None:
    raw = rates_map.get(key)
    try:
        return float(raw) if raw else None
    except Exception:
        return None


def build_context(
    records: list[JsonDict],
    broker: str,
    account: str | None = None,
    rates: JsonDict | None = None,
    decision_snapshot: JsonDict | None = None,
    lifecycle_now_ms: int | None = None,
) -> JsonDict:
    """Build risk context from projected position-lot records.

    Important: keep record_id for downstream actions (auto-close expired positions)
    without adding extra list calls.
    """

    broker_norm = normalize_broker(broker)
    account_norm = normalize_account(account) if account else None
    selected_items: list[RiskPositionView] = []
    for rec in records:
        view = position_lot_risk_view(rec)
        if not view.fields:
            continue
        if broker_norm and view.broker != broker_norm:
            continue
        if account_norm and view.account != account_norm:
            continue
        selected_items.append(view)

    ledger_status = summarize_position_lot_shadow_status(_position_records_from_views(selected_items))
    if ledger_status.get("fail_closed"):
        return _empty_context(
            broker_norm=broker_norm,
            account=account,
            account_norm=account_norm,
            rates=rates,
            raw_selected_count=len(selected_items),
            ledger_status=ledger_status,
        )

    # Aggregate open short positions for constraints
    locked_shares_by_symbol: dict[str, int] = {}
    locked_shares_unavailable_by_symbol: dict[str, str] = {}

    # cash_secured_amount is stored on projected position lots with an explicit currency field (USD/CNY/HKD).
    # We aggregate:
    # - by_symbol: in original currency buckets
    # - total_base_cny: unified base currency (CNY) using exchange rates when available
    cash_secured_by_symbol_by_ccy: dict[str, dict[str, float]] = {}
    cash_secured_total_by_ccy: dict[str, float] = {}
    cash_secured_unavailable_by_symbol: dict[str, str] = {}

    cash_secured_total_cny: float | None = 0.0

    usdcny_exchange_rate = None
    cny_per_hkd_exchange_rate = None
    if rates:
        # rates may be either the full cache object {rates:{...}, timestamp, cached_at} or already the dict of rates
        nested_rates = rates.get("rates")
        rates_map = nested_rates if isinstance(nested_rates, dict) else rates
        usdcny_exchange_rate = _rate_value(rates_map, "USDCNY")
        cny_per_hkd_exchange_rate = _rate_value(rates_map, "HKDCNY")

    # Minimal open positions list for downstream (auto-close), keeps record_id.
    open_positions_min: list[JsonDict] = []
    as_of_date = datetime.now(EXPIRATION_DATE_TZ).date()
    lifecycle_by_lot = build_lifecycle_read_models_from_decision_snapshot(
        decision_snapshot,
        now_ms=lifecycle_now_ms,
    )

    for it in selected_items:
        if not it.is_open:
            continue
        contracts_total = int(it.contracts or 0)
        contracts_open = int(it.contracts_open or 0)
        if contracts_open <= 0:
            continue

        symbol = it.canonical_underlying_symbol

        position_row = it.as_open_position_min(as_of_date=as_of_date)
        lifecycle = lifecycle_by_lot.get(it.record_id)
        if lifecycle is not None:
            position_row.update(lifecycle)
        open_positions_min.append(position_row)
        if not symbol:
            continue

        option_type = it.option_type
        side = it.side
        currency = normalize_currency(it.currency)

        if side == "short" and option_type == "call":
            locked = compute_short_call_locked_shares(
                contracts_open=contracts_open,
                contracts_total=contracts_total,
                multiplier=it.multiplier,
                underlying_share_locked=it.underlying_share_locked,
            )
            if locked is None:
                locked_shares_unavailable_by_symbol[symbol] = "short_call_locked_shares_basis_missing"
                continue
            locked_shares_by_symbol[symbol] = locked_shares_by_symbol.get(symbol, 0) + int(locked)

        if side == "short" and option_type == "put":
            cash_secured = compute_short_put_cash_secured(
                contracts_open=contracts_open,
                contracts_total=contracts_total,
                cash_secured_amount=it.cash_secured_amount,
                strike=it.strike,
                multiplier=it.multiplier,
            )
            if cash_secured is None:
                cash_secured_unavailable_by_symbol[symbol] = "short_put_cash_secured_basis_missing"
                cash_secured_total_cny = None
                continue
            if not currency:
                cash_secured_unavailable_by_symbol[symbol] = "short_put_cash_secured_currency_missing"
                cash_secured_total_cny = None
                continue
            if currency not in {"CNY", "USD", "HKD"}:
                cash_secured_unavailable_by_symbol[symbol] = f"short_put_cash_secured_currency_unsupported:{currency}"
                cash_secured_total_cny = None
                continue

            # bucket per symbol per currency
            m = cash_secured_by_symbol_by_ccy.get(symbol) or {}
            m[currency] = m.get(currency, 0.0) + float(cash_secured)
            cash_secured_by_symbol_by_ccy[symbol] = m

            cash_secured_total_by_ccy[currency] = cash_secured_total_by_ccy.get(currency, 0.0) + float(cash_secured)

            # unify to CNY if possible
            if cash_secured_total_cny is not None:
                if currency == 'CNY':
                    cash_secured_total_cny += float(cash_secured)
                elif currency == 'USD':
                    if usdcny_exchange_rate:
                        cash_secured_total_cny += float(cash_secured) * float(usdcny_exchange_rate)
                    else:
                        cash_secured_total_cny = None
                elif currency == 'HKD':
                    if cny_per_hkd_exchange_rate:
                        cash_secured_total_cny += float(cash_secured) * float(cny_per_hkd_exchange_rate)
                    else:
                        cash_secured_total_cny = None
                else:
                    cash_secured_total_cny = None

    out = {
        "context_status": "available",
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "filters": {"broker": broker_norm, "account": account_norm or account},
        "locked_shares_status": "available",
        "locked_shares_unavailable_reason": None,
        "locked_shares_by_symbol": locked_shares_by_symbol,
        "locked_shares_unavailable_by_symbol": locked_shares_unavailable_by_symbol,
        "cash_secured_by_symbol_by_ccy": cash_secured_by_symbol_by_ccy,
        "cash_secured_total_by_ccy": cash_secured_total_by_ccy,
        "cash_secured_unavailable_by_symbol": cash_secured_unavailable_by_symbol,
        "cash_secured_total_cny": cash_secured_total_cny,
        "exchange_rates": (rates or {}),
        "raw_selected_count": len(selected_items),
        "open_positions_min": open_positions_min,
        "combo_yield_groups": build_option_group_inventory(
            [
                {
                    "record_id": item.record_id,
                    "account": item.account,
                    "symbol": item.canonical_underlying_symbol,
                    "option_type": item.option_type,
                    "side": item.side,
                    "contracts": item.contracts,
                    "contracts_open": item.contracts_open,
                    "contracts_closed": item.contracts_closed,
                    "expiration_ymd": item.expiration_ymd,
                    "strategy": item.fields.get("strategy"),
                    "leg_role": item.fields.get("leg_role"),
                    "strategy_group_id": item.fields.get("strategy_group_id"),
                    "yield_enhancement_mode": item.fields.get("yield_enhancement_mode"),
                    "strategy_snapshot": item.fields.get("strategy_snapshot"),
                    **dict(lifecycle_by_lot.get(item.record_id) or {}),
                }
                for item in selected_items
            ]
        ),
        "position_lifecycle_by_lot": lifecycle_by_lot,
        "assigned_stock_events": [
            dict(item)
            for item in list(
                (decision_snapshot or {}).get("account_assigned_stock_events")
                or []
            )
            if isinstance(item, dict)
        ],
        "strategy_group_identities": [
            dict(item)
            for item in list(
                (decision_snapshot or {}).get("account_combo_identities") or []
            )
            if isinstance(item, dict)
        ],
        "decision_state_fingerprint": (
            (decision_snapshot or {}).get("decision_state_fingerprint")
        ),
        "decision_snapshot_status": str(
            (decision_snapshot or {}).get("snapshot_status")
            or "snapshot_unavailable"
        ),
    }
    out["ledger"] = ledger_status
    return out


def build_shared_context(
    records: list[JsonDict],
    broker: str,
    rates: JsonDict | None = None,
    *,
    decision_snapshots_by_account: dict[str, JsonDict] | None = None,
    lifecycle_now_ms: int | None = None,
) -> JsonDict:
    broker_norm = normalize_broker(broker)
    accounts: set[str] = set()
    for rec in records:
        fields = position_lot_snapshot(rec).fields
        if not fields:
            continue
        if broker_norm and fields.get("broker") != broker_norm:
            continue
        acct = fields.get("account")
        if acct:
            accounts.add(acct)
    snapshots = decision_snapshots_by_account or {}
    by_account = {
        acct: build_context(
            records,
            broker=broker_norm,
            account=acct,
            rates=rates,
            decision_snapshot=snapshots.get(acct),
            lifecycle_now_ms=lifecycle_now_ms,
        )
        for acct in sorted(accounts)
    }
    return {
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "filters": {"broker": broker_norm},
        "all_accounts": build_context(records, broker=broker_norm, account=None, rates=rates),
        "by_account": by_account,
    }


def build_lifecycle_read_models_from_decision_snapshot(
    decision_snapshot: JsonDict | None,
    *,
    now_ms: int | None = None,
) -> dict[str, JsonDict]:
    snapshot = dict(decision_snapshot or {})
    if str(snapshot.get("snapshot_status") or "") != "trusted":
        return {}
    resolved = snapshot.get("account_lifecycle_resolution")
    if isinstance(resolved, Mapping):
        validation_reasons = validate_account_lifecycle_resolution(
            resolved
        )
        if validation_reasons:
            raise ValueError(
                "invalid account lifecycle resolution: "
                + ",".join(validation_reasons)
            )
        return build_lifecycle_read_models_from_resolved_account(
            cases=[
                dict(item)
                for item in snapshot.get("account_lifecycle_cases") or []
                if isinstance(item, dict)
            ],
            allocations=[
                dict(item)
                for item in snapshot.get("account_lifecycle_allocations")
                or []
                if isinstance(item, dict)
            ],
            timing_policies=[
                dict(item)
                for item in snapshot.get(
                    "account_lifecycle_timing_policies"
                )
                or []
                if isinstance(item, dict)
            ],
            position_lots=[
                dict(item)
                for item in snapshot.get("account_position_lots") or []
                if isinstance(item, dict)
            ],
            account_resolution=resolved,
            void_event_ids=list(
                snapshot.get("effective_void_event_ids") or []
            ),
            now_ms=now_ms,
        )
    cases = [
        dict(item)
        for item in list(snapshot.get("account_lifecycle_cases") or [])
        if isinstance(item, dict)
        and str(item.get("schema_version") or "").strip() == "lifecycle_case.v2"
    ]
    evidence = [
        dict(item)
        for item in list(snapshot.get("account_lifecycle_evidence") or [])
        if isinstance(item, dict)
    ]
    allocations = [
        dict(item)
        for item in list(snapshot.get("account_lifecycle_allocations") or [])
        if isinstance(item, dict)
    ]
    void_event_ids = tuple(snapshot.get("effective_void_event_ids") or ())
    lots_by_id = {
        str(item.get("record_id") or "").strip(): dict(item.get("fields") or {})
        for item in list(snapshot.get("account_position_lots") or [])
        if isinstance(item, dict) and str(item.get("record_id") or "").strip()
    }
    evidence_by_case: dict[str, list[JsonDict]] = {}
    for item in evidence:
        case_id = str(item.get("case_id") or "").strip()
        if case_id:
            evidence_by_case.setdefault(case_id, []).append(item)
    allocations_by_case: dict[str, list[JsonDict]] = {}
    for item in allocations:
        case_id = str(item.get("case_id") or "").strip()
        if case_id:
            allocations_by_case.setdefault(case_id, []).append(item)

    output: dict[str, JsonDict] = {}
    for lifecycle_case in sorted(cases, key=lambda item: str(item.get("case_id") or "")):
        case_id = str(lifecycle_case.get("case_id") or "").strip()
        case_allocations = allocations_by_case.get(case_id, [])
        case_evidence = evidence_by_case.get(case_id, [])
        evidence_facts = lifecycle_evidence_facts(
            evidence=case_evidence,
            allocations=case_allocations,
            void_event_ids=void_event_ids,
        )
        resolution = resolve_allocations(
            dict(lifecycle_case.get("target_contracts_by_lot") or {}),
            case_allocations,
            void_event_ids=void_event_ids,
        )
        orphan_evidence_ids = list(evidence_facts.orphan_evidence_ids)
        quantity_drift = any(
            lot_id not in lots_by_id
            or int(lots_by_id[lot_id].get("contracts_open") or 0)
            != expected_remaining
            for lot_id, expected_remaining in resolution.remaining_contracts_by_lot.items()
        )
        persisted_status = str(lifecycle_case.get("status") or "").strip().lower()
        summary = dict(lifecycle_case.get("derived_summary") or {})
        conflict_reasons = (
            tuple(
                str(item)
                for item in summary.get("lifecycle_reason_codes") or []
                if str(item)
            )
            if persisted_status == "conflict"
            else ()
        )
        read_model = derive_lifecycle_read_model(
            expiration_ymd=str(lifecycle_case.get("expiration_ymd") or ""),
            market=str(
                lifecycle_case.get("market")
                or symbol_market(lifecycle_case.get("symbol"))
                or ""
            ),
            target_contracts_by_lot=dict(
                lifecycle_case.get("target_contracts_by_lot") or {}
            ),
            allocations=case_allocations,
            void_event_ids=void_event_ids,
            accepted_option_close_contracts_by_lot=(
                evidence_facts.reservation_contracts_by_lot
            ),
            now_ms=now_ms,
            conflict_reason_codes=conflict_reasons,
            orphan_evidence=bool(orphan_evidence_ids),
            quantity_drift=quantity_drift,
        )
        model = {
            "lifecycle_state": read_model.lifecycle_state,
            "lifecycle_case_id": case_id,
            "lifecycle_evidence_status": (
                "conflict"
                if read_model.lifecycle_state == "conflict"
                else "evidence_without_allocation"
                if orphan_evidence_ids
                else "missing"
                if not case_evidence
                else "closure_observed_cause_pending"
                if evidence_facts.reservation_evidence_ids
                else "partial"
                if any(read_model.remaining_contracts_by_lot.values())
                else "complete"
            ),
            "lifecycle_reason_codes": list(read_model.lifecycle_reason_codes),
            "pending_until_ms": read_model.pending_until_ms,
            "terminal_event_ids": sorted(
                str(item.get("canonical_terminal_event_id") or "").strip()
                for item in evidence_facts.effective_allocations
                if str(item.get("canonical_terminal_event_id") or "").strip()
            ),
            "target_contracts_by_lot": dict(
                lifecycle_case.get("target_contracts_by_lot") or {}
            ),
            "resolved_contracts_by_lot": read_model.resolved_contracts_by_lot,
            "remaining_contracts_by_lot": read_model.remaining_contracts_by_lot,
            "resolved_contracts_by_terminal_type": (
                read_model.resolved_contracts_by_terminal_type
            ),
            "reserved_contracts_by_lot": read_model.reserved_contracts_by_lot,
            "closure_fact": read_model.closure_fact,
            "reason_state": read_model.reason_state,
            "close_reason": read_model.close_reason,
            "allocation_ids": sorted(
                str(item.get("allocation_id") or "").strip()
                for item in evidence_facts.effective_allocations
                if str(item.get("allocation_id") or "").strip()
            ),
            "voided_terminal_event_ids": sorted(
                {
                    str(item.get("canonical_terminal_event_id") or "").strip()
                    for item in case_allocations
                    if str(
                        item.get("canonical_terminal_event_id") or ""
                    ).strip()
                    in set(void_event_ids)
                }
            ),
            "reservation_evidence_ids": list(
                evidence_facts.reservation_evidence_ids
            ),
            "actionable": False,
        }
        for lot_id in sorted(
            dict(lifecycle_case.get("target_contracts_by_lot") or {})
        ):
            if lot_id in output:
                output[lot_id] = {
                    **model,
                    "lifecycle_state": "conflict",
                    "lifecycle_reason_codes": ["lifecycle_case_target_overlap"],
                    "actionable": False,
                }
            else:
                output[lot_id] = dict(model)
    return output


def build_position_advice_share_coverage(
    *,
    portfolio_context: JsonDict,
    option_positions_context: JsonDict,
) -> JsonDict:
    """Build per-symbol uncommitted covered-share pools."""

    portfolio = dict(portfolio_context or {})
    option_ctx = dict(option_positions_context or {})
    stocks = portfolio.get("stocks_by_symbol")
    locked = option_ctx.get("locked_shares_by_symbol")
    unavailable = option_ctx.get("locked_shares_unavailable_by_symbol")
    stocks = dict(stocks) if isinstance(stocks, dict) else {}
    locked = dict(locked) if isinstance(locked, dict) else {}
    unavailable = (
        dict(unavailable) if isinstance(unavailable, dict) else {}
    )
    symbols = {
        canonical_symbol(item) or str(item or "").strip().upper()
        for item in (*stocks.keys(), *locked.keys(), *unavailable.keys())
        if str(item or "").strip()
    }
    by_symbol: dict[str, JsonDict] = {}
    for symbol in sorted(symbols):
        stock = stocks.get(symbol)
        if not isinstance(stock, dict):
            stock = next(
                (
                    value
                    for key, value in stocks.items()
                    if (canonical_symbol(key) or str(key).strip().upper())
                    == symbol
                    and isinstance(value, dict)
                ),
                None,
            )
        locked_value = next(
            (
                value
                for key, value in locked.items()
                if (canonical_symbol(key) or str(key).strip().upper())
                == symbol
            ),
            0,
        )
        unavailable_reason = next(
            (
                str(value or "").strip()
                for key, value in unavailable.items()
                if (canonical_symbol(key) or str(key).strip().upper())
                == symbol
            ),
            "",
        )
        reasons: list[str] = []
        eligible_shares = _nonnegative_int(
            stock.get("shares") if isinstance(stock, dict) else None
        )
        locked_shares = _nonnegative_int(locked_value)
        if not isinstance(stock, dict):
            reasons.append("underlying_holding_missing")
        elif eligible_shares is None:
            reasons.append("underlying_shares_invalid")
        if locked_shares is None:
            reasons.append("locked_shares_invalid")
        if unavailable_reason:
            reasons.append("locked_shares_unavailable")
        uncommitted = (
            eligible_shares - locked_shares
            if eligible_shares is not None
            and locked_shares is not None
            and not unavailable_reason
            else None
        )
        if uncommitted is not None and uncommitted < 0:
            reasons.append("locked_shares_exceed_holdings")
            uncommitted = None
        by_symbol[symbol] = {
            "symbol": symbol,
            "status": "available" if uncommitted is not None else "unavailable",
            "reason_codes": reasons,
            "unavailable_reason": unavailable_reason or None,
            "eligible_shares": eligible_shares,
            "locked_open_short_call_shares": locked_shares,
            "uncommitted_covered_shares": uncommitted,
            "avg_cost": (
                stock.get("avg_cost") if isinstance(stock, dict) else None
            ),
            "currency": (
                stock.get("currency") if isinstance(stock, dict) else None
            ),
        }
    return {
        "schema_version": "position_advice_share_coverage_values.v1",
        "share_coverage_semantics": "uncommitted_covered_shares.v1",
        "by_symbol": by_symbol,
    }


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed < 0:
        return None
    return parsed


def slice_shared_context_for_account(shared_ctx: JsonDict, account: str | None) -> JsonDict | None:
    if not isinstance(shared_ctx, dict):
        return None
    if not account:
        all_accounts = shared_ctx.get("all_accounts")
        return (dict(all_accounts) if isinstance(all_accounts, dict) else None)
    by_account = shared_ctx.get("by_account")
    if not isinstance(by_account, dict):
        return None
    out = by_account.get(str(account))
    return (dict(out) if isinstance(out, dict) else None)


def main():
    parser = argparse.ArgumentParser(description="Fetch projected position lot context")
    parser.add_argument("--data-config", default=None, help="portfolio data config path; auto-resolves when omitted")
    parser.add_argument("--broker", default="富途")
    parser.add_argument("--account", default=None)
    parser.add_argument("--shared-out", default=None, help="Optional output path for shared context cache")
    parser.add_argument("--out", default=None, help="Output JSON path (default: <state-dir>/option_positions_context.json)")
    parser.add_argument("--state-dir", default="output_shared/state", help="Directory for outputs (default: output_shared/state)")
    parser.add_argument("--quiet", action="store_true", help="suppress stdout (scheduled/cron)")
    args = parser.parse_args()

    base = Path(__file__).resolve().parents[3]
    _data_config_path, _repo, records = resolve_position_lot_snapshots(base=base, data_config=args.data_config)
    # Load exchange rates for base-currency normalization (CNY).
    # Uses current-project cache plus live refresh when needed.
    base = Path(__file__).resolve().parents[3]
    # Resolve output path/state_dir
    if args.out:
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = (base / out_path).resolve()
        state_dir = out_path.parent
    else:
        sd = Path(args.state_dir)
        if not sd.is_absolute():
            sd = (base / sd).resolve()
        sd.mkdir(parents=True, exist_ok=True)
        state_dir = sd
        out_path = (state_dir / 'option_positions_context.json').resolve()

    # Prefer co-locating rate_cache with state_dir

    rates = get_exchange_rates_or_fetch_latest(
        cache_path=(state_dir / 'rate_cache.json').resolve(),
        max_age_hours=24,
    )
    broker = normalize_broker(args.broker)

    ctx = build_context(records, broker=broker, account=args.account, rates=rates)

    atomic_write_json(out_path, ctx)
    if args.shared_out:
        shared_out = Path(args.shared_out)
        if not shared_out.is_absolute():
            shared_out = (base / shared_out).resolve()
        atomic_write_json(shared_out, build_shared_context(records, broker=broker, rates=rates))

    if not args.quiet:
        print(f"[DONE] option positions context -> {out_path}")
        print(f"broker={broker} account={args.account or '-'} selected={ctx['raw_selected_count']}")

        # Keep summary stats best-effort because old records may miss optional fields.
        cash_secured_syms = 0
        try:
            m = ctx.get('cash_secured_by_symbol_by_ccy') or {}
            cash_secured_syms = len(m)
        except Exception:
            cash_secured_syms = 0

        print(f"locked_symbols={len(ctx.get('locked_shares_by_symbol') or {})} cash_secured_symbols={cash_secured_syms}")


if __name__ == "__main__":
    main()
