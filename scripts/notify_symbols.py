#!/usr/bin/env python3
"""Symbols notification builder.

This is the same logic as the previous notify_watchlist.py, renamed for clarity.
"""

from __future__ import annotations

import argparse
import json
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


def _format_alert_line(line: str) -> str:
    raw = line.strip()
    if raw.startswith('- '):
        raw = raw[2:]
    parts = [p.strip() for p in raw.split('|')]
    if len(parts) < 3:
        return line

    symbol = parts[0]
    strategy = parts[1]
    contract = parts[2]

    annual = next((p for p in parts if p.startswith('年化')), '')
    income = next((p for p in parts if p.startswith('净收入')), '')
    dte = next((p for p in parts if p.startswith('DTE')), '')

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

    # For notification we intentionally do not surface bid/ask/delta/risk here.
    bid_val = None
    ask_val = None

    extras: dict[str, str] = {}
    comment = ''
    for p in parts[8:]:
        if p.startswith('通过准入') or p.startswith('已通过准入') or p.startswith('当前') or p.startswith('所需'):
            comment = p
            continue
        if ' ' in p:
            k, v = p.split(' ', 1)
            extras[k.strip()] = v.strip()

    if strategy == 'sell_put':
        cash_req_cny = extras.get('cash_req_cny', '')

        # Header line (more scannable)
        price_val = mid.split(' ', 1)[1] if mid and ' ' in mid else 'mid'
        ccy_val = ccy.split(' ', 1)[1] if ccy and ' ' in ccy else ''
        price_tag = f"卖价 {price_val} ({ccy_val})" if ccy_val else f"卖价 {price_val}"

        sug = _suggest_sell_price_tag(mid, bid_val, ask_val)
        sug_tag = f" | {sug}" if sug else ""

        # Move annual/net/dte to the end, keep only one separator
        meta = " | ".join([x for x in [annual, income_int_tag(income), dte] if x])
        line1 = f"{symbol} 卖Put {contract} | {price_tag}{sug_tag}" + (f" | {meta}" if meta else "")

        # Line 2 (cash): always base currency (CNY)
        line2 = ''
        if cash_req_cny:
            line2 = f"担保占用: {cash_req_cny} (CNY)"

        out = [line1]
        if line2:
            out.append(line2)
        # user preference: omit comment line
        return "\n".join(out)

    if strategy == 'sell_call':
        cover = extras.get('cover', '')
        shares = extras.get('shares', '')

        price_val = mid.split(' ', 1)[1] if mid and ' ' in mid else 'mid'
        ccy_val = ccy.split(' ', 1)[1] if ccy and ' ' in ccy else ''
        price_tag = f"卖价 {price_val} ({ccy_val})" if ccy_val else f"卖价 {price_val}"

        sug = _suggest_sell_price_tag(mid, bid_val, ask_val)
        sug_tag = f" | {sug}" if sug else ""

        meta = " | ".join([x for x in [annual, income_int_tag(income), dte] if x])
        line1 = f"{symbol} 卖Call {contract} | {price_tag}{sug_tag}" + (f" | {meta}" if meta else "")

        # Coverage line: make it more human
        line2 = ''
        if cover or shares:
            # keep original shares string (it already encodes locked shares like 160(-0))
            line2 = f"覆盖: {cover or '-'} 张 | shares {shares or '-'}"

        out = [line1]
        if line2:
            out.append(line2)
        # user preference: omit comment line
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


def build_notification(changes_text: str, alerts_text: str, fx_info: dict | None = None) -> str:
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
                block = _format_alert_line(x).strip()
                if not block:
                    continue
                lines.append(block)
                lines.append('')

        if groups['sell_put']:
            emit_plain('Put', groups['sell_put'])
        if groups['sell_call']:
            emit_plain('Call', groups['sell_call'])
        if groups['other']:
            emit_plain('Other', groups['other'])

    if significant_changes:
        lines.append('变化')
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

    notification = build_notification(changes_text, alerts_text, fx_info=fx_info)
    output_path.write_text(notification, encoding='utf-8')
    # When changes_input is /dev/null (scheduled fast mode), suppress stdout.
    if str(changes_path) != '/dev/null':
        print(notification)
        print(f'[DONE] notification -> {output_path}')


if __name__ == '__main__':
    main()
