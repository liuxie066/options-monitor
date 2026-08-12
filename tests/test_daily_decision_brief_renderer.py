from __future__ import annotations

from copy import deepcopy

from tests.notification_format_assertions import assert_mobile_flat_markdown


def _candidate(
    *,
    rank: int,
    symbol: str,
    option_type: str,
    expiration: str,
    strike: float,
    capacity: int | None = None,
) -> dict:
    row = {
        "rank": rank,
        "symbol": symbol,
        "option_type": option_type,
        "contract_symbol": f"US.{symbol}260821{option_type[:1].upper()}INTERNAL",
        "expiration": expiration,
        "strike": strike,
        "priority": "P1",
        "metrics": {
            "mid": 5.25,
            "annualized_net_return_on_cash_basis": 0.181,
            "annualized_net_premium_return": 0.126,
            "delta": -0.24 if option_type == "put" else 0.22,
            "dte": 32,
            "net_income": 480,
        },
        "source": {"path": "/private/internal/candidates.csv"},
    }
    if capacity is not None:
        row["capacity"] = {
            "contracts_available": capacity,
            "reason": "cash_supported",
        }
    return row


def _brief() -> dict:
    return {
        "schema_version": "daily_decision_brief.v1",
        "brief_id": "US:2026-07-20:lx",
        "market": "US",
        "market_trading_date": "2026-07-20",
        "account": "lx",
        "revision": 3,
        "run_id": "run-render-secret",
        "generated_at_utc": "2026-07-20T14:04:00+00:00",
        "data_as_of_utc": "2026-07-20T14:03:00+00:00",
        "valid_until_utc": "2026-07-20T20:00:00+00:00",
        "status": "ready",
        "actionability": "live_actionable",
        "strategy_summary": "internal strategy summary",
        "actions": [
            {
                "action_id": "close-1",
                "priority": "P0",
                "state": "active",
                "action_type": "close_position",
                "strategy_family": "sell_put",
                "account": "lx",
                "symbol": "NVDA",
                "contract_symbol": "US.NVDA260821P100000",
                "position_lot_id": "lot-put-secret",
                "reason": "internal reason",
            }
        ],
        "positions": [
            {
                "symbol": "NVDA",
                "strategy_family": "sell_put",
                "expiration": "2026-08-21",
                "strike": 100,
                "option_type": "put",
                "contract_symbol": "US.NVDA260821P100000",
                "recommendation_state": "close",
                "notification_eligible": True,
                "evaluation_status": "evaluable",
                "quote_status": "priced",
                "position_lot_id": "lot-put-secret",
            },
            {
                "symbol": "PDD",
                "strategy_family": "combo_yield",
                "leg_role": "funding_put",
                "contract_symbol": "US.PDD260821P95000",
                "recommendation_state": "not_evaluable",
                "evaluation_status": "not_evaluable",
                "quote_status": "coverage_missing",
                "position_lot_id": "lot-pdd-secret",
                "strategy_group_id": "combo-pdd-secret",
            },
            {
                "symbol": "FUTU",
                "strategy_family": "sell_put",
                "recommendation_state": "not_evaluable",
                "evaluation_status": "not_evaluable",
                "quote_status": "quote_unusable",
                "position_lot_id": "lot-futu-secret",
            },
        ],
        "capacity": {
            "sell_put": {"contracts_available": 999, "reason": "cash_supported"},
        },
        "candidates": {
            "sell_put": [
                _candidate(
                    rank=1,
                    symbol="MSFT",
                    option_type="put",
                    expiration="2026-08-21",
                    strike=400,
                    capacity=2,
                ),
                _candidate(
                    rank=2,
                    symbol="NVDA",
                    option_type="put",
                    expiration="2026-08-21",
                    strike=100,
                    capacity=5,
                ),
            ],
            "covered_call": [
                _candidate(
                    rank=1,
                    symbol="AAPL",
                    option_type="call",
                    expiration="2026-08-21",
                    strike=250,
                    capacity=1,
                )
            ],
            "combo_yield": [
                {
                    "rank": 1,
                    "symbol": "TSLA",
                    "priority": "P1",
                    "put_contract_symbol": "US.TSLA260821P300000",
                    "call_contract_symbol": "US.TSLA260918C400000",
                    "put_expiration": "2026-08-21",
                    "put_strike": 300,
                    "call_expiration": "2026-09-18",
                    "call_strike": 400,
                    "put_sell_reference": 3.45,
                    "call_buy_reference": 1.05,
                    "metrics": {"annualized_net_credit_yield": 0.154, "net_income": 620},
                    "strategy_group_id": "combo-candidate-secret",
                }
            ],
        },
        "rejections": {
            "top_categories": [
                {"category": "spread_too_wide", "count": 806, "sample_symbols": ["GOOGL"]}
            ]
        },
        "events": [{"event_type": "earnings_window", "symbol": "NVDA"}],
        "data_gaps": [{"scope": "position", "symbol": "PDD", "reason": "coverage_missing"}],
        "source_artifacts": ["/private/internal/run.json"],
    }


def _scheduled_context() -> dict:
    return {
        "trigger_kind": "scheduled",
        "scheduled_target_market": "10:00",
        "market_timezone": "America/New_York",
        "user_timezone": "Asia/Shanghai",
        "user_timezone_label": "北京",
    }


def _assert_no_internal_leak(value: object) -> None:
    text = str(value)
    for forbidden in (
        "position_lot_id",
        "lot-put-secret",
        "strategy_group_id",
        "combo-secret",
        "leg_role",
        "revision",
        "run-render-secret",
        "LIVE",
        "READY",
        "BLOCKED",
        "PLANNING",
        "2026-07-20T",
        "US.MSFT",
        "US.NVDA",
        "US.PDD",
        "spread_too_wide",
        "806",
        "/private/internal",
    ):
        assert forbidden not in text


