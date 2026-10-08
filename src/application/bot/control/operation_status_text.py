from __future__ import annotations


def user_facing_operation_status(status: str) -> str:
    normalized = str(status or "").strip().lower()
    return {
        "previewed": "等待确认",
        "confirmed": "确认已收到，正在执行或等待结果",
        "running": "正在执行",
        "applied": "已执行完成",
        "failed": "执行失败",
        "expired": "确认已过期",
        "cancelled": "已取消",
    }.get(normalized, "状态未知")


def cannot_repeat_message(subject: str, action: str, status: str) -> str:
    return f"这条{subject}不能再次{action}，当前进度：{user_facing_operation_status(status)}。"


def operation_candidate_hint(prefix: str, candidates: object, *, heading: str) -> str:
    lines = operation_candidate_summary_lines(candidates, prefix=prefix)
    if not lines:
        return f"请回复：{prefix} <operation_id>"
    return f"\n{heading}：\n" + "\n".join(lines)


def operation_candidate_summary_lines(candidates: object, *, prefix: str) -> list[str]:
    rows = candidates if isinstance(candidates, list) else []
    lines: list[str] = []
    for idx, item_raw in enumerate(rows[:5], start=1):
        if not isinstance(item_raw, dict):
            continue
        operation_id = str(item_raw.get("operation_id") or "").strip()
        if not operation_id:
            continue
        summary = str(item_raw.get("summary") or item_raw.get("operation_type") or "-").strip()
        command = f"{prefix} {operation_id}".strip()
        if command:
            lines.append(f"{idx}. {operation_id} | {summary} | 回复：{command}")
        else:
            lines.append(f"{idx}. {operation_id} | {summary}")
    return lines
