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
                "strategy_family": "combo_yield",
                "account": "lx",
                "symbol": "NVDA",
                "contract_symbol": "US.NVDA260821P100000",
                "position_lot_id": "lot-put-secret",
                "strategy_group_id": "combo-secret",
                "leg_role": "funding_put",
                "reason": "internal reason",
            }
        ],
        "positions": [
            {
                "symbol": "NVDA",
                "strategy_family": "combo_yield",
                "leg_role": "funding_put",
                "expiration": "2026-08-21",
                "strike": 100,
                "option_type": "put",
                "contract_symbol": "US.NVDA260821P100000",
                "close_action": "close_put_keep_call",
                "evaluation_status": "evaluable",
                "quote_status": "priced",
                "position_lot_id": "lot-put-secret",
                "strategy_group_id": "combo-secret",
            },
            {
                "symbol": "PDD",
                "strategy_family": "combo_yield",
                "leg_role": "funding_put",
                "contract_symbol": "US.PDD260821P95000",
                "close_action": "not_evaluable",
                "evaluation_status": "not_evaluable",
                "quote_status": "coverage_missing",
                "position_lot_id": "lot-pdd-secret",
                "strategy_group_id": "combo-pdd-secret",
            },
            {
                "symbol": "FUTU",
                "strategy_family": "sell_put",
                "close_action": "not_evaluable",
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
    assert "## 候选" in message
    assert "## 持仓" in message
    assert "## 资金" in message
    assert "MSFT｜Sell Put｜08-21 $400 Put（首选）" in message
    assert "NVDA｜Sell Put｜08-21 $100 Put（备选 2）" in message
    assert "AAPL｜Covered Call｜08-21 $250 Call（首选）" in message
    assert "TSLA｜组合增强（首选）" in message
    assert "Put｜08-21 $300 Put" in message
    assert "Call｜09-18 $400 Call" in message
    assert "PDD｜组合增强（Put 侧）｜暂无法评估（行情覆盖不足）" in message
    assert "FUTU｜Sell Put｜暂无法评估（价格不可用）" in message
    assert "MSFT 08-21 $400 Put｜按当前现金最多 2 手" in message
    assert "NVDA 08-21 $100 Put｜按当前现金最多 5 手" in message
    assert "多个 Sell Put 候选共享同一现金额度，手数不能相加" in message
    assert_mobile_flat_markdown(message)
    _assert_no_internal_leak(message)
    _assert_no_internal_leak(view)


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
            "close_action": "close",
            "evaluation_status": "evaluable",
            "quote_status": "priced",
            "metrics": {
                "close_mid": 0.52,
                "realized_if_close": 474.5,
                "remaining_annualized_return": 0.042,
            },
        }
    ]
    brief["candidates"] = {"sell_put": [], "covered_call": [], "combo_yield": []}

    message = render_full_brief(brief)

    assert "3690.HK｜Sell Put｜08-28 HK$65 Put｜建议平仓" in message
    assert (
        "参考｜参考平仓价 HK$0.52（mid） · 预计锁定收益 HK$474.50 · 剩余年化 4.2%"
        in message
    )

    brief["market"] = "US"
    brief["positions"][0]["symbol"] = "NVDA"
    us_message = render_full_brief(brief)
    assert "参考平仓价 $0.52（mid） · 预计锁定收益 $474.50 · 剩余年化 4.2%" in us_message


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
            "close_action": "close",
            "evaluation_status": "evaluable",
            "quote_status": "priced",
            "metrics": {"realized_if_close": -125.5},
        },
        {
            "symbol": "PARTIAL.HK",
            "strategy_family": "sell_put",
            "expiration": "2026-08-28",
            "strike": 55,
            "option_type": "put",
            "close_action": "close",
            "evaluation_status": "evaluable",
            "quote_status": "priced",
            "metrics": {
                "close_mid": 0.3,
                "realized_if_close": "nan",
                "remaining_annualized_return": "invalid",
            },
        },
        {
            "symbol": "HOLD.HK",
            "strategy_family": "sell_put",
            "close_action": "hold",
            "evaluation_status": "evaluable",
            "quote_status": "priced",
            "metrics": {"close_mid": 88, "realized_if_close": 9999},
        },
        {
            "symbol": "GAP.HK",
            "strategy_family": "sell_put",
            "close_action": "not_evaluable",
            "evaluation_status": "not_evaluable",
            "quote_status": "quote_unusable",
            "metrics": {"close_mid": 77, "realized_if_close": 8888},
        },
    ]
    brief["candidates"] = {"sell_put": [], "covered_call": [], "combo_yield": []}

    message = render_full_brief(brief)

    assert "预计平仓损益 -HK$125.50" in message
    assert "PARTIAL.HK｜Sell Put｜08-28 HK$55 Put｜建议平仓" in message
    assert "参考平仓价 HK$0.30（mid）" in message
    assert "nan" not in message.lower()
    assert "invalid" not in message.lower()
    assert "HK$88.00" not in message
    assert "HK$9,999.00" not in message
    assert "HK$77.00" not in message
    assert "HK$8,888.00" not in message


