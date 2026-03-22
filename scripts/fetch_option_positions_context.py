#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


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


def build_context(records: list[dict], market: str, account: str | None = None) -> dict:
    selected = []
    for rec in records:
        fields = rec.get("fields") or {}
        if not fields:
            continue
        if market and fields.get("market") != market:
            continue
        if account and fields.get("account") != account:
            continue
        selected.append(fields)

    # Aggregate open short positions for constraints
    locked_shares_by_symbol: dict[str, int] = {}
    cash_secured_by_symbol: dict[str, float] = {}

    for f in selected:
        note = f.get('note') or ''
        status = (f.get("status") or "").strip() or parse_note_kv(note, 'status')
        if status and status != "open":
            continue

        symbol = (f.get("symbol") or "").strip().upper()
        if not symbol:
            continue

        option_type = (f.get("option_type") or "").strip() or parse_note_kv(note, 'option_type')
        side = (f.get("side") or "").strip() or parse_note_kv(note, 'side')

        contracts = safe_float(f.get("contracts"))
        contracts_i = int(contracts) if contracts is not None else 0

        # fields name note: current table uses underlying_share_locked (singular)
        locked = safe_float(f.get("underlying_share_locked"))
        if locked is None:
            locked = safe_float(f.get("underlying_shares_locked"))
        cash_secured = safe_float(f.get("cash_secured_amount"))

        if side == "short" and option_type == "call":
            if locked is None:
                locked = contracts_i * 100
            locked_shares_by_symbol[symbol] = locked_shares_by_symbol.get(symbol, 0) + int(locked)

        if side == "short" and option_type == "put":
            if cash_secured is not None:
                cash_secured_by_symbol[symbol] = cash_secured_by_symbol.get(symbol, 0.0) + float(cash_secured)

    return {
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "filters": {"market": market, "account": account},
        "locked_shares_by_symbol": locked_shares_by_symbol,
        "cash_secured_by_symbol": cash_secured_by_symbol,
        "raw_selected_count": len(selected),
    }


def main():
    parser = argparse.ArgumentParser(description="Fetch option positions context from Feishu option_positions table")
    parser.add_argument("--pm-config", default="../portfolio-management/config.json")
    parser.add_argument("--market", default="富途")
    parser.add_argument("--account", default=None)
    parser.add_argument("--out", default="output/state/option_positions_context.json")
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
    ctx = build_context(records, market=args.market, account=args.account)

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = (base / out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(ctx, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[DONE] option positions context -> {out_path}")
    print(f"market={args.market} account={args.account or '-'} selected={ctx['raw_selected_count']}")
    print(f"locked_symbols={len(ctx['locked_shares_by_symbol'])} cash_secured_symbols={len(ctx['cash_secured_by_symbol'])}")


if __name__ == "__main__":
    main()
