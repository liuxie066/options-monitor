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
    assert envelope['render_meta']['renderer_version'] == '4'
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
    element = envelope['transport']['content']['body']['elements'][0]
    element['element_id'] = 'notification_body'
    element.pop('margin')
    envelope['fallback']['markdown'] = envelope['text']
    meta = envelope['render_meta']
    for key in list(meta):
        if key.startswith('fallback_'):
            del meta[key]
    meta['renderer_version'] = '1'
    meta.pop('primary_request_body_bytes')
    meta['rendered_sha256'] = hashlib.sha256(json.dumps(envelope['transport'], ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
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


def test_multi_block_card_preserves_order_and_explicit_spacing() -> None:
    from src.application.channels.feishu_notification_renderer import render_feishu_notification_card
    blocks = ['# 决策简报', '## CSP', '**PDD｜11-27 $78 Put**\n收益｜3.2%',
              '## CC\n暂无合适合约', '## 组合增强\n暂无合适合约', '## Wheel', 'Wheel 明细',
              '## 事件', 'PDD｜11-18 财报', '## 持仓\n共 10 条', '## 资金\n现金总额｜¥100,000']
    envelope = render_feishu_notification_card(
        markdown='\n\n'.join(blocks), markdown_blocks=blocks,
        fallback_text='备用正文', fallback_open_id='ou_fixture',
    )
    elements = envelope['transport']['content']['body']['elements']
    assert [item['content'] for item in elements] == blocks
    assert [item['element_id'] for item in elements] == [f'notification_{n}' for n in range(1, len(blocks) + 1)]
    assert [item['margin'] for item in elements] == [
        '0px 0px 0px 0px', '12px 0px 0px 0px', '0px 0px 0px 0px',
        '12px 0px 0px 0px', '12px 0px 0px 0px', '12px 0px 0px 0px', '0px 0px 0px 0px',
        '12px 0px 0px 0px', '0px 0px 0px 0px', '12px 0px 0px 0px', '12px 0px 0px 0px',
    ]
    assert envelope['render_meta']['rendered_content_bytes'] == len('\n\n'.join(blocks).encode())


@pytest.mark.parametrize('blocks', [[], ['正文', ''], ['不同正文'], '正文'])
def test_multi_block_card_rejects_invalid_or_mismatched_source(blocks) -> None:
    from src.application.channels.feishu_notification_renderer import render_feishu_notification_card
    with pytest.raises(ValueError, match='blocks'):
        render_feishu_notification_card(
            markdown='正文', markdown_blocks=blocks, fallback_text='备用', fallback_open_id='ou_test',
        )


@pytest.mark.parametrize('mutation', ['content', 'id', 'margin', 'bytes', 'version'])
def test_multi_block_validation_checks_all_elements(mutation: str) -> None:
    from src.application.channels.feishu_notification_renderer import normalize_feishu_notification_envelope, render_feishu_notification_card
    envelope = render_feishu_notification_card(
        markdown='标题\n\n第二块', markdown_blocks=['标题', '第二块'], fallback_text='备用', fallback_open_id='ou_test',
    )
    second = envelope['transport']['content']['body']['elements'][1]
    if mutation == 'content':
        second['content'] += '篡改'
    elif mutation == 'id':
        second['element_id'] = 'notification_1'
    elif mutation == 'margin':
        second['margin'] = '99px'
    elif mutation == 'bytes':
        envelope['render_meta']['rendered_content_bytes'] -= 1
    else:
        envelope['render_meta']['renderer_version'] = 'unknown'
    with pytest.raises(ValueError):
        normalize_feishu_notification_envelope(envelope)


@pytest.mark.parametrize('content', ['中文🙂"\\' * 120, 'x' * 160])
def test_multi_block_bounds_complete_primary_request_with_json_overhead(content: str) -> None:
    from src.application.channels.feishu_notification_renderer import FEISHU_NOTIFICATION_CONTENT_BUDGET_BYTES, normalize_feishu_notification_envelope, render_feishu_notification_card
    from src.infrastructure.feishu_bot import FEISHU_SEND_REQUEST_BUDGET_BYTES, build_message_payload, feishu_request_body_bytes
    blocks = [f'**候选 {n}**\n{content}' for n in range(100)]
    receiver = 'ou_中"\\' + 'x' * 64
    envelope = render_feishu_notification_card(
        markdown='\n\n'.join(blocks), markdown_blocks=blocks,
        fallback_text='备用', fallback_open_id=receiver,
    )
    transport = envelope['transport']
    elements = transport['content']['body']['elements']
    rendered = '\n\n'.join(item['content'] for item in elements)
    assert len(rendered.encode()) <= FEISHU_NOTIFICATION_CONTENT_BUDGET_BYTES
    assert envelope['render_meta']['truncated'] is True
    assert rendered.endswith('…（内容较长，已在完整内容边界截断）')
    assert elements[0]['content'] == blocks[0]
    assert [item['content'] for item in elements[:-1]] == blocks[:len(elements)-1]
    size = feishu_request_body_bytes(build_message_payload(
        open_id=receiver, msg_type='interactive', content=transport['content'], uuid='om-' + 'f' * 32,
    ))
    assert size == envelope['render_meta']['primary_request_body_bytes']
    assert size <= FEISHU_SEND_REQUEST_BUDGET_BYTES
    assert normalize_feishu_notification_envelope(envelope) == envelope


@pytest.mark.parametrize('version', ['2', '3'])
def test_frozen_legacy_card_is_read_back_without_changes(version: str) -> None:
    import hashlib
    import json
    from src.application.channels.feishu_notification_renderer import feishu_notification_envelope_sha256, normalize_feishu_notification_envelope
    from src.infrastructure.feishu_bot import build_post_message_payload, feishu_request_body_bytes
    transport = {'msg_type': 'interactive', 'content': {'schema': '2.0', 'body': {'elements': [
        {'tag': 'markdown', 'element_id': 'notification_body', 'content': '# 旧简报\n\n原文', 'text_size': 'normal'},
    ]}}}
    if version == '3':
        transport['content']['body']['elements'] = [
            {'tag': 'markdown', 'element_id': 'notification_1', 'content': '# 旧简报',
             'text_size': 'normal', 'margin': '0px 0px 0px 0px'},
            {'tag': 'markdown', 'element_id': 'notification_2', 'content': '原文',
             'text_size': 'normal', 'margin': '12px 0px 0px 0px'},
        ]
    fallback = '旧备用正文'
    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()
    def canonical(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    envelope = {
        'schema_version': 'feishu-proactive-notification.v1', 'provider': 'feishu_app',
        'text': fallback, 'render_mode': 'card_markdown_v2', 'transport': transport,
        'fallback': {'msg_type': 'post', 'markdown': fallback},
        'render_meta': {
            'renderer_version': version, 'card_schema': '2.0', 'source_chars': 9,
            'source_sha256': digest('# 旧简报\n\n原文'),
            'rendered_sha256': digest(canonical(transport)),
            'rendered_content_bytes': len('# 旧简报\n\n原文'.encode()),
            'markdown_table_detected': False, 'truncated': False,
            'fallback_sha256': digest(fallback), 'fallback_truncated': False,
            'fallback_receive_id_json_bytes': len(json.dumps('ou_old').encode()),
            'fallback_request_body_bytes': feishu_request_body_bytes(build_post_message_payload(
                open_id='ou_old', markdown=fallback, uuid='om-' + '0' * 32 + ':fallback',
            )),
        },
    }
    if version == '3':
        from src.infrastructure.feishu_bot import build_message_payload
        envelope['render_meta']['primary_request_body_bytes'] = feishu_request_body_bytes(build_message_payload(
            open_id='ou_old', msg_type='interactive', content=transport['content'], uuid='om-' + '0' * 32,
        ))
    assert normalize_feishu_notification_envelope(envelope) == envelope
    assert feishu_notification_envelope_sha256(envelope) == digest(canonical(envelope))
