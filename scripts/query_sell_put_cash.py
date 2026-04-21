#!/usr/bin/env python3
"""查询 sell put 担保占用与可用现金。"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from scripts.cash_secured_utils import (
    cash_secured_symbol_cny,
    normalize_cash_secured_by_symbol_by_ccy,
    normalize_cash_secured_total_by_ccy,
    read_cash_secured_total_cny,
)
from scripts.fx_rates import get_rates_or_fetch_latest


def run(cmd: list[str], cwd: Path, timeout_sec: int = 60):
    p = subprocess.run(cmd, cwd=str(cwd), timeout=timeout_sec)
    if p.returncode != 0:
        raise SystemExit(p.returncode)


def load_json(path: Path) -> dict:
    if not path.exists() or path.stat().st_size <= 0:
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def money(v: float | None, currency: str = "USD") -> str:
    if v is None:
        return "-"
    if currency.upper() in ("USD",):
        return f"${v:,.2f}"
    if currency.upper() in ("CNY", "RMB"):
        return f"¥{v:,.2f}"
    return f"{v:,.2f} {currency.upper()}"


def query_sell_put_cash(
    *,
    pm_config: str | Path = '../portfolio-management/config.json',
    market: str = '富途',
    account: str | None = None,
    output_format: str = 'text',
    top: int = 10,
    no_fx: bool = False,
    out_dir: str | Path = 'output/state',
    base_dir: Path | None = None,
) -> dict:
    """执行卖 put 现金占用查询并按指定格式输出。"""
    base = (base_dir or Path(__file__).resolve().parents[1]).resolve()

    pm_config_path = Path(pm_config)
    if not pm_config_path.is_absolute():
        pm_config_path = (base / pm_config_path).resolve()

    out_dir_path = Path(out_dir)
    if not out_dir_path.is_absolute():
        out_dir_path = (base / out_dir_path).resolve()
    out_dir_path.mkdir(parents=True, exist_ok=True)

    portfolio_out = out_dir_path / 'portfolio_context.json'
    option_out = out_dir_path / 'option_positions_context.json'

    run(
        [
            str(base / '.venv' / 'bin' / 'python'),
            'scripts/fetch_portfolio_context.py',
            '--pm-config',
            str(pm_config_path),
            '--market',
            market,
            '--account',
            (account or ''),
            '--out',
            str(portfolio_out),
        ],
        cwd=base,
        timeout_sec=90,
    )

    run(
        [
            str(base / '.venv' / 'bin' / 'python'),
            'scripts/fetch_option_positions_context.py',
            '--pm-config',
            str(pm_config_path),
            '--market',
            market,
            '--account',
            (account or ''),
            '--out',
            str(option_out),
        ],
        cwd=base,
        timeout_sec=90,
    )

    portfolio = load_json(portfolio_out)
    opt = load_json(option_out)

    cash_by_ccy = portfolio.get('cash_by_currency') or {}
    cash_avail_usd = cash_by_ccy.get('USD')
    try:
        cash_avail_usd = float(cash_avail_usd) if cash_avail_usd is not None else None
    except Exception:
        cash_avail_usd = None

    norm_by_ccy = normalize_cash_secured_by_symbol_by_ccy(opt)
    total_by_ccy_norm = normalize_cash_secured_total_by_ccy(opt, by_symbol_by_ccy=norm_by_ccy)
    cash_secured_total_cny = read_cash_secured_total_cny(opt)

    cash_secured_total_usd = total_by_ccy_norm.get('USD')
    cash_free_usd = None
    if cash_avail_usd is not None and cash_secured_total_usd is not None:
        cash_free_usd = cash_avail_usd - cash_secured_total_usd

    usdcny = None
    hkdcny = None
    cash_avail_cny = None
    cash_free_cny = None

    if not no_fx:
        try:
            fx = get_rates_or_fetch_latest(
                cache_path=(out_dir_path / 'rate_cache.json').resolve(),
                shared_cache_path=(base / 'output_shared' / 'state' / 'rate_cache.json').resolve(),
                max_age_hours=24,
            )
            rates = (fx.get('rates') or {}) if isinstance(fx, dict) else {}
            if rates.get('USDCNY'):
                usdcny = float(rates['USDCNY'])
            if rates.get('HKDCNY'):
                hkdcny = float(rates['HKDCNY'])
        except Exception:
            usdcny = None
            hkdcny = None

    try:
        cash_avail_cny = float((cash_by_ccy.get('CNY') if isinstance(cash_by_ccy, dict) else None))
    except Exception:
        cash_avail_cny = None

    if cash_avail_cny is not None and cash_secured_total_cny is not None:
        cash_free_cny = cash_avail_cny - cash_secured_total_cny

    cash_avail_total_cny = None
    if isinstance(cash_by_ccy, dict):
        total = 0.0
        ok = True
        for ccy, v in cash_by_ccy.items():
            try:
                fv = float(v)
            except Exception:
                continue
            if not fv:
                continue
            c = str(ccy).strip().upper()
            if c in ('CNY', 'RMB'):
                total += fv
            elif c == 'USD':
                if not usdcny:
                    ok = False
                    break
                total += fv * float(usdcny)
            elif c == 'HKD':
                if not hkdcny:
                    ok = False
                    break
                total += fv * float(hkdcny)
            else:
                ok = False
                break
        if ok:
            cash_avail_total_cny = total

    cash_free_total_cny = None
    if cash_avail_total_cny is not None and cash_secured_total_cny is not None:
        cash_free_total_cny = cash_avail_total_cny - cash_secured_total_cny

    payload = {
        'as_of_utc': datetime.now(timezone.utc).isoformat(),
        'market': market,
        'account': account,
        'cash_available_usd': cash_avail_usd,
        'cash_secured_used_usd': cash_secured_total_usd,
        'cash_free_usd': cash_free_usd,
        'cash_available_cny': cash_avail_cny,
        'cash_secured_used_cny': cash_secured_total_cny,
        'cash_free_cny': cash_free_cny,
        'cash_available_total_cny': cash_avail_total_cny,
        'cash_free_total_cny': cash_free_total_cny,
        'fx_rates': {'USDCNY': usdcny, 'HKDCNY': hkdcny},
        'cash_secured_total_by_ccy': total_by_ccy_norm,
        'cash_secured_by_symbol_by_ccy': norm_by_ccy,
    }

    if output_format == 'json':
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return payload

    lines = []
    lines.append('# Sell Put 担保现金占用 / 剩余现金')
    lines.append(f"as_of_utc: {payload['as_of_utc']}")
    lines.append(f"market: {market} | account: {account or '-'}")
    lines.append('')

    lines.append(f"- base(CNY) 现金（持仓表）: {money(cash_avail_cny, 'CNY')}")
    lines.append(f"- Sell Put 已占用担保现金（折算CNY）: {money(cash_secured_total_cny, 'CNY')}")
    lines.append(f"- 不在担保之内的剩余现金（base free, CNY）: {money(cash_free_cny, 'CNY')}")

    lines.append(f"- 总现金（holdings 全币种折算CNY）: {money(payload.get('cash_available_total_cny'), 'CNY')}")
    lines.append(f"- 总剩余现金（total free, 折算CNY）: {money(payload.get('cash_free_total_cny'), 'CNY')}")

    if usdcny or hkdcny:
        parts = []
        if usdcny:
            parts.append(f'USDCNY={usdcny:.4f}')
        if hkdcny:
            parts.append(f'HKDCNY={hkdcny:.4f}')
        lines.append('- 汇率: ' + ', '.join(parts))

    lines.append('')
    lines.append('## USD 视角（仅当 holdings 里记录了 USD 现金才可靠）')
    lines.append(f"- USD 现金（持仓表）: {money(cash_avail_usd, 'USD')}")
    lines.append(f"- Sell Put 占用（USD 项合计）: {money(cash_secured_total_usd, 'USD')}")
    lines.append(f"- USD free（仅扣 USD 占用）: {money(cash_free_usd, 'USD')}")

    lines.append('')
    lines.append(f'## 占用明细（Top {top}，按币种）')
    if not norm_by_ccy:
        lines.append('- (无记录：要么没有 open short puts，要么 option_positions 表未填写 cash_secured_amount/currency)')
    else:
        items = []
        for sym, m in norm_by_ccy.items():
            total = sum(m.values())
            items.append((sym, total, m))
        items.sort(key=lambda x: x[1], reverse=True)

        for sym, _, m in items[: max(top, 1)]:
            detail = ', '.join([f"{ccy} {money(v, ccy).replace('$', '').replace('¥', '')}" for ccy, v in sorted(m.items())])
            cny_eq = cash_secured_symbol_cny(
                opt,
                sym,
                by_symbol_by_ccy=norm_by_ccy,
                native_to_cny=lambda amt, ccy: (
                    float(amt)
                    if ccy == 'CNY'
                    else (
                        float(amt) * float(usdcny)
                        if (ccy == 'USD' and usdcny)
                        else (
                            float(amt) * float(hkdcny)
                            if (ccy == 'HKD' and hkdcny)
                            else None
                        )
                    )
                ),
            )
            cny_part = f" | ≈ {money(cny_eq, 'CNY')}" if cny_eq is not None else ''
            lines.append(f'- {sym}: {detail}{cny_part}')

    print('\n'.join(lines) + '\n')
    return payload
