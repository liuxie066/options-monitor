from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from domain.domain.fetch_source import is_futu_fetch_source, normalize_fetch_source
from src.infrastructure.futu_gateway import build_ready_futu_gateway
from domain.domain.ledger.position_fields import normalize_account
from domain.domain.option_position_identity import normalize_currency
from domain.domain.symbol_identity import (
    canonical_symbol,
    looks_like_option_contract_label,
    symbol_currency,
)
from src.application.account_config import resolve_futu_account_ids


_DEFAULT_TRD_ENV = "REAL"
_VALID_TRD_ENVS = {"REAL", "SIMULATE"}
_LONG_POSITION_SIDE = "LONG"
_NON_STOCK_SEC_TYPES = {"DRVT", "FUTURE", "IDX", "NONE", "N/A"}
_FUTU_CASH_FIELDS_BY_CCY = {
    "HKD": ("hk_cash",),
    "USD": ("us_cash",),
    "CNY": ("cn_cash",),
    "JPY": ("jp_cash",),
    "SGD": ("sg_cash",),
    "AUD": ("au_cash",),
    "CAD": ("ca_cash",),
    "MYR": ("my_cash",),
}
_FUTU_NET_CASH_POWER_FIELDS_BY_CCY = {
    "HKD": ("hkd_net_cash_power",),
    "USD": ("usd_net_cash_power",),
    "CNY": ("cnh_net_cash_power",),
    "JPY": ("jpy_net_cash_power",),
    "SGD": ("sgd_net_cash_power",),
    "AUD": ("aud_net_cash_power",),
    "CAD": ("cad_net_cash_power",),
    "MYR": ("myr_net_cash_power",),
}
_FUTU_FUND_ASSET_FIELDS = ("fund_assets", "mmf_assets", "money_fund_assets")


