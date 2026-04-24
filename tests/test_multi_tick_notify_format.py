from __future__ import annotations


def test_merged_message_is_plain_text_for_weixin() -> None:
    from scripts.multi_tick.misc import AccountResult
    from scripts.multi_tick.notify_format import build_merged_message

    notif = (
        "Put\n"
        "腾讯 卖Put 2026-04-29 460P\n"
        "担保 1张 加仓后余量 ¥-100\n"
        "\n"
        "Call\n"
        "英伟达 卖Call 2026-06-18 180C\n"
        "覆盖 1张 cover 1\n"
    )
    merged = build_merged_message(
        [
            AccountResult(
                account='lx',
                ran_scan=True,
                should_notify=True,
                decision_reason='dense',
                notification_text=notif,
            )
        ],
        now_bj='2026-04-08 22:31:00',
        cash_footer_lines=["💰 现金 CNY", "LX 持有 ¥1,000 (CNY) | 可用 ¥200 (CNY)"],
    )

    assert "# 📊 Options Monitor\n## 合并提醒" in merged
    assert "北京时间 2026-04-08 22:31:00" in merged
    assert "### lx · 本轮候选\n- Put 1 / Call 1" in merged
    assert "**" not in merged
    assert "\n>" not in merged


def test_merged_message_keeps_divider() -> None:
    from scripts.multi_tick.misc import AccountResult
    from scripts.multi_tick.notify_format import build_merged_message

    notif = "Put\n无须处理"
    merged = build_merged_message(
        [
            AccountResult(
                account='sy',
                ran_scan=True,
                should_notify=True,
                decision_reason='dense',
                notification_text=notif,
            )
        ],
        now_bj='2026-04-08 22:31:00',
        cash_footer_lines=None,
    )

    assert "\n---\n" in merged
