from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import time
from typing import Any, Iterator

try:
    import fcntl
except Exception:  # pragma: no cover - non-POSIX fallback
    fcntl = None

from domain.domain.tool_boundary import SCHEMA_VERSION_V1, normalize_tool_execution_payload
from domain.services import (
    ToolExecutionIntent,
    ToolExecutionService,
    adapt_opend_tool_payload,
)
from domain.domain.fetch_source import resolve_symbol_fetch_source
from domain.storage.repositories import state_repo
from src.application.config_sections import (
    resolve_templates_config,
    resolve_watchlist_config,
)
from src.application.config_profiles import apply_profiles
from src.application.multi_tick.prefetch_coordinator import PrefetchCoordinator
from src.application.multi_tick.prefetch_coordinator import PrefetchCoordinatorResult
from src.application.opend_fetch_config import resolve_opend_batch_config, resolve_opend_fetch_config
from src.application.opend_symbol_fetching import fetch_symbol
from src.application.opend_symbol_outputs import save_outputs
from src.application.required_data_coverage import required_data_frame_covers_fetch_plan
from src.application.required_data_observability import (
    summarize_prefetch_fetch_metrics,
    summarize_required_data_prefetch_run,
)
from src.application.required_data_planning import (
    RequiredDataFetchPlanBundle,
    _merge_same_side_plans as _merge_required_data_side_plans,
    build_required_data_fetch_plan,
)
from src.application.required_data_prefetch_planning import (
    build_prefetch_budget_plan,
    build_prefetch_symbol_plan,
)
from src.application.yield_enhancement_config import (
    derive_yield_enhancement_policy,
    resolve_yield_enhancement_cfg,
)
from src.infrastructure.futu_gateway_pool import ThreadLocalFutuGatewayPool
from src.infrastructure.io_utils import has_shared_required_data as _has_shared_required_data, safe_read_csv
from src.infrastructure.opend_retcodes import classify_opend_error


_gateway_pool = ThreadLocalFutuGatewayPool()
_DEFAULT_PREFETCH_MAX_WORKERS = 2

# Compatibility surface for older tests and operational monkeypatches.
has_shared_required_data = _has_shared_required_data


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


def _build_prefetch_fetch_plan(
    symbol_cfg: dict[str, Any],
    *,
    base: Path,
    shared_required: Path,
    opend_fetch_cfg: dict[str, Any],
) -> RequiredDataFetchPlanBundle:
    source_cfgs = symbol_cfg.get("_prefetch_source_symbol_cfgs")
    if isinstance(source_cfgs, list) and len(source_cfgs) > 1:
        bundles = [
            _build_single_prefetch_fetch_plan(
                item,
                base=base,
                shared_required=shared_required,
                opend_fetch_cfg=opend_fetch_cfg,
            )
            for item in source_cfgs
            if isinstance(item, dict)
        ]
        if bundles:
            spot_reference = next(
                (bundle.spot_reference for bundle in bundles if bundle.spot_reference is not None),
                None,
            )
            return RequiredDataFetchPlanBundle(
                symbol=str(symbol_cfg.get("symbol") or bundles[0].symbol),
                spot_reference=spot_reference,
                side_plans=_merge_required_data_side_plans([
                    side_plan
                    for bundle in bundles
                    for side_plan in bundle.side_plans
                ]),
                merged_specs=[
                    spec
                    for bundle in bundles
                    for spec in bundle.merged_specs
                ],
            )
    return _build_single_prefetch_fetch_plan(
        symbol_cfg,
        base=base,
        shared_required=shared_required,
        opend_fetch_cfg=opend_fetch_cfg,
    )


