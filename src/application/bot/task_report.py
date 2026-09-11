from __future__ import annotations

import re
from typing import Any


TASK_TOOL = "scheduled_tasks_read"
TASK_MARKER = "[[scheduled_tasks]]"


def task_market(value: Any) -> str:
    market = str(value or "").strip().lower()
    return market if market in {"us", "hk"} else "unknown"


def _cell(value: Any) -> str:
    # Unit names are data, including when a damaged profile contains Markdown.
    text = "未知" if value is None or value == "" else "是" if value is True else "否" if value is False else str(value)
    return re.sub(r"([\\`*_{}\[\]()#+.!|<>])", r"\\\1", text.replace("\n", " ").replace("\r", " "))


def render_task_report(reads: dict, *, narrowed: bool = False) -> tuple[str, str, set[str]]:
    """Render only the bounded projections actually delivered in this request."""
    markets = sorted({str(item["market"]).upper() for item in reads.values()})
    scope = "、".join(markets)
    lines = [f"任务查询范围：{scope}；仅限当前授权的 OM 部署清单，不代表整台机器。"]
    diagnostics: list[str] = []
    narrowing = narrowed or any(item["observation"].get("status") == "needs_narrowing" for item in reads.values())
    ids: set[str] = set()
    partial = False
    for item in reads.values():
        market = str(item["market"]).upper()
        obs = item["observation"]
        value = obs.get("value") if isinstance(obs.get("value"), dict) else {}
        rows = value.get("tasks")
        if obs.get("status") == "needs_narrowing":
            continue
        if obs.get("ok") is not True or not isinstance(rows, list) or value.get("availability") == "unavailable":
            partial = True
            reason = obs.get("diagnostic", "read_unavailable")
            messages = {
                "scope_conflict": "超出当前授权范围",
                "input_invalid": "查询参数无效",
                "POLICY_ERROR": "当前入口不支持此查询",
                "CONFIG_ERROR": "配置或部署清单不可用",
                "read_unavailable": "数据读取不可用",
                "provider_unsupported": "当前服务平台不支持此查询",
                "profile_missing": "部署清单缺失",
                "profile_unreadable": "部署清单无法读取",
                "profile_services_invalid": "部署清单的服务列表无效",
                "profile_runtime_mismatch": "部署清单与运行目录不匹配",
                "profile_market_unbound": "部署清单未绑定当前市场",
                "profile_config_unbound": "部署清单未绑定当前配置",
                "profile_config_mismatch": "部署清单与当前配置不匹配",
            }
            if obs.get("ok") is True:
                reason = next((code for code in value.get("reasons", []) if code in messages), reason)
            diagnostics.append(f"{market} 当前无法核实：{messages.get(reason, '数据读取失败')}；本次未自动重试，可发起新查询。")
            continue
        ids.add(str(obs["ref"]))
        incomplete = obs.get("status") == "partial" or obs.get("coverage", {}).get("status") != "complete"
        partial |= incomplete
        lines.append(f"{market}：已核实 {len(rows)} 项任务" + ("，覆盖不完整或部分状态未知。" if incomplete else "（当前授权清单内）。"))
        if value.get("reasons"):
            lines.append("查询限制：" + "、".join(_cell(reason) for reason in value["reasons"]) + "。")
        for row in rows:
            lines.append(
                f"- {_cell(row.get('name'))}：已配置={_cell(row.get('configured'))}；"
                f"启用={_cell(row.get('enabled'))}；活动={_cell(row.get('active'))}"
                + ("；原因=" + "、".join(_cell(reason) for reason in row["reasons"]) if row.get("reasons") else "")
            )
    if narrowing:
        return "\n".join([lines[0], *diagnostics, "结果无法完整呈现，请缩小查询范围后重试。"]), "needs_narrowing", set()
    return "\n".join([lines[0], *diagnostics, *lines[1:]]), ("insufficient_evidence" if not ids else "partial" if partial else "complete"), ids
