from __future__ import annotations

import re


_NESTED_LIST_RE = re.compile(r"^(?:\t+| {2,})(?:[-*+]|\d+\.)\s", re.MULTILINE)


def assert_mobile_flat_markdown(message: str, *, require_title: bool = True) -> None:
    lines = str(message or "").splitlines()
    if require_title:
        assert sum(line.startswith("# ") for line in lines) == 1
    # The only sanctioned per-strategy submodule headings are AI advice and
    # the candidate list (docs/AI_DECISION_ADVICE_DESIGN.md 15.1).
    allowed_subheadings = {
        "### AI建议",
        "### 策略候选",
        "### 新增策略候选",
    }
    assert not any(
        line.startswith("###") and line not in allowed_subheadings
        for line in lines
    )
    assert not any(line.startswith(">") for line in lines)
    assert _NESTED_LIST_RE.search(message) is None
    assert not any(line.strip().startswith("|") and line.strip().endswith("|") for line in lines)