def _build_single_prefetch_fetch_plan(
    symbol_cfg: dict[str, Any],
    *,
    base: Path,
    shared_required: Path,
    opend_fetch_cfg: dict[str, Any],
) -> RequiredDataFetchPlanBundle:
    symbol = str(symbol_cfg.get("symbol") or "").strip()
    fetch_cfg = _as_dict(symbol_cfg.get("fetch"))
    limit_exp = int(fetch_cfg.get("limit_expirations") or 8)
    sell_put_cfg = _as_dict(symbol_cfg.get("sell_put"))
    sell_call_cfg = _as_dict(symbol_cfg.get("sell_call"))
    yield_enhancement_cfg = resolve_yield_enhancement_cfg(symbol_cfg)
    yield_policy = derive_yield_enhancement_policy(yield_enhancement_cfg, sell_put_cfg)
    want_put = bool(sell_put_cfg.get("enabled", False))
    want_call = bool(sell_call_cfg.get("enabled", False))
    want_yield_enhancement = bool(yield_policy.enabled)
    snapshot_cfg = _as_dict(opend_fetch_cfg.get("market_snapshot"))
    expiration_cfg = _as_dict(opend_fetch_cfg.get("option_expiration"))
    return build_required_data_fetch_plan(
        base=base,
        required_data_dir=shared_required,
        symbol=symbol,
        limit_expirations=limit_exp,
        want_put=bool(want_put or want_yield_enhancement),
        want_call=want_call,
        sell_put_cfg=sell_put_cfg,
        sell_call_cfg=sell_call_cfg,
        yield_enhancement_cfg=yield_enhancement_cfg,
        symbol_cfg=symbol_cfg,
        fetch_host=str(fetch_cfg.get("host") or "127.0.0.1"),
        fetch_port=_to_int(fetch_cfg.get("port") or 11111, 11111),
        snapshot_max_wait_sec=float(snapshot_cfg.get("max_wait_sec") or 30.0),
        snapshot_window_sec=float(snapshot_cfg.get("window_sec") or 30.0),
        snapshot_max_calls=int(snapshot_cfg.get("max_calls") or 60),
        expiration_max_wait_sec=float(expiration_cfg.get("max_wait_sec") or 30.0),
        expiration_window_sec=float(expiration_cfg.get("window_sec") or 30.0),
        expiration_max_calls=int(expiration_cfg.get("max_calls") or 60),
    )


def _prefetch_fetch_kwargs_from_plan(fetch_plan: RequiredDataFetchPlanBundle | None) -> dict[str, Any]:
    if fetch_plan is None or not fetch_plan.side_plans:
        return {
            "option_types": "put,call",
            "min_dte": None,
            "max_dte": None,
            "side_strike_windows": None,
            "explicit_expirations": None,
            "spot_override": None,
            "include_realized_volatility": False,
        }

    option_types: list[str] = []
    min_dtes: list[int] = []
    max_dtes: list[int] = []
    expirations: list[str] = []
    side_strike_windows: dict[str, dict[str, float | None]] = {}
    for side_plan in fetch_plan.side_plans:
        option_type = str(side_plan.option_type)
        if option_type not in option_types:
            option_types.append(option_type)
        if side_plan.min_dte is not None:
            min_dtes.append(int(side_plan.min_dte))
        if side_plan.max_dte is not None:
            max_dtes.append(int(side_plan.max_dte))
        for expiration in side_plan.explicit_expirations:
            exp = str(expiration)
            if exp and exp not in expirations:
                expirations.append(exp)
        side_strike_windows[option_type] = {
            "min_strike": side_plan.strike_window.min_strike,
            "max_strike": side_plan.strike_window.max_strike,
        }

    return {
        "option_types": ",".join([side for side in ("put", "call") if side in set(option_types)]) or "put,call",
        "min_dte": min(min_dtes) if min_dtes else None,
        "max_dte": max(max_dtes) if max_dtes else None,
        "side_strike_windows": side_strike_windows or None,
        "explicit_expirations": expirations or None,
        "spot_override": fetch_plan.spot_reference,
        "include_realized_volatility": any(bool(spec.include_realized_volatility) for spec in fetch_plan.merged_specs),
    }


def _fetch_one_inprocess(
    symbol_cfg: dict[str, Any],
    *,
    base: Path,
    shared_required: Path,
    opend_fetch_cfg: dict[str, Any],
    batch_cfg: Any,
    fetch_plan: RequiredDataFetchPlanBundle | None = None,
) -> dict[str, Any]:
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
    fetch_kwargs = _prefetch_fetch_kwargs_from_plan(fetch_plan)
    try:
        gateway = _gateway_pool.get_gateway(host=host, port=port, chain_cache=True)
        payload0 = fetch_symbol(
            symbol,
            limit_expirations=limit_exp,
            host=host,
            port=port,
            spot_override=fetch_kwargs.get("spot_override"),
            base_dir=base,
            option_types=str(fetch_kwargs["option_types"]),
            side_strike_windows=fetch_kwargs.get("side_strike_windows"),
            min_dte=fetch_kwargs.get("min_dte"),
            max_dte=fetch_kwargs.get("max_dte"),
            explicit_expirations=fetch_kwargs.get("explicit_expirations"),
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
            include_realized_volatility=bool(fetch_kwargs.get("include_realized_volatility")),
        )
        _gateway_pool.mark_success()
        save_outputs(base, symbol, payload0, output_root=shared_required)
        raw_meta = payload0.get('meta')
        meta = raw_meta if isinstance(raw_meta, dict) else {}
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


def _load_cached_required_data_frame(symbol: str, shared_required: Path) -> Any | None:
    sym = str(symbol).strip()
    if not sym:
        return None
    raw_src = shared_required / 'raw' / f"{sym}_required_data.json"
    parsed_src = shared_required / 'parsed' / f"{sym}_required_data.csv"
    try:
        if not (raw_src.exists() and raw_src.stat().st_size > 0):
            return None
        if not (parsed_src.exists() and parsed_src.stat().st_size > 0):
            return None
        return safe_read_csv(parsed_src)
    except Exception:
        return None


