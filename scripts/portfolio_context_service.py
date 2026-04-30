from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from scripts.account_config import build_account_portfolio_source_plan
import scripts.fetch_portfolio_context as holdings_context

load_holdings_portfolio_context = holdings_context.load_holdings_portfolio_context
load_holdings_portfolio_shared_context = holdings_context.load_holdings_portfolio_shared_context
slice_shared_context_for_account = holdings_context.slice_shared_context_for_account


JsonLoader = Callable[[Path], dict | None]
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


def _portfolio_context_account_mismatch_reason(ctx: dict[str, Any], *, requested_account: str | None) -> str | None:
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
    mismatch = _portfolio_context_account_mismatch_reason(ctx, requested_account=requested_account)
    if mismatch is None:
        return True
    log(f"[CTX] portfolio_context cache rejected due to account mismatch source={source} {mismatch}")
    return False


def _expected_context_account(*, source_name: str, account: str | None, holdings_account: str | None) -> str | None:
    source_norm = str(source_name or "").strip().lower()
    if source_norm == "futu":
        return str(account or "").strip() or None
    return str(holdings_account or account or "").strip() or None


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
) -> dict[str, Any]:
    port_path = (state_dir / "portfolio_context.json").resolve()
    plan = build_account_portfolio_source_plan(runtime_config, account=account, portfolio_source=portfolio_source)
    holdings_source_name = "external_holdings" if plan.primary_source == "external_holdings" else "holdings"
    allow_holdings_fallback = (
        plan.primary_source in {"futu", "external_holdings"} and str(plan.requested_source or "").strip().lower() == "auto"
    ) or plan.primary_source == "external_holdings"

    cached = None
    try:
        if ttl_sec > 0 and is_fresh_fn(port_path, ttl_sec):
            cached = load_json_fn(port_path)
    except Exception:
        cached = None

    cached_source = str((cached or {}).get("portfolio_source_name") or "").strip().lower() if isinstance(cached, dict) else ""
    if isinstance(cached, dict):
        if cached_source == plan.primary_source:
            expected_account = _expected_context_account(
                source_name=cached_source,
                account=account,
                holdings_account=plan.holdings_account,
            )
            if _validate_portfolio_context_account(cached, requested_account=expected_account, log=log, source="account_cache"):
                cached = with_context_source(cached, "account_cache")
                log(f"[CTX] portfolio_context source=account_cache account={account or '-'}")
                return cached
        if plan.primary_source == "external_holdings" and cached_source in {"holdings", "external_holdings"}:
            expected_account = _expected_context_account(
                source_name=cached_source,
                account=account,
                holdings_account=plan.holdings_account,
            )
            if _validate_portfolio_context_account(cached, requested_account=expected_account, log=log, source="account_cache"):
                cached = with_context_source(cached, "account_cache")
                log(f"[CTX] portfolio_context fallback to holdings account={account or '-'} source=account_cache")
                log(f"[CTX] portfolio_context source=account_cache account={account or '-'}")
                return cached

    portfolio_cfg = (runtime_config.get("portfolio") or {}) if isinstance(runtime_config, dict) else {}
    if plan.primary_source == "futu":
        try:
            ctx = fetch_futu_portfolio_context_fn(
                cfg=(runtime_config or {}),
                account=account,
                market=str(market),
                base_currency=str(portfolio_cfg.get("base_currency") or "CNY"),
                )
            ctx = dict(ctx)
            ctx["portfolio_source_name"] = "futu"
            expected_account = _expected_context_account(
                source_name="futu",
                account=account,
                holdings_account=plan.holdings_account,
            )
            if not _validate_portfolio_context_account(ctx, requested_account=expected_account, log=log, source="futu_direct"):
                raise ValueError("futu_direct account mismatch")
            ctx = with_context_source(ctx, "futu_direct")
            port_path.parent.mkdir(parents=True, exist_ok=True)
            port_path.write_text(json.dumps(ctx, ensure_ascii=False, indent=2), encoding="utf-8")
            log(f"[CTX] portfolio_context source=futu_direct account={account or '-'}")
            return ctx
        except Exception as exc:
            if not allow_holdings_fallback:
                raise
            log(f"[CTX] portfolio_context fallback to holdings account={account or '-'} error={exc}")

    holdings_account = plan.holdings_account
    shared_root = (shared_state_dir or state_dir).resolve()
    shared_root.mkdir(parents=True, exist_ok=True)
    shared_path = (shared_root / "portfolio_context.shared.json").resolve()

    try:
        if ttl_sec > 0 and is_fresh_fn(shared_path, ttl_sec):
            shared_cached = load_json_fn(shared_path)
            if isinstance(shared_cached, dict):
                sliced = slice_shared_context_for_account(shared_cached, holdings_account)
                if isinstance(sliced, dict):
                    expected_account = _expected_context_account(
                        source_name=holdings_source_name,
                        account=account,
                        holdings_account=holdings_account,
                    )
                    if not _validate_portfolio_context_account(sliced, requested_account=expected_account, log=log, source="shared_slice"):
                        raise ValueError("shared slice account mismatch")
                    sliced = dict(sliced)
                    sliced["portfolio_source_name"] = holdings_source_name
                    sliced = with_context_source(sliced, "shared_slice")
                    port_path.parent.mkdir(parents=True, exist_ok=True)
                    port_path.write_text(json.dumps(sliced, ensure_ascii=False, indent=2), encoding="utf-8")
                    log(f"[CTX] portfolio_context source=shared_slice account={holdings_account or '-'}")
                    return sliced
    except Exception:
        pass

    for shared_out, context_source in ((shared_path, "shared_refresh"), (None, "direct_fetch")):
        try:
            if shared_out is not None:
                shared_ctx = load_holdings_portfolio_shared_context(
                    data_config_path=Path(data_config),
                    broker=str(market),
                )
                shared_out.write_text(json.dumps(shared_ctx, ensure_ascii=False, indent=2), encoding="utf-8")
                ctx = dict(slice_shared_context_for_account(shared_ctx, holdings_account) or {})
            else:
                ctx = load_holdings_portfolio_context(
                    data_config_path=Path(data_config),
                    broker=str(market),
                    account=holdings_account,
                )
            if not ctx:
                ctx = dict(_load_json_payload(load_json_fn, port_path))
            expected_account = _expected_context_account(
                source_name=holdings_source_name,
                account=account,
                holdings_account=holdings_account,
            )
            if not _validate_portfolio_context_account(ctx, requested_account=expected_account, log=log, source=context_source):
                raise ValueError(f"{context_source} account mismatch")
            ctx["portfolio_source_name"] = holdings_source_name
            ctx = with_context_source(ctx, context_source)
            port_path.parent.mkdir(parents=True, exist_ok=True)
            port_path.write_text(json.dumps(ctx, ensure_ascii=False, indent=2), encoding="utf-8")
            log(f"[CTX] portfolio_context source={context_source} account={holdings_account or '-'}")
            return ctx
        except Exception:
            if shared_out is None:
                if allow_holdings_fallback:
                    cached_fallback = _load_json_payload(load_json_fn, port_path)
                    expected_account = _expected_context_account(
                        source_name=holdings_source_name,
                        account=account,
                        holdings_account=holdings_account,
                    )
                    if not _validate_portfolio_context_account(
                        cached_fallback,
                        requested_account=expected_account,
                        log=log,
                        source="account_cache",
                    ):
                        raise
                    cached_fallback["portfolio_source_name"] = holdings_source_name
                    cached_fallback = with_context_source(cached_fallback, "account_cache")
                    log(f"[CTX] portfolio_context fallback to holdings account={account or '-'} source=account_cache")
                    log(f"[CTX] portfolio_context source=account_cache account={account or '-'}")
                    return cached_fallback
                raise
    raise RuntimeError("unreachable")
