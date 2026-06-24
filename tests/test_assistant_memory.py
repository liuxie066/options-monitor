from __future__ import annotations

import json
from pathlib import Path

from src.application.assistant.memory import load_assistant_memory_context


def test_assistant_memory_missing_directory_returns_empty(tmp_path: Path) -> None:
    memory = load_assistant_memory_context(path=tmp_path / "assistant_memory")

    assert memory["provided"] is False
    assert memory["reason"] == "missing"


def test_assistant_memory_loads_valid_markdown_topic_file(tmp_path: Path) -> None:
    memory_dir = tmp_path / "assistant_memory"
    memory_dir.mkdir()
    (memory_dir / "parameter-tuning.md").write_text(
        """\
---
type: parameter_tuning_preference
title: 参数调优偏好
summary: 用户希望先看候选过滤证据，再讨论放宽参数。
tags: [参数, 候选]
status: active
---
用户优化 sell put 参数时，偏好先看候选过滤、拒绝原因和回放证据，再讨论具体阈值。
""",
        encoding="utf-8",
    )

    memory = load_assistant_memory_context(path=memory_dir, query="参数怎么优化")

    assert memory["provided"] is True
    assert memory["memory_count"] == 1
    item = memory["memories"][0]
    assert item["memory_id"] == "parameter-tuning"
    assert item["type"] == "parameter_tuning_preference"
    assert item["relevance"]["score"] >= 1
    assert "参数" in item["relevance"]["matched_terms"]
    assert memory["policy"]["memory_cannot_authorize_writes"] is True


def test_assistant_memory_ignores_invalid_types_and_inactive_items(tmp_path: Path) -> None:
    memory_dir = tmp_path / "assistant_memory"
    memory_dir.mkdir()
    (memory_dir / "market-fact.md").write_text(
        """\
---
type: current_price
title: 不应加载
---
NVDA 当前价格是 123。
""",
        encoding="utf-8",
    )
    (memory_dir / "inactive.md").write_text(
        """\
---
type: workflow_pattern
title: 已废弃
status: archived
---
旧流程。
""",
        encoding="utf-8",
    )

    memory = load_assistant_memory_context(path=memory_dir, query="NVDA 当前价格")

    assert memory["provided"] is False
    assert memory["reason"] == "empty"


def test_assistant_memory_redacts_sensitive_lines(tmp_path: Path) -> None:
    memory_dir = tmp_path / "assistant_memory"
    memory_dir.mkdir()
    (memory_dir / "workflow.md").write_text(
        """\
---
type: workflow_pattern
title: 调参流程
---
先跑 replay。
webhook: https://example.invalid/secret
再看候选通过率。
""",
        encoding="utf-8",
    )

    memory = load_assistant_memory_context(path=memory_dir, query="调参 replay")

    assert memory["provided"] is True
    content = memory["memories"][0]["content"]
    assert "example.invalid" not in content
    assert "[redacted sensitive line]" in content
    assert memory["memories"][0]["redacted_line_count"] == 1


def test_assistant_memory_redacts_sensitive_frontmatter_before_projection(tmp_path: Path) -> None:
    memory_dir = tmp_path / "assistant_memory"
    memory_dir.mkdir()
    (memory_dir / "workflow.md").write_text(
        """\
---
type: workflow_pattern
title: webhook: https://example.invalid/secret
summary: password=abc123
tags: [流程, token=abc123]
---
先跑 replay，再看候选通过率。
""",
        encoding="utf-8",
    )

    memory = load_assistant_memory_context(path=memory_dir, query="候选")

    assert memory["provided"] is True
    item = memory["memories"][0]
    rendered = json.dumps(item, ensure_ascii=False)
    assert "example.invalid" not in rendered
    assert "password=abc123" not in rendered
    assert "token=abc123" not in rendered
    assert item["redacted_line_count"] == 3


def test_assistant_memory_query_filters_unmatched_items(tmp_path: Path) -> None:
    memory_dir = tmp_path / "assistant_memory"
    memory_dir.mkdir()
    (memory_dir / "income.md").write_text(
        """\
---
type: om_usage_preference
title: 收益分析习惯
tags: [收益]
---
用户问收益时偏好先看月度净收入拆解。
""",
        encoding="utf-8",
    )
    (memory_dir / "config.md").write_text(
        """\
---
type: om_usage_preference
title: 配置检查习惯
tags: [配置]
---
用户问配置时偏好先读当前运行配置来源。
""",
        encoding="utf-8",
    )

    memory = load_assistant_memory_context(path=memory_dir, query="收益怎么拆")

    assert memory["provided"] is True
    assert [item["memory_id"] for item in memory["memories"]] == ["income"]
