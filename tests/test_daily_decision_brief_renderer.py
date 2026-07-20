from __future__ import annotations

from copy import deepcopy


def _brief() -> dict:
    return {
        "schema_version": "daily_decision_brief.v1",
        "brief_id": "US:2026-07-19:lx",
        "market": "US",
        "market_trading_date": "2026-07-19",
        "account": "lx",
        "revision": 3,
        "run_id": "run-render",
        "generated_at_utc": "2026-07-19T13:40:00+00:00",
        "data_as_of_utc": "2026-07-19T13:39:00+00:00",
        "valid_until_utc": "2026-07-19T20:00:00+00:00",
        "status": "ready",
        "actionability": "live_actionable",
        "strategy_summary": "优先处理 P0 平仓，再评估新增敞口。",
        "actions": [
            {
                "action_id": "close-1",
                "priority": "P0",
                "state": "active",
                "action_type": "close_position",
                "strategy_family": "combo_yield",
                "account": "lx",
                "symbol": "NVDA",
                "contract_symbol": "NVDA260821P00100000",
                "title": "平掉融资 Put，保留 Call",
                "reason": "收益已锁定",
                "position_lot_id": "lot-put",
                "strategy_group_id": "group-1",
                "leg_role": "funding_put",
            },
            {
                "action_id": "open-1",
                "priority": "P1",
                "state": "active",
                "action_type": "open_candidate",
                "strategy_family": "sell_put",
                "account": "lx",
                "symbol": "MSFT",
                "contract_symbol": "MSFT260821P00400000",
                "title": "评估 Sell Put",
                "reason": "收益/风险通过筛选",
            },
            {
                "action_id": "observe-1",
                "priority": "P2",
                "state": "observe",
                "action_type": "observe",
                "strategy_family": "covered_call",
                "account": "lx",
                "symbol": "AAPL",
                "title": "继续观察",
            },
        ],
        "positions": [
            {
                "symbol": "NVDA",
                "contract_symbol": "NVDA260821P00100000",
                "close_action": "close_put_keep_call",
                "position_lot_id": "lot-put",
                "strategy_group_id": "group-1",
                "leg_role": "funding_put",
            }
        ],
        "capacity": {
            "sell_put": {"contracts_available": 2, "reason": "按整手资金约束"},
            "covered_call": {"contracts_available": 1},
        },
        "candidates": {
            "sell_put": [
                {"rank": 1, "symbol": "MSFT", "contract_symbol": "MSFT-P1", "priority": "P1"},
                {"rank": 2, "symbol": "NVDA", "contract_symbol": "NVDA-P2", "priority": "P1"},
            ],
            "covered_call": [
                {"rank": 1, "symbol": "AAPL", "contract_symbol": "AAPL-C1", "priority": "P2"}
            ],
            "combo_yield": [
                {
                    "rank": 1,
                    "symbol": "TSLA",
                    "put_contract_symbol": "TSLA-P1",
                    "call_contract_symbol": "TSLA-C1",
                    "priority": "P1",
                }
            ],
        },
        "rejections": {
            "top_categories": [
                {"category": "spread_too_wide", "count": 4, "sample_symbols": ["AMD", "META"]}
            ]
        },
        "events": [{"event_type": "earnings_window", "symbol": "NVDA", "reason": "临近财报"}],
        "data_gaps": [{"scope": "covered_call", "symbol": "AAPL", "reason": "缺少 IV"}],
        "source_artifacts": [],
    }


def test_full_renderer_includes_decision_sections_and_close_identity() -> None:
    from src.application.daily_decision_brief_renderer import render_full_brief

    message = render_full_brief(_brief())

    assert message.startswith("# 每日决策简报")
    for section in (
        "## P0 有效行动",
        "## P1 有效行动",
        "## 非执行状态（观察 / 阻塞 / 失效）",
        "## 已有仓位 / Close Advice",
        "## 行动容量",
        "## Sell Put 候选证据（非行动）",
        "## Covered Call 候选证据（非行动）",
        "## Combo Yield 候选证据（非行动）",
        "## 主要拒绝原因",
        "## 事件",
        "## 数据缺口",
    ):
        assert section in message
    assert "position_lot_id=lot-put" in message
    assert "strategy_group_id=group-1" in message
    assert "leg_role=funding_put" in message
    assert "可执行（LIVE）" in message
    assert "数据质量：就绪（READY）" in message
    assert "## P2 有效行动" not in message
    assert "[观察] 继续观察" in message


