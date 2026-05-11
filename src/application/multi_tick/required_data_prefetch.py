from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import threading
from typing import Any

from domain.domain.tool_boundary import SCHEMA_VERSION_V1, normalize_tool_execution_payload
from domain.services import (
    ToolExecutionIntent,
    ToolExecutionService,
    adapt_opend_tool_payload,
)
from domain.domain.fetch_source import is_futu_fetch_source, resolve_symbol_fetch_source
from domain.storage.repositories import state_repo
from src.application.config_loader import resolve_watchlist_config
from src.application.opend_fetch_config import resolve_opend_batch_config, resolve_opend_fetch_config
from src.application.opend_symbol_fetching import fetch_symbol, save_outputs
from src.infrastructure.io_utils import has_shared_required_data
from src.infrastructure.opend_retcodes import classify_opend_error
from domain.domain.symbol_identity import symbol_market


_thread_gateway = threading.local()
_thread_gateway_failures = threading.local()
_thread_gateway_registry_lock = threading.Lock()
_thread_gateway_registry: list[Any] = []


def _to_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return int(default)


def _resolve_prefetch_max_workers(cfg: dict[str, Any]) -> int:
    runtime = cfg.get("runtime") if isinstance(cfg.get("runtime"), dict) else {}
    prefetch_cfg = cfg.get("prefetch") if isinstance(cfg.get("prefetch"), dict) else {}
    v = runtime.get("prefetch_max_workers")
    if v is None:
        v = prefetch_cfg.get("max_workers")
    n = _to_int(v, 2)
    return max(1, n)


def _resolve_execution_mode(cfg: dict[str, Any]) -> str:
    runtime = cfg.get("runtime") if isinstance(cfg.get("runtime"), dict) else {}
    prefetch_cfg = runtime.get("prefetch") if isinstance(runtime.get("prefetch"), dict) else {}
    mode = str(prefetch_cfg.get("execution_mode") or "inprocess").strip().lower()
    return mode if mode in {"inprocess", "subprocess"} else "inprocess"


def _resolve_failure_budget(cfg: dict[str, Any]) -> tuple[int, int]:
    runtime = cfg.get("runtime") if isinstance(cfg.get("runtime"), dict) else {}
    prefetch_cfg = cfg.get("prefetch") if isinstance(cfg.get("prefetch"), dict) else {}
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


def _is_prefetch_error(payload: dict[str, Any]) -> bool:
    if not bool(payload.get("ok")):
        return True
    status = str(payload.get("status") or "").strip().lower()
    return status in {"error", "fail", "failed"}


def _is_opend_rate_limit_payload(payload: dict[str, Any]) -> bool:
    return classify_opend_error(payload).is_rate_limit


def _get_thread_gateway(host: str, port: int, chain_cache: bool):
    g = getattr(_thread_gateway, "gw", None)
    if g is not None:
        try:
            checker = getattr(g, "is_connected", None)
            if checker is None or checker():
                return g
        except Exception:
            pass
        try:
            g.close()
        except Exception:
            pass
        _thread_gateway.gw = None
    from src.infrastructure.futu_gateway import build_ready_futu_gateway

    g = build_ready_futu_gateway(host=host, port=int(port), is_option_chain_cache_enabled=bool(chain_cache))
    _thread_gateway.gw = g
    with _thread_gateway_registry_lock:
        _thread_gateway_registry.append(g)
    return g


def _close_thread_gateway():
    g = getattr(_thread_gateway, "gw", None)
    if g is not None:
        try:
            g.close()
        except Exception:
            pass
        _thread_gateway.gw = None
    _thread_gateway_failures.count = 0


def _close_registered_gateways() -> None:
    with _thread_gateway_registry_lock:
        gateways = list(_thread_gateway_registry)
        _thread_gateway_registry.clear()
    for g in gateways:
        try:
            g.close()
        except Exception:
            pass


def _is_thread_gateway_connection_error(exc: Exception) -> bool:
    text = str(exc or "")
    low = text.lower()
    if "ret_error" in low:
        return True
    keys = ("disconnected", "connection", "broken pipe", "connection reset", "timeout", "temporarily unavailable")
    return any(k in low for k in keys)


def _mark_thread_gateway_failure(exc: Exception) -> None:
    count = int(getattr(_thread_gateway_failures, "count", 0) or 0)
    if _is_thread_gateway_connection_error(exc):
        count += 1
    else:
        count = 0
    _thread_gateway_failures.count = count
    if count >= 2:
        _close_thread_gateway()


def _mark_thread_gateway_success() -> None:
    _thread_gateway_failures.count = 0


