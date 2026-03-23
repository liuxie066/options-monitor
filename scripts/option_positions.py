#!/usr/bin/env python3
"""Manage Feishu Bitable option_positions records (add/edit/close/list).

You asked for:
- 自动算 cash_secured_amount（卖 put 的担保占用）
- 平仓/删除用改 status=close 即可

Assumptions (match current tables):
- option_positions table fields include:
  market, account, symbol, option_type, side, contracts, currency, status,
  cash_secured_amount, note, underlying_share_locked
- cash_secured_amount is stored in *native* currency as specified by currency (USD/HKD/CNY)

Auto-calc rule:
- For short put:
    cash_secured_amount = strike * multiplier * contracts
  where strike is the option strike in the same currency.

Because the table currently doesn't have explicit strike/multiplier columns,
we store them in note as key=value pairs:
  strike=..., multiplier=..., exp=..., premium_per_share=...

This script uses Feishu app_id/app_secret from ../portfolio-management/config.json.
"""

from __future__ import annotations

import argparse
import json
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def http_json(method: str, url: str, payload: dict | None = None, headers: dict | None = None) -> dict:
    data = None
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
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


def bitable_list_records(tenant_token: str, app_token: str, table_id: str, page_size: int = 200) -> list[dict]:
    base = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
    headers = {"Authorization": f"Bearer {tenant_token}"}
    out: list[dict] = []
    page_token = None
    for _ in range(50):
        url = f"{base}?page_size={page_size}" + (f"&page_token={page_token}" if page_token else "")
        res = http_json("GET", url, None, headers=headers)
        if res.get("code") != 0:
            raise RuntimeError(f"bitable list records failed: {res}")
        data = res.get("data", {}) or {}
        out.extend(data.get("items", []) or [])
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
        if not page_token:
            break
    return out


def bitable_create_record(tenant_token: str, app_token: str, table_id: str, fields: dict) -> dict:
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
    headers = {"Authorization": f"Bearer {tenant_token}"}
    res = http_json("POST", url, {"fields": fields}, headers=headers)
    if res.get("code") != 0:
        raise RuntimeError(f"bitable create record failed: {res}")
    return res.get("data") or {}


def bitable_update_record(tenant_token: str, app_token: str, table_id: str, record_id: str, fields: dict) -> dict:
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}"
    headers = {"Authorization": f"Bearer {tenant_token}"}
    res = http_json("PUT", url, {"fields": fields}, headers=headers)
    if res.get("code") != 0:
        raise RuntimeError(f"bitable update record failed: {res}")
    return res.get("data") or {}


def parse_note_kv(note: str, key: str) -> str:
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


def merge_note(note: str | None, kv: dict[str, str]) -> str:
    base = (note or '').strip()
    parts = []
    if base:
        # keep existing; we don't try to dedup
        parts.append(base)
    for k, v in kv.items():
        if v is None or v == '':
            continue
        parts.append(f"{k}={v}")
    return ';'.join(parts)


def norm_symbol(s: str) -> str:
    return str(s).strip().upper()


def safe_float(x):
    try:
        if x is None or x == '':
            return None
        return float(x)
    except Exception:
        return None


def calc_cash_secured(strike: float, multiplier: float, contracts: int) -> float:
    return float(strike) * float(multiplier) * int(contracts)


def guess_multiplier(symbol: str) -> float | None:
    # US equity options commonly 100; HK stock options vary, so default None for .HK
    sym = norm_symbol(symbol)
    if sym.endswith('.HK'):
        return None
    return 100.0


def format_money(v: float | None, ccy: str) -> str:
    if v is None:
        return '-'
    c = ccy.upper()
    if c == 'USD':
        return f"${v:,.2f}"
    if c == 'HKD':
        return f"HKD {v:,.2f}"
    if c == 'CNY':
        return f"¥{v:,.2f}"
    return f"{v:,.2f} {c}"


def load_pm_config(pm_config: Path) -> tuple[str, str, str, str]:
    cfg = json.loads(pm_config.read_text(encoding='utf-8'))
    feishu_cfg = cfg.get('feishu', {}) or {}
    app_id = feishu_cfg.get('app_id')
    app_secret = feishu_cfg.get('app_secret')
    ref = (feishu_cfg.get('tables', {}) or {}).get('option_positions')
    if not (app_id and app_secret and ref and '/' in ref):
        raise SystemExit('pm config missing feishu app_id/app_secret/option_positions')
    app_token, table_id = ref.split('/', 1)
    return app_id, app_secret, app_token, table_id


