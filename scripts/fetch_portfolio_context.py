#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from scripts.io_utils import atomic_write_json


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


def bitable_search_records(tenant_token: str, app_token: str, table_id: str, page_size: int = 500) -> list[dict]:
    """Search records (preferred).

    Feishu has been gradually deprecating the GET list-records API for some tenants.
    The POST /records/search API is the recommended replacement and supports paging.
    """
    base = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/search"
    headers = {
        "Authorization": f"Bearer {tenant_token}",
        # doc says: application/json; charset=utf-8 (we set json already)
        "Content-Type": "application/json; charset=utf-8",
    }

    page_token = None
    out: list[dict] = []
    for _ in range(50):
        url = f"{base}?page_size={page_size}" + (f"&page_token={page_token}" if page_token else "")
        # empty body means: no filter, default view
        res = http_json("POST", url, payload={}, headers=headers)
        if res.get("code") != 0:
            raise RuntimeError(f"bitable search records failed: {res}")
        data = res.get("data", {})
        out.extend(data.get("items", []))
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
        if not page_token:
            break
    return out


def bitable_list_records(tenant_token: str, app_token: str, table_id: str, page_size: int = 500) -> list[dict]:
    """Legacy list records (kept as fallback)."""
    base = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
    headers = {"Authorization": f"Bearer {tenant_token}"}
    page_token = None
    out: list[dict] = []
    for _ in range(20):
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


def _as_text(v) -> str:
    """Normalize Feishu Bitable cell values into plain text.

    In records/search API, Text fields often come back as a rich-text array:
      [{"text": "富途", "type": "text"}, ...]
    We join the text parts.
    """
    try:
        if v is None:
            return ""
        if isinstance(v, str):
            return v
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, list):
            parts: list[str] = []
            for it in v:
                if isinstance(it, dict) and it.get('text') is not None:
                    parts.append(str(it.get('text')))
                elif isinstance(it, str):
                    parts.append(it)
            return "".join(parts)
        if isinstance(v, dict) and v.get('text') is not None:
            return str(v.get('text'))
    except Exception:
        pass
    return str(v)


def _normalize_symbol(asset_type: str | None, asset_id: str, market_text: str = "") -> str | None:
    """Normalize asset_id into monitoring symbol.

    - us_stock: keep as upper (e.g., NVDA)
    - hk_stock: convert 5-digit/4-digit codes into XXXX.HK (e.g., 00700 -> 0700.HK)
    """
    t = (asset_type or "").strip().lower()
    aid = (asset_id or "").strip()
    if not aid:
        return None

    if t == 'us_stock':
        return aid.upper()

    if t == 'hk_stock':
        # Accept forms: 00700 / 0700 / 700 / HK.00700 / 0700.HK
        s = aid.upper()
        if s.endswith('.HK'):
            core = s[:-3]
        else:
            core = s
        if core.startswith('HK.'):
            core = core[3:]
        core = core.strip()
        # keep digits only
        digits = ''.join(ch for ch in core if ch.isdigit())
        if not digits:
            return None
        digits = str(int(digits))  # strip leading zeros
        digits = digits.zfill(4)
        return f"{digits}.HK"

    return None


