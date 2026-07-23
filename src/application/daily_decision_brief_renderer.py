from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_DEFAULT_MAX_ACTIONS = 5
_DEFAULT_MAX_CANDIDATES = 3
_DEFAULT_MAX_REJECTIONS = 5
_MAX_TOTAL_ITEMS = 40
_MAX_MESSAGE_CHARS = 12_000

# 飞书 post 消息的 md 标签会把纯空行折叠掉，段落之间没有可视间距；
# 用零宽空格撑出可见空行（不是空白字符，不会被渲染端或 str.strip() 移除）。
_VISIBLE_BLANK_LINE = "\u200b"

_MARKET_LABELS = {"US": "美股", "HK": "港股", "CN": "A股"}
_MARKET_TIMEZONES = {"US": "America/New_York", "HK": "Asia/Hong_Kong", "CN": "Asia/Shanghai"}
_MARKET_TIME_LABELS = {"US": "美东", "HK": "香港", "CN": "北京时间"}
_STRATEGY_LABELS = {
    "sell_put": "Sell Put",
    "covered_call": "Covered Call",
    "combo_yield": "组合增强",
}
_OPTION_LABELS = {"put": "Put", "call": "Call"}
_EVENT_TYPE_LABELS = {"earnings": "财报", "ex_dividend": "除息", "split": "拆股"}
_COMBO_LEG_LABELS = {
    "funding_put": "Put 侧",
    "sell_put": "Put 侧",
    "put": "Put 侧",
    "participation_call": "Call 侧",
    "covered_call": "Call 侧",
    "call": "Call 侧",
}
_CLOSE_ACTION_LABELS = {
    "close": "建议平仓",
    "close_put_keep_call": "建议平掉 Put，保留 Call",
    "hold_put_keep_call": "继续持有 Put，保留 Call",
    "sell_call_take_profit": "建议卖出 Call 止盈",
    "sell_call_salvage": "建议卖出 Call 回收价值",
    "hold_to_expiry_or_expire": "继续持有至到期",
    "hold_call_as_convexity": "继续持有 Call",
    "hold_call": "继续持有 Call",
    "hold": "继续观察",
}
_STANDARD_CLOSE_TIER_LABELS = {
    "strong": "强烈建议平仓",
    "medium": "建议平仓",
    "weak": "可观察平仓",
    "optional": "低价买回可选",
}
_CLOSE_DETAIL_ACTIONS = {
    "close",
    "close_put_keep_call",
    "sell_call_take_profit",
    "sell_call_salvage",
}


@dataclass
class _RenderBudget:
    remaining: int = _MAX_TOTAL_ITEMS

    def take(self, rows: Iterable[Any], limit: int) -> list[Any]:
        count = max(0, min(int(limit), self.remaining))
        selected = list(rows)[:count]
        self.remaining -= len(selected)
        return selected


