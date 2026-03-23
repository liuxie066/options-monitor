#!/usr/bin/env python3
"""Chat-friendly option intake -> option_positions writer.

Usage examples:
  ./scripts/option_intake.py --text "期权：腾讯20260330 put，strike500，成本5.425每股，乘数100，short 10张，sy，HKD" --dry-run
  ./scripts/option_intake.py --text "期权：腾讯20260330 put，strike500，成本5.425每股，乘数100，short 10张，sy，HKD" --apply

Design:
- Parses message with scripts/parse_option_message.py
- Writes via scripts/option_positions.py add
- Default dry-run (safe). Use --apply to persist.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def run_capture(cmd: list[str], cwd: Path, timeout_sec: int = 120) -> tuple[int, str, str]:
    p = subprocess.run(cmd, cwd=str(cwd), timeout=timeout_sec, capture_output=True, text=True)
    return p.returncode, (p.stdout or ''), (p.stderr or '')


def main():
    ap = argparse.ArgumentParser(description='Option intake (parse + write)')
    ap.add_argument('--text', required=True)
    ap.add_argument('--market', default='富途')
    ap.add_argument('--dry-run', action='store_true', help='default behavior if neither --dry-run nor --apply specified')
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()

    base = Path(__file__).resolve().parents[1]
    py = str(base / '.venv' / 'bin' / 'python')

    # default safe mode
    if (not args.dry_run) and (not args.apply):
        args.dry_run = True

    code, out, err = run_capture([py, 'scripts/parse_option_message.py', '--text', args.text], cwd=base, timeout_sec=30)
    if code != 0:
        print(err.strip() or out.strip())
        return code
    parsed = json.loads(out)
    if not parsed.get('ok'):
        print('[PARSE_FAIL] missing: ' + ','.join(parsed.get('missing') or []))
        print(json.dumps(parsed, ensure_ascii=False, indent=2))
        return 2

    p = parsed['parsed']
    # Build add command
    cmd = [
        py, 'scripts/option_positions.py', 'add',
        '--market', args.market,
        '--account', p['account'],
        '--symbol', p['symbol'],
        '--option-type', p['option_type'],
        '--side', p['side'],
        '--contracts', str(int(p['contracts'])),
        '--currency', p['currency'],
        '--strike', str(float(p['strike'])),
        '--multiplier', str(int(p['multiplier'])),
        '--exp', p['exp'],
    ]
    if p.get('premium_per_share') is not None:
        cmd += ['--premium-per-share', str(float(p['premium_per_share']))]

    cmd += ['--note', f"user_input: {parsed.get('raw')}"]

    if args.dry_run and (not args.apply):
        cmd.append('--dry-run')

    rc = subprocess.call(cmd, cwd=str(base))
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
