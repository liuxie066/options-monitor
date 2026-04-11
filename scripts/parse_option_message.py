#!/usr/bin/env python3
"""Parse a Feishu message line like:

  期权：腾讯20260330 put，strike500，成本5.425每股，乘数100，short 10张，sy，HKD

into normalized params for option_positions writer.

This script is used by auto-intake (chat-driven). It does NOT write to Feishu.
"""

from __future__ import annotations

import json
import re
import os
from datetime import datetime
from pathlib import Path

# Suppress noisy OpenAPI logs when multiplier_cache triggers futu/OpenD imports.
os.environ.setdefault('OPENAPI_LOG_LEVEL', 'ERROR')


# NOTE: symbol aliases are now configurable in config.us.json/config.hk.json:intake.symbol_aliases.
# Keep a small built-in fallback for robustness.
ALIASES = {
    '腾讯': '0700.HK',
    '腾讯控股': '0700.HK',
    '泡泡玛特': '9992.HK',
    '美团': '3690.HK',
    '美团w': '3690.HK',
    '美团-w': '3690.HK',
    '美团-W': '3690.HK',
    '中海油': '0883.HK',
    '中国海洋石油': '0883.HK',
}


def load_intake_config() -> dict:
    """Load intake config from runtime entry configs (best-effort)."""
    base = Path(__file__).resolve().parents[1]
    merged: dict = {}
    for name in ("config.us.json", "config.hk.json"):
        try:
            cfg = json.loads((base / name).read_text(encoding="utf-8"))
        except Exception:
            continue
        intake = cfg.get("intake") or {}
        if not isinstance(intake, dict):
            continue
        for k, v in intake.items():
            if isinstance(v, dict):
                cur = merged.get(k)
                if isinstance(cur, dict):
                    cur.update(v)
                else:
                    merged[k] = dict(v)
            else:
                merged[k] = v
    return merged


def normalize_symbol(s: str) -> str | None:
    s = (s or '').strip()
    if not s:
        return None

    intake = load_intake_config()
    aliases = (intake.get('symbol_aliases') or {}) if isinstance(intake, dict) else {}

    if s in aliases:
        return str(aliases[s]).strip().upper()

    if s in ALIASES:
        return ALIASES[s]

    s2 = s.upper().replace(' ', '')

    # allow direct hk code 0700.HK / 9992.HK etc.
    if re.fullmatch(r"\d{4}\.HK", s2) or re.fullmatch(r"\d{5}\.HK", s2):
        return s2

    # allow US ticker like NVDA/TSLA/AAPL
    if re.fullmatch(r"[A-Z][A-Z0-9\.\-]{0,9}", s2):
        return s2

    return None


def parse_exp(s: str) -> str | None:
    """Parse expiration date.

    Supports:
    - YYYYMMDD (e.g. 20260330)
    - YYMMDD (e.g. 260330) -> 2026-03-30 (assumes 2000+)
    """
    # 8-digit date like 20260330
    m = re.search(r"(20\d{2})(\d{2})(\d{2})", s)
    if m:
        y, mo, d = m.group(1), m.group(2), m.group(3)
        return f"{y}-{mo}-{d}"

    # 6-digit date like 260330
    m2 = re.search(r"\b(\d{2})(\d{2})(\d{2})\b", s)
    if not m2:
        return None
    yy, mo, d = int(m2.group(1)), int(m2.group(2)), int(m2.group(3))
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    y = 2000 + yy
    return f"{y:04d}-{mo:02d}-{d:02d}"


def parse_float_after(keys: list[str], s: str) -> float | None:
    for k in keys:
        m = re.search(k + r"\s*([0-9]+(?:\.[0-9]+)?)", s, flags=re.I)
        if m:
            return float(m.group(1))
    return None


def parse_futu_strike(s: str) -> float | None:
    """Parse strike from Futu fill message.

    Supports:
      "$中海油 260330 30.00 购$" -> 30.00
      "$NVDA 260618 154.00P$" -> 154.00
    """
    m = re.search(r"\b\d{6}\s+([0-9]+(?:\.[0-9]+)?)\s*(?:购|沽)\b", s)
    if m:
        return float(m.group(1))

    m2 = re.search(r"\$[^$]*?\b\d{6}\s+([0-9]+(?:\.[0-9]+)?)\s*[CP]\$", s, flags=re.I)
    if m2:
        return float(m2.group(1))

    return None


def parse_futu_premium(s: str) -> float | None:
    # 成交价格：0.24
    m = re.search(r"成交价格\s*[:：]\s*([0-9]+(?:\.[0-9]+)?)", s)
    if m:
        return float(m.group(1))
    return None


