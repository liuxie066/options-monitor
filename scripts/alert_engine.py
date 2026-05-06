#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

repo_base = Path(__file__).resolve().parents[1]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

import pandas as pd
from pandas.errors import EmptyDataError

from scripts.alert_rules import (
    SELL_CALL_NOTIFICATION_HIGH,
    SELL_CALL_NOTIFICATION_LOW,
    SELL_PUT_NOTIFICATION_HIGH,
    SELL_PUT_NOTIFICATION_LOW,
)
from scripts.alert_policy import DEFAULT_ALERT_POLICY, load_alert_policy
from scripts.report_formatting import num, pct, strike_text

YIELD_ENHANCEMENT_NOTIFICATION_HIGH = '已按组合收益筛出推荐 Call，可作为该 Sell Put 的收益增强方案。'

def _load_symbol_display_map(base: Path, *, state_dir: Path | None = None) -> dict[str, str]:
    """Best-effort load display name mapping.

    Priority:
    1) portfolio_context.json: stocks_by_symbol[*].name
    2) config.us.json/config.hk.json:intake.symbol_aliases (name -> code inverted)

    Returns: {"0700.HK": "腾讯", ...}
    """
    m: dict[str, str] = {}

    # 1) from portfolio context
    try:
        if state_dir is not None:
            port_path = (state_dir / 'portfolio_context.json').resolve()
            if port_path.exists() and port_path.stat().st_size > 0:
                ctx = json.loads(port_path.read_text(encoding='utf-8'))
                stocks = (ctx.get('stocks_by_symbol') or {}) if isinstance(ctx, dict) else {}
                if isinstance(stocks, dict):
                    for sym, info in stocks.items():
                        if not sym or not isinstance(info, dict):
                            continue
                        name = str(info.get('name') or '').strip()
                        code = str(sym).strip().upper()
                        if name and code:
                            m[code] = name
    except Exception:
        pass

    # 2) from runtime config aliases (fallback)
    for cfg_name in ('config.us.json', 'config.hk.json'):
        try:
            cfg = json.loads((base / cfg_name).read_text(encoding='utf-8'))
            intake = (cfg.get('intake') or {}) if isinstance(cfg, dict) else {}
            aliases = (intake.get('symbol_aliases') or {}) if isinstance(intake, dict) else {}
            for name, code in (aliases or {}).items():
                n = str(name or '').strip()
                c = str(code or '').strip().upper()
                if not n or not c:
                    continue
                prev = m.get(c)
                if (prev is None) or (len(n) < len(prev)):
                    m[c] = n
        except Exception:
            continue

    return m


def _disp_symbol(symbol: str, mp: dict[str, str]) -> str:
    s = str(symbol or '').strip().upper()
    return mp.get(s) or str(symbol or '').strip()


# Alert priority policy (keep it simple):
# Layer 1 (收益率门槛) is already handled by the scanners (min_annualized_* in config).
# Layer 2 (风险/约束) is handled here:
#   - sell_put: base(CNY) cash headroom gate; then high vs medium by annualized return.
#   - sell_call: covered capacity gate; then high vs medium by annualized return.
DEFAULT_POLICY = DEFAULT_ALERT_POLICY.to_mapping()

# Initialized in main() from --policy-json (or DEFAULT_POLICY).
POLICY = DEFAULT_POLICY.copy()


def safe_read_csv(path: Path) -> pd.DataFrame:
    try:
        if path.exists() and path.stat().st_size > 0:
            return pd.read_csv(path)
    except EmptyDataError:
        pass
    return pd.DataFrame()


