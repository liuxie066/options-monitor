from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from domain.domain.fetch_source import is_futu_fetch_source, normalize_fetch_source
from scripts.futu_gateway import build_ready_futu_gateway
from scripts.option_positions_core.domain import normalize_account, normalize_currency
from scripts.trade_symbol_identity import canonical_symbol, symbol_currency


def _rows(data: Any) -> list[dict[str, Any]]:
    if hasattr(data, "to_dict"):
        try:
            recs = data.to_dict("records")
            if isinstance(recs, list):
                return [dict(r) for r in recs]
        except Exception:
            pass
    if isinstance(data, list):
        out: list[dict[str, Any]] = []
        for row in data:
            if isinstance(row, dict):
                out.append(dict(row))
        return out
    if isinstance(data, dict):
        return [dict(data)]
    return []


def _pick(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row:
            value = row.get(key)
            if value is not None:
                return value
    return None


def _to_float(value: Any) -> float | None:
    try:
        if value in (None, "", "-"):
            return None
        return float(value)
    except Exception:
        return None


def _to_int(value: Any) -> int | None:
    try:
        if value in (None, "", "-"):
            return None
        return int(float(value))
    except Exception:
        return None


def _to_futu_acc_id(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError(f"invalid futu account_id={value!r}")
    if isinstance(value, int):
        return value

    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"invalid futu account_id={value!r}")
    if raw.startswith("-"):
        digits = raw[1:]
    else:
        digits = raw
    if not digits.isdigit():
        raise ValueError(f"invalid futu account_id={value!r}")
    return int(raw)


def _normalize_currency(value: Any, *, fallback: str = "CNY") -> str:
    return normalize_currency(value) or normalize_currency(fallback) or "CNY"


def _normalize_symbol(value: Any) -> str | None:
    return canonical_symbol(value)


def _normalize_account_ids(raw: Mapping[str, Any] | Any, *, account: str | None) -> list[str]:
    if not account or not isinstance(raw, Mapping):
        return []
    want = normalize_account(account)
    out: list[str] = []
    for acc_id, mapped in raw.items():
        if normalize_account(mapped) != want:
            continue
        key = str(acc_id or "").strip()
        if key:
            out.append(key)
    return out


def resolve_trade_intake_futu_account_ids(cfg: Mapping[str, Any] | Any, *, account: str | None) -> list[str]:
    if not isinstance(cfg, Mapping):
        return []
    trade_intake = cfg.get("trade_intake")
    if not isinstance(trade_intake, Mapping):
        return []
    account_mapping = trade_intake.get("account_mapping")
    if not isinstance(account_mapping, Mapping):
        return []
    futu_mapping = account_mapping.get("futu")
    return _normalize_account_ids(futu_mapping, account=account)


def infer_futu_portfolio_settings(cfg: Mapping[str, Any] | Any, *, account: str | None = None) -> dict[str, Any]:
    if not isinstance(cfg, Mapping):
        return {}

    # 1. Prefer account-specific settings if account is provided
    if account:
        account_settings = cfg.get("account_settings")
        if isinstance(account_settings, Mapping):
            acc_cfg = account_settings.get(account)
            if isinstance(acc_cfg, Mapping):
                futu_cfg = acc_cfg.get("futu")
                if isinstance(futu_cfg, Mapping):
                    out = dict(futu_cfg)
                    if out.get("host") and out.get("port"):
                        return out

    # 2. Fall back to global portfolio.futu
    portfolio_cfg = cfg.get("portfolio")
    portfolio_futu = {}
    if isinstance(portfolio_cfg, Mapping):
        raw = portfolio_cfg.get("futu")
        if isinstance(raw, Mapping):
            portfolio_futu = dict(raw)

    out = dict(portfolio_futu)
    if out.get("host") and out.get("port"):
        return out

    # 3. Fall back to symbol-level fetch settings
    symbols = cfg.get("symbols") or cfg.get("watchlist") or []
    if not isinstance(symbols, list):
        return out

    for item in symbols:
        if not isinstance(item, Mapping):
            continue
        fetch = item.get("fetch")
        if not isinstance(fetch, Mapping):
            continue
        src = normalize_fetch_source(fetch.get("source", "opend"))
        if not is_futu_fetch_source(src):
            continue
        for key in ("host", "port", "trd_env", "acc_id", "trd_market", "cash_currency"):
            if out.get(key) in (None, "") and fetch.get(key) not in (None, ""):
                out[key] = fetch.get(key)
        if out.get("host") and out.get("port"):
            break
    return out


def should_try_futu_portfolio(cfg: Mapping[str, Any] | Any, *, account: str | None) -> bool:
    settings = infer_futu_portfolio_settings(cfg, account=account)
    if not settings.get("host") or not settings.get("port"):
        return False
    return bool(resolve_trade_intake_futu_account_ids(cfg, account=account))


def _filter_rows_for_account_ids(rows: list[dict[str, Any]], account_ids: set[str]) -> list[dict[str, Any]]:
    if not account_ids:
        return []
    out: list[dict[str, Any]] = []
    saw_account_column = False
    for row in rows:
        acc_id = str(
            _pick(
                row,
                "acc_id",
                "account_id",
                "trade_acc_id",
                "trd_acc_id",
                "accID",
            )
            or ""
        ).strip()
        if acc_id:
            saw_account_column = True
            if acc_id in account_ids:
                out.append(row)
            continue
        out.append(row)
    return out if saw_account_column else rows


def _query_rows_for_account_id(gateway: Any, method_name: str, account_id: str) -> list[dict[str, Any]]:
    method = getattr(gateway, method_name)
    try:
        return _rows(method(acc_id=_to_futu_acc_id(account_id)))
    except Exception as exc:
        raise ValueError(
            f"{method_name} failed for mapped account_id={account_id} via acc_id selector"
        ) from exc


def _query_rows_for_account_ids(gateway: Any, method_name: str, account_ids: set[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for account_id in sorted(account_ids):
        rows.extend(_query_rows_for_account_id(gateway, method_name, account_id))
    return rows


def build_futu_portfolio_context(
    *,
    balance_rows: list[dict[str, Any]],
    position_rows: list[dict[str, Any]],
    account: str | None,
    market: str = "富途",
    base_currency: str = "CNY",
) -> dict[str, Any]:
    cash_by_currency: dict[str, float] = {}
    stocks_by_symbol: dict[str, dict[str, Any]] = {}

    base_ccy = _normalize_currency(base_currency, fallback="CNY")
    for row in balance_rows:
        ccy = _normalize_currency(
            _pick(row, "currency", "cash_currency", "currency_code", "ccy"),
            fallback=base_ccy,
        )
        cash_v = _to_float(_pick(row, "cash", "available_funds", "withdraw_cash", "power"))
        fund_v = _to_float(_pick(row, "fund_assets", "mmf_assets", "money_fund_assets"))
        total = 0.0
        has_value = False
        if cash_v is not None:
            total += float(cash_v)
            has_value = True
        if fund_v is not None:
            total += float(fund_v)
            has_value = True
        if has_value:
            cash_by_currency[ccy] = cash_by_currency.get(ccy, 0.0) + total

    for row in position_rows:
        symbol = _normalize_symbol(_pick(row, "code", "symbol", "stock_code", "asset_id"))
        if not symbol:
            continue

        shares = _to_int(_pick(row, "qty", "quantity", "hold_qty", "shares"))
        if shares is None or shares <= 0:
            continue

        avg_cost = _to_float(_pick(row, "cost_price", "average_cost", "avg_cost", "cost"))
        currency = _normalize_currency(
            _pick(row, "currency", "currency_code", "ccy"),
            fallback=(symbol_currency(symbol) or base_ccy),
        )
        name = str(_pick(row, "stock_name", "name", "asset_name") or "").strip() or None

        existing = stocks_by_symbol.get(symbol)
        if existing is None:
            stocks_by_symbol[symbol] = {
                "symbol": symbol,
                "name": name,
                "shares": shares,
                "avg_cost": avg_cost,
                "currency": currency,
                "broker": str(market),
                "account": (normalize_account(account) if account else ""),
            }
            continue

        prev_shares = int(existing.get("shares") or 0)
        new_shares = prev_shares + shares
        prev_cost = _to_float(existing.get("avg_cost"))
        if prev_cost is not None and avg_cost is not None and new_shares > 0:
            weighted = ((prev_cost * prev_shares) + (avg_cost * shares)) / float(new_shares)
            existing["avg_cost"] = weighted
        elif existing.get("avg_cost") in (None, "") and avg_cost is not None:
            existing["avg_cost"] = avg_cost
        existing["shares"] = new_shares
        if not existing.get("name") and name:
            existing["name"] = name

    return {
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "filters": {"broker": str(market), "account": account},
        "cash_by_currency": cash_by_currency,
        "stocks_by_symbol": stocks_by_symbol,
        "raw_selected_count": len(balance_rows) + len(position_rows),
        "portfolio_source_name": "futu",
    }


def fetch_futu_portfolio_context(
    *,
    cfg: Mapping[str, Any] | Any,
    account: str | None,
    market: str = "富途",
    base_currency: str = "CNY",
) -> dict[str, Any]:
    if not account:
        raise ValueError("futu portfolio context requires account")

    settings = infer_futu_portfolio_settings(cfg, account=account)
    host = settings.get("host")
    port = settings.get("port")
    if not host or not port:
        raise ValueError("futu portfolio settings missing host/port")

    account_ids = set(resolve_trade_intake_futu_account_ids(cfg, account=account))
    if not account_ids:
        raise ValueError(f"no futu account mapping for account={account}")

    gateway = build_ready_futu_gateway(
        host=str(host),
        port=int(port),
        is_option_chain_cache_enabled=False,
    )
    try:
        balance_rows = _query_rows_for_account_ids(gateway, "get_account_balance", account_ids)
        position_rows = _query_rows_for_account_ids(gateway, "get_positions", account_ids)
    finally:
        gateway.close()

    balance_rows = _filter_rows_for_account_ids(balance_rows, account_ids)
    position_rows = _filter_rows_for_account_ids(position_rows, account_ids)

    return build_futu_portfolio_context(
        balance_rows=balance_rows,
        position_rows=position_rows,
        account=account,
        market=market,
        base_currency=base_currency,
    )