def _resolve_trd_env(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if raw in _VALID_TRD_ENVS:
        return raw
    return _DEFAULT_TRD_ENV


def _row_trd_env(row: Mapping[str, Any]) -> str | None:
    raw = _pick(row, "trd_env", "trdEnv", "trade_env", "tradeEnv")
    if raw in (None, ""):
        return None
    return str(raw).strip().upper()


def _is_long_position(row: Mapping[str, Any]) -> bool:
    side = _pick(row, "position_side", "positionSide", "side")
    if side in (None, ""):
        return True
    return str(side).strip().upper() == _LONG_POSITION_SIDE


def _looks_like_option_code(code: Any) -> bool:
    return looks_like_option_contract_label(code)


def _row_looks_like_option_position(row: Mapping[str, Any]) -> bool:
    for key in ("code", "symbol", "stock_code", "asset_id", "stock_name", "name", "asset_name"):
        if _looks_like_option_code(row.get(key)):
            return True
    return False


def _is_stock_position(row: Mapping[str, Any]) -> bool:
    if _row_looks_like_option_position(row):
        return False
    sec_type = _pick(row, "sec_type", "secType", "security_type")
    if sec_type in (None, ""):
        return True
    return str(sec_type).strip().upper() not in _NON_STOCK_SEC_TYPES


def _dedup_balance_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        acc = str(_pick(row, "acc_id", "account_id", "trd_acc_id", "trade_acc_id", "accID") or "").strip()
        if not acc:
            out.append(row)
            continue
        env = (_row_trd_env(row) or "").strip()
        ccy = str(_pick(row, "currency", "cash_currency", "currency_code", "ccy") or "").strip().upper()
        key = (acc, env, ccy)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


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


def _add_cash_component(
    cash_by_currency: dict[str, float],
    components_by_currency: dict[str, dict[str, float]],
    *,
    currency: str,
    source: str,
    value: float | None,
) -> bool:
    if value is None:
        return False
    ccy = _normalize_currency(currency, fallback="")
    if not ccy:
        return False
    amount = float(value)
    if amount:
        cash_by_currency[ccy] = cash_by_currency.get(ccy, 0.0) + amount
    components = components_by_currency.setdefault(ccy, {})
    components[source] = components.get(source, 0.0) + amount
    return True


def _extract_cash_components(
    row: Mapping[str, Any],
    *,
    base_currency: str,
) -> tuple[list[tuple[str, str, float]], str]:
    """Return cash-like components from a Futu accinfo row.

    OpenD still exposes legacy aggregate ``cash``, but that value is ambiguous
    for multi-currency accounts. Only explicit currency cash/fund fields are
    accepted here.
    """
    row_currency = _normalize_currency(
        _pick(row, "currency", "cash_currency", "currency_code", "ccy"),
        fallback=base_currency,
    )
    components: list[tuple[str, str, float]] = []

    fund_value = _to_float(_pick(row, *_FUTU_FUND_ASSET_FIELDS))
    if fund_value is not None:
        components.append((row_currency, "fund_assets", fund_value))

    for currency, fields in _FUTU_CASH_FIELDS_BY_CCY.items():
        value = _to_float(_pick(row, *fields))
        if value is not None:
            components.append((currency, fields[0], value))

    if components:
        return components, "futu_cash_like_assets"
    return [], "empty"


def _extract_net_cash_power(row: Mapping[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for currency, fields in _FUTU_NET_CASH_POWER_FIELDS_BY_CCY.items():
        value = _to_float(_pick(row, *fields))
        if value is None:
            continue
        out[_normalize_currency(currency, fallback=currency)] = float(value)
    return out


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


def _filter_rows_for_account_ids(
    rows: list[dict[str, Any]],
    account_ids: set[str],
    *,
    trd_env: str | None = None,
) -> list[dict[str, Any]]:
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
        if not acc_id:
            out.append(row)
            continue
        saw_account_column = True
        if acc_id not in account_ids:
            continue
        row_env = _row_trd_env(row)
        if trd_env and row_env and row_env != trd_env:
            continue
        out.append(row)
    return out if saw_account_column else rows


def _query_rows_for_account_id(
    gateway: Any,
    method_name: str,
    account_id: str,
    *,
    trd_env: str | None = None,
) -> list[dict[str, Any]]:
    method = getattr(gateway, method_name)
    try:
        kwargs: dict[str, Any] = {"acc_id": _to_futu_acc_id(account_id)}
        if trd_env:
            kwargs["trd_env"] = trd_env
        return _rows(method(**kwargs))
    except Exception as exc:
        raise ValueError(
            f"{method_name} failed for mapped account_id={account_id} via acc_id selector"
        ) from exc


def _query_rows_for_account_ids(
    gateway: Any,
    method_name: str,
    account_ids: set[str],
    *,
    trd_env: str | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for account_id in sorted(account_ids):
        rows.extend(
            _query_rows_for_account_id(gateway, method_name, account_id, trd_env=trd_env)
        )
    return rows


def build_futu_portfolio_context(
    *,
    balance_rows: list[dict[str, Any]],
    position_rows: list[dict[str, Any]],
    account: str | None,
    market: str = "富途",
    base_currency: str = "CNY",
    source_observed_at: str | None = None,
    broker_account_identifiers: set[str] | list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    cash_by_currency: dict[str, float] = {}
    cash_components_by_currency: dict[str, dict[str, float]] = {}
    cash_power_by_currency: dict[str, float] = {}
    cash_source_kinds: set[str] = set()
    stocks_by_symbol: dict[str, dict[str, Any]] = {}
    stock_cost_basis: dict[str, dict[str, float | int]] = {}

    base_ccy = _normalize_currency(base_currency, fallback="CNY")
    for row in _dedup_balance_rows(balance_rows):
        components, source_kind = _extract_cash_components(row, base_currency=base_ccy)
        if source_kind != "empty":
            cash_source_kinds.add(source_kind)
        for currency, source, amount in components:
            _add_cash_component(
                cash_by_currency,
                cash_components_by_currency,
                currency=currency,
                source=source,
                value=amount,
            )

        for currency, amount in _extract_net_cash_power(row).items():
            cash_power_by_currency[currency] = cash_power_by_currency.get(currency, 0.0) + amount

    for row in position_rows:
        if not _is_long_position(row):
            continue
        if not _is_stock_position(row):
            continue
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
            known_shares = shares if avg_cost is not None else 0
            unknown_shares = 0 if avg_cost is not None else shares
            stocks_by_symbol[symbol] = {
                "symbol": symbol,
                "name": name,
                "shares": shares,
                "avg_cost": avg_cost if unknown_shares == 0 else None,
                "cost_basis_complete": unknown_shares == 0,
                "cost_known_shares": known_shares,
                "cost_unknown_shares": unknown_shares,
                "currency": currency,
                "broker": str(market),
                "account": (normalize_account(account) if account else ""),
            }
            stock_cost_basis[symbol] = {
                "known_shares": known_shares,
                "unknown_shares": unknown_shares,
                "known_cost_total": float(avg_cost or 0.0) * known_shares,
            }
            continue

        new_shares = int(existing.get("shares") or 0) + shares
        basis = stock_cost_basis[symbol]
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
        if not existing.get("name") and name:
            existing["name"] = name

    observed_at = str(source_observed_at or datetime.now(timezone.utc).isoformat())
    identifiers = sorted(
        {str(item or "").strip() for item in broker_account_identifiers if str(item or "").strip()}
    )
    return {
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "source_observed_at": observed_at,
        "source_account_identifiers": identifiers,
        "filters": {"broker": str(market), "account": account},
        "cash_by_currency": cash_by_currency,
        "cash_components_by_currency": cash_components_by_currency,
        "cash_source": (
            "mixed" if len(cash_source_kinds) > 1 else next(iter(cash_source_kinds), "empty")
        ),
        "cash_power_by_currency": cash_power_by_currency,
        "cash_power_source": "futu_net_cash_power",
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
    trd_env = _resolve_trd_env(settings.get("trd_env"))

    account_ids = set(resolve_futu_account_ids(cfg, account=account))
    if not account_ids:
        raise ValueError(f"no futu account_id for account={account}")

    gateway = build_ready_futu_gateway(
        host=str(host),
        port=int(port),
        is_option_chain_cache_enabled=False,
    )
    try:
        balance_rows = _query_rows_for_account_ids(
            gateway, "get_account_balance", account_ids, trd_env=trd_env
        )
        position_rows = _query_rows_for_account_ids(
            gateway, "get_positions", account_ids, trd_env=trd_env
        )
    finally:
        gateway.close()

    balance_rows = _filter_rows_for_account_ids(balance_rows, account_ids, trd_env=trd_env)
    position_rows = _filter_rows_for_account_ids(position_rows, account_ids, trd_env=trd_env)
    source_observed_at = datetime.now(timezone.utc).isoformat()

    return build_futu_portfolio_context(
        balance_rows=balance_rows,
        position_rows=position_rows,
        account=account,
        market=market,
        base_currency=base_currency,
        source_observed_at=source_observed_at,
        broker_account_identifiers=account_ids,
    )
