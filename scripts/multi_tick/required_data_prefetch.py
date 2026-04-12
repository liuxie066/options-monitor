from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from om.domain import (
    SCHEMA_VERSION_V1,
    normalize_tool_execution_payload,
)
from om.services import (
    ToolExecutionIntent,
    ToolExecutionService,
    adapt_opend_tool_payload,
)
from om.storage.repositories import state_repo
from scripts.io_utils import has_shared_required_data


def prefetch_required_data(*, vpy: Path, base: Path, cfg: dict, shared_required: Path) -> dict:
    syms = [it for it in (cfg.get('symbols') or []) if isinstance(it, dict) and it.get('symbol')]
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
        src = str(fetch_cfg.get('source') or 'yahoo').lower()
        limit_exp = int(fetch_cfg.get('limit_expirations') or symbol_cfg.get('fetch', {}).get('limit_expirations', 8) or 8)
        opt_types = 'put,call'

        cmd: list[str]
        if src == 'opend':
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
            try:
                u = str(symbol).strip().upper()
                spot_from_pm = (not u.endswith('.HK')) if (fetch_cfg.get('spot_from_portfolio_management') is None) else bool(fetch_cfg.get('spot_from_portfolio_management'))
                if spot_from_pm:
                    cmd.append('--spot-from-pm')
            except Exception:
                pass
        else:
            cmd = [
                str(vpy), 'scripts/fetch_market_data.py',
                '--symbols', symbol,
                '--output-root', str(shared_required),
                '--limit-expirations', str(limit_exp),
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

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(todo_cfgs)))) as ex:
        futs = {ex.submit(_fetch_one, it): str(it.get('symbol')).strip() for it in todo_cfgs}
        for fut in as_completed(futs):
            payload = fut.result()
            audit_items.append(payload)
            sym = str(payload.get('symbol') or '').strip()
            ok1 = bool(payload.get('ok'))
            msg = str(payload.get('message') or '')
            if not sym:
                continue
            results[sym] = msg
            status = str(payload.get('status') or '')
            if status == 'skipped':
                skipped += 1
            elif ok1:
                ok += 1
            else:
                err += 1

    return {
        'schema_version': SCHEMA_VERSION_V1,
        'symbols_total': len(symbols),
        'to_fetch': len(todo_cfgs),
        'fetched_ok': ok,
        'errors': err,
        'skipped': skipped,
        'results': results,
        'audit': audit_items,
    }
