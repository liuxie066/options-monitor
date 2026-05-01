#!/usr/bin/env python3
"""Symbols notification builder.

This is the same logic as the previous notify_watchlist.py, renamed for clarity.

NOTE (template ownership):
- This file is the *single source of truth* for notification layout (Put/Call sections, blank lines, bullet lists).
- For multi-account per-account notifications, send_if_needed_multi.py must treat account notification text as opaque
  and must NOT reformat individual candidates.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

repo_base = Path(__file__).resolve().parents[1]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

from domain.domain import build_no_candidate_notification_text
from scripts.exchange_rates import load_exchange_rate_info
from scripts.alert_rules import (
    SELL_CALL_NOTIFICATION_MEDIUM,
    SELL_PUT_NOTIFICATION_HIGH,
)


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


def _present_money_or_zero(value: str | None, *, reason: str) -> str:
    if _is_missing_value(value):
        return f"缺失({reason})"
    v = str(value).strip()
    if v in {"0", "0.0", "0.00"}:
        return "0"
    return v


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
                    return acct.lower()
            if token == 'output_accounts' and (i + 1) < len(parts):
                acct = str(parts[i + 1]).strip()
                if acct:
                    return acct.lower()
    return '当前账户'


def _normalize_account_label(account_label: str) -> str:
    s = str(account_label or '').strip()
    if s and s != '当前账户':
        return s.lower()
    return s or '当前账户'


def _income_int_tag(token: str) -> str:
    try:
        if not token:
            return ''
        x = token.replace('净收入', '').strip()
        v = float(x)
        return f"净收 {int(round(v))}"
    except Exception:
        return token


@dataclass(frozen=True)
class ParsedAlertLine:
    raw: str
    symbol_name: str
    strategy: str
    contract: str
    extras: dict[str, str]
    comment: str
    annual_show: str
    income_show: str
    dte_show: str
    strike_show: str
    risk_tag: str
    premium: str
    suggestion: str


def _parse_alert_line(raw_line: str) -> ParsedAlertLine | None:
    raw = raw_line.strip()
    if raw.startswith('- '):
        raw = raw[2:]
    parts = [p.strip() for p in raw.split('|')]
    if len(parts) < 3:
        return None

    symbol_name, _symbol_code = _symbol_parts(parts[0])
    strategy = parts[1]
    contract = parts[2]
    _expiration, strike_from_contract = _parse_contract(contract)

    extras: dict[str, str] = {}
    comment = ''
    for p in parts[3:]:
        if p.startswith('通过准入') or p.startswith('已通过准入') or p.startswith('当前') or p.startswith('所需'):
            comment = p
            continue
        if ' ' in p:
            k, v = p.split(' ', 1)
            extras[k.strip()] = v.strip()

    annual = next((p for p in parts if p.startswith('年化')), '')
    income = next((p for p in parts if p.startswith('净收入')), '')
    dte = next((p for p in parts if p.startswith('DTE')), '')
    strike_tag = next((p for p in parts if p.startswith('Strike ')), '')
    risk_tag = next((p for p in parts if p in ('保守', '中性', '激进')), '')
    mid = next((p for p in parts if p.startswith('mid ')), '')
    ccy = next((p for p in parts if p.startswith('ccy ')), '')

    price_val = mid.split(' ', 1)[1] if mid and ' ' in mid else 'mid'
    ccy_val = ccy.split(' ', 1)[1] if ccy and ' ' in ccy else ''
    premium = f"{price_val} ({ccy_val})" if ccy_val else price_val
    suggestion = _suggest_sell_price_tag(mid, None, None)
    strike_from_tag = strike_tag.replace('Strike ', '').strip() if strike_tag else ''
    strike_val = strike_from_tag if not _is_missing_value(strike_from_tag) else _normalize_contract_strike(strike_from_contract)
    annual_val = _value_after_prefix(annual, '年化')

    return ParsedAlertLine(
        raw=raw,
        symbol_name=symbol_name,
        strategy=strategy,
        contract=contract,
        extras=extras,
        comment=comment,
        annual_show=f"年化 {_present_or_missing(annual_val, reason='告警未提供年化')}",
        income_show=_income_int_tag(income) or '净收 -',
        dte_show=dte.replace('DTE ', '').strip() if dte else '-',
        strike_show=_present_or_missing(strike_val, reason='告警未提供Strike/合约行权价'),
        risk_tag=risk_tag or '-',
        premium=premium,
        suggestion=suggestion.replace('建议挂单 ', '').strip() if suggestion else '',
    )


def _build_notification_block(
    *,
    account_label: str,
    symbol_name: str,
    action_label: str,
    contract: str,
    income_line: str,
    contract_line: str,
    risk_line: str,
    detail_line: str,
    note: str,
    suggestion: str = '',
    extra_detail_line: str = '',
) -> str:
    out = [
        f"### [{account_label}] {symbol_name} · {action_label}",
        f"- {symbol_name} {action_label} {contract}",
        income_line,
        contract_line,
        risk_line,
        detail_line,
    ]
    if extra_detail_line:
        out.append(extra_detail_line)
    if suggestion:
        out.append(f"- 操作: 建议挂单={suggestion}")
    out.append(f"- 备注: {note}")
    out.append("---")
    return "\n".join(out)


def _parse_shares_summary(shares: str) -> tuple[str, str, str]:
    shares_total = ''
    shares_locked = ''
    if not _is_missing_value(shares):
        m = re.match(r'^(?P<total>\d+)\(-(?P<locked>\d+)\)$', str(shares).strip())
        if m:
            shares_total = m.group('total')
            shares_locked = m.group('locked')
        else:
            shares_total = str(shares).strip()
    shares_available = ''
    try:
        if not _is_missing_value(shares_total) and not _is_missing_value(shares_locked):
            shares_available = str(max(0, int(shares_total) - int(shares_locked)))
    except Exception:
        shares_available = ''
    return shares_total, shares_locked, shares_available


def _format_margin(*, cash_req_cny: str, cash_req_usd: str) -> str:
    margin = _present_or_missing('', reason='告警未提供cash_req_cny/cash_req')
    raw_margin = cash_req_cny if not _is_missing_value(cash_req_cny) else cash_req_usd
    if _is_missing_value(raw_margin):
        return margin
    v = str(raw_margin).strip()
    margin = v
    try:
        cleaned = ''.join(ch for ch in v if (ch.isdigit() or ch in '.-'))
        if cleaned and cleaned not in ('-', '.', '-.'):
            n = float(cleaned)
            if v.startswith('¥') or (not _is_missing_value(cash_req_cny) and _is_missing_value(cash_req_usd)):
                return f"¥{n:,.0f} (CNY)"
            if '$' in v or (not _is_missing_value(cash_req_usd) and _is_missing_value(cash_req_cny)):
                return f"${n:,.0f} (USD)"
    except Exception:
        return _present_or_missing('', reason=f'cash_req值无效:{v}')
    return margin


def _format_alert_line(line: str, *, account_label: str = '当前账户') -> str:
    account_label = _normalize_account_label(account_label)
    parsed = _parse_alert_line(line)
    if parsed is None:
        return line

    if parsed.strategy == 'sell_put':
        cash_req_cny = parsed.extras.get('cash_req_cny', '')
        cash_req_usd = parsed.extras.get('cash_req', '')
        cash_used_sym_cny = parsed.extras.get('cash_used_sym_cny', '')
        cash_used_sym = parsed.extras.get('cash_used_sym', '')
        delta = parsed.extras.get('delta', '')
        iv = parsed.extras.get('iv', '') or parsed.extras.get('IV', '')
        delta_show = _present_or_missing(delta, reason='告警未提供delta')
        iv_show = _present_or_missing(iv, reason='告警未提供iv')
        note = parsed.comment or SELL_PUT_NOTIFICATION_HIGH
        used_symbol = cash_used_sym_cny if not _is_missing_value(cash_used_sym_cny) else cash_used_sym
        extra_detail_line = ''
        if not _is_missing_value(used_symbol):
            extra_detail_line = (
                f"- 已持仓: 同标的Sell Put占用="
                f"{_present_money_or_zero(used_symbol, reason='告警未提供cash_used_sym')}"
            )
        return _build_notification_block(
            account_label=account_label,
            symbol_name=parsed.symbol_name,
            action_label='卖Put',
            contract=parsed.contract,
            income_line=f"- 收益: 权利金={parsed.premium} | {parsed.annual_show} | {parsed.income_show}",
            contract_line=f"- 合约: 行权价={parsed.strike_show} | 数量=1张(默认) | DTE={parsed.dte_show}",
            risk_line=f"- 风控: 风险={parsed.risk_tag} | delta={delta_show} | IV={iv_show}",
            detail_line=f"- 资金: 保证金占用={_format_margin(cash_req_cny=cash_req_cny, cash_req_usd=cash_req_usd)}",
            extra_detail_line=extra_detail_line,
            note=note,
            suggestion=parsed.suggestion,
        )

    if parsed.strategy == 'sell_call':
        cover = parsed.extras.get('cover', '')
        shares = parsed.extras.get('shares', '')
        delta = parsed.extras.get('delta', '')
        iv = parsed.extras.get('iv', '') or parsed.extras.get('IV', '')
        delta_show = _present_or_missing(delta, reason='告警未提供delta')
        iv_show = _present_or_missing(iv, reason='告警未提供iv')
        qty = f"{cover}张(可覆盖)" if (not _is_missing_value(cover)) else '1张(默认)'
        note = parsed.comment or SELL_CALL_NOTIFICATION_MEDIUM
        shares_total, shares_locked, shares_available = _parse_shares_summary(shares)
        return _build_notification_block(
            account_label=account_label,
            symbol_name=parsed.symbol_name,
            action_label='卖Call',
            contract=parsed.contract,
            income_line=f"- 收益: 权利金={parsed.premium} | {parsed.annual_show} | {parsed.income_show}",
            contract_line=f"- 合约: 行权价={parsed.strike_show} | 数量={qty} | DTE={parsed.dte_show}",
            risk_line=f"- 风控: 风险={parsed.risk_tag} | delta={delta_show} | IV={iv_show}",
            detail_line=(
                f"- 持仓: 总股数={_present_or_missing(shares_total, reason='告警未提供shares')} | "
                f"已占用={_present_or_missing(shares_locked, reason='告警未提供shares')} | "
                f"可用={_present_or_missing(shares_available, reason='告警未提供shares')} | "
                f"可覆盖={_present_or_missing(cover, reason='告警未提供cover')}张"
            ),
            note=note,
            suggestion=parsed.suggestion,
        )

    return parsed.raw




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
    exchange_rate_info: dict | None = None,
    *,
    account_label: str = '当前账户',
) -> str:
    """Build markdown-ish notification text.

    User preference:
    - Two-line candidates
    - Notes are not folded

    Note: Some chat clients won't render markdown; we still gain readability.
    """

    high_lines = extract_section(alerts_text, '## 高优先级')
    medium_lines = extract_section(alerts_text, '## 中优先级')
    low_lines = extract_section(alerts_text, '## 低优先级')

    lines: list[str] = []

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

    if not candidate_lines:
        return build_no_candidate_notification_text(account_label=account_label)

    return '\n'.join(lines).strip() + '\n'


def main():
    parser = argparse.ArgumentParser(description='Build symbols notification text from alerts')
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
    account_label = _infer_account_label(output_path, alerts_path)

    exchange_rate_info = None
    try:
        if args.state_dir:
            sd = Path(args.state_dir)
            if not sd.is_absolute():
                sd = (base / sd).resolve()
            rate_path = (sd / 'rate_cache.json').resolve()
        else:
            rate_path = (base / 'output' / 'state' / 'rate_cache.json').resolve()
        data = load_exchange_rate_info(
            cache_path=rate_path,
            max_age_hours=24,
            fetch_latest_on_miss=False,
        )
        rates = (data.get('rates') or {}) if isinstance(data, dict) else {}
        if rates:
            exchange_rate_info = {'USDCNY': rates.get('USDCNY'), 'timestamp': data.get('timestamp')}
    except Exception:
        exchange_rate_info = None

    notification = build_notification(
        '',
        alerts_text,
        exchange_rate_info=exchange_rate_info,
        account_label=account_label,
    )
    output_path.write_text(notification, encoding='utf-8')
    if str(changes_path) != '/dev/null':
        print(notification)
    print(f'[DONE] notification -> {output_path}')


if __name__ == '__main__':
    main()