def _append_option_quote_parts(parts: list[str], row: pd.Series, *, prepend: bool = False) -> None:
    def _add(token: str) -> None:
        if prepend:
            parts.insert(0, token)
        else:
            parts.append(token)

    try:
        d = row.get('delta')
        if d is not None and not pd.isna(d):
            _add(f"delta {float(d):.2f}")
    except Exception:
        pass
    try:
        iv = row.get('iv')
        if iv is not None and not pd.isna(iv):
            _add(f"iv {pct(iv)}")
    except Exception:
        pass
    try:
        ccy = row.get('option_ccy')
        if ccy and isinstance(ccy, str):
            _add(f"ccy {ccy.strip().upper()}")
    except Exception:
        pass
    try:
        mid = row.get('mid')
        if mid is not None and not pd.isna(mid):
            _add(f"mid {float(mid):.3f}")
    except Exception:
        pass
    try:
        bid = row.get('bid')
        if bid is not None and not pd.isna(bid):
            _add(f"bid {float(bid):.3f}")
    except Exception:
        pass
    try:
        ask = row.get('ask')
        if ask is not None and not pd.isna(ask):
            _add(f"ask {float(ask):.3f}")
    except Exception:
        pass


def _build_sell_call_extra_parts(row: pd.Series) -> list[str]:
    shares_total = int(row.get('shares_total') or 0)
    shares_locked = int(row.get('shares_locked') or 0)
    cover_avail = int(row.get('cover_avail') or 0)
    parts: list[str] = []
    _append_option_quote_parts(parts, row)
    parts.append(f"cover {cover_avail}")
    parts.append(f"shares {shares_total}(-{shares_locked})")
    return parts


def _build_sell_put_extra_parts(row: pd.Series) -> list[str]:
    used_total = None
    used_symbol = None
    req_cny = None
    avail_cny = None
    free_cny = None
    free_total_cny = None
    req = None
    avail = row.get('cash_available_usd')
    free = row.get('cash_free_usd')
    avail_est = row.get('cash_available_usd_est')
    free_est = row.get('cash_free_usd_est')

    try:
        v = row.get('cash_secured_used_cny') or row.get('cash_secured_used_cny_total')
        used_total = float(v) if v is not None and not pd.isna(v) else None
    except Exception:
        used_total = None
    try:
        v = row.get('cash_secured_used_cny_symbol')
        used_symbol = float(v) if v is not None and not pd.isna(v) else None
    except Exception:
        used_symbol = None
    try:
        v = row.get('cash_required_cny')
        req_cny = float(v) if v is not None and not pd.isna(v) else None
    except Exception:
        req_cny = None
    try:
        v = row.get('cash_available_cny')
        avail_cny = float(v) if v is not None and not pd.isna(v) else None
    except Exception:
        avail_cny = None
    try:
        v = row.get('cash_free_cny')
        free_cny = float(v) if v is not None and not pd.isna(v) else None
    except Exception:
        free_cny = None
    try:
        v = row.get('cash_free_total_cny')
        free_total_cny = float(v) if v is not None and not pd.isna(v) else None
    except Exception:
        free_total_cny = None
    try:
        raw_req = row.get('cash_required_usd')
        req = float(raw_req) if raw_req is not None and not pd.isna(raw_req) else None
    except Exception:
        req = None

    parts: list[str] = []
    if req_cny is not None and req_cny > 0:
        parts.append(f"cash_req_cny ¥{req_cny:,.0f}")
    elif req is not None and req > 0:
        parts.append(f"cash_req ${req:,.0f}")
    if used_total is not None and used_total > 0:
        parts.append(f"cash_used_total_cny ¥{used_total:,.0f}")
        if used_symbol is not None and used_symbol > 0:
            parts.append(f"cash_used_sym_cny ¥{used_symbol:,.0f}")
    if avail_cny is not None and avail_cny > 0:
        parts.append(f"cash_avail_cny ¥{avail_cny:,.0f}")
    if free_cny is not None and free_cny > 0:
        parts.append(f"cash_free_cny ¥{free_cny:,.0f}")
    elif free_total_cny is not None and free_total_cny > 0:
        parts.append(f"cash_free_total_cny ¥{free_total_cny:,.0f}")

    if not parts:
        if used_total is not None and used_total > 0:
            parts.append(f"cash_used_total ${used_total:,.0f}")
            if used_symbol is not None and used_symbol > 0:
                parts.append(f"cash_used_sym ${used_symbol:,.0f}")
        try:
            if avail is not None and not pd.isna(avail):
                parts.append(f"cash_avail ${float(avail):,.0f}")
            elif avail_est is not None and not pd.isna(avail_est):
                parts.append(f"cash_avail_eq ${float(avail_est):,.0f}")
        except Exception:
            pass
        try:
            if free is not None and not pd.isna(free):
                parts.append(f"cash_free ${float(free):,.0f}")
            elif free_est is not None and not pd.isna(free_est):
                parts.append(f"cash_free_eq ${float(free_est):,.0f}")
        except Exception:
            pass

    _append_option_quote_parts(parts, row, prepend=True)

    linked_call_contract = str(row.get('linked_call_contract') or '').strip()
    if linked_call_contract:
        parts.append(f"linked_call {linked_call_contract}")
        try:
            value = row.get('linked_call_ask')
            if value is not None and not pd.isna(value):
                parts.append(f"linked_call_ask {float(value):.3f}")
        except Exception:
            pass
        try:
            value = row.get('linked_call_delta')
            if value is not None and not pd.isna(value):
                parts.append(f"linked_call_delta {float(value):.2f}")
        except Exception:
            pass
        try:
            value = row.get('linked_call_net_credit')
            if value is not None and not pd.isna(value):
                parts.append(f"linked_net_credit {num(value)}")
        except Exception:
            pass
        try:
            value = row.get('linked_call_scenario_score')
            if value is not None and not pd.isna(value):
                parts.append(f"linked_scenario_score {pct(value)}")
        except Exception:
            pass
        try:
            value = row.get('linked_call_count')
            if value is not None and not pd.isna(value):
                parts.append(f"linked_call_count {int(value)}")
        except Exception:
            pass
    return parts


