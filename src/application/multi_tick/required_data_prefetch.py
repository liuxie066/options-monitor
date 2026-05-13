from __future__ import annotations

from pathlib import Path
from typing import Any

from domain.domain.tool_boundary import SCHEMA_VERSION_V1, normalize_tool_execution_payload
from domain.services import (
    ToolExecutionIntent,
    ToolExecutionService,
    adapt_opend_tool_payload,
)
from domain.domain.fetch_source import resolve_symbol_fetch_source
from domain.storage.repositories import state_repo
from src.application.config_loader import resolve_templates_config, resolve_watchlist_config
from src.application.config_profiles import apply_profiles
from src.application.multi_tick.prefetch_coordinator import PrefetchCoordinator
from src.application.opend_fetch_config import resolve_opend_batch_config, resolve_opend_fetch_config
from src.application.opend_symbol_fetching import fetch_symbol
from src.application.opend_symbol_outputs import save_outputs
from src.application.required_data_coverage import required_data_csv_covers_strategy_bounds
from src.application.required_data_observability import (
    summarize_prefetch_fetch_metrics,
    summarize_required_data_prefetch_run,
)
from src.application.required_data_prefetch_planning import (
    build_prefetch_symbol_plan,
    strategy_prefetch_kwargs as _strategy_prefetch_kwargs,
)
from src.infrastructure.futu_gateway_pool import ThreadLocalFutuGatewayPool
from src.infrastructure.io_utils import has_shared_required_data
from src.infrastructure.opend_retcodes import classify_opend_error


_gateway_pool = ThreadLocalFutuGatewayPool()
_DEFAULT_PREFETCH_MAX_WORKERS = 2


def _to_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return int(default)


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _resolve_prefetch_max_workers(cfg: dict[str, Any]) -> int:
    runtime = _as_dict(cfg.get("runtime"))
    runtime_prefetch_cfg = _as_dict(runtime.get("prefetch"))
    prefetch_cfg = _as_dict(cfg.get("prefetch"))
    v = runtime.get("prefetch_max_workers")
    if v is None:
        v = runtime_prefetch_cfg.get("max_workers")
    if v is None:
        v = prefetch_cfg.get("max_workers")
    n = _to_int(v, _DEFAULT_PREFETCH_MAX_WORKERS)
    return n if n > 0 else _DEFAULT_PREFETCH_MAX_WORKERS


def _resolve_execution_mode(cfg: dict[str, Any]) -> str:
    runtime = _as_dict(cfg.get("runtime"))
    prefetch_cfg = _as_dict(runtime.get("prefetch"))
    mode = str(prefetch_cfg.get("execution_mode") or "inprocess").strip().lower()
    return mode if mode in {"inprocess", "subprocess"} else "inprocess"


def _resolve_failure_budget(cfg: dict[str, Any]) -> tuple[int, int]:
    runtime = _as_dict(cfg.get("runtime"))
    prefetch_cfg = _as_dict(cfg.get("prefetch"))
    max_consecutive = runtime.get("prefetch_fail_budget_consecutive")
    if max_consecutive is None:
        max_consecutive = prefetch_cfg.get("fail_budget_consecutive")
    max_total = runtime.get("prefetch_fail_budget_total")
    if max_total is None:
        max_total = prefetch_cfg.get("fail_budget_total")
    return (max(1, _to_int(max_consecutive, 3)), max(1, _to_int(max_total, 5)))


def _resolve_opend_fetch_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    resolved = resolve_opend_fetch_config(cfg)
    return {
        "option_chain": dict(resolved["option_chain"]),
        "market_snapshot": dict(resolved["market_snapshot"]),
        "option_expiration": dict(resolved["option_expiration"]),
    }


