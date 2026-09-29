from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

from src.application.account_config import build_account_portfolio_source_plan
from src.application.portfolio_context_builder import load_holdings_portfolio_shared_context


JsonLoader = Callable[[Path], Optional[dict]]
FreshnessChecker = Callable[[Path, int], bool]
Logger = Callable[[str], None]


def with_context_source(ctx: dict[str, Any], source: str) -> dict[str, Any]:
    out = dict(ctx)
    out["context_source"] = str(source)
    return out


def _read_json_from_path(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _load_json_payload(load_json_fn: JsonLoader, path: Path) -> dict[str, Any]:
    payload = load_json_fn(path)
    if isinstance(payload, dict) and payload:
        return payload
    return _read_json_from_path(path)


def portfolio_context_account_mismatch_reason(
    ctx: dict[str, Any],
    *,
    requested_account: str | None,
) -> str | None:
    account_norm = str(requested_account or "").strip()
    if not account_norm or not isinstance(ctx, dict):
        return None

    filters = ctx.get("filters")
    if isinstance(filters, dict):
        cached_account = str(filters.get("account") or "").strip()
        if cached_account and cached_account != account_norm:
            return f"filters.account requested={account_norm} cached={cached_account}"

    stocks = ctx.get("stocks_by_symbol")
    if not isinstance(stocks, dict):
        return None
    for symbol, row in stocks.items():
        if not isinstance(row, dict):
            continue
        stock_account = str(row.get("account") or "").strip()
        if stock_account and stock_account != account_norm:
            return f"stocks_by_symbol[{symbol}].account requested={account_norm} cached={stock_account}"
    return None


def _validate_portfolio_context_account(
    ctx: dict[str, Any],
    *,
    requested_account: str | None,
    log: Logger,
    source: str,
) -> bool:
    mismatch = portfolio_context_account_mismatch_reason(
        ctx,
        requested_account=requested_account,
    )
    if mismatch is None:
        return True
    log(f"[CTX] portfolio_context cache rejected due to account mismatch source={source} {mismatch}")
    return False


def load_account_portfolio_context(
    *,
    base: Path,
    data_config: str,
    market: str,
    account: str | None,
    ttl_sec: int,
    state_dir: Path,
    shared_state_dir: Path | None,
    log: Logger,
    runtime_config: dict[str, Any] | None,
    portfolio_source: str | None,
    fetch_futu_portfolio_context_fn: Callable[..., dict[str, Any]],
    is_fresh_fn: FreshnessChecker,
    load_json_fn: JsonLoader,
    write_cache: bool = True,
) -> dict[str, Any]:
    port_path = (state_dir / "portfolio_context.json").resolve()
    build_account_portfolio_source_plan(runtime_config, account=account, portfolio_source=portfolio_source)

    cached = None
    try:
        if ttl_sec > 0 and is_fresh_fn(port_path, ttl_sec):
            cached = load_json_fn(port_path)
    except Exception:
        cached = None

    cached_source = str((cached or {}).get("portfolio_source_name") or "").strip().lower() if isinstance(cached, dict) else ""
    if isinstance(cached, dict):
        cached_filters = cached.get("filters")
        if cached_source == "futu":
            expected_account = str(account or "").strip().lower() or None
            if _validate_portfolio_context_account(cached, requested_account=expected_account, log=log, source="account_cache") and isinstance(cached_filters, dict) and str(cached_filters.get("account") or "").strip().lower() == str(account or "").strip().lower():
                cached = with_context_source(cached, "account_cache")
                log(f"[CTX] portfolio_context source=account_cache account={account or '-'}")
                return cached
    portfolio_cfg = (runtime_config.get("portfolio") or {}) if isinstance(runtime_config, dict) else {}
    ctx = fetch_futu_portfolio_context_fn(
        cfg=(runtime_config or {}),
        account=account,
        market=str(market),
        base_currency=str(portfolio_cfg.get("base_currency") or "CNY"),
    )
    ctx = dict(ctx)
    ctx["portfolio_source_name"] = "futu"
    expected_account = str(account or "").strip().lower() or None
    if not _validate_portfolio_context_account(ctx, requested_account=expected_account, log=log, source="futu_direct"):
        raise ValueError("futu_direct account mismatch")
    ctx = with_context_source(ctx, "futu_direct")
    if write_cache:
        port_path.parent.mkdir(parents=True, exist_ok=True)
        port_path.write_text(json.dumps(ctx, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"[CTX] portfolio_context source=futu_direct account={account or '-'}")
    return ctx
