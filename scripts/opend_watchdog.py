#!/usr/bin/env python3
from __future__ import annotations

"""OpenD watchdog for options-monitor.

We do NOT keep futu-api contexts alive. We keep the OpenD process and login state healthy.

Checks:
- port 11111 reachable
- get_global_state program_status_type == READY
- qot_logined == True
- trd_logined == True

Actions:
- If port closed: optionally start OpenD console via futu-agent start.sh

Exit codes:
- 0: healthy
- 2: unhealthy (needs human action)
"""

import argparse
import json
import socket
import subprocess
import time
from dataclasses import dataclass


@dataclass
class Health:
    ok: bool
    ports_open: bool
    state: dict | None = None
    error: str | None = None
    action_taken: str | None = None


def port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        s = socket.socket()
        s.settimeout(timeout)
        s.connect((host, int(port)))
        s.close()
        return True
    except Exception:
        return False


def get_global_state(host: str, port: int) -> dict:
    from futu import OpenQuoteContext, RET_OK

    ctx = OpenQuoteContext(host=host, port=int(port))
    try:
        ret, data = ctx.get_global_state()
        if ret != RET_OK:
            raise RuntimeError(f"get_global_state ret={ret} data={data}")
        if not isinstance(data, dict):
            raise RuntimeError(f"get_global_state invalid: {data}")
        return data
    finally:
        try:
            ctx.close()
        except Exception:
            pass


def try_start_opend() -> tuple[bool, str]:
    start_sh = '/home/node/.openclaw/workspace/skills/futu-agent/scripts/start.sh'
    try:
        p = subprocess.run(['bash', start_sh], capture_output=True, text=True, timeout=20)
        out = ((p.stdout or '') + '\n' + (p.stderr or '')).strip()
        return (p.returncode == 0, out[-500:])
    except Exception as e:
        return (False, f"start_opend exception: {type(e).__name__}: {e}")


def main():
    ap = argparse.ArgumentParser(description='OpenD watchdog')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=11111)
    ap.add_argument('--ensure', action='store_true', help='try to start OpenD if port closed')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()

    h = Health(ok=False, ports_open=False)

    if not port_open(args.host, args.port):
        h.ports_open = False
        if args.ensure:
            ok, _msg = try_start_opend()
            h.action_taken = 'start_opend' if ok else 'start_opend_failed'
            time.sleep(1.0)

        if not port_open(args.host, args.port):
            h.error = f"OpenD port not open: {args.host}:{args.port}"
            print(json.dumps(h.__dict__, ensure_ascii=False) if args.json else f"[OPEND_UNHEALTHY] {h.error}")
            raise SystemExit(2)

    h.ports_open = True

    try:
        st = get_global_state(args.host, args.port)
        h.state = st
        ready = (st.get('program_status_type') in (None, '', 'READY'))
        qot = bool(st.get('qot_logined', True))
        trd = bool(st.get('trd_logined', True))
        if ready and qot and trd:
            h.ok = True
        else:
            h.error = f"OpenD not ready/logged in: READY={ready} qot={qot} trd={trd}"
    except Exception as e:
        h.error = f"get_global_state failed: {type(e).__name__}: {e}"

    print(json.dumps(h.__dict__, ensure_ascii=False) if args.json else ('[OPEND_OK]' if h.ok else f"[OPEND_UNHEALTHY] {h.error}"))
    raise SystemExit(0 if h.ok else 2)


if __name__ == '__main__':
    main()
