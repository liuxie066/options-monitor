#!/usr/bin/env python3
from __future__ import annotations

"""Pipeline context loading (portfolio/position-lots/exchange rates).

Stage 3 refactor target:
- keep unified scan entrypoint thin (orchestration only)
- move context fetch/caching logic into cohesive module

Design constraints:
- minimal/no behavior change
- best-effort context (should not fail the whole pipeline in scheduled mode)
"""

import json
from pathlib import Path

from src.application.account_config import build_account_portfolio_source_plan
from src.application.config_loader import resolve_data_config_path
from src.application.positions.context_builder import (
    build_context as build_option_positions_context,
    build_shared_context as build_shared_option_positions_context,
    validate_option_positions_context_account,
)
from src.application.futu_portfolio_context import fetch_futu_portfolio_context
from src.infrastructure.io_utils import atomic_write_json, is_fresh, load_cached_json
from src.application.ledger.api import (
    decision_state_snapshot,
    list_position_lot_snapshots,
    open_position_ledger,
)
from domain.domain.portfolio_scope import portfolio_scope_id
from src.application.portfolio_context_service import (
    load_account_portfolio_context,
    load_holdings_portfolio_shared_context,
    with_context_source,
)
from src.application.prepared_portfolio_context import (
    PreparedPortfolioContextError,
    load_prepared_portfolio_context,
)
from src.application.prepared_option_positions_context import (
    PreparedOptionPositionsContextError,
    exchange_rate_scalars_from_option_context,
    load_prepared_option_positions_context,
)
from domain.services import adapt_holdings_context, adapt_option_positions_context
from src.application.positions.context_builder import slice_shared_context_for_account as slice_shared_option_context_for_account
from domain.storage.repositories import state_repo
from src.application.strategy_policy import (
    SELL_CALL_FAMILY,
    SELL_PUT_FAMILY,
    strategy_semantics_for_side_config,
)


def _persist_source_snapshot(base: Path, snapshot: dict) -> None:
    try:
        state_repo.append_source_snapshot_event(base, snapshot)
    except Exception:
        pass


def _load_option_position_records(data_config: str) -> tuple[object, list[dict]]:
    repo = open_position_ledger(Path(data_config))
    return repo, list(list_position_lot_snapshots(repo))


def _decision_snapshots_for_records(
    repo: object,
    records: list[dict],
) -> dict[str, dict]:
    accounts = sorted(
        {
            str((item.get("fields") or {}).get("account") or "").strip().lower()
            for item in records
            if isinstance(item, dict)
            and str((item.get("fields") or {}).get("account") or "").strip()
        }
    )
    return {
        account: decision_state_snapshot(
            repo,
            account=account,
            portfolio_scope_id=portfolio_scope_id(account),
        )
        for account in accounts
    }


def load_portfolio_context(
    *,
    data_config: str,
    market: str,
    account: str | None,
    ttl_sec: int,
    base: Path,
    state_dir: Path,
    shared_state_dir: Path | None,
    log,
    runtime_config: dict | None = None,
    portfolio_source: str | None = None,
) -> dict | None:
    """Best-effort load portfolio context to dict."""
    try:
        ctx = load_account_portfolio_context(
            base=base,
            data_config=data_config,
            market=market,
            account=account,
            ttl_sec=ttl_sec,
            state_dir=state_dir,
            shared_state_dir=shared_state_dir,
            log=log,
            runtime_config=runtime_config,
            portfolio_source=portfolio_source,
            fetch_futu_portfolio_context_fn=fetch_futu_portfolio_context,
            is_fresh_fn=is_fresh,
            load_json_fn=load_cached_json,
        )
        snap = adapt_holdings_context(ctx)
        _persist_source_snapshot(base, snap)
        return ctx
    except Exception as e:
        log(f"[WARN] portfolio context not available: {e}")
        return None


