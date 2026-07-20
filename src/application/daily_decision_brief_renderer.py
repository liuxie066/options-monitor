from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


_DEFAULT_MAX_ACTIONS = 5
_DEFAULT_MAX_CANDIDATES = 3
_DEFAULT_MAX_REJECTIONS = 5
_MAX_TOTAL_ITEMS = 40
_MAX_MESSAGE_CHARS = 12_000

_ACTIONABILITY_LABELS = {
    "live_actionable": "可执行（LIVE）",
    "planning_only": "仅规划（PLANNING）",
    "blocked": "阻塞（BLOCKED）",
}
_STATUS_LABELS = {
    "ready": "就绪（READY）",
    "degraded": "降级（DEGRADED）",
    "blocked": "阻塞（BLOCKED）",
}
_STATE_LABELS = {
    "active": "有效",
    "observe": "观察",
    "blocked": "阻塞",
    "invalidated": "失效",
}
_CHANGE_LABELS = {
    "blocked": "日报进入阻塞状态",
    "recovered": "日报已恢复",
    "actionability_changed": "行动有效性变化",
    "p0_added": "新增 P0 行动",
    "action_added": "新增行动",
    "priority_upgraded_to_p0": "行动升级为 P0",
    "priority_changed": "行动优先级变化",
    "action_invalidated": "原行动已失效",
    "action_removed": "原行动已移除",
    "capacity_changed": "整手容量变化",
    "full_required": "需要发送完整日报",
}


@dataclass
class _RenderBudget:
    remaining: int = _MAX_TOTAL_ITEMS

    def take(self, rows: Iterable[Any], limit: int) -> list[Any]:
        count = max(0, min(int(limit), self.remaining))
        selected = list(rows)[:count]
        self.remaining -= len(selected)
        return selected


def render_full_brief(
    brief: Mapping[str, Any],
    *,
    limits: Mapping[str, Any] | None = None,
) -> str:
    if str(brief.get("actionability") or "").strip().lower() == "blocked":
        return render_blocked_brief(brief, limits=limits)

    cfg = resolve_daily_brief_render_limits(limits)
    budget = _RenderBudget()
    lines = _header(brief, title="每日决策简报")
    summary = str(brief.get("strategy_summary") or "").strip()
    if summary:
        lines.extend(["", f"> {summary}"])

    actions = [item for item in brief.get("actions") or [] if isinstance(item, Mapping)]
    active_actions = [item for item in actions if str(item.get("state") or "").strip().lower() == "active"]
    for priority in ("P0", "P1", "P2"):
        priority_rows = [
            item for item in active_actions if str(item.get("priority") or "").upper() == priority
        ]
        if not priority_rows:
            continue
        lines.extend(["", f"## {priority} 有效行动"])
        selected = budget.take(priority_rows, cfg["max_actions_per_priority"])
        lines.extend(_action_line(item) for item in selected)
        _append_omitted(lines, len(priority_rows) - len(selected))

    _append_non_active_actions(
        lines,
        actions,
        budget=budget,
        limit=cfg["max_actions_per_priority"],
    )
    _append_positions(lines, brief, budget=budget, limit=cfg["max_actions_per_priority"])
    _append_capacity(lines, brief, budget=budget)
    _append_candidates(lines, brief, budget=budget, limit=cfg["max_candidates_per_strategy"])
    _append_rejections(lines, brief, budget=budget, limit=cfg["max_rejection_reasons"])
    _append_events(lines, brief, budget=budget, limit=cfg["max_rejection_reasons"])
    _append_data_gaps(lines, brief, budget=budget, limit=cfg["max_rejection_reasons"])
    return _bounded_markdown(lines)


def render_blocked_brief(
    brief: Mapping[str, Any],
    *,
    limits: Mapping[str, Any] | None = None,
) -> str:
    cfg = resolve_daily_brief_render_limits(limits)
    budget = _RenderBudget()
    lines = _header(brief, title="每日决策简报 · 当前阻塞")
    lines.extend(["", "## 阻塞原因"])
    blockers = [
        item
        for item in brief.get("actions") or []
        if isinstance(item, Mapping) and str(item.get("state") or "").lower() == "blocked"
    ]
    selected = budget.take(blockers, cfg["max_actions_per_priority"])
    if selected:
        lines.extend(_action_line(item) for item in selected)
        _append_omitted(lines, len(blockers) - len(selected))
    else:
        lines.append("- 关键决策数据不可用；未提供可执行行动。")

    _append_capacity(lines, brief, budget=budget)
    _append_positions(lines, brief, budget=budget, limit=cfg["max_actions_per_priority"])
    _append_data_gaps(lines, brief, budget=budget, limit=cfg["max_rejection_reasons"])
    lines.extend(
        [
            "",
            "## 下一步",
            "- 等待下一轮 scheduled scan 重新计算；阻塞解除前，本日报不构成可执行建议。",
        ]
    )
    return _bounded_markdown(lines)


