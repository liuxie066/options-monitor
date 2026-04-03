from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

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

    def _fetch_one(symbol_cfg: dict) -> tuple[str, bool, str]:
        symbol = str(symbol_cfg.get('symbol')).strip()
        if not symbol:
            return '', False, 'empty_symbol'
        if not _need_fetch(symbol):
            return symbol, True, 'cached'

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

        p = subprocess.run(cmd, cwd=str(base), capture_output=True, text=True)
        if p.returncode != 0:
            tail = ((p.stderr or p.stdout) or '').strip().splitlines()[-1:]
            return symbol, False, (tail[0] if tail else f'returncode={p.returncode}')
        return symbol, True, 'fetched'

    todo_cfgs = [it for it in syms if _need_fetch(str(it.get('symbol')).strip())]

    ok = 0
    err = 0
    results: dict[str, str] = {}

    if not todo_cfgs:
        return {
            'symbols_total': len(symbols),
            'fetched': 0,
            'cached': len(symbols),
            'errors': 0,
        }

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(todo_cfgs)))) as ex:
        futs = {ex.submit(_fetch_one, it): str(it.get('symbol')).strip() for it in todo_cfgs}
        for fut in as_completed(futs):
            sym, ok1, msg = fut.result()
            if not sym:
                continue
            results[sym] = msg
            if ok1:
                ok += 1
            else:
                err += 1

    return {
        'symbols_total': len(symbols),
        'to_fetch': len(todo_cfgs),
        'fetched_ok': ok,
        'errors': err,
        'results': results,
    }
