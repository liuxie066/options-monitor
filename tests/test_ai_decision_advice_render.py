from __future__ import annotations

from src.application.ai_decision_advice.render import render_family_advice_lines


def _section(**overrides) -> dict:
    base = {
        "status": "completed",
        "unavailable_reason": None,
        "evidence_as_of": "2026-08-09T11:00:00+00:00",
        "sell_put": {
            "action": "keep",
            "baseline_candidate_id": "put-1",
            "selected_candidate_id": "put-1",
            "rationale": {},
            "source_refs": {},
        },
        "covered_call": [],
        "zero_candidate": {"sell_put": False, "covered_call": False},
        "reused": False,
        "advice_record_id": "adv-1",
    }
    base.update(overrides)
    return base


def test_absent_section_renders_nothing():
    assert render_family_advice_lines(None, family="sell_put") == []


def test_not_applicable_renders_nothing():
    section = _section(status="not_applicable")
    assert render_family_advice_lines(section, family="sell_put") == []


def test_zero_candidate_message():
    section = _section(
        status="not_applicable",
        sell_put=None,
        zero_candidate={"sell_put": True, "covered_call": False},
    )
    lines = render_family_advice_lines(section, family="sell_put")
    assert lines == ["### AI建议", "本轮无可供 AI 评估的策略候选。"]


def test_unavailable_message():
    section = _section(status="unavailable", unavailable_reason="timeout", sell_put=None)
    lines = render_family_advice_lines(section, family="sell_put")
    assert lines[0] == "### AI建议"
    assert "AI建议未完成" in lines[1]
    assert "原始排序" in lines[1]


def test_keep_copy_uses_no_fabricated_safety_language():
    lines = render_family_advice_lines(_section(), family="sell_put")
    text = "\n".join(lines)
    assert "结论｜维持策略排序 1。" in text
    assert "暂无足以改变本轮选择的因素" in text
    assert "安全" not in text
    assert "无风险" not in text
    assert "通过 AI 检查" not in text
    assert "外部信息｜截至 2026-08-09T11:00:00+00:00" in text


def test_switch_copy_includes_contract_and_reason():
    section = _section(
        sell_put={
            "action": "switch",
            "baseline_candidate_id": "put-1",
            "selected_candidate_id": "put-2",
            "rationale": {
                "risk_mechanism": "财报前下行跳跃风险上升",
                "candidate_effect": "当前 Put 在事件窗口内",
                "decision_reason": "改选到期更晚的候选可避开事件",
            },
            "source_refs": {"external_evidence_refs": ["ev-1"]},
        }
    )
    evidence = {
        "ev-1": {
            "source": {
                "title": "NVDA 财报日期确认",
                "publisher": "Reuters",
                "url": "https://example.com/nvda",
                "published_at": "2026-08-08T10:00:00+00:00",
            }
        }
    }
    lines = render_family_advice_lines(
        section,
        family="sell_put",
        candidate_contract_by_id={"put-2": "NVDA 09-18 $100 Put"},
        candidate_rank_by_id={"put-1": 1, "put-2": 2},
        evidence_by_ref=evidence,
    )
    text = "\n".join(lines)
    assert "建议改选策略排序 2：NVDA 09-18 $100 Put。" in text
    assert "财报前下行跳跃风险上升" in text
    assert "改选到期更晚的候选可避开事件" in text
    assert (
        "来源｜NVDA 财报日期确认（Reuters · 2026-08-08 · example.com） "
        "https://example.com/nvda"
    ) in text


def test_defer_copy():
    section = _section(
        sell_put={
            "action": "defer",
            "baseline_candidate_id": "put-1",
            "selected_candidate_id": None,
            "rationale": {"risk_mechanism": "同标的风险叠加明显", "candidate_effect": "加重集中度"},
            "source_refs": {},
        }
    )
    lines = render_family_advice_lines(section, family="sell_put")
    text = "\n".join(lines)
    assert "结论｜本轮暂缓新开仓。" in text
    assert "同标的风险叠加明显" in text


def test_needs_review_copy():
    section = _section(
        sell_put={
            "action": "needs_review",
            "baseline_candidate_id": "put-1",
            "selected_candidate_id": None,
            "rationale": {},
            "source_refs": {},
        }
    )
    lines = render_family_advice_lines(section, family="sell_put")
    assert "结论｜信息不完整或有冲突，需要人工判断。" in lines
    assert "原因｜支持信息不完整。" in lines


