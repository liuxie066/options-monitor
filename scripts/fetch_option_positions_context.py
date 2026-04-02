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
import urllib.request
from datetime import datetime, timezone

from scripts.io_utils import atomic_write_json

# Local helper to get FX rates (USDCNY/HKDCNY) for base-currency normalization.
# This file lives in the same scripts/ directory, so plain import works.
from fx_rates import get_rates


def http_json(method: str, url: str, payload: dict | None = None, headers: dict | None = None) -> dict:
    data = None
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers=req_headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body)


def get_tenant_access_token(app_id: str, app_secret: str) -> str:
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal/"
    res = http_json("POST", url, {"app_id": app_id, "app_secret": app_secret})
    if res.get("code") != 0:
        raise RuntimeError(f"feishu auth failed: {res}")
    return res["tenant_access_token"]


def bitable_list_records(tenant_token: str, app_token: str, table_id: str, page_size: int = 500) -> list[dict]:
    base = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
    headers = {"Authorization": f"Bearer {tenant_token}"}
    page_token = None
    out: list[dict] = []
    for _ in range(40):
        url = f"{base}?page_size={page_size}" + (f"&page_token={page_token}" if page_token else "")
        res = http_json("GET", url, None, headers=headers)
        if res.get("code") != 0:
            raise RuntimeError(f"bitable list records failed: {res}")
        data = res.get("data", {})
        out.extend(data.get("items", []))
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
        if not page_token:
            break
    return out


def safe_float(x):
    try:
        if x is None or x == "":
            return None
        return float(x)
    except Exception:
        return None


def parse_note_kv(note: str, key: str) -> str:
    # supports "key=value" segments separated by ; or ,
    if not note:
        return ''
    s = str(note)
    for part in s.replace(',', ';').split(';'):
        part = part.strip()
        if not part:
            continue
        if part.startswith(key + '='):
            return part.split('=', 1)[1].strip()
    return ''


def build_context(records: list[dict], market: str, account: str | None = None, rates: dict | None = None) -> dict:
    """Build context from raw Bitable records.

    Important: keep record_id for downstream actions (auto-close expired positions)
    without adding extra list calls.
    """

    selected_items: list[dict] = []  # each: {record_id, fields}
    selected_fields: list[dict] = []

    for rec in records:
        fields = rec.get("fields") or {}
        if not fields:
            continue
        if market and fields.get("market") != market:
            continue
        if account and fields.get("account") != account:
            continue
        selected_items.append({
            'record_id': rec.get('record_id') or rec.get('id'),
            'fields': fields,
        })
        selected_fields.append(fields)

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

    for f in selected_fields:
        note = f.get('note') or ''
        status = (f.get("status") or "").strip() or parse_note_kv(note, 'status')
        if status and status != "open":
            continue

        # Build open positions list (best-effort) for downstream tasks.
        # We'll attach record_id later by matching position_id when possible.
        open_positions_min.append({
            'record_id': None,
            'position_id': (f.get('position_id') or '').strip() or None,
            'market': f.get('market'),
            'account': f.get('account'),
            'symbol': (f.get('symbol') or '').strip().upper() or None,
            'option_type': (f.get('option_type') or '').strip() or parse_note_kv(note, 'option_type') or None,
            'side': (f.get('side') or '').strip() or parse_note_kv(note, 'side') or None,
            'status': 'open',
            'expiration': f.get('expiration'),
            'note': note,
        })

        symbol = (f.get("symbol") or "").strip().upper()
        if not symbol:
            continue

        option_type = (f.get("option_type") or "").strip() or parse_note_kv(note, 'option_type')
        side = (f.get("side") or "").strip() or parse_note_kv(note, 'side')

        contracts = safe_float(f.get("contracts"))
        contracts_i = int(contracts) if contracts is not None else 0

        locked = safe_float(f.get("underlying_share_locked"))
        if locked is None:
            locked = safe_float(f.get("underlying_shares_locked"))

        cash_secured = safe_float(f.get("cash_secured_amount"))
        currency = (f.get('currency') or '').strip().upper()

        if side == "short" and option_type == "call":
            if locked is None:
                locked = contracts_i * 100
            locked_shares_by_symbol[symbol] = locked_shares_by_symbol.get(symbol, 0) + int(locked)

        if side == "short" and option_type == "put":
            if cash_secured is None:
                continue
            if not currency:
                currency = 'USD'  # backward compatible default

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

    # Attach record_id to open positions list.
    # Prefer matching by primary key position_id; fallback to a coarse signature.
    id_by_position_id: dict[str, str] = {}
    for it in selected_items:
        rid = it.get('record_id')
        f = it.get('fields') or {}
        pid = (f.get('position_id') or '').strip()
        if rid and pid:
            id_by_position_id[pid] = rid

    open_positions_min2: list[dict] = []
    for p in open_positions_min:
        pid = (p.get('position_id') or '').strip()
        rid = id_by_position_id.get(pid) if pid else None
        p2 = dict(p)
        p2['record_id'] = rid
        open_positions_min2.append(p2)

    return {
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "filters": {"market": market, "account": account},
        "locked_shares_by_symbol": locked_shares_by_symbol,
        "cash_secured_by_symbol_by_ccy": cash_secured_by_symbol_by_ccy,
        "cash_secured_total_by_ccy": cash_secured_total_by_ccy,
        "cash_secured_total_cny": cash_secured_total_cny,
        "fx_rates": (rates or {}),
        "raw_selected_count": len(selected_fields),
        "open_positions_min": open_positions_min2,
    }


def main():
    parser = argparse.ArgumentParser(description="Fetch option positions context from Feishu option_positions table")
    parser.add_argument("--pm-config", default="../portfolio-management/config.json")
    parser.add_argument("--market", default="富途")
    parser.add_argument("--account", default=None)
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

    rates = get_rates(
        cache_path=(state_dir / 'rate_cache.json').resolve(),
        shared_cache_path=(Path(__file__).resolve().parents[2] / 'portfolio-management' / '.data' / 'rate_cache.json'),
        max_age_hours=24,
    )
    ctx = build_context(records, market=args.market, account=args.account, rates=rates)

    atomic_write_json(out_path, ctx)

    if not args.quiet:
        print(f"[DONE] option positions context -> {out_path}")
        print(f"market={args.market} account={args.account or '-'} selected={ctx['raw_selected_count']}")

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
