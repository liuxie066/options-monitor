#!/usr/bin/env python3
from __future__ import annotations

"""OpenD watchdog for options-monitor.

Checks:
- port 11111 reachable
- get_global_state program_status_type == READY
- qot_logined == True

Outputs:
- Structured JSON with explicit error_code + short message

Exit codes:
- 0: healthy
- 2: unhealthy
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Health:
    ok: bool
    ports_open: bool
    state: dict | None = None
    error: str | None = None
    action_taken: str | None = None
    error_code: str | None = None
    message: str | None = None


def port_open(host: str, port: int, timeout: float = 0.8) -> bool:
    try:
        s = socket.socket()
        s.settimeout(timeout)
        s.connect((host, int(port)))
        s.close()
        return True
    except Exception:
        return False


def _looks_like_rate_limit(msg: str) -> bool:
    s = (msg or '')
    sl = s.lower()
    keys = ['频率太高', '最多10次', 'too frequent', 'rate limit', '频率限制', '请求过快']
    return any(k in s for k in keys) or any(k in sl for k in ['too frequent', 'rate limit'])


def _looks_like_phone_verify(msg: str) -> bool:
    s = (msg or '')
    sl = s.lower()
    keys = ['phone verify', 'phone verification', 'verify code', '验证码', '手机验证', '短信验证', 'not login', 'not logged']
    return any(k in s for k in ['验证码', '手机验证', '短信验证']) or any(k in sl for k in keys)


def classify_watchdog_result(state: dict | None, error_text: str | None) -> tuple[str, str]:
    """Map watchdog result to stable error_code + short human message."""
    err = str(error_text or '').strip()
    st = state if isinstance(state, dict) else {}

    if err:
        low = err.lower()
        if 'port not open' in low or 'cannot connect' in low or 'connection refused' in low:
            return ('OPEND_PORT_CLOSED', 'OpenD 端口不可达')
        if _looks_like_rate_limit(err):
            return ('OPEND_RATE_LIMIT', 'OpenD 请求频率受限')
        if _looks_like_phone_verify(err):
            return ('OPEND_NEEDS_PHONE_VERIFY', 'OpenD 需要手机验证码登录')
        if 'not ready' in low:
            return ('OPEND_NOT_READY', 'OpenD 未就绪')
        if 'quote not logged in' in low or 'qot' in low:
            return ('OPEND_QOT_NOT_LOGINED', 'OpenD 行情未登录')

    # Fallback to state-based mapping.
    status = st.get('program_status_type')
    if status not in (None, '', 'READY'):
        # In practice, non-READY frequently means waiting for phone verify.
        if _looks_like_phone_verify(str(st)):
            return ('OPEND_NEEDS_PHONE_VERIFY', 'OpenD 需要手机验证码登录')
        return ('OPEND_NOT_READY', 'OpenD 未就绪')

    if not bool(st.get('qot_logined', True)):
        return ('OPEND_QOT_NOT_LOGINED', 'OpenD 行情未登录')

    return ('OPEND_API_ERROR', 'OpenD 接口异常')


def _looks_like_disconnect_error(msg: str) -> bool:
    s = (msg or '').strip()
    sl = s.lower()

    # Do not treat phone-verify errors as disconnect.
    if _looks_like_phone_verify(s):
        return False

    keys = [
        'econnrefused',
        'connection refused',
        'econnreset',
        'connection reset',
        'broken pipe',
        'timed out',
        'timeout',
        'socket',
        'disconnected',
        'remote closed',
        'callclose',
        'eof',
    ]
    return any(k in sl for k in keys)


def get_global_state_once(host: str, port: int) -> dict:
    # Prefer repo venv if available
    vpy = Path(__file__).resolve().parents[1] / '.venv' / 'bin' / 'python'
    if vpy.exists() and str(vpy) != sys.executable:
        os.execv(str(vpy), [str(vpy)] + sys.argv)

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


def get_global_state(host: str, port: int, *, retry_once: bool = True) -> tuple[dict | None, str | None, str | None]:
    """Get global state with a single reconnect-style retry on disconnect errors.

    Returns (state, error_text, action_taken).

    Policy:
    - For "needs phone verification" errors: fail-fast (no retry).
    - For rate-limit errors: no retry.
    - For disconnect-like errors: retry once after a short sleep.
    """
    try:
        st = get_global_state_once(host, port)
        return (st, None, None)
    except Exception as e:
        err1 = f"get_global_state failed: {type(e).__name__}: {e}"
        code1, _ = classify_watchdog_result(None, err1)

        # Fail-fast on phone verification.
        if code1 == 'OPEND_NEEDS_PHONE_VERIFY':
            return (None, err1, 'fail_fast_phone_verify')

        # Rate-limit won't be fixed by retry.
        if code1 == 'OPEND_RATE_LIMIT':
            return (None, err1, 'no_retry_rate_limit')

        if (not retry_once) or (not _looks_like_disconnect_error(err1)):
            return (None, err1, None)

        # One-shot reconnect: sleep a bit then retry.
        time.sleep(0.3)
        try:
            st2 = get_global_state_once(host, port)
            return (st2, None, 'retry_once')
        except Exception as e2:
            err2 = f"get_global_state failed after retry: {type(e2).__name__}: {e2}"
            return (None, err1 + ' | ' + err2, 'retry_once_failed')


def try_start_opend() -> tuple[bool, str]:
    start_sh = '/home/node/.openclaw/workspace/skills/futu-agent/scripts/start.sh'
    try:
        p = subprocess.run(['bash', start_sh], capture_output=True, text=True, timeout=20)
        out = ((p.stdout or '') + '\n' + (p.stderr or '')).strip()
        return (p.returncode == 0, out[-500:])
    except Exception as e:
        return (False, f"start_opend exception: {type(e).__name__}: {e}")


def _emit(h: Health, as_json: bool) -> None:
    if not h.error_code and not h.ok:
        h.error_code, h.message = classify_watchdog_result(h.state, h.error)
    if as_json:
        print(json.dumps(h.__dict__, ensure_ascii=False))
    else:
        if h.ok:
            print('[OPEND_OK] OpenD healthy')
        else:
            msg = h.message or h.error or 'OpenD unhealthy'
            code = h.error_code or 'OPEND_API_ERROR'
            print(f"[OPEND_UNHEALTHY] {code}: {msg}")


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
            h.error_code, h.message = classify_watchdog_result(None, h.error)
            _emit(h, args.json)
            raise SystemExit(2)

    h.ports_open = True

    try:
        st, err, action = get_global_state(args.host, args.port, retry_once=True)
        h.action_taken = action
        if err:
            h.error = err
        if st:
            h.state = st
            ready = (st.get('program_status_type') in (None, '', 'READY'))
            qot = bool(st.get('qot_logined', True))

            if ready and qot:
                h.ok = True
                h.error_code = None
                h.message = 'OpenD 健康'
            else:
                if not ready:
                    h.error = f"OpenD not READY: {st}"
                elif not qot:
                    h.error = f"OpenD quote not logged in: {st}"
                h.error_code, h.message = classify_watchdog_result(st, h.error)
        else:
            # no state
            h.error_code, h.message = classify_watchdog_result(h.state, h.error)
    except Exception as e:
        h.error = f"get_global_state wrapper failed: {type(e).__name__}: {e}"
        h.error_code, h.message = classify_watchdog_result(h.state, h.error)

    _emit(h, args.json)
    raise SystemExit(0 if h.ok else 2)


if __name__ == '__main__':
    main()