def load_option_positions_context(
    *,
    base: Path,
    data_config: str,
    market: str,
    account: str | None,
    ttl_sec: int,
    state_dir: Path,
    shared_state_dir: Path | None,
    log,
) -> tuple[dict | None, bool]:
    """Best-effort load position-lot context.

    Returns (context, refreshed).
    """
    try:
        def _is_exact_account(context: dict, *, source: str) -> bool:
            try:
                validate_option_positions_context_account(
                    context,
                    account=account,
                    broker=market,
                )
                return True
            except ValueError as exc:
                log(
                    "[WARN] option_positions_context rejected "
                    f"source={source}: {exc}"
                )
                return False

        opt_path = (state_dir / 'option_positions_context.json').resolve()
        cached = None
        if ttl_sec > 0 and is_fresh(opt_path, ttl_sec):
            cached = load_cached_json(opt_path)
        if isinstance(cached, dict) and _is_exact_account(
            cached,
            source="account_cache",
        ):
            cached = with_context_source(cached, 'account_cache')
            log(f"[CTX] option_positions_context source=account_cache account={account or '-'}")
            snap = adapt_option_positions_context(cached)
            _persist_source_snapshot(base, snap)
            return cached, False

        shared_root = (shared_state_dir or state_dir).resolve()
        shared_root.mkdir(parents=True, exist_ok=True)
        shared_path = (shared_root / 'option_positions_context.shared.json').resolve()

        # Reuse shared cache first; this keeps per-account output schema unchanged.
        try:
            if ttl_sec > 0 and is_fresh(shared_path, ttl_sec):
                shared_cached = load_cached_json(shared_path)
                if isinstance(shared_cached, dict):
                    sliced = slice_shared_option_context_for_account(shared_cached, account)
                    if isinstance(sliced, dict) and _is_exact_account(
                        sliced,
                        source="shared_slice",
                    ):
                        sliced = with_context_source(sliced, 'shared_slice')
                        opt_path.parent.mkdir(parents=True, exist_ok=True)
                        atomic_write_json(opt_path, sliced)
                        log(f"[CTX] option_positions_context source=shared_slice account={account or '-'}")
                        snap = adapt_option_positions_context(sliced)
                        _persist_source_snapshot(base, snap)
                        # Keep existing semantics: account-level context was refreshed for this run.
                        return sliced, True
        except Exception:
            pass

        # Refresh shared cache (single fetch) and produce account context in one command.
        try:
            _repo, records = _load_option_position_records(data_config)
            rates = _load_option_position_exchange_rates(
                base=base,
                state_dir=shared_root,
                log=log,
            )
            shared_ctx = build_shared_option_positions_context(
                records,
                broker=str(market),
                rates=rates,
                decision_snapshots_by_account=_decision_snapshots_for_records(
                    _repo,
                    records,
                ),
            )
            ctx = dict(slice_shared_option_context_for_account(shared_ctx, account) or {})
            if not _is_exact_account(ctx, source="shared_refresh"):
                raise ValueError("shared option context account validation failed")
            atomic_write_json(shared_path, shared_ctx)
            ctx = with_context_source(ctx, 'shared_refresh')
            atomic_write_json(opt_path, ctx)
            log(f"[CTX] option_positions_context source=shared_refresh account={account or '-'}")
            snap = adapt_option_positions_context(ctx)
            _persist_source_snapshot(base, snap)
            return ctx, True
        except Exception:
            pass

        # Fallback: direct per-account fetch path.
        _repo, records = _load_option_position_records(data_config)
        rates = _load_option_position_exchange_rates(
            base=base,
            state_dir=shared_root,
            log=log,
        )
        normalized_account = str(account or "").strip().lower()
        decision_snapshot = (
            decision_state_snapshot(
                _repo,
                account=normalized_account,
                portfolio_scope_id=portfolio_scope_id(normalized_account),
            )
            if normalized_account
            else None
        )
        ctx = build_option_positions_context(
            records,
            broker=str(market),
            account=account,
            rates=rates,
            decision_snapshot=decision_snapshot,
        )
        if not _is_exact_account(ctx, source="direct_fetch"):
            raise ValueError("direct option context account validation failed")
        ctx = with_context_source(ctx, 'direct_fetch')
        atomic_write_json(opt_path, ctx)
        log(f"[CTX] option_positions_context source=direct_fetch account={account or '-'}")
        snap = adapt_option_positions_context(ctx)
        _persist_source_snapshot(base, snap)
        return ctx, True
    except Exception as e:
        log(f"[WARN] option positions context not available: {e}")
        return None, False


