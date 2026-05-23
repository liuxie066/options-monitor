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