def _merge_coordinator_results(
    results: list[PrefetchCoordinatorResult],
    *,
    fail_budget_consecutive: int,
    fail_budget_total: int,
) -> PrefetchCoordinatorResult:
    merged = PrefetchCoordinatorResult(
        fail_budget_consecutive=fail_budget_consecutive,
        fail_budget_total=fail_budget_total,
    )
    for result in results:
        merged.fetched_ok += int(result.fetched_ok)
        merged.errors += int(result.errors)
        merged.skipped += int(result.skipped)
        merged.submitted_count += int(result.submitted_count)
        merged.completed_count += int(result.completed_count)
        merged.budget_triggered = bool(merged.budget_triggered or result.budget_triggered)
        merged.opend_rate_limit_classes.update(result.opend_rate_limit_classes)
        merged.opend_rate_limit_items.extend(result.opend_rate_limit_items)
        merged.results.update(result.results)
        merged.audit_items.extend(result.audit_items)
    return merged


def _sleep_after_rate_limit_wave(wait_sec: float) -> None:
    time.sleep(max(0.0, float(wait_sec)))


def _has_option_chain_rate_limit(items: list[dict[str, Any]]) -> bool:
    for item in items:
        endpoint = str(item.get("endpoint") or "").strip().lower()
        if endpoint in {"option_chain", "opend", ""}:
            return True
    return False


@contextmanager
def _required_data_prefetch_file_lock(base: Path) -> Iterator[None]:
    lock_path = Path(base) / "output_shared" / "state" / "required_data_prefetch.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_fp:
        if fcntl is not None:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)


def prefetch_required_data(
    *,
    vpy: Path,
    base: Path,
    repo_root: Path | None = None,
    cfg: dict[str, Any],
    shared_required: Path,
    force_refresh: bool = False,
) -> dict[str, Any]:
    with _required_data_prefetch_file_lock(base):
        return _prefetch_required_data_unlocked(
            vpy=vpy,
            base=base,
            repo_root=repo_root,
            cfg=cfg,
            shared_required=shared_required,
            force_refresh=force_refresh,
        )


