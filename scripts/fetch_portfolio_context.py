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
from datetime import datetime, timezone

from scripts.feishu_bitable import (
    FeishuAuthError,
    FeishuPermanentError,
    FeishuPermissionError,
    FeishuRateLimitError,
    bitable_search_records,
    bitable_list_records,
    with_tenant_token_retry,
)
from scripts.trade_symbol_identity import canonical_symbol
from scripts.config_loader import resolve_data_config_path
from scripts.io_utils import atomic_write_json
from scripts.option_positions_core.domain import normalize_account, normalize_currency

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


def _normalize_symbol(asset_type: str | None, asset_id: str) -> str | None:
    """Normalize asset_id into monitoring symbol.

    - us_stock: keep as upper (e.g., NVDA)
    - hk_stock: convert 5-digit/4-digit codes into XXXX.HK (e.g., 00700 -> 0700.HK)
    """
    t = (asset_type or "").strip().lower()
    aid = (asset_id or "").strip()
    if not aid:
        return None

    if t == 'us_stock':
        return canonical_symbol(aid)

    if t == 'hk_stock':
        return canonical_symbol(aid)

    return None


def _record_broker_text(fields: dict) -> str:
    broker = _as_text(fields.get("broker")).strip()
    if broker:
        return broker
    # Keep legacy `market` compatibility for older holdings tables.
    return _as_text(fields.get("market")).strip()


def build_context(
    records: list[dict],
    broker: str | None = None,
    account: str | None = None,
    *,
    market: str | None = None,
) -> dict:
    # holding schema fields we saw:
    # asset_id, asset_type, broker/market, account, quantity, avg_cost, currency
    selected = []
    broker_raw = broker if broker is not None else market
    broker_norm = str(broker_raw).strip() if broker_raw else None
    account_norm = normalize_account(account) if account else None

    for rec in records:
        fields0 = rec.get("fields") or {}
        if not fields0:
            continue

        b = _record_broker_text(fields0)
        a = normalize_account(_as_text(fields0.get("account")))

        # Be tolerant: broker/legacy market column is free-text; accept values that contain the target broker string.
        # Still keeps the "only 富途" constraint when market_norm is set.
        if broker_norm and broker_norm not in b:
            continue
        if account_norm and account_norm != a:
            continue

        # Normalize selected fields (avoid leaking rich-text arrays downstream)
        fields = dict(fields0)
        for k in ("broker", "asset_id", "asset_name"):
            if k in fields:
                fields[k] = _as_text(fields.get(k)).strip()
        if "account" in fields:
            fields["account"] = normalize_account(_as_text(fields.get("account")))
        selected.append(fields)

    stocks_by_symbol: dict[str, dict] = {}
    cash_by_currency: dict[str, float] = {}

    for f in selected:
        asset_type = _as_text(f.get("asset_type")).strip()
        asset_class = _as_text(f.get("asset_class")).strip()
        asset_id = _as_text(f.get("asset_id")).strip()
        asset_name = _as_text(f.get("asset_name")).strip()
        currency = normalize_currency(_as_text(f.get("currency"))) or None
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
                ccy_u = normalize_currency(currency)
                cash_by_currency[ccy_u] = cash_by_currency.get(ccy_u, 0.0) + qty
            continue

        sym = _normalize_symbol(asset_type, asset_id)
        if not sym or qty is None:
            continue

        # Keep only what downstream needs.
        stocks_by_symbol[sym] = {
            "symbol": sym,
            "name": asset_name or None,
            "shares": int(qty),
            "avg_cost": avg_cost,
            "currency": currency,
            "broker": _record_broker_text(f),
            "account": normalize_account(_as_text(f.get("account"))),
        }

    return {
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "filters": {"broker": broker_norm, "account": account_norm},
        "cash_by_currency": cash_by_currency,
        "stocks_by_symbol": stocks_by_symbol,
        "raw_selected_count": len(selected),
    }


