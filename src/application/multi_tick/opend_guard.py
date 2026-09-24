from __future__ import annotations

import fcntl
import os
import stat
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from src.application.notification_shells import render_system_notice
from src.application.system_alerts import report_system_failure, report_system_recovery
from src.application.trade_time_format import format_iso_time_beijing
from src.infrastructure.io_utils import read_json, atomic_write_json as write_json, utc_now


def opend_alert_rl_path(base: Path) -> Path:
    return (base / 'output_shared' / 'state' / 'opend_alert_rate_limit.json').resolve()


@contextmanager
def _alert_state_lock(base: Path):
    path = opend_alert_rl_path(base).with_suffix('.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("OpenD alert lock is not a regular file")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def opend_phone_verify_pending_path(base: Path) -> Path:
    return (base / 'output_shared' / 'state' / 'opend_phone_verify_pending.json').resolve()


def mark_opend_phone_verify_pending(base: Path, *, detail: str | None = None) -> None:
    try:
        p = opend_phone_verify_pending_path(base)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            'pending': True,
            'detected_at_utc': utc_now(),
            'detail': (detail or '')[:2000],
        }
        write_json(p, payload)
    except Exception:
        pass


def clear_opend_phone_verify_pending(base: Path) -> None:
    try:
        p = opend_phone_verify_pending_path(base)
        if p.exists():
            p.unlink()
    except Exception:
        pass


def is_opend_phone_verify_pending(base: Path) -> bool:
    try:
        p = opend_phone_verify_pending_path(base)
        if not p.exists() or p.stat().st_size <= 0:
            return False
        st = read_json(p, {})
        return bool(isinstance(st, dict) and st.get('pending'))
    except Exception:
        return False


def record_opend_failure(base: Path, scope: str = 'project') -> int:
    """Increment the consecutive failure counter for *scope* and return the new count."""
    with _alert_state_lock(base):
        return _record_opend_failure_unlocked(base, scope)


def _record_opend_failure_unlocked(base: Path, scope: str) -> int:
    p = opend_alert_rl_path(base)
    p.parent.mkdir(parents=True, exist_ok=True)
    st = read_json(p, {}) if p.exists() else {}
    if not isinstance(st, dict):
        st = {}
    fail_st = st.get('consecutive_fail_state')
    if not isinstance(fail_st, dict):
        fail_st = {}
    scope_key = str(scope or 'project')
    entry = fail_st.get(scope_key)
    if not isinstance(entry, dict):
        entry = {'count': 0}
    count = int(entry.get('count') or 0) + 1
    entry['count'] = count
    entry['last_fail_utc'] = datetime.now(timezone.utc).isoformat()
    fail_st[scope_key] = entry
    st['consecutive_fail_state'] = fail_st
    write_json(p, st)
    return count


def record_opend_recovery(base: Path, scope: str = 'project') -> int:
    """Reset the consecutive failure counter for *scope*; return the count before reset."""
    with _alert_state_lock(base):
        return _record_opend_recovery_unlocked(base, scope)


def _record_opend_recovery_unlocked(base: Path, scope: str) -> int:
    p = opend_alert_rl_path(base)
    if not p.exists():
        return 0
    st = read_json(p, {})
    if not isinstance(st, dict):
        return 0
    fail_st = st.get('consecutive_fail_state')
    if not isinstance(fail_st, dict):
        fail_st = {}
    scope_key = str(scope or 'project')
    entry = fail_st.get(scope_key)
    prev_count = int(entry.get('count') or 0) if isinstance(entry, dict) else 0
    sent = st.get('last_sent_utc_by_error')
    has_sent_latch = isinstance(sent, dict) and any(
        str(key).startswith(f'{scope_key}::') for key in sent
    )
    if prev_count == 0 and not has_sent_latch:
        return 0
    if not isinstance(entry, dict):
        entry = {'count': 0}
    entry['count'] = 0
    entry['last_ok_utc'] = datetime.now(timezone.utc).isoformat()
    fail_st[scope_key] = entry
    st['consecutive_fail_state'] = fail_st
    # A successful probe closes the incident.  Clear the incident latch and
    # burst history so a later failure is eligible for one fresh alert.
    if isinstance(sent, dict):
        st['last_sent_utc_by_error'] = {
            key: value
            for key, value in sent.items()
            if not str(key).startswith(f'{scope_key}::')
        }
    statuses = st.get('last_delivery_status_by_error')
    if isinstance(statuses, dict):
        st['last_delivery_status_by_error'] = {
            key: value for key, value in statuses.items()
            if not str(key).startswith(f'{scope_key}::')
        }
    recent = st.get('recent_sent')
    if isinstance(recent, list):
        st['recent_sent'] = [
            item for item in recent
            if not isinstance(item, dict) or str(item.get('scope') or 'project') != scope_key
        ]
    write_json(p, st)
    return prev_count


