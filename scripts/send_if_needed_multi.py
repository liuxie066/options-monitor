#!/usr/bin/env python3
"""Multi-account production tick (lx/sy) with ONE merged notification.

What you asked for:
- Same watchlist, but portfolio constraints (cash-secured headroom & covered-call capacity)
  must be computed per account separately.
- Do NOT send two separate messages. Merge them into one.
- In Sell Put section, clearly flag which account's cash does NOT support adding 1 contract.
- In Sell Call section, clearly flag which account is covered / not covered.

Implementation:
- Maintain per-account output dirs under ./output_accounts/<acct>/
- Swap ./output symlink to point at the account output dir before running any scripts.
- For each account:
  1) run scan_scheduler.py (account-local state)
  2) if due: run_pipeline.py (with config override: portfolio.account=<acct>)
  3) if should_notify and notification meaningful: collect notification text
- After looping accounts:
  - Build a merged message (with account tags and extra flags)
  - Send once via OpenClaw CLI
  - Mark notified for the accounts that were included in the merged send

Safety:
- Sequential execution.
- Assumes no other concurrent process uses ./output.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from time import monotonic
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta, time
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any
import socket

try:
    from scripts.run_log import RunLogger
except Exception:
    from run_log import RunLogger


DEBUG = False
_CURRENT_RUN_ID: str | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def bj_now() -> str:
    return datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')


def read_json(path: Path, default):
    try:
        if path.exists() and path.stat().st_size > 0:
            return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        pass
    return default


def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

def log(msg: str) -> None:
    try:
        if DEBUG:
            print(msg)
    except Exception:
        pass



def prefetch_required_data(vpy: Path, base: Path, cfg: dict, shared_required: Path) -> dict[str, int]:
    # Fetch required_data for all symbols once per tick into shared_required (raw/parsed).
    # Best-effort: failures should not crash the tick; downstream will handle empty/partial data.
    # New flow: write directly into shared_required; do not touch ./output/{raw,parsed}.
    stats = {
        'symbols_total': 0,
        'cache_hits': 0,
        'fetch_attempts': 0,
        'fallback_to_yahoo': 0,
        'fetch_errors': 0,
        'copied_raw': 0,
        'copied_csv': 0,
    }
    try:
        shared_required.mkdir(parents=True, exist_ok=True)
        (shared_required / 'raw').mkdir(parents=True, exist_ok=True)
        (shared_required / 'parsed').mkdir(parents=True, exist_ok=True)
    except Exception:
        return stats

    syms = cfg.get('symbols') or []
    for it in syms:
        if not isinstance(it, dict):
            continue
        symbol = str(it.get('symbol') or '').strip()
        if not symbol:
            continue
        stats['symbols_total'] += 1

        fetch = (it.get('fetch') or {})
        src = str(fetch.get('source') or 'yahoo').strip().lower()

        # Derive basic option-types from config (account-agnostic)
        want_put = bool((it.get('sell_put') or {}).get('enabled', False))
        want_call = bool((it.get('sell_call') or {}).get('enabled', False))
        opt_types = 'put,call'
        if want_put and (not want_call):
            opt_types = 'put'
        elif want_call and (not want_put):
            opt_types = 'call'

        # Expiration pick policy:
        # For US/HK we want expirations that can satisfy scan min_dte, otherwise the first N expirations
        # are often too near-term and produce 0 candidates.
        limit_exp = int(fetch.get('limit_expirations') or cfg.get('limit_expirations') or 8)
        min_dte_put = float((it.get('sell_put') or {}).get('min_dte') or 0)
        min_dte_call = float((it.get('sell_call') or {}).get('min_dte') or 0)
        min_dte = int(max(min_dte_put, min_dte_call, 0))

        # Skip if already present and sufficient for min_dte
        try:
            raw0 = (shared_required / 'raw' / f"{symbol}_required_data.json").resolve()
            csv0 = (shared_required / 'parsed' / f"{symbol}_required_data.csv").resolve()
            if raw0.exists() and csv0.exists() and raw0.stat().st_size > 0 and csv0.stat().st_size > 0:
                if min_dte > 0:
                    try:
                        import pandas as pd
                        df0 = pd.read_csv(csv0, usecols=['dte'])
                        mx = pd.to_numeric(df0['dte'], errors='coerce').max()
                        if mx is not None and mx >= float(min_dte):
                            stats['cache_hits'] += 1
                            continue
                    except Exception:
                        pass
                else:
                    stats['cache_hits'] += 1
                    continue
        except Exception:
            pass

        cmd = [str(vpy)]
        if src == 'opend':
            host = str(fetch.get('host') or '127.0.0.1')
            port = int(fetch.get('port') or 11111)

            max_dte_put = float((it.get('sell_put') or {}).get('max_dte') or 0)
            max_dte_call = float((it.get('sell_call') or {}).get('max_dte') or 0)
            max_dte = int(max(max_dte_put, max_dte_call, 0))
            max_dte = (max_dte if max_dte > 0 else None)

            cmd += [
                'scripts/fetch_market_data_opend.py',
                '--symbols', symbol,
                '--limit-expirations', str(limit_exp),
                '--min-dte', str(min_dte),
                '--host', host,
                '--port', str(port),
                '--option-types', str(opt_types),
                '--output-root', str(shared_required),
                '--chain-cache',
                '--quiet',
            ]
            # Keep behavior consistent with per-symbol pipeline: for US, default to PM spot unless explicitly disabled.
            try:
                if (str(symbol).strip().upper().endswith('.HK')):
                    pass
                else:
                    spot_from_pm0 = fetch.get('spot_from_portfolio_management', None)
                    if spot_from_pm0 is None:
                        cmd.append('--spot-from-pm')
                    elif bool(spot_from_pm0):
                        cmd.append('--spot-from-pm')
            except Exception:
                pass
            if max_dte is not None:
                cmd += ['--max-dte', str(max_dte)]
        else:
            cmd += [
                'scripts/fetch_market_data.py',
                '--symbols', symbol,
                '--limit-expirations', str(limit_exp),
                '--output-root', str(shared_required),
            ]

        try:
            stats['fetch_attempts'] += 1
            subprocess.run(cmd, cwd=str(base), capture_output=True, text=True, timeout=60)
            # prefetch fallback to yahoo ONLY for US (HK has only OpenD source; no downgrade)
            try:
                if src == 'opend':
                    market = str(it.get('market') or '').upper()
                    raw0 = (shared_required / 'raw' / f"{symbol}_required_data.json").resolve()
                    csv0 = (shared_required / 'parsed' / f"{symbol}_required_data.csv").resolve()
                    empty = (not raw0.exists()) or (not csv0.exists()) or (csv0.stat().st_size <= 0)

                    allow_downgrade = bool(((cfg.get('fetch_policy') or {}).get('allow_downgrade_to_yahoo', True)))

                    if empty and market == 'US' and allow_downgrade:
                        # Fail-fast: do NOT downgrade to Yahoo when OpenD is blocked by login/phone verification.
                        # Downgrading causes YF rate-limit loops and makes the system non-deterministic.
                        err_s = ''
                        try:
                            if raw0.exists() and raw0.stat().st_size > 0:
                                obj0 = json.loads(raw0.read_text(encoding='utf-8'))
                                meta0 = (obj0.get('meta') or {}) if isinstance(obj0, dict) else {}
                                err_s = str(meta0.get('error') or '')
                        except Exception:
                            err_s = ''

                        need_code = ('手机验证码' in err_s) or ('verification' in err_s.lower())
                        if need_code:
                            stats['fetch_errors'] += 1
                        else:
                            cmd2 = [
                                str(vpy), 'scripts/fetch_market_data.py',
                                '--symbols', symbol,
                                '--limit-expirations', str(limit_exp),
                                '--output-root', str(shared_required),
                            ]
                            subprocess.run(cmd2, cwd=str(base), capture_output=True, text=True, timeout=60)
                            stats['fallback_to_yahoo'] += 1
                    elif empty and market == 'US' and (not allow_downgrade):
                        # Explicitly disabled downgrade; count as fetch error for observability.
                        stats['fetch_errors'] += 1
            except Exception:
                pass
        except Exception:
            stats['fetch_errors'] += 1
            continue

        try:
            raw0 = (shared_required / 'raw' / f"{symbol}_required_data.json").resolve()
            csv0 = (shared_required / 'parsed' / f"{symbol}_required_data.csv").resolve()
            if raw0.exists() and raw0.stat().st_size > 0:
                stats['copied_raw'] += 1
            # Accept header-only CSV as valid required_data artifact (avoid 1-byte file issues).
            if csv0.exists() and csv0.stat().st_size > 0:
                stats['copied_csv'] += 1
        except Exception:
            pass

    return stats

def append_json_list(path: Path, payload: dict, max_entries: int = 200):
    """Append payload into a bounded JSON list file. Keeps last max_entries records."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        arr = []
        if path.exists() and path.stat().st_size > 0:
            try:
                obj = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(obj, list):
                    arr = obj
            except Exception:
                arr = []
        arr.append(payload)
        if len(arr) > int(max_entries):
            arr = arr[-int(max_entries):]
        path.write_text(json.dumps(arr, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    except Exception:
        pass


def parse_hhmm(value: str) -> time:
    hour, minute = value.split(':', 1)
    return time(hour=int(hour), minute=int(minute))


def _select_markets_to_run(now_utc: datetime, cfg: dict, market_config: str) -> list[str]:
    mc = str(market_config or 'auto').lower()
    if mc == 'hk':
        return ['HK']
    if mc == 'us':
        return ['US']
    if mc == 'all':
        return ['HK', 'US']

    schedule_hk = (cfg.get('schedule_hk') or {}) if isinstance(cfg, dict) else {}
    schedule_us = (cfg.get('schedule') or {}) if isinstance(cfg, dict) else {}

    try:
        from scripts.scan_scheduler import decide

        state0: dict = {
            'last_scan_utc': None,
            'last_scan_utc_by_account': {},
            'last_notify_utc': None,
            'last_notify_utc_by_account': {},
        }

        d_hk = decide(schedule_hk, state0, now_utc, account=None, schedule_key='schedule_hk')
        if d_hk.in_market_hours:
            return ['HK']

        d_us = decide(schedule_us, state0, now_utc, account=None, schedule_key='schedule')
        if d_us.in_market_hours:
            return ['US']
    except Exception:
        pass

    return []


def maybe_parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def is_high_priority_notification(text: str) -> bool:
    # notify_symbols.py emits "重点:" when there are high-priority candidates.
    return bool(re.search(r"(?m)^重点:\s*$", text or ""))


def parse_last_json(stdout: str) -> dict:
    """Parse the last JSON object printed to stdout (tolerant of logs above it)."""
    lines = (stdout or '').splitlines()
    buf = []
    for ln in reversed(lines):
        if not ln.strip():
            continue
        buf.append(ln)
        if ln.strip().startswith('{'):
            break
    txt = '\n'.join(reversed(buf)).strip()
    return json.loads(txt) if txt else {}


def money_cny(v) -> str:
    try:
        if v is None:
            return '-'
        return f"¥{float(v):,.0f} (CNY)"
    except Exception:
        return '-'


def _snapshot_fresh(payload: dict, max_age_sec: int) -> bool:
    if not payload or max_age_sec <= 0:
        return False
    try:
        as_of = payload.get('as_of_utc')
        if not as_of:
            return False
        dt = datetime.fromisoformat(str(as_of))
        # tolerate naive timestamps by treating them as UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
        return age.total_seconds() <= float(max_age_sec)
    except Exception:
        return False


def query_cash_footer(
    base: Path,
    *,
    market: str,
    accounts: list[str],
    timeout_sec: int = 180,
    snapshot_max_age_sec: int = 900,
) -> list[str]:
    """Compute the cash footer once.

    Engineering goals:
    - Prefer reading per-account cash_snapshot.json (fast + reproducible)
    - Refresh snapshot when missing/stale
    - Best-effort: if some account fails, still print others
    - Make failures visible in the footer
    """
    vpy = str(base / '.venv' / 'bin' / 'python')

    lines: list[str] = []
    payloads: dict[str, dict] = {}
    errors: dict[str, str] = {}

    def _run_one(acct_l: str) -> tuple[str, dict | None, str | None]:
        """Return (acct, payload, error)."""
        state_dir = (base / 'output_accounts' / acct_l / 'state').resolve()
        state_dir.mkdir(parents=True, exist_ok=True)
        snap_path = state_dir / 'cash_snapshot.json'

        # 1) Try snapshot first
        try:
            if snap_path.exists() and snap_path.stat().st_size > 0:
                snap = json.loads(snap_path.read_text(encoding='utf-8'))
                if isinstance(snap, dict) and _snapshot_fresh(snap, snapshot_max_age_sec):
                    return acct_l, snap, None
        except Exception:
            pass

        # 2) Refresh snapshot
        try:
            p = subprocess.run(
                [
                    vpy,
                    'scripts/query_sell_put_cash.py',
                    '--market', str(market),
                    '--account', acct_l,
                    '--format', 'json',
                    '--out-dir', str(state_dir),
                ],
                cwd=str(base),
                capture_output=True,
                text=True,
                timeout=timeout_sec,
            )
        except Exception as e:
            return acct_l, None, f"exec_error: {e}"

        if p.returncode != 0:
            err = (p.stderr or p.stdout or '').strip().splitlines()[-1:]  # last line only
            return acct_l, None, (err[0] if err else f"returncode={p.returncode}")

        payload = parse_last_json(p.stdout)
        try:
            if isinstance(payload, dict) and payload:
                snap_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        except Exception:
            pass

        return acct_l, payload, None

    acct_list = [str(a).strip().lower() for a in accounts if str(a).strip()]

    # Parallelize per-account snapshot read/refresh
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(acct_list)))) as ex:
        futs = {ex.submit(_run_one, acct_l): acct_l for acct_l in acct_list}
        for fut in as_completed(futs):
            acct_l, payload, err = fut.result()
            if err:
                errors[acct_l] = err
            elif payload is not None:
                payloads[acct_l] = payload

    if not payloads and not errors:
        return []

    def asof_bj(payload: dict) -> str:
        try:
            s = payload.get('as_of_utc')
            if not s:
                return ''
            dt = datetime.fromisoformat(str(s))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            bj = dt.astimezone(ZoneInfo('Asia/Shanghai'))
            return bj.strftime('%Y-%m-%d %H:%M')
        except Exception:
            return ''

    lines.append('现金（CNY）:')
    for acct in accounts:
        acct_l = str(acct).strip().lower()
        acct_u = acct_l.upper()
        if acct_l in payloads:
            payload = payloads[acct_l] or {}
            t = asof_bj(payload)
            tag = f" (as_of {t})" if t else ''
            lines.append(
                f"{acct_u}: holding {money_cny(payload.get('cash_available_cny'))} | free {money_cny(payload.get('cash_free_cny'))}{tag}"
            )
        elif acct_l in errors:
            lines.append(f"{acct_u}: (cash query failed) {errors[acct_l]}")

    return lines


