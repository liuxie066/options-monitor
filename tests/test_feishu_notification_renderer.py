from __future__ import annotations

from copy import deepcopy

import pytest


def test_feishu_notification_card_preserves_primary_and_flat_fallback() -> None:
    from src.application.channels.feishu_notification_renderer import (
        feishu_notification_envelope_sha256,
        normalize_feishu_notification_envelope,
        render_feishu_notification_card,
    )

    markdown = (
        "# OM · 决策简报 · lx\n\n"
        "| 优先 | 合约 |\n"
        "|---|---|\n"
        "| 首选 | SPCX 08-07 $110 Put |"
    )
    fallback = "# OM · 决策简报 · lx\n\n首选｜SPCX 08-07 $110 Put"

    envelope = render_feishu_notification_card(
        markdown=markdown,
        fallback_text=fallback,
    )
    normalized = normalize_feishu_notification_envelope(
        envelope,
        expected_text=fallback,
    )

    assert normalized["transport"]["msg_type"] == "interactive"
    assert normalized["transport"]["content"]["schema"] == "2.0"
    assert (
        normalized["transport"]["content"]["body"]["elements"][0]["content"]
        == markdown
    )
    assert normalized["fallback"] == {"msg_type": "post", "markdown": fallback}
    assert normalized["text"] == fallback
    assert normalized["render_meta"]["markdown_table_detected"] is True
    assert len(feishu_notification_envelope_sha256(normalized)) == 64


def test_feishu_notification_card_rejects_fallback_or_digest_tampering() -> None:
    from src.application.channels.feishu_notification_renderer import (
        normalize_feishu_notification_envelope,
        render_feishu_notification_card,
    )

    envelope = render_feishu_notification_card(
        markdown="| 项目 | 数值 |\n|---|---:|\n| 现金 | ¥1,000 |",
        fallback_text="现金｜¥1,000",
    )

    wrong_fallback = deepcopy(envelope)
    wrong_fallback["fallback"]["markdown"] = "现金｜¥2,000"
    with pytest.raises(ValueError, match="fallback transport"):
        normalize_feishu_notification_envelope(wrong_fallback)

    wrong_digest = deepcopy(envelope)
    wrong_digest["transport"]["content"]["body"]["elements"][0]["content"] += "\n篡改"
    with pytest.raises(ValueError, match="digest mismatch"):
        normalize_feishu_notification_envelope(wrong_digest)


def test_feishu_notification_card_truncates_only_at_complete_table_rows() -> None:
    from src.application.channels.feishu_notification_renderer import render_feishu_notification_card

    rows = [f"| {index} | {'X' * 40} |" for index in range(20)]
    markdown = "\n".join(
        [
            "# 候选",
            "",
            "| 优先 | 合约 |",
            "|---|---|",
            *rows,
            "",
            "提醒｜执行前复核报价。",
        ]
    )

    envelope = render_feishu_notification_card(
        markdown=markdown,
        fallback_text="候选较多，请查询完整简报。",
        max_chars=260,
    )
    rendered = envelope["transport"]["content"]["body"]["elements"][0]["content"]

    assert envelope["render_meta"]["truncated"] is True
    assert rendered.endswith("…（内容较长，已在完整内容边界截断）")
    assert "| 优先 | 合约 |" in rendered
    for line in rendered.splitlines():
        if line.startswith("| "):
            assert line.endswith(" |")