def render_delta_brief(
    brief: Mapping[str, Any],
    diff: Mapping[str, Any],
    *,
    limits: Mapping[str, Any] | None = None,
) -> str:
    cfg = resolve_daily_brief_render_limits(limits)
    budget = _RenderBudget()
    lines = _header(brief, title="日内决策增量")
    lines.append(
        "- 基线：revision "
        f"{_display(diff.get('from_revision'))} → {_display(diff.get('to_revision'))}"
    )
    lines.extend(["", "## 本轮变化"])
    changes = [item for item in diff.get("changes") or [] if isinstance(item, Mapping)]
    selected = budget.take(changes, cfg["max_actions_per_priority"] * 3)
    if selected:
        lines.extend(_change_line(item) for item in selected)
        _append_omitted(lines, len(changes) - len(selected))
    else:
        lines.append("- 无 material change；本消息不应发送。")
    lines.extend(["", f"- 当前状态：{_actionability_label(brief)}"])
    return _bounded_markdown(lines)


def render_recovery_brief(
    brief: Mapping[str, Any],
    diff: Mapping[str, Any],
    *,
    limits: Mapping[str, Any] | None = None,
) -> str:
    message = render_delta_brief(brief, diff, limits=limits)
    return message.replace("# 日内决策增量", "# 日内决策恢复", 1)


def render_daily_brief_lifecycle(
    lifecycle: Mapping[str, Any],
    *,
    limits: Mapping[str, Any] | None = None,
) -> str:
    brief = lifecycle.get("brief") if isinstance(lifecycle.get("brief"), Mapping) else {}
    diff = lifecycle.get("diff") if isinstance(lifecycle.get("diff"), Mapping) else {}
    delivery_kind = str(lifecycle.get("delivery_kind") or "").strip().lower()
    if delivery_kind == "full":
        if str(brief.get("actionability") or "").strip().lower() == "blocked":
            return render_blocked_brief(brief, limits=limits)
        return render_full_brief(brief, limits=limits)
    if delivery_kind == "delta":
        changes = [item for item in diff.get("changes") or [] if isinstance(item, Mapping)]
        if any(str(item.get("change_type") or "") == "recovered" for item in changes):
            return render_recovery_brief(brief, diff, limits=limits)
        return render_delta_brief(brief, diff, limits=limits)
    return ""


def _header(brief: Mapping[str, Any], *, title: str) -> list[str]:
    account = str(brief.get("account") or "?").strip().lower()
    market = str(brief.get("market") or "?").strip().upper()
    market_date = str(brief.get("market_trading_date") or "?").strip()
    revision = _display(brief.get("revision"))
    data_as_of = str(brief.get("data_as_of_utc") or "未知").strip()
    valid_until = str(brief.get("valid_until_utc") or "未知").strip()
    return [
        f"# {title}",
        f"- 账号：`{account}` | 市场：`{market}` | 交易日：`{market_date}` | revision：`{revision}`",
        f"- 状态：{_actionability_label(brief)} | 数据质量：{_status_label(brief)} "
        f"| 数据截至：`{data_as_of}` | 有效至：`{valid_until}`",
    ]


def _append_non_active_actions(
    lines: list[str],
    actions: list[Mapping[str, Any]],
    *,
    budget: _RenderBudget,
    limit: int,
) -> None:
    rows = [
        item
        for item in actions
        if str(item.get("state") or "").strip().lower() in {"observe", "blocked", "invalidated"}
    ]
    if not rows:
        return
    lines.extend(["", "## 非执行状态（观察 / 阻塞 / 失效）"])
    selected = budget.take(rows, limit)
    lines.extend(_action_line(item) for item in selected)
    _append_omitted(lines, len(rows) - len(selected))


