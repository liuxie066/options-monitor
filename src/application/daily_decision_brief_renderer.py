from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.application.ai_decision_advice.render import render_family_advice_lines


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
    "short_put": "Sell Put",
    "covered_call": "Covered Call",
    "combo_yield": "组合增强",
}
_AI_ADVICE_ACTION_LABELS = {
    "keep": "维持",
    "switch": "改选",
    "defer": "暂缓",
    "needs_review": "需人工判断",
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
_CLOSE_RECOMMENDATION_LABELS = {
    "close": "建议平仓",
    "hold": "继续观察",
    "not_evaluable": "暂无法评估（证据不足）",
}
_PARTIAL_DATA_REASON_TEXT = {
    "term_matched_rv_unavailable": "期限匹配的已实现波动率（RV）证据不可用",
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
    (
        position_views,
        position_total,
        position_actionable_total,
        position_review_total,
    ) = _position_views(
        brief,
        diff=normalized_diff,
        limits=cfg,
    )
    capacity, reminders = _capacity_views(brief, selected_rows=selected_candidate_rows)
    funds = _fund_views(brief)
    ai_candidate_contracts, ai_candidate_ranks = _ai_candidate_fact_maps(
        brief,
        market=market,
    )
    raw_candidates = brief.get("candidates")
    ai_raw_candidate_presence = {
        family: any(
            isinstance(item, Mapping)
            for item in (
                raw_candidates.get(family) or []
                if isinstance(raw_candidates, Mapping)
                else []
            )
        )
        for family in ("sell_put", "covered_call")
    }
    strategy_failure_items = _strategy_failure_items(brief)
    reminders.extend(_strategy_failure_reminders(strategy_failure_items))
    reminders.extend(_strategy_data_gap_reminders(brief))
    if strategy_failure_items and candidate_views:
        candidate_omissions.append(
            _strategy_failure_omission(strategy_failure_items)
        )
    evidence_holds = [
        item
        for item in brief.get("actions") or []
        if isinstance(item, Mapping)
        and _lower(item.get("evidence_state")) == "unavailable"
        and _lower(item.get("state")) == "observe"
    ]
    partial_data_gaps = [
        item
        for item in brief.get("data_gaps") or []
        if isinstance(item, Mapping)
        and _lower(item.get("reason")) == "opening_candidate_strategy_partial_data"
    ]
    for item in evidence_holds:
        symbol = _upper(item.get("symbol")) or "相关标的"
        strategy = _STRATEGY_LABELS.get(
            _lower(item.get("strategy_family")),
            "候选",
        )
        reminders.append(
            f"{symbol} {strategy} 行情证据不可用，待恢复（不是当前推荐）"
        )
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
            _candidate_empty_summary_for_failures(
                strategy_failure_items,
                partial_data=bool(partial_data_gaps),
                evidence_holds=bool(evidence_holds),
            )
            if strategy_failure_items
            else (
                "本轮行情证据不可用，原候选仅保留待恢复身份，不是当前推荐。"
                if evidence_holds
                else (
                    "本轮部分行情证据不可用，候选结果不完整。"
                    if partial_data_gaps
                    else "本轮暂无符合条件的候选。"
                )
            )
        ),
        "positions": position_views,
        "position_total": position_total,
        "position_actionable_total": position_actionable_total,
        "position_review_total": position_review_total,
        "funds": funds,
        "capacity": capacity,
        "reminders": reminders,
        "ai_decision_advice": brief.get("ai_decision_advice"),
        "ai_decision_advice_evidence_index": brief.get("ai_decision_advice_evidence_index"),
        "_ai_candidate_contract_by_id": ai_candidate_contracts,
        "_ai_candidate_rank_by_id": ai_candidate_ranks,
        "_ai_raw_candidate_presence": ai_raw_candidate_presence,
    }
    return view


def render_fixed_report(
    brief: Mapping[str, Any],
    *,
    diff: Mapping[str, Any] | None = None,
    limits: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
) -> str:
    view = build_daily_brief_user_view(
        brief,
        diff=diff,
        delivery_kind="fixed_report",
        limits=limits,
        context=context,
    )
    return _render_user_view(view, projection="fixed_report")


def render_fixed_report_card_markdown(
    brief: Mapping[str, Any],
    *,
    diff: Mapping[str, Any] | None = None,
    limits: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
) -> str:
    view = build_daily_brief_user_view(
        brief,
        diff=diff,
        delivery_kind="fixed_report",
        limits=limits,
        context=context,
    )
    return _render_user_view_card(view, projection="fixed_report")


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