def _build_yield_enhancement_extra_parts(row: pd.Series) -> list[str]:
    parts: list[str] = []
    _append_option_quote_parts(parts, row)
    try:
        value = row.get('put_strike')
        if value is not None and not pd.isna(value):
            parts.append(f"put_strike {strike_text(value)}")
    except Exception:
        pass
    try:
        value = row.get('call_strike')
        if value is not None and not pd.isna(value):
            parts.append(f"call_strike {strike_text(value)}")
    except Exception:
        pass
    try:
        value = row.get('call_ask')
        if value is not None and not pd.isna(value):
            parts.append(f"call_ask {float(value):.3f}")
    except Exception:
        pass
    try:
        value = row.get('call_delta')
        if value is not None and not pd.isna(value):
            parts.append(f"call_delta {float(value):.2f}")
    except Exception:
        pass
    try:
        value = row.get('net_credit')
        if value is not None and not pd.isna(value):
            parts.append(f"net_credit {num(value)}")
    except Exception:
        pass
    try:
        value = row.get('expected_move')
        if value is not None and not pd.isna(value):
            parts.append(f"expected_move {num(value)}")
    except Exception:
        pass
    try:
        value = row.get('expected_move_iv')
        if value is not None and not pd.isna(value):
            parts.append(f"expected_move_iv {pct(value)}")
    except Exception:
        pass
    try:
        value = row.get('scenario_score')
        if value is not None and not pd.isna(value):
            parts.append(f"scenario_score {pct(value)}")
    except Exception:
        pass
    try:
        value = row.get('combo_spread_ratio')
        if value is not None and not pd.isna(value):
            parts.append(f"combo_spread {pct(value)}")
    except Exception:
        pass
    try:
        value = row.get('call_candidate_count')
        if value is not None and not pd.isna(value):
            parts.append(f"call_candidate_count {int(value)}")
    except Exception:
        pass
    return parts


