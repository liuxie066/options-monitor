from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from src.application.copilot import tools as copilot_tools
from src.application.copilot.agent import ModelRequest, ModelTurn, ToolCall
from src.application.copilot.contracts import AppResult, CopilotRequest, CopilotScope, new_id
from src.application.copilot.host import run_contract
from src.application.copilot.service import prepare_contract


@dataclass(frozen=True)
class Scenario:
    name: str
    question: str
    turns: tuple[ModelTurn, ...]
    tool_results: dict[str, dict[str, Any]]
    expected_tools: tuple[str, ...]
    expected_terms: tuple[str, ...]
    forbidden_terms: tuple[str, ...] = ()
    context: tuple[dict[str, str], ...] = ()


def _call(name: str, arguments: dict[str, Any], call_id: str) -> ToolCall:
    return ToolCall(call_id=call_id, name=name, arguments=arguments)


SCENARIOS = (
    Scenario(
        name="option_performance_mtd_synthesis",
        question="7月 mtd 的期权收益",
        turns=(
            ModelTurn(
                tool_calls=(
                    _call(
                        "option_performance_report",
                        {"period": "mtd"},
                        "income_1",
                    ),
                )
            ),
            ModelTurn(
                text=(
                    "结论：截至 2026-07-23 的 MTD，全部账户（lx、sy）期权与指派股票"
                    "合计已实现净 PnL 为 USD 747；其中纯期权 USD 250，指派股票 USD 497。"
                    "总现金变动净额为 USD -4,500，包括指派结算本金 USD -10,000 和卖股"
                    "回款 USD 5,500；收到权利金 USD 800。指派已纳入，但本金和回款是"
                    "现金流，不是利润。"
                )
            ),
        ),
        tool_results={
            "option_performance_report": {
                "ok": True,
                "data": {
                    "period": {
                        "kind": "mtd",
                        "requested_start_date": "2026-07-01",
                        "requested_end_date": "2026-07-23",
                        "status": "partial_current",
                    },
                    "scope": {"account": None, "accounts": ["lx", "sy"]},
                    "activity": {"premium_collected_gross": {"by_currency": {"USD": 800}}},
                    "cash": {
                        "total_cash_change_net": {"by_currency": {"USD": -4500}},
                        "stock_settlement_cash_gross": {"by_currency": {"USD": -10000}},
                        "assigned_stock_sale_cash_gross": {"by_currency": {"USD": 5500}},
                    },
                    "pnl": {
                        "realized_net": {"by_currency": {"USD": 747}},
                        "option_realized_net": {"by_currency": {"USD": 250}},
                        "assigned_stock_realized_net": {"by_currency": {"USD": 497}},
                    },
                },
            }
        },
        expected_tools=("option_performance_report",),
        expected_terms=("结论", "MTD", "全部账户", "已实现", "纯期权", "指派股票", "现金流", "权利金"),
        forbidden_terms=("共 1 行", "分析完成"),
    ),
    Scenario(
        name="option_performance_mtd_correction",
        question="我写的是mtd",
        turns=(
            ModelTurn(
                tool_calls=(
                    _call("option_performance_report", {"period": "mtd"}, "income_correction_1"),
                )
            ),
            ModelTurn(
                text=(
                    "结论：已按 MTD 重查；范围是全部账户（lx、sy）。已实现 PnL 包含"
                    "纯期权与指派股票，现金流另列，指派结算本金不算利润。"
                )
            ),
        ),
        tool_results={
            "option_performance_report": {
                "ok": True,
                "data": {
                    "period": {"kind": "mtd", "status": "partial_current"},
                    "scope": {"account": None, "accounts": ["lx", "sy"]},
                    "cash": {},
                    "pnl": {},
                    "assignment_lifecycle": {},
                },
            }
        },
        expected_tools=("option_performance_report",),
        expected_terms=("结论", "MTD", "全部账户", "已实现 PnL", "纯期权", "指派股票", "现金流"),
        context=(
            {"role": "user", "content": "7月 mtd 的期权收益"},
            {"role": "assistant", "content": "我刚才错误地按自然月解释了。"},
        ),
    ),
    Scenario(
        name="risk_concentration",
        question="当前期权风险主要集中在哪里",
        turns=(
            ModelTurn(
                tool_calls=(
                    _call("option_positions_read", {"action": "list", "status": "open"}, "risk_1"),
                )
            ),
            ModelTurn(text="结论：风险主要集中在 0700.HK 的短 Put，名义担保金额约 269,000 港元；FUTU 次之。"),
        ),
        tool_results={
            "option_positions_read": {
                "ok": True,
                "data": {
                    "rows": [
                        {"symbol": "0700.HK", "strategy": "short_put", "cash_secured": 269000, "currency": "HKD"},
                        {"symbol": "FUTU", "strategy": "short_put", "cash_secured": 47000, "currency": "USD"},
                    ]
                },
            }
        },
        expected_tools=("option_positions_read",),
        expected_terms=("结论", "0700.HK", "269,000", "FUTU"),
    ),
    Scenario(
        name="operation_review",
        question="最近的期权操作有没有不合理，需要优化的地方",
        turns=(
            ModelTurn(
                tool_calls=(
                    _call("option_positions_read", {"action": "events"}, "review_1"),
                    _call("close_advice_read", {"status": "all"}, "review_2"),
                )
            ),
            ModelTurn(text="结论：操作整体有收益，但 0700.HK 连续被指派导致资金集中，应降低单一标的仓位并分散到期日。"),
        ),
        tool_results={
            "option_positions_read": {"ok": True, "data": {"assignment_events": 4, "symbol": "0700.HK"}},
            "close_advice_read": {"ok": True, "data": {"rows": [{"symbol": "0700.HK", "action": "hold"}]}},
        },
        expected_tools=("option_positions_read", "close_advice_read"),
        expected_terms=("结论", "有收益", "资金集中", "降低"),
    ),
    Scenario(
        name="partial_evidence",
        question="总结本月收益和风险",
        turns=(
            ModelTurn(
                tool_calls=(
                    _call("option_performance_report", {"period": "month", "month": "2026-07"}, "partial_1"),
                    _call("option_positions_read", {"action": "list", "status": "open"}, "partial_2"),
                )
            ),
            ModelTurn(text="结论：已确认本月权利金收入 800 美元；持仓数据源暂不可用，因此不能判断当前风险集中度。"),
        ),
        tool_results={
            "option_performance_report": {"ok": True, "data": {"month": "2026-07", "premium": 800, "currency": "USD"}},
            "option_positions_read": {"ok": False, "error": {"code": "READ_ERROR", "message": "position store unavailable"}},
        },
        expected_tools=("option_performance_report", "option_positions_read"),
        expected_terms=("已确认", "800", "暂不可用", "不能判断"),
    ),
    Scenario(
        name="follow_up_conclusion",
        question="结论呢",
        turns=(ModelTurn(text="结论：7月收益为正，主要来自权利金，但风险仍集中在 0700.HK。"),),
        tool_results={},
        expected_tools=(),
        expected_terms=("结论", "7月", "权利金", "0700.HK"),
        context=(
            {"role": "user", "content": "分析7月收益和持仓风险"},
            {"role": "assistant", "content": "我已经读取了收益与持仓数据。"},
        ),
    ),
)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda item: item.name)
def test_freeform_answer_quality_scenarios(monkeypatch, scenario: Scenario) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    requests: list[ModelRequest] = []
    turns: Iterator[ModelTurn] = iter(scenario.turns)

    def call_tool(name: str, payload: dict[str, Any], *, allowed_tools: tuple[str, ...]) -> dict[str, Any]:
        assert name in allowed_tools
        calls.append((name, dict(payload)))
        return dict(scenario.tool_results[name])

    def model(request: ModelRequest) -> ModelTurn:
        requests.append(request)
        return next(turns)

    monkeypatch.setattr(copilot_tools, "call_read_tool", call_tool)
    prepared = prepare_contract(
        CopilotRequest(
            request_id=new_id("eval"),
            source_entry="eval",
            user_message=scenario.question,
            explicit_scope=CopilotScope(config_key="us"),
            context_messages=scenario.context,
            execution_environment="local",
        ),
        reference_year=2026,
    )
    assert not isinstance(prepared, AppResult)

    result = run_contract(prepared, model_runner=model)

    assert result.status == "answered"
    assert tuple(name for name, _payload in calls) == scenario.expected_tools
    for term in scenario.expected_terms:
        assert term in result.user_response
    for term in scenario.forbidden_terms:
        assert term not in result.user_response
    if scenario.context:
        assert list(requests[0].messages[-3:-1]) == list(scenario.context)