def test_full_renderer_is_compact_human_readable_and_allowlisted() -> None:
    from src.application.daily_decision_brief_renderer import (
        build_daily_brief_user_view,
        render_daily_brief_lifecycle,
    )

    brief = _brief()
    lifecycle = {"brief": brief, "diff": {}, "delivery_kind": "full"}
    view = build_daily_brief_user_view(
        brief,
        delivery_kind="full",
        context=_scheduled_context(),
    )
    message = render_daily_brief_lifecycle(lifecycle, context=_scheduled_context())

    assert message.startswith("# OM · 决策简报 · lx")
    assert "状态｜今日首次 · 10:00 批次" in message
    assert "市场｜美股" in message
    assert "数据｜美东 10:03 / 北京 22:03" in message
    assert "## Sell Put" in message
    assert "## 持仓" in message
    assert "汇总｜共 3 条，需处理 1 条。" in message
    assert "## 资金" in message
    assert "MSFT｜Sell Put｜08-21 $400 Put（策略排序 1）" in message
    assert "NVDA｜Sell Put｜08-21 $100 Put（策略排序 2）" in message
    assert "AAPL｜Covered Call｜08-21 $250 Call（策略排序 1）" in message
    assert "TSLA｜组合增强（策略排序 1）" in message
    assert "Put｜08-21 $300 Put｜推荐卖出 $3.45" in message
    assert "Call｜09-18 $400 Call｜推荐买入 $1.05" in message
    assert "暂无法评估" not in message
    assert "PDD" not in message
    assert "FUTU" not in message
    assert "MSFT 08-21 $400 Put｜按当前现金最多 2 手" in message
    assert "NVDA 08-21 $100 Put｜按当前现金最多 5 手" in message
    assert "多个 Sell Put 候选共享同一现金额度，手数不能相加" in message
    assert_mobile_flat_markdown(message)
    _assert_no_internal_leak(message)
    _assert_no_internal_leak(view)


def test_success_empty_warning_is_embedded_without_failure_wording() -> None:
    from src.application.daily_decision_brief_renderer import (
        build_daily_brief_user_view,
        render_daily_brief_lifecycle,
    )

    brief = _brief()
    brief["status"] = "degraded"
    brief["data_gaps"].append(
        {
            "scope": "strategy",
            "symbol": "NVDA",
            "strategy_family": "sell_put",
            "outcome": "success_empty",
            "reason": "no_expirations",
            "severity": "warning",
            "actionable": False,
        }
    )

    view = build_daily_brief_user_view(
        brief,
        delivery_kind="full",
        context=_scheduled_context(),
    )
    message = render_daily_brief_lifecycle(
        {"brief": brief, "diff": {}, "delivery_kind": "full"},
        context=_scheduled_context(),
    )

    reminder = (
        "NVDA Sell Put：本轮未发现可用到期日，"
        "已按零候选完成（非操作建议）"
    )
    assert reminder in view["reminders"]
    assert reminder.replace("：", "｜") in message
    assert "数据异常" not in message
    assert "获取失败" not in message
    assert "扫描失败" not in message


def test_status_projection_mismatch_renders_only_a_local_reminder() -> None:
    from src.application.daily_decision_brief_renderer import (
        render_daily_brief_lifecycle,
    )

    brief = _brief()
    brief["status"] = "degraded"
    brief["data_gaps"].append(
        {
            "scope": "strategy",
            "symbol": "NVDA",
            "strategy_family": "sell_put",
            "reason": "strategy_status_projection_mismatch",
            "severity": "warning",
            "actionable": False,
        }
    )

    message = render_daily_brief_lifecycle(
        {"brief": brief, "diff": {}, "delivery_kind": "full"},
        context=_scheduled_context(),
    )

    assert (
        "NVDA Sell Put｜局部告警证据不一致，"
        "已忽略该提示（不影响其他可靠结果）"
    ) in message
    assert "数据异常" not in message


def test_hk_actionable_close_renders_price_locked_profit_and_remaining_yield() -> None:
    from src.application.daily_decision_brief_renderer import render_full_brief

    brief = _brief()
    brief["market"] = "HK"
    brief["positions"] = [
        {
            "symbol": "3690.HK",
            "strategy_family": "sell_put",
            "expiration": "2026-08-28",
            "strike": 65,
            "option_type": "put",
            "recommendation_state": "close",
            "evaluation_status": "evaluable",
            "quote_status": "priced",
            "metrics": {
                "ask": 0.52,
                "estimated_pnl_if_close_net": 474.5,
                "net_capture_ratio": 0.93,
                "close_cost_ratio": 0.0008,
                "remaining_term_ratio": 0.60,
            },
        }
    ]
    brief["candidates"] = {"sell_put": [], "covered_call": [], "combo_yield": []}

    message = render_full_brief(brief)

    assert "3690.HK｜Sell Put｜08-28 HK$65 Put｜建议平仓" in message
    assert (
        "参考｜买回参考价 HK$0.52（ask） · 预计锁定收益 HK$474.50 · "
        "净兑现比例 93.0% · 全成本平仓占名义本金 0.1% · 剩余期限比例 60.0%"
        in message
    )

    brief["market"] = "US"
    brief["positions"][0]["symbol"] = "NVDA"
    us_message = render_full_brief(brief)
    assert (
        "买回参考价 $0.52（ask） · 预计锁定收益 $474.50 · "
        "净兑现比例 93.0%"
        in us_message
    )


def test_strict_close_uses_one_action_label_for_every_close_state() -> None:
    from src.application.daily_decision_brief_renderer import render_full_brief

    brief = _brief()
    brief["positions"] = [
        {
            "symbol": symbol,
            "strategy_family": "sell_put",
            "recommendation_state": "close",
            "evaluation_status": "evaluable",
            "quote_status": "priced",
        }
        for symbol in ("A", "B", "C", "D", "E", "F")
    ]
    brief["candidates"] = {"sell_put": [], "covered_call": [], "combo_yield": []}

    message = render_full_brief(
        brief,
        limits={"max_actions_per_priority": 10},
    )

    for symbol in ("A", "B", "C", "D", "E", "F"):
        assert f"{symbol}｜Sell Put｜建议平仓" in message
    assert "汇总｜共 6 条，需处理 6 条。" in message


def test_close_details_use_signed_pnl_and_degrade_without_inventing_values() -> None:
    from src.application.daily_decision_brief_renderer import render_full_brief

    brief = _brief()
    brief["market"] = "HK"
    brief["positions"] = [
        {
            "symbol": "LOSS.HK",
            "strategy_family": "sell_put",
            "expiration": "2026-08-28",
            "strike": 50,
            "option_type": "put",
            "recommendation_state": "close",
            "evaluation_status": "evaluable",
            "quote_status": "priced",
            "metrics": {"estimated_pnl_if_close_net": -125.5},
        },
        {
            "symbol": "PARTIAL.HK",
            "strategy_family": "sell_put",
            "expiration": "2026-08-28",
            "strike": 55,
            "option_type": "put",
            "recommendation_state": "close",
            "evaluation_status": "evaluable",
            "quote_status": "priced",
            "metrics": {
                "ask": 0.3,
                "estimated_pnl_if_close_net": "nan",
                "remaining_term_ratio": "invalid",
            },
        },
        {
            "symbol": "HOLD.HK",
            "strategy_family": "sell_put",
            "recommendation_state": "hold",
            "evaluation_status": "evaluable",
            "quote_status": "priced",
            "metrics": {"ask": 88, "realized_if_close": 9999},
        },
        {
            "symbol": "GAP.HK",
            "strategy_family": "sell_put",
            "recommendation_state": "not_evaluable",
            "evaluation_status": "not_evaluable",
            "quote_status": "quote_unusable",
            "metrics": {"ask": 77, "realized_if_close": 8888},
        },
    ]
    brief["candidates"] = {"sell_put": [], "covered_call": [], "combo_yield": []}

    message = render_full_brief(brief)

    assert "预计平仓损益 -HK$125.50" in message
    assert "PARTIAL.HK｜Sell Put｜08-28 HK$55 Put｜建议平仓" in message
    assert "买回参考价 HK$0.30（ask）" in message
    assert "nan" not in message.lower()
    assert "invalid" not in message.lower()
    assert "HK$88.00" not in message
    assert "HK$9,999.00" not in message
    assert "HOLD.HK" not in message
    assert "GAP.HK" not in message
    assert "HK$77.00" not in message
    assert "HK$8,888.00" not in message


