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


def _is_optimizer_detail_line(line: str) -> bool:
    return str(line or "").strip().startswith("- 优化器:")


def _next_nonblank_line(lines: list[str], idx: int) -> str:
    for item in lines[idx + 1:]:
        if str(item or "").strip():
            return str(item)
    return ""


def _is_optimizer_close_line(line: str, next_line: str) -> bool:
    stripped = str(line or "").rstrip()
    return (
        OPTIMIZER_CLOSE_LABEL in stripped
        and OPTIMIZER_SWITCH_LABEL not in stripped
        and _is_optimizer_detail_line(next_line)
    )


def _highlight_optimizer_lines(text: str) -> str:
    if not text:
        return text
    out_lines: list[str] = []
    raw_lines = text.splitlines()
    for idx, ln in enumerate(raw_lines):
        stripped = ln.rstrip()
        if OPTIMIZER_SWITCH_LABEL in stripped and not stripped.endswith(OPTIMIZER_SWITCH_TAG):
            out_lines.append(stripped + OPTIMIZER_SWITCH_TAG)
        elif _is_optimizer_close_line(stripped, _next_nonblank_line(raw_lines, idx)) and not stripped.endswith(
            OPTIMIZER_CLOSE_TAG
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
    raw_lines = text.splitlines()
    for idx, ln in enumerate(raw_lines):
        if OPTIMIZER_SWITCH_LABEL in ln:
            switch_n += 1
        elif _is_optimizer_close_line(ln, _next_nonblank_line(raw_lines, idx)):
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


def _strip_markdown_heading(line: str) -> str:
    s = str(line or "").strip()
    if s.startswith("### "):
        s = s[4:].strip()
    if s.startswith("## "):
        s = s[3:].strip()
    if s in {"Put:", f"{SELL_PUT_SECTION_LABEL}:", f"{COVERED_CALL_SECTION_LABEL}:", "Enhancement:"}:
        return s[:-1]
    return s


def _split_monitor_sections(text: str) -> tuple[list[str], list[str], list[str]]:
    candidate_lines: list[str] = []
    reject_lines: list[str] = []
    close_lines: list[str] = []
    section = "candidate"
    for raw in str(text or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("### 拒绝摘要"):
            section = "reject"
            reject_lines.append(stripped)
            continue
        if stripped.startswith("### [") and "平仓建议" in stripped:
            section = "close"
            close_lines.append(stripped)
            continue
        if stripped.startswith("### ") and section in {"reject", "close"}:
            section = "candidate"
        if section == "reject":
            reject_lines.append(line)
        elif section == "close":
            close_lines.append(line)
        else:
            candidate_lines.append(line)
    return candidate_lines, reject_lines, close_lines


def _compact_candidate_lines(lines: list[str]) -> list[str]:
    out: list[str] = []
    saw_no_candidate = False
    for raw in lines:
        s = str(raw or "").strip()
        if not s:
            if out and out[-1] != "":
                out.append("")
            continue
        if "暂无符合条件的候选" in s:
            saw_no_candidate = True
            continue
        if s.startswith("📋 本轮扫描完成"):
            continue
        out.append(_strip_markdown_heading(s))
    while out and out[-1] == "":
        out.pop()
    if not out and saw_no_candidate:
        return ["- 无符合承保条件候选"]
    return out


def _compact_reject_lines(lines: list[str]) -> list[str]:
    if not lines:
        return []
    total_line = ""
    reason_line = ""
    unavailable_line = ""
    for raw in lines:
        s = str(raw or "").strip()
        if not s or s.startswith("###"):
            continue
        body = s[2:].strip() if s.startswith("- ") else s
        if "拒绝摘要不可用" in body:
            unavailable_line = body
        elif body.startswith("主要原因："):
            reason_line = "主要过滤：" + body.removeprefix("主要原因：").strip()
        elif body.startswith("通过 ") and ("过滤" in body or "拒绝" in body):
            total_line = body.replace("；", " · ")
    if unavailable_line:
        return [f"- {unavailable_line}"]
    out: list[str] = []
    if reason_line:
        out.append(f"- {reason_line}")
    if total_line:
        out.append(f"- {total_line}")
    return out


def _is_close_action_line(line: str) -> bool:
    s = str(line or "").strip()
    if not s or s.startswith("###") or "本次无" in s or "待补数据" in s or "无法评估" in s:
        return False
    has_contract = bool(re.search(r"\b(?:Put|Call)\b", s))
    if not has_contract:
        return False
    if s[:1] in {"🔴", "🟠", "🟡", "🟢", "⚪"}:
        return True
    return s.startswith("- ") and any(token in s for token in ("平仓", "换仓", "止盈", "风险"))


def _is_close_gap_line(line: str) -> bool:
    s = str(line or "").strip()
    return s.startswith("- ") and "无法评估" in s and bool(re.search(r"\b(?:Put|Call)\b", s))


def _compact_close_gap_line(line: str) -> str:
    s = str(line or "").strip()
    s = s.replace(" · 无法评估 | ", " · ")
    s = s.replace("收益捕获平仓仅支持 open short put/call", "非 short put/call，跳过收益捕获")
    return s


def _compact_close_lines(lines: list[str], *, max_gap_items: int = 3) -> tuple[list[str], int, int]:
    if not lines:
        return ["- 无高/中优先级平仓建议"], 0, 0
    action_count = sum(1 for line in lines if _is_close_action_line(line))
    gap_lines = [str(line).strip() for line in lines if _is_close_gap_line(str(line))]
    gap_count = len(gap_lines)

    out: list[str] = []
    skip_header = True
    in_gap = False
    shown_gap = 0
    for raw in lines:
        s = str(raw or "").strip()
        if not s:
            continue
        if skip_header and s.startswith("###"):
            continue
        if "本次无 strong/medium 平仓建议" in s or "本次未生成 strong/medium 提醒" in s:
            if not action_count:
                out.append("- 无高/中优先级平仓建议")
            continue
        if s == "- 待补数据:" or s == "待补数据:":
            in_gap = True
            if gap_count:
                out.append("- 待补:")
            continue
        if in_gap:
            if _is_close_gap_line(s):
                if shown_gap < max_gap_items:
                    out.append(_compact_close_gap_line(s))
                shown_gap += 1
            continue
        out.append(s)

    if gap_count > max_gap_items:
        out.append(f"- 另有 {gap_count - max_gap_items} 条待补数据")
    if not out:
        out.append("- 无高/中优先级平仓建议")
    return out, action_count, gap_count


def _compact_cash_footer_lines(footer_lines: list[str]) -> list[str]:
    out: list[str] = []
    for raw in footer_lines:
        s = str(raw or "").strip()
        if not s:
            continue
        if "💰" in s:
            continue
        cleaned = s.replace("**", "")
        if cleaned.startswith("- "):
            cleaned = cleaned[2:].strip()
        cleaned = cleaned.replace("总现金折算 ", "总现金 ")
        cleaned = cleaned.replace("担保后可用 ", "担保后 ")
        out.append(f"- {cleaned}")
    return out


def build_account_message_compact(
    result: AccountResult,
    *,
    now_bj: str,
    cash_footer_lines: list[str] | None = None,
) -> str:
    if not (result.should_notify and result.notification_text.strip()):
        return ''

    text = result.notification_text.strip()
    body = annotate_notification(result.account, text + '\n').strip()
    body = _highlight_optimizer_lines(body)
    candidate_raw, reject_raw, close_raw = _split_monitor_sections(body)
    candidate_lines = _compact_candidate_lines(candidate_raw)
    reject_lines = _compact_reject_lines(reject_raw)
    close_lines, close_action_n, close_gap_n = _compact_close_lines(close_raw)
    put_n = sum(1 for ln in text.splitlines() if _is_sell_put_line(ln))
    call_n = sum(1 for ln in text.splitlines() if _is_covered_call_line(ln))
    enhancement_n = sum(1 for ln in text.splitlines() if _is_yield_enhancement_line(ln))
    switch_n, close_n = count_optimizer_actions(text)
    acct = str(result.account).strip().lower()

    lines: list[str] = []
    lines.append(f"# OM · {acct}")
    lines.append(f"{now_bj} BJ")
    lines.append('')
    overview_parts = [f"{SELL_PUT_SECTION_LABEL} {put_n}", f"{COVERED_CALL_SECTION_LABEL} {call_n}"]
    if enhancement_n > 0:
        overview_parts.append(f"增强 {enhancement_n}")
    overview_parts.append(f"平仓 {close_action_n}")
    if close_gap_n > 0:
        overview_parts.append(f"待补 {close_gap_n}")
    lines.append(f"状态：{' · '.join(overview_parts)}")
    if switch_n > 0 or close_n > 0:
        lines.append(f"优化器：换仓 {switch_n} · 平仓 {close_n}")
    lines.append('')

    lines.append("候选")
    if candidate_lines:
        lines.extend(candidate_lines)
    else:
        lines.append("- 无符合承保条件候选")
    if reject_lines:
        lines.extend(reject_lines)
    lines.append('')

    lines.append("持仓")
    lines.extend(close_lines)
    lines.append('')

    cash_lines = _compact_cash_footer_lines(cash_footer_lines or [])
    if cash_lines:
        lines.append("资金")
        lines.extend(cash_lines)
        lines.append('')

    return '\n'.join(lines).strip() + '\n'