def build_shared_context(records: list[dict], broker: str | None = None, *, market: str | None = None) -> dict:
    broker_raw = broker if broker is not None else market
    broker_norm = str(broker_raw).strip() if broker_raw else None
    accounts: set[str] = set()
    for rec in records:
        fields0 = rec.get("fields") or {}
        if not fields0:
            continue
        b = _record_broker_text(fields0)
        if broker_norm and broker_norm not in b:
            continue
        a = normalize_account(_as_text(fields0.get("account")))
        if a:
            accounts.add(a)

    by_account = {acct: build_context(records, broker=broker_norm, account=acct) for acct in sorted(accounts)}
    return {
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "filters": {"broker": broker_norm, "account": None},
        "all_accounts": build_context(records, broker=broker_norm, account=None),
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
    out = by_account.get(normalize_account(account))
    return (dict(out) if isinstance(out, dict) else None)


def load_holdings_records(data_config_path: Path) -> list[dict]:
    cfg = json.loads(data_config_path.read_text(encoding="utf-8"))
    feishu_cfg = cfg.get("feishu", {}) or {}
    app_id = feishu_cfg.get("app_id")
    app_secret = feishu_cfg.get("app_secret")
    holdings_ref = (feishu_cfg.get("tables", {}) or {}).get("holdings")
    if not (app_id and app_secret and holdings_ref and "/" in holdings_ref):
        raise ValueError("data config missing feishu app_id/app_secret/holdings")

    app_token, table_id = holdings_ref.split("/", 1)

    def _list_records(token: str) -> list[dict]:
        try:
            return bitable_search_records(token, app_token, table_id)
        except (FeishuAuthError, FeishuPermissionError, FeishuRateLimitError):
            raise
        except FeishuPermanentError:
            return bitable_list_records(token, app_token, table_id)

    return with_tenant_token_retry(str(app_id), str(app_secret), _list_records)


def load_holdings_portfolio_context(
    *,
    data_config_path: Path,
    broker: str | None = None,
    account: str | None = None,
) -> dict:
    return build_context(load_holdings_records(data_config_path), broker=broker, account=account)


def load_holdings_portfolio_shared_context(
    *,
    data_config_path: Path,
    broker: str | None = None,
) -> dict:
    return build_shared_context(load_holdings_records(data_config_path), broker=broker)


def main():
    parser = argparse.ArgumentParser(description="Fetch portfolio context from Feishu holdings table")
    parser.add_argument("--data-config", default=None, help="portfolio data config path; auto-resolves when omitted")
    parser.add_argument("--broker", default="富途")
    parser.add_argument("--market", default=None, help="DEPRECATED alias of --broker")
    parser.add_argument("--account", default=None)
    parser.add_argument("--shared-out", default=None, help="Optional output path for shared context cache")
    parser.add_argument("--out", default=None, help="Output JSON path (default: <state-dir>/portfolio_context.json)")
    parser.add_argument("--state-dir", default="output/state", help="Directory for outputs (default: output/state)")
    parser.add_argument("--quiet", action="store_true", help="suppress stdout (scheduled/cron)")
    args = parser.parse_args()

    base = Path(__file__).resolve().parents[1]
    data_config_path = resolve_data_config_path(base=base, data_config=args.data_config)

    broker = args.market if args.market is not None else args.broker
    if args.market and not args.quiet:
        print("[WARN] --market is deprecated; use --broker")

    try:
        records = load_holdings_records(data_config_path)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    ctx = build_context(records, broker=broker, account=args.account)

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
    if args.shared_out:
        shared_out = Path(args.shared_out)
        if not shared_out.is_absolute():
            shared_out = (base / shared_out).resolve()
        atomic_write_json(shared_out, build_shared_context(records, broker=broker))

    if not args.quiet:
        usd_cash = ctx["cash_by_currency"].get("USD")
        print(f"[DONE] portfolio context -> {out_path}")
        print(f"broker={broker} account={args.account or '-'} selected={ctx['raw_selected_count']}")
        print(f"usd_cash={usd_cash if usd_cash is not None else 'N/A'}")
        print(f"us_stocks={len(ctx['stocks_by_symbol'])}")


if __name__ == "__main__":
    main()
