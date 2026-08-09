from __future__ import annotations

from typing import Any, Mapping


_ACTION_LABELS = {
    "keep": "维持",
    "switch": "改选",
    "defer": "暂缓",
    "needs_review": "需人工判断",
}


def render_family_advice_lines(
    section: Mapping[str, Any] | None,
    *,
    family: str,
    candidate_contract_by_id: Mapping[str, str] | None = None,
    candidate_rank_by_id: Mapping[str, int] | None = None,
    evidence_by_ref: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[str]:
    """Render the per-strategy AI建议 block (design 15.1 / 15.2 / 15.3 / 15.4).

    ``family`` is ``sell_put`` or ``covered_call``. Returns markdown lines to
    place inside the strategy module, or an empty list when the section is
    absent (module disabled or feature not configured — no empty block).
    """

    if not isinstance(section, Mapping):
        return []
    status = str(section.get("status") or "").strip().lower()
    if status == "not_applicable":
        return []

    zero_candidate = section.get("zero_candidate")
    zero = bool(zero_candidate.get(family)) if isinstance(zero_candidate, Mapping) else False
    lines: list[str] = ["### AI建议"]
    if zero:
        lines.append("本轮无可供 AI 评估的策略候选。")
        return lines

    if status == "unavailable":
        lines.append("AI建议未完成；以下仅展示策略原始排序，不代表已经完成综合判断。")
        return lines

    evidence_as_of = str(section.get("evidence_as_of") or "").strip()
    evidence_line = f"外部信息｜截至 {evidence_as_of}" if evidence_as_of else ""

    if family == "sell_put":
        decision = section.get("sell_put")
        if not isinstance(decision, Mapping) or not decision.get("action"):
            return []
        lines.extend(
            _render_decision(
                decision,
                candidate_contract_by_id=candidate_contract_by_id or {},
                candidate_rank_by_id=candidate_rank_by_id or {},
                evidence_by_ref=evidence_by_ref or {},
                show_symbol=False,
            )
        )
        if evidence_line:
            lines.append(evidence_line)
        return lines

    rows = [row for row in section.get("covered_call") or [] if isinstance(row, Mapping)]
    rows = [row for row in rows if row.get("action")]
    if not rows:
        return []
    counts: dict[str, int] = {}
    for row in rows:
        action = str(row.get("action"))
        counts[action] = counts.get(action, 0) + 1
    summary_parts = [
        f"{_ACTION_LABELS.get(action, action)} {count} 个标的"
        for action, count in counts.items()
    ]
    lines.append("汇总｜" + "，".join(summary_parts) + "。")
    for row in sorted(rows, key=lambda item: str(item.get("symbol") or "")):
        symbol = str(row.get("symbol") or "").strip().upper() or "未知标的"
        decision_lines = _render_decision(
            row,
            candidate_contract_by_id=candidate_contract_by_id or {},
            candidate_rank_by_id=candidate_rank_by_id or {},
            evidence_by_ref=evidence_by_ref or {},
            show_symbol=True,
            symbol=symbol,
        )
        if not decision_lines:
            continue
        first = decision_lines[0]
        prefix = "结论｜"
        body = first[len(prefix):] if first.startswith(prefix) else first
        lines.append(f"- {symbol}｜{body.rstrip('。')}")
        for extra in decision_lines[1:]:
            lines.append(f"  {extra}")
    if evidence_line:
        lines.append(evidence_line)
    return lines


def _decision_conclusion(
    decision: Mapping[str, Any],
    rank_by_id: Mapping[str, int],
) -> str:
    action = str(decision.get("action") or "")
    baseline_rank = rank_by_id.get(str(decision.get("baseline_candidate_id") or ""), 1)
    if action == "keep":
        return f"维持策略排序 {baseline_rank}"
    if action == "switch":
        selected_rank = rank_by_id.get(str(decision.get("selected_candidate_id") or ""))
        if selected_rank:
            return f"改选策略排序 {selected_rank}"
        return "建议改选"
    if action == "defer":
        return "本轮暂缓新开仓"
    if action == "needs_review":
        return "现有证据冲突，需要人工判断"
    return action


def _render_decision(
    decision: Mapping[str, Any],
    *,
    candidate_contract_by_id: Mapping[str, str],
    candidate_rank_by_id: Mapping[str, int],
    evidence_by_ref: Mapping[str, Mapping[str, Any]],
    show_symbol: bool,
    symbol: str = "",
) -> list[str]:
    action = str(decision.get("action") or "")
    lines: list[str] = []

    conclusion = _decision_conclusion(decision, candidate_rank_by_id)
    if action == "keep":
        lines.append(f"结论｜{conclusion}。")
        as_of_note = "综合当前组合、期权持仓和可靠外部信息，暂无足以改变本轮选择的因素。"
        lines.append(f"原因｜{as_of_note}")
        return lines
    if action == "needs_review":
        lines.append(f"结论｜{conclusion}。")
        lines.extend(_source_lines(decision, evidence_by_ref))
        return lines
    if action == "defer":
        lines.append(f"结论｜{conclusion}。")
        reason = _rationale_text(decision)
        if reason:
            lines.append(f"原因｜{reason}")
        lines.extend(_source_lines(decision, evidence_by_ref))
        return lines
    if action == "switch":
        selected_id = str(decision.get("selected_candidate_id") or "")
        contract = candidate_contract_by_id.get(selected_id, "")
        if contract:
            lines.append(f"结论｜建议{conclusion}：{contract}。")
        else:
            lines.append(f"结论｜建议{conclusion}。")
        reason = _rationale_text(decision)
        if reason:
            lines.append(f"原因｜{reason}")
        lines.extend(_source_lines(decision, evidence_by_ref))
        return lines
    lines.append(f"结论｜{conclusion}。")
    return lines


def _rationale_text(decision: Mapping[str, Any]) -> str:
    rationale = decision.get("rationale")
    if not isinstance(rationale, Mapping):
        return ""
    parts = [
        str(rationale.get("risk_mechanism") or "").strip(),
        str(rationale.get("candidate_effect") or "").strip(),
        str(rationale.get("decision_reason") or "").strip(),
    ]
    parts = [part.rstrip("。") for part in parts if part]
    if not parts:
        return ""
    # 15.3: risk mechanism; why it affects the candidate; why switch/defer —
    # at most two sentences in the receipt.
    text = "；".join(parts[:2])
    return text + "。"


def _source_lines(
    decision: Mapping[str, Any],
    evidence_by_ref: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    """At most 3 sources for switch/defer/needs_review (design 15.4)."""

    refs = decision.get("source_refs")
    if not isinstance(refs, Mapping):
        return []
    out: list[str] = []
    for ref in refs.get("external_evidence_refs") or []:
        row = evidence_by_ref.get(str(ref))
        if not isinstance(row, Mapping):
            continue
        source = row.get("source") if isinstance(row.get("source"), Mapping) else row
        title = str(source.get("title") or "").strip()
        publisher = str(source.get("publisher") or "").strip()
        published = str(source.get("published_at") or "").strip()
        url = str(source.get("url") or "").strip()
        if not title or not url:
            continue
        label = title
        extras = [item for item in (publisher, published[:10] if published else "") if item]
        if extras:
            label += "（" + " · ".join(extras) + "）"
        out.append(f"来源｜{label} {url}")
        if len(out) >= 3:
            break
    return out
