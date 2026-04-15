"""Notification markdown rendering regression tests."""

from __future__ import annotations

import pandas as pd


def _render_via_alert_engine(summary_row: dict) -> str:
    from domain.domain import normalize_processor_row
    from scripts.alert_engine import build_alert_text
    from scripts.notify_symbols import build_notification

    normalized = normalize_processor_row(summary_row)
    df = pd.DataFrame([normalized])
    alerts = build_alert_text(df)
    return build_notification("", alerts, account_label="SY")


def test_notify_symbols_markdown_put_layout() -> None:
    from scripts.notify_symbols import build_notification

    alerts = """# Symbols Alerts

## 高优先级
- [腾讯](0700.HK) | sell_put | 2026-04-29 460P | 年化 17.21% | 净收入 557.00 | DTE 26 | Strike 460 | 中性 | ccy HKD | ask 5.860 | bid 5.580 | mid 5.720 | delta -0.23 | cash_req_cny ¥110,720 | 通过准入后，收益/风险组合较强，值得优先看。
"""
    out = build_notification("", alerts, account_label="LX")

    expected = """Put

### [lx] 腾讯 · 卖Put
- 腾讯 卖Put 2026-04-29 460P
- 收益: 权利金=5.720 (HKD) | 年化 17.21% | 净收 557
- 合约: 行权价=460 | 数量=1张(默认) | DTE=26
- 风控: 风险=中性 | delta=-0.23 | IV=缺失(告警未提供iv)
- 资金: 保证金占用=¥110,720 (CNY)
- 操作: 建议挂单=5.720
- 备注: 通过准入后，收益/风险组合较强，值得优先看。
---
"""
    assert out == expected


def test_notify_symbols_no_candidate_message_is_heartbeat() -> None:
    from scripts.notify_symbols import build_notification

    out = build_notification('', '', account_label='LX')

    assert '监控正常触发：本轮无候选。' in out
    assert '今日无需要主动提醒的内容。' not in out


def test_notify_symbols_markdown_put_layout_missing_fields_have_reasons() -> None:
    from scripts.notify_symbols import build_notification

    alerts = """# Symbols Alerts

## 高优先级
- NVDA | sell_put | 2026-06-18 156P | 年化 - | 净收入 524.99 | DTE 76 | Strike nan | nan | ccy USD | ask 5.450 | bid 5.100 | mid 5.275 | delta nan | iv nan | cash_req - | 通过准入后，收益/风险组合较强，值得优先看。
"""
    out = build_notification("", alerts, account_label="SY")

    assert "nan" not in out.lower()
    assert "行权价=156" in out
    assert "年化 缺失(告警未提供年化)" in out
    assert "保证金占用=缺失(告警未提供cash_req_cny/cash_req)" in out
    assert "delta=缺失(告警未提供delta)" in out
    assert "IV=缺失(告警未提供iv)" in out


def test_notify_symbols_markdown_call_layout_and_changes() -> None:
    from scripts.notify_symbols import build_notification

    alerts = """# Symbols Alerts

## 高优先级
- [英伟达](NVDA) | sell_call | 2026-06-18 180C | 年化 12.30% | 净收入 240.40 | DTE 44 | Strike 180 | 保守 | ccy USD | ask 2.500 | bid 2.300 | mid 2.400 | delta 0.16 | cover 2 | shares 200(-0) | 已通过准入，可作为 sell call 备选。
"""
    changes = """# Symbols Changes

- NVDA sell_call: Top pick 由 2026-06-18 175C 变为 2026-06-18 180C。
"""
    out = build_notification(changes, alerts, account_label="SY")

    assert "### [sy] 英伟达 · 卖Call" in out
    assert "数量=2张(可覆盖)" in out
    assert "变化" in out
    assert "- NVDA sell_call: Top pick 由 2026-06-18 175C 变为 2026-06-18 180C。" in out


def test_notify_symbols_markdown_call_layout_missing_fields_have_reasons() -> None:
    from scripts.notify_symbols import build_notification

    alerts = """# Symbols Alerts

## 高优先级
- NVDA | sell_call | 2026-06-18 180C | 年化 - | 净收入 240.40 | DTE 44 | Strike nan | 保守 | ccy USD | ask 2.500 | bid 2.300 | mid 2.400 | delta nan | cover nan | shares nan | 已通过准入，可作为 sell call 备选。
"""
    out = build_notification("", alerts, account_label="SY")

    assert "nan" not in out.lower()
    assert "行权价=180" in out
    assert "年化 缺失(告警未提供年化)" in out
    assert "delta=缺失(告警未提供delta)" in out
    assert "IV=缺失(告警未提供iv)" in out
    assert "覆盖: 缺失(告警未提供cover) 张 | shares 缺失(告警未提供shares)" in out


def test_notify_symbols_markdown_put_chain_uses_upstream_fields_when_available() -> None:
    out = _render_via_alert_engine(
        {
            "symbol": "0700.HK",
            "strategy": "sell_put",
            "candidate_count": 1,
            "top_contract": "2026-04-29 460P",
            "annualized_return": 0.1721,
            "net_income": 557.00,
            "dte": 26,
            "strike": 460.0,
            "risk_label": "中性",
            "delta": -0.23,
            "iv": 0.41,
            "cash_required_cny": 110720.0,
            "mid": 5.72,
            "bid": 5.58,
            "ask": 5.86,
            "option_ccy": "HKD",
        }
    )

    assert "保证金占用=¥110,720 (CNY)" in out
    assert "delta=-0.23" in out
    assert "IV=41.00%" in out
    assert "告警未提供cash_req_cny/cash_req" not in out
    assert "告警未提供delta" not in out
    assert "告警未提供iv" not in out


def test_notify_symbols_markdown_put_falls_back_to_usd_margin_when_cny_margin_missing() -> None:
    out = _render_via_alert_engine(
        {
            "symbol": "0700.HK",
            "strategy": "sell_put",
            "candidate_count": 1,
            "top_contract": "2026-04-29 460P",
            "annualized_return": 0.1721,
            "net_income": 557.00,
            "dte": 26,
            "strike": 460.0,
            "risk_label": "中性",
            "delta": -0.23,
            "cash_required_usd": 58880.0,
            "cash_free_cny": 200000.0,
            "mid": 5.72,
            "option_ccy": "HKD",
        }
    )

    assert "保证金占用=$58,880 (USD)" in out
    assert "告警未提供cash_req_cny/cash_req" not in out


def test_notify_symbols_markdown_put_chain_missing_fields_keep_reasons() -> None:
    out = _render_via_alert_engine(
        {
            "symbol": "NVDA",
            "strategy": "sell_put",
            "candidate_count": 1,
            "top_contract": "2026-06-18 156P",
            "annualized_return": 0.1,
            "net_income": 524.99,
            "dte": 76,
            "strike": 156.0,
            "risk_label": "中性",
        }
    )

    assert "保证金占用=缺失(告警未提供cash_req_cny/cash_req)" in out
    assert "delta=缺失(告警未提供delta)" in out
    assert "IV=缺失(告警未提供iv)" in out