def _prefetch_required_data_unlocked(
    *,
    vpy: Path,
    base: Path,
    repo_root: Path | None = None,
    cfg: dict[str, Any],
    shared_required: Path,
    force_refresh: bool = False,
) -> dict[str, Any]:
    profiles = resolve_templates_config(cfg)
    syms = [apply_profiles(it, profiles) for it in resolve_watchlist_config(cfg) if it.get('symbol')]
    symbols = [str(it.get('symbol')).strip() for it in syms if str(it.get('symbol')).strip()]
    symbol_plan = build_prefetch_symbol_plan(syms)
    fetch_syms = symbol_plan.symbol_cfgs

    raw_dir = (shared_required / 'raw').resolve()
    parsed_dir = (shared_required / 'parsed').resolve()
    raw_dir.mkdir(parents=True, exist_ok=True)
    parsed_dir.mkdir(parents=True, exist_ok=True)

    process_root = (repo_root or base).resolve()
    exec_service = ToolExecutionService(base=base)
    opend_fetch_cfg = _resolve_opend_fetch_cfg(cfg)
    batch_cfg = resolve_opend_batch_config(cfg)
    execution_mode = _resolve_execution_mode(cfg)
    option_chain_fetch_cfg = opend_fetch_cfg["option_chain"]
    snapshot_fetch_cfg = opend_fetch_cfg["market_snapshot"]
    expiration_fetch_cfg = opend_fetch_cfg["option_expiration"]
    fetch_plan_cache: dict[int, RequiredDataFetchPlanBundle] = {}

    def _get_fetch_plan(symbol_cfg: dict[str, Any]) -> RequiredDataFetchPlanBundle:
        cache_key = id(symbol_cfg)
        cached = fetch_plan_cache.get(cache_key)
        if cached is not None:
            return cached
        fetch_plan = _build_prefetch_fetch_plan(
            symbol_cfg,
            base=base,
            shared_required=shared_required,
            opend_fetch_cfg=opend_fetch_cfg,
        )
        fetch_plan_cache[cache_key] = fetch_plan
        return fetch_plan

    def _need_fetch(symbol_cfg: dict[str, Any]) -> bool:
        symbol = str(symbol_cfg.get('symbol')).strip()
        if not symbol:
            return True
        if force_refresh:
            return True
        try:
            cached_df = _load_cached_required_data_frame(symbol, shared_required)
            if cached_df is None:
                return True
            fetch_plan = _get_fetch_plan(symbol_cfg)
            return not required_data_frame_covers_fetch_plan(
                df=cached_df,
                fetch_plan=fetch_plan,
            )
        except Exception:
            return True

    def _fetch_one(symbol_cfg: dict[str, Any]) -> dict[str, Any]:
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
        fetch_plan = _get_fetch_plan(symbol_cfg)
        fetch_kwargs = _prefetch_fetch_kwargs_from_plan(fetch_plan)
        opt_types = str(fetch_kwargs["option_types"])

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
        if fetch_kwargs.get("spot_override") is not None:
            cmd.extend(['--spot', str(fetch_kwargs["spot_override"])])
        if fetch_kwargs.get("min_dte") is not None:
            cmd.extend(['--min-dte', str(fetch_kwargs["min_dte"])])
        if fetch_kwargs.get("max_dte") is not None:
            cmd.extend(['--max-dte', str(fetch_kwargs["max_dte"])])
        if fetch_kwargs.get("side_strike_windows"):
            cmd.extend(['--side-strike-windows-json', json.dumps(fetch_kwargs["side_strike_windows"])])
        if fetch_kwargs.get("explicit_expirations"):
            cmd.extend(['--explicit-expirations', *[str(exp) for exp in fetch_kwargs["explicit_expirations"]]])
        if fetch_kwargs.get("include_realized_volatility"):
            cmd.append('--include-realized-volatility')

        payload = exec_service.execute(
            ToolExecutionIntent(
                tool_name='required_data_prefetch',
                symbol=symbol,
                source=src,
                limit_exp=limit_exp,
                cmd=cmd,
                cwd=process_root,
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
    budget_plan = build_prefetch_budget_plan(todo_cfgs, option_chain_cfg=option_chain_fetch_cfg)
    option_chain_fetch_cfg = dict(option_chain_fetch_cfg)
    option_chain_fetch_cfg["max_calls"] = int(budget_plan.safe_option_chain_calls_per_window)
    opend_fetch_cfg = dict(opend_fetch_cfg)
    opend_fetch_cfg["option_chain"] = option_chain_fetch_cfg

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
            'prefetch_budget_plan': budget_plan.summary(),
            'opend_rate_limit_classes': [],
            'opend_rate_limit_items': [],
            'rate_limit_cooldowns': [],
            'symbols': [],
            'audit': [],
        }

    configured_max_workers = _resolve_prefetch_max_workers(cfg)
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
            fetch_plan=_get_fetch_plan(symbol_cfg),
        )

    wave_results: list[PrefetchCoordinatorResult] = []
    rate_limit_cooldowns: list[dict[str, Any]] = []
    effective_max_workers = 0
    for wave_idx, wave in enumerate(budget_plan.waves):
        wave_workers = max(1, min(configured_max_workers, len(wave.symbol_cfgs)))
        effective_max_workers = max(effective_max_workers, wave_workers)
        coordinator = PrefetchCoordinator(
            symbol_cfgs=wave.symbol_cfgs,
            max_workers=wave_workers,
            execution_mode=execution_mode,
            fail_budget_consecutive=fail_budget_consecutive,
            fail_budget_total=fail_budget_total,
            dispatch_fn=_dispatch,
            cleanup_worker_fn=(_gateway_pool.close_current_thread if execution_mode == 'inprocess' else None),
            short_circuit_rate_limits=False,
            stop_on_failure_budget=False,
        )
        wave_result = coordinator.run()
        wave_results.append(wave_result)
        if wave_idx < len(budget_plan.waves) - 1 and _has_option_chain_rate_limit(wave_result.opend_rate_limit_items):
            wait_sec = float(option_chain_fetch_cfg.get("window_sec") or 30.0)
            rate_limit_cooldowns.append(
                {
                    "after_wave": int(wave.index),
                    "reason": "opend_rate_limit",
                    "wait_sec": wait_sec,
                }
            )
            _sleep_after_rate_limit_wave(wait_sec)
    max_workers = effective_max_workers
    coordinator_result = _merge_coordinator_results(
        wave_results,
        fail_budget_consecutive=fail_budget_consecutive,
        fail_budget_total=fail_budget_total,
    )
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
        'opend_rate_limit_items': list(coordinator_result.opend_rate_limit_items),
        'prefetch_budget_plan': budget_plan.summary(),
        'rate_limit_cooldowns': rate_limit_cooldowns,
        'fetch_metrics': fetch_metrics,
        'run_fetch_summary': run_fetch_summary,
        'force_refresh': bool(force_refresh),
        'results': coordinator_result.results,
        'symbols': coordinator_result.symbol_items,
        'audit': coordinator_result.audit_items,
    }
