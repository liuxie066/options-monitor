#!/usr/bin/env python3
from __future__ import annotations

# Allow running as a script without installation.
# When executed as `python scripts/fetch_option_positions_context.py`, ensure repo root is on sys.path
# so `import scripts.*` works consistently.
import sys
from pathlib import Path

repo_base = Path(__file__).resolve().parents[1]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

import argparse
import json
from datetime import datetime, timezone

from domain.domain.expiration_dates import (
    EXPIRATION_DATE_TZ,
)
from domain.domain.option_position_lots import normalize_account, normalize_broker
from scripts.io_utils import atomic_write_json
from src.application.option_positions_facade import (
    build_option_position_view,
    canonicalize_option_position_record,
    resolve_option_position_records,
)

# Local helper to get exchange rates (USDCNY/HKDCNY) for base-currency normalization.
# This file lives in the same scripts/ directory, so plain import works.
try:
    from exchange_rates import get_exchange_rates_or_fetch_latest
except Exception:
    from scripts.exchange_rates import get_exchange_rates_or_fetch_latest

def build_context(records: list[dict], broker: str, account: str | None = None, rates: dict | None = None) -> dict:
    """Build context from raw Bitable records.

    Important: keep record_id for downstream actions (auto-close expired positions)
    without adding extra list calls.
    """

    broker_norm = normalize_broker(broker)
    account_norm = normalize_account(account) if account else None
    selected_items: list[dict] = []
    for rec in records:
        view = build_option_position_view(rec)
        fields = view.get("fields") or {}
        if not fields:
            continue
        if broker_norm and view.get("broker") != broker_norm:
            continue
        if account_norm and view.get("account") != account_norm:
            continue
        selected_items.append(view)

    # Aggregate open short positions for constraints
    locked_shares_by_symbol: dict[str, int] = {}

    # cash_secured_amount is stored on projected position lots with an explicit currency field (USD/CNY/HKD).
    # We aggregate:
    # - by_symbol: in original currency buckets
    # - total_base_cny: unified base currency (CNY) using exchange rates when available
    cash_secured_by_symbol_by_ccy: dict[str, dict[str, float]] = {}
    cash_secured_total_by_ccy: dict[str, float] = {}

    cash_secured_total_cny: float | None = 0.0

    usdcny_exchange_rate = None
    cny_per_hkd_exchange_rate = None
    if rates:
        # rates may be either the full cache object {rates:{...}, timestamp, cached_at} or already the dict of rates
        rates_map = rates.get('rates') if isinstance(rates, dict) and 'rates' in rates else rates
        try:
            usdcny_exchange_rate = float(rates_map.get('USDCNY')) if rates_map.get('USDCNY') else None
        except Exception:
            usdcny_exchange_rate = None
        try:
            cny_per_hkd_exchange_rate = float(rates_map.get('HKDCNY')) if rates_map.get('HKDCNY') else None
        except Exception:
            cny_per_hkd_exchange_rate = None

    # Minimal open positions list for downstream (auto-close), keeps record_id.
    open_positions_min: list[dict] = []
    as_of_date = datetime.now(EXPIRATION_DATE_TZ).date()

    for it in selected_items:
        f = it.get('fields') or {}
        status = it.get("status")
        if status and status != "open":
            continue
        contracts_total = int(it.get("contracts") or 0)
        contracts_open = int(it.get("contracts_open") or 0)
        contracts_closed = int(it.get("contracts_closed") or 0)
        if contracts_open <= 0:
            continue
        expiration_date = it.get("expiration_date")
        days_to_expiration = (expiration_date - as_of_date).days if expiration_date is not None else None

        open_positions_min.append({
            'record_id': it.get('record_id'),
            'position_id': it.get('position_id'),
            'broker': it.get('broker'),
            'account': it.get('account'),
            'symbol': it.get('symbol'),
            'option_type': it.get('option_type'),
            'side': it.get('side'),
            'status': 'open',
            'contracts': contracts_total,
            'contracts_open': contracts_open,
            'contracts_closed': contracts_closed,
            'currency': it.get('currency'),
            'cash_secured_amount': it.get('cash_secured_amount'),
            'underlying_share_locked': it.get('underlying_share_locked'),
            'strike': it.get('strike'),
            'multiplier': it.get('multiplier'),
            'premium': it.get('premium'),
            'expiration': it.get('expiration'),
            'expiration_ymd': it.get('expiration_ymd'),
            'days_to_expiration': days_to_expiration,
            'opened_at': it.get('opened_at'),
            'last_action_at': it.get('last_action_at'),
            'close_type': it.get('close_type'),
            'close_reason': f.get('close_reason'),
            'note': it.get('note'),
        })

        symbol = str(it.get("symbol") or "").strip().upper()
        if not symbol:
            continue

        option_type = it.get("option_type")
        side = it.get("side")
        locked = it.get("underlying_share_locked")
        cash_secured = it.get("cash_secured_amount")
        currency = it.get("currency")

        if side == "short" and option_type == "call":
            if locked is None:
                locked = contracts_open * 100
            elif contracts_total > 0 and contracts_open < contracts_total:
                locked = float(locked) / float(contracts_total) * float(contracts_open)
            locked_shares_by_symbol[symbol] = locked_shares_by_symbol.get(symbol, 0) + int(locked)

        if side == "short" and option_type == "put":
            if cash_secured is None:
                continue
            if not currency:
                currency = 'USD'  # backward compatible default
            if contracts_total > 0 and contracts_open < contracts_total:
                cash_secured = float(cash_secured) / float(contracts_total) * float(contracts_open)

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

    return {
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "filters": {"broker": broker_norm, "account": account_norm or account},
        "locked_shares_by_symbol": locked_shares_by_symbol,
        "cash_secured_by_symbol_by_ccy": cash_secured_by_symbol_by_ccy,
        "cash_secured_total_by_ccy": cash_secured_total_by_ccy,
        "cash_secured_total_cny": cash_secured_total_cny,
        "exchange_rates": (rates or {}),
        "raw_selected_count": len(selected_items),
        "open_positions_min": open_positions_min,
    }


def build_shared_context(records: list[dict], broker: str, rates: dict | None = None) -> dict:
    broker_norm = normalize_broker(broker)
    accounts: set[str] = set()
    for rec in records:
        fields = canonicalize_option_position_record(rec).get("fields") or {}
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


def slice_shared_context_for_account(shared_ctx: dict, account: str | None) -> dict | None:
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
    parser.add_argument("--market", default=None, help="DEPRECATED alias of --broker")
    parser.add_argument("--account", default=None)
    parser.add_argument("--shared-out", default=None, help="Optional output path for shared context cache")
    parser.add_argument("--out", default=None, help="Output JSON path (default: <state-dir>/option_positions_context.json)")
    parser.add_argument("--state-dir", default="output/state", help="Directory for outputs (default: output/state)")
    parser.add_argument("--quiet", action="store_true", help="suppress stdout (scheduled/cron)")
    args = parser.parse_args()

    base = Path(__file__).resolve().parents[1]
    _data_config_path, _repo, records = resolve_option_position_records(base=base, data_config=args.data_config)
    # Load exchange rates for base-currency normalization (CNY).
    # Uses current-project cache plus live refresh when needed.
    base = Path(__file__).resolve().parents[1]
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
    if args.market:
        broker = normalize_broker(args.market)
        if not args.quiet:
            print("[WARN] --market is deprecated; use --broker")

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
