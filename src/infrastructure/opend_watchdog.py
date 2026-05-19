from __future__ import annotations

"""OpenD health probing owned by infrastructure, not scripts."""

import argparse
import json
import os
import socket
import subprocess
import time
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class Health:
    ok: bool
    ports_open: bool
    state: dict | None = None
    error: str | None = None
    action_taken: str | None = None
    error_code: str | None = None
    message: str | None = None
    retrycount: int | None = None
    retryelapsedms: int | None = None
    firstfailts: float | None = None
    recoveredts: float | None = None
    startedbywatchdog: bool | None = None

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        if not payload.get("error_code") and not bool(payload.get("ok")):
            payload["error_code"], payload["message"] = classify_watchdog_result(
                payload.get("state") if isinstance(payload.get("state"), dict) else None,
                payload.get("error"),
            )
        return payload


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
    s = msg or ""
    sl = s.lower()
    keys = ["频率太高", "最多10次", "too frequent", "rate limit", "频率限制", "请求过快"]
    return any(k in s for k in keys) or any(k in sl for k in ["too frequent", "rate limit"])


def _looks_like_phone_verify(msg: str) -> bool:
    s = msg or ""
    sl = s.lower()
    keys = [
        "phone verify",
        "phone verification",
        "verify code",
        "验证码",
        "手机验证",
        "短信验证",
        "not login",
        "not logged",
    ]
    return any(k in s for k in ["验证码", "手机验证", "短信验证"]) or any(k in sl for k in keys)


def classify_watchdog_result(state: dict | None, error_text: str | None) -> tuple[str, str]:
    err = str(error_text or "").strip()
    st = state if isinstance(state, dict) else {}

    if err:
        low = err.lower()
        if "port not open" in low or "cannot connect" in low or "connection refused" in low:
            return ("OPEND_PORT_CLOSED", "OpenD 端口不可达")
        if _looks_like_rate_limit(err):
            return ("OPEND_RATE_LIMIT", "OpenD 请求频率受限")
        if _looks_like_phone_verify(err):
            return ("OPEND_NEEDS_PHONE_VERIFY", "OpenD 需要手机验证码登录")
        if "not ready" in low:
            return ("OPEND_NOT_READY", "OpenD 未就绪")
        if "trade not logged in" in low or "trd" in low:
            return ("OPEND_TRD_NOT_LOGINED", "OpenD 交易未登录")
        if "quote not logged in" in low or "qot" in low:
            return ("OPEND_QOT_NOT_LOGINED", "OpenD 行情未登录")

    status = st.get("program_status_type")
    if status not in (None, "", "READY"):
        if _looks_like_phone_verify(str(st)):
            return ("OPEND_NEEDS_PHONE_VERIFY", "OpenD 需要手机验证码登录")
        return ("OPEND_NOT_READY", "OpenD 未就绪")

    if not bool(st.get("qot_logined", True)):
        return ("OPEND_QOT_NOT_LOGINED", "OpenD 行情未登录")
    if not bool(st.get("trd_logined", True)):
        return ("OPEND_TRD_NOT_LOGINED", "OpenD 交易未登录")

    return ("OPEND_API_ERROR", "OpenD 接口异常")


def _looks_like_disconnect_error(msg: str) -> bool:
    s = (msg or "").strip()
    sl = s.lower()
    if _looks_like_phone_verify(s):
        return False
    keys = [
        "econnrefused",
        "connection refused",
        "econnreset",
        "connection reset",
        "broken pipe",
        "timed out",
        "timeout",
        "socket",
        "disconnected",
        "remote closed",
        "callclose",
        "eof",
    ]
    return any(k in sl for k in keys)


def get_global_state_once(host: str, port: int) -> dict:
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


def get_global_state(host: str, port: int, *, retry_once: bool = True, ensure: bool = False) -> tuple[dict | None, str | None, str | None]:
    try:
        st = get_global_state_once(host, port)
        return (st, None, None)
    except Exception as exc:
        err1 = f"get_global_state failed: {type(exc).__name__}: {exc}"
        code1, _ = classify_watchdog_result(None, err1)
        if code1 == "OPEND_NEEDS_PHONE_VERIFY":
            return (None, err1, "fail_fast_phone_verify")
        if code1 == "OPEND_RATE_LIMIT":
            return (None, err1, "no_retry_rate_limit")
        if retry_once and _looks_like_disconnect_error(err1):
            if ensure and not port_open(host, port):
                ok, _msg = try_start_opend()
                if ok:
                    time.sleep(1.0)
            time.sleep(0.3)
            try:
                st2 = get_global_state_once(host, port)
                return (st2, None, "retry_once")
            except Exception as exc2:
                err2 = f"get_global_state failed after retry: {type(exc2).__name__}: {exc2}"
                return (None, err1 + " | " + err2, "retry_once_failed")
        return (None, err1, None)


def try_start_opend() -> tuple[bool, str]:
    start_sh = os.environ.get("OPEND_START_SCRIPT", "/home/node/.openclaw/workspace/skills/futu-agent/scripts/start.sh")
    try:
        proc = subprocess.run(["bash", start_sh], capture_output=True, text=True, timeout=20)
        out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        return (proc.returncode == 0, out[-500:])
    except Exception as exc:
        return (False, f"start_opend exception: {type(exc).__name__}: {exc}")


