#!/usr/bin/env python3
"""Parse a Feishu message line like:

  期权：腾讯20260330 put，strike500，成本5.425每股，乘数100，short 10张，sy，HKD

into normalized params for option_positions writer.

This script is used by auto-intake (chat-driven). It does NOT write to Feishu.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime


ALIASES = {
    '腾讯': '0700.HK',
    '腾讯控股': '0700.HK',
    '泡泡玛特': '9992.HK',
    '美团': '3690.HK',
    '美团w': '3690.HK',
    '美团-w': '3690.HK',
    '美团-W': '3690.HK',
}


def normalize_symbol(s: str) -> str | None:
    s = (s or '').strip()
    if not s:
        return None
    if s in ALIASES:
        return ALIASES[s]
    # allow direct hk code 0700.HK / 9992.HK etc.
    s2 = s.upper().replace(' ', '')
    if re.fullmatch(r"\d{4}\.HK", s2) or re.fullmatch(r"\d{5}\.HK", s2):
        return s2
    return None


def parse_exp(s: str) -> str | None:
    # find 8-digit date like 20260330
    m = re.search(r"(20\d{2})(\d{2})(\d{2})", s)
    if not m:
        return None
    y, mo, d = m.group(1), m.group(2), m.group(3)
    return f"{y}-{mo}-{d}"


def parse_float_after(keys: list[str], s: str) -> float | None:
    for k in keys:
        m = re.search(k + r"\s*([0-9]+(?:\.[0-9]+)?)", s, flags=re.I)
        if m:
            return float(m.group(1))
    return None


def parse_int_after(keys: list[str], s: str) -> int | None:
    for k in keys:
        m = re.search(k + r"\s*([0-9]+)", s, flags=re.I)
        if m:
            return int(m.group(1))
    return None


def parse_side(s: str) -> str | None:
    if re.search(r"\bshort\b", s, flags=re.I) or '卖' in s:
        return 'short'
    if re.search(r"\blong\b", s, flags=re.I) or '买' in s:
        return 'long'
    return None


def parse_option_type(s: str) -> str | None:
    if re.search(r"\bput\b", s, flags=re.I) or '认沽' in s:
        return 'put'
    if re.search(r"\bcall\b", s, flags=re.I) or '认购' in s:
        return 'call'
    return None


def parse_contracts(s: str) -> int | None:
    # "10张" or "short 10张"
    m = re.search(r"([0-9]+)\s*张", s)
    if m:
        return int(m.group(1))
    return None


def parse_account(s: str) -> str | None:
    m = re.search(r"\b(lx|sy)\b", s, flags=re.I)
    if m:
        return m.group(1).lower()
    # Chinese hint
    if '账户sy' in s.lower() or 'sy账户' in s.lower():
        return 'sy'
    if '账户lx' in s.lower() or 'lx账户' in s.lower():
        return 'lx'
    return None


def parse_currency(s: str) -> str | None:
    s2 = s.upper()
    if 'HKD' in s2 or '港币' in s or '港幣' in s:
        return 'HKD'
    if 'USD' in s2 or '美元' in s:
        return 'USD'
    if 'CNY' in s2 or 'RMB' in s2 or '人民币' in s or '人民幣' in s:
        return 'CNY'
    return None


def parse_underlying_name(s: str) -> str | None:
    # take leading Chinese name before date
    m = re.match(r"\s*([^0-9]{1,10}?)(20\d{6}).*", s)
    if m:
        return m.group(1).strip(' ：:,，')
    return None


def main():
    ap = argparse.ArgumentParser(description='Parse option intake message')
    ap.add_argument('--text', required=True)
    args = ap.parse_args()

    raw = args.text.strip()

    # strip prefix like "期权："
    raw2 = re.sub(r"^\s*期权\s*[:：]\s*", "", raw)

    underlying = parse_underlying_name(raw2)
    symbol = normalize_symbol(underlying or '')
    exp = parse_exp(raw2)
    opt_type = parse_option_type(raw2)
    side = parse_side(raw2)
    strike = parse_float_after(['strike', '行权价', '行权'], raw2)
    multiplier = parse_int_after(['乘数', 'multiplier'], raw2)
    premium = parse_float_after(['成本', 'premium', '权利金'], raw2)
    contracts = parse_contracts(raw2)
    account = parse_account(raw2)
    currency = parse_currency(raw2)

    ok = all([symbol, exp, opt_type, side, strike is not None, multiplier, contracts, account, currency])

    out = {
        'ok': ok,
        'raw': raw,
        'parsed': {
            'underlying': underlying,
            'symbol': symbol,
            'exp': exp,
            'option_type': opt_type,
            'side': side,
            'strike': strike,
            'multiplier': multiplier,
            'premium_per_share': premium,
            'contracts': contracts,
            'account': account,
            'currency': currency,
        },
        'missing': [
            k for k,v in {
                'symbol': symbol,
                'exp': exp,
                'option_type': opt_type,
                'side': side,
                'strike': strike,
                'multiplier': multiplier,
                'contracts': contracts,
                'account': account,
                'currency': currency,
            }.items() if v in (None, '')
        ],
        'ts': datetime.utcnow().isoformat() + 'Z',
    }

    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
