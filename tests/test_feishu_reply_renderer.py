from __future__ import annotations

from src.application.channels.feishu_reply_renderer import (
    FEISHU_REPLY_CONTENT_BUDGET_BYTES,
    FEISHU_REPLY_ENVELOPE_SCHEMA_VERSION,
    FEISHU_REPLY_TRUNCATION_NOTICE,
    flatten_markdown_tables,
    has_markdown_table,
    render_feishu_conversation_reply,
    sanitize_feishu_markdown,
    truncate_feishu_markdown,
)


def test_copilot_reply_uses_card_markdown_even_for_plain_sentence() -> None:
    envelope = render_feishu_conversation_reply(
        message_id="msg_1",
        text="结论：系统运行正常。",
        reply_in_thread=True,
        max_chars=3500,
        render_route="copilot",
    )

    assert envelope["schema_version"] == FEISHU_REPLY_ENVELOPE_SCHEMA_VERSION
    assert envelope["render_mode"] == "card_markdown_v2"
    assert envelope["reply_in_thread"] is True
    assert envelope["text"] == "结论：系统运行正常。"
    assert envelope["transport"] == {
        "msg_type": "interactive",
        "content": {
            "schema": "2.0",
            "body": {
                "elements": [
                    {
                        "tag": "markdown",
                        "element_id": "reply_body",
                        "content": "结论：系统运行正常。",
                        "text_size": "normal",
                    }
                ]
            },
        },
    }


def test_markdown_table_uses_card_without_changing_financial_values() -> None:
    source = (
        "拆解如下：\n\n"
        "| 项目 | CNY | 原币 |\n"
        "|---|---:|---|\n"
        "| 卖出开仓权利金收入 | ¥13,266.88 | HKD +10,449；USD +471 |\n"
        "| 买入权利金支出 | -¥244.47 | USD -36 |"
    )

    envelope = render_feishu_conversation_reply(
        message_id="msg_1",
        text=source,
        reply_in_thread=False,
        max_chars=3500,
        render_route="deterministic_control",
    )

    content = envelope["transport"]["content"]["body"]["elements"][0]["content"]
    assert envelope["render_mode"] == "card_markdown_v2"
    assert envelope["render_meta"]["markdown_table_detected"] is True
    assert content == source
    assert envelope["text"] == (
        "拆解如下：\n\n"
        "项目：卖出开仓权利金收入\n"
        "CNY：¥13,266.88\n"
        "原币：HKD +10,449；USD +471\n\n"
        "项目：买入权利金支出\n"
        "CNY：-¥244.47\n"
        "原币：USD -36"
    )
    assert "¥13,266.88" in content
    assert "HKD +10,449；USD +471" in content
    assert "-¥244.47" in content


def test_short_plain_control_reply_stays_text() -> None:
    envelope = render_feishu_conversation_reply(
        message_id="msg_1",
        text="当前没有待确认操作。",
        reply_in_thread=False,
        max_chars=3500,
        render_route="deterministic_control",
    )

    assert envelope["render_mode"] == "text"
    assert envelope["transport"] == {
        "msg_type": "text",
        "content": {"text": "当前没有待确认操作。"},
    }
    assert "fallback" not in envelope


def test_sanitizer_neutralizes_active_tags_images_and_unsafe_links() -> None:
    source = (
        '<at id="ou_1">所有人</at> '
        "[安全链接](https://example.com/report) "
        "[危险链接](javascript:alert(1)) "
        "![图表](https://example.com/chart.png) "
        "![附件][unsafe]\n"
        "[unsafe]: javascript:alert(1)"
    )

    sanitized = sanitize_feishu_markdown(source)

    assert '<at id="ou_1">' not in sanitized
    assert "&lt;at id=\"ou_1\"&gt;所有人&lt;/at&gt;" in sanitized
    assert "[安全链接](https://example.com/report)" in sanitized
    assert "javascript:" not in sanitized
    assert "危险链接" in sanitized
    assert "![" not in sanitized
    assert "图表（https://example.com/chart.png）" in sanitized
    assert "附件" in sanitized
    assert "[unsafe]:" not in sanitized


def test_truncation_keeps_table_structure_and_complete_rows() -> None:
    source = (
        "明细：\n\n"
        "| 项目 | CNY |\n"
        "|---|---:|\n"
        "| 第一项 | ¥1,000.00 |\n"
        "| 第二项 | ¥2,000.00 |\n"
        "| 第三项 | ¥3,000.00 |\n\n"
        "后续解释不会保留。"
    )

    rendered, truncated = truncate_feishu_markdown(
        source,
        max_chars=80,
        max_bytes=FEISHU_REPLY_CONTENT_BUDGET_BYTES,
    )

    assert truncated is True
    assert FEISHU_REPLY_TRUNCATION_NOTICE in rendered
    assert "| 项目 | CNY |" in rendered
    assert "|---|---:|" in rendered
    assert all(line.count("|") == 3 for line in rendered.splitlines() if line.startswith("|"))
    assert has_markdown_table(rendered) is True


def test_byte_budget_truncates_multibyte_content() -> None:
    rendered, truncated = truncate_feishu_markdown(
        "中🙂" * 20_000,
        max_chars=0,
        max_bytes=512,
    )

    assert truncated is True
    assert len(rendered.encode("utf-8")) <= 512
    assert rendered.endswith(FEISHU_REPLY_TRUNCATION_NOTICE)


def test_fallback_flattens_table_to_label_blocks() -> None:
    markdown = (
        "| 项目 | CNY | 原币 |\n"
        "|---|---:|---|\n"
        "| 卖出开仓权利金收入 | ¥13,266.88 | HKD +10,449 |\n"
        "| 买入权利金支出 | -¥244.47 | USD -36 |"
    )

    flattened = flatten_markdown_tables(markdown)

    assert "|" not in flattened
    assert "项目：卖出开仓权利金收入" in flattened
    assert "CNY：¥13,266.88" in flattened
    assert "原币：USD -36" in flattened
