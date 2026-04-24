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

from scripts.account_config import build_account_portfolio_source_plan
from scripts.config_loader import resolve_data_config_path
from scripts.fetch_option_positions_context import (
    build_context as build_option_positions_context,
    build_shared_context as build_shared_option_positions_context,
)
from scripts.futu_portfolio_context import fetch_futu_portfolio_context
from scripts.io_utils import is_fresh, load_cached_json
from scripts.option_positions_core.service import (
    auto_close_expired_positions,
    build_expired_close_decisions,
    load_option_positions_repo,
)
from scripts.portfolio_context_service import load_account_portfolio_context, with_context_source
from domain.services import adapt_holdings_context, adapt_option_positions_context
from scripts.fetch_option_positions_context import slice_shared_context_for_account as slice_shared_option_context_for_account
from src.application.option_positions_facade import load_option_position_records
try:
    from domain.storage.repositories import state_repo
except Exception:
    from scripts.domain.storage.repositories import state_repo  # type: ignore


def _persist_source_snapshot(base: Path, snapshot: dict) -> None:
    try:
        state_repo.append_source_snapshot_event(base, snapshot)
    except Exception:
        pass


def _load_option_position_records(data_config: str) -> tuple[object, list[dict]]:
    repo = load_option_positions_repo(Path(data_config))
    return repo, list(load_option_position_records(repo))


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
        opt_path = (state_dir / 'option_positions_context.json').resolve()
        cached = None
        if ttl_sec > 0 and is_fresh(opt_path, ttl_sec):
            cached = load_cached_json(opt_path)
        if cached is not None:
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
                    if isinstance(sliced, dict):
                        sliced = with_context_source(sliced, 'shared_slice')
                        opt_path.parent.mkdir(parents=True, exist_ok=True)
                        opt_path.write_text(json.dumps(sliced, ensure_ascii=False, indent=2), encoding='utf-8')
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
            rates = _load_option_position_exchange_rates(base=base, state_dir=state_dir, log=log)
            shared_ctx = build_shared_option_positions_context(records, broker=str(market), rates=rates)
            shared_path.write_text(json.dumps(shared_ctx, ensure_ascii=False, indent=2), encoding='utf-8')
            ctx = dict(slice_shared_option_context_for_account(shared_ctx, account) or {})
            ctx = with_context_source(ctx, 'shared_refresh')
            opt_path.write_text(json.dumps(ctx, ensure_ascii=False, indent=2), encoding='utf-8')
            log(f"[CTX] option_positions_context source=shared_refresh account={account or '-'}")
            snap = adapt_option_positions_context(ctx)
            _persist_source_snapshot(base, snap)
            return ctx, True
        except Exception:
            pass

        # Fallback: direct per-account fetch path.
        _repo, records = _load_option_position_records(data_config)
        rates = _load_option_position_exchange_rates(base=base, state_dir=state_dir, log=log)
        ctx = build_option_positions_context(records, broker=str(market), account=account, rates=rates)
        ctx = with_context_source(ctx, 'direct_fetch')
        opt_path.write_text(json.dumps(ctx, ensure_ascii=False, indent=2), encoding='utf-8')
        log(f"[CTX] option_positions_context source=direct_fetch account={account or '-'}")
        snap = adapt_option_positions_context(ctx)
        _persist_source_snapshot(base, snap)
        return ctx, True
    except Exception as e:
        log(f"[WARN] option positions context not available: {e}")
        return None, False


def maybe_auto_close_expired_positions(
    *,
    data_config: str,
    report_dir: Path,
    state_dir: Path,
    refreshed: bool,
    log,
) -> None:
    # Auto-close expired open lots without extra scans.
    # Only run when we refreshed context (avoid repeated close calls during rapid dev loops).
    if not refreshed:
        return
    try:
        from datetime import datetime, timezone

        ctx_path = (state_dir / 'option_positions_context.json').resolve()
        ctx = json.loads(ctx_path.read_text(encoding='utf-8')) if ctx_path.exists() else {}
        positions = [p for p in (ctx.get('open_positions_min') or []) if isinstance(p, dict)]
        as_of_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        decisions = build_expired_close_decisions(
            positions,
            as_of_ms=as_of_ms,
            grace_days=1,
        )
        to_close = [d for d in decisions if bool(d.get('should_close')) and d.get('record_id')]
        applied: list[dict] = []
        errors: list[str] = []
        if to_close:
            repo, _records = _load_option_position_records(data_config)
            decisions, applied, errors = auto_close_expired_positions(
                repo,
                positions,
                as_of_ms=as_of_ms,
                grace_days=1,
                max_close=20,
            )
            to_close = [d for d in decisions if bool(d.get('should_close')) and d.get('record_id')]

        lines: list[str] = []
        lines.append("Auto-close expired positions (grace_days=1)")
        lines.append(f"context: {ctx_path}")
        lines.append(f"candidates_should_close: {len(to_close)}")
        lines.append(f"applied_closed: {len(applied)}")
        lines.append(f"errors: {len(errors)}")
        summary_path = (report_dir / 'auto_close_summary.txt').resolve()
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text("\n".join(lines).strip() + "\n", encoding='utf-8')
    except Exception as e2:
        log(f"[WARN] auto-close expired position lots failed: {e2}")


def _load_option_position_exchange_rates(*, base: Path, state_dir: Path, log) -> dict | None:
    try:
        from scripts.exchange_rates import get_exchange_rates_or_fetch_latest

        return get_exchange_rates_or_fetch_latest(
            cache_path=(state_dir / 'rate_cache.json').resolve(),
            max_age_hours=24,
        )
    except Exception as exc:
        log(f"[WARN] option position exchange rates not available: {exc}")
        return None

def load_exchange_rates(*, base: Path, state_dir: Path, log, shared_state_dir: Path | None = None) -> tuple[float | None, float | None]:
    """Best-effort exchange-rate loader.

    Keep the original importlib boundary from run_pipeline, but use the
    repo-local exchange-rate helper so cache miss behavior stays consistent with other
    entrypoints.
    """
    usd_per_cny_exchange_rate = None
    cny_per_hkd_exchange_rate = None
    try:
        import importlib.util
        import sys as _sys

        exchange_rate_path = (base / 'scripts' / 'exchange_rates.py').resolve()
        spec = importlib.util.spec_from_file_location('exchange_rates', exchange_rate_path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        # dataclasses expects module to exist in sys.modules during exec
        _sys.modules['exchange_rates'] = mod
        spec.loader.exec_module(mod)  # type: ignore

        rates_obj = mod.get_exchange_rates_or_fetch_latest(  # type: ignore
            cache_path=(state_dir / 'rate_cache.json').resolve(),
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

    option_ctx, refreshed = load_option_positions_context(
        base=base,
        data_config=str(data_config),
        market=str(broker),
        account=(str(account) if account else None),
        ttl_sec=ttl_opt_ctx,
        state_dir=state_dir,
        shared_state_dir=shared_state_dir,
        log=log,
    )

    maybe_auto_close_expired_positions(
        data_config=str(data_config),
        report_dir=report_dir,
        state_dir=state_dir,
        refreshed=refreshed,
        log=log,
    )

    usd_per_cny_exchange_rate, cny_per_hkd_exchange_rate = load_exchange_rates(
        base=base,
        state_dir=state_dir,
        shared_state_dir=shared_state_dir,
        log=log,
    )

    return portfolio_ctx, option_ctx, usd_per_cny_exchange_rate, cny_per_hkd_exchange_rate
