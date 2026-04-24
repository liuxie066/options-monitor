from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from domain.domain import (
    SCHEMA_VERSION_V1,
    normalize_tool_execution_payload,
)
from domain.services import (
    ToolExecutionIntent,
    ToolExecutionService,
    adapt_opend_tool_payload,
)
from domain.domain.fetch_source import is_futu_fetch_source, resolve_symbol_fetch_source
from domain.storage.repositories import state_repo
from scripts.config_loader import resolve_watchlist_config
from scripts.io_utils import has_shared_required_data


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


def _is_prefetch_error(payload: dict[str, Any]) -> bool:
    if not bool(payload.get("ok")):
        return True
    status = str(payload.get("status") or "").strip().lower()
    return status in {"error", "fail", "failed"}


def _is_opend_rate_limit_payload(payload: dict[str, Any]) -> bool:
    code = str(payload.get("error_code") or "").strip().upper()
    message = str(payload.get("message") or "").strip()
    low = message.lower()
    if "RATE_LIMIT" in code:
        return True
    keys = ("频率太高", "最多10次", "rate limit", "too frequent", "频率限制", "请求过快")
    return any(k in message for k in keys) or any(k in low for k in ("rate limit", "too frequent"))


def _symbol_class(symbol: str) -> str:
    s = str(symbol or "").strip().upper()
    return "HK" if s.endswith(".HK") else "US"


def prefetch_required_data(*, vpy: Path, base: Path, cfg: dict, shared_required: Path) -> dict:
    syms = [it for it in resolve_watchlist_config(cfg) if it.get('symbol')]
    symbols = [str(it.get('symbol')).strip() for it in syms if str(it.get('symbol')).strip()]

    raw_dir = (shared_required / 'raw').resolve()
    parsed_dir = (shared_required / 'parsed').resolve()
    raw_dir.mkdir(parents=True, exist_ok=True)
    parsed_dir.mkdir(parents=True, exist_ok=True)

    def _need_fetch(symbol: str) -> bool:
        try:
            return (not has_shared_required_data(symbol, shared_required))
        except Exception:
            return True

    exec_service = ToolExecutionService(base=base)

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
            str(vpy), 'scripts/fetch_market_data_opend.py',
            '--symbols', symbol,
            '--limit-expirations', str(limit_exp),
            '--host', str(fetch_cfg.get('host') or '127.0.0.1'),
            '--port', str(int(fetch_cfg.get('port') or 11111)),
            '--option-types', opt_types,
            '--output-root', str(shared_required),
            '--chain-cache',
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

        def _submit_next() -> bool:
            nonlocal q_idx, skipped
            while q_idx < len(q):
                symbol_cfg = q[q_idx]
                q_idx += 1
                symbol = str((symbol_cfg or {}).get("symbol") or "").strip()
                fetch_cfg = (symbol_cfg.get("fetch") or {}) if isinstance(symbol_cfg, dict) else {}
                src, _decision = resolve_symbol_fetch_source(fetch_cfg)
                sym_class = _symbol_class(symbol)
                if src == "opend" and sym_class in rate_limit_blocked_classes:
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
                fut = ex.submit(_fetch_one, symbol_cfg)
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

    return {
        'schema_version': SCHEMA_VERSION_V1,
        'symbols_total': len(symbols),
        'to_fetch': len(todo_cfgs),
        'max_workers': max_workers,
        'fetched_ok': ok,
        'errors': err,
        'skipped': skipped,
        'fail_budget_consecutive': fail_budget_consecutive,
        'fail_budget_total': fail_budget_total,
        'budget_triggered': budget_triggered,
        'opend_rate_limit_classes': sorted(rate_limit_blocked_classes),
        'results': results,
        'audit': audit_items,
    }
