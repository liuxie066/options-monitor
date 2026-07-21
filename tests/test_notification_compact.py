from __future__ import annotations

import pytest

from tests.notification_format_assertions import assert_mobile_flat_markdown


def test_build_notification_block_compact_sell_put() -> None:
    from src.application.notify_symbols import _build_notification_block_compact

    out = _build_notification_block_compact(
        symbol_name="腾讯",
        action_label="卖Put",
        contract="2026-04-29 460",
        income_line="- 收益: 权利金=2.3 | 年化 12% | 净收 2300",
        contract_line="- 合约: 行权价=460 | 数量=1张(默认) | DTE=29",
        risk_line="- 风控: 风险=保守 | delta=0.25 | IV=0.45",
        detail_line="- 资金: 保证金占用=¥46000",
        note="通过准入",
        suggestion="2.3",
    )

    assert "🟢 Put 腾讯 460P · 04-29 · 挂单 2.3" in out
    assert "收益｜权利金 2.3 · 年化 12% · 29天" in out
    assert "风险｜保守 · Δ 0.25 · 担保 ¥46000" in out
    assert "---" not in out


def test_build_notification_block_compact_yield_enhancement() -> None:
    from src.application.notify_symbols import _build_notification_block_compact

    out = _build_notification_block_compact(
        symbol_name="英伟达",
        action_label="组合收益",
        contract="2026-06-19 95+110",
        income_line="- 收益: 组合净权利金=95 | 年化 8% | 场景评分=0.82",
        contract_line="- 组合: Put=95 | Call=110 | DTE=45",
        risk_line="- 风控: 风险=中性 | Call delta=0.15 | Call ask=1.2",
        detail_line="- 预期波动: expected_move=5.2 | IV=0.35",
        note="组合收益推荐",
    )

    assert "💎 组合·同期 英伟达 95P+110C · 06-19" in out
    assert "收益｜净权利金 95 · 年化 8.0% · 45天" in out
    assert "风险｜中性 · Call Δ 0.15" in out
    assert "评分" not in out
    assert "预期波动" not in out
    assert "---" not in out


def test_format_alert_line_compact_sell_put() -> None:
    from src.application.notify_symbols import _format_alert_line_compact

    line = "腾讯 | sell_put | 2026-04-29 460 | 年化 12% | 净收入 2300 | DTE 29 | Strike 460 | mid 2.3 | ccy USD | cash_req_cny ¥46000 | delta 0.25 | 风险 保守 | 通过准入"
    out = _format_alert_line_compact(line, account_label="lx")

    assert "🟢 Put 腾讯" in out
    assert "年化 12%" in out
    assert "---" not in out
    assert "###" not in out


def test_format_alert_line_compact_sell_put_shows_event_risk() -> None:
    from src.application.notify_symbols import _format_alert_line_compact

    line = "腾讯 | sell_put | 2026-04-29 460 | 年化 12% | 净收入 2300 | DTE 29 | Strike 460 | mid 2.3 | ccy USD | cash_req_cny ¥46000 | delta 0.25 | event earnings@2026-04-20 | 风险 保守 | 通过准入"
    out = _format_alert_line_compact(line, account_label="lx")

    assert "🟢 Put 腾讯" in out
    assert "事件 earnings@2026-04-20" in out
    assert "---" not in out


def test_fmt_date_compact_same_year() -> None:
    from src.application.notify_symbols import _fmt_date_compact

    result = _fmt_date_compact("2026-04-29 460")
    assert result == "@ 04-29"


def test_fmt_pct_compact() -> None:
    from src.application.notify_symbols import _fmt_pct_compact

    assert _fmt_pct_compact("12%") == "12%"
    assert _fmt_pct_compact("8.5%") == "8.5%"
    assert _fmt_pct_compact("0.05") == "5.0%"