def build_daily_brief_user_view(
    brief: Mapping[str, Any],
    *,
    diff: Mapping[str, Any] | None = None,
    delivery_kind: str = "full",
    limits: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project canonical audit facts into an allowlisted user-facing view."""

    cfg = resolve_daily_brief_render_limits(limits)
    ctx = dict(context or {})
    market = _upper(brief.get("market"))
    ctx.setdefault("market", market)
    account = _lower(brief.get("account")) or "-"
    actionability = _lower(brief.get("actionability"))
    normalized_diff = dict(diff or {})
    phase = _phase_label(
        actionability=actionability,
        delivery_kind=delivery_kind,
        diff=normalized_diff,
        context=ctx,
    )
    scheduled_batch = _scheduled_batch_label(ctx, market=market)
    if delivery_kind == "fixed_report" and scheduled_batch:
        phase_line = f"{scheduled_batch} 批次"
    else:
        phase_line = phase
        if scheduled_batch and delivery_kind not in {"candidate_alert", "query"}:
            phase_line += f" · {scheduled_batch} 批次"

    candidate_views, candidate_omissions, selected_candidate_rows = _candidate_views(
        brief,
        diff=normalized_diff,
        limits=cfg,
    )
    position_views, position_total, position_actionable_total = _position_views(
        brief,
        diff=normalized_diff,
        limits=cfg,
    )
    capacity, reminders = _capacity_views(brief, selected_rows=selected_candidate_rows)
    strategy_failure_labels = _strategy_failure_labels(brief)
    if strategy_failure_labels:
        reminders.append(f"{'、'.join(strategy_failure_labels)} 扫描异常，本轮结果不完整")
    view = {
        "account": account,
        "market": market,
        "market_label": _MARKET_LABELS.get(market, "市场"),
        "phase_line": phase_line,
        "data_as_of": _data_as_of_label(brief, context=ctx),
        "planning_notice": ("当前已不在可执行时段，仅供规划参考。" if actionability == "planning_only" else ""),
        "blocked": actionability == "blocked",
        "blocked_summary": _blocked_summary(brief),
        "change_summaries": _change_summaries(normalized_diff, market=market),
        "candidates": candidate_views,
        "candidate_omissions": candidate_omissions,
        "candidate_empty_summary": (
            "本轮候选结果不完整，部分策略扫描失败。"
            if strategy_failure_labels
            else "本轮暂无符合条件的候选。"
        ),
        "positions": position_views,
        "position_total": position_total,
        "position_actionable_total": position_actionable_total,
        "funds": _fund_views(brief),
        "capacity": capacity,
        "reminders": reminders,
    }
    return view


def render_fixed_report(
    brief: Mapping[str, Any],
    *,
    limits: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
) -> str:
    view = build_daily_brief_user_view(
        brief,
        delivery_kind="fixed_report",
        limits=limits,
        context=context,
    )
    return _render_user_view(view, projection="fixed_report")


def render_candidate_alert(
    brief: Mapping[str, Any],
    candidate_identities: Iterable[str],
    *,
    limits: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
) -> str:
    filtered, omitted = _candidate_alert_brief(brief, candidate_identities)
    alert_limits = dict(limits or {})
    alert_limits["max_candidates_per_strategy"] = _DEFAULT_MAX_CANDIDATES
    view = build_daily_brief_user_view(
        filtered,
        delivery_kind="candidate_alert",
        limits=alert_limits,
        context=context,
    )
    if omitted:
        view["candidate_omissions"] = [
            f"另有 {omitted} 个新增候选未展开，可随时查询“期权监控”查看最新结果"
        ]
    return _render_user_view(view, projection="candidate_alert")


def render_fixed_failure(
    failure: Mapping[str, Any],
    *,
    context: Mapping[str, Any] | None = None,
) -> str:
    market = _upper(failure.get("market"))
    account = _lower(failure.get("account")) or "-"
    batch = _scheduled_batch_label(dict(context or {}), market=market)
    phase = f"数据异常 · {batch} 批次失败" if batch else "数据异常 · 本轮批次失败"
    return _bounded_markdown(
        [
            f"# OM · 决策简报 · {account}",
            _VISIBLE_BLANK_LINE,
            f"状态｜{phase}",
            f"市场｜{_MARKET_LABELS.get(market, '市场')}",
            "结论｜本轮策略扫描未形成可靠结果，无法生成正常报告。",
            "后续｜下一计划扫描会继续尝试。",
        ]
    )


def render_query_brief(
    brief: Mapping[str, Any],
    *,
    limits: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
    heading_level: int = 1,
) -> str:
    view = build_daily_brief_user_view(
        brief,
        delivery_kind="query",
        limits=limits,
        context=context,
    )
    return _render_user_view(
        view,
        projection="query",
        heading_level=heading_level,
        query_status=_query_status_lines(brief, context=dict(context or {})),
    )


def render_full_brief(
    brief: Mapping[str, Any],
    *,
    limits: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
) -> str:
    view = build_daily_brief_user_view(
        brief,
        delivery_kind="current",
        limits=limits,
        context=context,
    )
    return _render_user_view(view)


def render_blocked_brief(
    brief: Mapping[str, Any],
    *,
    limits: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
) -> str:
    view = build_daily_brief_user_view(
        brief,
        delivery_kind="full",
        limits=limits,
        context=context,
    )
    return _render_user_view(view)


def render_delta_brief(
    brief: Mapping[str, Any],
    diff: Mapping[str, Any],
    *,
    limits: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
) -> str:
    view = build_daily_brief_user_view(
        brief,
        diff=diff,
        delivery_kind="delta",
        limits=limits,
        context=context,
    )
    return _render_user_view(view)


def render_recovery_brief(
    brief: Mapping[str, Any],
    diff: Mapping[str, Any],
    *,
    limits: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
) -> str:
    return render_delta_brief(brief, diff, limits=limits, context=context)


def render_daily_brief_lifecycle(
    lifecycle: Mapping[str, Any],
    *,
    limits: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
) -> str:
    brief = lifecycle.get("brief")
    if not isinstance(brief, Mapping):
        raise ValueError("daily brief lifecycle is missing brief")
    delivery_kind = _lower(lifecycle.get("delivery_kind"))
    if delivery_kind == "none":
        return ""
    if delivery_kind not in {"full", "delta"}:
        raise ValueError(f"unsupported daily brief delivery kind: {delivery_kind}")
    diff = lifecycle.get("diff") if isinstance(lifecycle.get("diff"), Mapping) else {}
    view = build_daily_brief_user_view(
        brief,
        diff=diff,
        delivery_kind=delivery_kind,
        limits=limits,
        context=context,
    )
    return _render_user_view(view)


def _render_user_view(
    view: Mapping[str, Any],
    *,
    projection: str = "legacy",
    heading_level: int = 1,
    query_status: Iterable[str] = (),
) -> str:
    heading_level = max(1, min(int(heading_level), 5))
    title_mark = "#" * heading_level
    section_mark = "#" * (heading_level + 1)
    lines = [
        f"{title_mark} OM · 决策简报 · {view['account']}",
        _VISIBLE_BLANK_LINE,
        f"状态｜{view['phase_line']}",
        f"市场｜{view['market_label']}",
        f"数据｜{_strip_display_label(view['data_as_of'], '数据截至：')}",
    ]
    lines.extend(_flat_field_line(item) for item in query_status if str(item).strip())
    planning_notice = str(view.get("planning_notice") or "")
    if planning_notice:
        lines.extend([_VISIBLE_BLANK_LINE, f"提示｜{planning_notice}"])

    if bool(view.get("blocked")):
        lines.extend(
            [
                _VISIBLE_BLANK_LINE,
                f"结论｜{view.get('blocked_summary') or '本轮关键数据不可用，暂时无法形成可靠决策。'}",
                "后续｜系统将在后续批次自动重新评估。",
            ]
        )
        return _bounded_markdown(lines)

    changes = [str(item) for item in view.get("change_summaries") or [] if str(item).strip()]
    if changes:
        lines.extend([_VISIBLE_BLANK_LINE, "变化｜" + "；".join(changes) + "。"])

    candidates = [item for item in view.get("candidates") or [] if isinstance(item, Mapping)]
    candidate_heading = "新增候选" if projection == "candidate_alert" else "当前候选"
    if projection == "legacy":
        candidate_heading = "候选"
    lines.extend([_VISIBLE_BLANK_LINE, f"{section_mark} {candidate_heading}"])
    if not candidates:
        lines.append(str(view.get("candidate_empty_summary") or "本轮暂无符合条件的候选。"))
    else:
        for index, item in enumerate(candidates, start=1):
            lines.extend([_VISIBLE_BLANK_LINE, f"**{index}｜{_flat_title(item['title'])}**"])
            for leg in item.get("legs") or []:
                lines.append(_flat_field_line(leg))
            for detail in item.get("details") or []:
                lines.append(f"{_candidate_detail_label(detail)}｜{detail}")
        for note in view.get("candidate_omissions") or []:
            lines.append(f"补充｜{note}")

    positions = [item for item in view.get("positions") or [] if isinstance(item, Mapping)]
    if projection != "candidate_alert":
        lines.extend([_VISIBLE_BLANK_LINE, f"{section_mark} 持仓"])
        position_total = _whole_number(view.get("position_total")) or 0
        position_actionable_total = _whole_number(view.get("position_actionable_total")) or 0
        position_summary = f"汇总｜共 {position_total} 条"
        if position_actionable_total:
            position_summary += f"，需处理 {position_actionable_total} 条"
            if len(positions) < position_actionable_total:
                position_summary += f"，本消息展示 {len(positions)} 条"
        else:
            position_summary += "，当前没有需要处理的持仓"
        lines.append(position_summary + "。")
        for item in positions:
            lines.extend([_VISIBLE_BLANK_LINE, f"**{_flat_title(item['title'])}｜{item['status']}**"])
            for detail in item.get("details") or []:
                lines.append(f"参考｜{detail}")

    funds = [str(item) for item in view.get("funds") or [] if str(item).strip()]
    capacity = [str(item) for item in view.get("capacity") or [] if str(item).strip()]
    lines.extend([_VISIBLE_BLANK_LINE, f"{section_mark} 资金"])
    lines.extend(_flat_field_line(item) for item in [*funds, *capacity])

    reminders = [str(item) for item in view.get("reminders") or [] if str(item).strip()]
    if reminders:
        lines.extend([_VISIBLE_BLANK_LINE, f"{section_mark} 提醒"])
        lines.extend(_flat_field_line(item) for item in reminders)

    return _bounded_markdown(lines)


def _strip_display_label(value: Any, prefix: str) -> str:
    text = str(value or "").strip()
    return text.removeprefix(prefix).strip()


def _flat_title(value: Any) -> str:
    return str(value or "").strip().replace(" · ", "｜")


def _flat_field_line(value: Any) -> str:
    text = str(value or "").strip()
    if "：" in text:
        label, body = text.split("：", 1)
        return f"{label}｜{body}"
    return text


def _candidate_detail_label(value: Any) -> str:
    text = str(value or "").strip()
    if "执行前" in text or text.startswith(("近期事件", "已确认", "预计")):
        return "事件"
    return "指标"


def _candidate_views(
    brief: Mapping[str, Any],
    *,
    diff: Mapping[str, Any],
    limits: Mapping[str, int],
) -> tuple[list[dict[str, Any]], list[str], dict[str, list[Mapping[str, Any]]]]:
    candidates = brief.get("candidates")
    source = candidates if isinstance(candidates, Mapping) else {}
    changed_keys = _changed_candidate_keys(diff)
    budget = _RenderBudget()
    out: list[dict[str, Any]] = []
    omissions: list[str] = []
    selected_by_family: dict[str, list[Mapping[str, Any]]] = {}
    limit = limits["max_candidates_per_strategy"]
    market = _upper(brief.get("market"))
    for family in ("sell_put", "covered_call", "combo_yield"):
        rows = [item for item in source.get(family) or [] if isinstance(item, Mapping)]
        changed_rows = [row for row in rows if _candidate_row_keys(family, row) & changed_keys]
        unchanged_rows = [row for row in rows if row not in changed_rows]
        selected = budget.take([*changed_rows, *unchanged_rows], max(limit, len(changed_rows)))
        selected_by_family[family] = selected
        omitted = len(rows) - len(selected)
        if omitted > 0:
            omissions.append(f"{_STRATEGY_LABELS[family]} 另有 {omitted} 个候选未展开")
        for position, row in enumerate(selected, start=1):
            rank = _positive_rank(row.get("rank"), fallback=position)
            choice = "首选" if rank == 1 else f"备选 {rank}"
            symbol = _upper(row.get("symbol")) or "未知标的"
            if family == "combo_yield":
                put_contract = _human_contract(
                    expiration=row.get("put_expiration"),
                    strike=row.get("put_strike"),
                    option_type="put",
                    market=market,
                )
                call_contract = _human_contract(
                    expiration=row.get("call_expiration"),
                    strike=row.get("call_strike"),
                    option_type="call",
                    market=market,
                )
                put_leg = f"Put：{put_contract}"
                put_ref = _number(row.get("put_sell_reference"))
                if put_ref is not None:
                    put_leg += f" · 卖出参考 {_money(put_ref, market=market)}"
                call_leg = f"Call：{call_contract}"
                call_ref = _number(row.get("call_buy_reference"))
                if call_ref is not None:
                    call_leg += f" · 买入参考 {_money(call_ref, market=market)}"
                out.append(
                    {
                        "family": family,
                        "title": f"{symbol} · 组合增强（{choice}）",
                        "legs": [put_leg, call_leg],
                        "details": [
                            *_candidate_metric_details(row, family=family, market=market),
                            _candidate_event_line(row, family=family),
                        ],
                    }
                )
                continue

            contract = _human_contract(
                expiration=row.get("expiration"),
                strike=row.get("strike"),
                option_type=row.get("option_type"),
                market=market,
            )
            out.append(
                {
                    "family": family,
                    "title": (f"{symbol} · {_STRATEGY_LABELS[family]} · {contract}（{choice}）"),
                    "details": [
                        *_candidate_metric_details(row, family=family, market=market),
                        _candidate_event_line(row, family=family),
                    ],
                    "legs": [],
                }
            )
    return out, omissions, selected_by_family


def _candidate_metric_details(
    candidate: Mapping[str, Any],
    *,
    family: str,
    market: str,
) -> list[str]:
    metrics = candidate.get("metrics")
    values = metrics if isinstance(metrics, Mapping) else {}
    parts: list[str] = []
    mid = _number(values.get("mid"))
    bid = _number(values.get("bid"))
    ask = _number(values.get("ask"))
    if mid is not None:
        parts.append(f"权利金 {_money(mid, market=market)}")
    elif bid is not None or ask is not None:
        bid_text = _money(bid, market=market) if bid is not None else "-"
        ask_text = _money(ask, market=market) if ask is not None else "-"
        parts.append(f"Bid/Ask {bid_text}/{ask_text}")

    annualized_key = {
        "sell_put": "annualized_net_return_on_cash_basis",
        "covered_call": "annualized_net_premium_return",
        "combo_yield": "annualized_net_credit_yield",
    }.get(family)
    annualized = _number(values.get(annualized_key)) if annualized_key else None
    if annualized is not None:
        parts.append(f"年化 {_percent(annualized)}")
    delta = _number(values.get("delta"))
    if delta is not None:
        parts.append(f"Delta {delta:.2f}")
    dte = _number(values.get("dte"))
    if dte is not None:
        parts.append(f"{max(0, int(dte))} 天")
    net_income = _number(values.get("net_income"))
    if net_income is not None:
        parts.append(f"预计净收入 {_money(net_income, market=market)}")
    return [" · ".join(parts)] if parts else []


def _candidate_event_line(candidate: Mapping[str, Any], *, family: str) -> str:
    risk = candidate.get("event_risk") if isinstance(candidate.get("event_risk"), Mapping) else {}
    state = _lower(risk.get("user_state"))
    if state == "confirmed_none":
        return "已确认当前期权到期前没有近期重要事件；执行前仍需复核报价。"
    if state != "confirmed_event":
        return "近期事件数据不完整，当前无法确认没有重要事件；执行前需要再次检查。"

    event = risk.get("nearest_event") if isinstance(risk.get("nearest_event"), Mapping) else {}
    event_label = _event_label(event)
    relation = _event_expiry_relation_text(risk, family=family)
    if not event_label:
        return "近期事件数据不完整，当前无法确认没有重要事件；执行前需要再次检查。"
    relation_text = f"，{relation}" if relation else ""
    return f"预计 {event_label}{relation_text}；执行前需要重新确认事件窗口和报价。"


def _position_views(
    brief: Mapping[str, Any],
    *,
    diff: Mapping[str, Any],
    limits: Mapping[str, int],
) -> tuple[list[dict[str, Any]], int, int]:
    positions = [item for item in brief.get("positions") or [] if isinstance(item, Mapping)]
    total = len(positions)
    # 无行动指向的持仓（继续观察、无法评估）不逐条展示；
    # 总持仓数和需处理数在汇总中分开呈现，避免把非行动项误说成“折叠”。
    positions = [row for row in positions if _position_has_advice(row)]
    actionable_total = len(positions)
    changed_keys = _changed_position_keys(diff)
    changed_positions = [row for row in positions if _position_row_keys(row) & changed_keys]
    unchanged_positions = [row for row in positions if row not in changed_positions]
    selected = _RenderBudget().take(
        [*changed_positions, *unchanged_positions],
        max(limits["max_actions_per_priority"], len(changed_positions)),
    )
    market = _upper(brief.get("market"))
    out: list[dict[str, Any]] = []
    for row in selected:
        symbol = _upper(row.get("symbol")) or "未知标的"
        strategy = _position_strategy_label(row)
        contract = _position_contract_label(row, market=market)
        title_parts = [symbol]
        if strategy:
            title_parts.append(strategy)
        if contract:
            title_parts.append(contract)
        status = _position_status_label(row)
        out.append(
            {
                "title": " · ".join(title_parts),
                "status": status,
                "details": _position_close_details(row, market=market, status=status),
            }
        )
    return out, total, actionable_total


def _position_strategy_label(row: Mapping[str, Any]) -> str:
    family = _lower(row.get("strategy_family"))
    if family == "combo_yield":
        leg = _COMBO_LEG_LABELS.get(_lower(row.get("leg_role")))
        return f"组合增强（{leg}）" if leg else "组合增强"
    return _STRATEGY_LABELS.get(family, "")


def _position_contract_label(row: Mapping[str, Any], *, market: str) -> str:
    if not any(row.get(key) not in (None, "") for key in ("expiration", "strike", "option_type")):
        return ""
    return _human_contract(
        expiration=row.get("expiration"),
        strike=row.get("strike"),
        option_type=row.get("option_type"),
        market=market,
    )


def _position_status_label(row: Mapping[str, Any]) -> str:
    evaluation = _lower(row.get("evaluation_status"))
    quote = _lower(row.get("quote_status"))
    statuses = {evaluation, quote}
    if "coverage_missing" in statuses:
        return "暂无法评估（行情覆盖不足）"
    if statuses & {"quote_unusable", "unavailable"}:
        return "暂无法评估（价格不可用）"
    if statuses & {"not_evaluable", "error", "blocked"}:
        return "暂无法评估（报价质量不足）"

    known_evaluation = {"", "evaluable", "evaluated", "ready", "priced"}
    known_quote = {"", "available", "fresh", "priced", "ready"}
    if evaluation not in known_evaluation or quote not in known_quote:
        return "暂无法评估（报价质量不足）"

    action = _lower(row.get("close_action"))
    if action == "not_evaluable":
        return "暂无法评估（报价质量不足）"
    if action == "close":
        return _STANDARD_CLOSE_TIER_LABELS.get(
            _lower(row.get("tier")),
            _CLOSE_ACTION_LABELS[action],
        )
    if action in _CLOSE_ACTION_LABELS:
        return _CLOSE_ACTION_LABELS[action]
    tier = _lower(row.get("tier"))
    if tier in {"strong", "medium"}:
        return "建议复核持仓"
    if tier in {"", "observe", "weak", "none"}:
        return "继续观察"
    return "暂无法评估（报价质量不足）"


_POSITION_ACTIONABLE_LABELS = frozenset(
    [
        *(_CLOSE_ACTION_LABELS[action] for action in _CLOSE_DETAIL_ACTIONS),
        *_STANDARD_CLOSE_TIER_LABELS.values(),
        "建议复核持仓",
    ]
)


def _position_has_advice(row: Mapping[str, Any]) -> bool:
    # 只展示指向实际动作的持仓（平仓/止盈建议、需人工复核）；
    # “继续观察”和无法评估的持仓不需要用户做出变化，属于干扰信息
    return _position_status_label(row) in _POSITION_ACTIONABLE_LABELS


def _position_close_details(row: Mapping[str, Any], *, market: str, status: str) -> list[str]:
    if status.startswith("暂无法评估") or _lower(row.get("close_action")) not in _CLOSE_DETAIL_ACTIONS:
        return []
    metrics = row.get("metrics") if isinstance(row.get("metrics"), Mapping) else {}
    parts: list[str] = []
    close_mid = _number(metrics.get("close_mid"))
    if close_mid is not None:
        parts.append(f"参考平仓价 {_money(close_mid, market=market)}（mid）")
    realized = _number(metrics.get("realized_if_close"))
    if realized is not None:
        label = "预计锁定收益" if realized >= 0 else "预计平仓损益"
        parts.append(f"{label} {_money(realized, market=market)}")
    remaining_annualized = _number(metrics.get("remaining_annualized_return"))
    if remaining_annualized is not None:
        parts.append(f"剩余权利金毛年化 {_percent(remaining_annualized)}")
    return [" · ".join(parts)] if parts else []


def _strategy_failure_labels(brief: Mapping[str, Any]) -> list[str]:
    failed = {
        _lower(item.get("strategy_family"))
        for item in brief.get("data_gaps") or []
        if isinstance(item, Mapping) and _lower(item.get("reason")) == "strategy_step_failed"
    }
    return [label for family, label in _STRATEGY_LABELS.items() if family in failed]


def _capacity_views(
    brief: Mapping[str, Any],
    *,
    selected_rows: Mapping[str, list[Mapping[str, Any]]],
) -> tuple[list[str], list[str]]:
    candidates = brief.get("candidates")
    source = candidates if isinstance(candidates, Mapping) else {}
    market = _upper(brief.get("market"))
    out: list[str] = []
    all_sell_put_rows = [item for item in source.get("sell_put") or [] if isinstance(item, Mapping)]
    sell_put_rows = list(selected_rows.get("sell_put") or [])
    for row in sell_put_rows:
        contracts = _capacity_contracts(row)
        if contracts is None:
            continue
        symbol = _upper(row.get("symbol")) or "未知标的"
        contract = _human_contract(
            expiration=row.get("expiration"),
            strike=row.get("strike"),
            option_type="put",
            market=market,
        )
        out.append(f"{symbol} {contract}：按当前现金最多 {contracts} 手")
    reminders = []
    if len(all_sell_put_rows) > 1 and out:
        reminders.append("多个 Sell Put 候选共享同一现金额度，手数不能相加")

    covered_call_rows = list(selected_rows.get("covered_call") or [])
    for row in covered_call_rows:
        contracts = _capacity_contracts(row)
        if contracts is None:
            continue
        symbol = _upper(row.get("symbol")) or "未知标的"
        contract = _human_contract(
            expiration=row.get("expiration"),
            strike=row.get("strike"),
            option_type="call",
            market=market,
        )
        out.append(f"{symbol} {contract}：按当前持股最多 {contracts} 手")
    return out, reminders


def _fund_views(brief: Mapping[str, Any]) -> list[str]:
    funds = brief.get("funds") if isinstance(brief.get("funds"), Mapping) else {}
    cash = _currency_amounts(funds.get("cash_total_by_currency"))
    opening = _currency_amounts(funds.get("option_opening_available_by_currency"))
    cash_total_cny = _number(funds.get("cash_total_cny"))
    opening_cny = _number(funds.get("option_opening_available_cny"))
    cash_lines: list[str] = []
    if cash_total_cny is not None:
        cash_lines.append(f"现金总额（折CNY）：{_currency_money('CNY', cash_total_cny)}")
    else:
        cash_lines.extend(f"现金总额：{_currency_money(currency, amount)}" for currency, amount in cash.items())
    opening_lines: list[str] = []
    if opening_cny is not None:
        opening_lines.append(f"可用于期权开仓（折CNY）：{_currency_money('CNY', opening_cny)}")
    else:
        opening_lines.extend(
            f"可用于期权开仓：{_currency_money(currency, amount)}" for currency, amount in opening.items()
        )
    out = [
        *(cash_lines if cash_lines else ["现金总额：暂不可用"]),
        *(opening_lines if opening_lines else ["可用于期权开仓：暂不可用"]),
    ]
    return out


def _currency_amounts(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, float] = {}
    for raw_currency, raw_amount in value.items():
        currency = _upper(raw_currency)
        amount = _number(raw_amount)
        if currency and amount is not None:
            out[currency] = amount
    return {currency: out[currency] for currency in sorted(out)}


def _currency_money(currency: str, value: float) -> str:
    prefixes = {"USD": "$", "HKD": "HK$", "CNY": "¥", "RMB": "¥"}
    sign = "-" if value < 0 else ""
    prefix = prefixes.get(currency, f"{currency} ")
    return f"{sign}{prefix}{abs(value):,.2f}"


def _capacity_contracts(candidate: Mapping[str, Any]) -> int | None:
    capacity = candidate.get("capacity")
    if not isinstance(capacity, Mapping):
        return None
    value = _number(capacity.get("contracts_available"))
    return max(0, int(value)) if value is not None else None


def _changed_candidate_keys(diff: Mapping[str, Any]) -> set[tuple[str, ...]]:
    out: set[tuple[str, ...]] = set()
    for change in diff.get("changes") or []:
        if not isinstance(change, Mapping):
            continue
        action = change.get("action") if isinstance(change.get("action"), Mapping) else {}
        if _lower(action.get("action_type")) not in {"open_candidate", "open_combo_yield"}:
            continue
        out.update(_candidate_action_keys(action))
    return out


def _candidate_action_keys(action: Mapping[str, Any]) -> set[tuple[str, ...]]:
    family = _lower(action.get("strategy_family"))
    option_type = _lower(action.get("option_type"))
    if family == "combo_yield":
        option_type = "put"
    return _contract_identity_keys(
        family=family,
        symbol=action.get("symbol"),
        expiration=action.get("expiration"),
        strike=action.get("strike"),
        option_type=option_type,
        contract_symbol=action.get("contract_symbol"),
    )


def _candidate_row_keys(family: str, row: Mapping[str, Any]) -> set[tuple[str, ...]]:
    combo = family == "combo_yield"
    return _contract_identity_keys(
        family=family,
        symbol=row.get("symbol"),
        expiration=row.get("put_expiration") if combo else row.get("expiration"),
        strike=row.get("put_strike") if combo else row.get("strike"),
        option_type="put" if combo else row.get("option_type"),
        contract_symbol=(row.get("put_contract_symbol") if combo else row.get("contract_symbol")),
    )


def _changed_position_keys(diff: Mapping[str, Any]) -> set[tuple[str, ...]]:
    out: set[tuple[str, ...]] = set()
    for change in diff.get("changes") or []:
        if not isinstance(change, Mapping):
            continue
        action = change.get("action") if isinstance(change.get("action"), Mapping) else {}
        if _lower(action.get("action_type")) != "close_position":
            continue
        out.update(_position_action_keys(action))
    return out


def _position_action_keys(action: Mapping[str, Any]) -> set[tuple[str, ...]]:
    lot_id = str(action.get("position_lot_id") or "").strip()
    if lot_id:
        return {("lot", lot_id)}
    return _contract_identity_keys(
        family=_lower(action.get("strategy_family")),
        symbol=action.get("symbol"),
        expiration=action.get("expiration"),
        strike=action.get("strike"),
        option_type=action.get("option_type"),
        contract_symbol=action.get("contract_symbol"),
    )


def _position_row_keys(row: Mapping[str, Any]) -> set[tuple[str, ...]]:
    keys = _contract_identity_keys(
        family=_lower(row.get("strategy_family")),
        symbol=row.get("symbol"),
        expiration=row.get("expiration"),
        strike=row.get("strike"),
        option_type=row.get("option_type"),
        contract_symbol=row.get("contract_symbol"),
    )
    lot_id = str(row.get("position_lot_id") or "").strip()
    if lot_id:
        keys.add(("lot", lot_id))
    return keys


def _contract_identity_keys(
    *,
    family: str,
    symbol: Any,
    expiration: Any,
    strike: Any,
    option_type: Any,
    contract_symbol: Any,
) -> set[tuple[str, ...]]:
    keys: set[tuple[str, ...]] = set()
    normalized_symbol = _upper(symbol)
    normalized_expiration = str(expiration or "").strip()[:10]
    normalized_strike = _canonical_decimal_text(strike)
    normalized_option = _lower(option_type)
    if family and normalized_symbol and normalized_expiration and normalized_strike and normalized_option:
        keys.add(
            (
                "structured",
                family,
                normalized_symbol,
                normalized_expiration,
                normalized_strike,
                normalized_option,
            )
        )
    normalized_contract = _upper(contract_symbol)
    if family and normalized_contract:
        keys.add(("contract", family, normalized_contract))
    return keys


def _canonical_decimal_text(value: Any) -> str:
    number = _decimal(value)
    return _decimal_text(number) if number is not None else ""


def _change_summaries(diff: Mapping[str, Any], *, market: str) -> list[str]:
    changes = [item for item in diff.get("changes") or [] if isinstance(item, Mapping)]
    recovered = any(_lower(item.get("change_type")) == "recovered" for item in changes)

    summaries: list[str] = []
    event_summaries: list[str] = []
    date_changed_actions = {
        str((item.get("action") or {}).get("action_id"))
        for item in changes
        if _lower(item.get("change_type")) == "candidate_event_date_changed"
        and isinstance(item.get("action"), Mapping)
        and (item.get("action") or {}).get("action_id")
    }
    grouped: dict[tuple[str, str], int] = {}
    invalidated_candidate_labels: list[str] = []
    position_symbols: list[str] = []
    capacity_changes: list[str] = []
    generic_state_change = False
    for change in changes:
        change_type = _lower(change.get("change_type"))
        action = change.get("action") if isinstance(change.get("action"), Mapping) else {}
        family = _lower(action.get("strategy_family"))
        action_type = _lower(action.get("action_type"))
        candidate_action = action_type in {"open_candidate", "open_combo_yield"}
        if change_type == "candidate_event_date_changed":
            summary = _event_change_summary(change, market=market)
            if summary:
                event_summaries.append(summary)
        elif change_type == "candidate_event_entered_expiry_window":
            if str(action.get("action_id") or "") not in date_changed_actions:
                summary = _event_change_summary(change, market=market)
                if summary:
                    event_summaries.append(summary)
        elif change_type.startswith("candidate_event_"):
            summary = _event_change_summary(change, market=market)
            if summary:
                event_summaries.append(summary)
        elif change_type == "candidate_added":
            grouped[(change_type, family)] = grouped.get((change_type, family), 0) + 1
        elif change_type == "candidate_invalidated":
            label = _change_contract_label(action, market=market)
            if label:
                if label not in invalidated_candidate_labels:
                    invalidated_candidate_labels.append(label)
            else:
                grouped[(change_type, family)] = grouped.get((change_type, family), 0) + 1
        elif change_type in {
            "candidate_priority_upgraded_to_p0",
            "candidate_priority_downgraded",
        }:
            grouped[("candidate_priority_changed", family)] = grouped.get(("candidate_priority_changed", family), 0) + 1
        elif candidate_action and change_type in {"action_added", "action_invalidated"}:
            normalized = "candidate_added" if change_type == "action_added" else "candidate_invalidated"
            grouped[(normalized, family)] = grouped.get((normalized, family), 0) + 1
        elif candidate_action and change_type in {
            "priority_upgraded_to_p0",
            "priority_downgraded",
            "priority_changed",
        }:
            grouped[("candidate_priority_changed", family)] = grouped.get(("candidate_priority_changed", family), 0) + 1
        elif change_type == "candidate_capacity_changed":
            label = _change_contract_label(action, market=market)
            before = _whole_number(change.get("before"))
            after = _whole_number(change.get("after"))
            if label and before is not None and after is not None:
                capacity_changes.append(f"较上一轮：{label} 条件容量 {before} → {after} 手")
        elif action_type == "close_position":
            symbol = _upper(action.get("symbol"))
            if symbol and symbol not in position_symbols:
                position_symbols.append(symbol)
        elif change_type in {"actionability_changed", "blocked"}:
            generic_state_change = True

    if recovered:
        recovered_summaries = ["数据已恢复，以下为当前结果", *event_summaries]
        if len(recovered_summaries) <= 2:
            return recovered_summaries
        return [*recovered_summaries[:2], f"另有 {len(recovered_summaries) - 2} 项变化"]

    summaries.extend(event_summaries)
    for (change_type, family), count in grouped.items():
        strategy = _STRATEGY_LABELS.get(family, "期权")
        if change_type == "candidate_added":
            summaries.append(f"较上一轮：新增 {count} 个 {strategy} 候选")
        elif change_type == "candidate_invalidated":
            summaries.append(f"较上一轮：{count} 个 {strategy} 候选已失效")
        else:
            summaries.append(f"较上一轮：{count} 个 {strategy} 候选优先级已变化")
    if invalidated_candidate_labels:
        shown = "、".join(invalidated_candidate_labels[:2])
        extra = len(invalidated_candidate_labels) - 2
        suffix = f" 等 {len(invalidated_candidate_labels)} 个候选已失效" if extra > 0 else " 候选已失效"
        summaries.append(f"较上一轮：{shown}{suffix}")
    if position_symbols:
        shown = "、".join(position_symbols[:2])
        extra = len(position_symbols) - 2
        suffix = f"，另有 {extra} 个标的" if extra > 0 else ""
        summaries.append(f"较上一轮：{shown} 持仓建议已变化{suffix}")
    summaries.extend(capacity_changes)
    if generic_state_change and not summaries:
        summaries.append("较上一轮：决策状态已更新")
    if not summaries and changes:
        summaries.append("较上一轮：决策内容已更新")
    if len(summaries) <= 2:
        return summaries
    return [*summaries[:2], f"另有 {len(summaries) - 2} 项变化"]


def _event_change_summary(change: Mapping[str, Any], *, market: str) -> str:
    change_type = _lower(change.get("change_type"))
    action = change.get("action") if isinstance(change.get("action"), Mapping) else {}
    before = change.get("before_event_risk") if isinstance(change.get("before_event_risk"), Mapping) else {}
    after = change.get("after_event_risk") if isinstance(change.get("after_event_risk"), Mapping) else {}
    label = _candidate_change_label(action, market=market)
    if not label:
        return ""
    before_event = before.get("nearest_event") if isinstance(before.get("nearest_event"), Mapping) else {}
    after_event = after.get("nearest_event") if isinstance(after.get("nearest_event"), Mapping) else {}
    event = before_event if change_type == "candidate_event_removed" else (after_event or before_event)
    event_label = _event_label(event)
    family = _lower(action.get("strategy_family"))
    relation = _event_expiry_relation_text(after, family=family, current=True)

    if change_type == "candidate_event_added":
        return f"较上一轮：{label} 新增 {event_label or '重要事件'}" + (f"，{relation}" if relation else "")
    if change_type == "candidate_event_date_changed":
        event_type = _EVENT_TYPE_LABELS.get(_lower(event.get("event_type")), "重要事件")
        event_date = _event_date_label(event.get("event_date"))
        return f"较上一轮：{label} {event_type}日期调整至 {event_date}" + (f"，{relation}" if relation else "")
    if change_type == "candidate_event_entered_expiry_window":
        return f"较上一轮：{label} 的{event_label or '重要事件'}已进入当前合约关注窗口" + (
            f"，{relation}" if relation else ""
        )
    if change_type == "candidate_event_evidence_degraded":
        return f"较上一轮：{label} 近期事件数据变得不完整，当前无法确认没有重要事件"
    if change_type == "candidate_event_evidence_recovered":
        if _lower(after.get("user_state")) == "confirmed_none":
            return f"较上一轮：{label} 事件证据已恢复，已确认当前期权到期前没有近期重要事件"
        return f"较上一轮：{label} 事件证据已恢复，现预计 {event_label or '有重要事件'}" + (
            f"，{relation}" if relation else ""
        )
    if change_type == "candidate_event_removed":
        return f"较上一轮：{label} 已确认移除原定 {event_label or '重要事件'}"
    return ""


def _change_contract_label(action: Mapping[str, Any], *, market: str) -> str:
    symbol = _upper(action.get("symbol"))
    contract = _human_contract(
        expiration=action.get("expiration"),
        strike=action.get("strike"),
        option_type=action.get("option_type"),
        market=market,
    )
    if not symbol or contract == "合约信息不完整":
        return ""
    return f"{symbol} {contract}"


def _candidate_change_label(action: Mapping[str, Any], *, market: str) -> str:
    if _lower(action.get("strategy_family")) != "combo_yield":
        return _change_contract_label(action, market=market)
    symbol = _upper(action.get("symbol"))
    contract = _human_contract(
        expiration=action.get("expiration"),
        strike=action.get("strike"),
        option_type="put",
        market=market,
    )
    return f"{symbol} 组合增强（{contract}）" if symbol and contract != "合约信息不完整" else symbol


def _event_label(event: Mapping[str, Any]) -> str:
    event_type = _EVENT_TYPE_LABELS.get(_lower(event.get("event_type")))
    event_date = _event_date_label(event.get("event_date"))
    if not event_type or not event_date:
        return ""
    verb = {"财报": "发布财报", "除息": "除息", "拆股": "实施拆股"}[event_type]
    return f"{event_date}{verb}"


def _event_date_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text[:10])
    except ValueError:
        return ""
    return f"{parsed.month} 月 {parsed.day} 日"


def _event_expiry_relation_text(
    risk: Mapping[str, Any],
    *,
    family: str,
    current: bool = False,
) -> str:
    relations = risk.get("expiration_relations") if isinstance(risk.get("expiration_relations"), Mapping) else {}
    prefix = "现在" if current else ""
    if family != "combo_yield":
        relation = relations.get("contract") if isinstance(relations.get("contract"), Mapping) else {}
        option = "Put" if family == "sell_put" else ("Call" if family == "covered_call" else "期权")
        return _one_expiry_relation(relation.get("relation"), label=option, prefix=prefix)

    parts = []
    for key, label in (("put", "Put"), ("call", "Call")):
        relation = relations.get(key) if isinstance(relations.get(key), Mapping) else {}
        text = _one_expiry_relation(relation.get("relation"), label=label, prefix="")
        if text:
            parts.append(text)
    return prefix + "、".join(parts) if parts else ""


def _one_expiry_relation(value: Any, *, label: str, prefix: str) -> str:
    relation = _lower(value)
    if relation == "before_expiration":
        return f"{prefix}早于当前 {label} 到期日"
    if relation == "on_expiration":
        return f"{prefix}与当前 {label} 同日"
    if relation == "after_expiration":
        return f"{prefix}晚于当前 {label} 到期日"
    return ""


def _phase_label(
    *,
    actionability: str,
    delivery_kind: str,
    diff: Mapping[str, Any],
    context: Mapping[str, Any],
) -> str:
    trigger_kind = _lower(context.get("trigger_kind"))
    if delivery_kind == "query":
        query_time = _parse_datetime(context.get("query_time_utc")) or datetime.now(timezone.utc)
        user_tz = _safe_zoneinfo(str(context.get("user_timezone") or "Asia/Shanghai"))
        return f"当前查询 · 查询时间 {query_time.astimezone(user_tz).strftime('%H:%M')}"
    if delivery_kind == "candidate_alert":
        batch = _scheduled_batch_label(context, market=_upper(context.get("market")))
        return f"新增候选 · {batch} 发现" if batch else "新增候选"
    if delivery_kind == "fixed_report":
        return "固定报告"
    if trigger_kind in {"manual", "force"}:
        return "手动触发"
    if actionability == "blocked":
        return "数据异常"
    change_types = {_lower(item.get("change_type")) for item in diff.get("changes") or [] if isinstance(item, Mapping)}
    if "recovered" in change_types:
        return "数据已恢复"
    if delivery_kind == "delta":
        return "盘中更新"
    if delivery_kind == "full":
        return "今日首次"
    return "当前简报"


def _query_status_lines(brief: Mapping[str, Any], *, context: Mapping[str, Any]) -> list[str]:
    now_utc = _parse_datetime(context.get("query_time_utc")) or datetime.now(timezone.utc)
    market = _upper(brief.get("market"))
    market_tz = _safe_zoneinfo(str(context.get("market_timezone") or _MARKET_TIMEZONES.get(market) or "UTC"))
    trading_date = str(brief.get("market_trading_date") or "").strip()
    today = now_utc.astimezone(market_tz).date().isoformat()
    effective = _lower(brief.get("actionability"))
    if trading_date == today and effective == "live_actionable":
        return ["状态：今日最新"]
    lines = ["状态：已过期，仅供计划参考"]
    if trading_date != today:
        lines.append("今日扫描暂不可用")
    return lines


def _scheduled_batch_label(context: Mapping[str, Any], *, market: str) -> str:
    if _lower(context.get("trigger_kind")) in {"manual", "force"}:
        return ""
    value = context.get("scheduled_target_market")
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) == 5 and text[2] == ":" and text.replace(":", "").isdigit():
        hour, minute = text.split(":", 1)
        if 0 <= int(hour) <= 23 and 0 <= int(minute) <= 59:
            return text
        return ""
    parsed = _parse_datetime(text)
    if parsed is None:
        return ""
    market_tz = _safe_zoneinfo(str(context.get("market_timezone") or _MARKET_TIMEZONES.get(market) or "UTC"))
    return parsed.astimezone(market_tz).strftime("%H:%M")


def _data_as_of_label(brief: Mapping[str, Any], *, context: Mapping[str, Any]) -> str:
    parsed = _parse_datetime(brief.get("data_as_of_utc"))
    if parsed is None:
        return "数据截至：数据时间未知"
    market = _upper(brief.get("market"))
    market_tz = _safe_zoneinfo(str(context.get("market_timezone") or _MARKET_TIMEZONES.get(market) or "UTC"))
    user_tz = _safe_zoneinfo(str(context.get("user_timezone") or "Asia/Shanghai"))
    market_local = parsed.astimezone(market_tz)
    user_local = parsed.astimezone(user_tz)
    trading_date = str(brief.get("market_trading_date") or "").strip()
    market_text = _local_time_text(market_local, trading_date=trading_date)
    user_text = _local_time_text(user_local, trading_date=trading_date)
    market_label = _MARKET_TIME_LABELS.get(market, "市场")
    user_label = str(context.get("user_timezone_label") or "北京").strip() or "本地"
    if market_tz.key == user_tz.key:
        return f"数据截至：{market_label} {market_text}"
    return f"数据截至：{market_label} {market_text} / {user_label} {user_text}"


def _candidate_alert_brief(
    brief: Mapping[str, Any],
    candidate_identities: Iterable[str],
) -> tuple[dict[str, Any], int]:
    identities = list(dict.fromkeys(str(item or "").strip() for item in candidate_identities if str(item or "").strip()))
    index = {
        str(item.get("identity") or "").strip(): item
        for item in brief.get("candidate_index") or []
        if isinstance(item, Mapping) and str(item.get("identity") or "").strip()
    }
    selected = [index[identity] for identity in identities if identity in index]
    missing = [identity for identity in identities if identity not in index]
    if missing:
        raise ValueError("candidate alert identity is absent from the successful brief")
    shown = selected[:_DEFAULT_MAX_CANDIDATES]
    candidates: dict[str, list[Mapping[str, Any]]] = {
        "sell_put": [],
        "covered_call": [],
        "combo_yield": [],
    }
    for item in shown:
        family = _lower(item.get("strategy_family"))
        representative = item.get("representative")
        if family in candidates and isinstance(representative, Mapping):
            candidates[family].append(representative)
    filtered = dict(brief)
    filtered["candidates"] = candidates
    filtered["positions"] = []
    return filtered, max(0, len(selected) - len(shown))


def _local_time_text(value: datetime, *, trading_date: str) -> str:
    if value.date().isoformat() == trading_date:
        return value.strftime("%H:%M")
    return value.strftime("%m-%d %H:%M")


def _blocked_summary(brief: Mapping[str, Any]) -> str:
    reasons = {_lower(item.get("reason")) for item in brief.get("data_gaps") or [] if isinstance(item, Mapping)}
    if "coverage_missing" in reasons:
        return "本轮行情覆盖不足，暂时无法形成可靠决策。"
    if reasons & {"quote_unusable", "quote_unavailable"}:
        return "本轮可用价格不足，暂时无法形成可靠决策。"
    return "本轮关键数据不可用，暂时无法形成可靠决策。"


def _human_contract(*, expiration: Any, strike: Any, option_type: Any, market: str) -> str:
    expiration_text = _expiration_label(expiration)
    strike_text = _strike_label(strike, market=market)
    option_label = _OPTION_LABELS.get(_lower(option_type))
    if not expiration_text or not strike_text or not option_label:
        return "合约信息不完整"
    return f"{expiration_text} {strike_text} {option_label}"


def _expiration_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text[:10])
    except ValueError:
        return ""
    return parsed.strftime("%m-%d")


def _strike_label(value: Any, *, market: str) -> str:
    number = _decimal(value)
    if number is None:
        return ""
    prefix = "$" if market == "US" else ("HK$" if market == "HK" else "")
    return prefix + _decimal_text(number)


def _money(value: float, *, market: str) -> str:
    sign = "-" if value < 0 else ""
    prefix = "$" if market == "US" else ("HK$" if market == "HK" else "")
    return f"{sign}{prefix}{abs(value):,.2f}"


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def _positive_rank(value: Any, *, fallback: int) -> int:
    try:
        rank = int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return rank if rank > 0 else fallback


def _whole_number(value: Any) -> int | None:
    number = _number(value)
    return max(0, int(number)) if number is not None else None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if result != result or result in {float("inf"), float("-inf")}:
        return None
    return result


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return format(normalized, "f").split(".", 1)[0]
    return format(normalized, "f")


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_zoneinfo(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


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


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _upper(value: Any) -> str:
    return str(value or "").strip().upper()


def _bounded_markdown(lines: list[str]) -> str:
    message = "\n".join(lines).strip()
    if len(message) <= _MAX_MESSAGE_CHARS:
        return message
    marker = "\n\n- … 消息已按总长度上限截断；完整结构化简报仍保存在审计记录中。"
    return message[: _MAX_MESSAGE_CHARS - len(marker)].rstrip() + marker


__all__ = [
    "build_daily_brief_user_view",
    "resolve_daily_brief_render_limits",
    "render_blocked_brief",
    "render_candidate_alert",
    "render_daily_brief_lifecycle",
    "render_delta_brief",
    "render_fixed_failure",
    "render_fixed_report",
    "render_full_brief",
    "render_query_brief",
    "render_recovery_brief",
]