def render_candidate_alert_card_markdown(
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
    return _render_user_view_card(view, projection="candidate_alert")


def select_rendered_combo_candidate_rows(
    brief: Mapping[str, Any],
    *,
    delivery_kind: str,
    candidate_identities: Iterable[str] = (),
    diff: Mapping[str, Any] | None = None,
    limits: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return the Combo rows selected by the same projection used for rendering."""

    kind = str(delivery_kind or "").strip().lower()
    source: Mapping[str, Any] = brief
    selection_limits = dict(limits or {})
    if kind == "candidate_alert":
        source, _omitted = _candidate_alert_brief(brief, candidate_identities)
        selection_limits["max_candidates_per_strategy"] = _DEFAULT_MAX_CANDIDATES
    elif kind != "fixed_report":
        return []
    selected = _candidate_views(
        source,
        diff=dict(diff or {}),
        limits=resolve_daily_brief_render_limits(selection_limits),
    )[2]
    return [dict(item) for item in selected.get("combo_yield") or []]


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
    subsection_mark = "#" * min(heading_level + 2, 6)
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
    market = _upper(view.get("market"))
    ai_section = view.get("ai_decision_advice")
    ai_zero = (
        ai_section.get("zero_candidate")
        if isinstance(ai_section, Mapping) and isinstance(ai_section.get("zero_candidate"), Mapping)
        else {}
    )
    ai_visible_families = {
        family
        for family in ("sell_put", "covered_call")
        if isinstance(ai_section, Mapping)
        and (
            bool(ai_zero.get(family))
            or str(ai_section.get("status") or "") != "not_applicable"
        )
    }
    candidate_families = {str(item.get("family") or "") for item in candidates}
    visible_families = candidate_families | ai_visible_families
    if projection == "candidate_alert":
        if not candidates:
            lines.extend([_VISIBLE_BLANK_LINE, f"{section_mark} 策略候选"])
            lines.append(str(view.get("candidate_empty_summary") or "本轮暂无符合条件的候选。"))
        else:
            for family in ("sell_put", "covered_call", "combo_yield"):
                family_rows = [item for item in candidates if item.get("family") == family]
                if not family_rows:
                    continue
                lines.extend([_VISIBLE_BLANK_LINE, f"{section_mark} {_STRATEGY_LABELS[family]}"])
                advice_lines = _ai_advice_lines_for_family(
                    view,
                    family=family,
                    market=market,
                    heading_level=heading_level + 2,
                )
                if advice_lines:
                    lines.extend(advice_lines)
                lines.extend([_VISIBLE_BLANK_LINE, f"{subsection_mark} 新增策略候选"])
                for item in family_rows:
                    lines.extend([_VISIBLE_BLANK_LINE, f"**{_flat_title(item['title'])}**"])
                    for leg in item.get("legs") or []:
                        lines.append(_flat_field_line(leg))
                    for detail in item.get("details") or []:
                        lines.append(f"{_candidate_detail_label(detail)}｜{detail}")
            for note in view.get("candidate_omissions") or []:
                lines.append(f"补充｜{note}")
    else:
        candidate_heading = "策略候选"
        omissions_by_family: dict[str, list[str]] = {family: [] for family in _STRATEGY_LABELS}
        other_omissions: list[str] = []
        for note in view.get("candidate_omissions") or []:
            matched = False
            for family, label in _STRATEGY_LABELS.items():
                if str(note).startswith(f"{label} "):
                    omissions_by_family.setdefault(family, []).append(str(note)[len(label):].strip())
                    matched = True
                    break
            if not matched:
                other_omissions.append(str(note))
        for family in ("sell_put", "covered_call", "combo_yield"):
            family_rows = [item for item in candidates if item.get("family") == family]
            if family not in visible_families:
                continue
            lines.extend([_VISIBLE_BLANK_LINE, f"{section_mark} {_STRATEGY_LABELS[family]}"])
            advice_lines = _ai_advice_lines_for_family(
                view,
                family=family,
                market=market,
                heading_level=heading_level + 2,
            )
            if advice_lines:
                lines.extend(advice_lines)
                lines.append(_VISIBLE_BLANK_LINE)
            if family_rows:
                lines.append(f"{subsection_mark} 策略候选")
                for item in family_rows:
                    lines.extend([_VISIBLE_BLANK_LINE, f"**{_flat_title(item['title'])}**"])
                    for leg in item.get("legs") or []:
                        lines.append(_flat_field_line(leg))
                    for detail in item.get("details") or []:
                        lines.append(f"{_candidate_detail_label(detail)}｜{detail}")
            for note in omissions_by_family.get(family) or []:
                lines.append(f"补充｜{note}")
        for note in other_omissions:
            lines.append(f"补充｜{note}")
        if not candidates and not visible_families:
            lines.extend([_VISIBLE_BLANK_LINE, f"{section_mark} {candidate_heading}"])
            lines.append(str(view.get("candidate_empty_summary") or "本轮暂无符合条件的候选。"))

    position_rows = [
        item
        for item in view.get("positions") or []
        if isinstance(item, Mapping)
    ]
    positions, fact_reviews = _partition_position_views(position_rows)
    if projection != "candidate_alert":
        lines.extend([_VISIBLE_BLANK_LINE, f"{section_mark} 持仓"])
        lines.append(
            _position_summary(
                view,
                visible_actionable_count=len(position_rows),
            )
        )
        for item in positions:
            lines.extend([_VISIBLE_BLANK_LINE, f"**{_flat_title(item['title'])}｜{item['status']}**"])
            for detail in item.get("details") or []:
                lines.append(f"参考｜{detail}")
        if fact_reviews:
            lines.extend(
                [
                    _VISIBLE_BLANK_LINE,
                    f"{section_mark} 持仓事实核查（非交易建议）",
                ]
            )
            for item in fact_reviews:
                lines.extend(
                    [
                        _VISIBLE_BLANK_LINE,
                        f"**{_flat_title(item['title'])}｜{item['status']}**",
                    ]
                )
                for detail in item.get("details") or []:
                    lines.append(f"核查原因｜{detail}")

    funds = [str(item) for item in view.get("funds") or [] if str(item).strip()]
    capacity = [str(item) for item in view.get("capacity") or [] if str(item).strip()]
    lines.extend([_VISIBLE_BLANK_LINE, f"{section_mark} 资金"])
    lines.extend(_flat_field_line(item) for item in [*funds, *capacity])

    reminders = [str(item) for item in view.get("reminders") or [] if str(item).strip()]
    if reminders:
        lines.extend([_VISIBLE_BLANK_LINE, f"{section_mark} 提醒"])
        lines.extend(_flat_field_line(item) for item in reminders)

    return _bounded_markdown(lines)


def _render_user_view_card(
    view: Mapping[str, Any],
    *,
    projection: str,
) -> str:
    lines = [
        f"# OM · 决策简报 · {view['account']}",
        "",
        f"状态｜{view['phase_line']}",
        f"市场｜{view['market_label']}",
        f"数据｜{_strip_display_label(view['data_as_of'], '数据截至：')}",
    ]
    planning_notice = str(view.get("planning_notice") or "")
    if planning_notice:
        lines.extend(["", f"提示｜{planning_notice}"])
    if bool(view.get("blocked")):
        lines.extend(
            [
                "",
                f"结论｜{view.get('blocked_summary') or '本轮关键数据不可用，暂时无法形成可靠决策。'}",
                "后续｜系统将在后续批次自动重新评估。",
            ]
        )
        return "\n".join(lines).strip()

    changes = [str(item) for item in view.get("change_summaries") or [] if str(item).strip()]
    if changes:
        lines.extend(["", "变化｜" + "；".join(changes) + "。"])

    candidates = [item for item in view.get("candidates") or [] if isinstance(item, Mapping)]
    market = _upper(view.get("market"))
    ai_section = view.get("ai_decision_advice")
    ai_zero = (
        ai_section.get("zero_candidate")
        if isinstance(ai_section, Mapping) and isinstance(ai_section.get("zero_candidate"), Mapping)
        else {}
    )
    ai_visible_families = {
        family
        for family in ("sell_put", "covered_call")
        if isinstance(ai_section, Mapping)
        and (
            bool(ai_zero.get(family))
            or str(ai_section.get("status") or "") != "not_applicable"
        )
    }
    candidate_families = {str(item.get("family") or "") for item in candidates}
    visible_families = candidate_families | ai_visible_families
    if projection == "candidate_alert":
        visible_families = candidate_families
    if not visible_families:
        lines.extend(["", "## 策略候选"])
        lines.append(str(view.get("candidate_empty_summary") or "本轮暂无符合条件的候选。"))
    else:
        candidate_heading = "新增策略候选" if projection == "candidate_alert" else "策略候选"
        for family in ("sell_put", "covered_call", "combo_yield"):
            if family not in visible_families:
                continue
            family_rows = [item for item in candidates if item.get("family") == family]
            advice_lines = _ai_advice_lines_for_family(
                view,
                family=family,
                market=market,
                heading_level=3,
            )
            lines.extend(
                _render_candidate_family_card(
                    family,
                    family_rows,
                    advice_lines=advice_lines,
                    candidate_heading=candidate_heading,
                )
            )
        if candidates:
            event_lines = _render_candidate_event_card(candidates)
            if event_lines:
                lines.extend(["", *event_lines])
    for note in view.get("candidate_omissions") or []:
        lines.append(f"补充｜{note}")

    if projection != "candidate_alert":
        lines.extend(["", "## 持仓"])
        position_rows = [
            item
            for item in view.get("positions") or []
            if isinstance(item, Mapping)
        ]
        positions, fact_reviews = _partition_position_views(position_rows)
        lines.append(
            _position_summary(
                view,
                visible_actionable_count=len(position_rows),
            )
        )
        if positions:
            # design 15.5: itemized list, same visual form as candidates.
            for index, item in enumerate(positions, start=1):
                lines.extend(
                    [
                        "",
                        f"**{index}｜{_flat_title(item.get('holding'))}｜{item.get('status')}**",
                    ]
                )
                facts = [
                    f"买回参考价 {_table_cell(item.get('close_ask'))}"
                    if item.get("close_ask") not in (None, "")
                    else "",
                    f"预计锁定损益 {_table_cell(item.get('realized_if_close'))}"
                    if item.get("realized_if_close") not in (None, "")
                    else "",
                    f"净兑现比例 {_table_cell(item.get('net_capture'))}"
                    if item.get("net_capture") not in (None, "")
                    else "",
                    f"平仓成本占本金 {_table_cell(item.get('close_cost_ratio'))}"
                    if item.get("close_cost_ratio") not in (None, "")
                    else "",
                    f"剩余期限比例 {_table_cell(item.get('remaining_term_ratio'))}"
                    if item.get("remaining_term_ratio") not in (None, "")
                    else "",
                ]
                facts = [fact for fact in facts if fact]
                if facts:
                    lines.append("参考｜" + " · ".join(facts))
        if fact_reviews:
            lines.extend(
                [
                    "",
                    "## 持仓事实核查（非交易建议）",
                ]
            )
            for item in fact_reviews:
                lines.extend(
                    [
                        "",
                        f"**{_flat_title(item['title'])}｜{item['status']}**",
                    ]
                )
                for detail in item.get("details") or []:
                    lines.append(f"核查原因｜{detail}")

    funds = [str(item) for item in view.get("funds") or [] if str(item).strip()]
    lines.extend(["", "## 资金"])
    if funds:
        lines.extend(_flat_field_line(item) for item in funds)
    else:
        lines.append("资金数据暂不可用。")

    reminders = [str(item) for item in view.get("reminders") or [] if str(item).strip()]
    if reminders:
        lines.extend(["", "## 提醒"])
        lines.extend(f"提醒｜{item}" for item in reminders)
    return "\n".join(lines).strip()


def _render_candidate_family_card(
    family: str,
    rows: list[Mapping[str, Any]],
    *,
    advice_lines: Iterable[str] = (),
    candidate_heading: str = "策略候选",
) -> list[str]:
    heading = _STRATEGY_LABELS.get(family, family)
    lines = ["", f"## {heading}"]
    advice = [str(item) for item in advice_lines if str(item).strip()]
    if advice:
        lines.extend(["", *advice])
    if rows:
        lines.extend(["", f"### {candidate_heading}"])
    for item in rows:
        lines.extend(
            [
                "",
                f"**{_flat_title(item.get('title'))}**",
                *[
                    _flat_field_line(leg)
                    for leg in item.get("legs") or []
                    if str(leg).strip()
                ],
                *[
                    f"{_candidate_detail_label(detail)}｜{detail}"
                    for detail in item.get("details") or []
                    if str(detail) != str(item.get("event_line") or "")
                ],
            ]
        )
    return lines


def _render_candidate_event_card(candidates: list[Mapping[str, Any]]) -> list[str]:
    grouped: dict[str, list[str]] = {}
    family_counts: dict[str, int] = {}
    for item in candidates:
        family = str(item.get("family") or "")
        family_counts[family] = family_counts.get(family, 0) + 1
        label = f"{_STRATEGY_LABELS.get(family, family)} #{family_counts[family]}"
        event_line = str(item.get("event_line") or "").strip()
        if event_line:
            grouped.setdefault(event_line, []).append(label)
    return [
        f"事件｜{'、'.join(labels)}：{event_line}"
        for event_line, labels in grouped.items()
    ]


def _table_cell(value: Any) -> str:
    return (
        str(value or "")
        .replace("\r\n", " ")
        .replace("\r", " ")
        .replace("\n", " ")
        .replace("|", r"\|")
        .strip()
    )


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


def _ai_referenced_candidate_ids(brief: Mapping[str, Any]) -> set[str]:
    """Candidate ids referenced by AI decisions (design 15.6)."""

    section = brief.get("ai_decision_advice")
    if not isinstance(section, Mapping):
        return set()
    ids: set[str] = set()
    rows = []
    sell_put = section.get("sell_put")
    if isinstance(sell_put, Mapping):
        rows.append(sell_put)
    for row in section.get("covered_call") or []:
        if isinstance(row, Mapping):
            rows.append(row)
    for row in rows:
        for key in ("baseline_candidate_id", "selected_candidate_id"):
            value = str(row.get(key) or "").strip()
            if value:
                ids.add(value)
    return ids


def _ai_candidate_fact_maps(
    brief: Mapping[str, Any],
    *,
    market: str,
) -> tuple[dict[str, str], dict[str, int]]:
    """candidate_id -> (human contract label, strategy rank) for AI rendering."""

    prepared_contracts = brief.get("_ai_candidate_contract_by_id")
    prepared_ranks = brief.get("_ai_candidate_rank_by_id")
    if isinstance(prepared_contracts, Mapping) and isinstance(prepared_ranks, Mapping):
        return (
            {
                str(key): str(value)
                for key, value in prepared_contracts.items()
                if str(key).strip() and str(value).strip()
            },
            {
                str(key): int(value)
                for key, value in prepared_ranks.items()
                if str(key).strip() and isinstance(value, int) and not isinstance(value, bool)
            },
        )

    candidates = (
        brief.get("_ai_candidate_catalog")
        if isinstance(brief.get("_ai_candidate_catalog"), Mapping)
        else brief.get("candidates")
    )
    if isinstance(candidates, Mapping):
        by_family: dict[str, list[Mapping[str, Any]]] = {}
        for family in ("sell_put", "covered_call"):
            by_family[family] = [
                row for row in candidates.get(family) or [] if isinstance(row, Mapping)
            ]
    else:
        # view-shaped input: flat candidate views carry `family`.
        by_family = {"sell_put": [], "covered_call": []}
        for row in candidates if isinstance(candidates, list) else []:
            if not isinstance(row, Mapping):
                continue
            family = str(row.get("family") or "")
            if family in by_family:
                by_family[family].append(row)
    source = by_family
    contracts: dict[str, str] = {}
    ranks: dict[str, int] = {}
    for family in ("sell_put", "covered_call"):
        for row in source.get(family) or []:
            if not isinstance(row, Mapping):
                continue
            candidate_id = str(row.get("candidate_id") or "").strip()
            if not candidate_id:
                continue
            symbol = _upper(row.get("symbol")) or "未知标的"
            contract = _human_contract(
                expiration=row.get("expiration"),
                strike=row.get("strike"),
                option_type=row.get("option_type"),
                market=market,
            )
            contracts[candidate_id] = f"{symbol} {contract}".strip()
            rank = _positive_rank(row.get("rank"), fallback=0)
            if rank:
                ranks[candidate_id] = rank
    return contracts, ranks


def _ai_advice_lines_for_family(
    brief: Mapping[str, Any],
    *,
    family: str,
    market: str,
    heading_level: int = 3,
) -> list[str]:
    if family not in {"sell_put", "covered_call"}:
        return []
    section = brief.get("ai_decision_advice")
    if not isinstance(section, Mapping):
        return []
    contracts, ranks = _ai_candidate_fact_maps(brief, market=market)
    evidence_by_ref = _ai_evidence_ref_map(brief)
    raw_presence = brief.get("_ai_raw_candidate_presence")
    if isinstance(raw_presence, Mapping) and isinstance(
        raw_presence.get(family), bool
    ):
        has_raw_candidates = raw_presence[family]
    else:
        candidates = brief.get("candidates")
        if isinstance(candidates, Mapping):
            has_raw_candidates = any(
                isinstance(item, Mapping)
                for item in candidates.get(family) or []
            )
        else:
            candidate_rows = candidates if isinstance(candidates, list) else []
            has_raw_candidates = any(
                isinstance(item, Mapping)
                and _lower(item.get("family")) == family
                for item in candidate_rows
            )
    return render_family_advice_lines(
        section,
        family=family,
        candidate_contract_by_id=contracts,
        candidate_rank_by_id=ranks,
        evidence_by_ref=evidence_by_ref,
        heading_level=heading_level,
        has_raw_candidates=has_raw_candidates,
    )


def _ai_evidence_ref_map(brief: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Resolve frozen external evidence refs for AI advice source lines (15.4)."""

    view = brief.get("ai_decision_advice_evidence_index")
    symbols = view.get("symbols") if isinstance(view, Mapping) else None
    out: dict[str, Mapping[str, Any]] = {}
    for symbol_row in symbols or []:
        if not isinstance(symbol_row, Mapping):
            continue
        for row in symbol_row.get("evidence") or []:
            if not isinstance(row, Mapping):
                continue
            ref = str(row.get("ref") or "")
            if ref:
                out[ref] = row
    return out


def _candidate_views(
    brief: Mapping[str, Any],
    *,
    diff: Mapping[str, Any],
    limits: Mapping[str, int],
) -> tuple[list[dict[str, Any]], list[str], dict[str, list[Mapping[str, Any]]]]:
    candidates = brief.get("candidates")
    source = candidates if isinstance(candidates, Mapping) else {}
    changed_keys = _changed_candidate_keys(diff)
    ai_referenced_ids = _ai_referenced_candidate_ids(brief)
    budget = _RenderBudget()
    out: list[dict[str, Any]] = []
    omissions: list[str] = []
    selected_by_family: dict[str, list[Mapping[str, Any]]] = {}
    limit = limits["max_candidates_per_strategy"]
    market = _upper(brief.get("market"))
    for family in ("sell_put", "covered_call", "combo_yield"):
        rows = [item for item in source.get(family) or [] if isinstance(item, Mapping)]
        changed_rows = [row for row in rows if _candidate_row_keys(family, row) & changed_keys]
        # design 15.6: AI-referenced candidates must be shown even beyond the
        # ordinary top-N budget, without changing their true strategy rank.
        ai_rows = [
            row
            for row in rows
            if row not in changed_rows
            and str(row.get("candidate_id") or "") in ai_referenced_ids
        ]
        unchanged_rows = [
            row for row in rows if row not in changed_rows and row not in ai_rows
        ]
        selected = budget.take(
            [*changed_rows, *ai_rows, *unchanged_rows],
            max(limit, len(changed_rows) + len(ai_rows)),
        )
        selected_by_family[family] = selected
        omitted = len(rows) - len(selected)
        if omitted > 0:
            omissions.append(f"{_STRATEGY_LABELS[family]} 另有 {omitted} 个策略候选未展开")
        for position, row in enumerate(selected, start=1):
            rank = _positive_rank(row.get("rank"), fallback=position)
            choice = f"策略排序 {rank}"
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
                put_ref = _number(row.get("put_sell_reference"))
                put_recommendation = (
                    f"推荐卖出 {_money(put_ref, market=market)}"
                    if put_ref is not None
                    else "推荐卖出价暂不可用"
                )
                put_leg = f"Put：{put_contract}｜{put_recommendation}"
                call_ref = _number(row.get("call_buy_reference"))
                call_recommendation = (
                    f"推荐买入 {_money(call_ref, market=market)}"
                    if call_ref is not None
                    else "推荐买入价暂不可用"
                )
                call_leg = f"Call：{call_contract}｜{call_recommendation}"
                out.append(
                    {
                        "family": family,
                        "rank": rank,
                        "choice": choice,
                        "symbol": symbol,
                        "candidate_id": str(row.get("candidate_id") or "") or None,
                        "title": f"{symbol} · 组合增强（{choice}）",
                        "legs": [put_leg, call_leg],
                        "details": [
                            *_candidate_metric_details(row, family=family, market=market),
                            _candidate_event_line(row, family=family),
                        ],
                        "event_line": _candidate_event_line(row, family=family),
                        "put_leg_card": _combo_leg_card(
                            direction="卖",
                            contract=put_contract,
                            reference=put_ref,
                            market=market,
                        ),
                        "call_leg_card": _combo_leg_card(
                            direction="买",
                            contract=call_contract,
                            reference=call_ref,
                            market=market,
                        ),
                        "return_card": _combo_return_card(row, market=market),
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
                    "rank": rank,
                    "choice": choice,
                    "symbol": symbol,
                    "candidate_id": str(row.get("candidate_id") or "") or None,
                    "title": (f"{symbol} · {_STRATEGY_LABELS[family]} · {contract}（{choice}）"),
                    "details": [
                        *_candidate_metric_details(row, family=family, market=market),
                        _candidate_event_line(row, family=family),
                    ],
                    "legs": [],
                    "event_line": _candidate_event_line(row, family=family),
                    "contract_card": f"{symbol} {contract}",
                    **_regular_candidate_card_fields(row, family=family, market=market),
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

    # design 15.5: period (non-annualized) net return is the primary metric;
    # the annualized value stays as an explicit "门槛年化" gate label.
    period_key = {
        "sell_put": "period_net_return_on_cash_basis",
        "covered_call": "period_net_premium_return",
    }.get(family)
    period = _number(values.get(period_key)) if period_key else None
    if period is None:
        period = _number(values.get("period_net_return"))
    if period is not None:
        parts.append(f"持有期净收益 {_percent(period)}")
    annualized_key = {
        "sell_put": "annualized_net_return_on_cash_basis",
        "covered_call": "annualized_net_premium_return",
        "combo_yield": "annualized_net_credit_yield",
    }.get(family)
    annualized = _number(values.get(annualized_key)) if annualized_key else None
    if annualized is not None:
        parts.append(f"门槛年化 {_percent(annualized)}")
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


def _regular_candidate_card_fields(
    candidate: Mapping[str, Any],
    *,
    family: str,
    market: str,
) -> dict[str, str]:
    metrics = candidate.get("metrics") if isinstance(candidate.get("metrics"), Mapping) else {}
    mid = _number(metrics.get("mid"))
    bid = _number(metrics.get("bid"))
    ask = _number(metrics.get("ask"))
    if mid is not None:
        premium = _money(mid, market=market)
    elif bid is not None or ask is not None:
        bid_text = _money(bid, market=market) if bid is not None else "—"
        ask_text = _money(ask, market=market) if ask is not None else "—"
        premium = f"{bid_text}/{ask_text}"
    else:
        premium = "—"
    net_income = _number(metrics.get("net_income"))
    income_card = premium
    if net_income is not None:
        income_card += f" / {_money(net_income, market=market)}"
    annualized_key = {
        "sell_put": "annualized_net_return_on_cash_basis",
        "covered_call": "annualized_net_premium_return",
    }.get(family)
    annualized = _number(metrics.get(annualized_key)) if annualized_key else None
    delta = _number(metrics.get("delta"))
    dte = _number(metrics.get("dte"))
    capacity = _capacity_contracts(candidate)
    risk_parts: list[str] = []
    if delta is not None:
        risk_parts.append(f"Δ {delta:.2f}")
    if dte is not None:
        risk_parts.append(f"{max(0, int(dte))}天")
    if capacity is not None:
        action = "可卖" if family == "covered_call" else "可开"
        risk_parts.append(f"{action}{capacity}手")
    return {
        "income_card": income_card,
        "annualized_card": _percent(annualized) if annualized is not None else "—",
        "risk_capacity_card": " · ".join(risk_parts) or "—",
    }


def _combo_leg_card(
    *,
    direction: str,
    contract: str,
    reference: float | None,
    market: str,
) -> str:
    value = f"{direction} {contract}"
    if reference is not None:
        value += f" @ {_money(reference, market=market)}"
    return value


def _combo_return_card(candidate: Mapping[str, Any], *, market: str) -> str:
    metrics = candidate.get("metrics") if isinstance(candidate.get("metrics"), Mapping) else {}
    parts: list[str] = []
    mid = _number(metrics.get("mid"))
    if mid is not None:
        parts.append(f"净权利金 {_money(mid, market=market)}")
    annualized = _number(metrics.get("annualized_net_credit_yield"))
    if annualized is not None:
        parts.append(f"年化 {_percent(annualized)}")
    net_income = _number(metrics.get("net_income"))
    if net_income is not None:
        parts.append(f"净收入 {_money(net_income, market=market)}")
    return " · ".join(parts) or "—"


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
    if risk.get("in_attention_window") is False:
        return (
            f"预计 {event_label}{relation_text}，位于到期前 6 个自然日硬窗口之外，"
            "不触发财报过滤；执行前仍需复核事件和报价。"
        )
    return f"预计 {event_label}{relation_text}；执行前需要重新确认事件窗口和报价。"


def _position_views(
    brief: Mapping[str, Any],
    *,
    diff: Mapping[str, Any],
    limits: Mapping[str, int],
) -> tuple[list[dict[str, Any]], int, int, int]:
    positions = [item for item in brief.get("positions") or [] if isinstance(item, Mapping)]
    total = len(positions)
    # 无行动指向的持仓（继续观察、无法评估）不逐条展示；
    # 总持仓数和需处理数在汇总中分开呈现，避免把非行动项误说成“折叠”。
    positions = [row for row in positions if _position_has_advice(row)]
    actionable_total = len(positions)
    review_total = sum(1 for row in positions if _is_position_fact_review(row))
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
        display_kind = (
            "fact_review"
            if _is_position_fact_review(row)
            else "advice"
        )
        out.append(
            {
                "title": " · ".join(title_parts),
                "holding": " · ".join(title_parts),
                "status": status,
                "display_kind": display_kind,
                "recommendation": _lower(row.get("recommendation")),
                "details": _position_close_details(row, market=market, status=status),
                **_position_card_fields(row, market=market, status=status),
            }
        )
    return out, total, actionable_total, review_total


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
    recommendation = _lower(row.get("recommendation_state"))
    if recommendation == "not_evaluable":
        return _CLOSE_RECOMMENDATION_LABELS["not_evaluable"]
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

    if recommendation in _CLOSE_RECOMMENDATION_LABELS:
        return _CLOSE_RECOMMENDATION_LABELS[recommendation]
    return "暂无法评估（报价质量不足）"


_POSITION_ACTIONABLE_LABELS = frozenset({"建议平仓"})


def _position_has_advice(row: Mapping[str, Any]) -> bool:
    # 只展示严格策略已触发的平仓动作；hold 和
    # not_evaluable 仍保留在结构化审计数据中，但不进入普通提醒。
    if row.get("notification_eligible") is False:
        return False
    return _position_status_label(row) in _POSITION_ACTIONABLE_LABELS


def _is_position_fact_review(row: Mapping[str, Any]) -> bool:
    return False


def _partition_position_views(
    rows: list[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    fact_reviews = [
        row
        for row in rows
        if _lower(row.get("display_kind")) == "fact_review"
    ]
    advice = [
        row
        for row in rows
        if _lower(row.get("display_kind")) != "fact_review"
    ]
    return advice, fact_reviews


def _position_summary(
    view: Mapping[str, Any],
    *,
    visible_actionable_count: int,
) -> str:
    position_total = _whole_number(view.get("position_total")) or 0
    actionable_total = (
        _whole_number(view.get("position_actionable_total")) or 0
    )
    review_total = _whole_number(view.get("position_review_total")) or 0
    summary = f"汇总｜共 {position_total} 条"
    if review_total:
        trade_total = max(0, actionable_total - review_total)
        if trade_total:
            summary += f"，需交易处理 {trade_total} 条"
        else:
            summary += "，当前没有需要交易处理的持仓"
        summary += f"，另有事实核查 {review_total} 条（非交易建议）"
    elif actionable_total:
        summary += f"，需处理 {actionable_total} 条"
    else:
        summary += "，当前没有需要处理的持仓"
    if visible_actionable_count < actionable_total:
        summary += f"，本消息展示 {visible_actionable_count} 条"
    return summary + "。"


def _position_close_details(row: Mapping[str, Any], *, market: str, status: str) -> list[str]:
    if (
        status.startswith("暂无法评估")
        or _lower(row.get("recommendation_state")) != "close"
    ):
        return []
    metrics = row.get("metrics") if isinstance(row.get("metrics"), Mapping) else {}
    parts: list[str] = []
    close_ask = _number(metrics.get("ask"))
    if close_ask is not None:
        parts.append(f"买回参考价 {_money(close_ask, market=market)}（ask）")
    realized = _number(metrics.get("estimated_pnl_if_close_net"))
    if realized is not None:
        label = "预计锁定收益" if realized >= 0 else "预计平仓损益"
        parts.append(f"{label} {_money(realized, market=market)}")
    capture = _number(metrics.get("net_capture_ratio"))
    if capture is not None:
        parts.append(f"净兑现比例 {_percent(capture)}")
    close_cost_ratio = _number(metrics.get("close_cost_ratio"))
    if close_cost_ratio is not None:
        parts.append(f"全成本平仓占名义本金 {_percent(close_cost_ratio)}")
    remaining_term = _number(metrics.get("remaining_term_ratio"))
    if remaining_term is not None:
        parts.append(f"剩余期限比例 {_percent(remaining_term)}")
    return [" · ".join(parts)] if parts else []


def _position_card_fields(row: Mapping[str, Any], *, market: str, status: str) -> dict[str, str]:
    if (
        status.startswith("暂无法评估")
        or _lower(row.get("recommendation_state")) != "close"
    ):
        return {
            "close_ask": "—",
            "realized_if_close": "—",
            "net_capture": "—",
            "close_cost_ratio": "—",
            "remaining_term_ratio": "—",
        }
    metrics = row.get("metrics") if isinstance(row.get("metrics"), Mapping) else {}
    close_ask = _number(metrics.get("ask"))
    realized = _number(metrics.get("estimated_pnl_if_close_net"))
    capture = _number(metrics.get("net_capture_ratio"))
    close_cost_ratio = _number(metrics.get("close_cost_ratio"))
    remaining_term = _number(metrics.get("remaining_term_ratio"))
    realized_text = "—"
    if realized is not None:
        realized_text = _money(realized, market=market)
        if realized > 0:
            realized_text = "+" + realized_text
    return {
        "close_ask": _money(close_ask, market=market) if close_ask is not None else "—",
        "realized_if_close": realized_text,
        "net_capture": _percent(capture) if capture is not None else "—",
        "close_cost_ratio": _percent(close_cost_ratio) if close_cost_ratio is not None else "—",
        "remaining_term_ratio": _percent(remaining_term) if remaining_term is not None else "—",
    }


def _strategy_failure_items(brief: Mapping[str, Any]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in brief.get("data_gaps") or []:
        if not isinstance(item, Mapping):
            continue
        if _lower(item.get("reason")) != "strategy_step_failed":
            continue
        family = _lower(item.get("strategy_family"))
        if family not in _STRATEGY_LABELS:
            continue
        symbol = _upper(item.get("symbol"))
        identity = (family, symbol)
        if identity in seen:
            continue
        seen.add(identity)
        items.append({"family": family, "symbol": symbol})
    return items


def _strategy_failure_reminders(items: list[dict[str, str]]) -> list[str]:
    reminders: list[str] = []
    for item in items:
        strategy = _STRATEGY_LABELS.get(item["family"], "候选")
        symbol = item["symbol"] or "相关标的"
        reminders.append(
            f"{symbol} {strategy} 扫描失败，本轮无结果"
        )
    return reminders


def _strategy_failure_subject(items: list[dict[str, str]]) -> str:
    """Group failed symbols per strategy family, preserving first-seen order."""
    grouped: dict[str, list[str]] = {}
    for item in items:
        symbol = item["symbol"] or "相关标的"
        grouped.setdefault(item["family"], [])
        if symbol not in grouped[item["family"]]:
            grouped[item["family"]].append(symbol)
    return "、".join(
        f"{'、'.join(symbols)} {_STRATEGY_LABELS.get(family, '候选')}"
        for family, symbols in grouped.items()
    )


def _strategy_failure_omission(items: list[dict[str, str]]) -> str:
    return f"{_strategy_failure_subject(items)} 扫描失败，未纳入本轮候选"


def _candidate_empty_summary_for_failures(
    items: list[dict[str, str]],
    *,
    partial_data: bool = False,
    evidence_holds: bool = False,
) -> str:
    subject = _strategy_failure_subject(items)
    if partial_data or evidence_holds:
        return f"本轮部分行情证据不可用；{subject} 扫描失败。"
    return f"本轮暂无符合条件的候选；{subject} 扫描失败。"


def _strategy_data_gap_reminders(
    brief: Mapping[str, Any],
) -> list[str]:
    reminders: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for item in brief.get("data_gaps") or []:
        if not isinstance(item, Mapping):
            continue
        reason = _lower(item.get("reason"))
        symbol = _upper(item.get("symbol")) or "相关标的"
        family = _STRATEGY_LABELS.get(
            _lower(item.get("strategy_family")),
            "策略",
        )
        identity = (symbol, family, reason)
        if reason == "earnings_soft_coverage_partial":
            if identity not in seen:
                reminders.append(
                    f"{symbol} {family}：较远财报日历上下文部分不可用；6 个自然日硬窗口证据完整，候选资格不受此缺口影响"
                )
                seen.add(identity)
            continue
        if (
            _lower(item.get("scope")) != "strategy"
            or _lower(item.get("severity")) != "warning"
            or item.get("actionable") is not False
        ):
            continue
        if identity in seen:
            continue
        if (
            _lower(item.get("outcome")) == "success_empty"
            and reason in {"no_expirations", "no_contract_rows"}
        ):
            detail = (
                "本轮未发现可用到期日"
                if reason == "no_expirations"
                else "本轮未找到可扫描合约"
            )
            reminders.append(
                f"{symbol} {family}：{detail}，已按零候选完成（非操作建议）"
            )
        elif reason == "strategy_status_projection_mismatch":
            reminders.append(
                f"{symbol} {family}：局部告警证据不一致，已忽略该提示（不影响其他可靠结果）"
            )
        elif reason == "opening_candidate_strategy_partial_data":
            detail = _PARTIAL_DATA_REASON_TEXT.get(
                _lower(item.get("reason_code"))
            )
            reminders.append(
                f"{symbol} {family}：{detail}，候选结果不完整"
                if detail
                else f"{symbol} {family}：本轮部分行情证据不可用，候选结果不完整"
            )
        seen.add(identity)
    return reminders


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
        elif change_type == "candidate_evidence_unavailable":
            label = _change_contract_label(action, market=market)
            summaries.append(
                f"较上一轮：{label or '候选'} 行情证据不可用，待恢复"
            )
        elif change_type == "candidate_evidence_recovered":
            label = _change_contract_label(action, market=market)
            summaries.append(
                f"较上一轮：{label or '候选'} 行情证据已恢复"
            )
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
        elif change_type == "ai_decision_advice_action_changed":
            scope_label = _ai_advice_scope_label(change.get("ai_advice_scope"))
            before_label = _AI_ADVICE_ACTION_LABELS.get(
                _lower(change.get("before")),
                str(change.get("before") or "原建议"),
            )
            after_label = _AI_ADVICE_ACTION_LABELS.get(
                _lower(change.get("after")),
                str(change.get("after") or "新建议"),
            )
            summaries.append(
                f"较上一轮：{scope_label} AI建议由{before_label}变为{after_label}"
            )
        elif change_type == "ai_decision_advice_selected_candidate_changed":
            scope_label = _ai_advice_scope_label(change.get("ai_advice_scope"))
            summaries.append(f"较上一轮：{scope_label} AI改选目标已变化")
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


def _ai_advice_scope_label(value: Any) -> str:
    scope = str(value or "").strip()
    if scope == "sell_put":
        return "Sell Put"
    if scope.startswith("covered_call:"):
        symbol = _upper(scope.split(":", 1)[1])
        return f"Covered Call {symbol}".strip()
    return "AI建议"


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
    filtered["_ai_candidate_catalog"] = (
        brief.get("_ai_candidate_catalog")
        if isinstance(brief.get("_ai_candidate_catalog"), Mapping)
        else brief.get("candidates")
    )
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
    "render_candidate_alert_card_markdown",
    "render_daily_brief_lifecycle",
    "render_delta_brief",
    "render_fixed_failure",
    "render_fixed_report",
    "render_fixed_report_card_markdown",
    "render_full_brief",
    "render_query_brief",
    "render_recovery_brief",
]
