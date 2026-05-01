from __future__ import annotations


def test_account_message_is_plain_text_for_weixin() -> None:
    from scripts.multi_tick.misc import AccountResult
    from scripts.multi_tick.notify_format import build_account_message

    notif = (
        "Put\n"
        "腾讯 卖Put 2026-04-29 460P\n"
        "担保 1张 加仓后余量 ¥-100\n"
        "\n"
        "Call\n"
        "英伟达 卖Call 2026-06-18 180C\n"
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
    assert "### 账户 lx · 本轮候选\n- Put 1 / Call 1" in message
    assert "LX 持有 ¥1,000 (CNY) | 可用 ¥200 (CNY)" in message
    assert "**" not in message
    assert "\n>" not in message


def test_account_message_skips_accounts_without_notification_text() -> None:
    from scripts.multi_tick.misc import AccountResult
    from scripts.multi_tick.notify_format import build_account_message

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
