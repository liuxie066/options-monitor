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

from scripts.feishu_bitable import (
    get_tenant_access_token,
    bitable_list_records,
    safe_float,
    parse_note_kv,
)
from scripts.option_positions_core.domain import (
    effective_contracts,
    effective_contracts_closed,
    effective_contracts_open,
    normalize_account,
    normalize_broker,
    normalize_close_type,
    normalize_currency,
    normalize_option_type,
    normalize_side,
    normalize_status,
)
from scripts.io_utils import atomic_write_json

# Local helper to get FX rates (USDCNY/HKDCNY) for base-currency normalization.
# This file lives in the same scripts/ directory, so plain import works.
try:
    from fx_rates import get_rates_or_fetch_latest
except Exception:
    from scripts.fx_rates import get_rates_or_fetch_latest


def build_context(records: list[dict], broker: str, account: str | None = None, rates: dict | None = None) -> dict:
    """Build context from raw Bitable records.

    Important: keep record_id for downstream actions (auto-close expired positions)
    without adding extra list calls.
    """

    broker_norm = normalize_broker(broker)
    account_norm = normalize_account(account) if account else None
    selected_items: list[dict] = []  # each: {record_id, fields}
    for rec in records:
        fields = rec.get("fields") or {}
        if not fields:
            continue
        # broker: prefer new field; fallback to legacy market field (backward compatible)
        if broker_norm:
            rec_broker = normalize_broker(fields.get("broker") or fields.get("market"))
            if rec_broker != broker_norm:
                continue
        if account_norm and normalize_account(fields.get("account")) != account_norm:
            continue
        selected_items.append({
            'record_id': rec.get('record_id') or rec.get('id'),
            'fields': fields,
        })

    # Aggregate open short positions for constraints
    locked_shares_by_symbol: dict[str, int] = {}

    # cash_secured_amount is stored in option_positions table with an explicit currency field (USD/CNY/HKD).
    # We aggregate:
    # - by_symbol: in original currency buckets
    # - total_base_cny: unified base currency (CNY) using FX rates when available
    cash_secured_by_symbol_by_ccy: dict[str, dict[str, float]] = {}
    cash_secured_total_by_ccy: dict[str, float] = {}

    cash_secured_total_cny: float | None = 0.0

    usdcny = None
    hkdcny = None
    if rates:
        # rates may be either the full cache object {rates:{...}, timestamp, cached_at} or already the dict of rates
        rates_map = rates.get('rates') if isinstance(rates, dict) and 'rates' in rates else rates
        try:
            usdcny = float(rates_map.get('USDCNY')) if rates_map.get('USDCNY') else None
        except Exception:
            usdcny = None
        try:
            hkdcny = float(rates_map.get('HKDCNY')) if rates_map.get('HKDCNY') else None
        except Exception:
            hkdcny = None

    # Minimal open positions list for downstream (auto-close), keeps record_id.
    open_positions_min: list[dict] = []

    for it in selected_items:
        f = it.get('fields') or {}
        note = f.get('note') or ''
        status = normalize_status(f.get("status") or parse_note_kv(note, 'status'))
        if status and status != "open":
            continue
        contracts_total = effective_contracts(f)
        contracts_open = effective_contracts_open(f)
        contracts_closed = effective_contracts_closed(f)
        if contracts_open <= 0:
            continue

        open_positions_min.append({
            'record_id': it.get('record_id'),
            'position_id': (f.get('position_id') or '').strip() or None,
            'broker': normalize_broker(f.get('broker') or f.get('market')),
            'account': normalize_account(f.get('account')) or f.get('account'),
            'symbol': (f.get('symbol') or '').strip().upper() or None,
            'option_type': normalize_option_type(f.get('option_type') or parse_note_kv(note, 'option_type')) or None,
            'side': normalize_side(f.get('side') or parse_note_kv(note, 'side')) or None,
            'status': 'open',
            'contracts': f.get('contracts'),
            'contracts_open': contracts_open,
            'contracts_closed': contracts_closed,
            'currency': normalize_currency(f.get('currency')) or f.get('currency'),
            'cash_secured_amount': f.get('cash_secured_amount'),
            'underlying_share_locked': f.get('underlying_share_locked') or f.get('underlying_shares_locked'),
            'strike': f.get('strike'),
            'multiplier': f.get('multiplier') or parse_note_kv(note, 'multiplier') or None,
            'premium': f.get('premium') if f.get('premium') is not None else parse_note_kv(note, 'premium_per_share'),
            'expiration': f.get('expiration'),
            'opened_at': f.get('opened_at'),
            'last_action_at': f.get('last_action_at'),
            'close_type': normalize_close_type(f.get('close_type')) if f.get('close_type') else None,
            'close_reason': f.get('close_reason'),
            'note': note,
        })

        symbol = (f.get("symbol") or "").strip().upper()
        if not symbol:
            continue

        option_type = normalize_option_type(f.get("option_type") or parse_note_kv(note, 'option_type'))
        side = normalize_side(f.get("side") or parse_note_kv(note, 'side'))

        locked = safe_float(f.get("underlying_share_locked"))
        if locked is None:
            locked = safe_float(f.get("underlying_shares_locked"))

        cash_secured = safe_float(f.get("cash_secured_amount"))
        currency = normalize_currency(f.get('currency'))

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
                    if usdcny:
                        cash_secured_total_cny += float(cash_secured) * float(usdcny)
                    else:
                        cash_secured_total_cny = None
                elif currency == 'HKD':
                    if hkdcny:
                        cash_secured_total_cny += float(cash_secured) * float(hkdcny)
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
        "fx_rates": (rates or {}),
        "raw_selected_count": len(selected_items),
        "open_positions_min": open_positions_min,
    }


