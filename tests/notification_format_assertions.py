from __future__ import annotations

import re


_NESTED_LIST_RE = re.compile(r"^(?:\t+| {2,})(?:[-*+]|\d+\.)\s", re.MULTILINE)


def assert_mobile_flat_markdown(message: str, *, require_title: bool = True) -> None:
    lines = str(message or "").splitlines()
    if require_title:
        assert sum(line.startswith("# ") for line in lines) == 1
    assert not any(line.startswith("###") for line in lines)
    assert not any(line.startswith(">") for line in lines)
    assert _NESTED_LIST_RE.search(message) is None
    assert not any(line.strip().startswith("|") and line.strip().endswith("|") for line in lines)
