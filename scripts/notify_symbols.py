#!/usr/bin/env python3
"""Symbols notification builder.

This is the same logic as the previous notify_watchlist.py, renamed for clarity.

NOTE (template ownership):
- This file is the *single source of truth* for notification layout (Put/Call sections, blank lines, bullet lists).
- For multi-account merged notifications, send_if_needed_multi.py must treat per-account notification text as opaque
  and must NOT reformat individual candidates.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def _suggest_sell_price_tag(mid: str, bid: str | None, ask: str | None) -> str:
    """Return suggested order price tag.

    Keep the format aligned with the existing US-option notification style:
      "建议挂单 1.980"

    So we intentionally prefer a simple suggestion based on the mid used for return calc.
    """
    try:
        v = mid.split(' ', 1)[1] if mid and ' ' in mid else ''
        return f"建议挂单 {v}" if v else ''
    except Exception:
        return ''


def read_text(path: Path) -> str:
    if path.exists() and path.stat().st_size > 0:
        return path.read_text(encoding='utf-8').strip()
    return ''


def extract_section(text: str, heading: str) -> list[str]:
    lines = text.splitlines()
    captured: list[str] = []
    in_section = False
    for line in lines:
        if line.strip() == heading:
            in_section = True
            continue
        if in_section and line.startswith('## '):
            break
        if in_section:
            if line.strip():
                captured.append(line)
    return captured


def extract_change_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith('- ')]


_MD_LINK_RE = re.compile(r"^\[(?P<label>[^\]]+)\]\((?P<target>[^\)]+)\)$")
_MISSING_MARKERS = {"", "-", "nan", "none", "null", "n/a", "na"}


def _is_missing_value(value: str | None) -> bool:
    if value is None:
        return True
    v = str(value).strip().lower()
    return v in _MISSING_MARKERS


def _present_or_missing(value: str | None, *, reason: str) -> str:
    if _is_missing_value(value):
        return f"缺失({reason})"
    return str(value).strip()


def _value_after_prefix(token: str | None, prefix: str) -> str:
    if not token:
        return ''
    s = str(token).strip()
    if not s.startswith(prefix):
        return s
    return s[len(prefix):].strip()


def _symbol_parts(symbol: str) -> tuple[str, str]:
    """Parse symbol display/code from raw field.

    Support markdown-link style symbol tags emitted by alert_engine:
    - [腾讯](0700.HK) -> ("腾讯", "0700.HK")
    - NVDA -> ("NVDA", "NVDA")
    """
    s = (symbol or '').strip()
    m = _MD_LINK_RE.match(s)
    if m:
        label = (m.group('label') or '').strip()
        target = (m.group('target') or '').strip()
        if label and target:
            return (label, target)
    return (s, s)


def _symbol_display_name(symbol: str) -> str:
    return _symbol_parts(symbol)[0]


def _quote_url(symbol_code: str) -> str:
    return ''


def _parse_contract(contract: str) -> tuple[str, str]:
    s = (contract or '').strip()
    m = re.match(r'^(?P<exp>\d{4}-\d{2}-\d{2})\s+(?P<strike>.+)$', s)
    if not m:
        return ('-', s or '-')
    return (m.group('exp').strip(), m.group('strike').strip())


def _normalize_contract_strike(strike_token: str) -> str:
    s = (strike_token or '').strip()
    m = re.match(r'^(?P<num>\d+(?:\.\d+)?)[CP]$', s, flags=re.IGNORECASE)
    if m:
        return m.group('num')
    return s


def _infer_account_label(*paths: Path | None) -> str:
    for p in paths:
        if p is None:
            continue
        parts = list(p.parts)
        for i, token in enumerate(parts):
            if token == 'accounts' and (i + 1) < len(parts):
                acct = str(parts[i + 1]).strip()
                if acct:
                    return acct.upper()
            if token == 'output_accounts' and (i + 1) < len(parts):
                acct = str(parts[i + 1]).strip()
                if acct:
                    return acct.upper()
    return '当前账户'


def _format_alert_line(line: str, *, account_label: str = '当前账户') -> str:
    raw = line.strip()
    if raw.startswith('- '):
        raw = raw[2:]
    parts = [p.strip() for p in raw.split('|')]
    if len(parts) < 3:
        return line

    symbol_raw = parts[0]
    symbol_name, symbol_code = _symbol_parts(symbol_raw)
    strategy = parts[1]
    contract = parts[2]
    exp, strike_from_contract = _parse_contract(contract)

    annual = next((p for p in parts if p.startswith('年化')), '')
    income = next((p for p in parts if p.startswith('净收入')), '')
    dte = next((p for p in parts if p.startswith('DTE')), '')
    strike_tag = next((p for p in parts if p.startswith('Strike ')), '')
    risk_tag = next((p for p in parts if p in ('保守', '中性', '激进')), '')

    def income_int_tag(s: str) -> str:
        # input like "净收入 137.99" -> "净收 138"
        try:
            if not s:
                return ''
            x = s.replace('净收入', '').strip()
            v = float(x)
            return f"净收 {int(round(v))}"
        except Exception:
            return s
    # Mid price used for return calculation (if present)
    mid = next((p for p in parts if p.startswith('mid ')), '')
    ccy = next((p for p in parts if p.startswith('ccy ')), '')

    extras: dict[str, str] = {}
    comment = ''
    for p in parts[3:]:
        if p.startswith('通过准入') or p.startswith('已通过准入') or p.startswith('当前') or p.startswith('所需'):
            comment = p
            continue
        if ' ' in p:
            k, v = p.split(' ', 1)
            extras[k.strip()] = v.strip()

    if strategy == 'sell_put':
        cash_req_cny = extras.get('cash_req_cny', '')
        cash_req_usd = extras.get('cash_req', '')
        delta = extras.get('delta', '')
        iv = extras.get('iv', '') or extras.get('IV', '')

        price_val = mid.split(' ', 1)[1] if mid and ' ' in mid else 'mid'
        ccy_val = ccy.split(' ', 1)[1] if ccy and ' ' in ccy else ''
        premium = f"{price_val} ({ccy_val})" if ccy_val else price_val
        sug = _suggest_sell_price_tag(mid, None, None)

        margin = _present_or_missing('', reason='告警未提供cash_req_cny/cash_req')
        raw_margin = cash_req_cny if not _is_missing_value(cash_req_cny) else cash_req_usd
        if not _is_missing_value(raw_margin):
            v = str(raw_margin).strip()
            margin = v
            try:
                cleaned = ''.join(ch for ch in v if (ch.isdigit() or ch in '.-'))
                if cleaned and cleaned not in ('-', '.', '-.'):
                    n = float(cleaned)
                    if v.startswith('¥') or (not _is_missing_value(cash_req_cny) and _is_missing_value(cash_req_usd)):
                        margin = f"¥{n:,.0f} (CNY)"
                    elif '$' in v or (not _is_missing_value(cash_req_usd) and _is_missing_value(cash_req_cny)):
                        margin = f"${n:,.0f} (USD)"
            except Exception:
                margin = _present_or_missing('', reason=f'cash_req值无效:{v}')

        strike_from_tag = strike_tag.replace('Strike ', '').strip() if strike_tag else ''
        strike_val = strike_from_tag if not _is_missing_value(strike_from_tag) else _normalize_contract_strike(strike_from_contract)
        strike_show = _present_or_missing(strike_val, reason='告警未提供Strike/合约行权价')
        annual_val = _value_after_prefix(annual, '年化')
        annual_show = f"年化 {_present_or_missing(annual_val, reason='告警未提供年化')}"
        delta_show = _present_or_missing(delta, reason='告警未提供delta')
        iv_show = _present_or_missing(iv, reason='告警未提供iv')
        title = f"### [{account_label}] {symbol_name} | 到期 {exp} | 策略 卖Put"
        out = [
            title,
            f"- {symbol_name} 卖Put {contract}",
            f"- 指标: 方向=卖Put | 行权价={strike_show} | 数量=1张(默认) | 权利金={premium} | {annual_show} | {income_int_tag(income) or '净收 -'} | 保证金占用={margin} | delta={delta_show} | IV={iv_show}",
        ]
        if sug:
            out.append(f"- 建议挂单: {sug.replace('建议挂单 ', '').strip()}")
        out.append("> 次要信息")
        out.append(f"> 风险: {risk_tag or '-'}")
        out.append(f"> DTE: {dte.replace('DTE ', '').strip() if dte else '-'}")
        out.append("---")
        return "\n".join(out)

    if strategy == 'sell_call':
        cover = extras.get('cover', '')
        shares = extras.get('shares', '')
        delta = extras.get('delta', '')
        iv = extras.get('iv', '') or extras.get('IV', '')

        price_val = mid.split(' ', 1)[1] if mid and ' ' in mid else 'mid'
        ccy_val = ccy.split(' ', 1)[1] if ccy and ' ' in ccy else ''
        premium = f"{price_val} ({ccy_val})" if ccy_val else price_val
        sug = _suggest_sell_price_tag(mid, None, None)
        strike_from_tag = strike_tag.replace('Strike ', '').strip() if strike_tag else ''
        strike_val = strike_from_tag if not _is_missing_value(strike_from_tag) else _normalize_contract_strike(strike_from_contract)
        strike_show = _present_or_missing(strike_val, reason='告警未提供Strike/合约行权价')
        annual_val = _value_after_prefix(annual, '年化')
        annual_show = f"年化 {_present_or_missing(annual_val, reason='告警未提供年化')}"
        delta_show = _present_or_missing(delta, reason='告警未提供delta')
        iv_show = _present_or_missing(iv, reason='告警未提供iv')
        qty = f"{cover}张(可覆盖)" if (not _is_missing_value(cover)) else '1张(默认)'
        title = f"### [{account_label}] {symbol_name} | 到期 {exp} | 策略 卖Call"
        out = [
            title,
            f"- {symbol_name} 卖Call {contract}",
            f"- 指标: 方向=卖Call | 行权价={strike_show} | 数量={qty} | 权利金={premium} | {annual_show} | {income_int_tag(income) or '净收 -'} | 保证金占用=- | delta={delta_show} | IV={iv_show}",
        ]
        if sug:
            out.append(f"- 建议挂单: {sug.replace('建议挂单 ', '').strip()}")
        out.append("> 次要信息")
        out.append(f"> 覆盖: {_present_or_missing(cover, reason='告警未提供cover')} 张 | shares {_present_or_missing(shares, reason='告警未提供shares')}")
        out.append(f"> 风险: {risk_tag or '-'}")
        out.append(f"> DTE: {dte.replace('DTE ', '').strip() if dte else '-'}")
        out.append("---")
        return "\n".join(out)

    return raw




def _group_by_strategy(raw_lines: list[str]) -> dict[str, list[str]]:
    g = {'sell_put': [], 'sell_call': [], 'other': []}
    for ln in raw_lines:
        s = ln
        if '| sell_put |' in s:
            g['sell_put'].append(ln)
        elif '| sell_call |' in s:
            g['sell_call'].append(ln)
        else:
            g['other'].append(ln)
    return g


def build_notification(
    changes_text: str,
    alerts_text: str,
    fx_info: dict | None = None,
    *,
    account_label: str = '当前账户',
) -> str:
    """Build markdown-ish notification text.

    User preference:
    - Two-line candidates
    - Notes are not folded

    Note: Some chat clients won't render markdown; we still gain readability.
    """

    change_lines = extract_change_lines(changes_text)
    high_lines = extract_section(alerts_text, '## 高优先级')
    medium_lines = extract_section(alerts_text, '## 中优先级')
    low_lines = extract_section(alerts_text, '## 低优先级')

    lines: list[str] = []

    significant_changes = [
        line for line in change_lines
        if '无显著变化' not in line and '初始记录' not in line
    ]

    # ===== Candidates (align with existing US-option notification style) =====
    # Prefer: high > medium > low
    candidate_lines: list[str] = []
    if high_lines:
        candidate_lines = high_lines[:5]
    elif medium_lines:
        candidate_lines = medium_lines[:5]
    elif low_lines:
        candidate_lines = low_lines[:5]

    if candidate_lines:
        groups = _group_by_strategy(candidate_lines)

        def emit_plain(title: str, items: list[str]):
            if not items:
                return
            lines.append(title)
            lines.append('')  # blank line after section heading (Feishu plaintext friendly)
            for x in items:
                block = _format_alert_line(x, account_label=account_label).strip()
                if not block:
                    continue
                b_lines = block.splitlines()
                if not b_lines:
                    continue
                lines.append(b_lines[0].strip())
                for ln in b_lines[1:]:
                    s = ln.rstrip()
                    if not s.strip():
                        continue
                    lines.append(s)
                lines.append('')

        if groups['sell_put']:
            emit_plain('Put', groups['sell_put'])
        if groups['sell_call']:
            emit_plain('Call', groups['sell_call'])
        if groups['other']:
            emit_plain('Other', groups['other'])

    if significant_changes:
        lines.append('变化')
        lines.append('')
        for ln in significant_changes[:8]:
            s = ln.strip()
            if s.startswith('- '):
                s = s[2:]
            lines.append(f"- {s}")
        lines.append('')

    if not candidate_lines and not significant_changes:
        lines.append('今日无需要主动提醒的内容。')
        lines.append('')

    return '\n'.join(lines).strip() + '\n'


def main():
    parser = argparse.ArgumentParser(description='Build symbols notification text from alerts and changes')
    parser.add_argument('--alerts-input', default='output/reports/symbols_alerts.txt')
    parser.add_argument('--changes-input', default='output/reports/symbols_changes.txt')
    parser.add_argument('--output', default='output/reports/symbols_notification.txt')
    parser.add_argument('--state-dir', default=None, help='[optional] state dir for rate_cache.json')
    args = parser.parse_args()

    base = Path(__file__).resolve().parents[1]
    alerts_path = base / args.alerts_input
    changes_path = base / args.changes_input
    output_path = base / args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)

    alerts_text = read_text(alerts_path)
    changes_text = read_text(changes_path)
    account_label = _infer_account_label(output_path, alerts_path)

    fx_info = None
    try:
        if args.state_dir:
            sd = Path(args.state_dir)
            if not sd.is_absolute():
                sd = (base / sd).resolve()
            rate_path = (sd / 'rate_cache.json').resolve()
        else:
            rate_path = (base / 'output' / 'state' / 'rate_cache.json').resolve()

        if rate_path.exists() and rate_path.stat().st_size > 0:
            data = json.loads(rate_path.read_text(encoding='utf-8'))
            rates = (data.get('rates') or {}) if isinstance(data, dict) else {}
            fx_info = {'USDCNY': rates.get('USDCNY'), 'timestamp': data.get('timestamp')}
    except Exception:
        fx_info = None

    notification = build_notification(changes_text, alerts_text, fx_info=fx_info, account_label=account_label)
    output_path.write_text(notification, encoding='utf-8')
    # When changes_input is /dev/null (scheduled fast mode), suppress stdout.
    if str(changes_path) != '/dev/null':
        print(notification)
        print(f'[DONE] notification -> {output_path}')


if __name__ == '__main__':
    main()