def atomic_symlink(path: Path, target: Path):
    tmp = path.with_name(path.name + '.tmp')
    # tmp may be left as a directory from previous runs; remove it robustly.
    if tmp.exists() or tmp.is_symlink():
        try:
            tmp.unlink(missing_ok=True)
        except IsADirectoryError:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
    tmp.symlink_to(target, target_is_directory=True)
    # Replace destination even if it's an existing directory (first-time migration).
    if path.exists() and not path.is_symlink():
        import shutil
        shutil.rmtree(path, ignore_errors=True)
    os.replace(tmp, path)


def ensure_account_output_dir(d: Path):
    (d / 'raw').mkdir(parents=True, exist_ok=True)
    (d / 'parsed').mkdir(parents=True, exist_ok=True)
    (d / 'reports').mkdir(parents=True, exist_ok=True)
    (d / 'state').mkdir(parents=True, exist_ok=True)


def migrate_output_if_needed(base: Path, accounts_root: Path, default_acct: str = 'lx'):
    out = base / 'output'
    if out.exists() and not out.is_symlink():
        dst = accounts_root / default_acct
        ensure_account_output_dir(dst)
        # move children of output/ into dst/
        for child in out.iterdir():
            target = dst / child.name
            if target.exists():
                # best-effort: skip existing
                continue
            child.rename(target)
        try:
            out.rmdir()
        except Exception:
            pass
        atomic_symlink(out, dst)


