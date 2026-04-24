from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from scripts.account_config import build_account_portfolio_source_plan
from scripts.fetch_portfolio_context import (
    load_holdings_portfolio_context,
    load_holdings_portfolio_shared_context,
    slice_shared_context_for_account,
)


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

    cached = None
    try:
        if ttl_sec > 0 and is_fresh_fn(port_path, ttl_sec):
            cached = load_json_fn(port_path)
    except Exception:
        cached = None

    if isinstance(cached, dict) and str(cached.get("portfolio_source_name") or "").strip().lower() == plan.primary_source:
        cached = with_context_source(cached, "account_cache")
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
            ctx = with_context_source(ctx, "futu_direct")
            port_path.parent.mkdir(parents=True, exist_ok=True)
            port_path.write_text(json.dumps(ctx, ensure_ascii=False, indent=2), encoding="utf-8")
            log(f"[CTX] portfolio_context source=futu_direct account={account or '-'}")
            return ctx
        except Exception as exc:
            if plan.fallback_source != "holdings":
                raise
            log(f"[WARN] futu portfolio context unavailable; fallback to holdings: {exc}")
            if isinstance(cached, dict) and str(cached.get("portfolio_source_name") or "").strip().lower() == "holdings":
                cached = with_context_source(cached, "account_cache")
                log(f"[CTX] portfolio_context source=account_cache account={account or '-'}")
                return cached

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
                    sliced = dict(sliced)
                    sliced["portfolio_source_name"] = "holdings"
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
                    pm_config_path=Path(data_config),
                    broker=str(market),
                )
                shared_out.write_text(json.dumps(shared_ctx, ensure_ascii=False, indent=2), encoding="utf-8")
                ctx = dict(slice_shared_context_for_account(shared_ctx, holdings_account) or {})
            else:
                ctx = load_holdings_portfolio_context(
                    pm_config_path=Path(data_config),
                    broker=str(market),
                    account=holdings_account,
                )
            if not ctx:
                ctx = dict(_load_json_payload(load_json_fn, port_path))
            ctx["portfolio_source_name"] = "holdings"
            ctx = with_context_source(ctx, context_source)
            port_path.parent.mkdir(parents=True, exist_ok=True)
            port_path.write_text(json.dumps(ctx, ensure_ascii=False, indent=2), encoding="utf-8")
            log(f"[CTX] portfolio_context source={context_source} account={holdings_account or '-'}")
            return ctx
        except Exception:
            if shared_out is None:
                raise
    raise RuntimeError("unreachable")