def test_build_notification_compact_style() -> None:
    from src.application.notify_symbols import build_notification

    alerts_text = "## 高优先级\n腾讯 | sell_put | 2026-04-29 460 | 年化 12% | 净收入 2300 | DTE 29 | Strike 460 | mid 2.3 | ccy USD | 风险 保守 | 通过准入\n"
    out = build_notification("", alerts_text, render_style="compact")

    assert "### Put" in out
    assert "🟢 Put 腾讯" in out
    assert "---" not in out


def test_build_notification_defaults_to_compact_and_rejects_unknown_renderer() -> None:
    from src.application.notify_symbols import build_notification

    alerts_text = "## 高优先级\n腾讯 | sell_put | 2026-04-29 460 | 年化 12% | 净收入 2300 | DTE 29 | Strike 460 | mid 2.3 | ccy USD | 风险 保守 | 通过准入\n"

    assert build_notification("", alerts_text) == build_notification("", alerts_text, render_style="compact")
    with pytest.raises(ValueError, match="compact, legacy"):
        build_notification("", alerts_text, render_style="unknown")


def test_build_notification_compact_style_uses_markdown_enhancement_heading() -> None:
    from src.application.notify_symbols import build_notification

    alerts_text = (
        "## 高优先级\n"
        "NVDA | yield_enhancement | 2026-06-19 95P+110C | 年化 8% | DTE 45 | 保守 | "
        "mid 0.950 | put_bid 2.150 | net_credit 95 | scenario_score 0.82 | put_strike 95 | call_strike 110 | call_delta 0.15 | call_ask 1.2 | 通过准入\n"
    )
    out = build_notification("", alerts_text, render_style="compact")

    assert "### 组合" in out
    assert "· 卖2.15/买1.2" in out
    assert "卖0.950/买1.2" not in out


def test_build_notification_legacy_style_uses_flat_fields() -> None:
    from src.application.notify_symbols import build_notification

    alerts_text = "## 高优先级\n腾讯 | sell_put | 2026-04-29 460 | 年化 12% | 净收入 2300 | DTE 29 | Strike 460 | mid 2.3 | ccy USD | 风险 保守 | 通过准入\n"
    with pytest.warns(DeprecationWarning, match="Legacy Tick renderer"):
        out = build_notification("", alerts_text, render_style="legacy")

    assert "Put" in out
    assert "**[当前账户] 腾讯｜卖Put｜2026-04-29 460**" in out
    assert "收益｜" in out
    assert "---" not in out


def test_build_notification_compact_keeps_medium_strategy_with_total_limit() -> None:
    from src.application.notify_symbols import build_notification

    put_lines = [
        (
            f"PUT{i} | sell_put | 2026-06-19 10{i}P | 年化 {20 - i:.2f}% | 净收入 {100 + i:.2f} | "
            f"DTE 30 | Strike 10{i} | 中性 | ccy USD | mid 1.000 | 通过准入后，收益/风险组合较强，值得优先看。"
        )
        for i in range(1, 7)
    ]
    medium_call = (
        "CALL1 | sell_call | 2026-06-19 180C | 年化 6.50% | 净收入 80.00 | "
        "DTE 30 | Strike 180 | 保守 | ccy USD | mid 0.800 | cover 1 | shares 100(-0) | 已通过准入，可作为 Covered Call 备选。"
    )
    alerts_text = (
        "## 高优先级\n"
        + "\n".join(put_lines)
        + "\n\n## 中优先级\n"
        + medium_call
        + "\n"
    )

    out = build_notification("", alerts_text, render_style="compact")

    assert out.count("🟢 Put") == 5
    assert "PUT5" in out
    assert "PUT6" not in out
    assert "CALL1" in out
    assert "🟢 Call CALL1" in out
    assert out.index("### Put") < out.index("### Call")


