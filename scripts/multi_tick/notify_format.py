from __future__ import annotations

import re

from .misc import (
    AccountResult,
    AUTO_CLOSE_APPLIED_RE,
    AUTO_CLOSE_CAND_RE,
    AUTO_CLOSE_ERR_RE,
    COVER_RE,
    CNY_RE,
)


def is_high_priority_notification(text: str) -> bool:
    return bool(re.search(r"(?m)^重点:\s*$", text or ""))


def _parse_cny(s: str) -> float | None:
    m = CNY_RE.search(s)
    if not m:
        return None
    try:
        return float(m.group('num').replace(',', ''))
    except Exception:
        return None


def annotate_notification(acct: str, text: str) -> str:
    if not text:
        return text

    lines = text.splitlines()
    out: list[str] = []

    in_put = False
    in_call = False
    last_line1_idx: int | None = None

    for ln in lines:
        s = ln.rstrip('\n')

        hdr = s.strip()

        if hdr in ('Put', 'Put:'):
            in_put, in_call = True, False
            if out and out[-1].strip() != '':
                out.append('')
            out.append('Put:')
            last_line1_idx = None
            continue

        if hdr in ('Call', 'Call:'):
            in_put, in_call = False, True
            if out and out[-1].strip() != '':
                out.append('')
            out.append('Call:')
            last_line1_idx = None
            continue

        if hdr in ('变化', '变化:'):
            in_put, in_call = False, False
            if out and out[-1].strip() != '':
                out.append('')
            out.append('变化:')
            last_line1_idx = None
            continue

        if in_put and ' 卖Put ' in s:
            if s.lstrip().startswith('- '):
                out.append(s)
            else:
                out.append('- ' + s)
            last_line1_idx = len(out) - 1
            continue
        if in_call and ' 卖Call ' in s:
            if s.lstrip().startswith('- '):
                out.append(s)
            else:
                out.append('- ' + s)
            last_line1_idx = len(out) - 1
            continue

        if in_put and s.startswith('担保') and ('加仓后余量' in s):
            headroom = _parse_cny(s)
            tag = ''
            if headroom is not None:
                tag = '【现金不足】' if headroom < 0 else '【现金支持】'
            if last_line1_idx is not None and tag:
                out[last_line1_idx] = out[last_line1_idx] + ' ' + tag
            out.append(s)
            continue

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


def flatten_auto_close_summary(text: str, *, always_show: bool = False) -> str:
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

    if err > 0 or applied > 0:
        for ln in text.splitlines():
            if ln.startswith('- '):
                lines.append(ln)
            if len(lines) >= 1 + 6:
                break

    return ('---\n' + '\n'.join(lines).strip()).strip()


def build_merged_message(
    results: list[AccountResult],
    *,
    now_bj: str,
    cash_footer_lines: list[str] | None = None,
) -> str:
    lines: list[str] = []
    lines.append("options-monitor 合并提醒")
    lines.append(f"北京时间: {now_bj}")
    lines.append('')

    any_content = False

    for r in results:
        if not (r.should_notify and r.meaningful and r.notification_text.strip()):
            continue
        any_content = True

        kept = r.notification_text.strip().splitlines()

        put_n = sum(1 for ln in kept if ' 卖Put ' in ln)
        call_n = sum(1 for ln in kept if ' 卖Call ' in ln)

        lines.append(f"[{r.account.upper()}]（Put {put_n} / Call {call_n}）")
        lines.append(annotate_notification(r.account, '\n'.join(kept).strip() + '\n').strip())
        lines.append('')

    if not any_content:
        return ''

    footer_lines = cash_footer_lines or []
    if footer_lines:
        lines.extend(list(footer_lines))
        lines.append('')

    return '\n'.join(lines).strip() + '\n'