def test_blocked_renderer_is_explicit_and_does_not_render_candidates() -> None:
    from src.application.daily_decision_brief_renderer import render_full_brief

    brief = _brief()
    brief["actionability"] = "blocked"
    brief["status"] = "blocked"
    brief["actions"] = [
        {
            "priority": "P0",
            "state": "blocked",
            "action_type": "data_blocked",
            "account": "lx",
            "title": "关键数据阻塞",
            "reason": "pipeline_failed",
        }
    ]

    message = render_full_brief(brief)

    assert message.startswith("# 每日决策简报 · 当前阻塞")
    assert "## 阻塞原因" in message
    assert "等待下一轮 scheduled scan" in message
    assert "## Sell Put 候选证据（非行动）" not in message
    assert "阻塞（BLOCKED）" in message


def test_delta_and_recovery_render_only_material_change_summary() -> None:
    from src.application.daily_decision_brief_renderer import render_daily_brief_lifecycle

    brief = _brief()
    diff = {
        "from_revision": 2,
        "to_revision": 3,
        "changes": [
            {
                "change_type": "action_invalidated",
                "priority": "P0",
                "action": {
                    "symbol": "NVDA",
                    "contract_symbol": "NVDA-P",
                    "position_lot_id": "lot-put",
                    "strategy_group_id": "group-1",
                    "leg_role": "funding_put",
                },
            }
        ],
    }
    delta = render_daily_brief_lifecycle(
        {"brief": brief, "diff": diff, "delivery_kind": "delta"}
    )
    assert delta.startswith("# 日内决策增量")
    assert "原行动已失效" in delta
    assert "position_lot_id=lot-put" in delta
    assert "strategy_group_id=group-1" in delta
    assert "leg_role=funding_put" in delta
    assert "## Sell Put 候选证据（非行动）" not in delta

    recovery_diff = {
        "from_revision": 3,
        "to_revision": 4,
        "changes": [{"change_type": "recovered", "priority": "P0"}],
    }
    recovery = render_daily_brief_lifecycle(
        {"brief": brief, "diff": recovery_diff, "delivery_kind": "delta"}
    )
    assert recovery.startswith("# 日内决策恢复")
    assert "日报已恢复" in recovery


def test_renderer_honors_section_limits_and_total_length_bound() -> None:
    from src.application.daily_decision_brief_renderer import render_full_brief

    brief = _brief()
    brief["actions"] = [
        {
            "priority": "P0",
            "state": "active",
            "action_type": "open_candidate",
            "account": "lx",
            "symbol": f"S{i}",
            "title": "X" * 2_000,
            "reason": "Y" * 2_000,
        }
        for i in range(30)
    ]
    brief["candidates"]["sell_put"] = [
        {"rank": i + 1, "symbol": f"C{i}", "contract_symbol": f"CONTRACT-{i}"}
        for i in range(8)
    ]

    message = render_full_brief(
        brief,
        limits={
            "max_actions_per_priority": 2,
            "max_candidates_per_strategy": 1,
            "max_rejection_reasons": 1,
        },
    )

    assert message.count("[有效]") == 2
    assert "另有 28 条已按展示上限省略" in message
    assert "#2 `C1`" not in message
    assert len(message) <= 12_000


def test_no_delivery_kind_renders_empty_message() -> None:
    from src.application.daily_decision_brief_renderer import render_daily_brief_lifecycle

    assert render_daily_brief_lifecycle({"brief": deepcopy(_brief()), "delivery_kind": "none"}) == ""


def test_renderer_exposes_unknown_data_quality_and_shared_limit_normalization() -> None:
    from src.application.daily_decision_brief_renderer import (
        render_full_brief,
        resolve_daily_brief_render_limits,
    )

    brief = _brief()
    brief["status"] = "future_state"
    limits = resolve_daily_brief_render_limits(
        {
            "max_actions_per_priority": 0,
            "max_candidates_per_strategy": "7",
            "max_rejection_reasons": 999,
        }
    )
    message = render_full_brief(brief, limits=limits)

    assert limits == {
        "max_actions_per_priority": 1,
        "max_candidates_per_strategy": 7,
        "max_rejection_reasons": 20,
    }
    assert "数据质量：未知（FUTURE_STATE）" in message
