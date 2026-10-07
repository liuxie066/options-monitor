from __future__ import annotations

import json
from concurrent.futures import CancelledError
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from domain.domain.risk_capacity import evaluate_cash_snapshot
from domain.domain.position_snapshot import (
    normalize_persisted_position_snapshot_input,
    position_snapshot_scope_errors,
)
from src.application.runtime_paths import resolve_runtime_root
from src.application.account_config import build_account_portfolio_source_plan, resolve_futu_account_ids
from src.application.config_defaults import cash_snapshot_ttl_sec
from src.application.futu_portfolio_context import infer_futu_portfolio_settings, _runtime_market
from src.infrastructure.exchange_rates import exchange_rate_observation_status, project_exchange_rate_snapshot, shared_exchange_rate_cache_path
from src.infrastructure.io_utils import atomic_write_json

_FX_NOT_PROVIDED = object()


JsonLoader = Callable[[Path], Optional[dict]]
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


def cash_snapshot_is_usable(context: Mapping[str, Any]) -> bool:
    """Consume a well-formed shared verdict; never reconstruct one from legacy flags."""
    snapshot = context.get("cash_snapshot")
    if not isinstance(snapshot, Mapping) or set(snapshot) != {
        "status", "reason_codes", "source_observed_at", "evaluated_at", "max_age_sec",
    }:
        return False
    if (snapshot["status"] != "fresh" or snapshot["reason_codes"] != []
            or type(snapshot["max_age_sec"]) is not int or snapshot["max_age_sec"] <= 0):
        return False
    for key in ("source_observed_at", "evaluated_at"):
        try:
            value = snapshot[key]
            if not isinstance(value, str) or datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() is None:
                return False
        except (ValueError, TypeError, OverflowError):
            return False
    return True


def evaluate_account_cash_snapshot(
    context: Mapping[str, Any], *, config: Mapping[str, Any], account: str | None,
    evaluated_at: datetime,
) -> dict[str, Any]:
    """Evaluate retained or live cash with the same effective account policy."""
    try:
        ttl = cash_snapshot_ttl_sec(config)
    except ValueError:
        ttl = None
    try:
        ids = resolve_futu_account_ids(config, account=account)
        settings = infer_futu_portfolio_settings(config, account=account)
    except ValueError:
        ids, settings = [], {}
    portfolio_cfg = config.get("portfolio") or {}
    base_currency = str(portfolio_cfg.get("base_currency") or "CNY")
    expected = {
        "logical_account": str(account or "").strip().lower(),
        "futu_account_id": str(ids[0]) if len(ids) == 1 else None,
        "trd_env": settings.get("trd_env"),
        "market": _runtime_market(config, fallback=base_currency),
    }

    return evaluate_cash_snapshot(
        context, expected_authority=expected, evaluated_at=evaluated_at, max_age_sec=ttl,
    )


def cash_snapshot_evidence(context: Mapping[str, Any]) -> dict[str, Any]:
    """Stable decision inputs; evaluation time and the derived verdict are excluded."""
    keys = (
        "portfolio_source_name", "capacity_authority", "capacity_identity_hash",
        "filters", "source_account_identifiers", "cash_by_currency",
        "cash_balance_reliable", "cash_balance_unavailable_by_row",
        "cash_source_observed_at", "cash_source_observation_status",
        "source_observation_status", "cash_provider_error",
    )
    snapshot = context.get("cash_snapshot")
    return {**{key: deepcopy(context[key]) for key in keys if key in context},
            "max_age_sec": snapshot.get("max_age_sec") if isinstance(snapshot, Mapping) else None}