def _fetch_one_inprocess(
    symbol_cfg: dict,
    *,
    base: Path,
    shared_required: Path,
    opend_fetch_cfg: dict[str, Any],
    batch_cfg: Any,
) -> dict:
    symbol = str(symbol_cfg.get('symbol')).strip()
    if not symbol:
        payload = normalize_tool_execution_payload(
            tool_name='required_data_prefetch',
            symbol='',
            source='unknown',
            limit_exp=8,
            status='error',
            ok=False,
            message='empty_symbol',
            returncode=None,
        )
        source_snapshot = adapt_opend_tool_payload(payload)
        payload["source_snapshot"] = source_snapshot
        try:
            state_repo.append_source_snapshot_event(base, source_snapshot)
        except Exception:
            pass
        return payload

    fetch_cfg = (symbol_cfg.get('fetch') or {}) if isinstance(symbol_cfg, dict) else {}
    src, _decision = resolve_symbol_fetch_source(fetch_cfg)
    limit_exp = int(fetch_cfg.get('limit_expirations') or symbol_cfg.get('fetch', {}).get('limit_expirations', 8) or 8)
    host = str(fetch_cfg.get('host') or '127.0.0.1')
    port = _to_int(fetch_cfg.get('port') or 11111, 11111)
    strategy_kwargs = _strategy_prefetch_kwargs(symbol_cfg, enabled=True)
    try:
        gateway = _gateway_pool.get_gateway(host=host, port=port, chain_cache=True)
        payload0 = fetch_symbol(
            symbol,
            limit_expirations=limit_exp,
            host=host,
            port=port,
            base_dir=base,
            option_types=str(strategy_kwargs["option_types"]),
            min_strike=strategy_kwargs.get("min_strike"),
            max_strike=strategy_kwargs.get("max_strike"),
            side_strike_windows=strategy_kwargs.get("side_strike_windows"),
            min_dte=strategy_kwargs.get("min_dte"),
            max_dte=strategy_kwargs.get("max_dte"),
            chain_cache=True,
            chain_cache_force_refresh=False,
            freshness_policy='cache_first',
            gateway=gateway,
            snapshot_batch_size=int(getattr(batch_cfg, 'market_snapshot', 0) or 0),
            snapshot_fallback_max_codes=int(getattr(batch_cfg, 'market_snapshot_fallback_max_codes', 100) or 0),
            snapshot_fallback_batch_size=int(getattr(batch_cfg, 'market_snapshot_fallback_batch_size', 20) or 20),
            max_wait_sec=float(opend_fetch_cfg['option_chain']['max_wait_sec']),
            option_chain_window_sec=float(opend_fetch_cfg['option_chain']['window_sec']),
            option_chain_max_calls=int(opend_fetch_cfg['option_chain']['max_calls']),
            snapshot_max_wait_sec=float(opend_fetch_cfg['market_snapshot']['max_wait_sec']),
            snapshot_window_sec=float(opend_fetch_cfg['market_snapshot']['window_sec']),
            snapshot_max_calls=int(opend_fetch_cfg['market_snapshot']['max_calls']),
            expiration_max_wait_sec=float(opend_fetch_cfg['option_expiration']['max_wait_sec']),
            expiration_window_sec=float(opend_fetch_cfg['option_expiration']['window_sec']),
            expiration_max_calls=int(opend_fetch_cfg['option_expiration']['max_calls']),
        )
        _gateway_pool.mark_success()
        save_outputs(base, symbol, payload0, output_root=shared_required)
        meta = payload0.get('meta') if isinstance(payload0.get('meta'), dict) else {}
        ok = str(meta.get('status') or '').strip().lower() not in {'error', 'fail', 'failed'}
        message = str(meta.get('error') or meta.get('status') or 'fetched')
        payload = normalize_tool_execution_payload(
            tool_name='required_data_prefetch',
            symbol=symbol,
            source=src,
            limit_exp=limit_exp,
            status=('fetched' if ok else 'error'),
            ok=ok,
            message=message,
            returncode=(0 if ok else 1),
        )
        if isinstance(payload0, dict):
            payload['payload'] = payload0
    except Exception as exc:
        _gateway_pool.mark_failure(exc)
        message = str(exc or '')
        payload = normalize_tool_execution_payload(
            tool_name='required_data_prefetch',
            symbol=symbol,
            source=src,
            limit_exp=limit_exp,
            status='error',
            ok=False,
            message=message,
            returncode=None,
        )
        if classify_opend_error({"message": message}).is_rate_limit:
            payload['error_code'] = 'RATE_LIMIT'
    source_snapshot = adapt_opend_tool_payload(payload)
    payload["source_snapshot"] = source_snapshot
    try:
        state_repo.append_source_snapshot_event(base, source_snapshot)
    except Exception:
        pass
    return payload