def build_shared_context(records: list[dict], broker: str, rates: dict | None = None) -> dict:
    broker_norm = normalize_broker(broker)
    accounts: set[str] = set()
    for rec in records:
        fields = rec.get("fields") or {}
        if not fields:
            continue
        rec_broker = normalize_broker(fields.get("broker") or fields.get("market"))
        if broker_norm and rec_broker != broker_norm:
            continue
        acct = normalize_account(fields.get("account"))
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
    parser = argparse.ArgumentParser(description="Fetch option positions context from Feishu option_positions table")
    parser.add_argument("--pm-config", default="../portfolio-management/config.json")
    parser.add_argument("--broker", default="富途")
    parser.add_argument("--market", default=None, help="DEPRECATED alias of --broker")
    parser.add_argument("--account", default=None)
    parser.add_argument("--shared-out", default=None, help="Optional output path for shared context cache")
    parser.add_argument("--out", default=None, help="Output JSON path (default: <state-dir>/option_positions_context.json)")
    parser.add_argument("--state-dir", default="output/state", help="Directory for outputs (default: output/state)")
    parser.add_argument("--quiet", action="store_true", help="suppress stdout (scheduled/cron)")
    args = parser.parse_args()

    base = Path(__file__).resolve().parents[1]
    pm_config_path = Path(args.pm_config)
    if not pm_config_path.is_absolute():
        pm_config_path = (base / pm_config_path).resolve()

    cfg = json.loads(pm_config_path.read_text(encoding="utf-8"))
    feishu_cfg = cfg.get("feishu", {}) or {}
    app_id = feishu_cfg.get("app_id")
    app_secret = feishu_cfg.get("app_secret")

    tables = feishu_cfg.get("tables", {}) or {}
    ref = tables.get("option_positions")
    if not (app_id and app_secret and ref and "/" in ref):
        raise SystemExit("pm config missing feishu app_id/app_secret/option_positions")

    app_token, table_id = ref.split("/", 1)

    token = get_tenant_access_token(app_id, app_secret)
    records = bitable_list_records(token, app_token, table_id)
    # Load FX rates for base-currency normalization (CNY).
    # Uses local cache + shared portfolio-management cache when available.
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

    rates = get_rates_or_fetch_latest(
        cache_path=(state_dir / 'rate_cache.json').resolve(),
        shared_cache_path=(Path(__file__).resolve().parents[2] / 'portfolio-management' / '.data' / 'rate_cache.json'),
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