def load_account_portfolio_context(
    *,
    market: str,
    account: str | None,
    state_dir: Path,
    log: Logger,
    runtime_config: dict[str, Any] | None,
    portfolio_source: str | None,
    fetch_futu_portfolio_context_fn: Callable[..., dict[str, Any]],
    load_json_fn: JsonLoader = _read_json_from_path,
    write_cache: bool = True,
    exchange_rate_observation: Mapping[str, Any] | None | object = _FX_NOT_PROVIDED,
    exchange_rate_cache_path: Path | None = None,
    include_options: bool = False,
    required_position_asset_types: tuple[str, ...] = (),
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    config = runtime_config or {}
    port_path = (state_dir / "portfolio_context.json").resolve()
    build_account_portfolio_source_plan(config, account=account, portfolio_source=portfolio_source)
    ttl = None
    try:
        ttl = cash_snapshot_ttl_sec(config)
    except ValueError as exc:
        log(f"[CTX] cash config unavailable: {exc}")

    def evaluate(context: dict[str, Any], source: str) -> dict[str, Any]:
        result = deepcopy(context)
        result["context_source"] = source
        result["cash_snapshot"] = evaluate_account_cash_snapshot(
            result, config=config, account=account,
            evaluated_at=now_utc or datetime.now(timezone.utc),
        )
        fx = exchange_rate_observation if exchange_rate_observation is not _FX_NOT_PROVIDED else result.get("exchange_rates")
        result["exchange_rates"] = project_exchange_rate_snapshot(fx, purpose="capacity", now=now_utc) if isinstance(fx, Mapping) else None
        result["exchange_rate_status"] = exchange_rate_observation_status(result["exchange_rates"])
        return result

    if ttl is None:
        return evaluate({}, "unavailable")
    try:
        cached = load_json_fn(port_path)
    except (TimeoutError, CancelledError):
        raise
    except Exception:
        cached = None
    if isinstance(cached, dict):
        cached_snapshot = cached.get("position_snapshot_input")
        if isinstance(cached_snapshot, Mapping):
            cached = deepcopy(cached)
            cached["position_snapshot_input"] = (
                normalize_persisted_position_snapshot_input(cached_snapshot)
            )
        result = evaluate(cached, "account_cache")
        assets = required_position_asset_types or (("stock", "option") if include_options else ())
        snapshot = result.get("position_snapshot_input")
        snapshot = snapshot if isinstance(snapshot, Mapping) else {}
        authority = result.get("capacity_authority")
        authority = authority if isinstance(authority, Mapping) else {}
        position_errors = [
            error for asset in assets if cash_snapshot_is_usable(result)
            for error in position_snapshot_scope_errors(
                snapshot, account_label=str(account or "").lower(),
                external_account_id=authority.get("futu_account_id"), environment=str(authority.get("trd_env") or ""),
                market=authority.get("market"), asset_type=asset,
                now_utc=now_utc or datetime.now(timezone.utc),
                max_age_seconds=60 if include_options else 300,
            )
        ]
        account_valid = _validate_portfolio_context_account(result, requested_account=account, log=log, source="account_cache")
        if cash_snapshot_is_usable(result) and not position_errors and account_valid:
            log(f"[CTX] portfolio_context source=account_cache account={account or '-'}")
            return result
    kwargs: dict[str, Any] = {
        "cfg": config, "account": account, "market": str(market), "base_currency": str((config.get("portfolio") or {}).get("base_currency") or "CNY"),
        "write_cache": write_cache,
        "exchange_rate_cache_path": exchange_rate_cache_path or shared_exchange_rate_cache_path(resolve_runtime_root(repo_root=Path(__file__).resolve().parents[2]).runtime_root),
    }
    if include_options:
        kwargs["include_options"] = True
    if exchange_rate_observation is not _FX_NOT_PROVIDED:
        kwargs["exchange_rate_observation"] = exchange_rate_observation
    try:
        context = fetch_futu_portfolio_context_fn(**kwargs)
        if not isinstance(context, dict):
            raise ValueError("portfolio response is not an object")
    except (TimeoutError, CancelledError):
        raise
    except Exception as exc:
        log(f"[WARN] portfolio cash provider unavailable: {exc}")
        return evaluate({"cash_provider_error": type(exc).__name__}, "unavailable")
    result = evaluate(context, "futu_direct")
    if not _validate_portfolio_context_account(result, requested_account=account, log=log, source="futu_direct"):
        raise ValueError("futu_direct account mismatch")
    if write_cache:
        atomic_write_json(port_path, result)
    log(f"[CTX] portfolio_context source=futu_direct account={account or '-'}")
    return result