@dataclass
class AccountResult:
    account: str
    ran_scan: bool
    should_notify: bool
    meaningful: bool
    decision_reason: str
    notification_text: str


HEADROOM_RE = re.compile(r"加仓后余量\s+(?P<val>[-+]?¥?\$?[0-9,]+(?:\.[0-9]+)?)")
CNY_RE = re.compile(r"¥\s*(?P<num>[-+]?[0-9][0-9,]*(?:\.[0-9]+)?)")
COVER_RE = re.compile(r"cover\s+(?P<num>-?[0-9]+)")


def _parse_cny(s: str) -> float | None:
    m = CNY_RE.search(s)
    if not m:
        return None
    try:
        return float(m.group('num').replace(',', ''))
    except Exception:
        return None


def annotate_notification(acct: str, text: str) -> str:
    """Add explicit support flags for Put/Call blocks."""
    if not text:
        return text

    lines = text.splitlines()
    out: list[str] = []

    in_put = False
    in_call = False
    last_line1_idx: int | None = None

    for ln in lines:
        s = ln.rstrip('\n')

        if s.strip() == 'Put:':
            in_put, in_call = True, False
            out.append(s)
            last_line1_idx = None
            continue
        if s.strip() == 'Call:':
            in_put, in_call = False, True
            out.append(s)
            last_line1_idx = None
            continue

        # Heuristic: line1 begins with "<SYMBOL> 卖Put" or "<SYMBOL> 卖Call"
        if in_put and ' 卖Put ' in s:
            out.append(s)
            last_line1_idx = len(out) - 1
            continue
        if in_call and ' 卖Call ' in s:
            out.append(s)
            last_line1_idx = len(out) - 1
            continue

        # For put: annotate based on headroom_cny (negative => cash unsupported)
        if in_put and s.startswith('担保') and ('加仓后余量' in s):
            headroom = _parse_cny(s)
            tag = ''
            if headroom is not None:
                tag = '【现金不足】' if headroom < 0 else '【现金支持】'
            if last_line1_idx is not None and tag:
                out[last_line1_idx] = out[last_line1_idx] + ' ' + tag
            out.append(s)
            continue

        # For call: annotate based on cover >= 1
        if in_call and s.startswith('覆盖') and ('cover' in s):
            m = COVER_RE.search(s)
            tag = ''
            if m:
                try:
                    cover = int(m.group('num'))
                    tag = '【可覆盖】' if cover >= 1 else '【不可覆盖】'
                except Exception:
                    tag = ''
            if last_line1_idx is not None and tag:
                out[last_line1_idx] = out[last_line1_idx] + ' ' + tag
            out.append(s)
            continue

        out.append(s)

    return '\n'.join(out).strip() + '\n'


AUTO_CLOSE_APPLIED_RE = re.compile(r"applied_closed:\s*(?P<n>\d+)")
AUTO_CLOSE_CAND_RE = re.compile(r"candidates_should_close:\s*(?P<n>\d+)")
AUTO_CLOSE_ERR_RE = re.compile(r"^ERRORS:\s*(?P<n>\d+)\s*$", re.M)


def flatten_auto_close_summary(text: str, *, always_show: bool = False) -> str:
    """Return a compact auto-close summary block.

    - When nothing happened (applied=0 and errors=0), return '' unless always_show=True.
    - When applied>0 or errors>0, include compact header plus a few detail lines.
    """
    if not text:
        return ''

    m_applied = AUTO_CLOSE_APPLIED_RE.search(text)
    m_cand = AUTO_CLOSE_CAND_RE.search(text)
    m_err = AUTO_CLOSE_ERR_RE.search(text)

    applied = int(m_applied.group('n')) if m_applied else 0
    cand = int(m_cand.group('n')) if m_cand else applied
    err = int(m_err.group('n')) if m_err else 0

    if applied == 0 and err == 0 and (not always_show):
        return ''

    header = f"Auto-close(exp+1d): closed {applied}/{cand}, errors {err}"
    lines = [header]

    # Append detail lines only when something happened.
    if err > 0 or applied > 0:
        # Prefer explicit error bullets, else closed list bullets.
        for ln in text.splitlines():
            if ln.startswith('- '):
                lines.append(ln)
            if len(lines) >= 1 + 6:
                break

    return ('---\n' + '\n'.join(lines).strip()).strip()