def top_pick_line(row: pd.Series) -> str:
    extra = ''
    try:
        if row.get('strategy') == 'sell_call':
            parts = _build_sell_call_extra_parts(row)
            extra = " | " + " | ".join(parts)
        elif row.get('strategy') == 'sell_put':
            parts = _build_sell_put_extra_parts(row)
            if parts:
                extra = " | " + " | ".join(parts)
        elif row.get('strategy') == 'yield_enhancement':
            parts = _build_yield_enhancement_extra_parts(row)
            if parts:
                extra = " | " + " | ".join(parts)
    except Exception:
        extra = ''

    return (
        f"{row['symbol']} | {row['strategy']} | {row['top_contract'] or '-'} | "
        f"年化 {pct(row['annualized_return'])} | 净收入 {num(row['net_income'])} | "
        f"DTE {('-' if pd.isna(row['dte']) else int(row['dte']))} | "
        f"Strike {strike_text(row['strike'])} | {row['risk_label'] or '-'}"
        f"{extra}"
    )


def classify_alert(row: pd.Series) -> tuple[str | None, str]:
    if int(row.get('candidate_count', 0) or 0) <= 0:
        return None, ''

    strategy = row.get('strategy', '')
    annual = float(row.get('annualized_return', 0) or 0)

    if strategy == 'sell_put':
        # Defensive guard for standalone summary->alert generation paths.
        # The main pipeline should already filter cash-insufficient candidates
        # upstream, but this public alert entrypoint can also consume replayed or
        # externally supplied summary CSVs.
        cash_free = None
        cash_free_est = None
        cash_req = None
        cash_free_cny = None
        cash_free_total_cny = None
        cash_req_cny = None
        try:
            v = row.get('cash_free_cny')
            cash_free_cny = float(v) if v is not None and not pd.isna(v) else None
        except Exception:
            cash_free_cny = None
        try:
            v = row.get('cash_free_total_cny')
            cash_free_total_cny = float(v) if v is not None and not pd.isna(v) else None
        except Exception:
            cash_free_total_cny = None
        try:
            v = row.get('cash_required_cny')
            cash_req_cny = float(v) if v is not None and not pd.isna(v) else None
        except Exception:
            cash_req_cny = None

        if cash_free_cny is not None and cash_req_cny is not None and cash_req_cny > cash_free_cny:
            return 'low', f'所需担保现金约 ¥{cash_req_cny:,.0f}，但当前 base(CNY) 现金余量约 ¥{cash_free_cny:,.0f}（扣占用后折算），可能无法再加仓。'
        if cash_free_cny is None and cash_free_total_cny is not None and cash_req_cny is not None and cash_req_cny > cash_free_total_cny:
            return 'low', f'所需担保现金约 ¥{cash_req_cny:,.0f}，但当前总可用折算约 ¥{cash_free_total_cny:,.0f}（扣占用后折算），可能无法再加仓。'

        try:
            v = row.get('cash_free_usd')
            cash_free = float(v) if v is not None and not pd.isna(v) else None
        except Exception:
            cash_free = None
        try:
            v = row.get('cash_free_usd_est')
            cash_free_est = float(v) if v is not None and not pd.isna(v) else None
        except Exception:
            cash_free_est = None
        try:
            v = row.get('cash_required_usd')
            cash_req = float(v) if v is not None and not pd.isna(v) else None
        except Exception:
            cash_req = None

        if cash_free is not None and cash_req is not None and cash_req > cash_free:
            return 'low', f'所需担保现金约 ${cash_req:,.0f}，但当前账户可用担保现金约 ${cash_free:,.0f}（已扣占用），可能无法再加仓。'

        if (cash_free is None) and (cash_free_cny is None) and (cash_free_total_cny is None) and cash_free_est is not None and cash_req is not None and cash_req > cash_free_est:
            return 'low', f'所需担保现金约 ${cash_req:,.0f}，但账户可用担保现金(折算USD)约 ${cash_free_est:,.0f}（已扣占用）；可能无法再加仓，仅供观察。'

        if annual > 0:
            return 'high', SELL_PUT_NOTIFICATION_HIGH
        return 'low', SELL_PUT_NOTIFICATION_LOW

    if strategy == 'sell_call':
        # account-aware gating: if no covered capacity, do not promote to high/medium
        try:
            cover_avail = int(row.get('cover_avail') or 0)
        except Exception:
            cover_avail = 0
        if cover_avail <= 0:
            return 'low', '当前富途可覆盖张数为 0（可能已占用或持仓不足），仅供观察。'

        if annual > 0:
            return 'high', SELL_CALL_NOTIFICATION_HIGH
        return 'low', SELL_CALL_NOTIFICATION_LOW

    if strategy == 'yield_enhancement':
        if annual > 0:
            return 'high', YIELD_ENHANCEMENT_NOTIFICATION_HIGH
        return 'low', '当前收益增强推荐未通过优先级阈值，仅供观察。'

    return None, ''