def _load_option_position_exchange_rates(*, base: Path, state_dir: Path, log) -> dict | None:
    try:
        from src.infrastructure.exchange_rates import get_exchange_rates_or_fetch_latest

        return get_exchange_rates_or_fetch_latest(
            cache_path=(state_dir / 'rate_cache.json').resolve(),
            max_age_hours=24,
        )
    except Exception as exc:
        log(f"[WARN] option position exchange rates not available: {exc}")
        return None


def _wants_global_path_risk_context(cfg: dict | None) -> bool:
    if not isinstance(cfg, dict):
        return False

    def _uses_path_risk(node: object, *, family: str) -> bool:
        return (
            isinstance(node, dict)
            and strategy_semantics_for_side_config(family=family, side_cfg=node).scan_uses_path_risk
        )

    templates = cfg.get("templates")
    if isinstance(templates, dict):
        for profile in templates.values():
            if isinstance(profile, dict) and (
                _uses_path_risk(profile.get("sell_put"), family=SELL_PUT_FAMILY)
                or _uses_path_risk(profile.get("sell_call"), family=SELL_CALL_FAMILY)
            ):
                return True
    for item in cfg.get("symbols") or []:
        if isinstance(item, dict) and (
            _uses_path_risk(item.get("sell_put"), family=SELL_PUT_FAMILY)
            or _uses_path_risk(item.get("sell_call"), family=SELL_CALL_FAMILY)
        ):
            return True
    return False


def load_global_holdings_risk_context(
    *,
    base: Path,
    data_config: str,
    ttl_sec: int,
    shared_state_dir: Path | None,
    state_dir: Path,
    log,
) -> dict | None:
    """Best-effort all-broker holdings context for portfolio risk limits."""

    try:
        shared_root = (shared_state_dir or state_dir).resolve()
        shared_root.mkdir(parents=True, exist_ok=True)
        path = (shared_root / "portfolio_context.global.json").resolve()
        if ttl_sec > 0 and is_fresh(path, ttl_sec):
            cached = load_cached_json(path)
            if isinstance(cached, dict):
                cached = with_context_source(cached, "global_cache")
                log("[CTX] portfolio_context source=global_cache account=all broker=all")
                return cached

        shared_ctx = load_holdings_portfolio_shared_context(
            data_config_path=Path(data_config),
            broker=None,
        )
        all_accounts = shared_ctx.get("all_accounts") if isinstance(shared_ctx, dict) else None
        if not isinstance(all_accounts, dict):
            raise ValueError("global holdings context missing all_accounts")
        out = dict(all_accounts)
        out["portfolio_source_name"] = "holdings_global"
        out = with_context_source(out, "global_refresh")
        path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        log("[CTX] portfolio_context source=global_refresh account=all broker=all")
        snap = adapt_holdings_context(out)
        _persist_source_snapshot(base, snap)
        return out
    except Exception as exc:
        log(f"[WARN] global holdings risk context not available: {exc}")
        return None


def load_global_option_positions_risk_context(
    *,
    base: Path,
    data_config: str,
    ttl_sec: int,
    shared_state_dir: Path | None,
    state_dir: Path,
    log,
) -> dict | None:
    """Best-effort all-broker option-position context for short-put exposure."""

    try:
        shared_root = (shared_state_dir or state_dir).resolve()
        shared_root.mkdir(parents=True, exist_ok=True)
        path = (shared_root / "option_positions_context.global.json").resolve()
        if ttl_sec > 0 and is_fresh(path, ttl_sec):
            cached = load_cached_json(path)
            if isinstance(cached, dict):
                cached = with_context_source(cached, "global_cache")
                log("[CTX] option_positions_context source=global_cache account=all broker=all")
                return cached

        _repo, records = _load_option_position_records(data_config)
        rates = _load_option_position_exchange_rates(
            base=base,
            state_dir=shared_root,
            log=log,
        )
        shared_ctx = build_shared_option_positions_context(records, broker="", rates=rates)
        all_accounts = shared_ctx.get("all_accounts") if isinstance(shared_ctx, dict) else None
        if not isinstance(all_accounts, dict):
            raise ValueError("global option positions context missing all_accounts")
        out = dict(all_accounts)
        out = with_context_source(out, "global_refresh")
        path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        log("[CTX] option_positions_context source=global_refresh account=all broker=all")
        snap = adapt_option_positions_context(out)
        _persist_source_snapshot(base, snap)
        return out
    except Exception as exc:
        log(f"[WARN] global option positions risk context not available: {exc}")
        return None

