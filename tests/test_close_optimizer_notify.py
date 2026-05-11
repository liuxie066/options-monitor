from __future__ import annotations


def test_highlight_optimizer_lines_tags_switch_rows() -> None:
    from src.application.multi_tick.notify_format import _highlight_optimizer_lines

    text = "- NVDA Put 2026-06-19 150P · 强烈建议平仓换仓"
    out = _highlight_optimizer_lines(text)

    assert out.endswith("🔄")


def test_highlight_optimizer_lines_tags_close_rows() -> None:
    from src.application.multi_tick.notify_format import _highlight_optimizer_lines

    text = "- TSLA Put 2026-06-19 200P · 建议平仓"
    out = _highlight_optimizer_lines(text)

    assert out.endswith("⚠️")


def test_highlight_optimizer_lines_does_not_double_tag() -> None:
    from src.application.multi_tick.notify_format import _highlight_optimizer_lines

    text = "- NVDA Put 2026-06-19 150P · 强烈建议平仓换仓 🔄"
    out = _highlight_optimizer_lines(text)

    assert out.count("🔄") == 1


def test_highlight_optimizer_lines_skips_normal_close_advice() -> None:
    from src.application.multi_tick.notify_format import _highlight_optimizer_lines

    text = "- NVDA Put 2026-06-19 150P · 强烈推荐平仓"
    out = _highlight_optimizer_lines(text)

    assert "🔄" not in out
    assert "⚠️" not in out


def test_highlight_optimizer_lines_preserves_other_content() -> None:
    from src.application.multi_tick.notify_format import _highlight_optimizer_lines

    text = "Put\n- NVDA 卖Put 150P\n### [lx] 平仓建议\n- TSLA Put · 强烈建议平仓换仓\n- 价格: 0.5"
    out = _highlight_optimizer_lines(text)

    assert "Put\n- NVDA 卖Put 150P" in out
    assert "### [lx] 平仓建议" in out
    assert "强烈建议平仓换仓 🔄" in out
    assert "- 价格: 0.5" in out


def test_count_optimizer_actions_empty() -> None:
    from src.application.multi_tick.notify_format import count_optimizer_actions

    assert count_optimizer_actions("") == (0, 0)
    assert count_optimizer_actions(None) == (0, 0)


def test_count_optimizer_actions_counts_both_kinds() -> None:
    from src.application.multi_tick.notify_format import count_optimizer_actions

    text = (
        "- NVDA · 强烈建议平仓换仓\n"
        "- TSLA · 建议平仓\n"
        "- AAPL · 强烈建议平仓换仓\n"
        "- MSFT · 建议持有\n"
    )

    assert count_optimizer_actions(text) == (2, 1)


def test_count_optimizer_actions_switch_does_not_double_count_close() -> None:
    from src.application.multi_tick.notify_format import count_optimizer_actions

    text = "- NVDA · 强烈建议平仓换仓"

    assert count_optimizer_actions(text) == (1, 0)


def test_build_account_message_appends_optimizer_counts_to_header() -> None:
    from src.application.multi_tick.misc import AccountResult
    from src.application.multi_tick.notify_format import build_account_message

    notif = (
        "Put\n"
        "腾讯 卖Put 2026-04-29 460P\n"
        "\n"
        "### [lx] 平仓建议\n"
        "- NVDA Put 2026-06-19 150P · 强烈建议平仓换仓\n"
        "- 优化器: 持有年化=5.0% → 替代候选年化=12.0% | 尾部风险=0.045\n"
        "- TSLA Put 2026-06-19 200P · 建议平仓\n"
        "- 优化器: 持有年化=4.0% | 尾部风险=0.060 | 无可替换候选\n"
    )

    message = build_account_message(
        AccountResult(
            account="lx",
            ran_scan=True,
            should_notify=True,
            decision_reason="dense",
            notification_text=notif,
        ),
        now_bj="2026-04-08 22:31:00",
        cash_footer_lines=None,
    )

    assert "### 账户 lx · 本轮候选" in message
    assert "优化器 换仓1 平仓1" in message
    assert "强烈建议平仓换仓 🔄" in message
    assert "建议平仓 ⚠️" in message


def test_build_account_message_without_optimizer_omits_optimizer_counts() -> None:
    from src.application.multi_tick.misc import AccountResult
    from src.application.multi_tick.notify_format import build_account_message

    notif = (
        "Put\n"
        "腾讯 卖Put 2026-04-29 460P\n"
        "\n"
        "### [lx] 平仓建议\n"
        "- NVDA Put 2026-06-19 150P · 强烈推荐平仓\n"
    )

    message = build_account_message(
        AccountResult(
            account="lx",
            ran_scan=True,
            should_notify=True,
            decision_reason="dense",
            notification_text=notif,
        ),
        now_bj="2026-04-08 22:31:00",
        cash_footer_lines=None,
    )

    assert "优化器" not in message
    assert "🔄" not in message
    assert "⚠️" not in message


def test_build_account_message_optimizer_only_no_other_candidates() -> None:
    from src.application.multi_tick.misc import AccountResult
    from src.application.multi_tick.notify_format import build_account_message

    notif = (
        "### [sy] 平仓建议\n"
        "- NVDA Put 2026-06-19 150P · 强烈建议平仓换仓\n"
        "- 优化器: 持有年化=5.0% → 替代候选年化=12.0% | 尾部风险=0.045\n"
    )

    message = build_account_message(
        AccountResult(
            account="sy",
            ran_scan=True,
            should_notify=True,
            decision_reason="dense",
            notification_text=notif,
        ),
        now_bj="2026-04-08 22:31:00",
        cash_footer_lines=None,
    )

    assert "Put 0 / Call 0 / 优化器 换仓1 平仓0" in message
    assert "强烈建议平仓换仓 🔄" in message


def test_close_advice_render_markdown_emits_optimizer_labels_for_notify_format() -> None:
    from src.application.close_advice_runner import render_markdown

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

    md = render_markdown(rows, notify_levels={"strong", "medium"}, max_items=5)

    assert "强烈建议平仓换仓" in md
    assert "### [lx] 平仓建议" in md
