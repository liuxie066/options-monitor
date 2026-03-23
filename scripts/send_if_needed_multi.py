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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def build_merged_message(results: list[AccountResult]) -> str:
    now = utc_now()
    lines: list[str] = []
    lines.append(f"options-monitor 合并提醒")
    lines.append(f"UTC: {now}")
    lines.append('')

    any_content = False
    cash_footer: dict[str, str] = {}

    for r in results:
        if not (r.should_notify and r.meaningful and r.notification_text.strip()):
            continue
        any_content = True

        # Extract and remove any per-notification cash footer (we append both accounts at the very end)
        txt_lines = r.notification_text.strip().splitlines()
        kept: list[str] = []
        in_cash = False
        for ln in txt_lines:
            if ln.strip() == '现金结余:':
                in_cash = True
                continue
            if in_cash:
                # expected: "LX账户 base free(CNY): ¥..."
                if '账户 base free(CNY):' in ln:
                    try:
                        acct2, rest = ln.split('账户', 1)
                        cash_footer[acct2.strip().upper()] = '账户' + rest
                    except Exception:
                        pass
                continue
            kept.append(ln)

        # Count Put/Call blocks for this account (for quick sanity check; sections may be omitted when empty)
        put_n = sum(1 for ln in kept if ' 卖Put ' in ln)
        call_n = sum(1 for ln in kept if ' 卖Call ' in ln)

        lines.append(f"[{r.account.upper()}]（Put {put_n} / Call {call_n}）")
        lines.append(annotate_notification(r.account, '\n'.join(kept).strip() + '\n').strip())
        lines.append('')

    if not any_content:
        return ''

    # Append cash footer at the end
    if cash_footer:
        lines.append('现金结余:')
        # Keep deterministic order
        for acct in ['LX', 'SY']:
            if acct in cash_footer:
                lines.append(f"{acct}{cash_footer[acct]}")
        lines.append('')

    return '\n'.join(lines).strip() + '\n'


def main():
    ap = argparse.ArgumentParser(description='Multi-account tick with merged notification')
    ap.add_argument('--config', default='config.json')
    ap.add_argument('--accounts', nargs='+', required=True)
    ap.add_argument('--default-account', default='lx')
    args = ap.parse_args()

    base = Path(__file__).resolve().parents[1]
    vpy = base / '.venv' / 'bin' / 'python'

    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = (base / cfg_path).resolve()
    base_cfg = json.loads(cfg_path.read_text(encoding='utf-8'))

    # Ensure output/accounts layout
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

    for acct in args.accounts:
        acct = str(acct).strip()
        if not acct:
            continue

        acct_out = accounts_root / acct
        ensure_account_output_dir(acct_out)

        # Switch ./output -> this account
        atomic_symlink(out_link, acct_out)

        # Write per-account config override (portfolio.account)
        cfg = json.loads(json.dumps(base_cfg))
        cfg.setdefault('portfolio', {})
        cfg['portfolio']['account'] = acct
        cfg_override = acct_out / 'state' / 'config.override.json'
        cfg_override.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

        state_path = acct_out / 'state' / 'scheduler_state.json'
        notif_path = acct_out / 'reports' / 'symbols_notification.txt'

        # 1) scheduler decision
        sch = subprocess.run(
            [str(vpy), 'scripts/scan_scheduler.py', '--config', str(cfg_override), '--state', str(state_path), '--jsonl'],
            cwd=str(base),
            capture_output=True,
            text=True,
        )
        if sch.returncode != 0:
            results.append(AccountResult(acct, False, False, False, f"scheduler error: {(sch.stderr or sch.stdout).strip()}", ''))
            continue

        decision = json.loads((sch.stdout or '').strip())
        should_run = bool(decision.get('should_run_scan'))
        should_notify = bool(decision.get('should_notify'))
        reason = str(decision.get('reason') or '')

        if not should_run:
            results.append(AccountResult(acct, False, should_notify, False, reason, ''))
            continue

        # 2) pipeline
        pipe = subprocess.run([str(vpy), 'scripts/run_pipeline.py', '--config', str(cfg_override)], cwd=str(base))
        if pipe.returncode != 0:
            results.append(AccountResult(acct, True, should_notify, False, 'pipeline failed', ''))
            continue

        text = notif_path.read_text(encoding='utf-8', errors='replace').strip() if notif_path.exists() else ''

        # Append compact auto-close summary (only when applied>0 or errors>0)
        auto_close_path = acct_out / 'reports' / 'auto_close_summary.txt'
        auto_close_text = auto_close_path.read_text(encoding='utf-8', errors='replace').strip() if auto_close_path.exists() else ''
        auto_close_flat = flatten_auto_close_summary(auto_close_text, always_show=False)
        if auto_close_flat:
            text = (text.strip() + '\n\n' + auto_close_flat.strip()).strip()

        meaningful = bool(text) and (text != '今日无需要主动提醒的内容。')
        results.append(AccountResult(acct, True, should_notify, meaningful, reason, text))

    merged = build_merged_message(results)
    if not merged:
        return 0

    # Send ONCE
    channel = (base_cfg.get('notifications') or {}).get('channel') or 'feishu'
    target = (base_cfg.get('notifications') or {}).get('target')
    if not target:
        raise SystemExit('[CONFIG_ERROR] notifications.target is required')

    send = subprocess.run(
        ['openclaw', 'message', 'send', '--channel', str(channel), '--target', str(target), '--message', merged, '--json'],
        cwd=str(base),
        capture_output=True,
        text=True,
    )
    if send.returncode != 0:
        raise SystemExit(send.returncode)

    # Mark notified for accounts that were included
    for r in results:
        if r.should_notify and r.meaningful and r.notification_text.strip():
            acct_out = accounts_root / r.account
            state_path = acct_out / 'state' / 'scheduler_state.json'
            cfg_override = acct_out / 'state' / 'config.override.json'
            subprocess.run(
                [str(vpy), 'scripts/scan_scheduler.py', '--config', str(cfg_override), '--state', str(state_path), '--mark-notified'],
                cwd=str(base),
            )

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