def build_context(records: list[dict], market: str, account: str | None = None) -> dict:
    # holding schema fields we saw:
    # asset_id, asset_type, market, account, quantity, avg_cost, currency
    selected = []
    market_norm = str(market).strip() if market else None
    account_norm = str(account).strip() if account else None

    for rec in records:
        fields0 = rec.get("fields") or {}
        if not fields0:
            continue

        m = _as_text(fields0.get("market")).strip()
        a = _as_text(fields0.get("account")).strip()

        # Be tolerant: market column is free-text; accept values that contain the target market string.
        # Still keeps the "only 富途" constraint when market_norm is set.
        if market_norm and market_norm not in m:
            continue
        if account_norm and account_norm != a:
            continue

        # Normalize selected fields (avoid leaking rich-text arrays downstream)
        fields = dict(fields0)
        for k in ("market", "account", "asset_id", "asset_name"):
            if k in fields:
                fields[k] = _as_text(fields.get(k)).strip()
        selected.append(fields)

    stocks_by_symbol: dict[str, dict] = {}
    cash_by_currency: dict[str, float] = {}

    for f in selected:
        asset_type = _as_text(f.get("asset_type")).strip()
        asset_class = _as_text(f.get("asset_class")).strip()
        asset_id = _as_text(f.get("asset_id")).strip()
        asset_name = _as_text(f.get("asset_name")).strip()
        currency = _as_text(f.get("currency")).strip() or None
        qty = safe_float(f.get("quantity"))
        avg_cost = safe_float(f.get("avg_cost"))

        # Be tolerant: some rows may miss asset_type (data entry). Infer cash rows.
        inferred_cash = False
        if asset_type == "cash":
            inferred_cash = True
        elif asset_class == "现金":
            inferred_cash = True
        elif asset_id.upper().endswith("-CASH") or asset_id.upper().endswith("-MMF"):
            inferred_cash = True
        elif asset_name in ("账户余额", "货基", "余额宝") and avg_cost is None:
            inferred_cash = True

        if inferred_cash:
            # holdings 表里 cash 的 quantity 可能是字符串；currency 是单选，值为 'USD'/'CNY'/...
            if currency and qty is not None:
                ccy_u = str(currency).strip().upper()
                cash_by_currency[ccy_u] = cash_by_currency.get(ccy_u, 0.0) + qty
            continue

        sym = _normalize_symbol(asset_type, asset_id, market_text=_as_text(f.get('market')))
        if not sym or qty is None:
            continue

        # Keep only what downstream needs.
        stocks_by_symbol[sym] = {
            "symbol": sym,
            "shares": int(qty),
            "avg_cost": avg_cost,
            "currency": currency,
            "market": _as_text(f.get("market")).strip(),
            "account": _as_text(f.get("account")).strip(),
        }

    return {
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "filters": {"market": market, "account": account},
        "cash_by_currency": cash_by_currency,
        "stocks_by_symbol": stocks_by_symbol,
        "raw_selected_count": len(selected),
    }


def main():
    parser = argparse.ArgumentParser(description="Fetch portfolio context from Feishu holdings table")
    parser.add_argument("--pm-config", default="../portfolio-management/config.json")
    parser.add_argument("--market", default="富途")
    parser.add_argument("--account", default=None)
    parser.add_argument("--out", default="output/state/portfolio_context.json")
    args = parser.parse_args()

    base = Path(__file__).resolve().parents[1]
    pm_config_path = Path(args.pm_config)
    if not pm_config_path.is_absolute():
        pm_config_path = (base / pm_config_path).resolve()

    cfg = json.loads(pm_config_path.read_text(encoding="utf-8"))
    feishu_cfg = cfg.get("feishu", {}) or {}
    app_id = feishu_cfg.get("app_id")
    app_secret = feishu_cfg.get("app_secret")
    holdings_ref = (feishu_cfg.get("tables", {}) or {}).get("holdings")
    if not (app_id and app_secret and holdings_ref and "/" in holdings_ref):
        raise SystemExit("pm config missing feishu app_id/app_secret/holdings")

    app_token, table_id = holdings_ref.split("/", 1)

    token = get_tenant_access_token(app_id, app_secret)
    # Prefer the search API (newer, more compatible). Fallback to legacy list.
    try:
        records = bitable_search_records(token, app_token, table_id)
    except Exception as e:
        # last resort: some tenants still allow list
        records = bitable_list_records(token, app_token, table_id)
    ctx = build_context(records, market=args.market, account=args.account)

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = (base / out_path).resolve()
    atomic_write_json(out_path, ctx)

    # concise stdout
    usd_cash = ctx["cash_by_currency"].get("USD")
    print(f"[DONE] portfolio context -> {out_path}")
    print(f"market={args.market} account={args.account or '-'} selected={ctx['raw_selected_count']}")
    print(f"usd_cash={usd_cash if usd_cash is not None else 'N/A'}")
    print(f"us_stocks={len(ctx['stocks_by_symbol'])}")


if __name__ == "__main__":
    main()
