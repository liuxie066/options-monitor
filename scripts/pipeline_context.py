#!/usr/bin/env python3
from __future__ import annotations

"""Pipeline context loading (portfolio/option_positions/fx).

Stage 3 refactor target:
- keep scripts/run_pipeline.py thin (orchestration only)
- move context fetch/caching logic into cohesive module

Design constraints:
- minimal/no behavior change
- best-effort context (should not fail the whole pipeline in scheduled mode)
"""

import json
from pathlib import Path

from scripts.io_utils import is_fresh, load_cached_json
from scripts.subprocess_utils import run_cmd
from domain.services import adapt_holdings_context, adapt_option_positions_context
try:
    from domain.storage.repositories import state_repo
except Exception:
    from scripts.domain.storage.repositories import state_repo  # type: ignore


def _persist_source_snapshot(base: Path, snapshot: dict) -> None:
    try:
        state_repo.append_source_snapshot_event(base, snapshot)
    except Exception:
        pass


def load_portfolio_context(
    *,
    py: str,
    base: Path,
    pm_config: str,
    market: str,
    account: str | None,
    ttl_sec: int,
    timeout_sec: int,
    is_scheduled: bool,
    state_dir: Path,
    log,
) -> dict | None:
    """Best-effort load portfolio context to dict."""
    try:
        port_path = (state_dir / 'portfolio_context.json').resolve()
        cached = None
        if ttl_sec > 0 and is_fresh(port_path, ttl_sec):
            cached = load_cached_json(port_path)
        if cached is not None:
            snap = adapt_holdings_context(cached)
            _persist_source_snapshot(base, snap)
            return cached

        cmd = [
            py, 'scripts/fetch_portfolio_context.py',
            '--pm-config', str(pm_config),
            '--market', str(market),
            '--out', str((state_dir / 'portfolio_context.json').as_posix()),
        ]
        if account:
            cmd.extend(['--account', str(account)])
        if is_scheduled:
            cmd.append('--quiet')
        run_cmd(cmd, cwd=base, timeout_sec=timeout_sec, is_scheduled=is_scheduled)
        ctx = load_cached_json(port_path) or json.loads(port_path.read_text(encoding='utf-8'))
        snap = adapt_holdings_context(ctx)
        _persist_source_snapshot(base, snap)
        return ctx
    except BaseException as e:
        log(f"[WARN] portfolio context not available: {e}")
        return None


def load_option_positions_context(
    *,
    py: str,
    base: Path,
    pm_config: str,
    market: str,
    account: str | None,
    ttl_sec: int,
    timeout_sec: int,
    is_scheduled: bool,
    report_dir: Path,
    state_dir: Path,
    log,
) -> tuple[dict | None, bool]:
    """Best-effort load option_positions context.

    Returns (context, refreshed).
    """
    try:
        opt_path = (state_dir / 'option_positions_context.json').resolve()
        cached = None
        if ttl_sec > 0 and is_fresh(opt_path, ttl_sec):
            cached = load_cached_json(opt_path)
        if cached is not None:
            snap = adapt_option_positions_context(cached)
            _persist_source_snapshot(base, snap)
            return cached, False

        cmd = [
            py, 'scripts/fetch_option_positions_context.py',
            '--pm-config', str(pm_config),
            '--market', str(market),
            '--out', str(opt_path.as_posix()),
        ]
        if account:
            cmd.extend(['--account', str(account)])
        if is_scheduled:
            cmd.append('--quiet')
        run_cmd(cmd, cwd=base, timeout_sec=timeout_sec, is_scheduled=is_scheduled)
        ctx = load_cached_json(opt_path) or json.loads(opt_path.read_text(encoding='utf-8'))
        snap = adapt_option_positions_context(ctx)
        _persist_source_snapshot(base, snap)
        return ctx, True
    except BaseException as e:
        log(f"[WARN] option positions context not available: {e}")
        return None, False


