from __future__ import annotations


def test_account_message_is_plain_text_for_weixin() -> None:
    from src.application.multi_tick.misc import AccountResult
    from src.application.multi_tick.notify_format import build_account_message

    notif = (
        "Put\n"
        "腾讯 卖Put 2026-04-29 460P\n"
        "担保 1张 余量 ¥-100\n"
        "\n"
        "Covered Call\n"
        "英伟达 Covered Call 2026-06-18 180C\n"
        "覆盖 1张 cover 1\n"
    )
    message = build_account_message(
        AccountResult(
            account='lx',
            ran_scan=True,
            should_notify=True,
            decision_reason='dense',
            notification_text=notif,
        ),
        now_bj='2026-04-08 22:31:00',
        cash_footer_lines=["💰 现金 CNY", "LX 持有 ¥1,000 (CNY) | 可用 ¥200 (CNY)"],
    )

    assert "# 📊 Options Monitor\n## 账户提醒（lx）" in message
    assert "北京时间 2026-04-08 22:31:00" in message
    assert "### 账户 lx · 本轮候选\n- Put 1 / Covered Call 1" in message
    assert "LX 持有 ¥1,000 (CNY) | 可用 ¥200 (CNY)" in message
    assert "**" not in message
    assert "\n>" not in message


def test_account_message_skips_accounts_without_notification_text() -> None:
    from src.application.multi_tick.misc import AccountResult
    from src.application.multi_tick.notify_format import build_account_message

    message = build_account_message(
        AccountResult(
            account='sy',
            ran_scan=True,
            should_notify=False,
            decision_reason='window_closed',
            notification_text='Put\n无须处理',
        ),
        now_bj='2026-04-08 22:31:00',
        cash_footer_lines=None,
    )

    assert message == ''


def test_account_message_counts_yield_enhancement_when_present() -> None:
    from src.application.multi_tick.misc import AccountResult
    from src.application.multi_tick.notify_format import build_account_message

    notif = (
        "Put\n"
        "腾讯 卖Put 2026-04-29 460P\n"
        "\n"
        "Combo Yield\n"
        "英伟达 组合收益 2026-06-19 95P+110C\n"
    )

    message = build_account_message(
        AccountResult(
            account='lx',
            ran_scan=True,
            should_notify=True,
            decision_reason='dense',
            notification_text=notif,
        ),
        now_bj='2026-04-08 22:31:00',
        cash_footer_lines=[],
    )

    assert "### 账户 lx · 本轮候选\n- Put 1 / Covered Call 0 / Combo Yield 1" in message


def test_compact_account_overview_ignores_reject_summary_strategy_names() -> None:
    from src.application.multi_tick.misc import AccountResult
    from src.application.multi_tick.notify_format import build_account_message_compact

    notif = (
        "📋 本轮扫描完成，暂无符合条件的候选。\n\n"
        "### 拒绝摘要\n"
        "- 通过 184 条；过滤 279 条\n"
        "- 主要原因：波动率边际不足 127、基础条件不符 49、其他 31\n"
    )

    message = build_account_message_compact(
        AccountResult(
            account='lx',
            ran_scan=True,
            should_notify=True,
            decision_reason='dense',
            notification_text=notif,
        ),
        now_bj='2026-06-01 10:00:24',
        cash_footer_lines=[],
    )

    assert "Put 0 · Call 0 · 平仓 0\n" in message
    assert "## 候选\n- 无符合承保条件候选" in message
    assert "- 主要过滤：" not in message
    assert "通过 184 条" not in message
    assert "组合收益 1" not in message


def test_compact_account_overview_counts_candidate_lines_only() -> None:
    from src.application.multi_tick.misc import AccountResult
    from src.application.multi_tick.notify_format import build_account_message_compact

    notif = (
        "### Covered Call\n\n"
        "🟢 Covered Call 英伟达 180C @ 06-18 | 🎯建议挂单 2.4\n"
        "- 权利金 2.4USD · 年化 12% · 44天\n\n"
        "### Combo Yield\n\n"
        "💎 组合收益 英伟达 95P+110C @ 06-19 | 🎯卖1.2/买0.4\n"
        "- 净权利金 0.8USD · 年化 15% · 45天\n\n"
        "### 拒绝摘要\n"
        "- 主要原因：波动率边际不足 9、组合收益不成立 2\n"
    )

    message = build_account_message_compact(
        AccountResult(
            account='lx',
            ran_scan=True,
            should_notify=True,
            decision_reason='dense',
            notification_text=notif,
        ),
        now_bj='2026-06-01 10:00:24',
        cash_footer_lines=[],
    )

    assert "Put 0 · Call 1 · 组合 1 · 平仓 0\n" in message
    assert "## 候选\nCall" in message
    assert "- 主要过滤：" not in message