def test_strict_close_position_is_independent_from_new_combo_candidates() -> None:
    from src.application.daily_decision_brief_renderer import render_full_brief

    brief = _brief()
    brief["candidates"]["combo_yield"] = []
    message = render_full_brief(brief)

    assert "TSLA · 组合增强" not in message
    assert "NVDA｜Sell Put" in message
    assert "PDD" not in message
    assert "combo-pdd-secret" not in message
    assert "funding_put" not in message


def test_blocked_renderer_is_short_safe_and_has_no_candidate_snapshot() -> None:
    from src.application.daily_decision_brief_renderer import render_daily_brief_lifecycle

    brief = _brief()
    brief["actionability"] = "blocked"
    brief["status"] = "blocked"
    message = render_daily_brief_lifecycle(
        {"brief": brief, "diff": {"changes": [{"change_type": "blocked"}]}, "delivery_kind": "full"},
        context=_scheduled_context(),
    )

    assert message.startswith("# OM · 决策简报 · lx")
    assert "状态｜数据异常 · 10:00 批次" in message
    assert "结论｜本轮行情覆盖不足，暂时无法形成可靠决策。" in message
    assert "后续｜系统将在后续批次自动重新评估。" in message
    assert "## Sell Put" not in message
    assert "## 持仓" not in message
    assert "MSFT" not in message
    _assert_no_internal_leak(message)


def test_delta_and_recovery_add_change_banner_but_keep_current_snapshot() -> None:
    from src.application.daily_decision_brief_renderer import render_daily_brief_lifecycle

    brief = _brief()
    delta = render_daily_brief_lifecycle(
        {
            "brief": brief,
            "delivery_kind": "delta",
            "diff": {
                "changes": [
                    {
                        "change_type": "candidate_added",
                        "action": {
                            "action_type": "open_candidate",
                            "strategy_family": "sell_put",
                            "symbol": "MSFT",
                            "expiration": "2026-08-21",
                            "strike": 400,
                            "option_type": "put",
                            "position_lot_id": "secret-in-diff",
                        },
                    },
                    {
                        "change_type": "candidate_capacity_changed",
                        "before": 1,
                        "after": 2,
                        "action": {
                            "action_type": "open_candidate",
                            "strategy_family": "sell_put",
                            "symbol": "MSFT",
                            "expiration": "2026-08-21",
                            "strike": 400,
                            "option_type": "put",
                        },
                    },
                ]
            },
        },
        context=_scheduled_context(),
    )
    assert "状态｜盘中更新 · 10:00 批次" in delta
    assert "较上一轮：新增 1 个 Sell Put 候选" in delta
    assert "MSFT 08-21 $400 Put 条件容量 1 → 2 手" in delta
    assert "MSFT｜Sell Put｜08-21 $400 Put（策略排序 1）" in delta
    assert "secret-in-diff" not in delta

    recovery = render_daily_brief_lifecycle(
        {
            "brief": brief,
            "delivery_kind": "delta",
            "diff": {"changes": [{"change_type": "recovered"}]},
        },
        context=_scheduled_context(),
    )
    assert "状态｜数据已恢复 · 10:00 批次" in recovery
    assert "数据已恢复，以下为当前结果。" in recovery
    assert "MSFT｜Sell Put｜08-21 $400 Put（策略排序 1）" in recovery


def test_old_candidate_diff_vocabulary_is_not_mislabeled_as_position_change() -> None:
    from src.application.daily_decision_brief_renderer import render_daily_brief_lifecycle

    message = render_daily_brief_lifecycle(
        {
            "brief": _brief(),
            "delivery_kind": "delta",
            "diff": {
                "changes": [
                    {
                        "change_type": "action_added",
                        "action": {
                            "action_type": "open_candidate",
                            "strategy_family": "sell_put",
                            "symbol": "MSFT",
                        },
                    },
                    {
                        "change_type": "blocked",
                        "action": {
                            "action_type": "resolve_data_blocker",
                            "symbol": "ACCOUNT",
                        },
                    },
                ]
            },
        }
    )

    assert "新增 1 个 Sell Put 候选" in message
    assert "持仓建议已变化" not in message


def test_position_statuses_use_safe_allowlisted_fallbacks() -> None:
    from src.application.daily_decision_brief_renderer import render_full_brief

    brief = _brief()
    brief["positions"] = [
        {"symbol": "A", "strategy_family": "sell_put", "quote_status": "coverage_missing"},
        {"symbol": "B", "strategy_family": "sell_put", "quote_status": "unavailable"},
        {"symbol": "C", "strategy_family": "sell_put", "quote_status": "future_state"},
        {
            "symbol": "D",
            "strategy_family": "sell_put",
            "quote_status": "priced",
            "evaluation_status": "evaluable",
            "recommendation_state": "hold",
        },
    ]
    message = render_full_brief(brief)

    assert "暂无法评估" not in message
    assert "继续观察" not in message
    assert "**A｜Sell Put" not in message
    assert "**B｜Sell Put" not in message
    assert "**C｜Sell Put" not in message
    assert "**D｜Sell Put" not in message
    assert "汇总｜共 4 条，当前没有需要处理的持仓。" in message
    assert "持仓未展开" not in message
    assert "future_state" not in message


