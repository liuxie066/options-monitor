#!/usr/bin/env python3
"""Check sell-put cash headroom and notify only when below threshold.

- Reuses scripts/query_sell_put_cash.py to compute base(CNY) free cash.
- Emits a short text payload; can be used in cron.

Policy:
- If cash_free_cny is None -> WARN (send)
- If cash_free_cny < threshold -> WARN (send)
- Else -> silent

This script does not send messages by itself; it prints output and exits 0.
Cron delivery can announce it on WARN.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def run_json(base: Path, cmd: list[str], timeout_sec: int = 120) -> dict:
    p = subprocess.run(cmd, cwd=str(base), timeout=timeout_sec, capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(p.returncode)

    # query_sell_put_cash.py prints logs + a JSON payload; take the last JSON object.
    lines = (p.stdout or '').splitlines()
    buf = []
    for ln in reversed(lines):
        if not ln.strip():
            continue
        buf.append(ln)
        if ln.strip().startswith('{'):
            break
    txt = '\n'.join(reversed(buf)).strip()
    try:
        return json.loads(txt)
    except Exception:
        raise RuntimeError(f"failed to parse json output (tail): {txt[:2000]}")


# Allow running as a script (python scripts/tools/xxx.py) without package install
# by ensuring repo root is on sys.path.
import sys
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.io_utils import money_cny


def _money_cny(v: float | None) -> str:
    # Keep this script's legacy formatting: 2 decimals, no (CNY).
    return money_cny(v, decimals=2, show_ccy=False)


def main():
    ap = argparse.ArgumentParser(description='Notify when sell-put cash free falls below threshold')
    ap.add_argument('--config', default='config.json')
    ap.add_argument('--pm-config', default='../portfolio-management/config.json')
    ap.add_argument('--market', default='富途')
    ap.add_argument('--account', required=True)
    ap.add_argument('--threshold-cny', type=float, default=100000.0)
    args = ap.parse_args()

    base = Path(__file__).resolve().parents[1]

    payload = run_json(
        base,
        [
            str(base / '.venv' / 'bin' / 'python'),
            'scripts/query_sell_put_cash.py',
            '--pm-config', str((base / args.pm_config).resolve()) if not Path(args.pm_config).is_absolute() else str(args.pm_config),
            '--market', args.market,
            '--account', args.account,
            '--format', 'json',
        ],
        timeout_sec=180,
    )

    free_cny = payload.get('cash_free_cny')
    avail_cny = payload.get('cash_available_cny')
    used_cny = payload.get('cash_secured_used_cny')

    level = 'OK'
    reason = ''
    if free_cny is None:
        level = 'WARN'
        reason = 'free cash unavailable'
    else:
        try:
            free_cny = float(free_cny)
        except Exception:
            free_cny = None
            level = 'WARN'
            reason = 'free cash parse failed'

    if free_cny is not None and free_cny < float(args.threshold_cny):
        level = 'WARN'
        reason = f"free<{args.threshold_cny:.0f}"

    if level == 'OK':
        return 0

    # Build concise message
    lines = []
    lines.append(f"[WARN] Sell Put 现金覆盖偏紧 ({args.account})")
    if reason:
        lines.append(f"reason: {reason}")
    lines.append(f"base(CNY)现金: {_money_cny(avail_cny)}")
    lines.append(f"担保占用(CNY): {_money_cny(used_cny)}")
    lines.append(f"free(CNY): {_money_cny(free_cny)}")

    # Top symbols breakdown (compact)
    by_sym = payload.get('cash_secured_by_symbol_by_ccy') or {}
    if isinstance(by_sym, dict) and by_sym:
        parts = []
        for sym, m in by_sym.items():
            if not isinstance(m, dict):
                continue
            hkd = m.get('HKD')
            usd = m.get('USD')
            cny = m.get('CNY')
            seg = []
            if hkd:
                seg.append(f"HKD {float(hkd):,.0f}")
            if usd:
                seg.append(f"USD {float(usd):,.0f}")
            if cny:
                seg.append(f"CNY {float(cny):,.0f}")
            if seg:
                parts.append(f"{sym}: " + '/'.join(seg))
        if parts:
            lines.append("占用明细: " + ' | '.join(parts[:5]))

    print('\n'.join(lines).strip())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