def build_merged_message(
    results: list[AccountResult],
    *,
    base_cfg: dict | None = None,
    cash_accounts: list[str] | None = None,
) -> str:
    now_bj = bj_now()
    lines: list[str] = []
    lines.append("options-monitor 合并提醒")
    lines.append(f"北京时间: {now_bj}")
    lines.append('')

    any_content = False
    # Cash footer is computed once at the end (do not rely on parsing per-account notifications).

    for r in results:
        if not (r.should_notify and r.meaningful and r.notification_text.strip()):
            continue
        any_content = True

        # Per-account notification should not include cash footer anymore.
        # Keep logic simple: just take the text as-is.
        kept = r.notification_text.strip().splitlines()

        # Count Put/Call blocks for this account (for quick sanity check; sections may be omitted when empty)
        put_n = sum(1 for ln in kept if ' 卖Put ' in ln)
        call_n = sum(1 for ln in kept if ' 卖Call ' in ln)

        lines.append(f"[{r.account.upper()}]（Put {put_n} / Call {call_n}）")
        lines.append(annotate_notification(r.account, '\n'.join(kept).strip() + '\n').strip())
        lines.append('')

    if not any_content:
        return ''

    # Append cash footer at the end (compute once)
    try:
        base = Path(__file__).resolve().parents[1]
        cfg = base_cfg or {}
        cfg_market = str((cfg.get('portfolio') or {}).get('market') or '富途')

        notif_cfg = (cfg.get('notifications') or {}) if isinstance(cfg, dict) else {}
        accts = cash_accounts or (notif_cfg.get('cash_footer_accounts') or ['lx', 'sy'])
        timeout_sec = int(notif_cfg.get('cash_footer_timeout_sec') or 180)
        max_age_sec = int(notif_cfg.get('cash_snapshot_max_age_sec') or 900)

        footer_lines = query_cash_footer(
            base,
            market=cfg_market,
            accounts=accts,
            timeout_sec=timeout_sec,
            snapshot_max_age_sec=max_age_sec,
        )
        if footer_lines:
            lines.extend(footer_lines)
            lines.append('')
    except Exception:
        # best-effort: skip cash footer if anything fails
        pass

    return '\n'.join(lines).strip() + '\n'


def _parse_last_json_obj(text: str) -> dict:
    s = (text or '').strip()
    if not s:
        return {}
    i = s.find('{')
    j = s.rfind('}')
    if i < 0 or j < 0 or j <= i:
        return {}
    try:
        return json.loads(s[i:j+1])
    except Exception:
        return {}


def _opend_alert_rl_path(base: Path) -> Path:
    return (base / 'output_shared' / 'state' / 'opend_alert_rate_limit.json').resolve()


def _should_send_opend_alert(base: Path, error_code: str, cooldown_sec: int = 600) -> bool:
    p = _opend_alert_rl_path(base)
    now = datetime.now(timezone.utc)
    st = read_json(p, {}) if p.exists() else {}
    if not isinstance(st, dict):
        st = {}

    m = st.get('last_sent_utc_by_error')
    if not isinstance(m, dict):
        m = {}

    prev = m.get(str(error_code))
    if prev:
        try:
            dt = datetime.fromisoformat(str(prev))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if (now - dt.astimezone(timezone.utc)).total_seconds() < int(cooldown_sec):
                return False
        except Exception:
            pass

    m[str(error_code)] = now.isoformat()
    st['last_sent_utc_by_error'] = m
    write_json(p, st)
    return True


def _send_opend_alert(base: Path, cfg: dict, *, error_code: str, message_text: str, detail: str = '', no_send: bool = False) -> bool:
    """Send OpenD unhealthy alert with per-error-code rate limit.

    Returns True iff a message was actually sent.
    """
    cooldown_sec = 600
    try:
        v = ((cfg.get('notifications') or {}).get('opend_alert_cooldown_sec'))
        if v is not None:
            cooldown_sec = max(60, int(v))
    except Exception:
        cooldown_sec = 600

    if not _should_send_opend_alert(base, str(error_code), cooldown_sec=cooldown_sec):
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


def _tcp_open(host: str, port: int, timeout_sec: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout_sec):
            return True
    except Exception:
        return False


def _safe_runlog_data(data: dict[str, Any] | None, max_items: int = 16) -> dict[str, Any]:
    """Small summary-only payload for run log events."""
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    for i, (k, v) in enumerate(data.items()):
        if i >= max_items:
            out['_truncated'] = True
            break
        kk = str(k)[:60]
        if isinstance(v, dict):
            out[kk] = {'_type': 'dict', 'size': len(v), 'keys': list(v.keys())[:8]}
        elif isinstance(v, (list, tuple, set)):
            out[kk] = {'_type': 'list', 'size': len(v)}
        elif isinstance(v, str):
            out[kk] = v[:160]
        elif v is None or isinstance(v, (bool, int, float)):
            out[kk] = v
        else:
            out[kk] = str(v)[:160]
    return out