def test_render_markdown_compact_long_call_metrics() -> None:
    from src.application.close_advice_runner import render_markdown_compact

    rows = [
        {
            "account": "sy",
            "symbol": "9992.HK",
            "option_type": "call",
            "position_side": "long",
            "expiration": "2026-07-30",
            "strike": 167.5,
            "tier": "medium",
            "evaluation_status": "priced",
            "dte": 59,
            "premium": 6.38,
            "close_mid": 19.0,
            "bid": 18.8,
            "ask": 19.2,
            "realized_if_close": 2458.0,
            "remaining_premium": 3760.0,
            "long_call_value_ratio": 2.978,
            "long_call_cost_basis": 1276.0,
            "long_call_current_value": 3760.0,
            "currency": "HKD",
            "strategy": "yield_enhancement",
            "leg_role": "enhancement_call",
            "risk_model": "long_call_convexity",
            "close_action": "sell_call_take_profit",
        }
    ]

    md = render_markdown_compact(rows, notify_levels={"strong", "medium"}, max_items=5)

    assert "🟠 卖Call止盈 9992.HK Call 167.5C @ 07-30" in md
    assert "- 现值/成本 3.0x · 59天 · 浮盈 +198%" in md
    assert "- 建议出价 ¥19 · 买一/卖一 ¥18.8/¥19.2 · 收益 ¥2,458（余 ¥3,760）" in md
    assert "已锁定 -" not in md
    assert "余年化 -" not in md


def test_build_account_message_compact() -> None:
    from src.application.multi_tick.misc import AccountResult
    from src.application.multi_tick.notify_format import build_account_message_compact

    notif = (
        "Put\n"
        "腾讯 卖Put 2026-04-29 460P\n"
        "担保 1张 余量 ¥-100\n"
        "\n"
        "### [lx] 平仓建议\n"
        "- NVDA Put 2026-06-19 150P · 强烈建议平仓\n"
        "- 已锁定: 85.0% | 剩余DTE=14 | 剩余收益年化=5.0%\n"
    )

    message = build_account_message_compact(
        AccountResult(
            account="lx",
            ran_scan=True,
            should_notify=True,
            decision_reason="dense",
            notification_text=notif,
        ),
        now_bj="2026-05-12 22:31:00",
        cash_footer_lines=[
            "**💰 现金 CNY**",
            "- **LX** 总现金折算 ¥1,000 (CNY) | 担保后可用 ¥200 (CNY)",
            "",
            "> 截至 2026-05-12 22:30:00 北京时间",
        ],
    )

    assert "# OM · 决策简报 · lx" in message
    assert "时间｜2026-05-12 22:31:00 北京时间" in message
    assert "结论｜Put 1 · Call 0 · 平仓 1" in message
    assert "## 候选\nPut\n- 腾讯 卖Put 2026-04-29 460P" in message
    assert "## 持仓\nNVDA Put 2026-06-19 150P · 强烈建议平仓" in message
    assert "## 资金\n账户｜LX 总现金 ¥1,000 (CNY)｜担保后 ¥200 (CNY)" in message
    assert "数据｜截至 2026-05-12 22:30:00 北京时间" in message
    assert "──────────────" not in message
    assert_mobile_flat_markdown(message)


def test_build_account_message_compact_without_close_advice() -> None:
    from src.application.multi_tick.misc import AccountResult
    from src.application.multi_tick.notify_format import build_account_message_compact

    notif = (
        "Put\n"
        "腾讯 卖Put 2026-04-29 460P\n"
    )

    message = build_account_message_compact(
        AccountResult(
            account="sy",
            ran_scan=True,
            should_notify=True,
            decision_reason="dense",
            notification_text=notif,
        ),
        now_bj="2026-05-12 22:31:00",
        cash_footer_lines=None,
    )

    assert "# OM · 决策简报 · sy" in message
    assert "结论｜Put 1 · Call 0 · 平仓 0" in message
    assert "## 持仓\n当前没有平仓建议。" in message
    assert "\n## 资金\n" not in message
    assert_mobile_flat_markdown(message)
