from __future__ import annotations

import re

from domain.domain.strategy_vocab import (
    STRATEGY_COVERED_CALL,
    STRATEGY_SELL_PUT,
    strategy_action_label,
    strategy_section_label,
)
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
SELL_PUT_ACTION_LABEL = strategy_action_label(STRATEGY_SELL_PUT)
SELL_PUT_SECTION_LABEL = strategy_section_label(STRATEGY_SELL_PUT)
COVERED_CALL_ACTION_LABEL = strategy_action_label(STRATEGY_COVERED_CALL)
COVERED_CALL_SECTION_LABEL = strategy_section_label(STRATEGY_COVERED_CALL)
_COMPACT_EXPIRY_RE = re.compile(r"@\s*\d{2}-\d{2}\b")
_ISO_EXPIRY_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_OPTION_STRIKE_RE = re.compile(r"\b\d+(?:\.\d+)?[PC]\b")


def _looks_like_option_candidate_line(text: str) -> bool:
    s = str(text or "").strip()
    if not s:
        return False
    return bool(_ISO_EXPIRY_RE.search(s) or _COMPACT_EXPIRY_RE.search(s) or _OPTION_STRIKE_RE.search(s))


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


def _is_covered_call_line(text: str) -> bool:
    return (
        (f" {COVERED_CALL_ACTION_LABEL} " in text or " 卖Call " in text)
        and _looks_like_option_candidate_line(text)
    )


def _is_sell_put_line(text: str) -> bool:
    return f" {SELL_PUT_ACTION_LABEL} " in text and _looks_like_option_candidate_line(text)


def _is_yield_enhancement_line(text: str) -> bool:
    return " 收益增强 " in text and _looks_like_option_candidate_line(text)


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

        if hdr in (SELL_PUT_SECTION_LABEL, f'{SELL_PUT_SECTION_LABEL}:'):
            in_put, in_call = True, False
            if out and out[-1].strip() != '':
                out.append('')
            out.append(f'{SELL_PUT_SECTION_LABEL}:')
            last_line1_idx = None
            continue

        if hdr in ('Call', 'Call:', COVERED_CALL_SECTION_LABEL, f'{COVERED_CALL_SECTION_LABEL}:'):
            in_put, in_call = False, True
            if out and out[-1].strip() != '':
                out.append('')
            out.append(f'{COVERED_CALL_SECTION_LABEL}:')
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
        if in_call and _is_covered_call_line(s):
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
    put_n = sum(1 for ln in kept if _is_sell_put_line(ln))
    call_n = sum(1 for ln in kept if _is_covered_call_line(ln))
    enhancement_n = sum(1 for ln in kept if _is_yield_enhancement_line(ln))
    switch_n, close_n = count_optimizer_actions(result.notification_text)
    acct = str(result.account).strip().lower()

    lines: list[str] = []
    lines.append("# 📊 Options Monitor")
    lines.append(f"## 账户提醒（{acct}）")
    lines.append('')
    lines.append(f"北京时间 {now_bj}")
    lines.append('')
    lines.append(f"### 账户 {acct} · 本轮候选")
    counts_line = f"- {SELL_PUT_SECTION_LABEL} {put_n} / {COVERED_CALL_SECTION_LABEL} {call_n}"
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
    put_n = sum(1 for ln in text.splitlines() if _is_sell_put_line(ln))
    call_n = sum(1 for ln in text.splitlines() if _is_covered_call_line(ln))
    enhancement_n = sum(1 for ln in text.splitlines() if _is_yield_enhancement_line(ln))
    switch_n, close_n = count_optimizer_actions(text)
    acct = str(result.account).strip().lower()

    lines: list[str] = []
    lines.append("# 📊 Options Monitor")
    lines.append(f"## 账户提醒（{acct}）")
    lines.append('')
    lines.append(f"⏰ 北京时间 {now_bj}")
    lines.append('')
    lines.append("📋 本轮概览")
    overview_parts = [f"{SELL_PUT_SECTION_LABEL} {put_n}", f"{COVERED_CALL_SECTION_LABEL} {call_n}"]
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
