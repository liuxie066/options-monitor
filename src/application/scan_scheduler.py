#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass
class SchedulerDecision:
    now_utc: str
    now_market: str
    now_beijing: str
    in_run_window: bool
    should_run_scan: bool
    is_notify_window_open: bool
    reason: str
    next_run_utc: str
    next_run_market: str
    next_run_beijing: str
    run_window_start_beijing: str
    run_window_end_beijing: str
    schedule_key: str


STATE_DEFAULT = {
    'last_run_utc_by_account': {},
    'last_notify_utc': None,  # legacy
    'last_notify_utc_by_account': {},
}


def read_state(path: Path) -> dict:
    if path.exists() and path.stat().st_size > 0:
        return json.loads(path.read_text(encoding='utf-8'))
    return STATE_DEFAULT.copy()


def write_state(path: Path, state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    from src.infrastructure.io_utils import atomic_write_text
    atomic_write_text(path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def _resolve_base(base_dir: Path | None = None) -> Path:
    return (base_dir or Path(__file__).resolve().parents[2]).resolve()


def _resolve_config_path(config: str | Path, *, base: Path) -> Path:
    config_path = Path(config)
    if not config_path.is_absolute():
        config_path = (base / config_path).resolve()
    return config_path


def _resolve_state_path(
    *,
    base: Path,
    state_dir: str | Path = 'output/state',
    state: str | Path | None = None,
) -> Path:
    state_dir_path = Path(state_dir)
    if not state_dir_path.is_absolute():
        state_dir_path = (base / state_dir_path).resolve()
    state_dir_path.mkdir(parents=True, exist_ok=True)

    if state:
        state_path = Path(state)
        if not state_path.is_absolute():
            state_path = (base / state_path).resolve()
        return state_path
    return (state_dir_path / 'scheduler_state.json').resolve()


def _load_scheduler_config(config: str | Path | dict, *, base: Path) -> dict:
    if isinstance(config, dict):
        return config
    config_path = _resolve_config_path(config, base=base)
    if config_path.suffix.lower() != '.json':
        raise SystemExit('[CONFIG_ERROR] scheduler config must be a .json file')
    return json.loads(config_path.read_text(encoding='utf-8'))


def build_scheduler_decision_payload(
    *,
    config: str | Path | dict,
    state: str | Path,
    schedule_key: str = 'schedule',
    account: str | None = None,
    force: bool = False,
    base_dir: Path | None = None,
    now_utc: datetime | None = None,
) -> dict:
    """Build one scheduler decision without spawning the scheduler CLI."""
    base = _resolve_base(base_dir)
    cfg = _load_scheduler_config(config, base=base)
    state_path = Path(state)
    if not state_path.is_absolute():
        state_path = (base / state_path).resolve()

    schedule_key_val = str(schedule_key or 'schedule')
    schedule_cfg = cfg.get(schedule_key_val, {}) or {}
    state_data = read_state(state_path)
    decision = decide(
        schedule_cfg,
        state_data,
        now_utc or datetime.now(timezone.utc),
        account=(str(account) if account else None),
        schedule_key=schedule_key_val,
        force=bool(force),
    )
    payload = asdict(decision)
    payload['should_notify'] = bool(payload.get('is_notify_window_open'))
    return payload


def mark_scheduler_accounts(
    *,
    config: str | Path | dict,
    state: str | Path,
    state_dir: str | Path = 'output/state',
    schedule_key: str = 'schedule',
    accounts: list[str],
    mark_notified: bool = False,
    mark_scanned: bool = False,
    force: bool = False,
    base_dir: Path | None = None,
    now_utc: datetime | None = None,
) -> dict:
    """Batch account scheduler state writes in-process.

    This keeps multi-account tick from spawning one scheduler process per
    account for simple state updates.
    """
    base = _resolve_base(base_dir)
    cfg = _load_scheduler_config(config, base=base)
    state_path = Path(state)
    if not state_path.is_absolute():
        state_path = _resolve_state_path(base=base, state_dir=state_dir, state=state)

    schedule_cfg = cfg.get(str(schedule_key or 'schedule'), {}) or {}
    schedule_enabled = bool(schedule_cfg.get('enabled', True))
    account_ids = [str(a).strip() for a in accounts if str(a).strip()]
    if not account_ids or not (mark_notified or mark_scanned):
        return {
            'updated': False,
            'state_path': str(state_path),
            'accounts': account_ids,
            'mark_notified': bool(mark_notified),
            'mark_scanned': bool(mark_scanned),
        }

    state_data = read_state(state_path)
    if not schedule_enabled and not force:
        return {
            'updated': False,
            'state_path': str(state_path),
            'reason': 'schedule_disabled',
            'accounts': account_ids,
        }

    now_s = to_iso(now_utc or datetime.now(timezone.utc))
    if mark_notified:
        state_data['last_notify_utc'] = now_s
        if account_ids:
            m = state_data.get('last_notify_utc_by_account')
            if not isinstance(m, dict):
                m = {}
            for account in account_ids:
                m[str(account)] = now_s
            state_data['last_notify_utc_by_account'] = m
    if mark_scanned:
        if account_ids:
            m = state_data.get('last_run_utc_by_account')
            if not isinstance(m, dict):
                m = {}
            for account in account_ids:
                m[str(account)] = now_s
            state_data['last_run_utc_by_account'] = m

    if mark_notified or mark_scanned:
        write_state(state_path, state_data)
    return {
        'updated': bool(mark_notified or mark_scanned),
        'state_path': str(state_path),
        'accounts': account_ids,
        'mark_notified': bool(mark_notified),
        'mark_scanned': bool(mark_scanned),
    }


def parse_hhmm(value: str) -> time:
    hour, minute = value.split(':', 1)
    return time(hour=int(hour), minute=int(minute))


def _parse_breaks(raw_breaks: object) -> list[tuple[time, time]]:
    if not isinstance(raw_breaks, list):
        return []
    out: list[tuple[time, time]] = []
    for item in raw_breaks:
        if not isinstance(item, dict):
            continue
        start = item.get('start')
        end = item.get('end')
        if not start or not end:
            continue
        out.append((parse_hhmm(str(start)), parse_hhmm(str(end))))
    return out


def _resolve_run_window(schedule_cfg: dict) -> tuple[time, time, list[tuple[time, time]]]:
    run_window = schedule_cfg.get('run_window') if isinstance(schedule_cfg.get('run_window'), dict) else {}
    start = parse_hhmm(str(run_window.get('start') or '09:30'))
    end = parse_hhmm(str(run_window.get('end') or '16:00'))
    return start, end, _parse_breaks(run_window.get('breaks'))


def is_run_window_open(now_market: datetime, run_start: time, run_end: time, breaks: list[tuple[time, time]] | None = None) -> bool:
    """Return whether the business run window is open."""
    if now_market.weekday() >= 5:
        return False
    current = now_market.time()
    if not (run_start <= current <= run_end):
        return False

    for break_start, break_end in breaks or []:
        if _time_in_range(current, break_start, break_end) and current != break_end:
            return False
    return True


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def maybe_parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _time_in_range(t: time, start: time, end: time) -> bool:
    # inclusive range; supports wrap-around
    if start <= end:
        return start <= t <= end
    return t >= start or t <= end


def _market_session_dt(now_market: datetime, market_tz: ZoneInfo, session_time: time) -> datetime:
    return datetime.combine(now_market.date(), session_time, tzinfo=market_tz)


def _next_trading_day(day):
    next_day = day + timedelta(days=1)
    while next_day.weekday() >= 5:
        next_day += timedelta(days=1)
    return next_day


def _gate_cutoff_dt(
    *,
    gate: dict,
    window_start_dt: datetime,
) -> datetime | None:
    if str(gate.get('type') or '').strip().lower() != 'before':
        return None
    gate_tz = ZoneInfo(str(gate.get('timezone') or 'Asia/Shanghai'))
    gate_time = parse_hhmm(str(gate.get('time') or '02:00'))
    try:
        day_offset = int(gate.get('day_offset_from_window_start') or 0)
    except Exception:
        day_offset = 0
    base_date = window_start_dt.astimezone(gate_tz).date() + timedelta(days=day_offset)
    return datetime.combine(base_date, gate_time, tzinfo=gate_tz)


def _target_allowed_by_gates(*, target: datetime, window_start_dt: datetime, gates: object) -> bool:
    if not isinstance(gates, list):
        return True
    for gate in gates:
        if not isinstance(gate, dict):
            continue
        gate_type = str(gate.get('type') or '').strip().lower()
        if gate_type == 'before':
            cutoff = _gate_cutoff_dt(gate=gate, window_start_dt=window_start_dt)
            if cutoff is not None and not (target.astimezone(cutoff.tzinfo) < cutoff):
                return False
    return True


def _scheduled_run_targets(
    *,
    now_market: datetime,
    market_tz: ZoneInfo,
    run_start: time,
    run_end: time,
    breaks: list[tuple[time, time]],
    run_points: dict,
    gates: object,
) -> list[datetime]:
    start_dt = _market_session_dt(now_market, market_tz, run_start)
    end_dt = _market_session_dt(now_market, market_tz, run_end)
    targets: list[datetime] = []
    if 'start_plus_min' in run_points:
        targets.append(start_dt + timedelta(minutes=max(int(run_points.get('start_plus_min') or 0), 0)))

    if 'hourly_minute' in run_points:
        minute = max(0, min(59, int(run_points.get('hourly_minute') or 0)))
        cursor = start_dt.replace(minute=minute, second=0, microsecond=0)
        if cursor <= start_dt:
            cursor += timedelta(hours=1)
        while cursor < end_dt:
            targets.append(cursor)
            cursor += timedelta(hours=1)

    if 'end_minus_min' in run_points:
        targets.append(end_dt - timedelta(minutes=max(int(run_points.get('end_minus_min') or 0), 0)))

    targets = sorted(set(targets))
    return [
        target
        for target in targets
        if is_run_window_open(target, run_start, run_end, breaks)
        and _target_allowed_by_gates(target=target, window_start_dt=start_dt, gates=gates)
    ]


def _next_target_after(
    *,
    now_market: datetime,
    market_tz: ZoneInfo,
    run_start: time,
    run_end: time,
    breaks: list[tuple[time, time]],
    run_points: dict,
    gates: object,
) -> datetime:
    day_cursor = now_market
    for _ in range(8):
        targets = _scheduled_run_targets(
            now_market=day_cursor,
            market_tz=market_tz,
            run_start=run_start,
            run_end=run_end,
            breaks=breaks,
            run_points=run_points,
            gates=gates,
        )
        for target in targets:
            if target > now_market:
                return target.astimezone(timezone.utc)

        next_day = _next_trading_day(day_cursor.date())
        day_cursor = datetime.combine(next_day, time(0, 0), tzinfo=market_tz)

    return now_market.astimezone(timezone.utc) + timedelta(days=1)


def _last_run_for_account(state: dict, account: str | None) -> datetime | None:
    if not account:
        return maybe_parse_dt(state.get('last_run_utc') or state.get('last_scan_utc'))
    account_key = str(account)
    for map_key in ('last_run_utc_by_account', 'last_scan_utc_by_account'):
        raw_map = state.get(map_key)
        if not isinstance(raw_map, dict):
            continue
        raw_value = raw_map.get(account_key)
        if raw_value:
            return maybe_parse_dt(raw_value)
    return None


def decide(
    schedule_cfg: dict,
    state: dict,
    now_utc: datetime,
    account: str | None = None,
    *,
    schedule_key: str = 'schedule',
    force: bool = False,
) -> SchedulerDecision:
    market_tz = ZoneInfo(str(schedule_cfg.get('timezone') or 'America/New_York'))
    now_market = now_utc.astimezone(market_tz)

    bj_tz = ZoneInfo(str(schedule_cfg.get('beijing_timezone') or 'Asia/Shanghai'))
    now_bj = now_utc.astimezone(bj_tz)

    run_start, run_end, breaks = _resolve_run_window(schedule_cfg)
    in_run_window = is_run_window_open(now_market, run_start, run_end, breaks)
    run_points = schedule_cfg.get('run_points') if isinstance(schedule_cfg.get('run_points'), dict) else {}
    if not run_points:
        run_points = {'start_plus_min': 10, 'hourly_minute': 0, 'end_minus_min': 10}
    gates = schedule_cfg.get('gates') if isinstance(schedule_cfg.get('gates'), list) else []
    try:
        cron_interval_min = int(schedule_cfg.get('cron_interval_min') or 10)
    except Exception:
        cron_interval_min = 10
    catchup_grace = timedelta(minutes=max(1, cron_interval_min) + 2)

    last_run = _last_run_for_account(state, account)

    targets = _scheduled_run_targets(
        now_market=now_market,
        market_tz=market_tz,
        run_start=run_start,
        run_end=run_end,
        breaks=breaks,
        run_points=run_points,
        gates=gates,
    )
    due_targets = [
        target for target in targets
        if target <= now_market and now_market <= target + catchup_grace
    ]
    due_target = due_targets[-1] if due_targets else None
    next_target = next(
        (target for target in targets if target > now_market),
        None,
    )

    if force:
        should_run_scan = True
        is_notify_window_open = True
        reason = 'force 模式：忽略交易时段目标点直接执行。'
        next_run = now_utc
    elif not in_run_window:
        should_run_scan = False
        is_notify_window_open = False
        reason = '业务运行窗口外：不扫描、不通知。'
        next_run = _next_target_after(
            now_market=now_market,
            market_tz=market_tz,
            run_start=run_start,
            run_end=run_end,
            breaks=breaks,
            run_points=run_points,
            gates=gates,
        )
    elif due_target is None:
        should_run_scan = False
        is_notify_window_open = False
        reason = '业务运行窗口内，当前没有待执行运行点。'
        next_run = (
            next_target.astimezone(timezone.utc)
            if next_target
            else _next_target_after(
                now_market=now_market,
                market_tz=market_tz,
                run_start=run_start,
                run_end=run_end,
                breaks=breaks,
                run_points=run_points,
                gates=gates,
            )
        )
    else:
        due_target_utc = due_target.astimezone(timezone.utc)
        already_processed = (
            last_run is not None
            and last_run.astimezone(timezone.utc) >= due_target_utc
        )
        should_run_scan = not already_processed
        is_notify_window_open = not already_processed
        if already_processed:
            reason = f'当前运行点 {due_target.strftime("%H:%M")} 已处理，等待下一个运行点。'
            if next_target is None:
                next_run = _next_target_after(
                    now_market=now_market,
                    market_tz=market_tz,
                    run_start=run_start,
                    run_end=run_end,
                    breaks=breaks,
                    run_points=run_points,
                    gates=gates,
                )
            else:
                next_run = next_target.astimezone(timezone.utc)
        else:
            reason = f'到达运行点 {due_target.strftime("%H:%M")}：执行扫描并允许通知。'
            next_run = now_utc

    start_dt_market = datetime.combine(now_market.date(), run_start, tzinfo=market_tz)
    end_dt_market = datetime.combine(now_market.date(), run_end, tzinfo=market_tz)

    return SchedulerDecision(
        now_utc=to_iso(now_utc),
        now_market=now_market.isoformat(),
        now_beijing=now_bj.isoformat(),
        in_run_window=in_run_window,
        should_run_scan=should_run_scan,
        is_notify_window_open=is_notify_window_open,
        reason=reason,
        next_run_utc=to_iso(next_run),
        next_run_market=next_run.astimezone(market_tz).isoformat(),
        next_run_beijing=next_run.astimezone(bj_tz).isoformat(),
        run_window_start_beijing=start_dt_market.astimezone(bj_tz).isoformat(),
        run_window_end_beijing=end_dt_market.astimezone(bj_tz).isoformat(),
        schedule_key=str(schedule_key),
    )


def run_scheduler(
    *,
    config: str | Path,
    state_dir: str | Path = 'output/state',
    state: str | Path | None = None,
    schedule_key: str = 'schedule',
    account: str | None = None,
    run_if_due: bool = False,
    mark_notified: bool = False,
    mark_scanned: bool = False,
    jsonl: bool = False,
    force: bool = False,
    base_dir: Path | None = None,
) -> dict:
    """执行调度判定并处理状态副作用。"""
    base = _resolve_base(base_dir)
    config_path = _resolve_config_path(config, base=base)
    state_path = _resolve_state_path(base=base, state_dir=state_dir, state=state)

    if config_path.suffix.lower() != '.json':
        raise SystemExit('[CONFIG_ERROR] scheduler config must be a .json file')
    cfg = json.loads(config_path.read_text(encoding='utf-8'))

    schedule_key_val = str(schedule_key or 'schedule')
    schedule_cfg = cfg.get(schedule_key_val, {}) or {}
    schedule_enabled = bool(schedule_cfg.get('enabled', True))

    now_utc = datetime.now(timezone.utc)
    state_data = read_state(state_path)
    decision = decide(
        schedule_cfg,
        state_data,
        now_utc,
        account=(str(account) if account else None),
        schedule_key=schedule_key_val,
        force=bool(force),
    )

    payload = asdict(decision)
    payload['should_notify'] = bool(payload.get('is_notify_window_open'))

    if jsonl:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    if not schedule_enabled and not force:
        print('[INFO] schedule disabled in config; no scheduling enforcement applied.')
        return payload

    if mark_notified:
        now_s = to_iso(datetime.now(timezone.utc))
        state_data['last_notify_utc'] = now_s
        if account:
            m = state_data.get('last_notify_utc_by_account')
            if not isinstance(m, dict):
                m = {}
            m[str(account)] = now_s
            state_data['last_notify_utc_by_account'] = m
        write_state(state_path, state_data)
        print(f'[DONE] marked notified -> {state_path}')
        return payload

    if mark_scanned:
        now_s = to_iso(datetime.now(timezone.utc))
        if account:
            m = state_data.get('last_run_utc_by_account')
            if not isinstance(m, dict):
                m = {}
            m[str(account)] = now_s
            state_data['last_run_utc_by_account'] = m
        write_state(state_path, state_data)
        print(f'[DONE] marked scanned -> {state_path}')
        return payload

    if run_if_due and decision.should_run_scan:
        cmd = [sys.executable, '-m', 'src.interfaces.cli.main', 'scan-pipeline', '--config', str(config_path)]
        print(f"[RUN] {' '.join(cmd)}")
        result = subprocess.run(cmd, cwd=str(base))
        if result.returncode != 0:
            raise SystemExit(result.returncode)
        if account:
            m = state_data.get('last_run_utc_by_account')
            if not isinstance(m, dict):
                m = {}
            m[str(account)] = to_iso(datetime.now(timezone.utc))
            state_data['last_run_utc_by_account'] = m
        write_state(state_path, state_data)
        print(f'[DONE] scheduler state -> {state_path}')
    elif run_if_due:
        print('[SKIP] 当前未到应扫描时间。')

    return payload


# NOTE: market-session selection lives in domain.domain.select_markets_to_run.