def test_malformed_fields_and_unknown_enums_do_not_echo_raw_values() -> None:
    from src.application.daily_decision_brief_renderer import render_full_brief

    brief = _brief()
    brief["market"] = "FUTURE_MARKET"
    brief["status"] = "FUTURE_STATE"
    brief["actionability"] = "FUTURE_ACTIONABILITY"
    brief["data_as_of_utc"] = "RAW_BAD_TIME"
    brief["candidates"] = {
        "sell_put": [
            {
                "rank": 1,
                "symbol": "TCOM",
                "option_type": "FUTURE_OPTION",
                "expiration": "RAW_BAD_EXPIRY",
                "strike": "RAW_BAD_STRIKE",
                "contract_symbol": "US.TCOM260821P40000",
            }
        ],
        "covered_call": [],
        "combo_yield": [],
    }
    message = render_full_brief(brief)

    assert message.startswith("# OM · 决策简报 · lx")
    assert "市场｜市场" in message
    assert "数据｜数据时间未知" in message
    assert "TCOM｜Sell Put｜合约信息不完整（策略排序 1）" in message
    for raw in (
        "FUTURE_MARKET",
        "FUTURE_STATE",
        "FUTURE_ACTIONABILITY",
        "RAW_BAD_TIME",
        "RAW_BAD_EXPIRY",
        "RAW_BAD_STRIKE",
        "US.TCOM260821P40000",
    ):
        assert raw not in message


def test_manual_trigger_omits_scheduled_batch_and_planning_is_plain_language() -> None:
    from src.application.daily_decision_brief_renderer import render_full_brief

    brief = _brief()
    brief["actionability"] = "planning_only"
    message = render_full_brief(
        brief,
        context={
            **_scheduled_context(),
            "trigger_kind": "force",
        },
    )

    assert "状态｜手动触发" in message
    assert "10:00 批次" not in message
    assert "当前已不在可执行时段，仅供规划参考。" in message
    assert "PLANNING" not in message


def test_renderer_honors_section_limits() -> None:
    from src.application.daily_decision_brief_renderer import render_full_brief

    brief = _brief()
    brief["positions"] = [
        {
            "symbol": f"P{i}",
            "strategy_family": "sell_put",
            "quote_status": "priced",
            "evaluation_status": "evaluable",
            "recommendation_state": "close",
        }
        for i in range(8)
    ]
    brief["candidates"] = {
        "sell_put": [
            _candidate(
                rank=i + 1,
                symbol=f"C{i}",
                option_type="put",
                expiration="2026-08-21",
                strike=100 + i,
                capacity=1,
            )
            for i in range(20)
        ],
        "covered_call": [],
        "combo_yield": [],
    }

    message = render_full_brief(
        brief,
        limits={
            "max_actions_per_priority": 2,
            "max_candidates_per_strategy": 7,
            "max_rejection_reasons": 999,
        },
    )

    assert "P0｜Sell Put" in message
    assert "P1｜Sell Put" in message
    assert "P2｜Sell Put" not in message
    assert "汇总｜共 8 条，需处理 8 条，本消息展示 2 条。" in message
    assert "持仓未展开" not in message
    assert "C6｜Sell Put" in message
    assert "C7｜Sell Put" not in message
    assert "补充｜另有 13 个策略候选未展开" in message
    assert "C6 08-21 $106 Put｜按当前现金最多 1 手" in message
    assert "C7 08-21 $107 Put｜按当前现金最多 1 手" not in message


def test_material_candidates_break_soft_limit_and_keep_funds_in_sync() -> None:
    from src.application.daily_decision_brief_renderer import render_delta_brief

    brief = _brief()
    brief["positions"] = []
    brief["candidates"] = {
        "sell_put": [
            _candidate(
                rank=i + 1,
                symbol=f"C{i}",
                option_type="put",
                expiration="2026-08-21",
                strike=100 + i,
                capacity=i + 1,
            )
            for i in range(4)
        ],
        "covered_call": [],
        "combo_yield": [],
    }
    diff = {
        "changes": [
            {
                "change_type": "candidate_added",
                "action": {
                    "action_type": "open_candidate",
                    "strategy_family": "sell_put",
                    "symbol": symbol,
                    "option_type": "put",
                    "expiration": "2026-08-21",
                    "strike": strike,
                },
            }
            for symbol, strike in (("C2", 102), ("C3", 103))
        ]
    }

    message = render_delta_brief(
        brief,
        diff,
        limits={"max_candidates_per_strategy": 1},
    )

    assert "C2｜Sell Put｜08-21 $102 Put（策略排序 3）" in message
    assert "C3｜Sell Put｜08-21 $103 Put（策略排序 4）" in message
    assert "C0｜Sell Put" not in message
    assert "补充｜另有 2 个策略候选未展开" in message
    assert "C2 08-21 $102 Put｜按当前现金最多 3 手" in message
    assert "C3 08-21 $103 Put｜按当前现金最多 4 手" in message
    assert "C0 08-21 $100 Put｜按当前现金最多 1 手" not in message


def test_invalidated_candidate_banner_keeps_removed_contract_identifiable() -> None:
    from src.application.daily_decision_brief_renderer import render_delta_brief

    brief = _brief()
    brief["positions"] = []
    brief["candidates"] = {"sell_put": [], "covered_call": [], "combo_yield": []}
    diff = {
        "changes": [
            {
                "change_type": "candidate_invalidated",
                "action": {
                    "action_type": "open_candidate",
                    "strategy_family": "sell_put",
                    "symbol": "TCOM",
                    "option_type": "put",
                    "expiration": "2026-08-21",
                    "strike": 40,
                    "contract_symbol": "US.TCOM260821P40000",
                },
            }
        ]
    }

    message = render_delta_brief(brief, diff)

    assert "较上一轮：TCOM 08-21 $40 Put 候选已失效。" in message
    assert "US.TCOM260821P40000" not in message


def test_material_position_uses_exact_lot_before_same_contract_siblings() -> None:
    from src.application.daily_decision_brief_renderer import render_delta_brief

    brief = _brief()
    brief["candidates"] = {"sell_put": [], "covered_call": [], "combo_yield": []}
    brief["positions"] = [
        {
            "symbol": "PDD",
            "strategy_family": "sell_put",
            "expiration": "2026-08-21",
            "strike": 95,
            "option_type": "put",
            "contract_symbol": "US.PDD260821P95000",
            "position_lot_id": f"lot-{i}",
            "evaluation_status": "evaluable",
            "quote_status": "priced",
            "recommendation_state": state,
        }
        for i, state in enumerate(("hold", "hold", "close"))
    ]
    diff = {
        "changes": [
            {
                "change_type": "action_added",
                "action": {
                    "action_type": "close_position",
                    "strategy_family": "sell_put",
                    "symbol": "PDD",
                    "option_type": "put",
                    "expiration": "2026-08-21",
                    "strike": 95,
                    "contract_symbol": "US.PDD260821P95000",
                    "position_lot_id": "lot-2",
                },
            }
        ]
    }

    view_message = render_delta_brief(
        brief,
        diff,
        limits={"max_actions_per_priority": 1},
    )

    assert "PDD｜Sell Put｜08-21 $95 Put｜建议平仓" in view_message
    assert "继续观察" not in view_message
    assert "汇总｜共 3 条，需处理 1 条。" in view_message
    assert "持仓未展开" not in view_message
    assert "lot-2" not in view_message


