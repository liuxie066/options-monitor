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
        fallback_open_id="ou_test",
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
        fallback_open_id="ou_test",
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
        fallback_open_id="ou_test",
        max_chars=260,
    )
    rendered = envelope["transport"]["content"]["body"]["elements"][0]["content"]

    assert envelope["render_meta"]["truncated"] is True
    assert rendered.endswith("…（内容较长，已在完整内容边界截断）")
    assert "| 优先 | 合约 |" in rendered
    for line in rendered.splitlines():
        if line.startswith("| "):
            assert line.endswith(" |")


@pytest.mark.parametrize('fallback', [
    '中文🙂"\\\n' * 6000,
    '\n\n'.join('段落中🙂"\\' for _ in range(1800)),
    '| 列 | 内容 |\n|---|---|\n' + '\n'.join(f'| {n} | 中文🙂"\\ |' for n in range(2500)),], ids=['escaped-unicode-lines', 'paragraphs', 'large-table'])
def test_notification_bounds_complete_serialized_fallback_before_freeze(fallback: str) -> None:
    from src.application.channels.feishu_notification_renderer import (
        feishu_notification_envelope_sha256, normalize_feishu_notification_envelope, render_feishu_notification_card,
    )
    from src.infrastructure.feishu_bot import (
        FEISHU_POST_REQUEST_BUDGET_BYTES, build_post_message_payload, feishu_request_body_bytes,
    )
    receiver = 'ou_fixture_"\\中'
    uuid = 'om-' + 'f' * 32 + ':fallback'
    assert feishu_request_body_bytes(build_post_message_payload(open_id=receiver, markdown=fallback, uuid=uuid)) > FEISHU_POST_REQUEST_BUDGET_BYTES
    envelope = render_feishu_notification_card(markdown='# 测试', fallback_text=fallback, fallback_open_id=receiver)
    assert envelope['text'] == fallback.strip()
    assert envelope['render_meta']['renderer_version'] == '2'
    assert envelope['render_meta']['fallback_truncated'] is True
    bounded = envelope['fallback']['markdown']
    assert bounded != fallback.strip()
    assert bounded.endswith('…（内容较长，已在完整内容边界截断）')
    assert len(bounded) > len('…（内容较长，已在完整内容边界截断）')
    size = feishu_request_body_bytes(build_post_message_payload(open_id=receiver, markdown=bounded, uuid=uuid))
    assert size == envelope['render_meta']['fallback_request_body_bytes']
    assert size <= FEISHU_POST_REQUEST_BUDGET_BYTES
    assert receiver not in repr(envelope)
    assert normalize_feishu_notification_envelope(envelope, expected_text=fallback) == envelope
    assert feishu_notification_envelope_sha256(envelope) == feishu_notification_envelope_sha256(deepcopy(envelope))
    if fallback.startswith('|'):
        table, notice = bounded.rsplit('\n\n', 1)
        assert notice == '…（内容较长，已在完整内容边界截断）'
        original_lines = fallback.splitlines()
        retained_lines = table.splitlines()
        assert 2 < len(retained_lines) < len(original_lines)
        assert retained_lines == original_lines[:len(retained_lines)]


@pytest.mark.parametrize('extra', [0, 1])
def test_notification_fallback_budget_counts_receiver_and_fallback_uuid(extra: int) -> None:
    from src.application.channels.feishu_notification_renderer import render_feishu_notification_card
    from src.infrastructure.feishu_bot import (
        FEISHU_POST_REQUEST_BUDGET_BYTES, build_post_message_payload, feishu_request_body_bytes,
    )
    receiver = 'ou_' + 'x' * 64
    uuid = 'om-' + 'f' * 32 + ':fallback'
    overhead = feishu_request_body_bytes(build_post_message_payload(open_id=receiver, markdown='a', uuid=uuid)) - 1
    text = 'a' * (FEISHU_POST_REQUEST_BUDGET_BYTES - overhead + extra)
    envelope = render_feishu_notification_card(markdown='short', fallback_text=text, fallback_open_id=receiver)
    assert envelope['text'] == text
    assert envelope['render_meta']['fallback_truncated'] is bool(extra)
    assert (envelope['fallback']['markdown'] == text) is (extra == 0)
    assert envelope['render_meta']['fallback_request_body_bytes'] <= FEISHU_POST_REQUEST_BUDGET_BYTES


def test_notification_normalize_preserves_oversized_frozen_v1_and_digest() -> None:
    import hashlib
    import json
    from src.application.channels.feishu_notification_renderer import (
        feishu_notification_envelope_sha256, normalize_feishu_notification_envelope, render_feishu_notification_card,
    )
    envelope = render_feishu_notification_card(markdown='short', fallback_text='原文' * 20000, fallback_open_id='ou_test')
    envelope['fallback']['markdown'] = envelope['text']
    meta = envelope['render_meta']
    for key in list(meta):
        if key.startswith('fallback_'):
            del meta[key]
    meta['renderer_version'] = '1'
    expected_sha = hashlib.sha256(json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    assert normalize_feishu_notification_envelope(envelope) == envelope
    assert feishu_notification_envelope_sha256(envelope) == expected_sha


def test_notification_rejects_frozen_v2_budget_metadata_tampering() -> None:
    from src.application.channels.feishu_notification_renderer import normalize_feishu_notification_envelope, render_feishu_notification_card
    envelope = render_feishu_notification_card(markdown='short', fallback_text='正文', fallback_open_id='ou_test')
    envelope['render_meta']['fallback_request_body_bytes'] += 1
    with pytest.raises(ValueError, match='byte count mismatch'):
        normalize_feishu_notification_envelope(envelope)


def test_notification_refuses_identity_overhead_that_cannot_fit_notice() -> None:
    from src.application.channels.feishu_notification_renderer import render_feishu_notification_card
    with pytest.raises(ValueError, match='identity exceeds'):
        render_feishu_notification_card(markdown='short', fallback_text='正文', fallback_open_id='x' * 30000)