def build_alert_text(summary: pd.DataFrame, *, symbol_display_map: dict[str, str] | None = None) -> str:
    lines: list[str] = ['# Symbols Alerts', '']
    mp = symbol_display_map or {}

    if summary.empty:
        lines.append('无提醒。')
        return '\n'.join(lines) + '\n'

    high_rows: list[str] = []
    medium_rows: list[str] = []
    low_rows: list[str] = []

    ordered = summary.sort_values(['symbol', 'strategy']).copy()
    for _, row in ordered.iterrows():
        level, comment = classify_alert(row)
        if not level:
            continue
        sym_disp = _disp_symbol(row.get('symbol', ''), mp)
        sym_code = str(row.get('symbol', '')).strip()
        sym_tag = f"[{sym_disp}]({sym_code})" if sym_disp and sym_code and (sym_disp != sym_code) else sym_code

        # Reuse original line builder for all fields/extras, only replace the first field (symbol) with sym_tag.
        base_line = top_pick_line(row)
        try:
            parts = base_line.split(' | ')
            if parts:
                parts[0] = sym_tag
            line_core = ' | '.join(parts)
        except Exception:
            line_core = base_line

        line = f"- {line_core} | {comment}"
        if level == 'high':
            high_rows.append(line)
        elif level == 'medium':
            medium_rows.append(line)
        else:
            low_rows.append(line)

    if high_rows:
        lines.append('## 高优先级')
        lines.extend(high_rows)
        lines.append('')

    if medium_rows:
        lines.append('## 中优先级')
        lines.extend(medium_rows)
        lines.append('')

    if low_rows:
        lines.append('## 低优先级')
        lines.extend(low_rows)
        lines.append('')

    if not (high_rows or medium_rows or low_rows):
        lines.append('无提醒。')
        lines.append('')

    lines.append('## 说明')
    lines.append('提醒模块不会重新做准入筛选；它只是对已通过扫描条件的候选做优先级排序。')
    return '\n'.join(lines) + '\n'


def clean_text(v) -> str:
    if pd.isna(v):
        return ''
    return str(v).strip()