def test_renderer_honors_total_length_bound() -> None:
    from src.application.daily_decision_brief_renderer import render_full_brief

    brief = _brief()
    brief["candidates"]["sell_put"] = [
        _candidate(
            rank=i + 1,
            symbol=(f"C{i}" + "X" * 2_000),
            option_type="put",
            expiration="2026-08-21",
            strike=100 + i,
            capacity=1,
        )
        for i in range(20)
    ]
    message = render_full_brief(
        brief,
        limits={"max_candidates_per_strategy": 20},
    )

    assert len(message) <= 12_000
    assert "消息已按总长度上限截断" in message


def test_no_delivery_kind_renders_empty_message() -> None:
    from src.application.daily_decision_brief_renderer import render_daily_brief_lifecycle

    assert render_daily_brief_lifecycle({"brief": deepcopy(_brief()), "delivery_kind": "none"}) == ""


def test_notification_and_query_projections_use_plain_language_and_account_funds() -> None:
    from src.application.daily_decision_brief_renderer import (
        render_candidate_alert,
        render_fixed_failure,
        render_fixed_report,
        render_query_brief,
    )

    brief = deepcopy(_brief())
    brief["funds"] = {
        "cash_total_by_currency": {"USD": 180_000.0},
        "option_opening_available_by_currency": {"USD": 75_000.0},
        "cash_total_cny": 1_260_000.0,
        "cash_secured_total_cny": 735_000.0,
        "option_opening_available_cny": 525_000.0,
        "available": True,
        "reason": "ok",
    }
    candidate_index = []
    for family in ("sell_put", "covered_call", "combo_yield"):
        for row in brief["candidates"][family]:
            representative = deepcopy(row)
            representative["strategy_family"] = family
            identity = f"candidate:v1:lx:US:{row['symbol']}:{family}"
            candidate_index.append(
                {
                    "identity": identity,
                    "symbol": row["symbol"],
                    "strategy_family": family,
                    "representative": representative,
                    "contract_count": 1,
                }
            )
    brief["candidate_index"] = candidate_index

    fixed = render_fixed_report(brief, context=_scheduled_context())
    assert fixed.startswith("# OM · 决策简报 · lx")
    assert "状态｜10:00 批次" in fixed
    assert "## Sell Put" in fixed
    assert "现金总额｜$180,000.00" not in fixed
    assert "现金总额（折CNY）｜¥1,260,000.00" in fixed
    assert "可用于期权开仓｜$75,000.00" not in fixed
    assert "可用于期权开仓（折CNY）｜¥525,000.00" in fixed
    assert all(label not in fixed for label in ("总资产", "NAV", "证券市值", "revision"))

    alert_context = {**_scheduled_context(), "scheduled_target_market": "10:30"}
    alert = render_candidate_alert(
        brief,
        [item["identity"] for item in candidate_index],
        limits={"max_candidates_per_strategy": 1},
        context=alert_context,
    )
    assert "状态｜新增候选 · 10:30 发现" in alert
    assert "## Sell Put" in alert
    assert "### 新增策略候选" in alert
    assert "## 持仓" not in alert
    assert "另有 1 个新增候选未展开" in alert
    assert "MSFT｜Sell Put" in alert
    assert "NVDA｜Sell Put" in alert
    assert "较上一轮" not in alert
    assert "现金总额｜$180,000.00" not in alert
    assert "现金总额（折CNY）｜¥1,260,000.00" in alert

    failure = render_fixed_failure(brief, context=_scheduled_context())
    assert "数据异常 · 10:00 批次失败" in failure
    assert "未形成可靠结果" in failure
    assert "## Sell Put" not in failure
    assert "本轮暂无符合条件的候选" not in failure
    assert_mobile_flat_markdown(fixed)
    assert_mobile_flat_markdown(alert)
    assert_mobile_flat_markdown(failure)

    current_query = render_query_brief(
        brief,
        context={"query_time_utc": "2026-07-20T15:00:00+00:00"},
    )
    stale_query = render_query_brief(
        brief,
        context={"query_time_utc": "2026-07-21T15:00:00+00:00"},
    )
    assert "当前查询 · 查询时间" in current_query
    assert "状态｜今日最新" in current_query
    assert "状态｜已过期，仅供计划参考" in stale_query
    assert "今日扫描暂不可用" in stale_query
    assert "revision" not in current_query + stale_query
    assert_mobile_flat_markdown(current_query)
    assert_mobile_flat_markdown(stale_query)


def test_funds_fall_back_to_per_currency_lines_when_cny_unavailable() -> None:
    from src.application.daily_decision_brief_renderer import render_fixed_report

    brief = deepcopy(_brief())
    brief["funds"] = {
        "cash_total_by_currency": {"HKD": 480_000.0, "USD": 18_000.0},
        "option_opening_available_by_currency": {"HKD": 225_000.0},
        "available": True,
        "reason": "ok",
    }

    message = render_fixed_report(brief, context=_scheduled_context())

    assert "现金总额｜HK$480,000.00" in message
    assert "现金总额｜$18,000.00" in message
    assert "可用于期权开仓｜HK$225,000.00" in message
    assert "折CNY" not in message


def test_funds_unknown_are_explicit_and_never_rendered_as_zero() -> None:
    from src.application.daily_decision_brief_renderer import render_fixed_report

    brief = deepcopy(_brief())
    brief["funds"] = {
        "cash_total_by_currency": {},
        "option_opening_available_by_currency": {},
        "available": False,
        "reason": "portfolio_cash_unavailable",
    }

    message = render_fixed_report(brief, context=_scheduled_context())

    assert "现金总额｜暂不可用" in message
    assert "可用于期权开仓｜暂不可用" in message
    assert "现金总额｜$0" not in message


def test_render_limit_normalization_remains_bounded() -> None:
    from src.application.daily_decision_brief_renderer import resolve_daily_brief_render_limits

    assert resolve_daily_brief_render_limits(
        {
            "max_actions_per_priority": 0,
            "max_candidates_per_strategy": "7",
            "max_rejection_reasons": 999,
        }
    ) == {
        "max_actions_per_priority": 1,
        "max_candidates_per_strategy": 7,
        "max_rejection_reasons": 20,
    }


def _render_event_risk(
    state: str,
    *,
    date: str | None = None,
    relation: str = "before_expiration",
) -> dict:
    event = (
        {
            "event_id": "event-q2",
            "event_series_id": "event-series-earnings",
            "event_type": "earnings",
            "event_date": date,
            "anchored": True,
        }
        if date
        else None
    )
    return {
        "user_state": state,
        "reason_code": "internal_raw_reason_must_not_render",
        "reliable": state != "unknown",
        "evidence_chain_id": "internal-event-chain",
        "nearest_event": event,
        "events": [event] if event else [],
        "expiration_relations": (
            {
                "contract": {
                    "expiration": "2026-08-21",
                    "relation": relation,
                    "days_before_expiration": 16,
                }
            }
            if event
            else {}
        ),
        "in_attention_window": relation in {"before_expiration", "on_expiration"} if event else False,
    }


