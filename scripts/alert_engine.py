#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import pandas as pd
from pandas.errors import EmptyDataError


# Alert priority policy (keep it simple):
# Layer 1 (收益率门槛) is already handled by the scanners (min_annualized_* in config).
# Layer 2 (风险/约束) is handled here:
#   - sell_put: base(CNY) cash headroom gate; then high vs medium by annualized return.
#   - sell_call: covered capacity gate; then high vs medium by annualized return.
DEFAULT_POLICY = {
    'sell_put_high_return': 0.12,
    'sell_call_high_return': 0.08,
    'change_annual_threshold': 0.02,
}

# Initialized in main() from --policy-json (or DEFAULT_POLICY).
POLICY = DEFAULT_POLICY.copy()


def safe_read_csv(path: Path) -> pd.DataFrame:
    try:
        if path.exists() and path.stat().st_size > 0:
            return pd.read_csv(path)
    except EmptyDataError:
        pass
    return pd.DataFrame()


def pct(v, digits=2) -> str:
    if pd.isna(v):
        return '-'
    return f"{float(v) * 100:.{digits}f}%"


def num(v, digits=2) -> str:
    if pd.isna(v):
        return '-'
    return f"{float(v):,.{digits}f}"


def strike_text(v) -> str:
    if pd.isna(v):
        return '-'
    v = float(v)
    return str(int(v)) if v.is_integer() else f"{v:.2f}"


def top_pick_line(row: pd.Series) -> str:
    extra = ''
    try:
        if row.get('strategy') == 'sell_call':
            shares_total = int(row.get('shares_total') or 0)
            shares_locked = int(row.get('shares_locked') or 0)
            cover_avail = int(row.get('cover_avail') or 0)
            # include suggested sell price (bid/ask/mid) + delta + currency
            parts = []
            try:
                d = row.get('delta')
                if d is not None and not pd.isna(d):
                    parts.append(f"delta {float(d):.2f}")
            except Exception:
                pass
            try:
                ccy = row.get('option_ccy')
                if ccy and isinstance(ccy, str):
                    parts.append(f"ccy {ccy.strip().upper()}")
            except Exception:
                pass
            try:
                mid = row.get('mid')
                if mid is not None and not pd.isna(mid):
                    parts.append(f"mid {float(mid):.3f}")
            except Exception:
                pass
            try:
                bid = row.get('bid')
                if bid is not None and not pd.isna(bid):
                    parts.append(f"bid {float(bid):.3f}")
            except Exception:
                pass
            try:
                ask = row.get('ask')
                if ask is not None and not pd.isna(ask):
                    parts.append(f"ask {float(ask):.3f}")
            except Exception:
                pass
            parts.append(f"cover {cover_avail}")
            parts.append(f"shares {shares_total}(-{shares_locked})")
            extra = " | " + " | ".join(parts)
        if row.get('strategy') == 'sell_put':
            used_total = float(row.get('cash_secured_used_usd') or 0.0)
            used_symbol = float(row.get('cash_secured_used_usd_symbol') or 0.0) if 'cash_secured_used_usd_symbol' in row else 0.0
            req = row.get('cash_required_usd')
            try:
                req = float(req) if req is not None and not pd.isna(req) else float(row.get('strike') or 0.0) * 100.0
            except Exception:
                req = None
            avail = row.get('cash_available_usd')
            free = row.get('cash_free_usd')
            avail_est = row.get('cash_available_usd_est')
            free_est = row.get('cash_free_usd_est')

            parts = []
            if req is not None and req > 0:
                parts.append(f"cash_req ${req:,.0f}")
            if used_total > 0:
                parts.append(f"cash_used_total ${used_total:,.0f}")
                if used_symbol > 0:
                    parts.append(f"cash_used_sym ${used_symbol:,.0f}")

            # Only show one pair: real USD cash first; else estimated.
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

            # Cash required for 1 contract (keep only requirement; do not compute remaining after buy)
            try:
                req_cny = row.get('cash_required_cny')
                req_cny_v = float(req_cny) if req_cny is not None and not pd.isna(req_cny) else None
                if req_cny_v is not None and req_cny_v > 0:
                    parts.append(f"cash_req_cny ¥{req_cny_v:,.0f}")
                elif req is not None and req > 0:
                    parts.append(f"cash_req ${float(req):,.0f}")
            except Exception:
                pass

            # include delta + suggested sell price (bid/ask/mid) and option currency for clarity
            try:
                d = row.get('delta')
                if d is not None and not pd.isna(d):
                    parts.insert(0, f"delta {float(d):.2f}")
            except Exception:
                pass
            try:
                mid = row.get('mid')
                if mid is not None and not pd.isna(mid):
                    parts.insert(0, f"mid {float(mid):.3f}")
            except Exception:
                pass
            try:
                bid = row.get('bid')
                if bid is not None and not pd.isna(bid):
                    parts.insert(0, f"bid {float(bid):.3f}")
            except Exception:
                pass
            try:
                ask = row.get('ask')
                if ask is not None and not pd.isna(ask):
                    parts.insert(0, f"ask {float(ask):.3f}")
            except Exception:
                pass
            try:
                ccy = row.get('option_ccy')
                if ccy and isinstance(ccy, str):
                    parts.insert(0, f"ccy {ccy.strip().upper()}")
            except Exception:
                pass

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
    risk = row.get('risk_label', '')

    if strategy == 'sell_put':
        # cash-aware gating:
        # Preferred: base-currency gating (CNY) using holdings base cash.
        # Fallback: USD gating using real USD cash (cash_free_usd) or USD equivalent (cash_free_usd_est).
        cash_free = None
        cash_free_est = None
        cash_req = None
        cash_free_cny = None
        cash_req_cny = None
        # Base-currency (CNY) gating first
        try:
            v = row.get('cash_free_cny')
            cash_free_cny = float(v) if v is not None and not pd.isna(v) else None
        except Exception:
            cash_free_cny = None
        try:
            v = row.get('cash_required_cny')
            cash_req_cny = float(v) if v is not None and not pd.isna(v) else None
        except Exception:
            cash_req_cny = None

        if cash_free_cny is not None and cash_req_cny is not None and cash_req_cny > cash_free_cny:
            return 'low', f'所需担保现金约 ¥{cash_req_cny:,.0f}，但当前 base(CNY) 现金余量约 ¥{cash_free_cny:,.0f}（扣占用后折算），可能无法再加仓。'

        # Fallback USD gating (only when base-currency is unavailable)
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

        if (cash_free is None) and (cash_free_cny is None) and cash_free_est is not None and cash_req is not None and cash_req > cash_free_est:
            return 'low', f'所需担保现金约 ${cash_req:,.0f}，但账户可用担保现金(折算USD)约 ${cash_free_est:,.0f}（已扣占用）；可能无法再加仓，仅供观察。'

        # Priority by return only (risk label is display-only):
        if annual >= float(POLICY.get('sell_put_high_return', DEFAULT_POLICY['sell_put_high_return'])):
            return 'high', '通过准入后，收益/风险组合较强，值得优先看。'
        # If it passed scanner's min_annualized_net_return but not high, treat as medium.
        if annual > 0:
            return 'medium', '已通过准入，可作为今日观察候选。'
        return 'low', '已通过准入，但优先级一般。'

    if strategy == 'sell_call':
        # account-aware gating: if no covered capacity, do not promote to high/medium
        try:
            cover_avail = int(row.get('cover_avail') or 0)
        except Exception:
            cover_avail = 0
        if cover_avail <= 0:
            return 'low', '当前富途可覆盖张数为 0（可能已占用或持仓不足），仅供观察。'

        if annual >= float(POLICY.get('sell_call_high_return', DEFAULT_POLICY['sell_call_high_return'])):
            return 'high', '通过准入后，权利金回报与行权空间比较平衡。'
        if annual > 0:
            return 'medium', '已通过准入，可作为 sell call 备选。'
        return 'low', '已通过准入，但优先级一般。'

    return None, ''