def _port_retry_loop(
    h: Health,
    host: str,
    port: int,
    *,
    ensure: bool,
    retry_interval_sec: float,
    retry_timeout_sec: float,
    success_threshold: int,
) -> bool:
    firstfail = time.time()
    h.firstfailts = firstfail
    h.retrycount = 0

    if ensure:
        ok_start, _msg = try_start_opend()
        h.startedbywatchdog = ok_start
        h.action_taken = "start_opend" if ok_start else "start_opend_failed"

    consecutive_success = 0
    deadline = firstfail + retry_timeout_sec

    while time.time() < deadline:
        remaining = deadline - time.time()
        sleep_duration = min(retry_interval_sec, max(0.0, remaining))
        time.sleep(sleep_duration)
        h.retrycount += 1
        if port_open(host, port):
            consecutive_success += 1
            if consecutive_success >= success_threshold:
                h.recoveredts = time.time()
                h.retryelapsedms = int((h.recoveredts - firstfail) * 1000)
                return True
        else:
            consecutive_success = 0

    h.retryelapsedms = int((time.time() - firstfail) * 1000)
    return False


def run_watchdog_check(
    *,
    host: str = "127.0.0.1",
    port: int = 11111,
    ensure: bool = False,
    retry_enabled: bool = False,
    retry_interval_sec: float = 3.0,
    retry_timeout_sec: float = 25.0,
    success_threshold: int = 2,
) -> Health:
    h = Health(ok=False, ports_open=False)

    if not port_open(host, port):
        h.ports_open = False
        if retry_enabled:
            recovered = _port_retry_loop(
                h,
                host,
                port,
                ensure=ensure,
                retry_interval_sec=retry_interval_sec,
                retry_timeout_sec=retry_timeout_sec,
                success_threshold=success_threshold,
            )
            if not recovered:
                h.error = f"OpenD port not open: {host}:{port}"
                h.error_code, h.message = classify_watchdog_result(None, h.error)
                return h
        else:
            if ensure:
                ok, _msg = try_start_opend()
                h.action_taken = "start_opend" if ok else "start_opend_failed"
                if ok:
                    deadline = time.time() + 8.0
                    while time.time() < deadline and not port_open(host, port):
                        time.sleep(0.8)
                else:
                    time.sleep(1.0)
            if not port_open(host, port):
                h.error = f"OpenD port not open: {host}:{port}"
                h.error_code, h.message = classify_watchdog_result(None, h.error)
                return h

    h.ports_open = True

    try:
        st, err, action = get_global_state(host, port, retry_once=True, ensure=bool(ensure))
        h.action_taken = action
        if err:
            h.error = err
        if st:
            h.state = st
            ready = st.get("program_status_type") in (None, "", "READY")
            qot = bool(st.get("qot_logined", True))
            trd = bool(st.get("trd_logined", True))
            if ready and qot and trd:
                h.ok = True
                h.error_code = None
                h.message = "OpenD 健康"
            else:
                if not ready:
                    h.error = f"OpenD not READY: {st}"
                elif not qot:
                    h.error = f"OpenD quote not logged in: {st}"
                elif not trd:
                    h.error = f"OpenD trade not logged in: {st}"
                h.error_code, h.message = classify_watchdog_result(st, h.error)
        else:
            h.error_code, h.message = classify_watchdog_result(h.state, h.error)
    except Exception as exc:
        h.error = f"get_global_state wrapper failed: {type(exc).__name__}: {exc}"
        h.error_code, h.message = classify_watchdog_result(h.state, h.error)

    return h


def _emit(h: Health, as_json: bool) -> None:
    payload = h.to_payload()
    if as_json:
        print(json.dumps(payload, ensure_ascii=False))
    elif h.ok:
        print("[OPEND_OK] OpenD healthy")
    else:
        print(f"[OPEND_UNHEALTHY] {payload.get('error_code')}: {payload.get('message') or payload.get('error')}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenD watchdog")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=11111)
    parser.add_argument("--ensure", action="store_true", help="try to start OpenD if port closed")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--retry-enabled", action="store_true", default=False)
    parser.add_argument("--retry-interval-sec", type=float, default=3.0)
    parser.add_argument("--retry-timeout-sec", type=float, default=25.0)
    parser.add_argument("--success-threshold", type=int, default=2)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    h = run_watchdog_check(
        host=str(args.host),
        port=int(args.port),
        ensure=bool(args.ensure),
        retry_enabled=bool(args.retry_enabled),
        retry_interval_sec=float(args.retry_interval_sec),
        retry_timeout_sec=float(args.retry_timeout_sec),
        success_threshold=int(args.success_threshold),
    )
    _emit(h, bool(args.json))
    return 0 if h.ok else 2


__all__ = [
    "Health",
    "build_parser",
    "classify_watchdog_result",
    "get_global_state",
    "get_global_state_once",
    "main",
    "port_open",
    "run_watchdog_check",
    "try_start_opend",
    "_port_retry_loop",
]