def load_exchange_rates(
    *,
    base: Path,
    state_dir: Path,
    log,
    shared_state_dir: Path | None = None,
    status_out: dict[str, str] | None = None,
) -> tuple[float | None, float | None]:
    """Best-effort exchange-rate loader.

    Use the shared infrastructure exchange-rate helper so cache miss behavior
    stays consistent with other entrypoints.
    """
    usd_per_cny_exchange_rate = None
    cny_per_hkd_exchange_rate = None
    if status_out is not None:
        status_out["status"] = "unavailable"
    try:
        from src.infrastructure.exchange_rates import get_exchange_rates_or_fetch_latest

        rates_obj = get_exchange_rates_or_fetch_latest(
            cache_path=(
                (shared_state_dir or state_dir) / "rate_cache.json"
            ).resolve(),
            max_age_hours=24,
            log=log,
        )
        rates_map = rates_obj.get('rates') if isinstance(rates_obj, dict) and isinstance(rates_obj.get('rates'), dict) else rates_obj
        if isinstance(rates_map, dict):
            try:
                usdcny = rates_map.get('USDCNY')
                usdcny = float(usdcny) if usdcny else None
            except Exception:
                usdcny = None
            try:
                cny_per_hkd_rate_value = rates_map.get('HKDCNY')
                cny_per_hkd_exchange_rate = float(cny_per_hkd_rate_value) if cny_per_hkd_rate_value else None
            except Exception:
                cny_per_hkd_exchange_rate = None
            if usdcny and usdcny > 0:
                usd_per_cny_exchange_rate = 1.0 / usdcny
            if status_out is not None and (
                usd_per_cny_exchange_rate is not None
                or cny_per_hkd_exchange_rate is not None
            ):
                status_out["status"] = "ready"
    except Exception as e:
        log(f"[WARN] exchange rates not available: {e}")
    return usd_per_cny_exchange_rate, cny_per_hkd_exchange_rate


