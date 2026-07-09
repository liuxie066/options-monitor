from __future__ import annotations

import json
from typing import Any


FORBIDDEN_EXTERNAL_ACTION_CLAIMS = (
    "已写入",
    "已发送通知",
    "已下单",
    "已修改配置",
    "pushed release",
    "deployed",
)


def contains_forbidden_external_action_claim(value: Any) -> bool:
    text = _claim_text(value)
    return any(claim in text for claim in FORBIDDEN_EXTERNAL_ACTION_CLAIMS)


def _claim_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value or "")
