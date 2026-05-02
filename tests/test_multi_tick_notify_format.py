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


def test_account_message_uses_compact_auto_close_template_when_scan_skipped() -> None:
    from scripts.multi_tick.misc import AccountResult
    from scripts.multi_tick.notify_format import build_account_message

    message = build_account_message(
        AccountResult(
            account='lx',
            ran_scan=False,
            should_notify=True,
            decision_reason='scheduler_skip',
            notification_text=(
                "Auto-close(exp+2d): closed 1/1, errors 0\n"
                "- rec_1 | pos_1 | exp=2026-05-01"
            ),
        ),
        now_bj='2026-05-02 22:31:00',
        cash_footer_lines=["LX 持有 ¥1,000 (CNY) | 可用 ¥200 (CNY)"],
    )

    assert "# Auto-close\n## 账户提醒（lx）" in message
    assert "Auto-close(exp+2d): closed 1/1, errors 0" in message
    assert "- rec_1 | pos_1 | exp=2026-05-01" in message
    assert "本轮候选" not in message
    assert "Put 0 / Call 0" not in message
    assert "LX 持有" not in message


def test_flatten_auto_close_summary_keeps_error_only_summary() -> None:
    from scripts.multi_tick.notify_format import flatten_auto_close_summary

    text = "\n".join(
        [
            "Auto-close expired positions (grace_days=2)",
            "candidates_should_close: 1",
            "applied_closed: 0",
            "errors: 1",
            "- rec_1 | close failed",
        ]
    )

    out = flatten_auto_close_summary(text)

    assert "Auto-close(exp+2d): closed 0/1, errors 1" in out
    assert "- rec_1 | close failed" in out


def test_flatten_auto_close_summary_includes_skipped_already_closed_count() -> None:
    from scripts.multi_tick.notify_format import flatten_auto_close_summary

    text = "\n".join(
        [
            "Auto-close expired positions (grace_days=1)",
            "candidates_should_close: 1",
            "applied_closed: 1",
            "skipped_already_closed: 1",
            "ERRORS: 0",
            "- rec_1 | pos_1 | exp=2026-05-01",
        ]
    )

    out = flatten_auto_close_summary(text)

    assert "Auto-close(exp+1d): closed 1/1, skipped 1, errors 0" in out
    assert "errors 1" not in out


def test_flatten_auto_close_summary_suppresses_skipped_only_summary() -> None:
    from scripts.multi_tick.notify_format import flatten_auto_close_summary

    text = "\n".join(
        [
            "Auto-close expired positions (grace_days=1)",
            "candidates_should_close: 0",
            "applied_closed: 0",
            "skipped_already_closed: 1",
            "ERRORS: 0",
        ]
    )

    assert flatten_auto_close_summary(text) == ""
