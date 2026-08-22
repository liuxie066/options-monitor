#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

import argparse
import json
import math
from datetime import datetime, timezone

from src.infrastructure.feishu_bitable import (
    FeishuAuthError,
    FeishuPermanentError,
    FeishuPermissionError,
    FeishuRateLimitError,
    bitable_search_records,
    bitable_list_records,
    with_tenant_token_retry,
)
from domain.domain.symbol_identity import canonical_symbol
from src.application.config_loader import resolve_data_config_path
from src.application.secret_resolver import resolve_feishu_holdings_config
from src.infrastructure.io_utils import atomic_write_json
from domain.domain.ledger.position_fields import normalize_account
from domain.domain.option_position_identity import normalize_currency

from src.infrastructure.feishu_bitable import safe_float


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


def _source_timestamp(value) -> datetime | None:
    if isinstance(value, (int, float)) or (
        isinstance(value, str) and value.strip().isdigit()
    ):
        try:
            numeric = float(value)
            if numeric >= 10_000_000_000:
                numeric /= 1000.0
            return datetime.fromtimestamp(numeric, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _record_source_observation(record: dict) -> tuple[datetime | None, str | None]:
    fields = record.get("fields") if isinstance(record.get("fields"), dict) else {}
    for key in (
        "snapshot_observed_at",
        "source_observed_at",
        "observed_at_utc",
    ):
        if key in fields:
            return _source_timestamp(fields.get(key)), f"holdings_field:{key}"
    for key in ("last_modified_time", "updated_at_utc"):
        if key in record:
            return _source_timestamp(record.get(key)), f"feishu_record:{key}"
    return None, None


def _portfolio_source_observation(
    *,
    records: list[dict],
    source_observed_at: str | None,
) -> tuple[str | None, str, str]:
    if source_observed_at is not None:
        parsed = _source_timestamp(source_observed_at)
        if parsed is None:
            return None, "invalid", "producer_provided"
        return (
            parsed.isoformat().replace("+00:00", "Z"),
            "trusted",
            "producer_provided",
        )
    if not records:
        return None, "unknown", "no_selected_holdings"
    observations = [_record_source_observation(record) for record in records]
    if any(observed is None for observed, _basis in observations):
        return None, "unknown", "record_observation_missing"
    timestamps = [observed for observed, _basis in observations if observed is not None]
    bases = sorted({str(basis) for _observed, basis in observations if basis})
    oldest = min(timestamps)
    return (
        oldest.isoformat().replace("+00:00", "Z"),
        "trusted",
        bases[0] if len(bases) == 1 else "mixed_owner_metadata",
    )


def build_context(
    records: list[dict],
    broker: str | None = None,
    account: str | None = None,
    *,
    source_observed_at: str | None = None,
    portfolio_source_name: str = "holdings",
    source_account_identifiers: list[str] | tuple[str, ...] | set[str] = (),
) -> dict:
    # holding schema fields we saw:
    # asset_id, asset_type, broker/market, account, quantity, avg_cost, currency
    selected = []
    selected_records: list[dict] = []
    broker_norm = str(broker).strip() if broker else None
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
        selected_records.append({**rec, "fields": fields})

    stocks_by_symbol: dict[str, dict] = {}
    stock_cost_basis: dict[str, dict[str, float | int]] = {}
    cash_by_currency: dict[str, float] = {}
    cash_balance_unavailable_by_row: dict[str, str] = {}
    cash_selected_records: list[dict] = []
    cash_row_count = 0

    for selected_index, f in enumerate(selected):
        asset_type = _as_text(f.get("asset_type")).strip()
        asset_class = _as_text(f.get("asset_class")).strip()
        asset_id = _as_text(f.get("asset_id")).strip()
        asset_name = _as_text(f.get("asset_name")).strip()
        currency = normalize_currency(_as_text(f.get("currency"))) or None
        raw_qty = f.get("quantity")
        qty = safe_float(raw_qty)
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
            cash_selected_records.append(selected_records[selected_index])
            cash_row_count += 1
            row_key = f"cash_row_{cash_row_count}"
            # holdings 表里 cash 的 quantity 可能是字符串；currency 是单选，值为 'USD'/'CNY'/...
            if not currency:
                cash_balance_unavailable_by_row[row_key] = "currency_missing"
            elif isinstance(raw_qty, bool) or qty is None or not math.isfinite(qty):
                cash_balance_unavailable_by_row[row_key] = (
                    f"{currency}:quantity_invalid"
                )
            else:
                ccy_u = normalize_currency(currency)
                cash_by_currency[ccy_u] = cash_by_currency.get(ccy_u, 0.0) + qty
            continue

        sym = _normalize_symbol(asset_type, asset_id)
        if not sym or qty is None:
            continue

        # Keep only what downstream needs. Multiple holdings rows for the same
        # account/symbol must aggregate; otherwise sell-call capacity can be
        # undercounted or overwritten by the last row.
        shares = int(qty)
        existing = stocks_by_symbol.get(sym)
        if existing is None:
            known_shares = shares if avg_cost is not None else 0
            unknown_shares = 0 if avg_cost is not None else shares
            stocks_by_symbol[sym] = {
                "symbol": sym,
                "name": asset_name or None,
                "shares": shares,
                "avg_cost": avg_cost if unknown_shares == 0 else None,
                "cost_basis_complete": unknown_shares == 0,
                "cost_known_shares": known_shares,
                "cost_unknown_shares": unknown_shares,
                "currency": currency,
                "broker": _record_broker_text(f),
                "account": normalize_account(_as_text(f.get("account"))),
            }
            stock_cost_basis[sym] = {
                "known_shares": known_shares,
                "unknown_shares": unknown_shares,
                "known_cost_total": float(avg_cost or 0.0) * known_shares,
            }
            continue

        new_shares = int(existing.get("shares") or 0) + shares
        basis = stock_cost_basis[sym]
        if avg_cost is None:
            basis["unknown_shares"] = int(basis["unknown_shares"]) + shares
        else:
            basis["known_shares"] = int(basis["known_shares"]) + shares
            basis["known_cost_total"] = float(basis["known_cost_total"]) + (float(avg_cost) * shares)
        existing["shares"] = new_shares
        known_shares = int(basis["known_shares"])
        unknown_shares = int(basis["unknown_shares"])
        existing["cost_known_shares"] = known_shares
        existing["cost_unknown_shares"] = unknown_shares
        existing["cost_basis_complete"] = unknown_shares == 0
        existing["avg_cost"] = (
            float(basis["known_cost_total"]) / float(known_shares)
            if unknown_shares == 0 and known_shares > 0
            else None
        )
        if not existing.get("name") and asset_name:
            existing["name"] = asset_name
        if not existing.get("currency") and currency:
            existing["currency"] = currency

    if cash_row_count == 0:
        cash_balance_unavailable_by_row["cash_snapshot"] = "cash_rows_missing"

    retrieved_at = datetime.now(timezone.utc).isoformat()
    observed_at, observation_status, observation_basis = (
        _portfolio_source_observation(
            records=selected_records,
            source_observed_at=source_observed_at,
        )
    )
    cash_observed_at, cash_observation_status, cash_observation_basis = (
        _portfolio_source_observation(
            records=cash_selected_records,
            source_observed_at=source_observed_at,
        )
    )
    identifiers = sorted(
        {
            str(item or "").strip().lower()
            for item in source_account_identifiers
            if str(item or "").strip()
        }
    )
    if not identifiers and account_norm:
        identifiers = [account_norm]
    return {
        "as_of_utc": retrieved_at,
        "retrieved_at_utc": retrieved_at,
        "source_observed_at": observed_at,
        "source_observation_status": observation_status,
        "source_observation_basis": observation_basis,
        "source_account_identifiers": identifiers,
        "filters": {"broker": broker_norm, "account": account_norm},
        "cash_by_currency": cash_by_currency,
        "cash_balance_reliable": not cash_balance_unavailable_by_row,
        "cash_balance_unavailable_by_row": cash_balance_unavailable_by_row,
        "cash_source_observed_at": cash_observed_at,
        "cash_source_observation_status": cash_observation_status,
        "cash_source_observation_basis": cash_observation_basis,
        "stocks_by_symbol": stocks_by_symbol,
        "raw_selected_count": len(selected),
        "portfolio_source_name": str(portfolio_source_name or "holdings"),
    }


def build_shared_context(
    records: list[dict],
    broker: str | None = None,
    *,
    source_observed_at: str | None = None,
    portfolio_source_name: str = "holdings",
) -> dict:
    broker_norm = str(broker).strip() if broker else None
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

    by_account = {
        acct: build_context(
            records,
            broker=broker_norm,
            account=acct,
            source_observed_at=source_observed_at,
            portfolio_source_name=portfolio_source_name,
            source_account_identifiers=[acct],
        )
        for acct in sorted(accounts)
    }
    all_accounts = build_context(
        records,
        broker=broker_norm,
        account=None,
        source_observed_at=source_observed_at,
        portfolio_source_name=portfolio_source_name,
        source_account_identifiers=sorted(accounts),
    )
    return {
        "as_of_utc": all_accounts["as_of_utc"],
        "retrieved_at_utc": all_accounts["retrieved_at_utc"],
        "source_observed_at": all_accounts["source_observed_at"],
        "source_observation_status": all_accounts[
            "source_observation_status"
        ],
        "source_observation_basis": all_accounts[
            "source_observation_basis"
        ],
        "source_account_identifiers": sorted(accounts),
        "portfolio_source_name": str(portfolio_source_name or "holdings"),
        "filters": {"broker": broker_norm, "account": None},
        "all_accounts": all_accounts,
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
    cfg: dict = {}
    if data_config_path.exists():
        payload = json.loads(data_config_path.read_text(encoding="utf-8"))
        cfg = payload if isinstance(payload, dict) else {}
    feishu = resolve_feishu_holdings_config(cfg)
    if not feishu.ready:
        missing = ", ".join(feishu.missing_fields)
        raise ValueError(f"environment missing Feishu holdings config: {missing}")

    app_token, table_id = feishu.holdings_ref.split("/", 1)

    def _list_records(token: str) -> list[dict]:
        try:
            return bitable_search_records(token, app_token, table_id)
        except (FeishuAuthError, FeishuPermissionError, FeishuRateLimitError):
            raise
        except FeishuPermanentError:
            return bitable_list_records(token, app_token, table_id)

    return with_tenant_token_retry(feishu.app_id, feishu.app_secret, _list_records)


def load_holdings_portfolio_context(
    *,
    data_config_path: Path,
    broker: str | None = None,
    account: str | None = None,
) -> dict:
    records = load_holdings_records(data_config_path)
    return build_context(
        records,
        broker=broker,
        account=account,
        portfolio_source_name="external_holdings",
        source_account_identifiers=([account] if account else []),
    )


def load_holdings_portfolio_shared_context(
    *,
    data_config_path: Path,
    broker: str | None = None,
) -> dict:
    records = load_holdings_records(data_config_path)
    return build_shared_context(
        records,
        broker=broker,
        portfolio_source_name="external_holdings",
    )


def main():
    parser = argparse.ArgumentParser(description="Fetch portfolio context from Feishu holdings table")
    parser.add_argument("--data-config", default=None, help="portfolio data config path; auto-resolves when omitted")
    parser.add_argument("--broker", default="富途")
    parser.add_argument("--account", default=None)
    parser.add_argument("--shared-out", default=None, help="Optional output path for shared context cache")
    parser.add_argument("--out", default=None, help="Output JSON path (default: <state-dir>/portfolio_context.json)")
    parser.add_argument("--state-dir", default="output_shared/state", help="Directory for outputs (default: output_shared/state)")
    parser.add_argument("--quiet", action="store_true", help="suppress stdout (scheduled/cron)")
    args = parser.parse_args()

    base = Path(__file__).resolve().parents[2]
    data_config_path = resolve_data_config_path(base=base, data_config=args.data_config)

    try:
        records = load_holdings_records(data_config_path)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    ctx = build_context(records, broker=args.broker, account=args.account)

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
        atomic_write_json(shared_out, build_shared_context(records, broker=args.broker))

    if not args.quiet:
        usd_cash = ctx["cash_by_currency"].get("USD")
        print(f"[DONE] portfolio context -> {out_path}")
        print(f"broker={args.broker} account={args.account or '-'} selected={ctx['raw_selected_count']}")
        print(f"usd_cash={usd_cash if usd_cash is not None else 'N/A'}")
        print(f"us_stocks={len(ctx['stocks_by_symbol'])}")


if __name__ == "__main__":
    main()
