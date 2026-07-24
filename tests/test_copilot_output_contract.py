from __future__ import annotations

import pytest

from src.application.copilot.contracts import AppResult
from src.application.copilot.result_admission import (
    admit_result,
    output_contract_matches,
    output_contract_rejection_reason,
)


@pytest.mark.parametrize(
    "response",
    (
        '{"status":"ok","items":[1,2]}',
        "[1,2,3]",
        '```json\n{"status":"ok"}\n```',
        "```markdown\n# 标题\n\n正文\n```",
        "````markdown\n# 示例\n\n```json\n{}\n```\n````",
        "结论：当前证据不足。\n\n补充说明：缺少当前价格。",
        "示例：\n\n```python\nprint('ok')\n```",
    ),
)
def test_output_contract_accepts_valid_structures(response: str) -> None:
    assert output_contract_rejection_reason(response) is None
    result = admit_result(AppResult(status="answered", user_response=response))
    assert result.status == "answered"


@pytest.mark.parametrize(
    ("response", "reason"),
    (
        ('{"value":NaN}', "invalid_raw_json"),
        ('{"value":1,}', "invalid_raw_json"),
        ('{"value":1} trailing', "invalid_raw_json"),
        ("说明\n```json\n{}\n```", "invalid_json_container"),
        ("```json\n{}\n```\n说明", "invalid_json_container"),
        ("```json\n{bad}\n```", "invalid_json_container"),
        ("```markdown\n# 标题\n```\n说明", "invalid_markdown_container"),
        ("```json\n{}", "unbalanced_code_fence"),
        ("普通文本\n```", "unbalanced_code_fence"),
    ),
)
def test_output_contract_rejects_malformed_structures(
    response: str,
    reason: str | None,
) -> None:
    assert output_contract_rejection_reason(response) == reason


def test_output_contract_does_not_add_broad_keyword_guard() -> None:
    response = "诊断结论：配置记录中提到了 runtime_status，但没有内部调用回执。"
    assert output_contract_rejection_reason(response) is None
    assert admit_result(AppResult(status="answered", user_response=response)).status == "answered"


@pytest.mark.parametrize(
    ("mode", "response", "expected"),
    (
        ("prose", "结论：等待。", True),
        ("prose", "```markdown\n结论：等待。\n```", False),
        ("raw_json", '{"status":"ok"}', True),
        ("raw_json", '```json\n{"status":"ok"}\n```', False),
        ("json_fence", '```json\n{"status":"ok"}\n```', True),
        ("json_fence", '{"status":"ok"}', False),
        ("markdown_fence", "```markdown\n# 结论\n等待。\n```", True),
        ("markdown_fence", "```json\n{}\n```", False),
        ("unknown", "结论：等待。", False),
    ),
)
def test_output_contract_matches_explicit_eval_mode(
    mode: str,
    response: str,
    expected: bool,
) -> None:
    assert output_contract_matches(mode, response) is expected