def maybe_auto_close_expired_positions(
    *,
    py: str,
    base: Path,
    pm_config: str,
    report_dir: Path,
    state_dir: Path,
    timeout_sec: int,
    is_scheduled: bool,
    refreshed: bool,
    log,
) -> None:
    # Auto-close expired open positions (table maintenance) without extra scans.
    # Only run when we refreshed context (avoid repeated close calls during rapid dev loops).
    if not refreshed:
        return
    try:
        cmd = [
            py, 'scripts/auto_close_expired_positions.py',
            '--pm-config', str(pm_config),
            '--context', str((state_dir / 'option_positions_context.json').as_posix()),
            '--state-dir', str(state_dir),
            '--grace-days', '1',
            '--max-close', '20',
            '--summary-out', str((report_dir / 'auto_close_summary.txt').as_posix()),
        ]
        if is_scheduled:
            cmd.append('--quiet')
        run_cmd(cmd, cwd=base, timeout_sec=timeout_sec, is_scheduled=is_scheduled)
    except Exception as e2:
        log(f"[WARN] auto-close expired positions failed: {e2}")


def load_fx_rates(*, base: Path, state_dir: Path, log) -> tuple[float | None, float | None]:
    """Best-effort FX loader.

    Keep the original behavior from run_pipeline:
    - load scripts/fx_rates.py via importlib by file path
    - fx_usd_per_cny from get_usd_per_cny(base)
    - hkdcny from state_dir/rate_cache.json via get_rates
    """
    fx_usd_per_cny = None
    hkdcny = None
    try:
        import importlib.util
        import sys as _sys

        fx_path = (base / 'scripts' / 'fx_rates.py').resolve()
        spec = importlib.util.spec_from_file_location('fx_rates', fx_path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        # dataclasses expects module to exist in sys.modules during exec
        _sys.modules['fx_rates'] = mod
        spec.loader.exec_module(mod)  # type: ignore

        fx_usd_per_cny = mod.get_usd_per_cny(base)  # type: ignore
        try:
            rates = mod.get_rates(cache_path=(state_dir / 'rate_cache.json').resolve(), shared_cache_path=None)
            # Support both legacy {HKDCNY: <value>} and nested {rates: {HKDCNY: <value>}} schemas
            hkdcny = None
            if isinstance(rates, dict):
                hkdcny = rates.get('HKDCNY')
                if hkdcny is None:
                    nested = rates.get('rates')
                    if isinstance(nested, dict):
                        hkdcny = nested.get('HKDCNY')
            if hkdcny is not None:
                try:
                    hkdcny = float(hkdcny)
                except Exception:
                    hkdcny = None
        except Exception:
            hkdcny = None
    except BaseException as e:
        log(f"[WARN] fx rates not available: {e}")
    return fx_usd_per_cny, hkdcny


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
    log,
    no_context: bool,
    want_scan: bool,
) -> tuple[dict | None, dict | None, float | None, float | None]:
    """Load portfolio_ctx, option_ctx, fx_usd_per_cny, hkdcny."""
    if (not want_scan) or bool(no_context):
        return None, None, None, None

    portfolio_cfg = cfg.get('portfolio', {}) or {}
    pm_config = portfolio_cfg.get('pm_config', '../portfolio-management/config.json')
    market = portfolio_cfg.get('market', '富途')
    account = portfolio_cfg.get('account')

    # Cache policy (TTL seconds)
    ttl_opt_ctx = int(runtime.get('option_positions_context_ttl_sec', 900 if is_scheduled else 120) or 0)
    ttl_port_ctx = int(runtime.get('portfolio_context_ttl_sec', 900 if is_scheduled else 60) or 0)

    portfolio_ctx = load_portfolio_context(
        py=py,
        base=base,
        pm_config=str(pm_config),
        market=str(market),
        account=(str(account) if account else None),
        ttl_sec=ttl_port_ctx,
        timeout_sec=portfolio_timeout_sec,
        is_scheduled=is_scheduled,
        state_dir=state_dir,
        log=log,
    )

    option_ctx, refreshed = load_option_positions_context(
        py=py,
        base=base,
        pm_config=str(pm_config),
        market=str(market),
        account=(str(account) if account else None),
        ttl_sec=ttl_opt_ctx,
        timeout_sec=portfolio_timeout_sec,
        is_scheduled=is_scheduled,
        report_dir=report_dir,
        state_dir=state_dir,
        log=log,
    )

    maybe_auto_close_expired_positions(
        py=py,
        base=base,
        pm_config=str(pm_config),
        report_dir=report_dir,
        state_dir=state_dir,
        timeout_sec=portfolio_timeout_sec,
        is_scheduled=is_scheduled,
        refreshed=refreshed,
        log=log,
    )

    fx_usd_per_cny, hkdcny = load_fx_rates(base=base, state_dir=state_dir, log=log)

    return portfolio_ctx, option_ctx, fx_usd_per_cny, hkdcny