def _append_positions(
    lines: list[str],
    brief: Mapping[str, Any],
    *,
    budget: _RenderBudget,
    limit: int,
) -> None:
    rows = [item for item in brief.get("positions") or [] if isinstance(item, Mapping)]
    if not rows:
        return
    lines.extend(["", "## 已有仓位 / Close Advice"])
    selected = budget.take(rows, limit)
    for item in selected:
        symbol = _first(item, "symbol", "underlying", default="未知标的")
        contract = _first(item, "contract_symbol", "option_code")
        action = _first(item, "close_action", "action", "tier", default="观察")
        identity = _identity_suffix(item)
        lines.append(f"- `{symbol}` {contract} · {action}{identity}".rstrip())
    _append_omitted(lines, len(rows) - len(selected))


def _append_capacity(lines: list[str], brief: Mapping[str, Any], *, budget: _RenderBudget) -> None:
    capacity = brief.get("capacity")
    if not isinstance(capacity, Mapping) or not capacity:
        return
    lines.extend(["", "## 行动容量"])
    selected = budget.take(sorted(capacity.items(), key=lambda item: str(item[0])), len(capacity))
    for kind, raw in selected:
        item = raw if isinstance(raw, Mapping) else {}
        contracts = item.get("contracts_available")
        reason = str(item.get("reason") or "").strip()
        suffix = f" | {reason}" if reason else ""
        lines.append(f"- `{kind}`：可行动整手 `{_display(contracts)}`{suffix}")
    _append_omitted(lines, len(capacity) - len(selected))


def _append_candidates(
    lines: list[str],
    brief: Mapping[str, Any],
    *,
    budget: _RenderBudget,
    limit: int,
) -> None:
    candidates = brief.get("candidates")
    if not isinstance(candidates, Mapping):
        return
    labels = {"sell_put": "Sell Put", "covered_call": "Covered Call", "combo_yield": "Combo Yield"}
    for family in ("sell_put", "covered_call", "combo_yield"):
        rows = [item for item in candidates.get(family) or [] if isinstance(item, Mapping)]
        if not rows:
            continue
        lines.extend(["", f"## {labels[family]} 候选证据（非行动）"])
        selected = budget.take(rows, limit)
        for item in selected:
            symbol = _first(item, "symbol", default="未知标的")
            contracts = [
                value
                for value in (
                    _first(item, "contract_symbol"),
                    _first(item, "put_contract_symbol"),
                    _first(item, "call_contract_symbol"),
                )
                if value
            ]
            contract_text = " / ".join(contracts) or "无合约标识"
            priority = _first(item, "priority")
            rank = _display(item.get("rank"))
            suffix = f" | {priority}" if priority else ""
            lines.append(f"- #{rank} `{symbol}` · {contract_text}{suffix}")
        _append_omitted(lines, len(rows) - len(selected))


def _append_rejections(
    lines: list[str],
    brief: Mapping[str, Any],
    *,
    budget: _RenderBudget,
    limit: int,
) -> None:
    rejections = brief.get("rejections")
    if not isinstance(rejections, Mapping):
        return
    rows = [item for item in rejections.get("top_categories") or [] if isinstance(item, Mapping)]
    if not rows:
        return
    lines.extend(["", "## 主要拒绝原因"])
    selected = budget.take(rows, limit)
    for item in selected:
        category = _first(item, "category", "rule", "reason", default="unknown")
        count = _display(item.get("count"))
        samples = item.get("sample_symbols")
        sample_text = ", ".join(str(value) for value in samples[:3]) if isinstance(samples, list) else ""
        suffix = f" | 样例 {sample_text}" if sample_text else ""
        lines.append(f"- `{category}`：{count} 条{suffix}")
    _append_omitted(lines, len(rows) - len(selected))


def _append_events(
    lines: list[str],
    brief: Mapping[str, Any],
    *,
    budget: _RenderBudget,
    limit: int,
) -> None:
    rows = [item for item in brief.get("events") or [] if isinstance(item, Mapping)]
    if not rows:
        return
    lines.extend(["", "## 事件"])
    selected = budget.take(rows, limit)
    for item in selected:
        event_type = _first(item, "event_type", "kind", "type", default="event")
        symbol = _first(item, "symbol")
        reason = _first(item, "reason", "status")
        parts = [value for value in (symbol, reason) if value]
        lines.append(f"- `{event_type}`" + (f" · {' | '.join(parts)}" if parts else ""))
    _append_omitted(lines, len(rows) - len(selected))