def test_candidate_event_lines_render_decision_semantics_without_raw_enums() -> None:
    from src.application.daily_decision_brief_renderer import render_full_brief

    brief = _brief()
    brief["market_trading_date"] = "2026-07-21"
    brief["candidates"]["sell_put"][0]["event_risk"] = _render_event_risk(
        "confirmed_event", date="2026-08-05"
    )
    brief["candidates"]["sell_put"][1]["event_risk"] = _render_event_risk("unknown")
    brief["candidates"]["covered_call"][0]["event_risk"] = _render_event_risk("confirmed_none")

    message = render_full_brief(brief)

    assert "预计 8 月 5 日发布财报，早于当前 Put 到期日；执行前需要重新确认事件窗口和报价。" in message
    assert "近期事件数据不完整，当前无法确认没有重要事件；执行前需要再次检查。" in message
    assert "已确认当前期权到期前没有近期重要事件；执行前仍需复核报价。" in message
    assert "internal_raw_reason_must_not_render" not in message
    assert "internal-event-chain" not in message


def test_event_date_change_summary_names_candidate_and_expiry_relation() -> None:
    from src.application.daily_decision_brief_renderer import render_delta_brief

    brief = _brief()
    brief["market_trading_date"] = "2026-07-21"
    brief["candidates"]["sell_put"][1]["event_risk"] = _render_event_risk(
        "confirmed_event", date="2026-08-05"
    )
    before = _render_event_risk("confirmed_event", date="2026-08-25", relation="after_expiration")
    after = _render_event_risk("confirmed_event", date="2026-08-05")
    action = {
        "action_id": "action-nvda-put",
        "action_type": "open_candidate",
        "strategy_family": "sell_put",
        "symbol": "NVDA",
        "option_type": "put",
        "expiration": "2026-08-21",
        "strike": 100,
        "contract_symbol": "NVDA260821P00100000",
    }

    message = render_delta_brief(
        brief,
        {
            "changes": [
                {
                    "change_type": "candidate_event_date_changed",
                    "action": action,
                    "before_event_risk": before,
                    "after_event_risk": after,
                },
                {
                    "change_type": "candidate_event_entered_expiry_window",
                    "action": action,
                    "before_event_risk": before,
                    "after_event_risk": after,
                },
            ]
        },
    )

    assert "较上一轮：NVDA 08-21 $100 Put 财报日期调整至 8 月 5 日，现在早于当前 Put 到期日。" in message
    assert message.count("进入当前合约关注窗口") == 0
    assert "NVDA｜Sell Put｜08-21 $100 Put（策略排序 2）" in message


def test_event_evidence_degradation_summary_does_not_claim_event_removal() -> None:
    from src.application.daily_decision_brief_renderer import render_delta_brief

    action = {
        "action_id": "action-nvda-put",
        "action_type": "open_candidate",
        "strategy_family": "sell_put",
        "symbol": "NVDA",
        "option_type": "put",
        "expiration": "2026-08-21",
        "strike": 100,
    }
    message = render_delta_brief(
        _brief(),
        {
            "changes": [
                {
                    "change_type": "candidate_event_evidence_degraded",
                    "action": action,
                    "before_event_risk": _render_event_risk("confirmed_event", date="2026-08-05"),
                    "after_event_risk": _render_event_risk("unknown"),
                }
            ]
        },
    )

    assert "近期事件数据变得不完整，当前无法确认没有重要事件" in message
    assert "确认移除" not in message


def test_data_recovery_keeps_candidate_event_change_summary() -> None:
    from src.application.daily_decision_brief_renderer import render_delta_brief

    action = {
        "action_id": "action-nvda-put",
        "action_type": "open_candidate",
        "strategy_family": "sell_put",
        "symbol": "NVDA",
        "option_type": "put",
        "expiration": "2026-08-21",
        "strike": 100,
    }
    message = render_delta_brief(
        _brief(),
        {
            "changes": [
                {"change_type": "recovered"},
                {
                    "change_type": "candidate_event_evidence_recovered",
                    "action": action,
                    "before_event_risk": _render_event_risk("unknown"),
                    "after_event_risk": _render_event_risk("confirmed_event", date="2026-08-05"),
                },
            ]
        },
    )

    assert "数据已恢复，以下为当前结果" in message
    assert "事件证据已恢复，现预计 8 月 5 日发布财报" in message


def test_ai_referenced_candidate_beyond_top_n_is_expanded_with_true_rank() -> None:
    from src.application.daily_decision_brief_renderer import render_fixed_report

    brief = _brief()
    extra = [
        _candidate(
            rank=rank,
            symbol=f"C{rank}",
            option_type="put",
            expiration="2026-08-21",
            strike=100 + rank,
            capacity=1,
        )
        for rank in (3, 4)
    ]
    brief["candidates"]["sell_put"][1]["candidate_id"] = "put-rank2"
    extra[1]["candidate_id"] = "put-rank4"
    brief["candidates"]["sell_put"].extend(extra)
    brief["candidates"]["covered_call"] = []
    brief["ai_decision_advice"] = {
        "status": "completed",
        "unavailable_reason": None,
        "evidence_as_of": "2026-07-20T13:00:00+00:00",
        "sell_put": {
            "action": "switch",
            "baseline_candidate_id": "put-rank2",
            "selected_candidate_id": "put-rank4",
            "rationale": {"risk_mechanism": "集中度叠加", "candidate_effect": "加重风险"},
            "source_refs": {},
        },
        "covered_call": None,
        "zero_candidate": {"sell_put": False, "covered_call": True},
        "reused": False,
        "advice_record_id": "adv-1",
    }

    message = render_fixed_report(
        brief,
        limits={"max_candidates_per_strategy": 1},
        context=_scheduled_context(),
    )
    # AI 引用的 baseline（排序 2）与改选目标（排序 4）都必须展开且保留真实排序。
    assert "NVDA｜Sell Put｜08-21 $100 Put（策略排序 2）" in message
    assert "C4｜Sell Put｜08-21 $104 Put（策略排序 4）" in message
    assert "C3｜Sell Put" not in message
    assert "### AI建议" in message
    assert "建议改选策略排序 4：C4 08-21 $104 Put。" in message
    assert "## Covered Call" in message
    assert "本轮无可供 AI 评估的策略候选。" in message