def parse_futu_underlying(s: str) -> str | None:
    # Patterns:
    # - $中海油 260330 30.00 购$
    # - $NVDA 260618 154.00P$
    m = re.search(r"\$([^$]+?)\s+\d{6}\s+[0-9]+(?:\.[0-9]+)?\s*(?:购|沽)\$", s)
    if m:
        return m.group(1).strip()

    m2 = re.search(r"\$([^$]+?)\s+\d{6}\s+[0-9]+(?:\.[0-9]+)?\s*[CP]\$", s, flags=re.I)
    if m2:
        return m2.group(1).strip()

    return None


def infer_currency(s: str) -> str | None:
    c = parse_currency(s)
    if c:
        return c
    # Futu HK hint
    if '香港' in s or '富途证券(香港' in s or '富途证券（香港' in s:
        return 'HKD'
    return None


def infer_market(s: str) -> str | None:
    """Infer broker/source market label for option_positions.

    Current policy: map Futu notifications to market='富途'.
    """
    s2 = str(s or '')
    if ('富途证券' in s2) or ('富途' in s2):
        return '富途'
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
    if re.search(r"\bput\b", s, flags=re.I) or '认沽' in s or '沽' in s:
        return 'put'
    if re.search(r"\bcall\b", s, flags=re.I) or '认购' in s or '购' in s:
        return 'call'

    # Futu style: $NVDA 260618 154.00P$
    if re.search(r"\$[^$]*\d{6}\s+[0-9]+(?:\.[0-9]+)?\s*P\$", s, flags=re.I):
        return 'put'
    if re.search(r"\$[^$]*\d{6}\s+[0-9]+(?:\.[0-9]+)?\s*C\$", s, flags=re.I):
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
    # take leading Chinese name before date (manual intake format)
    m = re.match(r"\s*([^0-9]{1,10}?)(20\d{6}).*", s)
    if m:
        return m.group(1).strip(' ：:,，')
    return None


def _infer_multiplier_if_missing(*, symbol: str | None, multiplier: int | None, repo_base: Path) -> int | None:
    """在缺少乘数时，尽力通过 OpenD 推断。"""
    if multiplier is not None or not symbol:
        return multiplier

    # Policy: do NOT default silently. Prefer fetching from OpenD (futu-api) for the underlier.
    # If OpenD is unavailable, leave missing so user must input explicitly.
    try:
        import sys
        if str(repo_base) not in sys.path:
            sys.path.insert(0, str(repo_base))

        from scripts import multiplier_cache

        r = multiplier_cache.refresh_via_opend(
            repo_base=repo_base,
            symbol=symbol,
            host='127.0.0.1',
            port=11111,
            limit_expirations=1,
        )
        if r.ok and r.multiplier and int(r.multiplier) > 0:
            multiplier = int(r.multiplier)
            # Persist cache entry as opend-derived for later reuse
            try:
                cache_path = multiplier_cache.default_cache_path(repo_base)
                cache = multiplier_cache.load_cache(cache_path)
                cache[str(symbol).upper()] = {
                    'multiplier': int(multiplier),
                    'as_of_utc': multiplier_cache.utc_now(),
                    'source': 'opend',
                }
                multiplier_cache.save_cache(cache_path, cache)
            except Exception:
                pass
    except Exception:
        pass
    return multiplier


def parse_option_message_text(text: str) -> dict:
    """解析单条期权消息，返回结构化字段。"""
    raw = (text or '').strip()

    # strip prefix like "期权："
    raw2 = re.sub(r"^\s*期权\s*[:：]\s*", "", raw)

    # 1) detect Futu fill-like message
    futu_underlying = parse_futu_underlying(raw2)
    if futu_underlying:
        underlying = futu_underlying
        symbol = normalize_symbol(underlying)
        exp = parse_exp(raw2)
        opt_type = parse_option_type(raw2)
        side = parse_side(raw2)
        strike = parse_futu_strike(raw2)
        premium = parse_futu_premium(raw2)
        contracts = parse_contracts(raw2)
        account = parse_account(raw2)
        currency = infer_currency(raw2)
        market = infer_market(raw2)
        multiplier = None  # not present in message; fill later
    else:
        # 2) manual intake format
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
        currency = infer_currency(raw2)
        market = infer_market(raw2)

    base = Path(__file__).resolve().parents[1]
    multiplier = _infer_multiplier_if_missing(symbol=symbol, multiplier=multiplier, repo_base=base)

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
            'market': market,
        },
        'missing': [
            k for k, v in {
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

    return out