def test_needs_review_explains_known_data_gaps():
    section = _section(
        sell_put={
            "action": "needs_review",
            "baseline_candidate_id": "put-1",
            "selected_candidate_id": None,
            "rationale": {},
            "source_refs": {
                "internal_fact_refs": [
                    "gap:portfolio:stale",
                    "gap:option_positions:missing",
                ]
            },
        }
    )

    text = "\n".join(render_family_advice_lines(section, family="sell_put"))

    assert "原因｜组合数据不完整；期权持仓数据不完整。" in text


def test_heading_level_is_configurable():
    lines = render_family_advice_lines(
        _section(),
        family="sell_put",
        heading_level=4,
    )
    assert lines[0] == "#### AI建议"


def test_covered_call_aggregates_per_symbol():
    section = _section(
        sell_put=None,
        covered_call=[
            {
                "symbol": "NVDA",
                "action": "defer",
                "baseline_candidate_id": "call-1",
                "selected_candidate_id": None,
                "rationale": {"risk_mechanism": "强上行催化剂"},
                "source_refs": {},
            },
            {
                "symbol": "AAPL",
                "action": "keep",
                "baseline_candidate_id": "call-2",
                "selected_candidate_id": "call-2",
                "rationale": {},
                "source_refs": {},
            },
        ],
    )
    lines = render_family_advice_lines(
        section,
        family="covered_call",
        candidate_rank_by_id={"call-1": 1, "call-2": 1},
    )
    text = "\n".join(lines)
    assert "汇总｜暂缓 1 个标的，维持 1 个标的。" in text
    assert "- AAPL｜维持策略排序 1" in text
    assert "- NVDA｜本轮暂缓新开仓" in text
    # 标的不出现在模块标题；标题只有 AI建议
    assert lines[0] == "### AI建议"


def test_source_display_capped_at_three():
    refs = {"external_evidence_refs": ["e1", "e2", "e3", "e4"]}
    section = _section(
        sell_put={
            "action": "defer",
            "baseline_candidate_id": "put-1",
            "selected_candidate_id": None,
            "rationale": {"risk_mechanism": "风险"},
            "source_refs": refs,
        }
    )
    evidence = {
        key: {
            "source": {
                "title": f"标题{key}",
                "publisher": "P",
                "url": f"https://example.com/{key}",
                "published_at": "2026-08-08",
            }
        }
        for key in ("e1", "e2", "e3", "e4")
    }
    lines = render_family_advice_lines(section, family="sell_put", evidence_by_ref=evidence)
    source_lines = [line for line in lines if line.startswith("来源｜")]
    assert len(source_lines) == 3


def test_sources_are_sanitized_and_only_valid_https_urls_are_rendered():
    section = _section(
        sell_put={
            "action": "defer",
            "baseline_candidate_id": "put-1",
            "selected_candidate_id": None,
            "rationale": {"risk_mechanism": "风险"},
            "source_refs": {
                "external_evidence_refs": ["unsafe", "http", "safe"]
            },
        }
    )
    evidence = {
        "unsafe": {
            "source": {
                "title": "[伪装标题](https://attacker.invalid)\n## 注入",
                "publisher": "**媒体**",
                "url": "https://example.com/path#fragment",
                "published_at": "2026-08-08",
            }
        },
        "http": {
            "source": {
                "title": "不安全链接",
                "publisher": "媒体",
                "url": "http://example.com/plain",
                "published_at": "2026-08-08",
            }
        },
        "safe": {
            "source": {
                "title": "可靠来源",
                "publisher": "媒体",
                "url": "https://sub.example.org/report",
                "published_at": "2026-08-08",
            }
        },
    }

    text = "\n".join(
        render_family_advice_lines(
            section,
            family="sell_put",
            evidence_by_ref=evidence,
        )
    )

    assert "伪装标题 注入（媒体 · 2026-08-08 · example.com）" in text
    assert "https://example.com/path" in text
    assert "fragment" not in text
    assert "http://example.com/plain" not in text
    assert "可靠来源（媒体 · 2026-08-08 · sub.example.org）" in text


def test_rationale_is_flattened_before_markdown_rendering():
    section = _section(
        sell_put={
            "action": "defer",
            "baseline_candidate_id": "put-1",
            "selected_candidate_id": None,
            "rationale": {
                "risk_mechanism": "风险上升\n## 伪造结论",
                "candidate_effect": "**当前候选**受影响",
                "decision_reason": "[暂缓](https://attacker.invalid)",
            },
            "source_refs": {},
        }
    )

    lines = render_family_advice_lines(section, family="sell_put")
    text = "\n".join(lines)

    assert "## 伪造结论" not in text
    assert "**当前候选**" not in text
    assert "attacker.invalid" not in text
    assert "原因｜风险上升 伪造结论；当前候选 受影响；暂缓。" in text
