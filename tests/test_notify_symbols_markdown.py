"""Notification markdown rendering regression tests."""

from __future__ import annotations


def test_notify_symbols_markdown_put_layout() -> None:
    from scripts.notify_symbols import build_notification

    alerts = """# Symbols Alerts

## 高优先级
- [腾讯](0700.HK) | sell_put | 2026-04-29 460P | 年化 17.21% | 净收入 557.00 | DTE 26 | Strike 460 | 中性 | ccy HKD | ask 5.860 | bid 5.580 | mid 5.720 | delta -0.23 | cash_req_cny ¥110,720 | 通过准入后，收益/风险组合较强，值得优先看。
"""
    out = build_notification("", alerts, account_label="LX")

    expected = """Put

### [LX] 腾讯 | 到期 2026-04-29 | 策略 卖Put
- 腾讯 卖Put 2026-04-29 460P
- 指标: 方向=卖Put | 行权价=460 | 数量=1张(默认) | 权利金=5.720 (HKD) | 年化 17.21% | 净收 557 | 保证金占用=¥110,720 (CNY) | delta=-0.23 | IV=-
- 建议挂单: 5.720
> 次要信息
> 风险: 中性
> DTE: 26
---
"""
    assert out == expected


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

    assert "### [SY] 英伟达 | 到期 2026-06-18 | 策略 卖Call" in out
    assert "数量=2张(可覆盖)" in out
    assert "变化" in out
    assert "- NVDA sell_call: Top pick 由 2026-06-18 175C 变为 2026-06-18 180C。" in out