def test_ai_advice_source_lines_render_from_brief_evidence_index() -> None:
    from src.application.daily_decision_brief_renderer import render_fixed_report

    brief = _brief()
    brief["candidates"]["sell_put"][1]["candidate_id"] = "put-rank2"
    brief["ai_decision_advice"] = {
        "status": "completed",
        "unavailable_reason": None,
        "evidence_as_of": "2026-07-20T13:00:00+00:00",
        "sell_put": {
            "action": "defer",
            "baseline_candidate_id": "put-rank2",
            "selected_candidate_id": None,
            "rationale": {"risk_mechanism": "监管风险上升"},
            "source_refs": {"external_evidence_refs": ["ev-aaa111bbb222"]},
        },
        "covered_call": None,
        "zero_candidate": {"sell_put": False, "covered_call": True},
        "reused": False,
        "advice_record_id": "adv-1",
    }
    brief["ai_decision_advice_evidence_index"] = {
        "frozen_at": "2026-07-20T13:00:00+00:00",
        "symbols": [
            {
                "symbol": "NVDA",
                "coverage": "completed",
                "evidence": [
                    {
                        "ref": "ev-aaa111bbb222",
                        "source": {
                            "title": "监管新规征求意见",
                            "publisher": "监管机构",
                            "published_at": "2026-07-19",
                            "url": "https://example.gov/rule",
                        },
                    }
                ],
            }
        ],
    }

    message = render_fixed_report(brief, context=_scheduled_context())
    assert "### AI建议" in message
    assert (
        "来源｜监管新规征求意见（监管机构 · 2026-07-19 · example.gov） "
        "https://example.gov/rule"
    ) in message


def test_fixed_report_card_places_ai_advice_inside_strategy_module() -> None:
    from src.application.daily_decision_brief_renderer import (
        render_fixed_report_card_markdown,
    )

    brief = _brief()
    brief["candidates"]["sell_put"][0]["candidate_id"] = "put-1"
    brief["ai_decision_advice"] = {
        "status": "completed",
        "unavailable_reason": None,
        "evidence_as_of": "2026-07-20T13:00:00+00:00",
        "sell_put": {
            "action": "keep",
            "baseline_candidate_id": "put-1",
            "selected_candidate_id": "put-1",
            "rationale": {},
            "source_refs": {},
        },
        "covered_call": None,
        "zero_candidate": {"sell_put": False, "covered_call": False},
        "reused": False,
        "advice_record_id": "adv-1",
    }

    message = render_fixed_report_card_markdown(
        brief,
        context=_scheduled_context(),
    )

    sell_put = message.index("## Sell Put")
    advice = message.index("### AI建议", sell_put)
    candidates = message.index("### 策略候选", advice)
    assert sell_put < advice < candidates
    assert "结论｜维持策略排序 1。" in message


def test_unavailable_ai_copy_matches_each_family_candidate_presence() -> None:
    from src.application.daily_decision_brief_renderer import render_fixed_report

    brief = _brief()
    brief["candidates"]["sell_put"] = []
    brief["ai_decision_advice"] = {
        "status": "unavailable",
        "unavailable_reason": "timeout",
        "evidence_as_of": None,
        "sell_put": None,
        "covered_call": None,
        "zero_candidate": {"sell_put": False, "covered_call": False},
        "reused": False,
        "advice_record_id": None,
    }

    message = render_fixed_report(brief, context=_scheduled_context())
    sell_put_block = message[
        message.index("## Sell Put") : message.index("## Covered Call")
    ]
    covered_call_block = message[
        message.index("## Covered Call") : message.index("## 组合增强")
    ]

    assert "AI建议未完成；本轮没有可展示的策略原始排序。" in sell_put_block
    assert "以下仅展示策略原始排序" not in sell_put_block
    assert "AI建议未完成；以下仅展示策略原始排序" in covered_call_block


def test_unavailable_ai_copy_uses_raw_family_presence_when_render_budget_omits_rows() -> None:
    from src.application.daily_decision_brief_renderer import render_delta_brief

    brief = _brief()
    brief["candidates"]["sell_put"] = [
        _candidate(
            rank=index + 1,
            symbol=f"P{index}",
            option_type="put",
            expiration="2026-08-21",
            strike=100 + index,
        )
        for index in range(41)
    ]
    brief["ai_decision_advice"] = {
        "status": "unavailable",
        "unavailable_reason": "timeout",
        "evidence_as_of": None,
        "sell_put": None,
        "covered_call": None,
        "zero_candidate": {"sell_put": False, "covered_call": False},
        "reused": False,
        "advice_record_id": None,
    }
    diff = {
        "changes": [
            {
                "change_type": "candidate_added",
                "action": {
                    "action_type": "open_candidate",
                    "strategy_family": "sell_put",
                    "symbol": f"P{index}",
                    "option_type": "put",
                    "expiration": "2026-08-21",
                    "strike": 100 + index,
                },
            }
            for index in range(41)
        ]
    }

    message = render_delta_brief(
        brief,
        diff,
        limits={"max_candidates_per_strategy": 1},
    )
    covered_call_block = message[
        message.index("## Covered Call") : message.index("## 持仓")
    ]

    assert "补充｜另有 1 个策略候选未展开" in covered_call_block
    assert "AI建议未完成；以下仅展示策略原始排序" in covered_call_block
    assert "本轮没有可展示的策略原始排序" not in covered_call_block


def test_candidate_alert_ai_can_select_old_candidate_without_relisting_it() -> None:
    from domain.domain.daily_decision_brief import (
        build_daily_brief_candidate_identity,
    )
    from src.application.daily_decision_brief_renderer import (
        render_candidate_alert_card_markdown,
    )

    brief = _brief()
    new_candidate = brief["candidates"]["sell_put"][0]
    old_candidate = brief["candidates"]["sell_put"][1]
    new_candidate["candidate_id"] = "put-new"
    old_candidate["candidate_id"] = "put-old"
    identity = build_daily_brief_candidate_identity(
        account="lx",
        market="US",
        symbol="MSFT",
        strategy_family="sell_put",
    )
    brief["candidate_index"] = [
        {
            "identity": identity,
            "symbol": "MSFT",
            "strategy_family": "sell_put",
            "representative": new_candidate,
            "contract_count": 1,
        }
    ]
    brief["ai_decision_advice"] = {
        "status": "completed",
        "unavailable_reason": None,
        "evidence_as_of": "2026-07-20T13:00:00+00:00",
        "sell_put": {
            "action": "switch",
            "baseline_candidate_id": "put-new",
            "selected_candidate_id": "put-old",
            "rationale": {
                "risk_mechanism": "当前首选存在额外事件风险",
                "candidate_effect": "风险落在本次到期窗口内",
                "decision_reason": "旧候选避开该窗口",
            },
            "source_refs": {},
        },
        "covered_call": None,
        "zero_candidate": {"sell_put": False, "covered_call": False},
        "reused": False,
        "advice_record_id": "adv-1",
    }

    message = render_candidate_alert_card_markdown(
        brief,
        [identity],
        context=_scheduled_context(),
    )

    assert "结论｜建议改选策略排序 2：NVDA 08-21 $100 Put。" in message
    assert "**MSFT｜Sell Put｜08-21 $400 Put（策略排序 1）**" in message
    assert "**NVDA｜Sell Put" not in message