def build_alert_text(summary: pd.DataFrame) -> str:
    lines: list[str] = ['# Symbols Alerts', '']

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
        line = f"- {top_pick_line(row)} | {comment}"
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


def main():
    global POLICY

    parser = argparse.ArgumentParser(description='Build alert and change text from symbols summary')
    parser.add_argument('--summary-input', default='output/reports/symbols_summary.csv')
    parser.add_argument('--output', default='output/reports/symbols_alerts.txt')
    parser.add_argument('--changes-output', default='output/reports/symbols_changes.txt')
    parser.add_argument('--previous-summary', default='output/state/symbols_summary_prev.csv')
    parser.add_argument('--update-snapshot', action='store_true')
    parser.add_argument('--policy-json', default=None, help='JSON file for alert policy overrides')
    args = parser.parse_args()

    if args.policy_json:
        try:
            p = Path(args.policy_json)
            if not p.is_absolute():
                base0 = Path(__file__).resolve().parents[1]
                p = (base0 / p).resolve()
            if p.exists() and p.stat().st_size > 0:
                data = json.loads(p.read_text(encoding='utf-8'))
                if isinstance(data, dict):
                    POLICY = {**DEFAULT_POLICY, **data}
        except Exception:
            POLICY = DEFAULT_POLICY.copy()

    base = Path(__file__).resolve().parents[1]
    summary_path = base / args.summary_input
    output_path = base / args.output
    changes_path = base / args.changes_output
    previous_path = base / args.previous_summary

    output_path.parent.mkdir(parents=True, exist_ok=True)
    changes_path.parent.mkdir(parents=True, exist_ok=True)
    previous_path.parent.mkdir(parents=True, exist_ok=True)

    current = safe_read_csv(summary_path)
    previous = safe_read_csv(previous_path)

    # Parse sell_call cover capacity out of note (added by pipeline), so alerts/notifications become account-aware.
    if not current.empty and 'note' in current.columns:
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

        # sell_put: prefer structured column; fallback to parsing note if needed
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

    alert_text = build_alert_text(current)
    changes_text = build_changes_text(current, previous)

    output_path.write_text(alert_text, encoding='utf-8')
    changes_path.write_text(changes_text, encoding='utf-8')

    print(alert_text)
    print(f'[DONE] alerts -> {output_path}')
    print(changes_text)
    print(f'[DONE] changes -> {changes_path}')

    if args.update_snapshot:
        snapshot_summary(summary_path, previous_path)
        print(f'[DONE] snapshot updated -> {previous_path}')


if __name__ == '__main__':
    main()