def test_combo_position_attribution_is_independent_from_new_combo_candidates() -> None:
    from src.application.daily_decision_brief_renderer import render_full_brief

    brief = _brief()
    brief["candidates"]["combo_yield"] = []
    message = render_full_brief(brief)

    assert "TSLA · 组合增强" not in message
    assert "PDD｜组合增强（Put 侧）｜暂无法评估（行情覆盖不足）" in message
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
    assert "## 候选" not in message
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
    assert "MSFT｜Sell Put｜08-21 $400 Put（首选）" in delta
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
    assert "MSFT｜Sell Put｜08-21 $400 Put（首选）" in recovery


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
            "close_action": "hold",
        },
    ]
    message = render_full_brief(brief)

    assert "A｜Sell Put｜暂无法评估（行情覆盖不足）" in message
    assert "B｜Sell Put｜暂无法评估（价格不可用）" in message
    assert "C｜Sell Put｜暂无法评估（数据暂不可用）" in message
    assert "D｜Sell Put｜继续观察" in message
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
    assert "TCOM｜Sell Put｜合约信息不完整（首选）" in message
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
            "close_action": "hold",
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
    assert "另有 6 个持仓未展开" in message
    assert "C6｜Sell Put" in message
    assert "C7｜Sell Put" not in message
    assert "Sell Put 另有 13 个候选未展开" in message
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

    assert "C2｜Sell Put｜08-21 $102 Put（备选 3）" in message
    assert "C3｜Sell Put｜08-21 $103 Put（备选 4）" in message
    assert "C0｜Sell Put" not in message
    assert "Sell Put 另有 2 个候选未展开" in message
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
            "strategy_family": "combo_yield",
            "leg_role": "funding_put",
            "expiration": "2026-08-21",
            "strike": 95,
            "option_type": "put",
            "contract_symbol": "US.PDD260821P95000",
            "position_lot_id": f"lot-{i}",
            "evaluation_status": "evaluable",
            "quote_status": "priced",
            "close_action": action,
        }
        for i, action in enumerate(("hold", "hold", "close_put_keep_call"))
    ]
    diff = {
        "changes": [
            {
                "change_type": "action_added",
                "action": {
                    "action_type": "close_position",
                    "strategy_family": "combo_yield",
                    "symbol": "PDD",
                    "option_type": "put",
                    "expiration": "2026-08-21",
                    "strike": 95,
                    "contract_symbol": "US.PDD260821P95000",
                    "position_lot_id": "lot-2",
                    "leg_role": "funding_put",
                },
            }
        ]
    }

    view_message = render_delta_brief(
        brief,
        diff,
        limits={"max_actions_per_priority": 1},
    )

    assert "PDD｜组合增强（Put 侧）｜08-21 $95 Put｜建议平掉 Put，保留 Call" in view_message
    assert "继续观察" not in view_message
    assert "另有 2 个持仓未展开" in view_message
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
    assert "## 当前候选" in fixed
    assert "现金总额｜$180,000.00" in fixed
    assert "可用于期权开仓｜$75,000.00" in fixed
    assert all(label not in fixed for label in ("总资产", "NAV", "证券市值", "revision"))

    alert_context = {**_scheduled_context(), "scheduled_target_market": "10:30"}
    alert = render_candidate_alert(
        brief,
        [item["identity"] for item in candidate_index],
        limits={"max_candidates_per_strategy": 1},
        context=alert_context,
    )
    assert "状态｜新增候选 · 10:30 发现" in alert
    assert "## 新增候选" in alert
    assert "## 持仓" not in alert
    assert "另有 1 个新增候选未展开" in alert
    assert "MSFT｜Sell Put" in alert
    assert "NVDA｜Sell Put" in alert
    assert "较上一轮" not in alert
    assert "现金总额｜$180,000.00" in alert

    failure = render_fixed_failure(brief, context=_scheduled_context())
    assert "数据异常 · 10:00 批次失败" in failure
    assert "未形成可靠结果" in failure
    assert "## 当前候选" not in failure
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
    assert "NVDA｜Sell Put｜08-21 $100 Put（备选 2）" in message


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