def build_pipeline_context(
    *,
    py: str,
    base: Path,
    cfg: dict,
    report_dir: Path,
    portfolio_timeout_sec: int,
    runtime: dict,
    is_scheduled: bool,
    state_dir: Path,
    shared_state_dir: Path | None = None,
    log,
    no_context: bool,
    want_scan: bool,
    prepared_portfolio_context_manifest: Path | None = None,
    prepared_portfolio_context_run_id: str | None = None,
    prepared_portfolio_context_account_config_sha256: str | None = None,
    prepared_portfolio_context_manifest_sha256: str | None = None,
    prepared_option_positions_context_manifest: Path | None = None,
    prepared_option_positions_context_run_id: str | None = None,
    prepared_option_positions_context_account_config_sha256: str | None = None,
    prepared_option_positions_context_manifest_sha256: str | None = None,
) -> tuple[dict | None, dict | None, float | None, float | None]:
    """Load portfolio_ctx, option_ctx, usd_per_cny_exchange_rate, cny_per_hkd_exchange_rate."""
    if (not want_scan) or bool(no_context):
        return None, None, None, None

    portfolio_cfg = cfg.get('portfolio', {}) or {}
    data_config = resolve_data_config_path(base=base, data_config=portfolio_cfg.get('data_config'))
    broker = portfolio_cfg.get('broker') or '富途'
    account = portfolio_cfg.get('account')
    portfolio_source = build_account_portfolio_source_plan(
        cfg,
        account=(str(account) if account else None),
    ).requested_source

    # Cache policy (TTL seconds)
    ttl_opt_ctx = int(runtime.get('option_positions_context_ttl_sec', 900 if is_scheduled else 120) or 0)
    ttl_port_ctx = int(runtime.get('portfolio_context_ttl_sec', 900 if is_scheduled else 60) or 0)

    if prepared_portfolio_context_manifest is not None:
        try:
            portfolio_ctx = load_prepared_portfolio_context(
                manifest_path=prepared_portfolio_context_manifest,
                expected_base=base,
                expected_run_id=str(prepared_portfolio_context_run_id or ""),
                expected_account=str(account or ""),
                expected_account_config_sha256=str(
                    prepared_portfolio_context_account_config_sha256 or ""
                ),
                expected_manifest_sha256=str(
                    prepared_portfolio_context_manifest_sha256 or ""
                ),
                expected_runtime_config=cfg,
            )
            source = "prepared" if portfolio_ctx is not None else "prepared_unavailable"
            log(f"[CTX] portfolio_context source={source} account={account or '-'}")
        except PreparedPortfolioContextError as exc:
            log(f"[WARN] prepared portfolio context not available: {exc}")
            raise
    else:
        portfolio_ctx = load_portfolio_context(
            base=base,
            data_config=str(data_config),
            market=str(broker),
            account=(str(account) if account else None),
            ttl_sec=ttl_port_ctx,
            state_dir=state_dir,
            shared_state_dir=shared_state_dir,
            log=log,
            runtime_config=cfg,
            portfolio_source=str(portfolio_source),
        )

    if prepared_option_positions_context_manifest is not None:
        if _wants_global_path_risk_context(cfg):
            raise PreparedOptionPositionsContextError(
                "prepared option context does not support global path risk"
            )
        try:
            option_ctx = load_prepared_option_positions_context(
                manifest_path=(
                    prepared_option_positions_context_manifest
                ),
                expected_base=base,
                expected_run_id=str(
                    prepared_option_positions_context_run_id or ""
                ),
                expected_account=str(account or ""),
                expected_account_config_sha256=str(
                    prepared_option_positions_context_account_config_sha256
                    or ""
                ),
                expected_manifest_sha256=str(
                    prepared_option_positions_context_manifest_sha256 or ""
                ),
                expected_runtime_config=cfg,
            )
            log(
                "[CTX] option_positions_context source=prepared "
                f"account={account or '-'}"
            )
            _persist_source_snapshot(
                base,
                adapt_option_positions_context(option_ctx),
            )
        except PreparedOptionPositionsContextError as exc:
            log(f"[WARN] prepared option context not available: {exc}")
            raise
    else:
        option_ctx, _ = load_option_positions_context(
            base=base,
            data_config=str(data_config),
            market=str(broker),
            account=(str(account) if account else None),
            ttl_sec=ttl_opt_ctx,
            state_dir=state_dir,
            shared_state_dir=shared_state_dir,
            log=log,
        )

    if portfolio_ctx is not None and _wants_global_path_risk_context(cfg):
        portfolio_ctx = dict(portfolio_ctx)
        if prepared_portfolio_context_manifest is None:
            global_portfolio_ctx = load_global_holdings_risk_context(
                base=base,
                data_config=str(data_config),
                ttl_sec=ttl_port_ctx,
                shared_state_dir=shared_state_dir,
                state_dir=state_dir,
                log=log,
            )
            if global_portfolio_ctx is not None:
                portfolio_ctx["_global_portfolio_ctx"] = global_portfolio_ctx
        if prepared_option_positions_context_manifest is None:
            global_option_ctx = load_global_option_positions_risk_context(
                base=base,
                data_config=str(data_config),
                ttl_sec=ttl_opt_ctx,
                shared_state_dir=shared_state_dir,
                state_dir=state_dir,
                log=log,
            )
            if global_option_ctx is not None:
                portfolio_ctx["_global_option_ctx"] = global_option_ctx

    if prepared_option_positions_context_manifest is not None:
        usd_per_cny_exchange_rate, cny_per_hkd_exchange_rate = (
            exchange_rate_scalars_from_option_context(option_ctx or {})
        )
        prepared_authority = (
            option_ctx.get("prepared_authority")
            if isinstance(option_ctx, dict)
            and isinstance(option_ctx.get("prepared_authority"), dict)
            else {}
        )
        fx_status = str(prepared_authority.get("fx_status") or "").strip().lower()
    else:
        rate_status: dict[str, str] = {}
        usd_per_cny_exchange_rate, cny_per_hkd_exchange_rate = (
            load_exchange_rates(
                base=base,
                state_dir=state_dir,
                shared_state_dir=shared_state_dir,
                log=log,
                status_out=rate_status,
            )
        )
        fx_status = str(rate_status.get("status") or "").strip().lower()

    if fx_status == "unavailable_stale" and isinstance(portfolio_ctx, dict):
        portfolio_ctx = dict(portfolio_ctx)
        portfolio_ctx["_sell_put_fx_status"] = fx_status

    return portfolio_ctx, option_ctx, usd_per_cny_exchange_rate, cny_per_hkd_exchange_rate