def prefetch_required_data(*, vpy: Path, base: Path, cfg: dict, shared_required: Path, force_refresh: bool = False) -> dict:
    profiles = resolve_templates_config(cfg)
    syms = [apply_profiles(it, profiles) for it in resolve_watchlist_config(cfg) if it.get('symbol')]
    symbols = [str(it.get('symbol')).strip() for it in syms if str(it.get('symbol')).strip()]
    symbol_plan = build_prefetch_symbol_plan(syms)
    fetch_syms = symbol_plan.symbol_cfgs

    raw_dir = (shared_required / 'raw').resolve()
    parsed_dir = (shared_required / 'parsed').resolve()
    raw_dir.mkdir(parents=True, exist_ok=True)
    parsed_dir.mkdir(parents=True, exist_ok=True)

    def _need_fetch(symbol_cfg: dict[str, Any]) -> bool:
        symbol = str(symbol_cfg.get('symbol')).strip()
        if not symbol:
            return True
        if force_refresh:
            return True
        try:
            if not has_shared_required_data(symbol, shared_required):
                return True
            strategy_kwargs = _strategy_prefetch_kwargs(symbol_cfg, enabled=True)
            parsed = shared_required / 'parsed' / f"{symbol}_required_data.csv"
            return not required_data_csv_covers_strategy_bounds(
                parsed=parsed,
                option_types=str(strategy_kwargs["option_types"]),
                min_dte=strategy_kwargs.get("min_dte"),
                max_dte=strategy_kwargs.get("max_dte"),
                min_strike=strategy_kwargs.get("min_strike"),
                max_strike=strategy_kwargs.get("max_strike"),
                side_strike_windows=strategy_kwargs.get("side_strike_windows"),
            )
        except Exception:
            return True

    exec_service = ToolExecutionService(base=base)
    opend_fetch_cfg = _resolve_opend_fetch_cfg(cfg)
    batch_cfg = resolve_opend_batch_config(cfg)
    execution_mode = _resolve_execution_mode(cfg)
    option_chain_fetch_cfg = opend_fetch_cfg["option_chain"]
    snapshot_fetch_cfg = opend_fetch_cfg["market_snapshot"]
    expiration_fetch_cfg = opend_fetch_cfg["option_expiration"]

    def _fetch_one(symbol_cfg: dict) -> dict:
        symbol = str(symbol_cfg.get('symbol')).strip()
        if not symbol:
            return normalize_tool_execution_payload(
                tool_name='required_data_prefetch',
                symbol='',
                source='unknown',
                limit_exp=8,
                status='error',
                ok=False,
                message='empty_symbol',
                returncode=None,
            )
        if not _need_fetch(symbol_cfg):
            return normalize_tool_execution_payload(
                tool_name='required_data_prefetch',
                symbol=symbol,
                source='cache',
                limit_exp=8,
                status='cached',
                ok=True,
                message='cached_strategy_covered',
                returncode=0,
            )

        fetch_cfg = (symbol_cfg.get('fetch') or {}) if isinstance(symbol_cfg, dict) else {}
        src, _decision = resolve_symbol_fetch_source(fetch_cfg)
        limit_exp = int(fetch_cfg.get('limit_expirations') or symbol_cfg.get('fetch', {}).get('limit_expirations', 8) or 8)
        strategy_kwargs = _strategy_prefetch_kwargs(symbol_cfg, enabled=True)
        opt_types = str(strategy_kwargs["option_types"])

        cmd = [
            str(vpy), '-m', 'src.application.opend_symbol_fetching_cli',
            '--symbols', symbol,
            '--limit-expirations', str(limit_exp),
            '--host', str(fetch_cfg.get('host') or '127.0.0.1'),
            '--port', str(int(fetch_cfg.get('port') or 11111)),
            '--option-types', opt_types,
            '--output-root', str(shared_required),
            '--chain-cache',
            '--option-chain-window-sec', str(option_chain_fetch_cfg["window_sec"]),
            '--option-chain-max-calls', str(option_chain_fetch_cfg["max_calls"]),
            '--option-chain-max-wait-sec', str(option_chain_fetch_cfg["max_wait_sec"]),
            '--snapshot-window-sec', str(snapshot_fetch_cfg["window_sec"]),
            '--snapshot-max-calls', str(snapshot_fetch_cfg["max_calls"]),
            '--snapshot-max-wait-sec', str(snapshot_fetch_cfg["max_wait_sec"]),
            '--snapshot-batch-size', str(int(getattr(batch_cfg, 'market_snapshot', 0) or 0)),
            '--snapshot-fallback-max-codes', str(int(getattr(batch_cfg, 'market_snapshot_fallback_max_codes', 100) or 0)),
            '--snapshot-fallback-batch-size', str(int(getattr(batch_cfg, 'market_snapshot_fallback_batch_size', 20) or 20)),
            '--expiration-window-sec', str(expiration_fetch_cfg["window_sec"]),
            '--expiration-max-calls', str(expiration_fetch_cfg["max_calls"]),
            '--expiration-max-wait-sec', str(expiration_fetch_cfg["max_wait_sec"]),
            '--quiet',
        ]
        if strategy_kwargs.get("min_dte") is not None:
            cmd.extend(['--min-dte', str(strategy_kwargs["min_dte"])])
        if strategy_kwargs.get("max_dte") is not None:
            cmd.extend(['--max-dte', str(strategy_kwargs["max_dte"])])
        if strategy_kwargs.get("min_strike") is not None:
            cmd.extend(['--min-strike', str(strategy_kwargs["min_strike"])])
        if strategy_kwargs.get("max_strike") is not None:
            cmd.extend(['--max-strike', str(strategy_kwargs["max_strike"])])

        payload = exec_service.execute(
            ToolExecutionIntent(
                tool_name='required_data_prefetch',
                symbol=symbol,
                source=src,
                limit_exp=limit_exp,
                cmd=cmd,
                cwd=base,
                capture_output=True,
                text=True,
                idempotency_scope='required_data_prefetch',
                force_refresh=bool(force_refresh),
            )
        )
        # Canonical adapter validation before entering next layer.
        source_snapshot = adapt_opend_tool_payload(payload)
        payload["source_snapshot"] = source_snapshot
        try:
            state_repo.append_source_snapshot_event(base, source_snapshot)
        except Exception:
            pass
        return payload

    todo_cfgs = [it for it in fetch_syms if _need_fetch(it)]
    unique_cached_count = max(0, len(fetch_syms) - len(todo_cfgs))

    if not todo_cfgs:
        fetch_metrics = summarize_prefetch_fetch_metrics([])
        run_fetch_summary = summarize_required_data_prefetch_run(
            symbols_total=len(symbols),
            unique_symbols_total=len(fetch_syms),
            to_fetch=0,
            cached_unique_symbols=unique_cached_count,
            submitted_count=0,
            completed_count=0,
            skipped_count=0,
            failed_count=0,
            fetch_metrics=fetch_metrics,
            dedupe=symbol_plan.summary(),
        )
        return {
            'schema_version': SCHEMA_VERSION_V1,
            'symbols_total': len(symbols),
            'unique_symbols_total': len(fetch_syms),
            'deduped_count': symbol_plan.deduped_count,
            'dedupe': symbol_plan.summary(),
            'to_fetch': 0,
            'fetched': 0,
            'fetched_ok': 0,
            'cached': len(symbols),
            'cached_unique_symbols': unique_cached_count,
            'errors': 0,
            'skipped': 0,
            'max_workers': 0,
            'prefetch_max_workers': _resolve_prefetch_max_workers(cfg),
            'effective_prefetch_workers': 0,
            'submitted_count': 0,
            'completed_count': 0,
            'skipped_count': 0,
            'failed_count': 0,
            'execution_mode': _resolve_execution_mode(cfg),
            'fetch_metrics': fetch_metrics,
            'run_fetch_summary': run_fetch_summary,
            'symbols': [],
            'audit': [],
        }

    configured_max_workers = _resolve_prefetch_max_workers(cfg)
    max_workers = max(1, min(configured_max_workers, len(todo_cfgs)))
    fail_budget_consecutive, fail_budget_total = _resolve_failure_budget(cfg)

    def _dispatch(symbol_cfg: dict[str, Any]) -> dict[str, Any]:
        if execution_mode == 'subprocess':
            return _fetch_one(symbol_cfg)
        return _fetch_one_inprocess(
            symbol_cfg,
            base=base,
            shared_required=shared_required,
            opend_fetch_cfg=opend_fetch_cfg,
            batch_cfg=batch_cfg,
        )

    coordinator = PrefetchCoordinator(
        symbol_cfgs=todo_cfgs,
        max_workers=max_workers,
        execution_mode=execution_mode,
        fail_budget_consecutive=fail_budget_consecutive,
        fail_budget_total=fail_budget_total,
        dispatch_fn=_dispatch,
        cleanup_worker_fn=(_gateway_pool.close_current_thread if execution_mode == 'inprocess' else None),
        short_circuit_rate_limits=False,
        stop_on_failure_budget=False,
    )
    coordinator_result = coordinator.run()
    fetch_metrics = summarize_prefetch_fetch_metrics(coordinator_result.audit_items)
    run_fetch_summary = summarize_required_data_prefetch_run(
        symbols_total=len(symbols),
        unique_symbols_total=len(fetch_syms),
        to_fetch=len(todo_cfgs),
        cached_unique_symbols=unique_cached_count,
        submitted_count=coordinator_result.submitted_count,
        completed_count=coordinator_result.completed_count,
        skipped_count=coordinator_result.skipped,
        failed_count=coordinator_result.errors,
        fetch_metrics=fetch_metrics,
        dedupe=symbol_plan.summary(),
    )

    if execution_mode == 'inprocess':
        _gateway_pool.close_registered()

    return {
        'schema_version': SCHEMA_VERSION_V1,
        'symbols_total': len(symbols),
        'unique_symbols_total': len(fetch_syms),
        'deduped_count': symbol_plan.deduped_count,
        'dedupe': symbol_plan.summary(),
        'to_fetch': len(todo_cfgs),
        'cached_unique_symbols': unique_cached_count,
        'max_workers': max_workers,
        'prefetch_max_workers': configured_max_workers,
        'effective_prefetch_workers': max_workers,
        'execution_mode': execution_mode,
        'fetched_ok': coordinator_result.fetched_ok,
        'errors': coordinator_result.errors,
        'skipped': coordinator_result.skipped,
        'submitted_count': coordinator_result.submitted_count,
        'completed_count': coordinator_result.completed_count,
        'skipped_count': coordinator_result.skipped,
        'failed_count': coordinator_result.errors,
        'fail_budget_consecutive': fail_budget_consecutive,
        'fail_budget_total': fail_budget_total,
        'budget_triggered': coordinator_result.budget_triggered,
        'opend_rate_limit_classes': sorted(coordinator_result.opend_rate_limit_classes),
        'fetch_metrics': fetch_metrics,
        'run_fetch_summary': run_fetch_summary,
        'force_refresh': bool(force_refresh),
        'results': coordinator_result.results,
        'symbols': coordinator_result.symbol_items,
        'audit': coordinator_result.audit_items,
    }