def main():
    ap = argparse.ArgumentParser(description='Multi-account tick with merged notification')
    ap.add_argument('--config', default='config.json')
    ap.add_argument('--accounts', nargs='+', required=True)
    ap.add_argument('--default-account', default='lx')
    ap.add_argument('--market-config', default='auto', choices=['auto','hk','us','all'], help='Select symbols by market at config-load time (auto=by session).')
    ap.add_argument('--no-send', action='store_true', help='Do not send messages (for smoke tests / debugging).')
    ap.add_argument('--smoke', action='store_true', help='Smoke mode: run scheduler decisions but skip pipeline execution.')
    ap.add_argument('--force', action='store_true', help='Force running scan pipeline regardless of market hours / scan interval (sending still respects --no-send and should_notify decisions).')
    ap.add_argument('--debug', action='store_true', help='Verbose logs to stdout (for manual debugging).')
    args = ap.parse_args()

    global DEBUG
    DEBUG = bool(getattr(args, 'debug', False))

    no_send = bool(getattr(args, 'no_send', False))
    smoke = bool(getattr(args, 'smoke', False))

    base = Path(__file__).resolve().parents[1]
    vpy = base / '.venv' / 'bin' / 'python'
    runlog = RunLogger(base)
    global _CURRENT_RUN_ID
    _CURRENT_RUN_ID = runlog.run_id

    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = (base / cfg_path).resolve()
    base_cfg = json.loads(cfg_path.read_text(encoding='utf-8'))

    # run_start checkpoint
    try:
        syms0 = base_cfg.get('symbols') or []
        src_counts: dict[str, int] = {}
        for it in syms0:
            if not isinstance(it, dict):
                continue
            src = str(((it.get('fetch') or {}).get('source') or 'yahoo')).lower()
            src_counts[src] = src_counts.get(src, 0) + 1
        runlog.event(
            'run_start',
            'start',
            data=_safe_runlog_data({
                'accounts': [str(a).strip().lower() for a in (args.accounts or []) if str(a).strip()],
                'symbols_count': len([x for x in syms0 if isinstance(x, dict)]),
                'source_selections': src_counts,
                'market_config': str(getattr(args, 'market_config', 'auto') or 'auto'),
                'no_send': bool(no_send),
                'smoke': bool(smoke),
            }),
        )
    except Exception:
        pass

    market_cfg = str(getattr(args, 'market_config', 'auto') or 'auto').lower()
    if market_cfg in ('hk','us'):
        try:
            base_cfg = dict(base_cfg)
            syms = base_cfg.get('symbols') or []
            base_cfg['symbols'] = [it for it in syms if isinstance(it, dict) and (it.get('market') == market_cfg.upper())]
        except Exception:
            pass
    # auto/all: keep full config; later market-aware filtering still applies

    schedule_cfg = base_cfg.get('schedule', {}) or {}
    dense_notify_cooldown_min = int(schedule_cfg.get('notify_cooldown_dense_min', 30))
    sparse_after_beijing = parse_hhmm(schedule_cfg.get('sparse_after_beijing', '02:00'))
    bj_tz = ZoneInfo(schedule_cfg.get('beijing_timezone', 'Asia/Shanghai'))

    # Preflight: OpenD watchdog (ensure process + login state).
    # When watchdog is unhealthy, fail-fast and alert (rate-limited per error_code).
    t_watchdog0 = monotonic()
    try:
        runlog.event('watchdog', 'start')
    except Exception:
        pass
    try:
        fetch_policy = base_cfg.get('fetch_policy') if isinstance(base_cfg, dict) else None
        allow_downgrade = True
        try:
            if isinstance(fetch_policy, dict) and ('allow_downgrade_to_yahoo' in fetch_policy):
                allow_downgrade = bool(fetch_policy.get('allow_downgrade_to_yahoo'))
        except Exception:
            allow_downgrade = True

        need_opend = False
        ports = set()
        has_hk_opend = False
        for sym in (base_cfg.get('symbols') or []):
            fetch = (sym or {}).get('fetch') or {}
            if str(fetch.get('source') or '').lower() == 'opend':
                need_opend = True
                host = fetch.get('host') or '127.0.0.1'
                port = fetch.get('port') or 11111
                ports.add((str(host), int(port)))
                if str((sym or {}).get('market') or '').upper() == 'HK':
                    has_hk_opend = True

        if need_opend:
            unhealthy = None

            # Ensure OpenD process once, then do formal watchdog json check.
            for host, port in sorted(ports):
                try:
                    wd0 = subprocess.run(
                        [str(vpy), 'scripts/opend_watchdog.py', '--ensure', '--host', str(host), '--port', str(port), '--json'],
                        cwd=str(base),
                        capture_output=True,
                        text=True,
                        timeout=35,
                    )
                    payload0 = _parse_last_json_obj((wd0.stdout or '') + '\n' + (wd0.stderr or ''))
                    ok0 = bool(payload0.get('ok')) if payload0 else (wd0.returncode == 0)
                    if not ok0:
                        unhealthy = {
                            'host': host,
                            'port': port,
                            'payload': payload0,
                            'detail': ((wd0.stdout or '') + '\n' + (wd0.stderr or '')).strip(),
                        }
                        break
                except Exception as e:
                    unhealthy = {
                        'host': host,
                        'port': port,
                        'payload': {'ok': False, 'error_code': 'OPEND_API_ERROR', 'message': 'OpenD 看门狗执行失败'},
                        'detail': f'{type(e).__name__}: {e}',
                    }
                    break

            if unhealthy is not None:
                payload = unhealthy.get('payload') or {}
                error_code = str(payload.get('error_code') or 'OPEND_API_ERROR')
                msg = str(payload.get('message') or payload.get('error') or 'OpenD 不健康')
                detail = str(unhealthy.get('detail') or '')
                host = unhealthy.get('host')
                port = unhealthy.get('port')

                # Optional degrade for US only; if HK OpenD exists, always fail-fast.
                degraded = False
                if allow_downgrade and (not has_hk_opend):
                    try:
                        for sym in (base_cfg.get('symbols') or []):
                            if str((sym or {}).get('market') or '').upper() != 'US':
                                continue
                            fetch = (sym or {}).get('fetch') or {}
                            if str(fetch.get('source') or '').lower() == 'opend':
                                fetch['source'] = 'yahoo'
                                for k in ['host', 'port', 'spot_from_portfolio_management']:
                                    fetch.pop(k, None)
                                sym['fetch'] = fetch
                                degraded = True
                    except Exception:
                        degraded = False

                # Alert on any unhealthy watchdog.
                _send_opend_alert(
                    base,
                    base_cfg,
                    error_code=error_code,
                    message_text=msg,
                    detail=(f"{host}:{port} {detail}" if host is not None and port is not None else detail),
                    no_send=no_send,
                )

                now = utc_now()
                for acct in args.accounts:
                    acct0 = str(acct).strip().lower()
                    if not acct0:
                        continue
                    try:
                        state_dir = (base / 'output_accounts' / acct0 / 'state')
                        state_dir.mkdir(parents=True, exist_ok=True)
                        write_json(state_dir / 'last_run.json', {
                            'last_run_utc': now,
                            'sent': False,
                            'reason': 'opend_unhealthy',
                            'error_code': error_code,
                            'detail': msg,
                        })
                    except Exception:
                        pass

                if degraded:
                    log(f"[WARN] OpenD unhealthy ({error_code}); degraded US opend sources to yahoo for this run")
                    try:
                        runlog.event(
                            'watchdog',
                            'degraded',
                            duration_ms=int((monotonic() - t_watchdog0) * 1000),
                            error_code=error_code,
                            message=msg,
                            data=_safe_runlog_data({'degraded': True, 'host': host, 'port': port}),
                        )
                    except Exception:
                        pass
                else:
                    # Fail-fast: do not proceed symbol scans when OpenD source is unhealthy.
                    try:
                        runlog.event(
                            'watchdog',
                            'error',
                            duration_ms=int((monotonic() - t_watchdog0) * 1000),
                            error_code=error_code,
                            message=msg,
                            data=_safe_runlog_data({'degraded': False, 'host': host, 'port': port}),
                        )
                        runlog.event(
                            'run_end',
                            'error',
                            error_code=error_code,
                            message='opend watchdog unhealthy',
                            data=_safe_runlog_data({'sent': False, 'reason': 'opend_unhealthy'}),
                        )
                    except Exception:
                        pass
                    return 0

    except SystemExit:
        raise
    except Exception as e:
        # best-effort: do not block execution if watchdog fails unexpectedly
        try:
            runlog.event(
                'watchdog',
                'error',
                duration_ms=int((monotonic() - t_watchdog0) * 1000),
                error_code='WATCHDOG_EXCEPTION',
                message=str(e),
            )
        except Exception:
            pass
        pass

    try:
        runlog.event('watchdog', 'ok', duration_ms=int((monotonic() - t_watchdog0) * 1000))
    except Exception:
        pass

    # Ensure output/accounts layout
    # Transitional: clean run_dir history (keep last 7 days)
    # Safety: only delete directories that look like real run_id (YYYYMMDDTHHMMSS).
    # Never delete manual runs / ad-hoc dirs.
    try:
        import shutil, time, re
        runs_root = (base / 'output_runs').resolve()
        runs_root.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - 7 * 86400
        pat = re.compile(r'^\d{8}T\d{6}$')
        for d in runs_root.iterdir():
            try:
                if not d.is_dir():
                    continue
                if not pat.match(d.name):
                    continue
                if d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass
    except Exception:
        pass

    accounts_root = (base / 'output_accounts').resolve()
    accounts_root.mkdir(parents=True, exist_ok=True)
    migrate_output_if_needed(base, accounts_root, default_acct=args.default_account)

    out_link = base / 'output'
    if not out_link.exists():
        dst = accounts_root / args.default_account
        ensure_account_output_dir(dst)
        out_link.symlink_to(dst, target_is_directory=True)
    if not out_link.is_symlink():
        raise SystemExit(f"./output must be a symlink for multi-account mode: {out_link}")

    results: list[AccountResult] = []

    # Market-aware filtering (speed): only run symbols for the current market session.
    # Scheduler is the single source of truth for market-hours judging.
    now_utc = datetime.now(timezone.utc)
    markets_to_run: list[str] = _select_markets_to_run(now_utc, base_cfg, getattr(args, 'market_config', 'auto'))


    # Transitional run_dir (new flow): keep required_data as a per-run artifact.
    # For now we still run per-account pipelines, but we fetch required_data once and copy it into run_dir.
    run_id = utc_now().replace(':','').replace('-','').split('.')[0]
    run_dir = (base / 'output_runs' / run_id).resolve()
    required_dir = (run_dir / 'required_data').resolve()
    required_raw = (required_dir / 'raw').resolve()
    required_parsed = (required_dir / 'parsed').resolve()
    required_raw.mkdir(parents=True, exist_ok=True)
    required_parsed.mkdir(parents=True, exist_ok=True)

    prefetch_done = False
    # Reuse existing prefetch implementation by passing run_dir/required_data as the shared_required sink.
    shared_required = required_dir
    tick_metrics_path = (base / 'output_shared' / 'state' / 'tick_metrics.json').resolve()
    tick_metrics_history_path = (base / 'output_shared' / 'state' / 'tick_metrics_history.json').resolve()

    # Per-run state (new flow): write a full copy of tick_metrics into run_dir for replay/audit.
    run_state_dir = (run_dir / 'state').resolve()
    run_state_dir.mkdir(parents=True, exist_ok=True)
    tick_metrics_run_path = (run_state_dir / 'tick_metrics.json').resolve()
    tick_metrics_history_run_path = (run_state_dir / 'tick_metrics_history.json').resolve()

    # Record run_dir for traceability (latest pointer)
    try:
        tick_metrics_run_dir_path = (base / 'output_shared' / 'state' / 'last_run_dir.txt').resolve()
        tick_metrics_run_dir_path.write_text(str(run_dir) + "\n", encoding='utf-8')
    except Exception:
        pass
    tick_metrics = {
        'as_of_utc': utc_now(),
        'markets_to_run': markets_to_run,
        'run_dir': str(run_dir),
        'accounts': [],
        'sent': False,
        'reason': '',
    }

    for acct in args.accounts:
        acct = str(acct).strip()
        if not acct:
            continue

        acct_out = accounts_root / acct
        # Legacy cleanup: per-account scheduler_state.json is no longer authoritative. Rename once to avoid confusion.
        try:
            legacy = (acct_out / 'state' / 'scheduler_state.json').resolve()
            legacy_dst = (acct_out / 'state' / 'scheduler_state.legacy.json').resolve()
            if legacy.exists() and (not legacy_dst.exists()):
                legacy_dst.parent.mkdir(parents=True, exist_ok=True)
                legacy.rename(legacy_dst)
        except Exception:
            pass
        acct_metrics = {
            'account': acct,
            'scheduler_ms': None,
            'pipeline_ms': None,
            'ran_scan': False,
            'should_notify': False,
            'meaningful': False,
            'reason': '',
        }
        ensure_account_output_dir(acct_out)

        # Switch ./output -> this account
        atomic_symlink(out_link, acct_out)

        # Write per-account config override (portfolio.account) + market-aware symbol filtering
        cfg = json.loads(json.dumps(base_cfg))
        cfg.setdefault('portfolio', {})
        cfg['portfolio']['account'] = acct

        try:
            syms = cfg.get('symbols') or []
            if markets_to_run:
                syms = [it for it in syms if isinstance(it, dict) and (it.get('market') in markets_to_run)]
            cfg['symbols'] = syms
        except Exception:
            pass
        cfg_override = acct_out / 'state' / 'config.override.json'
        cfg_override.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

        # Unified scan timing: use ONE shared scheduler_state per market.
        # Notify cooldown remains per-account (stored in last_notify_utc_by_account within the same shared file).
        shared_state_dir = (base / 'output_shared' / 'state').resolve()
        shared_state_dir.mkdir(parents=True, exist_ok=True)
        if markets_to_run == ['HK']:
            state_path = shared_state_dir / 'scheduler_state_hk.json'
        elif markets_to_run == ['US']:
            state_path = shared_state_dir / 'scheduler_state_us.json'
        else:
            state_path = shared_state_dir / 'scheduler_state.json'

        # Ensure shared scheduler state exists (so ops/debug can inspect even before first scan/notify)
        try:
            if (not state_path.exists()) or state_path.stat().st_size <= 0:
                write_json(state_path, {
                    'last_scan_utc': None,
                    'last_notify_utc': None,
                    'last_notify_utc_by_account': {},
                })
        except Exception:
            pass

        # Migrate legacy per-account scheduler_state.json into shared state (one-time best-effort)
        try:
            st0 = read_json(state_path, {})
            if isinstance(st0, dict) and (not st0.get('last_scan_utc')) and (not st0.get('last_notify_utc')):
                legacy_candidates = []
                for _acct in args.accounts:
                    lp = (accounts_root / _acct / 'state' / 'scheduler_state.json').resolve()
                    if lp.exists() and lp.stat().st_size > 0:
                        try:
                            obj = read_json(lp, {})
                            if isinstance(obj, dict):
                                legacy_candidates.append(obj)
                        except Exception:
                            pass

                best_scan = None
                best_notify = None
                for obj in legacy_candidates:
                    s = obj.get('last_scan_utc')
                    n = obj.get('last_notify_utc')
                    if s and ((best_scan is None) or (str(s) > str(best_scan))):
                        best_scan = s
                    if n and ((best_notify is None) or (str(n) > str(best_notify))):
                        best_notify = n

                if best_scan or best_notify:
                    st0['last_scan_utc'] = best_scan
                    st0['last_notify_utc'] = best_notify
                    write_json(state_path, st0)
        except Exception:
            pass
        # New flow: pipeline writes into run_dir/accounts/<acct>
        acct_report_dir = (run_dir / 'accounts' / acct).resolve()
        acct_state_dir = (acct_report_dir / 'state').resolve()
        try:
            acct_state_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        def _write_acct_run_state(name: str, payload: dict):
            try:
                acct_state_dir.mkdir(parents=True, exist_ok=True)
                write_json((acct_state_dir / name).resolve(), payload)
            except Exception:
                pass

        notif_path = (acct_report_dir / 'symbols_notification.txt').resolve()

        # 1) scheduler decision
        # market-aware schedule: use schedule_hk during HK session, otherwise default schedule
        sch_args = [str(vpy), 'scripts/scan_scheduler.py', '--config', str(cfg_override), '--state', str(state_path), '--state-dir', str((run_dir / 'state').resolve()), '--jsonl', '--account', str(acct)]
        try:
            if markets_to_run == ['HK'] and ('schedule_hk' in cfg):
                sch_args.extend(['--schedule-key', 'schedule_hk'])
        except Exception:
            pass
        t_sch0 = monotonic()
        sch = subprocess.run(
            sch_args,
            cwd=str(base),
            capture_output=True,
            text=True,
        )
        acct_metrics['scheduler_ms'] = int((monotonic() - t_sch0) * 1000)
        if sch.returncode != 0:
            acct_metrics['ran_scan'] = False
            acct_metrics['should_notify'] = False
            acct_metrics['meaningful'] = False
            acct_metrics['reason'] = f"scheduler error: {(sch.stderr or sch.stdout).strip()}"
            tick_metrics['accounts'].append(acct_metrics)
            results.append(AccountResult(acct, False, False, False, f"scheduler error: {(sch.stderr or sch.stdout).strip()}", ''))
            continue

        decision = json.loads((sch.stdout or '').strip())
        # Persist scheduler decision per-account into run_dir for replay/audit.
        _write_acct_run_state('scheduler_decision.json', {
            'as_of_utc': utc_now(),
            'account': acct,
            'decision': decision,
        })

        should_run = bool(decision.get('should_run_scan'))
        should_notify = bool(decision.get('is_notify_window_open', decision.get('should_notify')))
        reason = str(decision.get('reason') or '')

        # Transitional: allow forcing pipeline runs even off-hours (useful for dev/test).
        # Notify eligibility is still controlled by scheduler + send_if_needed_multi logic.
        if bool(getattr(args, 'force', False)):
            should_run = True
            reason = (reason + ' | force').strip(' |')

        if smoke:
            should_run = False
            reason = (str(reason) + ' | smoke_skip_pipeline').strip()
        acct_metrics['should_notify'] = bool(should_notify)
        acct_metrics['reason'] = str(reason)

        # Persist lightweight per-account metrics snapshot early.
        _write_acct_run_state('account_metrics.json', {
            'as_of_utc': utc_now(),
            'account': acct,
            'markets_to_run': markets_to_run,
            'scheduler_ms': acct_metrics.get('scheduler_ms'),
            'pipeline_ms': acct_metrics.get('pipeline_ms'),
            'ran_scan': acct_metrics.get('ran_scan'),
            'should_notify': acct_metrics.get('should_notify'),
            'meaningful': acct_metrics.get('meaningful'),
            'reason': acct_metrics.get('reason'),
            'run_dir': str(run_dir),
        })

        if not should_run:
            acct_metrics['ran_scan'] = False
            acct_metrics['meaningful'] = False
            tick_metrics['accounts'].append(acct_metrics)
            results.append(AccountResult(acct, False, should_notify, False, reason, ''))
            continue

        # 2) pipeline (scheduled mode: faster, less output)
        # If market-aware filtering leaves us with no symbols, skip early.
        try:
            if markets_to_run and (not (cfg.get('symbols') or [])):
                results.append(AccountResult(acct, False, should_notify, False, reason + ' | 本时段无对应市场标的', ''))
                continue
        except Exception:
            pass

        # Prefetch required_data once per tick into run_dir/required_data.
        if (not prefetch_done):
            try:
                runlog.event('fetch_chain_cache', 'start', data=_safe_runlog_data({'account': acct, 'symbols_count': len(cfg.get('symbols') or [])}))
            except Exception:
                pass
            try:
                prefetch_stats = prefetch_required_data(vpy=vpy, base=base, cfg=cfg, shared_required=shared_required)
                runlog.event('fetch_chain_cache', 'ok', data=_safe_runlog_data(prefetch_stats))
            except Exception as e:
                try:
                    runlog.event('fetch_chain_cache', 'error', error_code='FETCH_CHAIN_EXCEPTION', message=str(e))
                except Exception:
                    pass
            prefetch_done = True

        # run_pipeline consumes shared_required_data as authoritative required_data source.
        acct_report_dir.mkdir(parents=True, exist_ok=True)

        pipe_cmd = [
            str(vpy), 'scripts/run_pipeline.py',
            '--config', str(cfg_override),
            '--mode', 'scheduled',
            '--shared-required-data', str(shared_required),
            '--report-dir', str(acct_report_dir),
            '--state-dir', str((run_dir / 'state').resolve()),
        ]
        try:
            runlog.event(
                'snapshot_batches',
                'start',
                data=_safe_runlog_data({'account': acct}),
            )
        except Exception:
            pass

        t_pipe0 = monotonic()
        pipe = subprocess.run(
            pipe_cmd,
            cwd=str(base),
            capture_output=True,
            text=True,
            env=dict(os.environ, PYTHONPATH=str(base)),
        )
        acct_metrics['pipeline_ms'] = int((monotonic() - t_pipe0) * 1000)
        if pipe.returncode != 0:
            try:
                runlog.event(
                    'snapshot_batches',
                    'error',
                    duration_ms=acct_metrics['pipeline_ms'],
                    error_code='PIPELINE_FAILED',
                    message=f'pipeline failed for {acct}',
                    data=_safe_runlog_data({'account': acct, 'returncode': pipe.returncode}),
                )
            except Exception:
                pass
            # Only print the tail for debugging (avoid noisy logs on success)
            out = ((pipe.stdout or '') + '\n' + (pipe.stderr or '')).strip()
            if out:
                tail = '\n'.join(out.splitlines()[-60:])
                log(f"[ERR] pipeline failed ({acct})\n{tail}")
            acct_metrics['ran_scan'] = True
            acct_metrics['meaningful'] = False
            acct_metrics['reason'] = 'pipeline failed'
            tick_metrics['accounts'].append(acct_metrics)
            results.append(AccountResult(acct, True, should_notify, False, 'pipeline failed', ''))
            continue

        try:
            runlog.event(
                'snapshot_batches',
                'ok',
                duration_ms=acct_metrics['pipeline_ms'],
                data=_safe_runlog_data({'account': acct}),
            )
        except Exception:
            pass
        # Mark scanned (shared scan clock)
        try:
            subprocess.run([str(vpy), 'scripts/scan_scheduler.py', '--config', str(cfg_override), '--state', str(state_path), '--state-dir', str((run_dir / 'state').resolve()), '--mark-scanned', '--account', str(acct)], cwd=str(base))
        except Exception:
            pass

        text = notif_path.read_text(encoding='utf-8', errors='replace').strip() if notif_path.exists() else ''

        # Transitional: persist per-account outputs into run_dir for traceability.
        try:
            acct_run_dir = (run_dir / 'accounts' / acct).resolve()
            acct_run_dir.mkdir(parents=True, exist_ok=True)
            # notification
            (acct_run_dir / 'symbols_notification.txt').write_text(text + '\n', encoding='utf-8')
            # summary is already written into acct_run_dir by run_pipeline via --report-dir
            # keep copy step as no-op for backward compat
            src_summary = (acct_run_dir / 'symbols_summary.csv').resolve()
            if src_summary.exists() and src_summary.stat().st_size > 0:
                pass
            # config override (best-effort)
            if cfg_override.exists() and cfg_override.stat().st_size > 0:
                (acct_run_dir / 'config.override.json').write_bytes(cfg_override.read_bytes())
        except Exception:
            pass

        # Append compact auto-close summary (only when applied>0 or errors>0)
        auto_close_path = acct_report_dir / 'auto_close_summary.txt'
        auto_close_text = auto_close_path.read_text(encoding='utf-8', errors='replace').strip() if auto_close_path.exists() else ''
        auto_close_flat = flatten_auto_close_summary(auto_close_text, always_show=False)
        if auto_close_flat:
            text = (text.strip() + '\n\n' + auto_close_flat.strip()).strip()

        meaningful = bool(text) and (text != '今日无需要主动提醒的内容。')

        # Content-aware notify override (your preference):
        # Before Beijing 02:00, allow HIGH-priority notifications as frequently as every 30 minutes.
        # Medium/changes still follow scan_scheduler's base cooldown (typically 60 minutes).
        should_notify_effective = should_notify
        try:
            now_bj = datetime.now(timezone.utc).astimezone(bj_tz)
            before_sparse = now_bj.time() < sparse_after_beijing
            high_pri = meaningful and is_high_priority_notification(text)

            if (not should_notify_effective) and before_sparse and high_pri:
                st = read_json(state_path, {'last_notify_utc': None})
                last_notify = maybe_parse_dt((st or {}).get('last_notify_utc')) if isinstance(st, dict) else None
                if last_notify is None:
                    should_notify_effective = True
                    reason = (reason + f" | override(high,dense): last_notify missing")
                else:
                    elapsed = datetime.now(timezone.utc) - last_notify.astimezone(timezone.utc)
                    if elapsed >= timedelta(minutes=dense_notify_cooldown_min):
                        should_notify_effective = True
                        reason = (reason + f" | override(high,dense): elapsed>={dense_notify_cooldown_min}m")
        except Exception:
            pass

        acct_metrics['ran_scan'] = True
        acct_metrics['should_notify'] = bool(should_notify_effective)
        acct_metrics['meaningful'] = bool(meaningful)
        acct_metrics['reason'] = str(reason)
        tick_metrics['accounts'].append(acct_metrics)
        results.append(AccountResult(acct, True, should_notify_effective, meaningful, reason, text))

    try:
        runlog.event(
            'notify',
            'prepare',
            data=_safe_runlog_data({
                'results_count': len(results),
                'notify_candidates': len([r for r in results if r.should_notify and r.meaningful and bool(r.notification_text.strip())]),
            }),
        )
    except Exception:
        pass

    merged = build_merged_message(results, base_cfg=base_cfg, cash_accounts=['lx', 'sy'])
    if not merged:
        try:
            runlog.event('notify', 'skip', message='no merged notification content')
        except Exception:
            pass

        # Even if we didn't send, record that we ran.
        try:
            # True shared marker (NOT under ./output symlink)
            shared_last = (base / 'output_shared' / 'state' / 'last_run.json').resolve()
            shared_last.parent.mkdir(parents=True, exist_ok=True)
            write_json(shared_last, {
                'last_run_utc': utc_now(),
                'sent': False,
                'reason': 'no_merged_notification',
                'account': 'merged',
                'accounts': [r.account for r in results],
                'results': [r.__dict__ for r in results],
            })
        except Exception:
            pass

        # Per-account marker for easier debugging.
        try:
            for r in results:
                acct_out = accounts_root / r.account
                payload = {
                    'last_run_utc': utc_now(),
                    'sent': False,
                    'reason': 'no_merged_notification',
                    'account': r.account,
                    'result': r.__dict__,
                    'run_dir': str(run_dir),
                }
                # legacy pointer
                write_json(acct_out / 'state' / 'last_run.json', payload)
                # per-run copy
                write_json((run_dir / 'accounts' / r.account / 'state' / 'last_run.json').resolve(), payload)
        except Exception:
            pass

        try:
            tick_metrics['sent'] = False
            tick_metrics['reason'] = 'no_merged_notification'
            # shared (latest pointer)
            write_json(tick_metrics_path, tick_metrics)
            append_json_list(tick_metrics_history_path, tick_metrics)
            # per-run (replay/audit)
            write_json(tick_metrics_run_path, tick_metrics)
            append_json_list(tick_metrics_history_run_path, tick_metrics)
        except Exception:
            pass

        try:
            runlog.event('run_end', 'ok', data=_safe_runlog_data({'sent': False, 'reason': 'no_merged_notification', 'accounts': [r.account for r in results]}))
        except Exception:
            pass

        return 0

    no_send = bool(getattr(args, 'no_send', False))

    # Send ONCE
    channel = (base_cfg.get('notifications') or {}).get('channel') or 'feishu'
    target = (base_cfg.get('notifications') or {}).get('target')

    if not no_send:
        if not target:
            try:
                runlog.event('notify', 'error', error_code='CONFIG_ERROR', message='notifications.target is required')
            except Exception:
                pass
            raise SystemExit('[CONFIG_ERROR] notifications.target is required')

        try:
            runlog.event('notify', 'start', data=_safe_runlog_data({'channel': channel, 'target_set': bool(target), 'message_len': len(merged)}))
        except Exception:
            pass

        t_notify0 = monotonic()
        send = subprocess.run(
            ['openclaw', 'message', 'send', '--channel', str(channel), '--target', str(target), '--message', merged, '--json'],
            cwd=str(base),
            capture_output=True,
            text=True,
        )
        if send.returncode != 0:
            try:
                runlog.event(
                    'notify',
                    'error',
                    duration_ms=int((monotonic() - t_notify0) * 1000),
                    error_code='SEND_FAILED',
                    message='message send failed',
                    data=_safe_runlog_data({'returncode': send.returncode}),
                )
            except Exception:
                pass
            raise SystemExit(send.returncode)

        try:
            runlog.event('notify', 'ok', duration_ms=int((monotonic() - t_notify0) * 1000), data=_safe_runlog_data({'channel': channel}))
        except Exception:
            pass
    else:
        # Smoke/debug: do not send and do not mark notified
        target = None
        try:
            runlog.event('notify', 'skip', message='no_send mode')
        except Exception:
            pass

    # Mark notified once per merged-message recipient to avoid stale cross-account notify cooldown.
    if not no_send:
        try:
            notified_accounts = [
                str(r.account).strip()
                for r in results
                if r.should_notify and r.meaningful and str(r.notification_text or '').strip()
            ]
            for acct0 in notified_accounts:
                if not acct0:
                    continue
                cfg_override0 = (accounts_root / acct0 / 'state' / 'config.override.json').resolve()
                if not cfg_override0.exists():
                    continue
                subprocess.run(
                    [
                        str(vpy), 'scripts/scan_scheduler.py',
                        '--config', str(cfg_override0),
                        '--state', str(state_path),
                        '--state-dir', str((run_dir / 'state').resolve()),
                        '--mark-notified',
                        '--account', str(acct0),
                    ],
                    cwd=str(base),
                )
        except Exception:
            pass

    try:
        tick_metrics['sent'] = (not no_send)
        tick_metrics['reason'] = ('sent' if (not no_send) else 'no_send')
        # shared (latest pointer)
        write_json(tick_metrics_path, tick_metrics)
        append_json_list(tick_metrics_history_path, tick_metrics)
        # per-run (replay/audit)
        write_json(tick_metrics_run_path, tick_metrics)
        append_json_list(tick_metrics_history_run_path, tick_metrics)
    except Exception:
        pass

    # Write shared last_run.json (for cron observability)
    try:
        last_run_path = (base / 'output_shared' / 'state' / 'last_run.json').resolve()
        prev = read_json(last_run_path, {})
        run_meta = {
            'last_run_utc': utc_now(),
            'sent': True,
            'channel': str(channel),
            'target': str(target),
            'account': 'merged',
            'accounts': [r.account for r in results],
            'results': [r.__dict__ for r in results],
        }
        # Keep a small history for debugging (no unbounded growth)
        hist = prev.get('history') if isinstance(prev, dict) else None
        if not isinstance(hist, list):
            hist = []
        hist.append(run_meta)
        hist = hist[-20:]
        write_json(last_run_path, {
            **(prev if isinstance(prev, dict) else {}),
            **run_meta,
            'history': hist,
        })
    except Exception:
        pass

    try:
        runlog.event('run_end', 'ok', data=_safe_runlog_data({'sent': (not no_send), 'accounts': [r.account for r in results]}))
    except Exception:
        pass

    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:
        try:
            base = Path(__file__).resolve().parents[1]
            RunLogger(base, run_id=_CURRENT_RUN_ID).event(
                'run_error',
                'error',
                error_code=(getattr(e, 'error_code', None) or type(e).__name__),
                message=str(e),
            )
        except Exception:
            pass
        raise