def build_changes_text(current: pd.DataFrame, previous: pd.DataFrame) -> str:
    lines: list[str] = ['# Symbols Changes', '']

    if current.empty and previous.empty:
        lines.append('无变化。')
        return '\n'.join(lines) + '\n'

    if previous.empty:
        lines.append('这是第一份快照，后续运行才会开始比较变化。')
        for _, row in current.sort_values(['symbol', 'strategy']).iterrows():
            if int(row.get('candidate_count', 0) or 0) > 0:
                lines.append(f"- 初始记录: {top_pick_line(row)}")
        return '\n'.join(lines) + '\n'

    cur = current.copy()
    prev = previous.copy()
    key_cols = ['symbol', 'strategy']
    merged = cur.merge(prev, on=key_cols, how='outer', suffixes=('_cur', '_prev'))

    changes: list[str] = []
    for _, row in merged.sort_values(key_cols).iterrows():
        symbol = row['symbol']
        strategy = row['strategy']
        cur_count = int(row.get('candidate_count_cur', 0) or 0) if not pd.isna(row.get('candidate_count_cur')) else 0
        prev_count = int(row.get('candidate_count_prev', 0) or 0) if not pd.isna(row.get('candidate_count_prev')) else 0
        cur_top = clean_text(row.get('top_contract_cur', ''))
        prev_top = clean_text(row.get('top_contract_prev', ''))
        cur_annual = row.get('annualized_return_cur')
        prev_annual = row.get('annualized_return_prev')
        cur_risk = clean_text(row.get('risk_label_cur', ''))
        prev_risk = clean_text(row.get('risk_label_prev', ''))

        if prev_count == 0 and cur_count > 0:
            changes.append(f"- {symbol} {strategy}: 从无候选变为有候选，当前 Top 为 {cur_top or '-'}。")
            continue
        if prev_count > 0 and cur_count == 0:
            changes.append(f"- {symbol} {strategy}: 从有候选变为无候选。")
            continue
        if cur_count <= 0 and prev_count <= 0:
            continue
        if prev_top and cur_top and prev_top != cur_top:
            changes.append(f"- {symbol} {strategy}: Top pick 由 {prev_top} 变为 {cur_top}。")
        if (not pd.isna(prev_annual)) and (not pd.isna(cur_annual)):
            diff = float(cur_annual) - float(prev_annual)
            if abs(diff) >= float(POLICY.get('change_annual_threshold', DEFAULT_POLICY['change_annual_threshold'])):
                direction = '上升' if diff > 0 else '下降'
                changes.append(
                    f"- {symbol} {strategy}: 年化从 {pct(prev_annual)} {direction} 到 {pct(cur_annual)}。"
                )
        if prev_risk and cur_risk and prev_risk != cur_risk:
            changes.append(f"- {symbol} {strategy}: 风险标签从 {prev_risk} 变为 {cur_risk}。")

    if not changes:
        lines.append('无显著变化。')
    else:
        lines.extend(changes)

    return '\n'.join(lines) + '\n'


def snapshot_summary(current_path: Path, snapshot_path: Path):
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    if current_path.exists() and current_path.stat().st_size > 0:
        shutil.copyfile(current_path, snapshot_path)
    else:
        pd.DataFrame().to_csv(snapshot_path, index=False)