def _symbol_class(symbol: str) -> str:
    return symbol_market(symbol) or "US"


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
    try:
        gateway = _get_thread_gateway(host, port, True)
        payload0 = fetch_symbol(
            symbol,
            limit_expirations=limit_exp,
            host=host,
            port=port,
            base_dir=base,
            option_types='put,call',
            chain_cache=True,
            chain_cache_force_refresh=False,
            freshness_policy='cache_first',
            gateway=gateway,
            snapshot_batch_size=int(getattr(batch_cfg, 'market_snapshot', 0) or 0),
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
        _mark_thread_gateway_success()
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
        _mark_thread_gateway_failure(exc)
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
        if _is_opend_rate_limit_payload({"message": message}):
            payload['error_code'] = 'RATE_LIMIT'
    source_snapshot = adapt_opend_tool_payload(payload)
    payload["source_snapshot"] = source_snapshot
    try:
        state_repo.append_source_snapshot_event(base, source_snapshot)
    except Exception:
        pass
    return payload


def prefetch_required_data(*, vpy: Path, base: Path, cfg: dict, shared_required: Path, force_refresh: bool = False) -> dict:
    syms = [it for it in resolve_watchlist_config(cfg) if it.get('symbol')]
    symbols = [str(it.get('symbol')).strip() for it in syms if str(it.get('symbol')).strip()]

    raw_dir = (shared_required / 'raw').resolve()
    parsed_dir = (shared_required / 'parsed').resolve()
    raw_dir.mkdir(parents=True, exist_ok=True)
    parsed_dir.mkdir(parents=True, exist_ok=True)

    def _need_fetch(symbol: str) -> bool:
        if force_refresh:
            return True
        try:
            return (not has_shared_required_data(symbol, shared_required))
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
        if not _need_fetch(symbol):
            return normalize_tool_execution_payload(
                tool_name='required_data_prefetch',
                symbol=symbol,
                source='cache',
                limit_exp=8,
                status='cached',
                ok=True,
                message='cached',
                returncode=0,
            )

        fetch_cfg = (symbol_cfg.get('fetch') or {}) if isinstance(symbol_cfg, dict) else {}
        src, _decision = resolve_symbol_fetch_source(fetch_cfg)
        limit_exp = int(fetch_cfg.get('limit_expirations') or symbol_cfg.get('fetch', {}).get('limit_expirations', 8) or 8)
        opt_types = 'put,call'

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
            '--expiration-window-sec', str(expiration_fetch_cfg["window_sec"]),
            '--expiration-max-calls', str(expiration_fetch_cfg["max_calls"]),
            '--expiration-max-wait-sec', str(expiration_fetch_cfg["max_wait_sec"]),
            '--quiet',
        ]

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

    todo_cfgs = [it for it in syms if _need_fetch(str(it.get('symbol')).strip())]

    ok = 0
    err = 0
    skipped = 0
    results: dict[str, str] = {}
    audit_items: list[dict] = []

    if not todo_cfgs:
        return {
            'schema_version': SCHEMA_VERSION_V1,
            'symbols_total': len(symbols),
            'fetched': 0,
            'fetched_ok': 0,
            'cached': len(symbols),
            'errors': 0,
            'skipped': 0,
            'audit': [],
        }

    max_workers = min(_resolve_prefetch_max_workers(cfg), max(1, len(todo_cfgs)))
    fail_budget_consecutive, fail_budget_total = _resolve_failure_budget(cfg)
    fail_consecutive = 0
    fail_total = 0
    rate_limit_blocked_classes: set[str] = set()
    budget_triggered = False
    budget_summary_emitted = False

    def _budget_exceeded() -> bool:
        return (fail_consecutive >= fail_budget_consecutive) or (fail_total >= fail_budget_total)

    q = list(todo_cfgs)
    q_idx = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs: dict[Any, dict[str, Any]] = {}

        def _dispatch(symbol_cfg: dict) -> dict:
            if execution_mode == 'subprocess':
                return _fetch_one(symbol_cfg)
            return _fetch_one_inprocess(
                symbol_cfg,
                base=base,
                shared_required=shared_required,
                opend_fetch_cfg=opend_fetch_cfg,
                batch_cfg=batch_cfg,
            )

        def _submit_next() -> bool:
            nonlocal q_idx, skipped
            while q_idx < len(q):
                symbol_cfg = q[q_idx]
                q_idx += 1
                symbol = str((symbol_cfg or {}).get("symbol") or "").strip()
                fetch_cfg = (symbol_cfg.get("fetch") or {}) if isinstance(symbol_cfg, dict) else {}
                src, _decision = resolve_symbol_fetch_source(fetch_cfg)
                sym_class = _symbol_class(symbol)
                if is_futu_fetch_source(src) and sym_class in rate_limit_blocked_classes:
                    payload = normalize_tool_execution_payload(
                        tool_name='required_data_prefetch',
                        symbol=symbol,
                        source=src,
                        limit_exp=int(fetch_cfg.get('limit_expirations') or 8),
                        status='skipped',
                        ok=True,
                        message=f'opend_rate_limit_short_circuit class={sym_class}',
                        returncode=None,
                    )
                    audit_items.append(payload)
                    if symbol:
                        results[symbol] = str(payload.get("message") or "")
                    skipped += 1
                    continue
                fut = ex.submit(_dispatch, symbol_cfg)
                futs[fut] = symbol_cfg
                return True
            return False

        while len(futs) < max_workers and _submit_next():
            pass

        while futs:
            fut = next(as_completed(futs))
            symbol_cfg = futs.pop(fut)
            payload = fut.result()
            audit_items.append(payload)

            sym = str(payload.get('symbol') or '').strip()
            msg = str(payload.get('message') or '')
            if sym:
                results[sym] = msg
            status = str(payload.get('status') or '').strip().lower()
            if status == 'skipped':
                skipped += 1
            elif _is_prefetch_error(payload):
                err += 1
                fail_consecutive += 1
                fail_total += 1
                if is_futu_fetch_source(payload.get("source")) and _is_opend_rate_limit_payload(payload):
                    rate_limit_blocked_classes.add(_symbol_class(sym))
                if (not budget_triggered) and _budget_exceeded():
                    budget_triggered = True
                    if not budget_summary_emitted:
                        budget_summary_emitted = True
                        summary = normalize_tool_execution_payload(
                            tool_name='required_data_prefetch',
                            symbol='*',
                            source='budget',
                            limit_exp=0,
                            status='error',
                            ok=False,
                            message=(
                                f'prefetch_failure_budget_exceeded consecutive={fail_consecutive}/{fail_budget_consecutive} '
                                f'total={fail_total}/{fail_budget_total}; stopped_early'
                            ),
                            returncode=None,
                        )
                        audit_items.append(summary)
            else:
                ok += 1
                fail_consecutive = 0

            if budget_triggered:
                for pending in list(futs.keys()):
                    if pending.cancel():
                        cfg0 = futs.pop(pending)
                        symbol0 = str((cfg0 or {}).get("symbol") or "").strip()
                        payload0 = normalize_tool_execution_payload(
                            tool_name='required_data_prefetch',
                            symbol=symbol0,
                            source=str(((cfg0.get('fetch') or {}) if isinstance(cfg0, dict) else {}).get('source') or 'unknown'),
                            limit_exp=int((((cfg0.get('fetch') or {}) if isinstance(cfg0, dict) else {}).get('limit_expirations') or 8)),
                            status='skipped',
                            ok=True,
                            message='prefetch_stopped_by_failure_budget',
                            returncode=None,
                        )
                        audit_items.append(payload0)
                        if symbol0:
                            results[symbol0] = str(payload0.get("message") or "")
                        skipped += 1
                while q_idx < len(q):
                    cfg1 = q[q_idx]
                    q_idx += 1
                    symbol1 = str((cfg1 or {}).get("symbol") or "").strip()
                    payload1 = normalize_tool_execution_payload(
                        tool_name='required_data_prefetch',
                        symbol=symbol1,
                        source=str(((cfg1.get('fetch') or {}) if isinstance(cfg1, dict) else {}).get('source') or 'unknown'),
                        limit_exp=int((((cfg1.get('fetch') or {}) if isinstance(cfg1, dict) else {}).get('limit_expirations') or 8)),
                        status='skipped',
                        ok=True,
                        message='prefetch_stopped_by_failure_budget',
                        returncode=None,
                    )
                    audit_items.append(payload1)
                    if symbol1:
                        results[symbol1] = str(payload1.get("message") or "")
                    skipped += 1
                continue

            while len(futs) < max_workers and _submit_next():
                pass

        if execution_mode == 'inprocess':
            list(ex.map(lambda _idx: _close_thread_gateway(), range(max_workers)))

    if execution_mode == 'inprocess':
        _close_registered_gateways()

    return {
        'schema_version': SCHEMA_VERSION_V1,
        'symbols_total': len(symbols),
        'to_fetch': len(todo_cfgs),
        'max_workers': max_workers,
        'execution_mode': execution_mode,
        'fetched_ok': ok,
        'errors': err,
        'skipped': skipped,
        'fail_budget_consecutive': fail_budget_consecutive,
        'fail_budget_total': fail_budget_total,
        'budget_triggered': budget_triggered,
        'opend_rate_limit_classes': sorted(rate_limit_blocked_classes),
        'force_refresh': bool(force_refresh),
        'results': results,
        'audit': audit_items,
    }
