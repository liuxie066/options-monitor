#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

import argparse
import json
from datetime import datetime, timezone
from typing import Any

from domain.domain.expiration_dates import (
    EXPIRATION_DATE_TZ,
)
from domain.domain.ledger.position_fields import normalize_account, normalize_broker, normalize_currency
from domain.domain.risk_capacity import (
    compute_short_call_locked_shares,
    compute_short_put_cash_secured,
)
from src.infrastructure.io_utils import atomic_write_json
from src.application.ledger.api import (
    RiskPositionView,
    position_lot_risk_view,
    position_lot_snapshot,
    resolve_position_lot_snapshots,
    summarize_position_lot_shadow_status,
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
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "filters": {"broker": broker_norm, "account": account_norm or account},
        "locked_shares_by_symbol": {},
        "locked_shares_unavailable_by_symbol": {},
        "cash_secured_by_symbol_by_ccy": {},
        "cash_secured_total_by_ccy": {},
        "cash_secured_unavailable_by_symbol": {},
        "cash_secured_total_cny": 0.0,
        "exchange_rates": (rates or {}),
        "raw_selected_count": raw_selected_count,
        "open_positions_min": [],
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
) -> JsonDict:
    """Build context from raw Bitable records.

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

    for it in selected_items:
        if not it.is_open:
            continue
        contracts_total = int(it.contracts or 0)
        contracts_open = int(it.contracts_open or 0)
        if contracts_open <= 0:
            continue

        symbol = it.canonical_underlying_symbol

        open_positions_min.append(it.as_open_position_min(as_of_date=as_of_date))
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
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "filters": {"broker": broker_norm, "account": account_norm or account},
        "locked_shares_by_symbol": locked_shares_by_symbol,
        "locked_shares_unavailable_by_symbol": locked_shares_unavailable_by_symbol,
        "cash_secured_by_symbol_by_ccy": cash_secured_by_symbol_by_ccy,
        "cash_secured_total_by_ccy": cash_secured_total_by_ccy,
        "cash_secured_unavailable_by_symbol": cash_secured_unavailable_by_symbol,
        "cash_secured_total_cny": cash_secured_total_cny,
        "exchange_rates": (rates or {}),
        "raw_selected_count": len(selected_items),
        "open_positions_min": open_positions_min,
    }
    out["ledger"] = ledger_status
    return out


def build_shared_context(records: list[JsonDict], broker: str, rates: JsonDict | None = None) -> JsonDict:
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
    by_account = {acct: build_context(records, broker=broker_norm, account=acct, rates=rates) for acct in sorted(accounts)}
    return {
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "filters": {"broker": broker_norm},
        "all_accounts": build_context(records, broker=broker_norm, account=None, rates=rates),
        "by_account": by_account,
    }


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
    parser.add_argument("--state-dir", default="output/state", help="Directory for outputs (default: output/state)")
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

        # Backward/forward compatible stats
        cash_secured_syms = 0
        try:
            m = ctx.get('cash_secured_by_symbol_by_ccy') or {}
            cash_secured_syms = len(m)
        except Exception:
            cash_secured_syms = 0

        print(f"locked_symbols={len(ctx.get('locked_shares_by_symbol') or {})} cash_secured_symbols={cash_secured_syms}")


if __name__ == "__main__":
    main()