def test_freeform_loop_recovers_from_bad_arguments(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def call_tool(name: str, payload: dict[str, Any], *, allowed_tools: tuple[str, ...]) -> dict[str, Any]:
        calls.append(dict(payload))
        if payload.get("month") == "2026-13":
            return {"ok": False, "error": {"code": "INPUT_ERROR", "message": "invalid month"}}
        return {"ok": True, "data": {"month": "2026-07", "premium": 800, "currency": "USD"}}

    turns = iter(
        (
            ModelTurn(tool_calls=(_call("option_performance_report", {"period": "month", "month": "2026-13"}, "retry_1"),)),
            ModelTurn(tool_calls=(_call("option_performance_report", {"period": "month", "month": "2026-07"}, "retry_2"),)),
            ModelTurn(text="结论：修正月份后确认 7月权利金收入为 800 美元。"),
        )
    )
    monkeypatch.setattr(copilot_tools, "call_read_tool", call_tool)

    result = run_contract(_contract("7月收益"), model_runner=lambda _request: next(turns))

    assert [item["month"] for item in calls] == ["2026-13", "2026-07"]
    assert "修正" in result.user_response
    assert "800" in result.user_response


def test_freeform_write_request_cannot_execute(monkeypatch) -> None:
    executed = False

    def call_tool(name: str, payload: dict[str, Any], *, allowed_tools: tuple[str, ...]) -> dict[str, Any]:
        nonlocal executed
        executed = True
        return {"ok": True, "data": {}}

    def model(request: ModelRequest) -> ModelTurn:
        tool_messages = [item for item in request.messages if item.get("role") == "tool"]
        if not tool_messages:
            return ModelTurn(tool_calls=(_call("symbol_config_update", {"symbol": "NVDA"}, "write_1"),))
        error = json.loads(tool_messages[-1]["content"])["error"]
        assert error == "POLICY_ERROR"
        return ModelTurn(text="这个 Copilot 是只读环境，不能修改配置；请使用显式配置命令并完成确认。")

    monkeypatch.setattr(copilot_tools, "call_read_tool", call_tool)
    result = run_contract(_contract("把 NVDA 加到配置里"), model_runner=model)

    assert executed is False
    assert result.status == "answered"
    assert "只读" in result.user_response


def _contract(text: str):
    prepared = prepare_contract(
        CopilotRequest(
            request_id=new_id("eval"),
            source_entry="eval",
            user_message=text,
            explicit_scope=CopilotScope(config_key="us"),
            execution_environment="local",
        ),
        reference_year=2026,
    )
    assert not isinstance(prepared, AppResult)
    return prepared