def test_fixed_report_card_renders_candidate_paragraphs_and_actionable_position_table() -> None:
    from src.application.daily_decision_brief_renderer import render_fixed_report_card_markdown

    brief = _brief()
    brief["positions"][0].update(
        {
            "strategy_family": "sell_put",
            "leg_role": None,
            "recommendation_state": "close",
            "notification_eligible": True,
        }
    )
    brief["positions"][0]["metrics"] = {
        "ask": 0.35,
        "estimated_pnl_if_close_net": 285,
        "net_capture_ratio": 0.925,
        "close_cost_ratio": 0.0008,
        "remaining_term_ratio": 0.61,
    }
    brief["positions"].append(
        {
            "symbol": "AMD",
            "strategy_family": "sell_put",
            "expiration": "2026-08-21",
            "strike": 150,
            "option_type": "put",
            "recommendation_state": "close",
            "notification_eligible": True,
            "evaluation_status": "evaluable",
            "quote_status": "priced",
            "metrics": {
                "ask": 0.52,
                "estimated_pnl_if_close_net": 74.5,
                "net_capture_ratio": 0.91,
                "close_cost_ratio": 0.0009,
                "remaining_term_ratio": 0.55,
            },
        }
    )

    message = render_fixed_report_card_markdown(
        brief,
        context=_scheduled_context(),
    )

    assert "| 优先 | 合约 | 权利金 / 净收入 | 年化 | 风险 / 容量 |" not in message
    assert "## Sell Put" in message
    assert "### 策略候选" in message
    assert "**MSFT｜Sell Put｜08-21 $400 Put（策略排序 1）**" in message
    assert "**NVDA｜Sell Put｜08-21 $100 Put（策略排序 2）**" in message
    assert (
        "指标｜权利金 $5.25 · 门槛年化 18.1% · Delta -0.24 · 32 天 · "
        "预计净收入 $480.00"
        in message
    )
    assert "## Covered Call" in message
    assert "**AAPL｜Covered Call｜08-21 $250 Call（策略排序 1）**" in message
    assert "| 优先 | 标的 | Put 侧 | Call 侧 | 收益 |" not in message
    assert "## 组合增强" in message
    assert "**TSLA｜组合增强（策略排序 1）**" in message
    assert "Put｜08-21 $300 Put｜推荐卖出 $3.45" in message
    assert "Call｜09-18 $400 Call｜推荐买入 $1.05" in message
    assert "指标｜门槛年化 15.4% · 预计净收入 $620.00" in message
    assert "\n\n事件｜" in message
    assert "**1｜NVDA｜Sell Put｜08-21 $100 Put｜建议平仓**" in message
    assert "参考｜买回参考价 $0.35 · 预计锁定损益 +$285.00" in message
    assert "净兑现比例 92.5%" in message
    assert "平仓成本占本金 0.1%" in message
    assert "剩余期限比例 61.0%" in message
    assert "AMD｜Sell Put｜08-21 $150 Put｜建议平仓" in message
    assert "现金总额｜暂不可用" in message
    assert "可用于期权开仓｜暂不可用" in message
    assert "| 项目 | 数值 |" not in message
    assert "<br>" not in message
    _assert_no_internal_leak(message)

def test_combo_candidate_prices_are_explicit_when_leg_quotes_are_missing() -> None:
    from src.application.daily_decision_brief_renderer import render_fixed_report_card_markdown

    brief = _brief()
    combo = brief["candidates"]["combo_yield"][0]
    combo.pop("put_sell_reference")
    combo.pop("call_buy_reference")

    message = render_fixed_report_card_markdown(
        brief,
        context=_scheduled_context(),
    )

    assert "Put｜08-21 $300 Put｜推荐卖出价暂不可用" in message
    assert "Call｜09-18 $400 Call｜推荐买入价暂不可用" in message


def test_candidate_alert_card_keeps_single_candidate_compact_and_events_explicit() -> None:
    from domain.domain.daily_decision_brief import build_daily_brief_candidate_identity
    from src.application.daily_decision_brief_renderer import render_candidate_alert_card_markdown

    brief = _brief()
    identity = build_daily_brief_candidate_identity(
        account="lx",
        market="US",
        symbol="MSFT",
        strategy_family="sell_put",
    )
    representative = deepcopy(brief["candidates"]["sell_put"][0])
    representative["strategy_family"] = "sell_put"
    brief["candidate_index"] = [
        {
            "identity": identity,
            "symbol": "MSFT",
            "strategy_family": "sell_put",
            "representative": representative,
            "contract_count": 1,
        }
    ]

    message = render_candidate_alert_card_markdown(
        brief,
        [identity],
        context=_scheduled_context(),
    )

    assert "## Sell Put" in message
    assert "### 新增策略候选" in message
    assert "**MSFT｜Sell Put｜08-21 $400 Put（策略排序 1）**" in message
    assert "| 优先 | 合约 |" not in message
    assert "\n\n事件｜Sell Put #1：" in message
    assert message.count("执行前需要再次检查") == 1
    assert "## 持仓" not in message
    assert "现金总额｜暂不可用" in message
    assert "可用于期权开仓｜暂不可用" in message
    assert "| 项目 | 数值 |" not in message


def test_evidence_hold_is_rendered_as_waiting_not_current_recommendation() -> None:
    from src.application.daily_decision_brief_renderer import render_fixed_report

    brief = _brief()
    brief["candidates"] = {
        "sell_put": [],
        "covered_call": [],
        "combo_yield": [],
    }
    brief["actions"] = [
        {
            "priority": "P1",
            "state": "observe",
            "action_type": "open_candidate",
            "strategy_family": "sell_put",
            "account": "lx",
            "symbol": "NVDA",
            "option_type": "put",
            "side": "short",
            "expiration": "2026-08-21",
            "strike": 100,
            "contract_symbol": "NVDA260821P00100000",
            "evidence_state": "unavailable",
            "evidence_reason": "empty_chain",
        }
    ]

    rendered = render_fixed_report(brief, context=_scheduled_context())

    assert "行情证据不可用，待恢复（不是当前推荐）" in rendered
    assert "原候选仅保留待恢复身份，不是当前推荐" in rendered
