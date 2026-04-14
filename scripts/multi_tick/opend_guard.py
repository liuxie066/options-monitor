from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from scripts.io_utils import read_json, atomic_write_json as write_json, utc_now


def opend_alert_rl_path(base: Path) -> Path:
    return (base / 'output_shared' / 'state' / 'opend_alert_rate_limit.json').resolve()


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


def _opend_alert_family(error_code: str) -> str:
    code = str(error_code or '').strip().upper()
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
            if (now - dt.astimezone(timezone.utc)).total_seconds() < int(cooldown_sec):
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

    m[error_key] = now.isoformat()
    st['last_sent_utc_by_error'] = m
    recent.append({'ts': now.isoformat(), 'scope': scope_key, 'error_code': str(error_code), 'family': family})
    st['recent_sent'] = recent[-200:]
    write_json(p, st)
    return True


def send_opend_alert(base: Path, cfg: dict, *, error_code: str, message_text: str, detail: str = '', no_send: bool = False) -> bool:
    cooldown_sec = 600
    burst_window_sec = 900
    burst_max = 3
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
    except Exception:
        cooldown_sec = 600
        burst_window_sec = 900
        burst_max = 3

    if not should_send_opend_alert(
        base,
        str(error_code),
        cooldown_sec=cooldown_sec,
        burst_window_sec=burst_window_sec,
        burst_max=burst_max,
    ):
        return False

    if no_send:
        return False

    notif = cfg.get('notifications') or {}
    channel = notif.get('channel') or 'feishu'
    target = notif.get('target')
    if not target:
        return False

    msg = (
        f"options-monitor OpenD 告警\n"
        f"error_code: {error_code}\n"
        f"message: {message_text}\n"
        f"time_utc: {utc_now()}"
    )
    if detail:
        msg += f"\ndetail: {detail[:1200]}"

    send = subprocess.run(
        ['openclaw', 'message', 'send', '--channel', str(channel), '--target', str(target), '--message', msg, '--json'],
        cwd=str(base),
        capture_output=True,
        text=True,
    )
    return send.returncode == 0