def main():
    ap = argparse.ArgumentParser(description='Manage Feishu option_positions table')
    ap.add_argument('--pm-config', default='../portfolio-management/config.json')

    sub = ap.add_subparsers(dest='cmd', required=True)

    p_list = sub.add_parser('list', help='list records')
    p_list.add_argument('--market', default='富途')
    p_list.add_argument('--account', default=None)
    p_list.add_argument('--status', default='open', choices=['open', 'close', 'all'])
    p_list.add_argument('--format', default='text', choices=['text', 'json'])
    p_list.add_argument('--limit', type=int, default=50)

    p_add = sub.add_parser('add', help='add a record')
    p_add.add_argument('--market', default='富途')
    p_add.add_argument('--account', required=True)
    p_add.add_argument('--symbol', required=True)
    p_add.add_argument('--option-type', required=True, choices=['put', 'call'])
    p_add.add_argument('--side', required=True, choices=['short', 'long'])
    p_add.add_argument('--contracts', type=int, required=True)
    p_add.add_argument('--currency', required=True, choices=['USD', 'HKD', 'CNY'])
    p_add.add_argument('--strike', type=float, default=None, help='required for auto cash_secured on short put')
    p_add.add_argument('--multiplier', type=float, default=None, help='default 100 for US; required for HK if strike provided')
    p_add.add_argument('--exp', default=None, help='YYYY-MM-DD (stored in note)')
    p_add.add_argument('--premium-per-share', type=float, default=None, help='stored in note')
    p_add.add_argument('--underlying-share-locked', type=int, default=None, help='for covered call locking shares')
    p_add.add_argument('--note', default=None)
    p_add.add_argument('--dry-run', action='store_true')

    p_close = sub.add_parser('close', help='mark record status=close')
    p_close.add_argument('--record-id', required=True)
    p_close.add_argument('--dry-run', action='store_true')

    p_edit = sub.add_parser('edit', help='patch fields for a record')
    p_edit.add_argument('--record-id', required=True)
    p_edit.add_argument('--set', action='append', default=[], help='field=value (repeatable)')
    p_edit.add_argument('--dry-run', action='store_true')

    args = ap.parse_args()

    base = Path(__file__).resolve().parents[1]
    pm_config = Path(args.pm_config)
    if not pm_config.is_absolute():
        pm_config = (base / pm_config).resolve()

    app_id, app_secret, app_token, table_id = load_pm_config(pm_config)
    token = get_tenant_access_token(app_id, app_secret)

    if args.cmd == 'list':
        items = bitable_list_records(token, app_token, table_id, page_size=200)
        rows = []
        for it in items:
            rid = it.get('record_id')
            f = it.get('fields') or {}
            if args.market and (f.get('market') != args.market):
                continue
            if args.account and (f.get('account') != args.account):
                continue
            st = (f.get('status') or '').strip()
            if args.status != 'all' and st != args.status:
                continue
            rows.append({
                'record_id': rid,
                'market': f.get('market'),
                'account': f.get('account'),
                'symbol': f.get('symbol'),
                'option_type': f.get('option_type'),
                'side': f.get('side'),
                'contracts': f.get('contracts'),
                'currency': f.get('currency'),
                'cash_secured_amount': f.get('cash_secured_amount'),
                'underlying_share_locked': f.get('underlying_share_locked'),
                'status': st,
                'note': f.get('note'),
            })
        rows = rows[: max(args.limit, 1)]
        if args.format == 'json':
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return

        if not rows:
            print('(no records)')
            return
        print('# option_positions')
        for r in rows:
            ccy = (r.get('currency') or 'USD').upper()
            cash = safe_float(r.get('cash_secured_amount'))
            cash_txt = format_money(cash, ccy) if cash is not None else '-'
            print(
                f"- {r['record_id']} | {r.get('account')} | {r.get('symbol')} | {r.get('side')} {r.get('option_type')} | "
                f"contracts {r.get('contracts')} | {ccy} cash_secured {cash_txt} | status {r.get('status')}"
            )
        return

    if args.cmd == 'add':
        sym = norm_symbol(args.symbol)
        acct = str(args.account).strip()
        ccy = str(args.currency).strip().upper()
        side = args.side
        opt_type = args.option_type
        contracts = int(args.contracts)

        strike = args.strike
        multiplier = args.multiplier

        if side == 'short' and opt_type == 'put':
            if strike is None:
                raise SystemExit('add short put requires --strike for auto cash_secured_amount')
            if multiplier is None:
                multiplier = guess_multiplier(sym)
            if multiplier is None:
                raise SystemExit('add short put requires --multiplier for HK symbols (cannot guess)')
            cash_secured = calc_cash_secured(float(strike), float(multiplier), contracts)
        else:
            cash_secured = None

        note_kv = {}
        if strike is not None:
            note_kv['strike'] = str(strike)
        if multiplier is not None:
            note_kv['multiplier'] = str(multiplier)
        if args.exp:
            note_kv['exp'] = str(args.exp)
        if args.premium_per_share is not None:
            note_kv['premium_per_share'] = str(args.premium_per_share)

        note = merge_note(args.note, note_kv)

        fields = {
            'market': args.market,
            'account': acct,
            'symbol': sym,
            'option_type': opt_type,
            'side': side,
            'contracts': str(contracts),
            'currency': ccy,
            'status': 'open',
            'note': note or None,
        }
        if args.underlying_share_locked is not None:
            fields['underlying_share_locked'] = str(int(args.underlying_share_locked))
        if cash_secured is not None:
            # store as string or number both work; match existing table strings
            fields['cash_secured_amount'] = str(float(cash_secured))

        if args.dry_run:
            print('[DRY_RUN] create fields:')
            print(json.dumps(fields, ensure_ascii=False, indent=2))
            return

        res = bitable_create_record(token, app_token, table_id, fields)
        rec = (res.get('record') or {})
        rid = rec.get('record_id')
        print(f"[DONE] created record_id={rid}")
        if cash_secured is not None:
            print(f"cash_secured_amount={format_money(float(cash_secured), ccy)}")
        return

    if args.cmd == 'close':
        if args.dry_run:
            print(f"[DRY_RUN] update {args.record_id}: status=close")
            return
        bitable_update_record(token, app_token, table_id, args.record_id, {'status': 'close'})
        print(f"[DONE] closed {args.record_id}")
        return

    if args.cmd == 'edit':
        if not args.set:
            raise SystemExit('edit requires at least one --set field=value')
        patch = {}
        for s in args.set:
            if '=' not in s:
                raise SystemExit(f"invalid --set: {s}")
            k, v = s.split('=', 1)
            k = k.strip()
            v = v.strip()
            patch[k] = v

        # If user edits strike/multiplier/contracts on a short put, offer recalculation when requested.
        # Minimal: if patch includes strike or multiplier, recompute cash_secured_amount.
        # We need existing record to know side/option_type/currency/contracts.
        items = bitable_list_records(token, app_token, table_id, page_size=200)
        existing = None
        for it in items:
            if it.get('record_id') == args.record_id:
                existing = it.get('fields') or {}
                break
        if not existing:
            raise SystemExit(f"record not found: {args.record_id}")

        side = (existing.get('side') or '').strip()
        opt_type = (existing.get('option_type') or '').strip()
        currency = (patch.get('currency') or existing.get('currency') or 'USD').strip().upper()

        # merge note if user passes note+= style? Keep simple: if patch has note_append, append.
        if 'note_append' in patch:
            new_note = merge_note(existing.get('note'), {'append': patch.pop('note_append')})
            patch['note'] = new_note

        # Recalc trigger
        recalc = False
        if side == 'short' and opt_type == 'put':
            if any(k in patch for k in ('strike', 'multiplier', 'contracts', 'cash_secured_amount')):
                # If user explicitly sets cash_secured_amount, do not override.
                if 'cash_secured_amount' not in patch and ('strike' in patch or 'multiplier' in patch or 'contracts' in patch):
                    recalc = True

        if recalc:
            # strike/multiplier may be in patch, else in note
            note = patch.get('note') if 'note' in patch else (existing.get('note') or '')
            strike = safe_float(patch.get('strike') or parse_note_kv(note, 'strike'))
            mult = safe_float(patch.get('multiplier') or parse_note_kv(note, 'multiplier'))
            contracts = int(safe_float(patch.get('contracts') or existing.get('contracts') or 0) or 0)
            if strike is None or mult is None or contracts <= 0:
                raise SystemExit('recalc requires strike, multiplier, contracts')
            patch['cash_secured_amount'] = str(calc_cash_secured(strike, mult, contracts))

        # Clean: only allow actual table fields
        allowed = {
            'market','account','symbol','option_type','side','contracts','currency','status',
            'cash_secured_amount','note','underlying_share_locked'
        }
        patch2 = {k: v for k, v in patch.items() if k in allowed}

        if args.dry_run:
            print('[DRY_RUN] update fields:')
            print(json.dumps(patch2, ensure_ascii=False, indent=2))
            return

        bitable_update_record(token, app_token, table_id, args.record_id, patch2)
        print(f"[DONE] updated {args.record_id}")
        return


if __name__ == '__main__':
    main()
