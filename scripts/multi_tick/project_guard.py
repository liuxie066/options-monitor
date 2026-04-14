from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from scripts.io_utils import atomic_write_json as write_json, read_json


def project_guard_state_path(base: Path) -> Path:
    return (base / 'output_shared' / 'state' / 'project_guard_state.json').resolve()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _load_policy(cfg: dict | None) -> dict[str, Any]:
    raw = ((cfg or {}).get('project_guard') or {}) if isinstance(cfg, dict) else {}
    return {
        'enabled': bool(raw.get('enabled', True)),
        'min_run_interval_sec': max(0, int(raw.get('min_run_interval_sec', 30))),
        'circuit_failure_threshold': max(1, int(raw.get('circuit_failure_threshold', 3))),
        'circuit_window_sec': max(60, int(raw.get('circuit_window_sec', 300))),
        'circuit_open_sec': max(30, int(raw.get('circuit_open_sec', 300))),
        'probe_max_accounts': max(1, int(raw.get('probe_max_accounts', 1))),
    }


def _read_state(path: Path) -> dict[str, Any]:
    st = read_json(path, {}) if path.exists() else {}
    if not isinstance(st, dict):
        st = {}
    return st


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, state)


def admit_project_run(base: Path, cfg: dict | None) -> dict[str, Any]:
    policy = _load_policy(cfg)
    if not bool(policy.get('enabled')):
        return {
            'allowed': True,
            'mode': 'normal',
            'reason': 'project_guard_disabled',
            'error_code': '',
            'probe_max_accounts': int(policy.get('probe_max_accounts', 1)),
        }

    now = _now_utc()
    path = project_guard_state_path(base)
    st = _read_state(path)
    state_name = str(st.get('state') or 'closed')
    open_until = _parse_dt(st.get('circuit_open_until_utc'))

    if state_name == 'open' and open_until and now < open_until:
        return {
            'allowed': False,
            'mode': 'open',
            'reason': f"project_circuit_open_until={open_until.isoformat()}",
            'error_code': 'PROJECT_CIRCUIT_OPEN',
            'probe_max_accounts': int(policy.get('probe_max_accounts', 1)),
        }

    if state_name == 'open' and (open_until is None or now >= open_until):
        state_name = 'half_open'
        st['state'] = 'half_open'
        st['circuit_open_until_utc'] = None

    last_admit = _parse_dt(st.get('last_admit_utc'))
    min_interval = int(policy.get('min_run_interval_sec', 0))
    if last_admit is not None and min_interval > 0:
        elapsed = (now - last_admit).total_seconds()
        if elapsed < min_interval:
            return {
                'allowed': False,
                'mode': state_name,
                'reason': f'project_rate_limited elapsed={elapsed:.1f}s<{min_interval}s',
                'error_code': 'PROJECT_RATE_LIMIT',
                'probe_max_accounts': int(policy.get('probe_max_accounts', 1)),
            }

    st['last_admit_utc'] = now.isoformat()
    st.setdefault('failures', [])
    st.setdefault('state', state_name)
    _write_state(path, st)
    return {
        'allowed': True,
        'mode': ('probe' if state_name == 'half_open' else 'normal'),
        'reason': ('project_circuit_half_open_probe' if state_name == 'half_open' else 'project_guard_pass'),
        'error_code': '',
        'probe_max_accounts': int(policy.get('probe_max_accounts', 1)),
    }


def apply_project_load_shed(accounts: list[str], admission: dict[str, Any]) -> list[str]:
    if str(admission.get('mode') or 'normal') != 'probe':
        return list(accounts)
    n = max(1, int(admission.get('probe_max_accounts') or 1))
    return list(accounts)[:n]


def record_project_failure(base: Path, cfg: dict | None, *, error_code: str, stage: str) -> dict[str, Any]:
    policy = _load_policy(cfg)
    if not bool(policy.get('enabled')):
        return {'opened': False, 'state': 'disabled', 'failure_count': 0}

    now = _now_utc()
    path = project_guard_state_path(base)
    st = _read_state(path)
    failures_raw = st.get('failures')
    if not isinstance(failures_raw, list):
        failures_raw = []
    window_sec = int(policy.get('circuit_window_sec', 300))
    cutoff = now - timedelta(seconds=window_sec)
    failures: list[str] = []
    for value in failures_raw:
        dt = _parse_dt(value)
        if dt is not None and dt >= cutoff:
            failures.append(dt.isoformat())
    failures.append(now.isoformat())
    st['failures'] = failures[-200:]
    st['last_failure_utc'] = now.isoformat()
    st['last_failure_error_code'] = str(error_code or '')
    st['last_failure_stage'] = str(stage or '')

    state_name = str(st.get('state') or 'closed')
    should_open = (state_name == 'half_open') or (len(failures) >= int(policy.get('circuit_failure_threshold', 3)))
    opened = False
    if should_open:
        open_until = now + timedelta(seconds=int(policy.get('circuit_open_sec', 300)))
        st['state'] = 'open'
        st['circuit_open_until_utc'] = open_until.isoformat()
        opened = True
    else:
        st['state'] = state_name if state_name in {'closed', 'half_open'} else 'closed'
        st.setdefault('circuit_open_until_utc', None)

    _write_state(path, st)
    return {
        'opened': opened,
        'state': str(st.get('state') or 'closed'),
        'failure_count': len(failures),
        'open_until_utc': st.get('circuit_open_until_utc'),
    }


def record_project_success(base: Path, cfg: dict | None) -> dict[str, Any]:
    policy = _load_policy(cfg)
    if not bool(policy.get('enabled')):
        return {'closed': False, 'state': 'disabled'}

    now = _now_utc()
    path = project_guard_state_path(base)
    st = _read_state(path)
    changed = False
    if str(st.get('state') or 'closed') != 'closed':
        st['state'] = 'closed'
        changed = True
    if st.get('circuit_open_until_utc') is not None:
        st['circuit_open_until_utc'] = None
        changed = True
    if st.get('failures'):
        st['failures'] = []
        changed = True
    st['last_success_utc'] = now.isoformat()
    if changed or (not path.exists()):
        _write_state(path, st)
    return {'closed': changed, 'state': str(st.get('state') or 'closed')}

