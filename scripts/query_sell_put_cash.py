#!/usr/bin/env python3
"""查询 sell put 担保占用与可用现金。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from scripts.cash_secured_utils import (
    cash_secured_symbol_cny,
    normalize_cash_secured_by_symbol_by_ccy,
    normalize_cash_secured_total_by_ccy,
    read_cash_secured_total_cny,
)
from scripts.config_loader import normalize_portfolio_broker_config, resolve_data_config_path
from scripts.exchange_rates import get_exchange_rates_or_fetch_latest
from scripts.fetch_option_positions_context import build_context as build_option_positions_context
from scripts.futu_portfolio_context import fetch_futu_portfolio_context
from scripts.option_positions_core.service import load_option_positions_repo
from scripts.portfolio_context_service import load_account_portfolio_context
from src.application.option_positions_facade import load_option_position_records


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


def _resolve_runtime_config_path(*, base: Path, config: str | Path | None) -> Path | None:
    if config is None or not str(config).strip():
        return None
    path = Path(config)
    if not path.is_absolute():
        path = (base / path).resolve()
    return path


def _normalize_runtime_config(cfg: dict) -> dict:
    out = dict(cfg or {})
    if 'templates' in out and 'profiles' not in out:
        out['profiles'] = out.get('templates')
    if 'symbols' in out and 'watchlist' not in out:
        out['watchlist'] = out.get('symbols')
    return normalize_portfolio_broker_config(out)


def _load_runtime_config(
    *,
    base: Path,
    config: str | Path | None,
    runtime_config: dict | None,
) -> dict:
    if isinstance(runtime_config, dict):
        return _normalize_runtime_config(dict(runtime_config))

    cfg_path = _resolve_runtime_config_path(base=base, config=config)
    if cfg_path is None:
        return {}

    cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
    if not isinstance(cfg, dict):
        raise SystemExit('[CONFIG_ERROR] runtime config must be a JSON object')
    return _normalize_runtime_config(cfg)


def _load_exchange_rate_payload(*, cache_path: Path, enabled: bool) -> dict:
    if not enabled:
        return {}
    payload = get_exchange_rates_or_fetch_latest(
        cache_path=cache_path,
        max_age_hours=24,
    )
    return payload if isinstance(payload, dict) else {}


def _load_option_position_records(data_config_path: Path) -> list[dict]:
    option_repo = load_option_positions_repo(data_config_path)
    return list(load_option_position_records(option_repo))


def query_sell_put_cash(
    *,
    config: str | Path | None = None,
    data_config: str | Path | None = None,
    market: str = '富途',
    account: str | None = None,
    output_format: str = 'text',
    top: int = 10,
    no_exchange_rates: bool = False,
    out_dir: str | Path = 'output/state',
    base_dir: Path | None = None,
    runtime_config: dict | None = None,
) -> dict:
    """执行卖 put 现金占用查询并按指定格式输出。"""
    base = (base_dir or Path(__file__).resolve().parents[1]).resolve()

    runtime_cfg = _load_runtime_config(base=base, config=config, runtime_config=runtime_config)
    data_config_path = resolve_data_config_path(base=base, data_config=data_config)

    out_dir_path = Path(out_dir)
    if not out_dir_path.is_absolute():
        out_dir_path = (base / out_dir_path).resolve()
    out_dir_path.mkdir(parents=True, exist_ok=True)

    portfolio = load_account_portfolio_context(
        base=base,
        data_config=str(data_config_path),
        market=market,
        account=account,
        ttl_sec=0,
        state_dir=out_dir_path,
        shared_state_dir=None,
        log=lambda _message: None,
        runtime_config=runtime_cfg,
        portfolio_source=None,
        fetch_futu_portfolio_context_fn=fetch_futu_portfolio_context,
        is_fresh_fn=lambda _path, _ttl_sec: False,
        load_json_fn=load_json,
    )

    option_records = _load_option_position_records(data_config_path)
    exchange_rate_payload = _load_exchange_rate_payload(
        cache_path=(out_dir_path / 'rate_cache.json').resolve(),
        enabled=(not no_exchange_rates),
    )
    opt = build_option_positions_context(
        option_records,
        broker=market,
        account=account,
        rates=exchange_rate_payload,
    )
    portfolio_source_name = (
        str((portfolio or {}).get('portfolio_source_name') or 'holdings').strip().lower() or 'holdings'
        if isinstance(portfolio, dict)
        else 'holdings'
    )

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

    usdcny_exchange_rate = None
    cny_per_hkd_exchange_rate = None
    cash_avail_cny = None
    cash_free_cny = None

    if not no_exchange_rates:
        try:
            rates = exchange_rate_payload.get('rates') or {}
            if rates.get('USDCNY'):
                usdcny_exchange_rate = float(rates['USDCNY'])
            if rates.get('HKDCNY'):
                cny_per_hkd_exchange_rate = float(rates['HKDCNY'])
        except Exception:
            usdcny_exchange_rate = None
            cny_per_hkd_exchange_rate = None

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
                if not usdcny_exchange_rate:
                    ok = False
                    break
                total += fv * float(usdcny_exchange_rate)
            elif c == 'HKD':
                if not cny_per_hkd_exchange_rate:
                    ok = False
                    break
                total += fv * float(cny_per_hkd_exchange_rate)
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
        'portfolio_source_name': portfolio_source_name,
        'cash_available_usd': cash_avail_usd,
        'cash_secured_used_usd': cash_secured_total_usd,
        'cash_free_usd': cash_free_usd,
        'cash_available_cny': cash_avail_cny,
        'cash_secured_used_cny': cash_secured_total_cny,
        'cash_free_cny': cash_free_cny,
        'cash_available_total_cny': cash_avail_total_cny,
        'cash_free_total_cny': cash_free_total_cny,
        'exchange_rates': {'USDCNY': usdcny_exchange_rate, 'HKDCNY': cny_per_hkd_exchange_rate},
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
    lines.append(f"portfolio_source: {portfolio_source_name}")
    lines.append('')

    lines.append(f"- base(CNY) 现金（账户口径）: {money(cash_avail_cny, 'CNY')}")
    lines.append(f"- Sell Put 已占用担保现金（折算CNY）: {money(cash_secured_total_cny, 'CNY')}")
    lines.append(f"- 不在担保之内的剩余现金（base free, CNY）: {money(cash_free_cny, 'CNY')}")

    lines.append(f"- 总现金（全币种折算CNY）: {money(payload.get('cash_available_total_cny'), 'CNY')}")
    lines.append(f"- 总剩余现金（total free, 折算CNY）: {money(payload.get('cash_free_total_cny'), 'CNY')}")

    if usdcny_exchange_rate or cny_per_hkd_exchange_rate:
        parts = []
        if usdcny_exchange_rate:
            parts.append(f'USDCNY={usdcny_exchange_rate:.4f}')
        if cny_per_hkd_exchange_rate:
            parts.append(f'HKDCNY={cny_per_hkd_exchange_rate:.4f}')
        lines.append('- 汇率: ' + ', '.join(parts))

    lines.append('')
    lines.append('## USD 视角（仅当账户口径里有 USD 现金时可靠）')
    lines.append(f"- USD 现金（账户口径）: {money(cash_avail_usd, 'USD')}")
    lines.append(f"- Sell Put 占用（USD 项合计）: {money(cash_secured_total_usd, 'USD')}")
    lines.append(f"- USD free（仅扣 USD 占用）: {money(cash_free_usd, 'USD')}")

    lines.append('')
    lines.append(f'## 占用明细（Top {top}，按币种）')
    if not norm_by_ccy:
        lines.append('- (无记录：要么没有 open short puts，要么持仓 lot 视图缺少 cash_secured_amount/currency)')
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
                        float(amt) * float(usdcny_exchange_rate)
                        if (ccy == 'USD' and usdcny_exchange_rate)
                        else (
                            float(amt) * float(cny_per_hkd_exchange_rate)
                            if (ccy == 'HKD' and cny_per_hkd_exchange_rate)
                            else None
                        )
                    )
                ),
            )
            cny_part = f" | ≈ {money(cny_eq, 'CNY')}" if cny_eq is not None else ''
            lines.append(f'- {sym}: {detail}{cny_part}')

    print('\n'.join(lines) + '\n')
    return payload
