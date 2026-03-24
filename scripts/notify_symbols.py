#!/usr/bin/env python3
"""Symbols notification builder.

This is the same logic as the previous notify_watchlist.py, renamed for clarity.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _suggest_sell_price_tag(mid: str, bid: str | None, ask: str | None) -> str:
    """Return human-readable suggested order price tag.

    Example: "建议挂单 bid 1.200 / fair 1.260".

    We assume bid/ask are numeric strings; if missing, fallback to mid.
    """
    try:
        if bid is None or ask is None:
            # fallback
            v = mid.split(' ', 1)[1] if mid and ' ' in mid else ''
            if v:
                return f"建议挂单 {v}"
            return ''
        b = float(str(bid).strip())
        a = float(str(ask).strip())
        if a < b:
            b, a = a, b
        spread = max(0.0, a - b)
        fast = b
        fair = b + 0.3 * spread
        return f"建议挂单 bid {fast:.3f} / fair {fair:.3f}"
    except Exception:
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
    # mid price used for return calculation (if present)
    mid = next((p for p in parts if p.startswith('mid ')), '')
    ccy = next((p for p in parts if p.startswith('ccy ')), '')
    delta = next((p for p in parts if p.startswith('delta ')), '')
    risk = parts[7] if len(parts) >= 8 else ''

    bid = next((p for p in parts if p.startswith('bid ')), None)
    ask = next((p for p in parts if p.startswith('ask ')), None)
    bid_val = bid.split(' ', 1)[1] if bid and ' ' in bid else None
    ask_val = ask.split(' ', 1)[1] if ask and ' ' in ask else None

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
        cash_req = extras.get('cash_req', '')
        cash_req_cny = extras.get('cash_req_cny', '')
        # Only show cash required (no headroom calc)
        cash_req_only = extras.get('cash_req_cny', '') or extras.get('cash_req', '')

        # Line 1 (compact)
        # Include suggested sell price (mid)
        price_val = mid.split(' ', 1)[1] if mid and ' ' in mid else 'mid'
        ccy_val = ccy.split(' ', 1)[1] if ccy and ' ' in ccy else ''
        price_tag = f"卖价 {price_val} ({ccy_val})" if ccy_val else f"卖价 {price_val}"
        sug = _suggest_sell_price_tag(mid, bid_val, ask_val)
        sug_tag = f" | {sug}" if sug else ""
        delta_tag = f" | Δ(est) {delta.split(' ',1)[1]}" if delta and ' ' in delta else ''
        line1 = f"{symbol} 卖Put {contract} | {price_tag}{sug_tag}{delta_tag} | {annual} | {income_int_tag(income)} | {dte}"

        # Line 2 (cash)
        req = cash_req_cny or cash_req or cash_req_only or ''
        line2 = ''
        if req:
            # Append explicit currency tag (CNY/USD)
            cur = 'CNY' if (cash_req_cny or '¥' in str(req)) else 'USD'
            line2 = f"占用担保 {req} ({cur})"

        out = [line1]
        if line2:
            out.append(line2)
        if comment:
            out.append(f"备注: {comment}")
        return "\n".join(out)

    if strategy == 'sell_call':
        cover = extras.get('cover', '')
        shares = extras.get('shares', '')

        # Include suggested sell price (mid)
        price_val = mid.split(' ', 1)[1] if mid and ' ' in mid else 'mid'
        ccy_val = ccy.split(' ', 1)[1] if ccy and ' ' in ccy else ''
        price_tag = f"卖价 {price_val} ({ccy_val})" if ccy_val else f"卖价 {price_val}"
        sug = _suggest_sell_price_tag(mid, bid_val, ask_val)
        sug_tag = f" | {sug}" if sug else ""
        delta_tag = f" | Δ(est) {delta.split(' ',1)[1]}" if delta and ' ' in delta else ''
        line1 = f"{symbol} 卖Call {contract} | {price_tag}{sug_tag}{delta_tag} | {annual} | {income_int_tag(income)} | {dte}"
        line2 = ''
        if cover or shares:
            line2 = f"覆盖 cover {cover or '-'} | shares {shares or '-'}"

        out = [line1]
        if line2:
            out.append(line2)
        if comment:
            out.append(f"备注: {comment}")
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
    change_lines = extract_change_lines(changes_text)
    high_lines = extract_section(alerts_text, '## 高优先级')
    medium_lines = extract_section(alerts_text, '## 中优先级')

    lines: list[str] = []

    # Header: keep minimal (cash summary appended later by append_cash_summary.py)

    significant_changes = [
        line for line in change_lines
        if '无显著变化' not in line and '初始记录' not in line
    ]

    def emit_grouped(title: str, raw: list[str]):
        if not raw:
            return
        lines.append(f"{title}:")
        groups = _group_by_strategy(raw)

        def emit_items(items: list[str]):
            for x in items:
                block = _format_alert_line(x).strip()
                lines.append(block)
                # Add a blank line only for multi-line blocks (e.g., Put/Call candidates).
                if '\n' in block:
                    lines.append('')

        # Put first, then Call
        if groups['sell_put']:
            lines.append('Put:')
            emit_items(groups['sell_put'])
        if groups['sell_call']:
            lines.append('Call:')
            emit_items(groups['sell_call'])
        if groups['other']:
            emit_items(groups['other'])

        # Ensure a trailing blank line between sections
        if lines and lines[-1] != '':
            lines.append('')

    # Always show candidates first (per-account & non-mergeable, especially covered call),
    # then show changes as supplementary info.
    if high_lines:
        emit_grouped('重点', high_lines[:5])
    elif medium_lines:
        emit_grouped('观察', medium_lines[:5])

    if significant_changes:
        emit_grouped('变化', significant_changes[:8])

    if not (high_lines or medium_lines or significant_changes):
        lines.append('今日无需要主动提醒的内容。')
        lines.append('')

    return '\n'.join(lines).strip() + '\n'


def main():
    parser = argparse.ArgumentParser(description='Build symbols notification text from alerts and changes')
    parser.add_argument('--alerts-input', default='output/reports/symbols_alerts.txt')
    parser.add_argument('--changes-input', default='output/reports/symbols_changes.txt')
    parser.add_argument('--output', default='output/reports/symbols_notification.txt')
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
        rate_path = base / 'output' / 'state' / 'rate_cache.json'
        if rate_path.exists() and rate_path.stat().st_size > 0:
            data = json.loads(rate_path.read_text(encoding='utf-8'))
            rates = (data.get('rates') or {}) if isinstance(data, dict) else {}
            fx_info = {'USDCNY': rates.get('USDCNY'), 'timestamp': data.get('timestamp')}
    except Exception:
        fx_info = None

    notification = build_notification(changes_text, alerts_text, fx_info=fx_info)
    output_path.write_text(notification, encoding='utf-8')
    print(notification)
    print(f'[DONE] notification -> {output_path}')


if __name__ == '__main__':
    main()