def _opend_alert_family(error_code: str) -> str:
    code = str(error_code or '').strip().upper()
    if code in {'OPEND_NEEDS_PHONE_VERIFY', 'OPEND_NEEDS_PIC_VERIFY', 'OPEND_LOGIN_INVALID'}:
        return 'OPEND_LOGIN_ACTION_REQUIRED'
    if code in {'OPEND_PORT_CLOSED', 'OPEND_NOT_READY', 'OPEND_QOT_NOT_LOGINED', 'OPEND_API_ERROR'}:
        return 'OPEND_UNHEALTHY'
    if code.startswith('OPEND_'):
        return code
    return (code or 'OPEND_UNKNOWN')


def should_send_opend_alert(
    base: Path,
    error_code: str,
    cooldown_sec: int = 600,
    *,
    burst_window_sec: int = 900,
    burst_max: int = 3,
    scope: str = 'project',
    record: bool = True,
) -> bool:
    p = opend_alert_rl_path(base)
    now = datetime.now(timezone.utc)
    st = read_json(p, {}) if p.exists() else {}
    if not isinstance(st, dict):
        st = {}

    m = st.get('last_sent_utc_by_error')
    if not isinstance(m, dict):
        m = {}

    family = _opend_alert_family(str(error_code))
    error_key = f"{str(scope or 'project')}::{family}"
    prev = m.get(error_key)
    if prev:
        try:
            dt = datetime.fromisoformat(str(prev))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            last_ok = None
            fail_st = st.get('consecutive_fail_state')
            entry = fail_st.get(str(scope or 'project')) if isinstance(fail_st, dict) else None
            if isinstance(entry, dict) and entry.get('last_ok_utc'):
                last_ok = datetime.fromisoformat(str(entry['last_ok_utc']))
                if last_ok.tzinfo is None:
                    last_ok = last_ok.replace(tzinfo=timezone.utc)
            # One alert per incident; recovery clears this latch.
            if last_ok is None or dt.astimezone(timezone.utc) >= last_ok.astimezone(timezone.utc):
                return False
        except Exception:
            pass

    # Project-level burst limit: cap total alert sends in a rolling window to avoid spam storms.
    rec = st.get('recent_sent')
    if not isinstance(rec, list):
        rec = []
    window_start = now.timestamp() - max(60, int(burst_window_sec))
    recent: list[dict] = []
    for item in rec:
        if not isinstance(item, dict):
            continue
        ts = item.get('ts')
        try:
            d = datetime.fromisoformat(str(ts))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            if d.astimezone(timezone.utc).timestamp() >= window_start:
                recent.append(item)
        except Exception:
            continue
    scope_key = str(scope or 'project')
    recent_count = sum(1 for item in recent if str(item.get('scope') or 'project') == scope_key)
    if recent_count >= max(1, int(burst_max)):
        return False

    if record:
        m[error_key] = now.isoformat()
        st['last_sent_utc_by_error'] = m
        statuses = st.get('last_delivery_status_by_error')
        if not isinstance(statuses, dict):
            statuses = {}
        statuses[error_key] = 'delivery_unknown'
        st['last_delivery_status_by_error'] = statuses
        recent.append({'ts': now.isoformat(), 'scope': scope_key, 'error_code': str(error_code), 'family': family})
        st['recent_sent'] = recent[-200:]
        write_json(p, st)
    return True