def _append_data_gaps(
    lines: list[str],
    brief: Mapping[str, Any],
    *,
    budget: _RenderBudget,
    limit: int,
) -> None:
    rows = [item for item in brief.get("data_gaps") or [] if isinstance(item, Mapping)]
    if not rows:
        return
    lines.extend(["", "## 数据缺口"])
    selected = budget.take(rows, limit)
    for item in selected:
        scope = _first(item, "scope", "strategy_family", "symbol", default="unknown")
        reason = _first(item, "reason", "error_type", default="unspecified")
        symbol = _first(item, "symbol")
        suffix = f" | `{symbol}`" if symbol else ""
        lines.append(f"- `{scope}`{suffix}：{reason}")
    _append_omitted(lines, len(rows) - len(selected))


def _action_line(action: Mapping[str, Any]) -> str:
    priority = str(action.get("priority") or "P2").upper()
    state = str(action.get("state") or "observe").lower()
    title = _first(action, "title", "action_type", default="行动")
    symbol = _first(action, "symbol")
    contract = _first(action, "contract_symbol")
    reason = _first(action, "reason")
    identity = _identity_suffix(action)
    subject = " ".join(value for value in (symbol, contract) if value)
    details = " | ".join(value for value in (subject, reason) if value)
    suffix = f" · {details}" if details else ""
    return f"- **{priority}** [{_STATE_LABELS.get(state, state)}] {title}{suffix}{identity}"


def _change_line(change: Mapping[str, Any]) -> str:
    change_type = str(change.get("change_type") or "unknown")
    priority = str(change.get("priority") or "P2").upper()
    label = _CHANGE_LABELS.get(change_type, change_type)
    action = change.get("action") if isinstance(change.get("action"), Mapping) else {}
    symbol = _first(action, "symbol")
    contract = _first(action, "contract_symbol")
    subject = " ".join(value for value in (symbol, contract) if value)
    before = change.get("before")
    after = change.get("after")
    detail_parts = [subject] if subject else []
    if before is not None or after is not None:
        detail_parts.append(f"{_display(before)} → {_display(after)}")
    capacity_kind = _first(change, "capacity_kind")
    if capacity_kind:
        detail_parts.append(capacity_kind)
    suffix = f" · {' | '.join(detail_parts)}" if detail_parts else ""
    return f"- **{priority}** {label}{suffix}{_identity_suffix(action)}"


def _identity_suffix(item: Mapping[str, Any]) -> str:
    parts = []
    for key in ("position_lot_id", "strategy_group_id", "leg_role"):
        value = str(item.get(key) or "").strip()
        if value:
            parts.append(f"{key}={value}")
    return f" | {'; '.join(parts)}" if parts else ""


def _actionability_label(brief: Mapping[str, Any]) -> str:
    value = str(brief.get("actionability") or "blocked").strip().lower()
    return _ACTIONABILITY_LABELS.get(value, f"未知（{value.upper()}）")


def _status_label(brief: Mapping[str, Any]) -> str:
    value = str(brief.get("status") or "missing").strip().lower()
    return _STATUS_LABELS.get(value, f"未知（{value.upper()}）")


def resolve_daily_brief_render_limits(value: Mapping[str, Any] | None) -> dict[str, int]:
    src = value if isinstance(value, Mapping) else {}
    return {
        "max_actions_per_priority": _positive_int(src.get("max_actions_per_priority"), _DEFAULT_MAX_ACTIONS),
        "max_candidates_per_strategy": _positive_int(src.get("max_candidates_per_strategy"), _DEFAULT_MAX_CANDIDATES),
        "max_rejection_reasons": _positive_int(src.get("max_rejection_reasons"), _DEFAULT_MAX_REJECTIONS),
    }


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(1, min(parsed, 20))


def _first(item: Mapping[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return default


def _display(value: Any) -> str:
    if value is None or value == "":
        return "未知"
    return str(value)


def _append_omitted(lines: list[str], count: int) -> None:
    if count > 0:
        lines.append(f"- … 另有 {count} 条已按展示上限省略")


def _bounded_markdown(lines: list[str]) -> str:
    message = "\n".join(lines).strip()
    if len(message) <= _MAX_MESSAGE_CHARS:
        return message
    marker = "\n\n- … 消息已按总长度上限截断；完整结构化日报仍保存在本地。"
    return message[: _MAX_MESSAGE_CHARS - len(marker)].rstrip() + marker


__all__ = [
    "resolve_daily_brief_render_limits",
    "render_blocked_brief",
    "render_daily_brief_lifecycle",
    "render_delta_brief",
    "render_full_brief",
    "render_recovery_brief",
]
