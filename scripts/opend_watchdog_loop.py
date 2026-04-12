#!/usr/bin/env python3
"""Run OpenD watchdog in a loop.

This is a lightweight "keep OpenD alive" helper for environments without systemd/cron.

Behavior:
- Periodically checks OpenD health (port + get_global_state)
- If unhealthy and --ensure is set, tries to start OpenD via futu-agent
- Exits non-zero if unhealthy and --exit-on-unhealthy is set

Usage:
  ./.venv/bin/python scripts/opend_watchdog_loop.py --ensure
  ./.venv/bin/python scripts/opend_watchdog_loop.py --ensure --interval-sec 30

Recommended:
- Run it under a process supervisor (OpenClaw job runner, tmux, etc.)
"""

from __future__ import annotations

import argparse
import json
import time

from om.domain import normalize_watchdog_subprocess_output


def main() -> None:
    ap = argparse.ArgumentParser(description='OpenD watchdog loop')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=11111)
    ap.add_argument('--ensure', action='store_true', help='try to start OpenD if port closed')
    ap.add_argument('--interval-sec', type=int, default=60)
    ap.add_argument('--exit-on-unhealthy', action='store_true')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()

    # Import existing watchdog entry
    import subprocess
    cmd = [
        str(__import__('sys').executable),
        'scripts/opend_watchdog.py',
        '--host', str(args.host),
        '--port', str(args.port),
    ]
    if args.ensure:
        cmd.append('--ensure')
    if args.json:
        cmd.append('--json')

    while True:
        p = subprocess.run(cmd, capture_output=True, text=True)
        normalized = normalize_watchdog_subprocess_output(
            returncode=int(p.returncode),
            stdout=str(p.stdout or ""),
            stderr=str(p.stderr or ""),
        )
        # opend_watchdog.py may print futu logs; extract JSON block if present.
        txt = ((p.stdout or '') + '\n' + (p.stderr or '')).strip()
        out = txt.strip()
        s = out.find('{')
        e = out.rfind('}')
        if s >= 0 and e >= 0 and e > s:
            out_json = out[s:e+1]
        else:
            out_json = ''
        if args.json and out_json:
            try:
                obj = json.loads(out_json)
                ok = bool(obj.get('ok'))
            except Exception:
                ok = bool(normalized.get('ok'))
        else:
            ok = bool(normalized.get('ok'))

        print(out_json if out_json else out, flush=True)
        if not ok and args.exit_on_unhealthy:
            raise SystemExit(2)

        time.sleep(max(5, int(args.interval_sec)))


if __name__ == '__main__':
    main()
