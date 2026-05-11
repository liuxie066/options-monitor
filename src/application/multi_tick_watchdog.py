from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from time import monotonic
from typing import Any, Callable


@dataclass(frozen=True)
class MultiTickWatchdogOutcome:
    should_continue: bool
    return_code: int


def run_multi_tick_watchdog(
    *,
    base,
    base_cfg: dict[str, Any],
    accounts: list[str],
    no_send: bool,
    vpy,
    runlog,
    safe_data_fn: Callable[[dict[str, Any]], dict[str, Any]],
    utc_now_fn: Callable[[], str],
    audit_fn: Callable[..., Any],
    on_guard_failure: Callable[[str, str], Any],
    run_opend_watchdog,
    parse_last_json_obj,
    classify_failure,
    resolve_watchlist_config,
    is_futu_fetch_source,
    resolve_multi_tick_engine_entrypoint,
    build_opend_unhealthy_execution_plan,
    mark_opend_phone_verify_pending,
    send_opend_alert,
    send_opend_recovery_notice,
    state_repo,
) -> MultiTickWatchdogOutcome:
    t_watchdog0 = monotonic()
    runlog.safe_event("watchdog", "start")
    audit_fn("tool_call", "opend_watchdog_start", tool_name="opend_watchdog")
    try:
        need_opend = False
        ports = set()
        for sym in resolve_watchlist_config(base_cfg):
            fetch = (sym or {}).get("fetch") or {}
            if is_futu_fetch_source(fetch.get("source")):
                need_opend = True
                host = fetch.get("host") or "127.0.0.1"
                port = fetch.get("port") or 11111
                ports.add((str(host), int(port)))

        if need_opend:
            unhealthy = None
            wd_cfg = (base_cfg or {}).get("watchdog") or {}
            retry_enabled = bool(wd_cfg.get("retry_enabled", True))
            retry_interval_sec = float(wd_cfg.get("retry_interval_sec", 3.0))
            retry_timeout_sec = float(wd_cfg.get("retry_timeout_sec", 25.0))
            success_threshold = int(wd_cfg.get("success_threshold", 2))
            # Base watchdog timeout budget; enough for port check + get_global_state.
            # When retry is enabled the watchdog itself runs the window, so add
            # the retry budget on top.
            _BASE_WATCHDOG_TIMEOUT_SEC = 35
            watchdog_timeout_sec = (
                int(retry_timeout_sec) + _BASE_WATCHDOG_TIMEOUT_SEC if retry_enabled else _BASE_WATCHDOG_TIMEOUT_SEC
            )
            for host, port in sorted(ports):
                try:
                    wd0 = run_opend_watchdog(
                        vpy=vpy,
                        base=base,
                        host=str(host),
                        port=int(port),
                        ensure=True,
                        timeout_sec=watchdog_timeout_sec,
                        retry_enabled=retry_enabled,
                        retry_interval_sec=retry_interval_sec,
                        retry_timeout_sec=retry_timeout_sec,
                        success_threshold=success_threshold,
                    )
                    if isinstance(wd0, dict):
                        payload0 = wd0
                        ok0 = bool(payload0.get("ok"))
                        returncode = 0 if ok0 else 2
                        detail_text = json.dumps(payload0, ensure_ascii=False)
                    else:
                        payload0 = parse_last_json_obj((wd0.stdout or "") + "\n" + (wd0.stderr or ""))
                        ok0 = bool(payload0.get("ok")) if payload0 else (wd0.returncode == 0)
                        returncode = int(wd0.returncode)
                        detail_text = ((wd0.stdout or "") + "\n" + (wd0.stderr or "")).strip()
                    audit_fn(
                        "tool_call",
                        "opend_watchdog_result",
                        status=("ok" if ok0 else "error"),
                        tool_name="opend_watchdog",
                        extra={"host": str(host), "port": int(port), "returncode": returncode},
                    )
                    if not ok0:
                        unhealthy = {
                            "host": host,
                            "port": port,
                            "payload": payload0,
                            "detail": detail_text,
                        }
                        break
                except Exception as exc:
                    watchdog_timed_out = isinstance(exc, subprocess.TimeoutExpired)
                    classified = classify_failure(
                        exc=exc,
                        upstream="opend",
                        error_code=("OPEND_TIMEOUT" if watchdog_timed_out else "OPEND_API_ERROR"),
                        message=str(exc),
                    )
                    unhealthy = {
                        "host": host,
                        "port": port,
                        "payload": {
                            "ok": False,
                            "error_code": str(classified.get("error_code") or "OPEND_API_ERROR"),
                            "message": "OpenD 看门狗执行失败",
                            "category": classified.get("category"),
                        },
                        "detail": f"{type(exc).__name__}: {exc}",
                    }
                    break

            if unhealthy is not None:
                payload = unhealthy.get("payload") or {}
                error_code = str(payload.get("error_code") or "OPEND_API_ERROR")
                msg = str(payload.get("message") or payload.get("error") or "OpenD 不健康")
                detail = str(unhealthy.get("detail") or "")
                host = unhealthy.get("host")
                port = unhealthy.get("port")
                opend_plan = resolve_multi_tick_engine_entrypoint(
                    opend_unhealthy={
                        "error_code": error_code,
                        "degraded": False,
                        "message_text": msg,
                        "detail_text": detail,
                        "host": host,
                        "port": port,
                    }
                ).get("watchdog") or build_opend_unhealthy_execution_plan(
                    error_code=error_code,
                    degraded=False,
                    message_text=msg,
                    detail_text=detail,
                    host=host,
                    port=port,
                )
                alert_message_text = str(opend_plan.get("alert_message_text") or msg)
                alert_detail = str(opend_plan.get("alert_detail") or detail)
                if bool(opend_plan.get("should_mark_phone_verify_pending")):
                    mark_opend_phone_verify_pending(base, detail=alert_detail)
                    send_opend_alert(
                        base,
                        base_cfg,
                        error_code=error_code,
                        message_text=alert_message_text,
                        detail=alert_detail,
                        no_send=no_send,
                        skip_consecutive_gate=True,
                    )
                    runlog.safe_event(
                        "run_end",
                        "skip",
                        error_code=error_code,
                        message="opend needs phone verify; paused until user confirmation",
                        data=safe_data_fn({"sent": False, "reason": "opend_phone_verify_pending"}),
                    )
                    audit_fn(
                        "notify",
                        "send_opend_alert",
                        status="error",
                        error_code=error_code,
                        message="opend needs phone verify; paused",
                        fallback_used=bool(opend_plan.get("fallback_used")),
                    )
                    return MultiTickWatchdogOutcome(should_continue=False, return_code=0)

                send_opend_alert(
                    base,
                    base_cfg,
                    error_code=error_code,
                    message_text=alert_message_text,
                    detail=alert_detail,
                    no_send=no_send,
                )
                on_guard_failure(error_code, "opend_watchdog")
                now = utc_now_fn()
                for acct in accounts:
                    acct0 = str(acct).strip().lower()
                    if not acct0:
                        continue
                    try:
                        state_repo.write_account_last_run(
                            base,
                            acct0,
                            {
                                "last_run_utc": now,
                                "sent": False,
                                "reason": "opend_unhealthy",
                                "error_code": error_code,
                                "detail": msg,
                            },
                        )
                        audit_fn("write", "write_account_last_run", account=acct0, error_code=error_code)
                    except Exception:
                        pass

                runlog.safe_event(
                    "watchdog",
                    "error",
                    duration_ms=int((monotonic() - t_watchdog0) * 1000),
                    error_code=error_code,
                    message=msg,
                    data=safe_data_fn({"degraded": False, "host": host, "port": port}),
                )
                runlog.safe_event(
                    "run_end",
                    "error",
                    error_code=error_code,
                    message="opend watchdog unhealthy",
                    data=safe_data_fn({"sent": False, "reason": "opend_unhealthy"}),
                )
                audit_fn(
                    "fallback",
                    "opend_unhealthy_no_fallback",
                    status="error",
                    error_code=error_code,
                    fallback_used=bool(opend_plan.get("fallback_used")),
                    message=msg,
                )
                return MultiTickWatchdogOutcome(should_continue=False, return_code=0)
            else:
                # OpenD healthy: reset consecutive failure counter and send recovery notice if applicable.
                try:
                    send_opend_recovery_notice(base, base_cfg, no_send=no_send)
                except Exception:
                    pass
    except SystemExit:
        raise
    except Exception as exc:
        on_guard_failure("WATCHDOG_EXCEPTION", "opend_watchdog")
        runlog.safe_event(
            "watchdog",
            "error",
            duration_ms=int((monotonic() - t_watchdog0) * 1000),
            error_code="WATCHDOG_EXCEPTION",
            message=str(exc),
        )
    runlog.safe_event("watchdog", "ok", duration_ms=int((monotonic() - t_watchdog0) * 1000))
    return MultiTickWatchdogOutcome(should_continue=True, return_code=0)
