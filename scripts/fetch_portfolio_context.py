#!/usr/bin/env python3
from __future__ import annotations

# Allow running as a script without installation.
# When executed as `python scripts/fetch_portfolio_context.py`, ensure repo root is on sys.path
# so `import scripts.*` works consistently.
import sys
from pathlib import Path

repo_base = Path(__file__).resolve().parents[1]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from scripts.feishu_bitable import (
    FeishuAuthError,
    FeishuError,
    get_tenant_access_token,
    bitable_search_records,
    bitable_list_records,
)
from scripts.io_utils import atomic_write_json

from scripts.feishu_bitable import safe_float


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
            "name": asset_name or None,
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
    parser.add_argument("--out", default=None, help="Output JSON path (default: <state-dir>/portfolio_context.json)")
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
    holdings_ref = (feishu_cfg.get("tables", {}) or {}).get("holdings")
    if not (app_id and app_secret and holdings_ref and "/" in holdings_ref):
        raise SystemExit("pm config missing feishu app_id/app_secret/holdings")

    app_token, table_id = holdings_ref.split("/", 1)

    token = get_tenant_access_token(app_id, app_secret)
    # Prefer the search API (newer, more compatible). Fallback to legacy list.
    try:
        records = bitable_search_records(token, app_token, table_id)
    except FeishuError as e:
        # last resort: some tenants still allow list
        records = bitable_list_records(token, app_token, table_id)
    ctx = build_context(records, market=args.market, account=args.account)

    if args.out:
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = (base / out_path).resolve()
    else:
        sd = Path(args.state_dir)
        if not sd.is_absolute():
            sd = (base / sd).resolve()
        sd.mkdir(parents=True, exist_ok=True)
        out_path = (sd / 'portfolio_context.json').resolve()
    atomic_write_json(out_path, ctx)

    if not args.quiet:
        usd_cash = ctx["cash_by_currency"].get("USD")
        print(f"[DONE] portfolio context -> {out_path}")
        print(f"market={args.market} account={args.account or '-'} selected={ctx['raw_selected_count']}")
        print(f"usd_cash={usd_cash if usd_cash is not None else 'N/A'}")
        print(f"us_stocks={len(ctx['stocks_by_symbol'])}")


if __name__ == "__main__":
    main()
