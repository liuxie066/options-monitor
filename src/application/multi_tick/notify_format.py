from __future__ import annotations

import re

from .misc import (
    AccountResult,
    COVER_RE,
    CNY_RE,
)


def is_high_priority_notification(text: str) -> bool:
    return bool(re.search(r"(?m)^重点:\s*$", text or ""))


OPTIMIZER_SWITCH_LABEL = "强烈建议平仓换仓"
OPTIMIZER_CLOSE_LABEL = "建议平仓"
OPTIMIZER_SWITCH_TAG = " 🔄"
OPTIMIZER_CLOSE_TAG = " ⚠️"


def _highlight_optimizer_lines(text: str) -> str:
    if not text:
        return text
    out_lines: list[str] = []
    for ln in text.splitlines():
        stripped = ln.rstrip()
        if OPTIMIZER_SWITCH_LABEL in stripped and not stripped.endswith(OPTIMIZER_SWITCH_TAG):
            out_lines.append(stripped + OPTIMIZER_SWITCH_TAG)
        elif (
            OPTIMIZER_CLOSE_LABEL in stripped
            and OPTIMIZER_SWITCH_LABEL not in stripped
            and not stripped.endswith(OPTIMIZER_CLOSE_TAG)
        ):
            out_lines.append(stripped + OPTIMIZER_CLOSE_TAG)
        else:
            out_lines.append(ln)
    return "\n".join(out_lines)


def count_optimizer_actions(text: str) -> tuple[int, int]:
    if not text:
        return (0, 0)
    switch_n = 0
    close_n = 0
    for ln in text.splitlines():
        if OPTIMIZER_SWITCH_LABEL in ln:
            switch_n += 1
        elif OPTIMIZER_CLOSE_LABEL in ln:
            close_n += 1
    return (switch_n, close_n)


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
        if in_put and s.startswith('担保') and ('余量' in s):
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

        normalized = s
        if normalized.startswith('> '):
            normalized = normalized[2:]
        out.append(normalized)

    return '\n'.join(out).strip() + '\n'


def build_account_message(
    result: AccountResult,
    *,
    now_bj: str,
    cash_footer_lines: list[str] | None = None,
) -> str:
    if not (result.should_notify and result.notification_text.strip()):
        return ''

    kept = result.notification_text.strip().splitlines()
    put_n = sum(1 for ln in kept if ' 卖Put ' in ln)
    call_n = sum(1 for ln in kept if ' 卖Call ' in ln)
    enhancement_n = sum(1 for ln in kept if ' 收益增强 ' in ln)
    switch_n, close_n = count_optimizer_actions(result.notification_text)
    acct = str(result.account).strip().lower()

    lines: list[str] = []
    lines.append("# 📊 Options Monitor")
    lines.append(f"## 账户提醒（{acct}）")
    lines.append('')
    lines.append(f"北京时间 {now_bj}")
    lines.append('')
    lines.append(f"### 账户 {acct} · 本轮候选")
    counts_line = f"- Put {put_n} / Call {call_n}"
    if enhancement_n > 0:
        counts_line += f" / Enhance {enhancement_n}"
    if switch_n > 0 or close_n > 0:
        counts_line += f" / 优化器 换仓{switch_n} 平仓{close_n}"
    lines.append(counts_line)
    lines.append('')
    body = annotate_notification(result.account, '\n'.join(kept).strip() + '\n').strip()
    body = _highlight_optimizer_lines(body)
    lines.append(body)
    lines.append('')

    footer_lines = cash_footer_lines or []
    if footer_lines:
        lines.extend(list(footer_lines))
        lines.append('')

    return '\n'.join(lines).strip() + '\n'


SECTION_DIVIDER = "──────────────"


def build_account_message_compact(
    result: AccountResult,
    *,
    now_bj: str,
    cash_footer_lines: list[str] | None = None,
) -> str:
    if not (result.should_notify and result.notification_text.strip()):
        return ''

    text = result.notification_text.strip()
    put_n = sum(1 for ln in text.splitlines() if ' 卖Put ' in ln)
    call_n = sum(1 for ln in text.splitlines() if ' 卖Call ' in ln)
    enhancement_n = sum(1 for ln in text.splitlines() if ' 收益增强 ' in ln)
    switch_n, close_n = count_optimizer_actions(text)
    acct = str(result.account).strip().lower()

    lines: list[str] = []
    lines.append("# 📊 Options Monitor")
    lines.append(f"## 账户提醒（{acct}）")
    lines.append('')
    lines.append(f"⏰ 北京时间 {now_bj}")
    lines.append('')
    lines.append("📋 本轮概览")
    overview_parts = [f"Put {put_n}", f"Call {call_n}"]
    if enhancement_n > 0:
        overview_parts.append(f"增强 {enhancement_n}")
    lines.append(f"  {' · '.join(overview_parts)}")
    if switch_n > 0 or close_n > 0:
        lines.append(f"  🔴 优化器 换仓 {switch_n} · 平仓 {close_n}")
    lines.append('')
    lines.append(SECTION_DIVIDER)
    lines.append('')

    body = annotate_notification(result.account, text + '\n').strip()
    body = _highlight_optimizer_lines(body)
    lines.append(body)
    lines.append('')
    lines.append(SECTION_DIVIDER)
    lines.append('')

    footer_lines = cash_footer_lines or []
    if footer_lines:
        has_emoji_header = any('💰' in str(ln) for ln in footer_lines[:1])
        if not has_emoji_header:
            lines.append("💰 资金概览")
        for ln in footer_lines:
            lines.append(f"  {ln}")
        lines.append('')

    return '\n'.join(lines).strip() + '\n'