def send_opend_alert(base: Path, cfg: dict, *, error_code: str, message_text: str, detail: str = '', no_send: bool = False, skip_consecutive_gate: bool = False) -> bool:
    cooldown_sec = 600
    burst_window_sec = 900
    burst_max = 3
    consecutive_threshold = 3
    try:
        notif_cfg = (cfg.get('notifications') or {})
        v = notif_cfg.get('opend_alert_cooldown_sec')
        if v is not None:
            cooldown_sec = max(60, int(v))
        bw = notif_cfg.get('opend_alert_burst_window_sec')
        if bw is not None:
            burst_window_sec = max(60, int(bw))
        bm = notif_cfg.get('opend_alert_burst_max')
        if bm is not None:
            burst_max = max(1, int(bm))
        ct = notif_cfg.get('opend_alert_after_consecutive_failures')
        if ct is not None:
            consecutive_threshold = max(1, int(ct))
    except Exception:
        cooldown_sec = 600
        burst_window_sec = 900
        burst_max = 3
        consecutive_threshold = 3

    # Gate: only alert after consecutive_threshold watchdog failures.
    if not skip_consecutive_gate:
        try:
            fail_count = record_opend_failure(base)
            if fail_count < consecutive_threshold:
                return False
        except Exception:
            pass

    if no_send:
        return False

    now_utc = utc_now()
    fields: list[tuple[str, object]] = [
        ("时间", format_iso_time_beijing(now_utc) or now_utc),
        ("影响", "本轮行情与交易数据可能不完整"),
        ("原因", message_text),
        ("诊断", f"`{error_code}`"),
    ]
    if detail:
        fields.append(("详情", str(detail)[:1200]))
    msg = render_system_notice(
        component="OpenD",
        status="❌ 不可用",
        fields=fields,
    )

    with _alert_state_lock(base):
        # Reserve the incident before contacting the provider. An ambiguous
        # response remains reserved; recovery is the only automatic reset.
        if not should_send_opend_alert(
            base,
            str(error_code),
            cooldown_sec=cooldown_sec,
            burst_window_sec=burst_window_sec,
            burst_max=burst_max,
            record=False,
        ):
            return False
        should_send_opend_alert(
            base, str(error_code), cooldown_sec=cooldown_sec,
            burst_window_sec=burst_window_sec, burst_max=burst_max,
        )
        key = f'project::{_opend_alert_family(error_code)}'
        reserved_at = (read_json(opend_alert_rl_path(base), {})
                       .get('last_sent_utc_by_error', {}).get(key))
    try:
        outcome = report_system_failure(
            base=base, config=cfg, unit='OpenD', market='all', account='all',
            failure_code=_opend_alert_family(error_code), stage='watchdog',
            run_id=now_utc, rc=1, first_error_at=reserved_at or now_utc,
            opend_login_state=str(error_code), message=msg,
        )
    except Exception:
        print('<3>OPEND_ALERT_INFRA_FAILED', file=sys.stderr)
        return False
    confirmed = outcome == 'confirmed'
    if confirmed:
        with _alert_state_lock(base):
            path = opend_alert_rl_path(base)
            state = read_json(path, {})
            if isinstance(state, dict):
                statuses = state.get('last_delivery_status_by_error')
                sent = state.get('last_sent_utc_by_error')
                if isinstance(statuses, dict) and isinstance(sent, dict) and sent.get(key) == reserved_at:
                    statuses[key] = 'delivery_confirmed'
                    write_json(path, state)
    return confirmed


def send_opend_recovery_notice(base: Path, cfg: dict, *, scope: str = 'project', no_send: bool = False) -> bool:
    """Reset consecutive failure counter and send a recovery notice if configured.

    Only sends when:
    - ``opend_alert_send_recovery_notice`` is true (default true).
    - The previous consecutive failure count was >= ``opend_alert_after_consecutive_failures``.
    """
    notif_cfg = (cfg.get('notifications') or {})
    send_recovery = True
    consecutive_threshold = 3
    try:
        v = notif_cfg.get('opend_alert_send_recovery_notice')
        if v is not None:
            send_recovery = bool(v)
        ct = notif_cfg.get('opend_alert_after_consecutive_failures')
        if ct is not None:
            consecutive_threshold = max(1, int(ct))
    except Exception:
        pass

    state = read_json(opend_alert_rl_path(base), {})
    sent = state.get('last_sent_utc_by_error') if isinstance(state, dict) else {}
    families = sorted({str(key).split('::', 1)[1] for key in sent
                       if str(key).startswith(f'{scope}::')}) if isinstance(sent, dict) else []
    try:
        prev_count = record_opend_recovery(base, scope=scope)
    except Exception:
        prev_count = 0

    should_notify = send_recovery and prev_count >= consecutive_threshold and not no_send
    if not families and not should_notify:
        return False

    now_utc = utc_now()
    msg = render_system_notice(
        component="OpenD",
        status="✅ 已恢复",
        fields=(
            ("时间", format_iso_time_beijing(now_utc) or now_utc),
            ("结果", "数据连接已恢复，后续批次将自动重新评估"),
        ),
    )

    try:
        outcomes = [report_system_recovery(
            base=base, config=cfg, unit='OpenD', market='all', account='all',
            failure_code=family, stage='watchdog', message=msg,
            notify=should_notify and index == 0,
            allow_without_incident=should_notify and index == 0,
        ) for index, family in enumerate(families)]
        if not families and should_notify:
            outcomes.append(report_system_recovery(
                base=base, config=cfg, unit='OpenD', market='all', account='all',
                failure_code='OPEND_RECOVERY_ONLY', stage='watchdog', message=msg,
                allow_without_incident=True,
            ))
    except Exception:
        print('<3>OPEND_ALERT_INFRA_FAILED', file=sys.stderr)
        return False
    return 'confirmed' in outcomes
