from __future__ import annotations


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

    assert "🟢 卖Put 腾讯 460P @ 04-29" in out
    assert "权利金 2.3" in out or "年化 12%" in out
    assert "---" not in out


def test_build_notification_block_compact_yield_enhancement() -> None:
    from src.application.notify_symbols import _build_notification_block_compact

    out = _build_notification_block_compact(
        symbol_name="英伟达",
        action_label="收益增强",
        contract="2026-06-19 95+110",
        income_line="- 收益: 组合净权利金=95 | 年化 8% | 场景评分=0.82",
        contract_line="- 组合: Put=95 | Call=110 | DTE=45",
        risk_line="- 风控: 风险=中性 | Call delta=0.15 | Call ask=1.2",
        detail_line="- 预期波动: expected_move=5.2 | IV=0.35",
        note="收益增强推荐",
    )

    assert "💎 收益增强 英伟达 95P+110C @ 06-19" in out
    assert "净权利金 95" in out
    assert "评分 0.820" in out
    assert "Call Δ 0.15" in out
    assert "ask 1.2" in out
    assert "预期波动 5.2" in out
    assert "IV 0.35" in out
    assert "---" not in out


def test_format_alert_line_compact_sell_put() -> None:
    from src.application.notify_symbols import _format_alert_line_compact

    line = "腾讯 | sell_put | 2026-04-29 460 | 年化 12% | 净收入 2300 | DTE 29 | Strike 460 | mid 2.3 | ccy USD | cash_req_cny ¥46000 | delta 0.25 | 风险 保守 | 通过准入"
    out = _format_alert_line_compact(line, account_label="lx")

    assert "🟢 卖Put 腾讯" in out
    assert "年化 12%" in out
    assert "---" not in out
    assert "###" not in out


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
    assert "🟢 卖Put 腾讯" in out
    assert "---" not in out


def test_build_notification_compact_style_uses_markdown_enhancement_heading() -> None:
    from src.application.notify_symbols import build_notification

    alerts_text = (
        "## 高优先级\n"
        "NVDA | yield_enhancement | 2026-06-19 95P+110C | 年化 8% | DTE 45 | 保守 | "
        "mid 0.950 | put_bid 2.150 | net_credit 95 | scenario_score 0.82 | put_strike 95 | call_strike 110 | call_delta 0.15 | call_ask 1.2 | 通过准入\n"
    )
    out = build_notification("", alerts_text, render_style="compact")

    assert "### Enhancement" in out
    assert "🎯卖2.150/买1.2" in out
    assert "卖0.950/买1.2" not in out


def test_build_notification_legacy_style_unchanged() -> None:
    from src.application.notify_symbols import build_notification

    alerts_text = "## 高优先级\n腾讯 | sell_put | 2026-04-29 460 | 年化 12% | 净收入 2300 | DTE 29 | Strike 460 | mid 2.3 | ccy USD | 风险 保守 | 通过准入\n"
    out = build_notification("", alerts_text, render_style="legacy")

    assert "Put" in out
    assert "### [当前账户] 腾讯 · 卖Put" in out
    assert "---" in out


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
        "DTE 30 | Strike 180 | 保守 | ccy USD | mid 0.800 | cover 1 | shares 100(-0) | 已通过准入，可作为 sell call 备选。"
    )
    alerts_text = (
        "## 高优先级\n"
        + "\n".join(put_lines)
        + "\n\n## 中优先级\n"
        + medium_call
        + "\n"
    )

    out = build_notification("", alerts_text, render_style="compact")

    assert out.count("卖Put") == 4
    assert "PUT5" not in out
    assert "PUT6" not in out
    assert "CALL1" in out
    assert out.count("卖Call") == 1
    assert out.index("### Put") < out.index("### Call")


def test_render_markdown_compact_close_advice() -> None:
    from src.application.close_advice_runner import render_markdown_compact

    rows = [
        {
            "account": "lx",
            "symbol": "NVDA",
            "option_type": "put",
            "expiration": "2026-06-19",
            "strike": 150.0,
            "tier": "optimizer_switch",
            "tier_label": "强烈建议平仓换仓",
            "evaluation_status": "priced",
            "capture_ratio": 0.85,
            "dte": 14,
            "remaining_annualized_return": 0.05,
            "premium": 2.5,
            "close_mid": 0.4,
            "realized_if_close": 210.0,
            "remaining_premium": 40.0,
            "currency": "USD",
            "reason": "switch",
            "optimizer_tier": "optimizer_switch",
            "effective_annualized_return": 0.05,
            "alternative_annualized_return": 0.12,
            "tail_risk_score": 0.045,
        }
    ]

    md = render_markdown_compact(rows, notify_levels={"strong", "medium"}, max_items=5)

    assert "### [lx] 平仓建议 (1)" in md
    assert "🔴 换仓 NVDA Put 150P @ 06-19" in md
    assert "已锁定 85%" in md
    assert "💡 持有 5.0% → 替代 12%" in md
    assert "---" not in md
    assert "理由: switch" not in md


def test_render_markdown_compact_hold() -> None:
    from src.application.close_advice_runner import render_markdown_compact

    rows = [
        {
            "account": "lx",
            "symbol": "TSLA",
            "option_type": "put",
            "expiration": "2026-07-15",
            "strike": 200.0,
            "tier": "optimizer_hold",
            "tier_label": "建议继续持有",
            "evaluation_status": "priced",
            "capture_ratio": 0.60,
            "dte": 45,
            "remaining_annualized_return": 0.08,
            "premium": 3.0,
            "close_mid": 1.2,
            "realized_if_close": 180.0,
            "remaining_premium": 120.0,
            "currency": "USD",
            "reason": "hold_deep_otm",
            "optimizer_tier": "optimizer_hold",
            "effective_annualized_return": 0.08,
            "risk_adjusted_return": 0.40,
            "tail_risk_score": 0.015,
            "delta": 0.03,
        }
    ]

    md = render_markdown_compact(rows, notify_levels={"optimizer_hold"}, max_items=5)

    assert "🟢 持有 TSLA Put 200P" in md
    assert "💡 风险调整 40%" in md


def test_build_account_message_compact() -> None:
    from src.application.multi_tick.misc import AccountResult
    from src.application.multi_tick.notify_format import build_account_message_compact

    notif = (
        "Put\n"
        "腾讯 卖Put 2026-04-29 460P\n"
        "担保 1张 余量 ¥-100\n"
        "\n"
        "### [lx] 平仓建议\n"
        "- NVDA Put 2026-06-19 150P · 强烈建议平仓换仓\n"
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
        cash_footer_lines=["LX 持有 ¥1,000 (CNY) | 可用 ¥200 (CNY)"],
    )

    assert "⏰ 北京时间 2026-05-12 22:31:00" in message
    assert "📋 本轮概览" in message
    assert "Put 1 · Call 0" in message
    assert "──────────────" in message
    assert "💰 资金概览" in message
    assert "  LX 持有 ¥1,000 (CNY) | 可用 ¥200 (CNY)" in message
    assert "🔴 优化器" in message


def test_build_account_message_compact_without_optimizer() -> None:
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

    assert "📋 本轮概览" in message
    assert "🔴 优化器" not in message
    assert "💰 资金概览" not in message