def _resolve_repo_path(*, repo_base: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = (repo_base / path).resolve()
    return path


def _resolve_previous_summary_path(*, repo_base: Path, previous_summary: str | None, state_dir: str | None) -> Path:
    if state_dir:
        sd = _resolve_repo_path(repo_base=repo_base, value=state_dir)
        return (sd / 'symbols_summary_prev.csv').resolve()
    if previous_summary:
        return _resolve_repo_path(repo_base=repo_base, value=previous_summary)
    return (repo_base / 'output' / 'state' / 'symbols_summary_prev.csv').resolve()


def _load_policy(policy_json: str | None, *, repo_base: Path) -> dict:
    return load_alert_policy(policy_json, repo_base=repo_base, resolve_repo_path_fn=_resolve_repo_path)


def _fill_capacity_fields_from_note(current: pd.DataFrame) -> pd.DataFrame:
    """补齐摘要里依赖 note 的衍生字段，保持旧行为。"""
    if current.empty or ('note' not in current.columns):
        return current

    def _parse_int_after(s: str, key: str) -> int:
        try:
            txt = str(s)
            if key not in txt:
                return 0
            parts = txt.split(key, 1)[1].strip().split()
            if not parts:
                return 0
            return int(float(parts[0]))
        except Exception:
            return 0

    mask = current.get('strategy').astype(str) == 'sell_call'
    if mask.any():
        current.loc[mask, 'cover_avail'] = current.loc[mask, 'note'].apply(lambda x: _parse_int_after(x, 'cover_avail'))
        current.loc[mask, 'shares_total'] = current.loc[mask, 'note'].apply(lambda x: _parse_int_after(x, 'shares_total'))
        current.loc[mask, 'shares_locked'] = current.loc[mask, 'note'].apply(lambda x: _parse_int_after(x, 'shares_locked'))

    mask2 = current.get('strategy').astype(str) == 'sell_put'
    if mask2.any() and 'cash_secured_used_usd' not in current.columns:
        def _parse_float_after(s: str, key: str) -> float:
            try:
                txt = str(s)
                if key not in txt:
                    return 0.0
                parts = txt.split(key, 1)[1].strip().split()
                if not parts:
                    return 0.0
                return float(parts[0])
            except Exception:
                return 0.0

        current.loc[mask2, 'cash_secured_used_usd'] = current.loc[mask2, 'note'].apply(lambda x: _parse_float_after(x, 'cash_secured_used_usd'))

    return current


def run_alert_engine(
    *,
    summary_input: str = 'output/reports/symbols_summary.csv',
    output: str = 'output/reports/symbols_alerts.txt',
    changes_output: str = 'output/reports/symbols_changes.txt',
    previous_summary: str | None = None,
    state_dir: str | None = None,
    update_snapshot: bool = False,
    policy_json: str | None = None,
) -> dict:
    """执行提醒文本构建，不包含 CLI 参数解析。"""
    global POLICY

    base = Path(__file__).resolve().parents[1]
    POLICY = _load_policy(policy_json, repo_base=base)

    summary_path = _resolve_repo_path(repo_base=base, value=summary_input)
    output_path = _resolve_repo_path(repo_base=base, value=output)
    changes_path = Path(changes_output) if str(changes_output) == '/dev/null' else _resolve_repo_path(repo_base=base, value=changes_output)
    previous_path = _resolve_previous_summary_path(repo_base=base, previous_summary=previous_summary, state_dir=state_dir)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if str(changes_path) != '/dev/null':
            changes_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    previous_path.parent.mkdir(parents=True, exist_ok=True)

    current = safe_read_csv(summary_path)
    previous = safe_read_csv(previous_path)
    current = _fill_capacity_fields_from_note(current)

    resolved_state_dir = None
    try:
        if state_dir:
            resolved_state_dir = _resolve_repo_path(repo_base=base, value=state_dir)
    except Exception:
        resolved_state_dir = None

    symbol_display_map = _load_symbol_display_map(base, state_dir=resolved_state_dir)
    alert_text = build_alert_text(current, symbol_display_map=symbol_display_map)
    changes_text = build_changes_text(current, previous)

    output_path.write_text(alert_text, encoding='utf-8')
    try:
        if str(changes_path) != '/dev/null':
            changes_path.write_text(changes_text, encoding='utf-8')
    except Exception:
        pass

    if update_snapshot:
        snapshot_summary(summary_path, previous_path)

    return {
        'alert_text': alert_text,
        'changes_text': changes_text,
        'output_path': output_path,
        'changes_path': changes_path,
        'previous_path': previous_path,
        'snapshot_updated': bool(update_snapshot),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Build alert and change text from symbols summary')
    parser.add_argument('--summary-input', default='output/reports/symbols_summary.csv')
    parser.add_argument('--output', default='output/reports/symbols_alerts.txt')
    parser.add_argument('--changes-output', default='output/reports/symbols_changes.txt')
    parser.add_argument('--previous-summary', default=None, help='Previous summary snapshot CSV (default: <state-dir>/symbols_summary_prev.csv)')
    parser.add_argument('--state-dir', default=None, help='[optional] state dir for symbols_summary_prev.csv (overrides --previous-summary when set)')
    parser.add_argument('--update-snapshot', action='store_true')
    parser.add_argument('--policy-json', default=None, help='JSON file for alert policy overrides')
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_alert_engine(
        summary_input=args.summary_input,
        output=args.output,
        changes_output=args.changes_output,
        previous_summary=args.previous_summary,
        state_dir=args.state_dir,
        update_snapshot=args.update_snapshot,
        policy_json=args.policy_json,
    )

    changes_path = str(result['changes_path'])
    if changes_path != '/dev/null':
        print(result['alert_text'])
        print(f"[DONE] alerts -> {result['output_path']}")
        print(result['changes_text'])
        print(f"[DONE] changes -> {result['changes_path']}")
    if result.get('snapshot_updated'):
        print(f"[DONE] snapshot updated -> {result['previous_path']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