def test_compact_account_overview_does_not_count_combo_legs_as_put_or_call() -> None:
    from src.application.multi_tick.misc import AccountResult
    from src.application.multi_tick.notify_format import build_account_message_compact

    notif = (
        "### 组合\n"
        "🧩 组合·跨期 PDD 100P+120C\n"
        "- Put 卖 100P · 08-21/35天 · bid 2.35 · 估算净收 228 USD\n"
        "- Call 买 120C · 10-16/91天 · ask 2.1 · Δ 0.31 · 估算成本 218 USD\n"
        "- 组合 覆盖 104.59% · 净现金流 10 USD\n"
    )

    message = build_account_message_compact(
        AccountResult(
            account='lx',
            ran_scan=True,
            should_notify=True,
            decision_reason='dense',
            notification_text=notif,
        ),
        now_bj='2026-07-17 10:00:24',
        cash_footer_lines=[],
    )

    assert "Put 0 · Call 0 · 组合 1 · 平仓 0" in message


def test_compact_account_overview_does_not_count_gap_as_close_action() -> None:
    from src.application.multi_tick.misc import AccountResult
    from src.application.multi_tick.notify_format import build_account_message_compact

    notif = (
        "### Covered Call\n\n"
        "🟢 Covered Call 英伟达 180C @ 06-18 | 🎯建议挂单 2.4\n"
        "- 权利金 2.4USD · 年化 12% · 44天\n\n"
        "### [lx] 平仓建议 (0)\n"
        "- 本次无 strong/medium 平仓建议\n"
        "- 待补数据:\n"
        "- 0700.HK Call 2026-07-30 520.00C · 无法评估 | 收益捕获平仓仅支持 open short put/call\n"
        "- 0700.HK Put 2026-07-30 440.00P · 无法评估 | 价差过宽\n"
        "- 9992.HK Call 2026-07-30 200.00C · 无法评估 | 持仓对应合约已定位，但当前未取得可用价格，暂无法评估平仓建议\n"
    )

    message = build_account_message_compact(
        AccountResult(
            account='lx',
            ran_scan=True,
            should_notify=True,
            decision_reason='dense',
            notification_text=notif,
        ),
        now_bj='2026-06-01 15:50:24',
        cash_footer_lines=[],
    )

    assert "Put 0 · Call 1 · 平仓 0 · 待补 1" in message
    assert "## 持仓\n- 无平仓建议\n- 待补:" in message
    assert "9992.HK Call 2026-07-30 200.00C · 持仓对应合约已定位，但当前未取得可用价格" in message
    assert "0700.HK Call 2026-07-30 520.00C" not in message
    assert "价差过宽" not in message


def test_compact_account_overview_hides_non_data_gap_count() -> None:
    from src.application.multi_tick.misc import AccountResult
    from src.application.multi_tick.notify_format import build_account_message_compact

    notif = (
        "📋 本轮扫描完成，暂无符合条件的候选。\n\n"
        "### [lx] 平仓建议 (0)\n"
        "- 本次无 strong/medium 平仓建议\n"
        "- 待补数据:\n"
        "- 0700.HK Call 2026-07-30 520.00C · 无法评估 | 收益捕获平仓仅支持 open short put/call\n"
        "- 0700.HK Put 2026-07-30 440.00P · 无法评估 | 价差过宽\n"
    )

    message = build_account_message_compact(
        AccountResult(
            account='lx',
            ran_scan=True,
            should_notify=True,
            decision_reason='dense',
            notification_text=notif,
        ),
        now_bj='2026-06-01 15:50:24',
        cash_footer_lines=[],
    )

    assert "Put 0 · Call 0 · 平仓 0\n" in message
    assert "待补" not in message
    assert "价差过宽" not in message
